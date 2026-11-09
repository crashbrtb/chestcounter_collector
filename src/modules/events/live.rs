//! The tournaments of the game's Tournaments window: the ones running now and the
//! ones about to start, with the clan's points and place, the best clans, the clan's
//! own players and what each place receives - for the clan site's list of events.
//!
//! Measured on the live game, 2026-11-07:
//!
//! - route 24001 `[[[me], [clan], kingdom], []]` -> `[[[type, variant], run, state, at, ...,
//!   changed]...]`: state 1 is running (`at` is its end, `changed` its start), state 2
//!   is waiting (`at` is its next start);
//! - route 24201 `[[[type, variant]...]]` -> `[[type, variant], my points, clan points, _]`;
//! - route 24003 `[[[me], [clan], kingdom], [[type, variant], [1, 1]]]` -> the top 100
//!   players, clans and kingdoms, and the places of mine (0 when not among them);
//! - route 38003 `[[type, variant]]` -> the clan's players: `[[player], points, place]`;
//! - route 15000 `[[[[clan], 8]...]]` -> clans: `[[clan], [tag, name, ...]]`.
//!
//! What the game's client knows by itself comes from its static data: Table 44 (key
//! `[[type, variant], sub]`) gives the title (`row[10].info_title`), the banner
//! (`process_back`), the icon and, for the clan tournaments, the rewards of each place
//! (`row[8].clansRewards`); Table 12 what each reward hands out; Table 8 the items'
//! names (`row[3]`) and icons (`row[6].icon`).

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value as Json};

use super::catalog::format_info_title;
use crate::config::{read_json, write_json_atomic};
use crate::error::Res;
use crate::link::{first_rows, GameApi};
use crate::modules::chests::catalog::resolve_vector;
use crate::static_data::{self, literal_ints, text_at};
use crate::tbcodec::Value;

pub const SCHEDULE_ROUTE: i128 = 24001;
pub const SCORES_ROUTE: i128 = 24201;
pub const RATINGS_ROUTE: i128 = 24003;
pub const MEMBERS_ROUTE: i128 = 38003;
pub const CLANS_ROUTE: i128 = 15000;
pub const PROFILES_ROUTE: i128 = 402;
const TOURNAMENT_TABLE: usize = 44;
const REWARD_TABLE: usize = 12;
const ITEM_TABLE: usize = 8;
/// How far ahead the waiting tournaments are listed, as the game's "Get ready".
pub const UPCOMING_WITHIN: i128 = 4 * 86400;
/// How far ahead the announced ones are listed: those the game shows before their
/// start (`show_before_announce`: Trials of Olympus, Season's Legacy...) and the clan
/// tournaments (Ragnarök), which come weeks apart.
pub const ANNOUNCED_WITHIN: i128 = 120 * 86400;
/// The best clans kept for the site.
pub const TOP_CLANS: usize = 10;
const CACHE_FORMAT: u32 = 2;

// ------------------------------------------------------------ static data

/// What the client knows of one tournament (type and variant).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct TournamentInfo {
    pub name: String,
    pub banner: String,
    pub banner_inactive: String,
    pub icon: String,
    /// How many places each tier covers, and its rewards.
    pub clan_tiers: Vec<(usize, Vec<i128>)>,
    pub show_before: Option<i128>,
    /// The game hides it from a player who cannot join it (`hide_if_unavailable`).
    pub hide_if_unavailable: bool,
    /// Who can join (`row[9].joinCondition`), as the game writes it.
    pub join_condition: String,
}

impl TournamentInfo {
    /// Joining depends on an A/B group of the game, which only the server knows.
    pub fn depends_on_a_group(&self) -> bool {
        self.join_condition.contains("IsAliasConditionMet")
    }
}

#[derive(Debug, Clone, Default)]
pub struct LiveCatalog {
    pub version: String,
    pub tournaments: HashMap<String, TournamentInfo>,
    /// item id -> (name, icon)
    pub items: HashMap<i128, (String, String)>,
    /// reward id -> [(item, amount)]
    pub rewards: HashMap<i128, Vec<(i128, i128)>>,
}

pub fn key_of(game_type: i128, variant: i128) -> String {
    format!("{}:{}", game_type, variant)
}

fn json_string(strings: &[Value], v: &Value) -> Json {
    let s = text_at(strings, v);
    if s.starts_with('{') { serde_json::from_str(&s).unwrap_or(json!({})) } else { json!({}) }
}

fn map_id(k: &Value) -> Option<i128> {
    match k {
        Value::Str(s) => s.trim().parse().ok(),
        other => other.strict_int(),
    }
}

