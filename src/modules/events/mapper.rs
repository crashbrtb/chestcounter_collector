//! Walks the Journal over the network, and finds the tournament types the site has no name for.
//!
//! 1. the list is asked for page after page, newest first, until the walk reaches
//!    what it was told not to look behind, or the end;
//! 2. each card of the clan's results has its ranking asked for, and the players
//!    whose names are not known yet have their profiles asked for;
//! 3. a type the site's catalogue has no name for is told to the catalogue, nameless;
//!    its rankings wait, and the search is not complete.
//!
//! Which cards are the clan's is in the list: the fourth field of a classic result
//! is 1 for the clan's ranking and 0 for the player's own (a world ranking).

use std::collections::HashSet;
use std::time::{SystemTime, UNIX_EPOCH};

use chrono::{Local, TimeZone};
use serde_json::{json, Value as Json};

use super::journal::{self, JournalCollector, Tournament};
use crate::cancel;
use crate::error::{Error, Res};
use crate::link::JournalSource;
use crate::tbcodec::{expand_blobs, key_text, Value};

pub const DEFAULT_CARDS_PER_PAGE: usize = 4;
pub const STATUS_MAPPED: &str = "mapped";
pub const STATUS_NO_ANSWER: &str = "no answer from the game";
pub const STATUS_UNKNOWN_FORMAT: &str = "the game answered in an unknown format";
pub const STATUS_NO_NAME: &str = "its type has no name yet";
pub const REQUEST_INTERVAL: f64 = 0.2;
pub const MAX_FAILURES: usize = 3;

pub const FINISH_END: &str = "end";
pub const FINISH_CUTOFF: &str = "cutoff";
pub const FINISH_DAYS: &str = "days";
pub const FINISH_MAX_PAGES: &str = "max_pages";
pub const FINISH_NO_JOURNAL: &str = "no_journal";
pub const COMPLETE_REASONS: [&str; 3] = [FINISH_END, FINISH_CUTOFF, FINISH_DAYS];

#[derive(Debug, Clone, PartialEq)]
pub struct MappedCard {
    pub name: String,
    pub page: usize,
    pub card: usize,
    pub status: String,
    pub result_uid: String,
    pub tournament_key: Option<String>,
    pub ended_at: Option<i128>,
    pub unnamed: usize,
    pub ranking: String,
    /// The card a new type is told to the site's catalogue with.
    pub for_catalogue: bool,
}

impl MappedCard {
    pub fn game_type(&self) -> Option<i128> {
        self.tournament_key.as_ref()?.split(':').next()?.parse().ok()
    }
}

#[derive(Debug, Clone)]
pub struct MapperOptions {
    pub max_pages: usize,
    pub cards_per_page: usize,
    /// Look back only to this moment (UTC seconds).
    pub stop_before: Option<i128>,
    /// Look back only this many days, counted as the calendar does.
    pub max_age_days: Option<i64>,
    pub interval: f64,
}

impl Default for MapperOptions {
    fn default() -> Self {
        MapperOptions { max_pages: 200, cards_per_page: DEFAULT_CARDS_PER_PAGE, stop_before: None, max_age_days: None,
                        interval: REQUEST_INTERVAL }
    }
}

pub type KnownName<'a> = &'a mut dyn FnMut(&Tournament) -> String;

pub fn now_f64() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

pub struct JournalMapper<'a> {
    pub feed: &'a mut dyn JournalSource,
    pub collector: &'a mut JournalCollector,
    pub options: MapperOptions,
    known_name: KnownName<'a>,
    on_log: &'a mut dyn FnMut(&str),
    pub cards: Vec<MappedCard>,
    /// The cards with the clans' ranking met on the walk.
    pub clan_cards: Vec<journal::ClanCard>,
    pub finish_reason: String,
    entries: usize,
    failures: usize,
    pointed_out: HashSet<String>,
}

