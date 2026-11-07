//! Maintenance of the chest types already in a clan database.
//!
//! `fix-chest-types` checks every chest type against the game's official catalogue
//! (Table 8) and puts right: types missing from `game_chest_types`, types named
//! wrong there, rows of `collected_chests` named wrong, and pending
//! `incomplete_chests` the catalogue can now name. Without `--apply` it only says
//! what it would do.
//!
//! `audit-chest-types` looks for types named wrong the way 5784 was, from what the
//! other types say about them (their block, twins, quantities, dates). It writes nothing.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use chrono::{Duration, NaiveDateTime};
use mysql::prelude::Queryable;
use mysql::{Conn, Params, Value as Sql};

use crate::cli::{EXIT_FAILED, EXIT_MISCONFIGURED, EXIT_OK};
use crate::config::{ConfigManager, DatabaseConfig, ProfileConfig};
use crate::db::{describe, open_connection};
use crate::modules::chests::catalog::lookup_chest_types;
use crate::text::fold_key;

pub fn run(tool: &str, args: &[String], config: &ConfigManager) -> i32 {
    let mut opts: HashMap<String, String> = HashMap::new();
    let mut apply = false;
    let mut i = 0;
    while i < args.len() {
        let a = &args[i];
        if a == "--apply" {
            apply = true;
        } else if let Some(name) = a.strip_prefix("--") {
            if let Some(v) = args.get(i + 1) {
                opts.insert(name.to_string(), v.clone());
                i += 1;
            }
        }
        i += 1;
    }
    match tool {
        "fix-chest-types" => fix_main(config, &opts, apply),
        "audit-chest-types" => audit_main(config, &opts),
        "diagnose" => diagnose(config),
        "journal-dump" => crate::journal_dump::run(config, &opts),
        "live-tournaments" => crate::journal_dump::live(config, &opts),
        _ => EXIT_MISCONFIGURED,
    }
}

fn profiles(config: &ConfigManager, wanted: Option<&String>) -> Vec<ProfileConfig> {
    let all: Vec<ProfileConfig> =
        config.enabled_accounts().iter().flat_map(|a| a.active_profiles()).filter(|p| p.has_database()).collect();
    match wanted {
        Some(w) => all.into_iter().filter(|p| &p.name == w || &p.label() == w).collect(),
        None => all,
    }
}

// ---------------------------------------------------------------- diagnose

