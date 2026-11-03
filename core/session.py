"""
Who is playing: logging in to an account, and moving between its profiles.

The old collector assumed the game was already logged in and only swapped
cities. That made a single account a hard limit, and it made the run depend on
whatever session happened to be left in the client. Here an account is a real
login, and the order is explicit: authenticate, then walk that account's
profiles, then end the session and move to the next account.

Logging in happens through the page's own form, driven by CDP. The fields are
found by their markup and then clicked and typed into with real input events -
not filled by assigning `.value`, which a login form built on a JS framework
ignores because no keystroke ever happened.

The session is kept, not erased
-------------------------------
An earlier version cleared cookies and storage before each account, to be sure
of who was logged in. The game reads that as a brand-new device and mails a
verification code - which an unattended nightly run has no way to answer, so
every run stopped at the code screen.

So the browser profile is left alone and each run simply asks the page where it
stands: already authenticated, or showing the login screen. Logging in becomes
the exception rather than the routine.

That is what `AccountConfig.browser_profile` is for - one Chrome folder per
account, each keeping its own verified session - and the runner refuses to start
rather than let two accounts share one, which would collect the second account's
chests into the first account's database.

The first login of a folder is a person's
-----------------------------------------
On a folder the game has not seen, it mails a code. Typing the password there
would only send e-mails nobody will answer, so the form is not touched: the account
stops, says so, and the others go on. Once the game has come up in a folder a small
file there says so (`SESSION_MARK`), and from then on a dropped session is logged
back into with the password, which the game accepts without a code.
"""

import json
import os
import time
from typing import Any, Sequence, Tuple

from config.settings import AccountConfig, ProfileConfig
from utils.cancel import cancellation
from utils.logger import logger
from utils.text_utils import normalize_name

# Shared helpers for the page-side scripts below.
#
# `visible` is measured, not inferred: the login form of totalbattle.com is in
# the markup from the first paint and only becomes reachable after the Login
# button is pressed. Existing in the DOM therefore says nothing about whether it
# can be typed into - the rectangle does.
_JS_HELPERS = r"""
  const visible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  const label = (el) => ((el.textContent || el.value || el.getAttribute('aria-label') || '')
                         .replace(/\s+/g, ' ').trim());
  // 'Entrar com o Google' opens someone else's login page, and clicking it
  // would strand the run on a consent screen.
  const SOCIAL = /google|facebook|apple|vkontakte|\bvk\b|twitter|discord|steam|yandex|ok\.ru|huawei/i;
  const LOGIN_WORD = /log ?in|sign ?in|entrar|acessar/i;
  // 'Log in with a code' and 'Forgot password?' also talk about logging in, and
  // both lead somewhere this automation cannot follow.
  const OTHER_ROUTE = /code|c[oó]digo|forgot|esqueci|recuperar|sign ?up|cadastr|registr/i;
  const TEXT_INPUT = (i) => !['checkbox', 'radio', 'hidden', 'submit', 'button', 'file', 'range',
                              'image', 'reset', 'color'].includes(i.type);
  // The clickable thing is not always a <button>: this page builds them out of
  // divs and spans, which is why the search cannot be limited by tag name.
  const CLICKABLE = 'button, a, [role=button], input[type=submit], input[type=button], div, span, li';

  // Keeps only the innermost matches. A container inherits the text of
  // everything inside it, so without this the whole modal would look like a
  // login button because a login button lives somewhere in it.
  const innermost = (els) => els.filter(el => !els.some(other => other !== el && el.contains(other)));
"""

