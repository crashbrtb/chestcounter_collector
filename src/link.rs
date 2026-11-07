//! Talks to the game over the network, from inside its own tab, without touching the screen.
//!
//! What a request looks like (measured on the live game, 01/11/2026):
//!
//! ```text
//! [u32 LE total][u32 LE inner][[route, seq, [[player_id], session], ""]][objects][tail]
//! ```
//!
//! in the game's little-endian MessagePack, POSTed as raw bytes. The tail holds the
//! cursors of the events the client has seen; the latest one the game sent is reused.
//!
//! Nothing has to be known in advance: the player, the session, the address and the
//! tail are learned from the requests the game itself keeps sending (it asks for
//! something every couple of seconds, on any screen). The requests are sent with a
//! fetch inside the tab, so they leave from where the game's own do. The session
//! stays in memory, in this object: it is never logged and never written anywhere.
//!
//! The Journal (`JournalFeed`) goes through the socket the game keeps open for it:
//!
//! ```text
//! [{2002062134: pack([method, call]) + pack(0)}] + pack(arguments)
//! ```
//!
//! answered by `[{733765024: pack([call, code, text])}]` followed by the result. The
//! socket is taken from the game's next message: WebSocket.prototype.send is wrapped
//! until then and put back at once, so nothing stays installed in the page.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine;
use serde_json::{json, Value as Json};

use crate::arr;
use crate::cdp;
use crate::error::{game, Error, Res};
use crate::modules::chests::gifts::{clan_gifts, Gift, CLAN_ROUTE, OPEN_ROUTE};
use crate::modules::events::journal::JournalCollector;
use crate::modules::members::roster::{parse_profile_row, CLAN_RECORD_WHOLE, PROFILES_PER_REQUEST, PROFILES_ROUTE};
use crate::tbcodec::{self, Frame, LEUnpacker, Value};

const GAME_ADDRESS: &str = "rubens-";
/// The route of the frame the server answers with when it refuses a request.
pub const REFUSED_ROUTE: i128 = 0xFFFF_FFFE;
const SESSION_WAIT: Duration = Duration::from_secs(15);
const ANSWER_WAIT: f64 = 20.0;

pub const CHANNEL: &str = "00000000-0000-4000-8000-000000000004";
pub const LIST_METHOD: i64 = 1778507014;
pub const RANKING_METHOD: i64 = 2663587727;
pub const CALL_KEY: i64 = 2002062134;
pub const ANSWER_KEY: &str = "733765024";
const CALLS_FROM: i128 = 1 << 30;
pub const ENTRIES_PER_PAGE: i64 = 50;
const JOURNAL_ANSWER_WAIT: Duration = Duration::from_secs(30);
const SOCKET_WAIT: f64 = 40.0;
const SOCKET_ATTEMPTS: usize = 3;

// ----------------------------------------------------------------- requests

pub fn build_request(route: i128, seq: i128, auth: &Value, objects: &Value, tail: &[u8]) -> Vec<u8> {
    let mut body = tbcodec::pack(&Value::Arr(vec![Value::Int(route), Value::Int(seq), auth.clone(), Value::str("")]));
    body.extend(tbcodec::pack(objects));
    let inner = 8 + body.len();
    let mut out = Vec::with_capacity(inner + tail.len());
    out.extend(((inner + tail.len()) as u32).to_le_bytes());
    out.extend((inner as u32).to_le_bytes());
    out.extend(body);
    out.extend_from_slice(tail);
    out
}

/// (route, seq, auth, tail) of a request of the game; None for anything else.
pub fn parse_request(data: &[u8]) -> Option<(i128, i128, Value, Vec<u8>)> {
    if data.len() < 8 {
        return None;
    }
    let total = u32::from_le_bytes(data[0..4].try_into().unwrap()) as usize;
    let inner = u32::from_le_bytes(data[4..8].try_into().unwrap()) as usize;
    if total != data.len() || !(8 <= inner && inner <= total) {
        return None;
    }
    let header = LEUnpacker::new(data, 8).read().ok()?;
    let h = header.as_arr()?;
    if h.len() != 4 || !h[2].is_list() {
        return None;
    }
    Some((h[0].py_int()?, h[1].py_int()?, h[2].clone(), data[inner..].to_vec()))
}

