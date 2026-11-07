//! Shield module - keeps the Shield of Peace up on the profiles it is turned on for.
//!
//! It asks the game, over the network, when the shield of the profile's city ends.
//! With less than the configured time left, it buys a shield with gold and
//! activates it. A new shield replaces the current one and lasts 7 days.
//!
//! ```text
//! Read      route 14007   [[city_id]]                   -> [[start, end, active]]
//! Buy       route 1002    [[city_id], 2113, 1, 0, 0]    -> [[[{"3003": 1}, ...]]]
//! Activate  route 14001   [[[3003, 1]]]                 -> [[new_end]]
//! ```
//!
//! The city is field 16 of the player's own profile (route 402). A purchase is
//! never repeated within a run: a shield bought and not activated fails without a retry.

use chrono::TimeZone;
use serde_json::{json, Map, Value as Json};

use crate::arr;
use crate::config::{Field, FieldType, ProfileConfig};
use crate::context::Window;
use crate::error::{game, Error, Res};
use crate::link::GameApi;
use crate::modules::chests::from_game::now_seconds;
use crate::modules::members::roster::PROFILES_ROUTE;
use crate::modules::{opt_bool, opt_f64, Module, ModuleResult};
use crate::tbcodec::Value;

pub const KEY: &str = "shield";
pub const TITLE: &str = "Shield of Peace";
pub const DESCRIPTION: &str = "Asks the game when the city's shield ends and, when it is about to, buys a new one with gold \
                               and activates it. Runs on the profiles it is turned on for (Accounts and profiles > Modules). \
                               A new shield replaces the current one and lasts 7 days. The time between runs must be \
                               shorter than the time set here, or the shield can end before a run sees it.";

pub const READ_ROUTE: i128 = 14007;
pub const BUY_ROUTE: i128 = 1002;
pub const ACTIVATE_ROUTE: i128 = 14001;
pub const SHIELD_PRODUCT: i128 = 2113;
pub const SHIELD_ITEM: i128 = 3003;
const PROFILE_CITY: usize = 16;

pub fn settings() -> Vec<Field> {
    vec![Field::new("renew_below_hours", FieldType::Int, "Renew with less than (hours)", json!(24),
                    "A shield with less than this left is replaced by a new one, bought with gold.")
        .range(1.0, 167.0)]
}

pub fn profile_fields() -> Vec<Field> {
    vec![Field::new("enabled", FieldType::Bool, "Keep the shield up", json!(false),
                    "Buys and activates a shield for this profile's city when the current one runs low. Spends gold.")]
}

pub fn configured_for(profile: &ProfileConfig) -> bool {
    opt_bool(&profile.module_settings(KEY), "enabled", false)
}

/// `[city_id]` at field 16 of a profile row -> city_id.
pub fn parse_profile_city(row: &Value) -> Option<i128> {
    row.at(PROFILE_CITY)?.at(0)?.strict_int()
}

/// (end, active) of an answer of route 14007.
pub fn parse_shield(objects: &[Value]) -> Option<(i128, bool)> {
    let first = objects.first()?.as_arr()?;
    if first.len() != 3 {
        return None;
    }
    match (&first[1], &first[2]) {
        (Value::Int(end), Value::Bool(active)) => Some((*end, *active)),
        _ => None,
    }
}

/// Whether an answer of route 1002 says the item was handed over.
pub fn bought_a_shield(objects: &[Value]) -> bool {
    objects
        .first()
        .and_then(|o| o.at(0))
        .and_then(|o| o.at(0))
        .filter(|m| m.is_map())
        .and_then(|m| m.get(&SHIELD_ITEM.to_string()))
        .and_then(|v| v.py_int())
        .map(|n| n >= 1)
        .unwrap_or(false)
}

/// The new end, in an answer of route 14001.
pub fn parse_activation(objects: &[Value]) -> Option<i128> {
    objects.first()?.at(0)?.strict_int()
}

