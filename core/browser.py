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
  scale we ask for. A name read at scale 3 is rendered three times larger by the
  browser itself, with no interpolation - which is most of the OCR improvement;
* logging in is a form on a web page, so accounts can be switched by clearing
  the session instead of hunting for a menu inside the game.

Everything here speaks CSS pixels relative to the top-left of the viewport.
"""

import base64
import json
import os
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
            raise BrowserError("dependência 'requests' não instalada (rode install.bat)")
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
                "Chrome/Edge/Brave não encontrado. Informe o caminho em Navegador > Caminho do Chrome."
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

            # Belt and braces against Chrome throttling a window it considers
            # covered. Measured on this machine, captures stay fresh with a
            # fullscreen always-on-top window over Chrome even WITHOUT these
            # flags (same image difference, 0.05s latency), so they fix nothing
            # that was observed - they only remove a variable that would be
            # miserable to diagnose if it ever did bite.
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling",
            "--disable-features=CalculateNativeWinOcclusion,Translate,TranslateUI",

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
        raise BrowserError(f"O navegador não respondeu na porta CDP {self.port} em 30s.")

    def start(self) -> bool:
        """Reuses a browser that is already listening, otherwise launches one."""
        if self.reuse_existing and self.is_cdp_ready():
            logger.info(f"Reusing the browser already listening on CDP port {self.port}.")
        else:
            self.launch()
        if not self.connect():
            raise BrowserError("Não foi possível conectar na aba do jogo via CDP.")
        return True

    def switch_profile(self, user_data_dir: str) -> bool:
        """
        Restarts the browser on another profile folder.

        Each account keeps its own logged-in session in its own folder, which is
        how accounts are switched now that clearing cookies is off the table -
        it would cost an e-mail verification code every time.
        """
        if os.path.normcase(os.path.abspath(user_data_dir)) == os.path.normcase(
            os.path.abspath(self.user_data_dir)
        ):
            return True

        if not self._launched_by_us and self.is_cdp_ready():
            logger.warning(
                "O navegador já estava aberto quando a execução começou, então não dá para trocar "
                "o perfil dele. Feche o Chrome do coletor ou desligue 'Reaproveitar navegador já aberto'."
            )
            return False

        logger.info(f"Switching browser profile to {user_data_dir}")
        self.close()
        self.user_data_dir = user_data_dir
        self._launched_by_us = False
        self._process = None
        self.reuse_existing = False      # a porta pode demorar a liberar; nunca reaproveitar aqui
        time.sleep(1.5)
        return self.start()

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
            raise BrowserError("dependência 'websocket-client' não instalada (rode install.bat)")

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
                raise BrowserError("Sem conexão CDP ativa.")

            self._msg_id += 1
            msg_id = self._msg_id
            try:
                self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
            except Exception as exc:
                self.ws = None
                raise BrowserError(f"Falha ao enviar {method}: {exc}")

            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    raw = self.ws.recv()
                except Exception as exc:
                    # A half-read socket cannot be trusted for the next command.
                    self.ws = None
                    raise BrowserError(f"Conexão CDP perdida durante {method}: {exc}")
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

    def clear_session(self):
        """
        Logs the current account out by erasing everything that remembers it.

        There is no reliable 'log out' button to hunt for inside the game, and a
        leftover session is what would make the next account silently collect
        into the previous account's database.
        """
        logger.info("Clearing browser session (cookies and storage)...")
        for method, params in (
            ("Network.clearBrowserCookies", None),
            ("Network.clearBrowserCache", None),
        ):
            try:
                self.send(method, params)
            except BrowserError as exc:
                logger.debug(f"{method}: {exc}")

        origin = self.evaluate("location.origin") or self.game_url
        try:
            self.send("Storage.clearDataForOrigin", {"origin": origin, "storageTypes": "all"})
        except BrowserError as exc:
            logger.debug(f"Storage.clearDataForOrigin: {exc}")
        self.evaluate("(()=>{try{localStorage.clear();sessionStorage.clear();}catch(e){}return 1})()")

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
        the way pushing the wheel away from you reveals what is below. Reading it
        the other way round is what made the profile list scroll to the bottom
        when the code meant to send it to the top, so the sign is spelled out
        here and the callers say `scroll_to_top` when that is what they mean.
        """
        self.move(x, y)
        time.sleep(0.03)
        for _ in range(repetitions):
            self.send("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": float(x), "y": float(y),
                "deltaX": 0, "deltaY": int(delta_y), "button": "none", "buttons": 0,
            })
            time.sleep(delay)

    def scroll_to_top(self, x: float, y: float, repetitions: int = 6):
        """Sends whatever is under (x, y) back to its first item."""
        self.scroll(x, y, delta_y=-600, repetitions=repetitions)

    def scroll_down(self, x: float, y: float, repetitions: int = 5):
        """Advances one screenful or so down the list under (x, y)."""
        self.scroll(x, y, delta_y=600, repetitions=repetitions)

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
        afterwards - the pixels are real, which is exactly what OCR needs.
        """
        if cv2 is None:
            raise BrowserError("dependência 'opencv-python' não instalada (rode install.bat)")

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
