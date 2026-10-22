"""K11 lane C2: the header's real figures. Tokens today from the transcripts' own message stamps (an unreadable
transcript is counted and named, never a zero), and a node with no hook log drawing its rate from its transcript."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from levain.cockpit import topology as topo
from levain.cockpit.engine import ReadContext
from tests.test_cockpit_activity import _session
from tests.test_cockpit_topology import _log, _stem


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _msg(mid: str, at: datetime, role: str = "assistant", **usage: int) -> str:
    d: dict = {"type": role, "timestamp": _iso(at), "uuid": f"u-{mid}-{_iso(at)}", "message": {"id": mid, "role": role}}
    if role == "assistant":
        d["message"]["usage"] = {"input_tokens": usage.get("i", 0), "cache_creation_input_tokens": usage.get("c", 0),
                                 "cache_read_input_tokens": 9_999, "output_tokens": usage.get("o", 0)}
    return json.dumps(d)


def _with_transcript(root: Path, agent: str, sid: str, path: Path) -> None:
    import hashlib

    f = root / ".levain" / "sessions" / (hashlib.sha1(f"{agent}:{sid}".encode()).hexdigest()[:24] + ".json")
    rec = json.loads(f.read_text())
    rec["transcript"] = str(path)
    f.write_text(json.dumps(rec))


def _read(root: Path, now: datetime) -> dict:
    return topo.topology_reader(install_root=root, consent=lambda: None, questions=lambda: None,
                                hub_label=lambda c: "Sage")(ReadContext(now)).value


def test_tokens_today_sums_the_readable_transcript_and_counts_the_unreadable_one(tmp_path):
    now = datetime.now(timezone.utc)
    root = tmp_path / "inst"
    ok = tmp_path / "ok.jsonl"
    ok.write_text("\n".join([
        _msg("m-old", now - timedelta(hours=30), i=1000, c=1000, o=1000),         # yesterday: not today
        _msg("m-1", now - timedelta(seconds=3), i=10, c=5, o=20),
        _msg("m-1", now - timedelta(seconds=2), i=10, c=5, o=20),                 # the same message written twice: once
        _msg("m-2", now - timedelta(seconds=1), i=10, c=5, o=20)]) + "\n")
    _session(root, "readable", str(tmp_path), timedelta(minutes=1))
    _session(root, "gone", str(tmp_path), timedelta(minutes=1))
    _with_transcript(root, "claude_code", "readable", ok)
    _with_transcript(root, "claude_code", "gone", tmp_path / "missing.jsonl")
    counts = _read(root, now)["counts"]
    assert counts["tokens_today"] == 70 and counts["tokens_read"] == 1 and counts["tokens_unread"] == 1, counts   # fresh = in + out + cache write; reads not counted


def test_a_node_with_no_hook_log_draws_its_rate_from_its_transcript_and_a_log_wins(tmp_path):
    now = datetime.now(timezone.utc)
    root = tmp_path / "inst"
    tx = tmp_path / "a.jsonl"
    tx.write_text("\n".join([
        _msg("old", now - timedelta(minutes=9), role="user"),                     # outside the 5-minute window
        _msg("a", now - timedelta(minutes=2), role="user"), _msg("b", now - timedelta(minutes=1), i=1, o=1),
        _msg("c", now - timedelta(seconds=30), role="user")]) + "\n")
    _session(root, "nolog", str(tmp_path), timedelta(minutes=1))
    _session(root, "haslog", str(tmp_path), timedelta(minutes=1))
    _with_transcript(root, "claude_code", "nolog", tx)
    _with_transcript(root, "claude_code", "haslog", tx)                           # its transcript is busy too; the hook log still wins
    _log(root, "claude_code", "haslog", [("prompt", timedelta(minutes=1)), ("tool", timedelta(seconds=40))])
    by = {n["id"]: n for n in _read(root, now)["nodes"]}
    quiet, logged = by[_stem("claude_code", "nolog")], by[_stem("claude_code", "haslog")]
    assert quiet["rate"] == 0.6 and quiet["rate_source"] == "transcript", quiet      # 3 messages in 5 minutes
    assert logged["rate"] == 0.4 and logged["rate_source"] == "log", logged          # 2 logged events in 5 minutes
