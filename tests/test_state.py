import time
from pathlib import Path

from streamlabels.config import Config
from streamlabels.events import BITS, DONATION, FOLLOW, RAID, SUB_GIFT, SUBSCRIPTION, Event
from streamlabels.state import LabelState, parse_amount


def make(tmp_path: Path) -> tuple[LabelState, Config]:
    cfg = Config(output_dir=tmp_path / "out", state_file=tmp_path / "state.json")
    return LabelState(cfg.state_file), cfg


def test_follow_and_session_labels(tmp_path):
    st, cfg = make(tmp_path)
    for n in ("alice", "bob", "alice"):
        st.apply(Event(FOLLOW, n))
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


def test_donation_labels_official_formats(tmp_path):
    st, cfg = make(tmp_path)
    st.apply(Event(DONATION, "small", amount=5, currency="USD"))
    st.apply(Event(DONATION, "small", amount=5, currency="USD"))
    st.apply(Event(DONATION, "big", amount=8, currency="USD", formatted_amount="$8.00"))
    lab = st.labels(cfg)
    assert lab["most_recent_donator"] == "big"
    assert lab["most_recent_donation_amount"] == "$8.00"  # Streamlabs' own formatting kept
    assert lab["session_top_donator"] == "small: 10.00 USD"  # cumulative per person
    assert lab["session_top_donators"] == "small: 10.00 USD, big: 8.00 USD"
    assert lab["session_top_donations"] == "big: $8.00, small: 5.00 USD, small: 5.00 USD"  # single tips
    assert lab["session_donation_amount"] == "18.00 USD"
    assert lab["all_time_top_donator"] == "small: 10.00 USD"
    assert lab["30day_top_donator"] == lab["weekly_top_donator"] == lab["monthly_top_donator"] == "small: 10.00 USD"
    assert lab["total_donation_amount"] == "18.00 USD"
    assert lab["session_donators"] == "small, big"


def test_time_windows(tmp_path):
    st, cfg = make(tmp_path)
    old = Event(DONATION, "ancient", amount=1000, currency="EUR")
    old.ts = time.time() - 40 * 86400
    st.apply(old)
    st.apply(Event(DONATION, "recent", amount=1, currency="EUR"))
    lab = st.labels(cfg)
    assert lab["all_time_top_donator"] == "ancient: 1000.00 EUR"
    assert lab["30day_top_donator"] == "recent: 1.00 EUR"
    assert lab["weekly_top_donator"] == "recent: 1.00 EUR"
    assert lab["all_time_donation_amount"] == "1001.00 EUR"
    assert lab["30day_donation_amount"] == "1.00 EUR"


def test_bits_raids_gifts_subs(tmp_path):
    st, cfg = make(tmp_path)
    st.apply(Event(BITS, "c1", amount=100))
    st.apply(Event(BITS, "c2", amount=50))
    st.apply(Event(BITS, "c2", amount=60))
    st.apply(Event(RAID, "r1", amount=10))
    st.apply(Event(RAID, "r2", amount=99))
    st.apply(Event(SUB_GIFT, "g1", amount=5))
    st.apply(Event(SUB_GIFT, "g2", amount=1))
    st.apply(Event(SUBSCRIPTION, "recipient", is_gift=True, tier="1000"))
    st.apply(Event(SUBSCRIPTION, "t3", tier="3000"))
    st.apply(Event(SUBSCRIPTION, "vet", months=27, is_resub=True, tier="Prime"))
    lab = st.labels(cfg)
    assert lab["most_recent_cheerer"] == "c2" and lab["most_recent_cheer_amount"] == "60"
    assert lab["session_top_cheerer"] == "c2: 110"
    assert lab["session_top_cheerers"] == "c2: 110, c1: 100"
    assert lab["session_top_cheers"] == "c1: 100, c2: 60, c2: 50"
    assert lab["session_cheer_amount"] == lab["total_cheer_amount"] == "210"
    assert lab["most_recent_raider"] == "r2" and lab["most_recent_raid_viewers"] == "99"
    assert lab["session_top_raider"] == "r2: 99"
    assert lab["most_recent_sub_gifter"] == "g2"
    assert lab["session_top_sub_gifter"] == "g1 gifted 5 times"
    assert lab["all_time_top_sub_gifters"] == "g1 gifted 5 times, g2 gifted 1 times"
    assert lab["session_gifted_sub_count"] == "6"
    assert lab["most_recent_subscriber"] == "vet"
    assert lab["most_recent_resubscriber"] == "vet"
    assert lab["session_subscriber_count"] == "3"
    assert lab["session_subscriber_score"] == "8"  # 1 + 6 + 1
    assert lab["session_top_subscriber"] == "vet: 27"
    assert lab["session_top_subscribers"] == "vet: 27, recipient: 0, t3: 0"


