"""
Looks through the database for chest types named wrong, the way type 5784 was.

A type the game sends for the first time is named from the screen, and a type
named wrong stays wrong: on 12/10/2026 type 5784 - Chest of the Cursed, of a
Level 35 epic Crypt - was read off a Fenrir chest that sat next to it on the
panel, and hundreds of crypt chests were counted as Fenrir's from then on, long
after the event had ended. modules/chests/from_game.py no longer lets a reading
like that through; this finds what got in before.

Nothing here writes to the database. Each type is checked against what the
others say about it:

    table     the rows of the type are not named as `game_chest_types` names it;
    names     the type was written under more than one name;
    block     the game numbers related types together - the chests of one
              source (5776-5789 are the Level 35 epic Crypt's) or one chest at
              every level (7010-7014 are the Elven Citadel Chest's, levels 10
              to 30) - and this one sits among another source's or chest's;
    twin      another type, numbered far away, already carries the same name
              and source (types of one chest are numbered side by side);
    quantity  the gifts of the type carry a different number of chests than the
              other types of its source (an epic squad drops several, a crypt one);
    outlives  the type goes on arriving days after every other type of its
              source stopped - what an event's chests do not do.

One sign alone can be innocent - a source with a single type is free to be
numbered anywhere. Two or more, and the type is worth a look. For a type in
another source's block, the names that source had on the screen and that none
of its types carries yet are listed: the misnamed type is usually one of them.

Found this way on 19/10/2026: 5784 and 5786 (Level 35 epic Crypt chests counted
as Fenrir's and Jormungandr's) and 7013 (the Level 25 Citadel's Elven Citadel
Chest, counted as a Level 25 Crypt's Mayan Chest).

Every enabled profile with a database is audited, each clan's database on its
own, since each has its own `game_chest_types`.

Usage:
    python tools/audit_chest_types.py [--profile NAME] [--days N]
"""

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

# How far either side of a type its neighbours are looked for, and how many of
# them have to share another source for the type to be out of its block.
BLOCK_REACH = 3
BLOCK_MIN_NEIGHBOURS = 3
BLOCK_SHARE = 2 / 3

# How far apart two types with the same name and source have to be numbered to
# be twins that should not be: the real ones sit next to each other (6511 and
# 6512 are both a Level 25 rare Crypt's Rare Dragon Chest).
TWIN_DISTANCE = 50

# Chests per gift: a source whose other types average at least this many while
# the type stays at about one (or the other way round) does not fit.
SEVERAL = 1.5
ONE = 1.1

# Days a type has to go on after every other type of its source stopped, and
# how long those have to have been silent, for it to 'outlive' them.
OUTLIVES_DAYS = 2


def load(cursor, days: int):
    since = datetime.now() - timedelta(days=days)
    cursor.execute(
        "SELECT game_type_id, name, source, COUNT(*), COUNT(DISTINCT game_gift_id), "
        "MIN(collected_at), MAX(collected_at) FROM collected_chests "
        "WHERE game_type_id IS NOT NULL AND collected_at >= %s GROUP BY game_type_id, name, source", (since,))
    written = defaultdict(list)       # type -> [(name, source, rows, gifts, first, last)]
    for type_id, name, source, rows, gifts, first, last in cursor.fetchall():
        written[int(type_id)].append((name, source, int(rows), int(gifts), first, last))

    cursor.execute(
        "SELECT t.game_type_id, t.name, COALESCE(s.source, t.source) FROM game_chest_types t "
        "LEFT JOIN standard_chests s ON s.id = t.standard_chest_id")
    table = {int(type_id): (name, source) for type_id, name, source in cursor.fetchall()}

    # The names each source had while the chests were read off the screen only.
    cursor.execute(
        "SELECT source, name, COUNT(*) FROM collected_chests WHERE game_type_id IS NULL AND collected_at >= %s "
        "GROUP BY source, name", (since - timedelta(days=days),))
    screen = defaultdict(dict)
    for source, name, rows in cursor.fetchall():
        screen[source][name] = int(rows)
    return written, table, screen