/// A request as Chrome reports it: its binary bodies come base64-encoded once more
/// than the field promises, so both forms are tried.
pub fn request_bytes(text: &[u8]) -> Option<(i128, i128, Value, Vec<u8>)> {
    if let Some(found) = parse_request(text) {
        return Some(found);
    }
    if text.is_empty() || !text.iter().all(|b| b.is_ascii_alphanumeric() || *b == b'+' || *b == b'/' || *b == b'=') {
        return None;
    }
    parse_request(&B64.decode(text).ok()?)
}

pub fn build_call(method: i64, call: i128, arguments: &Value) -> Vec<u8> {
    let mut inner = tbcodec::pack(&arr![method, call]);
    inner.extend(tbcodec::pack(&Value::Int(0)));
    let mut out = tbcodec::pack(&Value::Arr(vec![Value::Map(vec![(Value::Int(CALL_KEY as i128), Value::Bin(inner))])]));
    out.extend(tbcodec::pack(arguments));
    out
}

/// (call, code, text, result objects) of a frame answering a call; None for anything else.
pub fn parse_answer(data: &[u8]) -> Option<(Value, Value, Value, Vec<Value>)> {
    let stream = tbcodec::unpack_stream(data)?;
    let first = stream.first()?.as_arr()?.first()?;
    let bytes = first.get(ANSWER_KEY)?.as_bin()?;
    if !first.is_map() {
        return None;
    }
    let header = tbcodec::unpack_stream(bytes)?;
    let h = header.first()?.as_arr()?;
    if h.len() < 3 {
        return None;
    }
    Some((h[0].clone(), h[1].clone(), h[2].clone(), stream[1..].to_vec()))
}

// ------------------------------------------------------------- the game API

/// The game, as the modules ask it. The real one is `GameLink`; the tests have stand-ins.
pub trait GameApi: Send {
    fn player_id(&self) -> Option<i128>;
    /// Sends one request and returns the frames answering it.
    fn call(&self, route: i128, objects: Value) -> Res<Vec<Frame>>;
    /// The same, to the game's rating service, where the tournaments' schedule and
    /// rankings are asked (routes 24001 to 24004).
    fn call_rating(&self, route: i128, objects: Value) -> Res<Vec<Frame>> {
        self.call(route, objects)
    }
    /// The same, to another realm's server (`https://game-usNN.totalbattle.com/rubens-realmNNN`).
    fn call_at(&self, _address: &str, route: i128, _objects: Value) -> Res<Vec<Frame>> {
        Err(game(format!("route {} cannot be sent to another realm here", route)))
    }
    /// Clans of other realms the game's own page asked about, and where.
    fn observed_realms(&self) -> HashMap<i128, String> {
        HashMap::new()
    }
}

/// Where the game asks for its tournaments' schedule and rankings (measured 2026-11-07).
pub const RATING_URL: &str = "https://rating-proxy.totalbattle.com/";

pub fn first_rows(frame: &Frame) -> Vec<Value> {
    frame
        .objects
        .first()
        .and_then(|o| if o.truthy() { o.at(0) } else { None })
        .and_then(|r| r.as_arr().cloned())
        .unwrap_or_default()
}

/// The clan of the profile the game is on, from its own profile. None when it has no clan.
pub fn clan_id(api: &dyn GameApi) -> Res<Option<i128>> {
    let player = api.player_id();
    let me = player.map(Value::Int).unwrap_or(Value::Nil);
    for frame in api.call(PROFILES_ROUTE, arr![arr![arr![me]]])? {
        for row in first_rows(&frame) {
            if let Some(member) = parse_profile_row(&row) {
                if Some(member.player_id) == player {
                    return Ok(member.clan_id);
                }
            }
        }
    }
    Ok(None)
}

/// Player id -> the name in the game, as many as it knows, 100 at a time.
pub fn player_names(api: &dyn GameApi, player_ids: &[i128]) -> Res<HashMap<i128, String>> {
    let mut names = HashMap::new();
    for chunk in player_ids.chunks(PROFILES_PER_REQUEST) {
        let ids = Value::Arr(chunk.iter().map(|p| arr![*p]).collect());
        for frame in api.call(PROFILES_ROUTE, arr![ids])? {
            for row in first_rows(&frame) {
                if let Some(member) = parse_profile_row(&row) {
                    names.insert(member.player_id, member.name);
                }
            }
        }
    }
    Ok(names)
}

