//! Collecting the clan's gifts over the network.
//!
//! 1. the game is asked for the list of gifts;
//! 2. unknown players are named by the game and unknown chest types from the
//!    static chest catalogue (local cache / CDN);
//! 3. each gift whose type has a name, oldest first, is written to the database
//!    and then opened, waiting for the game's answer before the next;
//! 4. the list is asked for again, and the database made to agree with it.
//!
//! Writing comes before opening: a chest written and not opened can be put right,
//! one opened and not written is gone. A gift is written whole and opened whole, and
//! one found in the database already (a run stopped halfway) is opened without being
//! written again.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use crate::cancel;
use crate::db::{ChestRepo, GameIds};
use crate::error::{Error, Res};
use crate::link::pace;

use super::gifts::{tab_name, Gift, KNOWN_KINDS, PLAIN_GIFT};

/// Opening stops after this many refusals in a row.
pub const MAX_FAILURES: usize = 3;
/// How many times the list is walked; the later passes pick up what arrived meanwhile.
pub const MAX_PASSES: usize = 4;
/// A gift with less than this to live is not left for the next run.
pub const ABOUT_TO_EXPIRE: i128 = 60 * 60;

/// The game as the collection asks it.
pub trait GiftGame {
    fn gifts(&mut self, clan_id: i128) -> Res<Vec<Gift>>;
    fn open_gift(&mut self, gift: &Gift) -> Res<()>;
    fn player_names(&mut self, player_ids: &[i128]) -> Res<HashMap<i128, String>>;
}

/// A link to the real game, seen as a GiftGame.
pub struct OverLink<'a>(pub &'a dyn crate::link::GameApi);

impl GiftGame for OverLink<'_> {
    fn gifts(&mut self, clan_id: i128) -> Res<Vec<Gift>> {
        crate::link::gifts(self.0, clan_id)
    }
    fn open_gift(&mut self, gift: &Gift) -> Res<()> {
        crate::link::open_gift(self.0, gift)
    }
    fn player_names(&mut self, ids: &[i128]) -> Res<HashMap<i128, String>> {
        crate::link::player_names(self.0, ids)
    }
}

pub type LookupTypes = Box<dyn FnMut(&[i128]) -> HashMap<i128, (String, String)>>;

pub fn now_seconds() -> i128 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i128).unwrap_or(0)
}

pub struct GameCollection<'a> {
    label: String,
    repo: &'a mut dyn ChestRepo,
    game: &'a mut dyn GiftGame,
    clan_id: i128,
    interval: f64,
    lookup_types: LookupTypes,
    failure_wait: f64,

    types: HashMap<i128, (String, String)>,
    players: HashMap<i128, String>,
    written: HashMap<i128, Vec<u64>>,
    pub collected: i64,
    pub incomplete: i64,
    warned_kinds: bool,
    set_aside: BTreeSet<i128>,
    unnamed: BTreeSet<i128>,
    catalog_tried: HashSet<i128>,
    /// Why the run did not finish, for the run's record.
    pub problem: Option<String>,
}

impl<'a> GameCollection<'a> {
    pub fn new(label: &str, repo: &'a mut dyn ChestRepo, game: &'a mut dyn GiftGame, clan_id: i128, interval: f64,
               lookup_types: LookupTypes) -> Self {
        let types = repo.chest_types();
        let players = repo.game_players();
        GameCollection {
            label: label.to_string(),
            repo,
            game,
            clan_id,
            interval,
            lookup_types,
            failure_wait: 1.0,
            types,
            players,
            written: HashMap::new(),
            collected: 0,
            incomplete: 0,
            warned_kinds: false,
            set_aside: BTreeSet::new(),
            unnamed: BTreeSet::new(),
            catalog_tried: HashSet::new(),
            problem: None,
        }
    }

    pub fn failure_wait(mut self, seconds: f64) -> Self {
        self.failure_wait = seconds;
        self
    }

