//! The whole network path against a real Chrome and a simulated game server.
//!
//! A headless Chrome, on a folder of its own, opens a page served here that behaves
//! as the game does: it POSTs its requests to a `rubens-` address every second and
//! keeps a WebSocket to the Journal open. The collector attaches over CDP, learns the
//! session from those requests, and asks the "game" for its clan, gifts, profiles and
//! Journal from inside the tab - exactly as it does with the real one.
//!
//! Run by hand (it opens Chrome): `cargo test --test cdp_game -- --ignored --nocapture`

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine;
use chrono::NaiveDateTime;
use serde_json::json;

use tbc::arr;
use tbc::browser::{Browser, THROTTLING_FLAGS};
use tbc::db::{ChestRepo, GameIds};
use tbc::link::{self, GameApi, GameLink, JournalFeed, JournalSource, Target};
use tbc::modules::chests::from_game::{now_seconds, GameCollection, OverLink};
use tbc::modules::events::journal::{JournalCollector, USER_RESULT};
use tbc::modules::events::mapper::{JournalMapper, MapperOptions, FINISH_END, STATUS_MAPPED};
use tbc::tbcodec::{self, pack, Value};

const ME: i128 = 485331363759;
const CLAN: i128 = 936302870583;
const ANDRA: i128 = 901943467017;
const BONE: i128 = 5512;
const RESULT_UID: &str = "eede1d1c-3f13-481a-819d-51fdcebb1e54";

fn profile_row(id: i128, name: &str) -> Value {
    arr![arr![id], "tb:1", name, "BR", 3, 0, 0, 25, 45, 14, 100, arr![CLAN], 0, "LPL", 0, arr![1, 2, 3], arr![485390815746i64]]
}

struct Game {
    gifts: Vec<Value>,
    opened: Vec<i128>,
    requests: usize,
}

fn answer(game: &Mutex<Game>, body: &[u8]) -> Vec<u8> {
    let Some((route, seq, _auth, _tail)) = link::parse_request(body) else { return vec![] };
    let mut u = tbcodec::LEUnpacker::new(body, 8);
    let _ = u.read();
    let objects = u.read().unwrap_or(Value::Nil);
    let mut g = game.lock().unwrap();
    g.requests += 1;
    let reply: Vec<Value> = match route {
        402 => {
            let rows: Vec<Value> = objects.at(0).and_then(|o| o.as_arr()).cloned().unwrap_or_default().iter()
                .filter_map(|p| p.at(0).and_then(|v| v.strict_int()))
                .map(|id| profile_row(id, if id == ME { "Crash BR" } else { "AndraBianca" }))
                .collect();
            vec![arr![Value::Arr(rows)]]
        }
        15000 => {
            let node = arr![arr![CLAN], arr!["LPL", "LION PRIDE LEGION"], arr![arr![arr![ANDRA], 3, 1]], arr![], Value::Map(vec![]), arr![],
                            100, 1, Value::Bin(b"x".to_vec()), 2, Value::Bin(b"y".to_vec()), 3, Value::Map(vec![]), Value::Arr(g.gifts.clone()), false];
            vec![arr![arr![node]]]
        }
        15013 => {
            let id = objects.at(0).and_then(|m| m.as_map()).and_then(|m| m.first()).and_then(|(k, _)| k.as_str().and_then(|s| s.parse().ok()).or(k.strict_int()));
            if let Some(id) = id {
                g.gifts.retain(|r| r.at(0).and_then(|v| v.strict_int()) != Some(id));
                g.opened.push(id);
            }
            vec![arr![Value::Map(vec![(Value::str("1071"), Value::Int(1))])]]
        }
        _ => vec![arr![0]],
    };
    tbcodec::build_frame(route, seq, &reply, &[])
}

