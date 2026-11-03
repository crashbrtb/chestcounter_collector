"""
The game inside Chrome, driven by CDP (Chrome DevTools Protocol).

Why the browser replaced the desktop client
-------------------------------------------
The desktop client could only be operated through the physical screen: the
window had to be in front, at a known resolution, and every click was a real
mouse move that any other window could steal. In the browser the same actions go
through CDP, which changes three things that mattered:

* coordinates are CSS pixels of the page, not of the monitor - so the window can
  be moved, and another window can be on top, without breaking anything;
* screenshots come from `Page.captureScreenshot`, which renders a region at any
  scale we ask for, rendered by the browser itself with no interpolation;
* logging in is a form on a web page.

One Browser is one account's window: a debugging port and the folder that keeps
the session the game verified. A port says nothing about who is behind it - any
other program's Chrome can be listening there - so a window is only used, or
closed, after its command line shows it is on our folder (`check_owner`). The
wrong window would mean another account's chests in this one's database.

Everything here speaks CSS pixels relative to the top-left of the viewport.
"""

import base64
import json
import os
import re
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover - guarded by requirements.txt
    cv2 = None

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

try:
    import websocket
except ImportError:  # pragma: no cover
    websocket = None

from config.settings import same_folder
from utils.logger import logger

CHROME_PATHS = [
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe",
]

# Keys we may need to send; CDP wants the Windows virtual key code alongside the name.
KEY_CODES = {
    "Enter": (13, "Enter"),
    "Tab": (9, "Tab"),
    "Escape": (27, "Escape"),
    "Backspace": (8, "Backspace"),
}


class BrowserError(RuntimeError):
    pass


class WrongWindow(BrowserError):
    """The port or the folder belongs to another Chrome. Restarting would close somebody else's window."""


def command_line_folder(command_line: str) -> str:
    """The --user-data-dir of a Chrome command line, whichever way it was quoted; '' when it has none."""
    match = re.search(r'--user-data-dir=(?:"([^"]+)"|(.+?)(?=\s+"?--|\s+https?:|"|\s*$))', command_line)
    return (match.group(1) or match.group(2)) if match else ""


def on_port(command_line: str, port: int) -> bool:
    return re.search(rf"--remote-debugging-port={port}(?!\d)", command_line) is not None


# Against Chrome throttling a window it considers covered or unseen. A
# fullscreen window on top did not throttle it on the development machine. In
# production, from 17/10/2026, with the collector reusing a Chrome it had not
# opened, the Journal's "Show details" stopped being found (similarity stuck at
# 0.46) on most runs and worked again on others with nothing in the collector
# changed - throttling of an unwatched window is the suspect. start() restarts a
# reused browser that lacks any of them, so that suspect is out either way.
THROTTLING_FLAGS = (
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--disable-features=CalculateNativeWinOcclusion,Translate,TranslateUI",
)


