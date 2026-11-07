//! Who is playing: logging in to an account, and moving between its profiles.
//!
//! Logging in happens through the page's own form, driven by CDP: the fields are
//! found by their markup and then clicked and typed into with real input events.
//!
//! The session is kept, not erased: clearing cookies makes the game treat the
//! browser as a new device and mail a code no unattended run can answer. So each run
//! asks the page where it stands, and logging in is the exception.
//!
//! The first login of a folder is a person's: on a folder the game has not seen, the
//! form is not touched; the account stops and says so. Once the game has come up in
//! a folder a small file there says so (`SESSION_MARK`), and from then on a dropped
//! session is logged back into with the password.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value as Json};

use crate::browser::Browser;
use crate::cancel;
use crate::config::{AccountConfig, ProfileConfig};
use crate::error::{Error, Res};
use crate::link::{GameApi, GameLink};
use crate::text::normalize_name;

const JS_HELPERS: &str = r#"
  const visible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  const label = (el) => ((el.textContent || el.value || el.getAttribute('aria-label') || '')
                         .replace(/\s+/g, ' ').trim());
  const SOCIAL = /google|facebook|apple|vkontakte|\bvk\b|twitter|discord|steam|yandex|ok\.ru|huawei/i;
  const LOGIN_WORD = /log ?in|sign ?in|entrar|acessar/i;
  const OTHER_ROUTE = /code|c[oó]digo|forgot|esqueci|recuperar|sign ?up|cadastr|registr/i;
  const TEXT_INPUT = (i) => !['checkbox', 'radio', 'hidden', 'submit', 'button', 'file', 'range',
                              'image', 'reset', 'color'].includes(i.type);
  const CLICKABLE = 'button, a, [role=button], input[type=submit], input[type=button], div, span, li';
  const innermost = (els) => els.filter(el => !els.some(other => other !== el && el.contains(other)));
"#;

const MARK_LOGIN_FIELDS_BODY: &str = r#"
  const attr = (el) => [el.type, el.name, el.id, el.placeholder, el.autocomplete,
                        el.getAttribute('aria-label')].join(' ').toLowerCase();
  document.querySelectorAll('[data-tbc-role]').forEach(el => el.removeAttribute('data-tbc-role'));
  const password = Array.from(document.querySelectorAll('input[type=password]')).filter(visible)[0];
  if (!password) return {password: false, email: false, submit: false, scope: ''};
  password.setAttribute('data-tbc-role', 'password');
  let scope = password.closest('form');
  if (!scope) {
    let node = password.parentElement;
    while (node && node !== document.documentElement) {
      const fields = Array.from(node.querySelectorAll('input'))
                          .filter(i => i !== password && visible(i) && TEXT_INPUT(i));
      const actions = Array.from(node.querySelectorAll(CLICKABLE))
                           .filter(el => visible(el) && LOGIN_WORD.test(label(el))
                                         && !SOCIAL.test(label(el)) && !OTHER_ROUTE.test(label(el)));
      if (fields.length && actions.length) { scope = node; break; }
      node = node.parentElement;
    }
  }
  scope = scope || document;
  const textual = Array.from(scope.querySelectorAll('input'))
                       .filter(i => i !== password && visible(i) && TEXT_INPUT(i));
  const email = textual.find(i => i.type === 'email')
             || textual.find(i => /e-?mail|login|user|usuario|conta/.test(attr(i)))
             || textual.filter(i => password.compareDocumentPosition(i) & Node.DOCUMENT_POSITION_PRECEDING).pop()
             || textual[0];
  if (email) email.setAttribute('data-tbc-role', 'email');
  const actions = innermost(Array.from(scope.querySelectorAll(CLICKABLE))
      .filter(el => visible(el) && !SOCIAL.test(label(el)) && label(el).length <= 40));
  const worded = actions.filter(el => LOGIN_WORD.test(label(el)) && !OTHER_ROUTE.test(label(el)))
                        .sort((a, b) => label(a).length - label(b).length);
  const submit = worded[0] || actions.find(b => b.type === 'submit');
  if (submit) submit.setAttribute('data-tbc-role', 'submit');
  return {
    password: true, email: !!email, submit: !!submit,
    scope: scope === document ? 'whole page' : (scope.tagName || '') + (scope.className ? '.' + String(scope.className).split(' ')[0] : ''),
    submit_label: submit ? label(submit).slice(0, 30) : '',
    email_name: email ? (email.name || email.placeholder || '') : '',
  };
