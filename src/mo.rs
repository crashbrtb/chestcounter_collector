//! GNU gettext `.mo` files: the game's translations (msgid -> English text).
//!
//! Only singular messages are kept, under their msgid as text, as Python's
//! `GNUTranslations._catalog` keeps them under a string key.

use std::collections::HashMap;

pub fn parse_mo(data: &[u8]) -> Result<HashMap<String, String>, String> {
    if data.len() < 28 {
        return Err("translation file too short".into());
    }
    let magic = u32::from_le_bytes(data[0..4].try_into().unwrap());
    let little = match magic {
        0x950412de => true,
        0xde120495 => false,
        _ => return Err("not a gettext .mo file".into()),
    };
    let word = |pos: usize| -> Result<usize, String> {
        let bytes: [u8; 4] = data.get(pos..pos + 4).ok_or("truncated .mo file")?.try_into().unwrap();
        Ok(if little { u32::from_le_bytes(bytes) } else { u32::from_be_bytes(bytes) } as usize)
    };
    let count = word(8)?;
    let originals = word(12)?;
    let translations = word(16)?;
    let mut catalog = HashMap::with_capacity(count);
    for i in 0..count {
        let (olen, ooff) = (word(originals + i * 8)?, word(originals + i * 8 + 4)?);
        let (tlen, toff) = (word(translations + i * 8)?, word(translations + i * 8 + 4)?);
        let msgid = data.get(ooff..ooff + olen).ok_or("truncated .mo file")?;
        let msgstr = data.get(toff..toff + tlen).ok_or("truncated .mo file")?;
        if msgid.contains(&0) {
            continue; // plural forms are kept under a tuple key, never looked up by text
        }
        catalog.insert(String::from_utf8_lossy(msgid).into_owned(), String::from_utf8_lossy(msgstr).into_owned());
    }
    Ok(catalog)
}

/// A `.mo` file from pairs, for tests.
pub fn build_mo(pairs: &[(&str, &str)]) -> Vec<u8> {
    let mut sorted: Vec<&(&str, &str)> = pairs.iter().collect();
    sorted.sort_by_key(|p| p.0);
    let n = sorted.len();
    let header = 28;
    let otable = header;
    let ttable = otable + n * 8;
    let mut strings = Vec::new();
    let mut otab = Vec::new();
    let mut ttab = Vec::new();
    let base = ttable + n * 8;
    for (k, _) in &sorted {
        otab.push((k.len(), base + strings.len()));
        strings.extend(k.as_bytes());
        strings.push(0);
    }
    for (_, v) in &sorted {
        ttab.push((v.len(), base + strings.len()));
        strings.extend(v.as_bytes());
        strings.push(0);
    }
    let mut out = Vec::new();
    for w in [0x950412deu32, 0, n as u32, otable as u32, ttable as u32, 0, 0] {
        out.extend(w.to_le_bytes());
    }
    for (l, o) in otab.iter().chain(ttab.iter()) {
        out.extend((*l as u32).to_le_bytes());
        out.extend((*o as u32).to_le_bytes());
    }
    out.extend(strings);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_built_file_reads_back() {
        let mo = build_mo(&[("item.name.x", "Bone Chest"), ("", "Content-Type: text/plain; charset=UTF-8")]);
        let cat = parse_mo(&mo).unwrap();
        assert_eq!("Bone Chest", cat["item.name.x"]);
        assert!(parse_mo(b"nope").is_err());
    }
}