/// Every gift waiting in the clan, oldest first.
pub fn gifts(api: &dyn GameApi, clan: i128) -> Res<Vec<Gift>> {
    for frame in api.call(CLAN_ROUTE, arr![arr![arr![arr![clan], CLAN_RECORD_WHOLE]]])? {
        if let Some(gifts) = clan_gifts(&Value::Arr(frame.objects.clone())) {
            return Ok(gifts);
        }
    }
    Err(game("the game's answer did not carry the list of gifts"))
}

/// Opens every chest of a gift. Returns once the game has said so.
pub fn open_gift(api: &dyn GameApi, gift: &Gift) -> Res<()> {
    api.call(OPEN_ROUTE, arr![Value::Map(vec![(Value::Int(gift.gift_id), Value::Int(gift.quantity))])]).map(|_| ())
}

/// How long to wait so that requests start at least `interval` seconds apart.
pub fn pace(started_at: Instant, interval: f64) -> f64 {
    (interval - started_at.elapsed().as_secs_f64()).max(0.0)
}

// -------------------------------------------------------------- the listener

/// Where to find the game's tab.
#[derive(Clone, Debug)]
pub struct Target {
    pub host: String,
    pub port: u16,
    pub tab_id: Option<String>,
    pub url_filter: String,
}

type StatusFn = Arc<dyn Fn(&str) + Send + Sync>;

struct LinkState {
    address: Option<String>,
    auth: Option<Value>,
    tail: Vec<u8>,
    seq: i128,
    learned: bool,
    bodies: HashMap<u64, String>,
    answers: HashMap<u64, Sender<Json>>,
    calls: HashMap<i128, Sender<(Value, Value, Value, Vec<Value>)>>,
    /// Clan -> the other realm's server the page asked about it (routes 15000 and 15200).
    realm_of: HashMap<i128, String>,
}

struct Shared {
    running: AtomicBool,
    alive: AtomicBool,
    quit: AtomicBool,
    next_id: AtomicU64,
    state: Mutex<LinkState>,
    learned: Condvar,
    status: Option<StatusFn>,
}

impl Shared {
    fn status(&self, text: &str) {
        if let Some(s) = &self.status {
            s(text);
        }
    }
    fn id(&self) -> u64 {
        self.next_id.fetch_add(1, Ordering::SeqCst) + 1
    }
}

/// A second connection to the game tab, beside the one the browser sends its
/// commands on, that listens to the game's own traffic and asks the game.
pub struct GameLink {
    target: Target,
    shared: Arc<Shared>,
    outbox: Option<Sender<String>>,
    thread: Option<JoinHandle<()>>,
}

impl GameLink {
    pub fn new(target: Target, on_status: Option<StatusFn>) -> Self {
        GameLink {
            target,
            shared: Arc::new(Shared {
                running: AtomicBool::new(false),
                alive: AtomicBool::new(false),
                quit: AtomicBool::new(false),
                next_id: AtomicU64::new(5000),
                state: Mutex::new(LinkState {
                    address: None,
                    auth: None,
                    tail: vec![],
                    seq: 0,
                    learned: false,
                    bodies: HashMap::new(),
                    answers: HashMap::new(),
                    calls: HashMap::new(),
                    realm_of: HashMap::new(),
                }),
                learned: Condvar::new(),
                status: on_status,
            }),
            outbox: None,
            thread: None,
        }
    }

    /// Attaches to the game tab and waits to learn the session. False when it could not.
    pub fn start(&mut self) -> bool {
        if self.shared.running.load(Ordering::SeqCst) {
            return true;
        }
        let tab = cdp::find_game_tab(&self.target.host, self.target.port, self.target.tab_id.as_deref(),
                                     &self.target.url_filter);
        let Some(url) = tab.as_ref().and_then(|t| t["webSocketDebuggerUrl"].as_str()).map(String::from) else {
            self.shared.status(&format!("No Total Battle tab found in the Chrome on port {}.", self.target.port));
            return false;
        };
        let ws = match cdp::connect(&url, Duration::from_secs(10)) {
            Ok(ws) => ws,
            Err(_) => {
                self.shared.status("Could not attach to the game tab to listen to its traffic.");
                return false;
            }
        };
        let (tx, rx) = mpsc::channel::<String>();
        self.shared.running.store(true, Ordering::SeqCst);
        self.shared.alive.store(true, Ordering::SeqCst);
        self.shared.quit.store(false, Ordering::SeqCst);
        let shared = self.shared.clone();
        let first = vec![
            cdp::command(shared.id(), "Target.setAutoAttach",
                         json!({"autoAttach": true, "waitForDebuggerOnStart": false, "flatten": true}), None),
            cdp::command(shared.id(), "Network.enable", json!({"maxPostDataSize": 1 << 20}), None),
        ];
        self.thread = Some(thread::spawn(move || reader(ws, rx, shared, first)));
        self.outbox = Some(tx);

        if self.wait_learned(SESSION_WAIT) {
            return true;
        }
        self.shared.status(&format!("The game sent no request in {}s to learn its session from.", SESSION_WAIT.as_secs()));
        self.stop();
        false
    }

