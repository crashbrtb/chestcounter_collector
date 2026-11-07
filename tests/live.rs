//! Checks against the real world, run by hand: `cargo test --test live -- --ignored`.
//! They read the game's public CDN only; nothing is sent to the game or to a clan site.

use std::path::PathBuf;

fn dir() -> Option<PathBuf> {
    std::env::var("TBC_STATIC_DIR").ok().map(PathBuf::from)
}

#[test]
#[ignore]
fn the_static_data_decodes_as_the_python_version_decoded_it() {
    let Some(d) = dir() else { return };
    let read = |n: &str| std::fs::read(d.join(n)).unwrap();
    let chests = tbc::modules::chests::catalog::decode_chest_table(&read("strings"), &read("vectors"), &read("no_duplicates"), &read("mo")).unwrap();
    let mut out = serde_json::Map::new();
    let mut ids: Vec<_> = chests.keys().collect();
    ids.sort();
    for id in ids {
        out.insert(id.to_string(), serde_json::json!([chests[id].0, chests[id].1]));
    }
    std::fs::write(d.join("rs_chests.json"), serde_json::to_string(&out).unwrap()).unwrap();
    let (r, t) = tbc::modules::events::catalog::decode_tournament_tables(&read("strings"), &read("no_duplicates"), &read("mo")).unwrap();
    let types: serde_json::Map<String, serde_json::Value> = t.iter().map(|(k, v)| (k.to_string(), serde_json::json!(v))).collect();
    std::fs::write(d.join("rs_tournaments.json"), serde_json::json!({"rankings": r, "types": types}).to_string()).unwrap();
}

#[test]
#[ignore]
fn the_cdn_says_which_build_is_current() {
    let v = tbc::static_data::fetch_cdn_version(&["no_duplicates", "strings", "vectors"]).unwrap();
    assert!(v["no_duplicates"].as_str().unwrap().ends_with("_no_duplicates"));
    assert_eq!(16, v["en_US"].as_str().unwrap().len());
    println!("{:?}", v);
}

#[test]
#[ignore]
fn the_real_databases_read_as_the_python_version_reads_them() {
    use tbc::db::{ChestRepo, ChestRepository};
    let config = tbc::config::ConfigManager::open_default().unwrap();
    for account in config.accounts() {
        for p in account.profiles.iter().filter(|p| p.has_database()) {
            let db = p.database.clone().unwrap();
            let (conn, _) = tbc::db::open_connection(&db, Some(15)).unwrap();
            let mut repo = ChestRepository::new(conn, db.clone());
            let ready = repo.can_read_from_game();
            let players = repo.game_players();
            let gifts = repo.chests_of_gifts(0, i64::MAX as i128);
            let total: i64 = gifts.values().sum();
            println!("RESULT {} ready={} players={} gifts={} chests={}", p.name, ready, players.len(), gifts.len(), total);
        }
    }
}

/// Creates the collector's task in the real Task Scheduler, reads it back and removes it.
/// Left alone when the task already exists, so a real schedule is never touched.
#[test]
#[ignore]
fn the_task_scheduler_entry_is_created_read_back_and_removed() {
    use tbc::task_scheduler as ts;
    if ts::query().unwrap().is_some() {
        println!("SKIPPED: a '{}' task already exists", ts::TASK_NAME);
        return;
    }
    let exe = std::env::current_exe().unwrap();
    for (minutes, start) in [(30, ""), (120, "08:15"), (1440, "06:00")] {
        ts::create_for(&exe, minutes, start).unwrap();
        let t = ts::query().unwrap().expect("task created");
        println!("RESULT {} {:?} -> {:?}", minutes, start, t);
        assert_eq!(minutes, t.every_minutes);
        if !start.is_empty() {
            assert_eq!(start, t.start);
        }
        assert!(t.enabled);
        assert_eq!(exe.to_string_lossy().to_lowercase(), t.command.to_lowercase());
    }
    ts::remove().unwrap();
    assert_eq!(None, ts::query().unwrap());
}

