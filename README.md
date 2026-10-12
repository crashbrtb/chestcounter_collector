# Total Battle Chest Collector

Automatically collects clan chests in Total Battle and records each one into a
MySQL/MariaDB database. The web reporting and query interface remains
[chestcounter](https://github.com/crashbrtb/chestcounter).

Version 2.0 rebuilt the foundation of the application: the game now runs in
**Chrome controlled via CDP** instead of the desktop client, each **account has
its own login and password** along with its profiles, and there are no longer
hand-written coordinates — everything is calibrated by clicking directly on the
screen and configured through a graphical interface.

The application is made of **modules**, each one a job done on a profile of the
game. Three exist today, and the structure is there for more:

| Module | What it does | Runs |
|---|---|---|
| **Clan chests** | Opens every clan gift and records it in the profile's database | every run |
| **Clan members** | Reads the clan's Members list — player id, name, power, rank and level of every member — from the game's own traffic | once a day |
| **Tournament rankings** | Reads the clan's tournament results in the Journal and sends the rankings to the clan site (the former standalone *EventUploader*) | once a day |

See [Modules](#modules).

---

## Installation

1. Download the files to a folder (for example `C:\chestcounter`).
2. Run `install.bat` — creates the virtual environment and installs all dependencies.
3. Double-click `Configure.bat` and follow the steps in the **Execution** tab.

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

The tournament module reads the Journal card titles with it as well: Tesseract
keeps the spaces between words, which is what the tournament name is cut out
by. Without it the titles are read by RapidOCR alone, which tends to glue words
together.

---

## Usage

Double-click to run without needing a command prompt:

| File | Description |
|---|---|
| **`Configure.bat`** | Opens the interface: modules, accounts, profiles, databases, and all parameters |
| **`Calibrate.bat`** | Opens the calibration wizard directly |
| **`Check_Environment.bat`** | Quick diagnostic check for Python, RapidOCR, ONNXRuntime, and Tesseract |
| `run.bat` | Runs every enabled module that is due. **This is what Windows Task Scheduler should call.** |

The first two are shortcuts for `run.bat`, which also accepts `run.bat config`,
`run.bat calibrate`, and `run.bat check` (verifies configuration and calibration
without running anything) for command-line users.

From a command prompt, `main.py` takes a few more options: `--module chests`
(or `members`, `events`; may be repeated) runs only that module, `--force` ignores the
schedule so a once-a-day module runs again, and `--account` / `--profile`
restrict the run to one of them.

Exit codes (visible in *Last Run Result* in Windows Task Scheduler):
`0` all done · `1` failures occurred · `2` incomplete configuration/calibration ·
`3` cancelled.

**To cancel an ongoing run, hold ESC** for a moment — from any window, even with
the game in focus. The interface also includes a *Stop* button. Cancellation
interrupts even long wait delays, and whatever was already collected remains
safely saved in the database. (The ESC key sent by the collector to close dialogs
passes through CDP and does not touch physical keyboard events, so it never
triggers cancellation).

### Scheduling

Point Windows Task Scheduler to `run.bat`, as often as the chests should be
collected. There is nothing to configure inside the `.bat` file itself: **all**
execution settings (which modules run and when, retry attempts, timings, log
level, and retention) are stored in `config/config.json`, editable through the
interface.

One scheduled task is enough for every module. A module set to *once a day* is
done on the first run of the day and left out of the others; when no module has
anything to do, the run ends without even opening the browser.

Logs are written directly by Python to
`execution_logs/collector_YYYY-MM-DD.log` in UTF-8, with automatic cleanup of
older files. Redirecting batch script output — as was done previously — caused
character encoding issues and truncated logs if the process exited unexpectedly.

### Heartbeat

Every run of the chest module, from `run.bat` or from the interface, writes a row to the site's
`job_runs` table in each database it collects into: `running` when it starts,
then `success`, `partial` (some profiles failed), `failed` or `cancelled`, with
the chests collected, the profiles that failed and why, and the duration. The
site's **Admin > Monitoring** page and `/api/v1/health` raise the alarm when no
good run arrives in time — a run that found no chests still counts.

A database whose site has not been updated yet (no `job_runs` table) is skipped
silently, and a heartbeat that cannot be written never stops the collection.

---

## Modules

The **Modules** tab of the interface has one card per module, with the same two
controls on each — **Enabled** and **Runs** (*every run* or *once a day*) —
followed by that module's own parameters, and a status line saying how many
profiles are set up for it, whether its screens are calibrated and when it last
ran.

A run visits each account and each profile once, and on each profile does the
modules that are enabled, set up for that profile, and due. The modules are
independent: one that fails does not stop the next, one whose screens were never
calibrated is left out of the run (and reported) while the others go on.

**Once a day** means *one successful run per profile per day*. A run that fails
is tried again on the next start of the same day, up to *Attempts per day*
times; the state is kept in `config/module_state.json`. The *Ignore schedule*
box next to **Run now** (or `--force`) runs everything regardless, which is
what you want when testing.

### Clan chests

Runs on every profile that has a **database**. Everything from *Calibration*
down to *Error Handling* below is about this module.

### Clan members

Runs on every profile where **Read clan members** is ticked — per profile in
*Accounts and profiles › Modules…*. The list is the clan's, so one profile of
each clan is enough.

One reading, for one profile:

1. a second connection to the game tab starts listening to what the game
   receives — nothing is read off the screen;
2. the **Clan** button is clicked: the game answers with the clan and its
   membership, the player id and rank of every member;
3. **Members** is clicked: the game answers with the members' profiles — name,
   power, level, place on the map. The whole clan comes in that one answer;
4. only if the membership names someone no profile came for, the list is
   scrolled (inside *Clan: members list*) until that stops being true. A list
   that still ends short is not saved, and the reading is tried again.

The result is kept per clan in `config/module_data/members/`, replaced at each
reading: `<TAG>_<clan id>.json` and the same rows as a `.csv` that opens in a
spreadsheet (`player_id, name, power, rank, rank_name, level, kingdom, x, y`).
Ranks are the game's own: 1 Leader, 2 Superior, 3 Officer, 4 Veteran, 5 Soldier.

It needs two calibration steps besides the chests' *Clan button*: *Clan:
Members* (the menu entry) and, optionally, *Clan: members list*.

**To the clan site.** With *Send the list to the clan site* on (the default), a
profile that has a clan site address and token — the ones the tournament module
uses, in *Accounts and profiles › Modules…* — also sends each list to
`POST /api/v1/members`. There, every player in the list becomes an **active**
member (created if new, linked to their game id, renamed, power updated) and
every member **not** in it becomes **inactive**. Because of that, only a
complete list is ever sent. The files are written first; a site that refuses
the list fails the module for that run (the next run of the day sends a fresh
one), and a site that does not have this API yet is only warned about.

The first list sent to a site that grew its members from chest readings can
change a lot at once. The site finds a member by game id, then by name, then by
a name that differs only in spaces, accents, punctuation or case (`TonyStanna`
is `Tony Stanna`); a member it cannot find that way is created anew and the old
row made inactive — merge the two on the site's chests page.

### Tournament rankings

Runs on every profile that has a **clan site address and API token** — set per
profile in *Accounts and profiles › Modules…*, next to that profile's database.
Each profile belongs to one clan and each clan has its own site, so each profile
carries its own address and token; a profile without them is not searched. The
*Test connection* button in that dialog asks the site whether it accepts the
token.

One search, for one profile:

1. the site is asked whether it accepts the token — before anything is clicked;
2. a second connection to the game tab starts listening to what the game
   receives (the rankings travel in the game's own traffic; nothing is read off
   the screen but the tournament names);
3. the search starts at once, on the game as the previous module left it. (The
   game does not send again a ranking it already fetched since the page loaded;
   if rankings come back without an answer, *Reload the game before searching*
   reloads the page first, at the cost of a minute per profile);
4. the Journal is walked, newest first: each *"Your Clanmates' results in …"*
   card is opened, *Show details* is pressed, and the ranking list is scrolled
   until the game has sent every player's name. The walk goes back *Days to
   look back* days at most, by the age each card shows (*yesterday*, *7 days
   ago*), and after a complete search only the days since that search — plus
   one of overlap. Raising *Days to look back* walks all of them once;
5. the site is asked which of those tournaments it already has, and the rest
   are sent. A ranking where every player has a name is published right away;
   the others stay as drafts for review on the site;
6. the site is told how the search went. Only a complete one moves forward the
   point the next search starts from.

Nothing is sent twice: the site keeps one event per game result, a published
one is never sent again, and locally a ranking that did not change since it was
sent is skipped (`config/module_data/events/`).

**Coming from the standalone EventUploader?** The *Import from EventUploader*
button on the module's card brings over its Journal calibration, the players'
names it had collected and the list of tournaments it had already sent. The
token is not imported (it lives in the Windows Credential Manager): paste it on
the profile. If the Journal was calibrated at a different window size than the
chests, the import says so — check each Journal step with *Test this step*.
Once this module is running, disable the uploader's own scheduled task
(`BuscarTorneios`): two programs clicking in the same game at the same time
would get in each other's way.

### Adding a module

A module is a folder under `modules/` and one line in `modules/registry.py`.
It subclasses `BaseModule` (`modules/base_module.py`) and declares:

| It declares | And gets |
|---|---|
| `key`, `title`, `description` | its card in the Modules tab, its block in `config.json` |
| `settings` (a list of `Field`) | its parameters on that card, validated and saved |
| `profile_fields` | its fields in each profile's *Modules…* dialog |
| `steps`, `required_steps` | its own group of steps in the calibration wizard |
| `default_schedule` | every run, or once a day |
| `run_for_profile(profile)` | called with the game already logged in and on that profile |

Nothing else needs to change: the interface, the configuration file, the
wizard and the run are all built from the registry. `modules/chat_automator.py`
is the smallest example (it is not registered).

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

**A profile without a database configured is skipped** by the chest module, with
a warning in the log, as there would be nowhere to store collected records. What
the other modules need from a profile is filled in its *Modules…* dialog.

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

There is **one calibration for every module**. The steps all modules depend on
come first (the store, the profile menu, the active profile name), then each
module's own steps under its name. A step is known by its name, so a point two
modules need is calibrated once. Only the steps of the modules in use are
required; the others can be left for when the module is set up.

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

The **Chest text** area must reach far enough right to include the **Time left**
counter. That counter is what dates the chest: a chest appears showing
`19 h : 59 m` and counts down, so `12 h : 40 m` left means it was generated 7h19
ago, and that is the hour stored in `collected_at` — not the hour the collector
read it. Without the counter inside the marked area every chest of a run is dated
on arrival, which is what it used to do. Including it costs nothing: the counter
and the "Open" caption are cut off the reading by where they are printed, so
neither reaches the database.

The **🧰 Test chest capture** button tests the three chest steps together, which
is the only way they mean anything: the "Open" button is found by its picture and
each chest's text is placed relative to the button found for it. It runs the
collector's own detection against the live screen and draws the answer — a box
around every button found, a box around the text area that belongs to it, what was
read inside each one, and each chest's countdown with the generation time it would
store — so a panel of four chests is confirmed before a run rather than after.

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

### Reading the Chests from the Game's Own List

The screen is no longer where a chest's data comes from. When the clan window is
opened the game sends the list of gifts that are waiting, and the collector
listens to it (the same way the *Clan members* and *Tournament rankings* modules
listen): for each gift its id, the id of the player who sent it, the chest type,
the moment it expires and how many chests it is. The screen is still what gets
clicked — there is no other way to open a chest — but it is only read to find
the **Open** buttons.

| | From the screen | From the game's list |
|---|---|---|
| Player | name read by OCR, matched to the member list | the player's id in the game (`members.game_player_id`) |
| Chest and source | text read by OCR, matched to known spellings | the chest type id, named by `game_chest_types` |
| Generation time | countdown in whole minutes | expiry to the second, minus the chest's twenty hours |
| A chest counted twice or missed | cannot be noticed | the list is asked for again at the end and the database is made to agree with it |

Measured on a real collection of 859 chests recorded both ways
(`tools/chest_traffic.py`): every chest was in the list, in the order the panel
shows them, and the screen had one batch of four wrong — the panel was captured
a moment before it redrew, so one chest was written twice and another not at all.

What it needs, and what happens without it:

- **The clan site's database** must have the `game_chest_types` table and the
  `game_gift_id`, `game_type_id` and `game_player_id` columns of
  `collected_chests` (site migrations `CreateGameChestTypes` and
  `AddGameIdentityToChests`). On an older site the collector says so in the log
  and reads the screen as before.
- **A chest type met for the first time** is named from the screen, once, and
  written to `game_chest_types`. The reading is believed only when its countdown
  agrees with the gift's own expiry; otherwise the chest goes to the review queue
  with its picture and the ids the game did send.
- **`game_chest_types` and `standard_chests` are two tables on purpose.**
  `standard_chests` has one row per *source* — what the clan scores — and a
  source gives several chests (a *Level 25 Crypt* alone has twelve), each with
  its own type id in the game. A type points at the row of its source, and takes
  that row's spelling when the chest is written, since the scoring joins on it.
- **The Triumphal Gifts tab** is still read from the screen: it was empty on
  every recording made, so how the game lists those gifts is not known yet.
- The module setting **Read the chests from the game's own list** turns all of
  this off, and every chest is read from the screen again.

### Database-Assisted OCR Correction

A clan environment is a bounded dataset: a few hundred members, around ninety
chest types, and approximately one hundred sources — all previously recorded in
`collected_chests`, `members`, and `player_name_mappings`. Prior to collecting a
profile, the collector loads this vocabulary (a one-time ~634 ms query against
hundreds of thousands of chests) and **matches** read values against known records.

**The clan's member list decides whose chest it is.** A clan chest can only
come from someone in the clan, so once the *Clan members* module has sent the
game's own list to the site, the active members are everyone a player name can
be. A reading is compared by what a screen reading cannot change — no case,
accents, spacing or punctuation, no letters outside the Latin alphabet (no
engine here writes them), and the look-alikes `I`/`l`/`1` and `O`/`0` as one:

- read exactly as a member's name, it is that member;
- sharing that key with **one** member, it is recorded under the game's
  spelling: `HeStla BR` → `HeStIa BR`, `DRIKA BR` → `DRIKA BRف`;
- sharing it with **more than one** member (`Alok BR` and `ALOK BR` are two
  people), it is read again with the careful engine and, if still not exactly
  one of them, recorded as read for a person to correct — never handed to one
  of the two;
- like nobody in the list, it is matched the old way, below.

The list is only trusted for **26 hours** after it was read from the game (the
site keeps that moment as its `member_list` job): past it, someone may have
joined under a name the list has not seen, and names are matched the old way
until the list is read again. The log says which of the two is in force.

**What matching does otherwise:**

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
core/       browser (CDP) · ocr · vision · calibration · session
            runner (accounts > profiles > modules) · schedule (once a day)
modules/    base_module (what a module is) · registry (the list of modules)
  chests/   collector · steps
  members/  module · roster (the clan's packets) · steps · store (json + csv)
  events/   module · mapper (walks the Journal) · autosend (the search)
            journal · tbcodec (the game's packets) · traffic (listens via CDP)
            api (clan site client) · layout · titles · store · legacy (import)
gui/        app (configuration UI) · calibration_wizard
database/   MySQL connections and repositories
utils/      logger · cancellation · text processing
tests/      unit tests: python -m unittest discover -s tests
```

`config/schema.py` defines each shared parameter once, and each module declares
its own next to its code: the GUI, default configuration, and validation logic
are generated directly from them.

The dependencies go one way: `modules` use `core`, `core` uses `config` and
`utils`. Only the runner and the context builder know the registry.

### Tests

```
python -m unittest discover -s tests
```

They need no browser, database or network: a run is exercised against a
simulated game and a fake clan site (`tests/fakes.py`, `tests/test_events_mapper.py`).

---

## Configuration Files

| File | Contents | Version Controlled? |
|---|---|---|
| `config/config.json` | Accounts, passwords, databases, site tokens, all settings | No (contains credentials) |
| `config/calibration.json` | Screen coordinates for this machine | No |
| `config/calib_refs/*.png` | Calibration reference images | No |
| `config/module_state.json` | When each module last ran on each profile | No |
| `config/module_data/` | What modules keep between runs (players' names, tournaments sent) | No |

Passwords and site API tokens are stored in plaintext in `config.json` (as was previously done in
`position.cfg`). This file is ignored by `.gitignore`; ensure folder permissions
are protected on shared machines.
