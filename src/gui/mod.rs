//! Configuration interface.
//!
//! Everything is configured here: the execution parameters, the modules, the
//! accounts with their logins, and the profiles with their databases. The window is
//! built from the declarations in config.rs and modules/, so a parameter added there
//! shows up here with its label and help text.
//!
//! The run button exists so the whole loop - configure, test, fix - happens in one
//! place: it runs in a worker thread and streams the same log the scheduled run
//! writes. Each module's card has a button that runs that module alone.

use std::collections::{HashMap, VecDeque};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use eframe::egui::{self, Color32, RichText};
use serde_json::{json, Map, Value as Json};

use crate::cancel::{escape_watcher, CANCELLATION};
use crate::config::{coerce, default_config, shared_sections, AccountConfig, ConfigManager, DatabaseConfig,
                    Field, FieldType, ProfileConfig, Section};
use crate::context::{browser_for, RealWindows, Window, Windows};
use crate::db::test_connection;
use crate::error::Error;
use crate::logger::{self, Level};
use crate::modules::{enabled_modules, Kind, Module, KINDS};
use crate::paths;
use crate::runner::ModuleRunner;
use crate::schedule::ModuleSchedule;
use crate::task_scheduler::{self, TaskInfo};

const ACCENT: Color32 = Color32::from_rgb(0x58, 0xa6, 0xff);
const OK: Color32 = Color32::from_rgb(0x3f, 0xb9, 0x50);
const WARN: Color32 = Color32::from_rgb(0xd2, 0x99, 0x22);
const ERR: Color32 = Color32::from_rgb(0xf8, 0x51, 0x49);
const MUTED: Color32 = Color32::from_rgb(0x8b, 0x94, 0x9e);
const CARD: Color32 = Color32::from_rgb(0x1c, 0x20, 0x29);
const DARK: Color32 = Color32::from_rgb(0x0d, 0x11, 0x17);
const GREEN: Color32 = Color32::from_rgb(0x23, 0x86, 0x36);
const MAX_LOG_LINES: usize = 5000;

#[derive(PartialEq, Clone, Copy)]
enum Tab {
    Execution,
    Modules,
    Parameters,
    Accounts,
}

/// A field being edited: text for everything but the checkboxes.
#[derive(Clone)]
enum Edit {
    Text(String),
    Flag(bool),
}

fn edit_of(value: Option<&Json>, spec: &Field) -> Edit {
    let v = value.cloned().unwrap_or_else(|| spec.default.clone());
    match spec.kind {
        FieldType::Bool => Edit::Flag(v.as_bool().unwrap_or(false)),
        _ => Edit::Text(match v {
            Json::String(s) => s,
            Json::Null => String::new(),
            Json::Number(n) => {
                if spec.kind == FieldType::Float {
                    crate::tbcodec::py_float(n.as_f64().unwrap_or(0.0))
                } else {
                    n.to_string()
                }
            }
            other => other.to_string(),
        }),
    }
}

fn json_of(edit: &Edit) -> Json {
    match edit {
        Edit::Text(s) => Json::String(s.trim().to_string()),
        Edit::Flag(b) => Json::Bool(*b),
    }
}

#[derive(Clone)]
struct ProfileEdit {
    enabled: bool,
    name: String,
    database: Option<DatabaseConfig>,
    modules: Map<String, Json>,
}

#[derive(Clone)]
struct AccountEdit {
    enabled: bool,
    name: String,
    login: String,
    password: String,
    show_password: bool,
    browser_profile: String,
    port: String,
    profiles: Vec<ProfileEdit>,
}

impl AccountEdit {
    fn of(a: &AccountConfig) -> Self {
        AccountEdit {
            enabled: a.enabled,
            name: a.name.clone(),
            login: a.login.clone(),
            password: a.password.clone(),
            show_password: false,
            browser_profile: a.browser_profile.clone(),
            port: if a.cdp_port == 0 { String::new() } else { a.cdp_port.to_string() },
            profiles: a.profiles.iter().map(|p| ProfileEdit { enabled: p.enabled, name: p.name.clone(), database: p.database.clone(),
                                                              modules: p.modules.clone() }).collect(),
        }
    }

    /// The port typed, 0 when empty; Err with the text when it cannot be a port.
    fn port_typed(&self) -> Result<u16, String> {
        let t = self.port.trim();
        if t.is_empty() {
            return Ok(0);
        }
        match t.parse::<u32>() {
            Ok(p) if (1024..=65535).contains(&p) => Ok(p as u16),
            _ => Err(t.to_string()),
        }
    }

    fn to_account(&self) -> Result<AccountConfig, String> {
        let name = self.name.trim().to_string();
        Ok(AccountConfig {
            name: name.clone(),
            login: self.login.trim().to_string(),
            password: self.password.clone(),
            enabled: self.enabled,
            browser_profile: self.browser_profile.trim().to_string(),
            cdp_port: self.port_typed()?,
            profiles: self
                .profiles
                .iter()
                .filter(|p| !p.name.trim().is_empty())
                .map(|p| ProfileConfig { name: p.name.trim().to_string(), enabled: p.enabled, database: p.database.clone(),
                                         account_name: name.clone(), modules: p.modules.clone() })
                .collect(),
        })
    }
}

type Shared<T> = Arc<Mutex<Option<T>>>;

enum Dialog {
    Database { account: usize, profile: usize, fields: [String; 5], test: Shared<(bool, String)> },
    Modules { account: usize, profile: usize, values: HashMap<String, HashMap<String, Edit>>, tests: HashMap<String, Shared<(bool, String)>> },
    Confirm { title: String, text: String, action: Action },
    Message { title: String, text: String, error: bool },
}

#[derive(Clone)]
enum Action {
    RunAll,
    RunModule(Kind),
    RestoreDefaults,
    RemoveAccount(usize),
}

