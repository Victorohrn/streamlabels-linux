"""Optional direct Twitch source: device-code login, Helix totals, EventSub websocket.

Twitch docs: https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/#device-code-grant-flow
             https://dev.twitch.tv/docs/eventsub/handling-websocket-events/
"""

from __future__ import annotations

import json
import logging
import os
import queue
import tempfile
import threading
import time
from pathlib import Path

import requests
import websocket  # websocket-client, pulled in by python-socketio[client]

from .config import TWITCH_TOKEN_PATH
from .events import BITS, FOLLOW, RAID, SUB_GIFT, SUBSCRIPTION, Event

log = logging.getLogger(__name__)

AUTH = "https://id.twitch.tv/oauth2"
HELIX = "https://api.twitch.tv/helix"
WS_URL = "wss://eventsub.wss.twitch.tv/ws"
SCOPES = ["moderator:read:followers", "channel:read:subscriptions", "bits:read"]
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class TwitchAuthError(Exception):
    pass


class TwitchAuth:
    def __init__(self, client_id: str, token_path: Path = TWITCH_TOKEN_PATH):
        if not client_id:
            raise TwitchAuthError("twitch.client_id is not set in the config")
        self.client_id = client_id
        self.token_path = Path(token_path)
        self._lock = threading.Lock()

    # -- storage
    def load(self) -> dict | None:
        try:
            with open(self.token_path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def save(self, tok: dict) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".twitch-token.", dir=self.token_path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(tok, fh, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.token_path)

    # -- flows
    def device_login(self, say=print) -> dict:
        r = requests.post(f"{AUTH}/device", data={"client_id": self.client_id,
                                                   "scopes": " ".join(SCOPES)}, timeout=15)
        if r.status_code != 200:
            raise TwitchAuthError(f"device code request failed: {r.status_code} {r.text}")
        d = r.json()
        say(f"\nOpen this URL in a browser where you are logged in to the streamer's Twitch account:\n"
            f"  {d['verification_uri']}\n"
            f"and enter the code: {d['user_code']}\n"
            f"(expires in {int(d.get('expires_in', 1800)) // 60} minutes) ... waiting")
        interval = int(d.get("interval", 5))
        deadline = time.time() + int(d.get("expires_in", 1800))
        while time.time() < deadline:
            time.sleep(interval)
            r = requests.post(f"{AUTH}/token", data={
                "client_id": self.client_id, "scopes": " ".join(SCOPES),
                "device_code": d["device_code"], "grant_type": DEVICE_GRANT}, timeout=15)
            if r.status_code == 200:
                tok = r.json()
                tok["client_id"] = self.client_id
                tok["obtained_at"] = time.time()
                self.save(tok)
                return tok
            try:
                msg = str(r.json().get("message", "")).lower()
            except ValueError:
                msg = r.text.lower()
            if "authorization_pending" in msg:
                continue
            if "slow_down" in msg:
                interval += 5
                continue
            raise TwitchAuthError(f"login failed: {msg or r.text}")
        raise TwitchAuthError("login timed out; run twitch-login again")

    def refresh(self, tok: dict) -> dict:
        r = requests.post(f"{AUTH}/token", data={
            "client_id": self.client_id, "grant_type": "refresh_token",
            "refresh_token": tok.get("refresh_token", "")}, timeout=15)
        if r.status_code != 200:
            raise TwitchAuthError(f"token refresh failed ({r.status_code}): {r.text}. "
                                  "Run: streamlabels twitch-login")
        new = r.json()
        new["client_id"] = self.client_id
        new["obtained_at"] = time.time()
        self.save(new)
        return new

    def validate(self, access_token: str) -> dict | None:
        r = requests.get(f"{AUTH}/validate", headers={"Authorization": f"OAuth {access_token}"}, timeout=15)
        return r.json() if r.status_code == 200 else None

    def ensure(self) -> tuple[str, str, str]:
        """Return (access_token, user_id, login), refreshing when needed."""
        with self._lock:
            tok = self.load()
            if not tok:
                raise TwitchAuthError("not logged in to Twitch; run: streamlabels twitch-login")
            info = self.validate(tok.get("access_token", ""))
            if not info or int(info.get("expires_in", 0)) < 900:
                tok = self.refresh(tok)
                info = self.validate(tok["access_token"])
                if not info:
                    raise TwitchAuthError("refreshed token did not validate; run: streamlabels twitch-login")
            missing = set(SCOPES) - set(info.get("scopes") or [])
            if missing:
                log.warning("Twitch token is missing scopes %s; run twitch-login again", sorted(missing))
            return tok["access_token"], str(info["user_id"]), str(info.get("login", ""))


class TwitchHelix:
    def __init__(self, client_id: str, access_token: str):
        self.s = requests.Session()
        self.s.headers.update({"Client-Id": client_id, "Authorization": f"Bearer {access_token}"})

    def get(self, path: str, **params) -> dict:
        r = self.s.get(f"{HELIX}/{path}", params=params, timeout=15)
        if r.status_code != 200:
            raise TwitchAuthError(f"GET {path} -> {r.status_code}: {r.text[:200]}")
        return r.json()

    def followers(self, broadcaster_id: str) -> tuple[int, str | None]:
        d = self.get("channels/followers", broadcaster_id=broadcaster_id, first=1)
        latest = d["data"][0].get("user_name") if d.get("data") else None
        return int(d.get("total", 0)), latest

    def subscriptions(self, broadcaster_id: str) -> int:
        d = self.get("subscriptions", broadcaster_id=broadcaster_id, first=1)
        return int(d.get("total", 0))

    def create_ws_subscription(self, session_id: str, sub_type: str, version: str, condition: dict) -> None:
        r = self.s.post(f"{HELIX}/eventsub/subscriptions", json={
            "type": sub_type, "version": version, "condition": condition,
            "transport": {"method": "websocket", "session_id": session_id}}, timeout=15)
        if r.status_code not in (200, 202):
            raise TwitchAuthError(f"subscribe {sub_type} -> {r.status_code}: {r.text[:300]}")


def subscription_specs(broadcaster_id: str) -> list[tuple[str, str, dict]]:
    b = {"broadcaster_user_id": broadcaster_id}
    return [
        ("channel.follow", "2", {**b, "moderator_user_id": broadcaster_id}),
        ("channel.subscribe", "1", b),
        ("channel.subscription.message", "1", b),
        ("channel.subscription.gift", "1", b),
        ("channel.cheer", "1", b),
        ("channel.raid", "1", {"to_broadcaster_user_id": broadcaster_id}),
    ]


def parse_notification(sub_type: str, ev: dict) -> Event | None:
    name = str(ev.get("user_name") or ev.get("user_login") or "").strip()
    anon = bool(ev.get("is_anonymous"))
    if sub_type == "channel.follow":
        return Event(FOLLOW, name, source="twitch") if name else None
    if sub_type == "channel.subscribe":
        return Event(SUBSCRIPTION, name, months=1, is_gift=bool(ev.get("is_gift")), source="twitch") if name else None
    if sub_type == "channel.subscription.message":
        return Event(SUBSCRIPTION, name, months=int(ev.get("cumulative_months") or 1), is_resub=True,
                     message=str(ev.get("message", {}).get("text", "")), source="twitch") if name else None
    if sub_type == "channel.subscription.gift":
        return Event(SUB_GIFT, "Anonymous" if anon or not name else name,
                     amount=int(ev.get("total") or 1), source="twitch")
    if sub_type == "channel.cheer":
        return Event(BITS, "Anonymous" if anon or not name else name, amount=int(ev.get("bits") or 0),
                     message=str(ev.get("message") or ""), source="twitch")
    if sub_type == "channel.raid":
        frm = str(ev.get("from_broadcaster_user_name") or ev.get("from_broadcaster_user_login") or "")
        return Event(RAID, frm, amount=int(ev.get("viewers") or 0), source="twitch") if frm else None
    return None


class TwitchSource(threading.Thread):
    """EventSub websocket + periodic Helix totals, feeding the daemon queue."""

    def __init__(self, auth: TwitchAuth, out: queue.Queue, *, poll_interval: int = 600, dump: bool = False):
        super().__init__(name="twitch", daemon=True)
        self.auth = auth
        self.out = out
        self.poll_interval = max(60, int(poll_interval))
        self.dump = dump
        self._stop = threading.Event()
        self._ws: websocket.WebSocket | None = None
        self.connected = False

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    # -- helpers
    def _seed(self, helix: TwitchHelix, user_id: str) -> None:
        try:
            total_f, latest = helix.followers(user_id)
            total_s = helix.subscriptions(user_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Twitch totals poll failed: %s", exc)
            return
        self.out.put(("seed", {"follower_count": total_f, "subscriber_count": total_s,
                               "latest_follower": latest}))
        log.info("Twitch totals: %d followers (latest %s), %d subscribers", total_f, latest, total_s)

    def _poller(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self.poll_interval)
            if self._stop.is_set():
                break
            try:
                token, user_id, _ = self.auth.ensure()
                self._seed(TwitchHelix(self.auth.client_id, token), user_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("Twitch poll failed: %s", exc)

    def run(self) -> None:
        threading.Thread(target=self._poller, name="twitch-poll", daemon=True).start()
        backoff = 2.0
        url = WS_URL
        first = True
        while not self._stop.is_set():
            try:
                token, user_id, login = self.auth.ensure()
                helix = TwitchHelix(self.auth.client_id, token)
                if first:
                    self._seed(helix, user_id)
                    first = False
                url = self._session(url, helix, user_id, login)
                backoff = 2.0
                continue  # _session returned a reconnect_url
            except TwitchAuthError as exc:
                log.error("Twitch: %s", exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("Twitch EventSub connection ended: %s", exc)
            finally:
                self.connected = False
            if self._stop.is_set():
                break
            url = WS_URL
            log.info("retrying Twitch EventSub in %.0fs", backoff)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 120.0)

    def _session(self, url: str, helix: TwitchHelix, user_id: str, login: str) -> str:
        """Run one websocket session. Returns a reconnect URL, or raises when the socket dies."""
        ws = websocket.create_connection(url, timeout=30)
        self._ws = ws
        keepalive = 30
        try:
            while not self._stop.is_set():
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    raise RuntimeError(f"no keepalive within {keepalive + 10}s")
                if not raw:
                    raise RuntimeError("socket closed")
                msg = json.loads(raw)
                mtype = msg.get("metadata", {}).get("message_type")
                payload = msg.get("payload", {})
                if self.dump:
                    log.info("RAW twitch %s %s", mtype, json.dumps(payload, ensure_ascii=False)[:1500])
                if mtype == "session_welcome":
                    sess = payload["session"]
                    keepalive = int(sess.get("keepalive_timeout_seconds") or 30)
                    ws.settimeout(keepalive + 10)
                    if url == WS_URL:  # fresh session: (re)create subscriptions
                        for sub_type, version, cond in subscription_specs(user_id):
                            helix.create_ws_subscription(sess["id"], sub_type, version, cond)
                    self.connected = True
                    log.info("Twitch EventSub connected as %s (%d subscriptions)", login,
                             len(subscription_specs(user_id)))
                elif mtype == "session_keepalive":
                    continue
                elif mtype == "notification":
                    sub_type = msg["metadata"].get("subscription_type", "")
                    ev = parse_notification(sub_type, payload.get("event") or {})
                    if ev:
                        self.out.put(ev)
                elif mtype == "session_reconnect":
                    new_url = payload["session"]["reconnect_url"]
                    log.info("Twitch asked us to reconnect")
                    return new_url
                elif mtype == "revocation":
                    log.warning("Twitch revoked subscription: %s", json.dumps(payload)[:300])
                    raise RuntimeError("subscription revoked")
            raise RuntimeError("stopped")
        finally:
            self._ws = None
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
