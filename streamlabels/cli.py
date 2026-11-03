"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

from . import __version__
from .config import CONFIG_PATH, EXAMPLE_CONFIG, PID_PATH, TWITCH_TOKEN_PATH, Config, load_config
from .events import BITS, DONATION, FOLLOW, RAID, SUB_GIFT, SUBSCRIPTION, Event
from .state import LabelState
from .writer import LabelWriter


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stderr)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _cfg(args) -> Config:
    path = Path(args.config).expanduser() if args.config else None
    cfg = load_config(path)
    if args.output_dir:
        cfg.output_dir = Path(args.output_dir).expanduser()
    return cfg


def cmd_init(args) -> int:
    path = Path(args.config).expanduser() if args.config else CONFIG_PATH
    if path.exists():
        print(f"config already exists: {path}")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(EXAMPLE_CONFIG, encoding="utf-8")
        os.chmod(path, 0o600)
        print(f"wrote {path}")
    cfg = _cfg(args)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    print(f"label files go to: {cfg.output_dir}")
    print(f"state file:        {cfg.state_file}")
    print("next: put your Streamlabs socket token in the config, then "
          "`systemctl --user enable --now streamlabels`")
    return 0


def cmd_run(args) -> int:
    cfg = _cfg(args)
    _setup_logging("DEBUG" if args.verbose else cfg.log_level)
    from .daemon import Daemon
    Daemon(cfg, dump=args.dump).run()
    return 0


SAMPLE_EVENTS = [
    Event(FOLLOW, "NewFollower"),
    Event(SUBSCRIPTION, "LoyalViewer", months=1),
    Event(SUBSCRIPTION, "ReturningFan", months=7, is_resub=True),
    Event(SUB_GIFT, "GenerousGifter", amount=5),
    Event(SUBSCRIPTION, "LuckyRecipient", months=1, is_gift=True),
    Event(DONATION, "BigTipper", amount=25.0, currency="SEK", formatted_amount="25,00 kr"),
    Event(DONATION, "SmallTipper", amount=5.0, currency="SEK", formatted_amount="5,00 kr"),
    Event(BITS, "BitsCheerer", amount=500),
    Event(RAID, "FriendlyStreamer", amount=42),
]


def cmd_test(args) -> int:
    """Write sample values for every label so OBS sources can be set up without live events."""
    cfg = _cfg(args)
    state = LabelState(Path(tempfile.gettempdir()) / "streamlabels-test-state.json")
    for ev in SAMPLE_EVENTS:
        ev.source = "test"
        state.apply(ev)
    state.seed_totals(follower_count=1234, subscriber_count=56)
    writer = LabelWriter(cfg.output_dir)
    labels = state.labels(cfg)
    writer.write_all(labels)
    print(f"wrote {len(labels)} sample label files to {cfg.output_dir}")
    print("(the running daemon will overwrite them with real values on its next update)")
    for key in sorted(labels):
        print(f"  {key}.txt = {labels[key]!r}")
    return 0


def cmd_show(args) -> int:
    cfg = _cfg(args)
    state = LabelState(cfg.state_file)
    state.load()
    for key, value in sorted(state.labels(cfg).items()):
        print(f"{key:36s} {value!r}")
    return 0


def cmd_reset_session(args) -> int:
    try:
        pid = int(PID_PATH.read_text().strip())
        os.kill(pid, signal.SIGUSR1)
        print(f"sent session reset to streamlabels (pid {pid})")
        return 0
    except (OSError, ValueError):
        pass
    cfg = _cfg(args)
    state = LabelState(cfg.state_file)
    state.load()
    state.reset_session()
    state.save()
    LabelWriter(cfg.output_dir).write_all(state.labels(cfg))
    print("daemon not running; session reset in the state file and label files rewritten")
    return 0


def cmd_twitch_login(args) -> int:
    cfg = _cfg(args)
    from .twitch import TwitchAuth, TwitchAuthError
    client_id = args.client_id or cfg.twitch.client_id
    if not client_id:
        print("Set twitch.client_id in the config first (https://dev.twitch.tv/console/apps, "
              "category 'Broadcaster Suite', client type 'Public').", file=sys.stderr)
        return 2
    try:
        auth = TwitchAuth(client_id)
        auth.device_login()
        _, user_id, login = auth.ensure()
    except TwitchAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"logged in as {login} (id {user_id}); token saved to {TWITCH_TOKEN_PATH}")
    if not cfg.twitch.enabled:
        print("remember to set  enabled = true  under [twitch] in the config")
    return 0


def cmd_paths(args) -> int:
    cfg = _cfg(args)
    print(f"config:     {Path(args.config).expanduser() if args.config else CONFIG_PATH}")
    print(f"output_dir: {cfg.output_dir}")
    print(f"state_file: {cfg.state_file}")
    print(f"pid_file:   {PID_PATH}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="streamlabels",
                                description="Streamlabs Stream Labels replacement for Linux.")
    p.add_argument("--version", action="version", version=f"streamlabels {__version__}")
    p.add_argument("-c", "--config", help=f"config file (default {CONFIG_PATH})")
    p.add_argument("-o", "--output-dir", help="override output_dir from the config")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("run", help="run the daemon (default)")
    s.add_argument("--dump", action="store_true", help="log every raw event received")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_run)
    sub.add_parser("init", help="write a config template and create folders").set_defaults(fn=cmd_init)
    sub.add_parser("test", help="write sample values to every label file").set_defaults(fn=cmd_test)
    sub.add_parser("show", help="print current label values").set_defaults(fn=cmd_show)
    sub.add_parser("reset-session", help="start a new session (clears session_* labels)").set_defaults(fn=cmd_reset_session)
    s = sub.add_parser("twitch-login", help="log in to Twitch with a device code")
    s.add_argument("--client-id")
    s.set_defaults(fn=cmd_twitch_login)
    sub.add_parser("paths", help="print the files this tool uses").set_defaults(fn=cmd_paths)

    args = p.parse_args(argv)
    if not args.cmd:
        args.cmd, args.fn, args.dump, args.verbose = "run", cmd_run, False, False
    return args.fn(args)
