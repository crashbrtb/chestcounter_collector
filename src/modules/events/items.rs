//! The names of the game's items and what each reward id hands out, from the client
//! static data: Table 8 of `_no_duplicates` names every item (`row[3]`, a vector of
//! the `.mo`), Table 12 says what a reward gives (`row[4][0]`, `{item_id: amount}`).
//!
//! The clans' results only carry ids. What was looked up is kept on disk
//! (`config/module_data/events/game_items.json`) and the CDN is asked only for an id
//! not there, once per run.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

use serde_json::{json, Map, Value as Json};

use crate::config::{read_json, write_json_atomic};
use crate::modules::chests::catalog::resolve_vector;
use crate::static_data;
use crate::tbcodec::Value;

pub const ITEM_TABLE_INDEX: usize = 8;
pub const REWARD_TABLE_INDEX: usize = 12;

pub type Names = HashMap<i128, String>;
pub type Rewards = HashMap<i128, Vec<(i128, i128)>>;

fn key_id(k: &Value) -> Option<i128> {
    match k {
        Value::Str(s) => s.trim().parse().ok(),
        other => other.strict_int(),
    }
}

/// Every item name of Table 8, and what the wanted reward ids of Table 12 give.
pub fn decode(strings_raw: &[u8], vectors_raw: &[u8], nodup_raw: &[u8], mo_raw: &[u8], wanted: &[i128])
              -> Result<(Names, Rewards), String> {
    let translations = static_data::translations(mo_raw)?;
    let strings = static_data::first_list(strings_raw)?;
    let vectors = static_data::first_list(vectors_raw)?;
    let mut tables = static_data::tables(nodup_raw, &[ITEM_TABLE_INDEX, REWARD_TABLE_INDEX])?;
    let mut names = Names::new();
    if let Some(Value::Map(rows)) = tables.remove(&ITEM_TABLE_INDEX) {
        for (k, row) in rows {
            let (Some(id), Some(r)) = (key_id(&k), row.as_arr()) else { continue };
            if let Some(field) = r.get(3) {
                let name = resolve_vector(field, &vectors, &strings, &translations);
                if !name.is_empty() {
                    names.insert(id, name);
                }
            }
        }
    }
    let mut rewards = Rewards::new();
    if let Some(Value::Map(rows)) = tables.remove(&REWARD_TABLE_INDEX) {
        for (k, row) in rows {
            let Some(id) = key_id(&k).filter(|id| wanted.contains(id)) else { continue };
            let Some(Value::Map(items)) = row.at(4).and_then(|r| r.at(0)) else { continue };
            let gives: Vec<(i128, i128)> =
                items.iter().filter_map(|(item, amount)| Some((key_id(item)?, amount.strict_int()?))).collect();
            rewards.insert(id, gives);
        }
    }
    Ok((names, rewards))
}

pub type FetchFn = Box<dyn Fn(&[i128]) -> Result<(Names, Rewards), String> + Send>;

fn fetch_cdn(wanted: &[i128]) -> Result<(Names, Rewards), String> {
    let version = static_data::fetch_cdn_version(&["no_duplicates", "strings", "vectors"])?;
    let get = |k: &str| version.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
    decode(&static_data::fetch_static(&get("strings"))?, &static_data::fetch_static(&get("vectors"))?,
           &static_data::fetch_static(&get("no_duplicates"))?, &static_data::fetch_translation(&get(static_data::LOCALE))?, wanted)
}

pub struct ItemCatalog {
    pub path: PathBuf,
    loaded: bool,
    pub names: Names,
    pub rewards: Rewards,
    fetched: bool,
    fetch: FetchFn,
}

impl ItemCatalog {
    pub fn new(path: &Path) -> Self {
        Self::with_fetch(path, Box::new(fetch_cdn))
    }

    pub fn with_fetch(path: &Path, fetch: FetchFn) -> Self {
        ItemCatalog { path: path.to_path_buf(), loaded: false, names: Names::new(), rewards: Rewards::new(), fetched: false, fetch }
    }

    fn load(&mut self) {
        if self.loaded {
            return;
        }
        self.loaded = true;
        let Some(data) = read_json(&self.path) else { return };
        for (k, v) in data["items"].as_object().cloned().unwrap_or_default() {
            if let (Ok(id), Some(name)) = (k.parse::<i128>(), v.as_str()) {
                self.names.insert(id, name.to_string());
            }
        }
        for (k, v) in data["rewards"].as_object().cloned().unwrap_or_default() {
            let Ok(id) = k.parse::<i128>() else { continue };
            let gives = v
                .as_array()
                .map(|a| a.iter().filter_map(|p| Some((p.get(0)?.as_i64()? as i128, p.get(1)?.as_i64()? as i128))).collect())
                .unwrap_or_default();
            self.rewards.insert(id, gives);
        }
    }

