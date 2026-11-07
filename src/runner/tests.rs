//! The run: which module is done on which profile, when, and what a failure costs.
//! Against stand-in windows and scripted modules: no browser, database or network.

use super::*;
use crate::config::DatabaseConfig;
use crate::link::{GameApi, JournalSource};
use crate::schedule::{parse_iso, SCHEDULE_DAILY};
use chrono::{Duration, NaiveDateTime};
use serde_json::{json, Map, Value as Json};
use std::sync::Barrier;

type Log = Arc<Mutex<Vec<(String, String)>>>;
type Hook = Box<dyn Fn(&dyn Window, &ProfileConfig) + Send + Sync>;

#[derive(Default)]
struct FakeWindow {
    port: u16,
    browser_starts: Mutex<usize>,
    restarts: Mutex<usize>,
    switched: Mutex<Vec<String>>,
    unreachable: AtomicBool,
    active: Mutex<String>,
    login: bool,
    needs_person: bool,
    wrong_window: bool,
}

impl Window for FakeWindow {
    fn port(&self) -> u16 {
        self.port
    }
    fn start_browser(&self) -> Res<()> {
        if self.wrong_window {
            return Err(Error::WrongWindow("port taken".into()));
        }
        *self.browser_starts.lock().unwrap() += 1;
        Ok(())
    }
    fn login_enabled(&self) -> bool {
        self.login
    }
    fn enter_account(&self, _a: &AccountConfig) -> Res<bool> {
        if self.needs_person {
            return Err(Error::NeedsPerson("log in by hand".into()));
        }
        Ok(true)
    }
    fn navigate(&self) -> Res<()> {
        Ok(())
    }
    fn wait_for_load(&self) {}
    fn remember_session(&self) {}
    fn active_among(&self, names: &[String]) -> Res<String> {
        let a = self.active.lock().unwrap().clone();
        Ok(if names.contains(&a) { a } else { String::new() })
    }
    fn switch_to(&self, profile: &ProfileConfig) -> Res<bool> {
        self.switched.lock().unwrap().push(profile.name.clone());
        let ok = !self.unreachable.load(Ordering::SeqCst);
        if ok {
            *self.active.lock().unwrap() = profile.name.clone();
        }
        Ok(ok)
    }
    fn restart_browser(&self) -> Res<()> {
        *self.restarts.lock().unwrap() += 1;
        Ok(())
    }
    fn save_screenshot(&self, _p: &Path) -> bool {
        false
    }
    fn game_link(&self) -> Option<Box<dyn GameApi>> {
        None
    }
    fn journal(&self) -> Option<Box<dyn JournalSource + Send>> {
        None
    }
    fn close(&self) {}
    fn disconnect(&self) {}
}

/// One window for everyone, or one per account.
struct FakeWindows {
    shared: Arc<FakeWindow>,
    per_account: Option<Mutex<HashMap<String, Arc<FakeWindow>>>>,
}

impl FakeWindows {
    fn one() -> Arc<Self> {
        Arc::new(FakeWindows { shared: Arc::new(FakeWindow::default()), per_account: None })
    }
    fn each(config: &ConfigManager) -> Arc<Self> {
        let map = config
            .accounts()
            .iter()
            .map(|a| (a.name.clone(), Arc::new(FakeWindow { port: a.cdp_port, ..Default::default() })))
            .collect();
        Arc::new(FakeWindows { shared: Arc::new(FakeWindow::default()), per_account: Some(Mutex::new(map)) })
    }
    fn window(&self, name: &str) -> Arc<FakeWindow> {
        self.per_account.as_ref().unwrap().lock().unwrap()[name].clone()
    }
    fn set(&self, name: &str, w: FakeWindow) {
        self.per_account.as_ref().unwrap().lock().unwrap().insert(name.into(), Arc::new(w));
    }
}

impl Windows for FakeWindows {
    fn for_account(&self, account: &AccountConfig) -> Arc<dyn Window> {
        match &self.per_account {
            Some(m) => m.lock().unwrap()[&account.name].clone(),
            None => self.shared.clone(),
        }
    }
    fn login_enabled(&self) -> bool {
        false
    }
    fn close(&self) {}
}

