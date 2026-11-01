"""The operator's notification policy (NP S1): the kind->class map, the policy file, decide/should_push/held with it,
and the `levain ping policy` commands. Built from the hand runs of the S1 brief."""
from __future__ import annotations

import argparse
import itertools
import json

import pytest

from levain.webpush import held, presence
from levain.webpush.presence import ACTIVE_WINDOW_SECS, DEFAULT_CELLS, decide

NOW = 1_000_000.0
ACTIVE, INACTIVE = 10.0, ACTIVE_WINDOW_SECS + 1.0
PHILL = {"quiet": dict(DEFAULT_CELLS["quiet"]), "sleep": {"approval": "push", "attention": "push", "other": "hold"}}


@pytest.fixture(autouse=True)
def _as_operator(monkeypatch):
    from levain.consent import band

    monkeypatch.setattr(band, "agent_process", lambda: None)


def _w(word, **kw):
    rec = {"word": word, "ts": NOW - 100}
    rec.update(kw)
    return rec


def _cli(tmp_path, *argv):
    from levain.webpush import cli

    p = argparse.ArgumentParser()
    cli.register(p.add_subparsers(dest="cmd"))
    a = p.parse_args(["ping", *argv, "--queue-dir", str(tmp_path)])
    return a.func(a)


def _write(tmp_path, text):
    d = tmp_path / "webpush"
    d.mkdir(exist_ok=True)
    (d / "policy.json").write_text(text if isinstance(text, str) else json.dumps(text))


def _file(cells):
    return {"version": 1, "cells": cells}


def test_class_map_and_urgent_kinds_derive_from_it():
    # `dated` is a deliberate NEW kind (flow sends it for dated items), not part of the old rule; under the default
    # it behaves like `attention`.
    for k in ("consent", "push", "reping"):
        assert presence.kind_class(k) == "approval"
    for k in ("attention", "dated"):
        assert presence.kind_class(k) == "attention"
    for k in ("question", "ruling", "summary", "ping", "test", "never-heard-of-it", None, 7):
        assert presence.kind_class(k) == "other"
    assert presence.URGENT_KINDS == {k for k, c in presence.KIND_CLASSES.items() if c != "other"}
    assert "dated" in presence.URGENT_KINDS


@pytest.mark.parametrize("idle", [ACTIVE, INACTIVE, None])
@pytest.mark.parametrize("wordname", ["quiet", "sleep", None])
def test_default_policy_is_todays_rule_for_every_word_and_class(wordname, idle):
    word = None if wordname is None else _w(wordname, until=NOW + 600)
    for cls in presence.CLASSES:
        urgent = cls != "other"
        # the old call (urgent=) and the policy call give one decision
        old = decide(NOW, word, idle, urgent=urgent)
        assert decide(NOW, word, idle, class_name=cls, policy=DEFAULT_CELLS) == old
        assert decide(NOW, word, idle, class_name=cls) == old
    # the table of today's rule while muted
    if wordname == "quiet":
        assert [decide(NOW, word, idle, class_name=c).push for c in presence.CLASSES] == [True, True, False]
    if wordname == "sleep" and idle != ACTIVE:   # input 10 s ago is after the word: sleep has ended
        assert not any(decide(NOW, word, idle, class_name=c).push for c in presence.CLASSES)


def test_phills_table_changes_exactly_those_cells():
    sleep = _w("sleep")
    got = {c: decide(NOW, sleep, INACTIVE, class_name=c, policy=PHILL) for c in presence.CLASSES}
    assert got["approval"].push and got["attention"].push and not got["other"].push
    assert got["approval"].why == "sleep: approvals still reach you (your policy)"
    assert got["other"].state == "muted" and got["other"].by == "sleep"
    # quiet is untouched by a sleep-only change
    quiet = _w("quiet", until=NOW + 600)
    for c in presence.CLASSES:
        assert decide(NOW, quiet, ACTIVE, class_name=c, policy=PHILL) == decide(NOW, quiet, ACTIVE, class_name=c)