    fn wait_learned(&self, wait: Duration) -> bool {
        let deadline = Instant::now() + wait;
        let mut st = self.shared.state.lock().unwrap();
        while !st.learned {
            let now = Instant::now();
            if now >= deadline || !self.shared.alive.load(Ordering::SeqCst) {
                return st.learned;
            }
            st = self.shared.learned.wait_timeout(st, (deadline - now).min(Duration::from_millis(200))).unwrap().0;
        }
        true
    }

    pub fn stop(&mut self) {
        if !self.shared.running.swap(false, Ordering::SeqCst) {
            return;
        }
        // Give the bodies asked for last a moment to arrive.
        let deadline = Instant::now() + Duration::from_secs(2);
        while Instant::now() < deadline && !self.shared.state.lock().unwrap().bodies.is_empty() {
            thread::sleep(Duration::from_millis(100));
        }
        self.cmd("Network.disable", json!({}), None);
        self.shared.quit.store(true, Ordering::SeqCst);
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
        self.outbox = None;
    }

    fn cmd(&self, method: &str, params: Json, session: Option<&str>) -> Option<u64> {
        let tx = self.outbox.as_ref()?;
        let id = self.shared.id();
        tx.send(cdp::command(id, method, params, session)).ok()?;
        Some(id)
    }

    /// A command to the tab, waited on.
    fn runtime(&self, method: &str, params: Json, timeout: Duration, late: &str) -> Res<Json> {
        if !self.shared.alive.load(Ordering::SeqCst) || self.outbox.is_none() {
            return Err(game("the connection to the game tab failed (closed)"));
        }
        let (tx, rx) = mpsc::channel();
        let id = self.shared.id();
        self.shared.state.lock().unwrap().answers.insert(id, tx);
        let sent = self.outbox.as_ref().unwrap().send(cdp::command(id, method, params, None));
        let result = if sent.is_err() {
            Err(game("the connection to the game tab failed (closed)"))
        } else {
            match rx.recv_timeout(timeout) {
                Ok(v) => Ok(v),
                Err(RecvTimeoutError::Timeout) => Err(game(late)),
                Err(RecvTimeoutError::Disconnected) => Err(game("the connection to the game tab failed (closed)")),
            }
        };
        self.shared.state.lock().unwrap().answers.remove(&id);
        result
    }

    fn fetch(&self, body: &[u8], timeout: f64) -> Res<Vec<u8>> {
        let address = self.shared.state.lock().unwrap().address.clone().unwrap_or_default();
        self.fetch_at(&address, body, timeout)
    }

    fn fetch_at(&self, address: &str, body: &[u8], timeout: f64) -> Res<Vec<u8>> {
        let script = format!(
            "(async()=>{{const r=await fetch({},{{method:'POST',headers:{{'Content-Type':'application/octet-stream'}},\
             signal:AbortSignal.timeout({}),body:Uint8Array.from(atob({}),c=>c.charCodeAt(0))}});\
             if(!r.ok)throw new Error('HTTP '+r.status);\
             const b=new Uint8Array(await r.arrayBuffer());let s='';\
             for(let i=0;i<b.length;i+=8192)s+=String.fromCharCode.apply(null,b.subarray(i,i+8192));\
             return btoa(s);}})()",
            Json::String(address.to_string()),
            (timeout * 1000.0) as i64,
            Json::String(B64.encode(body))
        );
        let result = self.runtime(
            "Runtime.evaluate",
            json!({"expression": script, "awaitPromise": true, "returnByValue": true}),
            Duration::from_secs_f64(timeout + 2.0),
            "the game's server did not answer in time",
        )?;
        let value = result.get("result").and_then(|r| r.get("value")).and_then(|v| v.as_str());
        match value {
            Some(v) if result.get("exceptionDetails").is_none() => {
                B64.decode(v).map_err(|_| game("the request could not be sent from the game tab"))
            }
            _ => Err(game("the request could not be sent from the game tab")),
        }
    }
}

