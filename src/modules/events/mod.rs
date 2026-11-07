//! Events module - the clan's tournament rankings, from the Journal to the clan site.
//!
//! For each profile it asks the game for the Journal over the network and sends the
//! rankings the site does not have yet (see autosend.rs). A profile belongs to one
//! clan and each clan has its own site, so the site's address and API token are
//! kept with the profile. It runs once a day by default, the game's day turning at
//! 14:00; a search walks back `days_back` days at most, and after a complete one only
//! the days since it.

pub mod api;
pub mod autosend;
pub mod catalog;
pub mod items;
pub mod journal;
pub mod live;
pub mod mapper;
pub mod store;

use std::path::PathBuf;

use serde_json::{json, Map, Value as Json};

use crate::config::{Field, FieldType, ProfileConfig};
use crate::context::Window;
use crate::error::Res;
use crate::link::JournalSource;
use crate::modules::{opt_f64, setting_text, Module, ModuleResult};
use api::{normalize_site_url, Site, SiteClient};
use store::{profile_cache_path, SearchMemory, SentHistory};

pub const KEY: &str = "events";
pub const TITLE: &str = "Tournament rankings";
pub const DESCRIPTION: &str = "Reads the clan's tournament results in the Journal (the clanmates' rankings, and the clan's \
                               place and prizes among the clans) and sends them to the clan site, which decides what is \
                               published without review. \
                               Each profile uses its own site address and API token (Accounts and profiles > Modules); a \
                               profile without them is skipped.";
pub const DAY_START: &str = "14:00";
/// The version of the uploader this module carries, as the site records it.
pub const CLIENT_VERSION: &str = "1.3.0";

pub fn settings() -> Vec<Field> {
    vec![
        Field::new("days_back", FieldType::Int, "Days to look back", json!(45),
                   "How many days of the Journal a search walks. After a complete search only the days since it are \
                    walked again.")
            .range(1.0, 365.0),
        Field::new("max_pages", FieldType::Int, "Journal requests limit", json!(200),
                   "A safety limit, of 50 entries per request: a search stops by itself when it reaches tournaments an \
                    earlier search has already covered.")
            .range(1.0, 1000.0),
    ]
}

pub fn profile_fields() -> Vec<Field> {
    vec![
        Field::new("site_url", FieldType::Str, "Clan site address", json!(""),
                   "The chestcounter site of this profile's clan, e.g. https://myclan.example.com"),
        Field::new("api_token", FieldType::Password, "Site API token", json!(""),
                   "The uploader token created on that site. Each clan site has its own."),
    ]
}

pub fn site_of_values(values: &Map<String, Json>) -> (String, String) {
    (normalize_site_url(&setting_text(values, "site_url")), setting_text(values, "api_token").trim().to_string())
}

pub fn site_of(profile: &ProfileConfig) -> (String, String) {
    site_of_values(&profile.module_settings(KEY))
}

pub fn configured_for(profile: &ProfileConfig) -> bool {
    let (s, t) = site_of(profile);
    !s.is_empty() && !t.is_empty()
}

pub fn profile_summary(profile: &ProfileConfig) -> String {
    let (site, token) = site_of(profile);
    match (site.is_empty(), token.is_empty()) {
        (false, false) => format!("events: {}", site.split_once("://").map(|(_, b)| b).unwrap_or(&site)),
        (false, true) => "events: token missing".into(),
        (true, false) => "events: site address missing".into(),
        (true, true) => String::new(),
    }
}

pub fn test_profile(values: &Map<String, Json>) -> (bool, String) {
    let (site, token) = site_of_values(values);
    if site.is_empty() {
        return (false, "Fill in the clan site address.".into());
    }
    match SiteClient::new(&site, &token, CLIENT_VERSION).me() {
        Ok(me) => {
            let name = me.get("user").and_then(|u| u.get("name")).and_then(|n| n.as_str()).filter(|n| !n.is_empty()).unwrap_or("?");
            (true, format!("Connected to {} as {}.", site, name))
        }
        Err(e) => (false, e.describe()),
    }
}

pub struct EventsModule {
    pub options: Map<String, Json>,
    pub data_dir: PathBuf,
}

fn failure(profile: &ProfileConfig, reason: &str, retry: bool) -> ModuleResult {
    crate::error!("[{}] Tournament search not done: {}", profile.label(), reason);
    ModuleResult::failed(profile, reason, retry)
}