/// Every tournament's title, images and clan rewards, and the items they hand out.
pub fn decode(strings_raw: &[u8], vectors_raw: &[u8], nodup_raw: &[u8], mo_raw: &[u8]) -> Result<LiveCatalog, String> {
    let translations = static_data::translations(mo_raw)?;
    let strings = static_data::first_list(strings_raw)?;
    let vectors = static_data::first_list(vectors_raw)?;
    let mut tables = static_data::tables(nodup_raw, &[ITEM_TABLE, REWARD_TABLE, TOURNAMENT_TABLE])?;
    let mut catalog = LiveCatalog::default();

    let mut chosen: HashMap<String, i128> = HashMap::new();
    if let Some(Value::Map(rows)) = tables.remove(&TOURNAMENT_TABLE) {
        for (k, row) in &rows {
            let Some(r) = row.as_arr().filter(|r| r.len() > 10) else { continue };
            let Some(key) = k.as_str().and_then(literal_ints) else { continue };
            let parsed = (|| Some((key.at(0)?.at(0)?.strict_int()?, key.at(0)?.at(1)?.strict_int()?, key.at(1)?.strict_int()?)))();
            let Some((game_type, variant, sub)) = parsed else { continue };
            let id = key_of(game_type, variant);
            if chosen.get(&id).map(|s| *s <= sub).unwrap_or(false) {
                continue;
            }
            chosen.insert(id.clone(), sub);
            let ui = json_string(&strings, &r[10]);
            let text = |k: &str| ui.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
            let title_key = text("info_title");
            let name = format_info_title(&translations.get(&title_key).cloned().unwrap_or_default());
            let rewards = json_string(&strings, &r[8]);
            let join_condition = json_string(&strings, &r[9]).get("joinCondition").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let clan_tiers = rewards
                .get("clansRewards")
                .and_then(|c| c.as_array())
                .map(|tiers| {
                    tiers
                        .iter()
                        .filter_map(|t| {
                            let places = t.get("positions")?.as_u64()? as usize;
                            let ids = t.get("factoryPos")?.as_array()?.iter().filter_map(|v| v.as_i64().map(|i| i as i128)).collect();
                            Some((places, ids))
                        })
                        .collect()
                })
                .unwrap_or_default();
            catalog.tournaments.insert(id, TournamentInfo {
                name,
                banner: text("process_back"),
                banner_inactive: text("process_back_inactive"),
                icon: text("icon"),
                clan_tiers,
                show_before: ui.get("show_before_announce").and_then(|v| v.as_i64()).map(|v| v as i128),
                hide_if_unavailable: ui.get("hide_if_unavailable").and_then(|v| v.as_bool()).unwrap_or(false),
                join_condition,
            });
        }
    }

    let wanted: Vec<i128> = catalog.tournaments.values().flat_map(|t| t.clan_tiers.iter().flat_map(|(_, ids)| ids.clone())).collect();
    if let Some(Value::Map(rows)) = tables.remove(&REWARD_TABLE) {
        for (k, row) in rows {
            let Some(id) = map_id(&k).filter(|id| wanted.contains(id)) else { continue };
            let Some(Value::Map(items)) = row.at(4).and_then(|r| r.at(0)) else { continue };
            catalog.rewards.insert(id, items.iter().filter_map(|(i, a)| Some((map_id(i)?, a.strict_int()?))).collect());
        }
    }
    if let Some(Value::Map(rows)) = tables.remove(&ITEM_TABLE) {
        for (k, row) in rows {
            let (Some(id), Some(r)) = (map_id(&k), row.as_arr()) else { continue };
            let name = r.get(3).map(|f| resolve_vector(f, &vectors, &strings, &translations)).unwrap_or_default();
            let icon = r.get(6).map(|f| json_string(&strings, f)).and_then(|j| j.get("icon").and_then(|v| v.as_str()).map(String::from));
            if !name.is_empty() {
                catalog.items.insert(id, (name, icon.unwrap_or_default()));
            }
        }
    }
    Ok(catalog)
}

impl LiveCatalog {
    /// The catalogue of the CDN's current build, from the disk when it is the same build.
    pub fn current(cache: &Path) -> Result<LiveCatalog, String> {
        let version = static_data::fetch_cdn_version(&["no_duplicates", "strings", "vectors"])?;
        let get = |k: &str| version.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
        // The format of the cache comes first: a newer one is built again.
        let build = format!("{}|{}|{}", CACHE_FORMAT, get("no_duplicates"), get(static_data::LOCALE));
        if let Some(cached) = Self::load(cache).filter(|c| c.version == build) {
            return Ok(cached);
        }
        let mut catalog = decode(&static_data::fetch_static(&get("strings"))?, &static_data::fetch_static(&get("vectors"))?,
                                 &static_data::fetch_static(&get("no_duplicates"))?,
                                 &static_data::fetch_translation(&get(static_data::LOCALE))?)?;
        catalog.version = build;
        catalog.save(cache);
        Ok(catalog)
    }

    fn load(path: &Path) -> Option<LiveCatalog> {
        let d = read_json(path)?;
        let mut c = LiveCatalog { version: d["version"].as_str()?.to_string(), ..Default::default() };
        for (k, t) in d["tournaments"].as_object()? {
            let s = |f: &str| t[f].as_str().unwrap_or("").to_string();
            let tiers = t["clan_tiers"]
                .as_array()
                .map(|a| a.iter().filter_map(|x| Some((x[0].as_u64()? as usize, x[1].as_array()?.iter().filter_map(|v| v.as_i64().map(|i| i as i128)).collect()))).collect())
                .unwrap_or_default();
            c.tournaments.insert(k.clone(), TournamentInfo {
                name: s("name"), banner: s("banner"), banner_inactive: s("banner_inactive"), icon: s("icon"), clan_tiers: tiers,
                show_before: t["show_before"].as_i64().map(|v| v as i128),
                hide_if_unavailable: t["hide_if_unavailable"].as_bool().unwrap_or(false),
                join_condition: s("join_condition"),
            });
        }
        for (k, v) in d["items"].as_object()? {
            if let Ok(id) = k.parse() {
                c.items.insert(id, (v[0].as_str().unwrap_or("").to_string(), v[1].as_str().unwrap_or("").to_string()));
            }
        }
        for (k, v) in d["rewards"].as_object()? {
            if let Ok(id) = k.parse() {
                c.rewards.insert(id, v.as_array()?.iter().filter_map(|p| Some((p[0].as_i64()? as i128, p[1].as_i64()? as i128))).collect());
            }
        }
        Some(c)
    }

