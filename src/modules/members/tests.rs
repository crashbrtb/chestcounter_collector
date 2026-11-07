//! The members module: the packets it reads, the files it writes, and a run against a simulated game.

use super::roster::*;
use super::store::*;
use super::*;
use crate::modules::events::api::ApiError;
use crate::tbcodec::Frame;
use chrono::{FixedOffset, TimeZone};
use std::cell::RefCell;
use std::sync::{Arc, Mutex};

const CLAN_ID: i128 = 936302870583;

/// player id -> (name, rank, power, level)
fn players() -> Vec<(i128, &'static str, i128, i128, i128)> {
    vec![(644245120340, "Alok BR", 1, 1660747555, 450), (463856535987, "Ventura", 2, 1874523405, 454),
         (545461058706, "Vida BR", 2, 1601800905, 431), (335007482510, "Vithor", 3, 864258005, 331),
         (597000781537, "Daniela", 5, 242447015, 227)]
}

fn ids() -> Vec<i128> {
    players().iter().map(|p| p.0).collect()
}

fn profile_row(pid: i128, clan: i128) -> Value {
    let (name, power, level) = players().iter().find(|p| p.0 == pid).map(|p| (p.1, p.3, p.4)).unwrap_or(("Stranger", 5, 1));
    arr![arr![pid], "tb:1", name, "BR", 3, 10000054, 10000104, level, 45, 14, power, arr![clan], 1966850, "LPL", 0,
         arr![282, 640, 554], arr![335093746196i64], 26434, 43729967]
}

fn clan_record(members: &[i128]) -> Value {
    let membership: Vec<Value> =
        members.iter().map(|p| arr![arr![*p], players().iter().find(|x| x.0 == *p).unwrap().2, 1790833148i64]).collect();
    arr![arr![CLAN_ID], arr!["LPL", "LION PRIDE LEGION", 1966850, "Bem vindo"], Value::Arr(membership), arr![], Value::Map(vec![]), arr![], 100]
}

fn full() -> RosterCollector {
    let mut c = RosterCollector::default();
    c.feed(CLAN_ROUTE, &[arr![arr![clan_record(&ids())]]]);
    c.feed(PROFILES_ROUTE, &[Value::Arr(vec![Value::Arr(ids().iter().map(|p| profile_row(*p, CLAN_ID)).collect())])]);
    c
}

#[test]
fn a_profile_row_gives_id_name_power_level_and_place() {
    let m = parse_profile_row(&profile_row(644245120340, CLAN_ID)).unwrap();
    assert_eq!((644245120340, "Alok BR", Some(1660747555), Some(450)), (m.player_id, m.name.as_str(), m.power, m.level));
    assert_eq!(Some((282, 640, 554)), m.coordinates);
    assert_eq!(Some(CLAN_ID), m.clan_id);
    assert!(parse_profile_row(&arr![arr![1], "tb:1", ""]).is_none());
    assert!(parse_profile_row(&Value::str("not a row")).is_none());
}

#[test]
fn a_clan_record_counts_only_with_its_membership() {
    let clan = parse_clan_record(&clan_record(&ids())).unwrap();
    assert_eq!((CLAN_ID, "LPL", "LION PRIDE LEGION"), (clan.clan_id, clan.tag.as_str(), clan.name.as_str()));
    assert_eq!(players().iter().map(|p| (p.0, p.2)).collect::<Vec<_>>(), clan.ranks);
    assert!(parse_clan_record(&clan_record(&[])).is_none());
}

