"""Talks to the game over the network, from inside its own tab, without touching the screen.

What a request looks like (measured on the live game, 01/11/2026):

    [u32 LE total][u32 LE inner][[route, seq, [[player_id], session], ""]][objects][tail]

in the game's little-endian MessagePack (tbcodec), POSTed as raw bytes. The tail
holds the cursors of the events the client has seen; the server takes one
repeated, so the latest one the game sent is reused as it is.

Nothing here has to be known in advance: the player, the session, the address and
the tail are learned from the requests the game itself keeps sending (it asks for
something every couple of seconds, on any screen). The requests are sent with a
fetch inside the tab, so they leave from where the game's own do.

Unlike traffic.TrafficListener, this does read the body of the game's requests,
because that is where the session is. It stays in memory, in this object: it is
never logged and never written anywhere.
"""
from __future__ import annotations

import base64
import json
import struct
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from ..events import tbcodec
from ..events.traffic import GameLinkError, TrafficListener
from ..members.roster import CLAN_RECORD_WHOLE, PROFILES_PER_REQUEST, PROFILES_ROUTE, parse_profile_row
from .gifts import CLAN_ROUTE, OPEN_ROUTE, Gift, _clan_gifts

GAME_ADDRESS = "rubens-"

# The route of the frame the server answers with when it refuses a request.
REFUSED_ROUTE = 0xFFFFFFFE

# How long the game is given to send a request, to learn the session from.
SESSION_WAIT = 15.0
ANSWER_WAIT = 20.0


def build_request(route: int, seq: int, auth: Any, objects: Any, tail: bytes) -> bytes:
    body = tbcodec.pack([route, seq, auth, ""]) + tbcodec.pack(objects)
    inner = 8 + len(body)
    return struct.pack("<II", inner + len(tail), inner) + body + tail


def parse_request(data: bytes) -> Optional[Tuple[int, int, Any, bytes]]:
    """(route, seq, auth, tail) of a request of the game; None for anything else."""
    if len(data) < 8:
        return None
    total, inner = struct.unpack_from("<II", data)
    if total != len(data) or not 8 <= inner <= total:
        return None
    try:
        header = tbcodec.LEUnpacker(data, 8).read()
    except (tbcodec.TBDecodeError, RecursionError):
        return None
    if not (isinstance(header, list) and len(header) == 4 and isinstance(header[0], int)
            and isinstance(header[1], int) and isinstance(header[2], list)):
        return None
    return header[0], header[1], header[2], data[inner:]


def request_bytes(text: bytes) -> Optional[Tuple[int, int, Any, bytes]]:
    """
    A request as Chrome reports it. Its binary bodies come base64-encoded once more
    than the field promises, so both forms are tried: the size in the first bytes
    says which one is the request.
    """
    found = parse_request(text)
    if found:
        return found
    try:
        return parse_request(base64.b64decode(text, validate=True))
    except ValueError:
        return None