    /// (collected, incomplete, finished). Not finished means something was left that should not have been.
    pub fn run(&mut self) -> Res<(i64, i64, bool)> {
        crate::info!("[{}] Collecting the gifts over the network ({} chest types and {} players known)...", self.label,
                     self.types.len(), self.players.len());
        match self.walk() {
            Ok(Some(done)) => return Ok(done),
            Ok(None) => {}
            Err(Error::GameLink(e)) => {
                crate::warn!("[{}] {}. What was written and not opened is opened on the next run.", self.label, e);
                self.problem = Some("the game stopped answering".into());
                return Ok((self.collected, self.incomplete, false));
            }
            Err(e) => return Err(e),
        }
        crate::info!("[{}] {} collected, {} incomplete.", self.label, self.collected, self.incomplete);
        if !self.set_aside.is_empty() {
            let tabs: Vec<String> = self.set_aside.iter().map(|k| tab_name(*k)).collect();
            self.problem = Some(format!("the game did not open the gifts of the {} tab over the network; they were left in it",
                                        tabs.join(", ")));
            return Ok((self.collected, self.incomplete, false));
        }
        if !self.unnamed.is_empty() {
            let list: Vec<String> = self.unnamed.iter().map(|t| t.to_string()).collect();
            self.problem = Some(format!("chest types left unnamed in the game: [{}]", list.join(", ")));
            return Ok((self.collected, self.incomplete, false));
        }
        Ok((self.collected, self.incomplete, true))
    }

    /// The passes over the list. Some(result) when the run ended early.
    fn walk(&mut self) -> Res<Option<(i64, i64, bool)>> {
        let mut attempted: Vec<Gift> = Vec::new();
        let mut passes = 0;
        loop {
            let gifts = self.game.gifts(self.clan_id)?;
            self.settle(&attempted, &gifts);
            self.set_aside_unopened(&attempted, &gifts);
            let waiting = self.waiting(&gifts);
            self.name_players(&gifts)?;
            self.name_types(&waiting);

            let ready: Vec<Gift> = waiting.iter().copied().filter(|g| self.ready(g)).collect();
            if !ready.is_empty() && passes >= MAX_PASSES {
                let n: i128 = ready.iter().map(|g| g.quantity).sum();
                crate::info!("[{}] {} chest(s) arrived after the last pass; they are left for the next run.", self.label, n);
                return Ok(None);
            }
            if !ready.is_empty() {
                passes += 1;
                let (tried, finished) = self.open(&ready)?;
                attempted = tried;
                if !finished {
                    let listed = self.game.gifts(self.clan_id)?;
                    self.settle(&attempted, &listed);
                    self.problem = Some("the game stopped answering".into());
                    return Ok(Some((self.collected, self.incomplete, false)));
                }
                continue;
            }
            attempted = Vec::new();
            let nameless: Vec<Gift> = waiting.iter().copied().filter(|g| !self.unnamed.contains(&g.chest_type)).collect();
            if nameless.is_empty() {
                return Ok(None);
            }
            for gift in nameless {
                self.unnamed.insert(gift.chest_type);
                crate::warn!("[{}] Chest type {} is not in the catalog [gift {}]; left in the game unless about to expire.",
                             self.label, gift.chest_type, gift.gift_id);
            }
        }
    }

    /// Whether it can be written and opened now: its type is named, or it has no more time to wait.
    fn ready(&self, gift: &Gift) -> bool {
        self.types.contains_key(&gift.chest_type)
            || (self.unnamed.contains(&gift.chest_type) && gift.expires_at - now_seconds() < ABOUT_TO_EXPIRE)
    }

    /// What can be opened: an expired gift stays listed until someone deletes it, and opens nothing.
    fn waiting(&mut self, gifts: &[Gift]) -> Vec<Gift> {
        let strange: Vec<&Gift> = gifts.iter().filter(|g| !KNOWN_KINDS.contains(&g.kind)).collect();
        if !strange.is_empty() && !self.warned_kinds {
            self.warned_kinds = true;
            crate::warn!("[{}] {} gift(s) of a kind not seen before were left in the game (e.g. {}).", self.label,
                         strange.len(), strange[0]);
        }
        let now = now_seconds();
        gifts
            .iter()
            .copied()
            .filter(|g| KNOWN_KINDS.contains(&g.kind) && !self.set_aside.contains(&g.kind) && g.expires_at > now)
            .collect()
    }

    /// Leaves the gifts of a tab other than Gifts in the game for the rest of the run.
    fn set_tab_aside(&mut self, gift: &Gift, why: &str) {
        if !self.set_aside.insert(gift.kind) {
            return;
        }
        crate::warn!("[{}] The game {} [gift {}, tab {}]: the gifts of that tab are left in the game for the next run.",
                     self.label, why, gift.gift_id, tab_name(gift.kind));
    }

    /// A triumphal gift asked to be opened and still listed as it was: the game does not open them this way.
    fn set_aside_unopened(&mut self, attempted: &[Gift], listed: &[Gift]) {
        for gift in attempted {
            let still = listed.iter().find(|g| g.gift_id == gift.gift_id);
            if gift.kind != PLAIN_GIFT && still.map(|s| s.quantity >= gift.quantity).unwrap_or(false) {
                self.set_tab_aside(gift, "did not open a gift it was asked to");
            }
        }
    }

