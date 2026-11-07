//! The run itself: accounts, inside each one its profiles, and on each profile the
//! modules that are due.
//!
//! Accounts work at the same time, up to `execution.parallel_accounts`, each in its
//! own Chrome window; the profiles of one account never overlap. Each enabled module
//! is asked whether the profile has what it needs, the schedule says whether it is
//! due, and the ones left run one after the other with the game on that profile. A
//! profile with nothing to do is not switched to, and a run with nothing to do does
//! not open the browser.
//!
//! Failures are contained at the smallest level that makes sense: a module that
//! fails does not stop the next one, a profile that fails is retried and then
//! abandoned with a reason, an account that cannot be logged into loses its profiles
//! but not the rest of the run. A module whose preflight fails is left out, and the
//! others go on.

use std::collections::HashMap;
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

use crate::cancel;
use crate::config::{AccountConfig, ConfigManager, ProfileConfig};
use crate::context::{Window, Windows};
use crate::error::{Error, Res};
use crate::logger;
use crate::modules::{Kind, Module, ModuleResult, KINDS};
use crate::run_lock::RunLock;
use crate::schedule::ModuleSchedule;

type Plan = HashMap<(String, String), Vec<Arc<dyn Module>>>;

#[derive(Debug, Clone, Default)]
pub struct Summary {
    pub collected: i64,
    pub incomplete: i64,
    pub profiles_done: usize,
    pub failures: Vec<ModuleResult>,
    pub duration: String,
    pub results: Vec<ModuleResult>,
}

/// One result per module and profile: its last attempt.
pub fn final_results(results: &[ModuleResult]) -> Vec<ModuleResult> {
    let mut order: Vec<(String, String, String)> = Vec::new();
    let mut last: HashMap<(String, String, String), ModuleResult> = HashMap::new();
    for r in results {
        let key = (r.module.clone(), r.account.clone(), r.profile.clone());
        if !last.contains_key(&key) {
            order.push(key.clone());
        }
        last.insert(key, r.clone());
    }
    order.into_iter().map(|k| last.remove(&k).unwrap()).collect()
}

/// `str(timedelta)`: H:MM:SS, with microseconds when there are any.
pub fn format_duration(seconds: f64) -> String {
    let micros = (seconds * 1_000_000.0).round() as i64;
    let (s, us) = (micros / 1_000_000, micros % 1_000_000);
    let base = format!("{}:{:02}:{:02}", s / 3600, s % 3600 / 60, s % 60);
    if us == 0 {
        base
    } else {
        format!("{}.{:06}", base, us)
    }
}

pub struct ModuleRunner {
    pub config: ConfigManager,
    windows: Arc<dyn Windows>,
    pub modules: Vec<Arc<dyn Module>>,
    schedule: ModuleSchedule,
    lock: RunLock,
    results: Mutex<Vec<ModuleResult>>,
    not_due: Mutex<Vec<report::NotDue>>,
    halt: AtomicBool,
}

fn set_up(profile: &ProfileConfig) -> bool {
    KINDS.iter().any(|k| k.configured_for(profile))
}

fn profiles_of(account: &AccountConfig, only_profile: Option<&str>) -> Vec<ProfileConfig> {
    let profiles = account.active_profiles();
    match only_profile {
        Some(p) => profiles.into_iter().filter(|x| x.name.to_lowercase().contains(&p.to_lowercase())).collect(),
        None => profiles,
    }
}

impl ModuleRunner {
    pub fn new(config: ConfigManager, windows: Arc<dyn Windows>, modules: Vec<Arc<dyn Module>>, schedule: ModuleSchedule,
               lock_path: &Path) -> Self {
        ModuleRunner { config, windows, modules, schedule, lock: RunLock::new(lock_path), results: Mutex::new(vec![]), not_due: Mutex::new(vec![]),
                       halt: AtomicBool::new(false) }
    }

    fn execution_bool(&self, key: &str, default: bool) -> bool {
        self.config.get_bool("execution", key, default)
    }

