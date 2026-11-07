//! The game's own catalogue of tournaments (Tables 44 and 71 of the client static data).
//!
//! In the Journal the game never sends a tournament's name: each entry carries only
//! `[game_type, variant]` and the kind of ranking packet. The client names them from
//! Table 44 (`global_tournament`: slug at `row[0]`, UI JSON at `row[10]` with
//! `info_title` and `journal_users_result_titles`), Table 71 (its components and
//! their trackers) and the `.mo` of en_US.
//!
//! Known types come from the site (`/api/v1/tournaments/known`); a nameless one is
//! looked for in the decoded catalogue cached on disk
//! (`config/module_data/events/tournament_types.json`), and only when it is missing
//! there too is the CDN asked whether a newer build exists.

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

use regex::Regex;
use serde_json::{json, Map, Value as Json};

use super::api::Site;
use super::journal::Tournament;
use crate::config::{read_json, write_json_atomic};
use crate::static_data::{self, literal_ints, text_at};
use crate::tbcodec::Value;

pub const TOURNAMENT_TABLE_INDEX: usize = 44;
pub const COMPONENT_TABLE_INDEX: usize = 71;
const MAX_NAME_LEN: usize = 120;
const FILES: [&str; 2] = ["no_duplicates", "strings"];
const SMALL_WORDS: [&str; 10] = ["of", "the", "in", "on", "at", "to", "for", "and", "or", "with"];

/// The canonical form of a ranking identifier, as `game_tournaments` keeps it.
pub fn normalize_ranking(ranking: Option<&str>) -> String {
    let cleaned = ranking.unwrap_or("").trim().to_lowercase();
    if cleaned.is_empty() || cleaned == "global_tournament_user_result" {
        String::new()
    } else {
        cleaned
    }
}

pub fn ranking_key(game_type: i128, ranking: Option<&str>) -> String {
    format!("{}|{}", game_type, normalize_ranking(ranking))
}

fn cut(s: &str) -> String {
    s.chars().take(MAX_NAME_LEN).collect()
}

/// The tournament or ranking name in a Journal clanmates entry title:
/// "Your Clanmates' results in the Great Hunt tournament." -> "Great Hunt tournament".
pub fn clean_clanmates_title(text: &str) -> String {
    if text.is_empty() {
        return String::new();
    }
    let collapsed = crate::text::collapse_spaces(text);
    let results = Regex::new(r"(?i)^your\s+clanmates'\s+(?:results?|scores?)\s+in\s+the\s+(.+?)\.?$").unwrap();
    let any = Regex::new(r"(?i)^your\s+clanmates'\s+(.+?)\.?$").unwrap();
    for re in [results, any] {
        if let Some(m) = re.captures(&collapsed) {
            return cut(m[1].trim_matches(|c| c == ' ' || c == '.'));
        }
    }
    String::new()
}

fn is_upper(s: &str) -> bool {
    s.chars().any(|c| c.is_alphabetic()) && !s.chars().any(|c| c.is_lowercase())
}

fn capitalize(w: &str) -> String {
    let mut c = w.chars();
    match c.next() {
        Some(f) => f.to_uppercase().collect::<String>() + &c.as_str().to_lowercase(),
        None => String::new(),
    }
}

/// Normalizes a raw `info_title` ('TRIALS OF OLYMPUS', 'Christmas Tree!').
pub fn format_info_title(title: &str) -> String {
    let cleaned = crate::text::collapse_spaces(title.trim().trim_end_matches(['!', '.']));
    if cleaned.is_empty() {
        return String::new();
    }
    let cleaned = if is_upper(&cleaned) && cleaned.chars().count() > 3 {
        cleaned
            .to_lowercase()
            .split(' ')
            .enumerate()
            .map(|(i, w)| if i > 0 && SMALL_WORDS.contains(&w) { w.to_string() } else { capitalize(w) })
            .collect::<Vec<_>>()
            .join(" ")
    } else {
        cleaned
    };
    cut(&cleaned)
}

fn starts_lower(s: &str) -> bool {
    s.chars().next().map(|c| c.is_lowercase()).unwrap_or(false)
}