impl GameApi for GameLink {
    fn player_id(&self) -> Option<i128> {
        let st = self.shared.state.lock().unwrap();
        st.auth.as_ref()?.at(0)?.at(0)?.strict_int()
    }

    fn call(&self, route: i128, objects: Value) -> Res<Vec<Frame>> {
        let body = {
            let mut st = self.shared.state.lock().unwrap();
            st.seq += 1;
            let auth = st.auth.clone().unwrap_or(Value::Arr(vec![]));
            build_request(route, st.seq, &auth, &objects, &st.tail)
        };
        let answer = self.fetch(&body, ANSWER_WAIT)?;
        answers_of(route, &answer)
    }

    fn call_rating(&self, route: i128, objects: Value) -> Res<Vec<Frame>> {
        // The rating service takes the same request without the events' cursors.
        let body = {
            let mut st = self.shared.state.lock().unwrap();
            st.seq += 1;
            let auth = st.auth.clone().unwrap_or(Value::Arr(vec![]));
            build_request(route, st.seq, &auth, &objects, &[])
        };
        let answer = self.fetch_at(RATING_URL, &body, ANSWER_WAIT)?;
        answers_of(route, &answer)
    }

    fn observed_realms(&self) -> HashMap<i128, String> {
        self.shared.state.lock().unwrap().realm_of.clone()
    }

    fn call_at(&self, address: &str, route: i128, objects: Value) -> Res<Vec<Frame>> {
        if !address.starts_with("https://") || !address.contains(".totalbattle.com/rubens-") {
            return Err(game(format!("{} is not a server of the game", address)));
        }
        let body = {
            let mut st = self.shared.state.lock().unwrap();
            st.seq += 1;
            let auth = st.auth.clone().unwrap_or(Value::Arr(vec![]));
            build_request(route, st.seq, &auth, &objects, &[])
        };
        let answer = self.fetch_at(address, &body, ANSWER_WAIT)?;
        answers_of(route, &answer)
    }
}

fn answers_of(route: i128, answer: &[u8]) -> Res<Vec<Frame>> {
    {
        let frames = tbcodec::iter_frames(&tbcodec::decompress(answer));
        if let Some(refused) = frames.iter().find(|f| f.route == Some(REFUSED_ROUTE)) {
            let text = tbcodec::py_repr(&Value::Arr(refused.objects.clone()));
            return Err(game(format!("the game refused route {}: {}", route, crate::text::head(&text, 200))));
        }
        let answers: Vec<Frame> = frames.into_iter().filter(|f| f.route == Some(route) && f.error.is_none()).collect();
        if answers.is_empty() {
            return Err(game(format!("the game did not answer route {}", route)));
        }
        Ok(answers)
    }
}

impl Drop for GameLink {
    fn drop(&mut self) {
        self.stop();
    }
}

/// Every id inside a value (players, clans: 9 digits and more).
fn ids_in(v: &Value, out: &mut Vec<i128>) {
    match v {
        Value::Arr(items) => items.iter().for_each(|i| ids_in(i, out)),
        other => {
            if let Some(n) = other.strict_int().filter(|n| *n >= 100_000_000) {
                out.push(n);
            }
        }
    }
}

/// The objects of a request of the game.
fn request_objects(body: &[u8]) -> Option<Value> {
    let data = if parse_request(body).is_some() { body.to_vec() } else { B64.decode(body).ok()? };
    let mut u = LEUnpacker::new(&data, 8);
    u.read().ok()?;
    u.read().ok()
}

fn learn(shared: &Shared, url: &str, body: &[u8]) {
    let Some((route, seq, auth, tail)) = request_bytes(body) else { return };
    let mut st = shared.state.lock().unwrap();
    // The page also asks other realms' servers (a clan's name in a ranking): without the
    // events' cursors, to another address. Those only say where a clan is; the session
    // stays the one of this realm.
    if tail.is_empty() && st.address.as_deref().map(|a| a != url).unwrap_or(false) {
        if route == 15000 || route == 15200 {
            let mut ids = Vec::new();
            if let Some(objects) = request_objects(body) {
                ids_in(&objects, &mut ids);
            }
            for id in ids {
                st.realm_of.insert(id, url.to_string());
            }
        }
        return;
    }
    st.seq = st.seq.max(seq);
    st.address = Some(url.to_string());
    st.auth = Some(auth);
    // A batch (route 2) carries its cursors inside; only a plain request has the tail.
    if route != tbcodec::ROUTE_BATCH && !tail.is_empty() {
        st.tail = tail;
    }
    if !st.tail.is_empty() && !st.learned {
        st.learned = true;
        shared.learned.notify_all();
    }
}

