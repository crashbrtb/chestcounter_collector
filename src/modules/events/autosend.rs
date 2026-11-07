//! Automatic search: walk the Journal, then send every new tournament to the site.
//!
//! 1. asks the site how far back to look: the start of the last *complete* search,
//!    or the end of the newest tournament on the site, with a margin of overlap;
//! 2. the mapper walks the Journal back to there (or the days limit);
//! 3. new types go to the site's catalogue, nameless; their rankings wait;
//! 4. sends how the clan did (its place in the clans' ranking and what it received),
//!    before the rankings, so a tournament the site publishes on its own already has
//!    the clan's prizes to split;
//! 5. asks the site which tournaments it has, and sends the rest - the site publishes
//!    the ones whose tournament is set to publish on its own and every player has a
//!    name, and keeps the others as drafts for review;
//! 6. tells the site how the search went. Only a complete one moves the starting
//!    point of the next search forward.
//!
//! Nothing is sent twice: the site keeps one event per result id, a published one
//! is never sent again, and locally an unchanged ranking is skipped (`signature`).

use chrono::{DateTime, Utc};
use serde_json::{json, Map, Value as Json};
use sha1::{Digest, Sha1};

use super::api::{ApiError, Site};
use super::journal::{self, JournalCollector, Tournament};
use super::mapper::{self, JournalMapper, MappedCard, MapperOptions};
use crate::error::Res;
use crate::link::JournalSource;
use crate::tbcodec::{py_json, Value};

pub const CUTOFF_MARGIN: i128 = 2 * 3600;
/// How far back the first search goes on a site that has no clan results yet.
pub const CLAN_BACKFILL_DAYS: i64 = 45;
/// The local history keeps the clans' results under this prefix, apart from the rankings.
const CLAN_PREFIX: &str = "clan:";

fn blocked_reason(reason: &str) -> String {
    match reason {
        "unnamed" => "there are players without a name".into(),
        "unlinked" => "there are players not linked to members".into(),
        "empty" => "the ranking is empty".into(),
        "not_draft" => "the ranking is not a draft".into(),
        "auto_publish_off" => "the tournament is not set to publish on its own".into(),
        "clan_prizes" => "a clan prize chosen to be split did not come with it (the administrators were told)".into(),
        other => other.into(),
    }
}

pub fn finish_text(reason: &str) -> String {
    match reason {
        mapper::FINISH_END => "end of the Journal".into(),
        mapper::FINISH_CUTOFF => "reached the last complete search".into(),
        mapper::FINISH_DAYS => "reached the limit of days to look back".into(),
        mapper::FINISH_MAX_PAGES => "page limit".into(),
        mapper::FINISH_NO_JOURNAL => "the game stopped giving the Journal".into(),
        other => other.into(),
    }
}

#[derive(Debug, Clone, Default)]
pub struct Outcome {
    pub result_uid: String,
    pub name: String,
    pub published: bool,
    pub error: Option<String>,
    /// The site said no for good (409): not an error to retry.
    pub refused: bool,
    pub reply: Map<String, Json>,
}

fn text_of(v: Option<&Json>) -> String {
    match v {
        None | Some(Json::Null) => "None".into(),
        Some(Json::String(s)) => s.clone(),
        Some(other) => other.to_string(),
    }
}

impl Outcome {
    pub fn text(&self) -> String {
        let label = if self.reply.is_empty() {
            self.name.clone()
        } else {
            let name = match self.reply.get("event_name") {
                Some(Json::String(s)) if !s.is_empty() => s.clone(),
                _ => self.name.clone(),
            };
            format!("#{} {}", text_of(self.reply.get("event_number")), name)
        };
        if let Some(e) = &self.error {
            return format!("{}: {}", label, e);
        }
        if self.published {
            return format!("{}: sent and closed on the site", label);
        }
        let why = self.reply.get("publish_blocked").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(blocked_reason);
        format!("{}: sent for review{}", label, why.map(|w| format!(" ({})", w)).unwrap_or_default())
    }
}

#[derive(Debug, Clone, Default)]
pub struct Summary {
    pub started_at: String,
    pub cutoff: Option<String>,
    pub reason: String,
    pub complete: bool,
    pub cards: Vec<MappedCard>,
    pub catalogue: Vec<Json>,
    pub outcomes: Vec<Outcome>,
    pub skipped: usize,
    /// The clans' results sent to the site.
    pub clan_results: usize,
}