#[test]
fn the_roster_is_complete_when_every_member_has_a_profile() {
    let mut c = RosterCollector::default();
    assert!(!c.roster().complete(), "nothing is known before the clan arrives");
    assert!(c.feed(CLAN_ROUTE, &[arr![arr![clan_record(&ids())]]]));
    assert!(c.feed(PROFILES_ROUTE, &[Value::Arr(vec![Value::Arr(ids()[..3].iter().map(|p| profile_row(*p, CLAN_ID)).collect())])]));
    let r = c.roster();
    assert_eq!((5, 3, false), (r.expected(), r.members.len(), r.complete()));
    assert_eq!(vec![335007482510, 597000781537], r.missing);
    c.feed(PROFILES_ROUTE, &[Value::Arr(vec![Value::Arr(ids()[3..].iter().map(|p| profile_row(*p, CLAN_ID)).collect())])]);
    assert!(!c.feed(CLAN_ROUTE, &[arr![arr![clan_record(&[])]]]), "a refresh without members does not undo the membership");
    let r = c.roster();
    assert!(r.complete());
    assert_eq!(vec!["Alok BR", "Ventura", "Vida BR", "Vithor", "Daniela"], r.members.iter().map(|m| m.name.as_str()).collect::<Vec<_>>());
    assert_eq!(vec!["Leader", "Superior", "Superior", "Officer", "Soldier"],
               r.members.iter().map(|m| rank_name(m.rank)).collect::<Vec<_>>());
}

#[test]
fn players_of_other_clans_and_other_answers_are_left_out() {
    let mut c = full();
    c.feed(PROFILES_ROUTE, &[arr![arr![profile_row(999, 5)]]]);
    assert!(!c.feed(318, &[arr![arr![profile_row(998, CLAN_ID)]]]));
    let mut got: Vec<i128> = c.roster().members.iter().map(|m| m.player_id).collect();
    got.sort();
    let mut want = ids();
    want.sort();
    assert_eq!(want, got);
}

#[test]
fn the_roster_is_saved_as_json_and_as_a_spreadsheet() {
    let dir = tempfile::tempdir().unwrap();
    let roster = full().roster();
    let when = crate::schedule::parse_iso("2026-10-11T10:00:00").unwrap();
    let (json_path, csv_path) = save_roster(&roster, when, "Main / Alpha", dir.path()).unwrap();
    assert_eq!(roster_paths(roster.clan.as_ref().unwrap(), dir.path()), (json_path.clone(), csv_path.clone()));
    assert!(json_path.to_string_lossy().ends_with(&format!("LPL_{}.json", CLAN_ID)));
    let data = crate::config::read_json(&json_path).unwrap();
    assert_eq!(json!({"id": CLAN_ID as i64, "tag": "LPL", "name": "LION PRIDE LEGION"}), data["clan"]);
    assert_eq!("2026-10-11T10:00:00", data["read_at"]);
    assert_eq!(json!({"player_id": 644245120340i64, "name": "Alok BR", "power": 1660747555, "rank": 1, "rank_name": "Leader",
                      "level": 450, "kingdom": 282, "x": 640, "y": 554}), data["members"][0]);
    let csv = std::fs::read_to_string(&csv_path).unwrap();
    assert!(csv.starts_with('\u{feff}'));
    let lines: Vec<&str> = csv.trim_start_matches('\u{feff}').lines().collect();
    assert_eq!("player_id,name,power,rank,rank_name,level,kingdom,x,y", lines[0]);
    assert_eq!(6, lines.len());
    assert!(lines[2].starts_with("463856535987,Ventura,1874523405,"));
}

#[test]
fn the_time_of_the_reading_travels_in_utc() {
    let brasilia = FixedOffset::west_opt(3 * 3600).unwrap();
    let at = brasilia.with_ymd_and_hms(2026, 10, 11, 10, 9, 5).unwrap().with_timezone(&chrono::Local);
    assert_eq!("2026-10-11T13:09:05Z", build_payload(&full().roster(), at, CLIENT_VERSION)["read_at"]);
}

// ------------------------------------------------------------- the module

#[derive(Clone)]
struct ClanGame {
    batches: Vec<Vec<i128>>,
    state: Arc<Mutex<(Vec<Vec<i128>>, usize, Vec<Vec<i128>>)>>, // pending, clan requests, profile requests
    sends_membership: bool,
    fails: bool,
}

impl ClanGame {
    fn new(batches: Vec<Vec<i128>>) -> Self {
        ClanGame { batches, state: Arc::new(Mutex::new((vec![], 0, vec![]))), sends_membership: true, fails: false }
    }
}