def test_every_cell_decides_while_muted_and_loud_and_unmuted_are_unchanged():
    for mute, cls, value in itertools.product(("quiet", "sleep"), presence.CLASSES, ("push", "hold")):
        cells = {m: dict(c) for m, c in DEFAULT_CELLS.items()}
        cells[mute][cls] = value
        word = _w(mute, until=NOW + 600)
        assert decide(NOW, word, INACTIVE, class_name=cls, policy=cells).push is (value == "push")
        # loud first, whatever the policy
        loud = _w("loud", until=NOW + 600)
        assert decide(NOW, loud, INACTIVE, class_name=cls, policy=cells).push is True
        # no mute word: urgent classes push, other follows measurement; the policy is not read
        assert decide(NOW, None, ACTIVE, class_name=cls, policy=cells) == decide(NOW, None, ACTIVE, urgent=cls != "other")
    # a sleep that has ended (he typed) is no mute: the policy does not apply
    assert decide(NOW, _w("sleep"), 0.0, class_name="other", policy={
        "quiet": DEFAULT_CELLS["quiet"], "sleep": {"approval": "hold", "attention": "hold", "other": "hold"}}).state == "holding"


def test_quiet_other_push_is_named_as_policy():
    cells = {"quiet": {"approval": "push", "attention": "push", "other": "push"}, "sleep": DEFAULT_CELLS["sleep"]}
    d = decide(NOW, _w("quiet", until=NOW + 600), ACTIVE, class_name="other", policy=cells)
    assert d.push and "your policy" in d.why


def test_read_policy_default_loaded_and_malformed(tmp_path):
    assert presence.read_policy(tmp_path) == (DEFAULT_CELLS, "default")
    assert presence.read_policy(None)[1] == "default"
    _write(tmp_path, _file(PHILL))
    assert presence.read_policy(tmp_path) == (PHILL, "loaded")
    for bad in ("{nope", "[]", json.dumps({"version": 2, "cells": PHILL}), json.dumps({"version": True, "cells": PHILL}),
                json.dumps({"version": 1}), " " * 5000, json.dumps({"version": 1, "cells": PHILL, "pad": "x" * 5000})):
        _write(tmp_path, bad)
        cells, status = presence.read_policy(tmp_path)
        assert cells == DEFAULT_CELLS and status.startswith("malformed"), bad[:30]


def test_a_bad_cell_makes_the_whole_file_the_default(tmp_path):
    cells = {"quiet": {"approval": "hold", "attention": "banana", "other": "push"},
             "sleep": {"approval": "push", "other": None}}
    _write(tmp_path, _file(cells))
    got, status = presence.read_policy(tmp_path)
    assert status.startswith("malformed") and "quiet.attention" in status and "sleep.other" in status
    assert "whole default" in status and "(default used)" not in status
    assert got == DEFAULT_CELLS   # no valid cell of a malformed file stands
    # one bad cell plus quiet.approval=hold: quiet approval PUSHES (the default), the hold is not honoured
    bad = {"quiet": {"approval": "hold", "attention": "push", "other": "hold"},
           "sleep": {"approval": "hold", "attention": "hold", "other": "banana"}}
    _write(tmp_path, _file(bad))
    cells2, status2 = presence.read_policy(tmp_path)
    assert cells2["quiet"]["approval"] == "push" and status2.startswith("malformed")
    presence.set_word(tmp_path, "quiet", now=NOW - 10)
    assert presence.should_push(tmp_path, "consent", now=NOW, idle=lambda: ACTIVE).push is True


def test_policy_view_shape_malformed(tmp_path):
    _write(tmp_path, _file({"quiet": {"approval": "hold", "attention": "push", "other": "hold"},
                           "sleep": {"approval": "hold", "attention": "hold", "other": "banana"}}))
    v = presence.policy_view(tmp_path)
    assert v["status_kind"] == "malformed" and v["reasons"] == ["missing or invalid cell sleep.other"]
    assert v["status"].startswith("malformed: ") and v["cells"] == DEFAULT_CELLS and v["default_class"] == "other"
    _write(tmp_path, _file(PHILL))
    v = presence.policy_view(tmp_path)
    assert v["status_kind"] == "loaded" and v["reasons"] == [] and v["status"] == "loaded"


def test_set_policy_on_a_malformed_file_logs_the_prior_status(tmp_path):
    _write(tmp_path, "{nope")
    cells = presence.set_policy(tmp_path, "quiet", "other", "push", now=NOW)
    assert cells["quiet"]["other"] == "push" and cells["sleep"] == DEFAULT_CELLS["sleep"]
    rows = [json.loads(line) for line in (tmp_path / "webpush" / "presence.log.jsonl").read_text().splitlines()]
    assert "prior file malformed: not valid JSON" in rows[-1]["why"] and "rewritten from the default" in rows[-1]["why"]
    assert presence.read_policy(tmp_path)[1] == "loaded"


