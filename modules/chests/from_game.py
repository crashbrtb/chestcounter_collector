"""
Collecting the clan's gifts: over the network, with the screen only for a picture
of a chest type nothing knows yet.

How a run goes
--------------
    1. the game is asked for the list of gifts;
    2. each gift whose type has a name, oldest first, is written to the database
       and then opened, waiting for the game's answer before the next;
    3. the list is asked for again, and the database made to agree with it;
    4. while something is left, one type without a name is pictured on the panel,
       and it goes back to 2 with its chests bound for the site's review queue.

Writing comes before opening, as it always did: a chest written and not opened
can be put right, one opened and not written is gone. A gift is written whole -
an epic monster's five chests are five rows at once - and opened whole, so a gift
is in the database entirely or not at all, and one found there already (a run
stopped halfway) is opened without being written again.

Settling
--------
A request that failed may have reached the game or not, so the answer alone does
not say what happened to the gift. The next list does:

    * a gift that is gone was opened, and is in the database already;
    * a gift still there, untouched, was not opened: what this run wrote for it
      is taken back, and it waits for the next pass or the next run.

Names
-----
The game sends a chest's type and its player as numbers. A player the site's
member list does not know is asked of the game, which knows every name. A type is
named by `game_chest_types` or by the chests already written with it (see
repositories.chest_types). A type neither knows is for a person to name.

A type named wrong stays wrong - type 5784 was read off the line of another player's
chest on 12/10/2026, and named hundreds of chests after it - so nothing here reads
a name. The chests of a new type are opened and filed in the review queue, the first
with a picture of it and all with their type, player and date. Whoever
reviews one on the site names the type, for those chests and for the ones to come.
While a picture of the type is waiting there, no other is taken.

A type that cannot be pictured is left in the game for the next run, unless its gift
is about to expire: then it goes to the review queue without one, rather than be lost.
"""

import time
from typing import Callable, Dict, List, Optional, Set, Tuple

from utils.cancel import Cancelled, cancellation
from utils.logger import logger

from .game_link import GameLink, GameLinkError, pace
from .gifts import PLAIN_GIFT, Gift

# Opening stops after this many refusals in a row: the session is gone or the game
# is not accepting them, and insisting would change neither.
MAX_FAILURES = 3
FAILURE_WAIT = 1.0

# How many times the list is walked. One pass is the normal case; the others pick
# up what arrived meanwhile.
MAX_PASSES = 4

# How many times the panel is looked at before a type is left without a picture.
PICTURE_ATTEMPTS = 2

# A gift with less than this to live is not left for the next run.
ABOUT_TO_EXPIRE = 60 * 60