enum Answer {
    Result(ModuleResult),
    Crash(Error),
}

struct Scripted {
    key: &'static str,
    title: &'static str,
    options: Map<String, Json>,
    log: Log,
    answers: Mutex<HashMap<String, Vec<Answer>>>,
    began: Mutex<Option<Vec<String>>>,
    ended: Mutex<Option<(Vec<ModuleResult>, Option<String>)>>,
    problems: Vec<String>,
    hook: Option<Hook>,
}

impl Scripted {
    fn new(key: &'static str, config: &ConfigManager, log: &Log) -> Self {
        Scripted { key, title: if key == "chests" { "Clan chests" } else { "Tournament rankings" }, options: config.module(key),
                   log: log.clone(), answers: Mutex::new(HashMap::new()), began: Mutex::new(None), ended: Mutex::new(None),
                   problems: vec![], hook: None }
    }
    fn answer(&self, profile: &str, answers: Vec<Answer>) {
        self.answers.lock().unwrap().insert(profile.into(), answers);
    }
}

fn ok() -> ModuleResult {
    ModuleResult { success: true, ..Default::default() }
}

fn fails(reason: &str, retry: bool) -> ModuleResult {
    ModuleResult { reason: reason.into(), retry, ..Default::default() }
}

impl Module for Scripted {
    fn key(&self) -> &str {
        self.key
    }
    fn title(&self) -> &str {
        self.title
    }
    fn options(&self) -> &Map<String, Json> {
        &self.options
    }
    fn default_day_start(&self) -> &str {
        "00:00"
    }
    fn configured_for(&self, profile: &ProfileConfig) -> bool {
        if self.key == "chests" {
            profile.has_database()
        } else {
            crate::modules::events::configured_for(profile)
        }
    }
    fn preflight(&self, _p: &[ProfileConfig]) -> Vec<String> {
        self.problems.clone()
    }
    fn begin_run(&self, profiles: &[ProfileConfig]) {
        *self.began.lock().unwrap() = Some(profiles.iter().map(|p| p.name.clone()).collect());
    }
    fn end_run(&self, results: &[ModuleResult], _d: &str, error: Option<&str>, _c: bool) {
        *self.ended.lock().unwrap() = Some((results.to_vec(), error.map(String::from)));
    }
    fn run_for_profile(&self, window: &dyn Window, profile: &ProfileConfig) -> Res<ModuleResult> {
        self.log.lock().unwrap().push((self.key.to_string(), profile.name.clone()));
        if let Some(h) = &self.hook {
            h(window, profile);
            return Ok(ModuleResult { success: true, collected: 1, ..Default::default() });
        }
        let mut answers = self.answers.lock().unwrap();
        let queue = answers.entry(profile.name.clone()).or_default();
        let answer = if queue.len() > 1 { queue.remove(0) } else if let Some(a) = queue.first() {
            match a {
                Answer::Result(r) => Answer::Result(r.clone()),
                Answer::Crash(e) => Answer::Crash(e.clone()),
            }
        } else {
            Answer::Result(ok())
        };
        match answer {
            Answer::Result(r) => Ok(r),
            Answer::Crash(e) => Err(e),
        }
    }
}

fn database(name: &str) -> DatabaseConfig {
    DatabaseConfig { host: "127.0.0.1".into(), port: 3306, user: "collector".into(), password: "x".into(), database: name.into() }
}

fn site_a() -> Json {
    json!({"site_url": "https://clan-a.example.com", "api_token": "cct_a"})
}

struct World {
    dir: tempfile::TempDir,
    config: ConfigManager,
    clock: Arc<Mutex<NaiveDateTime>>,
    log: Log,
}