def audit(written, table, screen, now=None):
    now = now or datetime.now()
    # What each type mostly went into the database as.
    label = {t: max(entries, key=lambda e: e[2])[:2] for t, entries in written.items()}
    totals = {t: (sum(e[2] for e in entries), sum(e[3] for e in entries),
                  min(e[4] for e in entries), max(e[5] for e in entries)) for t, entries in written.items()}
    by_source = defaultdict(set)
    for t, (_name, source) in label.items():
        by_source[source].add(t)
    every_label = dict(table)
    every_label.update(label)
    by_label = defaultdict(set)
    for t, pair in label.items():
        by_label[pair].add(t)

    found = []
    for t in sorted(written):
        name, source = label[t]
        rows, gifts, first, last = totals[t]
        signs, hints = [], []

        if t in table and any((n, s) != table[t] for n, s, *_ in written[t]):
            wrong = sum(e[2] for e in written[t] if (e[0], e[1]) != table[t])
            signs.append(f"table: {wrong} row(s) not named as game_chest_types does, '{table[t][0]}' ({table[t][1]})")

        if len(written[t]) > 1:
            signs.append("names: written as " + "; ".join(f"'{n}' ({s}) x{r}" for n, s, r, *_ in
                                                          sorted(written[t], key=lambda e: -e[2])))

        around = [every_label[n] for n in range(t - BLOCK_REACH, t + BLOCK_REACH + 1)
                  if n != t and n in every_label]
        if len(around) >= BLOCK_MIN_NEIGHBOURS:
            sources = [s for _n, s in around]
            common = max(set(sources), key=sources.count)
            if common != source and sources.count(common) >= BLOCK_SHARE * len(around):
                signs.append(f"block: {sources.count(common)} of the {len(around)} types around it "
                             f"are '{common}'")
                claimed = {label[n][0] for n in by_source.get(common, ())} | \
                          {n for n, s in table.values() if s == common}
                missing = sorted(set(screen.get(common, {})) - claimed)
                if missing:
                    hints.append(f"'{common}' names no type has carried yet: {', '.join(missing)}")
            names = [n for n, _s in around]
            chest = max(set(names), key=names.count)
            if chest != name and names.count(chest) >= BLOCK_SHARE * len(around):
                levels = sorted({s for n, s in around if n == chest})
                signs.append(f"block: {names.count(chest)} of the {len(around)} types around it are "
                             f"'{chest}' ({', '.join(levels)})")
                claimed = {s for n, s in every_label.values() if n == chest}
                missing = sorted(s for s, names_seen in screen.items() if chest in names_seen and s not in claimed)
                if missing:
                    hints.append(f"'{chest}' was read on the screen from sources no type carries yet: "
                                 f"{', '.join(missing)}")

        # Of two twins, or of a type and its source's others, the one with fewer
        # chests is the odd one: the other is the source's own.
        twins = sorted(n for n in by_label[(name, source)] if abs(n - t) > TWIN_DISTANCE and totals[n][0] > rows)
        if twins:
            signs.append("twin: '" + name + f"' ({source}) is also type " +
                         ", ".join(f"{n} (x{totals[n][0]})" for n in twins))

        siblings = by_source[source] - {t}
        if siblings and gifts:
            own = rows / gifts
            sib_rows = sum(totals[s][0] for s in siblings)
            sib_gifts = sum(totals[s][1] for s in siblings)
            theirs = sib_rows / sib_gifts if sib_gifts else 0
            if sib_rows > rows and ((theirs >= SEVERAL and own <= ONE) or (theirs <= ONE and own >= SEVERAL)):
                signs.append(f"quantity: {own:.2f} chest(s) per gift, the other types of '{source}' {theirs:.2f}")

            stopped = max(totals[s][3] for s in siblings)
            if (last - stopped >= timedelta(days=OUTLIVES_DAYS)
                    and now - stopped >= timedelta(days=OUTLIVES_DAYS)):
                signs.append(f"outlives: still arriving {last:%d/%m}, the other types of '{source}' "
                             f"stopped {stopped:%d/%m}")

        if signs:
            found.append({"type": t, "name": name, "source": source, "rows": rows, "gifts": gifts,
                          "first": first, "last": last, "signs": signs, "hints": hints})
    found.sort(key=lambda f: (-len(f["signs"]), -f["rows"]))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", help="only this profile (its name, or 'account / profile'); default: all")
    parser.add_argument("--days", type=int, default=60, help="how far back to look (default 60 days)")
    args = parser.parse_args()

    from config.settings import ConfigManager

    profiles = [p for a in ConfigManager().enabled_accounts() for p in a.active_profiles if p.has_database]
    if args.profile:
        profiles = [p for p in profiles if args.profile in (p.name, p.label)]
    if not profiles:
        print("No enabled profile with a database" + (f" called '{args.profile}'." if args.profile else "."))
        return 2

    # Each clan has its database, and its own game_chest_types: they are audited
    # one by one. Two profiles sharing a database are audited once.
    seen, failed = set(), 0
    for profile in profiles:
        db = profile.database
        key = (db.host, db.port, db.database)
        print("=" * 78)
        if key in seen:
            print(f"{profile.label}: database '{db.database}' already audited above.\n")
            continue
        seen.add(key)
        print(f"{profile.label} - database '{db.database}' on {db.host}:{db.port}")
        print("=" * 78)
        try:
            audit_profile(profile, args.days)
        except Exception as exc:
            failed += 1
            print(f"Could not audit this database: {exc}\n")
    return 3 if failed else 0


def audit_profile(profile, days: int) -> None:
    from database.db_connection import open_connection

    connection, _ = open_connection(profile.database, timeout=10)
    try:
        cursor = connection.cursor()
        written, table, screen = load(cursor, days)
    finally:
        connection.close()

    found = audit(written, table, screen)
    print(f"{len(written)} chest types written in the last {days} days, {len(table)} in game_chest_types.\n")
    if not found:
        print("Nothing suspicious.\n")
        return
    for f in found:
        weight = "LIKELY WRONG" if len(f["signs"]) >= 2 else "worth a look"
        print(f"[{weight}] type {f['type']}: '{f['name']}' ({f['source']}) - {f['rows']} chest(s) in "
              f"{f['gifts']} gift(s), {f['first']:%d/%m/%Y} to {f['last']:%d/%m/%Y}")
        for sign in f["signs"]:
            print(f"      {sign}")
        for hint in f["hints"]:
            print(f"      hint: {hint}")
        print()
    print("To put a type right (after checking it in the game): correct game_chest_types first, then the rows -\n"
          "  UPDATE game_chest_types SET name=..., source=..., standard_chest_id=... WHERE game_type_id=...;\n"
          "  UPDATE collected_chests SET name=..., source=... WHERE game_type_id=...;\n")


if __name__ == "__main__":
    raise SystemExit(main())