    fn save(&self, path: &Path) {
        let tournaments: Map<String, Json> = self
            .tournaments
            .iter()
            .map(|(k, t)| (k.clone(), json!({"name": t.name, "banner": t.banner, "banner_inactive": t.banner_inactive, "icon": t.icon,
                "clan_tiers": t.clan_tiers.iter().map(|(n, ids)| json!([n, ids.iter().map(|i| *i as i64).collect::<Vec<_>>()])).collect::<Vec<_>>(),
                "show_before": t.show_before.map(|v| v as i64), "hide_if_unavailable": t.hide_if_unavailable,
                "join_condition": t.join_condition})))
            .collect();
        let items: Map<String, Json> = self.items.iter().map(|(k, (n, i))| (k.to_string(), json!([n, i]))).collect();
        let rewards: Map<String, Json> = self
            .rewards
            .iter()
            .map(|(k, v)| (k.to_string(), json!(v.iter().map(|(i, a)| json!([*i as i64, *a as i64])).collect::<Vec<_>>())))
            .collect();
        let _ = write_json_atomic(path, &json!({"version": self.version, "tournaments": tournaments, "items": items, "rewards": rewards}), 0);
    }

    /// The rewards of each tier: (first place, last place, [(item, amount)]).
    pub fn tiers_of(&self, key: &str) -> Vec<(usize, usize, Vec<(i128, i128)>)> {
        let Some(info) = self.tournaments.get(key) else { return vec![] };
        let mut first = 1;
        let mut out = Vec::new();
        for (places, ids) in &info.clan_tiers {
            let mut sum: indexmap::IndexMap<i128, i128> = indexmap::IndexMap::new();
            for id in ids {
                for (item, amount) in self.rewards.get(id).cloned().unwrap_or_default() {
                    *sum.entry(item).or_default() += amount;
                }
            }
            out.push((first, first + places - 1, sum.into_iter().collect()));
            first += places;
        }
        out
    }

    pub fn item_name(&self, id: i128) -> String {
        self.items.get(&id).map(|i| i.0.clone()).unwrap_or_default()
    }

    pub fn item_icon(&self, id: i128) -> String {
        self.items.get(&id).map(|i| i.1.clone()).unwrap_or_default()
    }
}

pub fn cache_path(data_dir: &Path) -> PathBuf {
    data_dir.join("live_tournaments.json")
}

// ------------------------------------------------------------ the game

#[derive(Debug, Clone, PartialEq, Default)]
pub struct ScheduleRow {
    pub game_type: i128,
    pub variant: i128,
    pub run: i128,
    pub running: bool,
    /// End when running, next start when waiting.
    pub at: i128,
    /// When the state last changed: the start of a running one.
    pub changed: i128,
}

/// The rows of route 24001.
pub fn parse_schedule(rows: &[Value]) -> Vec<ScheduleRow> {
    rows.iter()
        .filter_map(|r| {
            let r = r.as_arr()?;
            let key = r.first()?.as_arr()?;
            Some(ScheduleRow {
                game_type: key.first()?.strict_int()?,
                variant: key.get(1)?.strict_int()?,
                run: r.get(1)?.strict_int()?,
                running: r.get(2)?.strict_int()? == 1,
                at: r.get(3)?.py_int()?,
                changed: r.last()?.py_int()?,
            })
        })
        .collect()
}

/// The running ones, the ones starting within `UPCOMING_WITHIN` that the client knows,
/// and the announced ones starting within `ANNOUNCED_WITHIN`.
pub fn listed(schedule: &[ScheduleRow], catalog: &LiveCatalog, now: i128) -> Vec<ScheduleRow> {
    let mut out: Vec<ScheduleRow> = schedule
        .iter()
        .filter(|r| {
            let Some(info) = catalog.tournaments.get(&key_of(r.game_type, r.variant)) else { return false };
            if info.name.is_empty() || info.banner.is_empty() {
                return false;
            }
            if info.hide_if_unavailable && info.depends_on_a_group() && !r.running {
                return false;
            }
            if r.running {
                r.at > now
            } else {
                let announced = info.show_before.is_some() || !info.clan_tiers.is_empty();
                r.at > now && r.at - now <= if announced { ANNOUNCED_WITHIN } else { UPCOMING_WITHIN }
            }
        })
        .cloned()
        .collect();
    out.sort_by_key(|r| (!r.running, r.at));
    out
}

fn identity(api: &dyn GameApi) -> Res<(i128, Option<i128>, Option<i128>)> {
    let me = api.player_id().ok_or_else(|| crate::error::game("the game's session does not say who is playing"))?;
    for frame in api.call(PROFILES_ROUTE, crate::arr![crate::arr![crate::arr![me]]])? {
        for row in first_rows(&frame) {
            let Some(r) = row.as_arr() else { continue };
            if r.first().and_then(|v| v.at(0)).and_then(|v| v.py_int()) != Some(me) {
                continue;
            }
            let clan = r.get(11).and_then(|v| v.at(0)).and_then(|v| v.py_int()).filter(|c| *c > 0);
            let kingdom = r.get(15).and_then(|v| v.at(0)).and_then(|v| v.py_int());
            return Ok((me, clan, kingdom));
        }
    }
    Ok((me, None, None))
}

fn answer(frames: &[crate::tbcodec::Frame], route: i128) -> Vec<Value> {
    frames.iter().find(|f| f.route == Some(route)).map(first_rows).unwrap_or_default()
}