# Marks the login fields so they get stable selectors, whatever the site calls
# its inputs today.
MARK_LOGIN_FIELDS_JS = r"""
(() => {
""" + _JS_HELPERS + r"""
  const attr = (el) => [el.type, el.name, el.id, el.placeholder, el.autocomplete,
                        el.getAttribute('aria-label')].join(' ').toLowerCase();

  document.querySelectorAll('[data-tbc-role]').forEach(el => el.removeAttribute('data-tbc-role'));

  const password = Array.from(document.querySelectorAll('input[type=password]')).filter(visible)[0];
  if (!password) return {password: false, email: false, submit: false, scope: ''};
  password.setAttribute('data-tbc-role', 'password');

  // Everything else is looked for INSIDE the box that holds the password field.
  //
  // totalbattle.com shows a sign-up panel and the log-in modal at the same time,
  // and the sign-up panel has its own visible 'Email' input which comes FIRST in
  // the document. Searching the whole page found that one: the address went into
  // the registration form while the password went into the login modal, and the
  // login was never sent.
  //
  // The box is the smallest ancestor of the password field that also holds a
  // text input and something that says 'log in' - which is the modal, and never
  // the panel next to it.
  let scope = password.closest('form');
  if (!scope) {
    let node = password.parentElement;
    while (node && node !== document.documentElement) {
      const fields = Array.from(node.querySelectorAll('input'))
                          .filter(i => i !== password && visible(i) && TEXT_INPUT(i));
      const actions = Array.from(node.querySelectorAll(CLICKABLE))
                           .filter(el => visible(el) && LOGIN_WORD.test(label(el))
                                         && !SOCIAL.test(label(el)) && !OTHER_ROUTE.test(label(el)));
      if (fields.length && actions.length) { scope = node; break; }
      node = node.parentElement;
    }
  }
  scope = scope || document;

  const textual = Array.from(scope.querySelectorAll('input'))
                       .filter(i => i !== password && visible(i) && TEXT_INPUT(i));
  const email = textual.find(i => i.type === 'email')
             || textual.find(i => /e-?mail|login|user|usuario|conta/.test(attr(i)))
             || textual.filter(i => password.compareDocumentPosition(i) & Node.DOCUMENT_POSITION_PRECEDING).pop()
             || textual[0];
  if (email) email.setAttribute('data-tbc-role', 'email');

  const actions = innermost(Array.from(scope.querySelectorAll(CLICKABLE))
      .filter(el => visible(el) && !SOCIAL.test(label(el)) && label(el).length <= 40));
  // Shortest label first, so the 'Login' button wins over 'Log in with a code'.
  const worded = actions.filter(el => LOGIN_WORD.test(label(el)) && !OTHER_ROUTE.test(label(el)))
                        .sort((a, b) => label(a).length - label(b).length);
  // No blind fallback to 'the first button': that was as likely to be a close
  // icon as a submit. With nothing recognisable, Enter submits the form.
  const submit = worded[0] || actions.find(b => b.type === 'submit');
  if (submit) submit.setAttribute('data-tbc-role', 'submit');

  return {
    password: true,
    email: !!email,
    submit: !!submit,
    scope: scope === document ? 'whole page' : (scope.tagName || '') + (scope.className ? '.' + String(scope.className).split(' ')[0] : ''),
    submit_label: submit ? label(submit).slice(0, 30) : '',
    email_name: email ? (email.name || email.placeholder || '') : '',
  };
})()
"""

SESSION_MARK = "tb_counter_session_verified"

# Where the page stands: form reachable, form present but closed, or logged in.
LOGIN_STATE_JS = r"""
(() => {
""" + _JS_HELPERS + r"""
  document.querySelectorAll('[data-tbc-role="open-login"]').forEach(
      el => el.removeAttribute('data-tbc-role'));

  const passwords = Array.from(document.querySelectorAll('input[type=password]'));
  const openForm = passwords.some(visible);

  let opener = null;
  if (!openForm) {
    // Not restricted to <button> and <a>: the header entry here is a styled div,
    // and looking only at real buttons found nothing at all on this page.
    const candidates = innermost(Array.from(document.querySelectorAll(CLICKABLE))
        .filter(el => visible(el) && LOGIN_WORD.test(label(el))
                      && !SOCIAL.test(label(el)) && !OTHER_ROUTE.test(label(el))
                      && label(el).length <= 40));
    // Shortest label first: 'Log in' is the button, while
    // 'Log in to claim your reward' is a sentence that merely contains it.
    candidates.sort((a, b) => label(a).length - label(b).length);
    opener = candidates[0] || null;
    if (opener) opener.setAttribute('data-tbc-role', 'open-login');
  }

  const canvas = Array.from(document.querySelectorAll('canvas')).some(visible);
  // By how such fields are named, not measured on the game's own screen. The game
  // being up rules it out, since it may have fields of its own that mention codes.
  const CODE_FIELD = /verif|c[oó]digo|\bcode\b|otp|2fa|one.?time/i;
  const codeField = Array.from(document.querySelectorAll('input')).some(el =>
      visible(el) && TEXT_INPUT(el) && el.type !== 'password'
      && (el.autocomplete === 'one-time-code'
          || CODE_FIELD.test([el.name, el.id, el.placeholder, el.getAttribute('aria-label') || ''].join(' '))));

  return {
    form_open: openForm,
    form_in_dom: passwords.length > 0,
    opener: opener ? label(opener).slice(0, 40) : "",
    canvas: canvas,
    code_asked: codeField && !canvas,
  };
})()
"""


