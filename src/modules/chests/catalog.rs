//! The game's own catalogue of clan chests (Table 8 of the client static data).
//!
//! On route 15000 each gift carries a numeric `chest_type` and no text. The client
//! names it from Table 8 of `_no_duplicates` (the vector indices of its name at
//! `row[3]` and of its clan chest source at `row[14]`) and the `.mo` of en_US.
//!
//! Consulting the CDN as little as possible: known types come from the database;
//! an unknown one is looked for in the decoded table cached on disk
//! (`config/module_data/chests/chest_types.json`); only when it is missing there
//! too is the CDN asked whether a newer build exists, and downloaded if so.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

use serde_json::{json, Map, Value as Json};

use crate::config::{read_json, write_json_atomic};
use crate::static_data::{self, text_at};
use crate::tbcodec::Value;
use crate::text::normalize_text;

pub const CHEST_TABLE_INDEX: usize = 8;
const FILES: [&str; 3] = ["no_duplicates", "strings", "vectors"];

pub type Types = HashMap<i128, (String, String)>;

pub fn resolve_vector(index: &Value, vectors: &[Value], strings: &[Value], translations: &HashMap<String, String>) -> String {
    let Some(i) = index.strict_int().and_then(|i| usize::try_from(i).ok()) else { return String::new() };
    let Some(vec) = vectors.get(i).and_then(|v| v.as_arr()) else { return String::new() };
    if vec.is_empty() {
        return String::new();
    }
    let key = text_at(strings, &vec[0]);
    if key.is_empty() {
        return String::new();
    }
    let mut text = translations.get(&key).cloned().unwrap_or(key);
    let mut i = 1;
    while i + 1 < vec.len() {
        let placeholder = text_at(strings, &vec[i]);
        let val_key = text_at(strings, &vec[i + 1]);
        let val = if val_key.is_empty() { String::new() } else { translations.get(&val_key).cloned().unwrap_or(val_key) };
        if !placeholder.is_empty() {
            text = text.replace(&placeholder, &val);
        }
        i += 2;
    }
    normalize_text(&text)
}

/// `{game_type_id: (name, source)}` from the raw static and `.mo` bytes.
pub fn decode_chest_table(strings_raw: &[u8], vectors_raw: &[u8], nodup_raw: &[u8], mo_raw: &[u8]) -> Result<Types, String> {
    let translations = static_data::translations(mo_raw)?;
    let strings = static_data::first_list(strings_raw)?;
    let vectors = static_data::first_list(vectors_raw)?;
    let mut tables = static_data::tables(nodup_raw, &[CHEST_TABLE_INDEX])?;
    let table = tables.remove(&CHEST_TABLE_INDEX).unwrap();
    let Value::Map(rows) = table else { return Err("Table 8 in _no_duplicates is not a map".into()) };
    let mut catalog = Types::new();
    for (k, row) in rows {
        let Some(r) = row.as_arr() else { continue };
        if r.len() <= 3 {
            continue;
        }
        let Some(tid) = k.as_str().and_then(|s| s.trim().parse::<i128>().ok()) else { continue };
        let name = resolve_vector(&r[3], &vectors, &strings, &translations);
        let source = if r.len() > 14 { resolve_vector(&r[14], &vectors, &strings, &translations) } else { String::new() };
        if !name.is_empty() && !source.is_empty() {
            catalog.insert(tid, (name, source));
        }
    }
    Ok(catalog)
}

pub type VersionFn = Box<dyn Fn() -> Result<Map<String, Json>, String> + Send>;
pub type CatalogFn = Box<dyn Fn(&Map<String, Json>) -> Result<Types, String> + Send>;

pub fn fetch_cdn_catalog(version: &Map<String, Json>) -> Result<Types, String> {
    let get = |k: &str| version.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
    let mo = static_data::fetch_translation(&get(static_data::LOCALE))?;
    let vectors = static_data::fetch_static(&get("vectors"))?;
    let strings = static_data::fetch_static(&get("strings"))?;
    let nodup = static_data::fetch_static(&get("no_duplicates"))?;
    decode_chest_table(&strings, &vectors, &nodup, &mo)
}

