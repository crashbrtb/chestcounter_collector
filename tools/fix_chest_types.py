"""
Analyzes and repairs chest types and pending incomplete chests in any clan database
using the game's official static chest catalog (Table 8).

What it checks and fixes (for any chest type ID, automatically)
---------------------------------------------------------------
1. Missing `game_chest_types` entries:
   Any `game_type_id` present in `collected_chests` or `incomplete_chests` that is
   not yet in `game_chest_types` is added with its official `(name, source)` and
   linked to `standard_chests`.
2. Wrong `name`, `source`, or missing `standard_chest_id` in `game_chest_types`:
   Every row in `game_chest_types` is compared against the official catalog. When a
   type was named wrong (e.g. 5784 named as Fenrir's Chest, 6724 named as Ancients'
   Chest, or curly apostrophes), `game_chest_types` is updated to the official name
   and source (preserving `standard_chests` spelling when matched).
3. Wrong `name` or `source` in `collected_chests` (`WHERE game_type_id IS NOT NULL`):
   Every collected chest carrying a `game_type_id` whose `name` or `source` differs
   from the official catalog is updated in place.
4. Pending rows in `incomplete_chests` (`status = 'pending' AND game_type_id IS NOT NULL`):
   Each pending chest with a known `game_type_id` (and player name) is inserted into
   `collected_chests` with its original `collected_at` and game metadata, and the
   `incomplete_chests` row is marked `status = 'corrected'` with `screenshot = NULL`.

Usage
-----
Dry-run (analysis only, writes nothing):
    python tools/fix_chest_types.py
    python tools/fix_chest_types.py --profile "Crash BR"
    python tools/fix_chest_types.py --host db.example.com --user u --password p --database clan_db

Apply fixes to the database:
    python tools/fix_chest_types.py --apply
    python tools/fix_chest_types.py --host db.example.com --user u --password p --database clan_db --apply
"""
from __future__ import annotations

import argparse
import os
import sys
import unicodedata
from typing import Dict, List, Optional, Set, Tuple

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

from config.settings import ConfigManager, DatabaseConfig
from database.db_connection import open_connection
from modules.chests.catalog import lookup_chest_types


def _load_standard_sources(cursor) -> Dict[str, Tuple[int, str]]:
    """
    Maps normalized lowercase/ASCII-folded source -> `(standard_chest_id, exact_standard_source)`.
    Also queries MySQL directly for collation-equivalent matches when needed.
    """
    cursor.execute("SELECT id, source FROM standard_chests")
    rows = cursor.fetchall()
    by_exact: Dict[str, Tuple[int, str]] = {}
    for row_id, src in rows:
        if src:
            by_exact[src] = (int(row_id), src)
    return by_exact


def _resolve_against_standard(cursor, official_name: str, official_source: str,
                              std_cache: Dict[str, Optional[Tuple[int, str]]]) -> Tuple[str, str, Optional[int]]:
    """
    Returns `(canonical_name, canonical_source, standard_chest_id)` matching `standard_chests`
    under the database's own collation (so `'Epic Jörmungandr squad'` matches `'Epic Jormungandr squad'`).
    """
    if official_source not in std_cache:
        cursor.execute(
            "SELECT id, source FROM standard_chests WHERE source = %s LIMIT 1",
            (official_source,),
        )
        row = cursor.fetchone()
        std_cache[official_source] = (int(row[0]), str(row[1])) if row else None

    match = std_cache[official_source]
    if match is not None:
        return official_name, match[1], match[0]
    return official_name, official_source, None


