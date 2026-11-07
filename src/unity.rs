//! The game's own images, from its public Unity CDN.
//!
//! Measured on 2026-11-07 (downloads only):
//!
//! 1. `versions.totalbattle.com/config` -> `unity_catalog_version.webgl`: the current index;
//! 2. the index (`assets-unity.totalbattle.dev/webgl/<that>`) maps every asset name
//!    (`banner_ragnarok_clan_event_708x123`, `fangs_coin_72`...) to the file that holds it:
//!    `"I" u8 + 6 bytes, then [u32 len][name][u32 flags][16 bytes][16 bytes file id][u32 n][n x (u32 len, bundle)]`;
//! 3. the file (`assets-unity.totalbattle.dev/webgl/<file id>`) is a UnityFS bundle (2019.4,
//!    LZ4) with one Texture2D in RGBA32, stored bottom row first.
//!
//! The CDN refuses a request that does not say it is a browser.

use std::collections::HashMap;

pub const VERSIONS_URL: &str = "https://versions.totalbattle.com/config";
pub const ASSETS_URL: &str = "https://assets-unity.totalbattle.dev/webgl";
const USER_AGENT: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36";
const TEXTURE_RGBA32: u32 = 4;

pub fn fetch(url: &str) -> Result<Vec<u8>, String> {
    crate::http::ensure_crypto();
    let agent = ureq::AgentBuilder::new().timeout(std::time::Duration::from_secs(60)).user_agent(USER_AGENT).build();
    let response = agent.get(url).call().map_err(|e| format!("{}: {}", url, e))?;
    let mut bytes = Vec::new();
    std::io::Read::read_to_end(&mut response.into_reader(), &mut bytes).map_err(|e| e.to_string())?;
    Ok(bytes)
}

/// The current index of the WebGL client's images.
pub fn current_index_id() -> Result<String, String> {
    let config: serde_json::Value = serde_json::from_slice(&fetch(VERSIONS_URL)?).map_err(|e| e.to_string())?;
    config["result"]["config"]["unity_catalog_version"]["webgl"]
        .as_str()
        .filter(|s| !s.is_empty() && s.bytes().all(|b| b.is_ascii_hexdigit()))
        .map(String::from)
        .ok_or_else(|| "the game's version config does not name its image index".into())
}

/// Asset name -> file id, from the index.
pub fn parse_index(data: &[u8]) -> HashMap<String, String> {
    let u32_at = |i: usize| -> Option<usize> { Some(u32::from_le_bytes(data.get(i..i + 4)?.try_into().ok()?) as usize) };
    let mut out = HashMap::new();
    let mut i = 8;
    while let Some(n) = u32_at(i) {
        if n == 0 || n > 400 || i + 4 + n + 40 > data.len() {
            break;
        }
        let name = String::from_utf8_lossy(&data[i + 4..i + 4 + n]).into_owned();
        let mut j = i + 4 + n;
        let file_id = hex::encode(&data[j + 4 + 16..j + 4 + 32]);
        j += 4 + 32;
        let Some(count) = u32_at(j) else { break };
        j += 4;
        for _ in 0..count {
            let Some(m) = u32_at(j) else { break };
            j += 4 + m;
        }
        out.insert(name, file_id);
        i = j;
    }
    out
}

fn be32(d: &[u8], i: usize) -> Result<u32, String> {
    Ok(u32::from_be_bytes(d.get(i..i + 4).ok_or("bundle too short")?.try_into().unwrap()))
}

fn be64(d: &[u8], i: usize) -> Result<u64, String> {
    Ok(u64::from_be_bytes(d.get(i..i + 8).ok_or("bundle too short")?.try_into().unwrap()))
}

fn cstring(d: &[u8], i: &mut usize) -> Result<String, String> {
    let end = d[*i..].iter().position(|b| *b == 0).ok_or("unterminated string in bundle")? + *i;
    let s = String::from_utf8_lossy(&d[*i..end]).into_owned();
    *i = end + 1;
    Ok(s)
}

fn decompress(kind: u32, data: &[u8], size: usize) -> Result<Vec<u8>, String> {
    match kind {
        0 => Ok(data.to_vec()),
        2 | 3 => lz4_flex::block::decompress(data, size).map_err(|e| format!("LZ4: {}", e)),
        other => Err(format!("bundle compression {} is not read", other)),
    }
}