pub struct GuiApp {
    config: ConfigManager,
    tab: Tab,
    log: Arc<Mutex<VecDeque<(Level, String)>>>,
    sink: u64,
    busy: Arc<AtomicBool>,
    stoppable: bool,
    stopping: bool,
    force: bool,
    edits: HashMap<String, HashMap<String, Edit>>,
    accounts: Vec<AccountEdit>,
    dialog: Option<Dialog>,
    context: Arc<Mutex<Option<Arc<RealWindows>>>>,
    module_status: HashMap<&'static str, (String, Color32)>,
    status: (String, Color32),
    info: String,
    /// The Task Scheduler entry: what there is, and the interval and start being chosen.
    task: Result<Option<TaskInfo>, String>,
    task_minutes: u32,
    task_start: String,
}

impl GuiApp {
    pub fn new(mut config: ConfigManager) -> Self {
        config.pin_windows();
        let log: Arc<Mutex<VecDeque<(Level, String)>>> = Arc::new(Mutex::new(VecDeque::new()));
        let sink_log = log.clone();
        let sink = logger::add_sink(move |level, line| {
            let mut l = sink_log.lock().unwrap();
            l.push_back((level, line.to_string()));
            while l.len() > MAX_LOG_LINES {
                l.pop_front();
            }
        });
        let mut app = GuiApp {
            accounts: config.accounts().iter().map(AccountEdit::of).collect(),
            config,
            tab: Tab::Execution,
            log,
            sink,
            busy: Arc::new(AtomicBool::new(false)),
            stoppable: false,
            stopping: false,
            force: false,
            edits: HashMap::new(),
            dialog: None,
            context: Arc::new(Mutex::new(None)),
            module_status: HashMap::new(),
            status: (String::new(), MUTED),
            info: String::new(),
            task: Ok(None),
            task_minutes: 60,
            task_start: String::new(),
        };
        app.refresh_task();
        if let Ok(Some(t)) = &app.task {
            if t.every_minutes > 0 {
                app.task_minutes = t.every_minutes;
            }
            app.task_start = t.start.clone();
        }
        app.reload_edits();
        app.refresh_status();
        app
    }

    fn all_sections(&self) -> Vec<Section> {
        crate::config::all_sections()
    }

    fn reload_edits(&mut self) {
        self.edits.clear();
        for section in self.all_sections() {
            let stored = self.config.section(&section.key);
            let map = section.fields.iter().map(|f| (f.key.to_string(), edit_of(stored.get(f.key), f))).collect();
            self.edits.insert(section.key.clone(), map);
        }
    }

    fn message(&mut self, title: &str, text: &str, error: bool) {
        self.dialog = Some(Dialog::Message { title: title.into(), text: text.into(), error });
    }

    fn confirm(&mut self, title: &str, text: &str, action: Action) {
        self.dialog = Some(Dialog::Confirm { title: title.into(), text: text.into(), action });
    }

    fn refresh_status(&mut self) {
        let accounts = self.config.accounts();
        let profiles: Vec<ProfileConfig> = accounts.iter().flat_map(|a| a.profiles.clone()).collect();
        let collectable = profiles.iter().filter(|p| p.has_database()).count();
        let schedule = ModuleSchedule::new(&paths::module_state_file());
        let mut summary = Vec::new();
        for kind in KINDS {
            let options = self.config.module(kind.key());
            let enabled = options.get("enabled").and_then(|v| v.as_bool()).unwrap_or(kind.default_enabled());
            let ready = profiles.iter().filter(|p| p.enabled && !p.name.is_empty() && kind.configured_for(p)).count();
            let last = schedule.last_done(kind.key()).replace('T', " ");
            let last: String = last.chars().take(16).collect();
            let parts = format!("{} profile(s) set up   ·   {}", ready, if last.is_empty() { "never run".into() } else { format!("last done {}", last) });
            let (text, color) = if !enabled {
                ("○ Disabled", MUTED)
            } else if ready == 0 {
                ("○ Enabled, but no profile is set up for it yet", WARN)
            } else {
                ("✔ Enabled", OK)
            };
            self.module_status.insert(kind.key(), (format!("{}   ·   {}", text, parts), color));
            if enabled {
                summary.push(format!("{} ({}, {} profile(s))", kind.title(),
                                     options.get("schedule").and_then(|v| v.as_str()).unwrap_or(""), ready));
            }
        }
        self.status = if collectable > 0 {
            (format!("✔ {} profile(s) configured", collectable), OK)
        } else {
            ("✖ No profile configured".into(), WARN)
        };
        self.info = format!(
            "Registered accounts: {}   ·   profiles: {} ({} with configured database)\nModules: {}\nConfiguration: {}",
            accounts.len(),
            profiles.len(),
            collectable,
            if summary.is_empty() { "none enabled".into() } else { summary.join(",  ") },
            self.config.path.display()
        );
    }

    fn refresh_task(&mut self) {
        self.task = task_scheduler::query();
    }

    fn schedule_task(&mut self) {
        match task_scheduler::create(self.task_minutes, &self.task_start) {
            Ok(()) => {
                self.refresh_task();
                let when = task_scheduler::describe_interval(self.task_minutes);
                crate::info!("Windows Task Scheduler: '{}' now runs the collector {}.", task_scheduler::TASK_NAME, when);
                self.message("Task Scheduler", &format!("The collector will run {} through the Windows Task Scheduler \
                             (task '{}'), while you are logged on.", when, task_scheduler::TASK_NAME), false);
            }
            Err(e) => self.message("Task Scheduler", &format!("Could not schedule the collector: {}", e), true),
        }
    }

    fn remove_task(&mut self) {
        match task_scheduler::remove() {
            Ok(()) => {
                self.refresh_task();
                crate::info!("Windows Task Scheduler: task '{}' removed.", task_scheduler::TASK_NAME);
                self.message("Task Scheduler", "The scheduled task was removed: the collector only runs when started by hand.", false);
            }
            Err(e) => self.message("Task Scheduler", &format!("Could not remove the task: {}", e), true),
        }
    }