fn family_clan_result_name(family: &str, clanmates: &HashMap<String, String>) -> String {
    if family.is_empty() {
        return String::new();
    }
    for candidate in [
        format!("client.global_tournament.value.journal.entry_item.{}_clan_result.title_2", family),
        format!("client.global_tournament.value.journal.entry_item.{}_clan_result.title", family),
        format!("global_tournament.client.value.journal.entry_item.{}_clan_result.title_2", family),
        format!("global_tournament.client.value.journal.entry_item.{}_clan_result.title", family),
        format!("client.global_tournament.value.journal.entry_item.{}_tournament_user_result.title", family),
        format!("global_tournament.client.value.journal.entry_item.{}_tournament_user_result.title", family),
    ] {
        if let Some(v) = clanmates.get(&candidate) {
            return v.clone();
        }
    }
    String::new()
}

/// Matches a component tracker (`omens_damage` in family `sabotage`) to its `.mo` key.
fn family_tracker_result_name(family: &str, tracker: &str, clanmates: &[(String, String)]) -> String {
    if family.is_empty() {
        return String::new();
    }
    let p1 = format!("client.global_tournament.value.journal.entry_item.{}_", family);
    let p2 = format!("global_tournament.client.value.journal.entry_item.{}_", family);
    let lower = tracker.to_lowercase();
    let tokens: Vec<&str> = lower.split('_').filter(|t| !t.is_empty()).collect();
    let mut best = String::new();
    let mut best_score: i64 = -1;
    for (k, extracted) in clanmates {
        if !(k.starts_with(&p1) || k.starts_with(&p2)) || k.contains("_clan_result.") {
            continue;
        }
        let kl = k.to_lowercase();
        let score = tokens.iter().filter(|t| kl.contains(*t) || kl.contains(t.trim_end_matches('s'))).count() as i64;
        if score > best_score {
            best_score = score;
            best = extracted.clone();
        }
    }
    best
}

pub type Names = (BTreeMap<String, String>, BTreeMap<i128, String>);

