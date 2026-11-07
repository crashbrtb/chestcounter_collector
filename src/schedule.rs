//! When each module last ran for each profile.
//!
//! A module that runs once a day needs to know whether today's is done - and that
//! has to survive the process, so it is a small file (config/module_state.json).
//! 'Done' means it succeeded: a failed run is tried again on the next start, up to
//! the attempts the module allows per day. A 'day' is the game's, starting at the
//! module's `day_start` (14:00, say): a run at 10:00 still belongs to the day that
//! began at 14:00 the afternoon before.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use chrono::{Duration, NaiveDate, NaiveDateTime, NaiveTime};
use serde_json::{json, Map, Value as Json};

use crate::config::{read_json, write_json_atomic};

pub const SCHEDULE_EVERY_RUN: &str = "every run";
pub const SCHEDULE_DAILY: &str = "once a day";
pub const SCHEDULES: [&str; 2] = [SCHEDULE_EVERY_RUN, SCHEDULE_DAILY];
pub const DEFAULT_DAY_START: &str = "00:00";

/// '14:00' (or '14', '14:00:00') -> 14:00. Anything unreadable is midnight.
pub fn parse_day_start(text: &str) -> NaiveTime {
    let parts: Vec<&str> = text.trim().split(':').collect();
    if parts.is_empty() || parts.len() > 3 || parts.iter().any(|p| p.is_empty() || p.len() > 2 || !p.bytes().all(|b| b.is_ascii_digit())) {
        return NaiveTime::MIN;
    }
    let n: Vec<u32> = parts.iter().map(|p| p.parse().unwrap()).collect();
    let (h, m, s) = (n[0], *n.get(1).unwrap_or(&0), *n.get(2).unwrap_or(&0));
    NaiveTime::from_hms_opt(h, m, s).unwrap_or(NaiveTime::MIN)
}

/// The game day `moment` falls in, for a day that begins at `day_start`.
pub fn game_day(moment: NaiveDateTime, day_start: &str) -> NaiveDate {
    let start = parse_day_start(day_start);
    (moment - (start - NaiveTime::MIN)).date()
}

/// `datetime.fromisoformat` for the forms these files hold.
pub fn parse_iso(text: &str) -> Option<NaiveDateTime> {
    let t = text.trim();
    for fmt in ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S%.f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"] {
        if let Ok(d) = NaiveDateTime::parse_from_str(t, fmt) {
            return Some(d);
        }
    }
    NaiveDate::parse_from_str(t, "%Y-%m-%d").ok().map(|d| d.and_time(NaiveTime::MIN))
}

pub fn iso_seconds(moment: NaiveDateTime) -> String {
    moment.format("%Y-%m-%dT%H:%M:%S").to_string()
}

pub type Clock = Box<dyn Fn() -> NaiveDateTime + Send + Sync>;

pub fn local_now() -> NaiveDateTime {
    chrono::Local::now().naive_local()
}

pub struct ModuleSchedule {
    pub path: PathBuf,
    clock: Clock,
    pub state: Mutex<Map<String, Json>>,
}

impl ModuleSchedule {
    pub fn new(path: &Path) -> Self {
        Self::with_clock(path, Box::new(local_now))
    }

    pub fn with_clock(path: &Path, clock: Clock) -> Self {
        let state = read_json(path)
            .and_then(|v| v.as_object().cloned())
            .map(|m| m.into_iter().filter(|(_, v)| v.is_object()).collect())
            .unwrap_or_default();
        ModuleSchedule { path: path.to_path_buf(), clock, state: Mutex::new(state) }
    }

    fn who(account: &str, profile: &str) -> String {
        format!("{}/{}", account, profile)
    }

    pub fn entry(&self, module_key: &str, account: &str, profile: &str) -> Map<String, Json> {
        let st = self.state.lock().unwrap();
        st.get(module_key)
            .and_then(|m| m.get(Self::who(account, profile)))
            .and_then(|e| e.as_object())
            .cloned()
            .unwrap_or_default()
    }

    pub fn last_success(&self, module_key: &str, account: &str, profile: &str) -> Option<NaiveDateTime> {
        self.entry(module_key, account, profile).get("last_success").and_then(|v| v.as_str()).and_then(parse_iso)
    }

    /// The newest success of a module on any profile, for the interface.
    pub fn last_done(&self, module_key: &str) -> String {
        let st = self.state.lock().unwrap();
        st.get(module_key)
            .and_then(|m| m.as_object())
            .map(|m| {
                m.values()
                    .filter_map(|e| e.get("last_success").and_then(|v| v.as_str()).map(String::from))
                    .max()
                    .unwrap_or_default()
            })
            .unwrap_or_default()
    }

    /// (run it now?, why not).
    pub fn is_due(&self, module_key: &str, schedule: &str, account: &str, profile: &str, daily_attempts: i64,
                  day_start: &str) -> (bool, String) {
        if schedule != SCHEDULE_DAILY {
            return (true, String::new());
        }
        let today = game_day((self.clock)(), day_start);
        if let Some(done) = self.last_success(module_key, account, profile) {
            if game_day(done, day_start) == today {
                return (false, format!("already done today at {}", done.format("%H:%M")));
            }
        }
        let entry = self.entry(module_key, account, profile);
        let attempts = entry.get("attempts").and_then(|v| v.as_i64()).unwrap_or(0);
        if entry.get("attempts_day").and_then(|v| v.as_str()) == Some(&today.format("%Y-%m-%d").to_string())
            && attempts >= daily_attempts
        {
            return (false, format!("failed {} time(s) today; next try when the next day starts at {}", attempts,
                                   parse_day_start(day_start).format("%H:%M")));
        }
        (true, String::new())
    }

