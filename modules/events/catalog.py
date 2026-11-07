"""
The game's own catalogue of tournaments (Tables 44 and 71 of the client static data).

Why this exists
---------------
In the Journal the game never sends a tournament's name as text: each entry carries
only `[game_type, variant]` (e.g. `[1032, 1]`) and the kind of ranking packet
(e.g. `multistage_tournament_clan_statistics_entry`).

The game client turns those numbers and packet kinds into titles locally from two
sets of static files hosted on its CDN (and cached by the Desktop client):

    1. Client static data (`_strings`, `_no_duplicates`), encoded in the game's
       little-endian MessagePack format (`tbcodec`):
       - Table 44 (`global_tournament`) maps every `[game_type, variant]` to its
         internal slug (`row[0]`) and client UI JSON (`row[10]`), which holds
         `info_title` and `journal_users_result_titles`.
       - Table 71 (`global_tournament_components`) maps `[game_type, variant]` to
         its active feature components (`MultistageJrnTournamentResultsSender`,
         `ClanPointsMining`, `ClanMembersItemGainTracking`, `TriumphUnion`, etc.)
         and their tracker configurations (`row[6]`).
    2. The GNU gettext `.mo` translation file for `en_US`, which turns the Journal
       clanmates entry keys (`*.journal.entry_item.*`) and `info_title` keys into
       the English tournament names stored in `game_tournaments`.

Consulting the CDN as little as possible
----------------------------------------
    1. Known tournament types come straight from `GET /api/v1/tournaments/known`
       on the clan's site; while a search sees only types already named on the
       site, nothing in this module is called.
    2. When a Journal card or site catalogue entry has no name yet, the decoded
       catalogue cached on disk (`config/module_data/events/tournament_types.json`)
       is checked first. If the `(game_type, ranking)` is in that local file, it
       is named at once without touching the network.
    3. Only when a `(game_type, ranking)` is missing from both the site and the
       local disk cache does this module ask `https://versions.totalbattle.com/config`
       whether the CDN carries a newer static build, downloading it and updating
       the local cache file only when the build hash has changed.
"""
from __future__ import annotations

import ast
import gettext
import io
import json
import os
import pathlib
import re
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

from config.settings import MODULE_DATA_DIR
from modules.events import journal, tbcodec
from utils.logger import logger

VERSIONS_URL = "https://versions.totalbattle.com/config"
STATIC_CDN_URL = "https://assets-client-static.totalbattle.dev"
TRANSLATIONS_CDN_URL = "https://assets-translations.totalbattle.dev"

TOURNAMENT_TABLE_INDEX = 44
COMPONENT_TABLE_INDEX = 71
LOCALE = "en_US"
HTTP_TIMEOUT = 30
MAX_NAME_LEN = 120

DATA_DIR = os.path.join(MODULE_DATA_DIR, "events")
CACHE_PATH = os.path.join(DATA_DIR, "tournament_types.json")

_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Encoding": "gzip",
}

# Matches "Your Clanmates' results in the Rise of the Ancients tournament."
# while keeping compound descriptions like
# "Your Clanmates' contributions of Omen Essence in the Dark Omens tournament."
_CLANMATES_RESULTS_IN_THE = re.compile(
    r"^your\s+clanmates'\s+(?:results?|scores?)\s+in\s+the\s+(.+?)\.?$",
    re.IGNORECASE,
)
_CLANMATES_ANY = re.compile(
    r"^your\s+clanmates'\s+(.+?)\.?$",
    re.IGNORECASE,
)
_THE_TOURNAMENT_IS_OVER = re.compile(
    r'^the\s+"?(.+?)"?\s+(?:event\s+tournament|tournament|event|competition)?\s*is\s+over\b',
    re.IGNORECASE,
)
_SMALL_WORDS = {"of", "the", "in", "on", "at", "to", "for", "and", "or", "with"}


def normalize_ranking(ranking: Optional[str]) -> str:
    """Brings a ranking identifier to the canonical form used by `game_tournaments`."""
    if not ranking:
        return ""
    cleaned = ranking.strip().lower()
    if cleaned in ("", "global_tournament_user_result"):
        return ""
    return cleaned