/// The rows of route 24001 as the game sends them.
pub fn raw_schedule(api: &dyn GameApi) -> Res<Vec<Value>> {
    let (me, clan, kingdom) = identity(api)?;
    let who = crate::arr![crate::arr![me], crate::arr![clan.unwrap_or(0)], kingdom.unwrap_or(0)];
    Ok(answer(&api.call_rating(SCHEDULE_ROUTE, crate::arr![who, crate::arr![]])?, SCHEDULE_ROUTE))
}

/// A running or waiting tournament as the game shows it to this clan.
#[derive(Debug, Clone, Default)]
pub struct LiveTournament {
    pub row: ScheduleRow,
    pub my_points: Option<i128>,
    pub clan_points: Option<i128>,
    pub clan_place: Option<usize>,
    /// (clan, points), best first.
    pub top_clans: Vec<(i128, i128)>,
    /// (player, points, place)
    pub members: Vec<(i128, i128, usize)>,
}

/// Everything the Tournaments window shows, read over the network.
pub fn read(api: &dyn GameApi, catalog: &LiveCatalog, now: i128) -> Res<(Option<i128>, Vec<LiveTournament>, HashMap<i128, (String, String)>, HashMap<i128, String>)> {
    let (me, clan, kingdom) = identity(api)?;
    let who = crate::arr![crate::arr![me], crate::arr![clan.unwrap_or(0)], kingdom.unwrap_or(0)];
    let schedule = parse_schedule(&raw_schedule(api)?);
    let rows = listed(&schedule, catalog, now);
    let running: Vec<&ScheduleRow> = rows.iter().filter(|r| r.running).collect();

    let mut scores: HashMap<String, (i128, i128, i128)> = HashMap::new();
    if !running.is_empty() {
        let keys = Value::Arr(running.iter().map(|r| crate::arr![r.game_type, r.variant]).collect());
        for row in answer(&api.call(SCORES_ROUTE, crate::arr![keys])?, SCORES_ROUTE) {
            let (Some(key), Some(mine), Some(clan_points)) = (row.at(0), row.at(1).and_then(|v| v.py_int()), row.at(2).and_then(|v| v.py_int()))
            else { continue };
            let total = row.at(3).and_then(|v| v.py_int()).unwrap_or(0);
            if let (Some(t), Some(v)) = (key.at(0).and_then(|v| v.strict_int()), key.at(1).and_then(|v| v.strict_int())) {
                scores.insert(key_of(t, v), (mine, clan_points, total));
            }
        }
    }

    let mut out = Vec::new();
    let mut clan_ids: Vec<i128> = Vec::new();
    let mut player_ids: Vec<i128> = Vec::new();
    for row in rows {
        let key = key_of(row.game_type, row.variant);
        // What the game hides from this player: nobody here can take part in it.
        let hidden = catalog.tournaments.get(&key).map(|t| t.hide_if_unavailable).unwrap_or(false);
        if row.running && hidden && scores.get(&key).map(|(a, b, c)| *a == 0 && *b == 0 && *c == 0).unwrap_or(true) {
            continue;
        }
        let mut live = LiveTournament { row: row.clone(), ..Default::default() };
        if row.running {
            if let Some((mine, points, _)) = scores.get(&key) {
                live.my_points = Some(*mine);
                live.clan_points = Some(*points);
            }
            let is_clan = catalog.tournaments.get(&key).map(|t| !t.clan_tiers.is_empty()).unwrap_or(false);
            if is_clan {
                let objects = crate::arr![who.clone(), crate::arr![crate::arr![row.game_type, row.variant], crate::arr![1, 1]]];
                if let Ok(frames) = api.call_rating(RATINGS_ROUTE, objects) {
                    if let Some(f) = frames.iter().find(|f| f.route == Some(RATINGS_ROUTE)) {
                        // [[top players], [top clans], [top kingdoms]], then the places of mine in each.
                        let root = f.objects.first();
                        let lists = root.and_then(|o| o.at(0));
                        live.top_clans = lists
                            .and_then(|l| l.at(1))
                            .and_then(|l| l.at(0))
                            .and_then(|l| l.as_arr())
                            .map(|rows| rows.iter().filter_map(|r| Some((r.at(0)?.at(0)?.py_int()?, r.at(1)?.py_int()?))).collect())
                            .unwrap_or_default();
                        let place = root.and_then(|t| t.at(1)).and_then(|p| p.at(1)).and_then(|v| v.py_int()).unwrap_or(0);
                        live.clan_place = if place > 0 {
                            Some(place as usize)
                        } else {
                            clan.and_then(|c| live.top_clans.iter().position(|(id, _)| *id == c)).map(|i| i + 1)
                        };
                    }
                }
                live.top_clans.truncate(TOP_CLANS);
                clan_ids.extend(live.top_clans.iter().map(|(c, _)| *c));
            }
            if let Ok(frames) = api.call(MEMBERS_ROUTE, crate::arr![crate::arr![row.game_type, row.variant]]) {
                live.members = answer(&frames, MEMBERS_ROUTE)
                    .iter()
                    .filter_map(|r| Some((r.at(0)?.at(0)?.py_int()?, r.at(1)?.py_int()?, r.at(2)?.py_int()? as usize)))
                    .collect();
                player_ids.extend(live.members.iter().map(|m| m.0));
            }
        }
        out.push(live);
    }

    // The clans' tags and names (each from its own realm's server), and the players' names.
    clan_ids.sort();
    clan_ids.dedup();
    let mut directory = ClanDirectory::load(&directory_path());
    let clans = directory.resolve(api, &clan_ids);
    directory.save();
    player_ids.sort();
    player_ids.dedup();
    let names = crate::link::player_names(api, &player_ids).unwrap_or_default();
    Ok((clan, out, clans, names))
}

