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

The cost is that a second account cannot be reached by wiping the first one's
session. That is what `AccountConfig.browser_profile` is for - one browser
profile per account, each keeping its own verified session - and the runner
refuses to start rather than let two accounts share one session, which would
collect the second account's chests into the first account's database.
"""

import time
from typing import Sequence, Tuple

from config.settings import AccountConfig, ProfileConfig
from core.vision import changed_fraction
from utils.cancel import cancellation
from utils.logger import logger

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
    scope: scope === document ? 'pagina inteira' : (scope.tagName || '') + (scope.className ? '.' + String(scope.className).split(' ')[0] : ''),
    submit_label: submit ? label(submit).slice(0, 30) : '',
    email_name: email ? (email.name || email.placeholder || '') : '',
  };
})()
"""

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

  return {
    form_open: openForm,
    form_in_dom: passwords.length > 0,
    opener: opener ? label(opener).slice(0, 40) : "",
    canvas: Array.from(document.querySelectorAll('canvas')).some(visible),
  };
})()
"""


# The profile list is scrolled until it stops moving; this only caps a list that
# somehow never settles.
MAX_LIST_VIEWS = 8


class SessionError(RuntimeError):
    pass


class LoginManager:
    """Authenticates an account on the game's web form."""

    def __init__(self, browser, config: dict):
        self.browser = browser
        self.enabled = bool(config.get("enabled", True))
        self.clear_between_accounts = bool(config.get("clear_session_between_accounts", False))
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
        if state.get("form_open"):
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
                    "O formulário de login está na página mas fechado, e não achei o botão que o "
                    "abre. Informe o seletor em Login > Seletor CSS do botão que abre o login."
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
            raise SessionError("Campo de senha não encontrado na página de login.")
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
                f"A conta '{account.name}' não tem usuário e senha preenchidos. "
                f"Preencha na interface (aba Contas e perfis) ou desligue o login automático."
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

        if self.is_logged_in():
            logger.info(f"Session already authenticated in the browser profile; no login needed.")
            return True

        logger.info("The game is showing the login screen.")
        return self.login(account)

    def end_session(self):
        """
        Ends the session - only when the configuration asks for it.

        Off by default, and it should stay off for a single account: clearing
        cookies costs an e-mail verification code on the next login. It exists
        for the case of several accounts sharing one browser profile, where
        there is no other way to reach the second account.
        """
        if not self.clear_between_accounts:
            logger.debug("Keeping the session (clearing it would trigger the e-mail code).")
            return
        logger.info("Clearing the session as configured (the game may ask for an e-mail code).")
        self.browser.clear_session()
        self.browser.navigate()