def analyze_and_fix_database(connection, apply: bool = False) -> Dict[str, int]:
    cursor = connection.cursor()
    stats = {
        "missing_types_added": 0,
        "types_updated": 0,
        "collected_groups_fixed": 0,
        "collected_rows_fixed": 0,
        "incomplete_pending_resolved": 0,
        "incomplete_corrected_fixed": 0,
    }

    try:
        # 1. Gather all distinct game_type_id across all 3 tables
        all_type_ids: Set[int] = set()
        cursor.execute("SELECT game_type_id, name, source, standard_chest_id FROM game_chest_types")
        db_types = {int(tid): (name or "", source or "", std_id) for tid, name, source, std_id in cursor.fetchall()}
        all_type_ids.update(db_types.keys())

        cursor.execute("SELECT DISTINCT game_type_id FROM collected_chests WHERE game_type_id IS NOT NULL")
        all_type_ids.update(int(tid) for (tid,) in cursor.fetchall() if tid is not None)

        cursor.execute("SELECT DISTINCT game_type_id FROM incomplete_chests WHERE game_type_id IS NOT NULL")
        all_type_ids.update(int(tid) for (tid,) in cursor.fetchall() if tid is not None)

        if not all_type_ids:
            print("  No chest types with game_type_id found in this database.\n")
            return stats

        official_raw = lookup_chest_types(all_type_ids)
        std_cache: Dict[str, Optional[Tuple[int, str]]] = {}
        canonical: Dict[int, Tuple[str, str, Optional[int]]] = {}
        for tid, (oname, osource) in official_raw.items():
            canonical[tid] = _resolve_against_standard(cursor, oname, osource, std_cache)

        unknown_in_catalog = sorted(all_type_ids - set(canonical.keys()))
        print(f"  Catalog resolved {len(canonical)}/{len(all_type_ids)} distinct game_type_id(s).")
        if unknown_in_catalog:
            print(f"  [!] IDs not found in official catalog: {unknown_in_catalog}")

        # ------------------------------------------------------------------
        # Step A: Missing types in game_chest_types
        # ------------------------------------------------------------------
        missing_types = sorted(tid for tid in canonical if tid not in db_types)
        if missing_types:
            print(f"\n  [1] Missing from game_chest_types: {len(missing_types)} type(s)")
            for tid in missing_types:
                cname, csource, cstd_id = canonical[tid]
                print(f"      + ID {tid}: '{cname}' ({csource}) [standard_chest_id={cstd_id}]")
                if apply:
                    cursor.execute(
                        "INSERT IGNORE INTO game_chest_types "
                        "(game_type_id, name, source, standard_chest_id, created, modified) "
                        "VALUES (%s, %s, %s, %s, UTC_TIMESTAMP(), UTC_TIMESTAMP())",
                        (tid, cname, csource, cstd_id),
                    )
                stats["missing_types_added"] += 1

        # ------------------------------------------------------------------
        # Step B: Wrong name, source, or standard_chest_id in game_chest_types
        # ------------------------------------------------------------------
        wrong_types: List[Tuple[int, str, str, Optional[int], str, str, Optional[int]]] = []
        for tid, (dname, dsource, dstd_id) in sorted(db_types.items()):
            if tid not in canonical:
                continue
            cname, csource, cstd_id = canonical[tid]
            if dname != cname or dsource != csource or (dstd_id is None and cstd_id is not None):
                wrong_types.append((tid, dname, dsource, dstd_id, cname, csource, cstd_id))

        if wrong_types:
            print(f"\n  [2] Mismatches in game_chest_types: {len(wrong_types)} type(s)")
            for tid, dname, dsource, dstd_id, cname, csource, cstd_id in wrong_types:
                print(f"      ~ ID {tid}: DB=('{dname}', '{dsource}', std={dstd_id}) "
                      f"-> Official=('{cname}', '{csource}', std={cstd_id})")
                if apply:
                    cursor.execute(
                        "UPDATE game_chest_types "
                        "SET name = %s, source = %s, standard_chest_id = COALESCE(%s, standard_chest_id), "
                        "modified = UTC_TIMESTAMP() "
                        "WHERE game_type_id = %s",
                        (cname, csource, cstd_id, tid),
                    )
                stats["types_updated"] += 1
        else:
            print("\n  [2] game_chest_types: all existing rows match the official catalog.")

        # ------------------------------------------------------------------
        # Step C: Wrong name or source in collected_chests
        # ------------------------------------------------------------------
        cursor.execute(
            "SELECT game_type_id, name, source, COUNT(*) "
            "FROM collected_chests "
            "WHERE game_type_id IS NOT NULL "
            "GROUP BY game_type_id, name, source "
            "ORDER BY game_type_id"
        )
        wrong_collected = []
        for tid_raw, dname, dsource, cnt in cursor.fetchall():
            tid = int(tid_raw)
            if tid not in canonical:
                continue
            cname, csource, _ = canonical[tid]
            if (dname or "") != cname or (dsource or "") != csource:
                wrong_collected.append((tid, dname or "", dsource or "", int(cnt), cname, csource))

        if wrong_collected:
            total_rows = sum(x[3] for x in wrong_collected)
            print(f"\n  [3] Mismatched rows in collected_chests: {total_rows} row(s) across {len(wrong_collected)} group(s)")
            for tid, dname, dsource, cnt, cname, csource in wrong_collected:
                print(f"      ~ ID {tid} ({cnt} rows): ('{dname}', '{dsource}') -> ('{cname}', '{csource}')")
                if apply:
                    cursor.execute(
                        "UPDATE collected_chests SET name = %s, source = %s "
                        "WHERE game_type_id = %s AND (name != %s OR source != %s)",
                        (cname, csource, tid, cname, csource),
                    )
                stats["collected_groups_fixed"] += 1
                stats["collected_rows_fixed"] += cnt
        else:
            print("\n  [3] collected_chests: all rows with game_type_id match the official catalog.")

        # ------------------------------------------------------------------
        # Step D: Pending rows in incomplete_chests with game_type_id
        # ------------------------------------------------------------------
        # Build player fallback map in case any incomplete_chests row has game_player_id but empty player
        player_map: Dict[int, str] = {}
        cursor.execute("SELECT game_player_id, player FROM members WHERE game_player_id IS NOT NULL AND player != ''")
        for pid, pname in cursor.fetchall():
            player_map[int(pid)] = pname

        cursor.execute(
            "SELECT id, name, player, source, collected_at, game_gift_id, game_type_id, game_player_id, "
            "collected_chest_id "
            "FROM incomplete_chests "
            "WHERE status = 'pending' AND game_type_id IS NOT NULL "
            "ORDER BY id"
        )
        pending_rows = cursor.fetchall()
        resolvable_pending = []
        for row in pending_rows:
            inc_id, dname, dplayer, dsource, coll_at, gift_id, tid_raw, pid_raw, coll_id = row
            tid = int(tid_raw)
            if tid not in canonical:
                continue
            player_name = (dplayer or "").strip()
            if not player_name and pid_raw is not None:
                player_name = player_map.get(int(pid_raw), "")
            if not player_name:
                continue
            cname, csource, _ = canonical[tid]
            resolvable_pending.append((int(inc_id), player_name, coll_at, gift_id, tid, pid_raw, coll_id, cname, csource))

        if resolvable_pending:
            print(f"\n  [4] Pending incomplete_chests resolvable via catalog: {len(resolvable_pending)} row(s)")
            # Summarize by type_id for clean output
            by_type: Dict[int, int] = {}
            for item in resolvable_pending:
                by_type[item[4]] = by_type.get(item[4], 0) + 1
            for tid, count in sorted(by_type.items()):
                cname, csource, _ = canonical[tid]
                print(f"      + ID {tid} ({count} pending chest(s)) -> '{cname}' ({csource})")

            if apply:
                for inc_id, player_name, coll_at, gift_id, tid, pid_raw, coll_id, cname, csource in resolvable_pending:
                    new_coll_id = coll_id
                    if not new_coll_id:
                        cursor.execute(
                            "INSERT INTO collected_chests "
                            "(name, player, source, type, collected_at, game_gift_id, game_type_id, game_player_id) "
                            "VALUES (%s, %s, %s, 1, %s, %s, %s, %s)",
                            (cname, player_name, csource, coll_at, gift_id, tid, pid_raw),
                        )
                        new_coll_id = cursor.lastrowid
                    cursor.execute(
                        "UPDATE incomplete_chests "
                        "SET name = %s, player = %s, source = %s, status = 'corrected', "
                        "collected_chest_id = %s, reviewed_at = UTC_TIMESTAMP(), screenshot = NULL "
                        "WHERE id = %s",
                        (cname, player_name, csource, new_coll_id, inc_id),
                    )
            stats["incomplete_pending_resolved"] = len(resolvable_pending)
        else:
            print("\n  [4] incomplete_chests (pending): 0 pending rows with game_type_id to resolve.")

        # Also check already-corrected incomplete_chests that had wrong name/source
        cursor.execute(
            "SELECT game_type_id, name, source, COUNT(*) "
            "FROM incomplete_chests "
            "WHERE status = 'corrected' AND game_type_id IS NOT NULL "
            "GROUP BY game_type_id, name, source"
        )
        wrong_inc_corrected = []
        for tid_raw, dname, dsource, cnt in cursor.fetchall():
            tid = int(tid_raw)
            if tid not in canonical:
                continue
            cname, csource, _ = canonical[tid]
            if (dname or "") != cname or (dsource or "") != csource:
                wrong_inc_corrected.append((tid, dname or "", dsource or "", int(cnt), cname, csource))

        if wrong_inc_corrected:
            total_inc = sum(x[3] for x in wrong_inc_corrected)
            print(f"\n  [5] Historical corrected incomplete_chests with wrong name/source: {total_inc} row(s)")
            for tid, dname, dsource, cnt, cname, csource in wrong_inc_corrected:
                print(f"      ~ ID {tid} ({cnt} rows): ('{dname}', '{dsource}') -> ('{cname}', '{csource}')")
                if apply:
                    cursor.execute(
                        "UPDATE incomplete_chests SET name = %s, source = %s "
                        "WHERE status = 'corrected' AND game_type_id = %s AND (name != %s OR source != %s)",
                        (cname, csource, tid, cname, csource),
                    )
                stats["incomplete_corrected_fixed"] += cnt

        if apply:
            connection.commit()
            print("\n  [OK] All changes committed to the database.\n")
        else:
            print("\n  [DRY-RUN] No changes written. Run with --apply to apply these fixes.\n")

        return stats
    finally:
        cursor.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--apply", action="store_true", help="write fixes to the database (default is dry-run)")
    parser.add_argument("--profile", help="only this configured profile name or label")
    parser.add_argument("--host", help="direct database host (for production servers not in config.json)")
    parser.add_argument("--port", type=int, default=3306, help="direct database port (default: 3306)")
    parser.add_argument("--user", help="direct database user")
    parser.add_argument("--password", default="", help="direct database password")
    parser.add_argument("--database", help="direct database name")
    args = parser.parse_args()

    targets: List[Tuple[str, DatabaseConfig]] = []
    if args.host or args.database or args.user:
        if not (args.host and args.user and args.database):
            print("Error: when using direct database connection, --host, --user, and --database are required.")
            return 2
        targets.append((
            f"{args.database}@{args.host}:{args.port}",
            DatabaseConfig(
                host=args.host,
                port=args.port,
                user=args.user,
                password=args.password,
                database=args.database,
            ),
        ))
    else:
        profiles = [p for a in ConfigManager().enabled_accounts() for p in a.active_profiles if p.has_database]
        if args.profile:
            profiles = [p for p in profiles if args.profile.lower() in (p.name.lower(), p.label.lower())]
        if not profiles:
            print("No enabled profile with a database found" + (f" matching '{args.profile}'." if args.profile else "."))
            return 2
        seen = set()
        for p in profiles:
            db = p.database
            key = (db.host, db.port, db.database)
            if key in seen:
                continue
            seen.add(key)
            targets.append((f"{p.label} ({db.database} on {db.host}:{db.port})", db))

    failed = 0
    for label, db_cfg in targets:
        print("=" * 80)
        print(f"Database: {label} [{'APPLY' if args.apply else 'DRY-RUN'}]")
        print("=" * 80)
        try:
            conn, _ = open_connection(db_cfg, timeout=15)
            try:
                analyze_and_fix_database(conn, apply=args.apply)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  [ERROR] Could not process {label}: {exc}\n")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