    fn name_players(&mut self, gifts: &[Gift]) -> Res<()> {
        let unknown: Vec<i128> =
            gifts.iter().map(|g| g.player_id).filter(|p| !self.players.contains_key(p)).collect::<BTreeSet<_>>().into_iter().collect();
        if !unknown.is_empty() {
            let named = self.game.player_names(&unknown)?;
            let n = named.len();
            self.players.extend(named);
            crate::info!("[{}] {} of {} player(s) the site's member list does not know were named by the game.",
                         self.label, n, unknown.len());
        }
        Ok(())
    }

    /// Resolves unknown chest types from the static catalogue and saves them to `game_chest_types`.
    fn name_types(&mut self, gifts: &[Gift]) {
        let unknown: Vec<i128> = gifts
            .iter()
            .map(|g| g.chest_type)
            .filter(|t| !self.types.contains_key(t) && !self.catalog_tried.contains(t))
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        if unknown.is_empty() {
            return;
        }
        self.catalog_tried.extend(&unknown);
        let named = (self.lookup_types)(&unknown);
        if named.is_empty() {
            return;
        }
        let mut ids: Vec<&i128> = named.keys().collect();
        ids.sort();
        for id in ids {
            let (name, source) = &named[id];
            self.repo.learn_chest_type(*id, name, source);
            self.types.insert(*id, (name.clone(), source.clone()));
        }
        self.types.extend(self.repo.chest_types());
        crate::info!("[{}] {} of {} new chest type(s) were named from the game catalog and saved to game_chest_types.",
                     self.label, named.len(), unknown.len());
    }

    /// Writes and opens each gift. Returns the gifts it tried, and false when it had to give up.
    fn open(&mut self, waiting: &[Gift]) -> Res<(Vec<Gift>, bool)> {
        if let Err(e) = self.repo.keep_alive() {
            return Err(Error::Other(e));
        }
        let in_database = self.repo.chests_of_gifts(waiting[0].gift_id, waiting[waiting.len() - 1].gift_id);
        let already = waiting
            .iter()
            .filter(|g| in_database.get(&g.gift_id).copied().unwrap_or(0) > 0 && !self.written.contains_key(&g.gift_id))
            .count();
        if already > 0 {
            crate::info!("[{}] {} gift(s) are in the database already, from a run that did not finish; they are opened \
                          without being written again.", self.label, already);
        }
        let mut attempted = Vec::new();
        let mut failures = 0;
        for gift in waiting {
            cancel::check()?;
            if self.set_aside.contains(&gift.kind) {
                continue;
            }
            if in_database.get(&gift.gift_id).copied().unwrap_or(0) == 0 && !self.written.contains_key(&gift.gift_id) {
                self.write(gift);
            }
            attempted.push(*gift);
            let started = Instant::now();
            match self.game.open_gift(gift) {
                Ok(()) => failures = 0,
                Err(Error::GameLink(e)) => {
                    if gift.kind != PLAIN_GIFT {
                        // Taken back by the settling if it is still there; not counted against the Gifts tab.
                        self.set_tab_aside(gift, &format!("answered '{}' to opening a gift", e));
                        continue;
                    }
                    failures += 1;
                    crate::warn!("[{}] Gift {} was not opened: {} ({}/{}).", self.label, gift.gift_id, e, failures, MAX_FAILURES);
                    if failures >= MAX_FAILURES {
                        return Ok((attempted, false));
                    }
                    cancel::sleep(self.failure_wait)?;
                }
                Err(e) => return Err(e),
            }
            cancel::sleep(pace(started, self.interval))?;
        }
        Ok((attempted, true))
    }