fn reader(mut ws: cdp::Ws, rx: Receiver<String>, shared: Arc<Shared>, first: Vec<String>) {
    for msg in first {
        let _ = cdp::send(&mut ws, msg);
    }
    loop {
        while let Ok(msg) = rx.try_recv() {
            if cdp::send(&mut ws, msg).is_err() {
                break;
            }
        }
        if shared.quit.load(Ordering::SeqCst) {
            let _ = ws.close(None);
            let _ = ws.flush();
            break;
        }
        match cdp::recv(&mut ws, Duration::from_millis(25)) {
            Ok(Some(text)) => handle(&mut ws, &shared, &text),
            Ok(None) => {}
            Err(_) => {
                if shared.running.load(Ordering::SeqCst) {
                    shared.status("The connection that listens to the game's traffic dropped.");
                }
                break;
            }
        }
    }
    shared.alive.store(false, Ordering::SeqCst);
    let mut st = shared.state.lock().unwrap();
    st.answers.clear();
    st.calls.clear();
    st.bodies.clear();
    shared.learned.notify_all();
}

fn handle(ws: &mut cdp::Ws, shared: &Shared, text: &str) {
    let head: String = text.chars().take(80).collect();
    if head.contains("\"Network.webSocketFrameReceived\"") {
        if shared.state.lock().unwrap().calls.is_empty() {
            return;
        }
        let Ok(data) = serde_json::from_str::<Json>(text) else { return };
        let response = &data["params"]["response"];
        if response["opcode"].as_i64() != Some(2) {
            return;
        }
        let Ok(bytes) = B64.decode(response["payloadData"].as_str().unwrap_or("")) else { return };
        if let Some(answer) = parse_answer(&bytes) {
            if let Some(call) = answer.0.py_int() {
                if let Some(tx) = shared.state.lock().unwrap().calls.remove(&call) {
                    let _ = tx.send(answer);
                }
            }
        }
        return;
    }
    // The bodies of the game's answers are of no interest to this listener.
    if head.contains("\"Network.dataReceived\"") || head.contains("\"Network.responseReceived\"")
        || head.contains("\"Network.loadingFinished\"") || head.contains("\"Network.webSocketFrameSent\"")
    {
        return;
    }
    let Ok(data) = serde_json::from_str::<Json>(text) else { return };
    let method = data["method"].as_str().unwrap_or("");
    let params = &data["params"];
    let sid = data["sessionId"].as_str();

    if method == "Target.attachedToTarget" {
        if let Some(child) = params["sessionId"].as_str() {
            if ["page", "iframe", "worker"].contains(&params["targetInfo"]["type"].as_str().unwrap_or("")) {
                let _ = cdp::send(ws, cdp::command(shared.id(), "Network.enable", json!({"maxPostDataSize": 1 << 20}), Some(child)));
                let _ = cdp::send(ws, cdp::command(shared.id(), "Runtime.runIfWaitingForDebugger", json!({}), Some(child)));
            }
        }
    } else if method == "Network.requestWillBeSent" {
        let request = &params["request"];
        let url = request["url"].as_str().unwrap_or("");
        if !url.contains(GAME_ADDRESS) || request["method"].as_str() != Some("POST") {
            return;
        }
        let entries = request["postDataEntries"].as_array().cloned().unwrap_or_default();
        if !entries.is_empty() && entries[0]["bytes"].as_str().map(|s| !s.is_empty()).unwrap_or(false) {
            let joined: String = entries.iter().map(|e| e["bytes"].as_str().unwrap_or("")).collect();
            if let Ok(body) = B64.decode(joined) {
                learn(shared, url, &body);
            }
        } else if request["hasPostData"].as_bool() == Some(true) {
            let id = shared.id();
            let cmd = cdp::command(id, "Network.getRequestPostData", json!({"requestId": params["requestId"]}), sid);
            if cdp::send(ws, cmd).is_ok() {
                shared.state.lock().unwrap().bodies.insert(id, url.to_string());
            }
        }
    } else if let Some(id) = data["id"].as_u64() {
        let mut st = shared.state.lock().unwrap();
        if let Some(url) = st.bodies.remove(&id) {
            drop(st);
            if let Some(body) = data["result"]["postData"].as_str() {
                // Chrome hands the bytes over as text, one character per byte.
                let raw: Vec<u8> = body.chars().map(|c| if (c as u32) < 256 { c as u32 as u8 } else { b'?' }).collect();
                learn(shared, &url, &raw);
            }
        } else if let Some(tx) = st.answers.remove(&id) {
            let result = data.get("result").cloned().unwrap_or(json!({}));
            let result = if result.is_null() { json!({}) } else { result };
            let _ = tx.send(result);
        }
    }
}

