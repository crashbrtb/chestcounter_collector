//! Total Battle Chest Collector.
//!
//! Collects the clan chests of Total Battle into each profile's MySQL/MariaDB
//! database, keeps the Shield of Peace up, reads the clan's members and sends the
//! tournament rankings to the clan site - all over the game's own network, from
//! inside its tab in Chrome (CDP). Nothing is clicked or read off the screen.

pub mod logger;

pub mod browser;
pub mod cancel;
pub mod cdp;
pub mod cli;
pub mod config;
pub mod context;
pub mod db;
pub mod error;
pub mod gui;
pub mod http;
pub mod journal_dump;
pub mod link;
pub mod mo;
pub mod modules;
pub mod paths;
pub mod run_lock;
pub mod runner;
pub mod schedule;
pub mod session;
pub mod static_data;
pub mod task_scheduler;
pub mod tbcodec;
pub mod text;
pub mod tools;
pub mod unity;

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
