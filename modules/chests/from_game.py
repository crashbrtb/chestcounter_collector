"""
Collecting a tab of gifts from what the game sends, instead of from what it shows.

The screen is still what gets clicked - there is no other way to open a chest -
but it is no longer what gets written. Each chest's player, type and hour come
from the game's own list (see gifts.py), which the screen can only approximate:
a name the engine misreads, a countdown in whole minutes, a panel captured a
moment before it refreshed. Measured on a real run of 859 chests, the screen
got one batch of four wrong in exactly that last way - a chest written twice
and another never written - and nothing on it could have told.

How a run goes
--------------
The panel lists the chests oldest first, which is the order of the gift ids, and
the game answers once for every chest opened. So the chests on screen are the
next ones of the list, counted from how many were opened so far:

    1. the gifts those chests belong to are written to the database;
    2. their 'Open' buttons are clicked;
    3. the game's answers are counted, and the list moves on by that many.

Writing comes before clicking, as it always did: a chest written and not opened
can be put right, one opened and not written is gone.

A gift is written whole - an epic monster's five chests are five rows at once,
even with two of them still off screen - so a gift is in the database entirely
or not at all, and one found there already (a run stopped halfway) is simply not
written again.

Settling
--------
That bookkeeping rests on the panel showing exactly the head of the list. When
it does not - the recording has a gift the game announced and the panel never
showed until the window was reopened - a gift ends up written while another is
opened. So the tab ends by asking the game for the list again and comparing:

    * a gift that is gone was opened: if it was never written, it is now;
    * a gift still there, untouched, was not opened: what this run wrote for it
      is taken back, and it waits for the next run.

After that the database holds what was opened, whatever the screen did.

When the screen is needed
-------------------------
The game sends a chest's type as a number. A type met for the first time is
named from the screen - once, into `game_chest_types` - and so is a player the
site's member list does not know yet. The reading is only believed when its
countdown agrees with the gift's own expiry - or, where the capture holds no
countdown at all, when every line of the batch names the player the game gave
for it; otherwise the chest goes to the review queue with its picture and what
the game did say about it.
"""

import time
from collections import Counter
from typing import Callable, Dict, List, Optional, Tuple

from utils.cancel import cancellation
from utils.logger import logger
from utils.text_utils import parse_key_value_line

from .gifts import PLAIN_GIFT, Gift, GiftFeed

# How long the game is given to answer the clicks of one batch. It answers in
# about a third of a second; this only bounds the wait when a click missed.
CONFIRM_WAIT = 3.0

# Batches in a row with no answer from the game before the list is asked for
# again: either the clicks are not landing or the answers are not being heard,
# and in both cases the count of opened chests can no longer be trusted.
STALLED_BATCHES = 2

# How many times a tab is settled and resumed. One pass is the normal case; a
# second picks up what the panel only showed after being reopened.
MAX_PASSES = 4

# How long the panel is given to redraw before a chest it shows, and the game
# does not list, is believed to be there.
SCREEN_SETTLE_WAIT = 1.0

# How far a screen countdown may be from the gift's expiry and still be the
# same chest: the panel prints whole minutes and was read a moment before.
COUNTDOWN_TOLERANCE = 120