/// The city of the profile the game is on, from its own profile.
pub fn own_city(link: &dyn GameApi) -> Res<Option<i128>> {
    let player = link.player_id();
    let me = arr![player.map(Value::Int).unwrap_or(Value::Nil)];
    for frame in link.call(PROFILES_ROUTE, arr![arr![me.clone()]])? {
        let rows = frame.objects.first().filter(|o| o.truthy()).and_then(|o| o.at(0)).and_then(|r| r.as_arr().cloned()).unwrap_or_default();
        for row in rows {
            if row.at(0) == Some(&me) {
                return Ok(parse_profile_city(&row));
            }
        }
    }
    Ok(None)
}

pub fn read_shield(link: &dyn GameApi, city: i128) -> Res<(i128, bool)> {
    for frame in link.call(READ_ROUTE, arr![arr![city]])? {
        if let Some(s) = parse_shield(&frame.objects) {
            return Ok(s);
        }
    }
    Err(game("the game's answer did not carry the shield's end"))
}

fn when(end: i128) -> String {
    chrono::Local.timestamp_opt(end as i64, 0).single().map(|d| d.format("%d/%m %H:%M").to_string()).unwrap_or_default()
}

pub fn describe(end: i128, now: i128) -> String {
    format!("{:.1}h left, until {}", (end - now) as f64 / 3600.0, when(end))
}

pub struct ShieldModule {
    pub options: Map<String, Json>,
}

fn failure(profile: &ProfileConfig, reason: &str, retry: bool) -> ModuleResult {
    crate::error!("[{}] Shield: {}", profile.label(), reason);
    ModuleResult::failed(profile, reason, retry)
}

impl ShieldModule {
    pub fn keep_up(&self, link: &dyn GameApi, profile: &ProfileConfig) -> Res<ModuleResult> {
        let read = (|| -> Res<Option<(i128, i128, bool)>> {
            let Some(city) = own_city(link)? else { return Ok(None) };
            let (end, active) = read_shield(link, city)?;
            Ok(Some((city, end, active)))
        })();
        let (city, end, active) = match read {
            Ok(Some(found)) => found,
            Ok(None) => return Ok(failure(profile, "the game's profile did not name the city", true)),
            Err(Error::GameLink(e)) => return Ok(failure(profile, &format!("the game did not say when the shield ends: {}", e), true)),
            Err(e) => return Err(e),
        };
        let mut result = ModuleResult::ok(profile).with("renewed", json!(false)).with("shield_ends", json!(end as i64));
        let now = now_seconds();
        let limit = opt_f64(&self.options, "renew_below_hours", 24.0) * 3600.0;
        if active && (end - now) as f64 >= limit {
            crate::info!("[{}] Shield up, {}: nothing to do.", profile.label(), describe(end, now));
            return Ok(result);
        }
        let state = if active && end > now { format!("Shield running low, {}", describe(end, now)) } else { "No shield up".into() };
        crate::info!("[{}] {}: buying and activating a new one.", profile.label(), state);

        let answers = match link.call(BUY_ROUTE, arr![arr![city], SHIELD_PRODUCT, 0i64 + 1, 0i64, 0i64]) {
            Ok(a) => a,
            Err(Error::GameLink(e)) => {
                return Ok(failure(profile, &format!("the shield could not be bought (is there enough gold?): {}", e), false))
            }
            Err(e) => return Err(e),
        };
        if !answers.iter().any(|f| bought_a_shield(&f.objects)) {
            return Ok(failure(profile, "the game's answer to the purchase did not hand over a shield", false));
        }
        let activated = (|| -> Res<(i128, bool)> {
            link.call(ACTIVATE_ROUTE, arr![arr![arr![SHIELD_ITEM, 1i64]]])?;
            read_shield(link, city)
        })();
        let (new_end, new_active) = match activated {
            Ok(v) => v,
            Err(Error::GameLink(e)) => {
                // It may be the game refusing (a march on its way), and asking again would not change that.
                return Ok(failure(profile, &format!("a shield was bought but not confirmed as activated: {}", e), false));
            }
            Err(e) => return Err(e),
        };
        if !new_active || new_end <= end {
            return Ok(failure(profile, &format!("a shield was bought and activated, but the game still gives the same end ({})",
                                                when(new_end)), false));
        }
        crate::info!("[{}] New shield up, {}.", profile.label(), describe(new_end, now_seconds()));
        result.extra.insert("renewed".into(), json!(true));
        result.extra.insert("shield_ends".into(), json!(new_end as i64));
        Ok(result)
    }