/// Dumps the translation keys of the Journal's tournament cards (read-only, CDN only).
#[test]
#[ignore]
fn dump_journal_tournament_card_keys() {
    let v = tbc::static_data::fetch_cdn_version(&["no_duplicates"]).unwrap();
    let mo = tbc::static_data::fetch_translation(v["en_US"].as_str().unwrap()).unwrap();
    let t = tbc::static_data::translations(&mo).unwrap();
    let mut keys: Vec<&String> = t.keys().filter(|k| k.contains("journal") && (k.contains("tournament") || k.contains("clan_result") || k.contains("clan_rating") || k.contains("reward"))).collect();
    keys.sort();
    let out = std::env::var("TBC_DUMP").unwrap();
    let text: String = keys.iter().map(|k| format!("{}\t{}\n", k, t[*k].replace('\n', "\n"))).collect();
    std::fs::write(out, text).unwrap();
}

/// Looks for item / reward ids in every table of the CDN static data (read-only).
#[test]
#[ignore]
fn dump_rows_with_ids() {
    use tbc::tbcodec::Value;
    let v = tbc::static_data::fetch_cdn_version(&["no_duplicates", "strings", "vectors"]).unwrap();
    let get = |k: &str| v[k].as_str().unwrap().to_string();
    let nodup = tbc::static_data::fetch_static(&get("no_duplicates")).unwrap();
    let strings = tbc::static_data::first_list(&tbc::static_data::fetch_static(&get("strings")).unwrap()).unwrap();
    let vectors = tbc::static_data::first_list(&tbc::static_data::fetch_static(&get("vectors")).unwrap()).unwrap();
    let mo = tbc::static_data::translations(&tbc::static_data::fetch_translation(&get("en_US")).unwrap()).unwrap();
    let ids: Vec<String> = std::env::var("TBC_IDS").unwrap().split(',').map(String::from).collect();
    let all: Vec<usize> = (0..226).collect();
    let tables = tbc::static_data::tables(&nodup, &all).unwrap();
    let mut out = String::new();
    let mut idx: Vec<&usize> = tables.keys().collect();
    idx.sort();
    for t in idx {
        let Value::Map(rows) = &tables[t] else { continue };
        for (k, row) in rows {
            let key = match k { Value::Str(s) => s.trim().to_string(), Value::Int(i) => i.to_string(), _ => continue };
            if !ids.contains(&key) { continue }
            let mut fields = Vec::new();
            if let Some(r) = row.as_arr() {
                for (i, f) in r.iter().enumerate() {
                    let text = tbc::modules::chests::catalog::resolve_vector(f, &vectors, &strings, &mo);
                    let s = tbc::static_data::text_at(&strings, f);
                    fields.push(format!("[{}] {} | vec:{} | str:{}", i, tbc::tbcodec::py_json(f, false).chars().take(160).collect::<String>(), text, s.chars().take(80).collect::<String>()));
                }
            } else {
                fields.push(tbc::tbcodec::py_json(row, false).chars().take(400).collect());
            }
            out.push_str(&format!("TABLE {} KEY {}\n  {}\n", t, key, fields.join("\n  ")));
        }
    }
    std::fs::write(std::env::var("TBC_DUMP").unwrap(), out).unwrap();
}

/// Writes the UI JSON of the tournament rows (Table 44 row[10] and every other JSON string field).
#[test]
#[ignore]
fn dump_tournament_visuals() {
    use tbc::tbcodec::Value;
    let v = tbc::static_data::fetch_cdn_version(&["no_duplicates", "strings"]).unwrap();
    let get = |k: &str| v[k].as_str().unwrap().to_string();
    let nodup = tbc::static_data::fetch_static(&get("no_duplicates")).unwrap();
    let strings = tbc::static_data::first_list(&tbc::static_data::fetch_static(&get("strings")).unwrap()).unwrap();
    let types: Vec<String> = std::env::var("TBC_IDS").unwrap().split(',').map(String::from).collect();
    let mut tables = tbc::static_data::tables(&nodup, &[44]).unwrap();
    let Some(Value::Map(rows)) = tables.remove(&44) else { panic!() };
    let mut out = String::new();
    for (k, row) in rows {
        let key = k.as_str().unwrap_or("").to_string();
        if !types.iter().any(|t| key.starts_with(&format!("[[{},", t))) { continue }
        out.push_str(&format!("ROW {}\n", key));
        for (i, f) in row.as_arr().unwrap().iter().enumerate() {
            let s = tbc::static_data::text_at(&strings, f);
            if s.starts_with('{') || s.contains("icon") || s.contains("banner") {
                out.push_str(&format!("  [{}] {}\n", i, s));
            }
        }
    }
    std::fs::write(std::env::var("TBC_DUMP").unwrap(), out).unwrap();
}