    /// Writes every chest of a gift, or files what is known of it when something has no name.
    fn write(&mut self, gift: &Gift) {
        let game = GameIds { gift: gift.gift_id, chest_type: gift.chest_type, player: gift.player_id };
        let (name, source) = self.types.get(&gift.chest_type).cloned().unwrap_or_default();
        let player = self.players.get(&gift.player_id).cloned().unwrap_or_default();
        let times = if gift.quantity > 1 { format!(" x{}", gift.quantity) } else { String::new() };
        let generated = gift.generated_at();
        let mut rows = Vec::new();
        if !name.is_empty() && !source.is_empty() && !player.is_empty() {
            for _ in 0..gift.quantity {
                if let Some(id) = self.repo.insert_chest(&name, &player, &source, generated, game) {
                    rows.push(id);
                }
            }
            self.collected += rows.len() as i64;
            let complete = rows.len() as i128 == gift.quantity;
            let missing = gift.quantity - rows.len() as i128;
            self.written.insert(gift.gift_id, rows);
            if complete {
                crate::info!("Chest recorded -> '{}' from '{}' ({}){}, generated at {} [gift {}]", name, player, source, times,
                             generated.format("%H:%M"), gift.gift_id);
                return;
            }
            crate::error!("[{}] Database rejected {} chest(s) of gift {}.", self.label, missing, gift.gift_id);
            for _ in 0..missing {
                self.repo.insert_incomplete_chest(&name, &player, &source, generated, game);
            }
            self.incomplete += missing as i64;
        } else {
            self.written.insert(gift.gift_id, rows);
            crate::warn!("[{}] Incomplete chest{} -> name='{}' player='{}' source='{}' [gift {}, type {}, player {}]. Sent to \
                          review queue.", self.label, times, name, player, source, gift.gift_id, gift.chest_type, gift.player_id);
            for _ in 0..gift.quantity {
                self.repo.insert_incomplete_chest(&name, &player, &source, generated, game);
            }
            self.incomplete += gift.quantity as i64;
        }
    }

    /// Takes back what this run wrote for a gift the game says is still waiting, untouched.
    fn settle(&mut self, attempted: &[Gift], listed: &[Gift]) {
        for gift in attempted {
            let Some(still) = listed.iter().find(|g| g.gift_id == gift.gift_id) else { continue };
            if still.quantity < gift.quantity {
                continue;
            }
            let rows = self.written.remove(&gift.gift_id).unwrap_or_default();
            let removed = rows.iter().filter(|r| self.repo.delete_chest(**r)).count() as i64;
            self.collected -= removed;
            if removed > 0 {
                crate::info!("Gift {} was written but is still waiting in the game: {} row(s) taken back, it will be \
                              collected when it is opened.", gift.gift_id, removed);
            }
        }
    }
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use crate::modules::chests::gifts::{parse_gift_row, GIFT_LIFETIME_SECONDS};
    use crate::{arr, error::game, tbcodec::Value};
    use chrono::NaiveDateTime;
    use std::cell::RefCell;
    use std::rc::Rc;

    pub const ANDRA: i128 = 901943467017;
    pub const PIPO: i128 = 489626671378;
    pub const KALEL: i128 = 4496830759598;
    pub const BONE: i128 = 5512;
    pub const SCORPION: i128 = 5782;
    pub const BRIAREUS: i128 = 702062;
    pub const CLAN: i128 = 936302870583;

    pub fn soon() -> i128 {
        now_seconds() + 17 * 3600
    }

    pub fn row(gift_id: i128, player: i128, chest_type: i128, quantity: i128, expires: i128, kind: i128) -> Value {
        arr![gift_id, arr![player], chest_type, expires, quantity, kind]
    }

    pub fn plain(gift_id: i128) -> Value {
        row(gift_id, ANDRA, BONE, 1, soon(), 1)
    }

    #[derive(Default)]
    pub struct Repo {
        pub types: HashMap<i128, (String, String)>,
        pub players: HashMap<i128, String>,
        pub rows: std::collections::BTreeMap<u64, (String, String, String, NaiveDateTime, i128)>,
        pub incomplete: Vec<(String, String, String, i128, i128)>,
        pub learned: HashMap<i128, (String, String)>,
        pub existing: HashMap<i128, i64>,
        pub refuse: bool,
        next: u64,
    }

    impl Repo {
        pub fn standard() -> Repo {
            let mut r = Repo::default();
            r.types.insert(BONE, ("Bone Chest".into(), "Level 25 Crypt".into()));
            r.types.insert(SCORPION, ("Scorpion Chest".into(), "Level 35 epic Crypt".into()));
            r.types.insert(BRIAREUS, ("Briareus Chest".into(), "Epic Briareus squad".into()));
            r.players.insert(ANDRA, "AndraBianca".into());
            r.players.insert(PIPO, "Pipolebon".into());
            r.players.insert(KALEL, "KALEL".into());
            r.next = 100;
            r
        }
        pub fn gifts_written(&self) -> Vec<i128> {
            let mut g: Vec<i128> = self.rows.values().map(|r| r.4).collect();
            g.sort();
            g
        }
    }