    fn selected_accounts(&self, only: Option<&str>) -> Vec<AccountConfig> {
        let accounts = self.config.enabled_accounts();
        match only {
            Some(a) => accounts.into_iter().filter(|x| x.name.to_lowercase().contains(&a.to_lowercase())).collect(),
            None => accounts,
        }
    }

    fn selected_modules(&self, only: Option<&[String]>) -> Res<Vec<Arc<dyn Module>>> {
        let Some(only) = only.filter(|o| !o.is_empty()) else { return Ok(self.modules.clone()) };
        let wanted: Vec<String> = only.iter().map(|k| k.trim().to_lowercase()).filter(|k| !k.is_empty()).collect();
        let unknown: Vec<&String> = wanted.iter().filter(|k| Kind::by_key(k).is_none()).collect();
        if !unknown.is_empty() {
            let available: Vec<&str> = KINDS.iter().map(|k| k.key()).collect();
            return Err(Error::Runner(format!("Unknown module(s): {}. Available: {}.",
                                             unknown.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(", "), available.join(", "))));
        }
        let selected: Vec<Arc<dyn Module>> = self.modules.iter().filter(|m| wanted.contains(&m.key().to_string())).cloned().collect();
        let disabled: Vec<&String> = wanted.iter().filter(|k| !selected.iter().any(|m| m.key() == k.as_str())).collect();
        if !disabled.is_empty() {
            return Err(Error::Runner(format!("Module(s) disabled in the Modules tab: {}.",
                                             disabled.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(", "))));
        }
        Ok(selected)
    }

    // ------------------------------------------------------------- validation

    /// What no module can run without, reported before the run starts.
    pub fn blocking_problems(&self, accounts: Option<&[AccountConfig]>) -> Vec<String> {
        let mut problems = Vec::new();
        let accounts = accounts.map(|a| a.to_vec()).unwrap_or_else(|| self.config.enabled_accounts());
        if self.modules.is_empty() {
            problems.push("No module is enabled. Turn one on in the Modules tab.".to_string());
        }
        if accounts.is_empty() {
            problems.push("No enabled accounts with profiles found. Configure in Accounts and profiles.".to_string());
        }
        for account in &accounts {
            if self.windows.login_enabled() && (account.login.is_empty() || account.password.is_empty()) {
                problems.push(format!("Account '{}' has no username or password.", account.name));
            }
            if !account.active_profiles().iter().any(set_up) {
                problems.push(format!("Account '{}' has no profile a module can work on (a database for the chests, a clan \
                                       site and token for the tournaments).", account.name));
            }
        }
        problems.extend(self.config.window_problems(None));
        problems.extend(self.config.database_problems(None));
        problems
    }

    pub fn module_problems(&self, module: &dyn Module, accounts: Option<&[AccountConfig]>) -> Vec<String> {
        let accounts = accounts.map(|a| a.to_vec()).unwrap_or_else(|| self.config.enabled_accounts());
        let every: Vec<ProfileConfig> = accounts.iter().flat_map(|a| a.active_profiles()).collect();
        module.preflight(&every)
    }

    /// Everything that would make the run pointless or partial, for --check and the interface.
    pub fn preflight(&self) -> Vec<String> {
        let mut problems = self.blocking_problems(None);
        for m in &self.modules {
            problems.extend(self.module_problems(m.as_ref(), None));
        }
        problems
    }

    // --------------------------------------------------------------- planning

