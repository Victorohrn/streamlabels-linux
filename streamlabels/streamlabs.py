"""Streamlabs Socket API source.

Connects to https://sockets.streamlabs.com with the account's *Socket API token*
(dashboard -> Settings -> API Settings -> API Tokens). This is the same server
Streamlabs Desktop uses; it relays Streamlabs tips plus the Twitch follows,
subscriptions, bits and raids that Streamlabs already receives for alerts.
Payload formats: https://dev.streamlabs.com/docs/socket-api
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time

import socketio

from .events import BITS, DONATION, FOLLOW, RAID, SUB_GIFT, SUBSCRIPTION, Event, ServerLabels

log = logging.getLogger(__name__)

SOCKET_URL = "https://sockets.streamlabs.com"
TWITCH_FOR = {"twitch_account"}
IGNORED_TYPES = {
    "alertPlaying", "alertProfileChanged", "pauseQueue", "unpauseQueue", "eventsPanelSettingsUpdate",
    "mediaSharingSettingsUpdate", "safeModeEnabled", "safeModeDisabled", "userStateUpdated",
    "visionEvent", "streamlabs_prime_subscribe", "fm-ext-enabled", "host",
}
MYSTERY_GIFT_WINDOW = 120.0  # seconds to ignore per-recipient gifter credit after a mass gift


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v, default=0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def parse_event(payload: dict, *, count_test_events: bool, twitch_relay: bool,
                gift_memory: dict[str, float] | None = None) -> list[Event | ServerLabels]:
    """Translate one raw socket `event` payload into normalised events.

    `gift_memory` (gifter -> expiry timestamp) stops a mass gift ("submysterygift",
    N subs) from also being credited once per recipient "subgift" event.
    """
    if not isinstance(payload, dict):
        return []
    gift_memory = {} if gift_memory is None else gift_memory
    etype = str(payload.get("type", ""))
    target = str(payload.get("for", "") or "")
    msg = payload.get("message")

    if etype == "streamlabels":
        data = msg.get("data") if isinstance(msg, dict) else None
        if isinstance(data, dict):
            return [ServerLabels({str(k): "" if v is None else str(v) for k, v in data.items()})]
        return []
    if etype in IGNORED_TYPES:
        return []

    items = msg if isinstance(msg, list) else ([msg] if isinstance(msg, dict) else [])
    is_test_payload = bool(payload.get("isTest"))
    out: list[Event | ServerLabels] = []
    now = time.time()
    for item in items:
        if not isinstance(item, dict):
            continue
        is_test = is_test_payload or bool(item.get("isTest"))
        if is_test and not count_test_events:
            log.info("ignoring Streamlabs TEST %s event (set count_test_events=true to count them)", etype)
            continue
        name = str(item.get("name") or item.get("display_name") or item.get("from") or "").strip()
        base = dict(source="streamlabs", is_test=is_test, message=str(item.get("message") or ""))

        if etype == "donation":
            out.append(Event(DONATION, name or "Anonymous", amount=_f(item.get("amount")),
                             currency=str(item.get("currency") or ""),
                             formatted_amount=str(item.get("formatted_amount") or ""), **base))
            continue

        # Everything below is platform-relayed. Only Twitch is handled.
        if target and target not in TWITCH_FOR:
            log.debug("ignoring %s event for %s", etype, target)
            continue
        if not twitch_relay:
            log.debug("ignoring relayed Twitch %s (Twitch source handles it)", etype)
            continue

        if etype == "follow":
            if name:
                out.append(Event(FOLLOW, name, **base))
        elif etype == "subscription":
            sub_type = str(item.get("sub_type") or "").lower()
            months = _i(item.get("months"), 1) or 1
            gifter = str(item.get("gifter") or "").strip()
            if sub_type in ("submysterygift", "anonsubmysterygift"):
                who = gifter or name or "Anonymous"
                count = _i(item.get("amount"), 1) or 1
                gift_memory[who] = now + MYSTERY_GIFT_WINDOW
                out.append(Event(SUB_GIFT, who, amount=count, **base))
                continue
            if not name:
                continue
            is_gift = sub_type in ("subgift", "anonsubgift") or bool(gifter)
            is_resub = sub_type == "resub" or (months > 1 and not is_gift)
            out.append(Event(SUBSCRIPTION, name, months=months, is_gift=is_gift, is_resub=is_resub,
                             tier=str(item.get("sub_plan") or ""), **base))
            if is_gift:
                who = gifter or "Anonymous"
                if gift_memory.get(who, 0.0) < now:
                    out.append(Event(SUB_GIFT, who, amount=1, **base))
        elif etype == "bits":
            out.append(Event(BITS, name or "Anonymous", amount=_i(item.get("amount")), **base))
        elif etype in ("raid", "raids"):
            if name:
                out.append(Event(RAID, name, amount=_i(item.get("raiders") or item.get("viewers")), **base))
        else:
            log.debug("unhandled Streamlabs event type %r: %s", etype, json.dumps(item)[:300])
    return out


class StreamlabsSource(threading.Thread):
    """Background thread that keeps a Socket.IO connection open and feeds the queue."""

    def __init__(self, token: str, out: queue.Queue, *, count_test_events: bool,
                 twitch_relay: bool, dump: bool = False, url: str = SOCKET_URL):
        super().__init__(name="streamlabs", daemon=True)
        self.token = token
        self.url = url
        self.out = out
        self.count_test_events = count_test_events
        self.twitch_relay = twitch_relay
        self.dump = dump
        self._stop = threading.Event()
        self._sio: socketio.Client | None = None
        self._gift_memory: dict[str, float] = {}
        self.connected = False

    def stop(self) -> None:
        self._stop.set()
        sio = self._sio
        if sio is not None:
            try:
                sio.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def _on_event(self, payload) -> None:
        if self.dump:
            log.info("RAW %s", json.dumps(payload, ensure_ascii=False)[:2000])
        try:
            for ev in parse_event(payload, count_test_events=self.count_test_events,
                                  twitch_relay=self.twitch_relay, gift_memory=self._gift_memory):
                self.out.put(ev)
        except Exception:  # noqa: BLE001
            log.exception("failed to parse Streamlabs event: %r", payload)

    def run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            sio = socketio.Client(reconnection=True, reconnection_delay=2, reconnection_delay_max=60,
                                  logger=False, engineio_logger=False)
            self._sio = sio

            @sio.event
            def connect():
                self.connected = True
                log.info("Streamlabs socket connected")

            @sio.event
            def disconnect(reason=None):
                self.connected = False
                log.warning("Streamlabs socket disconnected (%s)", reason)

            @sio.event
            def connect_error(data):
                msg = data.get("message") if isinstance(data, dict) else data
                if str(msg).lower().startswith("authentication"):
                    log.error("Streamlabs rejected the socket token (%s). Re-copy it from "
                              "streamlabs.com/dashboard#/settings/api-settings", msg)
                else:
                    log.warning("Streamlabs connect error: %s", msg)

            sio.on("event", self._on_event)
            try:
                sio.connect(f"{self.url}?token={self.token}", transports=["websocket"], wait_timeout=20)
                backoff = 2.0
                sio.wait()  # returns when the connection ends for good
            except Exception as exc:  # noqa: BLE001
                log.warning("Streamlabs connection failed: %s", exc)
            finally:
                self.connected = False
                try:
                    sio.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            if self._stop.is_set():
                break
            log.info("retrying Streamlabs connection in %.0fs", backoff)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 120.0)
