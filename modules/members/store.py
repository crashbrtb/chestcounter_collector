"""Where a roster is kept, under config/module_data/members/.

One pair of files per clan, replaced at each reading: a JSON for programs and a
CSV that opens in a spreadsheet. Two profiles of the same clan write the same
pair - the roster is the clan's, not the profile's.
"""
from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime
from typing import Tuple

from config.settings import MODULE_DATA_DIR

from .roster import Clan, Roster

DATA_DIR = os.path.join(MODULE_DATA_DIR, "members")
CSV_COLUMNS = ("player_id", "name", "power", "rank", "rank_name", "level", "kingdom", "x", "y")


def roster_paths(clan: Clan, data_dir: str = DATA_DIR) -> Tuple[str, str]:
    """(json, csv) for a clan: 'LPL' with id 936302870583 -> 'LPL_936302870583.json'."""
    tag = re.sub(r"[^A-Za-z0-9-]+", "_", clan.tag).strip("_") or "clan"
    base = os.path.join(data_dir, f"{tag}_{clan.clan_id}")
    return base + ".json", base + ".csv"


def _rows(roster: Roster) -> list:
    rows = []
    for member in roster.members:
        kingdom, x, y = member.coordinates or (None, None, None)
        rows.append({
            "player_id": member.player_id, "name": member.name, "power": member.power,
            "rank": member.rank, "rank_name": member.rank_name, "level": member.level,
            "kingdom": kingdom, "x": x, "y": y,
        })
    return rows


def save_roster(roster: Roster, read_at: datetime, read_by: str = "", data_dir: str = DATA_DIR) -> Tuple[str, str]:
    """Writes the roster of a clan and returns (json path, csv path)."""
    clan = roster.clan
    if clan is None:
        raise ValueError("a roster without its clan cannot be saved")
    json_path, csv_path = roster_paths(clan, data_dir)
    os.makedirs(data_dir, exist_ok=True)
    rows = _rows(roster)

    data = {
        "clan": {"id": clan.clan_id, "tag": clan.tag, "name": clan.name},
        "read_at": read_at.isoformat(timespec="seconds"),
        "read_by": read_by,
        "members": rows,
    }
    temporary = json_path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(temporary, json_path)

    # utf-8-sig: without the mark a spreadsheet reads the accents of the names as cp1252.
    temporary = csv_path + ".tmp"
    with open(temporary, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, csv_path)
    return json_path, csv_path