impl World {
    /// One account: Alpha (database and site) and Beta (database only).
    fn new() -> World {
        let dir = tempfile::tempdir().unwrap();
        let mut config = ConfigManager::open(&dir.path().join("config.json")).unwrap();
        let mut alpha = ProfileConfig::named("Alpha", "Main");
        alpha.database = Some(database("clan_a"));
        alpha.modules.insert("events".into(), site_a());
        let mut beta = ProfileConfig::named("Beta", "Main");
        beta.database = Some(database("clan_b"));
        config.set_accounts(&[AccountConfig { name: "Main".into(), login: "me@example.com".into(), password: "pw".into(),
                                              cdp_port: 9400, browser_profile: "Main".into(), profiles: vec![alpha, beta],
                                              ..Default::default() }]);
        config.set("timing", "between_profiles_wait", json!(0));
        config.set("execution", "screenshot_on_error", json!(false));
        World { dir, config, clock: Arc::new(Mutex::new(parse_iso("2026-10-07T03:00:00").unwrap())), log: Arc::new(Mutex::new(vec![])) }
    }

    fn schedule(&self) -> ModuleSchedule {
        let clock = self.clock.clone();
        ModuleSchedule::with_clock(&self.dir.path().join("module_state.json"), Box::new(move || *clock.lock().unwrap()))
    }

    fn runner(&self, windows: Arc<FakeWindows>, modules: Vec<Arc<dyn Module>>) -> ModuleRunner {
        ModuleRunner::new(self.config.clone(), windows, modules, self.schedule(), &self.dir.path().join("run.lock"))
    }

    fn pair(&self) -> (Arc<Scripted>, Arc<Scripted>) {
        (Arc::new(Scripted::new("chests", &self.config, &self.log)), Arc::new(Scripted::new("events", &self.config, &self.log)))
    }

    fn standard(&self) -> (ModuleRunner, Arc<FakeWindows>, Arc<Scripted>, Arc<Scripted>) {
        let windows = FakeWindows::one();
        let (c, e) = self.pair();
        (self.runner(windows.clone(), vec![c.clone(), e.clone()]), windows, c, e)
    }

    fn logged(&self) -> Vec<(String, String)> {
        self.log.lock().unwrap().clone()
    }

    fn clear(&self) {
        self.log.lock().unwrap().clear();
    }
}

fn pairs(list: &[(&str, &str)]) -> Vec<(String, String)> {
    list.iter().map(|(a, b)| (a.to_string(), b.to_string())).collect()
}

#[test]
fn a_run_does_not_start_while_another_holds_the_lock() {
    let w = World::new();
    let other = RunLock::new(&w.dir.path().join("run.lock"));
    assert!(other.acquire());
    let (runner, windows, _, _) = w.standard();
    assert!(matches!(runner.run(None, None, None, false), Err(Error::RunAlreadyActive(_))));
    assert!(w.logged().is_empty());
    assert!(windows.shared.switched.lock().unwrap().is_empty());
    other.release();
    runner.run(None, None, None, false).unwrap();
    assert!(w.logged().contains(&("chests".into(), "Alpha".into())));
}

#[test]
fn each_profile_gets_the_modules_set_up_for_it_chests_first() {
    let w = World::new();
    let (runner, windows, chests, events) = w.standard();
    let summary = runner.run(None, None, None, false).unwrap();
    assert_eq!(pairs(&[("chests", "Alpha"), ("events", "Alpha"), ("chests", "Beta")]), w.logged());
    assert_eq!(vec!["Alpha", "Beta"], *windows.shared.switched.lock().unwrap());
    assert!(summary.failures.is_empty());
    assert_eq!(2, summary.profiles_done);
    assert_eq!(Some(vec!["Alpha".to_string(), "Beta".to_string()]), *chests.began.lock().unwrap());
    assert_eq!(Some(vec!["Alpha".to_string()]), *events.began.lock().unwrap());
    let ended = chests.ended.lock().unwrap().clone().unwrap();
    assert!(ended.0.iter().all(|r| r.module == "chests"));
}