/// Lazy, disk-cached resolver for `game_type_id -> (name, source)`.
pub struct ChestCatalog {
    pub cache_path: PathBuf,
    loaded: bool,
    pub version: Map<String, Json>,
    pub types: Types,
    checked: Option<Map<String, Json>>,
    fetch_version: VersionFn,
    fetch_catalog: CatalogFn,
}

impl ChestCatalog {
    pub fn new(cache_path: &Path) -> Self {
        Self::with_fetchers(cache_path, Box::new(|| static_data::fetch_cdn_version(&FILES)), Box::new(fetch_cdn_catalog))
    }

    pub fn with_fetchers(cache_path: &Path, fetch_version: VersionFn, fetch_catalog: CatalogFn) -> Self {
        ChestCatalog { cache_path: cache_path.to_path_buf(), loaded: false, version: Map::new(), types: Types::new(),
                       checked: None, fetch_version, fetch_catalog }
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
            crate::warn!("Could not read local chest catalog cache {}", self.cache_path.display());
            return;
        };
        self.version = data["version"].as_object().cloned().unwrap_or_default();
        if let Some(types) = data["types"].as_object() {
            for (k, v) in types {
                let (Ok(id), Some(pair)) = (k.parse::<i128>(), v.as_array()) else { continue };
                if pair.len() == 2 {
                    let (n, s) = (pair[0].as_str().unwrap_or(""), pair[1].as_str().unwrap_or(""));
                    if !n.is_empty() && !s.is_empty() {
                        self.types.insert(id, (normalize_text(n), normalize_text(s)));
                    }
                }
            }
        }
    }

    pub fn save(&self) {
        let mut ids: Vec<&i128> = self.types.keys().collect();
        ids.sort();
        let types: Map<String, Json> = ids.into_iter().map(|id| (id.to_string(), json!([self.types[id].0, self.types[id].1]))).collect();
        let payload = json!({"version": self.version, "types": types});
        if let Err(e) = write_json_atomic(&self.cache_path, &payload, 0) {
            crate::warn!("Could not write local chest catalog cache {}: {}", self.cache_path.display(), e);
        }
    }

    pub fn set_loaded(&mut self) {
        self.loaded = true;
    }

    fn refresh(&mut self, missing: &[i128]) {
        let cdn = match (self.fetch_version)() {
            Ok(v) => v,
            Err(e) => {
                crate::warn!("Could not check CDN chest catalog version for types {:?}: {}", missing, e);
                if self.types.is_empty() {
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
        crate::info!("New chest type(s) {:?} not in local cache; updating chest catalog from CDN ({})...", missing, build);
        match (self.fetch_catalog)(&cdn) {
            Ok(fresh) if !fresh.is_empty() => {
                self.version = cdn;
                self.types = fresh;
                self.save();
                crate::info!("Chest catalog updated from CDN: {} chest types cached locally.", self.types.len());
            }
            Ok(_) => {}
            Err(e) => {
                crate::warn!("Could not download chest catalog from CDN: {}", e);
                if self.types.is_empty() {
                    self.fallback_desktop();
                }
            }
        }
    }

    fn fallback_desktop(&mut self) {
        let Some(files) = static_data::desktop_files(true) else { return };
        match decode_chest_table(&files.strings, files.vectors.as_deref().unwrap_or(&[]), &files.nodup, &files.mo) {
            Ok(types) if !types.is_empty() => {
                self.version = files.version;
                self.types = types;
                self.save();
                crate::info!("Chest catalog initialized from local Desktop cache: {} chest types.", self.types.len());
            }
            Ok(_) => {}
            Err(e) => crate::warn!("Could not load local Desktop chest catalog fallback: {}", e),
        }
    }

    /// Resolves `type_ids`; the CDN is asked only when one is absent from the local cache.
    pub fn lookup(&mut self, type_ids: &[i128]) -> Types {
        let mut wanted: Vec<i128> = type_ids.to_vec();
        wanted.sort();
        wanted.dedup();
        if wanted.is_empty() {
            return Types::new();
        }
        self.load();
        let missing: Vec<i128> = wanted.iter().copied().filter(|t| !self.types.contains_key(t)).collect();
        if !missing.is_empty() {
            self.refresh(&missing);
        }
        wanted.into_iter().filter_map(|t| self.types.get(&t).map(|v| (t, v.clone()))).collect()
    }
}

fn shared() -> &'static Mutex<ChestCatalog> {
    static CATALOG: OnceLock<Mutex<ChestCatalog>> = OnceLock::new();
    CATALOG.get_or_init(|| Mutex::new(ChestCatalog::new(&crate::paths::module_data_dir().join("chests").join("chest_types.json"))))
}

/// Resolves unknown chest type IDs using the shared local/CDN catalogue.
pub fn lookup_chest_types(type_ids: &[i128]) -> Types {
    shared().lock().unwrap().lookup(type_ids)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::arr;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    #[test]
    fn local_cache_is_used_without_consulting_cdn_until_a_type_is_missing() {
        let dir = tempfile::tempdir().unwrap();
        let cache = dir.path().join("chest_types.json");
        let mut first = ChestCatalog::new(&cache);
        first.set_loaded();
        first.version = json!({"no_duplicates": "v1", "strings": "s1", "vectors": "vec1", "en_US": "m1"}).as_object().unwrap().clone();
        first.types.insert(5784, ("Chest of the Cursed".into(), "Level 35 epic Crypt".into()));
        first.save();

        let versions = Arc::new(AtomicUsize::new(0));
        let downloads = Arc::new(AtomicUsize::new(0));
        let (v, d) = (versions.clone(), downloads.clone());
        let mut cat = ChestCatalog::with_fetchers(
            &cache,
            Box::new(move || {
                v.fetch_add(1, Ordering::SeqCst);
                Ok(json!({"no_duplicates": "v2", "strings": "s2", "vectors": "vec2", "en_US": "m2"}).as_object().unwrap().clone())
            }),
            Box::new(move |_| {
                d.fetch_add(1, Ordering::SeqCst);
                Ok(Types::from([(5784, ("Chest of the Cursed".into(), "Level 35 epic Crypt".into())),
                                (5789, ("Braided Chest".into(), "Level 35 epic Crypt".into()))]))
            }),
        );
        assert_eq!(Types::from([(5784, ("Chest of the Cursed".into(), "Level 35 epic Crypt".into()))]), cat.lookup(&[5784]));
        assert_eq!(0, versions.load(Ordering::SeqCst));
        assert_eq!(Types::from([(5789, ("Braided Chest".into(), "Level 35 epic Crypt".into()))]), cat.lookup(&[5789]));
        assert_eq!((1, 1), (versions.load(Ordering::SeqCst), downloads.load(Ordering::SeqCst)));
        // The fresh table is on disk for the next run.
        let again = ChestCatalog::new(&cache).lookup(&[5789]);
        assert_eq!(1, again.len());
    }

    #[test]
    fn table_8_is_decoded_with_its_placeholders() {
        let strings = crate::tbcodec::pack(&arr![arr!["", "item.name.bone", "item.src.crypt", "%value%", "25"]]);
        let vectors = crate::tbcodec::pack(&arr![arr![arr![1], arr![2, 3, 4]]]);
        let mut row = vec![Value::Int(0); 15];
        row[3] = Value::Int(0);
        row[14] = Value::Int(1);
        let table8 = Value::Map(vec![(Value::Int(5512), Value::Arr(row))]);
        let mut tables: Vec<Value> = (0..8).map(|_| arr![]).collect();
        tables.push(table8);
        let nodup = crate::tbcodec::pack(&Value::Arr(tables));
        let mo = crate::mo::build_mo(&[("item.name.bone", "Bone Chest"), ("item.src.crypt", "Level %value% Crypt")]);
        let types = decode_chest_table(&strings, &vectors, &nodup, &mo).unwrap();
        assert_eq!(("Bone Chest".to_string(), "Level 25 Crypt".to_string()), types[&5512]);
    }
}
