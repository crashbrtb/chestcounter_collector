//! MySQL/MariaDB: one connection per profile, the chest repository and the run's
//! heartbeat in the site's `job_runs`.
//!
//! The charset is pinned to utf8mb4 (player names carry accents and emoji). The
//! collation is named explicitly with a fallback chain, because MySQL 8's default
//! is unknown to MariaDB. Opening a connection lives in one place so that 'Test
//! connection' in the interface and a run hit the server the exact same way.

use std::collections::HashMap;
use std::time::{Duration, Instant};

use chrono::{Datelike, NaiveDateTime, Timelike};
use mysql::prelude::Queryable;
use mysql::{Conn, OptsBuilder, Params, SslOpts, Value as Sql};
use serde_json::{json, Value as Json};

use crate::config::{DatabaseConfig, ProfileConfig};
use crate::modules::ModuleResult;
use crate::text::head;

pub const CHARSET: &str = "utf8mb4";
pub const COLLATIONS: [Option<&str>; 3] = [Some("utf8mb4_general_ci"), Some("utf8mb4_unicode_ci"), None];
const ER_UNKNOWN_COLLATION: u16 = 1273;
const ER_UNKNOWN_CHARSET: u16 = 1115;
const ER_NO_SUCH_TABLE: u16 = 1146;

/// What the game says about a chest read from its traffic.
pub const GAME_COLUMNS: [&str; 3] = ["game_gift_id", "game_type_id", "game_player_id"];

fn code(e: &mysql::Error) -> Option<u16> {
    match e {
        mysql::Error::MySqlError(m) => Some(m.code),
        _ => None,
    }
}

pub fn describe(e: &mysql::Error) -> String {
    match e {
        mysql::Error::MySqlError(m) => format!("{} ({}): {}", m.code, m.state, m.message),
        other => other.to_string(),
    }
}

fn opts(database: &DatabaseConfig, timeout: Option<u64>, tls: bool) -> OptsBuilder {
    let mut b = OptsBuilder::new()
        .ip_or_hostname(Some(database.host.clone()))
        .tcp_port(database.port)
        .user(Some(database.user.clone()))
        .pass(Some(database.password.clone()))
        .db_name(Some(database.database.clone()))
        .prefer_socket(false)
        .tcp_connect_timeout(Some(Duration::from_secs(timeout.unwrap_or(20))));
    if let Some(t) = timeout {
        b = b.read_timeout(Some(Duration::from_secs(t.max(30)))).write_timeout(Some(Duration::from_secs(t.max(30))));
    }
    if tls {
        b = b.ssl_opts(Some(SslOpts::default().with_danger_accept_invalid_certs(true).with_danger_skip_domain_validation(true)));
    }
    b
}

/// Opens a connection, with TLS when the server offers it (as mysql-connector
/// did), trying the collations in order. Returns the collation actually used.
pub fn open_connection(database: &DatabaseConfig, timeout: Option<u64>) -> Result<(Conn, Option<&'static str>), mysql::Error> {
    crate::http::ensure_crypto();
    let mut conn = match Conn::new(opts(database, timeout, true)) {
        Ok(c) => c,
        Err(mysql::Error::DriverError(mysql::DriverError::TlsNotSupported)) => Conn::new(opts(database, timeout, false))?,
        Err(e) if e.to_string().to_lowercase().contains("tls") || e.to_string().to_lowercase().contains("ssl") => {
            Conn::new(opts(database, timeout, false))?
        }
        Err(e) => return Err(e),
    };
    let mut last = None;
    for collation in COLLATIONS {
        let statement = match collation {
            Some(c) => format!("SET NAMES {} COLLATE {}", CHARSET, c),
            None => format!("SET NAMES {}", CHARSET),
        };
        match conn.query_drop(statement) {
            Ok(()) => return Ok((conn, collation)),
            Err(e) if matches!(code(&e), Some(ER_UNKNOWN_COLLATION) | Some(ER_UNKNOWN_CHARSET)) => {
                crate::debug!("Server rejected collation {}: {}", collation.unwrap_or("(default)"), e);
                last = Some(e);
            }
            Err(e) => return Err(e),
        }
    }
    Err(last.unwrap_or(mysql::Error::DriverError(mysql::DriverError::SetupError)))
}

/// (ok, message) - what the 'Test connection' button reports.
pub fn test_connection(database: &DatabaseConfig) -> (bool, String) {
    if !database.is_usable() {
        return (false, "Fill in host, user, and database.".into());
    }
    match open_connection(database, Some(6)) {
        Ok((mut conn, collation)) => {
            let server: String = conn.query_first("SELECT VERSION()").ok().flatten().unwrap_or_default();
            (true, format!("Connected — {}, {}/{}.", server, CHARSET, collation.unwrap_or("server default")))
        }
        Err(e) => (false, describe(&e)),
    }
}