    fn scheduler_card(&mut self, ui: &mut egui::Ui) {
        let (text, color) = match &self.task {
            Err(e) => (format!("✖ Could not read the Task Scheduler: {}", e), ERR),
            Ok(None) => ("○ Not scheduled: the collector only runs when started by hand".to_string(), WARN),
            Ok(Some(t)) => {
                let when = task_scheduler::describe_interval(t.every_minutes);
                let (mut text, mut color) = if t.enabled {
                    (format!("✔ Scheduled {}", when), OK)
                } else {
                    (format!("○ Scheduled {}, but disabled in the Task Scheduler", when), WARN)
                };
                if !t.start.is_empty() {
                    text.push_str(&format!(" (from {})", t.start));
                }
                let exe = task_scheduler::collector_exe();
                if !t.command.is_empty() && !t.command.eq_ignore_ascii_case(&exe.to_string_lossy()) {
                    text.push_str(&format!("   ·   it starts {}, not this installation: press Update", t.command));
                    color = WARN;
                }
                (text, color)
            }
        };
        let scheduled = matches!(self.task, Ok(Some(_)));
        Self::card(ui, |ui| {
            ui.horizontal_wrapped(|ui| {
                ui.label(RichText::new("Windows Task Scheduler").strong());
                ui.label(RichText::new(text).color(color));
            });
            ui.horizontal(|ui| {
                ui.label("Run");
                egui::ComboBox::from_id_salt("task_interval")
                    .selected_text(task_scheduler::describe_interval(self.task_minutes))
                    .show_ui(ui, |ui| {
                        for m in task_scheduler::INTERVALS {
                            ui.selectable_value(&mut self.task_minutes, m, task_scheduler::describe_interval(m));
                        }
                    });
                ui.label("starting at");
                ui.add(egui::TextEdit::singleline(&mut self.task_start).desired_width(50.0).hint_text("HH:MM"))
                    .on_hover_text("Time of day the runs count from, for instance 08:00. Empty: from now.");
                if ui.button(if scheduled { "Update schedule" } else { "Schedule" }).clicked() {
                    self.schedule_task();
                }
                if ui.add_enabled(scheduled, egui::Button::new("Remove")).clicked() {
                    self.remove_task();
                }
                if ui
                    .add_enabled(scheduled, egui::Button::new("Start task now"))
                    .on_hover_text("Runs the scheduled task once now, as Windows would (its log goes to the logs folder).")
                    .clicked()
                {
                    match task_scheduler::run_now() {
                        Ok(()) => crate::info!("Windows Task Scheduler: task '{}' started.", task_scheduler::TASK_NAME),
                        Err(e) => self.message("Task Scheduler", &format!("Could not start the task: {}", e), true),
                    }
                }
                if ui.button("Refresh").on_hover_text("Read the Task Scheduler again").clicked() {
                    self.refresh_task();
                }
            });
        });
    }

    // ------------------------------------------------------------ actions

    fn save_parameters(&mut self) {
        for (section, fields) in &self.edits {
            for (key, edit) in fields {
                self.config.set(section, key, json_of(edit));
            }
        }
        match self.config.save() {
            Ok(()) => {
                self.reload_edits();
                self.refresh_status();
                self.message("Configuration", "Modules and parameters saved to config/config.json.", false);
            }
            Err(e) => self.message("Configuration", &format!("Could not save: {}", e), true),
        }
    }

    fn restore_defaults(&mut self) {
        let defaults = default_config();
        for section in shared_sections() {
            for f in &section.fields {
                if let Some(v) = crate::config::bucket(&defaults, &section.key).and_then(|b| b.get(f.key)) {
                    self.config.set(&section.key, f.key, v.clone());
                }
            }
        }
        let _ = self.config.save();
        self.reload_edits();
        self.refresh_status();
    }

    fn save_accounts(&mut self) {
        let mut accounts = Vec::new();
        for card in self.accounts.iter().filter(|c| !c.name.trim().is_empty()) {
            match card.to_account() {
                Ok(a) => accounts.push(a),
                Err(bad) => {
                    self.message("Accounts and profiles", &format!("'{}' is not a debugging port. Use a number from 1024 to 65535, \
                                                                   or leave it empty to get a free one.", bad), true);
                    return;
                }
            }
        }
        let mut problems = self.config.window_problems(Some(&accounts));
        problems.extend(self.config.database_problems(Some(&accounts)));
        if !problems.is_empty() {
            self.message("Accounts and profiles", &format!("Not saved:\n\n{}", problems.join("\n\n")), true);
            return;
        }
        let without: Vec<String> =
            accounts.iter().flat_map(|a| a.profiles.iter().filter(|p| !p.has_database()).map(move |p| format!("{}/{}", a.name, p.name))).collect();
        self.config.set_accounts(&accounts);
        if let Err(e) = self.config.save() {
            self.message("Accounts and profiles", &format!("Could not save: {}", e), true);
            return;
        }
        self.config.pin_windows();
        self.accounts = self.config.accounts().iter().map(AccountEdit::of).collect();
        // What was connected may no longer be any account's window.
        if !self.busy.load(Ordering::SeqCst) {
            if let Some(ctx) = self.context.lock().unwrap().take() {
                ctx.primary.disconnect();
            }
        }
        self.refresh_status();
        let mut text = format!("{} account(s) saved to config/config.json.", accounts.len());
        if !without.is_empty() {
            text.push_str(&format!("\n\nProfiles without a database (will be skipped during collection):\n  {}", without.join("\n  ")));
        }
        self.message("Accounts and profiles", &text, false);
    }

    fn background(&mut self, stoppable: bool, work: impl FnOnce() + Send + 'static) {
        if self.busy.load(Ordering::SeqCst) {
            self.message("Please wait", "An operation is already in progress.", true);
            return;
        }
        self.busy.store(true, Ordering::SeqCst);
        self.stoppable = stoppable;
        self.stopping = false;
        self.tab = Tab::Execution;
        let busy = self.busy.clone();
        thread::spawn(move || {
            work();
            escape_watcher().stop();
            busy.store(false, Ordering::SeqCst);
        });
    }

    fn ensure_context(config: &ConfigManager, slot: &Arc<Mutex<Option<Arc<RealWindows>>>>) -> Arc<RealWindows> {
        let mut s = slot.lock().unwrap();
        if s.is_none() {
            let mut c = config.clone();
            *s = Some(Arc::new(RealWindows::new(&mut c)));
        }
        s.clone().unwrap()
    }

    fn connect_browser(&mut self) {
        let (config, slot) = (self.config.clone(), self.context.clone());
        self.background(false, move || {
            let ctx = Self::ensure_context(&config, &slot);
            match ctx.primary.start_browser() {
                Ok(()) => {
                    ctx.primary.browser.focus_tab();
                    crate::info!("Game opened and connected.");
                }
                Err(e) => crate::error!("Could not open the game: {}", e),
            }
        });
    }