#[test]
fn the_profile_the_game_opened_on_is_done_first() {
    let w = World::new();
    let (runner, windows, _, _) = w.standard();
    *windows.shared.active.lock().unwrap() = "Beta".into();
    runner.run(None, None, None, false).unwrap();
    assert_eq!(pairs(&[("chests", "Beta"), ("chests", "Alpha"), ("events", "Alpha")]), w.logged());
    assert_eq!(vec!["Beta", "Alpha"], *windows.shared.switched.lock().unwrap());
}

#[test]
fn a_profile_the_game_opened_on_with_nothing_due_does_not_change_the_order() {
    let w = World::new();
    let (runner, windows, _, _) = w.standard();
    *windows.shared.active.lock().unwrap() = "Beta".into();
    runner.run(None, None, Some(&["events".to_string()]), false).unwrap();
    assert_eq!(pairs(&[("events", "Alpha")]), w.logged());

    let (runner, windows, _, _) = w.standard();
    *windows.shared.active.lock().unwrap() = "Someone else".into();
    w.clear();
    runner.run(None, None, None, true).unwrap();
    assert_eq!(pairs(&[("chests", "Alpha"), ("events", "Alpha"), ("chests", "Beta")]), w.logged());
}

#[test]
fn the_daily_module_runs_once_a_day_and_the_other_every_time() {
    let mut w = World::new();
    w.config.set("modules.events", "schedule", json!(SCHEDULE_DAILY));
    w.standard().0.run(None, None, None, false).unwrap();

    *w.clock.lock().unwrap() += Duration::hours(2);
    w.clear();
    let (runner, _, _, events) = w.standard();
    runner.run(None, None, None, false).unwrap();
    assert_eq!(pairs(&[("chests", "Alpha"), ("chests", "Beta")]), w.logged());
    assert!(events.began.lock().unwrap().is_none(), "a module with nothing to do is not told a run started");

    w.clear();
    w.standard().0.run(None, None, None, true).unwrap();
    assert!(w.logged().contains(&("events".into(), "Alpha".into())));

    *w.clock.lock().unwrap() += Duration::days(1);
    w.clear();
    w.standard().0.run(None, None, None, false).unwrap();
    assert!(w.logged().contains(&("events".into(), "Alpha".into())));
}

#[test]
fn an_incomplete_daily_run_is_tried_again_on_the_next_start_up_to_the_limit() {
    let mut w = World::new();
    w.config.set("modules.events", "schedule", json!(SCHEDULE_DAILY));
    w.config.set("modules.events", "daily_attempts", json!(2));
    let (runner, _, _, events) = w.standard();
    events.answer("Alpha", vec![Answer::Result(fails("incomplete search", false))]);
    let summary = runner.run(None, None, None, false).unwrap();
    assert_eq!(1, w.logged().iter().filter(|l| **l == ("events".to_string(), "Alpha".to_string())).count(),
               "retry: false is not repeated in the same run");
    assert_eq!(vec!["incomplete search"], summary.failures.iter().map(|f| f.reason.as_str()).collect::<Vec<_>>());

    let (runner, _, _, events) = w.standard();
    events.answer("Alpha", vec![Answer::Result(fails("incomplete search", false))]);
    w.clear();
    runner.run(None, None, None, false).unwrap();
    assert!(w.logged().contains(&("events".into(), "Alpha".into())), "second start of the day: tried again");

    w.clear();
    w.standard().0.run(None, None, None, false).unwrap();
    assert!(!w.logged().contains(&("events".into(), "Alpha".into())), "two failures today: next try tomorrow");
}

#[test]
fn when_nothing_is_due_the_browser_is_not_even_opened() {
    let mut w = World::new();
    w.config.set("modules.chests", "schedule", json!(SCHEDULE_DAILY));
    w.config.set("modules.events", "schedule", json!(SCHEDULE_DAILY));
    let (runner, windows, _, _) = w.standard();
    runner.run(None, None, None, false).unwrap();
    assert_eq!(1, *windows.shared.browser_starts.lock().unwrap());
    w.clear();
    let (runner, windows, _, _) = w.standard();
    let summary = runner.run(None, None, None, false).unwrap();
    assert!(w.logged().is_empty());
    assert_eq!(0, *windows.shared.browser_starts.lock().unwrap());
    assert!(summary.failures.is_empty());
}