class Browser:
    """A Chrome tab showing Total Battle, plus the input and capture it accepts."""

    def __init__(self, config: Dict[str, Any], user_data_dir: str):
        self.host = config.get("cdp_host", "127.0.0.1")
        self.port = int(config.get("cdp_port", 9222))
        self.game_url = config.get("game_url", "https://totalbattle.com/en/")
        self.url_filter = (config.get("game_url_filter") or "totalbattle.com").lower()
        self.executable_path = config.get("executable_path", "")
        self.user_data_dir = user_data_dir
        self.reuse_existing = bool(config.get("reuse_existing", True))
        self.start_maximized = bool(config.get("start_maximized", True))
        self.window_size = (int(config.get("window_width", 1920)), int(config.get("window_height", 1080)))
        self.page_load_timeout = float(config.get("page_load_timeout", 90.0))

        self.ws = None
        self.tab_id: Optional[str] = None
        # Display scaling: captures come back multiplied by it (see capture()).
        self.dpr = 1.0
        self._msg_id = 0
        # One socket, one conversation at a time (see send()).
        self._lock = threading.RLock()
        self._process: Optional[subprocess.Popen] = None
        self._launched_by_us = False

    # ------------------------------------------------------------------ setup

    def _http(self, path: str, timeout: float = 2.0):
        if requests is None:
            raise BrowserError("dependency 'requests' is not installed (run install.bat)")
        return requests.get(f"http://{self.host}:{self.port}{path}", timeout=timeout)

    def is_cdp_ready(self) -> bool:
        try:
            return self._http("/json/version", timeout=1.0).status_code == 200
        except Exception:
            return False

    def find_executable(self) -> Optional[str]:
        if self.executable_path and os.path.exists(self.executable_path):
            return self.executable_path
        for path in CHROME_PATHS:
            expanded = os.path.expandvars(path)
            if os.path.exists(expanded):
                return expanded
        return None

    def launch(self) -> bool:
        """Starts Chrome with remote debugging on its own profile directory."""
        executable = self.find_executable()
        if not executable:
            raise BrowserError(
                "Chrome/Edge/Brave not found. Set the path in Browser > Chrome executable path."
            )

        os.makedirs(self.user_data_dir, exist_ok=True)
        command = [
            executable,
            f"--remote-debugging-port={self.port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={self.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--disable-popup-blocking",
            *THROTTLING_FLAGS,

            # The translate bar is browser chrome, not page content: it never
            # reaches a screenshot nor intercepts a CDP click. It is disabled
            # anyway so that what you see matches what the collector sees.
            "--disable-translate",
        ]
        if self.start_maximized:
            command.append("--start-maximized")
        else:
            command.append(f"--window-size={self.window_size[0]},{self.window_size[1]}")
        command.append(self.game_url)

        logger.info(f"Launching browser: {os.path.basename(executable)} (CDP port {self.port})")
        self._process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._launched_by_us = True

        deadline = time.time() + 30
        while time.time() < deadline:
            if self.is_cdp_ready():
                return True
            time.sleep(0.3)
        raise BrowserError(f"The browser did not respond on CDP port {self.port} within 30s.")

    def start(self) -> bool:
        """
        Reuses a browser that is already listening, otherwise launches one.

        A browser left open by an earlier run is checked before it is trusted:
        the game sometimes freezes until F5, and a run that attached to that tab
        failed on every step - and so did every run after it, because each one
        found the same frozen Chrome and attached to it again. One that does not
        answer is closed and launched fresh.
        """
        if self.is_cdp_ready():
            self.check_owner()
            if not self.reuse_existing:
                return self.restart()
            logger.info(f"Reusing the browser already listening on CDP port {self.port}.")
            missing = self.missing_launch_flags()
            if missing:
                logger.warning(
                    f"The browser on CDP port {self.port} was not opened by the collector and lacks "
                    f"{', '.join(missing)}: with nobody looking at the screen, Chrome stops drawing the "
                    "game and buttons are not found. Restarting it with the collector's flags."
                )
                return self.restart()
            try:
                if self.connect() and self.is_responsive():
                    return True
            except BrowserError as exc:
                logger.debug(f"Reused browser: {exc}")
            logger.warning("The browser that was already open is not responding; restarting it.")
            return self.restart()

        self.check_folder_free()
        self.launch()
        if not self.connect():
            raise BrowserError("Could not connect to the game tab via CDP.")
        return True

    def missing_launch_flags(self) -> List[str]:
        """
        The THROTTLING_FLAGS the browser on our port was started without.

        Empty when they are all there, and also when the command line cannot be
        read (not Windows, no access): a browser is only restarted on evidence.
        """
        ours = [line for line in self._browser_processes() or [] if on_port(line, self.port)]
        if not ours:
            return []
        return [flag for flag in THROTTLING_FLAGS if flag not in ours[0]]

    def _browser_processes(self) -> Optional[List[str]]:
        """The command line of every Chrome started with a folder of its own; None when they cannot be read."""
        if os.name != "nt":
            return None
        script = (
            "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -and $_.CommandLine.Contains('--user-data-dir=') "
            "-and -not $_.CommandLine.Contains('--type=') } | "
            "ForEach-Object { $_.CommandLine }"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, encoding="utf-8", errors="replace", timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            logger.debug(f"Reading the browsers' command lines: {exc}")
            return None
        return (result.stdout or "").splitlines() if result.returncode == 0 else None

    def check_owner(self):
        """Raises WrongWindow unless the Chrome on our port is on our folder. Not knowing counts as no."""
        if os.name != "nt":
            return
        lines = self._browser_processes()
        if lines is None:
            raise WrongWindow(f"Could not confirm whose Chrome is on port {self.port}, so it was not used.")
        ours = [line for line in lines if on_port(line, self.port)]
        folder = command_line_folder(ours[0]) if ours else ""
        if not folder or not same_folder(folder, self.user_data_dir):
            raise WrongWindow(
                f"Port {self.port} is taken by another Chrome (folder: {folder or 'unknown'}), not by "
                f"{self.user_data_dir}. It was not used nor closed. Change this account's port in "
                f"Accounts and profiles, or close that window."
            )

    def check_folder_free(self):
        """
        Raises WrongWindow when our folder is already open.

        Chrome hands a second start on the same folder to the window already open:
        no new window, and the port never answers.
        """
        for line in self._browser_processes() or []:
            folder = command_line_folder(line)
            if folder and same_folder(folder, self.user_data_dir):
                raise WrongWindow(
                    f"The Chrome folder {self.user_data_dir} is already open in a window that does not "
                    f"answer on port {self.port}. Close that window, or give this account another folder."
                )

    def open_for_person(self):
        """Opens the account's window and leaves it to whoever is at the keyboard (the code by e-mail)."""
        if self.is_cdp_ready():
            self.check_owner()
            tab = self.find_game_tab()
            if tab:
                self.tab_id = tab.get("id")
                self.focus_tab()
            return
        self.check_folder_free()
        self.launch()

    def is_responsive(self, timeout: float = 10.0) -> bool:
        """True when the game tab still runs JavaScript and still renders a frame."""
        try:
            result = self.send("Runtime.evaluate",
                               {"expression": "1+1", "returnByValue": True}, timeout=timeout)
            if result.get("result", {}).get("value") != 2:
                return False
            self.send("Page.captureScreenshot", {
                "format": "jpeg", "quality": 10,
                "clip": {"x": 0, "y": 0, "width": 8, "height": 8, "scale": 1},
            }, timeout=timeout)
            return True
        except BrowserError as exc:
            logger.debug(f"Responsiveness check failed: {exc}")
            return False

    def restart(self) -> bool:
        """
        Closes the browser - whoever opened it - and launches a fresh one.

        The equivalent of the F5 a person would press on a frozen game, but
        stronger: a new browser process also clears a hung renderer that a
        reload would not reach.
        """
        if self.is_cdp_ready():
            self.check_owner()
        logger.info("Restarting the browser...")
        self.kill()
        time.sleep(1.5)
        self.launch()
        if not self.connect():
            raise BrowserError("Could not connect to the game tab via CDP after restarting the browser.")
        return True

    def kill(self):
        """
        Closes the browser on our CDP port even when this run did not open it.

        Asked politely first (`Browser.close` on the browser's own socket, which
        answers even when the game tab is hung), then by process: ours if we
        launched it, otherwise every process started on our profile folder.
        """
        self.disconnect()
        if self.is_cdp_ready():
            try:
                url = self._http("/json/version").json().get("webSocketDebuggerUrl")
                if url and websocket is not None:
                    browser_ws = websocket.create_connection(url, timeout=5, suppress_origin=True)
                    try:
                        browser_ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
                    finally:
                        browser_ws.close()
            except Exception as exc:
                logger.debug(f"Browser.close: {exc}")

        if not self._wait_cdp_gone(15):
            if self._process and self._process.poll() is None:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._kill_profile_processes()
            if not self._wait_cdp_gone(10):
                raise BrowserError(f"Could not close the browser on CDP port {self.port}.")

        self._process = None
        self._launched_by_us = False
        self.tab_id = None
        logger.info("Browser closed.")

    def _wait_cdp_gone(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.is_cdp_ready():
                return True
            time.sleep(0.5)
        return not self.is_cdp_ready()

    def _kill_profile_processes(self):
        """Force-kills every process whose command line names our profile folder."""
        if os.name != "nt":
            return
        # The whole folder: 'Account' must not take 'Account2's window with it.
        folder = self.user_data_dir.replace("'", "''")
        script = (
            f"$d = '--user-data-dir=\"?' + [regex]::Escape('{folder}') + '(\"|\\s+\"?--|\\s+http|\\s*$)'; "
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -and $_.CommandLine -match $d } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            logger.debug(f"Killing browser processes: {exc}")

    # -------------------------------------------------------------- tab / link

    def tabs(self) -> List[dict]:
        try:
            response = self._http("/json/list")
            return response.json() if response.status_code == 200 else []
        except Exception:
            return []

    def find_game_tab(self) -> Optional[dict]:
        pages = [t for t in self.tabs() if t.get("type") == "page"]
        for tab in pages:
            if self.url_filter in (tab.get("url") or "").lower():
                return tab
        for tab in pages:
            if "total battle" in (tab.get("title") or "").lower():
                return tab
        return None

    def open_game_tab(self) -> Optional[dict]:
        """Ensures a tab with the game exists, creating one if needed."""
        tab = self.find_game_tab()
        if tab:
            return tab
        try:
            self._http(f"/json/new?{self.game_url}", timeout=5.0)
        except Exception:
            pass
        deadline = time.time() + 15
        while time.time() < deadline:
            tab = self.find_game_tab()
            if tab:
                return tab
            time.sleep(0.5)
        return None

    def connect(self) -> bool:
        if websocket is None:
            raise BrowserError("dependency 'websocket-client' is not installed (run install.bat)")

        tab = self.open_game_tab()
        if not tab:
            # No game tab yet: attach to any page so we can at least navigate.
            pages = [t for t in self.tabs() if t.get("type") == "page"]
            tab = pages[0] if pages else None
        if not tab or not tab.get("webSocketDebuggerUrl"):
            return False

        self.disconnect()
        try:
            self.ws = websocket.create_connection(
                tab["webSocketDebuggerUrl"], timeout=30, suppress_origin=True
            )
        except Exception as exc:
            logger.error(f"CDP websocket failed: {exc}")
            return False

        self.tab_id = tab.get("id")
        self.send("Page.enable")
        self.send("Runtime.enable")
        self.send("DOM.enable")
        self.refresh_dpr()
        logger.info(f"Connected to tab: {tab.get('url', '')[:90]}")
        return True

    def refresh_dpr(self) -> float:
        """Re-reads the display scaling; it changes with the monitor and with zoom."""
        value = self.evaluate("window.devicePixelRatio")
        self.dpr = float(value) if isinstance(value, (int, float)) and value > 0 else 1.0
        if abs(self.dpr - 1.0) > 0.01:
            logger.info(f"Display scaling is {self.dpr:g}x; captures are corrected to CSS pixels.")
        return self.dpr

    def disconnect(self):
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        self.ws = None

    def focus_tab(self) -> bool:
        """Brings the game tab to the front - needed only for the user to watch."""
        if not self.tab_id:
            return False
        try:
            return self._http(f"/json/activate/{self.tab_id}", timeout=2.0).status_code == 200
        except Exception:
            return False

    def close(self):
        """Closes the browser, but only if this run was the one that opened it."""
        self.disconnect()
        if not self._launched_by_us:
            logger.info("Browser was already open before this run; leaving it as it is.")
            return
        try:
            self._http("/json/close/" + (self.tab_id or ""), timeout=2.0)
        except Exception:
            pass
        if self._process and self._process.poll() is None:
            try:
                self._process.terminate()
                self._process.wait(timeout=10)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
        logger.info("Browser closed.")

    # --------------------------------------------------------------- protocol

    def send(self, method: str, params: Optional[dict] = None, timeout: float = 30.0) -> dict:
        """
        Sends one command and returns its result.

        Serialised on a lock, because this is a request/response conversation on
        a single socket: the caller sends an id and then reads until that id
        comes back. Two threads doing that at once each swallow the other's
        answer, and both end up timing out on a connection that is actually
        fine - which is exactly what happened when the interface tested a step
        while something else was talking to the browser.

        Messages that are not the answer (CDP events, answers to earlier
        commands) are discarded here: this client never subscribes to events.
        """
        with self._lock:
            if not self.ws and not self.connect():
                raise BrowserError("No active CDP connection.")

            self._msg_id += 1
            msg_id = self._msg_id
            try:
                self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
            except Exception as exc:
                self.ws = None
                raise BrowserError(f"Failed to send {method}: {exc}")

            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    raw = self.ws.recv()
                except Exception as exc:
                    # A half-read socket cannot be trusted for the next command.
                    self.ws = None
                    raise BrowserError(f"CDP connection lost during {method}: {exc}")
                if not raw:
                    continue
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if message.get("id") != msg_id:
                    continue
                if "error" in message:
                    raise BrowserError(f"{method}: {message['error'].get('message', message['error'])}")
                return message.get("result", {})

            self.ws = None
            raise BrowserError(f"Tempo esgotado esperando resposta de {method}.")

    def evaluate(self, expression: str, default: Any = None) -> Any:
        """Runs JavaScript in the page and returns its value (None on error)."""
        try:
            result = self.send("Runtime.evaluate", {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            })
        except BrowserError as exc:
            logger.debug(f"evaluate failed: {exc}")
            return default
        if result.get("exceptionDetails"):
            logger.debug(f"JS exception: {result['exceptionDetails'].get('text')}")
            return default
        return result.get("result", {}).get("value", default)

    # ------------------------------------------------------------- navigation

    def navigate(self, url: Optional[str] = None, wait: bool = True):
        target = url or self.game_url
        logger.info(f"Navigating to {target}")
        self.send("Page.navigate", {"url": target})
        if wait:
            self.wait_for_load()

    def reload(self, wait: bool = True):
        self.send("Page.reload", {"ignoreCache": False})
        if wait:
            self.wait_for_load()

    def wait_for_load(self, timeout: Optional[float] = None) -> bool:
        """Waits for document.readyState to reach 'complete'."""
        limit = time.time() + (timeout or self.page_load_timeout)
        while time.time() < limit:
            if self.evaluate("document.readyState") == "complete":
                return True
            time.sleep(0.5)
        logger.warning("Page did not finish loading within the timeout.")
        return False

    def wait_for(self, js_condition: str, timeout: float = 30.0, interval: float = 0.5) -> bool:
        """Polls a JavaScript expression until it is truthy."""
        limit = time.time() + timeout
        while time.time() < limit:
            if self.evaluate(f"!!({js_condition})") is True:
                return True
            time.sleep(interval)
        return False

    # ------------------------------------------------------------------ input

    def viewport(self) -> Tuple[int, int]:
        size = self.evaluate("[window.innerWidth, window.innerHeight]") or [0, 0]
        return int(size[0]), int(size[1])

    def set_viewport(self, width: int, height: int, tolerance: int = 2, attempts: int = 4) -> bool:
        """
        Resizes the window until the page area matches `width` x `height`.

        Calibrated positions are page coordinates, so a page of a different size
        forces every one of them to be rescaled - and a game interface does not
        scale linearly. Panels stay centred and buttons keep their size, so a
        rescaled point drifts off a small button entirely: the click happens, at
        the wrong place, and nothing appears to have been clicked.

        Restoring the calibrated page size removes the rescaling instead of
        trying to compensate for it. The loop converges because the difference
        between the window and the page area - toolbar, borders - is constant.
        """
        if width <= 0 or height <= 0:
            return False

        try:
            window = self.send("Browser.getWindowForTarget", {"targetId": self.tab_id})
        except BrowserError as exc:
            logger.debug(f"Browser.getWindowForTarget: {exc}")
            return False

        window_id = window.get("windowId")
        bounds = window.get("bounds", {})
        if not window_id:
            return False

        for _ in range(attempts):
            inner_w, inner_h = self.viewport()
            if abs(inner_w - width) <= tolerance and abs(inner_h - height) <= tolerance:
                return True
            if not inner_w or not inner_h:
                return False

            outer_w = int(bounds.get("width") or inner_w)
            outer_h = int(bounds.get("height") or inner_h)
            new_bounds = {
                "windowState": "normal",
                "width": outer_w + (width - inner_w),
                "height": outer_h + (height - inner_h),
            }
            try:
                self.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": new_bounds})
            except BrowserError as exc:
                logger.debug(f"Browser.setWindowBounds: {exc}")
                return False
            time.sleep(0.4)
            bounds = self.send("Browser.getWindowForTarget",
                               {"targetId": self.tab_id}).get("bounds", new_bounds)

        self.refresh_dpr()
        inner_w, inner_h = self.viewport()
        return abs(inner_w - width) <= tolerance and abs(inner_h - height) <= tolerance

    def move(self, x: float, y: float):
        self.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": float(x), "y": float(y), "button": "none", "buttons": 0,
        })

    def click(self, x: float, y: float, delay: float = 0.0, clicks: int = 1):
        """
        A click the page cannot tell from a real one.

        The move before pressing is not decoration: the game's canvas tracks
        hover state, and buttons that only react once the pointer is over them
        ignored a press that arrived out of nowhere.
        """
        self.move(x, y)
        time.sleep(0.03)
        for index in range(clicks):
            common = {"x": float(x), "y": float(y), "button": "left", "clickCount": index + 1}
            self.send("Input.dispatchMouseEvent", dict(common, type="mousePressed", buttons=1))
            time.sleep(0.04)
            self.send("Input.dispatchMouseEvent", dict(common, type="mouseReleased", buttons=0))
            if clicks > 1:
                time.sleep(0.05)
        if delay > 0:
            time.sleep(delay)

    def scroll(self, x: float, y: float, delta_y: int = 400, repetitions: int = 1, delay: float = 0.15):
        """
        Turns the wheel over (x, y).

        The sign is the wheel's own, not the content's: **positive scrolls DOWN**,
        the way pushing the wheel away from you reveals what is below.
        """
        self.move(x, y)
        time.sleep(0.03)
        for _ in range(repetitions):
            self.send("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": float(x), "y": float(y),
                "deltaX": 0, "deltaY": int(delta_y), "button": "none", "buttons": 0,
            })
            time.sleep(delay)

    def press_key(self, name: str, delay: float = 0.1):
        code, key = KEY_CODES.get(name, (0, name))
        for event_type in ("keyDown", "keyUp"):
            self.send("Input.dispatchKeyEvent", {
                "type": event_type, "key": key, "code": key,
                "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code,
            })
        if delay > 0:
            time.sleep(delay)

    def insert_text(self, text: str):
        """Types text as a unit - immune to keyboard layout and safe for accents."""
        self.send("Input.insertText", {"text": text})

    def select_all(self):
        for event_type in ("keyDown", "keyUp"):
            self.send("Input.dispatchKeyEvent", {
                "type": event_type, "key": "a", "code": "KeyA",
                "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65,
                "modifiers": 2,  # Ctrl
            })
        time.sleep(0.05)

    # ------------------------------------------------------------------- DOM

    def element_rect(self, selector: str) -> Optional[Dict[str, float]]:
        """Bounding box of the first visible match, in viewport CSS pixels."""
        script = """
        (() => {
          const el = document.querySelector(%s);
          if (!el) return null;
          const r = el.getBoundingClientRect();
          if (!r.width || !r.height) return null;
          return {x: r.x, y: r.y, width: r.width, height: r.height};
        })()
        """ % json.dumps(selector)
        return self.evaluate(script)

    def click_element(self, selector: str, delay: float = 0.2) -> bool:
        """Clicks the centre of an element with a real input event, not element.click()."""
        rect = self.element_rect(selector)
        if not rect:
            return False
        self.click(rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2, delay=delay)
        return True

    # --------------------------------------------------------------- capture

    def capture(
        self,
        region: Optional[Tuple[int, int, int, int]] = None,
        scale: float = 1.0,
    ) -> Optional[np.ndarray]:
        """
        A picture of the page, or of one region of it, as a BGR image.

        `region` is (left, top, width, height) in CSS pixels. `scale` asks the
        browser to RENDER that region larger instead of resizing a small capture
        afterwards - the pixels are real.
        """
        if cv2 is None:
            raise BrowserError("dependency 'opencv-python' is not installed (run install.bat)")

        if region:
            left, top, width, height = (int(v) for v in region)
        else:
            left, top = 0, 0
            width, height = self.viewport()
        if width <= 0 or height <= 0:
            return None

        # Undo the display scaling.
        #
        # Chrome renders the screenshot at the device pixel ratio: on a Windows
        # desktop at 125% a 1536-wide page comes back as a 1920-wide image. Every
        # coordinate in this application is a CSS pixel - that is what a click
        # takes - so a picture in device pixels silently made every position
        # 1.25x too large. Points marked on the calibration image landed off the
        # button, or off the page entirely, and no click reached anything.
        #
        # Dividing the requested scale by the ratio cancels it: the image comes
        # back at exactly `scale` times the CSS size, whatever the display is
        # set to.
        effective_scale = float(scale) / (self.dpr or 1.0)
        params: Dict[str, Any] = {
            "format": "png",
            "fromSurface": True,
            "captureBeyondViewport": False,
            "clip": {
                "x": float(left), "y": float(top),
                "width": float(width), "height": float(height),
                "scale": effective_scale,
            },
        }

        try:
            result = self.send("Page.captureScreenshot", params, timeout=30.0)
        except BrowserError as exc:
            logger.warning(f"Screenshot failed: {exc}")
            return None

        data = result.get("data")
        if not data:
            return None
        buffer = np.frombuffer(base64.b64decode(data), dtype=np.uint8)
        image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
        if image is None:
            return None

        # Chrome rounds the clip, so the result can be a pixel off. Coordinate
        # maths divides by `scale`, and a stray pixel there becomes a drift
        # across the image, so the size is made exact.
        expected = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
        if (image.shape[1], image.shape[0]) != expected:
            image = cv2.resize(image, expected, interpolation=cv2.INTER_AREA)
        return image

    def save_screenshot(self, path: str) -> bool:
        image = self.capture()
        if image is None or cv2 is None:
            return False
        try:
            cv2.imwrite(path, image)
            return True
        except Exception as exc:
            logger.debug(f"Could not write screenshot to {path}: {exc}")
            return False
