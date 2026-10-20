"""K10 S5: the F12 provider fills what the mock draws from data Levain really records.

Item 1: a default install's consent reader names no directory (``directory`` is None = the default queue); the dispatch
store sits in that default queue, so the topology reads it there. Item 2: file conflicts from the Edit/Write paths the
PostToolUse hook logs. Item 3: a dispatch task's label is its contract's own goal.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from tests.test_cockpit_topology import _rig, _stamp, _stem


def _file_dispatch(cdir, tmp_path, goal=None):
    from levain.cockpit.consent import utc_stamp
    from levain.dispatch.jobs import JobStore
    from tests.test_dispatch_contract import POLICY, make_repo, registry, submission

    path, base = make_repo(tmp_path / "dispatch-repo")
    exp = utc_stamp(datetime.now(timezone.utc) + timedelta(hours=2))
    sub = submission(path, base, expires_at=exp)
    if goal is not None:
        sub["goal"] = goal
    job, _cap = JobStore(cdir).file(sub, resolve_template=registry(), policy=POLICY, filer_session="s-run",
                                    idem_key=None)
    return job


class _DefaultReader:
    """web_server._ConsentReader as a default install builds it: ``directory`` present and None."""

    directory = None

    def __call__(self, now):
        from levain.consent.listing import read_listing

        return read_listing(self.directory, now=now)


def test_a_default_install_reads_the_dispatch_store_in_the_default_queue(tmp_path, monkeypatch):
    ck, _root, _src, cdir, _work = _rig(tmp_path, monkeypatch)       # LEVAIN_HOME -> tmp, queue = its consent/
    job = _file_dispatch(cdir, tmp_path)
    ck.attach_consent_rows(_DefaultReader())
    v = ck.panel("topology", credential_class="none")["value"]
    assert "dispatch" not in v["unreadable"], v["errors"]
    assert f"dispatch:{job.id}" in {t["id"] for t in v["tasks"]}


def test_a_reader_with_no_directory_attribute_names_no_dispatch_directory(tmp_path, monkeypatch):
    from levain.cockpit.providers import _dispatch_dir

    _rig(tmp_path, monkeypatch)
    assert _dispatch_dir(lambda now: None) is None
    assert _dispatch_dir(_DefaultReader()).name == "consent"
    explicit = tmp_path / "elsewhere"

    class _Explicit(_DefaultReader):
        directory = explicit

    assert _dispatch_dir(_Explicit()) == explicit


def _tools(root, agent, sid, events):
    """A session log of tool events: (age, text, cut)."""
    import hashlib

    stem = hashlib.sha1(f"{agent}:{sid}".encode()).hexdigest()[:24]
    lines = [json.dumps({"v": 1, "at": _stamp(ago), "kind": "tool", "text": text, "cut": cut})
             for ago, text, cut in events]
    (root / ".levain" / "sessions" / f"{stem}.log.jsonl").write_text("\n".join(lines) + "\n")


def test_two_live_nodes_editing_one_path_are_a_conflict_with_a_basename_only(tmp_path, monkeypatch):
    ck, root, _src, _cdir, work = _rig(tmp_path, monkeypatch)
    shared = str(work / "shared" / "money.py")
    m = timedelta(minutes=1)
    _tools(root, "claude_code", "s-run", [(2 * m, f"Edit: {shared}", 0), (m, "Read: /x/only_read.py", 0),
                                          (m, "Write: /x/solo.py", 0)])
    _tools(root, "claude_code", "s-stall", [(3 * m, f"Write: {shared}", 0), (m, "Read: /x/only_read.py", 0)])
    # one session's relative path resolves against its own cwd: another directory, so no clash with the others
    _tools(root, "claude_code", "s-done", [(m, "Edit: money.py", 0)])
    v = ck.panel("topology", credential_class="none")["value"]
    pair = sorted([_stem("claude_code", "s-run"), _stem("claude_code", "s-stall")])
    assert v["conflicts"] == [{"path": "money.py", "nodes": pair}]
    assert str(work) not in json.dumps(v["conflicts"])


def test_a_cut_event_an_old_event_and_an_idle_node_make_no_conflict(tmp_path, monkeypatch):
    ck, root, _src, _cdir, work = _rig(tmp_path, monkeypatch)
    shared = str(work / "shared" / "money.py")
    m = timedelta(minutes=1)
    _tools(root, "claude_code", "s-run", [(m, f"Edit: {shared}", 0)])
    _tools(root, "claude_code", "s-stall", [(m, f"Edit: {shared}", 12)])            # cut: not a whole path
    _tools(root, "claude_code", "s-done", [(20 * m, f"Edit: {shared}", 0)])         # outside the window
    _tools(root, "claude_code", "s-old", [(m, f"Edit: {shared}", 0)])               # not a live node (2 h old)
    v = ck.panel("topology", credential_class="none")["value"]
    assert v["conflicts"] == []


def test_conflicts_are_null_when_no_live_node_has_a_log(tmp_path, monkeypatch):
    ck, root, _src, _cdir, _work = _rig(tmp_path, monkeypatch)
    for f in (root / ".levain" / "sessions").glob("*.log.jsonl"):
        f.unlink()
    v = ck.panel("topology", credential_class="none")["value"]
    assert v["conflicts"] is None            # nothing observed: not "no conflicts"
