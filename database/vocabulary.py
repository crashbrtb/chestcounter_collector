"""
What the database already knows, used to read the screen better.

The clan is a closed world: a few hundred players, ninety-odd chest types, a
hundred sources - all of them already in `collected_chests`, `members` and
`player_name_mappings` after years of collecting. A reading does not have to be
guessed from pixels alone when it can be recognised against that list.

What recognition is allowed to do
---------------------------------
Only three things, and none is a guess:

* apply a correction a person already made, from `player_name_mappings`;
* place a player's name on the one clan member it can be, when the game's own
  member list is in the database and recent (see `Roster`) - and leave it as
  read when the list says it could be two;
* settle a difference of formatting - spacing, punctuation, case - against a
  spelling already known: `AncientWarrior'sChest` is `Ancient Warrior's Chest`.

It may not decide that one name is another because they look alike. That was
tried and taken out: at a 0.88 similarity this database merges `Common Chest of
Wealth` into `Uncommon Chest of Wealth`, and `DaNyx Darkher` into `Nyx Darkher`.

The two mistakes are not symmetric. A name recorded wrong is visible in the
reports, and the web interface can merge the two players afterwards - which also
writes the correction into `player_name_mappings`, so it stops happening. Two
players merged by the collector leave nothing to see and nothing to undo.

So an unfamiliar name is recorded exactly as it was read.

What recognition is still for
-----------------------------
Speed: a chest whose three fields are all already known needs no second opinion,
so the slower engine is spent only on something genuinely unfamiliar.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from utils.logger import logger
from utils.text_utils import alphanumeric_key, ocr_key

# How old the clan's member list may be and still decide whose chest it is. The
# members module reads it once a day; two hours over that covers a run that
# started late. Past it, someone may have joined under a name the list has not
# seen, and the readings are matched the way they were before the list existed.
ROSTER_MAX_AGE_HOURS = 26

# The job under which the site remembers each member list it applied.
MEMBER_LIST_JOB = "member_list"

# Below this many characters a key says too little to name anyone.
MIN_ROSTER_KEY = 3


@dataclass
class Roster:
    """
    The clan as the game lists it, used to place a reading on one of its members.

    A clan chest can only come from someone in the clan, so the list of active
    members is everyone a reading can be. That turns "is this name close enough?"
    into a question with an answer: a reading whose key (`ocr_key`) belongs to
    exactly one member is that member, whatever the screen did to the spelling -
    `HeStla BR` is `HeStIa BR`, `DRIKA BR` is the member whose name ends in a
    letter no engine writes.

    The same list says when that would be a guess. Two members whose names share
    a key (`Alok BR` and `ALOK BR` are two people) cannot be told apart by it:
    a reading spelt exactly like one of them is that one, and any other reading
    with their key is left for a person - recorded as it was read, never handed
    to one of the two.
    """

    names: List[str] = field(default_factory=list)
    age_hours: Optional[float] = None
    _exact: set = field(default_factory=set)
    _by_key: Dict[str, List[str]] = field(default_factory=dict)

    def __post_init__(self):
        self._exact = set(self.names)
        self._by_key = {}
        for name in self.names:
            key = ocr_key(name)
            if len(key) >= MIN_ROSTER_KEY and name not in self._by_key.setdefault(key, []):
                self._by_key[key].append(name)

    @property
    def twins(self) -> List[List[str]]:
        """Members a reading cannot tell apart, group by group."""
        return [names for names in self._by_key.values() if len(names) > 1]

    def resolve(self, reading: str) -> Tuple[Optional[str], str]:
        """
        (member, 'member') when the reading is one member and no other;
        (None, 'ambiguous') when it could be more than one; (None, '') when the
        list has nobody like it.
        """
        reading = (reading or "").strip()
        if reading in self._exact:
            return reading, "member"
        candidates = self._by_key.get(ocr_key(reading), [])
        if len(candidates) == 1:
            return candidates[0], "member"
        return (None, "ambiguous") if candidates else (None, "")


@dataclass
class KnownNames:
    """
    One closed vocabulary - players, chest names or sources.

    Spellings arrive from two kinds of place, and they do not carry the same
    weight:

    * a REFERENCE table - `standard_chests` for sources, `members` for players.
      These are maintained deliberately and hold the spelling the rest of the
      system expects. `standard_chests` is what the scoring joins on, so a
      source spelt any other way scores nothing, however often it was collected.
    * HISTORY - what was collected before. It covers everything the reference
      table does not list (18 of the sources in production, 'Dark Omens event'
      among them, with thousands of chests behind them), and ties are settled by
      frequency, since the same text can sit in the history spelt two ways.

    Reference wins over history, no matter how many rows history has.
    """

    label: str
    #: spelling -> how many times the history used it
    counts: Dict[str, int] = field(default_factory=dict)
    #: spellings that came from a reference table
    reference: set = field(default_factory=set)
    #: loose key of an alias -> the official name it stands for
    aliases: Dict[str, str] = field(default_factory=dict)
    _canonical: Optional[Dict[str, str]] = None

    def add(self, name: str, count: int = 0, authoritative: bool = False):
        """Records a spelling and, for history, how often it was used."""
        name = (name or "").strip()
        if not name or not any(c.isalnum() for c in name):
            return

        already_canonical = (self._canonical is not None
                             and self._canonical.get(alphanumeric_key(name)) == name)
        self.counts[name] = self.counts.get(name, 0) + count
        if authoritative:
            self.reference.add(name)

        # Counting a spelling that already won its key cannot change the answer,
        # and this is called for every chest collected - rebuilding the whole map
        # each time would be work with no effect.
        if not (already_canonical and not authoritative):
            self._canonical = None

    def add_alias(self, alias: str, canonical: str):
        """
        Another name for something already known, resolving to the official one.

        `standard_chests.alias` carries these: what the game may show as
        'Doomsday' is recorded as 'Epic Undead squad'.
        """
        alias, canonical = (alias or "").strip(), (canonical or "").strip()
        key = alphanumeric_key(alias) if alias else ""
        if key and canonical:
            self.aliases[key] = canonical
            self._canonical = None

    def _build(self):
        """
        Picks one spelling per key: reference first, then the most used.

        Deciding at the end rather than as each row arrives is what makes the
        answer independent of load order - and it has to be, because a reference
        table can hold the same thing twice. `standard_chests` really does list
        both `Hermes' Store` and `Hermes’ Store`, differing only in the
        apostrophe; the history settles it, having used the straight one 746
        times against 88.
        """
        best: Dict[str, tuple] = {}
        for name, count in self.counts.items():
            key = alphanumeric_key(name)
            if not key:
                continue
            rank = (1 if name in self.reference else 0, count)
            if key not in best or rank > best[key][0]:
                best[key] = (rank, name)

        self._canonical = {key: name for key, (_rank, name) in best.items()}
        # An alias is not a spelling of its own - it points at the official name.
        self._canonical.update(self.aliases)

    @property
    def canonical(self) -> Dict[str, str]:
        if self._canonical is None:
            self._build()
        return self._canonical

    @property
    def authoritative(self) -> set:
        """The spellings in use that came from a reference table."""
        return {name for name in self.canonical.values() if name in self.reference}

    @property
    def names(self) -> List[str]:
        return list(dict.fromkeys(self.canonical.values()))

    def resolve(self, reading: str) -> Optional[str]:
        """
        The known name this reading is - by its letters and digits, never by resemblance.

        Two spellings are the same name here only when they differ in spacing,
        punctuation or case: `AncientWarrior'sChest` is `Ancient Warrior's
        Chest`, `|IMPERATOR` is `IMPERATOR`. That is a formatting difference,
        not a judgement about who someone is.

        Anything looser was tried and removed. Matching by similarity at 0.88
        merges things that are genuinely different: in this very database,
        `Common Chest of Wealth` scores 0.957 against `Uncommon Chest of
        Wealth`, `DaNyx Darkher` 0.917 against `Nyx Darkher`, `Pandeménia`
        0.900 against `Pandeménio`. And the two mistakes are not equal - a name
        recorded wrong is visible and can be merged afterwards from the web
        interface, which records the correction in `player_name_mappings` so it
        never happens again; two players silently merged leave nothing to
        notice and nothing to undo.

        So an unfamiliar reading returns None and is recorded as it was read.
        """
        reading = (reading or "").strip()
        if not reading:
            return None
        key = alphanumeric_key(reading)
        return self.canonical.get(key) if key else None


@dataclass
class Vocabulary:
    """The three closed sets a chest is made of, plus the manual corrections."""

    players: KnownNames
    chests: KnownNames
    sources: KnownNames
    #: exact OCR text -> name a person chose for it
    mappings: Dict[str, str] = field(default_factory=dict)
    #: the clan's member list, when the database has one read recently enough
    roster: Optional[Roster] = None

    @property
    def is_useful(self) -> bool:
        """A brand-new database knows nothing yet, and matching would only get in the way."""
        return bool(self.players.names)

    def resolve_player(self, reading: str) -> Tuple[Optional[str], str]:
        """
        (name, how it was decided) - 'mapping', 'member', 'known', or
        (None, 'ambiguous' | 'unknown').

        The mapping table is consulted first and wins outright. Then the clan's
        member list, when there is a recent one: it places the reading on one
        member, or says it could be more than one - and then nothing older is
        asked, because the history would pick one of them by habit. Whoever the
        list does not know is looked for the old way.
        """
        if reading in self.mappings:
            return self.mappings[reading], "mapping"
        if self.roster is not None:
            member, how = self.roster.resolve(reading)
            if member or how == "ambiguous":
                return member, how
        found = self.players.resolve(reading)
        return (found, "known") if found else (None, "unknown")

    def learn(self, chest: str, player: str, source: str):
        """
        Remembers what this run has just accepted.

        A new clan member is unknown only once. Without this, every one of the
        fifty chests they send during an event is a fresh surprise, and each one
        pays 460 ms for the careful engine to read a name the run has already
        settled. The vocabulary is a snapshot taken at the start; this keeps it
        current within the run without going back to the database.
        """
        self.chests.add(chest, count=1)
        self.players.add(player, count=1)
        self.sources.add(source, count=1)

    def summary(self) -> str:
        return (f"{len(self.players.names)} players "
                f"({len(self.players.authoritative)} clan members), "
                f"{len(self.chests.names)} chests, "
                f"{len(self.sources.names)} sources "
                f"({len(self.sources.authoritative)} authoritative), "
                f"{len(self.mappings)} manual mappings")


def load_roster(query_rows) -> Optional[Roster]:
    """
    The clan's active members, if the site applied a member list recently enough.

    The site notes each list it applies as a `member_list` run, with the moment
    the list was read from the game. Its age is measured against the database's
    own clock, as the heartbeat is (database/job_runs.py): this computer's clock
    and timezone are not the site's.
    """
    rows = query_rows(
        f"SELECT summary, UTC_TIMESTAMP() FROM job_runs WHERE job = '{MEMBER_LIST_JOB}' "
        f"AND status = 'success' ORDER BY finished_at DESC, id DESC LIMIT 1"
    )
    if not rows:
        logger.info("No member list from the game in this database yet: player names are matched "
                    "against what was collected before.")
        return None

    summary, now = rows[0][0], rows[0][1]
    try:
        if isinstance(summary, (bytes, bytearray)):
            summary = summary.decode("utf-8")
        if isinstance(summary, str):
            summary = json.loads(summary)
        read_at = datetime.strptime(str(summary["read_at"]), "%Y-%m-%dT%H:%M:%SZ")
        age_hours = (now - read_at).total_seconds() / 3600
    except (KeyError, TypeError, ValueError) as exc:
        logger.debug(f"Member list run could not be read: {exc}")
        return None

    if age_hours > ROSTER_MAX_AGE_HOURS:
        logger.warning(f"The clan's member list was read {age_hours:.0f} h ago, more than the "
                       f"{ROSTER_MAX_AGE_HOURS} h it is trusted for: player names are matched against what "
                       f"was collected before, until the members module reads the list again.")
        return None

    names = [str(name).strip() for (name,) in query_rows("SELECT player FROM members WHERE active = 1") if name]
    if not names:
        return None

    roster = Roster(names, age_hours)
    logger.info(f"Member list read {max(age_hours, 0):.1f} h ago: readings are placed on its "
                f"{len(names)} active members.")
    # Said here so a chest recorded as read can be explained from the log; telling
    # the clan about players who share a name is the site's job.
    for twins in roster.twins:
        logger.info(f"Members a reading cannot tell apart: {' / '.join(twins)}. A chest is given to one of "
                    f"them only when the name is read exactly as theirs; otherwise it is recorded as read.")
    return roster


def load_vocabulary(connection) -> Vocabulary:
    """
    Builds the vocabulary from a profile's database.

    Counting rows costs a fraction of a second once per profile - measured at
    about 320 ms against 261 000 chests - and is repaid on every chest read
    afterwards.
    """
    players = KnownNames("players")
    chests = KnownNames("chests")
    sources = KnownNames("sources")
    mappings: Dict[str, str] = {}

    cursor = connection.cursor()

    def query_rows(sql: str):
        """A query that is allowed to fail: schemas differ between profiles."""
        try:
            cursor.execute(sql)
            return cursor.fetchall()
        except Exception as exc:
            logger.debug(f"Vocabulary query failed ({sql[:44]}...): {exc}")
            return []

    try:
        # --- reference tables first: these are the official spellings ---
        columns = {row[0] for row in query_rows("SHOW COLUMNS FROM standard_chests")}
        if "source" in columns:
            # One profile's table has an `alias` column and the other's does not.
            has_alias = "alias" in columns
            select = "SELECT source, alias FROM standard_chests" if has_alias \
                else "SELECT source FROM standard_chests"
            for row in query_rows(select):
                sources.add(row[0], authoritative=True)
                if has_alias and len(row) > 1 and row[1]:
                    sources.add_alias(row[1], row[0])

        for (name,) in query_rows("SELECT player FROM members"):
            players.add(name, authoritative=True)
        for (name,) in query_rows("SELECT DISTINCT correct_name FROM player_name_mappings"):
            players.add(name, authoritative=True)

        # --- then the history, for whatever the reference tables do not list ---
        for sql, target in (
            ("SELECT player, COUNT(*) FROM collected_chests GROUP BY player", players),
            ("SELECT name, COUNT(*) FROM collected_chests GROUP BY name", chests),
            ("SELECT source, COUNT(*) FROM collected_chests GROUP BY source", sources),
        ):
            for name, count in query_rows(sql):
                target.add(name, int(count or 1))

        mappings = {raw: correct for raw, correct
                    in query_rows("SELECT ocr_text, correct_name FROM player_name_mappings")
                    if raw and correct}

        # --- and the clan as the game lists it, when that is known and recent ---
        roster = load_roster(query_rows)
    finally:
        cursor.close()

    vocabulary = Vocabulary(players, chests, sources, mappings, roster)
    logger.info(f"Vocabulary loaded from database: {vocabulary.summary()}")
    return vocabulary
