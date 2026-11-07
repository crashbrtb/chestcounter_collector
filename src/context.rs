//! The services of one account's window, and the windows of a run.
//!
//! Every service is built from one ConfigManager, in one place, so the interface
//! builds the very same windows a scheduled run does.

use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, Mutex};

use crate::browser::Browser;
use crate::config::{AccountConfig, ConfigManager, ProfileConfig};
use crate::error::Res;
use crate::link::{GameApi, GameLink, JournalFeed, JournalSource};
use crate::paths;
use crate::session::{self, LoginManager};

/// One account's window, as the run and the modules use it.
pub trait Window: Send + Sync {
    fn port(&self) -> u16;
    /// Opens the browser; a no-op on a live connection.
    fn start_browser(&self) -> Res<()>;
    fn login_enabled(&self) -> bool;
    fn enter_account(&self, account: &AccountConfig) -> Res<bool>;
    fn navigate(&self) -> Res<()>;
    fn wait_for_load(&self);
    fn remember_session(&self);
    /// Which of `names` is the profile the game is on.
    fn active_among(&self, names: &[String]) -> Res<String>;
    fn switch_to(&self, profile: &ProfileConfig) -> Res<bool>;
    fn restart_browser(&self) -> Res<()>;
    fn save_screenshot(&self, path: &Path) -> bool;
    /// A started link to the game; None when it could not start.
    fn game_link(&self) -> Option<Box<dyn GameApi>>;
    /// A started Journal feed; None when it could not start.
    fn journal(&self) -> Option<Box<dyn JournalSource + Send>>;
    fn close(&self);
    fn disconnect(&self);
}

/// The windows of a run, one per account.
pub trait Windows: Send + Sync {
    fn for_account(&self, account: &AccountConfig) -> Arc<dyn Window>;
    fn login_enabled(&self) -> bool;
    fn close(&self);
}

pub struct RealWindow {
    pub browser: Arc<Browser>,
    pub login: LoginManager,
    close_on_finish: bool,
}

fn status() -> Option<Arc<dyn Fn(&str) + Send + Sync>> {
    Some(Arc::new(|t: &str| crate::warn!("Game link: {}", t)))
}

impl RealWindow {
    pub fn new(config: &ConfigManager, browser: Browser) -> Self {
        let browser = Arc::new(browser);
        let login = LoginManager::new(browser.clone(), &config.section("login"));
        RealWindow { browser, login, close_on_finish: config.get_bool("execution", "close_browser_on_finish", true) }
    }
}

impl Window for RealWindow {
    fn port(&self) -> u16 {
        self.browser.port
    }
    fn start_browser(&self) -> Res<()> {
        if self.browser.is_connected() {
            return Ok(());
        }
        self.browser.start()
    }
    fn login_enabled(&self) -> bool {
        self.login.enabled
    }
    fn enter_account(&self, account: &AccountConfig) -> Res<bool> {
        self.login.enter_account(account)
    }
    fn navigate(&self) -> Res<()> {
        self.browser.navigate(None)
    }
    fn wait_for_load(&self) {
        self.browser.wait_for_load(None);
    }
    fn remember_session(&self) {
        self.login.remember_session()
    }
    fn active_among(&self, names: &[String]) -> Res<String> {
        Ok(session::active_among(self.browser.as_ref(), names))
    }
    fn switch_to(&self, profile: &ProfileConfig) -> Res<bool> {
        session::switch_to(self.browser.as_ref(), profile)
    }
    fn restart_browser(&self) -> Res<()> {
        self.browser.restart()
    }
    fn save_screenshot(&self, path: &Path) -> bool {
        self.browser.save_screenshot(path)
    }
    fn game_link(&self) -> Option<Box<dyn GameApi>> {
        let mut link = GameLink::new(self.browser.target(), status());
        link.start().then(|| Box::new(link) as Box<dyn GameApi>)
    }
    fn journal(&self) -> Option<Box<dyn JournalSource + Send>> {
        let mut feed = JournalFeed::new(self.browser.target(), status());
        feed.start().then(|| Box::new(feed) as Box<dyn JournalSource + Send>)
    }
    fn close(&self) {
        if self.close_on_finish {
            self.browser.close();
        } else {
            crate::info!("Leaving the browser open (see Execution > Close browser on finish).");
            self.browser.disconnect();
        }
    }
    fn disconnect(&self) {
        self.browser.disconnect();
    }
}