impl<'a> JournalMapper<'a> {
    pub fn new(feed: &'a mut dyn JournalSource, collector: &'a mut JournalCollector, options: MapperOptions,
               known_name: KnownName<'a>, on_log: &'a mut dyn FnMut(&str)) -> Self {
        JournalMapper { feed, collector, options, known_name, on_log, cards: vec![], clan_cards: vec![], finish_reason: FINISH_MAX_PAGES.into(),
                        entries: 0, failures: 0, pointed_out: HashSet::new() }
    }

    fn log(&mut self, text: &str) {
        (self.on_log)(text)
    }

    pub fn run(&mut self) -> Res<Vec<MappedCard>> {
        match self.walk() {
            Ok(()) => {}
            Err(Error::GameLink(e)) => {
                self.log(&format!("The walk stopped: {}.", e));
                self.finish_reason = FINISH_NO_JOURNAL.into();
            }
            Err(e) => return Err(e),
        }
        self.collect_names()?;
        self.hold_new_types();
        Ok(self.cards.clone())
    }

    fn walk(&mut self) -> Res<()> {
        let today = Local::now().date_naive();
        let mut cursor: Option<Value> = None;
        for _ in 0..self.options.max_pages {
            cancel::check()?;
            let page = self.feed.journal_page(cursor.as_ref())?;
            if page.is_empty() {
                self.finish_reason = FINISH_END.into();
                return Ok(());
            }
            self.collector.take_stream(&expand_blobs(&Value::Arr(page.clone())), now_f64());
            for raw in &page {
                let received = raw.at(1).and_then(|v| v.py_int()).unwrap_or(0);
                if let Some(stop) = self.options.stop_before {
                    if received < stop {
                        self.finish_reason = FINISH_CUTOFF.into();
                        self.log("Reached entries older than the last complete search. End.");
                        return Ok(());
                    }
                }
                if let Some(days) = self.options.max_age_days {
                    let date = Local.timestamp_opt(received as i64, 0).single().map(|d| d.date_naive()).unwrap_or(today);
                    if (today - date).num_days() > days {
                        self.finish_reason = FINISH_DAYS.into();
                        self.log(&format!("Reached entries more than {} day(s) old. End.", days));
                        return Ok(());
                    }
                }
                self.take_entry(raw)?;
            }
            cursor = page.last().and_then(|e| e.at(2)).cloned();
            cancel::sleep(self.options.interval)?;
        }
        Ok(())
    }

    fn take_entry(&mut self, raw: &Value) -> Res<()> {
        let position = self.entries;
        self.entries += 1;
        let expanded = expand_blobs(raw);
        if let Some(card) = journal::parse_clan_entry(&expanded) {
            return self.take_clan_card(raw, card);
        }
        let entry = journal::parse_journal_entry(&expanded);
        let kind = raw.at(3).map(key_text).unwrap_or_default();
        let looks_like_a_ranking = kind.find("clan_").map(|i| kind[i + 5..].contains("statistic")).unwrap_or(false);
        if entry.is_none() && looks_like_a_ranking && !self.pointed_out.contains(&kind) {
            self.pointed_out.insert(kind.clone());
            self.log(&format!("A kind of entry nothing reads yet: {}", kind));
        }
        let Some(entry) = entry.filter(|e| e.has_details) else { return Ok(()) };

        let per_page = self.options.cards_per_page.max(1);
        let mut card = MappedCard {
            name: String::new(),
            page: position / per_page + 1,
            card: position % per_page + 1,
            status: STATUS_NO_ANSWER.into(),
            result_uid: entry.result_uid.clone(),
            tournament_key: entry.tournament_key.clone(),
            ended_at: entry.ended_at,
            unnamed: 0,
            ranking: entry.ranking.clone(),
            for_catalogue: false,
        };
        cancel::sleep(self.options.interval)?;
        let entry_uid = raw.at(0).and_then(|v| v.as_str()).unwrap_or("").to_string();
        let answer = match self.feed.ranking(&entry_uid, &entry.result_uid) {
            Ok(a) => a,
            Err(Error::GameLink(e)) => {
                self.failures += 1;
                self.log(&format!("  The ranking of {} ({}) did not come: {}.", entry.tournament_key.as_deref().unwrap_or("None"),
                                  entry.ended_at_text(), e));
                self.cards.push(card);
                if self.failures >= MAX_FAILURES {
                    return Err(Error::GameLink(e));
                }
                return Ok(());
            }
            Err(e) => return Err(e),
        };
        self.failures = 0;
        self.collector.take_stream(&expand_blobs(&Value::Arr(answer)), now_f64());

        if let Some((key, _, _, ranking)) = self.collector.rankings.get(&entry.result_uid).cloned() {
            card.status = STATUS_MAPPED.into();
            card.tournament_key = key.or(entry.tournament_key.clone());
            card.ranking = ranking;
            let probe = Tournament { ranking: card.ranking.clone(), ..Tournament::new(&entry.result_uid, card.tournament_key.as_deref(), None) };
            card.name = (self.known_name)(&probe);
        } else if let Some((kind, sample)) = self.collector.unknown_details.get(&entry.result_uid).cloned() {
            card.status = format!("{}: {}", STATUS_UNKNOWN_FORMAT, kind);
            self.log(&format!("  The game answered a ranking in a format not read yet: {} · {}", kind, sample));
        }
        self.cards.push(card);
        Ok(())
    }