def ranking_key(game_type: int, ranking: Optional[str] = "") -> str:
    return f"{int(game_type)}|{normalize_ranking(ranking)}"


def clean_clanmates_title(text: str) -> str:
    """
    Extracts the tournament or ranking name from a Journal clanmates entry title:
      - "Your Clanmates' results in the Great Hunt tournament." -> "Great Hunt tournament"
      - "Your Clanmates' contributions of Omen Essence in the Dark Omens tournament."
        -> "contributions of Omen Essence in the Dark Omens tournament"
      - "Your clanmates' score of Remnants of Dread in battles with Dark Omens."
        -> "score of Remnants of Dread in battles with Dark Omens"
    """
    if not text:
        return ""
    collapsed = " ".join(text.strip().split())
    for pat in (_CLANMATES_RESULTS_IN_THE, _CLANMATES_ANY):
        match = pat.match(collapsed)
        if match:
            return match.group(1).strip(" .")[:MAX_NAME_LEN]
    return ""


def _format_info_title(title: str) -> str:
    """Normalizes a raw `info_title` (e.g. `'TRIALS OF OLYMPUS'`, `'Christmas Tree!'`)."""
    cleaned = " ".join(title.strip().rstrip("!.").split())
    if not cleaned:
        return ""
    if cleaned.isupper() and len(cleaned) > 3:
        words = cleaned.lower().split()
        cleaned = " ".join(
            w if (i > 0 and w in _SMALL_WORDS) else w.capitalize()
            for i, w in enumerate(words)
        )
    return cleaned[:MAX_NAME_LEN]


def _unpack_tournament_tables(nodup_raw: bytes) -> Tuple[dict, dict]:
    """Unpacks only up to index 71 of `_no_duplicates`, returning `(table_44, table_71)`."""
    data = tbcodec._decompress(nodup_raw)
    unpacker = tbcodec.LEUnpacker(data, 0)
    b = unpacker.b[unpacker.p]
    unpacker.p += 1
    if 0x90 <= b <= 0x9F:
        count = b & 0x0F
    elif b == 0xDC:
        count = unpacker._u("<H", 2)
    elif b == 0xDD:
        count = unpacker._u("<I", 4)
    else:
        raise tbcodec.TBDecodeError(f"unexpected root byte 0x{b:02x} in _no_duplicates")
    if count <= COMPONENT_TABLE_INDEX:
        raise tbcodec.TBDecodeError(f"_no_duplicates has only {count} tables")

    table44 = None
    table71 = None
    for idx in range(COMPONENT_TABLE_INDEX + 1):
        tbl = unpacker.read()
        if idx == TOURNAMENT_TABLE_INDEX:
            table44 = tbl
        elif idx == COMPONENT_TABLE_INDEX:
            table71 = tbl
    if not isinstance(table44, dict) or not isinstance(table71, dict):
        raise tbcodec.TBDecodeError("Tournament tables in _no_duplicates are not maps")
    return table44, table71


def _translate(translations: Dict[str, str], key: str) -> str:
    """Looks up a non-empty translation key in `.mo` without returning the empty-key header."""
    if not key:
        return ""
    val = translations.get(key, "")
    return val if isinstance(val, str) else ""


def _family_clan_result_name(family: str, entry_item_clanmates: Dict[str, str]) -> str:
    if not family:
        return ""
    for candidate in (
        f"client.global_tournament.value.journal.entry_item.{family}_clan_result.title_2",
        f"client.global_tournament.value.journal.entry_item.{family}_clan_result.title",
        f"global_tournament.client.value.journal.entry_item.{family}_clan_result.title_2",
        f"global_tournament.client.value.journal.entry_item.{family}_clan_result.title",
        f"client.global_tournament.value.journal.entry_item.{family}_tournament_user_result.title",
        f"global_tournament.client.value.journal.entry_item.{family}_tournament_user_result.title",
    ):
        if candidate in entry_item_clanmates:
            return entry_item_clanmates[candidate]
    return ""