/// Decodes a traffic capture (jsonl of http_request / http_response / ws frames) to text.
#[test]
#[ignore]
fn decode_capture() {
    use base64::Engine;
    use tbc::tbcodec::{self, expand_blobs, py_json, LEUnpacker, Value};
    let b64 = base64::engine::general_purpose::STANDARD;
    let input = std::fs::read_to_string(std::env::var("TBC_CAPTURE").unwrap()).unwrap();
    let limit: usize = std::env::var("TBC_LIMIT").ok().and_then(|v| v.parse().ok()).unwrap_or(3000);
    let mut out = String::new();
    let mut routes: std::collections::HashMap<String, i128> = std::collections::HashMap::new();
    for line in input.lines() {
        let r: serde_json::Value = serde_json::from_str(line).unwrap();
        let kind = r["kind"].as_str().unwrap_or("");
        let id = r["id"].as_str().unwrap_or("").to_string();
        let mut bytes = r["b64"].as_str().and_then(|s| b64.decode(s).ok()).unwrap_or_default();
        // Chrome base64-encodes binary posts once more than it says.
        if kind == "http_request" && tbc::link::parse_request(&bytes).is_none() {
            if let Ok(again) = b64.decode(&bytes) { bytes = again; }
        }
        let text = match kind {
            "http_request" => match tbc::link::parse_request(&bytes) {
                Some((route, seq, _auth, _tail)) => {
                    routes.insert(id.clone(), route);
                    let mut u = LEUnpacker::new(&bytes, 8);
                    let _ = u.read();
                    let objects = u.read().map(|v| py_json(&expand_blobs(&v), false)).unwrap_or_default();
                    format!("REQUEST route {} seq {} {}", route, seq, objects)
                }
                None => format!("REQUEST undecoded {} bytes", bytes.len()),
            },
            "http_response" => {
                let frames = tbcodec::iter_frames(&tbcodec::decompress(&bytes));
                let parts: Vec<String> = frames.iter().map(|f| format!("[route {:?}] {}", f.route,
                    py_json(&expand_blobs(&Value::Arr(f.objects.clone())), false))).collect();
                format!("RESPONSE to route {:?} ({} bytes): {}", routes.get(&id), bytes.len(), parts.join(" | "))
            }
            "ws_in" | "ws_out" => {
                let data = tbcodec::decompress(&bytes);
                match tbcodec::unpack_stream(&data) {
                    Some(v) => format!("{} {}", kind, py_json(&expand_blobs(&Value::Arr(v)), false)),
                    None => format!("{} {} bytes (text: {:?})", kind, bytes.len(), r["text"].as_str().map(|t| t.chars().take(200).collect::<String>())),
                }
            }
            other => format!("{} {}", other, r),
        };
        out.push_str(&format!("{:.1} {}\n", r["t"].as_f64().unwrap_or(0.0), text.chars().take(limit).collect::<String>()));
    }
    std::fs::write(std::env::var("TBC_DUMP").unwrap(), out).unwrap();
}

