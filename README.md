# Total Battle Chest Collector

Automatically collects clan chests in Total Battle and records each one into a
MySQL/MariaDB database. The web reporting and query interface remains
[chestcounter](https://github.com/crashbrtb/chestcounter).

Version 2.0 rebuilt the foundation of the application: the game now runs in
**Chrome controlled via CDP** instead of the desktop client, each **account has
its own login and password** along with its profiles, and everything is
configured through a graphical interface. Every module talks to the
game **over the network**, from inside the game's own tab: nothing is clicked,
nothing is read off the screen, and there is no calibration.

The application is made of **modules**, each one a job done on a profile of the
game. Four exist today, and the structure is there for more:

| Module | What it does | Runs |
|---|---|---|
| **Shield of Peace** | Asks the game when the city's shield ends and, with less than a day left, buys a new one with gold and activates it | every run |
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

---

## Usage

Double-click to run without needing a command prompt:

| File | Description |
|---|---|
| **`Configure.bat`** | Opens the interface: modules, accounts, profiles, databases, and all parameters |
| **`Check_Environment.bat`** | Quick diagnostic check for Python and its libraries |
| `run.bat` | Runs every enabled module that is due. **This is what Windows Task Scheduler should call.** |

`Configure.bat` is a shortcut for `run.bat config`; `run.bat check` verifies the
configuration without running anything.

From a command prompt, `main.py` takes a few more options: `--module chests`
(or `shield`, `members`, `events`; may be repeated) runs only that module, `--force` ignores the
schedule so a once-a-day module runs again, and `--account` / `--profile`
restrict the run to one of them.

Exit codes (visible in *Last Run Result* in Windows Task Scheduler):
`0` all done · `1` failures occurred · `2` incomplete configuration ·
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
profiles are set up for it and when it last ran.

A run visits each account and each profile once, and on each profile does the
modules that are enabled, set up for that profile, and due. The modules are
independent: one that fails does not stop the next, and one that cannot work at
all is left out of the run (and reported) while the others go on.

**Once a day** means *one successful run per profile per day*. A run that fails
is tried again on the next start of the same day, up to *Attempts per day*
times; the state is kept in `config/module_state.json`. The *Ignore schedule*
box next to **Run now** (or `--force`) runs everything regardless, which is
what you want when testing.

Each card also has a **Run only this module** button: it runs that module alone
on every profile set up for it, ignoring the schedule and the *Enabled* switch
(the same as `--module KEY --force`, except that a disabled module runs too).

### Clan chests

Runs on every profile that has a **database**. It talks to the game over the
network, from inside the game's own tab:

1. the player, the session and the server's address are learned from the requests
   the game itself keeps sending; the session stays in memory and is never logged
   or saved;
2. the game is asked for the profile's clan and for the list of gifts waiting;
3. each gift whose type has a name, oldest first, is written to the database and
   then opened, one request at a time, each waiting for the game's answer;
4. the list is asked for again: a gift that is still there untouched was not opened,
   so what was written for it is taken back, and whatever arrived meanwhile is
   collected too;
5. a chest type the database does not know is looked up in the game's own chest
   catalogue (its static data, cached in `config/module_data/chests/` and
   downloaded from the game's CDN only when the game publishes a new build); the
   type is registered in `game_chest_types` and its chests are collected in the
   same run.

| Field | Where it comes from |
|---|---|
| Player | the player's id; named by `members.game_player_id`, or by the game itself when the site does not know the id |
| Chest and source | the type id; named by `game_chest_types`, or by the game's chest catalogue |
| Generation time | expiry to the second, minus the chest's twenty hours |

- **A type missing from the catalogue** stays in the game for the next run, and the
  run is reported as not finished. A gift of such a type with less than an hour to
  live is opened and filed in the site's *Incomplete Chests* queue with its code,
  player and date, for a person to name.
- `tools/fix_chest_types.py` corrects, from the catalogue, chests already written
  with a wrong type.

### Clan members

One reading, for one profile:

1. the game is asked over the network, from inside its tab, for the clan and its
   membership: the player id and rank of every member. Nothing is clicked or read
   off the screen;
2. it is asked for those members' profiles — name, power, level, place on the
   map — 100 at a time;
3. a list that ends short is asked for once more, and is not saved if it still
   does.

The result is kept per clan in `config/module_data/members/`, replaced at each
reading: `<TAG>_<clan id>.json` and the same rows as a `.csv` that opens in a
spreadsheet (`player_id, name, power, rank, rank_name, level, kingdom, x, y`).
Ranks are the game's own: 1 Leader, 2 Superior, 3 Officer, 4 Veteran, 5 Soldier.

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

### Shield of Peace

Runs on the profiles it is turned on for (*Keep the shield up*, in *Accounts and
profiles › Modules…*). Everything goes over the network, without clicking:

1. the game is asked when the shield of the profile's city ends;
2. with less than *Renew with less than (hours)* left (24 by default), or with no
   shield up, a shield is bought with gold and activated. A new shield replaces the
   current one and lasts 7 days;
3. the game is asked again, and the renewal only counts if the end moved.

**Spends gold, and needs runs closer together than that limit**: with a run a day
and a limit of 24 hours, a shield can end before the next run sees it. A shield
bought but not activated (the game can refuse one while a march is out) is reported
as a failure and not retried in that run; the next run buys another, since what the
player already holds is not read.

### Tournament rankings

Runs on every profile that has a **clan site address and API token** — set per
profile in *Accounts and profiles › Modules…*, next to that profile's database.
Each profile belongs to one clan and each clan has its own site, so each profile
carries its own address and token; a profile without them is not searched. The
*Test connection* button in that dialog asks the site whether it accepts the
token.

One search, for one profile:

1. the site is asked whether it accepts the token, before the game is asked anything;
2. the game is asked for the Journal over the network, newest first, through the
   socket it keeps open for it. Nothing is clicked and no card is marked as read.
   The walk goes back *Days to look back* days at most, and after a complete
   search only the days since that search, plus one of overlap. Raising *Days to
   look back* walks all of them once;
3. each card of the clan's results has its ranking asked for, and the players
   without a name have their profiles asked for;
4. the game sends a tournament's type but never its name. The site's catalogue
   names the types it knows. A new one is told to the catalogue, nameless, and shows
   up on the site under *Admin › Events › Pending Tournaments*, for a person to name.
   Its rankings wait: the search is reported as not complete, and the first one after
   the naming sends them. Name it within *Days to look back*, or raise that setting,
   since a search walks no further back than that;
5. the site is asked which of those tournaments it already has, and the rest
   are sent. A ranking where every player has a name is published right away;
   the others stay as drafts for review on the site;
6. the site is told how the search went. Only a complete one moves forward the
   point the next search starts from.

Nothing is sent twice: the site keeps one event per game result, a published
one is never sent again, and locally a ranking that did not change since it was
sent is skipped (`config/module_data/events/`).

**Coming from the standalone EventUploader?** Paste the site token on the profile, then disable the uploader's own scheduled task
(`BuscarTorneios`): two programs driving the same game would get in each other's way.

### Adding a module

A module is a folder under `modules/` and one line in `modules/registry.py`.
It subclasses `BaseModule` (`modules/base_module.py`) and declares:

| It declares | And gets |
|---|---|
| `key`, `title`, `description` | its card in the Modules tab, its block in `config.json` |
| `settings` (a list of `Field`) | its parameters on that card, validated and saved |
| `profile_fields` | its fields in each profile's *Modules…* dialog |
| `default_schedule` | every run, or once a day |
| `run_for_profile(profile)` | called with the game already logged in and on that profile |

Nothing else needs to change: the interface, the configuration file and the
run are all built from the registry. `modules/shield/module.py` is the smallest
example.

---

## Accounts and Profiles

```
Account  (totalbattle.com email + password)
  └── Profile (city)  → dedicated database
  └── Profile (city)  → dedicated database
```

An account's profiles are visited one after the other, while **accounts work at
the same time**, each in its own Chrome window (*Execution › Accounts at the same
time*: up to 4, and 1 for one account after the other).

**Each account has its own window**: a debugging port and a Chrome folder, written
down the first time and shown on the account in *Accounts and profiles*. Ports start
at *First debugging port* (9322), away from other tools on 9222. Folders are in
`%USERPROFILE%\.total_battle_counter_profiles`. Change a port or a folder there if
it clashes with something else; two active accounts cannot share either. A Chrome
found on the account's port is only used after its command line shows it is on the
account's folder: anything else stops that account and is left open.

**The first login of a folder is by hand.** The game mails a code to a browser it
does not know, and an unattended run cannot answer it. On a new folder the password
is not even typed: that account stops and says so, and the others go on. Press *Open
window* on the account, log in and type the code. The session stays in the folder, and
if the game drops it later the password is enough. A new folder means a new code.
Telling that the game is asking for the code goes by how such fields are named.

**Each profile has its own database.** Two active profiles on the same one, in any
account, cannot be saved, and a run refuses to start.

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

## Error Handling & Problem Tracking

Issues are categorized and routed to two destinations:

**Data Issues** — a chest with something missing (its type's name or its
player's). Saved to `incomplete_chests` with what is known and the game's ids, to
be reviewed by an administrator in the web interface.

**Execution Failures** — the game not answering, database errors, gifts the game
did not open, or unreachable profiles. Recorded **exclusively in the local
log** (`execution_logs/collector_YYYY-MM-DD.log`), as these require operational
attention rather than manual data entry. With *Save screenshot on error* on, a
failure also leaves a picture of the game's tab in `execution_logs/screenshots/`,
for a person to see what the game was showing.

### Incomplete Chests Review Queue

A chest about to expire whose type is not in the catalogue, a player the game does not
name, or a row the database refused do not interrupt collection: the chest is filed in `incomplete_chests` with what is known and the
game's ids, and opened.

In the web interface under **Admin › Chests › Incomplete Chests**, reviewers can
**correct** the entry (which writes to `collected_chests` **with the original
timestamp** to preserve scoring cycles) or mark it as **reviewed and unrecoverable**.
A chest recovered by a person is saved with `type = 1` (*Manual*).

Chests are written to the database before they are opened. A chest written and not
opened can be put right, while one opened and not written cannot be recovered.

---

## Directory Structure

```
config/     config.json (all parameters) · schema.py · settings
core/       browser (CDP) · session (login, profile switch)
            runner (accounts > profiles > modules) · schedule (once a day)
modules/    base_module (what a module is) · registry (the list of modules)
  chests/   collector · from_game (the collection) · game_link (the network) · gifts (the packets)
            catalog (chest types from the game's static data)
  members/  module · roster (the clan's packets) · store (json + csv)
  shield/   module (reads, buys and activates the shield over the network)
  events/   module · mapper (walks the Journal) · autosend (the search)
            feed (asks the game)
            journal · tbcodec (the game's packets) · traffic (listens via CDP)
            api (clan site client) · store · legacy (import)
gui/        app (configuration UI)
database/   MySQL connections and repositories
utils/      logger · cancellation · text processing
tools/      one-off maintenance scripts (audit_chest_types, fix_chest_types)
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
| `config/module_state.json` | When each module last ran on each profile | No |
| `config/module_data/` | What modules keep between runs (players' names, tournaments sent) | No |

Passwords and site API tokens are stored in plaintext in `config.json` (as was previously done in
`position.cfg`). This file is ignored by `.gitignore`; ensure folder permissions
are protected on shared machines.
