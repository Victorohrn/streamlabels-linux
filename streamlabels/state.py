"""Label state: remembers events, derives label values, persists to JSON.

Label names follow the ones Streamlabs Desktop uses for its Stream Labels
(most_recent_follower, session_follower_count, all_time_top_donator, ...), so an
OBS scene built for the Windows app keeps working with the same file names.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from .config import Config
from .events import BITS, DONATION, FOLLOW, RAID, SUB_GIFT, SUBSCRIPTION, Event

log = logging.getLogger(__name__)

STATE_VERSION = 1
HISTORY_CAP = 500  # per all-time list of follows/subs/raids/gifts (donations and bits are kept)
THIRTY_DAYS = 30 * 24 * 3600


def _empty_session() -> dict:
    return {
        "started": time.time(),
        "follows": [],
        "subs": [],
        "gifts": [],
        "donations": [],
        "bits": [],
        "raids": [],
    }


def _empty_state() -> dict:
    return {
        "version": STATE_VERSION,
        "session": _empty_session(),
        "all_time": {
            "follows": [],
            "subs": [],
            "gifts": [],
            "donations": [],
            "bits": [],
            "raids": [],
            "totals": {"follower_count": None, "subscriber_count": None},
        },
        "server_labels": {},
    }


class _SafeDict(dict):
    def __missing__(self, key):  # leave unknown {tokens} untouched
        return "{" + key + "}"


class LabelState:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = _empty_state()
        self.dirty = False

    # ------------------------------------------------------------------ persistence
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            log.warning("could not read %s (%s); starting with empty state", self.path, exc)
            return
        if data.get("version") != STATE_VERSION:
            log.warning("state file version %r unsupported; starting fresh", data.get("version"))
            return
        base = _empty_state()
        base["session"].update(data.get("session", {}))
        base["all_time"].update(data.get("all_time", {}))
        base["all_time"]["totals"].update(data.get("all_time", {}).get("totals", {}))
        base["server_labels"] = dict(data.get("server_labels", {}))
        self.data = base

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".state.", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=1)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self.dirty = False

    # ------------------------------------------------------------------ mutation
    def reset_session(self) -> None:
        self.data["session"] = _empty_session()
        self.dirty = True
        log.info("session reset")

    def set_server_labels(self, labels: dict[str, str]) -> None:
        self.data["server_labels"].update({str(k): "" if v is None else str(v) for k, v in labels.items()})
        self.dirty = True

    def seed_totals(self, follower_count: int | None = None, subscriber_count: int | None = None,
                    latest_follower: str | None = None) -> None:
        totals = self.data["all_time"]["totals"]
        if follower_count is not None:
            totals["follower_count"] = int(follower_count)
        if subscriber_count is not None:
            totals["subscriber_count"] = int(subscriber_count)
        if latest_follower:
            follows = self.data["all_time"]["follows"]
            if not follows or follows[-1]["name"] != latest_follower:
                follows.append({"ts": time.time(), "name": latest_follower, "amount": 0,
                                "currency": "", "formatted": "", "months": 0,
                                "is_gift": False, "is_resub": False, "seeded": True})
                del follows[:-HISTORY_CAP]
        self.dirty = True

    def apply(self, ev: Event) -> None:
        rec = ev.record()
        s, a = self.data["session"], self.data["all_time"]
        totals = a["totals"]
        if ev.kind == FOLLOW:
            self._push(s["follows"], a["follows"], rec)
            if totals["follower_count"] is not None:
                totals["follower_count"] += 1
        elif ev.kind == SUBSCRIPTION:
            self._push(s["subs"], a["subs"], rec)
            if totals["subscriber_count"] is not None and not ev.is_resub:
                totals["subscriber_count"] += 1
        elif ev.kind == SUB_GIFT:
            if rec["amount"] <= 0:
                rec["amount"] = 1
            self._push(s["gifts"], a["gifts"], rec)
        elif ev.kind == DONATION:
            self._push(s["donations"], a["donations"], rec, cap=False)
        elif ev.kind == BITS:
            self._push(s["bits"], a["bits"], rec, cap=False)
        elif ev.kind == RAID:
            self._push(s["raids"], a["raids"], rec)
        else:
            log.debug("ignoring event kind %r", ev.kind)
            return
        self.dirty = True

    @staticmethod
    def _push(session_list: list, all_list: list, rec: dict, cap: bool = True) -> None:
        session_list.append(rec)
        all_list.append(rec)
        if cap:
            del session_list[:-HISTORY_CAP]
            del all_list[:-HISTORY_CAP]

    # ------------------------------------------------------------------ rendering
    def labels(self, cfg: Config) -> dict[str, str]:
        """All computed labels, with Streamlabs-provided server labels layered on top."""
        out: dict[str, str] = {}
        s, a = self.data["session"], self.data["all_time"]
        totals = a["totals"]
        now = time.time()

        def money(rec: dict | None) -> str:
            if not rec:
                return ""
            if rec.get("formatted"):
                return rec["formatted"]
            try:
                return cfg.amount_format.format(amount=float(rec.get("amount") or 0),
                                                currency=rec.get("currency", "")).strip()
            except (KeyError, ValueError):
                return f"{rec.get('amount', '')} {rec.get('currency', '')}".strip()

        def tokens(rec: dict | None, **extra) -> dict:
            rec = rec or {}
            amt = float(rec.get("amount") or 0)
            t = {
                "name": rec.get("name", ""),
                "amount": amt,
                "currency": rec.get("currency", ""),
                "formatted": money(rec) if rec else "",
                "months": int(rec.get("months") or 0),
                "viewers": int(amt),
                "bits": int(amt),
                "count": int(amt),
            }
            t.update(extra)
            return t

        def put(key: str, default_fmt: str, rec: dict | None = None, **extra) -> None:
            fmt = cfg.formats.get(key, default_fmt)
            try:
                out[key] = fmt.format_map(_SafeDict(tokens(rec, **extra)))
            except (ValueError, IndexError):
                out[key] = fmt

        def names(records: list, limit: int | None = None) -> str:
            seen: list[str] = []
            for r in records:
                n = r.get("name", "")
                if n and n not in seen:
                    seen.append(n)
            limit = cfg.list_limit if limit is None else limit
            if limit and limit > 0:
                seen = seen[-limit:]
            return cfg.list_separator.join(seen)

        def latest(records: list) -> dict | None:
            return records[-1] if records else None

        def top(records: list, since: float | None = None) -> dict | None:
            """Person with the highest cumulative amount (optionally within a time window)."""
            sums: dict[str, float] = defaultdict(float)
            meta: dict[str, dict] = {}
            for r in records:
                if since is not None and r.get("ts", 0) < since:
                    continue
                n = r.get("name", "")
                if not n:
                    continue
                sums[n] += float(r.get("amount") or 0)
                meta[n] = r
            if not sums:
                return None
            best = max(sums, key=sums.get)
            rec = dict(meta[best])
            rec["amount"] = sums[best]
            rec["formatted"] = ""  # cumulative: re-render with amount_format
            if not rec.get("currency"):
                rec["currency"] = ""
            return rec

        def biggest(records: list) -> dict | None:
            """Single largest event."""
            if not records:
                return None
            return max(records, key=lambda r: float(r.get("amount") or 0))

        def total(records: list) -> float:
            return sum(float(r.get("amount") or 0) for r in records)

        subs_s = s["subs"] if cfg.include_resubs else [r for r in s["subs"] if not r.get("is_resub")]
        subs_a = a["subs"] if cfg.include_resubs else [r for r in a["subs"] if not r.get("is_resub")]
        cur = next((r.get("currency") for r in reversed(a["donations"]) if r.get("currency")), "")

        # -- followers
        put("most_recent_follower", "{name}", latest(a["follows"]))
        put("session_most_recent_follower", "{name}", latest(s["follows"]))
        out["session_followers"] = names(s["follows"])
        out["session_follower_count"] = str(len(s["follows"]))
        out["total_follower_count"] = "" if totals["follower_count"] is None else str(totals["follower_count"])

        # -- subscribers
        put("most_recent_subscriber", "{name}", latest(subs_a))
        put("session_most_recent_subscriber", "{name}", latest(subs_s))
        out["session_subscribers"] = names(subs_s)
        out["session_subscriber_count"] = str(len(subs_s))
        out["total_subscriber_count"] = "" if totals["subscriber_count"] is None else str(totals["subscriber_count"])

        # -- sub gifters
        put("most_recent_sub_gifter", "{name}", latest(a["gifts"]))
        put("session_most_recent_sub_gifter", "{name}", latest(s["gifts"]))
        out["session_sub_gifters"] = names(s["gifts"])
        out["session_sub_gifter_count"] = str(len({r["name"] for r in s["gifts"]}))
        out["session_gifted_sub_count"] = str(int(total(s["gifts"])))
        put("session_top_sub_gifter", "{name}", top(s["gifts"]))
        put("all_time_top_sub_gifter", "{name}", top(a["gifts"]))

        # -- donations / tips
        put("most_recent_donator", "{name}", latest(a["donations"]))
        put("most_recent_donation_amount", "{formatted}", latest(a["donations"]))
        put("session_most_recent_donator", "{name}", latest(s["donations"]))
        out["session_donators"] = names(s["donations"])
        out["session_donator_count"] = str(len(s["donations"]))
        put("session_donation_amount", "{formatted}",
            {"amount": total(s["donations"]), "currency": cur})
        put("session_top_donator", "{name}", top(s["donations"]))
        put("session_top_donation", "{formatted}", top(s["donations"]))
        put("session_biggest_donation", "{formatted}", biggest(s["donations"]))
        put("all_time_top_donator", "{name}", top(a["donations"]))
        put("all_time_top_donation", "{formatted}", top(a["donations"]))
        put("30day_top_donator", "{name}", top(a["donations"], now - THIRTY_DAYS))
        put("30day_top_donation", "{formatted}", top(a["donations"], now - THIRTY_DAYS))
        put("total_donation_amount", "{formatted}",
            {"amount": total(a["donations"]), "currency": cur})

        # -- bits / cheers
        put("most_recent_cheerer", "{name}", latest(a["bits"]))
        put("most_recent_cheer_amount", "{bits}", latest(a["bits"]))
        put("session_most_recent_cheerer", "{name}", latest(s["bits"]))
        out["session_cheerers"] = names(s["bits"])
        out["session_cheerer_count"] = str(len(s["bits"]))
        out["session_bits_amount"] = str(int(total(s["bits"])))
        put("session_top_cheerer", "{name}", top(s["bits"]))
        put("session_top_cheer", "{bits}", top(s["bits"]))
        put("all_time_top_cheerer", "{name}", top(a["bits"]))
        put("all_time_top_cheer", "{bits}", top(a["bits"]))
        put("30day_top_cheerer", "{name}", top(a["bits"], now - THIRTY_DAYS))

        # -- raids
        put("most_recent_raider", "{name}", latest(a["raids"]))
        put("most_recent_raid_viewers", "{viewers}", latest(a["raids"]))
        put("session_most_recent_raider", "{name}", latest(s["raids"]))
        out["session_raiders"] = names(s["raids"])
        out["session_raider_count"] = str(len(s["raids"]))
        put("session_top_raider", "{name}", biggest(s["raids"]))

        # Streamlabs-computed labels win for identical keys (they have the full account history).
        out.update(self.data.get("server_labels", {}))
        return out
