import json

from streamlabels.rawlog import RawLog


def test_rawlog_appends_and_rotates(tmp_path):
    rl = RawLog(tmp_path / "events.jsonl", max_bytes=200)
    for i in range(10):
        rl.write("streamlabs", {"type": "follow", "i": i, "pad": "x" * 40})
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert json.loads(lines[-1])["payload"]["i"] == 9
    assert (tmp_path / "events.jsonl.1").exists()