/// `(rankings_map, types_map)` from raw static and `.mo` bytes:
/// `{"<game_type>|<ranking>": name}` and `{game_type: default name}`.
pub fn decode_tournament_tables(strings_raw: &[u8], nodup_raw: &[u8], mo_raw: &[u8]) -> Result<Names, String> {
    let translations = static_data::translations(mo_raw)?;
    let strings = static_data::first_list(strings_raw)?;
    let mut tables = static_data::tables(nodup_raw, &[TOURNAMENT_TABLE_INDEX, COMPONENT_TABLE_INDEX])?;
    let (Some(Value::Map(t44)), Some(Value::Map(t71))) = (tables.remove(&TOURNAMENT_TABLE_INDEX), tables.remove(&COMPONENT_TABLE_INDEX))
    else {
        return Err("Tournament tables in _no_duplicates are not maps".into());
    };
    let translate = |key: &str| -> String { if key.is_empty() { String::new() } else { translations.get(key).cloned().unwrap_or_default() } };

    // 1. Every Journal clanmates entry title in the translation file, in a stable order.
    let mut sorted_keys: Vec<&String> = translations.keys().collect();
    sorted_keys.sort();
    let mut clanmates_list: Vec<(String, String)> = Vec::new();
    for k in sorted_keys {
        let v = &translations[k];
        if k.contains("journal") && v.to_lowercase().contains("your clanmates") {
            let extracted = clean_clanmates_title(v);
            if !extracted.is_empty() {
                clanmates_list.push((k.clone(), extracted));
            }
        }
    }
    let clanmates: HashMap<String, String> = clanmates_list.iter().cloned().collect();

    // 2. Table 44 rows by game_type.
    let mut by_type: BTreeMap<i128, Vec<(i128, i128, Vec<Value>)>> = BTreeMap::new();
    for (k, row) in &t44 {
        let Some(r) = row.as_arr().filter(|r| r.len() > 10) else { continue };
        let Some(key) = k.as_str().and_then(literal_ints) else { continue };
        let parsed = (|| Some((key.at(0)?.at(0)?.strict_int()?, key.at(0)?.at(1)?.strict_int()?, key.at(1)?.strict_int()?)))();
        if let Some((game_type, variant, sub)) = parsed {
            by_type.entry(game_type).or_default().push((variant, sub, r.clone()));
        }
    }

    // 3. Table 71 components by game_type.
    let json_at = |v: &Value| -> Json {
        let s = match v.strict_int() {
            Some(i) if i > 0 && (i as usize) < strings.len() => text_at(&strings, v),
            _ => "{}".into(),
        };
        if s.starts_with('{') { serde_json::from_str(&s).unwrap_or(json!({})) } else { json!({}) }
    };
    let mut comps: HashMap<i128, Vec<(String, Json)>> = HashMap::new();
    for (k, row) in &t71 {
        let Some(r) = row.as_arr().filter(|r| r.len() > 6) else { continue };
        let Some(game_type) = k.as_str().and_then(literal_ints).and_then(|key| key.at(0)?.at(0)?.strict_int()) else { continue };
        let name = match r[4].strict_int() {
            Some(i) if i > 0 && (i as usize) < strings.len() => text_at(&strings, &r[4]),
            _ => String::new(),
        };
        if !name.is_empty() {
            comps.entry(game_type).or_default().push((name, json_at(&r[6])));
        }
    }

    let family_re = Regex::new(r"global_tournament\.client\.value\.([^.]+)\.").unwrap();
    let over_re = Regex::new(r#"(?i)^the\s+"?(.+?)"?\s+(?:event\s+tournament|tournament|event|competition)?\s*is\s+over\b"#).unwrap();
    let mut rankings: BTreeMap<String, String> = BTreeMap::new();
    let mut types: BTreeMap<i128, String> = BTreeMap::new();

    for (game_type, rows) in by_type.iter_mut() {
        rows.sort_by_key(|(v, s, _)| (*v, *s));
        let row = &rows[0].2;
        let ui = json_at(&row[10]);
        let info_key = ui.get("info_title").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let info_title = format_info_title(&translate(&info_key));
        let family = family_re.captures(&info_key).map(|c| c[1].to_string()).unwrap_or_default();
        let j_user_key = ui.get("journal_users_result_titles").and_then(|j| j.get("0")).and_then(|v| v.as_str()).unwrap_or("");
        let j_user_text = translate(j_user_key);
        let direct = clean_clanmates_title(&j_user_text);
        let family_name = family_clan_result_name(&family, &clanmates);

        let mut over_title = String::new();
        if !j_user_text.is_empty() && direct.is_empty() {
            if let Some(m) = over_re.captures(&crate::text::collapse_spaces(&j_user_text)) {
                over_title = format_info_title(&m[1]);
            }
        }
        let base_title = if !info_title.is_empty() { info_title } else { over_title };
        let mut default_name = direct.clone();
        if default_name.is_empty() {
            if !family_name.is_empty() && !starts_lower(&family_name) {
                default_name = family_name.clone();
            } else if !base_title.is_empty() {
                let lower = base_title.to_lowercase();
                default_name = if lower.contains("tournament") || lower.contains("competition") {
                    base_title.clone()
                } else {
                    format!("{} tournament", base_title)
                };
            }
        }
        if !direct.is_empty() {
            rankings.insert(ranking_key(*game_type, None), direct.clone());
        }
        for (comp, cfg) in comps.get(game_type).cloned().unwrap_or_default() {
            match comp.as_str() {
                "TriumphUnion" => {
                    let union = clanmates
                        .get("client.global_tournament.value.journal.entry_item.union_of_triumph_clanmates_score.title")
                        .cloned()
                        .unwrap_or_else(|| default_name.clone());
                    if !union.is_empty() {
                        rankings.insert(ranking_key(*game_type, None), union.clone());
                        default_name = union;
                    }
                }
                "MultistageJrnTournamentResultsSender" => {
                    let name = if !family_name.is_empty() && !starts_lower(&family_name) { family_name.clone() } else { default_name.clone() };
                    if !name.is_empty() {
                        rankings.insert(ranking_key(*game_type, Some("multistage_tournament_clan_statistics_entry")), name);
                    }
                }
                "ClanPointsMining" => {
                    let name = if !family_name.is_empty() { family_name.clone() } else { default_name.clone() };
                    if !name.is_empty() {
                        rankings.insert(ranking_key(*game_type, Some("clan_points_mining_tournament_clan_statistics_entry")), name);
                    }
                }
                "ClanMembersItemGainTracking" => {
                    if let Some(trackers) = cfg.get("trackers").and_then(|t| t.as_array()) {
                        for tracker in trackers {
                            let id = tracker.get("trackerId").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
                            if id.is_empty() {
                                continue;
                            }
                            let key = ranking_key(*game_type,
                                                  Some(&format!("clan_members_item_gain_tracking_final_tracker_statistic_entry:{}", id)));
                            let mut name = family_tracker_result_name(&family, &id, &clanmates_list);
                            if name.is_empty() {
                                name = if !family_name.is_empty() { family_name.clone() } else { default_name.clone() };
                            }
                            if !name.is_empty() {
                                rankings.insert(key, name);
                            }
                        }
                    }
                }
                _ => {}
            }
        }
        if !default_name.is_empty() {
            types.insert(*game_type, default_name);
        }
    }
    Ok((rankings, types))
}

pub fn fetch_cdn_catalog(version: &Map<String, Json>) -> Result<Names, String> {
    let get = |k: &str| version.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
    let mo = static_data::fetch_translation(&get(static_data::LOCALE))?;
    let strings = static_data::fetch_static(&get("strings"))?;
    let nodup = static_data::fetch_static(&get("no_duplicates"))?;
    decode_tournament_tables(&strings, &nodup, &mo)
}

pub type VersionFn = Box<dyn Fn() -> Result<Map<String, Json>, String> + Send>;
pub type CatalogFn = Box<dyn Fn(&Map<String, Json>) -> Result<Names, String> + Send>;

/// Lazy, disk-cached resolver for `(game_type, ranking) -> tournament name`.
pub struct TournamentCatalog {
    pub cache_path: PathBuf,
    loaded: bool,
    pub version: Map<String, Json>,
    pub rankings: BTreeMap<String, String>,
    pub types: BTreeMap<i128, String>,
    checked: Option<Map<String, Json>>,
    fetch_version: VersionFn,
    fetch_catalog: CatalogFn,
}

impl TournamentCatalog {
    pub fn new(cache_path: &Path) -> Self {
        Self::with_fetchers(cache_path, Box::new(|| static_data::fetch_cdn_version(&FILES)), Box::new(fetch_cdn_catalog))
    }

    pub fn with_fetchers(cache_path: &Path, fetch_version: VersionFn, fetch_catalog: CatalogFn) -> Self {
        TournamentCatalog { cache_path: cache_path.to_path_buf(), loaded: false, version: Map::new(), rankings: BTreeMap::new(),
                            types: BTreeMap::new(), checked: None, fetch_version, fetch_catalog }
    }

    fn load(&mut self) {
        if self.loaded {
            return;
        }
        self.loaded = true;
        if !self.cache_path.is_file() {
            return;
        }
        let Some(data) = read_json(&self.cache_path) else {
            crate::warn!("Could not read local tournament catalog cache {}", self.cache_path.display());
            return;
        };
        self.version = data["version"].as_object().cloned().unwrap_or_default();
        if let Some(r) = data["rankings"].as_object() {
            for (k, v) in r {
                if let Some(s) = v.as_str().map(str::trim).filter(|s| !s.is_empty()) {
                    self.rankings.insert(k.clone(), cut(s));
                }
            }
        }
        if let Some(t) = data["types"].as_object() {
            for (k, v) in t {
                if let (Ok(id), Some(s)) = (k.parse::<i128>(), v.as_str().map(str::trim).filter(|s| !s.is_empty())) {
                    self.types.insert(id, cut(s));
                }
            }
        }
    }

    pub fn save(&self) {
        let types: Map<String, Json> = self.types.iter().map(|(k, v)| (k.to_string(), json!(v))).collect();
        let payload = json!({"version": self.version, "rankings": self.rankings, "types": types});
        if let Err(e) = write_json_atomic(&self.cache_path, &payload, 2) {
            crate::warn!("Could not write local tournament catalog cache {}: {}", self.cache_path.display(), e);
        }
    }

    pub fn set_loaded(&mut self) {
        self.loaded = true;
    }

    fn refresh(&mut self, missing: &str) {
        let cdn = match (self.fetch_version)() {
            Ok(v) => v,
            Err(e) => {
                crate::warn!("Could not check CDN tournament catalog version for {}: {}", missing, e);
                if self.rankings.is_empty() && self.types.is_empty() {
                    self.fallback_desktop();
                }
                return;
            }
        };
        if cdn == self.version || Some(&cdn) == self.checked.as_ref() {
            return;
        }
        self.checked = Some(cdn.clone());
        let build: String = cdn.get("no_duplicates").and_then(|v| v.as_str()).unwrap_or("").chars().take(12).collect();
        crate::info!("New tournament type '{}' not in local cache; updating tournament catalog from CDN ({})...", missing, build);
        match (self.fetch_catalog)(&cdn) {
            Ok((r, t)) if !r.is_empty() || !t.is_empty() => {
                self.version = cdn;
                self.rankings = r;
                self.types = t;
                self.save();
                crate::info!("Tournament catalog updated from CDN: {} ranking(s) and {} tournament type(s) cached locally.",
                             self.rankings.len(), self.types.len());
            }
            Ok(_) => {}
            Err(e) => {
                crate::warn!("Could not download tournament catalog from CDN: {}", e);
                if self.rankings.is_empty() && self.types.is_empty() {
                    self.fallback_desktop();
                }
            }
        }
    }

    fn fallback_desktop(&mut self) {
        let Some(files) = static_data::desktop_files(false) else { return };
        match decode_tournament_tables(&files.strings, &files.nodup, &files.mo) {
            Ok((r, t)) if !r.is_empty() || !t.is_empty() => {
                self.version = files.version;
                self.rankings = r;
                self.types = t;
                self.save();
                crate::info!("Tournament catalog initialized from local Desktop cache: {} ranking(s) and {} tournament type(s).",
                             self.rankings.len(), self.types.len());
            }
            Ok(_) => {}
            Err(e) => crate::warn!("Could not load local Desktop tournament catalog fallback: {}", e),
        }
    }

    /// The English name of `(game_type, ranking)`; the CDN only when the key is not cached.
    pub fn lookup(&mut self, game_type: Option<i128>, ranking: Option<&str>) -> String {
        let Some(g) = game_type.filter(|g| *g > 0) else { return String::new() };
        let norm = normalize_ranking(ranking);
        let key = ranking_key(g, Some(&norm));
        self.load();
        if let Some(n) = self.rankings.get(&key) {
            return n.clone();
        }
        if norm.is_empty() {
            if let Some(n) = self.types.get(&g) {
                return n.clone();
            }
        }
        self.refresh(&key);
        if let Some(n) = self.rankings.get(&key) {
            return n.clone();
        }
        self.types.get(&g).cloned().unwrap_or_default()
    }
}

fn shared() -> &'static Mutex<TournamentCatalog> {
    static CATALOG: OnceLock<Mutex<TournamentCatalog>> = OnceLock::new();
    CATALOG.get_or_init(|| Mutex::new(TournamentCatalog::new(&crate::paths::module_data_dir().join("events").join("tournament_types.json"))))
}