/// The files inside a UnityFS bundle: (path, bytes).
pub fn unpack_bundle(d: &[u8]) -> Result<Vec<(String, Vec<u8>)>, String> {
    if !d.starts_with(b"UnityFS\0") {
        return Err("not a UnityFS bundle".into());
    }
    let mut i = 8;
    let version = be32(d, i)?;
    i += 4;
    cstring(d, &mut i)?;
    cstring(d, &mut i)?;
    let _size = be64(d, i)?;
    let compressed = be32(d, i + 8)? as usize;
    let uncompressed = be32(d, i + 12)? as usize;
    let flags = be32(d, i + 16)?;
    i += 20;
    if version >= 7 {
        i = (i + 15) & !15;
    }
    let info_raw = if flags & 0x80 != 0 {
        d.get(d.len() - compressed..).ok_or("bundle too short")?
    } else {
        let raw = d.get(i..i + compressed).ok_or("bundle too short")?;
        i += compressed;
        raw
    };
    let info = decompress(flags & 0x3F, info_raw, uncompressed)?;
    let mut k = 16;
    let blocks = be32(&info, k)? as usize;
    k += 4;
    let mut data = Vec::new();
    if flags & 0x200 != 0 {
        i = (i + 15) & !15;
    }
    for _ in 0..blocks {
        let usize_ = be32(&info, k)? as usize;
        let csize = be32(&info, k + 4)? as usize;
        let bflags = u16::from_be_bytes(info.get(k + 8..k + 10).ok_or("bad block info")?.try_into().unwrap());
        k += 10;
        let raw = d.get(i..i + csize).ok_or("bundle block out of range")?;
        data.extend(decompress((bflags & 0x3F) as u32, raw, usize_)?);
        i += csize;
    }
    let nodes = be32(&info, k)? as usize;
    k += 4;
    let mut out = Vec::new();
    for _ in 0..nodes {
        let offset = be64(&info, k)? as usize;
        let size = be64(&info, k + 8)? as usize;
        k += 20;
        let path = cstring(&info, &mut k)?;
        out.push((path, data.get(offset..offset + size).ok_or("bundle node out of range")?.to_vec()));
    }
    Ok(out)
}

/// The RGBA32 texture of a bundle: (width, height, pixels top row first).
pub fn texture_of(files: &[(String, Vec<u8>)]) -> Result<(u32, u32, Vec<u8>), String> {
    let (_, main) = files.iter().find(|(p, _)| !p.ends_with(".resS")).ok_or("no serialized file in the bundle")?;
    let stream = files.iter().find(|(p, _)| p.ends_with(".resS")).map(|(_, b)| b.as_slice());
    let le = |i: usize| -> Option<u32> { Some(u32::from_le_bytes(main.get(i..i + 4)?.try_into().ok()?)) };
    // m_Width, m_Height, m_CompleteImageSize, m_TextureFormat follow each other in a Texture2D.
    let mut at = None;
    let mut i = 0;
    while i + 16 <= main.len() {
        if let (Some(w), Some(h), Some(size), Some(format)) = (le(i), le(i + 4), le(i + 8), le(i + 12)) {
            if (1..=4096).contains(&w) && (1..=4096).contains(&h) && format == TEXTURE_RGBA32 && size as u64 == w as u64 * h as u64 * 4 {
                at = Some((i, w, h, size as usize));
                break;
            }
        }
        i += 4;
    }
    let (start, w, h, size) = at.ok_or("no RGBA32 texture in the bundle")?;
    // Then, a few fields on: the image data (size + bytes), or 0 and a stream into the .resS file.
    let mut j = start + 16;
    let mut pixels = None;
    while j + 4 <= main.len() && j < start + 400 {
        match le(j) {
            Some(n) if n as usize == size && main.len() >= j + 4 + size => {
                pixels = Some(main[j + 4..j + 4 + size].to_vec());
                break;
            }
            Some(0) if stream.is_some() => {
                // m_StreamData: offset (u32), size (u32), path.
                let (offset, len) = (le(j + 4).unwrap_or(0) as usize, le(j + 8).unwrap_or(0) as usize);
                if len == size {
                    let s = stream.unwrap();
                    pixels = s.get(offset..offset + size).map(|p| p.to_vec());
                    if pixels.is_some() {
                        break;
                    }
                }
            }
            _ => {}
        }
        j += 4;
    }
    let raw = pixels.ok_or("the texture's pixels were not found")?;
    let row = w as usize * 4;
    let mut flipped = Vec::with_capacity(raw.len());
    for y in (0..h as usize).rev() {
        flipped.extend_from_slice(&raw[y * row..(y + 1) * row]);
    }
    Ok((w, h, flipped))
}

