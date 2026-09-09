"""
Configuration interface.

Everything that used to be edited by hand in position.cfg is here: the
execution parameters, the accounts with their logins, the profiles with their
databases, and the calibration. The window is built from `config/schema.py`, so
a parameter added there shows up here with its label and help text without any
change to this file.

The run button exists so the whole loop - configure, calibrate, test, fix -
happens in one place. It runs the collection in a worker thread and streams the
same log the scheduled run would write, because the useful question after a
change is always 'would tonight's run work?'.
"""

import queue
import logging
import threading
import webbrowser
from typing import Dict, List, Optional

import customtkinter as ctk
from tkinter import messagebox

from config.schema import SECTIONS
from config.settings import AccountConfig, ConfigManager, DatabaseConfig, ProfileConfig
from core.calibration import Calibration
from database.db_connection import test_connection
from utils.cancel import Cancelled, cancellation, escape_watcher
from utils.logger import logger

from .calibration_wizard import CalibrationWizard

PANEL_BG = "#16181d"
CARD_BG = "#1c2029"
DARK_BG = "#0d1117"
ACCENT = "#58a6ff"
OK_COLOR = "#3fb950"
WARN_COLOR = "#d29922"
ERROR_COLOR = "#f85149"
MUTED = "#8b949e"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


def labelled_entry(parent, label: str, width: int, **options) -> ctk.CTkEntry:
    """
    An entry with its name written above it.

    A placeholder is not a label: it disappears the moment anything is typed,
    so a filled-in account was four anonymous boxes and the only way to tell
    the login from the nickname was to clear one and see what came back.
    """
    column = ctk.CTkFrame(parent, fg_color="transparent")
    column.pack(side="left", padx=6)
    ctk.CTkLabel(column, text=label, font=ctk.CTkFont(size=11), text_color=MUTED,
                 anchor="w").pack(anchor="w")
    entry = ctk.CTkEntry(column, width=width, **options)
    entry.pack(anchor="w")
    return entry


class LogPipe(logging.Handler):
    """Feeds the application log into the interface without blocking the worker thread."""

    def __init__(self, sink: queue.Queue):
        super().__init__()
        self.sink = sink
        self.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)-7s] %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.sink.put_nowait(self.format(record))
        except Exception:
            pass


class DatabaseDialog(ctk.CTkToplevel):
    """Database credentials of one profile, with a connection test."""

    FIELDS = (
        ("host", "Host", False),
        ("port", "Port", False),
        ("user", "User", False),
        ("password", "Password", True),
        ("database", "Database", False),
    )

    def __init__(self, master, profile: ProfileConfig, on_save):
        super().__init__(master)
        self.profile = profile
        self.on_save = on_save
        self.entries: Dict[str, ctk.CTkEntry] = {}

        self.title(f"Database — {profile.name or 'new profile'}")
        self.geometry("460x420")
        self.transient(master)
        self.grab_set()

        database = profile.database or DatabaseConfig()
        ctk.CTkLabel(self, text="Database credentials for this profile",
                     font=ctk.CTkFont(size=15, weight="bold"), text_color=ACCENT).pack(pady=(16, 2))
        ctk.CTkLabel(self, text="A profile without a database is skipped during collection.",
                     font=ctk.CTkFont(size=11), text_color=MUTED).pack(pady=(0, 10))

        form = ctk.CTkFrame(self, fg_color="transparent")
        form.pack(fill="x", padx=24)
        for key, label, secret in self.FIELDS:
            row = ctk.CTkFrame(form, fg_color="transparent")
            row.pack(fill="x", pady=4)
            ctk.CTkLabel(row, text=label, width=90, anchor="w").pack(side="left")
            entry = ctk.CTkEntry(row, show="*" if secret else "")
            entry.insert(0, str(getattr(database, key, "")))
            entry.pack(side="left", fill="x", expand=True)
            self.entries[key] = entry

        self.lbl_test = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=11),
                                     text_color=MUTED, wraplength=400)
        self.lbl_test.pack(pady=(10, 4))

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack(fill="x", padx=24, pady=(6, 16))
        ctk.CTkButton(buttons, text="Test connection", fg_color="#21262d",
                      command=self._test).pack(side="left")
        ctk.CTkButton(buttons, text="Clear database", fg_color="#21262d",
                      command=self._clear).pack(side="left", padx=8)
        ctk.CTkButton(buttons, text="Save", command=self._save).pack(side="right")

    def _collect(self) -> DatabaseConfig:
        return DatabaseConfig.from_dict({key: entry.get().strip() for key, entry in self.entries.items()})

    def _test(self):
        self.lbl_test.configure(text="Testing...", text_color=MUTED)
        self.update_idletasks()
        ok, message = test_connection(self._collect())
        self.lbl_test.configure(text=("✔ " if ok else "✖ ") + message,
                                text_color=OK_COLOR if ok else ERROR_COLOR)

    def _clear(self):
        for entry in self.entries.values():
            entry.delete(0, "end")
        self.on_save(None)
        self.destroy()

    def _save(self):
        database = self._collect()
        self.on_save(database if database.is_usable or any(
            [database.host, database.user, database.database]) else None)
        self.destroy()