def test_include_resubs_false(tmp_path):
    st, cfg = make(tmp_path)
    cfg.include_resubs = False
    st.apply(Event(SUBSCRIPTION, "fresh"))
    st.apply(Event(SUBSCRIPTION, "veteran", months=12, is_resub=True))
    lab = st.labels(cfg)
    assert lab["most_recent_subscriber"] == "fresh"
    assert lab["session_subscribers"] == "fresh"
    assert lab["most_recent_resubscriber"] == "veteran"
    cfg.include_resubs = True
    assert st.labels(cfg)["most_recent_subscriber"] == "veteran"


def test_formats_and_list_options(tmp_path):
    st, cfg = make(tmp_path)
    cfg.formats = {"most_recent_subscriber": "{name} x{months}", "session_top_donator": "{name} ({formatted}) {unknown}",
                   "all_time_top_donators": "{name}"}
    cfg.list_separator = " | "
    cfg.list_limit = 2
    st.apply(Event(SUBSCRIPTION, "v", months=3, is_resub=True))
    st.apply(Event(DONATION, "d", amount=2.5, currency="USD"))
    st.apply(Event(DONATION, "e", amount=1, currency="USD"))
    st.apply(Event(DONATION, "f", amount=0.5, currency="USD"))
    for n in ("a", "b", "c"):
        st.apply(Event(FOLLOW, n))
    lab = st.labels(cfg)
    assert lab["most_recent_subscriber"] == "v x3"
    assert lab["session_top_donator"] == "d (2.50 USD) {unknown}"
    assert lab["all_time_top_donators"] == "d | e"  # limit applies to lists
    assert lab["session_followers"] == "b | c"


def test_trains(tmp_path):
    st, cfg = make(tmp_path)
    cfg.train_duration = 300
    t0 = 1_800_000_000.0
    lab = st.labels(cfg, now=t0)
    assert lab["followtrain_clock"] == "0:00" and lab["followtrain_counter"] == "0" and lab["followtrain_latest_follower"] == ""
    for i, n in enumerate(("a", "b", "c")):
        ev = Event(FOLLOW, n); ev.ts = t0 + i * 100
        st.apply(ev)
    d = Event(DONATION, "tipper", amount=4, currency="EUR"); d.ts = t0 + 200
    st.apply(d)
    lab = st.labels(cfg, now=t0 + 230)
    assert lab["followtrain_counter"] == "3" and lab["followtrain_latest_follower"] == "c"
    assert lab["followtrain_clock"] == "4:30"
    assert lab["donationtrain_counter"] == "1" and lab["donationtrain_latest_donator"] == "tipper"
    assert lab["donationtrain_latest_amount"] == "4.00 EUR" and lab["donationtrain_total_amount"] == "4.00 EUR"
    assert st.train_active(cfg, now=t0 + 230)
    lab = st.labels(cfg, now=t0 + 600)
    assert lab["followtrain_counter"] == "0" and lab["followtrain_clock"] == "0:00"
    assert not st.train_active(cfg, now=t0 + 600)
    # a follow long after the gap starts a fresh train of 1
    late = Event(FOLLOW, "z"); late.ts = t0 + 1000
    st.apply(late)
    assert st.labels(cfg, now=t0 + 1001)["followtrain_counter"] == "1"


def test_session_reset_keeps_all_time(tmp_path):
    st, cfg = make(tmp_path)
    st.apply(Event(FOLLOW, "a"))
    st.reset_session()
    lab = st.labels(cfg)
    assert lab["most_recent_follower"] == "a"
    assert lab["session_most_recent_follower"] == ""
    assert lab["session_follower_count"] == "0"


def test_persistence_roundtrip_and_server_precedence(tmp_path):
    st, cfg = make(tmp_path)
    st.apply(Event(DONATION, "d", amount=3, currency="EUR"))
    st.set_server_labels({"most_recent_follower": "from_server", "exotic_label": "x"})
    st.save()
    st2 = LabelState(cfg.state_file)
    st2.load()
    lab = st2.labels(cfg)
    assert lab["most_recent_donator"] == "d"
    assert lab["most_recent_follower"] == "from_server"
    assert lab["exotic_label"] == "x"
    st2.apply(Event(FOLLOW, "local"))
    assert st2.labels(cfg)["most_recent_follower"] == "from_server"


def test_corrupt_state_file_is_tolerated(tmp_path):
    st, cfg = make(tmp_path)
    cfg.state_file.write_text("{not json")
    st.load()
    assert st.labels(cfg)["session_follower_count"] == "0"


def test_parse_amount():
    assert parse_amount("$23.00") == (23.0, "USD")
    assert parse_amount("5,00 kr") == (5.0, "SEK")
    assert parse_amount("695") == (695.0, "")
    assert parse_amount("1,234.50 €") == (1234.5, "EUR")
    assert parse_amount("nothing") == (0.0, "")
    assert parse_amount("1 234,50 kr") == (1234.5, "SEK")
    assert parse_amount("$1,234") == (1234.0, "USD")