#[test]
fn one_module_only_on_request() {
    let w = World::new();
    let (runner, windows, _, _) = w.standard();
    runner.run(None, None, Some(&["events".to_string()]), false).unwrap();
    assert_eq!(pairs(&[("events", "Alpha")]), w.logged());
    assert_eq!(vec!["Alpha"], *windows.shared.switched.lock().unwrap(), "Beta has nothing to do: not switched to");
    assert!(matches!(w.standard().0.run(None, None, Some(&["nope".to_string()]), false), Err(Error::Runner(_))));
}

#[test]
fn a_module_that_fails_is_retried_without_repeating_the_one_that_worked() {
    let w = World::new();
    let (runner, _, chests, _) = w.standard();
    chests.answer("Alpha", vec![Answer::Result(fails("gift menu did not open", true)),
                                Answer::Result(ModuleResult { success: true, collected: 7, ..Default::default() })]);
    let summary = runner.run(None, None, None, false).unwrap();
    assert_eq!(pairs(&[("chests", "Alpha"), ("events", "Alpha"), ("chests", "Alpha"), ("chests", "Beta")]), w.logged());
    assert!(summary.failures.is_empty(), "the last attempt is what counts");
    assert_eq!(7, summary.collected);
}

#[test]
fn a_module_that_crashes_does_not_take_the_next_one_with_it() {
    let w = World::new();
    let (runner, _, chests, events) = w.standard();
    chests.answer("Alpha", vec![Answer::Crash(Error::Other("database exploded".into()))]);
    let summary = runner.run(None, None, None, false).unwrap();
    assert!(w.logged().contains(&("events".into(), "Alpha".into())));
    assert_eq!(2, w.logged().iter().filter(|l| **l == ("chests".to_string(), "Alpha".to_string())).count(), "retries_per_profile");
    assert_eq!(1, summary.failures.len());
    let f = &summary.failures[0];
    assert_eq!(("chests", "Alpha", "all attempts failed"), (f.module.as_str(), f.profile.as_str(), f.reason.as_str()));
    let ended = events.ended.lock().unwrap().clone().unwrap();
    assert_eq!(vec![true], ended.0.iter().map(|r| r.success).collect::<Vec<_>>());
    let mut mine: Vec<bool> = chests.ended.lock().unwrap().clone().unwrap().0.iter().map(|r| r.success).collect();
    mine.sort();
    assert_eq!(vec![false, true], mine);
}

#[test]
fn a_profile_that_cannot_be_reached_fails_every_module_due_on_it() {
    let w = World::new();
    let (runner, windows, _, _) = w.standard();
    windows.shared.unreachable.store(true, Ordering::SeqCst);
    let summary = runner.run(None, None, None, false).unwrap();
    assert!(w.logged().is_empty());
    let mut failed: Vec<(String, String)> = summary.failures.iter().map(|f| (f.module.clone(), f.profile.clone())).collect();
    failed.sort();
    assert_eq!(pairs(&[("chests", "Alpha"), ("chests", "Beta"), ("events", "Alpha")]), failed);
    assert_eq!(1, *windows.shared.restarts.lock().unwrap(), "a fresh browser for the retry");
}

#[test]
fn a_module_with_preflight_problems_is_left_out_and_the_rest_runs() {
    let w = World::new();
    let windows = FakeWindows::one();
    let (c, _) = w.pair();
    let mut e = Scripted::new("events", &w.config, &w.log);
    e.problems = vec!["Incomplete configuration for 'Tournament rankings'.".into()];
    let summary = w.runner(windows, vec![c, Arc::new(e)]).run(None, None, None, false).unwrap();
    assert_eq!(pairs(&[("chests", "Alpha"), ("chests", "Beta")]), w.logged());
    assert_eq!(1, summary.failures.len());
    assert_eq!("events", summary.failures[0].module);
    assert!(summary.failures[0].reason.contains("Incomplete configuration"));
}