    fn check_configuration(&mut self) {
        let mut config = self.config.clone();
        self.background(false, move || {
            let windows = Arc::new(RealWindows::new(&mut config));
            let runner = ModuleRunner::new(config.clone(), windows, enabled_modules(&config),
                                           ModuleSchedule::new(&paths::module_state_file()), &paths::run_lock_file());
            let problems = runner.preflight();
            if problems.is_empty() {
                crate::info!("Configuration is complete — ready to run.");
            }
            for p in problems {
                crate::error!("{}", p);
            }
        });
    }

    fn start_run(&mut self, force: bool, only: Option<Kind>) {
        let (config, slot) = (self.config.clone(), self.context.clone());
        CANCELLATION.reset();
        escape_watcher().start();
        self.background(true, move || {
            let ctx = Self::ensure_context(&config, &slot);
            let modules: Vec<Arc<dyn Module>> = match only {
                Some(k) => {
                    crate::info!("Running only '{}' (schedule ignored).", k.title());
                    vec![k.instantiate(&config)]
                }
                None => enabled_modules(&config),
            };
            let runner = ModuleRunner::new(config.clone(), ctx.clone(), modules, ModuleSchedule::new(&paths::module_state_file()),
                                           &paths::run_lock_file());
            // How it went is in the summary the run writes at the end of the log; only a run
            // that could not even begin (another one going) is said here.
            if let Err(e @ Error::RunAlreadyActive(_)) = runner.run(None, None, None, force) {
                crate::warn!("{}", e);
            }
            ctx.close();
            *slot.lock().unwrap() = None;
        });
    }

    fn stop_run(&mut self) {
        CANCELLATION.cancel("stop requested from interface");
        self.stopping = true;
        crate::warn!("Cancellation requested; stopping at next safe point...");
    }

    fn open_account_window(&mut self, index: usize) {
        let name = self.accounts[index].name.trim().to_string();
        let Some(account) = self.config.accounts().into_iter().find(|a| a.name == name && a.cdp_port != 0) else {
            self.message("Open window", "Save the accounts first: the window opens with what is saved.", false);
            return;
        };
        let config = self.config.clone();
        self.background(false, move || match browser_for(&config, &account).open_for_person() {
            Ok(()) => crate::info!("Window of '{}' open on port {}. Log in there and type the code from the e-mail, if asked; the \
                                    next run uses that session.", name, account.cdp_port),
            Err(e) => crate::error!("Could not open the window of '{}': {}", name, e),
        });
    }

    fn apply(&mut self, action: Action) {
        match action {
            Action::RunAll => self.start_run(self.force, None),
            Action::RunModule(k) => self.start_run(true, Some(k)),
            Action::RestoreDefaults => self.restore_defaults(),
            Action::RemoveAccount(i) => {
                if i < self.accounts.len() {
                    self.accounts.remove(i);
                }
            }
        }
    }

    // ----------------------------------------------------------- drawing

    fn field(ui: &mut egui::Ui, spec: &Field, edit: &mut Edit) {
        ui.horizontal(|ui| {
            ui.allocate_ui_with_layout(egui::vec2(200.0, 20.0), egui::Layout::left_to_right(egui::Align::Center), |ui| {
                ui.set_width(200.0);
                ui.add(egui::Label::new(spec.label).wrap());
            });
            match edit {
                Edit::Flag(b) => {
                    ui.checkbox(b, "");
                }
                Edit::Text(t) if spec.kind == FieldType::Choice => {
                    egui::ComboBox::from_id_salt(spec.key.to_string() + spec.label).selected_text(t.clone()).width(170.0).show_ui(ui, |ui| {
                        for c in &spec.choices {
                            ui.selectable_value(t, c.to_string(), *c);
                        }
                    });
                }
                Edit::Text(t) => {
                    ui.add(egui::TextEdit::singleline(t).desired_width(240.0).password(spec.is_secret()));
                }
            }
            if !spec.help.is_empty() {
                ui.add(egui::Label::new(RichText::new(spec.help).small().color(MUTED)).wrap());
            }
        });
    }

    fn card<R>(ui: &mut egui::Ui, add: impl FnOnce(&mut egui::Ui) -> R) -> R {
        egui::Frame::none().fill(CARD).rounding(8.0).inner_margin(12.0).show(ui, |ui| {
            ui.set_width(ui.available_width());
            add(ui)
        }).inner
    }