impl GameApi for ClanGame {
    fn player_id(&self) -> Option<i128> {
        Some(1)
    }
    fn call(&self, route: i128, objects: Value) -> Res<Vec<Frame>> {
        if self.fails {
            return Err(crate::error::game("the game did not answer route 15000"));
        }
        let mut st = self.state.lock().unwrap();
        if route == PROFILES_ROUTE && objects.at(0).and_then(|o| o.at(0)) == Some(&arr![1]) {
            // The player's own profile: the clan.
            return Ok(vec![Frame::of(vec![arr![arr![profile_row(1, CLAN_ID)]]])]);
        }
        if route == CLAN_ROUTE {
            st.1 += 1;
            st.0 = self.batches.clone();
            let members = if self.sends_membership { ids() } else { vec![] };
            return Ok(vec![Frame::of(vec![arr![arr![clan_record(&members)]]])]);
        }
        let asked: Vec<i128> = objects.at(0).unwrap().as_arr().unwrap().iter().map(|p| p.at(0).unwrap().strict_int().unwrap()).collect();
        st.2.push(asked);
        let batch = if st.0.is_empty() { vec![] } else { st.0.remove(0) };
        Ok(vec![Frame::of(vec![Value::Arr(vec![Value::Arr(batch.iter().map(|p| profile_row(*p, CLAN_ID)).collect())])])])
    }
}

fn alpha(site: bool) -> ProfileConfig {
    let mut p = ProfileConfig::named("Alpha", "Main");
    p.modules.insert("members".into(), json!({"enabled": true}));
    if site {
        p.modules.insert("events".into(), json!({"site_url": "https://clan-a.example.com", "api_token": "cct_a"}));
    }
    p
}

fn module(send: bool) -> MembersModule {
    MembersModule { options: json!({"send_to_site": send}).as_object().unwrap().clone() }
}

fn no_site(_s: &str, _t: &str, _p: &Json) -> Result<Json, ApiError> {
    panic!("nothing should be sent")
}

fn saved(dir: &std::path::Path) -> Option<Json> {
    crate::config::read_json(&dir.join(format!("LPL_{}.json", CLAN_ID)))
}

#[test]
fn it_runs_only_where_it_was_turned_on() {
    assert!(configured_for(&alpha(false)));
    assert!(!configured_for(&ProfileConfig::named("Beta", "Main")));
    assert_eq!("members", crate::modules::Kind::Members.profile_summary(&alpha(false)));
}

#[test]
fn the_whole_clan_is_saved_from_the_games_answers() {
    let dir = tempfile::tempdir().unwrap();
    let game = ClanGame::new(vec![ids()]);
    let r = module(true).run_with(Some(Box::new(game.clone())), &alpha(false), dir.path(), &no_site).unwrap();
    assert!(r.success, "{:?}", r);
    assert_eq!((json!(5), json!("LPL"), json!(CLAN_ID as i64)), (r.extra["members"].clone(), r.extra["clan_tag"].clone(), r.extra["clan_id"].clone()));
    assert_eq!(vec![ids()], game.state.lock().unwrap().2);
    let s = saved(dir.path()).unwrap();
    assert_eq!("Main / Alpha", s["read_by"]);
    assert_eq!("Alok BR", s["members"][0]["name"]);
}

#[test]
fn a_list_that_comes_short_is_asked_for_again_and_then_given_up() {
    let dir = tempfile::tempdir().unwrap();
    let game = ClanGame::new(vec![ids()[..4].to_vec()]);
    let r = module(true).run_with(Some(Box::new(game.clone())), &alpha(true), dir.path(), &no_site).unwrap();
    assert_eq!((false, true), (r.success, r.retry));
    assert!(r.reason.contains("4 of 5"));
    assert_eq!(2, game.state.lock().unwrap().1);
    assert!(saved(dir.path()).is_none(), "a roster with members missing is not saved, nor sent");
}

#[test]
fn without_the_membership_nothing_is_saved() {
    let dir = tempfile::tempdir().unwrap();
    let mut game = ClanGame::new(vec![ids()]);
    game.sends_membership = false;
    let r = module(true).run_with(Some(Box::new(game)), &alpha(false), dir.path(), &no_site).unwrap();
    assert_eq!((false, true), (r.success, r.retry));
    assert!(r.reason.contains("membership"));
    assert!(saved(dir.path()).is_none());
}