    fn plan(&self, modules: &[Arc<dyn Module>], accounts: &[AccountConfig], only_profile: Option<&str>, force: bool) -> Plan {
        let mut plan = Plan::new();
        for account in accounts {
            for profile in profiles_of(account, only_profile) {
                let mut tasks = Vec::new();
                for module in modules {
                    if !module.configured_for(&profile) {
                        continue;
                    }
                    let (due, why) = if force {
                        (true, String::new())
                    } else {
                        self.schedule.is_due(module.key(), &module.schedule(), &account.name, &profile.name,
                                             module.daily_attempts(), &module.day_start())
                    };
                    if due {
                        tasks.push(module.clone());
                    } else {
                        crate::info!("'{}' on '{}': {}; not run now.", module.title(), profile.label(), why);
                        self.not_due.lock().unwrap().push(report::NotDue { module: module.key().to_string(),
                                                                           profile: profile.label(), why });
                    }
                }
                if !tasks.is_empty() {
                    plan.insert((account.name.clone(), profile.name.clone()), tasks);
                }
            }
        }
        plan
    }

    // -------------------------------------------------------------- execution

    /// The run, with each module told when it starts and how it ended. Only one run
    /// at a time drives the browser: one that finds another going fails at once.
    pub fn run(&self, only_account: Option<&str>, only_profile: Option<&str>, only_modules: Option<&[String]>, force: bool) -> Res<Summary> {
        if !self.lock.acquire() {
            let holder = self.lock.holder().map(|h| format!(" (process {})", h)).unwrap_or_default();
            return Err(Error::RunAlreadyActive(format!(
                "Another run is already in progress{}; this one was not started, so the two do not fight over the browser.",
                holder
            )));
        }
        let result = self.locked_run(only_account, only_profile, only_modules, force);
        self.lock.release();
        result
    }

    fn locked_run(&self, only_account: Option<&str>, only_profile: Option<&str>, only_modules: Option<&[String]>,
                  force: bool) -> Res<Summary> {
        let started = Instant::now();
        logger::begin_tally();
        self.results.lock().unwrap().clear();
        self.not_due.lock().unwrap().clear();
        self.halt.store(false, Ordering::SeqCst);
        let outcome = self.do_run(started, only_account, only_profile, only_modules, force);
        self.write_report(&outcome, started);
        outcome
    }

    /// The summary at the very end of the log, however the run ended.
    fn write_report(&self, outcome: &Res<Summary>, started: Instant) {
        let ending = match outcome {
            Ok(_) => report::Ending::Finished,
            Err(e) if e.is_cancelled() => report::Ending::Cancelled(e.to_string()),
            Err(e @ Error::Runner(_)) => report::Ending::NotStarted(e.to_string()),
            Err(e) => report::Ending::Stopped(e.to_string()),
        };
        let duration = match outcome {
            Ok(s) => s.duration.clone(),
            Err(_) => format_duration(started.elapsed().as_secs() as f64),
        };
        let results = self.results.lock().unwrap().clone();
        let not_due = self.not_due.lock().unwrap().clone();
        let tally = logger::tally();
        let failed = !matches!(ending, report::Ending::Finished) || final_results(&results).iter().any(|r| !r.success);
        for line in report::lines(&ending, &duration, &results, &not_due, &tally) {
            if failed {
                crate::warn!("{}", line);
            } else {
                crate::info!("{}", line);
            }
        }
    }