    fn execution_tab(&mut self, ui: &mut egui::Ui) {
        let busy = self.busy.load(Ordering::SeqCst);
        ui.horizontal(|ui| {
            if ui.add_enabled(!busy, egui::Button::new("1 · Open game in Chrome")).clicked() {
                self.connect_browser();
            }
            if ui.add_enabled(!busy, egui::Button::new("2 · Verify configuration")).clicked() {
                self.check_configuration();
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                let run = egui::Button::new(RichText::new(if busy { "⏳ Running..." } else { "▶ Run now" }).color(Color32::WHITE)).fill(GREEN);
                if ui.add_enabled(!busy, run).clicked() {
                    self.confirm("Run now", "Run the enabled modules now with saved configuration?\n\nThe browser will be automated.",
                                 Action::RunAll);
                }
                let stop = egui::Button::new(if self.stopping { "stopping..." } else { "■ Stop" });
                if ui.add_enabled(busy && self.stoppable && !self.stopping, stop).clicked() {
                    self.stop_run();
                }
                ui.checkbox(&mut self.force, "Ignore schedule");
            });
        });
        ui.add_space(6.0);
        Self::card(ui, |ui| ui.label(RichText::new(&self.info).color(Color32::from_rgb(0xc9, 0xd1, 0xd9))));
        ui.add_space(6.0);
        self.scheduler_card(ui);
        ui.add_space(6.0);
        ui.label(RichText::new("Execution Log").strong().color(MUTED));
        let footer_height = 30.0;
        let height = (ui.available_height() - footer_height).max(100.0);
        egui::Frame::none().fill(DARK).inner_margin(6.0).show(ui, |ui| {
            ui.set_width(ui.available_width());
            egui::ScrollArea::vertical().max_height(height).stick_to_bottom(true).auto_shrink([false, false]).show(ui, |ui| {
                let log = self.log.lock().unwrap();
                for (level, line) in log.iter() {
                    let color = match level {
                        Level::Error => ERR,
                        Level::Warning => WARN,
                        Level::Debug => MUTED,
                        Level::Info => Color32::from_rgb(0xc9, 0xd1, 0xd9),
                    };
                    ui.label(RichText::new(line).monospace().size(11.5).color(color));
                }
            });
        });
        ui.horizontal(|ui| {
            ui.label(RichText::new("Hold ESC to cancel an ongoing run from any window.  ·  Every run ends its log with a summary.")
                .small().color(MUTED));
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if ui.button("Open logs folder").clicked() {
                    let dir = self.config.resolved_log_dir();
                    let _ = std::fs::create_dir_all(&dir);
                    let _ = std::process::Command::new("explorer").arg(dir).spawn();
                }
            });
        });
    }

    fn modules_tab(&mut self, ui: &mut egui::Ui) {
        let busy = self.busy.load(Ordering::SeqCst);
        ui.horizontal(|ui| {
            ui.label(RichText::new("What a run does. Each module is turned on or off here and runs on every profile that has what it \
                                    needs (Accounts and profiles).").color(MUTED));
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if ui.button("💾 Save modules").clicked() {
                    self.save_parameters();
                }
            });
        });
        let mut run: Option<Kind> = None;
        egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
            for kind in KINDS {
                let section = kind.section();
                Self::card(ui, |ui| {
                    ui.horizontal(|ui| {
                        ui.label(RichText::new(kind.title()).size(16.0).strong().color(ACCENT));
                        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                            let b = egui::Button::new(RichText::new("▶ Run only this module").color(Color32::WHITE)).fill(GREEN);
                            if ui.add_enabled(!busy, b).clicked() {
                                run = Some(kind);
                            }
                        });
                    });
                    ui.add(egui::Label::new(RichText::new(kind.description()).small().color(MUTED)).wrap());
                    if let Some((text, color)) = self.module_status.get(kind.key()) {
                        ui.label(RichText::new(text).small().color(*color));
                    }
                    ui.add_space(4.0);
                    let edits = self.edits.entry(section.key.clone()).or_default();
                    for spec in &section.fields {
                        let edit = edits.entry(spec.key.to_string()).or_insert_with(|| edit_of(None, spec));
                        Self::field(ui, spec, edit);
                    }
                });
                ui.add_space(8.0);
            }
        });
        if let Some(kind) = run {
            self.confirm(kind.title(), &format!("Run only '{}' now with saved configuration?\n\nThe other modules are left out and the \
                                                 schedule is ignored.\nThe browser will be automated.", kind.title()),
                         Action::RunModule(kind));
        }
    }

    fn parameters_tab(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.label(RichText::new("Parameters every module shares.").color(MUTED));
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if ui.button("Restore defaults").clicked() {
                    self.confirm("Restore defaults", "Reset all parameters to default values?\nAccounts and profiles are not affected.",
                                 Action::RestoreDefaults);
                }
                if ui.button("💾 Save parameters").clicked() {
                    self.save_parameters();
                }
            });
        });
        egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
            for section in shared_sections() {
                Self::card(ui, |ui| {
                    ui.label(RichText::new(&section.label).size(16.0).strong().color(ACCENT));
                    ui.add(egui::Label::new(RichText::new(&section.description).small().color(MUTED)).wrap());
                    ui.add_space(4.0);
                    let edits = self.edits.entry(section.key.clone()).or_default();
                    for spec in section.fields.iter().filter(|f| f.in_parameters_tab) {
                        let edit = edits.entry(spec.key.to_string()).or_insert_with(|| edit_of(None, spec));
                        Self::field(ui, spec, edit);
                    }
                });
                ui.add_space(8.0);
            }
        });
    }

    fn labelled(ui: &mut egui::Ui, label: &str, width: f32, text: &mut String, hint: &str, password: bool) {
        ui.vertical(|ui| {
            ui.label(RichText::new(label).small().color(MUTED));
            ui.add(egui::TextEdit::singleline(text).desired_width(width).hint_text(hint).password(password));
        });
    }

    fn profile_summary(p: &ProfileEdit) -> (String, Color32) {
        let probe = ProfileConfig { name: p.name.clone(), enabled: p.enabled, database: p.database.clone(), account_name: String::new(),
                                    modules: p.modules.clone() };
        let extras: Vec<String> = KINDS.iter().map(|k| k.profile_summary(&probe)).filter(|s| !s.is_empty()).collect();
        match &p.database {
            Some(d) if d.is_usable() => {
                let mut parts = vec![format!("{}@{}/{}", d.user, d.host, d.database)];
                parts.extend(extras);
                (parts.join("  ·  "), MUTED)
            }
            _ if !extras.is_empty() => {
                let mut parts = vec!["no database (no chests)".to_string()];
                parts.extend(extras);
                (parts.join("  ·  "), WARN)
            }
            _ => ("no database — profile will be skipped".into(), WARN),
        }
    }

    fn accounts_tab(&mut self, ui: &mut egui::Ui) {
        let mut save = false;
        ui.horizontal(|ui| {
            ui.label(RichText::new("Each account is a game login; each profile is a city within it. Collection follows this order: \
                                    account, then its profiles.").color(MUTED));
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if ui.button("+ New account").clicked() {
                    let n = self.accounts.len() + 1;
                    self.accounts.push(AccountEdit::of(&AccountConfig { name: format!("Account {}", n),
                                                                        profiles: vec![ProfileConfig::default()], ..Default::default() }));
                }
                if ui.button("💾 Save accounts").clicked() {
                    save = true;
                }
            });
        });
        let mut open_window = None;
        let mut remove = None;
        let mut open_db = None;
        let mut open_modules = None;
        egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
            for (ai, account) in self.accounts.iter_mut().enumerate() {
                Self::card(ui, |ui| {
                    ui.horizontal(|ui| {
                        ui.vertical(|ui| {
                            ui.label(RichText::new("Active").small().color(MUTED));
                            ui.checkbox(&mut account.enabled, "");
                        });
                        Self::labelled(ui, "Account name", 150.0, &mut account.name, "a name only you see", false);
                        Self::labelled(ui, "Game login (email or username)", 200.0, &mut account.login, "what you type in the game", false);
                        ui.vertical(|ui| {
                            ui.label(RichText::new("Password").small().color(MUTED));
                            ui.horizontal(|ui| {
                                ui.add(egui::TextEdit::singleline(&mut account.password).desired_width(140.0)
                                    .hint_text("game password").password(!account.show_password));
                                if ui.button("👁").clicked() {
                                    account.show_password = !account.show_password;
                                }
                            });
                        });
                        ui.with_layout(egui::Layout::right_to_left(egui::Align::Max), |ui| {
                            if ui.button(RichText::new("Remove account").color(ERR)).clicked() {
                                remove = Some(ai);
                            }
                        });
                    });
                    ui.horizontal(|ui| {
                        Self::labelled(ui, "Chrome folder", 330.0, &mut account.browser_profile, "empty = chosen when saved", false);
                        Self::labelled(ui, "Debugging port", 80.0, &mut account.port, "auto", false);
                        ui.vertical(|ui| {
                            ui.label("");
                            if ui.button("Open window").clicked() {
                                open_window = Some(ai);
                            }
                        });
                    });
                    ui.add(egui::Label::new(RichText::new("This account's own Chrome window. The folder keeps the session the game \
                        verified: the first login is by hand (Open window, then the code from the e-mail). Change the folder or the \
                        port only if they clash with something else; a new folder asks for a new code.").small().color(MUTED)).wrap());
                    ui.add_space(6.0);
                    ui.label(RichText::new("Profiles (cities) for this account, collected in this order:").small().color(MUTED));
                    let mut remove_profile = None;
                    for (pi, profile) in account.profiles.iter_mut().enumerate() {
                        egui::Frame::none().fill(DARK).rounding(6.0).inner_margin(6.0).show(ui, |ui| {
                            ui.set_width(ui.available_width());
                            ui.horizontal(|ui| {
                                ui.checkbox(&mut profile.enabled, "");
                                ui.add(egui::TextEdit::singleline(&mut profile.name).desired_width(220.0)
                                    .hint_text("Profile name as shown in the game"));
                                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                                    if ui.button(RichText::new("✖").color(ERR)).on_hover_text("Remove this profile").clicked() {
                                        remove_profile = Some(pi);
                                    }
                                    if ui.button("Modules…").clicked() {
                                        open_modules = Some((ai, pi));
                                    }
                                    if ui.button("Database…").clicked() {
                                        open_db = Some((ai, pi));
                                    }
                                    let (text, color) = Self::profile_summary(profile);
                                    ui.add(egui::Label::new(RichText::new(text).small().color(color)).truncate());
                                });
                            });
                        });
                        ui.add_space(3.0);
                    }
                    if let Some(pi) = remove_profile {
                        account.profiles.remove(pi);
                    }
                    if ui.button("+ Add profile").clicked() {
                        account.profiles.push(ProfileEdit { enabled: true, name: String::new(), database: None, modules: Map::new() });
                    }
                });
                ui.add_space(8.0);
            }
        });
        if let Some(i) = remove {
            self.confirm("Remove account", "Remove this account and all of its profiles?", Action::RemoveAccount(i));
        }
        if let Some(i) = open_window {
            self.open_account_window(i);
        }
        if let Some((a, p)) = open_db {
            let d = self.accounts[a].profiles[p].database.clone().unwrap_or_else(DatabaseConfig::new);
            self.dialog = Some(Dialog::Database { account: a, profile: p,
                                                  fields: [d.host, d.port.to_string(), d.user, d.password, d.database],
                                                  test: Arc::new(Mutex::new(None)) });
        }
        if let Some((a, p)) = open_modules {
            let profile = &self.accounts[a].profiles[p];
            let mut values = HashMap::new();
            let mut tests = HashMap::new();
            for kind in KINDS {
                let fields = kind.profile_fields();
                if fields.is_empty() {
                    continue;
                }
                let stored = profile.modules.get(kind.key()).and_then(|v| v.as_object()).cloned().unwrap_or_default();
                values.insert(kind.key().to_string(), fields.iter().map(|f| (f.key.to_string(), edit_of(stored.get(f.key), f))).collect());
                tests.insert(kind.key().to_string(), Arc::new(Mutex::new(None)));
            }
            self.dialog = Some(Dialog::Modules { account: a, profile: p, values, tests });
        }
        if save {
            self.save_accounts();
        }
    }

    fn dialogs(&mut self, ctx: &egui::Context) {
        let Some(dialog) = self.dialog.as_mut() else { return };
        let mut close = false;
        let mut then: Option<Action> = None;
        match dialog {
            Dialog::Message { title, text, error } => {
                egui::Window::new(title.as_str()).collapsible(false).resizable(false).anchor(egui::Align2::CENTER_CENTER, [0.0, 0.0])
                    .show(ctx, |ui| {
                        ui.set_max_width(520.0);
                        ui.label(RichText::new(text.as_str()).color(if *error { ERR } else { Color32::from_rgb(0xc9, 0xd1, 0xd9) }));
                        ui.add_space(8.0);
                        if ui.button("OK").clicked() {
                            close = true;
                        }
                    });
            }
            Dialog::Confirm { title, text, action } => {
                egui::Window::new(title.as_str()).collapsible(false).resizable(false).anchor(egui::Align2::CENTER_CENTER, [0.0, 0.0])
                    .show(ctx, |ui| {
                        ui.set_max_width(480.0);
                        ui.label(text.as_str());
                        ui.add_space(8.0);
                        ui.horizontal(|ui| {
                            if ui.button("Yes").clicked() {
                                then = Some(action.clone());
                                close = true;
                            }
                            if ui.button("No").clicked() {
                                close = true;
                            }
                        });
                    });
            }
            Dialog::Database { account, profile, fields, test } => {
                let name = self.accounts[*account].profiles[*profile].name.clone();
                let mut result: Option<Option<DatabaseConfig>> = None;
                egui::Window::new(format!("Database — {}", if name.is_empty() { "new profile" } else { &name }))
                    .collapsible(false).resizable(false).anchor(egui::Align2::CENTER_CENTER, [0.0, 0.0])
                    .show(ctx, |ui| {
                        ui.label(RichText::new("Database credentials for this profile").strong().color(ACCENT));
                        ui.label(RichText::new("A profile without a database is skipped during collection.").small().color(MUTED));
                        ui.add_space(6.0);
                        egui::Grid::new("db").num_columns(2).show(ui, |ui| {
                            for (i, label) in ["Host", "Port", "User", "Password", "Database"].iter().enumerate() {
                                ui.label(*label);
                                ui.add(egui::TextEdit::singleline(&mut fields[i]).desired_width(280.0).password(i == 3));
                                ui.end_row();
                            }
                        });
                        let collect = |f: &[String; 5]| DatabaseConfig::from_json(&json!({"host": f[0].trim(), "port": f[1].trim(),
                            "user": f[2].trim(), "password": f[3], "database": f[4].trim()})).unwrap_or_else(DatabaseConfig::new);
                        let shown = test.lock().unwrap().clone();
                        if let Some((ok, text)) = shown {
                            ui.label(RichText::new(format!("{} {}", if ok { "✔" } else { "✖" }, text)).small().color(if ok { OK } else { ERR }));
                        }
                        ui.horizontal(|ui| {
                            if ui.button("Test connection").clicked() {
                                let db = collect(fields);
                                let slot = test.clone();
                                *slot.lock().unwrap() = Some((true, "Testing...".into()));
                                thread::spawn(move || {
                                    let r = test_connection(&db);
                                    *slot.lock().unwrap() = Some(r);
                                });
                            }
                            if ui.button("Clear database").clicked() {
                                result = Some(None);
                            }
                            if ui.button("Save").clicked() {
                                let db = collect(fields);
                                let any = db.is_usable() || !db.host.is_empty() || !db.user.is_empty() || !db.database.is_empty();
                                result = Some(if any { Some(db) } else { None });
                            }
                            if ui.button("Cancel").clicked() {
                                close = true;
                            }
                        });
                    });
                if let Some(db) = result {
                    self.accounts[*account].profiles[*profile].database = db;
                    close = true;
                }
            }
            Dialog::Modules { account, profile, values, tests } => {
                let name = self.accounts[*account].profiles[*profile].name.clone();
                let mut save = false;
                egui::Window::new(format!("Modules — {}", if name.is_empty() { "new profile" } else { &name }))
                    .collapsible(false).resizable(true).default_width(560.0).anchor(egui::Align2::CENTER_CENTER, [0.0, 0.0])
                    .show(ctx, |ui| {
                        ui.label(RichText::new("Module settings for this profile").strong().color(ACCENT));
                        ui.add(egui::Label::new(RichText::new("Each profile belongs to one clan, so each one has its own values. A module \
                                                               left empty here is not run on this profile.").small().color(MUTED)).wrap());
                        for kind in KINDS {
                            let fields = kind.profile_fields();
                            let Some(edits) = values.get_mut(kind.key()) else { continue };
                            Self::card(ui, |ui| {
                                ui.label(RichText::new(kind.title()).strong().color(ACCENT));
                                for spec in &fields {
                                    let edit = edits.entry(spec.key.to_string()).or_insert_with(|| edit_of(None, spec));
                                    ui.horizontal(|ui| {
                                        ui.allocate_ui_with_layout(egui::vec2(130.0, 20.0),
                                                                   egui::Layout::left_to_right(egui::Align::Center), |ui| {
                                            ui.set_width(130.0);
                                            ui.label(spec.label);
                                        });
                                        match edit {
                                            Edit::Flag(b) => {
                                                ui.checkbox(b, "");
                                            }
                                            Edit::Text(t) => {
                                                ui.add(egui::TextEdit::singleline(t).desired_width(320.0).password(spec.is_secret()));
                                            }
                                        }
                                    });
                                    if !spec.help.is_empty() {
                                        ui.add(egui::Label::new(RichText::new(spec.help).small().color(MUTED)).wrap());
                                    }
                                }
                                if kind.can_test_profile() {
                                    let slot = tests[kind.key()].clone();
                                    if ui.button("Test connection").clicked() {
                                        let typed: Map<String, Json> =
                                            fields.iter().map(|f| (f.key.to_string(), coerce(&json_of(&edits[f.key]), Some(f)))).collect();
                                        *slot.lock().unwrap() = Some((true, "Testing...".into()));
                                        let s = slot.clone();
                                        thread::spawn(move || {
                                            let r = kind.test_profile(&typed).unwrap_or((false, "Nothing to test.".into()));
                                            *s.lock().unwrap() = Some(r);
                                        });
                                    }
                                    let shown = slot.lock().unwrap().clone();
                                    if let Some((ok, text)) = shown {
                                        ui.label(RichText::new(format!("{} {}", if ok { "✔" } else { "✖" }, text)).small()
                                            .color(if ok { OK } else { ERR }));
                                    }
                                }
                            });
                            ui.add_space(4.0);
                        }
                        ui.horizontal(|ui| {
                            if ui.button("Save").clicked() {
                                save = true;
                            }
                            if ui.button("Cancel").clicked() {
                                close = true;
                            }
                        });
                    });
                if save {
                    let mut modules = self.accounts[*account].profiles[*profile].modules.clone();
                    for kind in KINDS {
                        let fields = kind.profile_fields();
                        let Some(edits) = values.get(kind.key()) else { continue };
                        let typed: Map<String, Json> =
                            fields.iter().map(|f| (f.key.to_string(), coerce(&json_of(&edits[f.key]), Some(f)))).collect();
                        // Nothing typed is nothing stored: the profile simply does not use the module.
                        if fields.iter().any(|f| typed[f.key] != f.default) {
                            modules.insert(kind.key().to_string(), Json::Object(typed));
                        } else {
                            modules.remove(kind.key());
                        }
                    }
                    self.accounts[*account].profiles[*profile].modules = modules;
                    close = true;
                }
            }
        }
        if close {
            self.dialog = None;
        }
        if let Some(action) = then {
            self.apply(action);
        }
    }
}

