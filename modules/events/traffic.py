"""Listens to what the game receives, by reading Chrome's network through CDP.

A second connection to the game tab, beside the one core.browser.Browser sends
its commands on: that one is a request/response conversation and discards
everything else, while this one only listens. Every HTTP response from a Total
Battle host has its body asked for with `Network.getResponseBody`, and every
WebSocket frame received is taken as it comes. It works with the game already
open, without reloading.

Only what the game RECEIVES is looked at. The body of a request is never read:
the game session travels inside it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

import requests
import websocket

from . import tbcodec

MAX_BODY_BYTES = 8 * 1024 * 1024

_STATIC_EXT = re.compile(
    r"\.(png|jpe?g|gif|webp|svg|ico|css|js|mjs|map|woff2?|ttf|otf|mp3|ogg|wav|mp4|webm|"
    r"wasm|data|unityweb|bundle|br|gz|html?)(\?|$)",
    re.I,
)


def is_game_url(url: str) -> bool:
    if not url or "totalbattle" not in url.lower() and "rubens-" not in url.lower():
        return False
    return not _STATIC_EXT.search(urlsplit(url).path or "")


def safe_url(url: str) -> str:
    """URL without its query string: parameters can carry a session token."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def routes_of(data: bytes) -> list:
    try:
        return [f.route for f in tbcodec.iter_frames(data) if f.route is not None][:50]
    except Exception:
        return []


