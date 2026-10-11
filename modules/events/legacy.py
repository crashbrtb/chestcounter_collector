"""Brings over what the standalone EventUploader saved on this computer.

The uploader kept its files in `%APPDATA%\\EventUploader` (`~/.config/EventUploader`
on Linux): the Journal calibration, every player name it had seen, and the
tournaments it had already sent. All three are worth keeping - the first saves
calibrating twelve steps again, the other two keep names on old rankings and
keep a tournament from being sent twice.

Nothing already here is replaced: a step that is calibrated stays as it is, and
the histories are merged. The token is not brought over: the uploader kept it
in the system's credential vault, and here each profile has its own.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import List

from .api import normalize_site_url
from .store import SentHistory, profile_cache_path

# The uploader's step names, and what they are called among everyone's steps here.
STEP_NAMES = {
    "journal_button": "journal_button",
    "events_tab": "journal_events_tab",
    "card1_title": "journal_card1_title",
    "card2_title": "journal_card2_title",
    "card_icon": "journal_card_icon",
    "page_area": "journal_page_area",
    "next_page": "journal_next_page",
    "card_open": "journal_card_open",
    "show_details": "journal_show_details",
    "ranking_list": "journal_ranking_list",
    "details_close": "journal_details_close",
    "card_close": "journal_card_close",
}


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


def _import_calibration(folder: Path, calibration, config, notes: List[str]) -> None:
    data = _read_json(folder / "calibracao_journal.json")
    if not data:
        notes.append("No Journal calibration found there.")
        return

    stored = data.get("viewport") or {}
    viewport = (int(stored.get("width", 0)), int(stored.get("height", 0)))
    step_viewports = data.get("step_viewports") or {}
    imported, kept = [], []
    for old, new in STEP_NAMES.items():
        point, region = (data.get("points") or {}).get(old), (data.get("regions") or {}).get(old)
        if point is None and region is None:
            continue
        if calibration.is_calibrated(new):
            kept.append(new)
            continue
        if point is not None:
            calibration.points[new] = (int(point[0]), int(point[1]))
        else:
            calibration.regions[new] = {k: int(region[k]) for k in ("left", "top", "width", "height")}
            reference = folder / "calibracao_refs" / f"{old}.png"
            if reference.exists():
                os.makedirs(calibration.refs_dir, exist_ok=True)
                shutil.copyfile(reference, calibration.ref_path(new))
        recorded_at = step_viewports.get(old) or list(viewport)
        if recorded_at and recorded_at[0] and recorded_at[1]:
            calibration.step_viewports[new] = (int(recorded_at[0]), int(recorded_at[1]))
        imported.append(new)

    if imported:
        if not (calibration.viewport[0] and calibration.viewport[1]):
            calibration.set_viewport(*viewport)
        calibration.save()
        notes.append(f"Calibration: {len(imported)} Journal step(s) imported.")
        if viewport != tuple(calibration.viewport) and all(viewport):
            notes.append(
                f"  ⚠ They were recorded with the page at {viewport[0]}x{viewport[1]} and this calibration "
                f"is at {calibration.viewport[0]}x{calibration.viewport[1]}: they are rescaled, which can "
                f"miss small buttons. Open the calibration and use 'Test this step' on each Journal step, "
                f"redoing the ones that miss."
            )
    if kept:
        notes.append(f"Calibration: {len(kept)} step(s) already calibrated here were left as they are.")

    cards = data.get("cards_per_page")
    if isinstance(cards, int) and cards > 0:
        config.set("modules.events", "cards_per_page", cards)
        config.save()


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


def import_eventuploader(config, calibration) -> str:
    """Imports everything found; returns what was done, one line per item."""
    folder = legacy_dir()
    if not folder.is_dir():
        return f"Nothing to import: {folder} does not exist on this computer."

    notes: List[str] = []
    _import_calibration(folder, calibration, config, notes)
    _import_profiles(folder, notes)
    _import_sent(_read_json(folder / "config.json"), notes)
    return "\n".join(notes) if notes else f"Nothing new to import from {folder}."
