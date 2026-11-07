//! What the game says about the clan's gifts.
//!
//! - The list (route 15000). The clan record is
//!   `[[clan_id], [tag, name, ...], [members], ..., gifts at [13], ...]` and each
//!   gift `[gift_id, [player_id], chest_type, expires_at, quantity, kind]`. Only a
//!   record that has members counts, and such a one with no gifts says there are none.
//! - Opening a gift (route 15013): the answer carries what was inside it.
//!
//! `quantity` is how many chests a gift put in the panel (an epic monster drops
//! several at once). A gift expires twenty hours after it was generated, to the
//! second. The game sends neither the chest's name nor the player's.

use chrono::{NaiveDateTime, TimeZone};

use crate::tbcodec::Value;

pub const CLAN_ROUTE: i128 = 15000;
pub const OPEN_ROUTE: i128 = 15013;
pub const GIFTS_FIELD: usize = 13;
pub const GIFT_LIFETIME_SECONDS: i128 = 20 * 60 * 60;

/// The tab of the panel the gift is listed in: 1 Gifts, 2 Triumphal Gifts. One
/// with anything else is of a kind nobody has looked at yet, and is left in the game.
pub const PLAIN_GIFT: i128 = 1;
pub const TRIUMPHAL_GIFT: i128 = 2;
pub const KNOWN_KINDS: [i128; 2] = [PLAIN_GIFT, TRIUMPHAL_GIFT];

pub fn tab_name(kind: i128) -> String {
    match kind {
        PLAIN_GIFT => "Gifts".into(),
        TRIUMPHAL_GIFT => "Triumphal Gifts".into(),
        other => other.to_string(),
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Gift {
    pub gift_id: i128,
    pub player_id: i128,
    pub chest_type: i128,
    pub expires_at: i128,
    pub quantity: i128,
    pub kind: i128,
}

impl Gift {
    /// When the gift was generated, in this computer's time - what `collected_at` holds.
    pub fn generated_at(&self) -> NaiveDateTime {
        let ts = (self.expires_at - GIFT_LIFETIME_SECONDS) as i64;
        chrono::Local
            .timestamp_opt(ts, 0)
            .single()
            .map(|d| d.naive_local())
            .unwrap_or_else(|| chrono::DateTime::from_timestamp(ts, 0).unwrap_or_default().naive_utc())
    }
}

impl std::fmt::Display for Gift {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Gift(gift_id={}, player_id={}, chest_type={}, expires_at={}, quantity={}, kind={})",
               self.gift_id, self.player_id, self.chest_type, self.expires_at, self.quantity, self.kind)
    }
}

pub fn parse_gift_row(row: &Value) -> Option<Gift> {
    let r = row.as_arr()?;
    if r.len() != 6 {
        return None;
    }
    let player = r[1].as_arr()?;
    if player.len() != 1 {
        return None;
    }
    let player_id = player[0].strict_int()?;
    let gift_id = r[0].strict_int()?;
    let chest_type = r[2].strict_int()?;
    let expires_at = r[3].strict_int()?;
    let quantity = r[4].strict_int()?;
    let kind = r[5].strict_int()?;
    Some(Gift { gift_id, player_id, chest_type, expires_at, quantity: quantity.max(1), kind })
}

/// The gifts of a clan record that carries them; None for anything else.
///
/// The record the game repeats while the window is open has no members and no
/// gifts, and must not be taken for 'there are no gifts'.
pub fn parse_clan_gifts(node: &Value) -> Option<Vec<Gift>> {
    let n = node.as_arr()?;
    if n.len() <= GIFTS_FIELD {
        return None;
    }
    let clan = n[0].as_arr()?;
    if clan.len() != 1 || clan[0].strict_int().is_none() {
        return None;
    }
    let info = n[1].as_arr()?;
    if info.len() < 2 || info[0].as_str().is_none() {
        return None;
    }
    let members = n[2].as_arr()?;
    let rows = n[GIFTS_FIELD].as_arr()?;
    if members.is_empty() {
        return None;
    }
    let mut gifts = Vec::with_capacity(rows.len());
    for row in rows {
        gifts.push(parse_gift_row(row)?);
    }
    gifts.sort_by_key(|g| g.gift_id);
    Some(gifts)
}

/// Looks for the clan record anywhere in an answer.
pub fn clan_gifts(objects: &Value) -> Option<Vec<Gift>> {
    let mut stack = vec![(objects, 0usize)];
    while let Some((node, level)) = stack.pop() {
        let Some(items) = node.as_arr() else { continue };
        if level > 6 {
            continue;
        }
        if let Some(g) = parse_clan_gifts(node) {
            return Some(g);
        }
        for child in items {
            stack.push((child, level + 1));
        }
    }
    None
}
