//! The command line - what the Windows Task Scheduler calls.
//!
//! ```text
//! tbcollector                 run the enabled modules that are due
//! tbcollector --gui           open the configuration interface
//! tbcollector --check         validate the configuration, collect nothing
//! tbcollector --account NAME  restrict the run to one account
//! tbcollector --profile NAME  restrict the run to one profile
//! tbcollector --module KEY    run only this module (shield, chests, members, events); may be repeated
//! tbcollector --force         ignore the schedule: run once-a-day modules again
//! tbcollector --keep-open     do not close the browser when finished
//! tbcollector fix-chest-types [--apply] [--profile NAME | --host H --user U --password P --database D]
//! tbcollector audit-chest-types [--profile NAME] [--days N]
//! tbcollector diagnose
//! ```
//!
//! Exit codes: 0 done, 1 failures, 2 configuration issue, 3 cancelled.

use std::sync::Arc;

use crate::cancel::{escape_watcher, CANCELLATION};
use crate::config::{migrate_legacy_config, ConfigManager};
use crate::context::{RealWindows, Windows};
use crate::error::Error;
use crate::logger::{self, Level};
use crate::modules::enabled_modules;
use crate::paths;
use crate::runner::ModuleRunner;
use crate::schedule::ModuleSchedule;

pub const EXIT_OK: i32 = 0;
pub const EXIT_FAILED: i32 = 1;
pub const EXIT_MISCONFIGURED: i32 = 2;
pub const EXIT_CANCELLED: i32 = 3;

#[derive(Debug, Default, Clone)]
pub struct Args {
    pub gui: bool,
    pub check: bool,
    pub account: Option<String>,
    pub profile: Option<String>,
    pub modules: Vec<String>,
    pub force: bool,
    pub keep_open: bool,
    pub help: bool,
    pub version: bool,
    pub tool: Option<String>,
    pub rest: Vec<String>,
}

pub fn parse(args: &[String]) -> Result<Args, String> {
    let mut a = Args::default();
    let mut i = 0;
    while i < args.len() {
        let arg = args[i].as_str();
        let mut value = |name: &str| -> Result<String, String> {
            i += 1;
            args.get(i).cloned().ok_or_else(|| format!("{} needs a value", name))
        };
        match arg {
            "--gui" | "config" | "gui" => a.gui = true,
            "--check" | "check" => a.check = true,
            "--account" => a.account = Some(value("--account")?),
            "--profile" => a.profile = Some(value("--profile")?),
            "--module" => a.modules.push(value("--module")?),
            "--force" => a.force = true,
            "--keep-open" => a.keep_open = true,
            "-h" | "--help" => a.help = true,
            "-V" | "--version" => a.version = true,
            "fix-chest-types" | "audit-chest-types" | "diagnose" | "journal-dump" | "live-tournaments" => {
                a.tool = Some(arg.to_string());
                a.rest = args[i + 1..].to_vec();
                return Ok(a);
            }
            other => return Err(format!("unknown argument: {}", other)),
        }
        i += 1;
    }
    Ok(a)
}

pub const USAGE: &str = "Total Battle Chest Collector

Usage:
  tbcollector                 run every enabled module that is due (what Task Scheduler calls)
  tbcollector --gui           open the configuration interface
  tbcollector --check         validate the configuration only
  tbcollector --account NAME  run only this account
  tbcollector --profile NAME  run only this profile
  tbcollector --module KEY    run only this module (shield, chests, members, events); may be repeated
  tbcollector --force         ignore the schedule (run once-a-day modules again)
  tbcollector --keep-open     do not close the browser when finished
  tbcollector fix-chest-types [--apply] [--profile NAME] [--host H --port N --user U --password P --database D]
  tbcollector audit-chest-types [--profile NAME] [--days N]
  tbcollector diagnose        check the browser, every database and every clan site (reads only)
  tbcollector journal-dump [--account A] [--profile P] [--days N]  write the Journal's tournament cards (reads only)
  tbcollector live-tournaments [--account A]  write the game's Tournaments window to a file (reads only)

Exit codes: 0 done, 1 failures, 2 configuration issue, 3 cancelled.";

