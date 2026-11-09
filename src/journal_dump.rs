//! `tbcollector journal-dump`: writes the tournament cards of the Journal as the game
//! sends them, to learn the shape of the cards nothing reads yet.
//!
//! It logs into the account and opens the profile as a run does, walks the Journal
//! back `--days` days (14 by default) and writes, for every card about a tournament
//! or a clan, the entry and the detail the game answers for it. It sends nothing to
//! any site and changes nothing in the game.

use std::collections::BTreeMap;
use std::io::Write;

use crate::cli::{EXIT_FAILED, EXIT_MISCONFIGURED, EXIT_OK};
use crate::config::ConfigManager;
use crate::context::{RealWindows, Windows};
use crate::link::JournalSource;
use crate::modules::events::journal::{self, is_uuid};
use crate::tbcodec::{expand_blobs, py_json, Value};

fn interesting(kind: &str) -> bool {
    ["tournament", "clan", "multistage", "sabotage", "greathunt", "union", "ragnarok", "doom", "olymp"]
        .iter()
        .any(|w| kind.contains(w))
}

pub fn run(config: &ConfigManager, opts: &std::collections::HashMap<String, String>) -> i32 {
    let mut config = config.clone();
    let accounts = config.enabled_accounts();
    let wanted = opts.get("account").map(|a| a.to_lowercase());
    let Some(account) = accounts.iter().find(|a| wanted.as_ref().map(|w| a.name.to_lowercase().contains(w)).unwrap_or(true)).cloned()
    else {
        crate::error!("No enabled account matches.");
        return EXIT_MISCONFIGURED;
    };
    let wanted_profile = opts.get("profile").map(|p| p.to_lowercase());
    let Some(profile) = account
        .active_profiles()
        .into_iter()
        .find(|p| wanted_profile.as_ref().map(|w| p.name.to_lowercase().contains(w)).unwrap_or(true))
    else {
        crate::error!("Account '{}' has no matching profile.", account.name);
        return EXIT_MISCONFIGURED;
    };
    let days: i64 = opts.get("days").and_then(|d| d.parse().ok()).unwrap_or(14);
    let max_pages: usize = opts.get("pages").and_then(|d| d.parse().ok()).unwrap_or(80);

    let windows = RealWindows::new(&mut config);
    let window = windows.for_account(&account);
    let result = (|| -> crate::error::Res<String> {
        window.start_browser()?;
        if window.login_enabled() {
            window.enter_account(&account)?;
        } else {
            window.navigate()?;
        }
        window.wait_for_load();
        if !window.switch_to(&profile)? {
            return Err(crate::error::Error::Session(format!("could not open profile '{}'", profile.name)));
        }
        let mut feed = window.journal().ok_or_else(crate::link::not_started)?;
        walk(feed.as_mut(), days, max_pages)
    })();
    windows.close();
    match result {
        Ok(path) => {
            crate::info!("Journal written to {}", path);
            EXIT_OK
        }
        Err(e) => {
            crate::error!("journal-dump: {}", e);
            EXIT_FAILED
        }
    }
}

