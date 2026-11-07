//! The game inside Chrome, driven by CDP (Chrome DevTools Protocol).
//!
//! One Browser is one account's window: a debugging port and the folder that keeps
//! the session the game verified. A port says nothing about who is behind it, so a
//! window is only used, or closed, after its command line shows it is on our folder
//! (`check_owner`). The wrong window would mean another account's chests in this
//! one's database.

use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine;
use regex::Regex;
use serde_json::{json, Map, Value as Json};

use crate::cdp;
use crate::error::{Error, Res};
use crate::link::Target;
use crate::paths;

pub const CHROME_PATHS: [&str; 8] = [
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe",
];

/// Against Chrome throttling a window it considers covered or unseen: with nobody
/// looking at the screen, Chrome stops drawing the game. A reused browser that lacks
/// any of them is restarted.
pub const THROTTLING_FLAGS: [&str; 4] = [
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--disable-features=CalculateNativeWinOcclusion,Translate,TranslateUI",
];

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

fn hidden(command: &mut Command) -> &mut Command {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    command
}

/// The --user-data-dir of a Chrome command line, whichever way it was quoted; "" when it has none.
pub fn command_line_folder(command_line: &str) -> String {
    let quoted = Regex::new(r#"--user-data-dir="([^"]+)""#).unwrap();
    if let Some(c) = quoted.captures(command_line) {
        // Only when the quote follows the flag directly, as the original pattern had it.
        let start = c.get(0).unwrap().start();
        let first = command_line.find("--user-data-dir=").unwrap_or(start);
        if first == start {
            return c[1].to_string();
        }
    }
    let Some(pos) = command_line.find("--user-data-dir=") else { return String::new() };
    let rest = &command_line[pos + "--user-data-dir=".len()..];
    if rest.starts_with('"') {
        return String::new();
    }
    // Lazy match up to whitespace followed by an optional quote and "--", whitespace + http(s):, a quote, or the end.
    let stops = Regex::new(r#"\s+"?--|\s+https?:|"|\s*$"#).unwrap();
    let mut end = rest.len();
    for m in stops.find_iter(rest) {
        if m.start() > 0 {
            end = m.start();
            break;
        }
    }
    rest[..end].to_string()
}

pub fn on_port(command_line: &str, port: u16) -> bool {
    let needle = format!("--remote-debugging-port={}", port);
    let mut search = command_line;
    while let Some(pos) = search.find(&needle) {
        let after = &search[pos + needle.len()..];
        if !after.chars().next().map(|c| c.is_ascii_digit()).unwrap_or(false) {
            return true;
        }
        search = after;
    }
    false
}

/// The command line of every Chrome started with a folder of its own; None when they cannot be read.
pub fn browser_processes() -> Option<Vec<String>> {
    if !cfg!(windows) {
        return None;
    }
    let script = "[Console]::OutputEncoding = [Text.Encoding]::UTF8; \
                  Get-CimInstance Win32_Process | \
                  Where-Object { $_.CommandLine -and $_.CommandLine.Contains('--user-data-dir=') \
                  -and -not $_.CommandLine.Contains('--type=') } | \
                  ForEach-Object { $_.CommandLine }";
    let output = hidden(Command::new("powershell").args(["-NoProfile", "-NonInteractive", "-Command", script]))
        .stdin(Stdio::null())
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    Some(String::from_utf8_lossy(&output.stdout).lines().map(|l| l.trim_end_matches('\r').to_string()).filter(|l| !l.is_empty()).collect())
}

struct Conn {
    ws: Option<cdp::Ws>,
    msg_id: u64,
}

pub struct Browser {
    pub host: String,
    pub port: u16,
    pub game_url: String,
    pub url_filter: String,
    pub executable_path: String,
    pub user_data_dir: PathBuf,
    pub reuse_existing: bool,
    pub start_maximized: bool,
    pub window_size: (i64, i64),
    pub page_load_timeout: f64,
    conn: Mutex<Conn>,
    tab_id: Mutex<Option<String>>,
    process: Mutex<Option<Child>>,
    launched_by_us: AtomicBool,
}

fn text(section: &Map<String, Json>, key: &str, default: &str) -> String {
    section.get(key).and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or(default).to_string()
}

impl Browser {
    pub fn new(section: &Map<String, Json>, port: u16, user_data_dir: &Path) -> Self {
        let num = |k: &str, d: f64| section.get(k).and_then(|v| v.as_f64()).unwrap_or(d);
        let flag = |k: &str, d: bool| section.get(k).and_then(|v| v.as_bool()).unwrap_or(d);
        Browser {
            host: text(section, "cdp_host", "127.0.0.1"),
            port,
            game_url: text(section, "game_url", "https://totalbattle.com/en/"),
            url_filter: text(section, "game_url_filter", "totalbattle.com").to_lowercase(),
            executable_path: section.get("executable_path").and_then(|v| v.as_str()).unwrap_or("").to_string(),
            user_data_dir: user_data_dir.to_path_buf(),
            reuse_existing: flag("reuse_existing", true),
            start_maximized: flag("start_maximized", true),
            window_size: (num("window_width", 1920.0) as i64, num("window_height", 1080.0) as i64),
            page_load_timeout: num("page_load_timeout", 90.0),
            conn: Mutex::new(Conn { ws: None, msg_id: 0 }),
            tab_id: Mutex::new(None),
            process: Mutex::new(None),
            launched_by_us: AtomicBool::new(false),
        }
    }

    pub fn tab_id(&self) -> Option<String> {
        self.tab_id.lock().unwrap().clone()
    }

    pub fn target(&self) -> Target {
        Target { host: self.host.clone(), port: self.port, tab_id: self.tab_id(), url_filter: self.url_filter.clone() }
    }

    pub fn is_connected(&self) -> bool {
        self.conn.lock().unwrap().ws.is_some()
    }

    // ------------------------------------------------------------------ setup

    pub fn is_cdp_ready(&self) -> bool {
        matches!(cdp::http_get(&self.host, self.port, "/json/version", Duration::from_secs(1)), Ok((200, _)))
    }

    pub fn find_executable(&self) -> Option<PathBuf> {
        if !self.executable_path.is_empty() && Path::new(&self.executable_path).exists() {
            return Some(PathBuf::from(&self.executable_path));
        }
        CHROME_PATHS.iter().map(|p| PathBuf::from(paths::expand_vars(p))).find(|p| p.exists())
    }

    /// Starts Chrome with remote debugging on its own profile directory.
    pub fn launch(&self) -> Res<()> {
        let executable = self.find_executable().ok_or_else(|| {
            Error::Browser("Chrome/Edge/Brave not found. Set the path in Browser > Chrome executable path.".into())
        })?;
        let _ = std::fs::create_dir_all(&self.user_data_dir);
        let mut args = vec![
            format!("--remote-debugging-port={}", self.port),
            "--remote-allow-origins=*".to_string(),
            format!("--user-data-dir={}", self.user_data_dir.display()),
            "--no-first-run".into(),
            "--no-default-browser-check".into(),
            "--disable-session-crashed-bubble".into(),
            "--disable-popup-blocking".into(),
        ];
        args.extend(THROTTLING_FLAGS.iter().map(|s| s.to_string()));
        // The translate bar is browser chrome, not page content; disabled so what you see is what the collector sees.
        args.push("--disable-translate".into());
        if self.start_maximized {
            args.push("--start-maximized".into());
        } else {
            args.push(format!("--window-size={},{}", self.window_size.0, self.window_size.1));
        }
        args.push(self.game_url.clone());
        crate::info!("Launching browser: {} (CDP port {})", executable.file_name().unwrap_or_default().to_string_lossy(), self.port);
        let child = Command::new(&executable)
            .args(&args)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| Error::Browser(format!("Could not start {}: {}", executable.display(), e)))?;
        *self.process.lock().unwrap() = Some(child);
        self.launched_by_us.store(true, Ordering::SeqCst);
        let deadline = Instant::now() + Duration::from_secs(30);
        while Instant::now() < deadline {
            if self.is_cdp_ready() {
                return Ok(());
            }
            thread::sleep(Duration::from_millis(300));
        }
        Err(Error::Browser(format!("The browser did not respond on CDP port {} within 30s.", self.port)))
    }

    /// Reuses a browser that is already listening and answers, otherwise launches one.
    pub fn start(&self) -> Res<()> {
        if self.is_cdp_ready() {
            self.check_owner()?;
            if !self.reuse_existing {
                return self.restart();
            }
            crate::info!("Reusing the browser already listening on CDP port {}.", self.port);
            let missing = self.missing_launch_flags();
            if !missing.is_empty() {
                crate::warn!("The browser on CDP port {} was not opened by the collector and lacks {}: with nobody looking \
                              at the screen, Chrome stops drawing the game. Restarting it with the collector's flags.",
                             self.port, missing.join(", "));
                return self.restart();
            }
            match self.connect() {
                Ok(true) if self.is_responsive(10.0) => return Ok(()),
                Ok(_) => {}
                Err(e) => crate::debug!("Reused browser: {}", e),
            }
            crate::warn!("The browser that was already open is not responding; restarting it.");
            return self.restart();
        }
        self.check_folder_free()?;
        self.launch()?;
        if !self.connect()? {
            return Err(Error::Browser("Could not connect to the game tab via CDP.".into()));
        }
        Ok(())
    }

    /// The THROTTLING_FLAGS the browser on our port was started without; empty when unknown.
    pub fn missing_launch_flags(&self) -> Vec<String> {
        let ours: Vec<String> = browser_processes().unwrap_or_default().into_iter().filter(|l| on_port(l, self.port)).collect();
        let Some(first) = ours.first() else { return vec![] };
        THROTTLING_FLAGS.iter().filter(|f| !first.contains(*f)).map(|f| f.to_string()).collect()
    }

    /// Fails with WrongWindow unless the Chrome on our port is on our folder. Not knowing counts as no.
    pub fn check_owner(&self) -> Res<()> {
        if !cfg!(windows) {
            return Ok(());
        }
        let Some(lines) = browser_processes() else {
            return Err(Error::WrongWindow(format!("Could not confirm whose Chrome is on port {}, so it was not used.", self.port)));
        };
        let folder = lines.iter().find(|l| on_port(l, self.port)).map(|l| command_line_folder(l)).unwrap_or_default();
        if folder.is_empty() || !paths::same_folder(Path::new(&folder), &self.user_data_dir) {
            return Err(Error::WrongWindow(format!(
                "Port {} is taken by another Chrome (folder: {}), not by {}. It was not used nor closed. Change this \
                 account's port in Accounts and profiles, or close that window.",
                self.port,
                if folder.is_empty() { "unknown" } else { &folder },
                self.user_data_dir.display()
            )));
        }
        Ok(())
    }

    /// Fails with WrongWindow when our folder is already open in a window that does not answer on our port.
    pub fn check_folder_free(&self) -> Res<()> {
        for line in browser_processes().unwrap_or_default() {
            let folder = command_line_folder(&line);
            if !folder.is_empty() && paths::same_folder(Path::new(&folder), &self.user_data_dir) {
                return Err(Error::WrongWindow(format!(
                    "The Chrome folder {} is already open in a window that does not answer on port {}. Close that window, \
                     or give this account another folder.",
                    self.user_data_dir.display(),
                    self.port
                )));
            }
        }
        Ok(())
    }

    /// Opens the account's window and leaves it to whoever is at the keyboard (the code by e-mail).
    pub fn open_for_person(&self) -> Res<()> {
        if self.is_cdp_ready() {
            self.check_owner()?;
            if let Some(tab) = self.find_game_tab() {
                *self.tab_id.lock().unwrap() = tab["id"].as_str().map(String::from);
                self.focus_tab();
            }
            return Ok(());
        }
        self.check_folder_free()?;
        self.launch()
    }

    /// True when the game tab still runs JavaScript and still renders a frame.
    pub fn is_responsive(&self, timeout: f64) -> bool {
        let check = || -> Res<bool> {
            let r = self.send("Runtime.evaluate", json!({"expression": "1+1", "returnByValue": true}), timeout)?;
            if r["result"]["value"] != json!(2) {
                return Ok(false);
            }
            self.send("Page.captureScreenshot",
                      json!({"format": "jpeg", "quality": 10, "clip": {"x": 0, "y": 0, "width": 8, "height": 8, "scale": 1}}),
                      timeout)?;
            Ok(true)
        };
        match check() {
            Ok(v) => v,
            Err(e) => {
                crate::debug!("Responsiveness check failed: {}", e);
                false
            }
        }
    }

    /// Closes the browser - whoever opened it - and launches a fresh one.
    pub fn restart(&self) -> Res<()> {
        if self.is_cdp_ready() {
            self.check_owner()?;
        }
        crate::info!("Restarting the browser...");
        self.kill()?;
        thread::sleep(Duration::from_millis(1500));
        self.launch()?;
        if !self.connect()? {
            return Err(Error::Browser("Could not connect to the game tab via CDP after restarting the browser.".into()));
        }
        Ok(())
    }

    /// Closes the browser on our port even when this run did not open it: politely
    /// first, then by process.
    pub fn kill(&self) -> Res<()> {
        self.disconnect();
        if self.is_cdp_ready() {
            if let Ok((200, body)) = cdp::http_get(&self.host, self.port, "/json/version", Duration::from_secs(2)) {
                if let Some(url) = serde_json::from_str::<Json>(&body).ok().and_then(|v| v["webSocketDebuggerUrl"].as_str().map(String::from)) {
                    match cdp::connect(&url, Duration::from_secs(5)) {
                        Ok(mut ws) => {
                            let _ = cdp::send(&mut ws, cdp::command(1, "Browser.close", json!({}), None));
                            let _ = ws.close(None);
                        }
                        Err(e) => crate::debug!("Browser.close: {}", e),
                    }
                }
            }
        }
        if !self.wait_cdp_gone(15.0) {
            if let Some(child) = self.process.lock().unwrap().as_mut() {
                let _ = child.kill();
            }
            self.kill_profile_processes();
            if !self.wait_cdp_gone(10.0) {
                return Err(Error::Browser(format!("Could not close the browser on CDP port {}.", self.port)));
            }
        }
        *self.process.lock().unwrap() = None;
        self.launched_by_us.store(false, Ordering::SeqCst);
        *self.tab_id.lock().unwrap() = None;
        crate::info!("Browser closed.");
        Ok(())
    }

    fn wait_cdp_gone(&self, timeout: f64) -> bool {
        let deadline = Instant::now() + Duration::from_secs_f64(timeout);
        while Instant::now() < deadline {
            if !self.is_cdp_ready() {
                return true;
            }
            thread::sleep(Duration::from_millis(500));
        }
        !self.is_cdp_ready()
    }

    /// Force-kills every process whose command line names our whole profile folder.
    fn kill_profile_processes(&self) {
        if !cfg!(windows) {
            return;
        }
        let folder = self.user_data_dir.display().to_string().replace('\'', "''");
        let script = format!(
            "$d = '--user-data-dir=\"?' + [regex]::Escape('{}') + '(\"|\\s+\"?--|\\s+http|\\s*$)'; \
             Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -and $_.CommandLine -match $d }} | \
             ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}",
            folder
        );
        let _ = hidden(Command::new("powershell").args(["-NoProfile", "-NonInteractive", "-Command", &script]))
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }

    // -------------------------------------------------------------- tab / link

    pub fn find_game_tab(&self) -> Option<Json> {
        let pages: Vec<Json> = cdp::targets(&self.host, self.port).into_iter().filter(|t| t["type"] == "page").collect();
        if let Some(t) = pages.iter().find(|t| t["url"].as_str().unwrap_or("").to_lowercase().contains(&self.url_filter)) {
            return Some(t.clone());
        }
        pages.into_iter().find(|t| t["title"].as_str().unwrap_or("").to_lowercase().contains("total battle"))
    }

    /// Ensures a tab with the game exists, creating one if needed.
    pub fn open_game_tab(&self) -> Option<Json> {
        if let Some(tab) = self.find_game_tab() {
            return Some(tab);
        }
        let path = format!("/json/new?{}", self.game_url);
        // Newer Chrome only accepts PUT here; older ones GET.
        if !matches!(cdp::http_put(&self.host, self.port, &path, Duration::from_secs(5)), Ok((200, _))) {
            let _ = cdp::http_get(&self.host, self.port, &path, Duration::from_secs(5));
        }
        let deadline = Instant::now() + Duration::from_secs(15);
        while Instant::now() < deadline {
            if let Some(tab) = self.find_game_tab() {
                return Some(tab);
            }
            thread::sleep(Duration::from_millis(500));
        }
        None
    }

    pub fn connect(&self) -> Res<bool> {
        let tab = self.open_game_tab().or_else(|| {
            // No game tab yet: attach to any page so we can at least navigate.
            cdp::targets(&self.host, self.port).into_iter().find(|t| t["type"] == "page")
        });
        let Some(tab) = tab else { return Ok(false) };
        let Some(url) = tab["webSocketDebuggerUrl"].as_str() else { return Ok(false) };
        self.disconnect();
        let ws = match cdp::connect(url, Duration::from_secs(30)) {
            Ok(ws) => ws,
            Err(e) => {
                crate::error!("CDP websocket failed: {}", e);
                return Ok(false);
            }
        };
        self.conn.lock().unwrap().ws = Some(ws);
        *self.tab_id.lock().unwrap() = tab["id"].as_str().map(String::from);
        self.send("Page.enable", json!({}), 30.0)?;
        self.send("Runtime.enable", json!({}), 30.0)?;
        self.send("DOM.enable", json!({}), 30.0)?;
        crate::info!("Connected to tab: {}", crate::text::head(tab["url"].as_str().unwrap_or(""), 90));
        Ok(true)
    }

    pub fn disconnect(&self) {
        if let Some(mut ws) = self.conn.lock().unwrap().ws.take() {
            let _ = ws.close(None);
            let _ = ws.flush();
        }
    }

    /// Brings the game tab to the front - needed only for the user to watch.
    pub fn focus_tab(&self) -> bool {
        let Some(id) = self.tab_id() else { return false };
        matches!(cdp::http_get(&self.host, self.port, &format!("/json/activate/{}", id), Duration::from_secs(2)), Ok((200, _)))
    }

    /// Closes the browser, but only if this run was the one that opened it.
    pub fn close(&self) {
        self.disconnect();
        if !self.launched_by_us.load(Ordering::SeqCst) {
            crate::info!("Browser was already open before this run; leaving it as it is.");
            return;
        }
        let _ = cdp::http_get(&self.host, self.port, &format!("/json/close/{}", self.tab_id().unwrap_or_default()), Duration::from_secs(2));
        if let Some(mut child) = self.process.lock().unwrap().take() {
            if child.try_wait().ok().flatten().is_none() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
        self.launched_by_us.store(false, Ordering::SeqCst);
        crate::info!("Browser closed.");
    }

    // --------------------------------------------------------------- protocol

    /// Sends one command and returns its result. One conversation at a time on the
    /// socket; messages that are not the answer are discarded.
    pub fn send(&self, method: &str, params: Json, timeout: f64) -> Res<Json> {
        if !self.is_connected() && !self.connect()? {
            return Err(Error::Browser("No active CDP connection.".into()));
        }
        let mut conn = self.conn.lock().unwrap();
        conn.msg_id += 1;
        let id = conn.msg_id;
        let Some(ws) = conn.ws.as_mut() else { return Err(Error::Browser("No active CDP connection.".into())) };
        if let Err(e) = cdp::send(ws, cdp::command(id, method, params, None)) {
            conn.ws = None;
            return Err(Error::Browser(format!("Failed to send {}: {}", method, e)));
        }
        let deadline = Instant::now() + Duration::from_secs_f64(timeout.max(0.1));
        loop {
            let now = Instant::now();
            if now >= deadline {
                break;
            }
            let ws = conn.ws.as_mut().unwrap();
            match cdp::recv(ws, (deadline - now).min(Duration::from_millis(500))) {
                Ok(Some(text)) => {
                    if !text.contains(&format!("\"id\":{}", id)) {
                        continue;
                    }
                    let Ok(message) = serde_json::from_str::<Json>(&text) else { continue };
                    if message["id"].as_u64() != Some(id) {
                        continue;
                    }
                    if let Some(err) = message.get("error") {
                        let m = err.get("message").and_then(|m| m.as_str()).map(String::from).unwrap_or_else(|| err.to_string());
                        return Err(Error::Browser(format!("{}: {}", method, m)));
                    }
                    return Ok(message.get("result").cloned().unwrap_or(json!({})));
                }
                Ok(None) => {}
                Err(e) => {
                    // A half-read socket cannot be trusted for the next command.
                    conn.ws = None;
                    return Err(Error::Browser(format!("CDP connection lost during {}: {}", method, e)));
                }
            }
        }
        conn.ws = None;
        Err(Error::Browser(format!("Timed out waiting for the answer to {}.", method)))
    }

    /// Runs JavaScript in the page and returns its value (None on error).
    pub fn evaluate(&self, expression: &str) -> Option<Json> {
        let result = match self.send("Runtime.evaluate",
                                     json!({"expression": expression, "returnByValue": true, "awaitPromise": true}), 30.0) {
            Ok(r) => r,
            Err(e) => {
                crate::debug!("evaluate failed: {}", e);
                return None;
            }
        };
        if let Some(details) = result.get("exceptionDetails") {
            crate::debug!("JS exception: {}", details.get("text").and_then(|t| t.as_str()).unwrap_or(""));
            return None;
        }
        result.get("result").and_then(|r| r.get("value")).cloned()
    }

    // ------------------------------------------------------------- navigation

    pub fn navigate(&self, url: Option<&str>) -> Res<()> {
        let target = url.unwrap_or(&self.game_url).to_string();
        crate::info!("Navigating to {}", target);
        self.send("Page.navigate", json!({"url": target}), 30.0)?;
        self.wait_for_load(None);
        Ok(())
    }

    /// Waits for document.readyState to reach 'complete'.
    pub fn wait_for_load(&self, timeout: Option<f64>) -> bool {
        let limit = Instant::now() + Duration::from_secs_f64(timeout.unwrap_or(self.page_load_timeout).max(0.0));
        while Instant::now() < limit {
            if self.evaluate("document.readyState") == Some(json!("complete")) {
                return true;
            }
            thread::sleep(Duration::from_millis(500));
        }
        crate::warn!("Page did not finish loading within the timeout.");
        false
    }

    /// Polls a JavaScript expression until it is truthy.
    pub fn wait_for(&self, condition: &str, timeout: f64) -> bool {
        let limit = Instant::now() + Duration::from_secs_f64(timeout);
        while Instant::now() < limit {
            if self.evaluate(&format!("!!({})", condition)) == Some(json!(true)) {
                return true;
            }
            thread::sleep(Duration::from_millis(500));
        }
        false
    }

    // ------------------------------------------------------------------ input

    fn mouse(&self, kind: &str, x: f64, y: f64, button: &str, buttons: i64, count: Option<i64>) -> Res<()> {
        let mut params = json!({"type": kind, "x": x, "y": y, "button": button, "buttons": buttons});
        if let Some(c) = count {
            params["clickCount"] = json!(c);
        }
        self.send("Input.dispatchMouseEvent", params, 30.0).map(|_| ())
    }

    /// A click the page cannot tell from a real one: the pointer moves there first.
    pub fn click(&self, x: f64, y: f64, delay: f64) -> Res<()> {
        self.mouse("mouseMoved", x, y, "none", 0, None)?;
        thread::sleep(Duration::from_millis(30));
        self.mouse("mousePressed", x, y, "left", 1, Some(1))?;
        thread::sleep(Duration::from_millis(40));
        self.mouse("mouseReleased", x, y, "left", 0, Some(1))?;
        if delay > 0.0 {
            thread::sleep(Duration::from_secs_f64(delay));
        }
        Ok(())
    }

    pub fn press_key(&self, name: &str, delay: f64) -> Res<()> {
        let (code, key) = match name {
            "Enter" => (13, "Enter"),
            "Tab" => (9, "Tab"),
            "Escape" => (27, "Escape"),
            "Backspace" => (8, "Backspace"),
            other => (0, other),
        };
        for kind in ["keyDown", "keyUp"] {
            self.send("Input.dispatchKeyEvent",
                      json!({"type": kind, "key": key, "code": key, "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code}),
                      30.0)?;
        }
        if delay > 0.0 {
            thread::sleep(Duration::from_secs_f64(delay));
        }
        Ok(())
    }

    /// Types text as a unit - immune to keyboard layout and safe for accents.
    pub fn insert_text(&self, text: &str) -> Res<()> {
        self.send("Input.insertText", json!({"text": text}), 30.0).map(|_| ())
    }

    pub fn select_all(&self) -> Res<()> {
        for kind in ["keyDown", "keyUp"] {
            self.send("Input.dispatchKeyEvent",
                      json!({"type": kind, "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65,
                             "modifiers": 2}),
                      30.0)?;
        }
        thread::sleep(Duration::from_millis(50));
        Ok(())
    }

    /// Bounding box of the first visible match, in viewport CSS pixels.
    pub fn element_rect(&self, selector: &str) -> Option<(f64, f64, f64, f64)> {
        let script = format!(
            "(() => {{ const el = document.querySelector({}); if (!el) return null; const r = el.getBoundingClientRect(); \
             if (!r.width || !r.height) return null; return {{x: r.x, y: r.y, width: r.width, height: r.height}}; }})()",
            Json::String(selector.to_string())
        );
        let r = self.evaluate(&script)?;
        Some((r["x"].as_f64()?, r["y"].as_f64()?, r["width"].as_f64()?, r["height"].as_f64()?))
    }

    /// Clicks the centre of an element with a real input event, not element.click().
    pub fn click_element(&self, selector: &str, delay: f64) -> Res<bool> {
        let Some((x, y, w, h)) = self.element_rect(selector) else { return Ok(false) };
        self.click(x + w / 2.0, y + h / 2.0, delay)?;
        Ok(true)
    }

    /// Writes what the tab shows to `path`, as the PNG Chrome renders. For a person looking at the log.
    pub fn save_screenshot(&self, path: &Path) -> bool {
        let data = match self.send("Page.captureScreenshot", json!({"format": "png"}), 30.0) {
            Ok(r) => r["data"].as_str().map(String::from),
            Err(e) => {
                crate::warn!("Screenshot failed: {}", e);
                return false;
            }
        };
        let Some(data) = data else { return false };
        match B64.decode(data) {
            Ok(bytes) => std::fs::write(path, bytes).is_ok(),
            Err(_) => false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_folder_is_read_however_chrome_was_quoted() {
        assert_eq!(r"C:\Users\me\.tb\Main", command_line_folder(r#"chrome.exe --remote-debugging-port=9322 "--user-data-dir=C:\Users\me\.tb\Main" --no-first-run"#)
                   .trim_end_matches('"'), "a quote before the flag");
        assert_eq!(r"C:\Users\me\.tb\Main Account", command_line_folder(r#"chrome.exe --user-data-dir="C:\Users\me\.tb\Main Account" --x"#));
        assert_eq!(r"C:\Users\me\.tb\Main", command_line_folder(r"chrome.exe --user-data-dir=C:\Users\me\.tb\Main --no-first-run"));
        assert_eq!(r"C:\p\Main", command_line_folder(r"chrome.exe --user-data-dir=C:\p\Main https://totalbattle.com/en/"));
        assert_eq!(r"C:\p\Main", command_line_folder(r"chrome.exe --user-data-dir=C:\p\Main"));
        assert_eq!("", command_line_folder("chrome.exe --no-first-run"));
    }

    #[test]
    fn a_port_is_matched_whole() {
        assert!(on_port("chrome --remote-debugging-port=9322 --x", 9322));
        assert!(on_port("chrome --remote-debugging-port=9322", 9322));
        assert!(!on_port("chrome --remote-debugging-port=93221", 9322));
        assert!(!on_port("chrome --remote-debugging-port=9323", 9322));
    }
}
