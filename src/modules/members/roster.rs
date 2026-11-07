//! Reads the clan's members out of the game's answers.
//!
//! - The clan (route 15000), asked with record 58:
//!   `[[clan_id], [tag, name, ...], [[[player_id], rank, timestamp], ...], ...]`.
//!   The third item is the membership, with the rank as a number (1 Leader,
//!   2 Superior, 3 Officer, 4 Veteran, 5 Soldier). Asked with record 8, as the game
//!   repeats while the window is open, the membership comes empty: only one that has
//!   rows counts.
//! - The members' profiles (route 402): rows `[[player_id], account, name, country,
//!   ..., level at [7], ..., power at [10], [clan_id] at [11], ..., [kingdom, x, y] at [15]]`.
//!
//! A roster is complete when every id of the membership has a profile.

use std::collections::HashMap;

use crate::tbcodec::Value;

pub const CLAN_ROUTE: i128 = 15000;
pub const PROFILES_ROUTE: i128 = 402;
/// What the game asks when the clan window opens: brings the gifts and the membership.
pub const CLAN_RECORD_WHOLE: i128 = 58;
/// As many as the game itself asks for at a time.
pub const PROFILES_PER_REQUEST: usize = 100;

pub fn rank_name(rank: Option<i128>) -> &'static str {
    match rank {
        Some(1) => "Leader",
        Some(2) => "Superior",
        Some(3) => "Officer",
        Some(4) => "Veteran",
        Some(5) => "Soldier",
        _ => "",
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Member {
    pub player_id: i128,
    pub name: String,
    pub power: Option<i128>,
    pub rank: Option<i128>,
    pub level: Option<i128>,
    pub coordinates: Option<(i128, i128, i128)>,
    pub clan_id: Option<i128>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Clan {
    pub clan_id: i128,
    pub tag: String,
    pub name: String,
    /// player id -> rank, in the game's order.
    pub ranks: Vec<(i128, i128)>,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Roster {
    pub clan: Option<Clan>,
    pub members: Vec<Member>,
    /// Members the game sent no profile for.
    pub missing: Vec<i128>,
}

impl Roster {
    pub fn expected(&self) -> usize {
        self.clan.as_ref().map(|c| c.ranks.len()).unwrap_or(0)
    }
    pub fn complete(&self) -> bool {
        self.clan.is_some() && self.missing.is_empty()
    }
}

/// `[id]` -> id.
fn id(v: &Value) -> Option<i128> {
    v.as_arr()?.first()?.strict_int()
}

pub fn parse_profile_row(row: &Value) -> Option<Member> {
    let r = row.as_arr()?;
    if r.len() <= 11 {
        return None;
    }
    let player_id = id(&r[0])?;
    let name = r[2].as_str()?;
    if name.trim().is_empty() {
        return None;
    }
    let coordinates = r.get(15).and_then(|p| p.as_arr()).filter(|p| p.len() == 3).and_then(|p| {
        Some((p[0].strict_int()?, p[1].strict_int()?, p[2].strict_int()?))
    });
    Some(Member {
        player_id,
        name: name.to_string(),
        power: r[10].strict_int(),
        rank: None,
        level: r[7].strict_int(),
        coordinates,
        clan_id: id(&r[11]),
    })
}

/// A clan record that carries its membership; None for anything else, an empty membership included.
pub fn parse_clan_record(node: &Value) -> Option<Clan> {
    let n = node.as_arr()?;
    if n.len() < 3 {
        return None;
    }
    let clan_id = id(&n[0])?;
    let info = n[1].as_arr()?;
    if info.len() < 2 {
        return None;
    }
    let (tag, name) = (info[0].as_str()?, info[1].as_str()?);
    let rows = n[2].as_arr()?;
    if rows.is_empty() {
        return None;
    }
    let mut ranks: Vec<(i128, i128)> = Vec::new();
    for row in rows {
        let r = row.as_arr().filter(|r| r.len() >= 2)?;
        let player = id(&r[0])?;
        let rank = r[1].strict_int()?;
        if let Some(existing) = ranks.iter_mut().find(|(p, _)| *p == player) {
            existing.1 = rank;
        } else {
            ranks.push((player, rank));
        }
    }
    Some(Clan { clan_id, tag: tag.to_string(), name: name.to_string(), ranks })
}

/// Every list in a tree, parents before children.
pub fn iter_lists(node: &Value) -> Vec<&Value> {
    let mut out = Vec::new();
    let mut stack = vec![(node, 0usize)];
    while let Some((current, level)) = stack.pop() {
        if level > 60 {
            continue;
        }
        match current {
            Value::Arr(items) => {
                out.push(current);
                for c in items {
                    stack.push((c, level + 1));
                }
            }
            Value::Map(pairs) => {
                for (_, c) in pairs {
                    stack.push((c, level + 1));
                }
            }
            _ => {}
        }
    }
    out
}

/// Accumulates what the game answers and assembles the clan's roster.
#[derive(Default)]
pub struct RosterCollector {
    pub clan: Option<Clan>,
    pub profiles: HashMap<i128, Member>,
}

impl RosterCollector {
    pub fn take_profiles(&mut self, objects: &Value) -> bool {
        let mut learned = false;
        for node in iter_lists(objects) {
            for row in node.as_arr().unwrap() {
                if let Some(member) = parse_profile_row(row) {
                    self.profiles.insert(member.player_id, member);
                    learned = true;
                }
            }
        }
        learned
    }

    pub fn take_clan(&mut self, objects: &Value) -> bool {
        for node in iter_lists(objects) {
            if let Some(clan) = parse_clan_record(node) {
                self.clan = Some(clan);
                return true;
            }
        }
        false
    }

    /// Takes one answer of the game; true when something was learned.
    pub fn feed(&mut self, route: i128, objects: &[Value]) -> bool {
        let tree = Value::Arr(objects.to_vec());
        match route {
            PROFILES_ROUTE => self.take_profiles(&tree),
            CLAN_ROUTE => self.take_clan(&tree),
            _ => false,
        }
    }

    /// The members as the game lists them: by rank, the strongest first.
    pub fn roster(&self) -> Roster {
        let Some(clan) = &self.clan else { return Roster::default() };
        let mut members: Vec<Member> = clan
            .ranks
            .iter()
            .filter_map(|(p, rank)| self.profiles.get(p).map(|m| Member { rank: Some(*rank), ..m.clone() }))
            .collect();
        let missing: Vec<i128> = clan.ranks.iter().map(|(p, _)| *p).filter(|p| !self.profiles.contains_key(p)).collect();
        members.sort_by(|a, b| {
            (a.rank, -(a.power.unwrap_or(0)), a.name.to_lowercase()).cmp(&(b.rank, -(b.power.unwrap_or(0)), b.name.to_lowercase()))
        });
        Roster { clan: Some(clan.clone()), members, missing }
    }

    pub fn clear(&mut self) {
        self.clan = None;
        self.profiles.clear();
    }
}