class ProfileSwitcher:
    """Moves between the profiles (cities) of the account currently logged in."""

    def __init__(self, browser, ocr, vision, calibration, game_state, timing: dict):
        self.browser = browser
        self.ocr = ocr
        self.vision = vision
        self.calibration = calibration
        self.game_state = game_state
        self.switch_wait = float(timing.get("profile_switch_wait", 20.0))
        self.click_delay = float(timing.get("click_delay", 0.5))

    def active_profile_name(self) -> str:
        area = self.calibration.region("profile_name_display_area")
        if not area:
            return ""
        return " ".join(line.text for line in self.ocr.read_lines(area)).strip()

    def is_active(self, profile_name: str, siblings: Sequence[str] = ()) -> bool:
        """
        Is `profile_name` the profile on screen?

        The banner is identified against every profile of the account, not
        checked against this one alone: with names as close as 'Crash BR' and
        'Cash BR', a yes/no test says yes to both.
        """
        area = self.calibration.region("profile_name_display_area")
        if not area:
            logger.warning("'profile_name_display_area' is not calibrated; cannot confirm the active profile.")
            return False

        candidates = list(dict.fromkeys([profile_name, *[s for s in siblings if s]]))
        winner, _text = self.ocr.identify(area, candidates)
        return winner == profile_name

    def switch_to(self, profile: ProfileConfig, siblings: Sequence[str] = ()) -> bool:
        """
        Selects `profile`, or reports that it could not be reached.

        The list is scrolled to the top before searching, then searched again
        after scrolling down: the profile list is longer than its box, and a
        profile that was simply below the fold used to be reported as missing.
        """
        missing = self.calibration.require("profile_menu_button", "profiles_list_area")
        if missing:
            raise SessionError(f"Calibração incompleta para trocar de perfil: {', '.join(missing)}")

        self.game_state.prepare_board()

        competitors = [name for name in siblings if name and name != profile.name]

        if self.is_active(profile.name, siblings):
            # Not a failure, and it looks like one from outside: the game opens on
            # whichever profile was last used, so the very first profile of a run
            # is often already the right one and no menu is opened at all.
            logger.info(
                f"Profile '{profile.name}' is ALREADY active - skipping the profile menu on "
                f"purpose and going straight to the chests."
            )
            return True

        menu_point = self.calibration.point("profile_menu_button")
        list_area = self.calibration.region("profiles_list_area")

        logger.info(f"Opening the profile menu at {menu_point}...")
        before = self.browser.capture(scale=1.0)
        self.browser.click(menu_point[0], menu_point[1], delay=1.5)

        # Did the menu actually open? A click that misses is silent, and the run
        # would go on to search a list that is not on screen and blame the OCR.
        opened = changed_fraction(before, self.browser.capture(scale=1.0))
        if opened < 0.01:
            logger.error(
                f"Clicking the profile menu at {menu_point} changed nothing on screen "
                f"({opened:.1%}). The point is wrong for this page size, or something is on top "
                f"of it. Recalibrate 'Menu de perfis' and use 'Testar este passo'."
            )
            return False
        logger.info(f"The profile menu click changed {opened:.0%} of the screen.")

        center_x = list_area[0] + list_area[2] // 2
        center_y = list_area[1] + list_area[3] // 2
        self.browser.scroll_to_top(center_x, center_y)
        cancellation.sleep(0.6)

        # Scroll until the list stops changing instead of a fixed two views: the
        # number of profiles is whatever the account has, and a fixed number of
        # looks either misses the last ones or wastes time on a short list.
        for view in range(1, MAX_LIST_VIEWS + 1):
            logger.info(f"Looking for profile '{profile.name}' in the list (view {view})...")
            found = self.ocr.find_text(list_area, profile.name, competitors=competitors)
            if found:
                self.browser.click(found.center[0], found.center[1], delay=1.5)
                return self._confirm_switch(profile, siblings)

            list_before = self.browser.capture(list_area, scale=1.0)
            self.browser.scroll_down(center_x, center_y)
            cancellation.sleep(0.8)
            if changed_fraction(list_before, self.browser.capture(list_area, scale=1.0)) < 0.01:
                logger.info("The list did not move: this is the end of it.")
                break

        logger.error(f"Profile '{profile.name}' was not found in the profile list.")
        self.browser.press_key("Escape", delay=0.5)
        return False

    def _confirm_switch(self, profile: ProfileConfig, siblings: Sequence[str] = ()) -> bool:
        """Clicks the confirmation dialog, if the game asks for one, then waits it out."""
        if self.calibration.is_calibrated("profile_switch_confirm"):
            reference = self.calibration.ref_path("profile_switch_confirm")
            search_area = self.calibration.region("profile_switch_confirm")
            # Search the whole page: the dialog is centred and does not always
            # land where it was during calibration.
            match = self.vision.find(reference, None, base_scale=self.calibration.image_scale)
            if match:
                self.browser.click(match[0] + match[2] // 2, match[1] + match[3] // 2, delay=1.0)
                logger.info("Profile switch confirmed.")
            elif search_area:
                logger.info("No confirmation dialog appeared; continuing.")

        logger.info(f"Waiting {self.switch_wait:.0f}s for the game to load profile '{profile.name}'...")
        cancellation.sleep(self.switch_wait)
        self.browser.wait_for_load(timeout=self.switch_wait)
        self.game_state.prepare_board()

        if self.is_active(profile.name, siblings):
            return True

        logger.error(
            f"After switching, the active profile is not '{profile.name}' "
            f"(reads '{self.active_profile_name()}')."
        )
        return False