    /// Keeps a card with the clans' ranking, asking the game for its detail when the list does not carry it.
    fn take_clan_card(&mut self, raw: &Value, mut card: journal::ClanCard) -> Res<()> {
        if !card.complete {
            if let Some(result_uid) = card.result_uid.clone() {
                cancel::sleep(self.options.interval)?;
                let entry_uid = raw.at(0).and_then(|v| v.as_str()).unwrap_or("").to_string();
                match self.feed.ranking(&entry_uid, &result_uid) {
                    Ok(answer) => {
                        if !journal::read_stages_detail(&mut card, &expand_blobs(&Value::Arr(answer))) {
                            self.log(&format!("  The clans' results of {} came in a format not read yet.",
                                              card.tournament_key.as_deref().unwrap_or("None")));
                        }
                    }
                    Err(Error::GameLink(e)) => {
                        self.log(&format!("  The clans' results of {} did not come: {}.", card.tournament_key.as_deref().unwrap_or("None"), e))
                    }
                    Err(e) => return Err(e),
                }
            }
        }
        self.clan_cards.push(card);
        Ok(())
    }

    /// Asks the game for every player of these rankings whose name is not known yet.
    fn collect_names(&mut self) -> Res<()> {
        let mut missing: Vec<i128> = Vec::new();
        for card in &self.cards {
            if card.status == STATUS_MAPPED {
                for p in self.collector.missing_profiles(&card.result_uid) {
                    if !missing.contains(&p) {
                        missing.push(p);
                    }
                }
            }
        }
        if !missing.is_empty() {
            self.log(&format!("{} player(s) without a name: asking the game.", missing.len()));
            match self.feed.fetch_profiles(&missing, self.collector) {
                Ok(()) => {}
                Err(Error::GameLink(e)) => self.log(&format!("Could not ask for the players' names: {}.", e)),
                Err(e) => return Err(e),
            }
        }
        for i in 0..self.cards.len() {
            if self.cards[i].status == STATUS_MAPPED {
                self.cards[i].unnamed = self.collector.missing_profiles(&self.cards[i].result_uid).len();
            }
        }
        Ok(())
    }

    /// The rankings of a type without a name wait; its first card is the one told to the catalogue.
    fn hold_new_types(&mut self) {
        let mut waiting: indexmap::IndexMap<String, Vec<usize>> = indexmap::IndexMap::new();
        for (i, card) in self.cards.iter().enumerate() {
            if card.status == STATUS_MAPPED && card.name.is_empty() {
                waiting.entry(catalogue_key(card.game_type().unwrap_or(0), &card.ranking)).or_default().push(i);
            }
        }
        for (key, idx) in waiting {
            for i in &idx {
                self.cards[*i].status = STATUS_NO_NAME.into();
            }
            self.cards[idx[0]].for_catalogue = true;
            self.log(&format!("Tournament {} has no name on the site yet: {} ranking(s) wait for it.", key, idx.len()));
        }
    }
}

