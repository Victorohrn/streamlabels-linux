"""Label state: remembers events, derives label values, persists to JSON.

File names and formats follow the official Stream Labels app (periods session_/weekly_/
monthly_/30day_/all_time_/total_, "name: amount" lists, "X gifted N times", trains ...)
so a scene built against the Windows app keeps working with the same file names.
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import re
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config
from .events import BITS, DONATION, FOLLOW, RAID, SUB_GIFT, SUBSCRIPTION, Event

log = logging.getLogger(__name__)

STATE_VERSION = 2
HISTORY_CAP = 2000  # per all-time list of follows/subs/raids/gifts (donations and bits are kept)
PERIODS = ("session", "weekly", "monthly", "30day", "all_time")
TRAINS = {  # label prefix -> (record list name, name-label suffix)
    "donationtrain": ("donations", "latest_donator"),
    "followtrain": ("follows", "latest_follower"),
    "subtrain": ("subs", "latest_sub"),
}
SUB_POINTS = {"1000": 1, "2000": 2, "3000": 6, "prime": 1}
CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "kr": "SEK", "¥": "JPY", "zł": "PLN", "₽": "RUB"}


def _empty_session() -> dict:
    return {"started": time.time(), "follows": [], "subs": [], "gifts": [], "donations": [],
            "bits": [], "raids": []}


def _empty_state() -> dict:
    return {
        "version": STATE_VERSION,
        "session": _empty_session(),
        "all_time": {"follows": [], "subs": [], "gifts": [], "donations": [], "bits": [], "raids": [],
                     "totals": {"follower_count": None, "subscriber_count": None}},
        "server_labels": {},
        "legacy_labels": {},
        "legacy_imported": False,
    }


class _SafeDict(dict):
    def __missing__(self, key):  # leave unknown {tokens} untouched
        return "{" + key + "}"


def sub_points(tier: str | None) -> int:
    return SUB_POINTS.get(str(tier or "1000").lower(), 1)


def parse_amount(text: str) -> tuple[float, str]:
    """'$23.00' -> (23.0, 'USD'); '5,00 kr' -> (5.0, 'SEK'); '695' -> (695.0, '')."""
    t = text.strip()
    cur = ""
    for sym, code in CURRENCY_SYMBOLS.items():
        if sym in t:
            cur = code
            t = t.replace(sym, "")
            break
    m = re.search(r"-?\d[\d\s,]*(?:\.\d+)?", t)
    if not m:
        return 0.0, cur
    num = m.group(0).replace(" ", "")
    if "." not in num and re.search(r",\d{1,2}$", num):
        num = num.replace(",", ".")  # European decimal comma: "5,00"
    else:
        num = num.replace(",", "")  # thousands separator: "1,234.50"
    try:
        return float(num), cur
    except ValueError:
        return 0.0, cur


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
        if data.get("version") not in (1, STATE_VERSION):
            log.warning("state file version %r unsupported; starting fresh", data.get("version"))
            return
        base = _empty_state()
        base["session"].update(data.get("session", {}))
        base["all_time"].update(data.get("all_time", {}))
        base["all_time"]["totals"].update(data.get("all_time", {}).get("totals", {}))
        base["server_labels"] = dict(data.get("server_labels", {}))
        base["legacy_labels"] = dict(data.get("legacy_labels", {}))
        base["legacy_imported"] = bool(data.get("legacy_imported", False))
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
                follows.append({"ts": time.time(), "name": latest_follower, "amount": 0, "currency": "",
                                "formatted": "", "months": 0, "tier": "", "is_gift": False,
                                "is_resub": False, "seeded": True})
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

    # ------------------------------------------------------------------ legacy import
    def import_legacy(self, out_dir: Path, computed_keys: set[str] | None = None) -> dict:
        """Read an existing Stream Labels folder (from the Windows app) and keep its history.

        * every file becomes a fallback value (used while we have nothing better);
        * all_time_top_* lists are turned into timestamp-0 records so the all-time
          tops keep counting from the old totals instead of from zero.
        Files written by `streamlabels test` are backed up in `.before-test/`; those backups
        are read in preference to the sample values.
        """
        out_dir = Path(out_dir)
        backup = out_dir / ".before-test"
        found: dict[str, str] = {}
        if backup.is_dir():
            for f in backup.glob("*.txt"):
                found[f.stem] = f.read_text(encoding="utf-8", errors="replace").strip()
        test_written: set[str] = set()
        marker = out_dir / ".test-written"
        if marker.exists():
            test_written = set(marker.read_text().split())
        for f in out_dir.glob("*.txt"):
            if f.stem in found or f.stem in test_written:
                continue
            found[f.stem] = f.read_text(encoding="utf-8", errors="replace").strip()
        a = self.data["all_time"]
        imported = {"labels": 0, "donations": 0, "bits": 0, "gifts": 0}
        if not any(r.get("legacy") for r in a["donations"]):
            imported["donations"] = self._import_money(found.get("all_time_top_donators", ""),
                                                       found.get("all_time_top_donations", ""), a["donations"])
            imported["bits"] = self._import_money(found.get("all_time_top_cheerers", ""),
                                                  found.get("all_time_top_cheers", ""), a["bits"])
            for m in re.finditer(r"([^,]+?) gifted (\d+) times?", found.get("all_time_top_sub_gifters", "")):
                a["gifts"].append(self._legacy_rec(m.group(1).strip(), float(m.group(2))))
                imported["gifts"] += 1
        self.data["legacy_labels"] = {k: v for k, v in found.items() if v}
        imported["labels"] = len(self.data["legacy_labels"])
        self.data["legacy_imported"] = True
        self.dirty = True
        return imported

    @staticmethod
    def _legacy_rec(name: str, amount: float, currency: str = "") -> dict:
        return {"ts": 0, "name": name, "amount": amount, "currency": currency, "formatted": "",
                "months": 0, "tier": "", "is_gift": False, "is_resub": False, "legacy": True}

    @classmethod
    def _import_money(cls, totals_text: str, singles_text: str, into: list) -> int:
        """`name: $23.00, name2: $6.76` (per-person totals) + `name: $13.00, ...` (single events)."""
        def entries(text):
            for part in text.split(","):
                if ":" not in part:
                    continue
                name, amt = part.rsplit(":", 1)
                value, cur = parse_amount(amt)
                if name.strip() and value > 0:
                    yield name.strip(), value, cur
        singles: dict[str, list[tuple[float, str]]] = defaultdict(list)
        for name, value, cur in entries(singles_text):
            singles[name].append((value, cur))
        n = 0
        seen = set()
        for name, total, cur in entries(totals_text):
            seen.add(name)
            known = sum(v for v, _ in singles.get(name, []))
            for v, c in singles.get(name, []):
                into.append(cls._legacy_rec(name, v, c or cur))
                n += 1
            if total - known > 0.005:
                into.append(cls._legacy_rec(name, round(total - known, 2), cur))
                n += 1
        for name, vals in singles.items():  # single events whose person fell off the totals list
            if name not in seen:
                for v, c in vals:
                    into.append(cls._legacy_rec(name, v, c))
                    n += 1
        into.sort(key=lambda r: r.get("ts", 0))
        return n

    # ------------------------------------------------------------------ rendering
    def labels(self, cfg: Config, now: float | None = None) -> dict[str, str]:
        """All labels: computed, with legacy fallbacks and Streamlabs server labels layered on top."""
        now = time.time() if now is None else now
        out: dict[str, str] = {}
        s, a = self.data["session"], self.data["all_time"]
        totals = a["totals"]
        cur_default = next((r.get("currency") for r in reversed(a["donations"]) if r.get("currency")), "")

        def money(amount: float, currency: str = "", formatted: str = "") -> str:
            if formatted:
                return formatted
            try:
                return cfg.amount_format.format(amount=float(amount), currency=currency or cur_default).strip()
            except (KeyError, ValueError):
                return f"{amount} {currency}".strip()

        def tokens(rec: dict | None, **extra) -> dict:
            rec = rec or {}
            amt = float(rec.get("amount") or 0)
            t = {"name": rec.get("name", ""), "amount": amt, "currency": rec.get("currency", "") or cur_default,
                 "formatted": money(amt, rec.get("currency", ""), rec.get("formatted", "")) if rec else "",
                 "months": int(rec.get("months") or 0), "viewers": int(amt), "bits": int(amt),
                 "count": int(amt), "score": int(rec.get("score") or 0)}
            t.update(extra)
            return t

        def fmt(key: str, default_fmt: str, rec: dict | None, **extra) -> str:
            if rec is None:
                return ""
            f = cfg.formats.get(key, default_fmt)
            try:
                return f.format_map(_SafeDict(tokens(rec, **extra)))
            except (ValueError, IndexError):
                return f

        def put(key: str, default_fmt: str, rec: dict | None = None, **extra) -> None:
            out[key] = fmt(key, default_fmt, rec, **extra)

        def put_list(key: str, default_fmt: str, recs: list, limit: int | None = None) -> None:
            limit = cfg.list_limit if limit is None else limit
            if limit and limit > 0:
                recs = recs[:limit]
            out[key] = cfg.list_separator.join(fmt(key, default_fmt, r) for r in recs)

        def names(records: list) -> str:
            seen: list[str] = []
            for r in records:
                n = r.get("name", "")
                if n and n not in seen:
                    seen.append(n)
            if cfg.list_limit and cfg.list_limit > 0:
                seen = seen[-cfg.list_limit:]
            return cfg.list_separator.join(seen)

        def latest(records: list) -> dict | None:
            live = [r for r in records if not r.get("legacy")]
            return live[-1] if live else None

        def by_person(records: list, agg: str = "sum") -> list[dict]:
            """One synthetic record per person, sorted best first. agg: sum | max_months."""
            acc: dict[str, dict] = {}
            for r in records:
                n = r.get("name", "")
                if not n:
                    continue
                cur = acc.setdefault(n, {"name": n, "amount": 0.0, "currency": r.get("currency", ""),
                                         "months": 0, "formatted": ""})
                cur["amount"] += float(r.get("amount") or 0)
                cur["months"] = max(cur["months"], int(r.get("months") or 0))
                if r.get("currency"):
                    cur["currency"] = r["currency"]
            key = (lambda r: r["months"]) if agg == "max_months" else (lambda r: r["amount"])
            return sorted(acc.values(), key=key, reverse=True)

        def biggest(records: list) -> list[dict]:
            return sorted((r for r in records if r.get("name")), key=lambda r: float(r.get("amount") or 0), reverse=True)

        def total(records: list) -> float:
            return sum(float(r.get("amount") or 0) for r in records)

        def first(lst: list) -> dict | None:
            return lst[0] if lst else None

        # -- period windows over the all-time lists
        dt = datetime.fromtimestamp(now)
        week_start = (dt - timedelta(days=dt.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        month_start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        since = {"weekly": week_start, "monthly": month_start, "30day": now - 30 * 86400, "all_time": None}

        def period(kind: str, p: str) -> list:
            if p == "session":
                return list(s[kind])
            recs = a[kind]
            if since[p] is None:
                return list(recs)
            return [r for r in recs if r.get("ts", 0) >= since[p]]

        for p in PERIODS:
            don, bits, subs, gifts = period("donations", p), period("bits", p), period("subs", p), period("gifts", p)
            subs = subs if cfg.include_resubs else [r for r in subs if not r.get("is_resub")]
            # tips
            put(f"{p}_top_donator", "{name}: {formatted}", first(by_person(don)))
            put_list(f"{p}_top_donators", "{name}: {formatted}", by_person(don))
            put_list(f"{p}_top_donations", "{name}: {formatted}", biggest(don))
            out[f"{p}_donation_amount"] = money(total(don))
            # bits
            put(f"{p}_top_cheerer", "{name}: {bits}", first(by_person(bits)))
            put_list(f"{p}_top_cheerers", "{name}: {bits}", by_person(bits))
            put_list(f"{p}_top_cheers", "{name}: {bits}", biggest(bits))
            out[f"{p}_cheer_amount"] = str(int(total(bits)))
            # subs
            put(f"{p}_top_subscriber", "{name}: {months}", first(by_person(subs, "max_months")))
            put_list(f"{p}_top_subscribers", "{name}: {months}", by_person(subs, "max_months"))
            out[f"{p}_subscriber_count"] = str(len(subs))
            out[f"{p}_subscriber_score"] = str(sum(sub_points(r.get("tier")) for r in subs))
            # gifted subs
            put(f"{p}_top_sub_gifter", "{name} gifted {count} times", first(by_person(gifts)))
            put_list(f"{p}_top_sub_gifters", "{name} gifted {count} times", by_person(gifts))
            out[f"{p}_gifted_sub_count"] = str(int(total(gifts)))

        # the official "total_" family
        out["total_cheer_amount"] = out["all_time_cheer_amount"]
        out["total_donation_amount"] = out["all_time_donation_amount"]
        out["total_subscriber_score"] = out["all_time_subscriber_score"]
        out["total_follower_count"] = "" if totals["follower_count"] is None else str(totals["follower_count"])
        out["total_subscriber_count"] = "" if totals["subscriber_count"] is None else str(totals["subscriber_count"])

        # -- most recent (all time) and session lists
        subs_a = a["subs"] if cfg.include_resubs else [r for r in a["subs"] if not r.get("is_resub")]
        subs_s = s["subs"] if cfg.include_resubs else [r for r in s["subs"] if not r.get("is_resub")]
        resubs_a = [r for r in a["subs"] if r.get("is_resub")]
        resubs_s = [r for r in s["subs"] if r.get("is_resub")]
        put("most_recent_follower", "{name}", latest(a["follows"]))
        put("most_recent_subscriber", "{name}", latest(subs_a))
        put("most_recent_resubscriber", "{name}", latest(resubs_a))
        put("most_recent_sub_gifter", "{name}", latest(a["gifts"]))
        put("most_recent_donator", "{name}", latest(a["donations"]))
        put("most_recent_donation_amount", "{formatted}", latest(a["donations"]))
        put("most_recent_cheerer", "{name}", latest(a["bits"]))
        put("most_recent_cheer_amount", "{bits}", latest(a["bits"]))
        put("most_recent_raider", "{name}", latest(a["raids"]))
        put("most_recent_raid_viewers", "{viewers}", latest(a["raids"]))

        put("session_most_recent_follower", "{name}", latest(s["follows"]))
        out["session_followers"] = names(s["follows"])
        out["session_follower_count"] = str(len(s["follows"]))
        put("session_most_recent_subscriber", "{name}", latest(subs_s))
        out["session_subscribers"] = names(subs_s)
        put("session_most_recent_resubscriber", "{name}", latest(resubs_s))
        out["session_resubscribers"] = names(resubs_s)
        put("session_most_recent_sub_gifter", "{name}", latest(s["gifts"]))
        out["session_sub_gifters"] = names(s["gifts"])
        put("session_most_recent_donator", "{name}", latest(s["donations"]))
        out["session_donators"] = names(s["donations"])
        out["session_donator_count"] = str(len(s["donations"]))
        put("session_most_recent_cheerer", "{name}", latest(s["bits"]))
        out["session_cheerers"] = names(s["bits"])
        out["session_cheerer_count"] = str(len(s["bits"]))
        put("session_most_recent_raider", "{name}", latest(s["raids"]))
        out["session_raiders"] = names(s["raids"])
        out["session_raider_count"] = str(len(s["raids"]))
        put("session_top_raider", "{name}: {viewers}", first(biggest(s["raids"])))

        # -- trains (client-side, like the official app): events less than train_duration apart chain up
        for prefix, (kind, name_key) in TRAINS.items():
            recs = [r for r in a[kind] if not r.get("legacy")]
            train: list[dict] = []
            for r in recs:
                if train and r["ts"] - train[-1]["ts"] > cfg.train_duration:
                    train = []
                train.append(r)
            active = bool(train) and now - train[-1]["ts"] < cfg.train_duration
            if active:
                remaining = int(cfg.train_duration - (now - train[-1]["ts"]))
                out[f"{prefix}_clock"] = f"{remaining // 60}:{remaining % 60:02d}"
                out[f"{prefix}_counter"] = str(len(train))
                out[f"{prefix}_{name_key}"] = train[-1]["name"]
                if prefix == "donationtrain":
                    out["donationtrain_latest_amount"] = money(train[-1]["amount"], train[-1].get("currency", ""),
                                                               train[-1].get("formatted", ""))
                    out["donationtrain_total_amount"] = money(total(train))
            else:
                out[f"{prefix}_clock"] = "0:00"
                out[f"{prefix}_counter"] = "0"
                out[f"{prefix}_{name_key}"] = ""
                if prefix == "donationtrain":
                    out["donationtrain_latest_amount"] = "0"
                    out["donationtrain_total_amount"] = "0"

        # -- fallbacks and overrides
        for key, value in self.data.get("legacy_labels", {}).items():
            if key not in out:
                out[key] = value  # labels we do not compute at all (e.g. donation_goal, monthly_* donors)
            elif not out[key] and (key.startswith(("all_time_", "total_", "most_recent_"))):
                out[key] = value  # old history until we have something better
        out.update(self.data.get("server_labels", {}))  # Streamlabs' own values always win
        return out

    def train_active(self, cfg: Config, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        for kind, _ in TRAINS.values():
            recs = self.data["all_time"][kind]
            if recs and not recs[-1].get("legacy") and now - recs[-1]["ts"] < cfg.train_duration:
                return True
        return False