class GameCollection:
    """One tab of gifts, collected from the game's list. See the module's docstring."""

    def __init__(self, module, repo, feed: GiftFeed, label: str, reopen: Callable[[], bool],
                 answer_wait: float = 10.0):
        self.module = module
        self.repo = repo
        self.feed = feed
        self.label = label
        self.reopen = reopen              # closes the clan window and opens the tab again
        self.answer_wait = answer_wait

        self.types: Dict[int, Tuple[str, str]] = repo.chest_types()
        self.players: Dict[int, str] = repo.game_players()
        self.in_database: Dict[int, int] = {}       # gift id -> chests the database had when last asked
        self.written: Dict[int, List[int]] = {}     # gift id -> rows this run wrote for it
        self.filed: set = set()                     # gifts this run sent to the review queue
        self.collected = 0
        self.incomplete = 0
        self.clicked = 0

    # ------------------------------------------------------------------ run
    def run(self) -> Tuple[int, int, bool]:
        """
        (collected, incomplete, finished). Not finished means the game's list
        could not be followed to the end, and the screen has to take over.
        """
        logger.info(f"Collecting the '{self.label}' tab from the game's own list "
                    f"({len(self.types)} chest types and {len(self.players)} players known)...")
        troubles = 0
        for _ in range(MAX_PASSES):
            reason = self._open_listed()
            left = self._settle()
            if left is None:
                logger.warning(f"[{self.module.profile_label}] The game did not send the gift list again: "
                               f"what was written could not be checked against it.")
                return self.collected, self.incomplete, False
            if reason != "empty":
                troubles += 1
                if troubles > 1:
                    return self.collected, self.incomplete, False
            if not left or not self.module._visible_chests():
                if left:
                    logger.info(f"{left} chest(s) are still listed by the game but not shown in '{self.label}'; "
                                f"they are left for the next run.")
                break
            logger.info(f"{left} chest(s) still waiting in '{self.label}' after reopening it; continuing.")

        logger.info(f"Tab '{self.label}': {self.collected} collected, {self.incomplete} incomplete "
                    f"({self.clicked} opened).")
        return self.collected, self.incomplete, True

    # -------------------------------------------------------------- opening
    def _line(self) -> List[Gift]:
        """The chests as the panel lists them: one entry per chest, oldest gift first."""
        return [gift for gift in self.feed.snapshot() if gift.kind == PLAIN_GIFT for _ in range(gift.quantity)]

    def _open_listed(self) -> str:
        """
        Opens what the panel shows, writing each gift first. Returns why it stopped:
        'empty' (nothing left on screen), 'unlisted' (the panel shows a chest the
        game never listed), 'stalled' (the game stopped answering the clicks) or
        'ceiling'.
        """
        from .collector import MAX_CHESTS_PER_TAB

        module = self.module
        chest_delay = float(module.config.section("timing").get("chest_click_delay", 0.25))
        stalled = 0

        self._load_database_counts()
        while self.clicked < MAX_CHESTS_PER_TAB:
            cancellation.check()
            chests = module._visible_chests() or module._look_again(self.label)
            if not chests:
                return "empty"

            _lists, opened = self.feed.state()
            head = self._line()[opened:opened + len(chests)]
            if len(head) < len(chests):
                # Most often the panel has simply not redrawn after the last
                # clicks, and still shows chests that are gone: look once more.
                cancellation.sleep(SCREEN_SETTLE_WAIT)
                chests = module._visible_chests()
                if not chests:
                    continue
                _lists, opened = self.feed.state()
                head = self._line()[opened:opened + len(chests)]
            if len(head) < len(chests):
                logger.warning(f"[{module.profile_label}] {len(chests)} chest(s) on screen but the game listed "
                               f"only {len(head)} more; asking it for the list again.")
                return "unlisted"

            pending = [gift for gift in dict.fromkeys(head) if not self._is_written(gift)]
            readings = self._read_screen(chests, head) if any(self._needs_screen(g) for g in pending) else {}
            for gift in pending:
                cancellation.check()
                self._write(gift, readings.get(gift.gift_id))

            # Bottom-up, as ever: opening a chest shifts everything below it up.
            for chest in reversed(chests):
                button = chest["button"]
                module.browser.click(button[0] + button[2] // 2, button[1] + button[3] // 2, delay=chest_delay)
            self.clicked += len(chests)

            wanted = opened + len(chests)
            self._wait(lambda: self.feed.state()[1] >= wanted, CONFIRM_WAIT)
            answered = self.feed.state()[1] - opened
            if answered > 0:
                stalled = 0
            else:
                stalled += 1
                if stalled >= STALLED_BATCHES:
                    logger.warning(f"[{module.profile_label}] The game answered none of the last "
                                   f"{stalled * len(chests)} clicks on 'Open'; asking it for the list again.")
                    return "stalled"

        logger.warning(f"Reached the {MAX_CHESTS_PER_TAB} chest ceiling in '{self.label}'.")
        return "ceiling"

    # -------------------------------------------------------------- writing
    def _load_database_counts(self) -> None:
        gifts = self.feed.snapshot()
        self.in_database = self.repo.chests_of_gifts(gifts[0].gift_id, gifts[-1].gift_id) if gifts else {}
        already = sum(1 for gift in gifts if self.in_database.get(gift.gift_id) and gift.gift_id not in self.written)
        if already:
            logger.info(f"{already} gift(s) of the list are in the database already, from a run that did not "
                        f"finish; they are opened without being written again.")

    def _is_written(self, gift: Gift) -> bool:
        return bool(self.in_database.get(gift.gift_id)) or gift.gift_id in self.written or gift.gift_id in self.filed

    def _needs_screen(self, gift: Gift) -> bool:
        return gift.chest_type not in self.types or gift.player_id not in self.players

    def _read_screen(self, chests: List[dict], head: List[Gift]) -> Dict[int, dict]:
        """
        What the screen says about each gift of the batch: gift id -> reading.

        A reading is kept only when its countdown is the gift's own - the proof
        that the line read is the chest it is taken for, and not the one that
        was there a moment ago.
        """
        module = self.module
        scale = float(module.config.section("ocr").get("capture_scale", 2.0))

        readings: Dict[int, dict] = {}
        # Twice at most: a line that does not match is usually the panel caught
        # before it redrew, and a second look a moment later finds the right one.
        for attempt in (1, 2):
            read_at = time.time()
            batch = module._read_resolved(chests, scale)
            crops = module._crops if len(module._crops) == len(chests) else [None] * len(chests)
            minutes = module._minutes_left if len(module._minutes_left) == len(chests) else [None] * len(chests)

            # A chest area calibrated around the text alone leaves every countdown
            # out of the capture. The players then vouch for the lines instead.
            vouched = all(left is None for left in minutes) and self._players_agree(head, batch)

            unmatched = []
            for gift, rows, crop, left in zip(head, batch, crops, minutes):
                if readings.get(gift.gift_id, {}).get("chest"):
                    continue
                reading = {"crop": crop, "chest": "", "player": "", "source": ""}
                if left is None:
                    fits = vouched and gift.player_id in self.players
                else:
                    fits = abs(gift.expires_at - (read_at + left * 60)) <= COUNTDOWN_TOLERANCE
                if fits and len(rows) >= 3:
                    reading.update(chest=rows[0].strip(), player=parse_key_value_line(rows[1])[1],
                                   source=parse_key_value_line(rows[2])[1])
                elif self._needs_screen(gift):
                    unmatched.append((gift, rows, left))
                readings[gift.gift_id] = reading
            if not unmatched:
                break
            if attempt == 1:
                cancellation.sleep(SCREEN_SETTLE_WAIT)
            else:
                for gift, rows, left in unmatched:
                    logger.warning(f"[{module.profile_label}] The screen could not be matched to gift "
                                   f"{gift.gift_id} (read={rows}, countdown={left} min).")
        return readings

    def _players_agree(self, head: List[Gift], batch: List[List[str]]) -> bool:
        """
        Whether every line of the batch names the player the game gave for its
        place in the list - what stands in for the countdown when none was read.

        A panel caught before it redrew shows the chests of a moment ago, and
        their players with them: one line out of place is enough to refuse the
        whole batch. A player the site does not know proves nothing either way,
        and at least one line has to be checked for the batch to pass.
        """
        checked = 0
        for gift, rows in zip(head, batch):
            if len(rows) < 3:
                return False
            known = self.players.get(gift.player_id)
            if not known:
                continue
            if parse_key_value_line(rows[1])[1].strip().casefold() != known.strip().casefold():
                return False
            checked += 1
        return checked > 0

    def _write(self, gift: Gift, reading: Optional[dict] = None) -> None:
        """Writes every chest of a gift, naming whatever the game sent only as a number."""
        module = self.module
        reading = reading or {}
        game = {"game_gift_id": gift.gift_id, "game_type_id": gift.chest_type, "game_player_id": gift.player_id}

        name, source = self.types.get(gift.chest_type, ("", ""))
        if not name and reading.get("chest") and reading.get("source"):
            name, source = reading["chest"], reading["source"]
            if self.repo.learn_chest_type(gift.chest_type, name, source):
                logger.info(f"New chest type {gift.chest_type}: '{name}' ({source}), named from the screen.")
            self.types[gift.chest_type] = (name, source)

        player = self.players.get(gift.player_id, "")
        if not player and reading.get("player"):
            player = reading["player"]
            self.players[gift.player_id] = player
            logger.info(f"Player {gift.player_id} is not in the site's member list; recorded as '{player}', "
                        f"read from the screen.")

        times = f" x{gift.quantity}" if gift.quantity > 1 else ""
        if name and source and player:
            rows = []
            for _ in range(gift.quantity):
                if self.repo.insert_chest(name, player, source, gift.generated_at, game) and self.repo.last_id:
                    rows.append(self.repo.last_id)
            if len(rows) == gift.quantity:
                self.written[gift.gift_id] = rows
                self.collected += gift.quantity
                logger.info(f"Chest recorded -> '{name}' from '{player}' ({source}){times}, "
                            f"generated at {gift.generated_at:%H:%M} [gift {gift.gift_id}]")
                if module.vocabulary:
                    module.vocabulary.learn(name, player, source)
                return
            # A row the database refused: what did go in is kept, and the rest is
            # filed below, so no chest of the gift goes unrecorded.
            self.written[gift.gift_id] = rows
            self.collected += len(rows)
            missing = gift.quantity - len(rows)
            logger.error(f"[{module.profile_label}] Database rejected {missing} chest(s) of gift {gift.gift_id}.")
        else:
            missing = gift.quantity

        logger.warning(
            f"[{module.profile_label}] Incomplete chest{times} -> name='{name}' player='{player}' source='{source}' "
            f"[gift {gift.gift_id}, type {gift.chest_type}, player {gift.player_id}]. Sent to review queue.")
        picture = module._encode(reading.get("crop"))
        for _ in range(missing):
            self.repo.insert_incomplete_chest(name, player, source, picture, gift.generated_at, game)
        self.filed.add(gift.gift_id)
        self.incomplete += missing

    # ------------------------------------------------------------- settling
    def _settle(self) -> Optional[int]:
        """
        Asks the game for the list again and makes the database agree with it.
        Returns how many chests are still waiting, or None when the list did not come.
        """
        before = {gift.gift_id: gift for gift in self.feed.snapshot() if gift.kind == PLAIN_GIFT}
        lists, _opened = self.feed.state()

        if not self.reopen():
            return None
        if not self._wait(lambda: self.feed.state()[0] > lists, self.answer_wait):
            return None

        left = {gift.gift_id: gift for gift in self.feed.snapshot() if gift.kind == PLAIN_GIFT}
        # Whether a gift is in the database is asked again here: this run wrote to it.
        self.in_database = (self.repo.chests_of_gifts(min(before), max(before)) if before else {})

        late = Counter()
        for gift_id, gift in sorted(before.items()):
            still = left.get(gift_id)
            if still is None:
                # Gone because it was opened - or because its twenty hours ran out.
                expired = gift.expires_at <= time.time()
                if not expired and not self.in_database.get(gift_id) and gift_id not in self.filed:
                    self._write(gift)
                    late[gift_id] = gift.quantity
            elif still.quantity >= gift.quantity and self.written.get(gift_id):
                removed = sum(1 for row in self.written.pop(gift_id) if self.repo.delete_chest(row))
                self.collected -= removed
                logger.info(f"Gift {gift_id} was written but is still waiting in the game: {removed} row(s) "
                            f"taken back, it will be collected when it is opened.")
        if late:
            logger.info(f"{sum(late.values())} chest(s) of {len(late)} gift(s) were opened without having been "
                        f"written first, and were written from the game's list now: {sorted(late)[:10]}")
        return sum(gift.quantity for gift in left.values())

    @staticmethod
    def _wait(done: Callable[[], bool], seconds: float) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if done():
                return True
            cancellation.sleep(0.05)
        return done()