    impl ChestRepo for Repo {
        fn keep_alive(&mut self) -> Result<(), String> {
            Ok(())
        }
        fn chest_types(&mut self) -> HashMap<i128, (String, String)> {
            self.types.clone()
        }
        fn game_players(&mut self) -> HashMap<i128, String> {
            self.players.clone()
        }
        fn chests_of_gifts(&mut self, first: i128, last: i128) -> HashMap<i128, i64> {
            let mut counts: HashMap<i128, i64> =
                self.existing.iter().filter(|(g, _)| first <= **g && **g <= last).map(|(g, n)| (*g, *n)).collect();
            for r in self.rows.values() {
                if first <= r.4 && r.4 <= last {
                    *counts.entry(r.4).or_insert(0) += 1;
                }
            }
            for i in &self.incomplete {
                *counts.entry(i.3).or_insert(0) += 1;
            }
            counts
        }
        fn insert_chest(&mut self, name: &str, player: &str, source: &str, at: NaiveDateTime, game: GameIds) -> Option<u64> {
            if self.refuse {
                return None;
            }
            self.next += 1;
            self.rows.insert(self.next, (name.into(), player.into(), source.into(), at, game.gift));
            Some(self.next)
        }
        fn insert_incomplete_chest(&mut self, name: &str, player: &str, source: &str, _at: NaiveDateTime, game: GameIds) -> bool {
            self.incomplete.push((name.into(), player.into(), source.into(), game.gift, game.chest_type));
            true
        }
        fn learn_chest_type(&mut self, id: i128, name: &str, source: &str) -> bool {
            self.learned.insert(id, (name.into(), source.into()));
            self.types.entry(id).or_insert((name.into(), source.into()));
            true
        }
        fn delete_chest(&mut self, row: u64) -> bool {
            self.rows.remove(&row).is_some()
        }
    }

    /// The game as the collection sees it: a list of gifts, opened on request.
    #[derive(Default)]
    pub struct Game {
        pub waiting: std::collections::BTreeMap<i128, Gift>,
        pub opened: Vec<i128>,
        pub refused: HashSet<i128>,
        pub ignored: HashSet<i128>,
        pub lost: HashSet<i128>,
        pub arrivals: Vec<Value>,
        pub lists_left: Option<usize>,
        pub names: HashMap<i128, String>,
        pub on_open: Option<Rc<RefCell<Vec<Vec<i128>>>>>,
    }

    impl Game {
        pub fn new(rows: Vec<Value>) -> Game {
            let mut g = Game::default();
            for r in rows {
                let gift = parse_gift_row(&r).unwrap();
                g.waiting.insert(gift.gift_id, gift);
            }
            g
        }
    }

    impl GiftGame for Game {
        fn gifts(&mut self, _clan: i128) -> Res<Vec<Gift>> {
            if let Some(left) = self.lists_left.as_mut() {
                if *left == 0 {
                    return Err(game("the game's server did not answer in time"));
                }
                *left -= 1;
            }
            Ok(self.waiting.values().copied().collect())
        }
        fn open_gift(&mut self, gift: &Gift) -> Res<()> {
            if self.refused.contains(&gift.gift_id) {
                return Err(game("refused"));
            }
            if self.ignored.contains(&gift.gift_id) {
                return Ok(());
            }
            self.waiting.remove(&gift.gift_id);
            self.opened.push(gift.gift_id);
            for new in std::mem::take(&mut self.arrivals) {
                let parsed = parse_gift_row(&new).unwrap();
                self.waiting.insert(parsed.gift_id, parsed);
            }
            if self.lost.contains(&gift.gift_id) {
                return Err(game("the game's server did not answer in time"));
            }
            Ok(())
        }
        fn player_names(&mut self, ids: &[i128]) -> Res<HashMap<i128, String>> {
            Ok(ids.iter().filter_map(|p| self.names.get(p).map(|n| (*p, n.clone()))).collect())
        }
    }

    fn no_catalog() -> LookupTypes {
        Box::new(|_| HashMap::new())
    }

    fn collect(game: &mut Game, repo: &mut Repo) -> (i64, i64, bool) {
        GameCollection::new("Main / Alpha", repo, game, CLAN, 0.0, no_catalog()).failure_wait(0.0).run().unwrap()
    }

    #[test]
    fn every_gift_is_written_and_opened_oldest_first() {
        let soon = soon();
        let mut game = Game::new(vec![plain(10), row(11, PIPO, SCORPION, 1, soon, 1), row(12, KALEL, BRIAREUS, 3, soon, 1), plain(13)]);
        let mut repo = Repo::standard();
        assert_eq!((6, 0, true), collect(&mut game, &mut repo));
        assert_eq!(vec![10, 11, 12, 12, 12, 13], repo.gifts_written());
        assert_eq!(vec![10, 11, 12, 13], game.opened);
        let briareus: Vec<_> = repo.rows.values().filter(|r| r.4 == 12).collect();
        assert!(briareus.iter().all(|r| (r.0.as_str(), r.1.as_str(), r.2.as_str()) == ("Briareus Chest", "KALEL", "Epic Briareus squad")));
        let generated = chrono::Local.from_local_datetime(&briareus[0].3).unwrap().timestamp() as i128;
        assert_eq!(soon - GIFT_LIFETIME_SECONDS, generated);
    }

