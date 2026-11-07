"""
Collecting the clan's gifts over the network.

How a run goes
--------------
    1. the game is asked for the list of gifts;
    2. unknown player IDs are named by the game (route 402/12026) and unknown chest
       type IDs are named from the static chest catalog (local cache / CDN);
    3. each gift whose type has a name, oldest first, is written to the database
       and then opened, waiting for the game's answer before the next;
    4. the list is asked for again, and the database made to agree with it.

Writing comes before opening, as it always did: a chest written and not opened
can be put right, one opened and not written is gone. A gift is written whole -
an epic monster's five chests are five rows at once - and opened whole, so a gift
is in the database entirely or not at all, and one found there already (a run
stopped halfway) is opened without being written again.
"""

import time
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from utils.cancel import cancellation
from utils.logger import logger

from .catalog import lookup_chest_types
from .game_link import GameLink, GameLinkError, pace
from .gifts import KNOWN_KINDS, PLAIN_GIFT, TAB_NAMES, Gift

# Opening stops after this many refusals in a row: the session is gone or the game
# is not accepting them, and insisting would change neither.
MAX_FAILURES = 3
FAILURE_WAIT = 1.0

# How many times the list is walked. One pass is the normal case; the others pick
# up what arrived meanwhile.
MAX_PASSES = 4

# A gift with less than this to live is not left for the next run.
ABOUT_TO_EXPIRE = 60 * 60


