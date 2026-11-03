from streamlabels.events import BITS, FOLLOW, RAID, SUB_GIFT, SUBSCRIPTION
from streamlabels.twitch import parse_notification, subscription_specs


def test_parse_notifications():
    assert parse_notification("channel.follow", {"user_name": "F"}).kind == FOLLOW
    s = parse_notification("channel.subscribe", {"user_name": "S", "tier": "1000", "is_gift": True})
    assert s.kind == SUBSCRIPTION and s.is_gift and not s.is_resub
    r = parse_notification("channel.subscription.message", {"user_name": "R", "cumulative_months": 14,
                                                            "message": {"text": "yo"}})
    assert r.is_resub and r.months == 14 and r.message == "yo"
    g = parse_notification("channel.subscription.gift", {"user_name": None, "is_anonymous": True, "total": 10})
    assert g.kind == SUB_GIFT and g.name == "Anonymous" and g.amount == 10
    b = parse_notification("channel.cheer", {"user_name": "B", "bits": 300, "message": "pog"})
    assert b.kind == BITS and b.amount == 300
    raid = parse_notification("channel.raid", {"from_broadcaster_user_name": "X", "viewers": 7})
    assert raid.kind == RAID and raid.name == "X" and raid.amount == 7
    assert parse_notification("channel.unknown", {}) is None


def test_subscription_specs():
    specs = dict((t, (v, c)) for t, v, c in subscription_specs("42"))
    assert specs["channel.follow"] == ("2", {"broadcaster_user_id": "42", "moderator_user_id": "42"})
    assert specs["channel.raid"] == ("1", {"to_broadcaster_user_id": "42"})
