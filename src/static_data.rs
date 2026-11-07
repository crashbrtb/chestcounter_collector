//! The game client's static data, as its CDN (or the Desktop client's cache) holds it.
//!
//! `_strings`, `_vectors` and `_no_duplicates` are encoded in the game's
//! little-endian MessagePack; the English texts come from the gettext `.mo` file of
//! `en_US`. The public version manifest says which build is current, so the CDN is
//! only downloaded from when the game publishes a new build.

use std::collections::HashMap;
use std::path::PathBuf;
use std::time::Duration;

use serde_json::{Map, Value as Json};

use crate::tbcodec::{self, DecodeError, LEUnpacker, Value};

pub const VERSIONS_URL: &str = "https://versions.totalbattle.com/config";
pub const STATIC_CDN_URL: &str = "https://assets-client-static.totalbattle.dev";
pub const TRANSLATIONS_CDN_URL: &str = "https://assets-translations.totalbattle.dev";
pub const LOCALE: &str = "en_US";
const HTTP_TIMEOUT: Duration = Duration::from_secs(30);

pub fn fetch(url: &str) -> Result<Vec<u8>, String> {
    crate::http::fetch_bytes(url, HTTP_TIMEOUT)
}

fn json_of(bytes: &[u8]) -> Result<Json, String> {
    serde_json::from_slice(bytes).map_err(|e| e.to_string())
}

/// The production hashes of the static files named in `files`, plus the `.mo` of en_US.
pub fn fetch_cdn_version(files: &[&str]) -> Result<Map<String, Json>, String> {
    let cfg = json_of(&fetch(VERSIONS_URL)?)?;
    let cfg = &cfg["result"]["config"];
    let catalog_version = cfg["client_translations_catalog_version"].as_str().ok_or("no translations catalog version")?;
    let production = &cfg["client_static_version"]["production"];
    let catalog = json_of(&fetch(&format!("{}/{}", TRANSLATIONS_CDN_URL, catalog_version))?)?;
    let md5 = catalog["files"]
        .as_array()
        .and_then(|f| f.iter().find(|i| i["name"] == LOCALE))
        .and_then(|i| i["md5"].as_str())
        .ok_or("no en_US translation in the catalog")?;
    let mut version = Map::new();
    for file in files {
        let hash = production[*file].as_str().ok_or_else(|| format!("no '{}' in the static version", file))?;
        version.insert(file.to_string(), Json::String(hash.to_string()));
    }
    version.insert(LOCALE.into(), Json::String(md5.to_string()));
    Ok(version)
}

pub fn fetch_static(hash: &str) -> Result<Vec<u8>, String> {
    fetch(&format!("{}/{}", STATIC_CDN_URL, hash))
}

pub fn fetch_translation(md5: &str) -> Result<Vec<u8>, String> {
    fetch(&format!("{}/{}", TRANSLATIONS_CDN_URL, md5))
}

pub fn translations(mo_raw: &[u8]) -> Result<HashMap<String, String>, String> {
    crate::mo::parse_mo(&tbcodec::decompress(mo_raw))
}

/// The first object of a file (`_strings`, `_vectors`): a list.
pub fn first_list(raw: &[u8]) -> Result<Vec<Value>, String> {
    let v = tbcodec::unpack(&tbcodec::decompress(raw)).map_err(|e| e.0)?;
    match v {
        Value::Arr(a) => match a.into_iter().next() {
            Some(Value::Arr(inner)) => Ok(inner),
            _ => Err("unexpected static file shape".into()),
        },
        _ => Err("unexpected static file shape".into()),
    }
}

/// Unpacks `_no_duplicates` only up to the highest table wanted; returns those tables.
pub fn tables(nodup_raw: &[u8], wanted: &[usize]) -> Result<HashMap<usize, Value>, String> {
    let data = tbcodec::decompress(nodup_raw);
    let mut u = LEUnpacker::new(&data, 0);
    let b = u.u8().map_err(|e| e.0)?;
    let count = match b {
        0x90..=0x9f => (b & 0x0f) as usize,
        0xdc => u.u16().map_err(|e| e.0)? as usize,
        0xdd => u.u32().map_err(|e| e.0)? as usize,
        _ => return Err(format!("unexpected root byte 0x{:02x} in _no_duplicates", b)),
    };
    let last = *wanted.iter().max().unwrap_or(&0);
    if count <= last {
        return Err(format!("_no_duplicates has only {} tables", count));
    }
    let mut out = HashMap::new();
    for idx in 0..=last {
        let table = u.read().map_err(|e: DecodeError| e.0)?;
        if wanted.contains(&idx) {
            out.insert(idx, table);
        }
    }
    Ok(out)
}