"#;

const LOGIN_STATE_BODY: &str = r#"
  document.querySelectorAll('[data-tbc-role="open-login"]').forEach(el => el.removeAttribute('data-tbc-role'));
  const passwords = Array.from(document.querySelectorAll('input[type=password]'));
  const openForm = passwords.some(visible);
  let opener = null;
  if (!openForm) {
    const candidates = innermost(Array.from(document.querySelectorAll(CLICKABLE))
        .filter(el => visible(el) && LOGIN_WORD.test(label(el))
                      && !SOCIAL.test(label(el)) && !OTHER_ROUTE.test(label(el))
                      && label(el).length <= 40));
    candidates.sort((a, b) => label(a).length - label(b).length);
    opener = candidates[0] || null;
    if (opener) opener.setAttribute('data-tbc-role', 'open-login');
  }
  const canvas = Array.from(document.querySelectorAll('canvas')).some(visible);
  const CODE_FIELD = /verif|c[oó]digo|\bcode\b|otp|2fa|one.?time/i;
  const codeField = Array.from(document.querySelectorAll('input')).some(el =>
      visible(el) && TEXT_INPUT(el) && el.type !== 'password'
      && (el.autocomplete === 'one-time-code'
          || CODE_FIELD.test([el.name, el.id, el.placeholder, el.getAttribute('aria-label') || ''].join(' '))));
  return {
    form_open: openForm, form_in_dom: passwords.length > 0,
    opener: opener ? label(opener).slice(0, 40) : "",
    canvas: canvas, code_asked: codeField && !canvas,
  };
"#;

pub fn mark_login_fields_js() -> String {
    format!("(() => {{{}{}}})()", JS_HELPERS, MARK_LOGIN_FIELDS_BODY)
}

pub fn login_state_js() -> String {
    format!("(() => {{{}{}}})()", JS_HELPERS, LOGIN_STATE_BODY)
}

pub const SESSION_MARK: &str = "tb_counter_session_verified";

/// The page as the login sees it. The real one is the Browser.
pub trait Page: Send + Sync {
    fn evaluate(&self, expression: &str) -> Option<Json>;
    fn wait_for_load(&self, timeout: Option<f64>) -> bool;
    fn user_data_dir(&self) -> PathBuf;
    fn wait_for(&self, _condition: &str, _timeout: f64) -> bool {
        false
    }
    fn element_rect(&self, _selector: &str) -> Option<(f64, f64, f64, f64)> {
        None
    }
    fn click_element(&self, _selector: &str, _delay: f64) -> Res<bool> {
        Ok(false)
    }
    fn navigate(&self) -> Res<()> {
        Ok(())
    }
    fn select_all(&self) -> Res<()> {
        Ok(())
    }
    fn press_key(&self, _name: &str, _delay: f64) -> Res<()> {
        Ok(())
    }
    fn insert_text(&self, _text: &str) -> Res<()> {
        Ok(())
    }
}

impl Page for Browser {
    fn evaluate(&self, expression: &str) -> Option<Json> {
        Browser::evaluate(self, expression)
    }
    fn wait_for_load(&self, timeout: Option<f64>) -> bool {
        Browser::wait_for_load(self, timeout)
    }
    fn user_data_dir(&self) -> PathBuf {
        self.user_data_dir.clone()
    }
    fn wait_for(&self, condition: &str, timeout: f64) -> bool {
        Browser::wait_for(self, condition, timeout)
    }
    fn element_rect(&self, selector: &str) -> Option<(f64, f64, f64, f64)> {
        Browser::element_rect(self, selector)
    }
    fn click_element(&self, selector: &str, delay: f64) -> Res<bool> {
        Browser::click_element(self, selector, delay)
    }
    fn navigate(&self) -> Res<()> {
        Browser::navigate(self, None)
    }
    fn select_all(&self) -> Res<()> {
        Browser::select_all(self)
    }
    fn press_key(&self, name: &str, delay: f64) -> Res<()> {
        Browser::press_key(self, name, delay)
    }
    fn insert_text(&self, text: &str) -> Res<()> {
        Browser::insert_text(self, text)
    }
}

