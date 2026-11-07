//! What a feature module is, and the modules the application has.
//!
//! A module is one job done on a profile of the game. The configuration file, the
//! settings window and the run are built from what each module declares here
//! (key, title, description, its own parameters, what it needs to know about each
//! profile, when it runs), so adding one means writing its folder and adding it to
//! `KINDS`. Every module gets two parameters for free: whether it is enabled, and
//! when it runs.

pub mod chests;
pub mod events;
pub mod members;
pub mod shield;

use std::sync::Arc;

use serde_json::{json, Map, Value as Json};

use crate::config::{module_section_key, ConfigManager, Field, FieldType, ProfileConfig, Section};
use crate::context::Window;
use crate::error::Res;
use crate::schedule::{DEFAULT_DAY_START, SCHEDULES, SCHEDULE_DAILY, SCHEDULE_EVERY_RUN};

/// How one module ended on one profile.
#[derive(Debug, Clone, PartialEq)]
pub struct ModuleResult {
    pub success: bool,
    /// A failure is tried again while the profile has attempts left, unless this is false.
    pub retry: bool,
    pub reason: String,
    pub collected: i64,
    pub incomplete: i64,
    pub account: String,
    pub profile: String,
    pub module: String,
    pub extra: Map<String, Json>,
}

impl Default for ModuleResult {
    fn default() -> Self {
        ModuleResult { success: false, retry: true, reason: String::new(), collected: 0, incomplete: 0,
                       account: String::new(), profile: String::new(), module: String::new(), extra: Map::new() }
    }
}

impl ModuleResult {
    pub fn for_profile(profile: &ProfileConfig) -> Self {
        ModuleResult { account: profile.account_name.clone(), profile: profile.name.clone(), ..Default::default() }
    }

    pub fn ok(profile: &ProfileConfig) -> Self {
        ModuleResult { success: true, ..Self::for_profile(profile) }
    }

    pub fn failed(profile: &ProfileConfig, reason: &str, retry: bool) -> Self {
        ModuleResult { reason: reason.to_string(), retry, ..Self::for_profile(profile) }
    }

    pub fn with(mut self, key: &str, value: Json) -> Self {
        self.extra.insert(key.to_string(), value);
        self
    }
}

/// A module at work: one per run, shared by the accounts working at once.
pub trait Module: Send + Sync {
    fn key(&self) -> &str;
    fn title(&self) -> &str;
    /// Its block of config.json: its switch, its schedule, its parameters.
    fn options(&self) -> &Map<String, Json>;
    fn default_day_start(&self) -> &str {
        DEFAULT_DAY_START
    }
    /// Whether the profile has what this module needs to run on it.
    fn configured_for(&self, profile: &ProfileConfig) -> bool;
    /// What would make running on these profiles pointless, said before the run starts.
    fn preflight(&self, _profiles: &[ProfileConfig]) -> Vec<String> {
        vec![]
    }
    /// Called once, before the browser opens, with the profiles this run will do.
    fn begin_run(&self, _profiles: &[ProfileConfig]) {}
    /// Called once when the run ends, however it ends, with each profile's last result.
    fn end_run(&self, _results: &[ModuleResult], _duration: &str, _error: Option<&str>, _cancelled: bool) {}
    /// Does the module's job on the profile that is active in the game.
    fn run_for_profile(&self, window: &dyn Window, profile: &ProfileConfig) -> Res<ModuleResult>;

    fn schedule(&self) -> String {
        self.options().get("schedule").and_then(|v| v.as_str()).unwrap_or(SCHEDULE_EVERY_RUN).to_string()
    }
    fn daily_attempts(&self) -> i64 {
        self.options().get("daily_attempts").and_then(|v| v.as_f64()).map(|f| f as i64).unwrap_or(3)
    }
    fn day_start(&self) -> String {
        match self.options().get("day_start").and_then(|v| v.as_str()) {
            Some(s) if !s.is_empty() => s.to_string(),
            _ => self.default_day_start().to_string(),
        }
    }
}

/// The modules there are, in the order a run does them on each profile.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Kind {
    Shield,
    Chests,
    Members,
    Events,
}

pub const KINDS: [Kind; 4] = [Kind::Shield, Kind::Chests, Kind::Members, Kind::Events];

impl Kind {
    pub fn by_key(key: &str) -> Option<Kind> {
        KINDS.iter().copied().find(|k| k.key() == key)
    }