class ProfileRow(ctk.CTkFrame):
    """One profile inside an account card."""

    def __init__(self, master, profile: ProfileConfig, on_remove):
        super().__init__(master, fg_color=DARK_BG, corner_radius=6)
        self.profile = profile
        self.on_remove = on_remove

        self.enabled = ctk.CTkCheckBox(self, text="", width=28)
        self.enabled.pack(side="left", padx=(10, 4), pady=8)
        if profile.enabled:
            self.enabled.select()

        self.name = ctk.CTkEntry(self, placeholder_text="Profile name as shown in the game", width=260)
        self.name.insert(0, profile.name)
        self.name.pack(side="left", padx=4, pady=8)

        self.lbl_database = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=11),
                                         text_color=MUTED, anchor="w", width=250)
        self.lbl_database.pack(side="left", padx=8, fill="x", expand=True)

        ctk.CTkButton(self, text="Database…", width=80, fg_color="#21262d",
                      command=self._edit_database).pack(side="left", padx=4)
        ctk.CTkButton(self, text="✕", width=32, fg_color="#21262d", hover_color=ERROR_COLOR,
                      command=lambda: self.on_remove(self)).pack(side="left", padx=(4, 10))

        self._refresh_database_label()

    def _refresh_database_label(self):
        database = self.profile.database
        if database and database.is_usable:
            self.lbl_database.configure(text=f"{database.user}@{database.host}/{database.database}",
                                        text_color=MUTED)
        else:
            self.lbl_database.configure(text="no database — profile will be skipped", text_color=WARN_COLOR)

    def _edit_database(self):
        def apply(database):
            self.profile.database = database
            self._refresh_database_label()

        self.profile.name = self.name.get().strip()
        DatabaseDialog(self.winfo_toplevel(), self.profile, apply)

    def to_profile(self, account_name: str) -> ProfileConfig:
        return ProfileConfig(
            name=self.name.get().strip(),
            enabled=bool(self.enabled.get()),
            database=self.profile.database,
            account_name=account_name,
        )