    use chrono::TimeZone;

    #[test]
    fn a_gift_is_written_before_it_is_opened() {
        struct SharedRepo(Rc<RefCell<Repo>>);
        impl ChestRepo for SharedRepo {
            fn keep_alive(&mut self) -> Result<(), String> {
                Ok(())
            }
            fn chest_types(&mut self) -> HashMap<i128, (String, String)> {
                self.0.borrow_mut().chest_types()
            }
            fn game_players(&mut self) -> HashMap<i128, String> {
                self.0.borrow_mut().game_players()
            }
            fn chests_of_gifts(&mut self, a: i128, b: i128) -> HashMap<i128, i64> {
                self.0.borrow_mut().chests_of_gifts(a, b)
            }
            fn insert_chest(&mut self, n: &str, p: &str, s: &str, at: NaiveDateTime, g: GameIds) -> Option<u64> {
                self.0.borrow_mut().insert_chest(n, p, s, at, g)
            }
            fn insert_incomplete_chest(&mut self, n: &str, p: &str, s: &str, at: NaiveDateTime, g: GameIds) -> bool {
                self.0.borrow_mut().insert_incomplete_chest(n, p, s, at, g)
            }
            fn learn_chest_type(&mut self, id: i128, n: &str, s: &str) -> bool {
                self.0.borrow_mut().learn_chest_type(id, n, s)
            }
            fn delete_chest(&mut self, row: u64) -> bool {
                self.0.borrow_mut().delete_chest(row)
            }
        }
        struct Watching {
            game: Game,
            repo: Rc<RefCell<Repo>>,
            seen: Vec<Vec<i128>>,
        }
        impl GiftGame for Watching {
            fn gifts(&mut self, c: i128) -> Res<Vec<Gift>> {
                self.game.gifts(c)
            }
            fn open_gift(&mut self, g: &Gift) -> Res<()> {
                self.seen.push(self.repo.borrow().gifts_written());
                self.game.open_gift(g)
            }
            fn player_names(&mut self, ids: &[i128]) -> Res<HashMap<i128, String>> {
                self.game.player_names(ids)
            }
        }
        let repo = Rc::new(RefCell::new(Repo::standard()));
        let mut shared = SharedRepo(repo.clone());
        let mut watching = Watching { game: Game::new(vec![plain(10)]), repo, seen: vec![] };
        GameCollection::new("x", &mut shared, &mut watching, CLAN, 0.0, no_catalog()).run().unwrap();
        assert_eq!(vec![vec![10]], watching.seen);
    }

    #[test]
    fn no_gifts_is_a_finished_run() {
        assert_eq!((0, 0, true), collect(&mut Game::new(vec![]), &mut Repo::standard()));
    }

    #[test]
    fn requests_start_at_least_the_interval_apart() {
        let mut game = Game::new(vec![plain(10), plain(11), plain(12)]);
        let mut repo = Repo::standard();
        let started = Instant::now();
        GameCollection::new("x", &mut repo, &mut game, CLAN, 0.05, no_catalog()).run().unwrap();
        assert!(started.elapsed().as_secs_f64() >= 0.14);
    }

    #[test]
    fn a_gift_already_in_the_database_is_opened_and_not_written_again() {
        let mut game = Game::new(vec![plain(10), row(11, KALEL, BRIAREUS, 2, soon(), 1), plain(12)]);
        let mut repo = Repo::standard();
        repo.existing.insert(11, 2);
        assert_eq!((2, 0, true), collect(&mut game, &mut repo));
        assert_eq!(vec![10, 12], repo.gifts_written());
        assert_eq!(vec![10, 11, 12], game.opened);
    }

    #[test]
    fn a_gift_arriving_during_the_run_is_collected() {
        let mut game = Game::new(vec![plain(10), plain(11)]);
        game.arrivals = vec![row(12, PIPO, SCORPION, 1, soon(), 1)];
        let mut repo = Repo::standard();
        assert_eq!((3, 0, true), collect(&mut game, &mut repo));
        assert_eq!(vec![10, 11, 12], repo.gifts_written());
    }