    fn do_run(&self, started: Instant, only_account: Option<&str>, only_profile: Option<&str>,
              only_modules: Option<&[String]>, force: bool) -> Res<Summary> {
        let modules = self.selected_modules(only_modules)?;
        let accounts = self.selected_accounts(only_account);

        // A module that cannot work is left out, and said so; the others go on.
        let mut broken: Vec<(String, Vec<String>)> = Vec::new();
        for m in &modules {
            let problems = self.module_problems(m.as_ref(), Some(&accounts));
            if !problems.is_empty() {
                for p in &problems {
                    crate::error!("{}", p);
                }
                broken.push((m.key().to_string(), problems));
            }
        }
        let usable: Vec<Arc<dyn Module>> = modules.iter().filter(|m| !broken.iter().any(|(k, _)| k == m.key())).cloned().collect();
        let plan = self.plan(&usable, &accounts, only_profile, force);
        let active: Vec<Arc<dyn Module>> =
            usable.iter().filter(|m| plan.values().any(|t| t.iter().any(|x| x.key() == m.key()))).cloned().collect();
        for m in &active {
            let profiles: Vec<ProfileConfig> = accounts
                .iter()
                .flat_map(|a| {
                    a.active_profiles().into_iter().filter(|p| {
                        plan.get(&(a.name.clone(), p.name.clone())).map(|t| t.iter().any(|x| x.key() == m.key())).unwrap_or(false)
                    })
                })
                .collect();
            m.begin_run(&profiles);
        }

        let end = |duration: &str, error: Option<&str>, cancelled: bool| {
            let fin = final_results(&self.results.lock().unwrap());
            for m in &active {
                let mine: Vec<ModuleResult> = fin.iter().filter(|r| r.module == m.key() || r.module.is_empty()).cloned().collect();
                m.end_run(&mine, duration, error, cancelled);
            }
        };

        match self.run_inner(started, &modules, &accounts, &plan, &broken, only_account, only_profile) {
            Ok(summary) => {
                end(&summary.duration, None, false);
                Ok(summary)
            }
            Err(e) => {
                let text = e.to_string();
                end("", Some(&text), e.is_cancelled());
                Err(e)
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn run_inner(&self, started: Instant, modules: &[Arc<dyn Module>], accounts: &[AccountConfig], plan: &Plan,
                 broken: &[(String, Vec<String>)], only_account: Option<&str>, only_profile: Option<&str>) -> Res<Summary> {
        let mut problems = self.blocking_problems(if only_account.is_some() { Some(accounts) } else { None });
        if let Some(a) = only_account {
            if accounts.is_empty() {
                problems.push(format!("Account '{}' not found among enabled accounts.", a));
            }
        }
        if !modules.is_empty() && broken.len() == modules.len() {
            problems.push("No module can run (see the errors above).".into());
        }
        if !problems.is_empty() {
            for p in &problems {
                crate::error!("{}", p);
            }
            return Err(Error::Runner(format!("Execution cannot start: {}", problems.join(" | "))));
        }
        for (key, reasons) in broken {
            self.results.lock().unwrap().push(ModuleResult { module: key.clone(),
                                                             reason: format!("left out of the run: {}", reasons.join(" | ")),
                                                             ..Default::default() });
        }
        if plan.is_empty() {
            crate::info!("Nothing is due on this run: every module is either done for today or has no profile set up.");
            return Ok(self.summary(started));
        }
        let busy: Vec<&AccountConfig> = accounts
            .iter()
            .filter(|a| a.active_profiles().iter().any(|p| plan.contains_key(&(a.name.clone(), p.name.clone()))))
            .collect();
        let windows: Vec<(AccountConfig, Arc<dyn Window>)> =
            busy.iter().map(|a| ((*a).clone(), self.windows.for_account(a))).collect();
        let parallel = self.config.get_i64("execution", "parallel_accounts", 4).max(1) as usize;
        let workers = parallel.min(windows.len());
        if workers > 1 {
            self.run_at_once(&windows, workers, only_profile, plan)?;
        } else {
            for (i, (account, window)) in windows.iter().enumerate() {
                cancel::check()?;
                crate::info!("{}", "=".repeat(60));
                crate::info!("ACCOUNT {}/{}: '{}'", i + 1, windows.len(), account.name);
                crate::info!("{}", "=".repeat(60));
                self.run_account_with_restart(account, window.as_ref(), only_profile, plan)?;
            }
        }
        Ok(self.summary(started))
    }

    /// The accounts on threads of their own. Fails, once they have all stopped, with what would have ended the run.
    fn run_at_once(&self, windows: &[(AccountConfig, Arc<dyn Window>)], workers: usize, only_profile: Option<&str>,
                   plan: &Plan) -> Res<()> {
        let names: Vec<String> = windows.iter().map(|(a, w)| format!("{} (port {})", a.name, w.port())).collect();
        crate::info!("{} account(s), {} at a time: {}", windows.len(), workers, names.join(", "));
        let next = AtomicUsize::new(0);
        let ended: Mutex<Vec<Error>> = Mutex::new(vec![]);
        std::thread::scope(|scope| {
            for _ in 0..workers {
                scope.spawn(|| loop {
                    let i = next.fetch_add(1, Ordering::SeqCst);
                    let Some((account, window)) = windows.get(i) else { break };
                    if self.halt.load(Ordering::SeqCst) {
                        break;
                    }
                    let _speaker = logger::log_as(&account.name);
                    if let Err(e) = self.run_account_with_restart(account, window.as_ref(), only_profile, plan) {
                        ended.lock().unwrap().push(e);
                        self.halt.store(true, Ordering::SeqCst);
                    }
                });
            }
        });
        let ended = ended.into_inner().unwrap();
        if let Some(c) = ended.iter().find(|e| e.is_cancelled()) {
            return Err(c.clone());
        }
        match ended.into_iter().next() {
            Some(e) => Err(e),
            None => Ok(()),
        }
    }

    /// Runs the account; if nothing at all could be done, restarts the browser and
    /// runs it once more (a game frozen in the browser is what a person clears with F5).
    fn run_account_with_restart(&self, account: &AccountConfig, window: &dyn Window, only_profile: Option<&str>,
                                plan: &Plan) -> Res<()> {
        let attempts = if self.execution_bool("restart_browser_on_failure", true) { 2 } else { 1 };
        let stop_on_error = self.execution_bool("stop_on_error", false);
        for attempt in 1..=attempts {
            let last = attempt == attempts;
            let reason = match self.run_account(account, window, only_profile, plan) {
                Ok(true) => return Ok(()),
                Ok(false) => {
                    if last {
                        return Ok(()); // each profile already recorded its own failure
                    }
                    "nothing could be done on any profile of the account".to_string()
                }
                Err(e) if e.is_cancelled() => return Err(e),
                Err(e @ (Error::NeedsPerson(_) | Error::WrongWindow(_))) => {
                    // Another attempt cannot help, and a restart could close somebody else's window.
                    self.record_failure(account, None, &e.to_string(), None);
                    return if stop_on_error { Err(e) } else { Ok(()) };
                }
                Err(e) if e.is_session() => {
                    if last {
                        self.record_failure(account, None, &e.to_string(), None);
                        return if stop_on_error { Err(e) } else { Ok(()) };
                    }
                    e.to_string()
                }
                Err(e) => {
                    crate::error!("Unexpected failure on account '{}': {}", account.name, e);
                    self.save_error_screenshot(window, &format!("account_{}", account.name));
                    if last {
                        self.record_failure(account, None, &format!("unexpected error: {}", e), None);
                        return if stop_on_error { Err(e) } else { Ok(()) };
                    }
                    format!("unexpected error: {}", e)
                }
            };
            crate::warn!("Account '{}' failed ({}). The game may be frozen in the browser; closing it and trying the account \
                          again in a fresh one.", account.name, reason);
            if let Err(e) = window.restart_browser() {
                crate::error!("Could not restart the browser: {}", e);
                self.record_failure(account, None, &format!("{}; browser restart failed: {}", reason, e), None);
                return if stop_on_error { Err(e) } else { Ok(()) };
            }
        }
        Ok(())
    }

    /// Runs the account's profiles in its window. False when none of those tried succeeded.
    fn run_account(&self, account: &AccountConfig, window: &dyn Window, only_profile: Option<&str>, plan: &Plan) -> Res<bool> {
        cancel::check()?;
        window.start_browser()?;
        if window.login_enabled() {
            if !window.enter_account(account)? {
                return Err(Error::Session(format!("Could not log into account '{}'.", account.name)));
            }
        } else {
            window.navigate()?;
        }
        window.wait_for_load();
        if window.login_enabled() {
            window.remember_session();
        }
        let siblings: Vec<String> = account.profiles.iter().filter(|p| !p.name.is_empty()).map(|p| p.name.clone()).collect();
        let (mut tried, mut succeeded) = (0, 0);
        for profile in self.active_first(window, account, profiles_of(account, only_profile), &siblings, plan)? {
            if self.halt.load(Ordering::SeqCst) {
                break;
            }
            if !set_up(&profile) {
                crate::warn!("Profile '{}' has no database and nothing else set up, and was skipped (a profile without a \
                              database has nowhere to record its chests).", profile.label());
                self.record_failure(account, Some(&profile), "profile has no database and no module set up", None);
                continue;
            }
            let Some(tasks) = plan.get(&(account.name.clone(), profile.name.clone())) else { continue };
            cancel::check()?;
            tried += 1;
            if self.run_profile(window, account, &profile, tasks)? {
                succeeded += 1;
            }
            cancel::sleep(self.config.get_f64("timing", "between_profiles_wait", 3.0))?;
        }
        Ok(succeeded > 0 || tried == 0)
    }

    /// The profiles in the order to visit them: the one the game opened on first.
    fn active_first(&self, window: &dyn Window, account: &AccountConfig, profiles: Vec<ProfileConfig>, siblings: &[String],
                    plan: &Plan) -> Res<Vec<ProfileConfig>> {
        let active = match window.active_among(siblings) {
            Ok(a) => a,
            Err(e) if e.is_cancelled() => return Err(e),
            Err(e) => {
                crate::warn!("Could not learn which profile the game opened on: {}", e);
                return Ok(profiles);
            }
        };
        let first = profiles.iter().position(|p| p.name == active && plan.contains_key(&(account.name.clone(), p.name.clone())));
        let Some(first) = first else {
            if !active.is_empty() {
                crate::info!("The game opened on profile '{}', which has nothing due on this run.", active);
            } else {
                crate::info!("The profile the game opened on is not one of this account's; following the configured order.");
            }
            return Ok(profiles);
        };
        if first == 0 {
            return Ok(profiles);
        }
        crate::info!("The game opened on profile '{}': starting with it, then the others in their order.", active);
        let mut ordered = vec![profiles[first].clone()];
        ordered.extend(profiles.iter().enumerate().filter(|(i, _)| *i != first).map(|(_, p)| p.clone()));
        Ok(ordered)
    }

    /// Runs the profile's modules, retrying the ones that failed. True when any of them succeeded.
    fn run_profile(&self, window: &dyn Window, account: &AccountConfig, profile: &ProfileConfig,
                   tasks: &[Arc<dyn Module>]) -> Res<bool> {
        let attempts = self.config.get_i64("execution", "retries_per_profile", 2).max(1);
        let mut pending: Vec<Arc<dyn Module>> = tasks.to_vec();
        let mut outcome: HashMap<String, bool> = HashMap::new();
        for attempt in 1..=attempts {
            crate::info!("--- Profile '{}' (attempt {}/{}) ---", profile.label(), attempt, attempts);
            let step = (|| -> Res<()> {
                if !window.switch_to(profile)? {
                    return Err(Error::Session(format!("could not activate profile '{}'", profile.name)));
                }
                let mut i = 0;
                while i < pending.len() {
                    cancel::check()?;
                    if self.run_module(window, pending[i].as_ref(), account, profile, &mut outcome)? {
                        pending.remove(i);
                    } else {
                        i += 1;
                    }
                }
                Ok(())
            })();
            match step {
                Ok(()) => {}
                Err(e) if e.is_cancelled() => return Err(e),
                Err(e) if e.is_session() => crate::error!("Profile '{}': {}", profile.label(), e),
                Err(e) => {
                    crate::error!("Unexpected failure on profile '{}': {}", profile.label(), e);
                    self.save_error_screenshot(window, &format!("profile_{}", profile.name));
                }
            }
            if pending.is_empty() {
                break;
            }
            if attempt < attempts {
                crate::info!("Retrying the profile...");
            }
        }
        for m in &pending {
            outcome.insert(m.key().to_string(), false);
            self.record_failure(account, Some(profile), "all attempts failed", Some(m.as_ref()));
        }
        for m in tasks {
            self.schedule.record(m.key(), &account.name, &profile.name, *outcome.get(m.key()).unwrap_or(&false), &m.day_start());
        }
        Ok(outcome.values().any(|v| *v))
    }

    /// One module on the active profile. True when it is finished with, for better or worse.
    fn run_module(&self, window: &dyn Window, module: &dyn Module, account: &AccountConfig, profile: &ProfileConfig,
                  outcome: &mut HashMap<String, bool>) -> Res<bool> {
        crate::info!(">> {}: '{}'", module.title(), profile.label());
        let mut result = match module.run_for_profile(window, profile) {
            Ok(r) => r,
            Err(e) if e.is_cancelled() => return Err(e),
            Err(e) if e.is_session() => {
                crate::error!("{} on '{}': {}", module.title(), profile.label(), e);
                return Ok(false);
            }
            Err(e) => {
                crate::error!("Unexpected failure of '{}' on '{}': {}", module.title(), profile.label(), e);
                self.save_error_screenshot(window, &format!("{}_{}", module.key(), profile.name));
                return Ok(false);
            }
        };
        if result.account.is_empty() {
            result.account = account.name.clone();
        }
        if result.profile.is_empty() {
            result.profile = profile.name.clone();
        }
        result.module = module.key().to_string();
        let success = result.success;
        let retry = result.retry;
        let reason = if result.reason.is_empty() { "failure".to_string() } else { result.reason.clone() };
        self.results.lock().unwrap().push(result);
        outcome.insert(module.key().to_string(), success);
        if success {
            return Ok(true);
        }
        crate::warn!("{} on '{}' reported: {}", module.title(), profile.label(), reason);
        Ok(!retry)
    }

    // ---------------------------------------------------------------- results

    fn record_failure(&self, account: &AccountConfig, profile: Option<&ProfileConfig>, reason: &str, module: Option<&dyn Module>) {
        let where_ = match profile {
            Some(p) => format!("{} / {}", account.name, p.name),
            None => account.name.clone(),
        };
        crate::error!("FAILED: {}{} - {}", where_, module.map(|m| format!(" / {}", m.title())).unwrap_or_default(), reason);
        self.results.lock().unwrap().push(ModuleResult {
            module: module.map(|m| m.key().to_string()).unwrap_or_default(),
            account: account.name.clone(),
            profile: profile.map(|p| p.name.clone()).unwrap_or_default(),
            reason: reason.to_string(),
            ..Default::default()
        });
    }

    fn save_error_screenshot(&self, window: &dyn Window, label: &str) -> Option<String> {
        if !self.execution_bool("screenshot_on_error", true) {
            return None;
        }
        let safe: String = label.chars().map(|c| if c.is_alphanumeric() || c == '-' || c == '_' { c } else { '_' }).take(60).collect();
        let path = logger::screenshot_dir().join(format!("{}_{}.png", chrono::Local::now().format("%Y%m%d_%H%M%S"), safe));
        if window.save_screenshot(&path) {
            crate::info!("Screenshot of the failure saved to {}", path.display());
            return Some(path.display().to_string());
        }
        None
    }

    fn summary(&self, started: Instant) -> Summary {
        let results = self.results.lock().unwrap().clone();
        let collected = results.iter().map(|r| r.collected).sum();
        let incomplete = results.iter().map(|r| r.incomplete).sum();
        let fin = final_results(&results);
        let failures: Vec<ModuleResult> = fin.iter().filter(|r| !r.success).cloned().collect();
        let duration = format_duration(started.elapsed().as_secs() as f64);
        let mut done: Vec<(String, String)> = fin.iter().filter(|r| r.success).map(|r| (r.account.clone(), r.profile.clone())).collect();
        done.sort();
        done.dedup();
        Summary { collected, incomplete, profiles_done: done.len(), failures, duration, results }
    }
}

pub mod report;

#[cfg(test)]
mod tests;