    fn save(&self) {
        let items: Map<String, Json> = self.names.iter().map(|(k, v)| (k.to_string(), json!(v))).collect();
        let rewards: Map<String, Json> = self
            .rewards
            .iter()
            .map(|(k, v)| (k.to_string(), json!(v.iter().map(|(i, a)| json!([*i as i64, *a as i64])).collect::<Vec<_>>())))
            .collect();
        if let Err(e) = write_json_atomic(&self.path, &json!({"items": items, "rewards": rewards}), 0) {
            crate::warn!("Could not write the item catalogue {}: {}", self.path.display(), e);
        }
    }

    /// The names of `items` and what the `rewards` ids give, as far as the game's data says.
    pub fn lookup(&mut self, items: &[i128], rewards: &[i128]) -> (Names, Rewards) {
        self.load();
        let missing_reward: Vec<i128> = rewards.iter().copied().filter(|r| !self.rewards.contains_key(r)).collect();
        let reward_items: Vec<i128> = rewards.iter().filter_map(|r| self.rewards.get(r)).flatten().map(|(i, _)| *i).collect();
        let missing_name = items.iter().chain(reward_items.iter()).any(|i| !self.names.contains_key(i));
        if (missing_name || !missing_reward.is_empty()) && !self.fetched {
            self.fetched = true;
            match (self.fetch)(&missing_reward) {
                Ok((names, found)) => {
                    self.names.extend(names);
                    for r in &missing_reward {
                        // Kept even when the game has no such reward, so it is not looked for again.
                        self.rewards.insert(*r, found.get(r).cloned().unwrap_or_default());
                    }
                    self.save();
                }
                Err(e) => crate::warn!("Could not read the game's item names from the CDN: {}", e),
            }
        }
        let gives: Rewards = rewards.iter().filter_map(|r| Some((*r, self.rewards.get(r)?.clone()))).collect();
        let all: Vec<i128> = items.iter().copied().chain(gives.values().flatten().map(|(i, _)| *i)).collect();
        let names: Names = all.iter().filter_map(|i| Some((*i, self.names.get(i)?.clone()))).collect();
        (names, gives)
    }
}

fn shared() -> &'static Mutex<ItemCatalog> {
    static CATALOG: OnceLock<Mutex<ItemCatalog>> = OnceLock::new();
    CATALOG.get_or_init(|| Mutex::new(ItemCatalog::new(&crate::paths::module_data_dir().join("events").join("game_items.json"))))
}

pub fn lookup(items: &[i128], rewards: &[i128]) -> (Names, Rewards) {
    shared().lock().unwrap().lookup(items, rewards)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    #[test]
    fn what_was_looked_up_once_comes_from_the_disk_afterwards() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("game_items.json");
        let calls = Arc::new(AtomicUsize::new(0));
        let c = calls.clone();
        let fetch: FetchFn = Box::new(move |wanted: &[i128]| {
            c.fetch_add(1, Ordering::SeqCst);
            let mut rewards = Rewards::new();
            if wanted.contains(&2004282) {
                rewards.insert(2004282, vec![(31, 10_500_000)]);
            }
            Ok(([(31, "Dragon Coins".to_string()), (702059, "Olympus Elite Chest".to_string())].into_iter().collect(), rewards))
        });
        let mut catalog = ItemCatalog::with_fetch(&path, fetch);
        let (names, gives) = catalog.lookup(&[702059], &[2004282, 999]);
        assert_eq!(Some(&"Olympus Elite Chest".to_string()), names.get(&702059));
        assert_eq!(Some(&"Dragon Coins".to_string()), names.get(&31), "the items of a reward are named too");
        assert_eq!(Some(&vec![(31, 10_500_000)]), gives.get(&2004282));
        assert_eq!(Some(&vec![]), gives.get(&999), "a reward the game does not have is remembered as empty");

        let fail: FetchFn = Box::new(|_: &[i128]| Err("offline".into()));
        let mut later = ItemCatalog::with_fetch(&path, fail);
        let (names, gives) = later.lookup(&[31], &[2004282]);
        assert_eq!("Dragon Coins", names[&31]);
        assert_eq!(vec![(31, 10_500_000)], gives[&2004282]);
        assert_eq!(1, calls.load(Ordering::SeqCst));
    }
}