pub fn png_of(w: u32, h: u32, rgba: &[u8]) -> Result<Vec<u8>, String> {
    let mut out = Vec::new();
    {
        let mut encoder = png::Encoder::new(&mut out, w, h);
        encoder.set_color(png::ColorType::Rgba);
        encoder.set_depth(png::BitDepth::Eight);
        encoder.set_compression(png::Compression::High);
        let mut writer = encoder.write_header().map_err(|e| e.to_string())?;
        writer.write_image_data(rgba).map_err(|e| e.to_string())?;
    }
    Ok(out)
}

/// A bundle of the CDN as PNG.
pub fn png_from_bundle(bundle: &[u8]) -> Result<Vec<u8>, String> {
    let (w, h, rgba) = texture_of(&unpack_bundle(bundle)?)?;
    png_of(w, h, &rgba)
}

/// The game's index of images, fetched once and kept for the run.
pub struct ImageIndex {
    pub id: String,
    pub files: HashMap<String, String>,
}

impl ImageIndex {
    pub fn current() -> Result<Self, String> {
        let id = current_index_id()?;
        let files = parse_index(&fetch(&format!("{}/{}", ASSETS_URL, id))?);
        if files.is_empty() {
            return Err("the game's image index is empty".into());
        }
        Ok(ImageIndex { id, files })
    }

    /// The file id of an asset, which changes when the game changes the image.
    pub fn file_of(&self, name: &str) -> Option<&String> {
        self.files.get(name)
    }

    /// The asset as PNG.
    pub fn png(&self, name: &str) -> Result<(String, Vec<u8>), String> {
        let file = self.file_of(name).ok_or_else(|| format!("the game has no image named {}", name))?;
        Ok((file.clone(), png_from_bundle(&fetch(&format!("{}/{}", ASSETS_URL, file))?)?))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_index_names_each_asset_and_its_file() {
        let mut d = b"I\x17P.08\0\0".to_vec();
        for (name, file) in [("fangs_coin_72", [0xAAu8; 16]), ("banner_x_708x123", [0x0Bu8; 16])] {
            d.extend((name.len() as u32).to_le_bytes());
            d.extend(name.as_bytes());
            d.extend(2u32.to_le_bytes());
            d.extend([0u8; 16]);
            d.extend(file);
            d.extend(1u32.to_le_bytes());
            d.extend((name.len() as u32).to_le_bytes());
            d.extend(name.as_bytes());
        }
        let index = parse_index(&d);
        assert_eq!(Some(&"aa".repeat(16)), index.get("fangs_coin_72"));
        assert_eq!(Some(&"0b".repeat(16)), index.get("banner_x_708x123"));
    }

    #[test]
    fn a_texture_is_read_top_row_first() {
        // m_Width 2, m_Height 2, size 16, RGBA32, a few fields, then the pixel data.
        let mut main = vec![0u8; 12];
        for v in [2u32, 2, 16, 4, 1, 0, 0, 0] {
            main.extend(v.to_le_bytes());
        }
        main.extend(16u32.to_le_bytes());
        let bottom = [1u8, 1, 1, 255, 2, 2, 2, 255];
        let top = [9u8, 9, 9, 255, 8, 8, 8, 255];
        main.extend(bottom);
        main.extend(top);
        let (w, h, rgba) = texture_of(&[("CAB-x".to_string(), main)]).unwrap();
        assert_eq!((2, 2), (w, h));
        assert_eq!(&top, &rgba[..8]);
        assert!(png_of(w, h, &rgba).unwrap().starts_with(b"\x89PNG"));
    }
}