fn page(ws_port: u16) -> String {
    let request = link::build_request(318, 40, &arr![arr![ME], Value::Bin(b"session-0001".to_vec())], &arr![], b"cursors");
    format!(r#"<!doctype html><html><head><title>Total Battle (simulated)</title></head><body><canvas width=50 height=50></canvas>
<script>
const REQ = Uint8Array.from(atob("{}"), c => c.charCodeAt(0));
setInterval(() => fetch('/rubens-realm237', {{method: 'POST', body: REQ, headers: {{'Content-Type': 'application/octet-stream'}}}}), 700);
const ws = new WebSocket('ws://127.0.0.1:{}/journal-feed-kafka');
ws.binaryType = 'arraybuffer';
setInterval(() => {{ if (ws.readyState === 1) ws.send(new Uint8Array([0x91, 0x80])); }}, 700);
</script></body></html>"#, B64.encode(request), ws_port)
}

fn http_server(listener: TcpListener, html: String, game: Arc<Mutex<Game>>) {
    for stream in listener.incoming().flatten() {
        let (html, game) = (html.clone(), game.clone());
        thread::spawn(move || {
            let _ = serve(stream, &html, &game);
        });
    }
}

fn serve(stream: TcpStream, html: &str, game: &Mutex<Game>) -> std::io::Result<()> {
    let mut reader = BufReader::new(stream.try_clone()?);
    let mut line = String::new();
    reader.read_line(&mut line)?;
    let mut length = 0;
    loop {
        let mut h = String::new();
        reader.read_line(&mut h)?;
        if h.trim().is_empty() {
            break;
        }
        if let Some((k, v)) = h.split_once(':') {
            if k.eq_ignore_ascii_case("content-length") {
                length = v.trim().parse().unwrap_or(0);
            }
        }
    }
    let mut body = vec![0; length];
    reader.read_exact(&mut body)?;
    let (kind, out) = if line.starts_with("POST") && line.contains("rubens-") {
        ("application/octet-stream", answer(game, &body))
    } else {
        ("text/html", html.as_bytes().to_vec())
    };
    let mut s = stream;
    write!(s, "HTTP/1.1 200 OK\r\nContent-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", kind, out.len())?;
    s.write_all(&out)
}

fn journal_server(listener: TcpListener) {
    for stream in listener.incoming().flatten() {
        thread::spawn(move || {
            let Ok(mut ws) = tungstenite::accept(stream) else { return };
            while let Ok(msg) = ws.read() {
                let tungstenite::Message::Binary(data) = msg else { continue };
                let Some(stream) = tbcodec::unpack_stream(&data) else { continue };
                let Some(call) = stream.first().and_then(|f| f.at(0)).and_then(|m| m.get(&link::CALL_KEY.to_string())).and_then(|b| b.as_bin())
                else { continue };
                let header = tbcodec::unpack_stream(call).unwrap();
                let (method, number) = (header[0].at(0).unwrap().strict_int().unwrap(), header[0].at(1).unwrap().clone());
                let args = stream.get(1).cloned().unwrap_or(Value::Nil);
                let result = if method == link::LIST_METHOD as i128 {
                    if args.at(1) == Some(&Value::Nil) {
                        let summary = Value::Bin(pack(&arr![USER_RESULT, Value::Nil, arr![1024, 1], 1]));
                        let ended = now_seconds();
                        let entry = arr!["3cd16678-5f7b-44f8-847e-0bee495480d9", ended, 777, USER_RESULT, arr![summary], true, false, false, RESULT_UID];
                        arr![arr![entry]]
                    } else {
                        arr![arr![]]
                    }
                } else {
                    let detail = Value::Bin(pack(&arr![USER_RESULT, arr![1024, 1], 1, arr![arr![1, 1, arr![arr![arr![ANDRA], 500], arr![arr![ME], 400]], arr![], arr![]]]]));
                    arr![Value::Map(vec![(Value::str(RESULT_UID), arr![detail, true])])]
                };
                let mut out = pack(&arr![Value::Map(vec![(Value::Int(733765024), Value::Bin(pack(&arr![number, 0, ""])))])]);
                out.extend(pack(&result));
                if ws.send(tungstenite::Message::Binary(out)).is_err() {
                    return;
                }
            }
        });
    }
}

#[derive(Default)]
struct Repo {
    rows: Vec<(String, String, String, i128)>,
}

impl ChestRepo for Repo {
    fn keep_alive(&mut self) -> Result<(), String> {
        Ok(())
    }
    fn chest_types(&mut self) -> HashMap<i128, (String, String)> {
        HashMap::from([(BONE, ("Bone Chest".into(), "Level 25 Crypt".into()))])
    }
    fn game_players(&mut self) -> HashMap<i128, String> {
        HashMap::new()
    }
    fn chests_of_gifts(&mut self, _a: i128, _b: i128) -> HashMap<i128, i64> {
        HashMap::new()
    }
    fn insert_chest(&mut self, n: &str, p: &str, s: &str, _at: NaiveDateTime, g: GameIds) -> Option<u64> {
        self.rows.push((n.into(), p.into(), s.into(), g.gift));
        Some(self.rows.len() as u64)
    }
    fn insert_incomplete_chest(&mut self, _n: &str, _p: &str, _s: &str, _at: NaiveDateTime, _g: GameIds) -> bool {
        true
    }
    fn learn_chest_type(&mut self, _i: i128, _n: &str, _s: &str) -> bool {
        true
    }
    fn delete_chest(&mut self, _r: u64) -> bool {
        true
    }
}

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port()
}

