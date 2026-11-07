//! Every parameter, the account/profile model, and config/config.json.
//!
//! The interface, the default configuration file and the validation of
//! config.json are all generated from the declarations below, so a new parameter
//! only has to be declared once to become visible, editable and persisted. Each
//! feature module declares its own parameters next to its code; they are stored
//! under "modules" in config.json.
//!
//! Account model: an account is a login (e-mail + password) on totalbattle.com; a
//! profile is one of the cities that account owns. Each account lives in a Chrome
//! window of its own, with its own debugging port and folder, written down the
//! first time (`pin_windows`). Each profile has a database of its own.

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value as Json};

use crate::paths;
use crate::text::folder_name;

// ------------------------------------------------------------------ schema

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FieldType {
    Str,
    Password,
    Path,
    Int,
    Float,
    Bool,
    Choice,
}

#[derive(Debug, Clone)]
pub struct Field {
    pub key: &'static str,
    pub kind: FieldType,
    pub label: &'static str,
    pub default: Json,
    pub help: &'static str,
    pub choices: Vec<&'static str>,
    pub minimum: Option<f64>,
    pub maximum: Option<f64>,
    /// False keeps the parameter in config.json but out of the Parameters tab.
    pub in_parameters_tab: bool,
}

impl Field {
    pub fn new(key: &'static str, kind: FieldType, label: &'static str, default: Json, help: &'static str) -> Field {
        Field { key, kind, label, default, help, choices: vec![], minimum: None, maximum: None, in_parameters_tab: true }
    }
    pub fn range(mut self, minimum: f64, maximum: f64) -> Field {
        self.minimum = Some(minimum);
        self.maximum = Some(maximum);
        self
    }
    pub fn choices(mut self, choices: &[&'static str]) -> Field {
        self.choices = choices.to_vec();
        self
    }
    pub fn hidden(mut self) -> Field {
        self.in_parameters_tab = false;
        self
    }
    pub fn is_secret(&self) -> bool {
        self.kind == FieldType::Password
    }
}

#[derive(Debug, Clone)]
pub struct Section {
    pub key: String,
    pub label: String,
    pub description: String,
    pub fields: Vec<Field>,
}

use FieldType::*;

/// The sections every module shares.
pub fn shared_sections() -> Vec<Section> {
    vec![
        Section {
            key: "execution".into(),
            label: "Execution".into(),
            description: "How a run behaves when Windows Task Scheduler triggers tbcollector.exe. What a run does is \
                          decided in the Modules tab."
                .into(),
            fields: vec![
                Field::new("close_browser_on_finish", Bool, "Close browser on finish", json!(true),
                           "Disable to inspect the game screen after collection finishes."),
                Field::new("stop_on_error", Bool, "Stop on first error", json!(false),
                           "When enabled, a failing profile stops the entire run. When disabled, the collector logs the \
                            error and continues to the next profile."),
                Field::new("parallel_accounts", Int, "Accounts at the same time", json!(4),
                           "How many accounts work at once, each in its own Chrome window. The profiles of an account \
                            are always one after the other. 1 = one account at a time.").range(1.0, 4.0),
                Field::new("retries_per_profile", Int, "Retries per profile", json!(2),
                           "How many times a profile is retried before being considered failed.").range(1.0, 5.0),
                Field::new("restart_browser_on_failure", Bool, "Restart browser when an account fails", json!(true),
                           "When no profile of an account can be collected (usually the game froze in Chrome and needs \
                            F5), closes the browser, opens a fresh one and tries the account once more."),
                Field::new("log_level", Choice, "Log level", json!("INFO"), "DEBUG logs every step.")
                    .choices(&["DEBUG", "INFO", "WARNING", "ERROR"]),
                Field::new("log_dir", Path, "Log folder", json!("execution_logs"),
                           "Relative to the program folder, or an absolute path."),
                Field::new("log_retention_days", Int, "Log retention (days)", json!(7),
                           "Logs older than this are deleted at the start of each run. 0 = never delete.")
                    .range(0.0, 3650.0),
                Field::new("screenshot_on_error", Bool, "Save screenshot on error", json!(true),
                           "Saves a screenshot of the game to execution_logs/screenshots for diagnostics."),
            ],
        },
        Section {
            key: "browser".into(),
            label: "Browser".into(),
            description: "Chrome controlled via CDP (Chrome DevTools Protocol).".into(),
            fields: vec![
                Field::new("cdp_host", Str, "CDP Host", json!("127.0.0.1"), "Almost always 127.0.0.1."),
                Field::new("first_cdp_port", Int, "First debugging port", json!(9322),
                           "Accounts without a port get the next free one from here. Each account's port is shown, \
                            and can be changed, in Accounts and profiles.").range(1024.0, 65535.0).hidden(),
                Field::new("game_url", Str, "Game URL", json!("https://totalbattle.com/en/"),
                           "URL opened at the beginning of each account session."),
                Field::new("game_url_filter", Str, "Game tab URL filter", json!("totalbattle.com"),
                           "URL substring used to find the game tab among open tabs."),
                Field::new("executable_path", Path, "Chrome executable path", json!(""),
                           "Empty = search for Chrome, Edge, and Brave in standard Windows paths."),
                Field::new("profiles_dir", Path, "Folder of Chrome profiles", json!(""),
                           "Where new accounts get their Chrome folder. Empty = \
                            %USERPROFILE%/.total_battle_counter_profiles. An account that already has a folder keeps it.")
                    .hidden(),
                Field::new("reuse_existing", Bool, "Reuse open browser", json!(true),
                           "If the account's window is already open, connects to it instead of restarting it. A window \
                            of another folder on that port is never used."),
                Field::new("start_maximized", Bool, "Start maximized", json!(true), ""),
                Field::new("window_width", Int, "Window width", json!(1920),
                           "Used only when 'Start maximized' is disabled.").range(800.0, 7680.0),
                Field::new("window_height", Int, "Window height", json!(1080),
                           "Used only when 'Start maximized' is disabled.").range(600.0, 4320.0),
                Field::new("page_load_timeout", Float, "Page load timeout (s)", json!(90.0),
                           "Maximum wait time for the game to load after opening the URL.").range(10.0, 600.0),
            ],
        },
        Section {
            key: "login".into(),
            label: "Login".into(),
            description: "Authentication on the website form. Empty selectors are detected automatically.".into(),
            fields: vec![
                Field::new("enabled", Bool, "Automatic login", json!(true),
                           "Disable if you prefer leaving the session already logged into the browser profile."),
                Field::new("open_login_selector", Str, "CSS selector for login opener button", json!(""),
                           "The totalbattle.com login form is in the HTML on page load but hidden: you must click \
                            'Log in' before typing. Empty = automatically find a visible button with login text \
                            (social login buttons like Google/Facebook are discarded)."),
                Field::new("email_selector", Str, "CSS selector for email field", json!(""),
                           "Empty = detect automatically (input[type=email], name/id containing email or login)."),
                Field::new("password_selector", Str, "CSS selector for password field", json!(""),
                           "Empty = detect automatically (input[type=password])."),
                Field::new("submit_selector", Str, "CSS selector for submit button", json!(""),
                           "Empty = detect automatically (button[type=submit] or button with login text)."),
                Field::new("logged_in_selector", Str, "Selector confirming logged-in state", json!(""),
                           "Element that only exists after login, e.g. 'canvas'. Empty = consider logged in when the \
                            password field disappears."),
                Field::new("wait_after_submit", Float, "Wait after submit (s)", json!(30.0),
                           "Maximum wait time for the game to load after submitting login.").range(5.0, 300.0),
                Field::new("max_attempts", Int, "Login retry attempts", json!(2),
                           "Attempts before giving up on the account.").range(1.0, 5.0),
            ],
        },
        Section {
            key: "timing".into(),
            label: "Timing".into(),
            description: "Delays between actions. Increase if the game is sluggish; decrease to collect faster.".into(),
            fields: vec![
                Field::new("action_delay", Float, "Delay between actions (s)", json!(0.35), "").range(0.0, 10.0),
                Field::new("between_profiles_wait", Float, "Delay between profiles (s)", json!(3.0), "")
                    .range(0.0, 60.0),
            ],
        },
    ]
}

pub const MODULES_PREFIX: &str = "modules";

pub fn module_section_key(module_key: &str) -> String {
    format!("{}.{}", MODULES_PREFIX, module_key)
}

/// One section per registered module: its switch, its schedule, its parameters.
pub fn module_sections() -> Vec<Section> {
    crate::modules::KINDS.iter().map(|k| k.section()).collect()
}

pub fn all_sections() -> Vec<Section> {
    let mut s = shared_sections();
    s.extend(module_sections());
    s
}

/// The object a section is stored in, following the dots of its key.
pub fn bucket<'a>(data: &'a Json, section_key: &str) -> Option<&'a Map<String, Json>> {
    let mut node = data;
    for part in section_key.split('.') {
        node = node.get(part)?;
        if !node.is_object() {
            return None;
        }
    }
    node.as_object()
}

