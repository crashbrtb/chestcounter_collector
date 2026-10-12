"""
The clan gifts as the screen shows them against what the game sends: the
measurements modules/chests/gifts.py and from_game.py were built on, kept so
they can be made again when the game changes what it sends.

It opens the gifts panel exactly as the collector does, reads the chests on
screen with the OCR - the reference - and records everything the game receives
meanwhile. Then it looks for the reference inside the traffic: the words that
were read, the ids of the players known from the members module, and the moment
each chest expires (which the panel shows as a countdown).

Usage:
    python tools/chest_traffic.py capture            # game already open and logged in
    python tools/chest_traffic.py capture --reload   # also reload the page, to see what arrives at login
    python tools/chest_traffic.py record             # a REAL collection, traffic recorded beside it
    python tools/chest_traffic.py analyze [folder]   # the last capture when no folder is given
    python tools/chest_traffic.py types [--write]    # the chest types of a recording, into game_chest_types

`capture` never presses 'Open': a chest opened there would be a chest nothing
records. `record` does, on every chest, exactly as a run does - it is the run,
reading the screen (whatever 'Read the chests from the game's own list' says),
with a recorder attached - so that the two can be compared chest by chest.

Everything is written under execution_logs/chest_traffic/, which git ignores:
the recording holds what the game sent to this account.
"""

import argparse
import base64
import glob
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

from modules.events import tbcodec

OUT_ROOT = os.path.join(ROOT_DIR, "execution_logs", "chest_traffic")
MODULE_DATA = os.path.join(ROOT_DIR, "config", "module_data")
CLAN_ROUTE = 15000
GIFT_EVENT = 300020