pub fn lookup_tournament_name(game_type: Option<i128>, ranking: Option<&str>) -> String {
    shared().lock().unwrap().lookup(game_type, ranking)
}

pub type LookupFn = Box<dyn FnMut(Option<i128>, Option<&str>) -> String>;

/// The `catalogue_name(Tournament)` resolver of the search.
///
/// Nameless entries the site already has are named from the game's catalogue and
/// sent back named; during the walk, a type the site does not name is looked up the
/// same way, registered on the site's catalogue with its name, and returned.
pub fn make_catalogue_name<'a>(client: &'a dyn Site, known: &mut Map<String, Json>, mut lookup: LookupFn)
                               -> impl FnMut(&Tournament) -> String + 'a {
    let mut pending = Vec::new();
    for entry in known.values_mut() {
        if entry.get("name").map(|n| !n.is_null() && n.as_str() != Some("")).unwrap_or(false) {
            continue;
        }
        let gtype = match entry.get("game_type") {
            Some(Json::Number(n)) => n.as_i64().map(|v| v as i128),
            Some(Json::String(s)) => s.trim().parse().ok(),
            _ => None,
        };
        let Some(gtype) = gtype else { continue };
        let norm = normalize_ranking(entry.get("ranking").and_then(|r| r.as_str()));
        let resolved = lookup(Some(gtype), Some(&norm));
        if !resolved.is_empty() {
            entry["name"] = json!(resolved);
            let key = entry.get("tournament_key").and_then(|k| k.as_str()).filter(|k| !k.is_empty()).map(String::from)
                .unwrap_or_else(|| gtype.to_string());
            pending.push(json!({"tournament_key": key, "ranking": norm, "name": resolved}));
        }
    }
    if !pending.is_empty() {
        match client.send_catalog(&pending) {
            Ok(_) => crate::info!("Named {} pending tournament type(s) on the site from game catalog.", pending.len()),
            Err(e) => crate::warn!("Could not update pending tournament names on the site: {}", e),
        }
    }
    let mut known = known.clone();
    move |t: &Tournament| {
        let key = t.tournament_key.clone().unwrap_or_default();
        let gtype_str = key.split(':').next().unwrap_or("").to_string();
        let norm = normalize_ranking(Some(&t.ranking));
        let r_key = format!("{}|{}", gtype_str, norm);
        if let Some(name) = known.get(&r_key).and_then(|e| e.get("name")).filter(|n| !n.is_null() && n.as_str() != Some("")) {
            return name.as_str().map(String::from).unwrap_or_else(|| name.to_string());
        }
        let Ok(gtype) = gtype_str.trim().parse::<i128>() else { return String::new() };
        let resolved = lookup(Some(gtype), Some(&norm));
        if resolved.is_empty() {
            return String::new();
        }
        known.insert(r_key.clone(), json!({"game_type": gtype as i64, "ranking": norm, "name": resolved}));
        let mut entry = json!({"tournament_key": if key.is_empty() { gtype.to_string() } else { key.clone() }, "ranking": norm,
                               "name": resolved});
        if t.ended_at.is_some() {
            entry["ended_at"] = json!(t.ended_at_iso());
        }
        if let Err(e) = client.send_catalog(&[entry]) {
            crate::warn!("Could not register tournament '{}' ({}) on the site catalogue: {}", resolved, r_key, e);
        }
        resolved
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::modules::events::api::ApiError;
    use std::cell::RefCell;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    #[test]
    fn clean_clanmates_title_extracts_exact_names() {
        assert_eq!("Great Hunt tournament", clean_clanmates_title("Your Clanmates' results in the Great Hunt tournament."));
        assert_eq!("Rise of the Ancients tournament", clean_clanmates_title("Your clanmates'  scores in the Rise of the Ancients tournament"));
        assert_eq!("contributions of Omen Essence in the Dark Omens tournament",
                   clean_clanmates_title("Your Clanmates' contributions of Omen Essence in the Dark Omens tournament."));
        assert_eq!("score of Remnants of Dread in battles with Dark Omens",
                   clean_clanmates_title("Your clanmates' score of Remnants of Dread in battles with Dark Omens."));
        assert_eq!("", clean_clanmates_title("Something else"));
        assert_eq!("Trials of Olympus", format_info_title("TRIALS OF OLYMPUS"));
        assert_eq!("Christmas Tree", format_info_title("Christmas Tree!"));
    }

    fn cache_with(dir: &Path) -> PathBuf {
        let path = dir.join("tournament_types.json");
        let mut c = TournamentCatalog::new(&path);
        c.set_loaded();
        c.version = json!({"no_duplicates": "v1", "strings": "s1", "en_US": "m1"}).as_object().unwrap().clone();
        c.rankings.insert("1024|".into(), "Rise of the Ancients tournament".into());
        c.rankings.insert("1033|clan_points_mining_tournament_clan_statistics_entry".into(), "Dark Omens tournament".into());
        c.types.insert(1024, "Rise of the Ancients tournament".into());
        c.save();
        path
    }

    #[test]
    fn local_cache_resolves_without_network_and_the_cdn_only_on_an_unknown_type() {
        let dir = tempfile::tempdir().unwrap();
        let path = cache_with(dir.path());
        let versions = Arc::new(AtomicUsize::new(0));
        let v = versions.clone();
        let mut c = TournamentCatalog::with_fetchers(
            &path,
            Box::new(move || {
                v.fetch_add(1, Ordering::SeqCst);
                Ok(json!({"no_duplicates": "v2", "strings": "s2", "en_US": "m2"}).as_object().unwrap().clone())
            }),
            Box::new(|_| Ok((BTreeMap::from([("1050|".to_string(), "Epic Hunt tournament".to_string())]), BTreeMap::new()))),
        );
        assert_eq!("Rise of the Ancients tournament", c.lookup(Some(1024), Some("")));
        assert_eq!("Rise of the Ancients tournament", c.lookup(Some(1024), Some("global_tournament_user_result")));
        assert_eq!("Dark Omens tournament", c.lookup(Some(1033), Some("CLAN_POINTS_MINING_TOURNAMENT_CLAN_STATISTICS_ENTRY")));
        assert_eq!(0, versions.load(Ordering::SeqCst));
        assert_eq!("Epic Hunt tournament", c.lookup(Some(1050), None));
        assert_eq!(1, versions.load(Ordering::SeqCst));
        assert_eq!("", c.lookup(Some(0), None));
    }

    #[derive(Default)]
    struct Catalogue(RefCell<Vec<Json>>);

    impl Site for Catalogue {
        fn search_state(&self) -> Result<Json, ApiError> {
            Ok(json!({}))
        }
        fn lookup(&self, _u: &[String]) -> Result<Map<String, Json>, ApiError> {
            Ok(Map::new())
        }
        fn send_catalog(&self, entries: &[Json]) -> Result<Vec<Json>, ApiError> {
            self.0.borrow_mut().extend(entries.iter().cloned());
            Ok(vec![])
        }
        fn send_tournament(&self, _p: &Json) -> Result<Json, ApiError> {
            Ok(json!({}))
        }
        fn report_search(&self, _r: &Json) -> Result<Json, ApiError> {
            Ok(json!({}))
        }
    }

    #[test]
    fn make_catalogue_name_names_pending_and_new_tournaments_automatically() {
        let site = Catalogue::default();
        let mut known: Map<String, Json> = Map::new();
        known.insert("1024|".into(), json!({"game_type": 1024, "ranking": "", "name": null, "tournament_key": "1024:1"}));
        known.insert("1001|".into(), json!({"game_type": 1001, "ranking": "", "name": "Known One"}));
        let lookup: LookupFn = Box::new(|g, _r| match g {
            Some(1024) => "Rise of the Ancients tournament".into(),
            Some(1050) => "Epic Hunt tournament".into(),
            _ => String::new(),
        });
        let mut name = make_catalogue_name(&site, &mut known, lookup);
        assert_eq!(1, site.0.borrow().len(), "the pending one was named on the site");
        assert_eq!("Rise of the Ancients tournament", site.0.borrow()[0]["name"]);
        assert_eq!("Known One", name(&Tournament::new("u1", Some("1001:1"), None)));
        assert_eq!("Epic Hunt tournament", name(&Tournament::new("u2", Some("1050:2"), Some(1789232426))));
        assert_eq!(2, site.0.borrow().len());
        assert_eq!("1050:2", site.0.borrow()[1]["tournament_key"]);
        assert_eq!("2026-09-12T17:00:26Z", site.0.borrow()[1]["ended_at"]);
        assert_eq!("", name(&Tournament::new("u3", Some("9999:1"), None)));
    }
}