// ------------------------------------------------------------ the clans of other realms

pub const CLAN_INFO_ROUTE: i128 = 15200;
/// How many realms are asked, at most, for clans whose realm is not known yet.
const REALMS_TRIED: usize = 120;

pub fn directory_path() -> PathBuf {
    crate::paths::module_data_dir().join("events").join("clan_directory.json")
}

/// Where each clan is (its realm's server) and what it is called, as learned.
///
/// The game asks each clan's name of the server of its realm, and nothing in the
/// clan's id says which: the directory keeps what was seen (the game's own page
/// asking, or a realm answering) and asks the known realms about a new clan.
#[derive(Debug, Default)]
pub struct ClanDirectory {
    pub path: PathBuf,
    /// clan -> (tag, name, server)
    pub clans: HashMap<i128, (String, String, String)>,
    pub realms: Vec<String>,
}

/// `[[[clan], members, tag, name, ...], {...}]` rows of route 15200: clan -> (tag, name).
pub fn parse_clan_info(rows: &[Value]) -> HashMap<i128, (String, String)> {
    let mut out = HashMap::new();
    for row in rows {
        let Some(info) = row.at(0) else { continue };
        let Some(id) = info.at(0).and_then(|v| v.at(0)).and_then(|v| v.py_int()) else { continue };
        let s = |i: usize| info.at(i).and_then(|v| v.as_str()).unwrap_or("").to_string();
        if !s(3).is_empty() {
            out.insert(id, (s(2), s(3)));
        }
    }
    out
}

impl ClanDirectory {
    pub fn load(path: &Path) -> Self {
        let mut d = ClanDirectory { path: path.to_path_buf(), ..Default::default() };
        if let Some(data) = read_json(path) {
            for (k, v) in data["clans"].as_object().cloned().unwrap_or_default() {
                if let Ok(id) = k.parse::<i128>() {
                    let s = |i: usize| v[i].as_str().unwrap_or("").to_string();
                    d.clans.insert(id, (s(0), s(1), s(2)));
                }
            }
            d.realms = data["realms"].as_array().map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect()).unwrap_or_default();
        }
        d
    }

    pub fn save(&self) {
        let clans: Map<String, Json> = self.clans.iter().map(|(k, (t, n, u))| (k.to_string(), json!([t, n, u]))).collect();
        let _ = write_json_atomic(&self.path, &json!({"clans": clans, "realms": self.realms}), 0);
    }

    fn learn_realm(&mut self, url: &str) {
        if !url.is_empty() && !self.realms.iter().any(|r| r == url) {
            self.realms.push(url.to_string());
        }
    }

    fn ask(&mut self, api: &dyn GameApi, url: Option<&str>, ids: &[i128]) -> Vec<i128> {
        let objects = crate::arr![Value::Arr(ids.iter().map(|c| crate::arr![*c]).collect())];
        let frames = match url {
            Some(u) => api.call_at(u, CLAN_INFO_ROUTE, objects),
            None => api.call(CLAN_INFO_ROUTE, objects),
        };
        let Ok(frames) = frames else { return vec![] };
        let found = parse_clan_info(&answer(&frames, CLAN_INFO_ROUTE));
        for (id, (tag, name)) in &found {
            self.clans.insert(*id, (tag.clone(), name.clone(), url.unwrap_or("").to_string()));
        }
        found.into_keys().collect()
    }

    /// The tags and names of these clans: from their known realm, the home realm, or
    /// the other known realms; what could not be asked keeps its last known name.
    pub fn resolve(&mut self, api: &dyn GameApi, ids: &[i128]) -> HashMap<i128, (String, String)> {
        for (id, url) in api.observed_realms() {
            self.learn_realm(&url);
            let entry = self.clans.entry(id).or_insert((String::new(), String::new(), String::new()));
            entry.2 = url;
        }
        let mut by_url: BTreeMap<String, Vec<i128>> = BTreeMap::new();
        let mut unknown: Vec<i128> = Vec::new();
        for id in ids {
            match self.clans.get(id).map(|c| c.2.clone()) {
                Some(url) if !url.is_empty() => by_url.entry(url).or_default().push(*id),
                _ => unknown.push(*id),
            }
        }
        for (url, list) in by_url {
            let found = self.ask(api, Some(&url), &list);
            // Gone from there (moved): looked for again below.
            unknown.extend(list.into_iter().filter(|id| !found.contains(id)));
        }
        if !unknown.is_empty() {
            let found = self.ask(api, None, &unknown);
            unknown.retain(|id| !found.contains(id));
        }
        let realms: Vec<String> = self.realms.iter().take(REALMS_TRIED).cloned().collect();
        for url in realms {
            if unknown.is_empty() {
                break;
            }
            let found = self.ask(api, Some(&url), &unknown);
            unknown.retain(|id| !found.contains(id));
        }
        ids.iter().filter_map(|id| self.clans.get(id).filter(|c| !c.1.is_empty()).map(|c| (*id, (c.0.clone(), c.1.clone())))).collect()
    }
}

fn iso(seconds: i128) -> Option<String> {
    chrono::DateTime::from_timestamp(seconds as i64, 0).map(|d| d.format("%Y-%m-%dT%H:%M:%SZ").to_string())
}