fn flag(state: &Json, key: &str) -> bool {
    match state.get(key) {
        Some(Json::Bool(b)) => *b,
        Some(Json::String(s)) => !s.is_empty(),
        Some(Json::Number(n)) => n.as_f64().map(|f| f != 0.0).unwrap_or(false),
        _ => false,
    }
}

/// Authenticates an account on the game's web form.
pub struct LoginManager {
    pub page: Arc<dyn Page>,
    pub enabled: bool,
    open_login_selector: String,
    email_selector: String,
    password_selector: String,
    submit_selector: String,
    logged_in_selector: String,
    wait_after_submit: f64,
    max_attempts: i64,
}

impl LoginManager {
    pub fn new(page: Arc<dyn Page>, section: &Map<String, Json>) -> Self {
        let text = |k: &str| section.get(k).and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
        LoginManager {
            page,
            enabled: section.get("enabled").and_then(|v| v.as_bool()).unwrap_or(true),
            open_login_selector: text("open_login_selector"),
            email_selector: text("email_selector"),
            password_selector: text("password_selector"),
            submit_selector: text("submit_selector"),
            logged_in_selector: text("logged_in_selector"),
            wait_after_submit: section.get("wait_after_submit").and_then(|v| v.as_f64()).unwrap_or(30.0),
            max_attempts: section.get("max_attempts").and_then(|v| v.as_f64()).map(|f| f as i64).unwrap_or(2),
        }
    }

    /// What the page is showing right now, as seen from the DOM.
    pub fn state(&self) -> Json {
        self.page.evaluate(&login_state_js()).filter(|v| v.is_object()).unwrap_or(json!({}))
    }

    /// Logged in means: nothing on the page is offering to log us in. A visible
    /// Login button counts as evidence of being logged out, like a visible password
    /// field does; the game canvas being up outranks a stray 'Log in' in a footer.
    pub fn is_logged_in(&self) -> bool {
        if !self.logged_in_selector.is_empty() {
            return self.page.element_rect(&self.logged_in_selector).is_some();
        }
        let state = self.state();
        if flag(&state, "form_open") || flag(&state, "code_asked") {
            return false;
        }
        if flag(&state, "canvas") {
            return true;
        }
        !flag(&state, "opener")
    }

    /// Waits until the page says something about being logged in - either way.
    pub fn wait_until_decidable(&self, timeout: f64) -> Res<bool> {
        let deadline = Instant::now() + Duration::from_secs_f64(timeout);
        while Instant::now() < deadline {
            let state = self.state();
            if flag(&state, "form_in_dom") || flag(&state, "opener") || flag(&state, "canvas") {
                return Ok(true);
            }
            cancel::sleep(0.5)?;
        }
        crate::warn!("The page showed neither a login form nor the game within the timeout.");
        Ok(false)
    }

    fn open_login_form(&self, timeout: f64) -> Res<bool> {
        let state = self.state();
        if flag(&state, "form_open") {
            return Ok(true);
        }
        let selector = if !self.open_login_selector.is_empty() {
            self.open_login_selector.clone()
        } else if flag(&state, "opener") {
            "[data-tbc-role='open-login']".to_string()
        } else {
            String::new()
        };
        if selector.is_empty() {
            if flag(&state, "form_in_dom") {
                crate::warn!("The login form is in the page but closed, and the opener button was not found. Provide the \
                              selector in Login > CSS selector for login opener button.");
            }
            return Ok(false);
        }
        let via = state.get("opener").and_then(|o| o.as_str()).filter(|s| !s.is_empty()).map(String::from).unwrap_or(selector.clone());
        crate::info!("Opening the login form (via {}).", via);
        if !self.page.click_element(&selector, 0.6)? {
            return Ok(false);
        }
        let deadline = Instant::now() + Duration::from_secs_f64(timeout);
        while Instant::now() < deadline {
            if flag(&self.state(), "form_open") {
                crate::info!("Login form is open.");
                return Ok(true);
            }
            cancel::sleep(0.5)?;
        }
        crate::warn!("The login form did not open after clicking.");
        Ok(false)
    }