# ------------------------------------------------------------------- capture
def capture(args) -> int:
    from core.context import build_context
    from modules.chests.collector import ChestCollector
    from modules.events.traffic import TrafficListener
    from utils.text_utils import parse_key_value_line

    out_dir = os.path.join(OUT_ROOT, datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)

    ctx = build_context()
    ctx.start_browser()
    browser = ctx.browser
    chests = ChestCollector(ctx)
    scale = float(ctx.config.section("ocr").get("capture_scale", 2.0))

    records = []
    listener = TrafficListener(host=browser.host, port=browser.port, tab_id=browser.tab_id,
                               url_filter=browser.url_filter, on_record=records.append,
                               on_status=lambda text: print(f"traffic: {text}"))
    if not listener.start():
        print("Could not listen to the game's traffic.")
        return 1

    marks = []
    readings = []

    def mark(name):
        marks.append({"name": name, "ts": time.time()})
        print(f"--- {name} ({len(records)} records so far)")

    def read_screen(tab, page):
        visible = chests._visible_chests()
        if not visible:
            print(f"    {tab} #{page}: no 'Open' button on screen")
            return
        batch = chests._read_batch(visible, scale)
        now = time.time()
        for position, (rows, minutes) in enumerate(zip(batch, chests._minutes_left)):
            reading = {
                "tab": tab, "page": page, "position": position, "read_at": now, "rows": rows,
                "chest": rows[0].strip() if rows else "",
                "player": parse_key_value_line(rows[1])[1] if len(rows) >= 2 else "",
                "source": parse_key_value_line(rows[2])[1] if len(rows) >= 3 else "",
                "minutes_left": minutes,
            }
            readings.append(reading)
            print(f"    {tab} #{page}.{position}: {reading['chest']!r} from {reading['player']!r} "
                  f"({reading['source']!r}), {minutes} min left")

    def read_tab(tab):
        area = ctx.calibration.region("chest_area")
        x, y = area[0] + area[2] // 2, area[1] + area[3] // 2
        read_screen(tab, 0)
        # The collector never scrolls, but the list may be sent a page at a time.
        for page in range(1, args.scrolls + 1):
            mark(f"{tab}: scroll {page}")
            browser.scroll_down(x, y)
            time.sleep(1.5)
            read_screen(tab, page)
        browser.scroll_to_top(x, y)
        time.sleep(0.8)

    try:
        time.sleep(1.5)   # the listener needs a moment to attach before anything is clicked
        if args.reload:
            mark("reload")
            browser.reload()
            time.sleep(args.reload_wait)
            ctx.game_state.prepare_board()
        mark("idle")
        time.sleep(3.0)
        ctx.game_state.close_modals()

        clan = ctx.calibration.point("clan_button")
        gift = ctx.calibration.point("gift_button")
        mark("click clan")
        browser.click(clan[0], clan[1], delay=2.5)
        mark("click gifts")
        browser.click(gift[0], gift[1], delay=2.5)
        if chests.switch_tab("gifts_tab", "Gifts"):
            mark("tab gifts")
            time.sleep(2.0)
            read_tab("gifts")
        if chests.switch_tab("triumphal_gifts_tab", "Triumphal Gifts"):
            mark("tab triumphal")
            time.sleep(2.0)
            read_tab("triumphal")
            chests.switch_tab("gifts_tab", "Gifts")
        mark("close")
        ctx.game_state.close_modals()
        time.sleep(1.5)
    finally:
        listener.stop()
        browser.disconnect()

    with open(os.path.join(out_dir, "traffic.jsonl"), "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    with open(os.path.join(out_dir, "ocr.json"), "w", encoding="utf-8") as fh:
        json.dump({"marks": marks, "readings": readings}, fh, ensure_ascii=False, indent=1)

    print(f"\n{len(records)} records and {len(readings)} chest readings saved to {out_dir}")
    return 0


# -------------------------------------------------------------------- record
def record(args) -> int:
    """
    A real collection - every chest read, written to the database and opened, as
    on any run - with the traffic recorded beside it and a journal of what was
    opened, in the order it was opened.

    The game has to be open on the profile already: nothing here logs in or
    switches profile.
    """
    from core.context import build_context
    from modules.chests.collector import ChestCollector
    from modules.events.traffic import TrafficListener
    from utils.text_utils import parse_key_value_line

    journal = []

    class Recording(ChestCollector):
        tab = ""
        slot = 0
        read_at = 0.0
        after_record = staticmethod(lambda: None)

        def collect_tab(self, label, repo):
            self.tab = label
            return super().collect_tab(label, repo)

        def _read_batch(self, chests, scale, careful=False):
            self.slot = len(chests)
            self.read_at = time.time()
            return super()._read_batch(chests, scale, careful)

        def _record(self, rows, repo, crop=None, minutes_left=None):
            # Called from the last chest on screen to the first: see collect_tab.
            self.slot -= 1
            code = super()._record(rows, repo, crop, minutes_left)
            journal.append({
                "tab": self.tab, "slot": self.slot, "read_at": self.read_at, "opened_at": time.time(),
                "rows": rows, "code": code,
                "chest": rows[0].strip() if rows else "",
                "player": parse_key_value_line(rows[1])[1] if len(rows) >= 2 else "",
                "source": parse_key_value_line(rows[2])[1] if len(rows) >= 3 else "",
                "minutes_left": minutes_left,
            })
            self.after_record()
            return code

    ctx = build_context()
    profiles = [profile for account in ctx.config.enabled_accounts() for profile in account.active_profiles
                if profile.has_database and (not args.profile or args.profile.lower() in profile.name.lower())]
    if len(profiles) != 1:
        print(f"Expected exactly one profile with a database, found {len(profiles)}: "
              f"{[p.label for p in profiles]}. Name one with --profile.")
        return 2
    profile = profiles[0]

    out_dir = os.path.join(OUT_ROOT, datetime.now().strftime("%Y%m%d_%H%M%S") + "_record")
    os.makedirs(out_dir, exist_ok=True)

    ctx.start_browser()
    browser = ctx.browser
    chests = Recording(ctx)
    # The screen is the half of the comparison this run exists to produce.
    chests.options = dict(chests.options, read_from_game=False)

    # Written as it arrives: a run this long that is killed halfway must not take
    # the list it started from with it.
    records = []
    traffic_file = open(os.path.join(out_dir, "traffic.jsonl"), "w", encoding="utf-8")

    def on_record(item):
        records.append(item)
        traffic_file.write(json.dumps(item) + "\n")
        traffic_file.flush()

    marks = []
    result = {}

    def save_journal():
        with open(os.path.join(out_dir, "journal.json"), "w", encoding="utf-8") as fh:
            json.dump({"profile": profile.label, "marks": marks, "result": result, "opened": journal},
                      fh, ensure_ascii=False)

    listener = TrafficListener(host=browser.host, port=browser.port, tab_id=browser.tab_id,
                               url_filter=browser.url_filter, on_record=on_record,
                               on_status=lambda text: print(f"traffic: {text}"))
    if not listener.start():
        print("Could not listen to the game's traffic.")
        return 1

    def mark(name):
        marks.append({"name": name, "ts": time.time()})
        print(f"--- {name} ({len(records)} records so far)")

    chests.after_record = save_journal
    started = time.time()
    try:
        time.sleep(1.5)   # the listener needs a moment to attach before anything is clicked
        ctx.game_state.close_modals()
        mark("collection")
        chests.begin_run([profile])
        try:
            result = chests.run_for_profile(profile)
        finally:
            chests.end_run([dict(result, module=chests.key)] if result else [],
                           duration=str(datetime.now() - datetime.fromtimestamp(started)))
        time.sleep(2.0)

        # What the game says is left, asked the way the list was got in the first place.
        mark("clan window again")
        clan = ctx.calibration.point("clan_button")
        browser.click(clan[0], clan[1], delay=4.0)
        mark("close")
        ctx.game_state.close_modals()
        time.sleep(1.5)
    finally:
        listener.stop()
        browser.disconnect()
        traffic_file.close()
        save_journal()
        print(f"\n{len(records)} records and {len(journal)} opened chests saved to {out_dir}")
    return 0 if result.get("success") else 1


def screen_order(opened):
    """The journal put back in the order the panel listed the chests: oldest first."""
    ordered, batch = [], []
    for entry in opened:
        # A batch is opened from its last slot to its first, so the slot only falls inside one.
        if batch and (entry["slot"] >= batch[-1]["slot"] or entry["tab"] != batch[-1]["tab"]):
            ordered.extend(reversed(batch))
            batch = []
        batch.append(entry)
    ordered.extend(reversed(batch))
    return ordered


def report_recording(folder, records, journal, players) -> None:
    marks, opened = journal["marks"], journal["opened"]
    print(f"Profile {journal.get('profile')}: {journal.get('result')}")

    def phase(ts):
        name = "before"
        for m in marks:
            if ts >= m["ts"]:
                name = m["name"]
        return name

    ts_of = {r["idx"]: r["ts"] for r in records}
    print("\n== Lists of gifts anywhere in the traffic ==")
    found = list(gift_lists(records, anywhere=True))
    for idx, label, rows in found:
        print(f"  record {idx} ({phase(ts_of[idx])}, {datetime.fromtimestamp(ts_of[idx]):%H:%M:%S}) {label}: "
              f"{len(rows)} gifts, ids {rows[0][0]}..{rows[-1][0]}")
    if not found:
        print("  none")
        return

    first_open = min((e["opened_at"] for e in opened), default=float("inf"))
    before = [(idx, rows) for idx, label, rows in found
              if label == f"route {CLAN_ROUTE}" and ts_of[idx] < first_open]
    if not before:
        print("  no list arrived before the first chest was opened")
        return
    idx, gifts = max(before, key=lambda item: len(item[1]))
    listed = len(gifts)
    arrived = arriving_gifts(records)
    later = [row for gift_id, row in sorted(arrived.items()) if gift_id > gifts[-1][0]]
    print(f"\n== Gifts announced one at a time (event {GIFT_EVENT}) ==")
    print(f"  {len(arrived)} in the recording, {len(later)} of them after the list "
          f"(ids {later[0][0]}..{later[-1][0]})" if later else f"  {len(arrived)} in the recording, none after the list")
    gifts = gifts + later
    ids = [row[0] for row in gifts]
    print(f"  list + arrivals: {len(gifts)} gifts, ids {ids[0]}..{ids[-1]} "
          f"({'no gaps' if ids[-1] - ids[0] + 1 == len(ids) else 'WITH GAPS'}), "
          f"{sum(max(row[4], 1) for row in gifts)} chests")

    # What the game says is still waiting once the run is over - the list asked
    # for again - was not opened, whatever the reason, and is left out of the comparison.
    after = [rows_ for idx_, label, rows_ in found if ts_of[idx_] > first_open and label == f"route {CLAN_ROUTE}"]
    left = {row[0]: row for row in (after[-1] if after else [])}
    skipped = [row for row in gifts if row[0] in left]
    if after:
        print(f"  still waiting after the run: {len(left)} gift(s) "
              f"{[(row[0], players.get(row[1][0], '?'), row[2]) for row in left.values()][:10]}")
    gifts = [row for row in gifts if row[0] not in left]
    # The row's 5th field is how many chests the gift is: the panel lists one line
    # for each (a kill of an epic monster drops several at once).
    rows = [row for row in gifts for _ in range(max(row[4], 1))]
    by_tab = Counter(e["tab"] for e in opened)
    print(f"\n== {len(opened)} chests opened {dict(by_tab)} against {len(gifts)} gifts ({listed} listed in record "
          f"{idx} + {len(later)} arrived), {len(rows)} chests counting each gift's quantity ==")
    print(f"  written: {Counter(e['code'] for e in opened).get(1, 0)} complete, "
          f"{Counter(e['code'] for e in opened).get(2, 0)} incomplete")

    ids_of = defaultdict(set)
    for player_id, name in players.items():
        ids_of[norm(name)].add(player_id)

    screen = screen_order(opened)
    names = defaultdict(Counter)        # chest type -> (name, source) the collector recorded
    fifth = defaultdict(Counter)        # source -> the row's 5th field
    drifts, wrong = [], []
    for position, entry in enumerate(screen):
        if position >= len(rows):
            break
        row = rows[position]
        sender = players.get(row[1][0], f"id:{row[1][0]}")
        same_player = row[1][0] in ids_of.get(norm(entry["player"]), ())
        if entry["minutes_left"] is not None:
            drifts.append(row[3] - (entry["read_at"] + entry["minutes_left"] * 60))
        if entry["code"] == 1:
            names[row[2]][(entry["chest"], entry["source"])] += 1
            fifth[entry["source"]][row[4]] += 1
        if not same_player:
            wrong.append((position, row, sender, entry))

    compared = min(len(screen), len(rows))
    print(f"  compared in order: {compared}; sender agrees with the name read in {compared - len(wrong)}")
    if drifts:
        drifts.sort()
        print(f"  expiry against the countdown: median {drifts[len(drifts) // 2]:+.0f}s, "
              f"from {drifts[0]:+.0f}s to {drifts[-1]:+.0f}s")
    for position, row, sender, entry in wrong[:25]:
        print(f"    #{position} gift {row[0]} type {row[2]} from {sender!r}  <->  screen: {entry['chest']!r} from "
              f"{entry['player']!r} ({entry['source']!r}) [{entry['tab']}, code {entry['code']}]")
    if len(screen) > len(rows):
        extra = screen[len(rows):]
        print(f"  {len(extra)} chest(s) opened beyond the list (they arrived after it): "
              f"{Counter(e['tab'] for e in extra)}")
    if len(rows) > len(screen):
        print(f"  {len(rows) - len(screen)} gift(s) of the list were not opened")

    print("\n== Chest type -> what the collector recorded ==")
    clean = 0
    for chest_type, seen in sorted(names.items()):
        (chest, source), count = seen.most_common(1)[0]
        total = sum(seen.values())
        if len(seen) == 1:
            clean += 1
            print(f"  {chest_type}: {chest!r} / {source!r} x{count}")
        else:
            print(f"  {chest_type}: " + "; ".join(f"{c!r} / {s!r} x{n}" for (c, s), n in seen.most_common())
                  + f"   <-- {total - count} of {total} differ")
    print(f"  {clean} of {len(names)} types always recorded the same way")

    print("\n== The gift's quantity (5th field), by source: chests of each quantity ==")
    for source, values in sorted(fifth.items()):
        if set(values) != {1}:
            print(f"  {source!r}: {dict(sorted(values.items()))}")

    print(f"\n== Count ==\n  the traffic accounts for {len(rows)} chests opened ({len(skipped)} gift(s) left waiting); "
          f"the collector opened and wrote {len(screen)}")

    with open(os.path.join(folder, "chest_types.json"), "w", encoding="utf-8") as fh:
        json.dump({str(t): [{"name": c, "source": s, "count": n} for (c, s), n in seen.most_common()]
                   for t, seen in sorted(names.items())}, fh, ensure_ascii=False, indent=1)
    print(f"\nTypes saved to {os.path.join(folder, 'chest_types.json')}")


# --------------------------------------------------------------------- types
def types(args) -> int:
    """
    The chest types of a recording against `standard_chests`, and - with --write -
    into the profile's database.

    A type is one chest of one source ('Bone Chest' of a 'Level 25 Crypt'), while
    `standard_chests` has one row per source: several types point at the same row,
    which is why they are a table of their own rather than a column of that one.
    """
    from config.settings import ConfigManager
    from database.db_connection import open_connection

    folder = args.folder or (sorted(glob.glob(os.path.join(OUT_ROOT, "*_record", ""))) or [None])[-1]
    if not folder or not os.path.exists(os.path.join(folder, "chest_types.json")):
        print("No analysed recording: run 'record' and then 'analyze' first.")
        return 1
    with open(os.path.join(folder, "chest_types.json"), encoding="utf-8") as fh:
        found = json.load(fh)

    profiles = [p for a in ConfigManager().enabled_accounts() for p in a.active_profiles if p.has_database]
    if len(profiles) != 1:
        print(f"Expected exactly one profile with a database, found {len(profiles)}.")
        return 2
    connection, _ = open_connection(profiles[0].database, timeout=6)
    cursor = connection.cursor()
    cursor.execute("SELECT id, source, score, monster FROM standard_chests")
    standard = {source: (row_id, score, monster) for row_id, source, score, monster in cursor.fetchall()}

    rows, unsure = [], []
    for type_id, seen in sorted(found.items(), key=lambda item: int(item[0])):
        best, total = seen[0], sum(entry["count"] for entry in seen)
        # One chest read wrong in a run is expected (see the analysis); a type the
        # screen did not mostly agree on is not written.
        if best["count"] * 2 <= total:
            unsure.append((type_id, seen))
            continue
        rows.append((int(type_id), best["name"], best["source"], standard.get(best["source"], (None,))[0], best["count"]))

    by_source = defaultdict(list)
    for type_id, name, source, standard_id, count in rows:
        by_source[source].append((type_id, name, count))
    print(f"{len(rows)} chest types of {len(by_source)} sources (database '{profiles[0].database.database}'):\n")
    for source, items in sorted(by_source.items()):
        row = standard.get(source)
        where = f"standard_chests #{row[0]}, score {row[1]}" + (", epic monster" if row[2] else "") if row \
            else "NOT IN standard_chests"
        print(f"  {source}  [{where}]")
        for type_id, name, count in items:
            print(f"      {type_id:>7}  {name}  x{count}")
    for type_id, seen in unsure:
        print(f"  NOT WRITTEN, the screen disagreed: {type_id} {seen}")

    if args.write:
        from database.repositories import ChestRepository

        repo = ChestRepository(connection)
        if not repo.has_column("game_chest_types", "game_type_id"):
            print("\nThis database has no game_chest_types table: update the site and run its migrations.")
            connection.close()
            return 2
        # Only types the table does not name yet: one already there is left as it is.
        new = sum(1 for type_id, name, source, _standard, _count in rows
                  if repo.learn_chest_type(type_id, name, source))
        cursor.execute("SELECT COUNT(*), SUM(standard_chest_id IS NOT NULL) FROM game_chest_types")
        total, linked = cursor.fetchone()
        print(f"\nWritten: {new} new; game_chest_types now has {total} types, {linked} linked to standard_chests.")
    else:
        print("\nNothing written: pass --write to add the types game_chest_types does not have yet.")
    connection.close()
    return 0


# ------------------------------------------------------------------- analyze
def known_players() -> dict:
    """player id -> name, from what the members and events modules already saved."""
    players = {}
    try:
        with open(os.path.join(MODULE_DATA, "events", "player_profiles.json"), encoding="utf-8") as fh:
            for key, value in (json.load(fh).get("profiles") or {}).items():
                players[int(key)] = value.get("name", "")
    except (OSError, ValueError):
        pass
    for path in glob.glob(os.path.join(MODULE_DATA, "members", "*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                for member in json.load(fh).get("members") or []:
                    players[int(member["player_id"])] = member["name"]
        except (OSError, ValueError, KeyError):
            pass
    return players


def walk(node, path=""):
    """Every leaf of a decoded tree, with a path that tells where it sits."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{path}[{index}]")
    else:
        yield path, node


def shape(path: str) -> str:
    """A path with its list positions blanked from the second level on, to group rows of one list."""
    import re
    return re.sub(r"\[\d+\]", "[*]", path)


def trees_of(record):
    """(label, tree) for each frame of a record: the route for HTTP, 'ws' for a stream."""
    tree = tbcodec.decode_payload(base64.b64decode(record["b64"]))
    if not isinstance(tree, dict):
        return
    for frame in tree.get("frames") or []:
        yield f"route {frame.get('route')}", {"objects": frame.get("objects"), "events": frame.get("events")}
    if "stream" in tree:
        yield "ws", tree["stream"]
    if "json" in tree:
        yield f"tag {tree.get('tag')}", tree["json"]


def norm(text) -> str:
    return "".join(ch for ch in str(text).lower() if ch.isalnum())


def is_gift_row(row) -> bool:
    """`[gift_id, [player_id], chest_type, expires_at, n, n]`, as the clan record lists the gifts."""
    return (isinstance(row, list) and len(row) == 6
            and isinstance(row[1], list) and len(row[1]) == 1
            and all(isinstance(v, int) and not isinstance(v, bool) for v in (row[0], row[1][0], *row[2:])))


def gift_lists(records, anywhere: bool = False):
    """
    Every list of gifts in a recording: (record idx, rows oldest first).

    Looked for in the clan record only, unless `anywhere` - which is how gifts
    arriving some other way (a notification, say) would be noticed.
    """
    for record in records:
        if not anywhere and CLAN_ROUTE not in (record.get("routes") or []):
            continue
        for label, tree in trees_of(record):
            if not anywhere and label != f"route {CLAN_ROUTE}":
                continue
            stack = [tree]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    stack.extend(node.values())
                elif isinstance(node, list):
                    if node and all(is_gift_row(row) for row in node):
                        if anywhere:
                            yield record["idx"], label, sorted(node, key=lambda row: row[0])
                        else:
                            yield record["idx"], sorted(node, key=lambda row: row[0])
                    else:
                        stack.extend(node)


def arriving_gifts(records) -> dict:
    """
    The gifts announced one at a time, after the list was sent: gift id -> row.

    A gift generated while the game is open comes as an event on the tail of
    whatever answer is sent next: `[u32 len][[session, seq, time, 300020]][[row]]`.
    """
    gifts = {}
    for record in records:
        if record["kind"] != "RESP":
            continue
        data = tbcodec._decompress(base64.b64decode(record["b64"]))
        try:
            frames = list(tbcodec.iter_frames(data))
        except RecursionError:
            continue
        for frame in frames:
            for event in tbcodec.tail_events(frame.tail):
                header = event[0]
                if not (isinstance(header, list) and len(header) == 4 and header[3] == GIFT_EVENT):
                    continue
                for payload in event[1:]:
                    for row in payload if isinstance(payload, list) else []:
                        if is_gift_row(row):
                            gifts.setdefault(row[0], row)
    return gifts


def align(readings, rows, players):
    """
    Places each screen of the panel on the list of gifts.

    A screen is a run of consecutive chests, so it has to sit on a run of
    consecutive rows: same players in the same order, each expiring when its
    countdown says. Returns [(reading, row or None)].
    """
    ids_of = defaultdict(set)
    for player_id, name in players.items():
        ids_of[norm(name)].add(player_id)

    def fits(reading, row):
        if row[1][0] not in ids_of.get(norm(reading["player"]), ()):
            return False
        if reading["minutes_left"] is None:
            return True
        # The countdown is in whole minutes, and the screen was read a moment after it was drawn.
        return abs(row[3] - (reading["read_at"] + reading["minutes_left"] * 60)) <= 120

    screens = defaultdict(list)
    for reading in readings:
        screens[(reading["tab"], reading["page"])].append(reading)

    placed = []
    for screen in screens.values():
        offsets = [k for k in range(len(rows) - len(screen) + 1)
                   if all(fits(reading, rows[k + i]) for i, reading in enumerate(screen))]
        for i, reading in enumerate(screen):
            placed.append((reading, rows[offsets[0] + i] if len(offsets) == 1 else None, len(offsets)))
    return placed


def report_gifts(records, readings, players) -> None:
    print("\n== The list of gifts in the clan record (route 15000) ==")
    lists = list(gift_lists(records))
    if not lists:
        print("  none: the clan window has to be opened during the recording, and only its first answer "
              "carries the list")
        return
    idx, rows = max(lists, key=lambda item: len(item[1]))
    ids = [row[0] for row in rows]
    print(f"  record {idx}: {len(rows)} gifts, ids {ids[0]}..{ids[-1]} "
          f"({'no gaps' if ids[-1] - ids[0] + 1 == len(ids) else 'WITH GAPS'})")
    print(f"  expiring from {datetime.fromtimestamp(rows[0][3]):%d/%m %H:%M:%S} "
          f"to {datetime.fromtimestamp(rows[-1][3]):%d/%m %H:%M:%S}")
    unknown = sorted({row[1][0] for row in rows if row[1][0] not in players})
    print(f"  senders: {len({row[1][0] for row in rows})} players, {len(unknown)} without a known name {unknown[:5]}")
    print(f"  5th field: {dict(Counter(row[4] for row in rows))}   6th field: {dict(Counter(row[5] for row in rows))}")

    print("\n  Screen against list (row = gift id, sender, chest type):")
    names = defaultdict(Counter)
    matched = 0
    for reading, row, candidates in align(readings, rows, players):
        where = f"{reading['tab']} #{reading['page']}.{reading['position']}"
        seen = f"{reading['chest']!r} from {reading['player']!r} ({reading['source']!r})"
        if row is None:
            print(f"    {where}: {seen}  ->  {'no place' if not candidates else f'{candidates} places'} on the list")
            continue
        matched += 1
        names[row[2]][(reading["chest"], reading["source"])] += 1
        drift = row[3] - (reading["read_at"] + (reading["minutes_left"] or 0) * 60)
        print(f"    {where}: {seen}  ->  #{row[0]} {players.get(row[1][0], '?')!r} type {row[2]} "
              f"(expiry {drift:+.0f}s from the countdown)")
    print(f"\n  {matched} of {len(readings)} readings placed on the list")

    print("\n  Chest type -> what the screen calls it:")
    for chest_type, seen in sorted(names.items()):
        verdict = "" if len(seen) == 1 else "   <-- ONE TYPE, SEVERAL NAMES"
        print(f"    {chest_type}: " + "; ".join(f"{c!r} / {s!r} x{n}" for (c, s), n in seen.items()) + verdict)
    types = Counter(row[2] for row in rows)
    print(f"  {len(names)} of the list's {len(types)} chest types were on screen "
          f"({sum(types[t] for t in names)} of {len(rows)} gifts)")


def analyze(args) -> int:
    folder = args.folder
    if not folder:
        folders = sorted(glob.glob(os.path.join(OUT_ROOT, "*", "")))
        if not folders:
            print("No capture yet: run 'capture' first.")
            return 1
        folder = folders[-1]
    print(f"Capture: {folder}\n")

    with open(os.path.join(folder, "traffic.jsonl"), encoding="utf-8") as fh:
        records = [json.loads(line) for line in fh if line.strip()]
    if os.path.exists(os.path.join(folder, "journal.json")):
        with open(os.path.join(folder, "journal.json"), encoding="utf-8") as fh:
            report_recording(folder, records, json.load(fh), known_players())
        return 0
    with open(os.path.join(folder, "ocr.json"), encoding="utf-8") as fh:
        ocr = json.load(fh)
    marks, readings = ocr["marks"], ocr["readings"]
    players = known_players()

    def phase(ts):
        name = "before"
        for m in marks:
            if ts >= m["ts"]:
                name = m["name"]
        return name

    # 1. What arrived, and after which click.
    print("== Traffic by phase ==")
    by_phase = defaultdict(Counter)
    for record in records:
        labels = [label for label, _ in trees_of(record)] or ["(not decoded)"]
        for label in labels:
            by_phase[phase(record["ts"])][f"{record['kind']} {label}"] += 1
    for m in [{"name": "before"}] + marks:
        if by_phase.get(m["name"]):
            print(f"  {m['name']}: " + ", ".join(f"{k} x{v}" for k, v in sorted(by_phase[m["name"]].items())))

    # 2. The reference.
    print(f"\n== OCR reference: {len(readings)} chest readings ==")
    for r in readings:
        print(f"  {r['tab']} #{r['page']}.{r['position']}: {r['chest']!r} | {r['player']!r} | {r['source']!r} "
              f"| {r['minutes_left']} min")

    words = {}
    for r in readings:
        for kind in ("chest", "player", "source"):
            if len(norm(r[kind])) >= 4:
                words[norm(r[kind])] = (kind, r[kind])
    wanted_names = {norm(r["player"]) for r in readings if r["player"]}
    wanted_ids = {pid: name for pid, name in players.items() if norm(name) in wanted_names}
    print(f"  players of the reference with a known game id: "
          f"{', '.join(f'{n}={i}' for i, n in wanted_ids.items()) or 'none'}")

    # When each chest expires, as the panel's countdown puts it (a minute either way).
    expiries = [(r["read_at"] + r["minutes_left"] * 60, r) for r in readings if r["minutes_left"] is not None]

    # 3. Where the reference shows up in the traffic.
    text_hits = defaultdict(Counter)     # (label, shape) -> the words found there
    id_hits = defaultdict(Counter)       # (label, shape) -> the players found there
    time_hits = defaultdict(Counter)     # (label, shape) -> the chests whose expiry is there
    hit_records = defaultdict(set)
    for record in records:
        for label, tree in trees_of(record):
            for path, value in walk(tree):
                key = (label, shape(path))
                if isinstance(value, str):
                    plain = norm(value)
                    for word, (kind, original) in words.items():
                        if word in plain:
                            text_hits[key][f"{kind} {original!r}"] += 1
                            hit_records[key].add(record["idx"])
                elif isinstance(value, int) and not isinstance(value, bool):
                    if value in wanted_ids:
                        id_hits[key][wanted_ids[value]] += 1
                        hit_records[key].add(record["idx"])
                    for unit in (1, 1000):
                        for expiry, r in expiries:
                            if abs(value / unit - expiry) <= 90:
                                time_hits[key][f"{r['tab']} #{r['page']}.{r['position']}"] += 1
                                hit_records[key].add(record["idx"])

    def report(title, hits):
        print(f"\n== {title} ==")
        if not hits:
            print("  nothing")
        for (label, where), found in sorted(hits.items(), key=lambda item: -sum(item[1].values())):
            print(f"  {label} {where}  (records {sorted(hit_records[(label, where)])[:8]})")
            print("      " + ", ".join(f"{k} x{v}" for k, v in found.most_common(12)))

    report("Words of the reference found as text", text_hits)
    report("Ids of the reference's players found", id_hits)
    report("Expiry of the reference's chests found (countdown -> timestamp)", time_hits)

    report_gifts(records, readings, players)

    # 4. The records worth reading by eye, decoded.
    interesting = sorted(set().union(*hit_records.values())) if hit_records else []
    dump_path = os.path.join(folder, "candidates.json")
    with open(dump_path, "w", encoding="utf-8") as fh:
        json.dump([
            {"idx": r["idx"], "kind": r["kind"], "url": r["url"], "phase": phase(r["ts"]), "size": r["size"],
             "tree": tbcodec.decode_payload(base64.b64decode(r["b64"]))}
            for r in records if r["idx"] in interesting
        ], fh, ensure_ascii=False, indent=1, default=lambda o: f"<{len(o)} bytes>")
    print(f"\n{len(interesting)} candidate record(s) decoded to {dump_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    cap = sub.add_parser("capture", help="open the gifts panel, read it and record the traffic")
    cap.add_argument("--reload", action="store_true", help="reload the game first, to record what arrives at login")
    cap.add_argument("--reload-wait", type=float, default=40.0, help="seconds to let the game load after a reload")
    cap.add_argument("--scrolls", type=int, default=2, help="how many times to scroll each tab (default 2)")
    rec = sub.add_parser("record", help="a real collection, with the traffic recorded beside it")
    rec.add_argument("--profile", help="the profile the game is on, when more than one has a database")
    ana = sub.add_parser("analyze", help="look for the OCR reference inside a recording")
    ana.add_argument("folder", nargs="?", help="a capture folder (default: the last one)")
    typ = sub.add_parser("types", help="the chest types of a recording against standard_chests")
    typ.add_argument("folder", nargs="?", help="a recording folder (default: the last one)")
    typ.add_argument("--write", action="store_true", help="create game_chest_types in the database and fill it")
    args = parser.parse_args()
    return {"capture": capture, "record": record, "analyze": analyze, "types": types}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