    pub fn run_with(&self, link: Option<Box<dyn GameApi>>, profile: &ProfileConfig) -> Res<ModuleResult> {
        let Some(link) = link else { return Ok(failure(profile, "could not talk to the game", true)) };
        self.keep_up(link.as_ref(), profile)
    }
}

impl Module for ShieldModule {
    fn key(&self) -> &str {
        KEY
    }
    fn title(&self) -> &str {
        TITLE
    }
    fn options(&self) -> &Map<String, Json> {
        &self.options
    }
    fn configured_for(&self, profile: &ProfileConfig) -> bool {
        configured_for(profile)
    }
    fn run_for_profile(&self, window: &dyn Window, profile: &ProfileConfig) -> Res<ModuleResult> {
        self.run_with(window.game_link(), profile)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tbcodec::Frame;
    use std::sync::{Arc, Mutex};

    const PLAYER: i128 = 485331363759;
    const CITY: i128 = 485390815746;
    const HOUR: i128 = 3600;
    const WEEK: i128 = 7 * 24 * HOUR;

    #[derive(Clone)]
    struct ShieldGame {
        state: Arc<Mutex<(i128, bool, Vec<(i128, Value)>)>>,
        refuse: Vec<i128>,
        hands_over: bool,
    }

    impl ShieldGame {
        fn new(left: i128, active: bool) -> Self {
            ShieldGame { state: Arc::new(Mutex::new((now_seconds() + left, active, vec![]))), refuse: vec![], hands_over: true }
        }
        fn routes(&self) -> Vec<i128> {
            self.state.lock().unwrap().2.iter().map(|c| c.0).collect()
        }
        fn end(&self) -> i128 {
            self.state.lock().unwrap().0
        }
    }

    impl GameApi for ShieldGame {
        fn player_id(&self) -> Option<i128> {
            Some(PLAYER)
        }
        fn call(&self, route: i128, objects: Value) -> Res<Vec<Frame>> {
            let mut st = self.state.lock().unwrap();
            st.2.push((route, objects));
            if self.refuse.contains(&route) {
                return Err(game(format!("the game refused route {}", route)));
            }
            let objects = match route {
                PROFILES_ROUTE => {
                    let mut row = vec![arr![PLAYER], Value::str("tb:1"), Value::str("Crash BR")];
                    row.extend((0..13).map(|_| Value::Int(0)));
                    row.push(arr![CITY]);
                    vec![arr![Value::Arr(vec![Value::Arr(row)])]]
                }
                READ_ROUTE => vec![arr![st.0 - WEEK, st.0, st.1]],
                BUY_ROUTE => {
                    let handed = if self.hands_over { Value::Map(vec![(Value::str("3003"), Value::Int(1))]) } else { Value::Map(vec![]) };
                    vec![arr![arr![handed, Value::Map(vec![]), arr![]]]]
                }
                ACTIVATE_ROUTE => {
                    st.0 = now_seconds() + WEEK;
                    st.1 = true;
                    vec![arr![st.0]]
                }
                _ => panic!("unexpected route {}", route),
            };
            Ok(vec![Frame::of(objects)])
        }
    }

    fn module(hours: i64) -> ShieldModule {
        ShieldModule { options: json!({"renew_below_hours": hours}).as_object().unwrap().clone() }
    }

    fn alpha() -> ProfileConfig {
        let mut p = ProfileConfig::named("Alpha", "Main");
        p.modules.insert("shield".into(), json!({"enabled": true}));
        p
    }

    fn run(game: &ShieldGame, hours: i64) -> ModuleResult {
        module(hours).run_with(Some(Box::new(game.clone())), &alpha()).unwrap()
    }

    #[test]
    fn measured_answers() {
        assert_eq!(Some((1793803172, true)), parse_shield(&[arr![1793198372i64, 1793803172i64, true]]));
        for answer in [vec![], vec![arr![]], vec![arr![1, "x", true]], vec![arr![1, 2, 1]]] {
            assert_eq!(None, parse_shield(&answer));
        }
        let bought = vec![arr![arr![Value::Map(vec![(Value::str("3003"), Value::Int(1))]), Value::Map(vec![]), arr![]]]];
        assert!(bought_a_shield(&bought));
        assert_eq!(Some(1794249566), parse_activation(&[arr![1794249566i64]]));
        assert!(!bought_a_shield(&[arr![arr![Value::Map(vec![(Value::str("9"), Value::Int(1))])]]]));
        assert!(!bought_a_shield(&[]));
        let mut row: Vec<Value> = (0..16).map(|_| Value::Int(0)).collect();
        row.push(arr![CITY]);
        assert_eq!(Some(CITY), parse_profile_city(&Value::Arr(row)));
        assert_eq!(None, parse_profile_city(&arr![0, 0, 0]));
    }

    #[test]
    fn it_runs_only_where_it_was_turned_on() {
        assert!(configured_for(&alpha()));
        assert!(!configured_for(&ProfileConfig::named("Beta", "Main")));
    }

    #[test]
    fn a_shield_with_time_left_is_left_alone() {
        let game = ShieldGame::new(25 * HOUR, true);
        let r = run(&game, 24);
        assert!(r.success);
        assert_eq!(json!(false), r.extra["renewed"]);
        assert_eq!(vec![PROFILES_ROUTE, READ_ROUTE], game.routes());
        assert_eq!(arr![arr![CITY]], game.state.lock().unwrap().2[1].1);
    }

    #[test]
    fn a_shield_running_low_is_bought_activated_and_confirmed() {
        let game = ShieldGame::new(23 * HOUR, true);
        let r = run(&game, 24);
        assert!(r.success);
        assert_eq!(json!(true), r.extra["renewed"]);
        assert_eq!(vec![PROFILES_ROUTE, READ_ROUTE, BUY_ROUTE, ACTIVATE_ROUTE, READ_ROUTE], game.routes());
        let calls = game.state.lock().unwrap().2.clone();
        assert_eq!(arr![arr![CITY], 2113, 1, 0, 0], calls[2].1);
        assert_eq!(arr![arr![arr![3003, 1]]], calls[3].1);
        assert_eq!(json!(game.end() as i64), r.extra["shield_ends"]);
    }

    #[test]
    fn the_limit_comes_from_the_settings_and_a_city_without_a_shield_gets_one() {
        assert_eq!(json!(true), run(&ShieldGame::new(47 * HOUR, true), 48).extra["renewed"]);
        assert_eq!(json!(true), run(&ShieldGame::new(-HOUR, false), 24).extra["renewed"]);
    }

    #[test]
    fn nothing_is_bought_when_the_shield_could_not_be_read() {
        let mut game = ShieldGame::new(HOUR, true);
        game.refuse = vec![READ_ROUTE];
        let r = run(&game, 24);
        assert!(!r.success && r.retry);
        assert!(!game.routes().contains(&BUY_ROUTE));
    }

    #[test]
    fn a_refused_purchase_is_not_retried_nor_activated() {
        let mut game = ShieldGame::new(HOUR, true);
        game.refuse = vec![BUY_ROUTE];
        let r = run(&game, 24);
        assert!(!r.success && !r.retry);
        assert!(!game.routes().contains(&ACTIVATE_ROUTE));
    }

    #[test]
    fn a_purchase_that_handed_nothing_over_is_not_activated() {
        let mut game = ShieldGame::new(HOUR, true);
        game.hands_over = false;
        assert!(!run(&game, 24).success);
        assert!(!game.routes().contains(&ACTIVATE_ROUTE));
    }

    #[test]
    fn a_refused_activation_does_not_buy_again() {
        let mut game = ShieldGame::new(HOUR, true);
        game.refuse = vec![ACTIVATE_ROUTE];
        let r = run(&game, 24);
        assert!(!r.success && !r.retry);
        assert_eq!(1, game.routes().iter().filter(|r| **r == BUY_ROUTE).count());
        assert!(r.reason.contains("bought"));
    }

    #[test]
    fn no_link_to_the_game_is_tried_again() {
        let r = module(24).run_with(None, &alpha()).unwrap();
        assert!(!r.success && r.retry);
    }
}
