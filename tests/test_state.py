import json
import time
from pathlib import Path

from streamlabels.config import Config
from streamlabels.events import BITS, DONATION, FOLLOW, RAID, SUB_GIFT, SUBSCRIPTION, Event
from streamlabels.state import LabelState


def make(tmp_path: Path) -> tuple[LabelState, Config]:
    cfg = Config(output_dir=tmp_path / "out", state_file=tmp_path / "state.json")
    st = LabelState(cfg.state_file)
    return st, cfg


def test_follow_and_session_labels(tmp_path):
    st, cfg = make(tmp_path)
    st.apply(Event(FOLLOW, "alice"))
    st.apply(Event(FOLLOW, "bob"))
    st.apply(Event(FOLLOW, "alice"))
    lab = st.labels(cfg)
    assert lab["most_recent_follower"] == "alice"
    assert lab["session_most_recent_follower"] == "alice"
    assert lab["session_followers"] == "alice, bob"  # unique, chronological
    assert lab["session_follower_count"] == "3"
    assert lab["total_follower_count"] == ""  # unknown until seeded


def test_seed_totals_and_increment(tmp_path):
    st, cfg = make(tmp_path)
    st.seed_totals(follower_count=100, subscriber_count=10, latest_follower="zed")
    assert st.labels(cfg)["most_recent_follower"] == "zed"
    st.apply(Event(FOLLOW, "new"))
    st.apply(Event(SUBSCRIPTION, "sub1"))
    st.apply(Event(SUBSCRIPTION, "sub1", months=2, is_resub=True))
    lab = st.labels(cfg)
    assert lab["total_follower_count"] == "101"
    assert lab["total_subscriber_count"] == "11"  # resub does not add
    assert lab["most_recent_follower"] == "new"


def test_donations_top_is_cumulative(tmp_path):
    st, cfg = make(tmp_path)
    st.apply(Event(DONATION, "small", amount=5, currency="SEK"))
    st.apply(Event(DONATION, "small", amount=5, currency="SEK"))
    st.apply(Event(DONATION, "big", amount=8, currency="SEK", formatted_amount="8,00 kr"))
    lab = st.labels(cfg)
    assert lab["most_recent_donator"] == "big"
    assert lab["most_recent_donation_amount"] == "8,00 kr"  # Streamlabs' own formatting kept
    assert lab["session_top_donator"] == "small"  # 10 > 8
    assert lab["session_top_donation"] == "10.00 SEK"
    assert lab["session_biggest_donation"] == "8,00 kr"
    assert lab["all_time_top_donator"] == "small"
    assert lab["30day_top_donator"] == "small"
    assert lab["session_donation_amount"] == "18.00 SEK"
    assert lab["total_donation_amount"] == "18.00 SEK"


def test_thirty_day_window(tmp_path):
    st, cfg = make(tmp_path)
    old = Event(DONATION, "ancient", amount=1000, currency="EUR")
    old.ts = time.time() - 40 * 86400
    st.apply(old)
    st.apply(Event(DONATION, "recent", amount=1, currency="EUR"))
    lab = st.labels(cfg)
    assert lab["all_time_top_donator"] == "ancient"
    assert lab["30day_top_donator"] == "recent"


def test_bits_raids_gifts(tmp_path):
    st, cfg = make(tmp_path)
    st.apply(Event(BITS, "c1", amount=100))
    st.apply(Event(BITS, "c2", amount=50))
    st.apply(Event(BITS, "c2", amount=60))
    st.apply(Event(RAID, "r1", amount=10))
    st.apply(Event(RAID, "r2", amount=99))
    st.apply(Event(SUB_GIFT, "g1", amount=5))
    st.apply(Event(SUB_GIFT, "g2", amount=1))
    st.apply(Event(SUBSCRIPTION, "recipient", is_gift=True))
    lab = st.labels(cfg)
    assert lab["most_recent_cheerer"] == "c2"
    assert lab["most_recent_cheer_amount"] == "60"
    assert lab["session_top_cheerer"] == "c2"
    assert lab["session_top_cheer"] == "110"
    assert lab["session_bits_amount"] == "210"
    assert lab["most_recent_raider"] == "r2"
    assert lab["most_recent_raid_viewers"] == "99"
    assert lab["session_top_raider"] == "r2"
    assert lab["most_recent_sub_gifter"] == "g2"
    assert lab["session_top_sub_gifter"] == "g1"
    assert lab["session_gifted_sub_count"] == "6"
    assert lab["session_sub_gifter_count"] == "2"
    assert lab["most_recent_subscriber"] == "recipient"


def test_include_resubs_false(tmp_path):
    st, cfg = make(tmp_path)
    cfg.include_resubs = False
    st.apply(Event(SUBSCRIPTION, "fresh"))
    st.apply(Event(SUBSCRIPTION, "veteran", months=12, is_resub=True))
    lab = st.labels(cfg)
    assert lab["most_recent_subscriber"] == "fresh"
    assert lab["session_subscribers"] == "fresh"
    cfg.include_resubs = True
    assert st.labels(cfg)["most_recent_subscriber"] == "veteran"


def test_formats_and_list_options(tmp_path):
    st, cfg = make(tmp_path)
    cfg.formats = {"most_recent_subscriber": "{name} x{months}", "most_recent_donator": "{name} ({formatted}) {unknown}"}
    cfg.list_separator = " | "
    cfg.list_limit = 2
    st.apply(Event(SUBSCRIPTION, "v", months=3, is_resub=True))
    st.apply(Event(DONATION, "d", amount=2.5, currency="USD"))
    for n in ("a", "b", "c"):
        st.apply(Event(FOLLOW, n))
    lab = st.labels(cfg)
    assert lab["most_recent_subscriber"] == "v x3"
    assert lab["most_recent_donator"] == "d (2.50 USD) {unknown}"
    assert lab["session_followers"] == "b | c"


def test_session_reset_keeps_all_time(tmp_path):
    st, cfg = make(tmp_path)
    st.apply(Event(FOLLOW, "a"))
    st.reset_session()
    lab = st.labels(cfg)
    assert lab["most_recent_follower"] == "a"
    assert lab["session_most_recent_follower"] == ""
    assert lab["session_follower_count"] == "0"


def test_persistence_roundtrip(tmp_path):
    st, cfg = make(tmp_path)
    st.apply(Event(DONATION, "d", amount=3, currency="EUR"))
    st.set_server_labels({"most_recent_follower": "from_server", "exotic_label": "x"})
    st.save()
    assert (tmp_path / "state.json").exists()
    st2 = LabelState(cfg.state_file)
    st2.load()
    lab = st2.labels(cfg)
    assert lab["most_recent_donator"] == "d"
    assert lab["most_recent_follower"] == "from_server"  # server value wins over computed ""
    assert lab["exotic_label"] == "x"
    # computed value does not override a server-provided key
    st2.apply(Event(FOLLOW, "local"))
    assert st2.labels(cfg)["most_recent_follower"] == "from_server"


def test_corrupt_state_file_is_tolerated(tmp_path):
    st, cfg = make(tmp_path)
    cfg.state_file.write_text("{not json")
    st.load()
    assert st.labels(cfg)["session_follower_count"] == "0"
