//! Logging for the collector.
//!
//! The application is normally started by the Windows Task Scheduler, with nobody
//! watching: the log file is the only record of what happened. So the whole log is
//! written from here - one dated file per day, plus the console - in UTF-8, with old
//! files cleaned up on their own. The interface subscribes to the same lines.

use std::cell::RefCell;
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, SystemTime};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Level {
    Debug = 10,
    Info = 20,
    Warning = 30,
    Error = 40,
}

impl Level {
    pub fn name(self) -> &'static str {
        match self {
            Level::Debug => "DEBUG",
            Level::Info => "INFO",
            Level::Warning => "WARNING",
            Level::Error => "ERROR",
        }
    }

    pub fn parse(text: &str) -> Level {
        match text.trim().to_uppercase().as_str() {
            "DEBUG" => Level::Debug,
            "WARNING" | "WARN" => Level::Warning,
            "ERROR" => Level::Error,
            _ => Level::Info,
        }
    }
}

type Sink = Box<dyn Fn(Level, &str) + Send + Sync>;

struct State {
    level: Level,
    console: bool,
    file: Option<File>,
    dir: Option<PathBuf>,
    sinks: Vec<(u64, Sink)>,
    next_sink: u64,
    tally: Tally,
}

/// The warnings and errors logged since `begin_tally`, for the summary at the end of a run.
#[derive(Debug, Clone, Default)]
pub struct Tally {
    pub warnings: usize,
    pub errors: usize,
    /// The first of them (time, level, message), so the summary can repeat them.
    pub lines: Vec<(String, Level, String)>,
}

const TALLY_LINES: usize = 40;

fn state() -> &'static Mutex<State> {
    static STATE: OnceLock<Mutex<State>> = OnceLock::new();
    STATE.get_or_init(|| {
        Mutex::new(State { level: Level::Info, console: !cfg!(test), file: None, dir: None, sinks: Vec::new(), next_sink: 1,
                           tally: Tally::default() })
    })
}

thread_local! {
    static SPEAKER: RefCell<String> = const { RefCell::new(String::new()) };
}

/// While the guard lives, what this thread logs starts with `[name]`.
pub struct Speaker;

pub fn log_as(name: &str) -> Speaker {
    SPEAKER.with(|s| *s.borrow_mut() = name.to_string());
    Speaker
}

impl Drop for Speaker {
    fn drop(&mut self) {
        SPEAKER.with(|s| s.borrow_mut().clear());
    }
}

/// (Re)points the shared logger at a directory and level. Safe to call twice.
pub fn configure(log_dir: &Path, level: Level, retention_days: i64) {
    let mut st = state().lock().unwrap();
    st.level = level;
    st.file = None;
    match fs::create_dir_all(log_dir).and_then(|_| {
        let path = log_dir.join(format!("collector_{}.log", chrono::Local::now().format("%Y-%m-%d")));
        OpenOptions::new().create(true).append(true).open(path)
    }) {
        Ok(f) => st.file = Some(f),
        Err(e) => eprintln!("[WARN] Could not open log file in {}: {}", log_dir.display(), e),
    }
    st.dir = Some(log_dir.to_path_buf());
    drop(st);
    purge_old_logs(log_dir, retention_days);
}

/// Console output on or off (the interface has no console).
pub fn set_console(on: bool) {
    state().lock().unwrap().console = on;
}

pub fn set_level(level: Level) {
    state().lock().unwrap().level = level;
}

/// Lines also go to `sink`, with their level, until `remove_sink` is called.
pub fn add_sink(sink: impl Fn(Level, &str) + Send + Sync + 'static) -> u64 {
    let mut st = state().lock().unwrap();
    let id = st.next_sink;
    st.next_sink += 1;
    st.sinks.push((id, Box::new(sink)));
    id
}

pub fn remove_sink(id: u64) {
    state().lock().unwrap().sinks.retain(|(i, _)| *i != id);
}

/// Starts counting warnings and errors afresh.
pub fn begin_tally() {
    state().lock().unwrap().tally = Tally::default();
}

/// The warnings and errors logged since `begin_tally`.
pub fn tally() -> Tally {
    state().lock().unwrap().tally.clone()
}

/// Deletes *.log files older than the retention window (0 = keep forever).
pub fn purge_old_logs(log_dir: &Path, retention_days: i64) {
    if retention_days <= 0 {
        return;
    }
    let Ok(entries) = fs::read_dir(log_dir) else { return };
    let cutoff = SystemTime::now() - Duration::from_secs(retention_days as u64 * 86400);
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("log") || !path.is_file() {
            continue;
        }
        if let Ok(modified) = entry.metadata().and_then(|m| m.modified()) {
            if modified < cutoff {
                let _ = fs::remove_file(&path);
            }
        }
    }
}

/// Where diagnostic screenshots go, next to the logs of the same run.
pub fn screenshot_dir() -> PathBuf {
    let base = state().lock().unwrap().dir.clone().unwrap_or_else(|| crate::paths::base_dir().join("execution_logs"));
    let path = base.join("screenshots");
    let _ = fs::create_dir_all(&path);
    path
}

pub fn enabled(level: Level) -> bool {
    level >= state().lock().unwrap().level
}

pub fn log(level: Level, message: &str) {
    let speaker = SPEAKER.with(|s| s.borrow().clone());
    let message = if speaker.is_empty() { message.to_string() } else { format!("[{}] {}", speaker, message) };
    let mut st = state().lock().unwrap();
    if level < st.level {
        return;
    }
    let now = chrono::Local::now();
    if level >= Level::Warning {
        let t = &mut st.tally;
        if level == Level::Error {
            t.errors += 1;
        } else {
            t.warnings += 1;
        }
        if t.lines.len() < TALLY_LINES {
            t.lines.push((now.format("%H:%M:%S").to_string(), level, message.clone()));
        }
    }
    let line = format!("[{}] [{:<7}] {}", now.format("%Y-%m-%d %H:%M:%S"), level.name(), message);
    if st.console {
        let mut out = std::io::stdout().lock();
        let _ = writeln!(out, "{}", line);
        let _ = out.flush();
    }
    if let Some(f) = st.file.as_mut() {
        let _ = writeln!(f, "{}", line);
        let _ = f.flush();
    }
    if !st.sinks.is_empty() {
        let short = format!("[{}] [{:<7}] {}", now.format("%H:%M:%S"), level.name(), message);
        for (_, sink) in &st.sinks {
            sink(level, &short);
        }
    }
}

pub fn debug(m: &str) {
    log(Level::Debug, m)
}
pub fn info(m: &str) {
    log(Level::Info, m)
}
pub fn warning(m: &str) {
    log(Level::Warning, m)
}
pub fn error(m: &str) {
    log(Level::Error, m)
}

#[macro_export]
macro_rules! debug { ($($t:tt)*) => { $crate::logger::debug(&format!($($t)*)) } }
#[macro_export]
macro_rules! info { ($($t:tt)*) => { $crate::logger::info(&format!($($t)*)) } }
#[macro_export]
macro_rules! warn { ($($t:tt)*) => { $crate::logger::warning(&format!($($t)*)) } }
#[macro_export]
macro_rules! error { ($($t:tt)*) => { $crate::logger::error(&format!($($t)*)) } }
