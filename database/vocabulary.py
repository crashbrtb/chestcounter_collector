"""
What the database already knows, used to read the screen better.

The clan is a closed world: a few hundred players, ninety-odd chest types, a
hundred sources - all of them already in `collected_chests`, `members` and
`player_name_mappings` after years of collecting. A reading does not have to be
guessed from pixels alone when it can be recognised against that list.

What recognition is allowed to do
---------------------------------
Only two things, and neither is a guess:

* apply a correction a person already made, from `player_name_mappings`;
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

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from utils.logger import logger
from utils.text_utils import alphanumeric_key

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

    @property
    def is_useful(self) -> bool:
        """A brand-new database knows nothing yet, and matching would only get in the way."""
        return bool(self.players.names)

    def resolve_player(self, reading: str) -> Tuple[Optional[str], str]:
        """
        (name, how it was decided) - 'mapping', 'known', or ('', 'unknown').

        The mapping table is consulted first and wins outright.
        """
        if reading in self.mappings:
            return self.mappings[reading], "mapping"
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
        return (f"{len(self.players.names)} jogadores "
                f"({len(self.players.authoritative)} da lista do clã), "
                f"{len(self.chests.names)} baús, "
                f"{len(self.sources.names)} origens "
                f"({len(self.sources.authoritative)} da tabela oficial), "
                f"{len(self.mappings)} correções manuais")


def load_vocabulary(connection) -> Vocabulary:
    """
    Builds the vocabulary from a profile's database.

    Counting rows costs a fraction of a second once per profile - measured at
    about 320 ms against 261 000 chests - and is repaid on every chest read
    afterwards.
    """
    players = KnownNames("jogadores")
    chests = KnownNames("baús")
    sources = KnownNames("origens")
    mappings: Dict[str, str] = {}

    cursor = connection.cursor()

    def consultar(sql: str):
        """A query that is allowed to fail: schemas differ between profiles."""
        try:
            cursor.execute(sql)
            return cursor.fetchall()
        except Exception as exc:
            logger.debug(f"Vocabulary query failed ({sql[:44]}...): {exc}")
            return []

    try:
        # --- reference tables first: these are the official spellings ---
        columns = {row[0] for row in consultar("SHOW COLUMNS FROM standard_chests")}
        if "source" in columns:
            # One profile's table has an `alias` column and the other's does not.
            has_alias = "alias" in columns
            select = "SELECT source, alias FROM standard_chests" if has_alias \
                else "SELECT source FROM standard_chests"
            for row in consultar(select):
                sources.add(row[0], authoritative=True)
                if has_alias and len(row) > 1 and row[1]:
                    sources.add_alias(row[1], row[0])

        for (name,) in consultar("SELECT player FROM members"):
            players.add(name, authoritative=True)
        for (name,) in consultar("SELECT DISTINCT correct_name FROM player_name_mappings"):
            players.add(name, authoritative=True)

        # --- then the history, for whatever the reference tables do not list ---
        for sql, target in (
            ("SELECT player, COUNT(*) FROM collected_chests GROUP BY player", players),
            ("SELECT name, COUNT(*) FROM collected_chests GROUP BY name", chests),
            ("SELECT source, COUNT(*) FROM collected_chests GROUP BY source", sources),
        ):
            for name, count in consultar(sql):
                target.add(name, int(count or 1))

        mappings = {raw: correct for raw, correct
                    in consultar("SELECT ocr_text, correct_name FROM player_name_mappings")
                    if raw and correct}
    finally:
        cursor.close()

    vocabulary = Vocabulary(players, chests, sources, mappings)
    logger.info(f"Vocabulário carregado do banco: {vocabulary.summary()}")
    return vocabulary