impl Summary {
    pub fn opened(&self) -> usize {
        self.cards.iter().filter(|c| c.status == mapper::STATUS_MAPPED).count()
    }
    pub fn unanswered(&self) -> usize {
        self.cards.iter().filter(|c| c.status == mapper::STATUS_NO_ANSWER).count()
    }
    pub fn nameless(&self) -> usize {
        self.cards.iter().filter(|c| c.status == mapper::STATUS_NO_NAME).count()
    }
    pub fn published(&self) -> usize {
        self.outcomes.iter().filter(|o| o.published).count()
    }
    pub fn for_review(&self) -> usize {
        self.outcomes.iter().filter(|o| !o.published && o.error.is_none()).count()
    }
    pub fn failed(&self) -> usize {
        self.outcomes.iter().filter(|o| o.error.is_some() && !o.refused).count()
    }
    pub fn text(&self) -> String {
        let mut out = format!("{} tournament(s) read, {} closed on the site, {} for review, {} already on the site",
                              self.opened(), self.published(), self.for_review(), self.skipped);
        if self.failed() > 0 {
            out.push_str(&format!(", {} with an error", self.failed()));
        }
        if self.nameless() > 0 {
            out.push_str(&format!(", {} waiting for a name", self.nameless()));
        }
        if self.clan_results > 0 {
            out.push_str(&format!(", {} clan result(s) sent", self.clan_results));
        }
        out.push_str(&format!(" · {}", finish_text(&self.reason)));
        out.push_str(if self.complete { " · complete search" } else { " · incomplete search: the next one goes back to the same point" });
        out
    }
}

