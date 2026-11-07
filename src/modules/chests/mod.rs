//! The clan chests of one profile: every gift waiting is written to the profile's
//! database and opened, over the network (see from_game.rs).

pub mod catalog;
pub mod from_game;
pub mod gifts;

use std::sync::Mutex;

use serde_json::{json, Map, Value as Json};

use crate::config::{Field, FieldType, ProfileConfig};
use crate::context::Window;
use crate::db::{open_connection, ChestRepository, JobRunReporter};
use crate::error::Res;
use crate::modules::{opt_f64, Module, ModuleResult};

pub const KEY: &str = "chests";
pub const TITLE: &str = "Clan chests";
pub const DESCRIPTION: &str = "Opens every clan gift of each profile and records it in that profile's database. A profile \
                               without a database is skipped.";

pub fn settings() -> Vec<Field> {
    // Measured on a recorded run of 856 chests opened by clicking: 1.3 per second on
    // average, 4 at the most in any one second.
    vec![Field::new("open_interval", FieldType::Float, "Time between chests (s)", json!(0.2),
                    "The least time between asking the game to open one gift and the next. Each one also waits for the \
                     game's answer, so 0.2 means five per second at the very most.")
        .range(0.2, 5.0)]
}

pub struct ChestCollector {
    pub options: Map<String, Json>,
    reporter: Mutex<Option<JobRunReporter>>,
}

impl ChestCollector {
    pub fn new(options: Map<String, Json>) -> Self {
        ChestCollector { options, reporter: Mutex::new(None) }
    }
}

impl Module for ChestCollector {
    fn key(&self) -> &str {
        KEY
    }
    fn title(&self) -> &str {
        TITLE
    }
    fn options(&self) -> &Map<String, Json> {
        &self.options
    }
    fn configured_for(&self, profile: &ProfileConfig) -> bool {
        profile.has_database()
    }

    /// Opens the run's row in the site's `job_runs`, in every database it will write to.
    fn begin_run(&self, profiles: &[ProfileConfig]) {
        let mut reporter = JobRunReporter::new(profiles);
        reporter.start();
        *self.reporter.lock().unwrap() = Some(reporter);
    }

    fn end_run(&self, results: &[ModuleResult], duration: &str, error: Option<&str>, cancelled: bool) {
        if let Some(mut reporter) = self.reporter.lock().unwrap().take() {
            reporter.finish(results, duration, error, cancelled);
        }
    }

    fn run_for_profile(&self, window: &dyn Window, profile: &ProfileConfig) -> Res<ModuleResult> {
        let base = ModuleResult::for_profile(profile);
        let Some(database) = profile.database.clone().filter(|d| d.is_usable()) else {
            crate::error!("Profile '{}' has no database configured; skipping it.", profile.label());
            return Ok(ModuleResult { reason: "no database configured".into(), ..base });
        };
        let (conn, collation) = match open_connection(&database, None) {
            Ok(c) => c,
            Err(e) => {
                crate::error!("Could not connect to '{}' for '{}': {}", database.database, profile.label(), crate::db::describe(&e));
                return Ok(ModuleResult { reason: "database connection failure".into(), ..base });
            }
        };
        crate::info!("Connected to database '{}' for '{}' ({}/{}).", database.database, profile.label(), crate::db::CHARSET,
                     collation.unwrap_or("server default"));
        let mut repo = ChestRepository::new(conn, database.clone());
        if !repo.can_read_from_game() {
            crate::error!("[{}] This database has no game_chest_types table or no game columns in collected_chests: update \
                           the site and run its migrations.", profile.label());
            return Ok(ModuleResult { reason: "the site's database is too old".into(), ..base });
        }
        let Some(link) = window.game_link() else {
            return Ok(ModuleResult { reason: "could not talk to the game".into(), ..base });
        };
        let clan = match crate::link::clan_id(link.as_ref()) {
            Ok(Some(c)) => c,
            Ok(None) => return Ok(ModuleResult { reason: "the game says this profile has no clan".into(), ..base }),
            Err(e) if matches!(e, crate::error::Error::GameLink(_)) => return Err(crate::error::Error::Other(e.to_string())),
            Err(e) => return Err(e),
        };
        let interval = opt_f64(&self.options, "open_interval", 0.2);
        let mut game = from_game::OverLink(link.as_ref());
        let mut collection = from_game::GameCollection::new(&profile.label(), &mut repo, &mut game, clan, interval,
                                                            Box::new(|ids: &[i128]| catalog::lookup_chest_types(ids)));
        let (collected, incomplete, finished) = collection.run()?;
        let problem = collection.problem.clone();
        drop(collection);
        drop(link);
        crate::info!("Profile '{}': {} chests collected, {} incomplete.", profile.label(), collected, incomplete);
        crate::info!("Database connection closed for '{}'.", profile.label());
        let mut result = ModuleResult { success: finished, collected, incomplete, ..base };
        if !finished {
            result.reason = problem.unwrap_or_else(|| "the collection did not finish".into());
        }
        Ok(result)
    }
}
