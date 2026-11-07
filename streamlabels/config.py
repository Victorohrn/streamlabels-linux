"""Configuration (TOML) and well-known paths."""

from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def _xdg(var: str, fallback: str) -> Path:
    return Path(os.environ.get(var) or (Path.home() / fallback))


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "streamlabels"
STATE_DIR = _xdg("XDG_STATE_HOME", ".local/state") / "streamlabels"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/streamlabels-{os.getuid()}")
CONFIG_PATH = CONFIG_DIR / "config.toml"
TWITCH_TOKEN_PATH = CONFIG_DIR / "twitch-token.json"
PID_PATH = RUNTIME_DIR / "streamlabels.pid"


def default_output_dir() -> Path:
    """<Documents>/StreamLabels, honouring localised XDG user dirs (e.g. ~/Dokument)."""
    docs = ""
    try:
        docs = subprocess.run(
            ["xdg-user-dir", "DOCUMENTS"], capture_output=True, text=True, timeout=3
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    base = Path(docs) if docs and Path(docs).is_dir() else Path.home()
    return base / "StreamLabels"


@dataclass
class StreamlabsConfig:
    enabled: bool = True
    socket_token: str = ""
    # Streamlabs test alerts (dashboard "Test Follow" etc.) also arrive on the socket.
    count_test_events: bool = False
    # Streamlabs relays Twitch follows/subs/bits/raids too. "auto" uses them only when the
    # Twitch source is disabled (avoids double counting); "always"/"never" force it.
    relay_twitch_events: str = "auto"


@dataclass
class TwitchConfig:
    enabled: bool = False
    client_id: str = ""
    # Seconds between re-reading follower/subscriber totals from the Twitch API.
    poll_interval: int = 600


@dataclass
class Config:
    output_dir: Path = field(default_factory=default_output_dir)
    state_file: Path = field(default_factory=lambda: STATE_DIR / "state.json")
    list_separator: str = ", "
    list_limit: int = 25
    amount_format: str = "{amount:.2f} {currency}"
    include_resubs: bool = True
    session_reset_on_start: bool = True
    train_duration: int = 300
    log_level: str = "INFO"
    raw_log: bool = True
    ignore_names: list[str] = field(default_factory=lambda: ["Kevin"])
    formats: dict[str, str] = field(default_factory=dict)
    streamlabs: StreamlabsConfig = field(default_factory=StreamlabsConfig)
    twitch: TwitchConfig = field(default_factory=TwitchConfig)

    @property
    def twitch_relay_enabled(self) -> bool:
        mode = self.streamlabs.relay_twitch_events
        if mode == "always":
            return True
        if mode == "never":
            return False
        return not self.twitch.enabled


def _expand(p: str | Path) -> Path:
    return Path(os.path.expandvars(str(p))).expanduser()


def load_config(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    cfg = Config()
    if not path.exists():
        return cfg
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    if "output_dir" in raw:
        cfg.output_dir = _expand(raw["output_dir"])
    if "state_file" in raw:
        cfg.state_file = _expand(raw["state_file"])
    for key in ("list_separator", "list_limit", "amount_format", "include_resubs",
                "session_reset_on_start", "train_duration", "log_level", "raw_log"):
        if key in raw:
            setattr(cfg, key, raw[key])
    if "ignore_names" in raw:
        cfg.ignore_names = [str(x) for x in raw["ignore_names"]]
    cfg.formats = {str(k): str(v) for k, v in raw.get("formats", {}).items()}

    sl = raw.get("streamlabs", {})
    cfg.streamlabs = StreamlabsConfig(
        enabled=bool(sl.get("enabled", True)),
        socket_token=str(sl.get("socket_token", "")).strip(),
        count_test_events=bool(sl.get("count_test_events", False)),
        relay_twitch_events=str(sl.get("relay_twitch_events", "auto")),
    )
    tw = raw.get("twitch", {})
    cfg.twitch = TwitchConfig(
        enabled=bool(tw.get("enabled", False)),
        client_id=str(tw.get("client_id", "")).strip(),
        poll_interval=int(tw.get("poll_interval", 600)),
    )
    return cfg


EXAMPLE_CONFIG = """\
# streamlabels configuration. Paths may use ~ and $VARS.
# Where the .txt files go. Point OBS text sources at files in this folder.
#output_dir = "~/Documents/StreamLabels"

# Lists such as session_followers: separator and max number of names (0 = unlimited).
list_separator = ", "
list_limit = 25

# How money amounts are rendered when Streamlabs did not send a pre-formatted amount.
amount_format = "{amount:.2f} {currency}"

# Count resubscriptions as subscribers in most_recent_subscriber / session_subscribers.
include_resubs = true

# Start a fresh "session" (session_* labels) every time the daemon starts.
# You can also reset at any time with: streamlabels reset-session
session_reset_on_start = true

# Trains (followtrain_*, subtrain_*, donationtrain_*): events closer together than this many
# seconds chain into one train; the clock counts down from here after the last event.
train_duration = 300

# Streamlabs' dashboard test buttons do not always flag their events as tests. Events from these
# names (case-insensitive) are never counted; "Kevin" is Streamlabs' stock test follower/gifter.
# Scrub already-counted ones with: streamlabels forget NAME [NAME ...]
ignore_names = ["Kevin"]

# Keep every raw event in ~/.local/state/streamlabels/events.jsonl (5 MB, rotated once) for debugging.
raw_log = true

[streamlabs]
enabled = true
# Streamlabs dashboard -> Settings -> API Settings -> API Tokens -> "Your Socket API Token"
socket_token = ""
# Streamlabs test alerts from the dashboard also arrive here; count them?
count_test_events = false
# auto | always | never - use Twitch follows/subs/bits/raids relayed by Streamlabs
relay_twitch_events = "auto"

[twitch]
# Optional: talk to Twitch directly. Adds total_follower_count / total_subscriber_count,
# seeds most_recent_follower on startup and receives events even if Streamlabs is down.
# Needs a Client ID from https://dev.twitch.tv/console/apps (type "Public client"),
# then run: streamlabels twitch-login
enabled = false
client_id = ""
poll_interval = 600

[formats]
# Override how any label is rendered. Tokens: {name} {amount} {currency} {formatted}
# {months} {viewers} {bits} {count}. Unknown tokens are left as-is. Defaults match the official
# app ("name: amount" for tops, "name gifted N times" for gifters, bare name for most_recent_*). Examples:
#most_recent_donator = "{name} - {formatted}"
#all_time_top_donator = "{name}"
#session_top_subscriber = "{name} ({months} months)"
"""