/// The body of POST /api/v1/live-tournaments, and the names of the images it uses.
pub fn payload(clan: Option<i128>, live: &[LiveTournament], catalog: &LiveCatalog, clans: &HashMap<i128, (String, String)>,
               names: &HashMap<i128, String>, read_at: i128) -> (Json, Vec<String>) {
    let mut images: Vec<String> = Vec::new();
    let mut want = |n: &str| {
        if !n.is_empty() && !images.contains(&n.to_string()) {
            images.push(n.to_string());
        }
    };
    let mut list = Vec::new();
    for t in live {
        let key = key_of(t.row.game_type, t.row.variant);
        let info = catalog.tournaments.get(&key).cloned().unwrap_or_default();
        let banner = if t.row.running || info.banner_inactive.is_empty() { info.banner.clone() } else { info.banner_inactive.clone() };
        want(&banner);
        want(&info.icon);
        let tiers: Vec<Json> = catalog
            .tiers_of(&key)
            .into_iter()
            .map(|(from, to, items)| {
                json!({"from": from, "to": to, "items": items.iter().map(|(id, amount)| {
                    let icon = catalog.item_icon(*id);
                    want(&icon);
                    json!({"item_id": *id as i64, "name": catalog.item_name(*id), "icon": icon, "amount": *amount as i64})
                }).collect::<Vec<_>>()})
            })
            .collect();
        let (starts_at, ends_at) = if t.row.running { (Some(t.row.changed), Some(t.row.at)) } else { (Some(t.row.at), None) };
        list.push(json!({
            "key": key,
            "game_type": t.row.game_type as i64,
            "variant": t.row.variant as i64,
            "run": t.row.run as i64,
            "state": if t.row.running { "active" } else { "upcoming" },
            "starts_at": starts_at.and_then(iso),
            "ends_at": ends_at.and_then(iso),
            "name": info.name,
            "banner": banner,
            "icon": info.icon,
            "my_points": t.my_points.map(|v| v as i64),
            "clan_points": t.clan_points.map(|v| v as i64),
            "clan_place": t.clan_place,
            "top_clans": t.top_clans.iter().enumerate().map(|(i, (c, p))| {
                let (tag, name) = clans.get(c).cloned().unwrap_or_default();
                json!({"position": i + 1, "clan_id": *c as i64, "tag": tag, "name": name, "points": *p as i64})
            }).collect::<Vec<_>>(),
            "members": t.members.iter().map(|(p, pts, place)| json!({
                "position": place, "player_id": *p as i64, "name": names.get(p).cloned().unwrap_or_default(), "points": *pts as i64,
            })).collect::<Vec<_>>(),
            "rewards": tiers,
        }));
    }
    let mut keys: BTreeMap<String, bool> = BTreeMap::new();
    for t in live {
        keys.insert(key_of(t.row.game_type, t.row.variant), true);
    }
    (json!({"read_at": iso(read_at), "clan_id": clan.map(|c| c as i64), "tournaments": list}), images)
}

// ------------------------------------------------------------ to the clan site

/// The images the site lacks or has from another CDN file, as `[{name, file_id, png}]`.
pub fn images_to_send(wanted: &[String], known: &Map<String, Json>, file_of: &dyn Fn(&str) -> Option<String>,
                      png: &mut dyn FnMut(&str) -> Result<(String, Vec<u8>), String>, log: &mut dyn FnMut(&str)) -> Vec<Json> {
    use base64::Engine;
    let mut out = Vec::new();
    for name in wanted {
        let Some(file) = file_of(name) else { continue };
        if known.get(name).and_then(|v| v.as_str()) == Some(file.as_str()) {
            continue;
        }
        match png(name) {
            Ok((file, bytes)) => out.push(json!({"name": name, "file_id": file,
                                                  "png": base64::engine::general_purpose::STANDARD.encode(bytes)})),
            Err(e) => log(&format!("Image {} not read: {}", name, e)),
        }
    }
    out
}