def _family_tracker_result_name(
    family: str,
    tracker_id: str,
    entry_item_clanmates: Dict[str, str],
) -> str:
    """Matches a component tracker (such as `omens_damage` in family `sabotage`) to its `.mo` key."""
    if not family:
        return ""
    prefix_1 = f"client.global_tournament.value.journal.entry_item.{family}_"
    prefix_2 = f"global_tournament.client.value.journal.entry_item.{family}_"
    tokens = [tok for tok in tracker_id.lower().split("_") if tok]
    best_key = ""
    best_score = -1
    for k, extracted in entry_item_clanmates.items():
        if not (k.startswith(prefix_1) or k.startswith(prefix_2)):
            continue
        if "_clan_result." in k:
            continue
        score = sum(1 for tok in tokens if tok in k.lower() or tok.rstrip("s") in k.lower())
        if score > best_score:
            best_score = score
            best_key = extracted
    return best_key


def decode_tournament_tables(
    strings_raw: bytes,
    nodup_raw: bytes,
    mo_raw: bytes,
) -> Tuple[Dict[str, str], Dict[int, str]]:
    """
    Extracts `(rankings_map, types_map)` from raw static and `.mo` bytes:
      - `rankings_map`: `{"<game_type>|<ranking>": "<tournament_name>"}`
      - `types_map`: `{game_type: "<default_tournament_name>"}`
    """
    mo_bytes = tbcodec._decompress(mo_raw)
    translations = gettext.GNUTranslations(io.BytesIO(mo_bytes))._catalog
    strings = tbcodec.unpack(strings_raw)[0]
    table44, table71 = _unpack_tournament_tables(nodup_raw)

    # 1. Index every Journal clanmates entry title in the translation file.
    entry_item_clanmates: Dict[str, str] = {}
    for k, v in translations.items():
        if isinstance(k, str) and "journal" in k and isinstance(v, str) and "your clanmates" in v.lower():
            extracted = clean_clanmates_title(v)
            if extracted:
                entry_item_clanmates[k] = extracted

    # 2. Group Table 44 rows by game_type.
    by_type: Dict[int, List[Tuple[int, int, list]]] = {}
    for k_str, row in table44.items():
        if not isinstance(row, list) or len(row) <= 10:
            continue
        try:
            k = ast.literal_eval(k_str)
            (game_type, variant), sub = k[0], k[1]
            by_type.setdefault(int(game_type), []).append((int(variant), int(sub), row))
        except Exception:  # noqa: BLE001
            continue

    # 3. Group Table 71 feature components by game_type.
    comps_by_type: Dict[int, List[Tuple[str, dict]]] = {}
    for k_str, row in table71.items():
        if not isinstance(row, list) or len(row) <= 6:
            continue
        try:
            k = ast.literal_eval(k_str)
            game_type = int(k[0][0])
            comp_name = strings[row[4]] if isinstance(row[4], int) and 0 < row[4] < len(strings) else ""
            cfg_str = strings[row[6]] if isinstance(row[6], int) and 0 < row[6] < len(strings) else "{}"
            cfg = json.loads(cfg_str) if isinstance(cfg_str, str) and cfg_str.startswith("{") else {}
            if comp_name:
                comps_by_type.setdefault(game_type, []).append((comp_name, cfg))
        except Exception:  # noqa: BLE001
            continue

    rankings_map: Dict[str, str] = {}
    types_map: Dict[int, str] = {}

    for game_type in sorted(by_type):
        rows = sorted(by_type[game_type], key=lambda item: (item[0], item[1]))
        _, _, row = rows[0]

        c10_str = strings[row[10]] if isinstance(row[10], int) and 0 < row[10] < len(strings) else "{}"
        c10 = json.loads(c10_str) if isinstance(c10_str, str) and c10_str.startswith("{") else {}

        info_key = c10.get("info_title") or ""
        info_title = _format_info_title(_translate(translations, info_key))

        family = ""
        m_fam = re.search(r"global_tournament\.client\.value\.([^.]+)\.", info_key)
        if m_fam:
            family = m_fam.group(1)

        j_users = c10.get("journal_users_result_titles") or {}
        j_user_key = j_users.get("0") or "" if isinstance(j_users, dict) else ""
        j_user_text = _translate(translations, j_user_key)

        direct_clanmates = clean_clanmates_title(j_user_text)
        family_clanmates = _family_clan_result_name(family, entry_item_clanmates)

        # Fallback from "The <Name> is over..." in journal_users_result_titles
        over_title = ""
        if j_user_text and not direct_clanmates:
            m_over = _THE_TOURNAMENT_IS_OVER.match(" ".join(j_user_text.strip().split()))
            if m_over:
                over_title = _format_info_title(m_over.group(1))

        base_title = info_title or over_title
        default_name = direct_clanmates
        if not default_name:
            if family_clanmates and not family_clanmates[0].islower():
                default_name = family_clanmates
            elif base_title:
                default_name = (
                    base_title
                    if "tournament" in base_title.lower() or "competition" in base_title.lower()
                    else f"{base_title} tournament"
                )

        if direct_clanmates:
            rankings_map[ranking_key(game_type, "")] = direct_clanmates

        # Component-driven rankings from Table 71
        for comp_name, cfg in comps_by_type.get(game_type, []):
            if comp_name == "TriumphUnion":
                union_name = (
                    entry_item_clanmates.get(
                        "client.global_tournament.value.journal.entry_item.union_of_triumph_clanmates_score.title"
                    )
                    or default_name
                )
                if union_name:
                    rankings_map[ranking_key(game_type, "")] = union_name
                    default_name = union_name

            elif comp_name == "MultistageJrnTournamentResultsSender":
                r_key = ranking_key(game_type, "multistage_tournament_clan_statistics_entry")
                r_name = family_clanmates if (family_clanmates and not family_clanmates[0].islower()) else default_name
                if r_name:
                    rankings_map[r_key] = r_name

            elif comp_name == "ClanPointsMining":
                r_key = ranking_key(game_type, "clan_points_mining_tournament_clan_statistics_entry")
                r_name = family_clanmates or default_name
                if r_name:
                    rankings_map[r_key] = r_name

            elif comp_name == "ClanMembersItemGainTracking":
                trackers = cfg.get("trackers") if isinstance(cfg, dict) else None
                if isinstance(trackers, list):
                    for tracker in trackers:
                        tracker_id = (tracker.get("trackerId") or "").strip() if isinstance(tracker, dict) else ""
                        if not tracker_id:
                            continue
                        r_key = ranking_key(
                            game_type,
                            f"clan_members_item_gain_tracking_final_tracker_statistic_entry:{tracker_id}",
                        )
                        r_name = (
                            _family_tracker_result_name(family, tracker_id, entry_item_clanmates)
                            or family_clanmates
                            or default_name
                        )
                        if r_name:
                            rankings_map[r_key] = r_name

        if default_name:
            types_map[game_type] = default_name

    return rankings_map, types_map