#[test]
fn a_run_that_cannot_start_says_why() {
    let mut w = World::new();
    let mut accounts = w.config.accounts();
    let mut twin = accounts[0].clone();
    twin.name = "Twin".into();
    twin.profiles = vec![];
    accounts.push(twin);
    w.config.set_accounts(&accounts);
    let (runner, windows, chests, _) = w.standard();
    let err = runner.run(None, None, None, false).unwrap_err();
    assert!(err.to_string().contains("same port"));
    assert!(chests.ended.lock().unwrap().clone().unwrap().1.unwrap().starts_with("Execution cannot start: "),
            "the modules hear how the run ended");
    assert_eq!(0, *windows.shared.browser_starts.lock().unwrap());
}

#[test]
fn a_profile_with_nothing_set_up_is_reported() {
    let mut w = World::new();
    let mut accounts = w.config.accounts();
    accounts[0].profiles = vec![accounts[0].profiles[1].clone(), ProfileConfig::named("Empty", "Main")];
    accounts[0].profiles[0].name = "Alpha".into();
    w.config.set_accounts(&accounts);
    let summary = w.standard().0.run(None, None, None, false).unwrap();
    assert_eq!(pairs(&[("chests", "Alpha")]), w.logged());
    assert_eq!(vec![("".to_string(), "Empty".to_string())],
               summary.failures.iter().map(|f| (f.module.clone(), f.profile.clone())).collect::<Vec<_>>());
}

#[test]
fn check_lists_what_is_wrong_before_running() {
    let w = World::new();
    let (c, _) = w.pair();
    let mut e = Scripted::new("events", &w.config, &w.log);
    e.problems = vec!["Tournament rankings: missing token".into()];
    let problems = w.runner(FakeWindows::one(), vec![c.clone(), Arc::new(e)]).preflight();
    assert_eq!(1, problems.len());
    assert!(problems[0].contains("Tournament rankings"));
    assert!(w.runner(FakeWindows::one(), vec![c]).preflight().is_empty());
}

// --------------------------------------------------------- accounts at once

fn account(name: &str, profiles: usize, port: u16) -> AccountConfig {
    AccountConfig {
        name: name.into(),
        login: format!("{}@example.com", name),
        password: "pw".into(),
        cdp_port: port,
        browser_profile: name.into(),
        profiles: (0..profiles)
            .map(|i| ProfileConfig { database: Some(database(&format!("{}{}", name.to_lowercase(), i))),
                                     ..ProfileConfig::named(&format!("{}{}", name, i), name) })
            .collect(),
        ..Default::default()
    }
}

fn at_once() -> World {
    let mut w = World::new();
    w.config.set_accounts(&[account("A", 2, 9401), account("B", 1, 9402), account("C", 1, 9403)]);
    w
}

fn chests_with(w: &World, hook: Hook) -> Arc<Scripted> {
    let mut c = Scripted::new("chests", &w.config, &w.log);
    c.hook = Some(hook);
    Arc::new(c)
}

#[test]
fn accounts_overlap_each_on_its_window_and_the_profiles_of_one_do_not() {
    let w = at_once();
    let barrier = Arc::new(Barrier::new(3));
    let busy: Arc<Mutex<HashMap<String, bool>>> = Arc::new(Mutex::new(HashMap::new()));
    let seen: Arc<Mutex<Vec<(u16, String)>>> = Arc::new(Mutex::new(vec![]));
    let (b, s, bz) = (barrier.clone(), seen.clone(), busy.clone());
    let module = chests_with(&w, Box::new(move |window, profile| {
        {
            let mut busy = bz.lock().unwrap();
            assert!(!busy.get(&profile.account_name).copied().unwrap_or(false), "two profiles of one account at once");
            busy.insert(profile.account_name.clone(), true);
            s.lock().unwrap().push((window.port(), profile.name.clone()));
        }
        if profile.name.ends_with('0') {
            b.wait(); // only passes with all three accounts inside at once
        }
        bz.lock().unwrap().insert(profile.account_name.clone(), false);
    }));
    let windows = FakeWindows::each(&w.config);
    let summary = w.runner(windows.clone(), vec![module]).run(None, None, None, false).unwrap();
    assert!(summary.failures.is_empty());
    assert_eq!(4, summary.collected);
    let seen = seen.lock().unwrap().clone();
    let mut set = seen.clone();
    set.sort();
    assert_eq!(vec![(9401, "A0".to_string()), (9401, "A1".into()), (9402, "B0".into()), (9403, "C0".into())], set);
    let pos = |p: &str| seen.iter().position(|(_, n)| n == p).unwrap();
    assert!(pos("A0") < pos("A1"), "the configured order inside an account");
    for name in ["A", "B", "C"] {
        assert_eq!(1, *windows.window(name).browser_starts.lock().unwrap());
    }
}