/// What the old Check_Environment.bat looked at, for this program: where it lives,
/// whether Chrome is found, and whether every profile's database and clan site answer.
/// It only connects and reads.
pub fn diagnose(config: &ConfigManager) -> i32 {
    println!("{}", "=".repeat(70));
    println!("      DIAGNOSTICS - TOTAL BATTLE CHEST COLLECTOR {}", crate::VERSION);
    println!("{}", "=".repeat(70));
    println!("
[1] Program");
    println!("    Folder:        {}", crate::paths::base_dir().display());
    println!("    Configuration: {}{}", config.path.display(), if config.exists() { "" } else { " (not created yet)" });
    println!("    Logs:          {}", config.resolved_log_dir().display());
    let mut problems = 0;
    let browser = crate::browser::Browser::new(&config.section("browser"), 9322, &config.profiles_dir());
    match browser.find_executable() {
        Some(p) => println!("    Browser:       {}", p.display()),
        None => {
            problems += 1;
            println!("    Browser:       [FAILED] Chrome/Edge/Brave not found; set Browser > Chrome executable path");
        }
    }
    println!("
[2] Profiles");
    for account in config.accounts() {
        println!("    Account '{}' (port {}, folder {}){}", account.name, account.cdp_port, config.chrome_folder_for(&account).display(),
                 if account.enabled { "" } else { " - disabled" });
        for p in &account.profiles {
            println!("      Profile '{}'{}", p.name, if p.enabled { "" } else { " - disabled" });
            match &p.database {
                Some(db) if db.is_usable() => {
                    let (ok, text) = crate::db::test_connection(db);
                    if !ok {
                        problems += 1;
                    }
                    println!("        database {}@{}/{}: [{}] {}", db.user, db.host, db.database, if ok { "OK" } else { "FAILED" }, text);
                }
                _ => println!("        database: none (no chests)"),
            }
            if crate::modules::events::configured_for(p) {
                let (ok, text) = crate::modules::events::test_profile(&p.module_settings("events"));
                if !ok {
                    problems += 1;
                }
                println!("        clan site: [{}] {}", if ok { "OK" } else { "FAILED" }, text.replace('\n', " "));
            }
            let modules: Vec<&str> = crate::modules::KINDS.iter().filter(|k| k.configured_for(p)).map(|k| k.title()).collect();
            println!("        modules: {}", if modules.is_empty() { "none".to_string() } else { modules.join(", ") });
        }
    }
    println!("
{}", "=".repeat(70));
    if problems > 0 {
        println!("  RESULT: {} problem(s) found above.", problems);
        EXIT_FAILED
    } else {
        println!("  RESULT: Environment is READY!");
        EXIT_OK
    }
}

// --------------------------------------------------------------------- fix

fn fix_main(config: &ConfigManager, opts: &HashMap<String, String>, apply: bool) -> i32 {
    let mut targets: Vec<(String, DatabaseConfig)> = Vec::new();
    if let (Some(host), Some(user), Some(database)) = (opts.get("host"), opts.get("user"), opts.get("database")) {
        targets.push((format!("{}@{}", database, host), DatabaseConfig {
            host: host.clone(),
            port: opts.get("port").and_then(|p| p.parse().ok()).unwrap_or(3306),
            user: user.clone(),
            password: opts.get("password").cloned().unwrap_or_default(),
            database: database.clone(),
        }));
    } else {
        for p in profiles(config, opts.get("profile")) {
            targets.push((p.label(), p.database.unwrap()));
        }
    }
    if targets.is_empty() {
        println!("No enabled profile with a database found.");
        return EXIT_MISCONFIGURED;
    }
    let mut seen = HashSet::new();
    let mut failed = 0;
    for (label, db) in targets {
        if !seen.insert(db.identity()) {
            continue;
        }
        println!("{}", "=".repeat(78));
        println!("{} - database '{}' on {}:{}{}", label, db.database, db.host, db.port, if apply { "" } else { " (dry-run)" });
        println!("{}", "=".repeat(78));
        match open_connection(&db, Some(15)) {
            Ok((mut conn, _)) => {
                if let Err(e) = analyze_and_fix(&mut conn, apply) {
                    failed += 1;
                    println!("  Could not analyze this database: {}\n", describe(&e));
                }
            }
            Err(e) => {
                failed += 1;
                println!("  Could not connect: {}\n", describe(&e));
            }
        }
    }
    if failed > 0 {
        EXIT_FAILED
    } else {
        EXIT_OK
    }
}

type Canon = BTreeMap<i64, (String, String, Option<i64>)>;

fn case_update(table: &str, by: &BTreeMap<i64, (String, String)>, extra_where: &str) -> (String, Vec<Sql>) {
    let ids: Vec<&i64> = by.keys().collect();
    let cases = vec!["WHEN ? THEN ?"; ids.len()].join(" ");
    let mut params: Vec<Sql> = Vec::new();
    for id in &ids {
        params.push(Sql::Int(**id));
        params.push(by[id].0.clone().into());
    }
    for id in &ids {
        params.push(Sql::Int(**id));
        params.push(by[id].1.clone().into());
    }
    params.extend(ids.iter().map(|i| Sql::Int(**i)));
    let query = format!(
        "UPDATE {} SET name = CASE game_type_id {} ELSE name END, source = CASE game_type_id {} ELSE source END{} \
         WHERE game_type_id IN ({})",
        table, cases, cases, extra_where, vec!["?"; ids.len()].join(", ")
    );
    (query, params)
}

pub fn analyze_and_fix(conn: &mut Conn, apply: bool) -> Result<(), mysql::Error> {
    let mut tx = conn.start_transaction(mysql::TxOpts::default())?;
    let db_types: BTreeMap<i64, (String, String, Option<i64>)> = tx
        .query::<(i64, Option<String>, Option<String>, Option<i64>), _>(
            "SELECT game_type_id, name, source, standard_chest_id FROM game_chest_types")?
        .into_iter()
        .map(|(t, n, s, i)| (t, (n.unwrap_or_default(), s.unwrap_or_default(), i)))
        .collect();
    let mut all: BTreeSet<i64> = db_types.keys().copied().collect();
    all.extend(tx.query::<i64, _>("SELECT DISTINCT game_type_id FROM collected_chests WHERE game_type_id IS NOT NULL")?);
    all.extend(tx.query::<i64, _>("SELECT DISTINCT game_type_id FROM incomplete_chests WHERE game_type_id IS NOT NULL")?);
    if all.is_empty() {
        println!("  No chest types with game_type_id found in this database.\n");
        return Ok(());
    }
    let ids: Vec<i128> = all.iter().map(|i| *i as i128).collect();
    let official = lookup_chest_types(&ids);
    let mut standard: HashMap<String, (i64, String)> = HashMap::new();
    for (id, source) in tx.query::<(i64, Option<String>), _>("SELECT id, source FROM standard_chests")? {
        if let Some(s) = source.filter(|s| !s.is_empty()) {
            standard.entry(fold_key(&s)).or_insert((id, s));
        }
    }
    let mut canon: Canon = BTreeMap::new();
    for (tid, (name, source)) in &official {
        let resolved = match standard.get(&fold_key(source)) {
            Some((id, s)) => (name.clone(), s.clone(), Some(*id)),
            None => (name.clone(), source.clone(), None),
        };
        canon.insert(*tid as i64, resolved);
    }
    let unknown: Vec<i64> = all.iter().filter(|t| !canon.contains_key(t)).copied().collect();
    println!("  Catalog resolved {}/{} distinct game_type_id(s).", canon.len(), all.len());
    if !unknown.is_empty() {
        println!("  [!] IDs not found in official catalog: {:?}", unknown);
    }

    // A. Types missing from game_chest_types.
    let missing: Vec<i64> = canon.keys().filter(|t| !db_types.contains_key(t)).copied().collect();
    if !missing.is_empty() {
        println!("\n  [1] Missing from game_chest_types: {} type(s)", missing.len());
        for tid in &missing {
            let (n, s, std_id) = &canon[tid];
            println!("      + ID {}: '{}' ({}) [standard_chest_id={}]", tid, n, s, std_id.map(|i| i.to_string()).unwrap_or("None".into()));
            if apply {
                tx.exec_drop("INSERT IGNORE INTO game_chest_types (game_type_id, name, source, standard_chest_id, created, modified) \
                              VALUES (?, ?, ?, ?, UTC_TIMESTAMP(), UTC_TIMESTAMP())", (tid, n, s, std_id))?;
            }
        }
    }

    // B. Wrong name, source or standard_chest_id in game_chest_types.
    let mut wrong = 0;
    for (tid, (dn, ds, dstd)) in &db_types {
        let Some((cn, cs, cstd)) = canon.get(tid) else { continue };
        if dn != cn || ds != cs || (dstd.is_none() && cstd.is_some()) {
            if wrong == 0 {
                println!("\n  [2] Mismatches in game_chest_types:");
            }
            wrong += 1;
            println!("      ~ ID {}: DB=('{}', '{}', std={:?}) -> Official=('{}', '{}', std={:?})", tid, dn, ds, dstd, cn, cs, cstd);
            if apply {
                tx.exec_drop("UPDATE game_chest_types SET name = ?, source = ?, standard_chest_id = COALESCE(?, standard_chest_id), \
                              modified = UTC_TIMESTAMP() WHERE game_type_id = ?", (cn, cs, cstd, tid))?;
            }
        }
    }
    if wrong == 0 {
        println!("\n  [2] game_chest_types: all existing rows match the official catalog.");
    }

    // C. Wrong name or source in collected_chests.
    let groups: Vec<(i64, Option<String>, Option<String>, i64)> = tx.query(
        "SELECT game_type_id, name, source, COUNT(*) FROM collected_chests WHERE game_type_id IS NOT NULL \
         GROUP BY game_type_id, name, source ORDER BY game_type_id")?;
    let mut by: BTreeMap<i64, (String, String)> = BTreeMap::new();
    let mut rows = 0;
    for (tid, dn, ds, n) in groups {
        let Some((cn, cs, _)) = canon.get(&tid) else { continue };
        if dn.as_deref().unwrap_or("") != cn || ds.as_deref().unwrap_or("") != cs {
            if by.is_empty() {
                println!("\n  [3] Mismatched rows in collected_chests:");
            }
            println!("      ~ ID {} ({} rows): ('{}', '{}') -> ('{}', '{}')", tid, n, dn.unwrap_or_default(), ds.unwrap_or_default(), cn, cs);
            by.insert(tid, (cn.clone(), cs.clone()));
            rows += n;
        }
    }
    if by.is_empty() {
        println!("\n  [3] collected_chests: all rows with game_type_id match the official catalog.");
    } else {
        println!("      {} row(s) across {} type(s)", rows, by.len());
        if apply {
            let (q, p) = case_update("collected_chests", &by, "");
            tx.exec_drop(q, Params::Positional(p))?;
        }
    }

    // D. Pending incomplete_chests the catalogue can name.
    let players: HashMap<i64, String> = tx
        .query::<(i64, String), _>("SELECT game_player_id, player FROM members WHERE game_player_id IS NOT NULL AND player != ''")?
        .into_iter()
        .collect();
    let pending: Vec<mysql::Row> = tx.query(
        "SELECT id, name, player, source, collected_at, game_gift_id, game_type_id, game_player_id, collected_chest_id \
         FROM incomplete_chests WHERE status = 'pending' AND game_type_id IS NOT NULL ORDER BY id")?;
    let mut resolvable: Vec<(i64, String, Sql, Option<i64>, i64, Option<i64>, Option<i64>)> = Vec::new();
    for row in pending {
        let id: i64 = row.get(0).unwrap_or(0);
        let player: Option<String> = row.get(2).unwrap_or(None);
        let at: Sql = row.as_ref(4).cloned().unwrap_or(Sql::NULL);
        let gift: Option<i64> = row.get(5).unwrap_or(None);
        let tid: i64 = row.get::<Option<i64>, _>(6).flatten().unwrap_or(0);
        let pid: Option<i64> = row.get(7).unwrap_or(None);
        let linked: Option<i64> = row.get(8).unwrap_or(None);
        if !canon.contains_key(&tid) {
            continue;
        }
        let mut name = player.unwrap_or_default().trim().to_string();
        if name.is_empty() {
            name = pid.and_then(|p| players.get(&p).cloned()).unwrap_or_default();
        }
        if name.is_empty() {
            continue;
        }
        resolvable.push((id, name, at, gift, tid, pid, linked));
    }
    if resolvable.is_empty() {
        println!("\n  [4] incomplete_chests (pending): 0 pending rows with game_type_id to resolve.");
    } else {
        println!("\n  [4] Pending incomplete_chests resolvable via catalog: {} row(s)", resolvable.len());
        let mut per_type: BTreeMap<i64, usize> = BTreeMap::new();
        for r in &resolvable {
            *per_type.entry(r.4).or_insert(0) += 1;
        }
        for (tid, n) in &per_type {
            let (cn, cs, _) = &canon[tid];
            println!("      + ID {} ({} pending chest(s)) -> '{}' ({})", tid, n, cn, cs);
        }
        if apply {
            let to_insert: Vec<&(i64, String, Sql, Option<i64>, i64, Option<i64>, Option<i64>)> =
                resolvable.iter().filter(|r| r.6.is_none()).collect();
            for chunk in to_insert.chunks(500) {
                let marks = vec!["(?, ?, ?, 1, ?, ?, ?, ?)"; chunk.len()].join(", ");
                let mut params: Vec<Sql> = Vec::new();
                for (_, player, at, gift, tid, pid, _) in chunk {
                    let (cn, cs, _) = &canon[tid];
                    params.extend([cn.clone().into(), player.clone().into(), cs.clone().into(), at.clone(), (*gift).into(),
                                   Sql::Int(*tid), (*pid).into()]);
                }
                tx.exec_drop(format!("INSERT IGNORE INTO collected_chests (name, player, source, type, collected_at, game_gift_id, \
                                      game_type_id, game_player_id) VALUES {}", marks), Params::Positional(params))?;
            }
            tx.query_drop("UPDATE incomplete_chests ic INNER JOIN members m ON m.game_player_id = ic.game_player_id \
                           SET ic.player = m.player WHERE ic.status = 'pending' AND (ic.player IS NULL OR ic.player = '') \
                           AND m.player != ''")?;
            let types: BTreeMap<i64, (String, String)> =
                per_type.keys().map(|t| (*t, (canon[t].0.clone(), canon[t].1.clone()))).collect();
            let ids: Vec<i64> = resolvable.iter().map(|r| r.0).collect();
            for chunk in ids.chunks(500) {
                let (q, mut p) = case_update("incomplete_chests", &types,
                                             ", status = 'corrected', reviewed_at = UTC_TIMESTAMP(), screenshot = NULL");
                // Swap the type filter for the ids of this chunk.
                let n = types.len();
                p.truncate(p.len() - n);
                let q = q.replace(&format!("WHERE game_type_id IN ({})", vec!["?"; n].join(", ")),
                                  &format!("WHERE id IN ({})", vec!["?"; chunk.len()].join(", ")));
                p.extend(chunk.iter().map(|i| Sql::Int(*i)));
                tx.exec_drop(q, Params::Positional(p))?;
            }
            tx.query_drop("UPDATE incomplete_chests ic INNER JOIN collected_chests cc ON cc.game_gift_id = ic.game_gift_id \
                           SET ic.collected_chest_id = cc.id WHERE ic.status = 'corrected' AND ic.collected_chest_id IS NULL \
                           AND ic.game_gift_id IS NOT NULL")?;
        }
    }

    // E. Corrected incomplete_chests with a wrong name or source.
    let groups: Vec<(i64, Option<String>, Option<String>, i64)> = tx.query(
        "SELECT game_type_id, name, source, COUNT(*) FROM incomplete_chests WHERE status = 'corrected' AND game_type_id IS NOT NULL \
         GROUP BY game_type_id, name, source")?;
    let mut header = false;
    for (tid, dn, ds, n) in groups {
        let Some((cn, cs, _)) = canon.get(&tid) else { continue };
        if dn.as_deref().unwrap_or("") != cn || ds.as_deref().unwrap_or("") != cs {
            if !header {
                println!("\n  [5] Historical corrected incomplete_chests with wrong name/source:");
                header = true;
            }
            println!("      ~ ID {} ({} rows): ('{}', '{}') -> ('{}', '{}')", tid, n, dn.unwrap_or_default(), ds.unwrap_or_default(), cn, cs);
            if apply {
                tx.exec_drop("UPDATE incomplete_chests SET name = ?, source = ? WHERE status = 'corrected' AND game_type_id = ? \
                              AND (name != ? OR source != ?)", (cn, cs, tid, cn, cs))?;
            }
        }
    }

    if apply {
        tx.commit()?;
        println!("\n  [OK] All changes committed to the database.\n");
    } else {
        tx.rollback()?;
        println!("\n  [DRY-RUN] No changes written. Run with --apply to apply these fixes.\n");
    }
    Ok(())
}

// ------------------------------------------------------------------- audit

const BLOCK_REACH: i64 = 3;
const BLOCK_MIN_NEIGHBOURS: usize = 3;
const BLOCK_SHARE: f64 = 2.0 / 3.0;
const TWIN_DISTANCE: i64 = 50;
const SEVERAL: f64 = 1.5;
const ONE: f64 = 1.1;
const OUTLIVES_DAYS: i64 = 2;

/// (name, source, rows, gifts, first, last) of each way a type was written.
pub type Written = BTreeMap<i64, Vec<(String, String, i64, i64, NaiveDateTime, NaiveDateTime)>>;

#[derive(Debug, Clone)]
pub struct Finding {
    pub type_id: i64,
    pub name: String,
    pub source: String,
    pub rows: i64,
    pub gifts: i64,
    pub first: NaiveDateTime,
    pub last: NaiveDateTime,
    pub signs: Vec<String>,
    pub hints: Vec<String>,
}

fn most_common<'a>(items: &[&'a str]) -> &'a str {
    let mut counts: Vec<(&str, usize)> = Vec::new();
    for i in items {
        match counts.iter_mut().find(|(k, _)| k == i) {
            Some(c) => c.1 += 1,
            None => counts.push((i, 1)),
        }
    }
    counts.iter().max_by_key(|(_, n)| *n).map(|(k, _)| *k).unwrap_or("")
}

