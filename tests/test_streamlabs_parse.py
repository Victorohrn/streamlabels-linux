from streamlabels.events import BITS, DONATION, FOLLOW, RAID, SUB_GIFT, SUBSCRIPTION, ServerLabels
from streamlabels.streamlabs import parse_event


def P(payload, **kw):
    kw.setdefault("count_test_events", False)
    kw.setdefault("twitch_relay", True)
    return parse_event(payload, **kw)


def test_donation_from_docs():
    payload = {"type": "donation", "message": [{"id": 1, "name": "Alice", "amount": "5.00",
                                                "formatted_amount": "$5.00", "message": "hi",
                                                "currency": "USD", "from": "Alice", "to": {"name": "me"}}]}
    [ev] = P(payload)
    assert ev.kind == DONATION and ev.name == "Alice" and ev.amount == 5.0
    assert ev.currency == "USD" and ev.formatted_amount == "$5.00" and ev.message == "hi"


def test_follow_sub_bits_raid():
    assert P({"type": "follow", "for": "twitch_account", "message": [{"name": "f1", "id": "1"}]})[0].kind == FOLLOW
    [sub] = P({"type": "subscription", "for": "twitch_account",
               "message": [{"name": "s1", "months": 1, "sub_plan": "1000", "sub_type": "sub"}]})
    assert sub.kind == SUBSCRIPTION and not sub.is_resub and not sub.is_gift
    [resub] = P({"type": "subscription", "for": "twitch_account",
                 "message": [{"name": "s2", "months": 9, "sub_type": "resub"}]})
    assert resub.is_resub and resub.months == 9
    [bits] = P({"type": "bits", "for": "twitch_account", "message": [{"name": "b", "amount": "250"}]})
    assert bits.kind == BITS and bits.amount == 250
    [raid] = P({"type": "raid", "for": "twitch_account", "message": [{"name": "r", "raiders": 12}]})
    assert raid.kind == RAID and raid.amount == 12


def test_gift_subs_credit_gifter_once():
    mem = {}
    mass = P({"type": "subscription", "for": "twitch_account",
              "message": [{"name": "G", "gifter": "G", "sub_type": "subMysteryGift", "amount": 3}]}, gift_memory=mem)
    assert [e.kind for e in mass] == [SUB_GIFT] and mass[0].amount == 3
    single = P({"type": "subscription", "for": "twitch_account",
                "message": [{"name": "R1", "gifter": "G", "sub_type": "subgift", "months": 1}]}, gift_memory=mem)
    assert [e.kind for e in single] == [SUBSCRIPTION]  # gifter already credited by the mass gift
    assert single[0].is_gift
    lone = P({"type": "subscription", "for": "twitch_account",
              "message": [{"name": "R2", "gifter": "H", "sub_type": "subgift"}]}, gift_memory=mem)
    assert [e.kind for e in lone] == [SUBSCRIPTION, SUB_GIFT] and lone[1].name == "H"


def test_streamlabels_event_becomes_server_labels():
    [sl] = P({"type": "streamlabels", "message": {"data": {"most_recent_follower": "x", "n": 3}}})
    assert isinstance(sl, ServerLabels) and sl.data == {"most_recent_follower": "x", "n": "3"}


def test_test_events_are_ignored_by_default():
    payload = {"type": "follow", "for": "twitch_account", "message": [{"name": "t", "isTest": True}]}
    assert P(payload) == []
    assert P(payload, count_test_events=True)[0].is_test


def test_relay_disabled_keeps_donations_only():
    assert P({"type": "follow", "for": "twitch_account", "message": [{"name": "f"}]}, twitch_relay=False) == []
    assert P({"type": "donation", "message": [{"name": "d", "amount": 1}]}, twitch_relay=False)[0].kind == DONATION


def test_other_platforms_and_noise_ignored():
    assert P({"type": "follow", "for": "youtube_account", "message": [{"name": "y"}]}) == []
    assert P({"type": "alertPlaying", "message": {"type": "follow"}}) == []
    assert P("garbage") == []
    assert P({"type": "subscription", "for": "twitch_account", "message": "oops"}) == []
