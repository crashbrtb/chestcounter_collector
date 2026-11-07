//! Reads tournament results out of what the game answers.
//!
//! - Journal list: one entry per message, among them
//!   `[uuid, ended_at, n, "global_tournament_user_result", [{blob: [[kind, null, [type, variant], has_details]]}], ..., result_uid]`.
//! - Detail: `{result_uid: [{blob: [["global_tournament_user_result", [type, variant], n, [[1, 1, [[[player_id], points], ...], [], []]]]]}]}`.
//!   The ranking is already in order and includes players with 0 points.
//! - Other rankings (the Dark Omens, the Trials of Olympus): one tournament can have
//!   several, told apart by the entry's kind (and a statistic, "omens_damage"); their
//!   rows come as a list of `[[player_id], points]` or keyed by position.
//! - Player profiles (route 402): `[[player_id], account, name, ..., power at [10], ..., clan tag at [13]]`.
//!   Every profile seen is kept on disk (ProfileCache), since the game only sends a
//!   profile it has not sent since the page was loaded.
//!
//! The game never sends the tournament's name. The site remembers it per type.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use chrono::{DateTime, Utc};
use serde_json::{json, Map, Value as Json};

use crate::config::{read_json, write_json_atomic};
use crate::tbcodec::{py_json, Value};

pub const USER_RESULT: &str = "global_tournament_user_result";
pub const ITEM_GAIN_TRACKER: &str = "clan_members_item_gain_tracking_final_tracker_statistic_entry";
pub const POINTS_MINING: &str = "clan_points_mining_tournament_clan_statistics_entry";
pub const MULTISTAGE: &str = "multistage_tournament_clan_statistics_entry";
pub const RANKING_KINDS: [&str; 4] = [USER_RESULT, ITEM_GAIN_TRACKER, POINTS_MINING, MULTISTAGE];
pub const SAMPLE_CHARS: usize = 400;
pub const PROFILES_ROUTE: i128 = 402;
pub const UNKNOWN_NAME_PREFIX: &str = "id:";

pub fn is_uuid(text: &str) -> bool {
    let parts: Vec<&str> = text.split('-').collect();
    parts.len() == 5
        && [8, 4, 4, 4, 12].iter().zip(&parts).all(|(n, p)| p.len() == *n && p.bytes().all(|b| b.is_ascii_hexdigit()))
}

#[derive(Debug, Clone, PartialEq)]
pub struct Profile {
    pub player_id: i128,
    pub name: String,
    pub power: Option<i128>,
    pub clan_tag: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct RankingRow {
    pub position: usize,
    pub player_id: i128,
    pub points: i128,
    pub name: Option<String>,
    pub power: Option<i128>,
}

impl RankingRow {
    pub fn new(position: usize, player_id: i128, points: i128, name: Option<&str>) -> Self {
        RankingRow { position, player_id, points, name: name.map(String::from), power: None }
    }