class GameCollection:
    """The gifts of one clan, collected over the network. See the module's docstring."""

    def __init__(self, label: str, repo, link: GameLink, clan_id: int, interval: float,
                 lookup_types: Optional[Callable[[Iterable[int]], Dict[int, Tuple[str, str]]]] = None):
        self.label = label                # the profile, for the log
        self.repo = repo
        self.link = link
        self.clan_id = clan_id
        self.interval = interval          # the least time between the start of two requests
        self.lookup_types = lookup_types if lookup_types is not None else lookup_chest_types

        self.types: Dict[int, Tuple[str, str]] = repo.chest_types()
        self.players: Dict[int, str] = repo.game_players()
        self.written: Dict[int, List[int]] = {}     # gift id -> rows this run wrote for it
        self.collected = 0
        self.incomplete = 0
        self.warned_kinds = False
        self.set_aside: Set[int] = set()            # kinds the game would not open this run, left in the game
        self.unnamed: Set[int] = set()              # types not found in the catalog, left for the next run
        self._catalog_tried: Set[int] = set()       # types already asked of the static catalog this run
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
                self._set_aside_unopened(attempted, gifts)
                attempted = []
                waiting = self._waiting(gifts)
                self._name_players(gifts)
                self._name_types(waiting)

                ready = [gift for gift in waiting if self._ready(gift)]
                if ready and passes >= MAX_PASSES:
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
                for gift in nameless:
                    self.unnamed.add(gift.chest_type)
                    logger.warning(f"[{self.label}] Chest type {gift.chest_type} is not in the catalog "
                                   f"[gift {gift.gift_id}]; left in the game unless about to expire.")
        except GameLinkError as exc:
            logger.warning(f"[{self.label}] {exc}. What was written and not opened is opened on the next run.")
            self.problem = "the game stopped answering"
            return self.collected, self.incomplete, False

        logger.info(f"[{self.label}] {self.collected} collected, {self.incomplete} incomplete.")
        if self.set_aside:
            tabs = ", ".join(TAB_NAMES.get(kind, str(kind)) for kind in sorted(self.set_aside))
            self.problem = f"the game did not open the gifts of the {tabs} tab over the network; they were left in it"
            return self.collected, self.incomplete, False
        if self.unnamed:
            self.problem = f"chest types left unnamed in the game: {sorted(self.unnamed)}"
            return self.collected, self.incomplete, False
        return self.collected, self.incomplete, True

    def _ready(self, gift: Gift) -> bool:
        """Whether it can be written and opened now: its type is named, or it has no more time to wait."""
        return gift.chest_type in self.types or (gift.chest_type in self.unnamed
                                                 and gift.expires_at - time.time() < ABOUT_TO_EXPIRE)

    def _waiting(self, gifts: List[Gift]) -> List[Gift]:
        """What can be opened: an expired gift stays listed until someone deletes it, and opens nothing."""
        strange = [gift for gift in gifts if gift.kind not in KNOWN_KINDS]
        if strange and not self.warned_kinds:
            self.warned_kinds = True
            logger.warning(f"[{self.label}] {len(strange)} gift(s) of a kind not seen before were left in the "
                           f"game (e.g. {strange[0]}).")
        now = time.time()
        return [gift for gift in gifts
                if gift.kind in KNOWN_KINDS and gift.kind not in self.set_aside and gift.expires_at > now]

    def _set_aside(self, gift: Gift, why: str) -> None:
        """Leaves the gifts of a tab other than Gifts in the game for the rest of the run."""
        if gift.kind in self.set_aside:
            return
        self.set_aside.add(gift.kind)
        logger.warning(f"[{self.label}] The game {why} [gift {gift.gift_id}, tab {TAB_NAMES.get(gift.kind)}]: "
                       f"the gifts of that tab are left in the game for the next run.")

    def _set_aside_unopened(self, attempted: List[Gift], listed: List[Gift]) -> None:
        """A triumphal gift asked to be opened and still listed as it was: the game does not open them this way."""
        left = {gift.gift_id: gift for gift in listed}
        for gift in attempted:
            still = left.get(gift.gift_id)
            if gift.kind != PLAIN_GIFT and still is not None and still.quantity >= gift.quantity:
                self._set_aside(gift, "did not open a gift it was asked to")

    def _name_players(self, gifts: List[Gift]) -> None:
        unknown = sorted({gift.player_id for gift in gifts if gift.player_id not in self.players})
        if unknown:
            named = self.link.player_names(unknown)
            self.players.update(named)
            logger.info(f"[{self.label}] {len(named)} of {len(unknown)} player(s) the site's member list does not "
                        f"know were named by the game.")

    def _name_types(self, gifts: List[Gift]) -> None:
        """Resolves unknown chest types from the local/CDN static catalog and saves them to `game_chest_types`."""
        unknown = sorted({gift.chest_type for gift in gifts
                          if gift.chest_type not in self.types and gift.chest_type not in self._catalog_tried})
        if not unknown:
            return
        self._catalog_tried.update(unknown)
        named = self.lookup_types(unknown)
        if not named:
            return
        learn = getattr(self.repo, "learn_chest_type", None)
        for type_id, (name, source) in named.items():
            if callable(learn):
                learn(type_id, name, source)
            self.types[type_id] = (name, source)
        if callable(learn):
            self.types.update(self.repo.chest_types())
        logger.info(f"[{self.label}] {len(named)} of {len(unknown)} new chest type(s) were named from the game "
                    f"catalog and saved to game_chest_types.")

    # -------------------------------------------------------------- opening
    def _open(self, waiting: List[Gift]) -> Tuple[List[Gift], bool]:
        """Writes and opens each gift. Returns the gifts it tried, and False when it had to give up."""
        self.repo.keep_alive()
        in_database = self.repo.chests_of_gifts(waiting[0].gift_id, waiting[-1].gift_id)
        already = sum(1 for gift in waiting if in_database.get(gift.gift_id) and gift.gift_id not in self.written)
        if already:
            logger.info(f"[{self.label}] {already} gift(s) are in the database already, from a run that did not "
                        f"finish; they are opened without being written again.")

        attempted: List[Gift] = []
        failures = 0
        for gift in waiting:
            cancellation.check()
            if gift.kind in self.set_aside:
                continue
            if not in_database.get(gift.gift_id) and gift.gift_id not in self.written:
                self._write(gift)

            attempted.append(gift)
            started_at = time.time()
            try:
                self.link.open_gift(gift)
                failures = 0
            except GameLinkError as exc:
                if gift.kind != PLAIN_GIFT:
                    # Taken back by the settling if it is still there; not counted against the Gifts tab.
                    self._set_aside(gift, f"answered '{exc}' to opening a gift")
                    continue
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
            self.repo.insert_incomplete_chest(name, player, source, gift.generated_at, game)
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
