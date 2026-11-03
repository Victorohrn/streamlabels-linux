"""Normalised event model shared by all sources."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# Event kinds understood by the label state machine.
FOLLOW = "follow"
SUBSCRIPTION = "subscription"  # `name` is the (new or returning) subscriber
SUB_GIFT = "sub_gift"  # `name` is the gifter, `amount` is how many subs were gifted
DONATION = "donation"  # Streamlabs tip; `amount` in `currency`
BITS = "bits"  # `amount` is the number of bits
RAID = "raid"  # `amount` is the viewer count

KINDS = (FOLLOW, SUBSCRIPTION, SUB_GIFT, DONATION, BITS, RAID)


@dataclass
class Event:
    kind: str
    name: str
    amount: float = 0.0
    currency: str = ""
    formatted_amount: str = ""
    months: int = 0
    tier: str = ""  # Twitch sub tier: 1000 / 2000 / 3000 / prime
    is_gift: bool = False  # subscription that was gifted *to* `name`
    is_resub: bool = False
    message: str = ""
    source: str = ""  # streamlabs | twitch | test
    is_test: bool = False
    ts: float = field(default_factory=time.time)

    def record(self) -> dict:
        """Compact dict stored in the state file."""
        return {
            "ts": self.ts,
            "name": self.name,
            "amount": self.amount,
            "currency": self.currency,
            "formatted": self.formatted_amount,
            "months": self.months,
            "tier": self.tier,
            "is_gift": self.is_gift,
            "is_resub": self.is_resub,
        }


@dataclass
class ServerLabels:
    """A full set of ready-made label values pushed by the Streamlabs socket.

    Streamlabs Desktop receives these as `{"type": "streamlabels", "message": {"data": {...}}}`
    and writes each key straight into a text source. We do the same, and let them
    take precedence over our own computed values for the same key.
    """

    data: dict[str, str]
    source: str = "streamlabs"