pub fn bucket_mut<'a>(data: &'a mut Json, section_key: &str) -> &'a mut Map<String, Json> {
    let mut node = data;
    for part in section_key.split('.') {
        if !node.is_object() {
            *node = json!({});
        }
        let obj = node.as_object_mut().unwrap();
        if !obj.get(part).map(|v| v.is_object()).unwrap_or(false) {
            obj.insert(part.to_string(), json!({}));
        }
        node = obj.get_mut(part).unwrap();
    }
    node.as_object_mut().unwrap()
}

/// The full configuration with every parameter at its declared default.
pub fn default_config() -> Json {
    let mut data = json!({});
    for section in all_sections() {
        let b = bucket_mut(&mut data, &section.key);
        for f in &section.fields {
            b.insert(f.key.to_string(), f.default.clone());
        }
    }
    data.as_object_mut().unwrap().insert("accounts".into(), json!([]));
    data
}

pub fn field_for(section_key: &str, field_key: &str) -> Option<Field> {
    all_sections().into_iter().find(|s| s.key == section_key)?.fields.into_iter().find(|f| f.key == field_key)
}

// ------------------------------------------------------------- coercion

fn py_str(value: &Json) -> String {
    match value {
        Json::Null => String::new(),
        Json::String(s) => s.clone(),
        Json::Bool(true) => "True".into(),
        Json::Bool(false) => "False".into(),
        Json::Number(n) => {
            if let Some(i) = n.as_i64() {
                i.to_string()
            } else {
                crate::tbcodec::py_float(n.as_f64().unwrap_or(0.0))
            }
        }
        other => other.to_string(),
    }
}

