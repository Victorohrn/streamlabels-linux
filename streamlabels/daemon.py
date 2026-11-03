"""The long-running process: sources -> queue -> state -> text files."""

from __future__ import annotations

import logging
import os
import queue
import signal
import threading
import time

from .config import PID_PATH, Config
from .events import Event, ServerLabels
from .state import LabelState
from .writer import LabelWriter

log = logging.getLogger(__name__)


class Daemon:
    def __init__(self, cfg: Config, *, dump: bool = False):
        self.cfg = cfg
        self.dump = dump
        self.q: queue.Queue = queue.Queue()
        self.state = LabelState(cfg.state_file)
        self.writer = LabelWriter(cfg.output_dir)
        self.sources: list[threading.Thread] = []
        self._stopping = False

    # -- lifecycle
    def _write_pid(self) -> None:
        try:
            PID_PATH.parent.mkdir(parents=True, exist_ok=True)
            PID_PATH.write_text(str(os.getpid()))
        except OSError as exc:
            log.debug("could not write pid file: %s", exc)

    def _remove_pid(self) -> None:
        try:
            if PID_PATH.exists() and PID_PATH.read_text().strip() == str(os.getpid()):
                PID_PATH.unlink()
        except OSError:
            pass

    def _install_signals(self) -> None:
        signal.signal(signal.SIGUSR1, lambda *_: self.q.put(("reset_session",)))
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.q.put(("stop",)))

    def _start_sources(self) -> None:
        cfg = self.cfg
        if cfg.streamlabs.enabled:
            if cfg.streamlabs.socket_token:
                from .streamlabs import SOCKET_URL, StreamlabsSource
                src = StreamlabsSource(cfg.streamlabs.socket_token, self.q,
                                       count_test_events=cfg.streamlabs.count_test_events,
                                       twitch_relay=cfg.twitch_relay_enabled, dump=self.dump,
                                       url=os.environ.get("STREAMLABELS_SOCKET_URL", SOCKET_URL))
                src.start()
                self.sources.append(src)
            else:
                log.error("[streamlabs] socket_token is empty; Streamlabs source disabled")
        if cfg.twitch.enabled:
            from .twitch import TwitchAuth, TwitchAuthError, TwitchSource
            try:
                auth = TwitchAuth(cfg.twitch.client_id)
                auth.ensure()
            except TwitchAuthError as exc:
                log.error("[twitch] %s; Twitch source disabled", exc)
            else:
                src = TwitchSource(auth, self.q, poll_interval=cfg.twitch.poll_interval, dump=self.dump)
                src.start()
                self.sources.append(src)
        if not self.sources:
            log.error("no event sources are configured; labels will not update "
                      "(edit the config and restart)")

    # -- main loop
    def flush(self) -> None:
        labels = self.state.labels(self.cfg)
        n = self.writer.write_all(labels)
        if n:
            log.debug("updated %d label file(s)", n)
        if self.state.dirty:
            self.state.save()

    def handle(self, item) -> None:
        if isinstance(item, Event):
            log.info("%s: %s%s", item.kind, item.name,
                     f" ({item.amount:g} {item.currency})".rstrip() if item.amount else "")
            self.state.apply(item)
        elif isinstance(item, ServerLabels):
            log.info("received %d ready-made labels from Streamlabs", len(item.data))
            self.state.set_server_labels(item.data)
        elif isinstance(item, tuple):
            cmd = item[0]
            if cmd == "seed":
                self.state.seed_totals(**item[1])
            elif cmd == "reset_session":
                self.state.reset_session()
            elif cmd == "stop":
                self._stopping = True
        else:
            log.debug("unknown queue item %r", item)

    def run(self) -> None:
        cfg = self.cfg
        self.state.load()
        if not self.state.data.get("legacy_imported"):
            found = self.state.import_legacy(cfg.output_dir)
            if found["labels"]:
                log.info("imported existing label folder: %d files, %d tips, %d cheers, %d gifted subs "
                         "carried into the all-time stats", found["labels"], found["donations"],
                         found["bits"], found["gifts"])
        if cfg.session_reset_on_start:
            self.state.reset_session()
        self._install_signals()
        self._write_pid()
        log.info("writing labels to %s", cfg.output_dir)
        self.flush()
        self._start_sources()
        try:
            while not self._stopping:
                try:
                    # 1 s ticks keep the train clocks moving; otherwise a slow refresh keeps
                    # the time-windowed labels (weekly_/monthly_/30day_) honest
                    item = self.q.get(timeout=1 if self.state.train_active(cfg) else 30)
                except queue.Empty:
                    self.flush()
                    continue
                self.handle(item)
                # drain anything that arrived in the same burst before writing files
                deadline = time.time() + 0.2
                while not self._stopping and time.time() < deadline:
                    try:
                        self.handle(self.q.get_nowait())
                    except queue.Empty:
                        break
                self.flush()
        finally:
            log.info("shutting down")
            for src in self.sources:
                stop = getattr(src, "stop", None)
                if stop:
                    stop()
            try:
                self.flush()
            except Exception:  # noqa: BLE001
                log.exception("final flush failed")
            self._remove_pid()
