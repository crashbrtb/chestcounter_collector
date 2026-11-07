# Total Battle Chest Collector

Automatically collects clan chests in Total Battle and records each one into a
MySQL/MariaDB database. The web reporting and query interface remains
[chestcounter](https://github.com/crashbrtb/chestcounter).

Version 4 is written in Rust: two executables and an installer, nothing else to
install besides Chrome. The game runs in **Chrome controlled via CDP**, each
**account has its own login and password** along with its profiles, and everything
is configured through a graphical interface. Every module talks to the game **over
the network**, from inside the game's own tab: nothing is clicked, nothing is read
off the screen, and there is no calibration.

The application is made of **modules**, each one a job done on a profile of the
game:

| Module | What it does | Runs |
|---|---|---|
| **Shield of Peace** | Asks the game when the city's shield ends and, with less than a day left, buys a new one with gold and activates it | every run |
| **Clan chests** | Opens every clan gift and records it in the profile's database | every run |
| **Clan members** | Reads the clan's Members list — player id, name, power, rank and level of every member — from the game's own answers | once a day |
| **Tournament rankings** | Reads the clan's tournament results in the Journal and sends the rankings to the clan site | once a day |

See [Modules](#modules).

---

## Installation

1. Run `TBChestCollector-Setup-<version>.exe`.
2. Choose the folder. The default is `%LOCALAPPDATA%\Programs\TB Chest Collector`;
   any folder the user can write to works, since the configuration lives next to the
   executables (avoid `C:\Program Files` unless the program will always run as
   administrator).
3. Optionally let the installer schedule an hourly run in Windows Task Scheduler.
4. Open **Configure** (Start menu) and fill in accounts, profiles and databases.

**Coming from the Python version (3.x)?** Install into the old folder, or copy its
`config\` folder (`config.json`, `module_state.json`, `module_data\`) into the new
one: the files are read as they are. Schedule it again from the interface
(*Execution* tab), which points the task to `tbcollector.exe` instead of `run.bat`. A `position.cfg` found in the folder on the first run is
imported as before.

Uninstalling leaves `config\` and `execution_logs\` in place.

---

## Usage

| Program | Description |
|---|---|
| **`tbcollector-config.exe`** (Start menu: *Configure*) | The interface: modules, accounts, profiles, databases, and all parameters |
| **`tbcollector.exe`** | Runs every enabled module that is due. **This is what Windows Task Scheduler should call.** |

`tbcollector.exe` options:

```
tbcollector                 run every enabled module that is due
tbcollector --gui           open the configuration interface
tbcollector --check         validate the configuration, collect nothing
tbcollector --account NAME  run only this account
tbcollector --profile NAME  run only this profile
tbcollector --module KEY    run only this module (shield, chests, members, events); may be repeated
tbcollector --force         ignore the schedule, so a once-a-day module runs again
tbcollector --keep-open     do not close the browser when finished
tbcollector diagnose        check the browser, every database and every clan site (reads only)
tbcollector fix-chest-types [--apply] [--profile NAME | --host H --port N --user U --password P --database D]
tbcollector audit-chest-types [--profile NAME] [--days N]
```

Exit codes (visible in *Last Run Result* in Windows Task Scheduler):
`0` all done · `1` failures occurred · `2` incomplete configuration ·
`3` cancelled.

**To cancel an ongoing run, hold ESC** for a moment — from any window, even with
the game in focus. The interface also has a *Stop* button. Cancellation interrupts
even long waits, and whatever was already collected remains saved in the database.
(The ESC key the collector sends to the page passes through CDP and never touches
the physical keyboard, so it cannot cancel the run.)

### Scheduling

The **Execution** tab of the interface schedules the collector: pick how often it
runs (every 5 minutes up to once a day), optionally the time of day the runs
count from, and press *Schedule*. It creates (or updates, or removes) a task named
*TB Chest Collector* in Windows Task Scheduler that starts `tbcollector.exe` of
that installation, and shows whether the task is there, how often it runs and
whether it points to another folder. The installer can also create an hourly task.
There is nothing to configure in the task itself: **all** execution settings
(which modules run and when, retry attempts, timings, log level, and retention)
are stored in `config\config.json`, editable through the interface.

One scheduled task is enough for every module. A module set to *once a day* is
done on the first run of the day and left out of the others; when no module has
anything to do, the run ends without even opening the browser. Only one run at a
time drives the browser: a run started while another is going exits with code 0
and leaves the work to the one in progress.

Logs are written to `execution_logs\collector_YYYY-MM-DD.log` in UTF-8, with
automatic cleanup of older files.

Every run ends its log with a **RUN SUMMARY** block, so there is no need to read
the whole log: the status (*SUCCESS*, *FINISHED WITH FAILURES*, *CANCELLED*,
*NOT STARTED*, *STOPPED BY AN ERROR* or *NOTHING TO DO*), the chests collected in
total and per profile, each module's outcome on each profile (`[OK]` / `[FAILED]`
with the reason), what was not due on that run, and every warning and error the
run logged, repeated there. When anything failed, the block is written as warnings.

### Heartbeat

Every run of the chest module writes a row to the site's `job_runs` table in each
database it collects into: `running` when it starts, then `success`, `partial`
(some profiles failed), `failed` or `cancelled`, with the chests collected, the
profiles that failed and why, and the duration. The site's **Admin > Monitoring**
page and `/api/v1/health` raise the alarm when no good run arrives in time — a
run that found no chests still counts.

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
is tried again on the next start of the same day, up to *Attempts per day* times;
the state is kept in `config\module_state.json`. The game's day starts at *Game
day starts at* (14:00 for the tournaments). The *Ignore schedule* box next to
**Run now** (or `--force`) runs everything regardless.

Each card also has a **Run only this module** button: it runs that module alone on
every profile set up for it, ignoring the schedule and the *Enabled* switch.

### Clan chests

Runs on every profile that has a **database**:

1. the player, the session and the server's address are learned from the requests
   the game itself keeps sending; the session stays in memory and is never logged
   or saved;
2. the game is asked for the profile's clan and for the list of gifts waiting;
3. each gift whose type has a name, oldest first, is written to the database and
   then opened, one request at a time, each waiting for the game's answer;
4. the list is asked for again: a gift that is still there untouched was not
   opened, so what was written for it is taken back, and whatever arrived
   meanwhile is collected too;
5. a chest type the database does not know is looked up in the game's own chest
   catalogue (its static data, cached in `config\module_data\chests\` and downloaded
   from the game's CDN only when the game publishes a new build); the type is
   registered in `game_chest_types` and its chests are collected in the same run.

| Field | Where it comes from |
|---|---|
| Player | the player's id; named by `members.game_player_id`, or by the game itself |
| Chest and source | the type id; named by `game_chest_types`, or by the game's chest catalogue |
| Generation time | expiry to the second, minus the chest's twenty hours |

- **A type missing from the catalogue** stays in the game for the next run, and the
  run is reported as not finished. A gift of such a type with less than an hour to
  live is opened and filed in the site's *Incomplete Chests* queue.
- `tbcollector fix-chest-types` corrects, from the catalogue, chests already
  written with a wrong type (without `--apply` it only shows what it would do);
  `tbcollector audit-chest-types` looks for types named wrong and writes nothing.

### Clan members

1. the game is asked for the clan and its membership: the player id and rank of
   every member;
2. it is asked for those members' profiles — name, power, level, place on the
   map — 100 at a time;
3. a list that ends short is asked for once more, and is not saved if it still does.

The result is kept per clan in `config\module_data\members\`, replaced at each
reading: `<TAG>_<clan id>.json` and the same rows as a `.csv` that opens in a
spreadsheet (`player_id, name, power, rank, rank_name, level, kingdom, x, y`).
Ranks are the game's own: 1 Leader, 2 Superior, 3 Officer, 4 Veteran, 5 Soldier.

**To the clan site.** With *Send the list to the clan site* on (the default), a
profile that has a clan site address and token also sends each list to
`POST /api/v1/members`: every player in the list becomes an **active** member and
every member **not** in it becomes **inactive**. That is why only a complete list
is ever sent. The files are written first; a site that refuses the list fails the
module for that run, and a site that does not have this API yet is only warned about.

### Shield of Peace

Runs on the profiles it is turned on for (*Keep the shield up*, in *Accounts and
profiles › Modules…*):

1. the game is asked when the shield of the profile's city ends;
2. with less than *Renew with less than (hours)* left (24 by default), or with no
   shield up, a shield is bought with gold and activated. A new shield replaces the
   current one and lasts 7 days;
3. the game is asked again, and the renewal only counts if the end moved.

**Spends gold, and needs runs closer together than that limit.** A shield bought
but not activated is reported as a failure and not retried in that run.

### Tournament rankings

Runs on every profile that has a **clan site address and API token** (*Accounts and
profiles › Modules…*). The *Test connection* button there asks the site whether it
accepts the token.

1. the site is asked whether it accepts the token, before the game is asked anything;
2. the game is asked for the Journal over the network, newest first, through the
   socket it keeps open for it. Nothing is clicked and no card is marked as read.
   The walk goes back *Days to look back* days at most, and after a complete search
   only the days since that search, plus one of overlap;
3. each card of the clan's results has its ranking asked for, and the players
   without a name have their profiles asked for;
4. the game sends a tournament's type but never its name. The site's catalogue names
   the types it knows; the game's own catalogue (cached in
   `config\module_data\events\`) names the rest, which are registered on the site.
   A type nobody can name goes to the site's catalogue, nameless, under *Admin ›
   Events › Pending Tournaments*; its rankings wait until it is named;
5. the cards with the **clans' ranking** of each tournament (the second card the
   game sends, "...Here are the mightiest Clans!") are read too: the clan's place
   and points among the clans listed, and what the clan received (for the Trials
   of Olympus and the Dark Omens the card says it; for the others it comes from the
   reward tiers of the clan's place). The items are named from the game's catalogue
   (cached in `config\module_data\events\game_items.json`). They are sent to the
   site before the rankings, so a tournament published on its own already has the
   clan's prizes to split. A site with none of them yet gets the last 45 days once;
6. the site is asked which of those tournaments it already has, and the rest are
   sent. **The site decides what is published without review**, tournament by
   tournament (*Tournament Catalogue › View › Publish events automatically*); a
   ranking with a player without a name always waits for a review;
7. the site is told how the search went. Only a complete one moves forward the
   point the next search starts from.

After the search, the game's **Tournaments window** is read and sent to the site
(*Events › Tournaments and Goals*): the tournaments running now and those about to
start, with their dates, the clan's points and place, the best clans (each clan's
name is asked of its own realm's server, remembered in
`config\module_data\events\clan_directory.json`), the clan's players and the
rewards of each place. The banners, icons and item icons are the game's own, read
from its Unity CDN (`assets-unity.totalbattle.dev`) and sent again only when the
game changes them. *Days to look back* defaults to 45.

`tbcollector journal-dump [--account A] [--profile P] [--days N]` writes the
Journal's tournament cards as the game sends them, and how the clan did in each,
to `execution_logs\journal_dump_*.txt`. It reads only: nothing is sent to a site.

Nothing is sent twice: the site keeps one event per game result, a published one
is never sent again, and locally a ranking that did not change since it was sent
is skipped (`config\module_data\events\`).

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
down the first time and shown on the account in *Accounts and profiles*. Ports
start at 9322. Folders are in `%USERPROFILE%\.total_battle_counter_profiles`. A
Chrome found on the account's port is only used after its command line shows it is
on the account's folder.

**The first login of a folder is by hand.** The game mails a code to a browser it
does not know. On a new folder the password is not even typed: that account stops
and says so, and the others go on. Press *Open window* on the account, log in and
type the code. The session stays in the folder, and if the game drops it later the
password is enough.

**Each profile has its own database.** Two active profiles on the same one cannot
be saved, and a run refuses to start. **A profile without a database** is skipped
by the chest module.

### How Login Works

The totalbattle.com login form is in the page from the start, but hidden until
*Log in* is clicked. The collector finds the button that opens the form (any
clickable element, the innermost match with the shortest label, social logins
and "log in with a code"/"forgot password" left out), clicks it, waits for the
fields, scopes them to the box holding the password field, and types with real
input events. A page is logged in only when it shows neither a visible password
field nor a login button (or the game canvas is up). The CSS selectors in the
**Login** section override the automatic detection.

---

## Error Handling

**Data issues** — a chest with something missing (its type's name or its player's)
— go to `incomplete_chests` with what is known and the game's ids, to be reviewed
under **Admin › Chests › Incomplete Chests**. Chests are written before they are
opened: a chest written and not opened can be put right, one opened and not written
cannot.

**Execution failures** — the game not answering, database errors, gifts the game
did not open, unreachable profiles — are recorded in the local log. With *Save
screenshot on error* on, a failure also leaves a picture of the game's tab in
`execution_logs\screenshots\`.

---

## Building from source

Needs Rust (stable) and, for the installer, Inno Setup 6.

```
powershell -ExecutionPolicy Bypass -File build.ps1
```

runs the tests, builds `dist\tbcollector.exe` and `dist\tbcollector-config.exe`, and
the installer in `installer\`. Both folders are generated and ignored by git.

```
src/
  bin/            tbcollector (the run, the command line) · tbcollector-config (the window)
  cli.rs          options, exit codes, first-run configuration
  config.rs       every parameter, the account/profile model, config.json
  runner.rs       accounts > profiles > modules, retries, accounts at the same time
  schedule.rs     once a day, per profile, the game's day
  context.rs      the services of one account's window
  browser.rs      Chrome over CDP: start, reuse, ownership, input, screenshots
  session.rs      login and profile switch
  link.rs         the game over the network: requests and the Journal's socket
  tbcodec.rs      the game's little-endian MessagePack and its frames
  static_data.rs  the client's static data on the CDN · mo.rs (gettext)
  db.rs           MySQL/MariaDB, chest repository, job_runs heartbeat
  modules/        mod.rs (what a module is, the list of modules)
    chests/       gifts · from_game (the collection) · catalog
    members/      roster · store (json + csv)
    events/       journal · mapper · autosend · api (clan site) · catalog · store
    shield.rs
  gui/            the configuration window (egui)
  tools.rs        diagnose, fix-chest-types, audit-chest-types
tests/            checks run by hand against the real CDN and databases (live.rs) and
                  against a real Chrome talking to a simulated game (cdp_game.rs)
packaging/        installer.iss (Inno Setup)
```

### Tests

```
cargo test                                               # unit tests: no browser, database or network
cargo test --test cdp_game -- --ignored --nocapture      # a real Chrome and a simulated game
cargo test --test live -- --ignored --nocapture          # the real CDN and the configured databases (reads only)
```

### Adding a module

A module is a file or folder under `src/modules/` and one entry in `Kind`
(`src/modules/mod.rs`): its key, title, description, settings (a list of `Field`),
profile fields, default schedule, `configured_for`, and a type implementing
`Module` whose `run_for_profile` is called with the game already logged in and on
that profile. The interface, the configuration file and the run are all built
from it. `src/modules/shield.rs` is the smallest example.

---

## Configuration Files

| File | Contents | Version Controlled? |
|---|---|---|
| `config\config.json` | Accounts, passwords, databases, site tokens, all settings | No (contains credentials) |
| `config\module_state.json` | When each module last ran on each profile | No |
| `config\module_data\` | What modules keep between runs (catalogues, players' names, tournaments sent, member lists) | No |

Passwords and site API tokens are stored in plaintext in `config.json`. Protect the
folder's permissions on shared machines.
