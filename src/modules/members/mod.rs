//! Members module - who is in the clan, with each one's player id and power.
//!
//! For each profile it asks the game, over the network, for the clan's record (id
//! and rank of every member) and the members' profiles (name, power, level, place).
//! One profile per clan is enough: it runs where it was turned on.
//!
//! A profile that has a clan site (the address and token the tournament module
//! uses) also sends the list there: every player in it becomes an active member on
//! the site and every member not in it inactive. That is why only a complete list
//! is ever sent. The files are written first.

pub mod roster;
pub mod store;

use chrono::{DateTime, Local, Utc};
use serde_json::{json, Map, Value as Json};

use crate::arr;
use crate::config::{Field, FieldType, ProfileConfig};
use crate::context::Window;
use crate::error::{Error, Res};
use crate::link::GameApi;
use crate::modules::events::api::{normalize_site_url, SiteClient};
use crate::modules::{opt_bool, setting_text, Module, ModuleResult};
use crate::tbcodec::Value;
use roster::{Roster, RosterCollector, CLAN_RECORD_WHOLE, CLAN_ROUTE, PROFILES_PER_REQUEST, PROFILES_ROUTE};

pub const KEY: &str = "members";
pub const TITLE: &str = "Clan members";
pub const DESCRIPTION: &str = "Asks the game for the clan's members and records every one with their player id, power, rank \
                               and level, as the game sends them. Runs on the profiles it is turned on for (Accounts and \
                               profiles > Modules); the list is saved to config/module_data/members and sent to the \
                               profile's clan site, where it decides who is an active member.";
pub const CLIENT_VERSION: &str = "1.0.0";
/// The clan is asked for this many times before giving up on a list that came short.
const ATTEMPTS: usize = 2;
/// Where the profile's clan site is kept: with the tournament module.
const SITE_MODULE_KEY: &str = "events";

pub fn settings() -> Vec<Field> {
    vec![Field::new("send_to_site", FieldType::Bool, "Send the list to the clan site", json!(true),
                    "On a profile that has a clan site address and token (Accounts and profiles > Modules), the list is \
                     sent there: its players become active members on the site and every other member inactive. Off, \
                     the list is only saved to the files.")]
}

pub fn profile_fields() -> Vec<Field> {
    vec![Field::new("enabled", FieldType::Bool, "Read clan members", json!(false),
                    "Reads the members of this profile's clan. One profile per clan is enough.")]
}

pub fn configured_for(profile: &ProfileConfig) -> bool {
    opt_bool(&profile.module_settings(KEY), "enabled", false)
}

/// The profile's clan site and its token; ('', '') unless both are filled in.
pub fn site_of(profile: &ProfileConfig) -> (String, String) {
    let values = profile.module_settings(SITE_MODULE_KEY);
    let site = normalize_site_url(&setting_text(&values, "site_url"));
    let token = setting_text(&values, "api_token").trim().to_string();
    if !site.is_empty() && !token.is_empty() {
        (site, token)
    } else {
        (String::new(), String::new())
    }
}

/// The body of POST /api/v1/members. `read_at` travels in UTC.
pub fn build_payload(roster: &Roster, read_at: DateTime<Local>, client_version: &str) -> Json {
    let clan = roster.clan.as_ref().unwrap();
    json!({
        "clan": {"id": clan.clan_id as i64, "tag": clan.tag, "name": clan.name},
        "read_at": read_at.with_timezone(&Utc).format("%Y-%m-%dT%H:%M:%SZ").to_string(),
        "client_version": client_version,
        "members": roster.members.iter().map(|m| json!({
            "player_id": m.player_id as i64, "name": m.name, "power": m.power.map(|p| p as i64),
            "rank": m.rank.map(|r| r as i64), "level": m.level.map(|l| l as i64)})).collect::<Vec<_>>(),
    })
}

/// The clan's record, then the profiles of its members.
pub fn read_roster(link: &dyn GameApi, collector: &mut RosterCollector) -> Res<Roster> {
    let Some(clan_id) = crate::link::clan_id(link)? else { return Ok(Roster::default()) };
    let mut roster = Roster::default();
    for _ in 0..ATTEMPTS {
        collector.clear();
        for frame in link.call(CLAN_ROUTE, arr![arr![arr![arr![clan_id], CLAN_RECORD_WHOLE]]])? {
            collector.feed(CLAN_ROUTE, &frame.objects);
        }
        roster = collector.roster();
        if roster.clan.is_none() {
            continue;
        }
        for chunk in roster.missing.chunks(PROFILES_PER_REQUEST) {
            let ids = Value::Arr(chunk.iter().map(|p| arr![*p]).collect());
            for frame in link.call(PROFILES_ROUTE, arr![ids])? {
                collector.feed(PROFILES_ROUTE, &frame.objects);
            }
        }
        roster = collector.roster();
        if roster.complete() {
            break;
        }
    }
    Ok(roster)
}

pub struct MembersModule {
    pub options: Map<String, Json>,
}

pub type SendFn<'a> = &'a dyn Fn(&str, &str, &Json) -> Result<Json, crate::modules::events::api::ApiError>;