impl EventsModule {
    /// The search, with the game and the site handed in.
    pub fn search(&self, profile: &ProfileConfig, feed: &mut dyn JournalSource, client: &dyn Site,
                  known_tournaments: Result<Vec<Json>, api::ApiError>, lookup: catalog::LookupFn,
                  items: autosend::ItemLookup) -> Res<ModuleResult> {
        let (site, _) = site_of(profile);
        let data_dir = self.data_dir.join("events");
        let mut collector = journal::JournalCollector::new(Some(journal::ProfileCache::new(&profile_cache_path(&data_dir))));
        let mut known: Map<String, Json> = Map::new();
        match known_tournaments {
            Ok(list) => {
                for k in list {
                    let gtype = match k.get("game_type") {
                        Some(Json::Number(n)) => n.as_i64().map(|v| v as i128),
                        Some(Json::String(s)) => s.trim().parse().ok(),
                        _ => None,
                    };
                    if let Some(g) = gtype {
                        known.insert(catalog::ranking_key(g, k.get("ranking").and_then(|r| r.as_str())), k);
                    }
                }
            }
            Err(e) => crate::warn!("The site's catalogue is unavailable: {}", e.message),
        }
        let mut catalogue_name = catalog::make_catalogue_name(client, &mut known, lookup);
        let mut history = SentHistory::new(&site, &data_dir);
        let mut memory = SearchMemory::new(&site, &data_dir);
        let started = chrono::Local::now().naive_local();
        let days_back = (opt_f64(&self.options, "days_back", 7.0) as i64).max(1);
        let (days, afresh) = memory.days_to_walk(days_back, started.date());
        if afresh {
            crate::info!("The days to look back were raised to {}: walking all of them once.", days_back);
        } else if let Some(last) = memory.last_search() {
            crate::info!("Last complete search on {}: walking back {} day(s) of the {} configured.",
                         last.format("%d/%m/%Y %H:%M"), days, days_back);
        }
        let options = mapper::MapperOptions { max_pages: (opt_f64(&self.options, "max_pages", 200.0) as usize).max(1),
                                              max_age_days: Some(days), ..Default::default() };
        let clan_id = match feed.clan_id() {
            Ok(c) => c,
            Err(e) if e.is_cancelled() => return Err(e),
            Err(e) => {
                crate::warn!("[{}] Could not learn the profile's clan: {}", profile.label(), e);
                None
            }
        };
        let mut log = |t: &str| crate::logger::info(t);
        let result = {
            let mut search = autosend::AutoSearch {
                feed,
                collector: &mut collector,
                client,
                options,
                history: &mut history,
                catalogue_name: &mut catalogue_name,
                client_version: CLIENT_VERSION.into(),
                clan_id,
                items,
                on_log: &mut log,
                clock: Box::new(|| chrono::Utc::now().timestamp() as i128),
                use_site_cutoff: !afresh,
            };
            search.run()
        };
        collector.save_profiles();
        let summary = result?;
        if summary.complete {
            memory.record(started, days_back);
        }
        crate::info!("[{}] Tournament search: {}", profile.label(), summary.text());
        let mut r = ModuleResult::ok(profile)
            .with("tournaments", json!(summary.opened()))
            .with("sent", json!(summary.outcomes.len() - summary.failed()))
            .with("published", json!(summary.published()))
            .with("for_review", json!(summary.for_review()))
            .with("skipped", json!(summary.skipped));
        r.success = summary.complete;
        // An incomplete search is not repeated at once: the next run of the day goes back to the same point.
        r.retry = false;
        if !summary.complete {
            r.reason = summary.text();
        }
        Ok(r)
    }
}

impl Module for EventsModule {
    fn key(&self) -> &str {
        KEY
    }
    fn title(&self) -> &str {
        TITLE
    }
    fn options(&self) -> &Map<String, Json> {
        &self.options
    }
    fn default_day_start(&self) -> &str {
        DAY_START
    }
    fn configured_for(&self, profile: &ProfileConfig) -> bool {
        configured_for(profile)
    }

    fn preflight(&self, profiles: &[ProfileConfig]) -> Vec<String> {
        profiles
            .iter()
            .filter(|p| {
                let (s, t) = site_of(p);
                (!s.is_empty() || !t.is_empty()) && !configured_for(p)
            })
            .map(|p| format!("Profile '{}': the events module needs both the clan site address and its API token (Accounts \
                              and profiles > Modules). Fill in both, or clear both to skip it.", p.label()))
            .collect()
    }

    fn run_for_profile(&self, window: &dyn Window, profile: &ProfileConfig) -> Res<ModuleResult> {
        let (site, token) = site_of(profile);
        if site.is_empty() || token.is_empty() {
            return Ok(failure(profile, "no clan site address and token configured for this profile", false));
        }
        // Asked first: with a token the site refuses, the whole walk would end in rankings nobody accepts.
        let client = SiteClient::new(&site, &token, CLIENT_VERSION);
        if let Err(e) = client.me() {
            return Ok(failure(profile, &format!("the site {} refused the connection: {}", site, e.describe()), false));
        }
        crate::info!("[{}] Searching the Journal for tournaments to send to {}", profile.label(), site);
        let Some(mut feed) = window.journal() else {
            return Ok(failure(profile, "could not talk to the game", true));
        };
        let known = client.known_tournaments();
        let mut items = |i: &[i128], r: &[i128]| items::lookup(i, r);
        let result = self.search(profile, feed.as_mut(), &client, known, Box::new(catalog::lookup_tournament_name), &mut items);
        drop(feed);
        // Then the game's Tournaments window: what runs now and what is about to start.
        if let Some(api) = window.game_link() {
            let mut log = |t: &str| crate::info!("[{}] {}", profile.label(), t);
            live::refresh(api.as_ref(), &client, &self.data_dir.join("events"), &mut log)?;
        } else {
            crate::warn!("[{}] The game's Tournaments window was not read: could not talk to the game.", profile.label());
        }
        result
    }
}
