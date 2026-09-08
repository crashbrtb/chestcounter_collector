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
        ("port", "Porta", False),
        ("user", "Usuário", False),
        ("password", "Senha", True),
        ("database", "Banco", False),
    )

    def __init__(self, master, profile: ProfileConfig, on_save):
        super().__init__(master)
        self.profile = profile
        self.on_save = on_save
        self.entries: Dict[str, ctk.CTkEntry] = {}

        self.title(f"Banco de dados — {profile.name or 'novo perfil'}")
        self.geometry("460x420")
        self.transient(master)
        self.grab_set()

        database = profile.database or DatabaseConfig()
        ctk.CTkLabel(self, text="Credenciais do banco deste perfil",
                     font=ctk.CTkFont(size=15, weight="bold"), text_color=ACCENT).pack(pady=(16, 2))
        ctk.CTkLabel(self, text="Um perfil sem banco é ignorado na coleta.",
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
        ctk.CTkButton(buttons, text="Testar conexão", fg_color="#21262d",
                      command=self._test).pack(side="left")
        ctk.CTkButton(buttons, text="Limpar banco", fg_color="#21262d",
                      command=self._clear).pack(side="left", padx=8)
        ctk.CTkButton(buttons, text="Salvar", command=self._save).pack(side="right")

    def _collect(self) -> DatabaseConfig:
        return DatabaseConfig.from_dict({key: entry.get().strip() for key, entry in self.entries.items()})

    def _test(self):
        self.lbl_test.configure(text="Testando...", text_color=MUTED)
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

        self.name = ctk.CTkEntry(self, placeholder_text="Nome do perfil como aparece no jogo", width=260)
        self.name.insert(0, profile.name)
        self.name.pack(side="left", padx=4, pady=8)

        self.lbl_database = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=11),
                                         text_color=MUTED, anchor="w", width=250)
        self.lbl_database.pack(side="left", padx=8, fill="x", expand=True)

        ctk.CTkButton(self, text="Banco…", width=80, fg_color="#21262d",
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
            self.lbl_database.configure(text="sem banco — perfil será ignorado", text_color=WARN_COLOR)

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

        self.enabled = ctk.CTkCheckBox(header, text="", width=28)
        self.enabled.pack(side="left")
        if account.enabled:
            self.enabled.select()

        self.name = ctk.CTkEntry(header, placeholder_text="Apelido da conta", width=190)
        self.name.insert(0, account.name)
        self.name.pack(side="left", padx=6)

        self.login = ctk.CTkEntry(header, placeholder_text="E-mail / usuário do jogo", width=250)
        self.login.insert(0, account.login)
        self.login.pack(side="left", padx=6)

        self.password = ctk.CTkEntry(header, placeholder_text="Senha", show="*", width=170)
        self.password.insert(0, account.password)
        self.password.pack(side="left", padx=6)

        self.show_password = ctk.CTkButton(header, text="👁", width=36, fg_color="#21262d",
                                           command=self._toggle_password)
        self.show_password.pack(side="left")

        ctk.CTkButton(header, text="Remover conta", width=120, fg_color="#21262d",
                      hover_color=ERROR_COLOR,
                      command=lambda: self.on_remove(self)).pack(side="right")

        # Só faz falta com mais de uma conta: a sessão não é apagada entre elas
        # (isso custaria um código por e-mail), então cada conta precisa do seu
        # próprio perfil de navegador para manter a própria sessão verificada.
        second = ctk.CTkFrame(self, fg_color="transparent")
        second.pack(fill="x", padx=12, pady=(2, 0))
        ctk.CTkLabel(second, text="Perfil do navegador", font=ctk.CTkFont(size=11),
                     text_color=MUTED).pack(side="left", padx=(30, 6))
        self.browser_profile = ctk.CTkEntry(second, width=170, font=ctk.CTkFont(size=11),
                                            placeholder_text="vazio = perfil padrão")
        self.browser_profile.insert(0, account.browser_profile)
        self.browser_profile.pack(side="left")
        ctk.CTkLabel(second,
                     text="Só preencha se tiver mais de uma conta: cada uma guarda a própria "
                          "sessão numa pasta, evitando o código de verificação por e-mail.",
                     font=ctk.CTkFont(size=11), text_color=MUTED,
                     wraplength=560, justify="left").pack(side="left", padx=10)

        ctk.CTkLabel(self, text="Perfis (cidades) desta conta, coletados nesta ordem:",
                     font=ctk.CTkFont(size=11), text_color=MUTED).pack(anchor="w", padx=14, pady=(6, 2))

        self.profiles_box = ctk.CTkFrame(self, fg_color="transparent")
        self.profiles_box.pack(fill="x", padx=12, pady=(0, 6))

        for profile in account.profiles:
            self._add_row(profile)

        ctk.CTkButton(self, text="+ Adicionar perfil", height=28, width=150, fg_color="#21262d",
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

        self.title("Total Battle Chest Collector — Configuração")
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
        self.tabs.add("Execução")
        self.tabs.add("Parâmetros")
        self.tabs.add("Contas e perfis")

        self._build_run_tab(self.tabs.tab("Execução"))
        self._build_parameters_tab(self.tabs.tab("Parâmetros"))
        self._build_accounts_tab(self.tabs.tab("Contas e perfis"))
        self._refresh_status()

    # -- run tab
    def _build_run_tab(self, parent):
        top = ctk.CTkFrame(parent, fg_color="transparent")
        top.pack(fill="x", padx=8, pady=(10, 6))

        ctk.CTkButton(top, text="1 · Abrir o jogo no Chrome", width=200, height=38,
                      command=self.connect_browser).pack(side="left", padx=4)
        ctk.CTkButton(top, text="2 · Calibrar", width=150, height=38,
                      command=self.open_calibration).pack(side="left", padx=4)
        ctk.CTkButton(top, text="3 · Verificar configuração", width=200, height=38,
                      fg_color="#21262d", command=self.check_configuration).pack(side="left", padx=4)
        self.btn_run = ctk.CTkButton(top, text="▶ Executar coleta agora", width=210, height=38,
                                     fg_color="#238636", hover_color="#2ea043", command=self.run_collection)
        self.btn_run.pack(side="right", padx=4)
        self.btn_stop = ctk.CTkButton(top, text="■ Parar", width=100, height=38, state="disabled",
                                      fg_color="#21262d", hover_color=ERROR_COLOR, command=self.stop_run)
        self.btn_stop.pack(side="right", padx=4)

        info = ctk.CTkFrame(parent, fg_color=CARD_BG, corner_radius=8)
        info.pack(fill="x", padx=8, pady=(4, 8))
        self.lbl_info = ctk.CTkLabel(info, text="", justify="left", anchor="w",
                                     font=ctk.CTkFont(size=12), text_color="#c9d1d9")
        self.lbl_info.pack(fill="x", padx=14, pady=12)

        ctk.CTkLabel(parent, text="Log da execução", font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=MUTED).pack(anchor="w", padx=12, pady=(4, 2))
        self.log_box = ctk.CTkTextbox(parent, fg_color=DARK_BG, font=ctk.CTkFont(family="Consolas", size=11))
        self.log_box.pack(fill="both", expand=True, padx=8, pady=(0, 10))

        footer = ctk.CTkFrame(parent, fg_color="transparent")
        footer.pack(fill="x", padx=8, pady=(0, 10))
        ctk.CTkLabel(footer,
                     text="Segure ESC para cancelar uma execução em andamento, de qualquer janela.  ·  Para agendar, aponte o Agendador de Tarefas para o run.bat.",
                     font=ctk.CTkFont(size=11), text_color=MUTED).pack(side="left")
        ctk.CTkButton(footer, text="Abrir pasta de logs", width=150, fg_color="#21262d",
                      command=lambda: webbrowser.open(self.config_manager.resolved_log_dir())
                      ).pack(side="right")

    # -- parameters tab
    def _build_parameters_tab(self, parent):
        toolbar = ctk.CTkFrame(parent, fg_color="transparent")
        toolbar.pack(fill="x", padx=8, pady=(10, 4))
        ctk.CTkLabel(toolbar, text="Todos os parâmetros da aplicação. Passe o mouse nas descrições "
                                   "para entender cada um.",
                     font=ctk.CTkFont(size=12), text_color=MUTED).pack(side="left")
        ctk.CTkButton(toolbar, text="Restaurar padrões", width=160, fg_color="#21262d",
                      command=self.restore_defaults).pack(side="right", padx=4)
        ctk.CTkButton(toolbar, text="💾 Salvar parâmetros", width=180,
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
                     text="Cada conta é um login do jogo; cada perfil é uma cidade dentro dela. "
                          "A coleta segue esta ordem: conta, depois seus perfis.",
                     font=ctk.CTkFont(size=12), text_color=MUTED, wraplength=700,
                     justify="left").pack(side="left")
        ctk.CTkButton(toolbar, text="+ Nova conta", width=140, fg_color="#21262d",
                      command=self.add_account).pack(side="right", padx=4)
        ctk.CTkButton(toolbar, text="💾 Salvar contas", width=160,
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
        if not messagebox.askyesno("Remover conta", "Remover esta conta e todos os seus perfis?"):
            return
        self.cards.remove(card)
        card.destroy()

    def add_account(self):
        self._add_card(AccountConfig(name=f"Conta {len(self.cards) + 1}", profiles=[ProfileConfig()]))

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
        messagebox.showinfo("Configuração", "Parâmetros salvos em config/config.json.")

    def save_accounts(self):
        accounts = [card.to_account() for card in self.cards if card.name.get().strip()]
        without_database = [
            f"{a.name}/{p.name}" for a in accounts for p in a.profiles if not p.has_database
        ]
        self.config_manager.accounts = accounts
        self.config_manager.save()
        self._refresh_status()

        message = f"{len(accounts)} conta(s) salva(s) em config/config.json."
        if without_database:
            message += ("\n\nPerfis sem banco de dados (serão ignorados na coleta):\n  "
                        + "\n  ".join(without_database))
        messagebox.showinfo("Contas e perfis", message)

    def restore_defaults(self):
        if not messagebox.askyesno("Restaurar padrões",
                                   "Voltar todos os parâmetros aos valores padrão?\n"
                                   "As contas e a calibração não são afetadas."):
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
                logger.info("Jogo aberto e conectado. Faça login e navegue até a tela que quer calibrar.")
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Não foi possível abrir o jogo: {exc}")

        self._run_in_background(work)

    def open_calibration(self):
        try:
            context = self._ensure_context(connect=True)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Calibração",
                                 f"É preciso estar conectado ao jogo para calibrar.\n\n{exc}")
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
                logger.info("Configuração e calibração completas — pronto para executar.")

        self._run_in_background(work)

    def run_collection(self):
        if not messagebox.askyesno("Executar coleta",
                                   "Executar a coleta agora, com a configuração salva?\n\n"
                                   "O navegador será controlado automaticamente."):
            return

        def work():
            from core.runner import CollectorRunner, RunnerError

            context = self._ensure_context(connect=False)
            runner = CollectorRunner(context)
            try:
                summary = runner.run()
                logger.info(
                    f"Coleta concluída: {summary['collected']} baús, "
                    f"{len(summary['failures'])} falha(s), duração {summary['duration']}."
                )
            except Cancelled as exc:
                logger.warning(f"Execução cancelada ({exc}). O que já foi coletado está gravado.")
            except RunnerError as exc:
                logger.error(str(exc))
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"Falha inesperada: {exc}")
            finally:
                try:
                    context.close()
                except Exception:
                    pass
                self.context = None

        cancellation.reset()
        escape_watcher.start()
        self._run_in_background(work, busy_text="⏳ Coletando...", stoppable=True)

    # ---------------------------------------------------------------- helpers
    def stop_run(self):
        """Same cancellation the Escape key triggers, for whoever prefers the button."""
        cancellation.cancel("parada pedida na interface")
        self.btn_stop.configure(state="disabled", text="parando...")
        logger.warning("Cancelamento pedido; encerrando no próximo ponto seguro...")

    def _run_in_background(self, target, busy_text: str = "⏳ Executando...", stoppable: bool = False):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Aguarde", "Já existe uma operação em andamento.")
            return

        original = self.btn_run.cget("text")
        self.btn_run.configure(state="disabled", text=busy_text)
        if stoppable:
            self.btn_stop.configure(state="normal", text="■ Parar")
        self.tabs.set("Execução")

        def wrapper():
            try:
                target()
            finally:
                escape_watcher.stop()
                self.after(0, lambda: self.btn_run.configure(state="normal", text=original))
                self.after(0, lambda: self.btn_stop.configure(state="disabled", text="■ Parar"))
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
            calibration_text = f"✖ calibração incompleta ({len(missing)} passo(s) faltando)"
            color = ERROR_COLOR
        else:
            calibration_text = f"✔ calibrada em {self.calibration.created_at or 'data desconhecida'}"
            color = OK_COLOR
        self.lbl_status.configure(text=calibration_text, text_color=color)

        engine = "não verificado"
        if self.context is not None:
            ok, reason = self.context.ocr.status()
            engine = reason if ok else f"indisponível ({reason})"
        else:
            try:
                from core.ocr import RapidOCRBackend

                available, reason = RapidOCRBackend.available()
                engine = reason if available else f"RapidOCR indisponível — {reason}"
            except Exception:
                pass

        self.lbl_info.configure(text=(
            f"Contas cadastradas: {len(accounts)}   ·   perfis: {len(profiles)} "
            f"({len(collectable)} com banco configurado)\n"
            f"Calibração: {calibration_text}   ·   viewport calibrado: "
            f"{self.calibration.viewport[0]}x{self.calibration.viewport[1]}\n"
            f"OCR: {engine}\n"
            f"Configuração: {self.config_manager.path}"
        ))


def launch(config: Optional[ConfigManager] = None, open_calibration: bool = False):
    app = App(config or ConfigManager(), open_calibration=open_calibration)
    app.mainloop()