    /// Configured selectors win; anything left blank is discovered in the page.
    fn selectors(&self) -> Res<(String, String, String)> {
        if !self.email_selector.is_empty() && !self.password_selector.is_empty() && !self.submit_selector.is_empty() {
            return Ok((self.email_selector.clone(), self.password_selector.clone(), self.submit_selector.clone()));
        }
        let found = self.page.evaluate(&mark_login_fields_js()).unwrap_or(json!({}));
        if !flag(&found, "password") {
            return Err(Error::Session("Password field not found on the login page.".into()));
        }
        let pick = |configured: &str, mark: &str, has: bool| -> String {
            if !configured.is_empty() {
                configured.to_string()
            } else if has {
                format!("[data-tbc-role='{}']", mark)
            } else {
                String::new()
            }
        };
        Ok((pick(&self.email_selector, "email", flag(&found, "email")),
            pick(&self.password_selector, "password", true),
            pick(&self.submit_selector, "submit", flag(&found, "submit"))))
    }

    fn type_into(&self, selector: &str, text: &str) -> Res<bool> {
        if selector.is_empty() || !self.page.click_element(selector, 0.2)? {
            crate::error!("Could not click the field '{}'.", selector);
            return Ok(false);
        }
        self.page.select_all()?;
        self.page.press_key("Backspace", 0.05)?;
        self.page.insert_text(text)?;
        std::thread::sleep(Duration::from_millis(200));
        Ok(true)
    }

    /// Signs in as `account`, returning false if the credentials never took.
    pub fn login(&self, account: &AccountConfig) -> Res<bool> {
        if !self.enabled {
            crate::info!("Automatic login is disabled; using whatever session the browser has.");
            return Ok(self.is_logged_in());
        }
        if account.login.is_empty() || account.password.is_empty() {
            return Err(Error::Session(format!(
                "Account '{}' has no username and password configured. Fill them in the interface (Accounts and profiles \
                 tab) or disable automatic login.",
                account.name
            )));
        }
        for attempt in 1..=self.max_attempts {
            crate::info!("Logging in as '{}' ({}) - attempt {}/{}", account.name, account.login, attempt, self.max_attempts);
            self.wait_until_decidable(30.0)?;
            if self.is_logged_in() {
                crate::info!("A session was already open; nothing on the page asks for a login.");
                return Ok(true);
            }
            if !self.page.wait_for("document.querySelectorAll('input[type=password]').length > 0", 30.0) {
                crate::warn!("No login form exists in the page; reloading.");
                self.page.navigate()?;
                continue;
            }
            if !self.open_login_form(15.0)? && !flag(&self.state(), "form_open") {
                crate::warn!("Could not reach the login fields; reloading the page.");
                self.page.navigate()?;
                continue;
            }
            let (email, password, submit) = match self.selectors() {
                Ok(s) => s,
                Err(e) => {
                    crate::error!("{}", e);
                    self.page.navigate()?;
                    continue;
                }
            };
            if !email.is_empty() && !self.type_into(&email, &account.login)? {
                continue;
            }
            if !self.type_into(&password, &account.password)? {
                continue;
            }
            if !submit.is_empty() && self.page.click_element(&submit, 0.5)? {
                crate::info!("Login submitted.");
            } else {
                // Some forms have no button we can recognise; Enter submits them.
                crate::info!("No submit button identified; sending Enter.");
                self.page.press_key("Enter", 0.5)?;
            }
            if self.wait_until_logged_in()? {
                crate::info!("Logged in as '{}'.", account.name);
                return Ok(true);
            }
            crate::warn!("Login attempt {} for '{}' did not go through.", attempt, account.name);
        }
        crate::error!("Could not log in to account '{}'.", account.name);
        Ok(false)
    }

    /// Waits for the login to take effect, twice before believing it.
    fn wait_until_logged_in(&self) -> Res<bool> {
        let deadline = Instant::now() + Duration::from_secs_f64(self.wait_after_submit);
        while Instant::now() < deadline {
            cancel::check()?;
            if flag(&self.state(), "code_asked") {
                return Err(Error::NeedsPerson(self.needs_person("the game asked for the code it sent by e-mail")));
            }
            if self.is_logged_in() {
                cancel::sleep(1.0)?;
                if self.is_logged_in() {
                    let left = deadline.saturating_duration_since(Instant::now()).as_secs_f64();
                    self.page.wait_for_load(Some(left.max(5.0)));
                    return Ok(true);
                }
            }
            std::thread::sleep(Duration::from_secs(1));
        }
        Ok(false)
    }