    pub fn key(self) -> &'static str {
        match self {
            Kind::Shield => shield::KEY,
            Kind::Chests => chests::KEY,
            Kind::Members => members::KEY,
            Kind::Events => events::KEY,
        }
    }

    pub fn title(self) -> &'static str {
        match self {
            Kind::Shield => shield::TITLE,
            Kind::Chests => chests::TITLE,
            Kind::Members => members::TITLE,
            Kind::Events => events::TITLE,
        }
    }

    pub fn description(self) -> &'static str {
        match self {
            Kind::Shield => shield::DESCRIPTION,
            Kind::Chests => chests::DESCRIPTION,
            Kind::Members => members::DESCRIPTION,
            Kind::Events => events::DESCRIPTION,
        }
    }

    pub fn default_enabled(self) -> bool {
        true
    }

    pub fn default_schedule(self) -> &'static str {
        match self {
            Kind::Members | Kind::Events => SCHEDULE_DAILY,
            _ => SCHEDULE_EVERY_RUN,
        }
    }

    pub fn default_day_start(self) -> &'static str {
        match self {
            Kind::Events => events::DAY_START,
            _ => DEFAULT_DAY_START,
        }
    }

    pub fn settings(self) -> Vec<Field> {
        match self {
            Kind::Shield => shield::settings(),
            Kind::Chests => chests::settings(),
            Kind::Members => members::settings(),
            Kind::Events => events::settings(),
        }
    }

    pub fn profile_fields(self) -> Vec<Field> {
        match self {
            Kind::Shield => shield::profile_fields(),
            Kind::Chests => vec![],
            Kind::Members => members::profile_fields(),
            Kind::Events => events::profile_fields(),
        }
    }

    pub fn section(self) -> Section {
        let mut fields = vec![
            Field::new("enabled", FieldType::Bool, "Enabled", json!(self.default_enabled()),
                       "Turns the whole module on or off. A disabled module is not run."),
            Field::new("schedule", FieldType::Choice, "Runs", json!(self.default_schedule()),
                       "'every run': each time the application runs. 'once a day': on the first run of the day for \
                        each profile, and not again until tomorrow.")
                .choices(&SCHEDULES),
            Field::new("daily_attempts", FieldType::Int, "Attempts per day", json!(3),
                       "Only for 'once a day': a run that fails is tried again on the next runs of the same day, up \
                        to this many times.")
                .range(1.0, 24.0),
            Field::new("day_start", FieldType::Str, "Game day starts at (HH:MM)", json!(self.default_day_start()),
                       "Only for 'once a day': the time, on this computer's clock, the game's day turns. A run before \
                        it still counts as the previous day."),
        ];
        fields.extend(self.settings());
        Section { key: module_section_key(self.key()), label: self.title().into(), description: self.description().into(),
                  fields }
    }

    pub fn configured_for(self, profile: &ProfileConfig) -> bool {
        match self {
            Kind::Shield => shield::configured_for(profile),
            Kind::Chests => profile.has_database(),
            Kind::Members => members::configured_for(profile),
            Kind::Events => events::configured_for(profile),
        }
    }

    /// One short line about the profile, for its row in the accounts tab.
    pub fn profile_summary(self, profile: &ProfileConfig) -> String {
        match self {
            Kind::Shield => if shield::configured_for(profile) { "shield".into() } else { String::new() },
            Kind::Chests => String::new(),
            Kind::Members => if members::configured_for(profile) { "members".into() } else { String::new() },
            Kind::Events => events::profile_summary(profile),
        }
    }

    /// Whether the module can try the values typed for a profile.
    pub fn can_test_profile(self) -> bool {
        self == Kind::Events
    }

    pub fn test_profile(self, values: &Map<String, Json>) -> Option<(bool, String)> {
        match self {
            Kind::Events => Some(events::test_profile(values)),
            _ => None,
        }
    }

    /// The module at work, with the options the configuration has for it.
    pub fn instantiate(self, config: &ConfigManager) -> Arc<dyn Module> {
        let options = config.module(self.key());
        match self {
            Kind::Shield => Arc::new(shield::ShieldModule { options }),
            Kind::Chests => Arc::new(chests::ChestCollector::new(options)),
            Kind::Members => Arc::new(members::MembersModule { options }),
            Kind::Events => Arc::new(events::EventsModule { options, data_dir: crate::paths::module_data_dir() }),
        }
    }

    pub fn enabled_in(self, config: &ConfigManager) -> bool {
        config.get_bool(&module_section_key(self.key()), "enabled", self.default_enabled())
    }
}

/// The enabled modules, at work.
pub fn enabled_modules(config: &ConfigManager) -> Vec<Arc<dyn Module>> {
    KINDS.iter().filter(|k| k.enabled_in(config)).map(|k| k.instantiate(config)).collect()
}

/// The title of a module key, or the key when it is not one.
pub fn title_of(key: &str) -> String {
    Kind::by_key(key).map(|k| k.title().to_string()).unwrap_or_default()
}

/// `options[key]` as a number.
pub fn opt_f64(options: &Map<String, Json>, key: &str, default: f64) -> f64 {
    match options.get(key) {
        Some(Json::Number(n)) => n.as_f64().unwrap_or(default),
        Some(Json::String(s)) => s.trim().parse().unwrap_or(default),
        Some(Json::Bool(b)) => *b as i64 as f64,
        _ => default,
    }
}

pub fn opt_bool(options: &Map<String, Json>, key: &str, default: bool) -> bool {
    match options.get(key) {
        Some(Json::Bool(b)) => *b,
        Some(Json::Number(n)) => n.as_f64().map(|f| f != 0.0).unwrap_or(default),
        Some(Json::String(s)) => !s.is_empty(),
        Some(Json::Null) => false,
        _ => default,
    }
}

/// A profile setting as text.
pub fn setting_text(values: &Map<String, Json>, key: &str) -> String {
    match values.get(key) {
        Some(Json::String(s)) => s.clone(),
        Some(Json::Null) | None => String::new(),
        Some(other) => other.to_string(),
    }
}