pub fn iso(seconds: i128) -> String {
    DateTime::from_timestamp(seconds as i64, 0).unwrap_or_default().format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

pub fn parse_iso(text: Option<&str>) -> Option<i128> {
    let t = text?.trim();
    if t.is_empty() {
        return None;
    }
    let t = t.replace('Z', "+00:00");
    if let Ok(d) = DateTime::parse_from_rfc3339(&t) {
        return Some(d.timestamp() as i128);
    }
    for fmt in ["%Y-%m-%dT%H:%M:%S%:z", "%Y-%m-%d %H:%M:%S%:z"] {
        if let Ok(d) = DateTime::parse_from_str(&t, fmt) {
            return Some(d.timestamp() as i128);
        }
    }
    // Without a zone, Python reads it as local time.
    crate::schedule::parse_iso(&t).and_then(|n| {
        use chrono::TimeZone;
        chrono::Local.from_local_datetime(&n).single().map(|d| d.timestamp() as i128)
    })
}

/// How far back to look: the last complete search, else the newest tournament on the site.
pub fn cutoff_from(state: &Json, margin: i128) -> Option<i128> {
    parse_iso(state.get("covered_until").and_then(|v| v.as_str()))
        .or_else(|| parse_iso(state.get("latest_ended_at").and_then(|v| v.as_str())))
        .map(|m| m - margin)
}

/// What was sent: players, points and names. It changes only when names arrive.
/// The same digest the earlier versions wrote, so their history still holds.
pub fn signature(t: &Tournament) -> String {
    let rows: Vec<Value> = t
        .rows
        .iter()
        .map(|r| Value::Arr(vec![Value::Int(r.player_id), Value::Int(r.points), Value::Str(r.name.clone().unwrap_or_default())]))
        .collect();
    let text = py_json(&Value::Arr(vec![Value::Str(t.result_uid.clone()), Value::Arr(rows)]), true);
    hex::encode(Sha1::digest(text.as_bytes()))
}

/// (send it?, why not).
pub fn should_send(t: &Tournament, local: Option<&Json>, remote: Option<&Json>) -> (bool, String) {
    let truthy = |v: Option<&Json>| match v {
        Some(Json::Bool(b)) => *b,
        Some(Json::Null) | None => false,
        Some(Json::Number(n)) => n.as_f64().map(|f| f != 0.0).unwrap_or(false),
        Some(Json::String(s)) => !s.is_empty(),
        Some(_) => true,
    };
    if let Some(r) = remote {
        if truthy(r.get("published")) {
            return (false, "already published on the site".into());
        }
        if truthy(r.get("cancelled")) {
            return (false, "cancelled on the site".into());
        }
    }
    if let Some(l) = local {
        if truthy(l.get("conflict")) {
            return (false, format!("refused before: {}", text_of(l.get("conflict"))));
        }
        if l.get("signature").and_then(|s| s.as_str()) == Some(signature(t).as_str()) {
            return (false, "already sent, unchanged".into());
        }
    }
    (true, String::new())
}

/// Tournaments with a ranking, oldest first, so the newest is the last one to set
/// who is active in the clan.
pub fn candidates(tournaments: &[Tournament]) -> Vec<Tournament> {
    let mut found: Vec<Tournament> = tournaments.iter().filter(|t| t.has_ranking() && t.tournament_key.is_some()).cloned().collect();
    found.sort_by(|a, b| {
        let ka = a.ended_at.map(|e| e as f64).filter(|e| *e != 0.0).unwrap_or(a.seen_at);
        let kb = b.ended_at.map(|e| e as f64).filter(|e| *e != 0.0).unwrap_or(b.seen_at);
        ka.partial_cmp(&kb).unwrap_or(std::cmp::Ordering::Equal)
    });
    found
}

pub fn now_iso() -> String {
    Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

/// The local history of what was sent to one site.
pub trait History {
    fn info(&self, result_uid: &str) -> Option<Json>;
    fn mark(&mut self, result_uid: &str, info: Json);
}

impl History for super::store::SentHistory {
    fn info(&self, uid: &str) -> Option<Json> {
        super::store::SentHistory::info(self, uid)
    }
    fn mark(&mut self, uid: &str, info: Json) {
        super::store::SentHistory::mark(self, uid, info)
    }
}

impl History for Map<String, Json> {
    fn info(&self, uid: &str) -> Option<Json> {
        self.get(uid).cloned()
    }
    fn mark(&mut self, uid: &str, info: Json) {
        self.insert(uid.to_string(), info);
    }
}

pub type NameFor<'a> = &'a mut dyn FnMut(&Tournament) -> String;
/// Names the items and says what the reward ids give: (item ids, reward ids) -> (names, rewards).
pub type ItemLookup<'a> = &'a mut dyn FnMut(&[i128], &[i128]) -> (super::items::Names, super::items::Rewards);

/// Send how the clan did in every tournament whose clans' card was read and not sent yet.
/// The number sent.
pub fn send_clan_results(client: &dyn Site, cards: &[journal::ClanCard], clan_id: Option<i128>, items: ItemLookup,
                         history: &mut dyn History, log: &mut dyn FnMut(&str)) -> usize {
    let fresh: Vec<&journal::ClanCard> = cards
        .iter()
        .filter(|c| c.complete && c.ended_at.is_some() && history.info(&format!("{}{}", CLAN_PREFIX, c.entry_uid)).is_none())
        .collect();
    if fresh.is_empty() {
        return 0;
    }
    if clan_id.is_none() {
        log("The game did not say which clan this profile is in: the clan's place in the rankings is not known.");
    }
    let mut item_ids: Vec<i128> = Vec::new();
    let mut reward_ids: Vec<i128> = Vec::new();
    for c in &fresh {
        if let Some(r) = &c.received {
            item_ids.extend(r.iter().map(|(i, _)| *i));
        } else if let Some((p, _)) = clan_id.and_then(|id| c.position_of(id)) {
            reward_ids.extend(c.tier_of(p).cloned().unwrap_or_default());
        }
    }
    let (names, rewards) = items(&item_ids, &reward_ids);
    let payloads: Vec<Json> = fresh.iter().map(|c| journal::clan_result_payload(c, clan_id, &names, &rewards)).collect();
    let mut sent = 0;
    for (chunk, cards) in payloads.chunks(50).zip(fresh.chunks(50)) {
        match client.send_clan_results(chunk) {
            Ok(_) => {
                for c in cards {
                    history.mark(&format!("{}{}", CLAN_PREFIX, c.entry_uid), json!({"sent_at": now_iso()}));
                }
                sent += chunk.len();
            }
            Err(ApiError { status: 404, .. }) => {
                log("The site does not take the clan's results yet: update it to keep the clan's history.");
                return sent;
            }
            Err(e) => {
                log(&format!("The clan's results were not sent: {}", e.message));
                return sent;
            }
        }
    }
    for p in &payloads {
        let place = match p["position"].as_u64() {
            Some(n) => format!("#{}", n),
            None => format!("not among the {} clans listed", p["clans_listed"]),
        };
        let prizes: Vec<String> = p["rewards"]
            .as_array()
            .map(|r| r.iter().map(|x| format!("{} x{}", x["name"].as_str().filter(|n| !n.is_empty()).unwrap_or("?"), x["amount"])).collect())
            .unwrap_or_default();
        log(&format!("Clan result {} ({}): {}{}", p["tournament_key"].as_str().unwrap_or("?"),
                     crate::text::head(p["ended_at"].as_str().unwrap_or(""), 10), place,
                     if prizes.is_empty() { String::new() } else { format!(", received {}", prizes.join(", ")) }));
    }
    sent
}

/// Send every tournament the site does not have yet. (outcomes, how many were skipped).
#[allow(clippy::too_many_arguments)]
pub fn send_found(client: &dyn Site, tournaments: &[Tournament], name_for: NameFor, history: &mut dyn History,
                  client_version: &str, log: &mut dyn FnMut(&str)) -> (Vec<Outcome>, usize) {
    let found = candidates(tournaments);
    if found.is_empty() {
        return (vec![], 0);
    }
    let uids: Vec<String> = found.iter().map(|t| t.result_uid.clone()).collect();
    let remote = match client.lookup(&uids) {
        Ok(r) => r,
        Err(e) => {
            log(&format!("Could not ask the site what it has ({}); going by the local history alone.", e.message));
            Map::new()
        }
    };
    let mut outcomes = Vec::new();
    let mut skipped = 0;
    for t in &found {
        let there = remote.get(&t.result_uid);
        let local = history.info(&t.result_uid);
        let (send, _why) = should_send(t, local.as_ref(), there);
        if !send {
            skipped += 1;
            let published_there = there.and_then(|r| r.get("published")).and_then(|p| p.as_bool()).unwrap_or(false);
            let published_here = local.as_ref().and_then(|l| l.get("published")).and_then(|p| p.as_bool()).unwrap_or(false);
            if published_there && !published_here {
                let r = there.unwrap();
                history.mark(&t.result_uid, json!({"event_id": r.get("event_id"), "event_number": r.get("event_number"),
                                                   "published": true, "signature": signature(t), "sent_at": now_iso()}));
            }
            continue;
        }
        let name = name_for(t);
        // Whether the site may publish it on its own (it decides, per tournament): every player has a name,
        // and the end date is known - without it the site would date it "now".
        let close = t.unnamed() == 0 && t.ended_at.is_some();
        let payload = journal::build_payload(t, &name, t.ended_at_iso().as_deref(), client_version, close);
        let label = if !name.is_empty() {
            name.clone()
        } else {
            t.tournament_key.clone().unwrap_or_else(|| crate::text::head(&t.result_uid, 8))
        };
        let mut outcome = Outcome { result_uid: t.result_uid.clone(), name: label, ..Default::default() };
        match client.send_tournament(&payload) {
            Err(ApiError { status, message, .. }) => {
                outcome.error = Some(message.clone());
                if status == 409 {
                    // Published, cancelled, or points different from the game's: sending again gets the same answer.
                    outcome.refused = true;
                    history.mark(&t.result_uid, json!({"conflict": message, "signature": signature(t), "sent_at": now_iso()}));
                }
            }
            Ok(reply) => {
                outcome.reply = reply.as_object().cloned().unwrap_or_default();
                outcome.published = reply.get("published").and_then(|p| p.as_bool()).unwrap_or(false);
                history.mark(&t.result_uid, json!({"event_id": reply.get("event_id"), "event_number": reply.get("event_number"),
                                                   "review_url": reply.get("review_url"), "published": outcome.published,
                                                   "signature": signature(t), "sent_at": now_iso()}));
            }
        }
        log(&outcome.text());
        outcomes.push(outcome);
    }
    (outcomes, skipped)
}

/// One automatic search.
pub struct AutoSearch<'a> {
    pub feed: &'a mut dyn JournalSource,
    pub collector: &'a mut JournalCollector,
    pub client: &'a dyn Site,
    pub options: MapperOptions,
    pub history: &'a mut dyn History,
    pub catalogue_name: NameFor<'a>,
    pub client_version: String,
    /// The clan of the profile, to find it in the clans' rankings.
    pub clan_id: Option<i128>,
    pub items: ItemLookup<'a>,
    pub on_log: &'a mut dyn FnMut(&str),
    pub clock: Box<dyn Fn() -> i128 + 'a>,
    /// Off when the days to look back were raised: the site's last search did not reach that far.
    pub use_site_cutoff: bool,
}

impl<'a> AutoSearch<'a> {
    pub fn run(&mut self) -> Res<Summary> {
        let mut summary = Summary { started_at: iso((self.clock)()), reason: mapper::FINISH_MAX_PAGES.into(), ..Default::default() };
        let state = match self.client.search_state() {
            Ok(s) => s,
            Err(e) => {
                (self.on_log)(&format!("Could not learn how far back to look ({}): the whole Journal will be walked.", e.message));
                json!({})
            }
        };
        if state.get("clan_results").and_then(|v| v.as_i64()) == Some(0) {
            // The site has none of the clan's results yet: the Journal still has the last weeks' ones.
            let days = self.options.max_age_days.unwrap_or(CLAN_BACKFILL_DAYS).max(CLAN_BACKFILL_DAYS);
            (self.on_log)(&format!("The site has no clan results yet: walking back {} day(s) once to fill in the clan's history.", days));
            self.options.max_age_days = Some(days);
            self.use_site_cutoff = false;
        }
        let cutoff = if self.use_site_cutoff { cutoff_from(&state, CUTOFF_MARGIN) } else { None };
        self.options.stop_before = cutoff;
        summary.cutoff = cutoff.map(iso);
        let within = self.options.max_age_days.map(|d| format!(" within the last {} day(s)", d)).unwrap_or_default();
        if let Some(c) = &summary.cutoff {
            (self.on_log)(&format!("Looking for tournaments that ended since {} UTC{}.", crate::text::head(&c.replace('T', " "), 16), within));
        } else if let Some(d) = self.options.max_age_days {
            (self.on_log)(&format!("Walking the Journal back {} day(s).", d));
        } else {
            (self.on_log)("First search: walking the whole Journal.");
        }

        // A cancellation during the walk leaves through here: nothing is sent and nothing is reported.
        let options = self.options.clone();
        let (cards, reason, clan_cards) = {
            let mut m = JournalMapper::new(&mut *self.feed, &mut *self.collector, options, &mut *self.catalogue_name,
                                           &mut *self.on_log);
            let cards = m.run()?;
            (cards, m.finish_reason.clone(), m.clan_cards.clone())
        };
        summary.cards = cards;
        summary.reason = reason;
        summary.clan_results = send_clan_results(self.client, &clan_cards, self.clan_id, &mut *self.items, &mut *self.history,
                                                 &mut *self.on_log);
        self.send(&mut summary);

        summary.complete = mapper::COMPLETE_REASONS.contains(&summary.reason.as_str())
            && summary.unanswered() == 0
            && summary.failed() == 0
            && summary.nameless() == 0;
        if summary.unanswered() > 0 {
            (self.on_log)(&format!("{} tournament(s) without an answer from the game: the next search looks again.", summary.unanswered()));
        }
        self.report(&summary);
        Ok(summary)
    }

    fn send(&mut self, summary: &mut Summary) {
        let entries = mapper::catalogue_entries(&summary.cards);
        if !entries.is_empty() {
            match self.client.send_catalog(&entries) {
                Ok(r) => summary.catalogue = r,
                Err(e) => (self.on_log)(&format!("Catalogue not sent: {}", e.message)),
            }
        }
        let waiting: Vec<&str> =
            summary.cards.iter().filter(|c| c.status == mapper::STATUS_NO_NAME).map(|c| c.result_uid.as_str()).collect();
        let tournaments: Vec<Tournament> =
            self.collector.tournaments().into_iter().filter(|t| !waiting.contains(&t.result_uid.as_str())).collect();
        let (outcomes, skipped) = send_found(self.client, &tournaments, &mut *self.catalogue_name, &mut *self.history,
                                             &self.client_version, &mut *self.on_log);
        summary.outcomes = outcomes;
        summary.skipped = skipped;
        if summary.outcomes.is_empty() {
            (self.on_log)("No new tournament to send.");
        }
    }

    fn report(&mut self, summary: &Summary) {
        let reason = if summary.reason == mapper::FINISH_DAYS { mapper::FINISH_CUTOFF.to_string() } else { summary.reason.clone() };
        let report = json!({
            "started_at": summary.started_at,
            "complete": summary.complete,
            "reason": reason,
            "cutoff": summary.cutoff,
            "cards": summary.cards.len(),
            "sent": summary.outcomes.len() - summary.failed(),
            "published": summary.published(),
            "for_review": summary.for_review(),
            "clan_results": summary.clan_results,
            "errors": summary.failed() + summary.unanswered() + summary.nameless(),
            "client_version": self.client_version,
        });
        if let Err(e) = self.client.report_search(&report) {
            (self.on_log)(&format!("Could not record the search on the site ({}): the next one goes back to the same point.", e.message));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::modules::events::journal::tests::ENDED_AT;
    use crate::modules::events::journal::RankingRow;
    use crate::modules::events::mapper::tests::{ranking_entry, uid, FakeFeed};
    use std::cell::RefCell;

    const HOUR: i128 = 3600;
    const OUR_CLAN: i128 = 936302870583;

    /// The site's side: one event per result id, published ones refused, searches recorded.
    #[derive(Default)]
    struct FakeSite {
        events: RefCell<Map<String, Json>>,
        posts: RefCell<Vec<Json>>,
        catalogue: RefCell<Vec<Json>>,
        reports: RefCell<Vec<Json>>,
        state: RefCell<Json>,
        clan_posts: RefCell<Vec<Json>>,
        /// What was sent, in order: "clan" or "tournament".
        order: RefCell<Vec<&'static str>>,
        no_clan_results: bool,
    }

    impl FakeSite {
        fn new(covered_until: Option<String>, latest: Option<String>) -> Self {
            let s = FakeSite::default();
            *s.state.borrow_mut() = json!({"covered_until": covered_until, "latest_ended_at": latest});
            s
        }
    }

    impl Site for FakeSite {
        fn search_state(&self) -> Result<Json, ApiError> {
            Ok(self.state.borrow().clone())
        }
        fn lookup(&self, uids: &[String]) -> Result<Map<String, Json>, ApiError> {
            Ok(self
                .events
                .borrow()
                .iter()
                .filter(|(u, _)| uids.contains(u))
                .map(|(u, e)| (u.clone(), json!({"event_id": e["event_number"], "event_number": e["event_number"], "published": e["published"]})))
                .collect())
        }
        fn send_catalog(&self, entries: &[Json]) -> Result<Vec<Json>, ApiError> {
            self.catalogue.borrow_mut().extend(entries.iter().cloned());
            Ok(entries.iter().map(|_| json!({"created": true})).collect())
        }
        fn send_clan_results(&self, results: &[Json]) -> Result<Json, ApiError> {
            if self.no_clan_results {
                return Err(ApiError::new(404, "Not Found"));
            }
            self.order.borrow_mut().push("clan");
            self.clan_posts.borrow_mut().extend(results.iter().cloned());
            Ok(json!({"results": results.len()}))
        }
        fn send_tournament(&self, payload: &Json) -> Result<Json, ApiError> {
            self.order.borrow_mut().push("tournament");
            self.posts.borrow_mut().push(payload.clone());
            let uid = payload["result_uid"].as_str().unwrap().to_string();
            let points: Vec<Json> = payload["rows"].as_array().unwrap().iter().map(|r| json!([r["player_id"], r["points"]])).collect();
            let mut events = self.events.borrow_mut();
            if let Some(e) = events.get(&uid) {
                if e["published"] == json!(true) || e["points"] != json!(points) {
                    return Err(ApiError::new(409, if e["published"] == json!(true) { "already published" } else { "points cannot change" }));
                }
            }
            let n = events.len() + 1;
            let event = events.entry(uid).or_insert(json!({"event_number": n, "published": false, "points": points}));
            event["published"] = json!(payload["auto_publish"] == json!(true));
            let published = event["published"] == json!(true);
            Ok(json!({"event_id": event["event_number"], "event_number": event["event_number"], "event_name": payload["name"],
                      "published": published, "publish_blocked": if published { Json::Null } else { json!("unnamed") }}))
        }
        fn report_search(&self, report: &Json) -> Result<Json, ApiError> {
            self.reports.borrow_mut().push(report.clone());
            if report["complete"] == json!(true) {
                self.state.borrow_mut()["covered_until"] = report["started_at"].clone();
            }
            Ok(json!({"recorded": true}))
        }
    }

    fn journal() -> FakeFeed {
        let keys = [(1024, 1), (1007, 3), (1001, 1), (1002, 1), (1003, 1), (1004, 1)];
        FakeFeed::new(keys.iter().enumerate().map(|(i, k)| ranking_entry(i as i128 + 1, ENDED_AT - i as i128 * HOUR, *k, 1)).collect())
    }

    fn search(feed: &mut FakeFeed, site: &FakeSite, sent: &mut Map<String, Json>, known: &mut dyn FnMut(&Tournament) -> String,
              max_age_days: Option<i64>, use_site_cutoff: bool) -> Summary {
        let mut collector = JournalCollector::new(None);
        let mut log = |_t: &str| {};
        let mut items = |_i: &[i128], _r: &[i128]| (super::super::items::Names::new(), super::super::items::Rewards::new());
        let mut s = AutoSearch {
            feed,
            collector: &mut collector,
            client: site,
            options: MapperOptions { interval: 0.0, cards_per_page: 3, max_age_days, ..Default::default() },
            history: sent,
            catalogue_name: known,
            client_version: "test".into(),
            clan_id: Some(OUR_CLAN),
            items: &mut items,
            on_log: &mut log,
            clock: Box::new(|| ENDED_AT + 10 * HOUR),
            use_site_cutoff,
        };
        s.run().unwrap()
    }

    fn epic(_t: &Tournament) -> String {
        "Epic Hunt".into()
    }

    #[test]
    fn looks_back_to_the_last_complete_search_and_closes_what_has_every_name() {
        let site = FakeSite::new(Some(iso(ENDED_AT - 2 * HOUR + CUTOFF_MARGIN)), None);
        let mut sent = Map::new();
        let summary = search(&mut journal(), &site, &mut sent, &mut epic, None, true);
        assert_eq!(vec![uid(5001), uid(5002), uid(5003)], summary.cards.iter().map(|c| c.result_uid.clone()).collect::<Vec<_>>());
        assert_eq!(mapper::FINISH_CUTOFF, summary.reason);
        let posts = site.posts.borrow();
        assert_eq!(vec![uid(5003), uid(5002), uid(5001)], posts.iter().map(|p| p["result_uid"].as_str().unwrap().to_string()).collect::<Vec<_>>());
        assert!(posts.iter().all(|p| p["auto_publish"] == json!(true)));
        assert_eq!("Epic Hunt", posts[0]["name"]);
        assert!(site.catalogue.borrow().is_empty());
        assert!(summary.complete);
        assert_eq!(json!(true), site.reports.borrow()[0]["complete"]);
        assert_eq!(json!(iso(ENDED_AT + 10 * HOUR)), site.state.borrow()["covered_until"]);
    }

    #[test]
    fn someone_without_a_name_goes_to_review() {
        let site = FakeSite::new(None, None);
        let mut feed = FakeFeed::new(vec![ranking_entry(1, ENDED_AT, (1024, 1), 1)]);
        feed.names = std::collections::HashMap::from([(1001, "Lion".to_string())]);
        search(&mut feed, &site, &mut Map::new(), &mut epic, None, true);
        let posts = site.posts.borrow();
        assert_eq!(1, posts.len());
        assert_eq!(json!(false), posts[0]["auto_publish"]);
        assert!(posts[0]["rows"].as_array().unwrap().iter().any(|r| r["name"] == "id:1002"));
    }

    #[test]
    fn a_new_type_goes_to_the_catalogue_nameless_and_its_rankings_wait() {
        let mut known = |t: &Tournament| {
            let ty = t.tournament_key.clone().unwrap_or_default();
            if ["1024", "1007"].contains(&ty.split(':').next().unwrap()) { String::new() } else { "Known".into() }
        };
        let site = FakeSite::new(None, None);
        let mut feed = FakeFeed::new(vec![ranking_entry(1, ENDED_AT, (1024, 1), 1), ranking_entry(2, ENDED_AT - HOUR, (1007, 3), 1),
                                          ranking_entry(3, ENDED_AT - 2 * HOUR, (1001, 1), 1), ranking_entry(4, ENDED_AT - 3 * HOUR, (1007, 4), 1)]);
        let summary = search(&mut feed, &site, &mut Map::new(), &mut known, None, true);
        assert_eq!(vec![uid(5003)], site.posts.borrow().iter().map(|p| p["result_uid"].as_str().unwrap().to_string()).collect::<Vec<_>>());
        assert_eq!(3, summary.nameless());
        assert_eq!(2, site.catalogue.borrow().len());
        assert!(site.catalogue.borrow().iter().all(|e| e.get("name").is_none() && e.get("picture").is_none()));
        assert!(!summary.complete);
        assert_eq!(json!(3), site.reports.borrow()[0]["errors"]);
    }

    #[test]
    fn running_again_sends_nothing_twice() {
        let covered = iso(ENDED_AT - 2 * HOUR + CUTOFF_MARGIN);
        let site = FakeSite::new(Some(covered.clone()), None);
        search(&mut journal(), &site, &mut Map::new(), &mut epic, None, true);
        let first = site.posts.borrow().len();
        // Same Journal, and the local history lost (another computer, or a new install).
        site.state.borrow_mut()["covered_until"] = json!(covered);
        let summary = search(&mut journal(), &site, &mut Map::new(), &mut epic, None, true);
        assert_eq!(first, site.posts.borrow().len());
        assert!(summary.skipped >= 1);
    }

    #[test]
    fn a_draft_is_sent_again_once_its_names_arrived() {
        let mut t = Tournament::new(&uid(5002), Some("1024:1"), Some(ENDED_AT));
        t.rows = vec![RankingRow::new(1, 1001, 10, Some("A")), RankingRow::new(2, 1002, 5, None)];
        let site = FakeSite::new(None, None);
        let mut sent = Map::new();
        let mut log = |_t: &str| {};
        send_found(&site, &[t.clone()], &mut |_t| String::new(), &mut sent, "test", &mut log);
        send_found(&site, &[t.clone()], &mut |_t| String::new(), &mut sent, "test", &mut log);
        assert_eq!(1, site.posts.borrow().len(), "nothing changed: not sent again");
        t.rows[1].name = Some("B".into());
        let (outcomes, _) = send_found(&site, &[t], &mut |_t| String::new(), &mut sent, "test", &mut log);
        assert_eq!(2, site.posts.borrow().len());
        assert!(outcomes[0].published);
    }

    #[test]
    fn the_first_search_without_any_history_walks_the_whole_journal() {
        let summary = search(&mut journal(), &FakeSite::new(None, None), &mut Map::new(), &mut epic, None, true);
        assert!(summary.cutoff.is_none());
        assert_eq!(6, summary.cards.len());
        assert_eq!(mapper::FINISH_END, summary.reason);
        assert!(summary.complete);
    }

    #[test]
    fn a_walk_stopped_by_the_days_limit_is_a_complete_search() {
        let site = FakeSite::new(None, None);
        let now = chrono::Local::now().timestamp() as i128;
        let mut feed = FakeFeed::new(vec![ranking_entry(1, now, (1024, 1), 1), ranking_entry(2, now - 9 * 86400, (1024, 1), 1)]);
        let summary = search(&mut feed, &site, &mut Map::new(), &mut epic, Some(2), true);
        assert_eq!(vec![uid(5001)], summary.cards.iter().map(|c| c.result_uid.clone()).collect::<Vec<_>>());
        assert_eq!(mapper::FINISH_DAYS, summary.reason);
        assert!(summary.complete);
        assert_eq!(json!(mapper::FINISH_CUTOFF), site.reports.borrow()[0]["reason"], "the site only knows 'cutoff'");
    }

    #[test]
    fn the_sites_last_search_can_be_set_aside_and_the_newest_tournament_is_the_fallback() {
        let site = FakeSite::new(Some(iso(ENDED_AT + 5 * HOUR)), None);
        let summary = search(&mut journal(), &site, &mut Map::new(), &mut epic, None, false);
        assert!(summary.cutoff.is_none());
        assert_eq!(6, summary.cards.len());

        let site = FakeSite::new(None, Some(iso(ENDED_AT)));
        let summary = search(&mut journal(), &site, &mut Map::new(), &mut epic, None, true);
        assert_eq!(Some(iso(ENDED_AT - CUTOFF_MARGIN)), summary.cutoff);
        assert_eq!(mapper::FINISH_CUTOFF, summary.reason);
    }

    #[test]
    fn an_unanswered_ranking_does_not_move_the_starting_point() {
        let covered = iso(ENDED_AT - 2 * HOUR + CUTOFF_MARGIN);
        let site = FakeSite::new(Some(covered.clone()), None);
        let mut feed = journal();
        feed.failing.insert(uid(5002));
        let summary = search(&mut feed, &site, &mut Map::new(), &mut epic, None, true);
        assert!(!summary.complete);
        assert_eq!(json!(false), site.reports.borrow()[0]["complete"]);
        assert_eq!(json!(covered), site.state.borrow()["covered_until"]);
    }

    #[test]
    fn a_refused_tournament_is_not_tried_on_every_run() {
        let mut t = Tournament::new(&uid(5001), Some("1024:1"), Some(ENDED_AT));
        t.rows = vec![RankingRow::new(1, 1001, 10, Some("A"))];
        let site = FakeSite::new(None, None);
        site.events.borrow_mut().insert(uid(5001), json!({"event_number": 1, "published": false, "points": [[1001, 99]]}));
        let mut sent = Map::new();
        let mut log = |_t: &str| {};
        let (outcomes, _) = send_found(&site, &[t.clone()], &mut |_t| "A".into(), &mut sent, "test", &mut log);
        assert_eq!(Some("points cannot change".to_string()), outcomes[0].error);
        assert!(outcomes[0].refused);
        assert_eq!("points cannot change", sent[&uid(5001)]["conflict"]);
        let (outcomes, skipped) = send_found(&site, &[t], &mut |_t| "A".into(), &mut sent, "test", &mut log);
        assert!(outcomes.is_empty());
        assert_eq!(1, skipped);
    }

    #[test]
    fn without_the_end_date_the_site_is_not_asked_to_publish() {
        let rows = vec![RankingRow::new(1, 1001, 10, Some("A"))];
        let site = FakeSite::new(None, None);
        let mut sent = Map::new();
        let mut log = |_t: &str| {};
        let mut a = Tournament::new(&uid(5001), Some("1024:1"), None);
        a.rows = rows.clone();
        let mut b = Tournament::new(&uid(5002), Some("1024:1"), Some(ENDED_AT));
        b.rows = rows;
        send_found(&site, &[a], &mut |_t| String::new(), &mut sent, "test", &mut log);
        send_found(&site, &[b], &mut |_t| String::new(), &mut sent, "test", &mut log);
        let posts = site.posts.borrow();
        assert_eq!(json!(false), posts[0]["auto_publish"]);
        assert_eq!(json!(true), posts[1]["auto_publish"], "with every name and the end date the site decides");
    }

    #[test]
    fn the_signature_is_the_one_earlier_versions_wrote() {
        // sha1(json.dumps(["u", [[1, 10, "Andrá"]]])) as Python computes it.
        let mut t = Tournament::new("u", Some("1:1"), None);
        t.rows = vec![RankingRow::new(1, 1, 10, Some("Andrá"))];
        assert_eq!(hex::encode(Sha1::digest(b"[\"u\", [[1, 10, \"Andr\\u00e1\"]]]")), signature(&t));
    }

    #[test]
    fn the_clans_results_go_before_the_rankings_and_only_once() {
        use crate::modules::events::journal::tests::clan_card_entry;
        let mut entries = journal().entries;
        entries.insert(1, clan_card_entry(&uid(77), ENDED_AT - HOUR, (1024, 1), &[(7, 900), (OUR_CLAN, 800)]));
        let site = FakeSite::new(None, None);
        let mut sent = Map::new();
        let summary = search(&mut FakeFeed::new(entries.clone()), &site, &mut sent, &mut epic, None, true);
        assert_eq!(1, summary.clan_results);
        let posts = site.clan_posts.borrow().clone();
        assert_eq!(1, posts.len());
        assert_eq!((json!(2), json!(800), json!(uid(77))), (posts[0]["position"].clone(), posts[0]["points"].clone(), posts[0]["entry_uid"].clone()));
        assert_eq!("clan", site.order.borrow()[0], "before any ranking");
        assert!(site.order.borrow().iter().skip(1).all(|o| *o == "tournament"));
        assert_eq!(json!(1), site.reports.borrow()[0]["clan_results"]);

        let again = search(&mut FakeFeed::new(entries), &site, &mut sent, &mut epic, None, false);
        assert_eq!(0, again.clan_results, "already sent");
        assert_eq!(1, site.clan_posts.borrow().len());
    }

    #[test]
    fn a_site_without_the_clans_results_does_not_stop_the_search() {
        use crate::modules::events::journal::tests::clan_card_entry;
        let mut entries = journal().entries;
        entries.insert(0, clan_card_entry(&uid(77), ENDED_AT, (1024, 1), &[(OUR_CLAN, 800)]));
        let site = FakeSite { no_clan_results: true, ..FakeSite::new(None, None) };
        let mut sent = Map::new();
        let summary = search(&mut FakeFeed::new(entries), &site, &mut sent, &mut epic, None, true);
        assert_eq!(0, summary.clan_results);
        assert!(!site.posts.borrow().is_empty(), "the rankings still go");
        assert!(!sent.contains_key(&format!("clan:{}", uid(77))), "tried again once the site takes them");
    }

    #[test]
    fn a_site_with_no_clan_results_yet_gets_the_last_weeks_once() {
        use crate::modules::events::journal::tests::clan_card_entry;
        // The days limit counts from today: these entries are dated from now.
        let now = chrono::Utc::now().timestamp() as i128;
        let entries = vec![ranking_entry(1, now - HOUR, (1024, 1), 1),
                           clan_card_entry(&uid(78), now - 20 * 24 * HOUR, (1024, 1), &[(OUR_CLAN, 800)])];
        let site = FakeSite::new(Some(iso(now)), None);
        site.state.borrow_mut()["clan_results"] = json!(0);
        let mut sent = Map::new();
        let summary = search(&mut FakeFeed::new(entries.clone()), &site, &mut sent, &mut epic, Some(2), true);
        assert_eq!(1, summary.clan_results, "walked back past the last search and the days limit");

        let site = FakeSite::new(Some(iso(now)), None);
        site.state.borrow_mut()["clan_results"] = json!(3);
        let summary = search(&mut FakeFeed::new(entries), &site, &mut Map::new(), &mut epic, Some(2), true);
        assert_eq!(0, summary.clan_results, "a site that has them is walked as usual");
    }

}