    /// Makes sure the browser is authenticated as `account`, logging in only if needed.
    pub fn enter_account(&self, account: &AccountConfig) -> Res<bool> {
        self.enter_account_with(account, &mut |a| self.login(a))
    }

    pub fn enter_account_with(&self, account: &AccountConfig, login: &mut dyn FnMut(&AccountConfig) -> Res<bool>) -> Res<bool> {
        self.page.wait_for_load(None);
        self.wait_until_decidable(30.0)?;
        if flag(&self.state(), "code_asked") {
            return Err(Error::NeedsPerson(self.needs_person("the game is asking for the code it sent by e-mail")));
        }
        if self.is_logged_in() {
            crate::info!("Session already authenticated in the browser profile; no login needed.");
            return Ok(true);
        }
        if !self.folder_verified() {
            return Err(Error::NeedsPerson(self.needs_person("this Chrome folder has never logged in")));
        }
        crate::info!("The game is showing the login screen.");
        login(account)
    }

    fn mark_path(&self) -> PathBuf {
        self.page.user_data_dir().join(SESSION_MARK)
    }

    fn folder_verified(&self) -> bool {
        self.mark_path().exists()
    }

    /// Notes that the game came up in this folder, so a dropped session may be logged back into.
    pub fn remember_session(&self) {
        if self.folder_verified() || !flag(&self.state(), "canvas") {
            return;
        }
        if let Err(e) = std::fs::write(self.mark_path(), b"") {
            crate::warn!("Could not note the verified session in {}: {}", self.mark_path().display(), e);
        }
    }

    fn needs_person(&self, why: &str) -> String {
        format!("{}. Log in by hand once: Accounts and profiles > Open window, and type the code. The next runs use that \
                 session (folder: {}).", why, self.page.user_data_dir().display())
    }
}

// ---------------------------------------------------------------- profiles

/// Asks the game for its linked profiles and navigates to the target's auth token,
/// all inside the tab: the tokens never leave it, and what comes back is a status
/// and the id of the profile, which is not a secret.
pub const SWITCH_JS: &str = r#"
(() => {
  return (async () => {
    const CURRENT_AUTH = {
      suid: window.app && window.app.config ? window.app.config.suid : null,
      token: window.app && window.app.config ? (window.app.config.user_auth_token || (window.app.getGameStartDataResponse && window.app.getGameStartDataResponse.new_token)) : null,
    };
    if (!CURRENT_AUTH.suid || !CURRENT_AUTH.token) {
      return "no_auth";
    }
    const restUrl = (window.app && window.app.restApiUrl) || "https://totalbattle.com/restapi/totalbattle/web/";
    const deviceId = (window.app && window.app.deviceIdentifier) || "";
    let resp;
    try {
      resp = await fetch(restUrl, {
        method: "POST",
        headers: {"Content-Type": "application/json", "x-rpc-method": "GetLinkedAccountsInfo"},
        body: JSON.stringify({
          jsonrpc: "2.0", id: "switch-account", method: "GetLinkedAccountsInfo",
          params: [{suid: CURRENT_AUTH.suid, token: CURRENT_AUTH.token}],
          deviceIdentifier: deviceId
        })
      });
    } catch (err) {
      return "fetch_failed: " + (err && err.message ? err.message : String(err));
    }
    if (!resp.ok) {
      return "http_error_" + resp.status;
    }
    let data;
    try {
      data = await resp.json();
    } catch (err) {
      return "json_parse_error";
    }
    if (data && data.error) {
      return "rpc_error: " + (data.error.code || "") + " " + String(data.error.message || "").slice(0, 120);
    }
    const tethers = data && data.result && data.result.tethers;
    if (!tethers || !tethers.length) {
      return "no_tethers";
    }
    const targetName = __TARGET__;
    const norm = (s) => (s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/\s+/g, " ").trim().toLowerCase();
    const same = tethers.filter(t => norm(t.progress_short_info && t.progress_short_info.nickname) === norm(targetName));
    if (same.length !== 1) {
      return same.length ? "profile_ambiguous" : "profile_not_found";
    }
    const target = same[0];
    const uid = target.progress_short_info.gameUid;
    if (!uid) {
      return "no_game_id";
    }
    const playerId = (BigInt(uid.initial) * (2n ** 32n) + BigInt(uid.sequence)).toString();
    const siteUrl = (window.app && window.app.config && window.app.config.site_url)
      ? window.app.config.site_url.replace(/\/$/, "")
      : "https://totalbattle.com";
    const origin = siteUrl.startsWith("http") ? siteUrl : "https:" + siteUrl;
    let switchUrl = origin + "/login/bytokenoauth/?token=" + encodeURIComponent(target.token);
    if (target.suid) {
      switchUrl += "&suid=" + encodeURIComponent(target.suid);
    }
    // Without it the page expects to be a child of the game's and stays blank in the tab.
    switchUrl += "&state=switchAccount";
    setTimeout(() => { window.location.href = switchUrl; }, 50);
    return "going:" + playerId;
  })();
})()
"#;