def _fetch_bytes(url: str, timeout: int = HTTP_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return tbcodec._decompress(resp.read())


def fetch_cdn_version(timeout: int = HTTP_TIMEOUT) -> Tuple[Dict[str, str], str]:
    """Returns `(static_production_hashes, locale_mo_md5)` from the public version manifest."""
    cfg_raw = _fetch_bytes(VERSIONS_URL, timeout=timeout)
    cfg = json.loads(cfg_raw.decode("utf-8"))["result"]["config"]
    cat_version = cfg["client_translations_catalog_version"]
    static_prod = cfg["client_static_version"]["production"]

    cat_raw = _fetch_bytes(f"{TRANSLATIONS_CDN_URL}/{cat_version}", timeout=timeout)
    cat = json.loads(cat_raw.decode("utf-8"))
    mo_md5 = next(item["md5"] for item in cat["files"] if item["name"] == LOCALE)

    version = {
        "no_duplicates": static_prod["no_duplicates"],
        "strings": static_prod["strings"],
        LOCALE: mo_md5,
    }
    return version, mo_md5


def fetch_cdn_catalog(
    version: Dict[str, str],
    timeout: int = HTTP_TIMEOUT,
) -> Tuple[Dict[str, str], Dict[int, str]]:
    """Downloads the static files and translation `.mo` for `version` from the CDN and decodes Tables 44 & 71."""
    mo_raw = _fetch_bytes(f"{TRANSLATIONS_CDN_URL}/{version[LOCALE]}", timeout=timeout)
    strings_raw = _fetch_bytes(f"{STATIC_CDN_URL}/{version['strings']}", timeout=timeout)
    nodup_raw = _fetch_bytes(f"{STATIC_CDN_URL}/{version['no_duplicates']}", timeout=timeout)
    return decode_tournament_tables(strings_raw, nodup_raw, mo_raw)


def load_desktop_catalog() -> Tuple[Dict[str, str], Dict[str, str], Dict[int, str]]:
    """Fallback: reads Tables 44 & 71 from the local Total Battle Desktop installation if present."""
    local_low = pathlib.Path.home() / "AppData" / "LocalLow" / "Scorewarrior" / "TotalBattle"
    cache_dir = local_low / "Cache"
    raw_dir = local_low / "RawFiles"
    if not cache_dir.is_dir() or not raw_dir.is_dir():
        return {}, {}, {}

    strings_file = next(cache_dir.glob("*_strings"), None)
    nodup_file = next(cache_dir.glob("*_no_duplicates"), None)
    cat_txt = raw_dir / "translation_catalog.txt"
    if not (strings_file and nodup_file and cat_txt.is_file()):
        return {}, {}, {}

    cat_name = cat_txt.read_text(encoding="utf-8").strip()
    cat = json.loads((raw_dir / cat_name).read_text(encoding="utf-8"))
    mo_md5 = next(item["md5"] for item in cat["files"] if item["name"] == LOCALE)
    mo_file = raw_dir / mo_md5
    if not mo_file.is_file():
        return {}, {}, {}

    version = {
        "no_duplicates": nodup_file.name,
        "strings": strings_file.name,
        LOCALE: mo_md5,
    }
    rankings_map, types_map = decode_tournament_tables(
        strings_file.read_bytes(),
        nodup_file.read_bytes(),
        mo_file.read_bytes(),
    )
    return version, rankings_map, types_map


class TournamentCatalog:
    """Lazy, disk-cached resolver for `(game_type, ranking) -> tournament_name`."""

    def __init__(self, cache_path: str = CACHE_PATH):
        self.cache_path = cache_path
        self._loaded = False
        self._version: Dict[str, str] = {}
        self._rankings: Dict[str, str] = {}
        self._types: Dict[int, str] = {}
        self._checked_cdn_version: Optional[Dict[str, str]] = None

    def _load_disk_cache(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not os.path.isfile(self.cache_path):
            return
        try:
            with open(self.cache_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._version = dict(data.get("version") or {})
            raw_rankings = data.get("rankings") or {}
            self._rankings = {
                str(k): str(v).strip()[:MAX_NAME_LEN]
                for k, v in raw_rankings.items()
                if isinstance(v, str) and v.strip()
            }
            raw_types = data.get("types") or {}
            self._types = {
                int(k): str(v).strip()[:MAX_NAME_LEN]
                for k, v in raw_types.items()
                if isinstance(v, str) and v.strip()
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not read local tournament catalog cache {self.cache_path}: {exc}")

    def _save_disk_cache(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            tmp_path = f"{self.cache_path}.tmp"
            payload = {
                "version": self._version,
                "rankings": dict(sorted(self._rankings.items())),
                "types": {str(k): v for k, v in sorted(self._types.items())},
            }
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.cache_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not write local tournament catalog cache {self.cache_path}: {exc}")

    def _refresh_from_cdn_if_needed(self, missing_key: str) -> None:
        """Consults the CDN only when `missing_key` is absent from the local disk cache."""
        try:
            cdn_version, _ = fetch_cdn_version()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not check CDN tournament catalog version for {missing_key}: {exc}")
            if not self._rankings and not self._types:
                self._fallback_desktop()
            return

        if cdn_version == self._version or cdn_version == self._checked_cdn_version:
            return

        self._checked_cdn_version = cdn_version
        logger.info(
            f"New tournament type '{missing_key}' not in local cache; updating tournament catalog from CDN "
            f"({cdn_version['no_duplicates'][:12]})..."
        )
        try:
            rankings_map, types_map = fetch_cdn_catalog(cdn_version)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not download tournament catalog from CDN: {exc}")
            if not self._rankings and not self._types:
                self._fallback_desktop()
            return

        if rankings_map or types_map:
            self._version = cdn_version
            self._rankings = rankings_map
            self._types = types_map
            self._save_disk_cache()
            logger.info(
                f"Tournament catalog updated from CDN: {len(self._rankings)} ranking(s) and "
                f"{len(self._types)} tournament type(s) cached locally."
            )

    def _fallback_desktop(self) -> None:
        try:
            version, rankings_map, types_map = load_desktop_catalog()
            if rankings_map or types_map:
                self._version = version
                self._rankings = rankings_map
                self._types = types_map
                self._save_disk_cache()
                logger.info(
                    f"Tournament catalog initialized from local Desktop cache: {len(self._rankings)} ranking(s) "
                    f"and {len(self._types)} tournament type(s)."
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not load local Desktop tournament catalog fallback: {exc}")

    def lookup(self, game_type: Optional[int], ranking: Optional[str] = "") -> str:
        """
        Resolves `(game_type, ranking)` to its English tournament name.

        Checks the local disk cache first; only queries the CDN when the exact
        `(game_type, ranking)` key is absent from the local cache.
        """
        if game_type is None or int(game_type) <= 0:
            return ""
        gtype = int(game_type)
        norm_ranking = normalize_ranking(ranking)
        r_key = ranking_key(gtype, norm_ranking)

        self._load_disk_cache()
        if r_key in self._rankings:
            return self._rankings[r_key]
        if not norm_ranking and gtype in self._types:
            return self._types[gtype]

        # Not in the local cache -> check if the CDN has a newer static build.
        self._refresh_from_cdn_if_needed(r_key)
        if r_key in self._rankings:
            return self._rankings[r_key]
        return self._types.get(gtype, "")


_default_catalog = TournamentCatalog()


def lookup_tournament_name(game_type: Optional[int], ranking: Optional[str] = "") -> str:
    """Resolves a tournament `(game_type, ranking)` using the shared local/CDN catalog."""
    return _default_catalog.lookup(game_type, ranking)


def make_catalogue_name(
    client,
    known: Dict[str, dict],
    lookup_fn: Callable[[Optional[int], Optional[str]], str] = lookup_tournament_name,
) -> Callable[[journal.Tournament], str]:
    """
    Builds the `catalogue_name(Tournament) -> str` resolver used by `AutoSearch`.

    1. Any nameless entry already present in `known` on the site is immediately
       resolved via `lookup_fn` (local cache -> CDN only on unknown) and named on
       the site through `client.send_catalog(...)`.
    2. During the Journal walk, a tournament whose `(game_type, ranking)` is not
       named on the site is resolved via `lookup_fn`, registered on the site's
       catalogue with its name, and returned so its ranking is sent right away.
    """
    pending_entries: List[dict] = []
    for entry in list(known.values()):
        if entry.get("name"):
            continue
        try:
            gtype = int(entry["game_type"])
        except (KeyError, TypeError, ValueError):
            continue
        r_norm = normalize_ranking(entry.get("ranking"))
        resolved = lookup_fn(gtype, r_norm)
        if resolved:
            entry["name"] = resolved
            pending_entries.append({
                "tournament_key": entry.get("tournament_key") or str(gtype),
                "ranking": r_norm,
                "name": resolved,
            })

    if pending_entries:
        try:
            client.send_catalog(pending_entries)
            logger.info(f"Named {len(pending_entries)} pending tournament type(s) on the site from game catalog.")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not update pending tournament names on the site: {exc}")

    def catalogue_name(t: journal.Tournament) -> str:
        gtype_str = (t.tournament_key or "").split(":")[0]
        r_norm = normalize_ranking(t.ranking)
        r_key = f"{gtype_str}|{r_norm}"
        existing = known.get(r_key)
        if existing and existing.get("name"):
            return str(existing["name"])
        try:
            gtype = int(gtype_str)
        except (TypeError, ValueError):
            return ""
        resolved = lookup_fn(gtype, r_norm)
        if not resolved:
            return ""
        known[r_key] = {"game_type": gtype, "ranking": r_norm, "name": resolved}
        cat_entry: Dict[str, str] = {
            "tournament_key": t.tournament_key or str(gtype),
            "ranking": r_norm,
            "name": resolved,
        }
        if t.ended_at:
            cat_entry["ended_at"] = t.ended_at_iso
        try:
            client.send_catalog([cat_entry])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not register tournament '{resolved}' ({r_key}) on the site catalogue: {exc}")
        return resolved

    return catalogue_name