class GameLink(TrafficListener):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.address: Optional[str] = None
        self.auth: Any = None
        self.tail = b""
        self.seq = 0
        self.learned = threading.Event()
        self.bodies: dict = {}        # command id -> session of a request whose body was asked for
        self.answers: dict = {}       # command id -> {"done": Event, "result": ...}

    # ---------------------------------------------------------------- learning
    def _install(self, session_id: Optional[str] = None) -> None:
        self._cmd("Network.enable", {"maxPostDataSize": 1 << 20}, session_id)

    def _learn(self, url: str, text: bytes) -> None:
        found = request_bytes(text)
        if not found:
            return
        route, seq, auth, tail = found
        with self.lock:
            self.seq = max(self.seq, seq)
            self.address, self.auth = url, auth
            # A batch (route 2) carries its cursors inside; only a plain request has the tail.
            if route != tbcodec.ROUTE_BATCH and tail:
                self.tail = tail
        if self.tail:
            self.learned.set()

    def _on_message(self, _ws, message: str) -> None:
        try:
            data = json.loads(message)
        except ValueError:
            return
        method, params, sid = data.get("method", ""), data.get("params", {}), data.get("sessionId")

        if method == "Target.attachedToTarget":
            child = params.get("sessionId")
            if child and params.get("targetInfo", {}).get("type") in ("page", "iframe", "worker"):
                self._install(child)
                self._cmd("Runtime.runIfWaitingForDebugger", None, child)
        elif method == "Network.requestWillBeSent":
            request = params.get("request", {})
            url = request.get("url", "")
            if GAME_ADDRESS not in url or request.get("method") != "POST":
                return
            entries = request.get("postDataEntries") or []
            if entries and entries[0].get("bytes"):
                try:
                    self._learn(url, base64.b64decode("".join(e.get("bytes", "") for e in entries)))
                except ValueError:
                    pass
            elif request.get("hasPostData"):
                cmd = self._cmd("Network.getRequestPostData", {"requestId": params.get("requestId")}, sid)
                if cmd is not None:
                    self.bodies[cmd] = url
        elif data.get("id") in self.bodies:
            url = self.bodies.pop(data["id"])
            body = (data.get("result") or {}).get("postData")
            if body:
                self._learn(url, body.encode("latin-1", "replace"))
        elif data.get("id") in self.answers:
            waiting = self.answers[data["id"]]
            waiting["result"] = data.get("result") or {}
            waiting["done"].set()

    def start(self) -> bool:
        """Attaches to the game tab and waits to learn the session. False when it could not."""
        if not super().start():
            return False
        if self.learned.wait(SESSION_WAIT):
            return True
        self._status(f"The game sent no request in {SESSION_WAIT:g}s to learn its session from.")
        self.stop()
        return False

    # ----------------------------------------------------------------- asking
    def _fetch(self, body: bytes, timeout: float) -> bytes:
        script = (
            "(async()=>{"
            f"const r=await fetch({json.dumps(self.address)},{{method:'POST',"
            "headers:{'Content-Type':'application/octet-stream'},"
            f"signal:AbortSignal.timeout({int(timeout * 1000)}),"
            f"body:Uint8Array.from(atob({json.dumps(base64.b64encode(body).decode('ascii'))}),c=>c.charCodeAt(0))}});"
            "if(!r.ok)throw new Error('HTTP '+r.status);"
            "const b=new Uint8Array(await r.arrayBuffer());let s='';"
            "for(let i=0;i<b.length;i+=8192)s+=String.fromCharCode.apply(null,b.subarray(i,i+8192));"
            "return btoa(s);})()"
        )
        waiting = {"done": threading.Event(), "result": None}
        with self.lock:
            self.msg_id += 1
            cmd = self.msg_id
            self.answers[cmd] = waiting
        try:
            self.ws.send(json.dumps({"id": cmd, "method": "Runtime.evaluate", "params": {
                "expression": script, "awaitPromise": True, "returnByValue": True}}))
            if not waiting["done"].wait(timeout + 2.0):
                raise GameLinkError("the game's server did not answer in time")
        except GameLinkError:
            raise
        except Exception as exc:
            raise GameLinkError(f"the connection to the game tab failed ({type(exc).__name__})") from None
        finally:
            self.answers.pop(cmd, None)

        result = waiting["result"]
        value = (result.get("result") or {}).get("value")
        if result.get("exceptionDetails") or not isinstance(value, str):
            raise GameLinkError("the request could not be sent from the game tab")
        return base64.b64decode(value)

    def call(self, route: int, objects: Any, timeout: float = ANSWER_WAIT) -> List[tbcodec.Frame]:
        """Sends one request and returns the frames answering it, or raises GameLinkError."""
        with self.lock:
            self.seq += 1
            body = build_request(route, self.seq, self.auth, objects, self.tail)
        frames = list(tbcodec.iter_frames(tbcodec._decompress(self._fetch(body, timeout))))
        refused = next((frame for frame in frames if frame.route == REFUSED_ROUTE), None)
        if refused:
            raise GameLinkError(f"the game refused route {route}: {str(refused.objects)[:200]}")
        answers = [frame for frame in frames if frame.route == route and not frame.error]
        if not answers:
            raise GameLinkError(f"the game did not answer route {route}")
        return answers

    # -------------------------------------------------------------- the clan
    @property
    def player_id(self) -> Optional[int]:
        ids = self.auth[0] if self.auth else None
        return ids[0] if isinstance(ids, list) and ids else None

    def clan_id(self) -> Optional[int]:
        """The clan of the profile the game is on, from its own profile. None when it has no clan."""
        player = self.player_id
        for frame in self.call(PROFILES_ROUTE, [[[player]]]):
            rows = frame.objects[0][0] if frame.objects and frame.objects[0] else []
            for row in rows:
                member = parse_profile_row(row)
                if member and member.player_id == player:
                    return member.clan_id
        return None

    def player_names(self, player_ids: List[int]) -> Dict[int, str]:
        """Player id -> the name in the game, as many as it knows. It sends a whole clan's in one answer."""
        names: Dict[int, str] = {}
        for start in range(0, len(player_ids), PROFILES_PER_REQUEST):
            ids = player_ids[start:start + PROFILES_PER_REQUEST]
            for frame in self.call(PROFILES_ROUTE, [[[player] for player in ids]]):
                for row in frame.objects[0][0] if frame.objects and frame.objects[0] else []:
                    member = parse_profile_row(row)
                    if member:
                        names[member.player_id] = member.name
        return names

    def gifts(self, clan_id: int) -> List[Gift]:
        """Every gift waiting in the clan, oldest first."""
        for frame in self.call(CLAN_ROUTE, [[[[clan_id], CLAN_RECORD_WHOLE]]]):
            gifts = _clan_gifts(frame.objects)
            if gifts is not None:
                return gifts
        raise GameLinkError("the game's answer did not carry the list of gifts")

    def open_gift(self, gift: Gift) -> None:
        """Opens every chest of a gift. Returns once the game has said so; raises when it has not."""
        self.call(OPEN_ROUTE, [{gift.gift_id: gift.quantity}])


def pace(started_at: float, interval: float) -> float:
    """How long to wait so that requests start at least `interval` seconds apart."""
    return max(0.0, interval - (time.time() - started_at))
