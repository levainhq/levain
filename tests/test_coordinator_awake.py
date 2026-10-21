"""C7: the host is held awake exactly while a work thread works, through ONE locked reconcile after every store write.

Each test replays a failure reproduced in project_memory 1021-18/coord/fix_r1_awake_BEFORE.txt: (a) a work thread kept
working only by the watcher's service beats lost its holder when the holder's lifetime ran out; (c) a process whose
pid matched the record and whose argv said "caffeinate" was sent SIGTERM though it was not the holder."""
import json
import os
import shutil
import subprocess
import sys
import time

import pytest

from levain.coordinator import awake
from levain.coordinator.store import CoordStore, Part

pytestmark = pytest.mark.skipif(os.name != "posix" or shutil.which("sleep") is None or shutil.which("ps") is None,
                                reason="needs POSIX ps + sleep")


@pytest.fixture
def held(tmp_path, monkeypatch):
    """A store whose holder is a harmless sleep of ``seconds`` (no real assertion is taken)."""
    def make(seconds):
        monkeypatch.setattr(awake, "_IN_TESTS", True)
        monkeypatch.setattr(awake, "HOLD_SECONDS", seconds)
        monkeypatch.setattr(awake, "holder_command", lambda *a, **k: [shutil.which("sleep"), str(seconds)])
        q = tmp_path / "consent"
        q.mkdir(mode=0o700)
        return q, CoordStore(q)
    yield make
    for proc in awake._children:
        proc.kill()
        proc.wait()
    awake._children.clear()


def test_service_beats_alone_keep_the_holder_renewed(held, tmp_path):
    q, s = held(2)
    s.coord_set_agent("claude", cwd=str(tmp_path))
    d, _ = s.accept_drop("w", surface="cli")
    ln = s.coord_status()["launches"][0]
    s.launch_activate(ln["launch_id"], harness_session_id="aw")
    tok = s.coord_claim(ln["launch_id"], s.launch(ln["launch_id"]).session_key)
    t, _ = s.split(d.drop_id, [Part(kind="work", title="w"), Part(kind="note", title="n")], by="coordinator", token=tok)
    s.set_state(t.thread_id, "working", by="coordinator", token=tok)
    first = json.loads((q / "awake.json").read_text())
    end = time.time() + 3.5                      # past the first holder's own lifetime
    while time.time() < end:
        assert s.coord_service_beat(tok) is not None
        time.sleep(0.4)
    rec = json.loads((q / "awake.json").read_text())
    assert rec["pid"] != first["pid"] and awake._ours(rec)
    s.detach(t.thread_id, d.drop_id, by="operator")      # the last member out: the thread closes, the host is let go
    assert not (q / "awake.json").exists() and not awake._ours(rec)


def test_a_reused_pid_is_never_signalled(held):
    q, s = held(30)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "caffeinate"])
    try:
        time.sleep(0.2)
        (q / "awake.json").write_text(json.dumps({
            "pid": other.pid, "until": time.time() - 5, "threads": ["th-x"],
            "argv": [sys.executable, "-c", "import time; time.sleep(60)", "caffeinate"],
            "started": "Mon Jan  1 00:00:00 2001"}))
        assert awake.sync(s) == {"held": False, "why": "no work thread is working"}
        time.sleep(0.2)
        assert other.poll() is None and not (q / "awake.json").exists()
    finally:
        other.kill()
        other.wait()