fn py_truthy(value: &Json) -> bool {
    match value {
        Json::Null => false,
        Json::Bool(b) => *b,
        Json::Number(n) => n.as_f64().map(|f| f != 0.0).unwrap_or(false),
        Json::String(s) => !s.is_empty(),
        Json::Array(a) => !a.is_empty(),
        Json::Object(o) => !o.is_empty(),
    }
}

fn py_float(value: &Json) -> Option<f64> {
    match value {
        Json::Bool(b) => Some(*b as i64 as f64),
        Json::Number(n) => n.as_f64(),
        Json::String(s) => {
            let t = s.trim().to_lowercase();
            match t.as_str() {
                "inf" | "+inf" | "infinity" => Some(f64::INFINITY),
                "-inf" | "-infinity" => Some(f64::NEG_INFINITY),
                _ => t.parse::<f64>().ok().filter(|f| !f.is_nan()),
            }
        }
        _ => None,
    }
}

/// Brings a value read from disk (or typed in the interface) to its declared type.
pub fn coerce(value: &Json, spec: Option<&Field>) -> Json {
    let Some(spec) = spec else { return value.clone() };
    match spec.kind {
        Bool => {
            if let Json::String(s) = value {
                return json!(["1", "true", "yes", "on", "sim"].contains(&s.trim().to_lowercase().as_str()));
            }
            json!(py_truthy(value))
        }
        Int | Float => {
            let Some(mut f) = py_float(value) else { return spec.default.clone() };
            if spec.kind == Int {
                if !f.is_finite() {
                    return spec.default.clone();
                }
                f = f.trunc();
            }
            if let Some(min) = spec.minimum {
                f = f.max(min);
            }
            if let Some(max) = spec.maximum {
                f = f.min(max);
            }
            if spec.kind == Int {
                json!(f as i64)
            } else {
                json!(f)
            }
        }
        Choice => {
            let s = py_str(value);
            if !spec.choices.is_empty() && !spec.choices.contains(&s.as_str()) {
                return spec.default.clone();
            }
            json!(s)
        }
        Str | Password | Path => json!(py_str(value)),
    }
}

// ----------------------------------------------------------------- model

#[derive(Debug, Clone, PartialEq, Default)]
pub struct DatabaseConfig {
    pub host: String,
    pub port: u16,
    pub user: String,
    pub password: String,
    pub database: String,
}

const LOCAL_HOSTS: [&str; 3] = ["localhost", "127.0.0.1", "::1"];

impl DatabaseConfig {
    pub fn new() -> Self {
        DatabaseConfig { host: "127.0.0.1".into(), port: 3306, ..Default::default() }
    }

    pub fn is_usable(&self) -> bool {
        !self.host.is_empty() && !self.user.is_empty() && !self.database.is_empty()
    }