/// How a catalogue entry is told apart: a type can have several rankings.
pub fn catalogue_key(game_type: i128, ranking: &str) -> String {
    if ranking.is_empty() {
        game_type.to_string()
    } else {
        format!("{}:{}", game_type, ranking)
    }
}

/// One entry per new type and ranking, nameless, ready for POST /api/v1/tournament-catalog.
pub fn catalogue_entries(cards: &[MappedCard]) -> Vec<Json> {
    cards
        .iter()
        .filter(|c| c.for_catalogue && c.status == STATUS_NO_NAME && c.game_type().is_some())
        .map(|c| {
            let mut entry = json!({"tournament_key": c.tournament_key, "ranking": c.ranking});
            if let Some(e) = c.ended_at.filter(|e| *e != 0) {
                entry["ended_at"] = json!(Tournament::new("", None, Some(e)).ended_at_iso());
            }
            entry
        })
        .collect()
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use crate::arr;
    use crate::error::game;
    use crate::tbcodec::{pack, unpack_stream};
    use std::collections::HashMap;

    pub const NOW: i128 = 1793500000;
    pub const DAY: i128 = 86400;

    pub fn uid(n: i128) -> String {
        format!("11111111-0000-4000-8000-{:012}", n)
    }

    pub fn ranking_entry(n: i128, ended_at: i128, key: (i64, i64), clan: i64) -> Value {
        let summary = Value::Bin(pack(&arr![journal::USER_RESULT, Value::Nil, arr![key.0, key.1], clan]));
        arr![uid(n), ended_at, ended_at * 1000, journal::USER_RESULT, arr![summary], true, false, false, uid(n + 5000)]
    }

    pub fn other_entry(n: i128, ended_at: i128, kind: &str) -> Value {
        arr![uid(n), ended_at, ended_at * 1000, kind, arr![Value::Bin(pack(&arr![kind, "x"]))], true, false, false, Value::Nil]
    }

    /// journal_page, ranking and fetch_profiles as the game gives them.
    pub struct FakeFeed {
        pub entries: Vec<Value>,
        pub players: Vec<i128>,
        pub names: HashMap<i128, String>,
        pub per_request: usize,
        pub asked_rankings: Vec<String>,
        pub asked_profiles: Vec<Vec<i128>>,
        pub asked_sizes: Vec<usize>,
        pub failing: HashSet<String>,
        pub cut_at: Option<i128>,
        pub detail: Option<Value>,
        /// The detail answered for one result uid, before anything else.
        pub details: HashMap<String, Value>,
    }

    impl FakeFeed {
        pub fn new(entries: Vec<Value>) -> Self {
            let players = vec![1001, 1002];
            let names = players.iter().map(|p| (*p, format!("Player {}", p))).collect();
            FakeFeed { entries, players, names, per_request: 3, asked_rankings: vec![], asked_profiles: vec![], asked_sizes: vec![],
                       failing: HashSet::new(), cut_at: None, detail: None, details: HashMap::new() }
        }
    }

    impl JournalSource for FakeFeed {
        fn journal_page(&mut self, cursor: Option<&Value>) -> Res<Vec<Value>> {
            self.asked_sizes.push(self.per_request);
            let c = cursor.and_then(|c| c.py_int());
            if let (Some(cut), Some(c)) = (self.cut_at, c) {
                if c <= cut {
                    return Err(game("the game did not answer"));
                }
            }
            let start = match c {
                None => 0,
                Some(c) => self.entries.iter().position(|e| e.at(2).and_then(|v| v.py_int()) == Some(c)).unwrap() + 1,
            };
            Ok(self.entries.iter().skip(start).take(self.per_request).cloned().collect())
        }

        fn ranking(&mut self, entry_uid: &str, result_uid: &str) -> Res<Vec<Value>> {
            self.asked_rankings.push(result_uid.to_string());
            if self.failing.contains(result_uid) {
                return Err(game("the game did not answer"));
            }
            if let Some(d) = self.details.get(result_uid).or(self.detail.as_ref()) {
                return Ok(vec![arr![Value::Map(vec![(Value::str(result_uid), arr![d.clone(), true])])]]);
            }
            let entry = self.entries.iter().find(|e| e.at(0).and_then(|v| v.as_str()) == Some(entry_uid)).unwrap();
            let summary = unpack_stream(entry.at(4).unwrap().at(0).unwrap().as_bin().unwrap()).unwrap();
            let key = summary[0].at(2).unwrap().clone();
            let rows: Vec<Value> = self.players.iter().enumerate().map(|(i, p)| arr![arr![*p], 100 - i as i64]).collect();
            let detail = Value::Bin(pack(&arr![journal::USER_RESULT, key, 1, arr![arr![1, 1, Value::Arr(rows), arr![], arr![]]]]));
            Ok(vec![arr![Value::Map(vec![(Value::str(result_uid), arr![detail, true])])]])
        }

        fn fetch_profiles(&mut self, ids: &[i128], collector: &mut JournalCollector) -> Res<()> {
            self.asked_profiles.push(ids.to_vec());
            let rows: Vec<Value> = ids
                .iter()
                .filter_map(|p| self.names.get(p).map(|n| arr![arr![*p], 0, n.clone(), "", 0, 0, 0, 0, 0, 0, 5]))
                .collect();
            collector.take_profiles(&arr![arr![Value::Arr(rows)]]);
            Ok(())
        }
    }

    pub struct Walked {
        pub cards: Vec<MappedCard>,
        pub finish: String,
        pub logs: Vec<String>,
        pub collector: JournalCollector,
    }

    pub fn walk(feed: &mut FakeFeed, known: &mut dyn FnMut(&Tournament) -> String, options: MapperOptions) -> Walked {
        let mut collector = JournalCollector::new(None);
        let mut logs = Vec::new();
        let mut log = |t: &str| logs.push(t.to_string());
        let mut m = JournalMapper::new(feed, &mut collector, options, known, &mut log);
        let cards = m.run().unwrap();
        let finish = m.finish_reason.clone();
        drop(m);
        Walked { cards, finish, logs, collector }
    }

    fn quick() -> MapperOptions {
        MapperOptions { interval: 0.0, ..Default::default() }
    }

    fn named(_t: &Tournament) -> String {
        "Epic Hunt".into()
    }

    #[test]
    fn every_page_is_walked_and_only_the_clans_rankings_are_asked_for() {
        let mut feed = FakeFeed::new(vec![ranking_entry(1, NOW, (1024, 1), 1), other_entry(2, NOW - 1, "battle"),
                                          ranking_entry(3, NOW - 2, (1024, 1), 0), ranking_entry(4, NOW - 3, (1024, 1), 1),
                                          other_entry(5, NOW - 4, "battle")]);
        let w = walk(&mut feed, &mut named, quick());
        assert_eq!(vec![uid(5001), uid(5004)], feed.asked_rankings, "not the player's own world ranking");
        assert_eq!(FINISH_END, w.finish);
        assert!(w.cards.iter().all(|c| c.status == STATUS_MAPPED));
        assert_eq!(vec![Some("Player 1001".to_string()), Some("Player 1002".to_string())],
                   w.collector.tournaments()[0].rows.iter().map(|r| r.name.clone()).collect::<Vec<_>>());
    }

    #[test]
    fn the_names_are_asked_for_once_and_only_those_missing() {
        let mut feed = FakeFeed::new(vec![ranking_entry(1, NOW, (1024, 1), 1), ranking_entry(2, NOW - 1, (1024, 1), 1)]);
        feed.names = HashMap::from([(1001, "Lion".to_string())]);
        let w = walk(&mut feed, &mut named, quick());
        assert_eq!(vec![vec![1001, 1002]], feed.asked_profiles);
        assert_eq!(vec![1, 1], w.cards.iter().map(|c| c.unnamed).collect::<Vec<_>>());
    }

    #[test]
    fn it_stops_at_the_last_search() {
        let mut feed = FakeFeed::new(vec![ranking_entry(1, NOW, (1024, 1), 1), other_entry(2, NOW - 100, "battle"),
                                          ranking_entry(3, NOW - 200, (1024, 1), 1)]);
        let w = walk(&mut feed, &mut named, MapperOptions { stop_before: Some(NOW - 50), ..quick() });
        assert_eq!(FINISH_CUTOFF, w.finish);
        assert_eq!(vec![uid(5001)], feed.asked_rankings);
    }

    #[test]
    fn it_stops_at_the_days_to_look_back() {
        let now = Local::now().timestamp() as i128;
        let mut feed = FakeFeed::new(vec![ranking_entry(1, now, (1024, 1), 1), ranking_entry(2, now - 5 * DAY, (1024, 1), 1),
                                          ranking_entry(3, now - 6 * DAY, (1024, 1), 1)]);
        let w = walk(&mut feed, &mut named, MapperOptions { max_age_days: Some(2), ..quick() });
        assert_eq!(FINISH_DAYS, w.finish);
        assert_eq!(vec![uid(5001)], feed.asked_rankings);
    }

    #[test]
    fn a_ranking_that_does_not_come_is_counted_and_the_walk_goes_on() {
        let mut feed = FakeFeed::new(vec![ranking_entry(1, NOW, (1024, 1), 1), ranking_entry(2, NOW - 1, (1024, 1), 1)]);
        feed.failing.insert(uid(5001));
        let w = walk(&mut feed, &mut named, quick());
        assert_eq!(vec![STATUS_NO_ANSWER, STATUS_MAPPED], w.cards.iter().map(|c| c.status.as_str()).collect::<Vec<_>>());
        assert_eq!(FINISH_END, w.finish);
    }

    #[test]
    fn a_game_that_keeps_failing_ends_the_walk_as_not_complete() {
        let mut feed = FakeFeed::new((1..8).map(|n| ranking_entry(n, NOW - n, (1024, 1), 1)).collect());
        feed.per_request = 10;
        feed.failing = (1..8).map(|n| uid(5000 + n)).collect();
        let w = walk(&mut feed, &mut named, quick());
        assert_eq!(MAX_FAILURES, feed.asked_rankings.len());
        assert!(!COMPLETE_REASONS.contains(&w.finish.as_str()));
    }

    #[test]
    fn a_list_that_stops_coming_is_not_a_complete_walk() {
        let entries: Vec<Value> = (1..6).map(|n| ranking_entry(n, NOW - n, (1024, 1), 1)).collect();
        let cut = entries[2].at(2).unwrap().py_int();
        let mut feed = FakeFeed::new(entries);
        feed.cut_at = cut;
        let w = walk(&mut feed, &mut named, quick());
        assert_eq!(FINISH_NO_JOURNAL, w.finish);
        assert_eq!(3, w.cards.len(), "what was read before stays");
    }

    #[test]
    fn a_kind_nobody_reads_is_pointed_out_once() {
        let mut feed = FakeFeed::new(vec![other_entry(1, NOW, "new_clan_war_statistics"), other_entry(2, NOW - 1, "new_clan_war_statistics")]);
        // The pattern is searched anywhere in the kind, as re.search does.
        let w = walk(&mut feed, &mut |_t| String::new(), quick());
        assert_eq!(1, w.logs.iter().filter(|t| t.contains("new_clan_war_statistics")).count());
    }

    #[test]
    fn an_answer_in_an_unknown_shape_says_so() {
        let mut feed = FakeFeed::new(vec![ranking_entry(1, NOW, (1024, 1), 1)]);
        feed.detail = Some(Value::Bin(pack(&arr!["multistage_tournament_results_entry", arr![1040, 1], arr![arr![1, 2, 3]]])));
        let w = walk(&mut feed, &mut named, quick());
        assert!(w.cards[0].status.contains(STATUS_UNKNOWN_FORMAT));
    }

    fn new_type_entries() -> Vec<Value> {
        vec![other_entry(1, NOW, "battle"), other_entry(2, NOW - 1, "battle"), other_entry(3, NOW - 2, "battle"),
             ranking_entry(4, NOW - 3, (1040, 1), 1), ranking_entry(5, NOW - 4, (1040, 2), 1), ranking_entry(6, NOW - 5, (1050, 1), 1)]
    }

    fn known_but(unknown: &'static [&'static str]) -> impl FnMut(&Tournament) -> String {
        move |t: &Tournament| {
            let ty = t.tournament_key.clone().unwrap_or_default();
            if unknown.contains(&ty.split(':').next().unwrap_or("")) { String::new() } else { "Known".into() }
        }
    }

    #[test]
    fn the_first_card_of_a_new_type_goes_to_the_catalogue_and_all_of_it_waits() {
        let mut feed = FakeFeed::new(new_type_entries());
        let w = walk(&mut feed, &mut known_but(&["1040"]), MapperOptions { cards_per_page: 2, ..quick() });
        assert_eq!(vec![STATUS_NO_NAME, STATUS_NO_NAME, STATUS_MAPPED], w.cards.iter().map(|c| c.status.as_str()).collect::<Vec<_>>());
        let entries = catalogue_entries(&w.cards);
        assert_eq!(1, entries.len());
        assert_eq!("1040:1", entries[0]["tournament_key"]);
        assert!(entries[0].get("name").is_none());
        assert_eq!((2, 2), (w.cards[0].page, w.cards[0].card), "the fourth entry, two to a page");
    }

    #[test]
    fn each_new_type_is_told_on_its_own() {
        let mut feed = FakeFeed::new(new_type_entries());
        let w = walk(&mut feed, &mut known_but(&["1040", "1050"]), MapperOptions { cards_per_page: 2, ..quick() });
        let keys: Vec<String> = catalogue_entries(&w.cards).iter().map(|e| e["tournament_key"].as_str().unwrap().to_string()).collect();
        assert_eq!(vec!["1040:1", "1050:1"], keys);
    }

    #[test]
    fn the_clans_cards_are_kept_and_the_stages_one_is_asked_for() {
        use journal::tests::{clan_card_entry, stages_detail_message, stages_entry, OUR_CLAN};
        let stages_uid = "1f0e3089-a073-40bd-83b0-22d490595845";
        let mut feed = FakeFeed::new(vec![
            ranking_entry(1, NOW - 10, (1032, 1), 1),
            stages_entry(&uid(2), stages_uid, NOW - 20),
            clan_card_entry(&uid(3), NOW - 30, (1024, 1), &[(7, 900), (OUR_CLAN, 800)]),
        ]);
        feed.details.insert(stages_uid.to_string(), Value::Bin(pack(&stages_detail_message(1))));
        let mut collector = JournalCollector::new(None);
        let mut log = |_t: &str| {};
        let mut known = named;
        let mut m = JournalMapper::new(&mut feed, &mut collector, quick(), &mut known, &mut log);
        m.run().unwrap();
        let cards = m.clan_cards.clone();
        assert_eq!(1, m.cards.len(), "the clans' cards are not rankings to send");
        assert_eq!(2, cards.len());
        assert!(cards.iter().all(|c| c.complete));
        assert_eq!(Some((1, 43043175)), cards[0].position_of(OUR_CLAN));
        assert_eq!(Some((2, 800)), cards[1].position_of(OUR_CLAN));
        assert!(feed.asked_rankings.contains(&stages_uid.to_string()));
    }

}