#[test]
fn a_game_that_cannot_be_asked_is_tried_again_later() {
    let dir = tempfile::tempdir().unwrap();
    let r = module(true).run_with(None, &alpha(false), dir.path(), &no_site).unwrap();
    assert_eq!((false, true), (r.success, r.retry));
    let mut failing = ClanGame::new(vec![]);
    failing.fails = true;
    let r = module(true).run_with(Some(Box::new(failing)), &alpha(false), dir.path(), &no_site).unwrap();
    assert_eq!((false, true), (r.success, r.retry));
}

#[test]
fn a_profile_with_a_clan_site_sends_it_the_whole_list() {
    let dir = tempfile::tempdir().unwrap();
    let sent: RefCell<Vec<(String, String, Json)>> = RefCell::new(vec![]);
    let send = |s: &str, t: &str, p: &Json| -> Result<Json, ApiError> {
        sent.borrow_mut().push((s.into(), t.into(), p.clone()));
        Ok(json!({"members": {"created": 1, "activated": 0, "deactivated": 2, "renamed": 0, "ambiguous": []},
                  "active_members": p["members"].as_array().unwrap().len()}))
    };
    let r = module(true).run_with(Some(Box::new(ClanGame::new(vec![ids()]))), &alpha(true), dir.path(), &send).unwrap();
    assert!(r.success);
    assert_eq!((json!(true), json!(5), json!(2)), (r.extra["sent"].clone(), r.extra["active_on_site"].clone(), r.extra["deactivated"].clone()));
    let calls = sent.borrow();
    assert_eq!(1, calls.len());
    assert_eq!(("https://clan-a.example.com", "cct_a"), (calls[0].0.as_str(), calls[0].1.as_str()));
    let payload = &calls[0].2;
    assert_eq!(json!({"id": CLAN_ID as i64, "tag": "LPL", "name": "LION PRIDE LEGION"}), payload["clan"]);
    assert!(regex::Regex::new(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$").unwrap().is_match(payload["read_at"].as_str().unwrap()));
    assert_eq!(json!({"player_id": 644245120340i64, "name": "Alok BR", "power": 1660747555, "rank": 1, "level": 450}), payload["members"][0]);
}

#[test]
fn a_site_that_refuses_the_list_fails_the_module_but_keeps_the_files() {
    let dir = tempfile::tempdir().unwrap();
    let send = |_s: &str, _t: &str, _p: &Json| -> Result<Json, ApiError> { Err(ApiError::new(409, "The site already has a newer list of the clan.")) };
    let r = module(true).run_with(Some(Box::new(ClanGame::new(vec![ids()]))), &alpha(true), dir.path(), &send).unwrap();
    assert_eq!((false, false), (r.success, r.retry));
    assert!(r.reason.contains("newer list"));
    assert_eq!(5, saved(dir.path()).unwrap()["members"].as_array().unwrap().len());
}

#[test]
fn a_site_from_before_this_api_or_the_switch_off_leaves_only_the_files() {
    let dir = tempfile::tempdir().unwrap();
    let count = RefCell::new(0);
    let send = |_s: &str, _t: &str, _p: &Json| -> Result<Json, ApiError> {
        *count.borrow_mut() += 1;
        Err(ApiError::new(404, "Not Found"))
    };
    let r = module(true).run_with(Some(Box::new(ClanGame::new(vec![ids()]))), &alpha(true), dir.path(), &send).unwrap();
    assert_eq!((true, json!(false)), (r.success, r.extra["sent"].clone()));
    let r = module(false).run_with(Some(Box::new(ClanGame::new(vec![ids()]))), &alpha(true), dir.path(), &send).unwrap();
    assert_eq!((true, json!(false)), (r.success, r.extra["sent"].clone()));
    assert_eq!(1, *count.borrow(), "nothing more was sent");
    let mut half = alpha(false);
    half.modules.insert("events".into(), json!({"site_url": "https://clan-a.example.com"}));
    assert_eq!((String::new(), String::new()), site_of(&half), "a site address without its token is no site at all");
}