def test_import_legacy_windows_folder(tmp_path):
    """Mirrors a real folder left behind by the Windows app."""
    st, cfg = make(tmp_path)
    out = cfg.output_dir
    out.mkdir()
    (out / "all_time_top_donators.txt").write_text("hetochfet: $23.00, Urlove: $6.76, Hihat: $4.44")
    (out / "all_time_top_donations.txt").write_text("hetochfet: $13.00, Urlove: $6.76, hetochfet: $5.00")
    (out / "all_time_top_cheerers.txt").write_text("ScorpionKingdom: 695, Victorohrn1: 190, novalito1: 10")
    (out / "all_time_top_cheers.txt").write_text("AnAnonymousCheerer: 25, IdzaBun: 20, novalito1: 10")
    (out / "all_time_top_sub_gifters.txt").write_text("ScorpionKingdom gifted 2 times, ChriIleMedK gifted 1 times")
    (out / "donation_goal.txt").write_text("- Donation Goal: NEW KEYBOARD")
    (out / "most_recent_resubscriber.txt").write_text("Victorohrn1")
    (out / "session_top_cheerers.txt").write_text("novalito1: 10")  # stale session data must not leak
    (out / "most_recent_follower.txt").write_text("NewFollower")  # written by `streamlabels test`
    (out / ".test-written").write_text("most_recent_follower\n")
    found = st.import_legacy(out)
    assert found["donations"] == 5 and found["bits"] == 5 and found["gifts"] == 2  # tips: 13+5+remainder 5, 6.76, 4.44
    lab = st.labels(cfg)
    assert lab["all_time_top_donators"] == "hetochfet: 23.00 USD, Urlove: 6.76 USD, Hihat: 4.44 USD"
    assert lab["all_time_top_donations"] == "hetochfet: 13.00 USD, Urlove: 6.76 USD, hetochfet: 5.00 USD, hetochfet: 5.00 USD, Hihat: 4.44 USD"
    assert lab["all_time_top_cheerers"].startswith("ScorpionKingdom: 695, Victorohrn1: 190")
    assert lab["all_time_top_sub_gifter"] == "ScorpionKingdom gifted 2 times"
    assert lab["donation_goal"] == "- Donation Goal: NEW KEYBOARD"  # unknown file kept verbatim
    assert lab["most_recent_resubscriber"] == "Victorohrn1"  # fallback until a real resub
    assert lab["most_recent_follower"] == ""  # sample value from `test` was not imported
    assert lab["session_top_cheerers"] == ""  # session data never comes from legacy files
    assert lab["most_recent_donator"] == ""  # legacy records are not "most recent"
    assert lab["30day_top_donator"] == ""  # and not in time windows
    # new live events add on top of the imported totals
    st.apply(Event(DONATION, "Urlove", amount=20, currency="USD"))
    lab = st.labels(cfg)
    assert lab["all_time_top_donator"] == "Urlove: 26.76 USD"
    assert lab["most_recent_resubscriber"] == "Victorohrn1"
    st.apply(Event(SUBSCRIPTION, "someone", months=3, is_resub=True))
    assert st.labels(cfg)["most_recent_resubscriber"] == "someone"
    # importing twice does not double the history
    st.import_legacy(out)
    assert st.labels(cfg)["all_time_top_donator"] == "Urlove: 26.76 USD"


def test_forget_and_reload(tmp_path):
    st, cfg = make(tmp_path)
    st.apply(Event(FOLLOW, "Kevin"))
    st.apply(Event(FOLLOW, "real"))
    st.apply(Event(SUB_GIFT, "kevin", amount=1))
    st.apply(Event(SUBSCRIPTION, "reingerclaire2", is_gift=True))
    st.save()
    other = LabelState(cfg.state_file)
    other.load()
    assert other.forget(["KEVIN", "reingerclaire2"]) == 3
    other.save()
    st.reload()
    lab = st.labels(cfg)
    assert lab["most_recent_follower"] == "real"
    assert lab["session_follower_count"] == "1"
    assert lab["most_recent_sub_gifter"] == "" and lab["session_subscriber_count"] == "0"


def test_metadata_keys_scrubbed_from_old_state(tmp_path):
    st, cfg = make(tmp_path)
    st.data["server_labels"] = {"_id": "x", "priority": "10", "most_recent_follower": "f"}
    st.save()
    st.reload()
    assert st.data["server_labels"] == {"most_recent_follower": "f"}
    st.set_server_labels({"_id": "y", "total_cheer_amount": "5"})
    assert "_id" not in st.labels(cfg) and st.labels(cfg)["total_cheer_amount"] == "5"
