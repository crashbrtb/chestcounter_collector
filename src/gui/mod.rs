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

use eframe::egui::{self, Color32, RichText, Stroke};
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

mod theme;

use theme::{ACCENT, BG, BORDER, CARD, ERR, INPUT, MUTED, OK, PANEL, TEXT, TEXT_DIM, WARN};

const MAX_LOG_LINES: usize = 5000;
/// Width of the label column in front of every parameter.
const LABEL_WIDTH: f32 = 210.0;

/// The numbers the Execution page opens with.
#[derive(Default)]
struct Overview {
    accounts: usize,
    profiles: usize,
    with_database: usize,
    modules: Vec<String>,
}

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
    overview: Overview,
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
            overview: Overview::default(),
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
        self.overview = Overview { accounts: accounts.len(), profiles: profiles.len(), with_database: collectable, modules: summary };
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
            ui.horizontal(|ui| {
                ui.label(theme::title("⏰  Windows Task Scheduler", 15.0));
            });
            theme::pill(ui, &text, color);
            ui.add_space(2.0);
            ui.horizontal_wrapped(|ui| {
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
                if ui.add(theme::primary(if scheduled { "Update schedule" } else { "Schedule" }).min_size(egui::vec2(0.0, 28.0))).clicked() {
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

    /// The input of one parameter. `salt` keeps equal fields apart: every module has a
    /// "Runs" choice, and combo boxes sharing one id open and close in the same click.
    fn widget(ui: &mut egui::Ui, salt: &str, spec: &Field, edit: &mut Edit, width: f32) {
        match edit {
            Edit::Flag(b) => {
                theme::toggle(ui, b);
            }
            Edit::Text(t) if spec.kind == FieldType::Choice => {
                egui::ComboBox::from_id_salt((salt, spec.key)).selected_text(t.clone()).width(width.min(220.0)).show_ui(ui, |ui| {
                    for c in &spec.choices {
                        ui.selectable_value(t, c.to_string(), *c);
                    }
                });
            }
            Edit::Text(t) => {
                ui.add(egui::TextEdit::singleline(t).desired_width(width).margin(egui::vec2(8.0, 5.0)).password(spec.is_secret()));
            }
        }
    }

    fn field(ui: &mut egui::Ui, salt: &str, spec: &Field, edit: &mut Edit) {
        ui.horizontal(|ui| {
            ui.allocate_ui_with_layout(egui::vec2(LABEL_WIDTH, 28.0), egui::Layout::left_to_right(egui::Align::Center), |ui| {
                ui.set_width(LABEL_WIDTH);
                ui.add(egui::Label::new(RichText::new(spec.label).color(TEXT_DIM)).wrap());
            });
            Self::widget(ui, salt, spec, edit, 280.0);
        });
        if !spec.help.is_empty() {
            ui.add_space(-5.0);
            ui.horizontal(|ui| {
                ui.add_space(LABEL_WIDTH + ui.spacing().item_spacing.x);
                ui.add(egui::Label::new(RichText::new(spec.help).small().color(MUTED)).wrap());
            });
        }
        ui.add_space(2.0);
    }

    fn card<R>(ui: &mut egui::Ui, add: impl FnOnce(&mut egui::Ui) -> R) -> R {
        egui::Frame::none().fill(CARD).stroke(Stroke::new(1.0f32, BORDER)).rounding(10.0).inner_margin(16.0).show(ui, |ui| {
            ui.set_width(ui.available_width());
            add(ui)
        }).inner
    }

    /// The title of a page, its actions on the right and a line saying what it is for.
    fn page_header(ui: &mut egui::Ui, title: &str, subtitle: &str, actions: impl FnOnce(&mut egui::Ui)) {
        ui.horizontal(|ui| {
            ui.label(theme::title(title, 22.0));
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), actions);
        });
        ui.add(egui::Label::new(RichText::new(subtitle).color(MUTED)).wrap());
        ui.add_space(8.0);
    }

    fn stat(ui: &mut egui::Ui, label: &str, value: String, color: Color32) {
        Self::card(ui, |ui| {
            ui.spacing_mut().item_spacing.y = 2.0;
            ui.label(RichText::new(label).small().color(MUTED));
            ui.label(RichText::new(value).family(theme::semibold()).size(26.0).color(color));
        });
    }

    fn execution_tab(&mut self, ui: &mut egui::Ui) {
        let busy = self.busy.load(Ordering::SeqCst);
        Self::page_header(ui, "Execution", "Open the game, check the setup, then run the enabled modules. The log below shows \
                          everything a run does.", |ui| {
            if ui.add_enabled(!busy, theme::success(if busy { "⏳  Running..." } else { "▶  Run now" })).clicked() {
                self.confirm("Run now", "Run the enabled modules now with saved configuration?\n\nThe browser will be automated.",
                             Action::RunAll);
            }
            let stop = theme::secondary(if self.stopping { "Stopping..." } else { "■  Stop" });
            if ui.add_enabled(busy && self.stoppable && !self.stopping, stop).clicked() {
                self.stop_run();
            }
            theme::toggle(ui, &mut self.force).on_hover_text("Run every enabled module now, even the ones not due yet");
            ui.label(RichText::new("Ignore schedule").color(TEXT_DIM));
        });
        ui.horizontal(|ui| {
            if ui.add_enabled(!busy, theme::secondary("1  ·  Open game in Chrome")).clicked() {
                self.connect_browser();
            }
            if ui.add_enabled(!busy, theme::secondary("2  ·  Verify configuration")).clicked() {
                self.check_configuration();
            }
            if busy {
                ui.add_space(6.0);
                ui.add(egui::Spinner::new().size(16.0).color(WARN));
                ui.label(RichText::new(if self.stopping { "Stopping at the next safe point..." } else { "Working..." }).color(WARN));
            }
        });
        ui.add_space(4.0);
        let o = &self.overview;
        ui.columns(4, |cols| {
            Self::stat(&mut cols[0], "Accounts", o.accounts.to_string(), TEXT);
            Self::stat(&mut cols[1], "Profiles", o.profiles.to_string(), TEXT);
            Self::stat(&mut cols[2], "With a database", o.with_database.to_string(), if o.with_database > 0 { OK } else { WARN });
            Self::stat(&mut cols[3], "Modules enabled", o.modules.len().to_string(), if o.modules.is_empty() { WARN } else { ACCENT });
        });
        let modules = if o.modules.is_empty() { "none enabled".to_string() } else { o.modules.join(",  ") };
        ui.add(egui::Label::new(RichText::new(format!("Modules: {}", modules)).small().color(MUTED)).wrap());
        ui.add_space(-4.0);
        ui.add(egui::Label::new(RichText::new(format!("Configuration: {}", self.config.path.display())).small().color(MUTED)).wrap());
        ui.add_space(4.0);
        self.scheduler_card(ui);
        ui.add_space(6.0);
        ui.horizontal(|ui| {
            ui.label(theme::title("Execution log", 15.0));
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if ui.button("📂  Open logs folder").clicked() {
                    let dir = self.config.resolved_log_dir();
                    let _ = std::fs::create_dir_all(&dir);
                    let _ = std::process::Command::new("explorer").arg(dir).spawn();
                }
            });
        });
        let footer_height = 30.0;
        let height = (ui.available_height() - footer_height - 20.0).max(120.0);
        egui::Frame::none().fill(INPUT).stroke(Stroke::new(1.0f32, BORDER)).rounding(10.0).inner_margin(10.0).show(ui, |ui| {
            ui.set_width(ui.available_width());
            egui::ScrollArea::vertical().max_height(height).stick_to_bottom(true).auto_shrink([false, false]).show(ui, |ui| {
                ui.spacing_mut().item_spacing.y = 2.0;
                let log = self.log.lock().unwrap();
                if log.is_empty() {
                    ui.label(RichText::new("Nothing yet: the log of the next run appears here.").monospace().color(MUTED));
                }
                for (level, line) in log.iter() {
                    let color = match level {
                        Level::Error => ERR,
                        Level::Warning => WARN,
                        Level::Debug => MUTED,
                        Level::Info => TEXT_DIM,
                    };
                    ui.label(RichText::new(line).monospace().color(color));
                }
            });
        });
        ui.label(RichText::new("Hold ESC to cancel an ongoing run from any window.  ·  Every run ends its log with a summary.")
            .small().color(MUTED));
    }

    fn modules_tab(&mut self, ui: &mut egui::Ui) {
        let busy = self.busy.load(Ordering::SeqCst);
        Self::page_header(ui, "Modules", "What a run does. Each module is turned on or off here and runs on every profile that has \
                          what it needs (Accounts and profiles).", |ui| {
            if ui.add(theme::primary("💾  Save modules")).clicked() {
                self.save_parameters();
            }
        });
        let mut run: Option<Kind> = None;
        egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
            for kind in KINDS {
                let section = kind.section();
                Self::card(ui, |ui| {
                    ui.horizontal(|ui| {
                        ui.label(theme::title(kind.title(), 17.0));
                        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                            if ui.add_enabled(!busy, theme::success("▶  Run only this module")).clicked() {
                                run = Some(kind);
                            }
                        });
                    });
                    ui.add(egui::Label::new(RichText::new(kind.description()).color(MUTED)).wrap());
                    if let Some((text, color)) = self.module_status.get(kind.key()) {
                        theme::pill(ui, text, *color);
                    }
                    ui.add_space(2.0);
                    ui.separator();
                    ui.add_space(2.0);
                    let edits = self.edits.entry(section.key.clone()).or_default();
                    for spec in &section.fields {
                        let edit = edits.entry(spec.key.to_string()).or_insert_with(|| edit_of(None, spec));
                        Self::field(ui, &section.key, spec, edit);
                    }
                });
                ui.add_space(10.0);
            }
        });
        if let Some(kind) = run {
            self.confirm(kind.title(), &format!("Run only '{}' now with saved configuration?\n\nThe other modules are left out and the \
                                                 schedule is ignored.\nThe browser will be automated.", kind.title()),
                         Action::RunModule(kind));
        }
    }

    fn parameters_tab(&mut self, ui: &mut egui::Ui) {
        Self::page_header(ui, "Parameters", "Parameters every module shares.", |ui| {
            if ui.add(theme::primary("💾  Save parameters")).clicked() {
                self.save_parameters();
            }
            if ui.add(theme::secondary("Restore defaults")).clicked() {
                self.confirm("Restore defaults", "Reset all parameters to default values?\nAccounts and profiles are not affected.",
                             Action::RestoreDefaults);
            }
        });
        egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
            for section in shared_sections() {
                Self::card(ui, |ui| {
                    ui.label(theme::title(section.label.as_str(), 17.0));
                    ui.add(egui::Label::new(RichText::new(&section.description).color(MUTED)).wrap());
                    ui.add_space(2.0);
                    ui.separator();
                    ui.add_space(2.0);
                    let edits = self.edits.entry(section.key.clone()).or_default();
                    for spec in section.fields.iter().filter(|f| f.in_parameters_tab) {
                        let edit = edits.entry(spec.key.to_string()).or_insert_with(|| edit_of(None, spec));
                        Self::field(ui, &section.key, spec, edit);
                    }
                });
                ui.add_space(10.0);
            }
        });
    }

    fn labelled(ui: &mut egui::Ui, label: &str, width: f32, text: &mut String, hint: &str, password: bool) {
        ui.vertical(|ui| {
            ui.spacing_mut().item_spacing.y = 4.0;
            ui.label(RichText::new(label).small().color(MUTED));
            ui.add(egui::TextEdit::singleline(text).desired_width(width).margin(egui::vec2(8.0, 5.0)).hint_text(hint).password(password));
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
        let mut add = false;
        Self::page_header(ui, "Accounts and profiles", "Each account is a game login; each profile is a city within it. Collection \
                          follows this order: account, then its profiles.", |ui| {
            if ui.add(theme::primary("💾  Save accounts")).clicked() {
                save = true;
            }
            if ui.add(theme::secondary("+  New account")).clicked() {
                add = true;
            }
        });
        if add {
            let n = self.accounts.len() + 1;
            self.accounts.push(AccountEdit::of(&AccountConfig { name: format!("Account {}", n), profiles: vec![ProfileConfig::default()],
                                                                ..Default::default() }));
        }
        let mut open_window = None;
        let mut remove = None;
        let mut open_db = None;
        let mut open_modules = None;
        egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
            if self.accounts.is_empty() {
                Self::card(ui, |ui| {
                    ui.label(RichText::new("No account yet. Press “+ New account” to add your game login.").color(MUTED));
                });
            }
            for (ai, account) in self.accounts.iter_mut().enumerate() {
                Self::card(ui, |ui| {
                    ui.horizontal(|ui| {
                        theme::toggle(ui, &mut account.enabled).on_hover_text("Active: an inactive account is skipped by the runs");
                        let name = if account.name.trim().is_empty() { "Unnamed account".to_string() } else { account.name.trim().to_string() };
                        ui.label(theme::title(name, 17.0));
                        if !account.enabled {
                            theme::pill(ui, "inactive", MUTED);
                        }
                        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                            if ui.add(theme::danger("Remove account")).clicked() {
                                remove = Some(ai);
                            }
                            if ui.button("Open window").on_hover_text("Opens this account's own Chrome window, to log in by hand").clicked() {
                                open_window = Some(ai);
                            }
                        });
                    });
                    ui.add_space(4.0);
                    ui.horizontal_wrapped(|ui| {
                        Self::labelled(ui, "Account name", 170.0, &mut account.name, "a name only you see", false);
                        Self::labelled(ui, "Game login (email or username)", 230.0, &mut account.login, "what you type in the game", false);
                        ui.vertical(|ui| {
                            ui.spacing_mut().item_spacing.y = 4.0;
                            ui.label(RichText::new("Password").small().color(MUTED));
                            ui.horizontal(|ui| {
                                ui.add(egui::TextEdit::singleline(&mut account.password).desired_width(160.0).margin(egui::vec2(8.0, 5.0))
                                    .hint_text("game password").password(!account.show_password));
                                if ui.button(if account.show_password { "🙈" } else { "👁" }).on_hover_text("Show or hide the password")
                                    .clicked()
                                {
                                    account.show_password = !account.show_password;
                                }
                            });
                        });
                    });
                    ui.horizontal_wrapped(|ui| {
                        Self::labelled(ui, "Chrome folder", 360.0, &mut account.browser_profile, "empty = chosen when saved", false);
                        Self::labelled(ui, "Debugging port", 90.0, &mut account.port, "auto", false);
                    });
                    ui.add(egui::Label::new(RichText::new("This account's own Chrome window. The folder keeps the session the game \
                        verified: the first login is by hand (Open window, then the code from the e-mail). Change the folder or the \
                        port only if they clash with something else; a new folder asks for a new code.").small().color(MUTED)).wrap());
                    ui.add_space(6.0);
                    ui.horizontal(|ui| {
                        ui.label(theme::title("Profiles", 14.0));
                        ui.label(RichText::new("cities of this account, collected in this order").small().color(MUTED));
                    });
                    let mut remove_profile = None;
                    for (pi, profile) in account.profiles.iter_mut().enumerate() {
                        egui::Frame::none().fill(INPUT).stroke(Stroke::new(1.0f32, BORDER)).rounding(8.0)
                            .inner_margin(egui::Margin::symmetric(10.0, 8.0)).show(ui, |ui| {
                            ui.set_width(ui.available_width());
                            ui.horizontal(|ui| {
                                theme::toggle(ui, &mut profile.enabled).on_hover_text("Active: an inactive profile is skipped");
                                ui.add(egui::TextEdit::singleline(&mut profile.name).desired_width(220.0).margin(egui::vec2(8.0, 5.0))
                                    .hint_text("Profile name as shown in the game"));
                                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                                    if ui.add(theme::danger("✖")).on_hover_text("Remove this profile").clicked() {
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
                        ui.add_space(-2.0);
                    }
                    if let Some(pi) = remove_profile {
                        account.profiles.remove(pi);
                    }
                    ui.add_space(2.0);
                    if ui.button("+  Add profile").clicked() {
                        account.profiles.push(ProfileEdit { enabled: true, name: String::new(), database: None, modules: Map::new() });
                    }
                });
                ui.add_space(10.0);
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

    fn dialog_window(title: impl Into<egui::WidgetText>) -> egui::Window<'static> {
        egui::Window::new(title).collapsible(false).resizable(false).anchor(egui::Align2::CENTER_CENTER, [0.0, 0.0])
    }

    fn test_result(ui: &mut egui::Ui, shown: Option<(bool, String)>) {
        if let Some((ok, text)) = shown {
            theme::pill(ui, &format!("{}  {}", if ok { "✔" } else { "✖" }, text), if ok { OK } else { ERR });
        }
    }

    /// A row of buttons aligned to the right, only as tall as the buttons.
    fn button_row(ui: &mut egui::Ui, add: impl FnOnce(&mut egui::Ui)) {
        ui.allocate_ui_with_layout(egui::vec2(ui.available_width(), 32.0), egui::Layout::right_to_left(egui::Align::Center), add);
    }

    fn dialogs(&mut self, ctx: &egui::Context) {
        let Some(dialog) = self.dialog.as_mut() else { return };
        // Dims the window behind, so the dialog is what one looks at.
        ctx.layer_painter(egui::LayerId::new(egui::Order::PanelResizeLine, egui::Id::new("dialog-dim")))
            .rect_filled(ctx.screen_rect(), 0.0, Color32::from_black_alpha(150));
        let mut close = false;
        let mut then: Option<Action> = None;
        match dialog {
            Dialog::Message { title, text, error } => {
                Self::dialog_window(title.as_str()).show(ctx, |ui| {
                    ui.set_max_width(520.0);
                    ui.add(egui::Label::new(RichText::new(text.as_str()).color(if *error { ERR } else { TEXT_DIM })).wrap());
                    ui.add_space(10.0);
                    Self::button_row(ui, |ui| {
                        if ui.add(theme::primary("OK").min_size(egui::vec2(80.0, 32.0))).clicked() {
                            close = true;
                        }
                    });
                });
            }
            Dialog::Confirm { title, text, action } => {
                Self::dialog_window(title.as_str()).show(ctx, |ui| {
                    ui.set_max_width(480.0);
                    ui.add(egui::Label::new(RichText::new(text.as_str()).color(TEXT_DIM)).wrap());
                    ui.add_space(10.0);
                    Self::button_row(ui, |ui| {
                        if ui.add(theme::primary("Yes").min_size(egui::vec2(80.0, 32.0))).clicked() {
                            then = Some(action.clone());
                            close = true;
                        }
                        if ui.add(theme::secondary("No").min_size(egui::vec2(80.0, 32.0))).clicked() {
                            close = true;
                        }
                    });
                });
            }
            Dialog::Database { account, profile, fields, test } => {
                let name = self.accounts[*account].profiles[*profile].name.clone();
                let mut result: Option<Option<DatabaseConfig>> = None;
                Self::dialog_window(format!("Database — {}", if name.is_empty() { "new profile" } else { &name })).show(ctx, |ui| {
                    ui.label(theme::title("Database credentials for this profile", 15.0));
                    ui.label(RichText::new("A profile without a database is skipped during collection.").small().color(MUTED));
                    ui.add_space(6.0);
                    egui::Grid::new("db").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
                        for (i, label) in ["Host", "Port", "User", "Password", "Database"].iter().enumerate() {
                            ui.label(RichText::new(*label).color(TEXT_DIM));
                            ui.add(egui::TextEdit::singleline(&mut fields[i]).desired_width(300.0).margin(egui::vec2(8.0, 5.0))
                                .password(i == 3));
                            ui.end_row();
                        }
                    });
                    let collect = |f: &[String; 5]| DatabaseConfig::from_json(&json!({"host": f[0].trim(), "port": f[1].trim(),
                        "user": f[2].trim(), "password": f[3], "database": f[4].trim()})).unwrap_or_else(DatabaseConfig::new);
                    let shown = test.lock().unwrap().clone();
                    Self::test_result(ui, shown);
                    ui.add_space(6.0);
                    ui.horizontal(|ui| {
                        if ui.add(theme::secondary("Test connection")).clicked() {
                            let db = collect(fields);
                            let slot = test.clone();
                            *slot.lock().unwrap() = Some((true, "Testing...".into()));
                            thread::spawn(move || {
                                let r = test_connection(&db);
                                *slot.lock().unwrap() = Some(r);
                            });
                        }
                        if ui.add(theme::danger("Clear database").min_size(egui::vec2(0.0, 32.0))).clicked() {
                            result = Some(None);
                        }
                        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                            if ui.add(theme::primary("Save").min_size(egui::vec2(80.0, 32.0))).clicked() {
                                let db = collect(fields);
                                let any = db.is_usable() || !db.host.is_empty() || !db.user.is_empty() || !db.database.is_empty();
                                result = Some(if any { Some(db) } else { None });
                            }
                            if ui.add(theme::secondary("Cancel")).clicked() {
                                close = true;
                            }
                        });
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
                let max_height = ctx.screen_rect().height() * 0.65;
                Self::dialog_window(format!("Modules — {}", if name.is_empty() { "new profile" } else { &name }))
                    .default_width(600.0).show(ctx, |ui| {
                    ui.set_width(600.0);
                    ui.label(theme::title("Module settings for this profile", 15.0));
                    ui.add(egui::Label::new(RichText::new("Each profile belongs to one clan, so each one has its own values. A module \
                                                           left empty here is not run on this profile.").small().color(MUTED)).wrap());
                    ui.add_space(4.0);
                    egui::ScrollArea::vertical().max_height(max_height).auto_shrink([false, true]).show(ui, |ui| {
                        for kind in KINDS {
                            let fields = kind.profile_fields();
                            let Some(edits) = values.get_mut(kind.key()) else { continue };
                            Self::card(ui, |ui| {
                                ui.label(theme::title(kind.title(), 15.0));
                                for spec in &fields {
                                    let edit = edits.entry(spec.key.to_string()).or_insert_with(|| edit_of(None, spec));
                                    ui.horizontal(|ui| {
                                        ui.allocate_ui_with_layout(egui::vec2(140.0, 28.0),
                                                                   egui::Layout::left_to_right(egui::Align::Center), |ui| {
                                            ui.set_width(140.0);
                                            ui.label(RichText::new(spec.label).color(TEXT_DIM));
                                        });
                                        Self::widget(ui, kind.key(), spec, edit, 340.0);
                                    });
                                    if !spec.help.is_empty() {
                                        ui.add_space(-5.0);
                                        ui.add(egui::Label::new(RichText::new(spec.help).small().color(MUTED)).wrap());
                                    }
                                }
                                if kind.can_test_profile() {
                                    let slot = tests[kind.key()].clone();
                                    if ui.add(theme::secondary("Test connection")).clicked() {
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
                                    Self::test_result(ui, shown);
                                }
                            });
                            ui.add_space(6.0);
                        }
                    });
                    ui.add_space(6.0);
                    Self::button_row(ui, |ui| {
                        if ui.add(theme::primary("Save").min_size(egui::vec2(80.0, 32.0))).clicked() {
                            save = true;
                        }
                        if ui.add(theme::secondary("Cancel")).clicked() {
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

    fn sidebar(&mut self, ui: &mut egui::Ui, busy: bool) {
        ui.horizontal(|ui| {
            let (rect, _) = ui.allocate_exact_size(egui::vec2(36.0, 36.0), egui::Sense::hover());
            ui.painter().rect_filled(rect, 9.0, ACCENT);
            ui.painter().text(rect.center(), egui::Align2::CENTER_CENTER, "TB", egui::FontId::new(15.0, theme::semibold()),
                              Color32::WHITE);
            ui.vertical(|ui| {
                ui.spacing_mut().item_spacing.y = 0.0;
                ui.label(theme::title("Chest Collector", 16.0));
                ui.label(RichText::new(format!("Total Battle  ·  v{}", crate::VERSION)).small().color(MUTED));
            });
        });
        ui.add_space(20.0);
        ui.spacing_mut().item_spacing.y = 4.0;
        for (tab, icon, label) in [(Tab::Execution, "▶", "Execution"), (Tab::Modules, "📦", "Modules"), (Tab::Parameters, "⚙", "Parameters"),
                                   (Tab::Accounts, "👤", "Accounts")] {
            if theme::nav_item(ui, self.tab == tab, icon, label).clicked() {
                self.tab = tab;
            }
        }
        ui.with_layout(egui::Layout::bottom_up(egui::Align::Min), |ui| {
            ui.spacing_mut().item_spacing.y = 8.0;
            theme::pill(ui, &self.status.0, self.status.1);
            if busy {
                ui.horizontal(|ui| {
                    ui.add(egui::Spinner::new().size(14.0).color(WARN));
                    ui.label(RichText::new(if self.stopping { "Stopping..." } else { "Run in progress" }).small().color(WARN));
                });
            }
        });
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
        egui::SidePanel::left("nav")
            .resizable(false)
            .exact_width(210.0)
            .frame(egui::Frame::none().fill(PANEL).inner_margin(egui::Margin::symmetric(12.0, 18.0)))
            .show(ctx, |ui| self.sidebar(ui, busy));
        egui::CentralPanel::default()
            .frame(egui::Frame::none().fill(BG).inner_margin(egui::Margin::symmetric(24.0, 20.0)))
            .show(ctx, |ui| match self.tab {
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

/// The window's icon, decoded from the 256-pixel PNG built into the executable.
fn window_icon() -> egui::IconData {
    let decoder = png::Decoder::new(std::io::Cursor::new(&include_bytes!("../../assets/icon.png")[..]));
    let mut reader = decoder.read_info().expect("the built-in icon is a valid PNG");
    let mut rgba = vec![0; reader.output_buffer_size().expect("the built-in icon is small")];
    let info = reader.next_frame(&mut rgba).expect("the built-in icon is a valid PNG");
    rgba.truncate(info.buffer_size());
    egui::IconData { rgba, width: info.width, height: info.height }
}

/// Opens the interface; returns the exit code once it is closed.
pub fn launch(config: ConfigManager) -> i32 {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title("Total Battle Chest Collector — Configuration")
            .with_inner_size([1080.0, 700.0])
            .with_min_inner_size([960.0, 560.0])
            .with_icon(window_icon()),
        renderer: eframe::Renderer::Wgpu,
        ..Default::default()
    };
    let result = eframe::run_native("Total Battle Chest Collector", options, Box::new(move |cc| {
        theme::install(&cc.egui_ctx);
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
