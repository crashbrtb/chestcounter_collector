"""
The game's own catalogue of clan chests (Table 8 of the client static data).

Why this exists
---------------
On route 15000 the server sends each gift with a numeric `chest_type` (`game_type_id`)
and no text. The game client resolves that number locally from two sets of static
files hosted on its CDN (and cached by the Desktop client):

    1. Client static data (`_strings`, `_vectors`, `_no_duplicates`), encoded in
       the game's little-endian MessagePack format (`tbcodec`). Table 8 of
       `_no_duplicates` maps every `game_type_id` to the vector indices of its
       name (`row[3]`) and its clan chest source (`row[14]`).
    2. The GNU gettext `.mo` translation file for `en_US`, which turns keys such
       as `item.name.t2_crypt_clan_chest_9` and templates such as
       `item.clan_chest_source.epic_crypt` (`"Level %value% epic Crypt"`) into the
       English name and source stored in the database.

Consulting the CDN as little as possible
----------------------------------------
    1. Known types come straight from `game_chest_types` in the clan's database;
       while a run sees only those, nothing in this module is called.
    2. When a gift carries a `chest_type` the database does not know yet, the
       decoded Table 8 cached on disk (`config/module_data/chests/chest_types.json`)
       is checked first. If the type is already in that local file, it is named at
       once without touching the network.
    3. Only when a chest type is missing from both the database and the local cache
       does this module ask `https://versions.totalbattle.com/config` whether the
       CDN carries a newer static build, downloading it and updating the local
       cache file only when the build hash has changed.
"""
from __future__ import annotations

import gettext
import io
import json
import os
import pathlib
import unicodedata
import urllib.request
from typing import Dict, Iterable, List, Optional, Tuple

from config.settings import MODULE_DATA_DIR
from modules.events import tbcodec
from utils.logger import logger

VERSIONS_URL = "https://versions.totalbattle.com/config"
STATIC_CDN_URL = "https://assets-client-static.totalbattle.dev"
TRANSLATIONS_CDN_URL = "https://assets-translations.totalbattle.dev"

CHEST_TABLE_INDEX = 8
LOCALE = "en_US"
HTTP_TIMEOUT = 30
MAX_COLUMN_LEN = 50

DATA_DIR = os.path.join(MODULE_DATA_DIR, "chests")
CACHE_PATH = os.path.join(DATA_DIR, "chest_types.json")

_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Encoding": "gzip",
}


def normalize_text(text: str) -> str:
    """Normalizes game punctuation and diacritics to the ASCII forms used in `standard_chests`."""
    if not text:
        return ""
    cleaned = (
        text.replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u00a0", " ")
    )
    folded = "".join(ch for ch in unicodedata.normalize("NFKD", cleaned) if not unicodedata.combining(ch))
    return folded.strip()[:MAX_COLUMN_LEN]


def _resolve_vector(vec_idx: int, vectors: List[List[int]], strings: List[str],
                    translations: Dict[str, str]) -> str:
    if vec_idx is None or vec_idx < 0 or vec_idx >= len(vectors):
        return ""
    vec = vectors[vec_idx]
    if not vec:
        return ""
    key = strings[vec[0]]
    if not key:
        return ""
    text = translations.get(key, key)
    for i in range(1, len(vec) - 1, 2):
        placeholder = strings[vec[i]]
        val_key = strings[vec[i + 1]]
        val = translations.get(val_key, val_key) if val_key else ""
        text = text.replace(placeholder, val)
    return normalize_text(text)


def _unpack_table8(nodup_raw: bytes) -> dict:
    """Unpacks only up to index 8 of `_no_duplicates`, skipping the remaining 191 tables."""
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
    if count <= CHEST_TABLE_INDEX:
        raise tbcodec.TBDecodeError(f"_no_duplicates has only {count} tables")
    for _ in range(CHEST_TABLE_INDEX):
        unpacker.read()
    table8 = unpacker.read()
    if not isinstance(table8, dict):
        raise tbcodec.TBDecodeError("Table 8 in _no_duplicates is not a map")
    return table8


def decode_chest_table(strings_raw: bytes, vectors_raw: bytes, nodup_raw: bytes,
                       mo_raw: bytes) -> Dict[int, Tuple[str, str]]:
    """Extracts `{game_type_id: (name, source)}` from the raw static and `.mo` bytes."""
    mo_bytes = tbcodec._decompress(mo_raw)
    translations = gettext.GNUTranslations(io.BytesIO(mo_bytes))._catalog
    strings = tbcodec.unpack(strings_raw)[0]
    vectors = tbcodec.unpack(vectors_raw)[0]
    table8 = _unpack_table8(nodup_raw)

    catalog: Dict[int, Tuple[str, str]] = {}
    for tid_str, row in table8.items():
        if not isinstance(row, list) or len(row) <= 3:
            continue
        try:
            tid = int(tid_str)
        except (TypeError, ValueError):
            continue
        name = _resolve_vector(row[3], vectors, strings, translations)
        source = _resolve_vector(row[14], vectors, strings, translations) if len(row) > 14 else ""
        if name and source:
            catalog[tid] = (name, source)
    return catalog


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
        "vectors": static_prod["vectors"],
        LOCALE: mo_md5,
    }
    return version, mo_md5