pub fn sql_datetime(d: NaiveDateTime) -> Sql {
    Sql::Date(d.year() as u16, d.month() as u8, d.day() as u8, d.hour() as u8, d.minute() as u8, d.second() as u8,
              d.nanosecond() / 1000)
}

/// A DATETIME as the server sends it, text or binary.
pub fn datetime_of(v: &Sql) -> Option<NaiveDateTime> {
    match v {
        Sql::Date(y, mo, d, h, mi, s, us) => chrono::NaiveDate::from_ymd_opt(*y as i32, *mo as u32, *d as u32)?
            .and_hms_micro_opt(*h as u32, *mi as u32, *s as u32, *us),
        Sql::Bytes(b) => crate::schedule::parse_iso(&String::from_utf8_lossy(b)),
        _ => None,
    }
}

/// What the game said about a chest: the gift it belongs to, its type and who sent it.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct GameIds {
    pub gift: i128,
    pub chest_type: i128,
    pub player: i128,
}

/// The database as the collection uses it. The real one is `ChestRepository`.
pub trait ChestRepo {
    /// Opens the connection again if the server closed it while the collector was elsewhere.
    fn keep_alive(&mut self) -> Result<(), String>;
    /// Game chest type -> (chest name, source).
    fn chest_types(&mut self) -> HashMap<i128, (String, String)>;
    /// Player id in the game -> the name the site knows the member by.
    fn game_players(&mut self) -> HashMap<i128, String>;
    /// Gift id -> how many of its chests the database already has.
    fn chests_of_gifts(&mut self, first: i128, last: i128) -> HashMap<i128, i64>;
    /// The id of the row written, None when the database refused it.
    fn insert_chest(&mut self, name: &str, player: &str, source: &str, collected_at: NaiveDateTime, game: GameIds) -> Option<u64>;
    fn insert_incomplete_chest(&mut self, name: &str, player: &str, source: &str, collected_at: NaiveDateTime,
                               game: GameIds) -> bool;
    fn learn_chest_type(&mut self, type_id: i128, name: &str, source: &str) -> bool;
    fn delete_chest(&mut self, row_id: u64) -> bool;
}

pub struct ChestRepository {
    pub conn: Conn,
    database: DatabaseConfig,
    columns: HashMap<String, bool>,
}

impl ChestRepository {
    pub fn new(conn: Conn, database: DatabaseConfig) -> Self {
        ChestRepository { conn, database, columns: HashMap::new() }
    }

    /// Whether a column exists, asked once and remembered.
    pub fn has_column(&mut self, table: &str, column: &str) -> bool {
        let key = format!("{}.{}", table, column);
        if let Some(v) = self.columns.get(&key) {
            return *v;
        }
        let query = format!("SHOW COLUMNS FROM `{}` LIKE '{}'", table, column.replace('\'', "''"));
        let found = match self.conn.query_iter(query) {
            Ok(mut result) => {
                let any = result.iter().map(|mut s| s.next().is_some()).unwrap_or(false);
                any
            }
            Err(e) => {
                crate::debug!("Could not inspect {}: {}", key, e);
                false
            }
        };
        self.columns.insert(key, found);
        found
    }

    pub fn has_game_columns(&mut self, table: &str) -> bool {
        GAME_COLUMNS.iter().all(|c| self.has_column(table, c))
    }

    /// Whether this database is ready for chests read from the game's traffic.
    pub fn can_read_from_game(&mut self) -> bool {
        self.has_column("game_chest_types", "game_type_id") && self.has_game_columns("collected_chests")
    }

    fn insert_into(&mut self, table: &str, name: &str, player: &str, source: &str, collected_at: NaiveDateTime,
                   game: GameIds) -> Result<u64, mysql::Error> {
        let mut columns = vec!["name", "player", "source"];
        let mut values: Vec<Sql> = vec![name.into(), player.into(), source.into()];
        if self.has_column(table, "collected_at") {
            columns.push("collected_at");
            values.push(sql_datetime(collected_at));
        }
        if self.has_game_columns(table) {
            columns.extend(GAME_COLUMNS);
            values.extend([Sql::Int(game.gift as i64), Sql::Int(game.chest_type as i64), Sql::Int(game.player as i64)]);
        }
        let marks = vec!["?"; values.len()].join(", ");
        self.conn.exec_drop(format!("INSERT INTO {} ({}) VALUES ({})", table, columns.join(", "), marks), Params::Positional(values))?;
        Ok(self.conn.last_insert_id())
    }
}