fn walk(feed: &mut dyn JournalSource, days: i64, max_pages: usize) -> crate::error::Res<String> {
    let dir = crate::paths::base_dir().join("execution_logs");
    let _ = std::fs::create_dir_all(&dir);
    let path = dir.join(format!("journal_dump_{}.txt", chrono::Local::now().format("%Y%m%d_%H%M%S")));
    let mut out = std::fs::File::create(&path).map_err(|e| crate::error::Error::Other(e.to_string()))?;
    let oldest = chrono::Utc::now().timestamp() as i128 - days as i128 * 86400;
    let mut kinds: BTreeMap<String, usize> = BTreeMap::new();
    let mut clan_cards: Vec<journal::ClanCard> = Vec::new();
    let clan_id = feed.clan_id().unwrap_or(None);
    let mut cursor: Option<Value> = None;
    'pages: for page_number in 0..max_pages {
        crate::cancel::check()?;
        let page = feed.journal_page(cursor.as_ref())?;
        if page.is_empty() {
            break;
        }
        crate::info!("Journal page {}: {} entries", page_number + 1, page.len());
        for raw in &page {
            let entry = expand_blobs(raw);
            let kind = entry.at(3).and_then(|k| k.as_str()).unwrap_or("?").to_string();
            let at = entry.at(1).and_then(|v| v.py_int()).unwrap_or(0);
            if at < oldest {
                break 'pages;
            }
            *kinds.entry(kind.clone()).or_default() += 1;
            if !interesting(&kind) {
                continue;
            }
            let _ = writeln!(out, "=== ENTRY {} at {} ===\n{}", kind, at, py_json(&entry, false));
            let mut card = journal::parse_clan_entry(&entry);
            let entry_uid = entry.at(0).and_then(|v| v.as_str()).unwrap_or("").to_string();
            let result_uid = entry.as_arr().and_then(|a| a.last()).and_then(|v| v.as_str()).filter(|s| is_uuid(s));
            if let Some(result_uid) = result_uid {
                match feed.ranking(&entry_uid, result_uid) {
                    Ok(answer) => {
                        let answer = expand_blobs(&Value::Arr(answer));
                        if let Some(c) = card.as_mut() {
                            journal::read_stages_detail(c, &answer);
                        }
                        let _ = writeln!(out, "--- DETAIL {} ---\n{}", result_uid, py_json(&answer, false));
                    }
                    Err(e) => {
                        let _ = writeln!(out, "--- DETAIL {} failed: {} ---", result_uid, e);
                    }
                }
                crate::cancel::sleep(0.4)?;
            }
            if let Some(c) = card {
                clan_cards.push(c);
            }
        }
        cursor = page.last().and_then(|e| e.at(2)).cloned();
        crate::cancel::sleep(0.4)?;
    }
    let _ = writeln!(out, "=== CLAN RESULTS (clan {:?}) ===", clan_id);
    let mut items: Vec<i128> = Vec::new();
    let mut rewards: Vec<i128> = Vec::new();
    for c in &clan_cards {
        items.extend(c.received.iter().flatten().map(|(i, _)| *i));
        rewards.extend(c.reward_ids());
    }
    let (names, gives) = crate::modules::events::items::lookup(&items, &rewards);
    for c in &clan_cards {
        let p = journal::clan_result_payload(c, clan_id, &names, &gives);
        crate::info!("Clan result: {}", p);
        let _ = writeln!(out, "{}", p);
    }
    let _ = writeln!(out, "=== KINDS ===");
    for (k, n) in &kinds {
        let _ = writeln!(out, "{}\t{}", n, k);
    }
    Ok(path.display().to_string())
}

/// `tbcollector live-tournaments`: what the Tournaments window shows, read over the network
/// and written to a file, nothing sent anywhere.
pub fn live(config: &ConfigManager, opts: &std::collections::HashMap<String, String>) -> i32 {
    let mut config = config.clone();
    let accounts = config.enabled_accounts();
    let wanted = opts.get("account").map(|a| a.to_lowercase());
    let Some(account) = accounts.iter().find(|a| wanted.as_ref().map(|w| a.name.to_lowercase().contains(w)).unwrap_or(true)).cloned()
    else {
        crate::error!("No enabled account matches.");
        return EXIT_MISCONFIGURED;
    };
    let windows = RealWindows::new(&mut config);
    let window = windows.for_account(&account);
    let result = (|| -> crate::error::Res<String> {
        window.start_browser()?;
        if window.login_enabled() {
            window.enter_account(&account)?;
        } else {
            window.navigate()?;
        }
        window.wait_for_load();
        let data_dir = crate::paths::module_data_dir().join("events");
        let catalog = crate::modules::events::live::LiveCatalog::current(&crate::modules::events::live::cache_path(&data_dir))
            .map_err(crate::error::Error::Other)?;
        let api = window.game_link().ok_or_else(crate::link::not_started)?;
        let now = chrono::Utc::now().timestamp() as i128;
        let schedule: Vec<serde_json::Value> = crate::modules::events::live::raw_schedule(api.as_ref())?
            .iter()
            .map(|r| {
                let key = r.at(0).map(|k| format!("{}:{}", k.at(0).and_then(|v| v.py_int()).unwrap_or(0), k.at(1).and_then(|v| v.py_int()).unwrap_or(0)));
                let name = key.as_ref().and_then(|k| catalog.tournaments.get(k)).map(|t| t.name.clone()).unwrap_or_default();
                serde_json::json!({"key": key, "name": name, "row": format!("{:?}", r)})
            })
            .collect();
        let (clan, live, clans, names) = crate::modules::events::live::read(api.as_ref(), &catalog, now)?;
        let (body, images) = crate::modules::events::live::payload(clan, &live, &catalog, &clans, &names, now);
        let dir = crate::paths::base_dir().join("execution_logs");
        let path = dir.join(format!("live_tournaments_{}.json", chrono::Local::now().format("%Y%m%d_%H%M%S")));
        let text = serde_json::to_string_pretty(&serde_json::json!({"schedule": schedule, "payload": body, "images": images})).unwrap_or_default();
        std::fs::write(&path, text).map_err(|e| crate::error::Error::Other(e.to_string()))?;
        Ok(path.display().to_string())
    })();
    windows.close();
    match result {
        Ok(path) => {
            crate::info!("Written to {}", path);
            EXIT_OK
        }
        Err(e) => {
            crate::error!("live-tournaments: {}", e);
            EXIT_FAILED
        }
    }
}