    /// What says two configurations are the same database, however they were typed.
    pub fn identity(&self) -> (String, u16, String) {
        let host = self.host.trim().to_lowercase();
        let host = if LOCAL_HOSTS.contains(&host.as_str()) { "localhost".to_string() } else { host };
        (host, self.port, self.database.trim().to_lowercase())
    }

    pub fn to_json(&self) -> Json {
        json!({"host": self.host, "port": self.port, "user": self.user, "password": self.password,
               "database": self.database})
    }

    pub fn from_json(data: &Json) -> Option<DatabaseConfig> {
        let obj = data.as_object()?;
        if obj.is_empty() {
            return None;
        }
        let text = |k: &str| obj.get(k).map(py_str).unwrap_or_default();
        let port = obj
            .get("port")
            .and_then(|p| if py_truthy(p) { py_float(p) } else { Some(3306.0) })
            .filter(|p| p.is_finite() && *p >= 0.0 && *p <= 65535.0)
            .map(|p| p as u16)
            .unwrap_or(3306);
        Some(DatabaseConfig { host: text("host"), port, user: text("user"), password: text("password"),
                              database: text("database") })
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct ProfileConfig {
    pub name: String,
    pub enabled: bool,
    pub database: Option<DatabaseConfig>,
    pub account_name: String,
    /// What each module was told about this profile: {"events": {"site_url": ...}}.
    pub modules: Map<String, Json>,
}

impl Default for ProfileConfig {
    fn default() -> Self {
        ProfileConfig { name: String::new(), enabled: true, database: None, account_name: String::new(),
                        modules: Map::new() }
    }
}

impl ProfileConfig {
    pub fn named(name: &str, account: &str) -> Self {
        ProfileConfig { name: name.into(), account_name: account.into(), ..Default::default() }
    }

    pub fn has_database(&self) -> bool {
        self.database.as_ref().map(|d| d.is_usable()).unwrap_or(false)
    }

    pub fn module_settings(&self, module_key: &str) -> Map<String, Json> {
        self.modules.get(module_key).and_then(|v| v.as_object()).cloned().unwrap_or_default()
    }

    pub fn label(&self) -> String {
        if self.account_name.is_empty() {
            self.name.clone()
        } else {
            format!("{} / {}", self.account_name, self.name)
        }
    }

    pub fn to_json(&self) -> Json {
        let modules: Map<String, Json> = self
            .modules
            .iter()
            .filter(|(_, v)| v.as_object().map(|o| !o.is_empty()).unwrap_or(false))
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        json!({"name": self.name, "enabled": self.enabled,
               "database": self.database.as_ref().map(|d| d.to_json()).unwrap_or(Json::Null),
               "modules": modules})
    }

    pub fn from_json(data: &Json, account_name: &str) -> ProfileConfig {
        let modules = data
            .get("modules")
            .and_then(|m| m.as_object())
            .map(|m| m.iter().filter(|(_, v)| v.is_object()).map(|(k, v)| (k.clone(), v.clone())).collect())
            .unwrap_or_default();
        ProfileConfig {
            name: data.get("name").map(py_str).unwrap_or_default(),
            enabled: data.get("enabled").map(py_truthy).unwrap_or(true),
            database: data.get("database").and_then(DatabaseConfig::from_json),
            account_name: account_name.to_string(),
            modules,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct AccountConfig {
    pub name: String,
    pub login: String,
    pub password: String,
    pub enabled: bool,
    /// Its Chrome folder (a path, or a name inside the folder of profiles).
    pub browser_profile: String,
    pub cdp_port: u16,
    pub profiles: Vec<ProfileConfig>,
}

impl Default for AccountConfig {
    fn default() -> Self {
        AccountConfig { name: String::new(), login: String::new(), password: String::new(), enabled: true,
                        browser_profile: String::new(), cdp_port: 0, profiles: vec![] }
    }
}

impl AccountConfig {
    pub fn active_profiles(&self) -> Vec<ProfileConfig> {
        self.profiles.iter().filter(|p| p.enabled && !p.name.is_empty()).cloned().collect()
    }

    pub fn to_json(&self) -> Json {
        json!({"name": self.name, "login": self.login, "password": self.password, "enabled": self.enabled,
               "browser_profile": self.browser_profile, "cdp_port": self.cdp_port,
               "profiles": self.profiles.iter().map(|p| p.to_json()).collect::<Vec<_>>()})
    }

    pub fn from_json(data: &Json) -> AccountConfig {
        let name = data.get("name").map(py_str).unwrap_or_default();
        let port = data
            .get("cdp_port")
            .and_then(py_float)
            .filter(|p| p.is_finite() && *p >= 0.0 && *p <= 65535.0)
            .map(|p| p as u16)
            .unwrap_or(0);
        AccountConfig {
            login: data.get("login").map(py_str).unwrap_or_default(),
            password: data.get("password").map(py_str).unwrap_or_default(),
            enabled: data.get("enabled").map(py_truthy).unwrap_or(true),
            browser_profile: data.get("browser_profile").map(py_str).unwrap_or_default(),
            cdp_port: port,
            profiles: data
                .get("profiles")
                .and_then(|p| p.as_array())
                .map(|a| a.iter().map(|p| ProfileConfig::from_json(p, &name)).collect())
                .unwrap_or_default(),
            name,
        }
    }
}

// --------------------------------------------------------------- manager

/// Reads, validates and writes config/config.json.
#[derive(Debug, Clone)]
pub struct ConfigManager {
    pub path: PathBuf,
    pub data: Json,
}

impl ConfigManager {
    /// The configuration at `path`; defaults when the file does not exist.
    pub fn open(path: &Path) -> Result<ConfigManager, String> {
        let mut cm = ConfigManager { path: path.to_path_buf(), data: default_config() };
        cm.load()?;
        Ok(cm)
    }

    pub fn open_default() -> Result<ConfigManager, String> {
        Self::open(&paths::config_file())
    }

    pub fn load(&mut self) -> Result<(), String> {
        let mut raw = json!({});
        if self.path.exists() {
            let text = fs::read_to_string(&self.path)
                .map_err(|e| format!("Invalid config.json ({}). Fix the file or delete it to start over.", e))?;
            let text = text.trim_start_matches('\u{feff}');
            raw = serde_json::from_str(text)
                .map_err(|e| format!("Invalid config.json ({}). Fix the file or delete it to start over.", e))?;
            if raw.is_null() {
                raw = json!({});
            }
        }
        let mut merged = default_config();
        for section in all_sections() {
            let stored = bucket(&raw, &section.key).cloned().unwrap_or_default();
            let values = bucket_mut(&mut merged, &section.key);
            for spec in &section.fields {
                if let Some(v) = stored.get(spec.key) {
                    values.insert(spec.key.to_string(), coerce(v, Some(spec)));
                }
            }
        }
        let accounts = raw.get("accounts").filter(|a| py_truthy(a)).cloned().unwrap_or(json!([]));
        merged.as_object_mut().unwrap().insert("accounts".into(), accounts);
        self.data = merged;
        Ok(())
    }

    pub fn save(&self) -> std::io::Result<()> {
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent)?;
        }
        write_json_atomic(&self.path, &self.data, 2)
    }

    pub fn exists(&self) -> bool {
        self.path.exists()
    }

    pub fn get(&self, section: &str, key: &str) -> Option<Json> {
        bucket(&self.data, section).and_then(|b| b.get(key)).cloned()
    }

    pub fn get_bool(&self, section: &str, key: &str, default: bool) -> bool {
        self.get(section, key).map(|v| py_truthy(&v)).unwrap_or(default)
    }

    pub fn get_f64(&self, section: &str, key: &str, default: f64) -> f64 {
        self.get(section, key).and_then(|v| py_float(&v)).unwrap_or(default)
    }

    pub fn get_i64(&self, section: &str, key: &str, default: i64) -> i64 {
        self.get(section, key).and_then(|v| py_float(&v)).map(|f| f as i64).unwrap_or(default)
    }

    pub fn get_str(&self, section: &str, key: &str, default: &str) -> String {
        self.get(section, key).map(|v| py_str(&v)).unwrap_or_else(|| default.to_string())
    }

    pub fn set(&mut self, section: &str, key: &str, value: Json) {
        let spec = field_for(section, key);
        let v = coerce(&value, spec.as_ref());
        bucket_mut(&mut self.data, section).insert(key.to_string(), v);
    }

    pub fn section(&self, key: &str) -> Map<String, Json> {
        bucket(&self.data, key).cloned().unwrap_or_default()
    }

    /// A module's own parameters, with its switch and its schedule.
    pub fn module(&self, module_key: &str) -> Map<String, Json> {
        self.section(&module_section_key(module_key))
    }

    pub fn accounts(&self) -> Vec<AccountConfig> {
        self.data
            .get("accounts")
            .and_then(|a| a.as_array())
            .map(|a| a.iter().map(AccountConfig::from_json).collect())
            .unwrap_or_default()
    }

    pub fn set_accounts(&mut self, accounts: &[AccountConfig]) {
        self.data
            .as_object_mut()
            .unwrap()
            .insert("accounts".into(), Json::Array(accounts.iter().map(|a| a.to_json()).collect()));
    }

    pub fn enabled_accounts(&self) -> Vec<AccountConfig> {
        self.accounts().into_iter().filter(|a| a.enabled && !a.active_profiles().is_empty()).collect()
    }

    pub fn resolved_log_dir(&self) -> PathBuf {
        let raw = self.get_str("execution", "log_dir", "execution_logs");
        let raw = if raw.is_empty() { "execution_logs".to_string() } else { raw };
        if paths::is_absolute(&raw) {
            PathBuf::from(raw)
        } else {
            self.base_dir().join(raw)
        }
    }

    /// The folder this configuration belongs to: the one above config/.
    pub fn base_dir(&self) -> PathBuf {
        self.path.parent().and_then(|p| p.parent()).map(Path::to_path_buf).unwrap_or_else(paths::base_dir)
    }

    // -- windows
    pub fn profiles_dir(&self) -> PathBuf {
        let raw = self.get_str("browser", "profiles_dir", "");
        if raw.is_empty() {
            paths::default_profiles_dir()
        } else {
            PathBuf::from(paths::expand_vars(&paths::expand_user(&raw)))
        }
    }

    /// The Chrome folder of an account: its own path, or a name inside the folder of profiles.
    pub fn chrome_folder_for(&self, account: &AccountConfig) -> PathBuf {
        let custom = paths::expand_vars(&paths::expand_user(account.browser_profile.trim()));
        if !custom.is_empty() && paths::is_absolute(&custom) {
            return PathBuf::from(custom);
        }
        let name = if !custom.is_empty() {
            custom
        } else if !account.name.is_empty() {
            account.name.clone()
        } else {
            account.login.clone()
        };
        self.profiles_dir().join(folder_name(&name))
    }

    /// Writes down the port and the folder of every account that has none yet.
    ///
    /// Written, not worked out on each run: a folder derived from the name would
    /// change with the name, and a new folder means another code by e-mail.
    pub fn pin_windows(&mut self) -> bool {
        let mut accounts = self.accounts();
        let mut ports: Vec<u16> = accounts.iter().filter(|a| a.cdp_port != 0).map(|a| a.cdp_port).collect();
        let mut folders: Vec<PathBuf> = accounts
            .iter()
            .filter(|a| !a.browser_profile.trim().is_empty())
            .map(|a| self.chrome_folder_for(a))
            .collect();
        let mut port = self.get_i64("browser", "first_cdp_port", 9322).clamp(1, 65535) as u16;
        let mut changed = false;
        for account in accounts.iter_mut() {
            if account.cdp_port == 0 {
                while ports.contains(&port) {
                    port += 1;
                }
                account.cdp_port = port;
                ports.push(port);
                changed = true;
            }
            if account.browser_profile.trim().is_empty() {
                let base = self.chrome_folder_for(account);
                let mut folder = base.clone();
                let mut turn = 2;
                while folders.iter().any(|other| paths::same_folder(&folder, other)) {
                    folder = PathBuf::from(format!("{}_{}", base.display(), turn));
                    turn += 1;
                }
                account.browser_profile = folder.to_string_lossy().into_owned();
                folders.push(folder);
                changed = true;
            }
        }
        if changed {
            self.set_accounts(&accounts);
            let _ = self.save();
        }
        changed
    }

    /// Active accounts that would share a Chrome window.
    pub fn window_problems(&self, accounts: Option<&[AccountConfig]>) -> Vec<String> {
        let all = accounts.map(|a| a.to_vec()).unwrap_or_else(|| self.accounts());
        let active: Vec<&AccountConfig> = all.iter().filter(|a| a.enabled).collect();
        let mut problems = Vec::new();
        for (i, account) in active.iter().enumerate() {
            for other in &active[i + 1..] {
                if account.cdp_port != 0 && account.cdp_port == other.cdp_port {
                    problems.push(format!(
                        "Accounts '{}' and '{}' use the same port ({}). Each window needs its own.",
                        account.name, other.name, account.cdp_port
                    ));
                }
                if paths::same_folder(&self.chrome_folder_for(account), &self.chrome_folder_for(other)) {
                    problems.push(format!(
                        "Accounts '{}' and '{}' use the same Chrome folder ({}). Each keeps its session in its own.",
                        account.name,
                        other.name,
                        self.chrome_folder_for(account).display()
                    ));
                }
            }
        }
        problems
    }

    /// Active profiles that would write to the same database.
    pub fn database_problems(&self, accounts: Option<&[AccountConfig]>) -> Vec<String> {
        let all = accounts.map(|a| a.to_vec()).unwrap_or_else(|| self.accounts());
        let mut order: Vec<(String, u16, String)> = Vec::new();
        let mut users: HashMap<(String, u16, String), Vec<String>> = HashMap::new();
        for account in all.iter().filter(|a| a.enabled) {
            for profile in account.active_profiles() {
                if profile.has_database() {
                    let id = profile.database.as_ref().unwrap().identity();
                    if !users.contains_key(&id) {
                        order.push(id.clone());
                    }
                    users.entry(id).or_default().push(format!("{} / {}", account.name, profile.name));
                }
            }
        }
        order
            .into_iter()
            .filter_map(|id| {
                let names = &users[&id];
                (names.len() > 1).then(|| {
                    format!(
                        "Profiles {} use the same database ('{}' on {}). Each profile needs its own.",
                        names.join(", "),
                        id.2,
                        id.0
                    )
                })
            })
            .collect()
    }
}

/// Writes JSON through a temporary file, so a crash never leaves half a file.
/// `indent` 0 writes it compact.
pub fn write_json_atomic(path: &Path, value: &Json, indent: usize) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent)?;
        }
    }
    let text = if indent == 0 {
        serde_json::to_string(value)?
    } else {
        let pad = vec![b' '; indent];
        let mut buf = Vec::new();
        let fmt = serde_json::ser::PrettyFormatter::with_indent(&pad);
        let mut ser = serde_json::Serializer::with_formatter(&mut buf, fmt);
        serde::Serialize::serialize(value, &mut ser)?;
        String::from_utf8(buf).unwrap_or_default()
    };
    let mut temporary = path.as_os_str().to_owned();
    temporary.push(".tmp");
    let temporary = PathBuf::from(temporary);
    fs::write(&temporary, text)?;
    fs::rename(&temporary, path)
}