class AccountCard(ctk.CTkFrame):
    """An account and the profiles reached through it."""

    def __init__(self, master, account: AccountConfig, on_remove):
        super().__init__(master, fg_color=CARD_BG, corner_radius=8)
        self.account = account
        self.on_remove = on_remove
        self.rows: List[ProfileRow] = []

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=12, pady=(12, 4))

        active = ctk.CTkFrame(header, fg_color="transparent")
        active.pack(side="left")
        ctk.CTkLabel(active, text="Active", font=ctk.CTkFont(size=11),
                     text_color=MUTED).pack(anchor="w")
        self.enabled = ctk.CTkCheckBox(active, text="", width=28)
        self.enabled.pack(anchor="w", pady=(6, 0))
        if account.enabled:
            self.enabled.select()

        self.name = labelled_entry(header, "Account name", 190,
                                   placeholder_text="a name only you see")
        self.name.insert(0, account.name)

        self.login = labelled_entry(header, "Game login (email or username)", 250,
                                    placeholder_text="what you type in the game")
        self.login.insert(0, account.login)

        password_column = ctk.CTkFrame(header, fg_color="transparent")
        password_column.pack(side="left", padx=6)
        ctk.CTkLabel(password_column, text="Password", font=ctk.CTkFont(size=11),
                     text_color=MUTED).pack(anchor="w")
        password_row = ctk.CTkFrame(password_column, fg_color="transparent")
        password_row.pack(anchor="w")
        self.password = ctk.CTkEntry(password_row, show="*", width=170,
                                     placeholder_text="game password")
        self.password.insert(0, account.password)
        self.password.pack(side="left")
        self.show_password = ctk.CTkButton(password_row, text="👁", width=36, fg_color="#21262d",
                                           command=self._toggle_password)
        self.show_password.pack(side="left", padx=(4, 0))

        ctk.CTkButton(header, text="Remove account", width=120, fg_color="#21262d",
                      hover_color=ERROR_COLOR,
                      command=lambda: self.on_remove(self)).pack(side="right", pady=(17, 0))

        second = ctk.CTkFrame(self, fg_color="transparent")
        second.pack(fill="x", padx=12, pady=(6, 0))
        self.browser_profile = labelled_entry(second, "Browser profile", 190,
                                              placeholder_text="empty = default profile")
        self.browser_profile.insert(0, account.browser_profile)
        ctk.CTkLabel(second,
                     text="Only needed for multiple accounts: each keeps its own session in a folder, "
                          "avoiding email verification codes.",
                     font=ctk.CTkFont(size=11), text_color=MUTED,
                     wraplength=560, justify="left").pack(side="left", padx=10, pady=(17, 0))

        ctk.CTkLabel(self, text="Profiles (cities) for this account, collected in this order:",
                     font=ctk.CTkFont(size=11), text_color=MUTED).pack(anchor="w", padx=14, pady=(10, 2))

        # Column names for the rows below, at the widths the rows themselves use.
        headings = ctk.CTkFrame(self, fg_color="transparent")
        headings.pack(fill="x", padx=12)
        for text, width, padding in (("Active", 28, (10, 4)),
                                     ("Profile name (exactly as the game shows it)", 260, (4, 4)),
                                     ("Database", 250, (8, 8))):
            ctk.CTkLabel(headings, text=text, width=width, anchor="w",
                         font=ctk.CTkFont(size=11), text_color=MUTED).pack(side="left", padx=padding)

        self.profiles_box = ctk.CTkFrame(self, fg_color="transparent")
        self.profiles_box.pack(fill="x", padx=12, pady=(0, 6))

        for profile in account.profiles:
            self._add_row(profile)

        ctk.CTkButton(self, text="+ Add profile", height=28, width=150, fg_color="#21262d",
                      command=lambda: self._add_row(ProfileConfig(account_name=account.name))
                      ).pack(anchor="w", padx=14, pady=(0, 12))

    def _toggle_password(self):
        self.password.configure(show="" if self.password.cget("show") else "*")

    def _add_row(self, profile: ProfileConfig):
        row = ProfileRow(self.profiles_box, profile, self._remove_row)
        row.pack(fill="x", pady=3)
        self.rows.append(row)

    def _remove_row(self, row: ProfileRow):
        self.rows.remove(row)
        row.destroy()

    def to_account(self) -> AccountConfig:
        name = self.name.get().strip()
        return AccountConfig(
            name=name,
            login=self.login.get().strip(),
            password=self.password.get(),
            enabled=bool(self.enabled.get()),
            browser_profile=self.browser_profile.get().strip(),
            profiles=[row.to_profile(name) for row in self.rows if row.name.get().strip()],
        )