    pub fn display_name(&self) -> String {
        match &self.name {
            Some(n) if !n.is_empty() => n.clone(),
            _ => format!("{}{}", UNKNOWN_NAME_PREFIX, self.player_id),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct Tournament {
    pub result_uid: String,
    pub tournament_key: Option<String>,
    pub ended_at: Option<i128>,
    pub has_details: bool,
    pub rows: Vec<RankingRow>,
    pub seen_at: f64,
    /// Which ranking of the tournament; "" for the classic result.
    pub ranking: String,
}

impl Tournament {
    pub fn new(uid: &str, key: Option<&str>, ended_at: Option<i128>) -> Self {
        Tournament { result_uid: uid.into(), tournament_key: key.map(String::from), ended_at, has_details: true,
                     ..Default::default() }
    }

    pub fn has_ranking(&self) -> bool {
        !self.rows.is_empty()
    }

    fn utc(&self) -> Option<DateTime<Utc>> {
        DateTime::from_timestamp(self.ended_at? as i64, 0)
    }

    pub fn ended_at_iso(&self) -> Option<String> {
        self.utc().map(|d| d.format("%Y-%m-%dT%H:%M:%SZ").to_string())
    }

    pub fn ended_at_text(&self) -> String {
        self.utc().map(|d| d.format("%d/%m/%Y %H:%M").to_string()).unwrap_or_default()
    }

    pub fn unnamed(&self) -> usize {
        self.rows.iter().filter(|r| r.name.as_deref().map(|n| n.is_empty()).unwrap_or(true)).count()
    }
}

/// Every list and map in a tree, parents before children.
pub fn iter_nodes(node: &Value) -> Vec<&Value> {
    let mut out = Vec::new();
    let mut stack = vec![(node, 0usize)];
    while let Some((current, level)) = stack.pop() {
        if level > 60 {
            continue;
        }
        match current {
            Value::Arr(items) => {
                out.push(current);
                for c in items {
                    if c.is_list() || c.is_map() {
                        stack.push((c, level + 1));
                    }
                }
            }
            Value::Map(pairs) => {
                out.push(current);
                for (_, c) in pairs {
                    if c.is_list() || c.is_map() {
                        stack.push((c, level + 1));
                    }
                }
            }
            _ => {}
        }
    }
    out
}

/// The messages inside a `{"blob": [...]}` wrapper, or [] when it is not one.
fn blob_messages(node: &Value) -> Vec<&Value> {
    if let Value::Map(pairs) = node {
        if pairs.len() == 1 && pairs[0].0.as_str() == Some("blob") {
            if let Some(a) = pairs[0].1.as_arr() {
                return a.iter().collect();
            }
        }
    }
    vec![]
}

fn type_key(value: &Value) -> Option<String> {
    let a = value.as_arr()?;
    if a.is_empty() || !a.iter().all(|v| v.strict_int().is_some()) {
        return None;
    }
    Some(a.iter().take(3).map(|v| v.strict_int().unwrap().to_string()).collect::<Vec<_>>().join(":"))
}

/// The first message inside the first blob of a list, if any.
fn first_blob(node: &Value) -> Option<&Value> {
    for item in node.as_arr()? {
        for message in blob_messages(item) {
            if message.is_list() {
                return Some(message);
            }
        }
    }
    None
}

/// Which ranking a Journal message is: "" for the classic result, None when it is not a ranking.
pub fn ranking_name(message: Option<&Value>) -> Option<String> {
    let m = message?.as_arr()?;
    let kind = m.first()?.as_str()?;
    if !RANKING_KINDS.contains(&kind) {
        return None;
    }
    if kind == USER_RESULT {
        return Some(String::new());
    }
    let statistic = m.get(2).and_then(|v| v.as_str()).filter(|s| !s.trim().is_empty());
    Some(match statistic {
        Some(s) => format!("{}:{}", kind, s),
        None => kind.to_string(),
    })
}

/// Short name of a ranking for the screen.
pub fn ranking_label(ranking: &str) -> String {
    if ranking.is_empty() {
        return String::new();
    }
    if let Some((_, statistic)) = ranking.split_once(':') {
        if !statistic.is_empty() {
            return statistic.to_string();
        }
    }
    ranking.strip_suffix("_entry").unwrap_or(ranking).to_string()
}

/// A short, readable sketch of a message, to report a shape nobody reads yet.
pub fn sample_of(message: &Value) -> String {
    let text = py_json(message, false);
    if text.chars().count() <= SAMPLE_CHARS {
        text
    } else {
        format!("{}...", text.chars().take(SAMPLE_CHARS).collect::<String>())
    }
}

/// A Journal list entry for a tournament ranking.
pub fn parse_journal_entry(entry: &Value) -> Option<Tournament> {
    let e = entry.as_arr()?;
    if e.len() < 6 {
        return None;
    }
    let kind = e[3].as_str()?;
    if !RANKING_KINDS.contains(&kind) || e[0].as_str().is_none() {
        return None;
    }
    let ended_at = e[1].py_int()?;
    let result_uid = e.last()?.as_str().filter(|s| is_uuid(s))?;
    let message = e.get(4).and_then(first_blob);
    let mut key = None;
    let mut has_details = true;
    let ranking = match ranking_name(message) {
        None => {
            if kind == USER_RESULT {
                String::new()
            } else {
                kind.to_string()
            }
        }
        Some(r) => {
            let m = message.unwrap().as_arr().unwrap();
            if m[0].as_str() == Some(USER_RESULT) {
                key = m.iter().skip(1).take(2).find_map(type_key);
                if let Some(d) = m.get(3).and_then(|v| v.py_int()) {
                    has_details = d != 0;
                }
            } else {
                key = m.get(1).and_then(type_key);
            }
            r
        }
    };
    Some(Tournament { result_uid: result_uid.into(), tournament_key: key, ended_at: Some(ended_at), has_details, ranking,
                      ..Default::default() })
}

/// `[[player_id], points]` -> (player_id, points).
fn pair(row: &Value) -> Option<(i128, i128)> {
    let r = row.as_arr()?;
    if r.len() != 2 {
        return None;
    }
    let player = r[0].as_arr()?.first()?.py_int()?;
    Some((player, r[1].strict_int()?))
}

/// A ranking as a list of rows, or as rows keyed by position ("1", "2", ...); [] otherwise.
fn pairs(node: &Value) -> Vec<(i128, i128)> {
    match node {
        Value::Arr(rows) if !rows.is_empty() => rows.iter().map(pair).collect::<Option<Vec<_>>>().unwrap_or_default(),
        Value::Map(m) if !m.is_empty() => {
            let mut keyed: Vec<(u64, &Value)> = Vec::new();
            for (k, v) in m {
                let Some(k) = k.as_str().filter(|k| !k.is_empty() && k.bytes().all(|b| b.is_ascii_digit())) else { return vec![] };
                keyed.push((k.parse().unwrap_or(u64::MAX), v));
            }
            keyed.sort_by_key(|(k, _)| *k);
            keyed.iter().map(|(_, v)| pair(v)).collect::<Option<Vec<_>>>().unwrap_or_default()
        }
        _ => vec![],
    }
}

/// A ranking detail -> (key, [(player_id, points)], ranking).
pub fn parse_ranking_message(message: Option<&Value>) -> Option<(Option<String>, Vec<(i128, i128)>, String)> {
    let ranking = ranking_name(message)?;
    let m = message?.as_arr()?;
    if m.len() < 3 {
        return None;
    }
    let key = type_key(&m[1]);
    let rest = Value::Arr(m[2..].to_vec());
    for node in iter_nodes(&rest) {
        let p = pairs(node);
        if !p.is_empty() {
            return Some((key, p, ranking));
        }
    }
    None
}

pub fn parse_profile_row(row: &Value) -> Option<Profile> {
    let r = row.as_arr()?;
    if r.len() <= 10 {
        return None;
    }
    let player_id = r[0].as_arr()?.first()?.py_int()?;
    let name = r[2].as_str().filter(|n| !n.trim().is_empty())?;
    Some(Profile {
        player_id,
        name: name.to_string(),
        power: r[10].strict_int(),
        clan_tag: r.get(13).and_then(|v| v.as_str()).map(String::from),
    })
}

// ------------------------------------------------------------ the clan's results

/// The card with the clans' ranking of most tournaments, whole in the Journal list:
/// `[kind, [type, variant], n, [[1, 1, [[[clan_id], points], ...top 100], [[places, [reward_id, ...]], ...], []]]]`.
pub const CLAN_RESULT: &str = "global_tournament_clan_result";
/// The results of a tournament in stages (the Trials of Olympus, the Dark Omens). Its
/// detail, asked like a ranking's: `[kind, [type, variant], true,
/// [[[hash, {blob: [[item_id, amount]]}], ...]] (what the clan received), {"1": [[clan_id], points], ...},
/// [[places, [reward_id, ...]], ...]]`.
pub const STAGES_RESULT: &str = "multistage_tournament_results_entry";
pub const CLAN_RESULT_KINDS: [&str; 2] = [CLAN_RESULT, STAGES_RESULT];

/// How the clan did in one tournament, from the card with the clans' ranking.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct ClanCard {
    pub entry_uid: String,
    pub result_uid: Option<String>,
    pub kind: String,
    pub tournament_key: Option<String>,
    pub ended_at: Option<i128>,
    /// The clans the card lists, best first: (clan id, points).
    pub ranking: Vec<(i128, i128)>,
    /// The reward tiers: how many places each covers and the rewards of those places.
    pub tiers: Vec<(usize, Vec<i128>)>,
    /// What the clan received, when the card says so: (item id, amount).
    pub received: Option<Vec<(i128, i128)>>,
    /// Whether everything the card has was read (a stages card needs its detail).
    pub complete: bool,
}

impl ClanCard {
    /// The clan's place and points, when the card lists it.
    pub fn position_of(&self, clan_id: i128) -> Option<(usize, i128)> {
        self.ranking.iter().position(|(c, _)| *c == clan_id).map(|i| (i + 1, self.ranking[i].1))
    }

    /// The rewards of a place, from the tiers.
    pub fn tier_of(&self, position: usize) -> Option<&Vec<i128>> {
        let mut last = 0;
        for (places, rewards) in &self.tiers {
            last += places;
            if position <= last {
                return Some(rewards);
            }
        }
        None
    }

    pub fn ended_at_iso(&self) -> Option<String> {
        DateTime::from_timestamp(self.ended_at? as i64, 0).map(|d| d.format("%Y-%m-%dT%H:%M:%SZ").to_string())
    }

    /// Every reward id of the tiers, for the catalogue lookup.
    pub fn reward_ids(&self) -> Vec<i128> {
        self.tiers.iter().flat_map(|(_, r)| r.iter().copied()).collect()
    }
}

/// `[[places, [reward_id, ...]], ...]`, or [] when it is not that.
fn parse_tiers(node: Option<&Value>) -> Vec<(usize, Vec<i128>)> {
    let Some(rows) = node.and_then(|n| n.as_arr()) else { return vec![] };
    rows.iter()
        .map(|r| {
            let r = r.as_arr()?;
            let places = usize::try_from(r.first()?.strict_int()?).ok()?;
            let rewards = r.get(1)?.as_arr()?.iter().map(|v| v.strict_int()).collect::<Option<Vec<i128>>>()?;
            Some((places, rewards))
        })
        .collect::<Option<Vec<_>>>()
        .unwrap_or_default()
}

/// What the clan received: every `[item_id, amount]` inside the blobs of the list, in order.
fn parse_received(node: Option<&Value>) -> Vec<(i128, i128)> {
    let mut out = Vec::new();
    let Some(Value::Arr(groups)) = node else { return out };
    for group in groups {
        let Some(items) = group.as_arr() else { continue };
        for item in items {
            let Some(pair) = item.as_arr() else { continue };
            for part in pair {
                for message in blob_messages(part) {
                    if let Some(m) = message.as_arr() {
                        if let (Some(id), Some(amount)) = (m.first().and_then(|v| v.strict_int()), m.get(1).and_then(|v| v.strict_int())) {
                            if m.len() == 2 {
                                out.push((id, amount));
                            }
                        }
                    }
                }
            }
        }
    }
    out
}

/// A Journal list entry with the clans' ranking (complete) or pointing to it (to be asked for).
pub fn parse_clan_entry(entry: &Value) -> Option<ClanCard> {
    let e = entry.as_arr()?;
    if e.len() < 6 {
        return None;
    }
    let kind = e[3].as_str()?;
    if !CLAN_RESULT_KINDS.contains(&kind) {
        return None;
    }
    let entry_uid = e[0].as_str()?.to_string();
    let ended_at = e[1].py_int();
    let result_uid = e.last().and_then(|v| v.as_str()).filter(|s| is_uuid(s)).map(String::from);
    let message = e.get(4).and_then(first_blob)?.as_arr()?;
    let mut card = ClanCard { entry_uid, result_uid, kind: kind.into(), tournament_key: message.get(1).and_then(type_key), ended_at,
                              ..Default::default() };
    if kind == CLAN_RESULT {
        let body = message.get(3)?.as_arr()?.first()?.as_arr()?;
        card.ranking = body.get(2).map(pairs).unwrap_or_default();
        card.tiers = parse_tiers(body.get(3));
        card.complete = true;
    }
    Some(card)
}

/// Fills a stages card from its detail. False when the answer is not its detail.
pub fn read_stages_detail(card: &mut ClanCard, answer: &Value) -> bool {
    let Some(uid) = card.result_uid.clone() else { return false };
    for node in iter_nodes(answer) {
        let Value::Map(entries) = node else { continue };
        for (key, value) in entries {
            if key.as_str() != Some(uid.as_str()) {
                continue;
            }
            let Some(m) = first_blob(value).and_then(|m| m.as_arr()) else { continue };
            if m.first().and_then(|k| k.as_str()) != Some(STAGES_RESULT) || m.len() < 6 {
                continue;
            }
            if card.tournament_key.is_none() {
                card.tournament_key = type_key(&m[1]);
            }
            card.received = Some(parse_received(m.get(3)));
            card.ranking = pairs(&m[4]);
            card.tiers = parse_tiers(m.get(5));
            card.complete = true;
            return true;
        }
    }
    false
}

/// The body of one result for POST /api/v1/clan-results. `names` names the items,
/// `rewards` says what each reward id hands out (for the cards that only list tiers).
pub fn clan_result_payload(card: &ClanCard, clan_id: Option<i128>, names: &HashMap<i128, String>,
                           rewards: &HashMap<i128, Vec<(i128, i128)>>) -> Json {
    let place = clan_id.and_then(|c| card.position_of(c));
    let received: Vec<(i128, i128)> = match &card.received {
        Some(r) => r.clone(),
        None => place
            .and_then(|(p, _)| card.tier_of(p))
            .map(|ids| {
                let mut sum: indexmap::IndexMap<i128, i128> = indexmap::IndexMap::new();
                for id in ids {
                    for (item, amount) in rewards.get(id).cloned().unwrap_or_default() {
                        *sum.entry(item).or_default() += amount;
                    }
                }
                sum.into_iter().collect()
            })
            .unwrap_or_default(),
    };
    json!({
        "entry_uid": card.entry_uid,
        "result_uid": card.result_uid,
        "kind": card.kind,
        "tournament_key": card.tournament_key.clone().unwrap_or_else(|| "0".into()),
        "ended_at": card.ended_at_iso(),
        "clan_id": clan_id.map(|c| c as i64),
        "position": place.map(|(p, _)| p),
        "points": place.map(|(_, pts)| pts as i64),
        "clans_listed": card.ranking.len(),
        "rewards": received.iter().map(|(item, amount)| json!({
            "item_id": *item as i64,
            "name": names.get(item).cloned().unwrap_or_default(),
            "amount": *amount as i64,
        })).collect::<Vec<_>>(),
    })
}

/// Every player profile ever seen, in a JSON file: player id -> name, power, clan tag.
pub struct ProfileCache {
    pub path: PathBuf,
}

impl ProfileCache {
    pub fn new(path: &Path) -> Self {
        ProfileCache { path: path.to_path_buf() }
    }

    pub fn load(&self) -> HashMap<i128, Profile> {
        let mut out = HashMap::new();
        let Some(data) = read_json(&self.path) else { return out };
        let Some(profiles) = data.get("profiles").and_then(|p| p.as_object()) else { return out };
        for (key, value) in profiles {
            let Ok(id) = key.parse::<i128>() else { continue };
            if let Some(name) = value.get("name").and_then(|n| n.as_str()).filter(|n| !n.trim().is_empty()) {
                out.insert(id, Profile {
                    player_id: id,
                    name: name.to_string(),
                    power: value.get("power").and_then(|p| p.as_i64()).map(|p| p as i128),
                    clan_tag: value.get("clan_tag").and_then(|t| t.as_str()).map(String::from),
                });
            }
        }
        out
    }

    pub fn save(&self, profiles: &HashMap<i128, Profile>) -> std::io::Result<()> {
        let mut map = Map::new();
        for p in profiles.values() {
            map.insert(p.player_id.to_string(), json!({"name": p.name, "power": p.power.map(|v| v as i64), "clan_tag": p.clan_tag}));
        }
        write_json_atomic(&self.path, &json!({"profiles": map}), 0)
    }
}

/// Accumulates what the game reveals and assembles whole tournaments.
pub struct JournalCollector {
    pub entries: indexmap::IndexMap<String, Tournament>,
    /// result uid -> (key, [(player_id, points)], seen_at, ranking)
    pub rankings: indexmap::IndexMap<String, (Option<String>, Vec<(i128, i128)>, f64, String)>,
    /// result uid -> (kind, sample), for details in a shape not read here yet
    pub unknown_details: HashMap<String, (String, String)>,
    pub profiles: HashMap<i128, Profile>,
    cache: Option<ProfileCache>,
    saved: HashMap<i128, Profile>,
    pub packets: usize,
}

impl JournalCollector {
    pub fn new(cache: Option<ProfileCache>) -> Self {
        let profiles = cache.as_ref().map(|c| c.load()).unwrap_or_default();
        JournalCollector { entries: Default::default(), rankings: Default::default(), unknown_details: HashMap::new(),
                           saved: profiles.clone(), profiles, cache, packets: 0 }
    }

    /// The objects of an answer to route 402.
    pub fn take_profiles(&mut self, objects: &Value) -> bool {
        self.packets += 1;
        let mut learned = false;
        for node in iter_nodes(objects) {
            let Some(rows) = node.as_arr() else { continue };
            for row in rows {
                if let Some(p) = parse_profile_row(row) {
                    self.profiles.insert(p.player_id, p);
                    learned = true;
                }
            }
        }
        learned
    }

    /// What came on the Journal's socket (blobs already expanded).
    pub fn take_stream(&mut self, stream: &Value, seen_at: f64) -> bool {
        self.packets += 1;
        let mut learned = false;
        for node in iter_nodes(stream) {
            match node {
                Value::Arr(_) => {
                    if let Some(mut entry) = parse_journal_entry(node) {
                        entry.seen_at = seen_at;
                        self.entries.insert(entry.result_uid.clone(), entry);
                        learned = true;
                    }
                }
                Value::Map(pairs) => {
                    for (key, value) in pairs {
                        let Some(key) = key.as_str().filter(|k| is_uuid(k)) else { continue };
                        let message = first_blob(value);
                        if let Some((k, p, ranking)) = parse_ranking_message(message) {
                            self.rankings.insert(key.to_string(), (k, p, seen_at, ranking));
                            learned = true;
                        } else if let Some(m) = message.and_then(|m| m.as_arr()) {
                            if let Some(kind) = m.first().and_then(|k| k.as_str()) {
                                // An answer in a shape nobody has taught this yet: worth saying so,
                                // with a sketch of it, which is enough to learn the shape from.
                                self.unknown_details.insert(key.to_string(), (kind.to_string(), sample_of(message.unwrap())));
                                learned = true;
                            }
                        }
                    }
                }
                _ => {}
            }
        }
        learned
    }

    /// Every tournament with a ranking, plus Journal entries still to be opened.
    pub fn tournaments(&self) -> Vec<Tournament> {
        let mut out: indexmap::IndexMap<String, Tournament> = indexmap::IndexMap::new();
        for (uid, e) in &self.entries {
            if e.has_details {
                out.insert(uid.clone(), Tournament { rows: vec![], ..e.clone() });
            }
        }
        for (uid, (key, pairs, seen_at, ranking)) in &self.rankings {
            let mut t = out.get(uid).cloned().unwrap_or_else(|| Tournament {
                result_uid: uid.clone(),
                tournament_key: key.clone(),
                has_details: true,
                seen_at: *seen_at,
                ranking: ranking.clone(),
                ..Default::default()
            });
            if t.tournament_key.is_none() {
                t.tournament_key = key.clone();
            }
            t.ranking = ranking.clone();
            t.seen_at = t.seen_at.max(*seen_at);
            t.rows = pairs
                .iter()
                .enumerate()
                .map(|(i, (p, points))| RankingRow {
                    position: i + 1,
                    player_id: *p,
                    points: *points,
                    name: self.profiles.get(p).map(|x| x.name.clone()),
                    power: self.profiles.get(p).and_then(|x| x.power),
                })
                .collect();
            out.insert(uid.clone(), t);
        }
        let mut list: Vec<Tournament> = out.into_values().collect();
        list.sort_by(|a, b| {
            let ka = (!a.has_ranking(), -(a.ended_at.map(|e| e as f64).filter(|e| *e != 0.0).unwrap_or(a.seen_at)));
            let kb = (!b.has_ranking(), -(b.ended_at.map(|e| e as f64).filter(|e| *e != 0.0).unwrap_or(b.seen_at)));
            ka.partial_cmp(&kb).unwrap_or(std::cmp::Ordering::Equal)
        });
        list
    }

    /// Players of a ranking whose name is still unknown, in ranking order.
    pub fn missing_profiles(&self, result_uid: &str) -> Vec<i128> {
        self.rankings
            .get(result_uid)
            .map(|r| r.1.iter().map(|(p, _)| *p).filter(|p| !self.profiles.contains_key(p)).collect())
            .unwrap_or_default()
    }

    /// Writes the profiles to the cache when any changed. True when written.
    pub fn save_profiles(&mut self) -> bool {
        let Some(cache) = &self.cache else { return false };
        if self.profiles == self.saved {
            return false;
        }
        if cache.save(&self.profiles).is_ok() {
            self.saved = self.profiles.clone();
            return true;
        }
        false
    }

    /// Forget the tournaments. Profiles stay: a player's name does not depend on the tournament.
    pub fn clear(&mut self) {
        self.entries.clear();
        self.rankings.clear();
        self.unknown_details.clear();
        self.packets = 0;
    }
}

/// What to look at before sending.
pub fn checks(t: &Tournament) -> Vec<(bool, String)> {
    let rows = &t.rows;
    let mut out = vec![(!rows.is_empty() && rows.len() <= 100, format!("{} players in the ranking (expected: 1 to 100)", rows.len()))];
    out.push((rows.windows(2).all(|w| w[0].points >= w[1].points), "points in decreasing order".into()));
    let mut ids: Vec<i128> = rows.iter().map(|r| r.player_id).collect();
    let n = ids.len();
    ids.sort();
    ids.dedup();
    out.push((ids.len() == n, "no repeated players".into()));
    let missing = t.unnamed();
    out.push((missing == 0, if missing == 0 { "every player has a name".into() } else {
        format!("{} player(s) without a name: the game did not send their profiles", missing)
    }));
    out.push((t.ended_at.is_some(), if t.ended_at.is_some() { "end date known".into() } else {
        "end date unknown: the Journal list was not seen in this session".into()
    }));
    out.push((t.tournament_key.is_some(), format!("tournament type: {}", t.tournament_key.as_deref().unwrap_or("unknown"))));
    out
}

/// The body of POST /api/v1/tournaments. `auto_publish` is only asked for when
/// every player has a name; the site checks again.
pub fn build_payload(t: &Tournament, name: &str, ended_at_iso: Option<&str>, client_version: &str, auto_publish: bool) -> Json {
    let name = name.trim();
    json!({
        "result_uid": t.result_uid,
        "tournament_key": t.tournament_key.clone().unwrap_or_else(|| "0".into()),
        "ranking": t.ranking,
        "name": if name.is_empty() { Json::Null } else { json!(name) },
        "ended_at": ended_at_iso,
        "capture_method": "packet",
        "client_version": client_version,
        "auto_publish": auto_publish && t.unnamed() == 0,
        "rows": t.rows.iter().map(|r| json!({
            "position": r.position, "name": crate::text::head(&r.display_name(), 60), "points": r.points as i64,
            "player_id": r.player_id as i64, "power": r.power.map(|p| p as i64)})).collect::<Vec<_>>(),
    })
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use crate::arr;
    use crate::tbcodec::{expand_blobs, pack};

    pub const RESULT_UID: &str = "eede1d1c-3f13-481a-819d-51fdcebb1e54";
    pub const ENDED_AT: i128 = 1789232426; // 2026-09-12 17:00:26 UTC

    pub fn players() -> Vec<(i128, &'static str, i128)> {
        vec![(300647954111, "Naughtius Maximus", 1703103642), (438086712193, "Lion", 873578580),
             (4307852203038, "Lion", 503945198), (622770520040, "BENAR", 0)]
    }

    fn bin(v: &Value) -> Value {
        Value::Bin(pack(v))
    }

    pub fn journal_list(has_details: i64, key: (i64, i64)) -> Value {
        let summary = bin(&arr![USER_RESULT, Value::Nil, arr![key.0, key.1], has_details]);
        let entry = arr!["3cd16678-5f7b-44f8-847e-0bee495480d9", ENDED_AT, 1876146181246441i64, USER_RESULT, arr![summary], true,
                         false, false, RESULT_UID];
        let noise = arr!["4285e683-78b8-4f1d-a6b9-8ab18d5f9af3", ENDED_AT - 50, 1, "treasury_buy",
                         arr![Value::Bin(vec![0x94, 1, 2, 3, 4])], true, false, false, Value::Nil];
        expand_blobs(&arr![arr![Value::Map(vec![(Value::str("733765024"), Value::Bin(vec![0x93, 0xcd, 0x5e, 0x02, 0x00, 0xa0]))])],
                           arr![arr![noise, entry]]])
    }

    pub fn journal_detail(players: &[(i128, &str, i128)]) -> Value {
        let ranking: Vec<Value> = players.iter().map(|(p, _, pts)| arr![arr![*p], *pts]).collect();
        let inner = bin(&arr![USER_RESULT, arr![1024, 1], 1, arr![arr![1, 1, Value::Arr(ranking), arr![], arr![]]]]);
        expand_blobs(&arr![arr![Value::Map(vec![(Value::str("733765024"), Value::Bin(vec![0x93, 6, 0, 0xa0]))])],
                           arr![Value::Map(vec![(Value::str(RESULT_UID), arr![inner, true])])]])
    }

    pub fn profiles(players: &[(i128, &str, i128)]) -> Value {
        let rows: Vec<Value> = players
            .iter()
            .enumerate()
            .map(|(i, (p, name, _))| arr![arr![*p], format!("tb:{}", p), *name, "BR", 3, 1, 2, 444, 45, 25,
                                          1_000_000 + i as i64, arr![4410931413044i64], 528144, "KOK"])
            .collect();
        arr![Value::Arr(rows)]
    }

    #[test]
    fn assembles_a_tournament_from_list_detail_and_profiles() {
        let mut c = JournalCollector::new(None);
        assert!(c.take_stream(&journal_list(1, (1024, 1)), 101.0));
        assert!(c.take_stream(&journal_detail(&players()), 102.0));
        assert!(c.take_profiles(&profiles(&players())));
        let ts = c.tournaments();
        assert_eq!(1, ts.len());
        let t = &ts[0];
        assert_eq!(RESULT_UID, t.result_uid);
        assert_eq!(Some("1024:1".to_string()), t.tournament_key);
        assert_eq!(Some("2026-09-12T17:00:26Z".to_string()), t.ended_at_iso());
        assert_eq!(vec!["Naughtius Maximus", "Lion", "Lion", "BENAR"], t.rows.iter().map(|r| r.name.clone().unwrap()).collect::<Vec<_>>());
        assert_eq!(vec![1, 2, 3, 4], t.rows.iter().map(|r| r.position).collect::<Vec<_>>());
        assert_eq!(0, t.rows[3].points);
        assert_eq!(0, t.unnamed());
        assert!(checks(t).iter().all(|(ok, _)| *ok));
        let payload = build_payload(t, " Rise of the Ancients ", t.ended_at_iso().as_deref(), "1.0.0", false);
        assert_eq!("Rise of the Ancients", payload["name"]);
        assert_eq!("1024:1", payload["tournament_key"]);
        assert_eq!(json!({"position": 3, "name": "Lion", "points": 503945198, "player_id": 4307852203038i64, "power": 1000002}),
                   payload["rows"][2]);
    }

    #[test]
    fn order_of_arrival_does_not_matter() {
        let mut c = JournalCollector::new(None);
        c.take_profiles(&profiles(&players()));
        c.take_stream(&journal_detail(&players()), 2.0);
        c.take_stream(&journal_list(1, (1024, 1)), 1.0);
        let t = &c.tournaments()[0];
        assert_eq!(Some("2026-09-12T17:00:26Z".to_string()), t.ended_at_iso());
        assert_eq!(4, t.rows.len());
    }

    #[test]
    fn detail_without_list_or_profiles_is_still_usable_but_flagged() {
        let mut c = JournalCollector::new(None);
        c.take_stream(&journal_detail(&players()), 2.0);
        let t = &c.tournaments()[0];
        assert_eq!(Some("1024:1".to_string()), t.tournament_key, "the detail carries the type too");
        assert_eq!(None, t.ended_at);
        assert_eq!(4, t.unnamed());
        assert_eq!("id:622770520040", t.rows[3].display_name());
        assert_eq!(2, checks(t).iter().filter(|(ok, _)| !ok).count());
    }

    #[test]
    fn list_entry_without_details_is_ignored_and_one_with_details_waits() {
        let mut c = JournalCollector::new(None);
        c.take_stream(&journal_list(0, (1024, 1)), 1.0);
        assert!(c.tournaments().is_empty());
        c.take_stream(&journal_list(1, (1024, 1)), 1.0);
        let ts = c.tournaments();
        assert_eq!(1, ts.len());
        assert!(!ts[0].has_ranking());
    }

    #[test]
    fn profiles_seen_once_name_a_ranking_reopened_in_a_later_session() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("perfis.json");
        let mut first = JournalCollector::new(Some(ProfileCache::new(&path)));
        first.take_stream(&journal_detail(&players()), 2.0);
        assert_eq!(players().iter().map(|p| p.0).collect::<Vec<_>>(), first.missing_profiles(RESULT_UID));
        first.take_profiles(&profiles(&players()[..3]));
        assert_eq!(vec![players()[3].0], first.missing_profiles(RESULT_UID));
        assert!(first.save_profiles());
        assert!(!first.save_profiles(), "nothing new to write");
        first.clear();
        assert_eq!(3, first.profiles.len());

        let mut later = JournalCollector::new(Some(ProfileCache::new(&path)));
        later.take_stream(&journal_detail(&players()), 3.0);
        let t = &later.tournaments()[0];
        assert_eq!(vec![Some("Naughtius Maximus".to_string()), Some("Lion".into()), Some("Lion".into()), None],
                   t.rows.iter().map(|r| r.name.clone()).collect::<Vec<_>>());
        assert_eq!(Some(1_000_000), t.rows[0].power);
        assert_eq!(1, t.unnamed());
    }

    fn entry(uid: &str, message: Value, result_uid: &str) -> Value {
        let kind = message.at(0).unwrap().clone();
        arr![uid, ENDED_AT, 1, kind, arr![bin(&message)], true, false, false, result_uid]
    }

    #[test]
    fn the_dark_omens_rankings_are_read_and_told_apart() {
        let (damage_uid, essence_uid) = ("efa2484f-e61d-41e2-9c7f-25fac121b1df", "f553b1cf-3cde-4f2d-9ea3-2e9d0e96c8d1");
        let p = players();
        let listing = expand_blobs(&arr![arr![Value::Map(vec![])], arr![arr![
            entry("d4f94d3a-d11d-41e2-9c7f-25fac121b1df", arr![ITEM_GAIN_TRACKER, arr![1033, 1], "omens_damage"], damage_uid),
            entry("3980ca1e-e66d-4aa1-8a6e-c9f5ae6d2ca3", arr![POINTS_MINING, arr![1033, 1]], essence_uid)]]]);
        let damage = bin(&arr![ITEM_GAIN_TRACKER, arr![1033, 1], "omens_damage",
                               arr![arr![arr![arr![p[0].0], 16331921], arr![arr![p[1].0], 12880697]]]]);
        let essence = bin(&arr![POINTS_MINING, arr![1033, 1], Value::Map(vec![
            (Value::str("2"), arr![arr![p[0].0], 3122000]), (Value::str("1"), arr![arr![p[2].0], 3122001])])]);
        let details = expand_blobs(&arr![arr![Value::Map(vec![])], arr![Value::Map(vec![
            (Value::str(damage_uid), arr![damage, true]), (Value::str(essence_uid), arr![essence, true])])]]);
        let mut c = JournalCollector::new(None);
        c.take_stream(&listing, 1.0);
        c.take_stream(&details, 2.0);
        c.take_profiles(&profiles(&p));
        let by: HashMap<String, Tournament> = c.tournaments().into_iter().map(|t| (t.result_uid.clone(), t)).collect();
        let omens = &by[damage_uid];
        assert_eq!((Some("1033:1".to_string()), format!("{}:omens_damage", ITEM_GAIN_TRACKER)),
                   (omens.tournament_key.clone(), omens.ranking.clone()));
        assert_eq!(vec![("Naughtius Maximus".to_string(), 16331921), ("Lion".to_string(), 12880697)],
                   omens.rows.iter().map(|r| (r.name.clone().unwrap(), r.points)).collect::<Vec<_>>());
        assert_eq!(Some(ENDED_AT), omens.ended_at);
        let e = &by[essence_uid];
        assert_eq!(POINTS_MINING, e.ranking);
        assert_eq!(vec![p[2].0, p[0].0], e.rows.iter().map(|r| r.player_id).collect::<Vec<_>>());
        assert_eq!(POINTS_MINING, build_payload(e, "Omen Essence", None, "t", false)["ranking"]);
        assert_eq!("", build_payload(&Tournament::new("x", None, None), "", None, "t", false)["ranking"]);
    }

    #[test]
    fn the_trials_of_olympus_ranking_is_read() {
        let uid = "38509dc8-d8fa-4614-a4c0-fdc03336f6cf";
        let rows: Vec<(Value, Value)> =
            players().iter().enumerate().map(|(i, (p, _, _))| (Value::str((i + 1).to_string()), arr![arr![*p], 900 - i as i64])).collect();
        let detail = bin(&arr![MULTISTAGE, arr![1040, 1], Value::Map(rows)]);
        let mut c = JournalCollector::new(None);
        c.take_stream(&expand_blobs(&arr![arr![Value::Map(vec![])], arr![Value::Map(vec![(Value::str(uid), arr![detail, true])])]]), 4.0);
        c.take_profiles(&profiles(&players()));
        let t = &c.tournaments()[0];
        assert_eq!((Some("1040:1".to_string()), MULTISTAGE.to_string()), (t.tournament_key.clone(), t.ranking.clone()));
        assert_eq!(vec![900, 899, 898, 897], t.rows.iter().map(|r| r.points).collect::<Vec<_>>());
        assert_eq!(Some("Naughtius Maximus".to_string()), t.rows[0].name);
        assert!(c.unknown_details.is_empty());
    }

    #[test]
    fn an_unknown_detail_is_remembered_with_a_sketch_of_its_shape() {
        let uid = "11111111-2222-4333-8444-555555555555";
        let detail = bin(&arr!["some_new_tournament_entry", arr![1050, 2], arr![arr![arr![arr![42], 7]]]]);
        let mut c = JournalCollector::new(None);
        c.take_stream(&expand_blobs(&arr![arr![Value::Map(vec![])], arr![Value::Map(vec![(Value::str(uid), arr![detail, true])])]]), 5.0);
        let (kind, sample) = &c.unknown_details[uid];
        assert_eq!("some_new_tournament_entry", kind);
        assert!(sample.contains("[1050, 2]"));
        assert!(sample.len() <= SAMPLE_CHARS + 3);
        assert!(c.tournaments().is_empty());
    }

    pub const OUR_CLAN: i128 = 936302870583;

    /// A clans' ranking card as the Journal list carries it (shape of 2026-11-07, Rise of the Ancients).
    pub fn clan_card_entry(entry_uid: &str, ended_at: i128, key: (i64, i64), clans: &[(i128, i128)]) -> Value {
        let ranking: Vec<Value> = clans.iter().map(|(c, p)| arr![arr![*c], *p]).collect();
        let tiers = arr![arr![1, arr![2004282, 2004290]], arr![1, arr![2004283]], arr![1, arr![2004284]], arr![3, arr![2004285]],
                         arr![4, arr![2004286]]];
        let message = arr![CLAN_RESULT, arr![key.0, key.1], 1, arr![arr![1, 1, Value::Arr(ranking), tiers, arr![]]]];
        arr![entry_uid, ended_at, ended_at * 1000, CLAN_RESULT, arr![bin(&message)], true, false, false, Value::Nil]
    }

    /// A stages tournament's results entry, and its detail (shape of 2026-11-07, Trials of Olympus).
    pub fn stages_entry(entry_uid: &str, result_uid: &str, ended_at: i128) -> Value {
        arr![entry_uid, ended_at, ended_at * 1000, STAGES_RESULT, arr![bin(&arr![STAGES_RESULT, arr![1032, 1]])], true, false, false,
             result_uid]
    }

    pub fn stages_detail_message(our_place: usize) -> Value {
        let hash = 987815103534332535i64;
        let received = arr![arr![arr![hash, bin(&arr![702059, 1680])], arr![hash, bin(&arr![20, 500000000])],
                                 arr![hash, bin(&arr![31, 4000000])], arr![hash, bin(&arr![701216, 1320])]]];
        let mut clans = vec![(523986010945i128, 48291725i64), (210453398529, 35238057)];
        clans.insert(our_place - 1, (OUR_CLAN, 43043175));
        let ranking: Vec<(Value, Value)> =
            clans.iter().enumerate().map(|(i, (c, p))| (Value::str((i + 1).to_string()), arr![arr![*c], *p])).collect();
        arr![STAGES_RESULT, arr![1032, 1], true, received, Value::Map(ranking),
             arr![arr![1, arr![3000110, 3000610]], arr![1, arr![3000620]], arr![1, arr![3000630]]]]
    }

    #[test]
    fn the_clans_ranking_card_gives_the_clans_place_and_its_rewards() {
        let mut clans: Vec<(i128, i128)> = (1..=100).map(|i| (1_000_000 + i, 1000 - i)).collect();
        clans[4] = (OUR_CLAN, 995);
        let card = parse_clan_entry(&expand_blobs(&clan_card_entry("c7943f88-2856-4c50-9ec6-aa99929223c8", ENDED_AT, (1024, 1), &clans)))
            .unwrap();
        assert!(card.complete, "the list carries the whole card");
        assert_eq!(None, card.result_uid);
        assert_eq!(Some("1024:1".to_string()), card.tournament_key);
        assert_eq!(100, card.ranking.len());
        assert_eq!(Some((5, 995)), card.position_of(OUR_CLAN));
        assert_eq!(Some(&vec![2004285]), card.tier_of(5), "places 4 to 6 share the fourth tier");
        assert_eq!(Some(&vec![2004282, 2004290]), card.tier_of(1));
        assert_eq!(None, card.tier_of(11), "past the last tier there is nothing");

        let names: HashMap<i128, String> = [(31, "Dragon Coins".to_string())].into_iter().collect();
        let rewards: HashMap<i128, Vec<(i128, i128)>> = [(2004285, vec![(31, 2_000_000)])].into_iter().collect();
        let payload = clan_result_payload(&card, Some(OUR_CLAN), &names, &rewards);
        assert_eq!(json!(5), payload["position"]);
        assert_eq!(json!(995), payload["points"]);
        assert_eq!(json!([{"item_id": 31, "name": "Dragon Coins", "amount": 2000000}]), payload["rewards"]);
        assert_eq!("2026-09-12T17:00:26Z", payload["ended_at"]);

        let outside = clan_result_payload(&card, Some(42), &names, &rewards);
        assert_eq!((Json::Null, Json::Null, json!([]), json!(100)),
                   (outside["position"].clone(), outside["points"].clone(), outside["rewards"].clone(), outside["clans_listed"].clone()));
    }

    #[test]
    fn the_results_of_a_tournament_in_stages_say_what_the_clan_received() {
        let entry = expand_blobs(&stages_entry("c16a5e54-15d4-447c-8e9d-d05486620e29", RESULT_UID, ENDED_AT));
        let mut card = parse_clan_entry(&entry).unwrap();
        assert!(!card.complete, "its detail has to be asked for");
        assert_eq!(Some(RESULT_UID.to_string()), card.result_uid);
        let answer = expand_blobs(&arr![arr![Value::Map(vec![(Value::str(RESULT_UID), arr![bin(&stages_detail_message(2)), true])])]]);
        assert!(read_stages_detail(&mut card, &answer));
        assert!(card.complete);
        assert_eq!(Some((2, 43043175)), card.position_of(OUR_CLAN));
        assert_eq!(Some(vec![(702059, 1680), (20, 500000000), (31, 4000000), (701216, 1320)]), card.received);
        let names: HashMap<i128, String> = [(702059, "Olympus Elite Chest".to_string())].into_iter().collect();
        let payload = clan_result_payload(&card, Some(OUR_CLAN), &names, &HashMap::new());
        assert_eq!(json!(2), payload["position"]);
        assert_eq!(json!({"item_id": 702059, "name": "Olympus Elite Chest", "amount": 1680}), payload["rewards"][0]);
        assert_eq!(json!(4), json!(payload["rewards"].as_array().unwrap().len()));
        assert_eq!(STAGES_RESULT, payload["kind"]);
        assert_eq!(RESULT_UID, payload["result_uid"]);

        let mut other = parse_clan_entry(&entry).unwrap();
        assert!(!read_stages_detail(&mut other, &expand_blobs(&journal_detail(&players()))), "a ranking is not its detail");
    }

    #[test]
    fn a_broken_cache_file_is_an_empty_cache() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("perfis.json");
        std::fs::write(&path, "{not json").unwrap();
        assert!(ProfileCache::new(&path).load().is_empty());
    }
}