// --------------------------------------------------------------- the Journal

/// The Journal as the mapper asks it. The real one is `JournalFeed`.
pub trait JournalSource {
    /// The next entries of the Journal, newest first, after `cursor`. Empty at the end.
    fn journal_page(&mut self, cursor: Option<&Value>) -> Res<Vec<Value>>;
    /// The objects answering for one ranking, as the game sends them.
    fn ranking(&mut self, entry_uid: &str, result_uid: &str) -> Res<Vec<Value>>;
    /// Hands the collector the profiles of these players, as many as the game knows.
    fn fetch_profiles(&mut self, player_ids: &[i128], collector: &mut JournalCollector) -> Res<()>;
    /// The clan of the profile the game is on, to find it in the clans' rankings.
    fn clan_id(&mut self) -> Res<Option<i128>> {
        Ok(None)
    }
}

const FIND_SOCKET: &str = "new Promise(done=>{\
const send=WebSocket.prototype.send;\
const end=found=>{WebSocket.prototype.send=send;done(found)};\
WebSocket.prototype.send=function(data){\
if(typeof data!=='string'&&this.url.includes('journal'))end(this);\
return send.apply(this,arguments)};\
setTimeout(()=>end(null),__WAIT__)})";

pub struct JournalFeed {
    pub link: GameLink,
    socket: Option<String>,
    call_number: i128,
}

impl JournalFeed {
    pub fn new(target: Target, on_status: Option<StatusFn>) -> Self {
        JournalFeed { link: GameLink::new(target, on_status), socket: None, call_number: CALLS_FROM }
    }

    pub fn start(&mut self) -> bool {
        self.link.start()
    }

    pub fn stop(&mut self) {
        if self.socket.take().is_some() {
            self.link.cmd("Runtime.releaseObjectGroup", json!({"objectGroup": "journal-feed"}), None);
        }
        self.link.stop();
    }

    fn find_socket(&self) -> Res<String> {
        let wait_ms = (SOCKET_WAIT * 1000.0) as i64 - 2000;
        let expression = FIND_SOCKET.replace("__WAIT__", &wait_ms.to_string());
        for _ in 0..SOCKET_ATTEMPTS {
            let found = self.link.runtime(
                "Runtime.evaluate",
                json!({"expression": expression, "awaitPromise": true, "objectGroup": "journal-feed"}),
                Duration::from_secs_f64(SOCKET_WAIT),
                "the game's tab did not answer in time",
            )?;
            if let Some(id) = found["result"]["objectId"].as_str() {
                return Ok(id.to_string());
            }
        }
        Err(game("the game sent nothing on the Journal's socket"))
    }

    fn call(&mut self, method: i64, arguments: Value) -> Res<Vec<Value>> {
        if self.socket.is_none() {
            self.socket = Some(self.find_socket()?);
        }
        self.call_number += 1;
        let call = self.call_number;
        let (tx, rx) = mpsc::channel();
        self.link.shared.state.lock().unwrap().calls.insert(call, tx);
        let result = (|| {
            let sent = self.link.runtime(
                "Runtime.callFunctionOn",
                json!({"objectId": self.socket, "returnByValue": true,
                       "arguments": [{"value": B64.encode(build_call(method, call, &arguments))}],
                       "functionDeclaration": "function(body){this.send(Uint8Array.from(atob(body),c=>c.charCodeAt(0)));return this.readyState}"}),
                JOURNAL_ANSWER_WAIT,
                "the game's tab did not answer in time",
            )?;
            if sent["result"]["value"].as_i64() != Some(1) {
                // Closed, or gone with a reload: the next call looks for it again.
                self.socket = None;
                return Err(game("the Journal's socket is closed"));
            }
            rx.recv_timeout(JOURNAL_ANSWER_WAIT).map_err(|_| game(format!("the game did not answer call {}", method)))
        })();
        self.link.shared.state.lock().unwrap().calls.remove(&call);
        let (_call, code, text, objects) = result?;
        if code.truthy() {
            let text = match &text {
                Value::Str(s) => s.clone(),
                other => tbcodec::py_repr(other),
            };
            return Err(game(format!("the game refused call {}: {} {}", method, tbcodec::py_repr(&code),
                                    crate::text::head(&text, 200))));
        }
        Ok(objects)
    }
}