    #[test]
    fn an_expired_gift_is_neither_written_nor_opened() {
        let mut game = Game::new(vec![row(10, ANDRA, BONE, 1, now_seconds() - 60, 1), plain(11)]);
        let mut repo = Repo::standard();
        assert_eq!((1, 0, true), collect(&mut game, &mut repo));
        assert_eq!(vec![11], game.opened);
        assert_eq!(vec![11], repo.gifts_written());
    }

    #[test]
    fn a_gift_of_an_unknown_kind_is_left_in_the_game() {
        let mut game = Game::new(vec![row(10, ANDRA, BONE, 1, soon(), 3), plain(11)]);
        assert_eq!((1, 0, true), collect(&mut game, &mut Repo::standard()));
        assert_eq!(vec![11], game.opened);
        assert_eq!(vec![10], game.waiting.keys().copied().collect::<Vec<_>>());
    }

    #[test]
    fn a_triumphal_gift_is_written_and_opened_like_the_others() {
        let mut game = Game::new(vec![plain(10), row(11, PIPO, SCORPION, 3, soon(), 2), plain(12)]);
        let mut repo = Repo::standard();
        assert_eq!((5, 0, true), collect(&mut game, &mut repo));
        assert_eq!(vec![10, 11, 12], game.opened);
        assert_eq!(vec![10, 11, 11, 11, 12], repo.gifts_written());
    }

    #[test]
    fn a_triumphal_gift_the_game_refuses_sets_the_tab_aside_and_the_gifts_tab_goes_on() {
        let s = soon();
        let mut game = Game::new(vec![row(10, ANDRA, BONE, 1, s, 2), plain(11), row(12, ANDRA, BONE, 1, s, 2), plain(13), plain(14), plain(15)]);
        game.refused.insert(10);
        let mut repo = Repo::standard();
        let mut c = GameCollection::new("x", &mut repo, &mut game, CLAN, 0.0, no_catalog());
        assert_eq!((4, 0, false), c.run().unwrap());
        assert!(c.problem.clone().unwrap().contains("Triumphal Gifts"));
        drop(c);
        assert_eq!(vec![11, 13, 14, 15], game.opened);
        assert_eq!(vec![11, 13, 14, 15], repo.gifts_written());
        assert_eq!(vec![10, 12], game.waiting.keys().copied().collect::<Vec<_>>());
    }

    #[test]
    fn a_triumphal_gift_the_game_leaves_as_it_was_is_taken_back_and_the_tab_set_aside() {
        let s = soon();
        let mut game = Game::new(vec![row(10, ANDRA, BONE, 1, s, 2), plain(11), row(12, ANDRA, BONE, 1, s, 2)]);
        game.ignored.extend([10, 12]);
        let mut repo = Repo::standard();
        let mut c = GameCollection::new("x", &mut repo, &mut game, CLAN, 0.0, no_catalog());
        let (collected, _, finished) = c.run().unwrap();
        assert!(!finished);
        assert_eq!(1, collected);
        assert!(c.problem.clone().unwrap().contains("Triumphal Gifts"));
        drop(c);
        assert_eq!(vec![11], repo.gifts_written());
        assert_eq!(vec![10, 12], game.waiting.keys().copied().collect::<Vec<_>>());
    }

    #[test]
    fn a_player_the_site_does_not_know_is_named_by_the_game() {
        let mut game = Game::new(vec![row(10, 777, BONE, 1, soon(), 1)]);
        game.names.insert(777, "Newcomer".into());
        let mut repo = Repo::standard();
        assert_eq!((1, 0, true), collect(&mut game, &mut repo));
        let r = repo.rows.values().next().unwrap();
        assert_eq!(("Bone Chest", "Newcomer", "Level 25 Crypt"), (r.0.as_str(), r.1.as_str(), r.2.as_str()));
    }

    #[test]
    fn an_unknown_type_not_in_the_catalog_is_left_in_the_game_unless_about_to_expire() {
        let mut game = Game::new(vec![row(10, ANDRA, 9999, 1, soon(), 1), plain(11)]);
        assert_eq!((1, 0, false), collect(&mut game, &mut Repo::standard()));
        assert_eq!(vec![11], game.opened);
        assert_eq!(vec![10], game.waiting.keys().copied().collect::<Vec<_>>());
    }

    #[test]
    fn a_gift_about_to_expire_goes_to_the_review_queue_rather_than_be_lost() {
        let mut game = Game::new(vec![row(10, ANDRA, 9999, 1, now_seconds() + 600, 1), row(11, ANDRA, 9999, 1, soon(), 1)]);
        let mut repo = Repo::standard();
        let (collected, incomplete, _) = collect(&mut game, &mut repo);
        assert_eq!((0, 1), (collected, incomplete));
        assert_eq!(vec![10], game.opened);
        assert_eq!(vec![11], game.waiting.keys().copied().collect::<Vec<_>>());
        assert_eq!("AndraBianca", repo.incomplete[0].1);
    }

