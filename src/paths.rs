//! Where things live: everything sits next to the executable, as it sat next to
//! main.py, so an installation is one folder that can be moved or backed up whole.
//!
//! `TBC_HOME` overrides the folder (for development: `cargo run` keeps the
//! executable in target/).

use std::path::{Component, Path, PathBuf};

pub fn base_dir() -> PathBuf {
    if let Ok(home) = std::env::var("TBC_HOME") {
        if !home.trim().is_empty() {
            return PathBuf::from(home);
        }
    }
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(Path::to_path_buf))
        .unwrap_or_else(|| PathBuf::from("."))
}

pub fn config_dir() -> PathBuf {
    base_dir().join("config")
}

pub fn config_file() -> PathBuf {
    config_dir().join("config.json")
}

/// When each module last ran for each profile (see schedule.rs).
pub fn module_state_file() -> PathBuf {
    config_dir().join("module_state.json")
}

/// What a module keeps between runs, one folder per module.
pub fn module_data_dir() -> PathBuf {
    config_dir().join("module_data")
}

/// Held by the run in progress (run_lock.rs).
pub fn run_lock_file() -> PathBuf {
    config_dir().join("run.lock")
}

pub fn legacy_config_file() -> PathBuf {
    base_dir().join("position.cfg")
}

pub fn home_dir() -> PathBuf {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

pub fn default_profiles_dir() -> PathBuf {
    home_dir().join(".total_battle_counter_profiles")
}

/// `%VAR%`, `$VAR` and `${VAR}` replaced by the environment, as os.path.expandvars does.
pub fn expand_vars(text: &str) -> String {
    let mut out = String::new();
    let chars: Vec<char> = text.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        if c == '%' {
            if let Some(end) = chars[i + 1..].iter().position(|&x| x == '%') {
                let name: String = chars[i + 1..i + 1 + end].iter().collect();
                if !name.is_empty() {
                    if let Ok(v) = std::env::var(&name) {
                        out.push_str(&v);
                        i += end + 2;
                        continue;
                    }
                }
            }
        } else if c == '$' {
            if chars.get(i + 1) == Some(&'{') {
                if let Some(end) = chars[i + 2..].iter().position(|&x| x == '}') {
                    let name: String = chars[i + 2..i + 2 + end].iter().collect();
                    if let Ok(v) = std::env::var(&name) {
                        out.push_str(&v);
                        i += end + 3;
                        continue;
                    }
                }
            } else {
                let name: String = chars[i + 1..].iter().take_while(|c| c.is_alphanumeric() || **c == '_').collect();
                if !name.is_empty() {
                    if let Ok(v) = std::env::var(&name) {
                        out.push_str(&v);
                        i += name.chars().count() + 1;
                        continue;
                    }
                }
            }
        }
        out.push(c);
        i += 1;
    }
    out
}

/// `~` at the start becomes the user's folder.
pub fn expand_user(text: &str) -> String {
    if text == "~" {
        return home_dir().to_string_lossy().into_owned();
    }
    if let Some(rest) = text.strip_prefix("~/").or_else(|| text.strip_prefix("~\\")) {
        return home_dir().join(rest).to_string_lossy().into_owned();
    }
    text.to_string()
}

pub fn is_absolute(text: &str) -> bool {
    let p = Path::new(text);
    p.is_absolute() || (cfg!(windows) && (text.starts_with('\\') || text.starts_with('/')) && !text.is_empty())
        || (!cfg!(windows) && p.has_root())
}

/// abspath + normpath: the folder as one canonical text, without touching the disk.
pub fn abspath(path: &Path) -> PathBuf {
    let joined = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir().unwrap_or_default().join(path)
    };
    let mut out = PathBuf::new();
    for comp in joined.components() {
        match comp {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            other => out.push(other.as_os_str()),
        }
    }
    out
}

/// Whether two paths name the same folder, however they were typed.
pub fn same_folder(first: &Path, second: &Path) -> bool {
    norm_case(&abspath(first)) == norm_case(&abspath(second))
}

fn norm_case(p: &Path) -> String {
    let text = p.to_string_lossy().into_owned();
    if cfg!(windows) {
        text.replace('/', "\\").to_lowercase().trim_end_matches('\\').to_string()
    } else {
        text.trim_end_matches('/').to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_folder_typed_two_ways_is_one_folder() {
        assert!(same_folder(Path::new("C:/Profiles/A"), Path::new("c:\\profiles\\b\\..\\a\\")));
        assert!(!same_folder(Path::new("C:/Profiles/Account"), Path::new("C:/Profiles/Account2")));
    }

    #[test]
    fn variables_are_expanded() {
        std::env::set_var("TBC_TEST_VAR", "X");
        assert_eq!("a/X/b", expand_vars("a/%TBC_TEST_VAR%/b"));
        assert_eq!("X-X", expand_vars("$TBC_TEST_VAR-${TBC_TEST_VAR}"));
        assert_eq!("%NOPE_TBC%", expand_vars("%NOPE_TBC%"));
    }
}