impl JournalSource for JournalFeed {
    fn journal_page(&mut self, cursor: Option<&Value>) -> Res<Vec<Value>> {
        let result = self.call(LIST_METHOD, arr![CHANNEL, cursor.cloned().unwrap_or(Value::Nil), 0, false,
                                                  ENTRIES_PER_PAGE, Value::Nil])?;
        Ok(result
            .first()
            .filter(|r| r.truthy())
            .and_then(|r| r.at(0))
            .and_then(|r| r.as_arr().cloned())
            .unwrap_or_default())
    }

    fn ranking(&mut self, entry_uid: &str, result_uid: &str) -> Res<Vec<Value>> {
        self.call(RANKING_METHOD, arr![entry_uid, arr![result_uid]])
    }

    fn fetch_profiles(&mut self, player_ids: &[i128], collector: &mut JournalCollector) -> Res<()> {
        for chunk in player_ids.chunks(PROFILES_PER_REQUEST) {
            let ids = Value::Arr(chunk.iter().map(|p| arr![*p]).collect());
            for frame in self.link.call(PROFILES_ROUTE, arr![ids])? {
                collector.take_profiles(&Value::Arr(frame.objects));
            }
        }
        Ok(())
    }

    fn clan_id(&mut self) -> Res<Option<i128>> {
        clan_id(&self.link)
    }
}

impl Drop for JournalFeed {
    fn drop(&mut self) {
        self.stop();
    }
}

/// A game link that failed to start says why.
pub fn not_started() -> Error {
    game("could not talk to the game")
}

#[cfg(test)]
pub mod tests {
    use super::*;

    pub const ME: i128 = 485331363759;

    pub fn auth() -> Value {
        arr![arr![ME], Value::Bin(b"session-0001".to_vec())]
    }

    #[test]
    fn a_request_is_built_the_way_the_game_builds_it() {
        let objects = arr![Value::Map(vec![(Value::Int(123596), Value::Int(1))])];
        let data = build_request(OPEN_ROUTE, 1336, &auth(), &objects, b"cursors");
        assert_eq!(Some((OPEN_ROUTE, 1336, auth(), b"cursors".to_vec())), parse_request(&data));
        let mut u = LEUnpacker::new(&data, 8);
        u.read().unwrap();
        assert_eq!(Some(&Value::Int(1)), u.read().unwrap().at(0).unwrap().get("123596"));
    }

    #[test]
    fn chrome_reports_the_body_in_either_form() {
        let data = build_request(318, 7, &auth(), &arr![], b"cursors");
        assert_eq!(7, request_bytes(&data).unwrap().1);
        assert_eq!(7, request_bytes(B64.encode(&data).as_bytes()).unwrap().1);
        assert!(request_bytes(b"not a request").is_none());
    }

    #[test]
    fn a_call_is_built_byte_for_byte_as_the_game_builds_it() {
        // A heartbeat recorded from the game on 01/11/2026.
        assert_eq!("9181ce360b5577c40892cefaa920940c0090", hex::encode(build_call(2485168634, 12, &arr![])));
    }

    #[test]
    fn an_answer_is_told_from_everything_else_on_the_socket() {
        let mut answer = tbcodec::pack(&arr![Value::Map(vec![(Value::Int(733765024), Value::Bin(tbcodec::pack(&arr![7, 0, ""])))])]);
        answer.extend(tbcodec::pack(&arr![arr!["pong"]]));
        let (call, code, text, rest) = parse_answer(&answer).unwrap();
        assert_eq!((Value::Int(7), Value::Int(0), Value::str("")), (call, code, text));
        assert_eq!(vec![arr![arr!["pong"]]], rest);
        let mut other = tbcodec::pack(&arr![Value::Map(vec![(Value::Int(5), Value::Bin(tbcodec::pack(&arr![1])))])]);
        other.extend(tbcodec::pack(&arr![]));
        assert!(parse_answer(&other).is_none());
        assert!(parse_answer(b"PONG{}").is_none());
    }
}
