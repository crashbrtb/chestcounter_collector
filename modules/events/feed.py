"""The Journal over the network, through the socket the game itself keeps open.

What the game sends on `journal-feed-kafka` (measured on the live game, 01/11/2026):

    [{2002062134: pack([method, call]) + pack(0)}] + pack(arguments)

and the server answers `[{733765024: pack([call, code, text])}]` followed by the
result, matched by `call`. The socket is already authenticated, so no session goes
in these. Calls are numbered from CALLS_FROM on, far from the game's own count: its
heartbeat goes on undisturbed.

The socket is not reachable from the page's globals. It is taken from the game's
next message: WebSocket.prototype.send is wrapped until then (a few seconds, 15 at
the most) and put back at once, so nothing stays installed in the page.
Runtime.queryObjects would find it directly but crashed the game's tab
(STATUS_ACCESS_VIOLATION, 01/11/2026).

Two methods are used: the list and a ranking. The game's third one, which marks a
card as read, is not sent.
"""
from __future__ import annotations

import base64
import json
import threading
from typing import Any, List, Optional

from ..chests.game_link import PROFILES_PER_REQUEST, PROFILES_ROUTE, GameLink
from . import tbcodec
from .journal import JournalCollector
from .traffic import GameLinkError

CHANNEL = "00000000-0000-4000-8000-000000000004"   # the channel of the Journal's cards
LIST_METHOD = 1778507014
RANKING_METHOD = 2663587727
CALL_KEY = 2002062134
ANSWER_KEY = str(733765024)   # the codec gives map keys back as text

CALLS_FROM = 1 << 30
ENTRIES_PER_PAGE = 50
ANSWER_WAIT = 30.0
SOCKET_WAIT = 40.0
SOCKET_ATTEMPTS = 3   # right after a switch of profile the game may still be opening it

_FIND_SOCKET = f"""new Promise(done=>{{
const send=WebSocket.prototype.send;
const end=found=>{{WebSocket.prototype.send=send;done(found)}};
WebSocket.prototype.send=function(data){{
if(typeof data!=='string'&&this.url.includes('journal'))end(this);
return send.apply(this,arguments)}};
setTimeout(()=>end(null),{int(SOCKET_WAIT * 1000) - 2000})}})"""


def build_call(method: int, call: int, arguments: Any) -> bytes:
    return tbcodec.pack([{CALL_KEY: tbcodec.pack([method, call]) + tbcodec.pack(0)}]) + tbcodec.pack(arguments)


def parse_answer(data: bytes) -> Optional[tuple]:
    """(call, code, text, result objects) of a frame answering a call; None for anything else."""
    stream = tbcodec.unpack_stream(data)
    if not stream or not isinstance(stream[0], list) or not stream[0]:
        return None
    first = stream[0][0]
    if not isinstance(first, dict) or not isinstance(first.get(ANSWER_KEY), bytes):
        return None
    header = tbcodec.unpack_stream(first[ANSWER_KEY])
    if not header or not isinstance(header[0], list) or len(header[0]) < 3:
        return None
    call, code, text = header[0][:3]
    return call, code, text, stream[1:]


class JournalFeed(GameLink):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.socket: Optional[str] = None
        self.call_number = CALLS_FROM
        self.calls: dict = {}         # call -> {"done": Event, "answer": ...}

    # -------------------------------------------------------------- listening
    def _on_message(self, ws, message: str) -> None:
        if '"Network.webSocketFrameReceived"' not in message[:80]:
            super()._on_message(ws, message)
            return
        if not self.calls:
            return
        response = json.loads(message).get("params", {}).get("response", {})
        if response.get("opcode") != 2:
            return
        answer = parse_answer(base64.b64decode(response.get("payloadData", "")))
        waiting = self.calls.get(answer[0]) if answer else None
        if waiting:
            waiting["answer"] = answer
            waiting["done"].set()

    # ----------------------------------------------------------------- asking
    def _runtime(self, method: str, params: dict, timeout: float) -> dict:
        waiting = {"done": threading.Event(), "result": None}
        with self.lock:
            self.msg_id += 1
            command = self.msg_id
            self.answers[command] = waiting
        try:
            self.ws.send(json.dumps({"id": command, "method": method, "params": params}))
            if not waiting["done"].wait(timeout):
                raise GameLinkError("the game's tab did not answer in time")
        except GameLinkError:
            raise
        except Exception as exc:
            raise GameLinkError(f"the connection to the game tab failed ({type(exc).__name__})") from None
        finally:
            self.answers.pop(command, None)
        return waiting["result"]

    def _find_socket(self) -> str:
        for _attempt in range(SOCKET_ATTEMPTS):
            found = self._runtime("Runtime.evaluate", {"expression": _FIND_SOCKET, "awaitPromise": True,
                                                       "objectGroup": "journal-feed"}, SOCKET_WAIT)
            socket = (found.get("result") or {}).get("objectId")
            if socket:
                return socket
        raise GameLinkError("the game sent nothing on the Journal's socket")

    def _call(self, method: int, arguments: Any) -> List[Any]:
        if self.socket is None:
            self.socket = self._find_socket()
        self.call_number += 1
        call = self.call_number
        waiting = {"done": threading.Event(), "answer": None}
        self.calls[call] = waiting
        try:
            sent = self._runtime("Runtime.callFunctionOn", {
                "objectId": self.socket, "returnByValue": True,
                "arguments": [{"value": base64.b64encode(build_call(method, call, arguments)).decode("ascii")}],
                "functionDeclaration": "function(body){this.send(Uint8Array.from(atob(body),c=>c.charCodeAt(0)));"
                                       "return this.readyState}",
            }, ANSWER_WAIT)
            if (sent.get("result") or {}).get("value") != 1:
                self.socket = None   # closed, or gone with a reload: the next call looks for it again
                raise GameLinkError("the Journal's socket is closed")
            if not waiting["done"].wait(ANSWER_WAIT):
                raise GameLinkError(f"the game did not answer call {method}")
        finally:
            self.calls.pop(call, None)
        _call, code, text, result = waiting["answer"]
        if code:
            raise GameLinkError(f"the game refused call {method}: {code} {str(text)[:200]}")
        return result

    # ------------------------------------------------------------ the Journal
    def journal_page(self, cursor: Optional[int] = None, size: int = ENTRIES_PER_PAGE) -> List[list]:
        """The next entries of the Journal, newest first, after `cursor` (an entry's [2]). Empty at the end."""
        result = self._call(LIST_METHOD, [CHANNEL, cursor, 0, False, size, None])
        return result[0][0] if result and isinstance(result[0], list) and result[0] else []

    def ranking(self, entry_uid: str, result_uid: str) -> List[Any]:
        """The objects answering for one ranking, as the game sends them, for JournalCollector."""
        return self._call(RANKING_METHOD, [entry_uid, [result_uid]])

    def fetch_profiles(self, player_ids: List[int], collector: JournalCollector) -> None:
        """Hands the collector the profiles of these players, as many as the game knows."""
        for start in range(0, len(player_ids), PROFILES_PER_REQUEST):
            ids = player_ids[start:start + PROFILES_PER_REQUEST]
            for frame in self.call(PROFILES_ROUTE, [[[player] for player in ids]]):
                collector.feed_tree({"frames": [{"route": PROFILES_ROUTE, "objects": frame.objects}]})

    def stop(self) -> None:
        if self.socket is not None and self.ws:
            self._cmd("Runtime.releaseObjectGroup", {"objectGroup": "journal-feed"})
            self.socket = None
        super().stop()