impl eframe::App for GuiApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        let busy = self.busy.load(Ordering::SeqCst);
        if !busy && (self.stoppable || self.stopping) {
            self.stoppable = false;
            self.stopping = false;
            self.refresh_status();
        }
        egui::TopBottomPanel::top("header").show(ctx, |ui| {
            ui.add_space(6.0);
            ui.horizontal(|ui| {
                ui.label(RichText::new("Total Battle Chest Collector").size(20.0).strong().color(ACCENT));
                ui.label(RichText::new(format!("v{}", crate::VERSION)).small().color(MUTED));
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    ui.label(RichText::new(&self.status.0).color(self.status.1));
                });
            });
            ui.add_space(4.0);
            ui.horizontal(|ui| {
                for (tab, label) in [(Tab::Execution, "Execution"), (Tab::Modules, "Modules"), (Tab::Parameters, "Parameters"),
                                     (Tab::Accounts, "Accounts and profiles")] {
                    ui.selectable_value(&mut self.tab, tab, RichText::new(label).size(14.0));
                }
            });
            ui.add_space(4.0);
        });
        egui::CentralPanel::default().show(ctx, |ui| match self.tab {
            Tab::Execution => self.execution_tab(ui),
            Tab::Modules => self.modules_tab(ui),
            Tab::Parameters => self.parameters_tab(ui),
            Tab::Accounts => self.accounts_tab(ui),
        });
        self.dialogs(ctx);
        ctx.request_repaint_after(Duration::from_millis(if busy { 150 } else { 500 }));
    }

    fn on_exit(&mut self) {
        logger::remove_sink(self.sink);
        CANCELLATION.cancel("the interface was closed");
        if let Some(ctx) = self.context.lock().unwrap().take() {
            ctx.close();
        }
    }
}

