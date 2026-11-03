"""End-to-end: a local Socket.IO server stands in for sockets.streamlabs.com."""

import os
import queue
import signal
import subprocess
import sys
import threading
import time

import pytest
import socketio
from werkzeug.serving import make_server

from streamlabels.events import DONATION, FOLLOW, ServerLabels
from streamlabels.streamlabs import StreamlabsSource


class FakeStreamlabs:
    def __init__(self):
        self.sio = socketio.Server(async_mode="threading", logger=False, engineio_logger=False)
        self.clients: list[str] = []

        @self.sio.event
        def connect(sid, environ, auth=None):
            if "token=good" not in environ.get("QUERY_STRING", ""):
                raise socketio.exceptions.ConnectionRefusedError("Authentication error")
            self.clients.append(sid)

        # Werkzeug's dev server exposes the raw socket, which simple-websocket needs for the upgrade.
        self.httpd = make_server("127.0.0.1", 0, socketio.WSGIApp(self.sio), threaded=True)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def wait_client(self, timeout=10) -> str:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.clients:
                return self.clients[-1]
            time.sleep(0.05)
        raise AssertionError("client never connected")

    def emit(self, payload):
        self.sio.emit("event", payload, to=self.wait_client())

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def server():
    s = FakeStreamlabs()
    yield s
    s.close()


def test_source_receives_events_over_real_socketio(server):
    q: queue.Queue = queue.Queue()
    src = StreamlabsSource("good", q, count_test_events=False, twitch_relay=True, url=server.url)
    src.start()
    try:
        server.emit({"type": "follow", "for": "twitch_account", "message": [{"name": "alice"}]})
        server.emit({"type": "donation", "message": [{"name": "bob", "amount": "2.50", "currency": "EUR",
                                                      "formatted_amount": "€2.50"}]})
        server.emit({"type": "streamlabels", "message": {"data": {"all_time_top_donator": "carol"}}})
        got = [q.get(timeout=10) for _ in range(3)]
    finally:
        src.stop()
    assert got[0].kind == FOLLOW and got[0].name == "alice"
    assert got[1].kind == DONATION and got[1].formatted_amount == "€2.50"
    assert isinstance(got[2], ServerLabels) and got[2].data["all_time_top_donator"] == "carol"


def test_bad_token_is_reported_and_retried(server, caplog):
    q: queue.Queue = queue.Queue()
    src = StreamlabsSource("bad", q, count_test_events=False, twitch_relay=True, url=server.url)
    with caplog.at_level("ERROR"):
        src.start()
        time.sleep(2.5)
        src.stop()
    assert any("rejected the socket token" in r.message for r in caplog.records)
    assert server.clients == []


def test_daemon_end_to_end(server, tmp_path):
    out = tmp_path / "labels"
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'output_dir = "{out}"\nstate_file = "{tmp_path / "state.json"}"\n'
                   f'[streamlabs]\nsocket_token = "good"\n')
    env = dict(os.environ, STREAMLABELS_SOCKET_URL=server.url, XDG_RUNTIME_DIR=str(tmp_path))
    proc = subprocess.Popen([sys.executable, "-m", "streamlabels", "-c", str(cfg), "run", "-v"],
                            env=env, stderr=subprocess.PIPE, text=True)
    try:
        server.emit({"type": "follow", "for": "twitch_account", "message": [{"name": "dave"}]})
        server.emit({"type": "bits", "for": "twitch_account", "message": [{"name": "erin", "amount": "100"}]})
        server.emit({"type": "streamlabels", "message": {"data": {"session_followers": "from server"}}})
        deadline = time.time() + 10
        while time.time() < deadline:
            f = out / "most_recent_follower.txt"
            if f.exists() and f.read_text() == "dave" and (out / "session_followers.txt").read_text() == "from server" \
                    and (out / "most_recent_cheer_amount.txt").read_text() == "100":
                break
            time.sleep(0.1)
        else:
            raise AssertionError("label files were not written in time")
        # SIGUSR1 = new session; all-time labels survive, session ones clear
        proc.send_signal(signal.SIGUSR1)
        deadline = time.time() + 5
        while time.time() < deadline and (out / "session_follower_count.txt").read_text() != "0":
            time.sleep(0.1)
        assert (out / "session_follower_count.txt").read_text() == "0"
        assert (out / "most_recent_follower.txt").read_text() == "dave"
        assert (out / "session_followers.txt").read_text() == "from server"  # server labels persist
        assert (tmp_path / "streamlabels.pid").read_text() == str(proc.pid)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            _, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, err = proc.communicate()
    assert proc.returncode == 0, err
    assert not (tmp_path / "streamlabels.pid").exists()
    assert "Streamlabs socket connected" in err
    assert (tmp_path / "state.json").exists()
