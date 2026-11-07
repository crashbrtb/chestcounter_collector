"""Brings over what the standalone EventUploader saved on this computer.

The uploader kept its files in `%APPDATA%\\EventUploader` (`~/.config/EventUploader`
on Linux): every player name it had seen, and the tournaments it had already sent.
Both are worth keeping - they keep names on old rankings and keep a tournament
from being sent twice.

Nothing already here is replaced: the histories are merged. The token is not
brought over: the uploader kept it in the system's credential vault, and here
each profile has its own.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import List

from .api import normalize_site_url
from .store import SentHistory, profile_cache_path


def legacy_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home())
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "EventUploader"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _import_profiles(folder: Path, notes: List[str]) -> None:
    theirs = (_read_json(folder / "perfis_jogadores.json").get("profiles") or {})
    if not theirs:
        return
    path = Path(profile_cache_path())
    ours = (_read_json(path).get("profiles") or {})
    new = {pid: value for pid, value in theirs.items() if pid not in ours}
    if not new:
        return
    ours.update(new)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"profiles": ours}, ensure_ascii=False), encoding="utf-8")
    notes.append(f"Players: {len(new)} name(s) imported.")


def _import_sent(settings: dict, notes: List[str]) -> None:
    site = normalize_site_url(str(settings.get("site_url") or ""))
    sent = settings.get("sent") or {}
    if not site:
        return
    if isinstance(sent, dict) and sent:
        history = SentHistory(site)
        new = {uid: info for uid, info in sent.items() if isinstance(info, dict) and uid not in history.sent}
        if new:
            history.sent.update(new)
            history.save()
            notes.append(f"History: {len(new)} tournament(s) already sent to {site} imported.")
    notes.append(
        f"The uploader sent to {site}. Type that address and the site's API token on the profile of "
        f"that clan: Accounts and profiles > Modules… (the token is not imported)."
    )


def import_eventuploader(config=None) -> str:
    """Imports everything found; returns what was done, one line per item."""
    folder = legacy_dir()
    if not folder.is_dir():
        return f"Nothing to import: {folder} does not exist on this computer."

    notes: List[str] = []
    _import_profiles(folder, notes)
    _import_sent(_read_json(folder / "config.json"), notes)
    return "\n".join(notes) if notes else f"Nothing new to import from {folder}."