pub fn read_json(path: &Path) -> Option<Json> {
    let text = fs::read_to_string(path).ok()?;
    serde_json::from_str(text.trim_start_matches('\u{feff}')).ok()
}

/// Rebuilds the account list from the old position.cfg: only the database
/// credentials survive, as one account with an empty login holding every profile.
pub fn migrate_legacy_config(legacy_path: &Path) -> Vec<AccountConfig> {
    let Ok(text) = fs::read_to_string(legacy_path) else { return vec![] };
    let mut sections: Vec<(String, HashMap<String, String>)> = Vec::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') || line.starts_with(';') {
            continue;
        }
        if line.starts_with('[') && line.ends_with(']') {
            sections.push((line[1..line.len() - 1].trim().to_string(), HashMap::new()));
            continue;
        }
        let Some(split) = line.find(['=', ':']) else { continue };
        if let Some((_, values)) = sections.last_mut() {
            values.insert(line[..split].trim().to_lowercase(), line[split + 1..].trim().to_string());
        }
    }
    let name = "Conta importada";
    let mut profiles = Vec::new();
    for (section, values) in sections {
        if !section.to_lowercase().starts_with("account") {
            continue;
        }
        let profile_name = values.get("account").map(|s| s.trim().to_string()).unwrap_or_default();
        if profile_name.is_empty() {
            continue;
        }
        let get = |k: &str, d: &str| values.get(k).cloned().unwrap_or_else(|| d.to_string());
        let database = DatabaseConfig {
            host: get("host", "127.0.0.1"),
            port: values.get("port").and_then(|p| p.parse().ok()).unwrap_or(3306),
            user: get("user", ""),
            password: get("password", ""),
            database: get("database", ""),
        };
        profiles.push(ProfileConfig { name: profile_name, enabled: true, database: Some(database),
                                      account_name: name.into(), modules: Map::new() });
    }
    if profiles.is_empty() {
        return vec![];
    }
    vec![AccountConfig { name: name.into(), profiles, ..Default::default() }]
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp() -> tempfile::TempDir {
        tempfile::tempdir().unwrap()
    }

    #[test]
    fn every_module_has_its_switch_and_schedule_with_defaults() {
        let data = default_config();
        for kind in crate::modules::KINDS {
            let b = bucket(&data, &module_section_key(kind.key())).unwrap();
            assert_eq!(&json!(kind.default_enabled()), b.get("enabled").unwrap());
            assert_eq!(&json!(kind.default_schedule()), b.get("schedule").unwrap());
        }
        assert_eq!(&json!("once a day"), bucket(&data, "modules.events").unwrap().get("schedule").unwrap());
        assert_eq!(&json!("14:00"), bucket(&data, "modules.events").unwrap().get("day_start").unwrap());
    }

    #[test]
    fn module_values_are_validated_and_survive_a_save() {
        let dir = temp();
        let path = dir.path().join("config.json");
        let mut cm = ConfigManager::open(&path).unwrap();
        cm.set("modules.events", "days_back", json!("500"));
        cm.set("modules.events", "schedule", json!("whenever"));
        cm.set("modules.chests", "open_interval", json!(0.01));
        cm.set("modules.events", "enabled", json!("no"));
        cm.save().unwrap();
        let again = ConfigManager::open(&path).unwrap();
        assert_eq!(Some(json!(365)), again.get("modules.events", "days_back"));
        assert_eq!(Some(json!("once a day")), again.get("modules.events", "schedule"));
        assert_eq!(Some(json!(0.2)), again.get("modules.chests", "open_interval"));
        assert_eq!(Some(json!(false)), again.get("modules.events", "enabled"));
    }

    #[test]
    fn a_file_from_before_the_modules_loads_and_keeps_its_values() {
        let dir = temp();
        let path = dir.path().join("config.json");
        fs::write(&path, r#"{"execution": {"retries_per_profile": 4, "log_level": "DEBUG"},
            "accounts": [{"name": "Main", "login": "a", "password": "b", "cdp_port": "9400",
                          "profiles": [{"name": "Alpha", "database": {"host": "h", "port": "", "user": "u", "database": "d"}}]}]}"#).unwrap();
        let cm = ConfigManager::open(&path).unwrap();
        assert_eq!(4, cm.get_i64("execution", "retries_per_profile", 0));
        assert_eq!("DEBUG", cm.get_str("execution", "log_level", ""));
        assert!(cm.get_bool("modules.chests", "enabled", false));
        let account = &cm.accounts()[0];
        assert_eq!(9400, account.cdp_port);
        assert_eq!(3306, account.profiles[0].database.as_ref().unwrap().port);
        assert!(account.profiles[0].has_database());
        assert_eq!("Main / Alpha", account.profiles[0].label());
    }

    #[test]
    fn each_profile_keeps_its_own_module_values() {
        let dir = temp();
        let path = dir.path().join("config.json");
        let mut cm = ConfigManager::open(&path).unwrap();
        let mut a = ProfileConfig::named("Alpha", "Main");
        a.modules.insert("events".into(), json!({"site_url": "https://a", "api_token": "x"}));
        let b = ProfileConfig::named("Beta", "Main");
        cm.set_accounts(&[AccountConfig { name: "Main".into(), profiles: vec![a, b], ..Default::default() }]);
        cm.save().unwrap();
        let again = ConfigManager::open(&path).unwrap();
        let profiles = &again.accounts()[0].profiles;
        assert_eq!("https://a", profiles[0].module_settings("events")["site_url"]);
        assert!(profiles[1].module_settings("events").is_empty());
        assert!(!again.data["accounts"][0]["profiles"][1]["modules"].as_object().unwrap().contains_key("events"));
    }

    #[test]
    fn all_sections_have_distinct_keys() {
        let mut keys: Vec<String> = all_sections().iter().map(|s| s.key.clone()).collect();
        let n = keys.len();
        keys.sort();
        keys.dedup();
        assert_eq!(n, keys.len());
        for s in all_sections() {
            let mut f: Vec<&str> = s.fields.iter().map(|f| f.key).collect();
            let n = f.len();
            f.sort();
            f.dedup();
            assert_eq!(n, f.len(), "{}", s.key);
        }
    }

    #[test]
    fn a_broken_file_is_said_and_not_overwritten() {
        let dir = temp();
        let path = dir.path().join("config.json");
        fs::write(&path, "{not json").unwrap();
        assert!(ConfigManager::open(&path).unwrap_err().contains("Invalid config.json"));
    }

    #[test]
    fn the_old_position_cfg_brings_its_databases() {
        let dir = temp();
        let path = dir.path().join("position.cfg");
        fs::write(&path, "[COORDINATES]\nx = 1\n[account_1]\naccount = Crash BR\nhost = db\nport = 3307\nuser = u\n\
                          password = p\ndatabase = d1\n[account_2]\naccount = \n").unwrap();
        let accounts = migrate_legacy_config(&path);
        assert_eq!(1, accounts.len());
        assert_eq!("Crash BR", accounts[0].profiles[0].name);
        assert_eq!(3307, accounts[0].profiles[0].database.as_ref().unwrap().port);
    }
}