#[test]
#[ignore]
fn the_game_is_asked_over_the_network_from_inside_its_tab() {
    let http = TcpListener::bind("127.0.0.1:0").unwrap();
    let journal = TcpListener::bind("127.0.0.1:0").unwrap();
    let (http_port, ws_port) = (http.local_addr().unwrap().port(), journal.local_addr().unwrap().port());
    let soon = now_seconds() + 17 * 3600;
    let game = Arc::new(Mutex::new(Game { gifts: vec![arr![10, arr![ANDRA], BONE, soon, 2, 1], arr![11, arr![ANDRA], BONE, soon, 1, 1]],
                                          opened: vec![], requests: 0 }));
    let g = game.clone();
    let html = page(ws_port);
    thread::spawn(move || http_server(http, html, g));
    thread::spawn(move || journal_server(journal));

    // A Chrome on a folder of its own, with the collector's flags, so the browser reuses it.
    let folder = tempfile::tempdir().unwrap();
    let cdp_port = free_port();
    let url = format!("http://127.0.0.1:{}/", http_port);
    let browser_section = json!({"cdp_host": "127.0.0.1", "game_url": url, "game_url_filter": "127.0.0.1", "reuse_existing": true})
        .as_object().unwrap().clone();
    let browser = Browser::new(&browser_section, cdp_port, folder.path());
    let chrome = browser.find_executable().expect("Chrome must be installed for this test");
    let mut args = vec![format!("--remote-debugging-port={}", cdp_port), "--remote-allow-origins=*".into(),
                        format!("--user-data-dir={}", folder.path().display()), "--no-first-run".into(),
                        "--no-default-browser-check".into(), "--headless=new".into()];
    args.extend(THROTTLING_FLAGS.iter().map(|s| s.to_string()));
    args.push(url.clone());
    let mut child = std::process::Command::new(chrome).args(&args).spawn().unwrap();
    for _ in 0..60 {
        if browser.is_cdp_ready() {
            break;
        }
        thread::sleep(Duration::from_millis(250));
    }

    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        // The browser: our folder is recognised, the flags are there, the tab answers.
        browser.start().expect("reuses the Chrome on our folder");
        assert!(browser.wait_for_load(Some(20.0)));
        assert_eq!(Some(json!(2)), browser.evaluate("1+1"));
        assert!(browser.element_rect("canvas").is_some());
        let shot = folder.path().join("shot.png");
        assert!(browser.save_screenshot(&shot));
        assert!(std::fs::read(&shot).unwrap().starts_with(&[0x89, b'P', b'N', b'G']));

        // The game link: the session is learned from the page's own requests.
        let target = Target { host: "127.0.0.1".into(), port: cdp_port, tab_id: browser.tab_id(), url_filter: "127.0.0.1".into() };
        let mut link = GameLink::new(target.clone(), None);
        assert!(link.start(), "the session was learned");
        assert_eq!(Some(ME), link.player_id());
        assert_eq!(Some(CLAN), link::clan_id(&link).unwrap());
        assert_eq!(Some(&"AndraBianca".to_string()), link::player_names(&link, &[ANDRA]).unwrap().get(&ANDRA));
        assert_eq!(vec![10, 11], link::gifts(&link, CLAN).unwrap().iter().map(|g| g.gift_id).collect::<Vec<_>>());

        // A whole collection through it.
        let mut repo = Repo::default();
        let mut over = OverLink(&link);
        let mut collection = GameCollection::new("Test", &mut repo, &mut over, CLAN, 0.2, Box::new(|_| HashMap::new()));
        assert_eq!((3, 0, true), collection.run().unwrap());
        drop(collection);
        assert_eq!(vec![10, 11], game.lock().unwrap().opened);
        assert_eq!(3, repo.rows.len());
        assert_eq!(("Bone Chest", "AndraBianca"), (repo.rows[0].0.as_str(), repo.rows[0].1.as_str()));
        link.stop();

        // The profile the game is on, as the switcher asks it.
        assert_eq!("Crash BR", tbc::session::active_profile_name(&browser));

        // The Journal, through the socket the page keeps open.
        let mut feed = JournalFeed::new(target, None);
        assert!(feed.start());
        let mut collector = JournalCollector::new(None);
        let mut log = |t: &str| println!("  {}", t);
        let mut name = |_t: &tbc::modules::events::journal::Tournament| "Epic Hunt".to_string();
        let mut mapper = JournalMapper::new(&mut feed as &mut dyn JournalSource, &mut collector,
                                            MapperOptions { interval: 0.0, ..Default::default() }, &mut name, &mut log);
        let cards = mapper.run().unwrap();
        assert_eq!(FINISH_END, mapper.finish_reason);
        drop(mapper);
        assert_eq!(1, cards.len());
        assert_eq!(STATUS_MAPPED, cards[0].status);
        let t = &collector.tournaments()[0];
        assert_eq!(vec![(ANDRA, 500, Some("AndraBianca".to_string())), (ME, 400, Some("Crash BR".to_string()))],
                   t.rows.iter().map(|r| (r.player_id, r.points, r.name.clone())).collect::<Vec<_>>());
        feed.stop();

        // Closing: a browser this run did not open is left alone; kill closes it on request.
        browser.close();
        assert!(browser.is_cdp_ready(), "not ours to close");
        browser.kill().unwrap();
        assert!(!browser.is_cdp_ready());
    }));
    let _ = child.kill();
    let _ = child.wait();
    println!("game requests answered: {}", game.lock().unwrap().requests);
    if let Err(e) = outcome {
        std::panic::resume_unwind(e);
    }
}