/// Opens the interface; returns the exit code once it is closed.
pub fn launch(config: ConfigManager) -> i32 {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title("Total Battle Chest Collector — Configuration")
            .with_inner_size([980.0, 700.0])
            .with_min_inner_size([900.0, 520.0]),
        renderer: eframe::Renderer::Wgpu,
        ..Default::default()
    };
    let result = eframe::run_native("Total Battle Chest Collector", options, Box::new(move |cc| {
        cc.egui_ctx.set_visuals(egui::Visuals::dark());
        Ok(Box::new(GuiApp::new(config)))
    }));
    match result {
        Ok(()) => 0,
        Err(e) => {
            fatal("Total Battle Chest Collector", &format!("The interface could not be opened: {}", e));
            crate::cli::EXIT_MISCONFIGURED
        }
    }
}

/// A message for a person when there is no window and no console.
pub fn fatal(title: &str, message: &str) {
    crate::error!("{}", message);
    #[cfg(windows)]
    {
        use windows_sys::Win32::UI::WindowsAndMessaging::{MessageBoxW, MB_ICONERROR, MB_OK};
        let wide = |s: &str| s.encode_utf16().chain(std::iter::once(0)).collect::<Vec<u16>>();
        let (t, m) = (wide(title), wide(message));
        unsafe {
            MessageBoxW(std::ptr::null_mut(), m.as_ptr(), t.as_ptr(), MB_OK | MB_ICONERROR);
        }
    }
    #[cfg(not(windows))]
    eprintln!("{}: {}", title, message);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::module_section_key;

    fn config(dir: &std::path::Path) -> ConfigManager {
        let mut c = ConfigManager::open(&dir.join("config").join("config.json")).unwrap();
        c.set("browser", "profiles_dir", json!(dir.to_string_lossy()));
        let mut alpha = ProfileConfig::named("Alpha", "Main");
        alpha.database = Some(DatabaseConfig { host: "h".into(), port: 3306, user: "u".into(), password: String::new(), database: "a".into() });
        c.set_accounts(&[AccountConfig { name: "Main".into(), login: "a".into(), password: "b".into(), profiles: vec![alpha],
                                         ..Default::default() }]);
        c.save().unwrap();
        c
    }

    #[test]
    fn every_module_has_its_switch_schedule_and_parameters_and_turning_one_off_is_saved() {
        let dir = tempfile::tempdir().unwrap();
        let mut app = GuiApp::new(config(dir.path()));
        for kind in KINDS {
            let edits = &app.edits[&module_section_key(kind.key())];
            assert!(edits.contains_key("enabled") && edits.contains_key("schedule"));
            for f in kind.settings() {
                assert!(edits.contains_key(f.key), "{}", f.key);
            }
        }
        app.edits.get_mut("modules.events").unwrap().insert("enabled".into(), Edit::Flag(false));
        app.edits.get_mut("modules.events").unwrap().insert("days_back".into(), Edit::Text("14".into()));
        app.save_parameters();
        let again = ConfigManager::open(&app.config.path).unwrap();
        assert!(!again.get_bool("modules.events", "enabled", true));
        assert_eq!(14, again.get_i64("modules.events", "days_back", 0));
        assert!(app.module_status["events"].0.contains("Disabled"));
    }

    #[test]
    fn each_account_shows_its_port_and_folder_and_bad_ports_or_shared_databases_are_not_saved() {
        let dir = tempfile::tempdir().unwrap();
        let mut app = GuiApp::new(config(dir.path()));
        assert_eq!("9322", app.accounts[0].port, "pinned when the window opened");
        assert!(!app.accounts[0].browser_profile.is_empty());

        app.accounts[0].port = "80".into();
        app.save_accounts();
        assert!(matches!(&app.dialog, Some(Dialog::Message { error: true, text, .. }) if text.contains("not a debugging port")));
        app.accounts[0].port = String::new();
        app.save_accounts();
        assert_eq!("9322", app.accounts[0].port, "an empty port gets a free one when saved");

        let mut twin = app.accounts[0].profiles[0].clone();
        twin.name = "Beta".into();
        app.accounts[0].profiles.push(twin);
        app.save_accounts();
        assert!(matches!(&app.dialog, Some(Dialog::Message { error: true, text, .. }) if text.contains("same database")));
        assert_eq!(1, ConfigManager::open(&app.config.path).unwrap().accounts()[0].profiles.len());
    }
}