/// Reads the game's Tournaments window and brings the clan site up to date with it,
/// with the game's images. Says what it did; a failure is told and does not stop the run.
pub fn refresh(api: &dyn GameApi, site: &dyn super::api::Site, data_dir: &Path, log: &mut dyn FnMut(&str)) -> Res<()> {
    let catalog = match LiveCatalog::current(&cache_path(data_dir)) {
        Ok(c) => c,
        Err(e) => {
            log(&format!("The game's tournament data could not be read from its CDN: {}", e));
            return Ok(());
        }
    };
    let now = chrono::Utc::now().timestamp() as i128;
    let (clan, live, clans, names) = match read(api, &catalog, now) {
        Ok(r) => r,
        Err(e) if e.is_cancelled() => return Err(e),
        Err(e) => {
            log(&format!("The game's Tournaments window was not read: {}", e));
            return Ok(());
        }
    };
    let (mut body, mut images) = payload(clan, &live, &catalog, &clans, &names, now);

    // The items the site shows as prizes: those of the rewards, and those it asks for.
    let known = match site.known_images() {
        Ok(k) => k,
        Err(e) if e.status == 404 => {
            log("The site does not take the game's Tournaments window yet: update it.");
            return Ok(());
        }
        Err(e) => {
            log(&format!("Could not ask the site which images it has: {}", e.message));
            json!({})
        }
    };
    let mut item_ids: Vec<i128> = live.iter().flat_map(|t| catalog.tiers_of(&key_of(t.row.game_type, t.row.variant)))
        .flat_map(|(_, _, items)| items.into_iter().map(|(i, _)| i)).collect();
    item_ids.extend(known["items_needed"].as_array().map(|a| a.iter().filter_map(|v| v.as_i64().map(|i| i as i128)).collect::<Vec<_>>()).unwrap_or_default());
    item_ids.sort();
    item_ids.dedup();
    let items: Vec<Json> = item_ids.iter().filter(|i| catalog.items.contains_key(i)).map(|i| {
        let icon = catalog.item_icon(*i);
        if !icon.is_empty() && !images.contains(&icon) {
            images.push(icon.clone());
        }
        json!({"item_id": *i as i64, "name": catalog.item_name(*i), "icon": icon})
    }).collect();
    body["items"] = json!(items);

    // The images: only those the site lacks, or that the game changed since.
    match crate::unity::ImageIndex::current() {
        Ok(index) => {
            let empty = Map::new();
            let have = known["images"].as_object().unwrap_or(&empty);
            let file_of = |n: &str| index.file_of(n).cloned();
            let mut png = |n: &str| index.png(n);
            let fresh = images_to_send(&images, have, &file_of, &mut png, log);
            for chunk in fresh.chunks(20) {
                if let Err(e) = site.send_images(chunk) {
                    log(&format!("The game's images were not sent: {}", e.message));
                    break;
                }
            }
            if !fresh.is_empty() {
                log(&format!("{} image(s) of the game sent to the site.", fresh.len()));
            }
        }
        Err(e) => log(&format!("The game's image index could not be read: {}", e)),
    }

    match site.send_live(&body) {
        Ok(_) => {
            let active = live.iter().filter(|t| t.row.running).count();
            log(&format!("Tournaments window sent to the site: {} running, {} about to start.", active, live.len() - active));
        }
        Err(e) => log(&format!("The Tournaments window was not sent: {}", e.message)),
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::arr;

    fn catalog() -> LiveCatalog {
        let mut c = LiveCatalog::default();
        c.tournaments.insert("1026:1".into(), TournamentInfo { name: "Ragnarök".into(), banner: "banner_ragnarok_clan_event_708x123".into(),
            icon: "map_ragnarok_widget_62".into(), clan_tiers: vec![(1, vec![10, 11]), (3, vec![12])], ..Default::default() });
        c.tournaments.insert("1026:2".into(), TournamentInfo { name: "Ragnarök".into(), banner: "banner_ragnarok_clan_event_708x123".into(),
            clan_tiers: vec![(1, vec![10])], ..Default::default() });
        c.tournaments.insert("1032:1".into(), TournamentInfo { name: "Trials of Olympus".into(), banner: "banner_multistage_main_708x123".into(),
            show_before: Some(1), ..Default::default() });
        c.tournaments.insert("1007:1".into(), TournamentInfo { name: "Doomsday".into(), banner: "banner_doomsday".into(), ..Default::default() });
        c.tournaments.insert("1005:1".into(), TournamentInfo { name: "".into(), banner: "x".into(), ..Default::default() });
        c.tournaments.insert("1034:2".into(), TournamentInfo { name: "Ariadne's Thread".into(), banner: "b".into(), hide_if_unavailable: true,
            join_condition: "HasLevel(10,0)&&IsAliasConditionMet(\"ab_pr_show_new_bank\")".into(), ..Default::default() });
        c.rewards.insert(10, vec![(31, 10_500_000)]);
        c.rewards.insert(11, vec![(31, 500_000), (20, 7)]);
        c.rewards.insert(12, vec![(20, 3)]);
        c.items.insert(31, ("Dragon Coins".into(), "fangs_coin_72".into()));
        c
    }

    const NOW: i128 = 1794066272;

    #[test]
    fn only_the_images_the_site_lacks_or_that_changed_are_read_and_sent() {
        let wanted = vec!["a".to_string(), "b".to_string(), "c".to_string(), "gone".to_string()];
        let known: Map<String, Json> = [("a".to_string(), json!("f1")), ("b".to_string(), json!("old"))].into_iter().collect();
        let files: HashMap<&str, &str> = [("a", "f1"), ("b", "f2"), ("c", "f3")].into_iter().collect();
        let file_of = |n: &str| files.get(n).map(|f| f.to_string());
        let mut read: Vec<String> = Vec::new();
        let mut png = |n: &str| {
            read.push(n.to_string());
            Ok((files[n].to_string(), b"\x89PNG".to_vec()))
        };
        let mut log = |_t: &str| {};
        let sent = images_to_send(&wanted, &known, &file_of, &mut png, &mut log);
        assert_eq!(vec!["b", "c"], sent.iter().map(|i| i["name"].as_str().unwrap()).collect::<Vec<_>>());
        assert_eq!("f2", sent[0]["file_id"], "the game changed it");
        assert_eq!(vec!["b".to_string(), "c".to_string()], read, "an image the site has is not downloaded again");
    }

    #[test]
    fn a_clans_name_is_asked_of_its_realm_and_a_new_clan_is_looked_for_in_the_known_realms() {
        use crate::tbcodec::Frame;
        use std::sync::Mutex;
        struct Realms {
            asked: Mutex<Vec<(String, Vec<i128>)>>,
        }
        impl GameApi for Realms {
            fn player_id(&self) -> Option<i128> {
                Some(1)
            }
            fn call(&self, route: i128, objects: Value) -> Res<Vec<Frame>> {
                self.call_at("home", route, objects)
            }
            fn call_at(&self, address: &str, route: i128, objects: Value) -> Res<Vec<Frame>> {
                let mut ids = Vec::new();
                for v in objects.at(0).and_then(|v| v.as_arr()).cloned().unwrap_or_default() {
                    ids.push(v.at(0).and_then(|i| i.py_int()).unwrap());
                }
                self.asked.lock().unwrap().push((address.to_string(), ids.clone()));
                let lives = |id: i128| match id { 10 => "realm-a", 20 => "realm-b", _ => "nowhere" };
                let rows: Vec<Value> = ids.iter().filter(|i| lives(**i) == address)
                    .map(|i| arr![arr![arr![*i], 99, format!("T{}", i), format!("Clan {}", i)], Value::Map(vec![])]).collect();
                Ok(vec![Frame { route: Some(route), seq: None, objects: vec![arr![Value::Arr(rows)]], offset: 0, length: 0, error: None, tail: vec![] }])
            }
            fn observed_realms(&self) -> HashMap<i128, String> {
                [(10, "realm-a".to_string())].into_iter().collect()
            }
        }
        let dir = tempfile::tempdir().unwrap();
        let mut d = ClanDirectory::load(&dir.path().join("clans.json"));
        d.realms.push("realm-b".into());
        let api = Realms { asked: Mutex::new(vec![]) };
        let names = d.resolve(&api, &[10, 20]);
        assert_eq!(Some(&("T10".to_string(), "Clan 10".to_string())), names.get(&10), "asked of the realm the page used");
        assert_eq!(Some(&("T20".to_string(), "Clan 20".to_string())), names.get(&20), "found in a known realm");
        d.save();
        let again = ClanDirectory::load(&dir.path().join("clans.json"));
        assert_eq!("realm-b", again.clans[&20].2, "remembered for the next reading");
    }

    #[test]
    fn the_schedule_says_what_runs_and_what_is_about_to_start() {
        let rows = vec![
            arr![arr![1026, 1], 56, 1, NOW + 91080, false, 1, 0, NOW - 81720],
            arr![arr![1007, 1], 448, 2, NOW + 4680, false, 1, 0, NOW - 427320],
            arr![arr![1007, 1], 449, 2, NOW + 30 * 86400, false, 1, 0, NOW],
            arr![arr![1005, 1], 6, 2, 4294967295i64, false, 1, 0, 1702760423],
            arr![arr![9999, 1], 1, 1, NOW + 10, false, 1, 0, NOW],
            arr![arr![1034, 2], 9, 2, NOW + 3600, false, 1, 0, NOW],
            arr![arr![1026, 2], 57, 2, NOW + 21 * 86400, false, 1, 0, NOW],
            arr![arr![1032, 1], 9, 2, NOW + 15 * 86400, false, 1, 0, NOW],
            arr![arr![1032, 1], 9, 2, NOW + 200 * 86400, false, 1, 0, NOW],
        ];
        let schedule = parse_schedule(&rows);
        assert_eq!(ScheduleRow { game_type: 1026, variant: 1, run: 56, running: true, at: NOW + 91080, changed: NOW - 81720 }, schedule[0]);
        let shown = listed(&schedule, &catalog(), NOW);
        assert_eq!(vec![("1026:1".to_string(), true), ("1007:1".to_string(), false), ("1032:1".to_string(), false), ("1026:2".to_string(), false)],
                   shown.iter().map(|r| (key_of(r.game_type, r.variant), r.running)).collect::<Vec<_>>(),
                   "running first; the announced ones and the clan tournaments weeks ahead are listed; an ordinary one a month away,                     anything beyond four months, a nameless one, an unknown type and one only an A/B group of the game can join are left out");
    }

    #[test]
    fn the_payload_carries_dates_places_rewards_and_the_images_to_send() {
        let c = catalog();
        let live = vec![LiveTournament {
            row: ScheduleRow { game_type: 1026, variant: 1, run: 56, running: true, at: NOW + 3600, changed: NOW - 3600 },
            my_points: Some(398890104), clan_points: Some(39510128034), clan_place: None,
            top_clans: vec![(502511173792, 968496815057)], members: vec![(661425220559, 1114715569, 1)],
        }];
        let clans: HashMap<i128, (String, String)> = [(502511173792, ("DeF".to_string(), "Darkness Falls".to_string()))].into_iter().collect();
        let names: HashMap<i128, String> = [(661425220559, "Bob".to_string())].into_iter().collect();
        let (body, images) = payload(Some(936302870583), &live, &c, &clans, &names, NOW);
        let t = &body["tournaments"][0];
        assert_eq!(("active", "Ragnarök"), (t["state"].as_str().unwrap(), t["name"].as_str().unwrap()));
        assert_eq!(json!(39510128034i64), t["clan_points"]);
        assert_eq!(Json::Null, t["clan_place"], "not among the best 100");
        assert_eq!("Darkness Falls", t["top_clans"][0]["name"]);
        assert_eq!("Bob", t["members"][0]["name"]);
        assert_eq!(json!({"from": 1, "to": 1, "items": [
            {"item_id": 31, "name": "Dragon Coins", "icon": "fangs_coin_72", "amount": 11000000},
            {"item_id": 20, "name": "", "icon": "", "amount": 7}]}), t["rewards"][0]);
        assert_eq!(json!({"from": 2, "to": 4, "items": [{"item_id": 20, "name": "", "icon": "", "amount": 3}]}), t["rewards"][1]);
        assert_eq!(vec!["banner_ragnarok_clan_event_708x123", "map_ragnarok_widget_62", "fangs_coin_72"], images);
        assert_eq!(Some("2026-11-07T16:44:32Z".to_string()), t["ends_at"].as_str().map(String::from));
    }
}