impl ChestRepo for ChestRepository {
    fn keep_alive(&mut self) -> Result<(), String> {
        if self.conn.ping().is_ok() {
            return Ok(());
        }
        let mut last = String::new();
        for attempt in 0..3 {
            if attempt > 0 {
                std::thread::sleep(Duration::from_secs(2));
            }
            match open_connection(&self.database, None) {
                Ok((conn, _)) => {
                    self.conn = conn;
                    return Ok(());
                }
                Err(e) => last = describe(&e),
            }
        }
        Err(format!("the database connection was lost and could not be opened again: {}", last))
    }

    fn chest_types(&mut self) -> HashMap<i128, (String, String)> {
        let result = (|| -> Result<HashMap<i128, (String, String)>, mysql::Error> {
            self.conn.query_drop(
                "UPDATE game_chest_types t JOIN standard_chests s ON s.source = t.source \
                 SET t.standard_chest_id = s.id WHERE t.standard_chest_id IS NULL",
            )?;
            let mut types = HashMap::new();
            let rows: Vec<(i64, Option<String>, Option<String>)> = self.conn.query(
                "SELECT t.game_type_id, t.name, COALESCE(s.source, t.source) FROM game_chest_types t \
                 LEFT JOIN standard_chests s ON s.id = t.standard_chest_id",
            )?;
            for (id, name, source) in rows {
                types.insert(id as i128, (name.unwrap_or_default(), source.unwrap_or_default()));
            }
            // What the chests already written call a type the table does not name,
            // when they all call it the same.
            let rows: Vec<(i64, Option<String>, Option<String>)> = self.conn.query(
                "SELECT game_type_id, MIN(name), MIN(source) FROM collected_chests \
                 WHERE game_type_id IS NOT NULL GROUP BY game_type_id HAVING COUNT(DISTINCT name, source) = 1",
            )?;
            for (id, name, source) in rows {
                types.entry(id as i128).or_insert((name.unwrap_or_default(), source.unwrap_or_default()));
            }
            Ok(types)
        })();
        result.unwrap_or_else(|e| {
            crate::debug!("Could not read game_chest_types: {}", e);
            HashMap::new()
        })
    }

    fn game_players(&mut self) -> HashMap<i128, String> {
        match self.conn.query::<(i64, Option<String>), _>("SELECT game_player_id, player FROM members WHERE game_player_id IS NOT NULL") {
            Ok(rows) => rows
                .into_iter()
                .filter_map(|(id, name)| name.filter(|n| !n.is_empty()).map(|n| (id as i128, n)))
                .collect(),
            Err(e) => {
                crate::debug!("Could not read the members' game ids: {}", e);
                HashMap::new()
            }
        }
    }

    fn chests_of_gifts(&mut self, first: i128, last: i128) -> HashMap<i128, i64> {
        let mut counts = HashMap::new();
        for table in ["collected_chests", "incomplete_chests"] {
            if !self.has_column(table, "game_gift_id") {
                continue;
            }
            let rows: Result<Vec<(i64, i64)>, _> = self.conn.exec(
                format!("SELECT game_gift_id, COUNT(*) FROM {} WHERE game_gift_id BETWEEN ? AND ? GROUP BY game_gift_id", table),
                (first as i64, last as i64),
            );
            match rows {
                Ok(rows) => {
                    for (gift, count) in rows {
                        *counts.entry(gift as i128).or_insert(0) += count;
                    }
                }
                Err(e) => crate::debug!("Could not count the chests already written: {}", e),
            }
        }
        counts
    }

    fn insert_chest(&mut self, name: &str, player: &str, source: &str, collected_at: NaiveDateTime, game: GameIds) -> Option<u64> {
        match self.insert_into("collected_chests", name, player, source, collected_at, game) {
            Ok(id) => {
                crate::debug!("Inserted chest: '{}', player: '{}', source: '{}', generated {}", name, player, source,
                              collected_at.format("%Y-%m-%d %H:%M:%S"));
                (id > 0).then_some(id)
            }
            Err(e) => {
                crate::error!("Error inserting chest into collected_chests: {}", describe(&e));
                None
            }
        }
    }

    fn insert_incomplete_chest(&mut self, name: &str, player: &str, source: &str, collected_at: NaiveDateTime,
                               game: GameIds) -> bool {
        match self.insert_into("incomplete_chests", name, player, source, collected_at, game) {
            Ok(_) => {
                crate::warn!("Incomplete chest filed for review: '{}', player '{}', source '{}'", name, player, source);
                true
            }
            Err(e) => {
                crate::error!("Error inserting incomplete chest: {}", describe(&e));
                false
            }
        }
    }