pub fn audit(written: &Written, table: &BTreeMap<i64, (String, String)>, screen: &HashMap<String, HashMap<String, i64>>,
             now: NaiveDateTime) -> Vec<Finding> {
    let label: BTreeMap<i64, (String, String)> =
        written.iter().map(|(t, e)| (*t, e.iter().max_by_key(|x| x.2).map(|x| (x.0.clone(), x.1.clone())).unwrap())).collect();
    let totals: BTreeMap<i64, (i64, i64, NaiveDateTime, NaiveDateTime)> = written
        .iter()
        .map(|(t, e)| (*t, (e.iter().map(|x| x.2).sum(), e.iter().map(|x| x.3).sum(), e.iter().map(|x| x.4).min().unwrap(),
                            e.iter().map(|x| x.5).max().unwrap())))
        .collect();
    let mut by_source: HashMap<String, BTreeSet<i64>> = HashMap::new();
    for (t, (_, s)) in &label {
        by_source.entry(s.clone()).or_default().insert(*t);
    }
    let mut every = table.clone();
    every.extend(label.clone());
    let mut by_label: HashMap<(String, String), BTreeSet<i64>> = HashMap::new();
    for (t, pair) in &label {
        by_label.entry(pair.clone()).or_default().insert(*t);
    }
    let mut found = Vec::new();
    for t in written.keys() {
        let (name, source) = label[t].clone();
        let (rows, gifts, first, last) = totals[t];
        let mut signs = Vec::new();
        let mut hints = Vec::new();
        if let Some(official) = table.get(t) {
            if written[t].iter().any(|e| (&e.0, &e.1) != (&official.0, &official.1)) {
                let wrong: i64 = written[t].iter().filter(|e| (&e.0, &e.1) != (&official.0, &official.1)).map(|e| e.2).sum();
                signs.push(format!("table: {} row(s) not named as game_chest_types does, '{}' ({})", wrong, official.0, official.1));
            }
        }
        if written[t].len() > 1 {
            let mut ways = written[t].clone();
            ways.sort_by_key(|e| -e.2);
            signs.push(format!("names: written as {}", ways.iter().map(|e| format!("'{}' ({}) x{}", e.0, e.1, e.2)).collect::<Vec<_>>().join("; ")));
        }
        let around: Vec<&(String, String)> = (t - BLOCK_REACH..=t + BLOCK_REACH).filter(|n| n != t).filter_map(|n| every.get(&n)).collect();
        if around.len() >= BLOCK_MIN_NEIGHBOURS {
            let sources: Vec<&str> = around.iter().map(|(_, s)| s.as_str()).collect();
            let common = most_common(&sources).to_string();
            let count = sources.iter().filter(|s| **s == common).count();
            if common != source && count as f64 >= BLOCK_SHARE * around.len() as f64 {
                signs.push(format!("block: {} of the {} types around it are '{}'", count, around.len(), common));
                let mut claimed: HashSet<String> =
                    by_source.get(&common).map(|ts| ts.iter().map(|n| label[n].0.clone()).collect()).unwrap_or_default();
                claimed.extend(table.values().filter(|(_, s)| *s == common).map(|(n, _)| n.clone()));
                let mut missing: Vec<String> =
                    screen.get(&common).map(|m| m.keys().filter(|k| !claimed.contains(*k)).cloned().collect()).unwrap_or_default();
                missing.sort();
                if !missing.is_empty() {
                    hints.push(format!("'{}' names no type has carried yet: {}", common, missing.join(", ")));
                }
            }
            let names: Vec<&str> = around.iter().map(|(n, _)| n.as_str()).collect();
            let chest = most_common(&names).to_string();
            let count = names.iter().filter(|n| **n == chest).count();
            if chest != name && count as f64 >= BLOCK_SHARE * around.len() as f64 {
                let mut levels: Vec<&str> = around.iter().filter(|(n, _)| *n == chest).map(|(_, s)| s.as_str()).collect();
                levels.sort();
                levels.dedup();
                signs.push(format!("block: {} of the {} types around it are '{}' ({})", count, around.len(), chest, levels.join(", ")));
                let claimed: HashSet<&String> = every.values().filter(|(n, _)| *n == chest).map(|(_, s)| s).collect();
                let mut missing: Vec<&String> = screen.iter().filter(|(s, names)| names.contains_key(&chest) && !claimed.contains(s)).map(|(s, _)| s).collect();
                missing.sort();
                if !missing.is_empty() {
                    hints.push(format!("'{}' was read on the screen from sources no type carries yet: {}", chest,
                                       missing.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(", ")));
                }
            }
        }
        let twins: Vec<i64> = by_label[&(name.clone(), source.clone())]
            .iter()
            .filter(|n| (*n - t).abs() > TWIN_DISTANCE && totals[n].0 > rows)
            .copied()
            .collect();
        if !twins.is_empty() {
            signs.push(format!("twin: '{}' ({}) is also type {}", name, source,
                               twins.iter().map(|n| format!("{} (x{})", n, totals[n].0)).collect::<Vec<_>>().join(", ")));
        }
        let siblings: Vec<i64> = by_source[&source].iter().filter(|s| *s != t).copied().collect();
        if !siblings.is_empty() && gifts > 0 {
            let own = rows as f64 / gifts as f64;
            let sib_rows: i64 = siblings.iter().map(|s| totals[s].0).sum();
            let sib_gifts: i64 = siblings.iter().map(|s| totals[s].1).sum();
            let theirs = if sib_gifts > 0 { sib_rows as f64 / sib_gifts as f64 } else { 0.0 };
            if sib_rows > rows && ((theirs >= SEVERAL && own <= ONE) || (theirs <= ONE && own >= SEVERAL)) {
                signs.push(format!("quantity: {:.2} chest(s) per gift, the other types of '{}' {:.2}", own, source, theirs));
            }
            let stopped = siblings.iter().map(|s| totals[s].3).max().unwrap();
            if last - stopped >= Duration::days(OUTLIVES_DAYS) && now - stopped >= Duration::days(OUTLIVES_DAYS) {
                signs.push(format!("outlives: still arriving {}, the other types of '{}' stopped {}", last.format("%d/%m"), source,
                                   stopped.format("%d/%m")));
            }
        }
        if !signs.is_empty() {
            found.push(Finding { type_id: *t, name, source, rows, gifts, first, last, signs, hints });
        }
    }
    found.sort_by_key(|f| (-(f.signs.len() as i64), -f.rows));
    found
}

