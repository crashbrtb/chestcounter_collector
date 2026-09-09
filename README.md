# Total Battle Chest Collector

Automatically collects clan chests in Total Battle and records each one into a
MySQL/MariaDB database. The web reporting and query interface remains
[chestcounter](https://github.com/crashbrtb/chestcounter).

Version 2.0 rebuilt the foundation of the application: the game now runs in
**Chrome controlled via CDP** instead of the desktop client, each **account has
its own login and password** along with its profiles, and there are no longer
hand-written coordinates — everything is calibrated by clicking directly on the
screen and configured through a graphical interface.

---

## Installation

1. Download the files to a folder (for example `C:\chestcounter`).
2. Run `install.bat` — creates the virtual environment and installs all dependencies.
3. Double-click `Configure.bat` (or `Configurar.bat`) and follow the steps in the **Execution** tab.

If an older version's `position.cfg` exists, `install.bat` automatically imports
database credentials from it: profiles will already appear registered, requiring
only the account login credentials.

### Tesseract (recommended)

While RapidOCR is installed via `pip`, Tesseract is a standalone program — and
it is the **primary** and fastest of the two OCR engines. Collection still
works without it (falling back to RapidOCR), though it will be slower and won't
preserve certain accents.
Install [Tesseract for Windows](https://github.com/UB-Mannheim/tesseract/wiki)
with language packages selected (e.g., Portuguese/English as needed).

---

## Usage

Double-click to run without needing a command prompt:

| File | Description |
|---|---|
| **`Configure.bat`** (or `Configurar.bat`) | Opens the interface: accounts, profiles, databases, and all parameters |
| **`Calibrate.bat`** (or `Calibrar.bat`) | Opens the calibration wizard directly |
| `run.bat` | Collects chests. **This is what Windows Task Scheduler should call.** |

The first two are shortcuts for `run.bat`, which also accepts `run.bat config`,
`run.bat calibrate`, and `run.bat check` (verifies configuration and calibration
without collecting) for command-line users.

Exit codes (visible in *Last Run Result* in Windows Task Scheduler):
`0` all collected · `1` failures occurred · `2` incomplete configuration/calibration ·
`3` cancelled.

**To cancel an ongoing run, hold ESC** for a moment — from any window, even with
the game in focus. The interface also includes a *Stop* button. Cancellation
interrupts even long wait delays, and whatever was already collected remains
safely saved in the database. (The ESC key sent by the collector to close dialogs
passes through CDP and does not touch physical keyboard events, so it never
triggers cancellation).

### Scheduling

Point Windows Task Scheduler to `run.bat`. There is nothing to configure inside
the `.bat` file itself: **all** execution settings (module, retry attempts,
timings, log level, and retention) are stored in `config/config.json`, editable
through the interface.

Logs are written directly by Python to
`execution_logs/collector_YYYY-MM-DD.log` in UTF-8, with automatic cleanup of
older files. Redirecting batch script output — as was done previously — caused
character encoding issues and truncated logs if the process exited unexpectedly.

---

## Accounts and Profiles

```
Account  (totalbattle.com email + password)
  └── Profile (city)  → dedicated database
  └── Profile (city)  → dedicated database
```

Collection strictly follows this hierarchy: it logs into the account and visits
its profiles before proceeding to the next account.

**Browser sessions are preserved, never wiped.** Clearing cookies causes the
game to treat the browser as a new device and send an email verification code
— which an unattended nightly run cannot respond to. Therefore, each run simply
inspects where the page currently is: already authenticated, or on the login
screen. Logging in becomes the exception rather than routine.

With **multiple accounts**, each account requires its own *Browser profile*
(a dedicated user data directory configured in Accounts and Profiles). This
allows each account to maintain its own verified session, and the browser is
relaunched with that profile directory when switching accounts. If two accounts
share the same profile directory, execution **halts before starting** with an
explanation — otherwise, the second account would run within the first account's
session and save chests into the wrong database.

**A profile without a database configured is skipped**, with a warning in the
log, as there would be nowhere to store collected records.

### How Login Works

The totalbattle.com login form **is present in the HTML upon page load, but
hidden** — it only becomes visible after clicking *Log In*. Therefore, the
collector first searches for the button that opens the form, clicks it, waits
for fields to become visible, and only then types using authentic keyboard and
mouse events.

Specific requirements discovered for this page:

- Absence of a visible password field does **not** mean you are logged in — a
  logged-out landing page also hides it initially. An active session is confirmed
  only when there is *neither* a visible password field *nor* a login button on screen.
- The button that opens login **is a `div`**, not a `<button>` — searches cover
  any clickable element and select the innermost match so entire containers are
  not mistaken for buttons.
- The page displays **sign-up and login simultaneously**, containing up to 8 email
  inputs in total (with sign-up being first). Fields are scoped strictly inside
  the container that holds the password field; otherwise, email would enter the
  registration form while the password entered login.
- Third-party social login buttons (Google, Facebook, VK...) and alternative
  prompts (*"Log in with a code"*, *"Forgot password"*) are filtered out. Among
  valid candidates, the shortest label is selected: *"Log in"* is the button,
  while *"Log in to claim your reward"* is merely a sentence containing those words.

If automatic detection fails, the CSS selectors in the **Login** configuration
section allow manually specifying the form opener button, input fields, submit
button, and logged-in confirmation selector.

---

## Calibration

The calibration wizard (`Calibrate.bat`) captures a screenshot of the page via
CDP, allowing you to mark controls by **clicking on the picture** while a
magnifier follows your cursor. The recorded coordinates are native page pixels:
independent of window position and Windows display scaling.

Captures compensate for **Windows display scaling**: Chrome renders screenshots
at `devicePixelRatio` (at 125%, a 1536 px page yields a 1920 px image), but
click events use CSS pixels. Captures are requested pre-compensated so the exact
pixel clicked on the image **matches** the click target in the browser — without
this, points would be scaled by 1.25x and miss their targets.

- **point** — single click coordinate (clan button, gifts tab...)
- **area** — rectangle region (where to search for text or buttons)
- **region with reference** — rectangle *and* an image crop, later searched
  on screen; this allows finding the "Open" button even when its position shifts.

**Calibrate with the window sized as it will be during automated runs.** Each
step records the viewport size it was calibrated on. If window dimensions change,
coordinates are scaled accordingly, but the wizard will warn if any point falls
out of bounds — clicks outside the page boundary hit nothing and behave like
unresponsive buttons.

The **🔎 Test this step** button exercises the step live in the running game and
**measures** results: for clicks, it compares before and after screenshots and
reports *"clicked (x, y) and screen changed 13%"* or *"screen DID NOT change: click
hit empty space"*; for areas, it displays the recognized OCR text; for reference
images, it reports whether the template was matched and with what similarity score.

**Scrollable profile lists** are handled automatically: the collector scrolls to
the top, searches for the name, scrolls down, and repeats until the list stops
moving — ensuring accounts with many profiles are fully traversed. If clicking
the profile menu does not change the screen, the log explicitly reports this
rather than misattributing the issue to OCR.

The **🧰 Test chest capture** button tests the three chest steps together, which
is the only way they mean anything: the "Open" button is found by its picture and
each chest's text is placed relative to the button found for it. It runs the
collector's own detection against the live screen and draws the answer — a box
around every button found, a box around the text area that belongs to it, and
what was read inside each one — so a panel of four chests is confirmed before a
run rather than after.

**Advance automatically** also performs the step. A step that marks a button
presses it, waits, takes a fresh capture and only then opens the next step, so
the game arrives at the screen that step describes instead of being left a screen
behind. If the click changes nothing the wizard stays where it is and says so.
Steps that only mark an area to read have nothing to press. The "Open" button is
never pressed automatically: it would consume a chest that nothing records.

Each step can be recalibrated individually without redoing the entire sequence.
The first two steps (store detection and close button) are optional and come
first because the store is what covers everything else — the game opens it by
itself, and closing it is what clears the screen for the rest of the sequence.

---

## OCR

The primary motivation for version 2.0 was resolving misread foreign and
accented names, which caused chests to be credited to the wrong players.

Benchmark comparison on game-rendered text (light text on dark textured background):

| Engine | Letters Correct | Accents Correct | Error Example |
|---|---|---|---|
| Tesseract | 6/8 | 3/8 | `Şükrü Öztürk` → `Sukriú Oztiirk` |
| RapidOCR | 7/8 | 0/8 | `Aurélio Gonçalves` → `Aurelio Goncalves` |
| **Both Combined** | **7/8** | **3/8** | — |

Neither engine is sufficient on its own: Tesseract preserves Latin accents but
can misidentify characters in Turkish or Nordic names; RapidOCR identifies
characters accurately but lacks diacritics in its dictionary. The default
(`auto`) mode combines both: **characters from RapidOCR, accents from Tesseract**,
applied only when both agree on the underlying letter. Neither engine invents
accents that do not exist; they only omit them, making this rule robust.

Tesseract also restores **word spacing**: RapidOCR frequently groups adjacent
words together (`ShadowChest`, `Level35epicCrypt`) because it detects whole
phrases as blocks, whereas Tesseract segments words correctly. When character
readings agree, the spaced form is used — preserving consistency with years of
historical records already stored in the database.

Furthermore, cropped regions are rendered **directly by Chrome** at the configured
scale (`ocr.capture_scale`), allowing OCR engines to process genuinely larger
glyphs rather than blurry upscaled screenshots.

### Engine Execution Order

**Tesseract runs first** as it is the faster engine (~310 ms). RapidOCR is only
called when Tesseract's confidence drops — benchmarking showed correct readings
score between 71 and 96, whereas erroneous readings scored 16 and 35. Because
clans repeat the same names and sources across thousands of chests, strings
already cross-validated by both engines are cached: in practice, secondary checks
drop to zero after the first batch.

Benchmark comparison on the chest panel (4 chests, 100% accuracy):

| Engine | Duration |
|---|---|
| **Tesseract** (por, psm 6, oem 1) | **310 ms** |
| RapidOCR (4 threads, no classifier) | 462 ms |
| onnxtr `fast_tiny` | 1040 ms |
| easyocr | untested — would require ~2.5 GB PyTorch |

### Speed Optimizations

Measured in-game, per chest: **from 2278 ms originally down to 212 ms** — 1000
chests drop from 38 minutes to approximately 3.5 minutes. Improvements made:

| Change | Impact |
|---|---|
| Read 4 screen chests in a single pass | Engine execution cost is identical for 1 vs 4 chests |
| Tesseract first, RapidOCR on demand | 462 → 310 ms, dropping to 0 ms for cached names |
| Memory cache for verified readings | Zero cross-checks after initial batches |
| Single-language Tesseract model | 446 → 310 ms |
| 4 threads in RapidOCR | 1646 → 375 ms (8 threads worsen: 1097 ms due to contention) |
| Disabled angle classifier | −490 ms (game UI text is never rotated) |
| `capture_scale` 2.0 instead of 3.0 | Capture time drops 477 → 263 ms with identical accuracy |

To optimize speed further, adjust **Chest click delay** (`timing.chest_click_delay`,
default 0.25s). Because chests are clicked bottom-up — opening a chest only shifts
items below it — there is no need to wait for the entire list to re-settle between
clicks.

### Database-Assisted OCR Correction

A clan environment is a bounded dataset: a few hundred members, around ninety
chest types, and approximately one hundred sources — all previously recorded in
`collected_chests`, `members`, and `player_name_mappings`. Prior to collecting a
profile, the collector loads this vocabulary (a one-time ~634 ms query against
hundreds of thousands of chests) and **matches** read values against known records.

**What matching does:**

1. Applies manual corrections configured in `player_name_mappings`.
2. Resolves minor **formatting** discrepancies against known names — identical
   letters and digits differing only by spacing, punctuation, or casing:
   `AncientWarrior'sChest` → `Ancient Warrior's Chest`, `|IMPERATOR` → `IMPERATOR`.

**What matching does NOT do:**
It will never arbitrarily substitute one name for another based on fuzzy
similarity alone. Similarity thresholds (e.g. 0.88) would conflate distinct entities:
`Common Chest of Wealth` matches `Uncommon Chest of Wealth` (0.957),
`DaNyx Darkher` matches `Nyx Darkher` (0.917), and `Pandeménia` matches `Pandeménio` (0.900).
A misspelled name recorded in the database will be visible in web reports and can
be mapped once in `player_name_mappings` for future runs. Conversely, mistakenly
merging two distinct players leaves no trace and cannot be undone. Therefore,
**unrecognized names are stored exactly as read**.

Matching also improves speed: when all three fields match known vocabulary, the
slower secondary engine check is bypassed.

Vocabulary hierarchy: **authoritative reference tables first, history second**.
For sources, the reference table is `standard_chests` (which determines event
scoring). For players, references are `members` and manual mappings. The historical
records in `collected_chests` cover unlisted sources. Chest names follow frequency
rankings.

**Names differing only by numerical suffixes are kept distinct:** player names
such as `Player`, `Player 1`, and `Player 11` are treated as different individuals.
When alphabetic letters match but numeric digits do not, matching is rejected.

### Profiles with Similar Names

Names like `Crash BR` and `Cash BR` are close enough that generic similarity tests
could accept either. Instead of a simple threshold check, names **compete**: the
scanned text is compared against all profiles registered under the account, and
only wins if it decisively beats competitors. Ties are treated as ambiguous
reads rather than risking switching to the wrong profile.

---

## Error Handling & Problem Tracking

Issues are categorized and routed to two destinations:

**Data Issues** — a chest that could not be read cleanly. Saved to
`incomplete_chests` along with a screen capture, to be reviewed by an
administrator in the web interface.

**Execution Failures** — menus failing to open, database errors, clicks not
consuming chests, or unreachable profiles. Recorded **exclusively in the local
log** (`execution_logs/collector_YYYY-MM-DD.log`), as these require operational
attention rather than manual data entry.

### Incomplete Chests Review Queue

An unreadable chest no longer interrupts collection: the **screenshot crop** is
saved to `incomplete_chests.screenshot` (PNG, ~115 KB) and the chest is opened
so collection proceeds.

In the web interface under **Admin › Chests › Incomplete Chests**, pending
records are displayed alongside their screenshots. Reviewers can read the image
and either **correct** the entry (which writes to `collected_chests` **with the
original collection timestamp** to preserve scoring cycles) or mark it as
**reviewed and unrecoverable**.

If a chest was manually recovered by a person, it is saved with `type = 1`
(*Manual*), distinguishing automated OCR from manual entry.

**Empty screens do not immediately terminate runs.** The game occasionally
delays refreshing chest lists after opening the last items. The collector waits
1 second and checks again, terminating only if the screen remains empty.

Chests are read and written to the database before the open click is executed.
If a click fails, the chest will simply be re-read on the next execution:
duplicate entries are easily corrected, while missed chests cannot be recovered.

---

## Directory Structure

```
config/     config.json (all parameters) · calibration.json · schema.py
core/       browser (CDP) · ocr · vision · calibration · session · runner
modules/    chest_collector · journal_parser · chat_automator
gui/        app (configuration UI) · calibration_wizard
database/   MySQL connections and repositories
utils/      logger · cancellation · text processing
```

`config/schema.py` defines each parameter once: the GUI, default configuration,
and validation logic are generated directly from it.

---

## Configuration Files

| File | Contents | Version Controlled? |
|---|---|---|
| `config/config.json` | Accounts, passwords, databases, all settings | No (contains credentials) |
| `config/calibration.json` | Screen coordinates for this machine | No |
| `config/calib_refs/*.png` | Calibration reference images | No |

Passwords are stored in plaintext in `config.json` (as was previously done in
`position.cfg`). This file is ignored by `.gitignore`; ensure folder permissions
are protected on shared machines.