pub fn switch_js(profile_name: &str) -> String {
    SWITCH_JS.replace("__TARGET__", &Json::String(profile_name.to_string()).to_string())
}

/// How long a profile is given to load, and how often it is looked at meanwhile.
pub const SWITCH_TIMEOUT: f64 = 180.0;
pub const SWITCH_POLL: f64 = 5.0;

/// What the switcher needs from the window.
pub trait SwitchBackend {
    /// The value of an awaited expression; the error text when it failed.
    fn evaluate_async(&self, expression: &str) -> Json;
    fn ready_state(&self) -> Option<Json>;
    /// A started link to the game, None when it could not start.
    fn open_link(&self, quiet: bool) -> Option<Box<dyn GameApi>>;
    fn sleep(&self, seconds: f64) -> Res<()> {
        cancel::sleep(seconds)
    }
    fn now(&self) -> f64 {
        std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
    }
}

impl SwitchBackend for Browser {
    fn evaluate_async(&self, expression: &str) -> Json {
        match self.send("Runtime.evaluate", json!({"expression": expression, "awaitPromise": true, "returnByValue": true}), 30.0) {
            Err(e) => {
                crate::warn!("Failed to evaluate expression: {}", e);
                Json::String(e.to_string())
            }
            Ok(r) => {
                if let Some(d) = r.get("exceptionDetails") {
                    let text = d.get("text").and_then(|t| t.as_str()).unwrap_or("").trim().to_string();
                    crate::warn!("JS exception in evaluate_async: {}", text);
                    return Json::String(text);
                }
                r.get("result").and_then(|r| r.get("value")).cloned().unwrap_or(Json::Null)
            }
        }
    }
    fn ready_state(&self) -> Option<Json> {
        self.evaluate("document.readyState")
    }
    fn open_link(&self, quiet: bool) -> Option<Box<dyn GameApi>> {
        let status: Option<Arc<dyn Fn(&str) + Send + Sync>> =
            if quiet { None } else { Some(Arc::new(|t: &str| crate::warn!("Game link: {}", t))) };
        let mut link = GameLink::new(self.target(), status);
        link.start().then(|| Box::new(link) as Box<dyn GameApi>)
    }
}

/// The name of the profile the game is on, as the game says it; "" when it could not be asked.
pub fn active_profile_name(backend: &dyn SwitchBackend) -> String {
    let Some(link) = backend.open_link(false) else { return String::new() };
    let Some(player) = link.player_id() else { return String::new() };
    match crate::link::player_names(link.as_ref(), &[player]) {
        Ok(names) => names.get(&player).cloned().unwrap_or_default(),
        Err(e) => {
            crate::warn!("Could not ask the game which profile it is on: {}", e);
            String::new()
        }
    }
}

/// Which of `names` is the profile the game is on, or "" when it is none of them.
pub fn active_among(backend: &dyn SwitchBackend, names: &[String]) -> String {
    let active = normalize_name(&active_profile_name(backend));
    if active.is_empty() {
        return String::new();
    }
    names.iter().find(|n| !n.is_empty() && normalize_name(n) == active).cloned().unwrap_or_default()
}

fn is_playing(backend: &dyn SwitchBackend, player_id: i128) -> bool {
    if backend.ready_state() != Some(json!("complete")) {
        return false;
    }
    match backend.open_link(true) {
        Some(link) => link.player_id() == Some(player_id),
        None => false,
    }
}