class App(ctk.CTk):
    """The configuration window."""

    def __init__(self, config: ConfigManager, open_calibration: bool = False):
        super().__init__()
        self.config_manager = config
        self.calibration = Calibration()
        self.context = None
        self.log_queue: queue.Queue = queue.Queue()
        self.widgets: Dict[str, Dict[str, object]] = {}
        self.cards: List[AccountCard] = []
        self.worker: Optional[threading.Thread] = None

        self.title("Total Battle Chest Collector — Configuration")
        self.geometry("1180x820")
        self.minsize(1000, 700)

        self._build()
        logger.addHandler(LogPipe(self.log_queue))
        self.after(150, self._drain_log)
        if open_calibration:
            self.after(400, self.open_calibration)

    # ------------------------------------------------------------------- UI
    def _build(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(14, 4))
        ctk.CTkLabel(header, text="Total Battle Chest Collector",
                     font=ctk.CTkFont(size=20, weight="bold"), text_color=ACCENT).pack(side="left")
        self.lbl_status = ctk.CTkLabel(header, text="", font=ctk.CTkFont(size=12), text_color=MUTED)
        self.lbl_status.pack(side="right")

        self.tabs = ctk.CTkTabview(self, fg_color=PANEL_BG)
        self.tabs.pack(fill="both", expand=True, padx=16, pady=(4, 8))
        self.tabs.add("Execution")
        self.tabs.add("Parameters")
        self.tabs.add("Accounts and profiles")

        self._build_run_tab(self.tabs.tab("Execution"))
        self._build_parameters_tab(self.tabs.tab("Parameters"))
        self._build_accounts_tab(self.tabs.tab("Accounts and profiles"))
        self._refresh_status()

    # -- run tab
    def _build_run_tab(self, parent):
        top = ctk.CTkFrame(parent, fg_color="transparent")
        top.pack(fill="x", padx=8, pady=(10, 6))

        ctk.CTkButton(top, text="1 · Open game in Chrome", width=200, height=38,
                      command=self.connect_browser).pack(side="left", padx=4)
        ctk.CTkButton(top, text="2 · Calibrate", width=150, height=38,
                      command=self.open_calibration).pack(side="left", padx=4)
        ctk.CTkButton(top, text="3 · Verify configuration", width=200, height=38,
                      fg_color="#21262d", command=self.check_configuration).pack(side="left", padx=4)
        self.btn_run = ctk.CTkButton(top, text="▶ Run collection now", width=210, height=38,
                                     fg_color="#238636", hover_color="#2ea043", command=self.run_collection)
        self.btn_run.pack(side="right", padx=4)
        self.btn_stop = ctk.CTkButton(top, text="■ Stop", width=100, height=38, state="disabled",
                                      fg_color="#21262d", hover_color=ERROR_COLOR, command=self.stop_run)
        self.btn_stop.pack(side="right", padx=4)

        info = ctk.CTkFrame(parent, fg_color=CARD_BG, corner_radius=8)
        info.pack(fill="x", padx=8, pady=(4, 8))
        self.lbl_info = ctk.CTkLabel(info, text="", justify="left", anchor="w",
                                     font=ctk.CTkFont(size=12), text_color="#c9d1d9")
        self.lbl_info.pack(fill="x", padx=14, pady=12)

        ctk.CTkLabel(parent, text="Execution Log", font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=MUTED).pack(anchor="w", padx=12, pady=(4, 2))
        self.log_box = ctk.CTkTextbox(parent, fg_color=DARK_BG, font=ctk.CTkFont(family="Consolas", size=11))
        self.log_box.pack(fill="both", expand=True, padx=8, pady=(0, 10))

        footer = ctk.CTkFrame(parent, fg_color="transparent")
        footer.pack(fill="x", padx=8, pady=(0, 10))
        ctk.CTkLabel(footer,
                     text="Hold ESC to cancel an ongoing collection from any window.  ·  To schedule, point Windows Task Scheduler to run.bat.",
                     font=ctk.CTkFont(size=11), text_color=MUTED).pack(side="left")
        ctk.CTkButton(footer, text="Open logs folder", width=150, fg_color="#21262d",
                      command=lambda: webbrowser.open(self.config_manager.resolved_log_dir())
                      ).pack(side="right")

    # -- parameters tab
    def _build_parameters_tab(self, parent):
        toolbar = ctk.CTkFrame(parent, fg_color="transparent")
        toolbar.pack(fill="x", padx=8, pady=(10, 4))
        ctk.CTkLabel(toolbar, text="All application parameters. Hover over descriptions to understand each one.",
                     font=ctk.CTkFont(size=12), text_color=MUTED).pack(side="left")
        ctk.CTkButton(toolbar, text="Restore defaults", width=160, fg_color="#21262d",
                      command=self.restore_defaults).pack(side="right", padx=4)
        ctk.CTkButton(toolbar, text="💾 Save parameters", width=180,
                      command=self.save_parameters).pack(side="right", padx=4)

        scroll = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=8, pady=(0, 10))

        for section in SECTIONS:
            block = ctk.CTkFrame(scroll, fg_color=CARD_BG, corner_radius=8)
            block.pack(fill="x", pady=6)
            ctk.CTkLabel(block, text=section.label, font=ctk.CTkFont(size=15, weight="bold"),
                         text_color=ACCENT).pack(anchor="w", padx=14, pady=(12, 0))
            ctk.CTkLabel(block, text=section.description, font=ctk.CTkFont(size=11),
                         text_color=MUTED, wraplength=980, justify="left").pack(anchor="w", padx=14, pady=(2, 8))

            self.widgets[section.key] = {}
            for spec in section.fields:
                self._build_field(block, section.key, spec)

    def _build_field(self, parent, section_key: str, spec):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=3)

        ctk.CTkLabel(row, text=spec.label, width=260, anchor="w",
                     font=ctk.CTkFont(size=12)).pack(side="left")

        value = self.config_manager.get(section_key, spec.key, spec.default)
        if spec.type == "bool":
            widget = ctk.CTkCheckBox(row, text="", width=30)
            if value:
                widget.select()
            widget.pack(side="left")
        elif spec.type == "choice":
            widget = ctk.CTkOptionMenu(row, values=list(spec.choices), width=190)
            widget.set(str(value))
            widget.pack(side="left")
        else:
            widget = ctk.CTkEntry(row, width=320, show="*" if spec.is_secret else "")
            widget.insert(0, "" if value is None else str(value))
            widget.pack(side="left")

        if spec.help:
            ctk.CTkLabel(row, text=spec.help, font=ctk.CTkFont(size=11), text_color=MUTED,
                         wraplength=520, justify="left", anchor="w").pack(side="left", padx=12, fill="x", expand=True)

        self.widgets[section_key][spec.key] = widget

    # -- accounts tab
    def _build_accounts_tab(self, parent):
        toolbar = ctk.CTkFrame(parent, fg_color="transparent")
        toolbar.pack(fill="x", padx=8, pady=(10, 4))
        ctk.CTkLabel(toolbar,
                     text="Each account is a game login; each profile is a city within it. "
                          "Collection follows this order: account, then its profiles.",
                     font=ctk.CTkFont(size=12), text_color=MUTED, wraplength=700,
                     justify="left").pack(side="left")
        ctk.CTkButton(toolbar, text="+ New account", width=140, fg_color="#21262d",
                      command=self.add_account).pack(side="right", padx=4)
        ctk.CTkButton(toolbar, text="💾 Save accounts", width=160,
                      command=self.save_accounts).pack(side="right", padx=4)

        self.accounts_box = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        self.accounts_box.pack(fill="both", expand=True, padx=8, pady=(0, 10))

        for account in self.config_manager.accounts:
            self._add_card(account)

    def _add_card(self, account: AccountConfig):
        card = AccountCard(self.accounts_box, account, self._remove_card)
        card.pack(fill="x", pady=6)
        self.cards.append(card)

    def _remove_card(self, card: AccountCard):
        if not messagebox.askyesno("Remove account", "Remove this account and all of its profiles?"):
            return
        self.cards.remove(card)
        card.destroy()

    def add_account(self):
        self._add_card(AccountConfig(name=f"Account {len(self.cards) + 1}", profiles=[ProfileConfig()]))

    # -------------------------------------------------------------- actions
    def save_parameters(self):
        for section_key, fields in self.widgets.items():
            for key, widget in fields.items():
                if isinstance(widget, ctk.CTkCheckBox):
                    value = bool(widget.get())
                elif isinstance(widget, ctk.CTkOptionMenu):
                    value = widget.get()
                else:
                    value = widget.get()
                self.config_manager.set(section_key, key, value)
        self.config_manager.save()
        self._refresh_status()
        messagebox.showinfo("Configuration", "Parameters saved to config/config.json.")

    def save_accounts(self):
        accounts = [card.to_account() for card in self.cards if card.name.get().strip()]
        without_database = [
            f"{a.name}/{p.name}" for a in accounts for p in a.profiles if not p.has_database
        ]
        self.config_manager.accounts = accounts
        self.config_manager.save()
        self._refresh_status()

        message = f"{len(accounts)} account(s) saved to config/config.json."
        if without_database:
            message += ("\n\nProfiles without a database (will be skipped during collection):\n  "
                        + "\n  ".join(without_database))
        messagebox.showinfo("Accounts and profiles", message)

    def restore_defaults(self):
        if not messagebox.askyesno("Restore defaults",
                                   "Reset all parameters to default values?\n"
                                   "Accounts and calibration are not affected."):
            return
        from config.schema import default_config

        defaults = default_config()
        for section in SECTIONS:
            for spec in section.fields:
                self.config_manager.set(section.key, spec.key, defaults[section.key][spec.key])
                widget = self.widgets[section.key][spec.key]
                if isinstance(widget, ctk.CTkCheckBox):
                    widget.select() if spec.default else widget.deselect()
                elif isinstance(widget, ctk.CTkOptionMenu):
                    widget.set(str(spec.default))
                else:
                    widget.delete(0, "end")
                    widget.insert(0, str(spec.default))
        self.config_manager.save()
        self._refresh_status()

    def _ensure_context(self, connect: bool = True):
        from core.context import build_context

        if self.context is None:
            self.context = build_context(self.config_manager, self.calibration)
        if connect and self.context.browser.ws is None:
            self.context.start_browser()
        return self.context

    def connect_browser(self):
        def work():
            try:
                self._ensure_context(connect=True)
                self.context.browser.focus_tab()
                logger.info("Game opened and connected. Log in and navigate to the screen you want to calibrate.")
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Could not open the game: {exc}")

        self._run_in_background(work)

    def open_calibration(self):
        try:
            context = self._ensure_context(connect=True)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Calibration",
                                 f"Must be connected to the game to calibrate.\n\n{exc}")
            return
        CalibrationWizard(self, context, on_done=lambda _complete: self._refresh_status())

    def check_configuration(self):
        def work():
            from core.runner import CollectorRunner

            context = self._ensure_context(connect=False)
            problems = CollectorRunner(context).preflight()
            if problems:
                for problem in problems:
                    logger.error(problem)
            else:
                logger.info("Configuration and calibration are complete — ready to run.")

        self._run_in_background(work)

    def run_collection(self):
        if not messagebox.askyesno("Run collection",
                                   "Run collection now with saved configuration?\n\n"
                                   "The browser will be automated."):
            return

        def work():
            from core.runner import CollectorRunner, RunnerError

            context = self._ensure_context(connect=False)
            runner = CollectorRunner(context)
            try:
                summary = runner.run()
                logger.info(
                    f"Collection finished: {summary['collected']} chests, "
                    f"{len(summary['failures'])} failure(s), duration {summary['duration']}."
                )
            except Cancelled as exc:
                logger.warning(f"Execution cancelled ({exc}). Already collected chests remain saved.")
            except RunnerError as exc:
                logger.error(str(exc))
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"Unexpected failure: {exc}")
            finally:
                try:
                    context.close()
                except Exception:
                    pass
                self.context = None

        cancellation.reset()
        escape_watcher.start()
        self._run_in_background(work, busy_text="⏳ Collecting...", stoppable=True)

    # ---------------------------------------------------------------- helpers
    def stop_run(self):
        """Same cancellation the Escape key triggers, for whoever prefers the button."""
        cancellation.cancel("stop requested from interface")
        self.btn_stop.configure(state="disabled", text="stopping...")
        logger.warning("Cancellation requested; stopping at next safe point...")

    def _run_in_background(self, target, busy_text: str = "⏳ Running...", stoppable: bool = False):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Please wait", "An operation is already in progress.")
            return

        original = self.btn_run.cget("text")
        self.btn_run.configure(state="disabled", text=busy_text)
        if stoppable:
            self.btn_stop.configure(state="normal", text="■ Stop")
        self.tabs.set("Execution")

        def wrapper():
            try:
                target()
            finally:
                escape_watcher.stop()
                self.after(0, lambda: self.btn_run.configure(state="normal", text=original))
                self.after(0, lambda: self.btn_stop.configure(state="disabled", text="■ Stop"))
                self.after(0, self._refresh_status)

        self.worker = threading.Thread(target=wrapper, daemon=True)
        self.worker.start()

    def _drain_log(self):
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_box.insert("end", line + "\n")
            self.log_box.see("end")
        self.after(200, self._drain_log)

    def _refresh_status(self):
        self.calibration.load()
        accounts = self.config_manager.accounts
        profiles = [p for a in accounts for p in a.profiles]
        collectable = [p for p in profiles if p.has_database]
        missing = self.calibration.missing_steps()

        if missing:
            calibration_text = f"✖ Incomplete calibration ({len(missing)} step(s) missing)"
            color = ERROR_COLOR
        else:
            calibration_text = f"✔ Calibrated on {self.calibration.created_at or 'unknown date'}"
            color = OK_COLOR
        self.lbl_status.configure(text=calibration_text, text_color=color)

        engine = "not verified"
        if self.context is not None:
            ok, reason = self.context.ocr.status()
            engine = reason if ok else f"unavailable ({reason})"
        else:
            try:
                from core.ocr import RapidOCRBackend

                available, reason = RapidOCRBackend.available()
                engine = reason if available else f"RapidOCR unavailable — {reason}"
            except Exception:
                pass

        self.lbl_info.configure(text=(
            f"Registered accounts: {len(accounts)}   ·   profiles: {len(profiles)} "
            f"({len(collectable)} with configured database)\n"
            f"Calibration: {calibration_text}   ·   calibrated viewport: "
            f"{self.calibration.viewport[0]}x{self.calibration.viewport[1]}\n"
            f"OCR: {engine}\n"
            f"Configuration: {self.config_manager.path}"
        ))


def launch(config: Optional[ConfigManager] = None, open_calibration: bool = False):
    app = App(config or ConfigManager(), open_calibration=open_calibration)
    app.mainloop()