def test_malformed_file_is_the_default_decision(tmp_path):
    presence.set_word(tmp_path, "sleep", now=NOW - 100)
    _write(tmp_path, "{not json")
    for kind in ("consent", "attention", "dated", "question", "mystery"):
        d = presence.should_push(tmp_path, kind, now=NOW, idle=lambda: 500.0)
        assert d.push is False and d.state == "muted"
    (tmp_path / "webpush" / "policy.json").unlink()


def test_should_push_reads_the_policy_and_faults_read_as_push(tmp_path):
    presence.set_word(tmp_path, "sleep", now=NOW - 100)
    idle = lambda: 500.0   # noqa: E731 - no input since the word
    assert presence.should_push(tmp_path, "consent", now=NOW, idle=idle).push is False
    _write(tmp_path, _file(PHILL))
    assert presence.should_push(tmp_path, "consent", now=NOW, idle=idle).push is True
    assert presence.should_push(tmp_path, "dated", now=NOW, idle=idle).push is True
    assert presence.should_push(tmp_path, "attention", now=NOW, idle=idle).push is True
    assert presence.should_push(tmp_path, "question", now=NOW, idle=idle).push is False
    assert presence.should_push(tmp_path, "mystery", now=NOW, idle=idle).push is False

    def boom():
        raise RuntimeError("x")

    assert presence.should_push(tmp_path, "question", now=NOW, idle=boom).push is True


def test_held_ping_is_released_by_the_same_policy():
    sleep = _w("sleep")
    items = [{"kind": k, "reason": "muted", "id": k} for k in ("consent", "attention", "question")]
    act, muted = held.plan(items, sleep, 500.0, NOW)
    assert muted == []   # default: sleep holds all
    act, muted = held.plan(items, sleep, 500.0, NOW, PHILL)
    assert [i["kind"] for i in muted] == ["consent", "attention"]
    # a ping held as "active" is released by decide with the policy too
    act, _ = held.plan([{"kind": "question", "reason": "active", "id": 1}], _w("quiet", until=NOW + 600), ACTIVE, NOW,
                       {"quiet": {"approval": "push", "attention": "push", "other": "push"}, "sleep": DEFAULT_CELLS["sleep"]})
    assert len(act) == 1