/// Prints every field of the Table 44 rows of one tournament type (shapes only for long lists).
#[test]
#[ignore]
fn dump_tournament_row_fields() {
    use tbc::tbcodec::{py_json, Value};
    let v = tbc::static_data::fetch_cdn_version(&["no_duplicates", "strings"]).unwrap();
    let get = |k: &str| v[k].as_str().unwrap().to_string();
    let nodup = tbc::static_data::fetch_static(&get("no_duplicates")).unwrap();
    let strings = tbc::static_data::first_list(&tbc::static_data::fetch_static(&get("strings")).unwrap()).unwrap();
    let want = std::env::var("TBC_IDS").unwrap();
    let mut tables = tbc::static_data::tables(&nodup, &[44]).unwrap();
    let Some(Value::Map(rows)) = tables.remove(&44) else { panic!() };
    let mut out = String::new();
    for (k, row) in rows {
        let key = k.as_str().unwrap_or("").to_string();
        if !key.starts_with(&format!("[[{}", want)) { continue }
        out.push_str(&format!("ROW {}\n", key));
        for (i, f) in row.as_arr().unwrap().iter().enumerate() {
            let s = tbc::static_data::text_at(&strings, f);
            out.push_str(&format!("  [{}] {} | str:{}\n", i, py_json(f, false).chars().take(600).collect::<String>(), s.chars().take(20000).collect::<String>()));
        }
    }
    std::fs::write(std::env::var("TBC_DUMP").unwrap(), out).unwrap();
}

/// The images read from the game's CDN are the same pixels UnityPy reads (TBC_UNITYPY_DIR has its PNGs).
#[test]
#[ignore]
fn the_cdn_images_decode_like_unitypy() {
    let index = tbc::unity::ImageIndex::current().unwrap();
    println!("index {} with {} images", index.id, index.files.len());
    let dir = std::path::PathBuf::from(std::env::var("TBC_UNITYPY_DIR").unwrap());
    let decode = |bytes: &[u8]| {
        let mut reader = png::Decoder::new(std::io::Cursor::new(bytes.to_vec())).read_info().unwrap();
        let mut buf = vec![0; reader.output_buffer_size().unwrap()];
        let info = reader.next_frame(&mut buf).unwrap();
        (info.width, info.height, info.color_type, buf[..info.buffer_size()].to_vec())
    };
    for name in ["fangs_coin_72", "icon_multistage_epic_clan_chest", "banner_multistage_main_708x123", "widget_static_multistage_main"] {
        let (file, png) = index.png(name).unwrap();
        std::fs::write(dir.join(format!("rust_{}.png", name)), &png).unwrap();
        let mine = decode(&png);
        let theirs = decode(&std::fs::read(dir.join(format!("{}.png", name))).unwrap());
        println!("RESULT {} file {} {}x{} {:?} vs {}x{} {:?}", name, file, mine.0, mine.1, mine.2, theirs.0, theirs.1, theirs.2);
        assert_eq!((mine.0, mine.1), (theirs.0, theirs.1));
        if theirs.2 == png::ColorType::Rgba {
            assert!(mine.3 == theirs.3, "{}: pixels differ", name);
        }
    }
}

/// Looks for a table that maps 117 to 154 and 96 to 131 (clan id shard to realm).
#[test]
#[ignore]
fn find_shard_to_realm_table() {
    use tbc::tbcodec::{py_json, Value};
    let v = tbc::static_data::fetch_cdn_version(&["no_duplicates"]).unwrap();
    let nodup = tbc::static_data::fetch_static(v["no_duplicates"].as_str().unwrap()).unwrap();
    let all: Vec<usize> = (0..226).collect();
    let tables = tbc::static_data::tables(&nodup, &all).unwrap();
    for (t, table) in tables {
        let Value::Map(rows) = table else { continue };
        let text: HashMapLike = rows.iter().map(|(k, v)| (py_json(k, false), py_json(v, false))).collect();
        let has = |k: &str, want: &str| text.iter().any(|(kk, vv)| kk.trim_matches('"') == k && vv.contains(want));
        if has("117", "154") && has("96", "131") {
            println!("TABLE {} {:?}", t, text.iter().find(|(k, _)| k.trim_matches('"') == "117"));
        }
        // or the other way round: realm -> shard
        if has("154", "117") && has("131", "96") {
            println!("TABLE-REV {} {:?}", t, text.iter().find(|(k, _)| k.trim_matches('"') == "154").map(|(k, v)| (k.clone(), v.chars().take(200).collect::<String>())));
        }
    }
}
type HashMapLike = Vec<(String, String)>;