    #[test]
    fn a_player_nobody_knows_goes_to_the_review_queue_and_is_opened() {
        let mut game = Game::new(vec![row(11, 777, BONE, 1, soon(), 1)]);
        let mut repo = Repo::standard();
        assert_eq!((0, 1, true), collect(&mut game, &mut repo));
        assert_eq!(vec![11], game.opened);
        let i = &repo.incomplete[0];
        assert_eq!(("Bone Chest", "", "Level 25 Crypt"), (i.0.as_str(), i.1.as_str(), i.2.as_str()));
    }

    #[test]
    fn rows_the_database_refuses_go_to_the_review_queue() {
        let mut game = Game::new(vec![plain(10)]);
        let mut repo = Repo::standard();
        repo.refuse = true;
        assert_eq!((0, 1, true), collect(&mut game, &mut repo));
        assert_eq!(1, repo.incomplete.len());
        assert_eq!(vec![10], game.opened);
    }

    #[test]
    fn a_refused_gift_is_taken_back_and_the_others_go_on() {
        let mut game = Game::new(vec![plain(10), plain(11), plain(12)]);
        game.refused.insert(11);
        let mut repo = Repo::standard();
        let (collected, _, _) = collect(&mut game, &mut repo);
        assert_eq!(2, collected);
        assert_eq!(vec![10, 12], repo.gifts_written());
        assert_eq!(vec![11], game.waiting.keys().copied().collect::<Vec<_>>());
    }

    #[test]
    fn a_gift_whose_answer_was_lost_stays_written() {
        let mut game = Game::new(vec![plain(10), plain(11)]);
        game.lost.insert(10);
        let mut repo = Repo::standard();
        assert_eq!((2, 0, true), collect(&mut game, &mut repo));
        assert_eq!(vec![10, 11], repo.gifts_written());
    }

    #[test]
    fn refusals_in_a_row_end_the_run_and_what_was_written_is_taken_back() {
        let mut game = Game::new((10..20).map(plain).collect());
        game.refused = (10..20).collect();
        let mut repo = Repo::standard();
        assert_eq!((0, 0, false), collect(&mut game, &mut repo));
        assert!(repo.gifts_written().is_empty());
    }

    #[test]
    fn losing_the_game_mid_run_keeps_what_was_written_for_the_next_run() {
        let mut game = Game::new(vec![plain(10), plain(11)]);
        game.lists_left = Some(1);
        let mut repo = Repo::standard();
        let (collected, _, finished) = collect(&mut game, &mut repo);
        assert!(!finished);
        assert_eq!(2, collected);
        assert_eq!(vec![10, 11], repo.gifts_written());
    }

    #[test]
    fn a_new_type_found_in_the_catalog_is_learned_and_collected() {
        let mut game = Game::new(vec![row(10, ANDRA, 5789, 2, soon(), 1)]);
        let mut repo = Repo::standard();
        let asked = Rc::new(RefCell::new(Vec::new()));
        let a = asked.clone();
        let lookup: LookupTypes = Box::new(move |ids| {
            a.borrow_mut().push(ids.to_vec());
            HashMap::from([(5789, ("Braided Chest".to_string(), "Level 35 epic Crypt".to_string()))])
        });
        let result = GameCollection::new("x", &mut repo, &mut game, CLAN, 0.0, lookup).run().unwrap();
        assert_eq!((2, 0, true), result);
        assert_eq!(vec![vec![5789]], *asked.borrow());
        assert_eq!(("Braided Chest".to_string(), "Level 35 epic Crypt".to_string()), repo.learned[&5789]);
        assert!(repo.incomplete.is_empty());
        assert!(repo.rows.values().all(|r| (r.0.as_str(), r.1.as_str()) == ("Braided Chest", "AndraBianca")));
    }

    #[test]
    fn catalog_is_not_called_when_all_gift_types_are_already_known() {
        let mut game = Game::new(vec![plain(10), row(11, PIPO, SCORPION, 1, soon(), 1)]);
        let mut repo = Repo::standard();
        let asked = Rc::new(RefCell::new(0));
        let a = asked.clone();
        let lookup: LookupTypes = Box::new(move |_| {
            *a.borrow_mut() += 1;
            HashMap::new()
        });
        assert_eq!((2, 0, true), GameCollection::new("x", &mut repo, &mut game, CLAN, 0.0, lookup).run().unwrap());
        assert_eq!(0, *asked.borrow());
    }
}