/// Selects `profile`. It only counts when the game comes back carrying the id the
/// list gave for that name: a near-twin name would cost a database.
pub fn switch_to(backend: &dyn SwitchBackend, profile: &ProfileConfig) -> Res<bool> {
    if !active_among(backend, std::slice::from_ref(&profile.name)).is_empty() {
        crate::info!("Profile '{}' is ALREADY active: going straight to its modules.", profile.name);
        return Ok(true);
    }
    let status = backend.evaluate_async(&switch_js(&profile.name));
    let player = status.as_str().and_then(|s| s.strip_prefix("going:")).and_then(|s| s.parse::<i128>().ok());
    let Some(player) = player else {
        let shown = match &status {
            Json::String(s) => s.clone(),
            Json::Null => "None".into(),
            other => other.to_string(),
        };
        crate::error!("Profile '{}' not switched: {}", profile.name, shown);
        return Ok(false);
    };
    crate::info!("Switching to profile '{}'; waiting for the game to load it...", profile.name);
    let started = backend.now();
    while backend.now() - started < SWITCH_TIMEOUT {
        backend.sleep(SWITCH_POLL)?;
        if is_playing(backend, player) {
            crate::info!("Profile '{}' is active after {:.0}s.", profile.name, backend.now() - started);
            return Ok(true);
        }
    }
    crate::error!("The game did not come back on profile '{}' in {:.0}s.", profile.name, SWITCH_TIMEOUT);
    Ok(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::arr;
    use crate::tbcodec::{Frame, Value};
    use std::sync::Mutex;

    // ----------------------------------------------------------- first login

    struct FakePage {
        folder: PathBuf,
        state: Mutex<Json>,
    }

    impl Page for FakePage {
        fn evaluate(&self, _e: &str) -> Option<Json> {
            Some(self.state.lock().unwrap().clone())
        }
        fn wait_for_load(&self, _t: Option<f64>) -> bool {
            true
        }
        fn user_data_dir(&self) -> PathBuf {
            self.folder.clone()
        }
    }

    fn login(dir: &std::path::Path, state: Json) -> (LoginManager, Arc<FakePage>) {
        let page = Arc::new(FakePage { folder: dir.to_path_buf(), state: Mutex::new(state) });
        (LoginManager::new(page.clone(), &Map::new()), page)
    }

    fn who() -> AccountConfig {
        AccountConfig { name: "Main".into(), login: "a".into(), password: "b".into(), ..Default::default() }
    }

    #[test]
    fn a_folder_that_never_logged_in_waits_for_a_person_without_typing_the_password() {
        let dir = tempfile::tempdir().unwrap();
        let (l, _) = login(dir.path(), json!({"form_in_dom": true, "opener": "Log in"}));
        let mut typed = vec![];
        let r = l.enter_account_with(&who(), &mut |a| {
            typed.push(a.name.clone());
            Ok(true)
        });
        assert!(matches!(&r, Err(Error::NeedsPerson(m)) if m.contains("Open window")));
        assert!(typed.is_empty());
    }

    #[test]
    fn a_dropped_session_in_a_verified_folder_is_logged_back_into() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join(SESSION_MARK), b"").unwrap();
        let (l, _) = login(dir.path(), json!({"form_in_dom": true, "opener": "Log in"}));
        let mut typed = vec![];
        assert!(l.enter_account_with(&who(), &mut |a| {
            typed.push(a.name.clone());
            Ok(true)
        }).unwrap());
        assert_eq!(vec!["Main"], typed);
    }

    #[test]
    fn the_code_screen_is_not_taken_for_being_logged_in_even_in_a_verified_folder() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join(SESSION_MARK), b"").unwrap();
        let (l, _) = login(dir.path(), json!({"form_in_dom": false, "code_asked": true}));
        assert!(!l.is_logged_in());
        let mut typed = 0;
        assert!(matches!(l.enter_account_with(&who(), &mut |_| {
            typed += 1;
            Ok(true)
        }), Err(Error::NeedsPerson(_))));
        assert_eq!(0, typed);
    }

    #[test]
    fn a_folder_counts_as_verified_only_once_the_game_has_come_up_in_it() {
        let dir = tempfile::tempdir().unwrap();
        let (l, page) = login(dir.path(), json!({"opener": ""}));
        l.remember_session();
        assert!(!dir.path().join(SESSION_MARK).exists());
        *page.state.lock().unwrap() = json!({"canvas": true});
        assert!(l.enter_account_with(&who(), &mut |_| Ok(false)).unwrap(), "a person logged in: the game opens by itself");
        l.remember_session();
        assert!(dir.path().join(SESSION_MARK).exists());
    }

    // ------------------------------------------------------------- switching

    const TARGET: i128 = 485331438653;
    const OTHER: i128 = 485331363759;

    struct OneLook(i128);
    impl GameApi for OneLook {
        fn player_id(&self) -> Option<i128> {
            Some(self.0)
        }
        fn call(&self, _route: i128, objects: Value) -> Res<Vec<Frame>> {
            let id = objects.at(0).unwrap().at(0).unwrap().at(0).unwrap().strict_int().unwrap();
            let name = if id == TARGET { "Tesoureiro" } else { "Crash BR" };
            let mut row = vec![arr![id], Value::str("tb:1"), Value::str(name)];
            row.extend((0..10).map(|_| Value::Int(0)));
            Ok(vec![Frame::of(vec![arr![Value::Arr(vec![Value::Arr(row)])]])])
        }
    }

    struct Backend {
        status: Json,
        players: Mutex<Vec<i128>>,
        states: Mutex<Vec<&'static str>>,
        clock: Mutex<f64>,
    }

    impl Backend {
        fn new(status: Json, players: Vec<i128>, states: Vec<&'static str>) -> Self {
            Backend { status, players: Mutex::new(players), states: Mutex::new(states), clock: Mutex::new(0.0) }
        }
    }

    impl SwitchBackend for Backend {
        fn evaluate_async(&self, _e: &str) -> Json {
            self.status.clone()
        }
        fn ready_state(&self) -> Option<Json> {
            let mut s = self.states.lock().unwrap();
            Some(json!(if s.len() > 1 { s.remove(0) } else { s[0] }))
        }
        fn open_link(&self, _quiet: bool) -> Option<Box<dyn GameApi>> {
            let mut p = self.players.lock().unwrap();
            let id = if p.len() > 1 { p.remove(0) } else { p[0] };
            Some(Box::new(OneLook(id)))
        }
        fn sleep(&self, s: f64) -> Res<()> {
            *self.clock.lock().unwrap() += s;
            Ok(())
        }
        fn now(&self) -> f64 {
            *self.clock.lock().unwrap()
        }
    }

    fn switch(status: Json, players: Vec<i128>, states: Vec<&'static str>) -> (bool, Backend) {
        let b = Backend::new(status, players, states);
        let r = switch_to(&b, &ProfileConfig::named("Tesoureiro", "Main")).unwrap();
        (r, b)
    }

    #[test]
    fn it_arrives_when_the_loaded_game_carries_the_id_of_the_profile() {
        assert!(switch(json!(format!("going:{}", TARGET)), vec![OTHER, OTHER, TARGET], vec!["complete"]).0);
    }

    #[test]
    fn the_profile_already_on_needs_no_switch() {
        assert!(switch(json!("never sent"), vec![TARGET], vec!["complete"]).0);
    }

    #[test]
    fn nothing_is_counted_while_the_page_is_loading() {
        let (ok, b) = switch(json!(format!("going:{}", TARGET)), vec![OTHER, TARGET], vec!["loading", "loading", "complete"]);
        assert!(ok);
        assert_eq!(vec![TARGET], *b.players.lock().unwrap());
    }

    #[test]
    fn a_game_that_comes_back_on_another_profile_is_a_failure() {
        assert!(!switch(json!(format!("going:{}", TARGET)), vec![OTHER], vec!["complete"]).0);
    }

    #[test]
    fn the_login_page_is_told_it_opens_in_the_tab_itself() {
        assert!(SWITCH_JS.contains("\"&state=switchAccount\""));
        assert!(switch_js("Crash \"BR\"").contains(r#"const targetName = "Crash \"BR\"";"#));
    }

    #[test]
    fn a_refusal_from_the_page_is_not_waited_on() {
        for status in [json!("profile_not_found"), json!("profile_ambiguous"), json!("no_auth"), json!("rpc_error: 1 expired"), Json::Null] {
            let (ok, b) = switch(status, vec![OTHER], vec!["complete"]);
            assert!(!ok);
            assert_eq!(0.0, *b.clock.lock().unwrap(), "nothing waited for");
        }
    }
}