    /// Notes one run of a module on a profile, and writes it down at once.
    pub fn record(&self, module_key: &str, account: &str, profile: &str, success: bool, day_start: &str) {
        let now = (self.clock)();
        let today = game_day(now, day_start).format("%Y-%m-%d").to_string();
        let mut st = self.state.lock().unwrap();
        let mut entry = st
            .get(module_key)
            .and_then(|m| m.get(Self::who(account, profile)))
            .and_then(|e| e.as_object())
            .cloned()
            .unwrap_or_default();
        let same_day = entry.get("attempts_day").and_then(|v| v.as_str()) == Some(today.as_str());
        let attempts = if same_day { entry.get("attempts").and_then(|v| v.as_i64()).unwrap_or(0) + 1 } else { 1 };
        entry.insert("attempts".into(), json!(attempts));
        entry.insert("attempts_day".into(), json!(today));
        entry.insert("last_attempt".into(), json!(iso_seconds(now)));
        if success {
            entry.insert("last_success".into(), json!(iso_seconds(now)));
        }
        let module = st.entry(module_key.to_string()).or_insert_with(|| json!({}));
        if !module.is_object() {
            *module = json!({});
        }
        module.as_object_mut().unwrap().insert(Self::who(account, profile), Json::Object(entry));
        let snapshot = Json::Object(st.clone());
        drop(st);
        let _ = write_json_atomic(&self.path, &snapshot, 2);
    }
}

/// A day's worth of seconds, handy for clocks in tests.
pub fn days(n: i64) -> Duration {
    Duration::days(n)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    fn at(text: &str) -> NaiveDateTime {
        parse_iso(text).unwrap()
    }

    fn schedule(dir: &Path, now: Arc<Mutex<NaiveDateTime>>) -> ModuleSchedule {
        ModuleSchedule::with_clock(&dir.join("module_state.json"), Box::new(move || *now.lock().unwrap()))
    }

    #[test]
    fn every_run_is_always_due() {
        let dir = tempfile::tempdir().unwrap();
        let s = ModuleSchedule::new(&dir.path().join("x.json"));
        assert!(s.is_due("chests", SCHEDULE_EVERY_RUN, "A", "B", 3, "00:00").0);
    }

    #[test]
    fn once_a_day_is_due_until_it_succeeds_and_again_tomorrow() {
        let dir = tempfile::tempdir().unwrap();
        let now = Arc::new(Mutex::new(at("2026-11-02T09:00:00")));
        let s = schedule(dir.path(), now.clone());
        assert!(s.is_due("events", SCHEDULE_DAILY, "Main", "Alpha", 3, "00:00").0);
        s.record("events", "Main", "Alpha", true, "00:00");
        let (due, why) = s.is_due("events", SCHEDULE_DAILY, "Main", "Alpha", 3, "00:00");
        assert!(!due);
        assert!(why.contains("already done today at 09:00"));
        assert!(s.is_due("events", SCHEDULE_DAILY, "Main", "Beta", 3, "00:00").0, "each profile has its own");
        *now.lock().unwrap() = at("2026-11-03T00:01:00");
        assert!(s.is_due("events", SCHEDULE_DAILY, "Main", "Alpha", 3, "00:00").0);
        // It survives the process.
        let again = ModuleSchedule::new(&dir.path().join("module_state.json"));
        assert_eq!(Some(at("2026-11-02T09:00:00")), again.last_success("events", "Main", "Alpha"));
    }

    #[test]
    fn a_failed_day_is_retried_a_limited_number_of_times() {
        let dir = tempfile::tempdir().unwrap();
        let now = Arc::new(Mutex::new(at("2026-11-02T09:00:00")));
        let s = schedule(dir.path(), now.clone());
        for _ in 0..2 {
            s.record("events", "Main", "Alpha", false, "00:00");
            assert!(s.is_due("events", SCHEDULE_DAILY, "Main", "Alpha", 3, "00:00").0);
        }
        s.record("events", "Main", "Alpha", false, "00:00");
        let (due, why) = s.is_due("events", SCHEDULE_DAILY, "Main", "Alpha", 3, "00:00");
        assert!(!due);
        assert!(why.contains("failed 3 time(s) today"));
        *now.lock().unwrap() = at("2026-11-03T09:00:00");
        assert!(s.is_due("events", SCHEDULE_DAILY, "Main", "Alpha", 3, "00:00").0);
    }

    #[test]
    fn the_day_turns_at_the_games_day_start() {
        let dir = tempfile::tempdir().unwrap();
        let now = Arc::new(Mutex::new(at("2026-11-02T15:00:00")));
        let s = schedule(dir.path(), now.clone());
        s.record("events", "Main", "Alpha", true, "14:00");
        *now.lock().unwrap() = at("2026-11-03T10:00:00");
        assert!(!s.is_due("events", SCHEDULE_DAILY, "Main", "Alpha", 3, "14:00").0, "still yesterday's game day");
        *now.lock().unwrap() = at("2026-11-03T14:00:00");
        assert!(s.is_due("events", SCHEDULE_DAILY, "Main", "Alpha", 3, "14:00").0);
    }

    #[test]
    fn day_start_parsing() {
        assert_eq!(NaiveTime::from_hms_opt(14, 0, 0).unwrap(), parse_day_start("14:00"));
        assert_eq!(NaiveTime::from_hms_opt(14, 0, 0).unwrap(), parse_day_start("14"));
        assert_eq!(NaiveTime::from_hms_opt(7, 5, 9).unwrap(), parse_day_start(" 7:05:09 "));
        assert_eq!(NaiveTime::MIN, parse_day_start("25:00"));
        assert_eq!(NaiveTime::MIN, parse_day_start("noon"));
        assert_eq!(NaiveTime::MIN, parse_day_start(""));
    }
}
