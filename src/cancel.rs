//! Stopping a run with the Escape key, or the Stop button.
//!
//! Every long wait goes through `sleep()` instead of `thread::sleep`, so a
//! cancellation is noticed immediately rather than after the twenty seconds a
//! profile switch takes.
//!
//! The key is read with `GetAsyncKeyState`, which reports the physical keyboard
//! regardless of which window has focus - the game will have it, not us. The
//! Escape presses the collector itself sends to close dialogs go through CDP and
//! never touch the physical key state, so they cannot cancel the run.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

use crate::error::{Error, Res};

const HOLD: Duration = Duration::from_millis(350);

pub struct CancelToken {
    state: Mutex<(bool, String)>,
    wake: Condvar,
}

impl CancelToken {
    pub const fn new() -> Self {
        CancelToken { state: Mutex::new((false, String::new())), wake: Condvar::new() }
    }

    pub fn cancel(&self, reason: &str) {
        let mut st = self.state.lock().unwrap();
        *st = (true, reason.to_string());
        self.wake.notify_all();
    }

    pub fn reset(&self) {
        *self.state.lock().unwrap() = (false, String::new());
    }

    pub fn requested(&self) -> bool {
        self.state.lock().unwrap().0
    }

    fn cancelled(reason: &str) -> Error {
        Error::Cancelled(if reason.is_empty() { "cancelled by user".into() } else { reason.to_string() })
    }

    /// Fails if a cancellation is pending. Call between steps, not inside them.
    pub fn check(&self) -> Res<()> {
        let st = self.state.lock().unwrap();
        if st.0 {
            return Err(Self::cancelled(&st.1));
        }
        Ok(())
    }

    /// A wait that ends early when cancelled - and then fails.
    pub fn sleep(&self, seconds: f64) -> Res<()> {
        if seconds > 0.0 && seconds.is_finite() {
            let deadline = Instant::now() + Duration::from_secs_f64(seconds);
            let mut st = self.state.lock().unwrap();
            while !st.0 {
                let now = Instant::now();
                if now >= deadline {
                    break;
                }
                st = self.wake.wait_timeout(st, deadline - now).unwrap().0;
            }
        }
        self.check()
    }
}

impl Default for CancelToken {
    fn default() -> Self {
        Self::new()
    }
}

/// One run per process, so a single shared token spares every module a parameter.
pub static CANCELLATION: CancelToken = CancelToken::new();

pub fn check() -> Res<()> {
    CANCELLATION.check()
}

pub fn sleep(seconds: f64) -> Res<()> {
    CANCELLATION.sleep(seconds)
}

#[cfg(windows)]
fn escape_pressed() -> bool {
    use windows_sys::Win32::UI::Input::KeyboardAndMouse::{GetAsyncKeyState, VK_ESCAPE};
    unsafe { (GetAsyncKeyState(VK_ESCAPE as i32) as u16 & 0x8000) != 0 }
}

#[cfg(not(windows))]
fn escape_pressed() -> bool {
    false
}

/// Polls the physical Escape key and cancels the run when it is held.
pub struct EscapeWatcher {
    stop: Mutex<Option<Arc<AtomicBool>>>,
}

impl EscapeWatcher {
    pub fn start(&self) {
        if !cfg!(windows) {
            return;
        }
        let mut slot = self.stop.lock().unwrap();
        if slot.is_some() {
            return;
        }
        let stop = Arc::new(AtomicBool::new(false));
        *slot = Some(stop.clone());
        thread::spawn(move || {
            while !stop.load(Ordering::Relaxed) {
                if escape_pressed() {
                    let held = Instant::now();
                    while escape_pressed() && !stop.load(Ordering::Relaxed) {
                        if held.elapsed() >= HOLD {
                            CANCELLATION.cancel("ESC pressed");
                            return;
                        }
                        thread::sleep(Duration::from_millis(50));
                    }
                }
                thread::sleep(Duration::from_millis(100));
            }
        });
    }

    pub fn stop(&self) {
        if let Some(stop) = self.stop.lock().unwrap().take() {
            stop.store(true, Ordering::Relaxed);
        }
    }
}

pub fn escape_watcher() -> &'static EscapeWatcher {
    static WATCHER: OnceLock<EscapeWatcher> = OnceLock::new();
    WATCHER.get_or_init(|| EscapeWatcher { stop: Mutex::new(None) })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_cancelled_sleep_ends_early_and_says_so() {
        let token = Arc::new(CancelToken::new());
        let other = token.clone();
        thread::spawn(move || {
            thread::sleep(Duration::from_millis(50));
            other.cancel("stop requested");
        });
        let started = Instant::now();
        let result = token.sleep(5.0);
        assert!(started.elapsed() < Duration::from_secs(2));
        assert_eq!(Err(Error::Cancelled("stop requested".into())), result);
        token.reset();
        assert!(token.check().is_ok());
    }
}