#[test]
fn one_at_a_time_when_set_to_one() {
    let mut w = at_once();
    w.config.set("execution", "parallel_accounts", json!(1));
    let seen: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(vec![]));
    let s = seen.clone();
    let module = chests_with(&w, Box::new(move |_w, p| s.lock().unwrap().push(p.name.clone())));
    w.runner(FakeWindows::each(&w.config), vec![module]).run(None, None, None, false).unwrap();
    assert_eq!(vec!["A0", "A1", "B0", "C0"], *seen.lock().unwrap());
}

#[test]
fn never_more_accounts_than_the_setting() {
    let mut w = at_once();
    w.config.set("execution", "parallel_accounts", json!(2));
    let counts = Arc::new(Mutex::new((0, 0)));
    let c = counts.clone();
    let module = chests_with(&w, Box::new(move |_w, _p| {
        {
            let mut n = c.lock().unwrap();
            n.0 += 1;
            n.1 = n.1.max(n.0);
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
        c.lock().unwrap().0 -= 1;
    }));
    w.runner(FakeWindows::each(&w.config), vec![module]).run(None, None, None, false).unwrap();
    assert_eq!(2, counts.lock().unwrap().1);
}

#[test]
fn an_account_that_needs_a_person_or_a_window_not_ours_stops_alone_without_a_restart() {
    let w = at_once();
    let seen: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(vec![]));
    let s = seen.clone();
    let module = chests_with(&w, Box::new(move |_w, p| s.lock().unwrap().push(p.name.clone())));
    let windows = FakeWindows::each(&w.config);
    windows.set("B", FakeWindow { port: 9402, login: true, needs_person: true, ..Default::default() });
    windows.set("A", FakeWindow { port: 9401, wrong_window: true, ..Default::default() });
    let summary = w.runner(windows.clone(), vec![module]).run(None, None, None, false).unwrap();
    assert_eq!(vec!["C0"], *seen.lock().unwrap());
    let mut reasons: Vec<(String, String)> = summary.failures.iter().map(|f| (f.account.clone(), f.reason.clone())).collect();
    reasons.sort();
    assert_eq!(pairs(&[("A", "port taken"), ("B", "log in by hand")]), reasons);
    assert_eq!(0, *windows.window("A").restarts.lock().unwrap());
    assert_eq!(0, *windows.window("B").restarts.lock().unwrap());
}

#[test]
fn a_shared_database_keeps_the_whole_run_from_starting() {
    let mut w = at_once();
    let mut a = account("A", 1, 9401);
    let mut b = account("B", 1, 9402);
    a.profiles[0].database = Some(database("same"));
    b.profiles[0].database = Some(database("same"));
    w.config.set_accounts(&[a, b]);
    let seen: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(vec![]));
    let s = seen.clone();
    let module = chests_with(&w, Box::new(move |_w, p| s.lock().unwrap().push(p.name.clone())));
    let err = w.runner(FakeWindows::each(&w.config), vec![module]).run(Some("A"), None, None, false).unwrap_err();
    assert!(err.to_string().contains("same database"));
    assert!(seen.lock().unwrap().is_empty());
}

#[test]
fn durations_read_as_python_wrote_them() {
    assert_eq!("0:00:05", format_duration(5.0));
    assert_eq!("1:02:03.500000", format_duration(3723.5));
}