class SessionError(RuntimeError):
    pass


class NeedsPerson(SessionError):
    """Only someone at the keyboard can get past this: trying again, or a new browser, changes nothing."""


class LoginManager:
    """Authenticates an account on the game's web form."""

    def __init__(self, browser, config: dict):
        self.browser = browser
        self.enabled = bool(config.get("enabled", True))
        self.open_login_selector = (config.get("open_login_selector") or "").strip()
        self.email_selector = (config.get("email_selector") or "").strip()
        self.password_selector = (config.get("password_selector") or "").strip()
        self.submit_selector = (config.get("submit_selector") or "").strip()
        self.logged_in_selector = (config.get("logged_in_selector") or "").strip()
        self.wait_after_submit = float(config.get("wait_after_submit", 30.0))
        self.max_attempts = int(config.get("max_attempts", 2))

    # ------------------------------------------------------------------ state
    def state(self) -> dict:
        """What the page is showing right now, as seen from the DOM."""
        return self.browser.evaluate(LOGIN_STATE_JS) or {}

    def is_logged_in(self) -> bool:
        """
        Logged in means: nothing on the page is offering to log us in.

        'No visible password field' is NOT enough on its own, and assuming it was
        is a mistake worth naming: totalbattle.com ships the login form hidden in
        the markup from the first paint, so on the landing page - logged out -
        there is no visible password field either. The collector would have
        decided it was already logged in and gone off to collect somebody else's
        chests.

        So a visible Login button counts as evidence of being logged out, just
        like a visible password field does. `login.logged_in_selector` overrides
        all of it when the page offers something more definite.
        """
        if self.logged_in_selector:
            return bool(self.browser.element_rect(self.logged_in_selector))

        state = self.state()
        if state.get("form_open") or state.get("code_asked"):
            return False
        # The game canvas being up outranks a stray 'Log in' left in a footer:
        # if the game is rendering, the session is open.
        if state.get("canvas"):
            return True
        return not state.get("opener")

    def wait_until_decidable(self, timeout: float = 30.0) -> bool:
        """
        Waits until the page says something about being logged in - either way.

        `document.readyState === 'complete'` fires long before this page has
        anything to say: the header and the login form are built by JavaScript
        afterwards. Asking too early gets a page with no form and no login
        button, which reads exactly like a page where nobody needs to log in -
        and the run went off to collect without ever authenticating.

        So the question is not asked until at least one signal exists.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.state()
            if state.get("form_in_dom") or state.get("opener") or state.get("canvas"):
                return True
            cancellation.sleep(0.5)
        logger.warning("The page showed neither a login form nor the game within the timeout.")
        return False

    def wait_for_login_form(self, timeout: float = 30.0) -> bool:
        """Waits for the form to exist in the page - visible or not yet opened."""
        return self.browser.wait_for(
            "document.querySelectorAll('input[type=password]').length > 0", timeout=timeout
        )

    def open_login_form(self, timeout: float = 15.0) -> bool:
        """
        Clicks whatever opens the login form, when the form is closed.

        The fields are in the DOM from the start but unreachable until the Login
        button is pressed - typing into them before that would be typing into
        nothing.
        """
        state = self.state()
        if state.get("form_open"):
            return True

        selector = self.open_login_selector or (
            "[data-tbc-role='open-login']" if state.get("opener") else ""
        )
        if not selector:
            if state.get("form_in_dom"):
                logger.warning(
                    "The login form is in the page but closed, and the opener button was not found. "
                    "Provide the selector in Login > CSS selector for login opener button."
                )
            return False

        logger.info(f"Opening the login form (via {state.get('opener') or selector}).")
        if not self.browser.click_element(selector, delay=0.6):
            return False

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.state().get("form_open"):
                logger.info("Login form is open.")
                return True
            cancellation.sleep(0.5)

        logger.warning("The login form did not open after clicking.")
        return False

    # ----------------------------------------------------------------- actions
    def _selectors(self) -> Tuple[str, str, str]:
        """Configured selectors win; anything left blank is discovered in the page."""
        email = self.email_selector
        password = self.password_selector
        submit = self.submit_selector
        if email and password and submit:
            return email, password, submit

        found = self.browser.evaluate(MARK_LOGIN_FIELDS_JS) or {}
        if not found.get("password"):
            raise SessionError("Password field not found on the login page.")
        return (
            email or ("[data-tbc-role='email']" if found.get("email") else ""),
            password or "[data-tbc-role='password']",
            submit or ("[data-tbc-role='submit']" if found.get("submit") else ""),
        )

    def _type_into(self, selector: str, text: str) -> bool:
        """Clicks the field, clears it and types - all through real input events."""
        if not selector or not self.browser.click_element(selector, delay=0.2):
            logger.error(f"Could not click the field '{selector}'.")
            return False
        self.browser.select_all()
        self.browser.press_key("Backspace", delay=0.05)
        self.browser.insert_text(text)
        time.sleep(0.2)
        return True

    def login(self, account: AccountConfig) -> bool:
        """Signs in as `account`, returning False if the credentials never took."""
        if not self.enabled:
            logger.info("Automatic login is disabled; using whatever session the browser has.")
            return self.is_logged_in()

        if not account.login or not account.password:
            raise SessionError(
                f"Account '{account.name}' has no username and password configured. "
                f"Fill them in the interface (Accounts and profiles tab) or disable automatic login."
            )

        for attempt in range(1, self.max_attempts + 1):
            logger.info(f"Logging in as '{account.name}' ({account.login}) - attempt {attempt}/{self.max_attempts}")

            self.wait_until_decidable(timeout=30.0)
            if self.is_logged_in():
                logger.info("A session was already open; nothing on the page asks for a login.")
                return True

            if not self.wait_for_login_form(timeout=30.0):
                logger.warning("No login form exists in the page; reloading.")
                self.browser.navigate()
                continue

            # The form is in the markup, but it may still be closed.
            if not self.open_login_form():
                if not self.state().get("form_open"):
                    logger.warning("Could not reach the login fields; reloading the page.")
                    self.browser.navigate()
                    continue

            try:
                email_selector, password_selector, submit_selector = self._selectors()
            except SessionError as exc:
                logger.error(str(exc))
                self.browser.navigate()
                continue

            if email_selector and not self._type_into(email_selector, account.login):
                continue
            if not self._type_into(password_selector, account.password):
                continue

            if submit_selector and self.browser.click_element(submit_selector, delay=0.5):
                logger.info("Login submitted.")
            else:
                # Some forms have no button we can recognise; Enter submits them.
                logger.info("No submit button identified; sending Enter.")
                self.browser.press_key("Enter", delay=0.5)

            if self._wait_until_logged_in():
                logger.info(f"Logged in as '{account.name}'.")
                return True

            logger.warning(f"Login attempt {attempt} for '{account.name}' did not go through.")

        logger.error(f"Could not log in to account '{account.name}'.")
        return False

    def _wait_until_logged_in(self) -> bool:
        """
        Waits for the login to take effect, twice before believing it.

        While the page swaps the modal for the game there is a moment with no
        form and no login button on screen - which looks identical to being
        logged in. Requiring the same answer a second later costs one second and
        keeps the run from starting on a page that is still mid-transition.
        """
        deadline = time.time() + self.wait_after_submit
        while time.time() < deadline:
            cancellation.check()
            if self.state().get("code_asked"):
                raise NeedsPerson(self._needs_person("the game asked for the code it sent by e-mail"))
            if self.is_logged_in():
                cancellation.sleep(1.0)
                if self.is_logged_in():
                    # The form is gone, but the game still has to finish loading.
                    self.browser.wait_for_load(timeout=max(5.0, deadline - time.time()))
                    return True
            time.sleep(1.0)
        return False

    def enter_account(self, account: AccountConfig) -> bool:
        """
        Makes sure the browser is authenticated as `account`, logging in only if needed.

        The browser keeps its profile between runs, so most runs find a session
        already open and never touch the login form at all. That is deliberate:
        wiping cookies makes the game treat the browser as a new device and mail
        a verification code, which no unattended run can answer.
        """
        self.browser.wait_for_load()
        self.wait_until_decidable(timeout=30.0)

        if self.state().get("code_asked"):
            raise NeedsPerson(self._needs_person("the game is asking for the code it sent by e-mail"))
        if self.is_logged_in():
            logger.info("Session already authenticated in the browser profile; no login needed.")
            return True
        if not self._folder_verified():
            raise NeedsPerson(self._needs_person("this Chrome folder has never logged in"))

        logger.info("The game is showing the login screen.")
        return self.login(account)

    def _mark_path(self) -> str:
        return os.path.join(self.browser.user_data_dir, SESSION_MARK)

    def _folder_verified(self) -> bool:
        return os.path.exists(self._mark_path())

    def remember_session(self):
        """Notes that the game came up in this folder, so a dropped session may be logged back into."""
        if self._folder_verified() or not self.state().get("canvas"):
            return
        try:
            with open(self._mark_path(), "w", encoding="utf-8"):
                pass
        except OSError as exc:
            logger.warning(f"Could not note the verified session in {self._mark_path()}: {exc}")

    def _needs_person(self, why: str) -> str:
        return (f"{why}. Log in by hand once: Accounts and profiles > Open window, and type the code. "
                f"The next runs use that session (folder: {self.browser.user_data_dir}).")

# Asks the game for its linked profiles and navigates to the target's auth token.
# All of it happens inside the tab: the tokens never reach Python, and what comes
# back is a status and the id of the profile, which is not a secret.
_SWITCH_JS = r"""
(() => {
  return (async () => {
    const CURRENT_AUTH = {
      suid: window.app && window.app.config ? window.app.config.suid : null,
      token: window.app && window.app.config ? (window.app.config.user_auth_token || (window.app.getGameStartDataResponse && window.app.getGameStartDataResponse.new_token)) : null,
    };

    if (!CURRENT_AUTH.suid || !CURRENT_AUTH.token) {
      return "no_auth";
    }

    const restUrl = (window.app && window.app.restApiUrl) || "https://totalbattle.com/restapi/totalbattle/web/";
    const deviceId = (window.app && window.app.deviceIdentifier) || "";

    let resp;
    try {
      resp = await fetch(restUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-rpc-method": "GetLinkedAccountsInfo"
        },
        body: JSON.stringify({
          jsonrpc: "2.0",
          id: "switch-account",
          method: "GetLinkedAccountsInfo",
          params: [{
            suid: CURRENT_AUTH.suid,
            token: CURRENT_AUTH.token
          }],
          deviceIdentifier: deviceId
        })
      });
    } catch (err) {
      return "fetch_failed: " + (err && err.message ? err.message : String(err));
    }

    if (!resp.ok) {
      return "http_error_" + resp.status;
    }

    let data;
    try {
      data = await resp.json();
    } catch (err) {
      return "json_parse_error";
    }

    if (data && data.error) {
      return "rpc_error: " + (data.error.code || "") + " " + String(data.error.message || "").slice(0, 120);
    }

    const tethers = data && data.result && data.result.tethers;
    if (!tethers || !tethers.length) {
      return "no_tethers";
    }

    const targetName = %s;
    const norm = (s) => (s || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").replace(/\s+/g, " ").trim().toLowerCase();
    const same = tethers.filter(t => norm(t.progress_short_info && t.progress_short_info.nickname) === norm(targetName));
    if (same.length !== 1) {
      return same.length ? "profile_ambiguous" : "profile_not_found";
    }
    const target = same[0];

    // The id every request of that profile will carry, to tell it apart when it comes back.
    const uid = target.progress_short_info.gameUid;
    if (!uid) {
      return "no_game_id";
    }
    const playerId = (BigInt(uid.initial) * (2n ** 32n) + BigInt(uid.sequence)).toString();

    const siteUrl = (window.app && window.app.config && window.app.config.site_url)
      ? window.app.config.site_url.replace(/\/$/, "")
      : "https://totalbattle.com";
    const origin = siteUrl.startsWith("http") ? siteUrl : "https:" + siteUrl;
    let switchUrl = origin + "/login/bytokenoauth/?token=" + encodeURIComponent(target.token);
    if (target.suid) {
      switchUrl += "&suid=" + encodeURIComponent(target.suid);
    }
    // Without it the page expects to be a child of the game's and stays blank in the tab.
    switchUrl += "&state=switchAccount";

    setTimeout(() => {
      window.location.href = switchUrl;
    }, 50);

    return "going:" + playerId;
  })();
})()
"""


# How long a profile is given to load, and how often it is looked at meanwhile.
SWITCH_TIMEOUT = 180.0
SWITCH_POLL = 5.0


class ProfileSwitcher:
    """Moves between the profiles (cities) of the account currently logged in."""

    def __init__(self, browser):
        self.browser = browser

    def active_profile_name(self) -> str:
        """
        The name of the profile the game is on, as the game says it; '' when it could not be asked.

        Every request the game makes carries the id of the profile playing, and each
        profile has its own, so this cannot take 'Crash BR' for 'Cash BR' the way a
        reading of the screen could, and it does not depend on what is drawn. The
        link is opened for each question and learns from the game's next request, so
        the answer is about the game as it is now.
        """
        # Here and not at the top: the modules build on this package.
        from modules.chests.game_link import GameLink
        from modules.events.traffic import GameLinkError

        link = GameLink(host=self.browser.host, port=self.browser.port, tab_id=self.browser.tab_id,
                        url_filter=self.browser.url_filter,
                        on_status=lambda text: logger.warning(f"Game link: {text}"))
        if not link.start():
            return ""
        try:
            player = link.player_id
            return link.player_names([player]).get(player, "")
        except GameLinkError as exc:
            logger.warning(f"Could not ask the game which profile it is on: {exc}")
            return ""
        finally:
            link.stop()

    def active_among(self, names: Sequence[str]) -> str:
        """
        Which of `names` is the profile the game is on, or '' when it is none of them.

        The game opens on whichever profile was used last, so the runner asks
        this once, right after the board is clear, to start with that profile
        instead of switching away from it and back later.
        """
        active = normalize_name(self.active_profile_name())
        return next((name for name in names if name and normalize_name(name) == active), "") if active else ""

    def is_active(self, profile_name: str) -> bool:
        """Is `profile_name` the profile the game is on? The whole name must be the same."""
        return bool(self.active_among([profile_name]))

    def _evaluate_async(self, expression: str, timeout: float = 30.0) -> Any:
        try:
            result = self.browser.send(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
                timeout=timeout,
            )
        except Exception as exc:
            logger.warning(f"Failed to evaluate expression: {exc}")
            return str(exc)
        if result.get("exceptionDetails"):
            text = (result["exceptionDetails"].get("text") or "").strip()
            logger.warning(f"JS exception in evaluate_async: {text}")
            return text
        return result.get("result", {}).get("value")

    def _is_playing(self, player_id: int) -> bool:
        """Whether the page has loaded and the game's requests carry this profile's id. Quiet: it is polled."""
        from modules.chests.game_link import GameLink

        if self.browser.evaluate("document.readyState") != "complete":
            return False
        link = GameLink(host=self.browser.host, port=self.browser.port, tab_id=self.browser.tab_id,
                        url_filter=self.browser.url_filter)
        if not link.start():
            return False
        try:
            return link.player_id == player_id
        finally:
            link.stop()

    def switch_to(self, profile: ProfileConfig) -> bool:
        """
        Selects `profile`, or reports that it could not be reached.

        It only counts when the game comes back carrying the id the list gave for that
        name: a name alone could pass for a near-twin, and a wrong profile costs a database.
        """
        if self.is_active(profile.name):
            # Not a failure, and it looks like one from outside: the game opens on
            # whichever profile was last used, so the very first profile of a run
            # is often already the right one and nothing has to be switched.
            logger.info(f"Profile '{profile.name}' is ALREADY active: going straight to its modules.")
            return True

        status = self._evaluate_async(_SWITCH_JS % json.dumps(profile.name))
        if not (isinstance(status, str) and status.startswith("going:")):
            logger.error(f"Profile '{profile.name}' not switched: {status}")
            return False
        player_id = int(status.split(":", 1)[1])

        logger.info(f"Switching to profile '{profile.name}'; waiting for the game to load it...")
        started = time.time()
        while time.time() - started < SWITCH_TIMEOUT:
            cancellation.sleep(SWITCH_POLL)
            if self._is_playing(player_id):
                logger.info(f"Profile '{profile.name}' is active after {time.time() - started:.0f}s.")
                return True
        logger.error(f"The game did not come back on profile '{profile.name}' in {SWITCH_TIMEOUT:.0f}s.")
        return False