def test_held_flush_reads_the_policy_file(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(held.presence, "decide", lambda *a, **kw: seen.update(kw) or presence.Decision(True, "pushing", ""))
    from levain.webpush.store import PushStore

    PushStore(tmp_path)  # creates the store
    _write(tmp_path, _file(PHILL))
    monkeypatch.setattr(PushStore, "held_items", lambda self, now=None: [{"kind": "consent", "reason": "muted", "id": 1}])
    held._plan_now(tmp_path, NOW, lambda: 500.0)
    assert seen["policy"] == PHILL and seen["class_name"] == "approval"


def test_cli_show_default_json(tmp_path, capsys):
    assert _cli(tmp_path, "policy", "show", "--json") == 0
    v = json.loads(capsys.readouterr().out)
    assert set(v) == {"version", "cells", "classes", "default_class", "status", "status_kind", "reasons", "sentences",
                    "default_cells", "held_by_default"}
    assert v["version"] == 1 and v["cells"] == DEFAULT_CELLS and v["status"] == "default"
    assert v["status_kind"] == "default" and v["reasons"] == [] and v["default_class"] == "other"
    assert v["classes"] == presence.KIND_CLASSES and "(any other kind)" not in v["classes"]
    assert v["classes"]["dated"] == "attention" and v["classes"]["consent"] == "approval"
    assert v["sentences"]["sleep"] == "In sleep: nothing reaches you until you are back"
    assert not (tmp_path / "webpush" / "policy.json").exists()
    assert _cli(tmp_path, "policy", "show") == 0
    assert "In sleep: nothing reaches you until you are back" in capsys.readouterr().out


def test_cli_set_show_reset_and_log(tmp_path, capsys):
    assert _cli(tmp_path, "policy", "set", "sleep", "approval", "push") == 0
    assert _cli(tmp_path, "policy", "set", "sleep", "attention", "push") == 0
    assert presence.read_policy(tmp_path) == (PHILL, "loaded")
    assert (tmp_path / "webpush" / "policy.json").stat().st_mode & 0o777 == 0o600
    assert list((tmp_path / "webpush").glob(".policy.json.*")) == []   # no temp left
    capsys.readouterr()
    assert _cli(tmp_path, "policy", "show", "--json") == 0
    v = json.loads(capsys.readouterr().out)
    assert v["status"] == "loaded" and v["cells"] == PHILL
    assert _cli(tmp_path, "policy", "reset") == 0
    assert presence.read_policy(tmp_path) == (DEFAULT_CELLS, "default")
    rows = presence.read_log(tmp_path)
    assert [r["word"] for r in rows] == ["policy set sleep approval push", "policy set sleep attention push", "policy reset"]
    assert all(r["outcome"] == "set" for r in rows)
    with pytest.raises(SystemExit):
        _cli(tmp_path, "policy", "set", "sleep", "approval", "maybe")


def test_agent_setter_is_refused_and_logged(tmp_path, monkeypatch, capsys):
    from levain.consent import band

    monkeypatch.setattr(band, "agent_process", lambda: "claude")
    monkeypatch.setattr(band, "_agent_and_session", lambda agent, s: ("claude", "sess-1"))
    assert _cli(tmp_path, "policy", "set", "sleep", "approval", "push") == 2
    assert "operator's own" in capsys.readouterr().out
    assert _cli(tmp_path, "policy", "reset") == 2
    assert not (tmp_path / "webpush" / "policy.json").exists()
    rows = presence.read_log(tmp_path)
    assert [r["outcome"] for r in rows] == ["refused", "refused"]
    assert rows[0]["by"]["agent"] == "claude" and rows[0]["by"]["role"] == "seat"
    # an existing policy survives an agent's reset
    monkeypatch.setattr(band, "agent_process", lambda: None)
    assert _cli(tmp_path, "policy", "set", "sleep", "approval", "push") == 0
    monkeypatch.setattr(band, "agent_process", lambda: "claude")
    assert _cli(tmp_path, "policy", "reset") == 2
    assert presence.read_policy(tmp_path)[1] == "loaded"
    # reading stays open to an agent
    assert _cli(tmp_path, "policy", "show", "--json") == 0


def test_status_mentions_the_policy_only_when_it_is_not_the_default(tmp_path):
    base = presence.describe(tmp_path, now=NOW, idle=lambda: ACTIVE)
    assert "policy" not in base
    _write(tmp_path, _file(DEFAULT_CELLS))   # a file equal to the default says nothing
    assert presence.describe(tmp_path, now=NOW, idle=lambda: ACTIVE) == base
    _write(tmp_path, _file(PHILL))
    line = presence.describe(tmp_path, now=NOW, idle=lambda: ACTIVE)
    assert "notification policy" in line and "In sleep: approvals and attention items reach you" in line
    _write(tmp_path, "{bad")
    assert "malformed" in presence.describe(tmp_path, now=NOW, idle=lambda: ACTIVE)


def _no_lock(monkeypatch):
    import fcntl

    def boom(fd, op):
        raise OSError("lock unavailable")
    monkeypatch.setattr(fcntl, "flock", boom)


def test_policy_set_and_reset_fail_when_the_lock_cannot_be_taken(tmp_path, monkeypatch, capsys):
    presence.set_policy(tmp_path, "sleep", "approval", "push", now=NOW)
    before = (tmp_path / "webpush" / "policy.json").read_bytes()
    _no_lock(monkeypatch)
    with pytest.raises(presence.PresenceError, match="presence lock not taken"):
        presence.set_policy(tmp_path, "quiet", "other", "push", now=NOW)
    with pytest.raises(presence.PresenceError, match="presence lock not taken"):
        presence.reset_policy(tmp_path, now=NOW)
    assert (tmp_path / "webpush" / "policy.json").read_bytes() == before
    capsys.readouterr()
    assert _cli(tmp_path, "policy", "set", "quiet", "other", "push") != 0
    assert "presence lock not taken" in capsys.readouterr().out
    assert (tmp_path / "webpush" / "policy.json").read_bytes() == before


def test_policy_view_reads_the_file_once(tmp_path, monkeypatch):
    _write(tmp_path, _file(PHILL))
    real, calls = presence._read_policy, []
    monkeypatch.setattr(presence, "_read_policy", lambda q: calls.append(1) or real(q))
    v = presence.policy_view(tmp_path)
    assert len(calls) == 1 and v["status"] == "loaded" and v["cells"] == PHILL


def test_policy_log_rows_record_the_prior_cells(tmp_path):
    presence.set_policy(tmp_path, "quiet", "other", "push", now=NOW)
    presence.reset_policy(tmp_path, now=NOW)
    rows = presence.read_log(tmp_path)
    assert rows[0]["prior"] == {"quiet.other": "hold"}
    assert rows[1]["prior"] == {"quiet.approval": "push", "quiet.attention": "push", "quiet.other": "push",
                                "sleep.approval": "hold", "sleep.attention": "hold", "sleep.other": "hold"}
    assert presence.log_line(rows[0])