    fn learn_chest_type(&mut self, type_id: i128, name: &str, source: &str) -> bool {
        let r = self.conn.exec_drop(
            "INSERT IGNORE INTO game_chest_types (game_type_id, name, source, standard_chest_id, created, modified) \
             VALUES (?, ?, COALESCE((SELECT source FROM standard_chests WHERE source = ? LIMIT 1), ?), \
             (SELECT id FROM standard_chests WHERE source = ? LIMIT 1), UTC_TIMESTAMP(), UTC_TIMESTAMP())",
            (type_id as i64, name, source, source, source),
        );
        match r {
            Ok(()) => self.conn.affected_rows() > 0,
            Err(e) => {
                crate::error!("Could not write chest type {} to game_chest_types: {}", type_id, describe(&e));
                false
            }
        }
    }

    fn delete_chest(&mut self, row_id: u64) -> bool {
        match self.conn.exec_drop("DELETE FROM collected_chests WHERE id = ?", (row_id,)) {
            Ok(()) => self.conn.affected_rows() > 0,
            Err(e) => {
                crate::error!("Could not remove collected_chests row {}: {}", row_id, describe(&e));
                false
            }
        }
    }
}

// --------------------------------------------------------------- heartbeat

const JOB: &str = "collector";

struct Target {
    database: DatabaseConfig,
    profiles: Vec<(String, String)>,
    run_id: Option<u64>,
    unsupported: bool,
}

/// A row per run in `job_runs`, in every database the run writes chests to: the
/// site's health check tells a quiet day from a dead collector by it. Nothing here
/// may break a collection; times come from the database (`UTC_TIMESTAMP()`).
pub struct JobRunReporter {
    targets: Vec<((String, u16, String), Target)>,
    host: String,
    started: Instant,
}

impl JobRunReporter {
    pub fn new(profiles: &[ProfileConfig]) -> Self {
        let mut targets: Vec<((String, u16, String), Target)> = Vec::new();
        for profile in profiles.iter().filter(|p| p.has_database()) {
            let db = profile.database.clone().unwrap();
            let key = (db.host.to_lowercase(), db.port, db.database.to_lowercase());
            if let Some((_, t)) = targets.iter_mut().find(|(k, _)| *k == key) {
                t.profiles.push((profile.account_name.clone(), profile.name.clone()));
            } else {
                targets.push((key, Target { database: db, profiles: vec![(profile.account_name.clone(), profile.name.clone())],
                                            run_id: None, unsupported: false }));
            }
        }
        let host = std::env::var("COMPUTERNAME").or_else(|_| std::env::var("HOSTNAME")).unwrap_or_else(|_| "unknown".into());
        JobRunReporter { targets, host: head(&host, 100), started: Instant::now() }
    }

    pub fn start(&mut self) {
        self.started = Instant::now();
        let host = self.host.clone();
        for (_, target) in self.targets.iter_mut() {
            target.run_id = execute(
                target,
                "INSERT INTO job_runs (job, status, host, started_at, created) VALUES (?, ?, ?, UTC_TIMESTAMP(), UTC_TIMESTAMP())",
                vec![JOB.into(), "running".into(), host.clone().into()],
                true,
            );
        }
    }

    /// Closes the run in every database. `results` holds one entry per profile.
    pub fn finish(&mut self, results: &[ModuleResult], duration: &str, error: Option<&str>, cancelled: bool) {
        let elapsed = self.elapsed();
        let seconds = self.started.elapsed().as_secs() as i64;
        let host = self.host.clone();
        for (_, target) in self.targets.iter_mut() {
            if target.unsupported {
                continue;
            }
            let (status, mut summary) = outcome(target, results, error, cancelled);
            summary["duration"] = json!(if duration.is_empty() { elapsed.clone() } else { duration.to_string() });
            let payload = summary.to_string();
            if let Some(id) = target.run_id {
                execute(target, "UPDATE job_runs SET status = ?, finished_at = UTC_TIMESTAMP(), summary = ? WHERE id = ?",
                        vec![status.into(), payload.into(), Sql::UInt(id)], false);
            } else {
                execute(target,
                        "INSERT INTO job_runs (job, status, host, started_at, finished_at, summary, created) \
                         VALUES (?, ?, ?, UTC_TIMESTAMP() - INTERVAL ? SECOND, UTC_TIMESTAMP(), ?, UTC_TIMESTAMP())",
                        vec![JOB.into(), status.into(), host.clone().into(), Sql::Int(seconds), payload.into()], false);
            }
        }
    }