/// Loads config.json, creating it on first run (inheriting position.cfg's databases).
pub fn load_configuration() -> Result<ConfigManager, String> {
    let mut config = ConfigManager::open_default()?;
    if config.exists() {
        return Ok(config);
    }
    crate::info!("config/config.json not found; creating it with the default values.");
    let imported = migrate_legacy_config(&paths::legacy_config_file());
    if !imported.is_empty() {
        let n: usize = imported.iter().map(|a| a.profiles.len()).sum();
        config.set_accounts(&imported);
        crate::info!("Imported {} profile(s) from position.cfg. Open the interface to fill in the account login and password.", n);
    }
    if let Err(e) = config.save() {
        crate::error!("Could not write {}: {}", config.path.display(), e);
    }
    Ok(config)
}

pub fn configure_logging(config: &ConfigManager) {
    logger::configure(&config.resolved_log_dir(), Level::parse(&config.get_str("execution", "log_level", "INFO")),
                      config.get_i64("execution", "log_retention_days", 7));
}

/// The scheduled run, `--check`, and the tools. Returns the exit code.
pub fn main_with(args: Args) -> i32 {
    if args.help {
        println!("{}", USAGE);
        return EXIT_OK;
    }
    if args.version {
        println!("tbcollector {}", crate::VERSION);
        return EXIT_OK;
    }
    let mut config = match load_configuration() {
        Ok(c) => c,
        Err(e) => {
            crate::error!("{}", e);
            return EXIT_MISCONFIGURED;
        }
    };
    configure_logging(&config);
    if args.keep_open {
        config.set("execution", "close_browser_on_finish", serde_json::json!(false));
    }
    if let Some(tool) = &args.tool {
        return crate::tools::run(tool, &args.rest, &config);
    }
    if args.gui {
        return crate::gui::launch(config);
    }

    let windows = Arc::new(RealWindows::new(&mut config));
    let runner = ModuleRunner::new(config.clone(), windows.clone(), enabled_modules(&config),
                                   ModuleSchedule::new(&paths::module_state_file()), &paths::run_lock_file());
    if args.check {
        let problems = runner.preflight();
        for p in &problems {
            crate::error!("{}", p);
        }
        if !problems.is_empty() {
            return EXIT_MISCONFIGURED;
        }
        crate::info!("Configuration is complete.");
        return EXIT_OK;
    }

    let titles: Vec<&str> = runner.modules.iter().map(|m| m.title()).collect();
    crate::info!("Starting Total Battle Chest Collector {} (enabled modules: {})", crate::VERSION,
                 if titles.is_empty() { "none".to_string() } else { titles.join(", ") });
    CANCELLATION.reset();
    escape_watcher().start();
    crate::info!("Hold ESC for a moment to cancel execution.");
    let only = (!args.modules.is_empty()).then_some(args.modules.as_slice());
    let outcome = runner.run(args.account.as_deref(), args.profile.as_deref(), only, args.force);
    escape_watcher().stop();
    windows.close();
    match outcome {
        Ok(summary) => {
            if summary.failures.is_empty() {
                EXIT_OK
            } else {
                EXIT_FAILED
            }
        }
        Err(Error::Cancelled(e)) => {
            crate::warn!("Execution cancelled ({}). Already collected chests remain saved.", e);
            EXIT_CANCELLED
        }
        Err(Error::RunAlreadyActive(e)) => {
            // Not a failure: the run in progress does the work; the next scheduled one catches up.
            crate::warn!("{}", e);
            EXIT_OK
        }
        // Already said in the run's summary.
        Err(Error::Runner(_)) => EXIT_MISCONFIGURED,
        Err(e) => {
            crate::error!("Unhandled failure: {}", e);
            EXIT_FAILED
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(list: &[&str]) -> Vec<String> {
        list.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn the_options_of_the_old_main_py_are_understood() {
        let a = parse(&args(&["--module", "chests", "--module", "events", "--force", "--account", "Main"])).unwrap();
        assert_eq!(vec!["chests", "events"], a.modules);
        assert!(a.force);
        assert_eq!(Some("Main".to_string()), a.account);
        assert!(parse(&args(&["config"])).unwrap().gui);
        assert!(parse(&args(&["check"])).unwrap().check);
        assert!(parse(&args(&["--nope"])).is_err());
        let t = parse(&args(&["fix-chest-types", "--apply"])).unwrap();
        assert_eq!((Some("fix-chest-types".to_string()), args(&["--apply"])), (t.tool, t.rest));
    }
}
