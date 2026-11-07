//! Where a roster is kept, under config/module_data/members/: one pair of files
//! per clan, replaced at each reading - a JSON for programs and a CSV that opens in
//! a spreadsheet.

use std::path::{Path, PathBuf};

use chrono::NaiveDateTime;
use serde_json::{json, Value as Json};

use super::roster::{rank_name, Clan, Roster};
use crate::config::write_json_atomic;

pub const CSV_COLUMNS: [&str; 9] = ["player_id", "name", "power", "rank", "rank_name", "level", "kingdom", "x", "y"];

/// (json, csv) for a clan: 'LPL' with id 936302870583 -> 'LPL_936302870583.json'.
pub fn roster_paths(clan: &Clan, data_dir: &Path) -> (PathBuf, PathBuf) {
    let re = regex::Regex::new(r"[^A-Za-z0-9-]+").unwrap();
    let tag = re.replace_all(&clan.tag, "_").trim_matches('_').to_string();
    let tag = if tag.is_empty() { "clan".to_string() } else { tag };
    let base = format!("{}_{}", tag, clan.clan_id);
    (data_dir.join(format!("{}.json", base)), data_dir.join(format!("{}.csv", base)))
}

fn rows(roster: &Roster) -> Vec<Json> {
    roster
        .members
        .iter()
        .map(|m| {
            let (k, x, y) = match m.coordinates {
                Some((k, x, y)) => (json!(k as i64), json!(x as i64), json!(y as i64)),
                None => (Json::Null, Json::Null, Json::Null),
            };
            json!({"player_id": m.player_id as i64, "name": m.name, "power": m.power.map(|p| p as i64),
                   "rank": m.rank.map(|r| r as i64), "rank_name": rank_name(m.rank), "level": m.level.map(|l| l as i64),
                   "kingdom": k, "x": x, "y": y})
        })
        .collect()
}

fn csv_field(v: &Json) -> String {
    let text = match v {
        Json::Null => String::new(),
        Json::String(s) => s.clone(),
        other => other.to_string(),
    };
    if text.contains([',', '"', '\n', '\r']) {
        format!("\"{}\"", text.replace('"', "\"\""))
    } else {
        text
    }
}

/// Writes the roster of a clan and returns (json path, csv path).
pub fn save_roster(roster: &Roster, read_at: NaiveDateTime, read_by: &str, data_dir: &Path) -> std::io::Result<(PathBuf, PathBuf)> {
    let clan = roster.clan.as_ref().ok_or_else(|| std::io::Error::other("a roster without its clan cannot be saved"))?;
    let (json_path, csv_path) = roster_paths(clan, data_dir);
    std::fs::create_dir_all(data_dir)?;
    let rows = rows(roster);
    let data = json!({"clan": {"id": clan.clan_id as i64, "tag": clan.tag, "name": clan.name},
                      "read_at": read_at.format("%Y-%m-%dT%H:%M:%S").to_string(), "read_by": read_by, "members": rows});
    write_json_atomic(&json_path, &data, 1)?;

    // The BOM: without it a spreadsheet reads the accents of the names as cp1252.
    let mut text = String::from("\u{feff}");
    text.push_str(&CSV_COLUMNS.join(","));
    text.push_str("\r\n");
    for row in &rows {
        let fields: Vec<String> = CSV_COLUMNS.iter().map(|c| csv_field(&row[*c])).collect();
        text.push_str(&fields.join(","));
        text.push_str("\r\n");
    }
    let mut temporary = csv_path.as_os_str().to_owned();
    temporary.push(".tmp");
    std::fs::write(&temporary, text)?;
    std::fs::rename(&temporary, &csv_path)?;
    Ok((json_path, csv_path))
}