fn failure(profile: &ProfileConfig, reason: &str, retry: bool) -> ModuleResult {
    crate::error!("[{}] Clan members not read: {}", profile.label(), reason);
    ModuleResult::failed(profile, reason, retry)
}

impl MembersModule {
    /// The run, with where to save and how to reach the site handed in.
    pub fn run_with(&self, link: Option<Box<dyn GameApi>>, profile: &ProfileConfig, data_dir: &std::path::Path,
                    send: SendFn) -> Res<ModuleResult> {
        crate::info!("[{}] Reading the clan's members", profile.label());
        let Some(link) = link else { return Ok(failure(profile, "could not talk to the game", true)) };
        let mut collector = RosterCollector::default();
        let roster = match read_roster(link.as_ref(), &mut collector) {
            Ok(r) => r,
            Err(Error::GameLink(e)) => {
                return Ok(failure(profile, &format!("the game did not give the clan's members: {}", e), true))
            }
            Err(e) => return Err(e),
        };
        drop(link);
        let Some(clan) = roster.clan.clone() else {
            return Ok(failure(profile, "the game gave no clan's membership for this profile", true));
        };
        if !roster.complete() {
            return Ok(failure(profile, &format!("the game sent {} of {} members", roster.members.len(), roster.expected()), true));
        }
        let read_at = Local::now();
        let (json_path, csv_path) = store::save_roster(&roster, read_at.naive_local(), &profile.label(), data_dir)
            .map_err(|e| Error::Other(format!("could not save the members' list: {}", e)))?;
        let power: i128 = roster.members.iter().map(|m| m.power.unwrap_or(0)).sum();
        crate::info!("[{}] [{}] {}: {} members, total power {}. Saved to {}", profile.label(), clan.tag, clan.name,
                     roster.members.len(), thousands(power), csv_path.display());
        let mut result = ModuleResult::ok(profile)
            .with("clan_id", json!(clan.clan_id as i64))
            .with("clan_tag", json!(clan.tag))
            .with("members", json!(roster.members.len()))
            .with("file", json!(json_path.to_string_lossy()))
            .with("sent", json!(false));

        let (site, token) = site_of(profile);
        if !opt_bool(&self.options, "send_to_site", true) {
            return Ok(result);
        }
        if site.is_empty() {
            crate::info!("[{}] No clan site address and token on this profile: the list was saved to the files only.",
                         profile.label());
            return Ok(result);
        }
        let answer = match send(&site, &token, &build_payload(&roster, read_at, CLIENT_VERSION)) {
            Ok(a) => a,
            Err(e) if e.status == 404 => {
                crate::warn!("[{}] The site {} does not receive member lists yet (update the site): the list was saved \
                              to the files only.", profile.label(), site);
                return Ok(result);
            }
            Err(e) => {
                return Ok(failure(profile, &format!("the list was read and saved, but the site {} did not take it: {}", site,
                                                    e.describe()), false))
            }
        };
        let changes = answer.get("members").cloned().unwrap_or(json!({}));
        let n = |k: &str| changes.get(k).and_then(|v| v.as_i64()).unwrap_or(0);
        let active = answer.get("active_members").map(|v| v.to_string()).unwrap_or_else(|| "?".into());
        crate::info!("[{}] Sent to {}: {} active members there now ({} new, {} back to active, {} made inactive, {} renamed, \
                      {} chests moved to a new name).", profile.label(), site, active, n("created"), n("activated"),
                     n("deactivated"), n("renamed"), n("chests_moved"));
        if let Some(ambiguous) = changes.get("ambiguous").and_then(|a| a.as_array()).filter(|a| !a.is_empty()) {
            let names: Vec<String> = ambiguous.iter().map(|v| v.as_str().map(String::from).unwrap_or_else(|| v.to_string())).collect();
            crate::warn!("[{}] Not matched on the site, because more than one member without a game id has the same name: \
                          {}. Set the game id of the right one on the site.", profile.label(), names.join(", "));
        }
        result.extra.insert("sent".into(), json!(true));
        result.extra.insert("site".into(), json!(site));
        result.extra.insert("active_on_site".into(), answer.get("active_members").cloned().unwrap_or(Json::Null));
        result.extra.insert("deactivated".into(), json!(n("deactivated")));
        Ok(result)
    }
}

pub fn thousands(n: i128) -> String {
    let s = n.abs().to_string();
    let mut out = String::new();
    for (i, c) in s.chars().enumerate() {
        if i > 0 && (s.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    if n < 0 {
        format!("-{}", out)
    } else {
        out
    }
}

impl Module for MembersModule {
    fn key(&self) -> &str {
        KEY
    }
    fn title(&self) -> &str {
        TITLE
    }
    fn options(&self) -> &Map<String, Json> {
        &self.options
    }
    fn configured_for(&self, profile: &ProfileConfig) -> bool {
        configured_for(profile)
    }
    fn run_for_profile(&self, window: &dyn Window, profile: &ProfileConfig) -> Res<ModuleResult> {
        let send = |site: &str, token: &str, payload: &Json| SiteClient::new(site, token, CLIENT_VERSION).send_members(payload);
        self.run_with(window.game_link(), profile, &crate::paths::module_data_dir().join("members"), &send)
    }
}

#[cfg(test)]
mod tests;
