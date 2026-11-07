//! What the events module remembers between runs, under config/module_data/events/:
//! the players' names (one file for every profile), what was already sent and when
//! the Journal was last searched to the end (one file per clan site).

use std::path::{Path, PathBuf};

use chrono::{NaiveDate, NaiveDateTime};
use serde_json::{json, Map, Value as Json};

use super::api::normalize_site_url;
use crate::config::{read_json, write_json_atomic};
use crate::schedule::{iso_seconds, parse_iso};

pub fn profile_cache_path(data_dir: &Path) -> PathBuf {
    data_dir.join("player_profiles.json")
}

/// A file name for a site: 'https://kokmain.counter.li/x' -> 'kokmain.counter.li_x'.
pub fn site_slug(site_url: &str) -> String {
    let normalized = normalize_site_url(site_url);
    let bare = normalized.split_once("://").map(|(_, b)| b).unwrap_or(&normalized).to_lowercase();
    let re = regex::Regex::new(r"[^a-z0-9.-]+").unwrap();
    let slug = re.replace_all(&bare, "_").trim_matches('_').to_string();
    if slug.is_empty() {
        "site".into()
    } else {
        slug
    }
}

/// The tournaments already sent to one site: result uid -> what the site answered.
pub struct SentHistory {
    pub path: PathBuf,
    pub sent: Map<String, Json>,
}

impl SentHistory {
    pub fn new(site_url: &str, data_dir: &Path) -> Self {
        let path = data_dir.join(format!("sent_{}.json", site_slug(site_url)));
        let sent = read_json(&path)
            .and_then(|d| d.as_object().cloned())
            .map(|m| m.into_iter().filter(|(_, v)| v.is_object()).collect())
            .unwrap_or_default();
        SentHistory { path, sent }
    }

    pub fn info(&self, result_uid: &str) -> Option<Json> {
        self.sent.get(result_uid).cloned()
    }

    pub fn mark(&mut self, result_uid: &str, info: Json) {
        self.sent.insert(result_uid.to_string(), info);
        self.save();
    }

    pub fn save(&self) {
        if let Err(e) = write_json_atomic(&self.path, &Json::Object(self.sent.clone()), 1) {
            crate::warn!("Could not write {}: {}", self.path.display(), e);
        }
    }
}

/// When one site's Journal was last searched to the end, and how many days back.
pub struct SearchMemory {
    pub path: PathBuf,
    pub data: Map<String, Json>,
}

/// Days walked again before the last search: a card's "yesterday" turns at the game's midnight.
pub const OVERLAP_DAYS: i64 = 1;

impl SearchMemory {
    pub fn new(site_url: &str, data_dir: &Path) -> Self {
        let path = data_dir.join(format!("search_{}.json", site_slug(site_url)));
        let data = read_json(&path).and_then(|d| d.as_object().cloned()).unwrap_or_default();
        SearchMemory { path, data }
    }

    pub fn last_search(&self) -> Option<NaiveDateTime> {
        self.data.get("searched_at").and_then(|v| v.as_str()).and_then(parse_iso)
    }

    /// (how many days back to walk now, whether the earlier searches are set aside).
    pub fn days_to_walk(&self, days_back: i64, today: NaiveDate) -> (i64, bool) {
        let Some(last) = self.last_search() else { return (days_back, false) };
        let covered = match self.data.get("days_back") {
            Some(Json::Number(n)) => n.as_f64().map(|f| f as i64).unwrap_or(days_back),
            Some(Json::String(s)) => s.trim().parse().unwrap_or(days_back),
            _ => days_back,
        };
        if days_back > covered {
            return (days_back, true);
        }
        let since = (today - last.date()).num_days().max(0);
        ((since + OVERLAP_DAYS).min(days_back), false)
    }

    /// Notes a complete search.
    pub fn record(&mut self, when: NaiveDateTime, days_back: i64) {
        self.data = json!({"searched_at": iso_seconds(when), "days_back": days_back}).as_object().unwrap().clone();
        if let Err(e) = write_json_atomic(&self.path, &Json::Object(self.data.clone()), 1) {
            crate::warn!("Could not write {}: {}", self.path.display(), e);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn day(text: &str) -> NaiveDate {
        NaiveDate::parse_from_str(text, "%Y-%m-%d").unwrap()
    }

    #[test]
    fn the_first_search_walks_every_day_configured() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!((7, false), SearchMemory::new("https://a", dir.path()).days_to_walk(7, day("2026-11-02")));
    }

    #[test]
    fn later_ones_walk_the_days_since_the_last_complete_search() {
        let dir = tempfile::tempdir().unwrap();
        let mut m = SearchMemory::new("https://a", dir.path());
        m.record(parse_iso("2026-11-02T15:00:00").unwrap(), 7);
        let m = SearchMemory::new("https://a", dir.path());
        assert_eq!((1, false), m.days_to_walk(7, day("2026-11-02")));
        assert_eq!((2, false), m.days_to_walk(7, day("2026-11-03")));
        assert_eq!((7, false), m.days_to_walk(7, day("2026-12-30")));
    }

    #[test]
    fn raising_the_days_walks_all_of_them_once() {
        let dir = tempfile::tempdir().unwrap();
        let mut m = SearchMemory::new("https://a", dir.path());
        m.record(parse_iso("2026-11-02T15:00:00").unwrap(), 7);
        assert_eq!((30, true), m.days_to_walk(30, day("2026-11-03")));
        assert_eq!((2, false), m.days_to_walk(5, day("2026-11-03")));
    }

    #[test]
    fn each_site_has_its_own() {
        let dir = tempfile::tempdir().unwrap();
        let mut a = SearchMemory::new("https://a.example.com", dir.path());
        a.record(parse_iso("2026-11-02T15:00:00").unwrap(), 7);
        assert!(SearchMemory::new("https://b.example.com", dir.path()).last_search().is_none());
        let mut sent = SentHistory::new("https://a.example.com", dir.path());
        sent.mark("uid-1", json!({"published": true}));
        assert!(SentHistory::new("https://a.example.com", dir.path()).info("uid-1").is_some());
        assert!(SentHistory::new("https://b.example.com", dir.path()).info("uid-1").is_none());
    }

    #[test]
    fn a_site_address_becomes_a_file_name() {
        assert_eq!("kokmain.counter.li_x", site_slug("https://kokmain.counter.li/x"));
        assert_eq!("clan.example.com", site_slug("clan.example.com/api/v1"));
        assert_eq!("site", site_slug(""));
    }
}