/// The Chrome window of an account: its port, its folder.
pub fn browser_for(config: &ConfigManager, account: &AccountConfig) -> Browser {
    Browser::new(&config.section("browser"), account.cdp_port, &config.chrome_folder_for(account))
}

pub struct RealWindows {
    config: ConfigManager,
    pub primary: Arc<RealWindow>,
    primary_account: Option<AccountConfig>,
    windows: Mutex<HashMap<String, Arc<RealWindow>>>,
}

impl RealWindows {
    /// The windows of a run; the first account's window serves the checks.
    pub fn new(config: &mut ConfigManager) -> Self {
        config.pin_windows();
        let accounts = config.enabled_accounts();
        let account = accounts.first().cloned().or_else(|| config.accounts().first().cloned());
        let browser = match &account {
            Some(a) => browser_for(config, a),
            None => Browser::new(&config.section("browser"), config.get_i64("browser", "first_cdp_port", 9322) as u16,
                                 &config.profiles_dir().join("default")),
        };
        RealWindows { primary: Arc::new(RealWindow::new(config, browser)), primary_account: account, config: config.clone(),
                      windows: Mutex::new(HashMap::new()) }
    }

    fn is_primary(&self, account: &AccountConfig) -> bool {
        self.primary_account.is_some()
            && self.primary.browser.port == account.cdp_port
            && paths::same_folder(&self.primary.browser.user_data_dir, &self.config.chrome_folder_for(account))
    }
}

impl Windows for RealWindows {
    fn for_account(&self, account: &AccountConfig) -> Arc<dyn Window> {
        if self.is_primary(account) {
            return self.primary.clone();
        }
        let mut windows = self.windows.lock().unwrap();
        windows
            .entry(account.name.clone())
            .or_insert_with(|| Arc::new(RealWindow::new(&self.config, browser_for(&self.config, account))))
            .clone()
    }

    fn login_enabled(&self) -> bool {
        self.primary.login.enabled
    }

    fn close(&self) {
        for w in self.windows.lock().unwrap().values() {
            w.close();
        }
        self.primary.close();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::DatabaseConfig;

    fn account(name: &str, db: &str) -> AccountConfig {
        AccountConfig {
            name: name.into(),
            login: format!("{}@example.com", name),
            password: "pw".into(),
            profiles: vec![ProfileConfig { name: format!("{}0", name), account_name: name.into(),
                                           database: Some(DatabaseConfig { host: "127.0.0.1".into(), port: 3306, user: "u".into(),
                                                                           password: String::new(), database: db.into() }),
                                           ..Default::default() }],
            ..Default::default()
        }
    }

    #[test]
    fn the_services_of_each_account_are_on_its_own_window() {
        let dir = tempfile::tempdir().unwrap();
        let mut config = ConfigManager::open(&dir.path().join("config.json")).unwrap();
        config.set("browser", "profiles_dir", serde_json::json!(dir.path().to_string_lossy()));
        config.set_accounts(&[account("A", "a"), account("B", "b")]);
        let windows = RealWindows::new(&mut config);
        let accounts = config.accounts();
        assert_eq!(9322, windows.primary.browser.port, "the first account's window, for the checks");
        let first = windows.for_account(&accounts[0]);
        assert_eq!(9322, first.port());
        let second = windows.for_account(&accounts[1]);
        assert_eq!(9323, second.port());
        assert!(Arc::ptr_eq(&second, &windows.for_account(&accounts[1])));
    }
}