def fetch_cdn_catalog(version: Dict[str, str], timeout: int = HTTP_TIMEOUT) -> Dict[int, Tuple[str, str]]:
    """Downloads the static files and translation `.mo` for `version` from the CDN and decodes Table 8."""
    mo_raw = _fetch_bytes(f"{TRANSLATIONS_CDN_URL}/{version[LOCALE]}", timeout=timeout)
    vectors_raw = _fetch_bytes(f"{STATIC_CDN_URL}/{version['vectors']}", timeout=timeout)
    strings_raw = _fetch_bytes(f"{STATIC_CDN_URL}/{version['strings']}", timeout=timeout)
    nodup_raw = _fetch_bytes(f"{STATIC_CDN_URL}/{version['no_duplicates']}", timeout=timeout)
    return decode_chest_table(strings_raw, vectors_raw, nodup_raw, mo_raw)


def load_desktop_catalog() -> Tuple[Dict[str, str], Dict[int, Tuple[str, str]]]:
    """Fallback: reads Table 8 from the local Total Battle Desktop installation if present."""
    local_low = pathlib.Path.home() / "AppData" / "LocalLow" / "Scorewarrior" / "TotalBattle"
    cache_dir = local_low / "Cache"
    raw_dir = local_low / "RawFiles"
    if not cache_dir.is_dir() or not raw_dir.is_dir():
        return {}, {}

    strings_file = next(cache_dir.glob("*_strings"), None)
    vectors_file = next(cache_dir.glob("*_vectors"), None)
    nodup_file = next(cache_dir.glob("*_no_duplicates"), None)
    cat_txt = raw_dir / "translation_catalog.txt"
    if not (strings_file and vectors_file and nodup_file and cat_txt.is_file()):
        return {}, {}

    cat_name = cat_txt.read_text(encoding="utf-8").strip()
    cat = json.loads((raw_dir / cat_name).read_text(encoding="utf-8"))
    mo_md5 = next(item["md5"] for item in cat["files"] if item["name"] == LOCALE)
    mo_file = raw_dir / mo_md5
    if not mo_file.is_file():
        return {}, {}

    version = {
        "no_duplicates": nodup_file.name,
        "strings": strings_file.name,
        "vectors": vectors_file.name,
        LOCALE: mo_md5,
    }
    catalog = decode_chest_table(
        strings_file.read_bytes(),
        vectors_file.read_bytes(),
        nodup_file.read_bytes(),
        mo_file.read_bytes(),
    )
    return version, catalog


class ChestCatalog:
    """Lazy, disk-cached resolver for `game_type_id -> (name, source)`."""

    def __init__(self, cache_path: str = CACHE_PATH):
        self.cache_path = cache_path
        self._loaded = False
        self._version: Dict[str, str] = {}
        self._types: Dict[int, Tuple[str, str]] = {}
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
            raw_types = data.get("types") or {}
            self._types = {
                int(k): (normalize_text(v[0]), normalize_text(v[1]))
                for k, v in raw_types.items()
                if isinstance(v, list) and len(v) == 2 and v[0] and v[1]
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not read local chest catalog cache {self.cache_path}: {exc}")

    def _save_disk_cache(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            tmp_path = f"{self.cache_path}.tmp"
            payload = {
                "version": self._version,
                "types": {str(k): [v[0], v[1]] for k, v in sorted(self._types.items())},
            }
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_path, self.cache_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not write local chest catalog cache {self.cache_path}: {exc}")

    def _refresh_from_cdn_if_needed(self, missing_ids: List[int]) -> None:
        """Consults the CDN only when `missing_ids` are not in the local disk cache."""
        try:
            cdn_version, _ = fetch_cdn_version()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not check CDN chest catalog version for types {missing_ids}: {exc}")
            if not self._types:
                self._fallback_desktop()
            return

        if cdn_version == self._version or cdn_version == self._checked_cdn_version:
            return

        self._checked_cdn_version = cdn_version
        logger.info(f"New chest type(s) {missing_ids} not in local cache; updating chest catalog from CDN "
                    f"({cdn_version['no_duplicates'][:12]})...")
        try:
            fresh = fetch_cdn_catalog(cdn_version)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not download chest catalog from CDN: {exc}")
            if not self._types:
                self._fallback_desktop()
            return

        if fresh:
            self._version = cdn_version
            self._types = fresh
            self._save_disk_cache()
            logger.info(f"Chest catalog updated from CDN: {len(self._types)} chest types cached locally.")

    def _fallback_desktop(self) -> None:
        try:
            version, desktop = load_desktop_catalog()
            if desktop:
                self._version = version
                self._types = desktop
                self._save_disk_cache()
                logger.info(f"Chest catalog initialized from local Desktop cache: {len(self._types)} chest types.")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not load local Desktop chest catalog fallback: {exc}")

    def lookup(self, type_ids: Iterable[int]) -> Dict[int, Tuple[str, str]]:
        """
        Resolves `type_ids` to `{type_id: (name, source)}`.

        Reads the local disk cache first; only queries the CDN if at least one
        requested `type_id` is absent from the local cache.
        """
        wanted = sorted({int(tid) for tid in type_ids if tid is not None})
        if not wanted:
            return {}

        self._load_disk_cache()
        missing = [tid for tid in wanted if tid not in self._types]
        if missing:
            self._refresh_from_cdn_if_needed(missing)

        return {tid: self._types[tid] for tid in wanted if tid in self._types}


_default_catalog = ChestCatalog()


def lookup_chest_types(type_ids: Iterable[int]) -> Dict[int, Tuple[str, str]]:
    """Resolves unknown chest type IDs using the shared local/CDN catalog."""
    return _default_catalog.lookup(type_ids)