    fn elapsed(&self) -> String {
        let s = self.started.elapsed().as_secs();
        format!("{}:{:02}:{:02}", s / 3600, s % 3600 / 60, s % 60)
    }
}

fn outcome(target: &Target, results: &[ModuleResult], error: Option<&str>, cancelled: bool) -> (&'static str, Json) {
    let mine: Vec<&(String, String)> = target.profiles.iter().collect();
    let is_mine = |r: &ModuleResult| mine.iter().any(|(a, p)| *a == r.account && *p == r.profile);
    let accounts: Vec<&String> = target.profiles.iter().map(|(a, _)| a).collect();
    let relevant: Vec<&ModuleResult> = results
        .iter()
        .filter(|r| is_mine(r) || (r.profile.is_empty() && accounts.contains(&&r.account)))
        .collect();
    let done = relevant.iter().filter(|r| r.success && is_mine(r)).count();
    let failures: Vec<Json> = relevant
        .iter()
        .filter(|r| !r.success)
        .map(|r| json!({"account": r.account, "profile": r.profile,
                        "reason": if r.reason.is_empty() { "failure".to_string() } else { r.reason.clone() }}))
        .collect();
    let mut summary = json!({
        "collected": relevant.iter().map(|r| r.collected).sum::<i64>(),
        "incomplete": relevant.iter().map(|r| r.incomplete).sum::<i64>(),
        "profiles_done": done,
        "profiles_total": mine.len(),
    });
    if !failures.is_empty() {
        summary["failures"] = Json::Array(failures.iter().take(20).cloned().collect());
    }
    if cancelled {
        summary["message"] = json!(error.unwrap_or("cancelled"));
        return ("cancelled", summary);
    }
    if let Some(e) = error {
        summary["message"] = json!(head(e, 500));
        return ("failed", summary);
    }
    if done == mine.len() && failures.is_empty() {
        return ("success", summary);
    }
    (if done > 0 { "partial" } else { "failed" }, summary)
}

fn execute(target: &mut Target, query: &str, params: Vec<Sql>, insert: bool) -> Option<u64> {
    let label = target.database.database.clone();
    match open_connection(&target.database, Some(10)) {
        Ok((mut conn, _)) => match conn.exec_drop(query, Params::Positional(params)) {
            Ok(()) => insert.then(|| conn.last_insert_id()),
            Err(e) => {
                if code(&e) == Some(ER_NO_SUCH_TABLE) {
                    target.unsupported = true;
                    crate::debug!("Database '{}' has no job_runs table (site not updated); no heartbeat there.", label);
                } else {
                    crate::warn!("Could not record the run heartbeat in '{}': {}", label, describe(&e));
                }
                None
            }
        },
        Err(e) => {
            crate::warn!("Could not record the run heartbeat in '{}': {}", label, describe(&e));
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn target(profiles: &[(&str, &str)]) -> Target {
        Target { database: DatabaseConfig::new(), profiles: profiles.iter().map(|(a, p)| (a.to_string(), p.to_string())).collect(),
                 run_id: None, unsupported: false }
    }

    fn result(account: &str, profile: &str, success: bool, collected: i64) -> ModuleResult {
        ModuleResult { success, collected, account: account.into(), profile: profile.into(),
                       reason: if success { String::new() } else { "boom".into() }, ..Default::default() }
    }

    #[test]
    fn the_heartbeat_says_how_the_run_went_for_its_own_profiles() {
        let t = target(&[("Main", "Alpha"), ("Main", "Beta")]);
        let (status, summary) = outcome(&t, &[result("Main", "Alpha", true, 3), result("Main", "Beta", true, 2),
                                              result("Other", "Gamma", false, 0)], None, false);
        assert_eq!("success", status);
        assert_eq!(5, summary["collected"]);
        assert_eq!(2, summary["profiles_done"]);

        let (status, summary) = outcome(&t, &[result("Main", "Alpha", true, 3), result("Main", "Beta", false, 0)], None, false);
        assert_eq!("partial", status);
        assert_eq!("boom", summary["failures"][0]["reason"]);

        // An account that could not be opened at all fails its profiles.
        let (status, _) = outcome(&t, &[result("Main", "", false, 0)], None, false);
        assert_eq!("failed", status);
        assert_eq!("cancelled", outcome(&t, &[], Some("ESC"), true).0);
        assert_eq!("failed", outcome(&t, &[], Some("crash"), false).0);
    }
}