pub fn text_at(strings: &[Value], index: &Value) -> String {
    index
        .strict_int()
        .and_then(|i| usize::try_from(i).ok())
        .and_then(|i| strings.get(i))
        .and_then(|s| s.as_str())
        .unwrap_or("")
        .to_string()
}

/// The Desktop client's cache: (strings, vectors, no_duplicates, mo bytes, version).
pub struct DesktopFiles {
    pub strings: Vec<u8>,
    pub vectors: Option<Vec<u8>>,
    pub nodup: Vec<u8>,
    pub mo: Vec<u8>,
    pub version: Map<String, Json>,
}

pub fn desktop_files(with_vectors: bool) -> Option<DesktopFiles> {
    let local_low: PathBuf = crate::paths::home_dir().join("AppData").join("LocalLow").join("Scorewarrior").join("TotalBattle");
    let cache = local_low.join("Cache");
    let raw = local_low.join("RawFiles");
    if !cache.is_dir() || !raw.is_dir() {
        return None;
    }
    let find = |suffix: &str| -> Option<PathBuf> {
        std::fs::read_dir(&cache).ok()?.flatten().map(|e| e.path()).find(|p| {
            p.file_name().and_then(|n| n.to_str()).map(|n| n.ends_with(suffix)).unwrap_or(false)
        })
    };
    let strings = find("_strings")?;
    let nodup = find("_no_duplicates")?;
    let vectors = if with_vectors { Some(find("_vectors")?) } else { None };
    let catalog_name = std::fs::read_to_string(raw.join("translation_catalog.txt")).ok()?.trim().to_string();
    let catalog: Json = serde_json::from_str(&std::fs::read_to_string(raw.join(catalog_name)).ok()?).ok()?;
    let md5 = catalog["files"].as_array()?.iter().find(|i| i["name"] == LOCALE)?["md5"].as_str()?.to_string();
    let mo = raw.join(&md5);
    if !mo.is_file() {
        return None;
    }
    let name = |p: &PathBuf| p.file_name().unwrap().to_string_lossy().into_owned();
    let mut version = Map::new();
    version.insert("no_duplicates".into(), Json::String(name(&nodup)));
    version.insert("strings".into(), Json::String(name(&strings)));
    if let Some(v) = &vectors {
        version.insert("vectors".into(), Json::String(name(v)));
    }
    version.insert(LOCALE.into(), Json::String(md5));
    Some(DesktopFiles {
        strings: std::fs::read(&strings).ok()?,
        vectors: match &vectors {
            Some(v) => Some(std::fs::read(v).ok()?),
            None => None,
        },
        nodup: std::fs::read(&nodup).ok()?,
        mo: std::fs::read(&mo).ok()?,
        version,
    })
}

/// `ast.literal_eval` for the keys of the tables: nested lists of integers.
pub fn literal_ints(text: &str) -> Option<Value> {
    fn parse(chars: &[char], pos: &mut usize) -> Option<Value> {
        while *pos < chars.len() && chars[*pos].is_whitespace() {
            *pos += 1;
        }
        if *pos >= chars.len() {
            return None;
        }
        if chars[*pos] == '[' || chars[*pos] == '(' {
            let close = if chars[*pos] == '[' { ']' } else { ')' };
            *pos += 1;
            let mut items = Vec::new();
            loop {
                while *pos < chars.len() && chars[*pos].is_whitespace() {
                    *pos += 1;
                }
                if *pos < chars.len() && chars[*pos] == close {
                    *pos += 1;
                    return Some(Value::Arr(items));
                }
                items.push(parse(chars, pos)?);
                while *pos < chars.len() && chars[*pos].is_whitespace() {
                    *pos += 1;
                }
                match chars.get(*pos) {
                    Some(',') => *pos += 1,
                    Some(c) if *c == close => {}
                    _ => return None,
                }
            }
        }
        let start = *pos;
        if chars[*pos] == '-' {
            *pos += 1;
        }
        while *pos < chars.len() && chars[*pos].is_ascii_digit() {
            *pos += 1;
        }
        let s: String = chars[start..*pos].iter().collect();
        s.parse::<i128>().ok().map(Value::Int)
    }
    let chars: Vec<char> = text.chars().collect();
    let mut pos = 0;
    let v = parse(&chars, &mut pos)?;
    while pos < chars.len() && chars[pos].is_whitespace() {
        pos += 1;
    }
    (pos == chars.len()).then_some(v)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn table_keys_are_read_back() {
        assert_eq!(Some(crate::arr![crate::arr![1024, 1], 0]), literal_ints("[[1024, 1], 0]"));
        assert_eq!(Some(Value::Int(-5)), literal_ints(" -5 "));
        assert_eq!(None, literal_ints("[1, 'x']"));
    }
}
