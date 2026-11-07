//! One run at a time on this machine.
//!
//! The scheduled run and a run started from the interface drive the same Chrome;
//! two at once reset the game under each other. The lock is an OS lock on a file,
//! not the file's existence: the system drops it when the process ends, however it
//! ends, so a crash never leaves a stale lock that would keep every later run out.

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

pub struct RunLock {
    pub path: PathBuf,
    handle: Mutex<Option<File>>,
}

impl RunLock {
    pub fn new(path: &Path) -> Self {
        RunLock { path: path.to_path_buf(), handle: Mutex::new(None) }
    }

    /// True when this process now holds the lock; false when another one does.
    pub fn acquire(&self) -> bool {
        let mut slot = self.handle.lock().unwrap();
        if slot.is_some() {
            return true;
        }
        if let Some(parent) = self.path.parent() {
            let _ = fs::create_dir_all(parent);
        }
        let Ok(mut file) = OpenOptions::new().read(true).write(true).create(true).truncate(false).open(&self.path)
        else {
            return false;
        };
        if file.try_lock().is_err() {
            return false;
        }
        // Who holds it, for whoever opens the file wondering.
        let _ = file.set_len(0);
        let _ = file.seek(SeekFrom::Start(0));
        let _ = write!(file, "{}", std::process::id());
        let _ = file.flush();
        *slot = Some(file);
        true
    }

    pub fn release(&self) {
        if let Some(file) = self.handle.lock().unwrap().take() {
            let _ = file.unlock();
        }
    }

    /// The process id written by whoever holds the lock, if it can be read.
    pub fn holder(&self) -> Option<String> {
        let mut text = String::new();
        File::open(&self.path).ok()?.read_to_string(&mut text).ok()?;
        let t = text.trim().to_string();
        (!t.is_empty()).then_some(t)
    }
}

impl Drop for RunLock {
    fn drop(&mut self) {
        self.release();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_one_holder_at_a_time() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("run.lock");
        let first = RunLock::new(&path);
        let second = RunLock::new(&path);
        assert!(first.acquire());
        assert!(first.acquire(), "holding it again is fine");
        assert!(!second.acquire());
        first.release();
        assert!(second.acquire());
    }
}