fn audit_main(config: &ConfigManager, opts: &HashMap<String, String>) -> i32 {
    let days: i64 = opts.get("days").and_then(|d| d.parse().ok()).unwrap_or(60);
    let list = profiles(config, opts.get("profile"));
    if list.is_empty() {
        println!("No enabled profile with a database{}.", opts.get("profile").map(|p| format!(" called '{}'", p)).unwrap_or_default());
        return EXIT_MISCONFIGURED;
    }
    let mut seen = HashSet::new();
    let mut failed = 0;
    for p in list {
        let db = p.database.clone().unwrap();
        println!("{}", "=".repeat(78));
        if !seen.insert((db.host.clone(), db.port, db.database.clone())) {
            println!("{}: database '{}' already audited above.\n", p.label(), db.database);
            continue;
        }
        println!("{} - database '{}' on {}:{}", p.label(), db.database, db.host, db.port);
        println!("{}", "=".repeat(78));
        if let Err(e) = audit_profile(&db, days) {
            failed += 1;
            println!("Could not audit this database: {}\n", e);
        }
    }
    if failed > 0 {
        3
    } else {
        EXIT_OK
    }
}

fn audit_profile(db: &DatabaseConfig, days: i64) -> Result<(), String> {
    let (mut conn, _) = open_connection(db, Some(10)).map_err(|e| describe(&e))?;
    let since = chrono::Local::now().naive_local() - Duration::days(days);
    let earlier = since - Duration::days(days);
    let rows: Vec<(i64, Option<String>, Option<String>, i64, i64, Sql, Sql)> = conn
        .exec("SELECT game_type_id, name, source, COUNT(*), COUNT(DISTINCT game_gift_id), MIN(collected_at), MAX(collected_at) \
               FROM collected_chests WHERE game_type_id IS NOT NULL AND collected_at >= ? GROUP BY game_type_id, name, source",
              (crate::db::sql_datetime(since),))
        .map_err(|e| describe(&e))?;
    let mut written: Written = BTreeMap::new();
    for (t, n, s, r, g, f, l) in rows {
        let (Some(f), Some(l)) = (crate::db::datetime_of(&f), crate::db::datetime_of(&l)) else { continue };
        written.entry(t).or_default().push((n.unwrap_or_default(), s.unwrap_or_default(), r, g, f, l));
    }
    let table: BTreeMap<i64, (String, String)> = conn
        .query::<(i64, Option<String>, Option<String>), _>(
            "SELECT t.game_type_id, t.name, COALESCE(s.source, t.source) FROM game_chest_types t \
             LEFT JOIN standard_chests s ON s.id = t.standard_chest_id")
        .map_err(|e| describe(&e))?
        .into_iter()
        .map(|(t, n, s)| (t, (n.unwrap_or_default(), s.unwrap_or_default())))
        .collect();
    let mut screen: HashMap<String, HashMap<String, i64>> = HashMap::new();
    for (s, n, r) in conn
        .exec::<(Option<String>, Option<String>, i64), _, _>(
            "SELECT source, name, COUNT(*) FROM collected_chests WHERE game_type_id IS NULL AND collected_at >= ? GROUP BY source, name",
            (crate::db::sql_datetime(earlier),))
        .map_err(|e| describe(&e))?
    {
        screen.entry(s.unwrap_or_default()).or_default().insert(n.unwrap_or_default(), r);
    }
    let found = audit(&written, &table, &screen, chrono::Local::now().naive_local());
    println!("{} chest types written in the last {} days, {} in game_chest_types.\n", written.len(), days, table.len());
    if found.is_empty() {
        println!("Nothing suspicious.\n");
        return Ok(());
    }
    for f in &found {
        let weight = if f.signs.len() >= 2 { "LIKELY WRONG" } else { "worth a look" };
        println!("[{}] type {}: '{}' ({}) - {} chest(s) in {} gift(s), {} to {}", weight, f.type_id, f.name, f.source, f.rows,
                 f.gifts, f.first.format("%d/%m/%Y"), f.last.format("%d/%m/%Y"));
        for s in &f.signs {
            println!("      {}", s);
        }
        for h in &f.hints {
            println!("      hint: {}", h);
        }
        println!();
    }
    println!("To put a type right (after checking it in the game): correct game_chest_types first, then the rows -\n  \
              UPDATE game_chest_types SET name=..., source=..., standard_chest_id=... WHERE game_type_id=...;\n  \
              UPDATE collected_chests SET name=..., source=... WHERE game_type_id=...;\n");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(days_ago: i64) -> NaiveDateTime {
        crate::schedule::parse_iso("2026-10-19T12:00:00").unwrap() - Duration::days(days_ago)
    }

    fn entry(name: &str, source: &str, rows: i64, gifts: i64, first: i64, last: i64) -> (String, String, i64, i64, NaiveDateTime, NaiveDateTime) {
        (name.into(), source.into(), rows, gifts, at(first), at(last))
    }

    fn crypt_block() -> Written {
        let mut w = Written::new();
        for (t, name) in [(5781, "Bone Chest"), (5782, "Gold Chest"), (5783, "Mayan Chest"), (5785, "Scorpion Chest"),
                          (5786, "Braided Chest"), (5787, "Ruby Chest")] {
            w.insert(t, vec![entry(name, "Level 35 epic Crypt", 40, 40, 30, 1)]);
        }
        w
    }

    #[test]
    fn a_crypt_chest_counted_as_an_event_squads_is_found() {
        let mut w = crypt_block();
        w.insert(5784, vec![entry("Fenrir's Chest", "Epic Fenrir squad", 300, 300, 30, 0)]);
        w.insert(9000, vec![entry("Fenrir's Chest", "Epic Fenrir squad", 900, 300, 30, 10)]);
        let found = audit(&w, &BTreeMap::new(), &HashMap::new(), at(0));
        let f = found.iter().find(|f| f.type_id == 5784).unwrap();
        assert!(f.signs.iter().any(|s| s.starts_with("block:")), "{:?}", f.signs);
        assert!(f.signs.iter().any(|s| s.starts_with("twin:") || s.starts_with("quantity:")), "{:?}", f.signs);
        assert!(f.signs.len() >= 2);
    }

    #[test]
    fn the_table_disagreeing_with_the_rows_is_a_sign_and_chests_named_right_are_left_alone() {
        let mut w = crypt_block();
        let table: BTreeMap<i64, (String, String)> = BTreeMap::from([(5781, ("Skull Chest".to_string(), "Level 35 epic Crypt".to_string()))]);
        let found = audit(&w, &table, &HashMap::new(), at(0));
        assert_eq!(vec![5781], found.iter().map(|f| f.type_id).collect::<Vec<_>>());
        assert!(found[0].signs[0].starts_with("table:"));
        w.remove(&5781);
        assert!(audit(&w, &BTreeMap::new(), &HashMap::new(), at(0)).is_empty());
    }
}