class TrafficListener:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9222,
        tab_id: Optional[str] = None,
        url_filter: str = "totalbattle.com",
        on_record: Optional[Callable[[dict], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ):
        self.host = host
        self.port = port
        self.tab_id = tab_id            # the tab the browser is driving; any game tab when None
        self.url_filter = (url_filter or "totalbattle.com").lower()
        self.on_record = on_record
        self.on_status = on_status

        self.ws: Optional[websocket.WebSocketApp] = None
        self.lock = threading.Lock()
        self.msg_id = 5000
        self.is_running = False

        self.responses: Dict[tuple, dict] = {}   # (session, requestId) -> meta
        self.body_calls: Dict[int, dict] = {}     # command id -> meta
        self.websockets: Dict[tuple, str] = {}    # (session, requestId) -> url
        self.recent: Dict[str, float] = {}
        self.counter = 0
        self.stats = {"RESP": 0, "WS_IN": 0, "ignored": 0}

    # ------------------------------------------------------------------ infra
    def _status(self, msg: str) -> None:
        if self.on_status:
            try:
                self.on_status(msg)
            except Exception:
                pass

    def _cmd(self, method: str, params: Optional[dict] = None, session_id: Optional[str] = None) -> Optional[int]:
        if not self.ws:
            return None
        with self.lock:
            self.msg_id += 1
            cmd_id = self.msg_id
        msg: Dict[str, Any] = {"id": cmd_id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        try:
            self.ws.send(json.dumps(msg))
            return cmd_id
        except Exception:
            return None

    def find_game_tab(self) -> Optional[dict]:
        try:
            tabs = requests.get(f"http://{self.host}:{self.port}/json/list", timeout=3).json()
        except Exception:
            return None
        pages = [t for t in tabs if t.get("type") == "page"]
        if self.tab_id:
            for tab in pages:
                if tab.get("id") == self.tab_id:
                    return tab
        for tab in pages:
            if self.url_filter in (tab.get("url") or "").lower():
                return tab
        return None

    def _install(self, session_id: Optional[str] = None) -> None:
        self._cmd("Network.enable", {
            "maxResourceBufferSize": 64 * 1024 * 1024,
            "maxTotalBufferSize": 256 * 1024 * 1024,
        }, session_id)

    # --------------------------------------------------------------- records
    def _duplicate(self, data: bytes) -> bool:
        now = time.time()
        for key, seen in list(self.recent.items()):
            if now - seen > 15:
                self.recent.pop(key, None)
        key = hashlib.md5(data).hexdigest()
        if key in self.recent:
            return True
        self.recent[key] = now
        return False

    def _record(self, kind: str, url: str, data: bytes) -> None:
        if not data or len(data) > MAX_BODY_BYTES:
            self.stats["ignored"] += 1
            return
        if self._duplicate(data):
            return
        with self.lock:
            self.counter += 1
            idx = self.counter
        self.stats[kind] = self.stats.get(kind, 0) + 1
        record = {
            "idx": idx,
            "kind": kind,
            "ts": time.time(),
            "url": safe_url(url),
            "size": len(data),
            "routes": routes_of(data),
            "b64": base64.b64encode(data).decode("ascii"),
        }
        if self.on_record:
            try:
                self.on_record(record)
            except Exception:
                pass

    # -------------------------------------------------------------- messages
    def _on_message(self, _ws, message: str) -> None:
        try:
            data = json.loads(message)
        except ValueError:
            return

        method = data.get("method", "")
        params = data.get("params", {})
        sid = data.get("sessionId")

        if method == "Target.attachedToTarget":
            child = params.get("sessionId")
            if child and params.get("targetInfo", {}).get("type") in ("page", "iframe", "worker"):
                self._install(child)
                self._cmd("Runtime.runIfWaitingForDebugger", None, child)
            return

        if method == "Network.responseReceived":
            url = params.get("response", {}).get("url", "")
            if is_game_url(url):
                self.responses[(sid, params.get("requestId"))] = {"url": url, "sid": sid}
            return

        if method == "Network.loadingFinished":
            meta = self.responses.pop((sid, params.get("requestId")), None)
            if meta:
                cmd = self._cmd("Network.getResponseBody", {"requestId": params.get("requestId")}, sid)
                if cmd is not None:
                    self.body_calls[cmd] = meta
            return

        if method == "Network.loadingFailed":
            self.responses.pop((sid, params.get("requestId")), None)
            return

        if method == "Network.webSocketCreated":
            self.websockets[(sid, params.get("requestId"))] = params.get("url", "")
            return

        if method == "Network.webSocketFrameReceived":
            url = self.websockets.get((sid, params.get("requestId")), "websocket")
            resp = params.get("response", {})
            payload = resp.get("payloadData", "")
            if payload:
                try:
                    body = base64.b64decode(payload) if resp.get("opcode") == 2 else payload.encode("utf-8")
                except ValueError:
                    return
                self._record("WS_IN", url, body)
            return

        if "id" in data and data["id"] in self.body_calls:
            meta = self.body_calls.pop(data["id"])
            result = data.get("result") or {}
            body = result.get("body")
            if body is None:
                return
            try:
                raw = base64.b64decode(body) if result.get("base64Encoded") else body.encode("utf-8")
            except ValueError:
                return
            self._record("RESP", meta["url"], raw)

    # ------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        if self.is_running:
            return True
        tab = self.find_game_tab()
        if not tab or not tab.get("webSocketDebuggerUrl"):
            self._status(f"No Total Battle tab found in the Chrome on port {self.port}.")
            return False

        self.stats = {"RESP": 0, "WS_IN": 0, "ignored": 0}
        self.responses.clear()
        self.body_calls.clear()
        self.recent.clear()
        self.counter = 0
        opened = threading.Event()

        def on_open(_ws):
            self._cmd("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
            self._install()
            opened.set()

        def on_close(*_):
            if self.is_running:
                self._status("The connection that listens to the game's traffic dropped.")

        self.ws = websocket.WebSocketApp(
            tab["webSocketDebuggerUrl"],
            on_open=on_open,
            on_message=self._on_message,
            on_error=lambda *_: None,
            on_close=on_close,
        )
        self.is_running = True
        threading.Thread(target=lambda: self.ws.run_forever(suppress_origin=True), daemon=True).start()
        if not opened.wait(10):
            self._status("Could not attach to the game tab to listen to its traffic.")
            self.stop()
            return False
        return True

    def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        # Give the bodies asked for last a moment to arrive.
        deadline = time.time() + 2.0
        while self.body_calls and time.time() < deadline:
            time.sleep(0.1)
        try:
            if self.ws:
                self._cmd("Network.disable")
                self.ws.close()
        except Exception:
            pass
        self.ws = None