class GameCollection:
    """The gifts of one clan, collected over the network. See the module's docstring."""

    def __init__(self, label: str, repo, link: GameLink, clan_id: int, interval: float,
                 take_picture: Callable[[int], Optional[bytes]]):
        self.label = label                # the profile, for the log
        self.repo = repo
        self.link = link
        self.clan_id = clan_id
        self.interval = interval          # the least time between the start of two requests
        self.take_picture = take_picture  # the panel, with the chest at a place of the list outlined

        self.types: Dict[int, Tuple[str, str]] = repo.chest_types()
        self.players: Dict[int, str] = repo.game_players()
        self.written: Dict[int, List[int]] = {}     # gift id -> rows this run wrote for it
        self.collected = 0
        self.incomplete = 0
        self.warned_kinds = False
        self.pictures: Dict[int, bytes] = {}        # type -> the picture its next chest is filed with
        self.in_review: Set[int] = repo.types_in_review()   # types whose picture is, or will be, in the queue
        self.unnamed: Set[int] = set()              # types that could not be pictured, left for the next run
        self.problem: Optional[str] = None          # why the run did not finish, for the run's record

    # ------------------------------------------------------------------ run
    def run(self) -> Tuple[int, int, bool]:
        """(collected, incomplete, finished). Not finished means something was left that should not have been."""
        logger.info(f"[{self.label}] Collecting the gifts over the network ({len(self.types)} chest types "
                    f"and {len(self.players)} players known)...")
        attempted: List[Gift] = []
        passes = 0
        try:
            while True:
                gifts = self.link.gifts(self.clan_id)
                self._settle(attempted, gifts)
                attempted = []
                waiting = self._waiting(gifts)
                self._name_players(gifts)

                ready = [gift for gift in waiting if self._ready(gift)]
                # A type just pictured is opened whatever the count: its picture goes with its first chest.
                if ready and passes >= MAX_PASSES and not any(gift.chest_type in self.pictures for gift in ready):
                    logger.info(f"[{self.label}] {sum(g.quantity for g in ready)} chest(s) arrived after the last "
                                f"pass; they are left for the next run.")
                    break
                if ready:
                    passes += 1
                    attempted, finished = self._open(ready)
                    if not finished:
                        self._settle(attempted, self.link.gifts(self.clan_id))
                        self.problem = "the game stopped answering"
                        return self.collected, self.incomplete, False
                    continue

                nameless = [gift for gift in waiting if gift.chest_type not in self.unnamed]
                if not nameless:
                    break
                self._picture_type(nameless[0], gifts)
        except GameLinkError as exc:
            logger.warning(f"[{self.label}] {exc}. What was written and not opened is opened on the next run.")
            self.problem = "the game stopped answering"
            return self.collected, self.incomplete, False

        logger.info(f"[{self.label}] {self.collected} collected, {self.incomplete} incomplete.")
        if self.unnamed:
            self.problem = f"chest types left unnamed in the game: {sorted(self.unnamed)}"
            return self.collected, self.incomplete, False
        return self.collected, self.incomplete, True

    def _ready(self, gift: Gift) -> bool:
        """Whether it can be written and opened now: its type is named or in review, or it has no more time to wait."""
        return gift.chest_type in self.types or gift.chest_type in self.in_review or (gift.chest_type in self.unnamed
                                                                                      and gift.expires_at - time.time() < ABOUT_TO_EXPIRE)

    def _waiting(self, gifts: List[Gift]) -> List[Gift]:
        """What can be opened: an expired gift stays listed until someone deletes it, and opens nothing."""
        strange = [gift for gift in gifts if gift.kind != PLAIN_GIFT]
        if strange and not self.warned_kinds:
            self.warned_kinds = True
            logger.warning(f"[{self.label}] {len(strange)} gift(s) of a kind not seen before were left in the "
                           f"game (e.g. {strange[0]}).")
        now = time.time()
        return [gift for gift in gifts if gift.kind == PLAIN_GIFT and gift.expires_at > now]

    def _name_players(self, gifts: List[Gift]) -> None:
        unknown = sorted({gift.player_id for gift in gifts if gift.player_id not in self.players})
        if unknown:
            named = self.link.player_names(unknown)
            self.players.update(named)
            logger.info(f"[{self.label}] {len(named)} of {len(unknown)} player(s) the site's member list does not "
                        f"know were named by the game.")

    # --------------------------------------------------------------- naming
    def _picture_type(self, gift: Gift, listed: List[Gift]) -> None:
        """Pictures a new type on the panel for the review queue, or sets it aside when it cannot."""
        type_id = gift.chest_type
        # The panel's lines: one for each chest, in the order of the gifts.
        line = [item for item in listed if item.kind == PLAIN_GIFT for _ in range(item.quantity)]
        position = line.index(gift)

        for _ in range(PICTURE_ATTEMPTS):
            cancellation.check()
            try:
                picture = self.take_picture(position)
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - the screen failing must not take the collection with it
                logger.warning(f"[{self.label}] The gifts panel could not be pictured: {exc}")
                picture = None
            if picture:
                self.pictures[type_id] = picture
                self.in_review.add(type_id)
                logger.warning(f"[{self.label}] New chest type {type_id}: its chests go to the review queue on the "
                               f"site, with a picture, for someone to name it.")
                return

        self.unnamed.add(type_id)
        logger.warning(f"[{self.label}] Chest type {type_id} could not be pictured [gift {gift.gift_id}]; "
                       f"its chests are left in the game for the next run, unless they are about to expire.")

    # -------------------------------------------------------------- opening
    def _open(self, waiting: List[Gift]) -> Tuple[List[Gift], bool]:
        """Writes and opens each gift. Returns the gifts it tried, and False when it had to give up."""
        self.repo.keep_alive()   # a picture may have kept the database idle long enough for the server to drop it
        in_database = self.repo.chests_of_gifts(waiting[0].gift_id, waiting[-1].gift_id)
        already = sum(1 for gift in waiting if in_database.get(gift.gift_id) and gift.gift_id not in self.written)
        if already:
            logger.info(f"[{self.label}] {already} gift(s) are in the database already, from a run that did not "
                        f"finish; they are opened without being written again.")

        attempted: List[Gift] = []
        failures = 0
        for gift in waiting:
            cancellation.check()
            if not in_database.get(gift.gift_id) and gift.gift_id not in self.written:
                self._write(gift)

            attempted.append(gift)
            started_at = time.time()
            try:
                self.link.open_gift(gift)
                failures = 0
            except GameLinkError as exc:
                failures += 1
                logger.warning(f"[{self.label}] Gift {gift.gift_id} was not opened: {exc} "
                               f"({failures}/{MAX_FAILURES}).")
                if failures >= MAX_FAILURES:
                    return attempted, False
                cancellation.sleep(FAILURE_WAIT)
            cancellation.sleep(pace(started_at, self.interval))
        return attempted, True

    # -------------------------------------------------------------- writing
    def _write(self, gift: Gift) -> None:
        """Writes every chest of a gift, or files what is known of it when something has no name."""
        game = {"game_gift_id": gift.gift_id, "game_type_id": gift.chest_type, "game_player_id": gift.player_id}
        name, source = self.types.get(gift.chest_type, ("", ""))
        player = self.players.get(gift.player_id, "")
        times = f" x{gift.quantity}" if gift.quantity > 1 else ""

        rows: List[int] = []
        if name and source and player:
            for _ in range(gift.quantity):
                if self.repo.insert_chest(name, player, source, gift.generated_at, game) and self.repo.last_id:
                    rows.append(self.repo.last_id)
            self.written[gift.gift_id] = rows
            self.collected += len(rows)
            if len(rows) == gift.quantity:
                logger.info(f"Chest recorded -> '{name}' from '{player}' ({source}){times}, "
                            f"generated at {gift.generated_at:%H:%M} [gift {gift.gift_id}]")
                return
            logger.error(f"[{self.label}] Database rejected {gift.quantity - len(rows)} chest(s) of gift "
                         f"{gift.gift_id}.")
        else:
            self.written[gift.gift_id] = rows
            logger.warning(f"[{self.label}] Incomplete chest{times} -> name='{name}' player='{player}' "
                           f"source='{source}' [gift {gift.gift_id}, type {gift.chest_type}, "
                           f"player {gift.player_id}]. Sent to review queue.")

        missing = gift.quantity - len(rows)
        for _ in range(missing):
            self.repo.insert_incomplete_chest(name, player, source, self.pictures.pop(gift.chest_type, None),
                                              gift.generated_at, game)
        self.incomplete += missing

    # ------------------------------------------------------------- settling
    def _settle(self, attempted: List[Gift], listed: List[Gift]) -> None:
        """Takes back what this run wrote for a gift the game says is still waiting, untouched."""
        left = {gift.gift_id: gift for gift in listed}
        for gift in attempted:
            still = left.get(gift.gift_id)
            if still is None or still.quantity < gift.quantity:
                continue
            rows = self.written.pop(gift.gift_id, [])
            removed = sum(1 for row in rows if self.repo.delete_chest(row))
            self.collected -= removed
            if removed:
                logger.info(f"Gift {gift.gift_id} was written but is still waiting in the game: {removed} row(s) "
                            f"taken back, it will be collected when it is opened.")
