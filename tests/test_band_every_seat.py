"""Q17-ALL S4: the band on every seat. D4's derivation (launch row, then LEVAIN_SEAT, then non-autonomous), Codex hook
parity, the Stop verifier (bounded: no loop, fail open), the gate's fallback notice, the doctor row, the brief line."""
from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import sqlite3
from types import SimpleNamespace

import pytest

import levain
from levain import band_view
from levain.consent.questions import QuestionStore

OPTS = [("fix", "fixed 30 s"), ("exp", "exponential")]
TEMPLATES = pathlib.Path(levain.__file__).parent / "templates"


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def store(qdir):
    return QuestionStore(qdir)


def ask(store, what="Which retry policy?"):
    return store.file(agent="claude_code", agent_session_id="sess-1", what=what, options=OPTS, rec="exp",
                      blocks="the feed PR", cwd="/tmp/proj")


def env(qdir, **more):
    return {"LEVAIN_HOME": str(qdir.parent), **more}


def launch_rows(monkeypatch, rows):
    from levain.coordinator import lease
    from levain.coordinator.store import CoordStore

    monkeypatch.setattr(CoordStore, "open_readonly", lambda self, **_k: sqlite3.connect(":memory:"))
    monkeypatch.setattr(lease, "by_harness_session",
                        lambda conn, sid: SimpleNamespace(role=rows[sid]) if sid in rows else None)


def deliver(store, sid="s1", agent="claude_code", e=None):
    """What the UserPromptSubmit hook does for one prompt: render, commit the view."""
    text, commit = band_view.hook_prompt({"session_id": sid, "prompt": "hi"}, e, agent=agent)
    commit()
    return text


# --- D4: the derivation --------------------------------------------------------------------------------------
def test_launch_row_decides_before_the_env_marker(qdir, store, monkeypatch):
    launch_rows(monkeypatch, {"c1": "coordinator", "w1": "worker"})
    ask(store)
    # a coordinator is attended even when its env says autonomous (e.g. inherited from an entity process)
    assert band_view.is_autonomous("c1", qdir, {"LEVAIN_SEAT": "autonomous"}) is False
    assert band_view.is_autonomous("w1", qdir, {"LEVAIN_SEAT": "attended"}) is True
    assert deliver(store, "c1", e=env(qdir, LEVAIN_SEAT="autonomous")) != ""
    assert deliver(store, "w1", e=env(qdir, LEVAIN_SEAT="attended")) == ""


@pytest.mark.parametrize("value,auto", [("autonomous", True), ("attended", False), ("AUTONOMOUS", False),
                                        ("", False), ("yes", False), (None, False)])
def test_env_marker_when_there_is_no_launch_row(qdir, store, monkeypatch, value, auto):
    launch_rows(monkeypatch, {})
    ask(store)
    e = env(qdir) if value is None else env(qdir, LEVAIN_SEAT=value)
    assert band_view.is_autonomous("hand-opened", qdir, e) is auto
    assert (deliver(store, "hand-opened", e=e) == "") is auto
    assert (band_view.hook_contract({"session_id": "hand-opened"}, e) == "") is auto


def test_neither_fact_is_non_autonomous_and_a_broken_coordinator_store_too(qdir, store, monkeypatch):
    from levain.coordinator.store import CoordStore

    ask(store)
    monkeypatch.setattr(CoordStore, "open_readonly", lambda self, **_k: (_ for _ in ()).throw(OSError("unreadable")))
    assert band_view.is_autonomous("x", qdir, {}) is False
    assert band_view.is_autonomous("x#agent:sub", qdir, {}) is True


def test_posture_and_grants_in_env_never_decide(qdir, monkeypatch):
    launch_rows(monkeypatch, {})
    for k in ("LEVAIN_POSTURE", "LEVAIN_AWAY", "LEVAIN_GRANT"):
        assert band_view.is_autonomous("x", qdir, {k: "autonomous"}) is False


def test_levain_started_processes_carry_the_marker(monkeypatch):
    from levain import launch
    from levain.consent import delivery
    from levain.firing import binding

    monkeypatch.setenv("LEVAIN_SEAT", "autonomous")                    # whatever the parent carried
    assert launch.child_env(seat="attended")["LEVAIN_SEAT"] == "attended"
    assert launch.child_env()["LEVAIN_SEAT"] == "autonomous"           # no seat given: the operator's own stays
    with pytest.raises(ValueError):
        launch.child_env(seat="sometimes")
    monkeypatch.delenv("LEVAIN_SEAT")
    assert "LEVAIN_SEAT" not in launch.child_env()
    assert delivery._env("claude_code", {})["LEVAIN_SEAT"] == "attended"      # a resume of the operator's session
    monkeypatch.setenv("LEVAIN_ENTITY_PROCESS", "")                    # registered for restore
    monkeypatch.setattr(binding, "_LATCHED", False)
    binding.mark_entity_process()                                      # a dispatch executor's process
    import os
    assert os.environ["LEVAIN_SEAT"] == "autonomous"
    assert launch.child_env()["LEVAIN_SEAT"] == "autonomous"           # and its children inherit it


# --- the Stop verifier ---------------------------------------------------------------------------------------
def stop(sid, message, e, **more):
    return band_view.hook_stop({"session_id": sid, "last_assistant_message": message, **more}, e)


def test_verifier_blocks_a_dropped_item_once_and_never_loops(qdir, store, monkeypatch):
    launch_rows(monkeypatch, {})
    e = env(qdir)
    ask(store, "one?"), ask(store, "two?")
    band = deliver(store, e=e)
    assert "band@2" in band
    full = f"done.\n\n{band}"
    assert stop("s1", full, e) is None                                 # the reply kept the whole band
    dropped = "done.\n\n" + "\n".join(line for line in band.splitlines() if not line.startswith("2."))
    reason = stop("s1", dropped, e)
    assert reason and "2" in reason and "reprint" in reason.lower()
    assert stop("s1", dropped, e) is None                              # the retry also drops it: allowed, no loop
    assert stop("s1", dropped, e) is None
    deliver(store, e=e)                                                # the next human turn: a new view
    assert stop("s1", dropped, e) is not None                          # bounded per turn, not forever


def test_verifier_honours_stop_hook_active(qdir, store, monkeypatch):
    launch_rows(monkeypatch, {})
    e = env(qdir)
    ask(store)
    deliver(store, e=e)
    assert stop("s1", "nothing of the band", e, stop_hook_active=True) is None
    assert stop("s1", "nothing of the band", e) is not None            # the claim was not spent by the active call


def test_verifier_needs_the_band_header_too(qdir, store, monkeypatch):
    launch_rows(monkeypatch, {})
    e = env(qdir)
    ask(store)
    band = deliver(store, e=e)
    no_head = "\n".join(line for line in band.splitlines()[1:])
    assert "header" in (stop("s1", no_head, e) or "")


def test_verifier_a_numbered_list_in_prose_is_not_the_band_but_a_compact_item_is(qdir, store, monkeypatch):
    launch_rows(monkeypatch, {})
    e = env(qdir)
    for i in range(14):
        ask(store, f"question {i}?")
    band = deliver(store, e=e)
    assert stop("s1", band, e) is None                                 # compact lines past 12 still count
    assert stop("s1", "band@14 I did 1. this and 2. that", e) is not None


@pytest.mark.parametrize("payload", [None, [], "s", {}, {"session_id": ""}, {"session_id": "s1"},
                                     {"session_id": "s1", "last_assistant_message": None},
                                     {"session_id": "s1", "last_assistant_message": 5}])
def test_verifier_fails_open_on_anything_odd(qdir, store, monkeypatch, payload):
    launch_rows(monkeypatch, {})
    ask(store)
    deliver(store, e=env(qdir))
    assert band_view.hook_stop(payload, env(qdir)) is None


def test_verifier_fails_open_without_a_view_an_open_item_or_a_store(qdir, store, monkeypatch, tmp_path):
    launch_rows(monkeypatch, {})
    e = env(qdir)
    assert stop("s1", "hi", e) is None                                 # no store content, no view
    ask(store)
    assert stop("s1", "hi", e) is None                                 # an item, but this seat was never shown it
    assert stop("s1", "hi", {"LEVAIN_HOME": str(tmp_path / "nope")}) is None


def test_verifier_an_autonomous_seat_is_never_blocked(qdir, store, monkeypatch):
    launch_rows(monkeypatch, {"w1": "worker"})
    ask(store)
    deliver(store, "w1", e=env(qdir))
    assert stop("w1", "no band", env(qdir)) is None


def test_verifier_unwritable_claim_fails_open(qdir, store, monkeypatch):
    launch_rows(monkeypatch, {})
    e = env(qdir)
    ask(store)
    deliver(store, e=e)
    # the prompt hook's turn record made the directory (C11 r1): a directory the claim cannot be created in
    (qdir / "band_stop").chmod(0o500)
    try:
        assert stop("s1", "no band", e) is None                        # cannot record the claim: no block
    finally:
        (qdir / "band_stop").chmod(0o700)


def test_verifier_is_per_viewer(qdir, store, monkeypatch):
    launch_rows(monkeypatch, {})
    e = env(qdir)
    ask(store)
    deliver(store, "a", e=e)
    deliver(store, "b", e=e)
    assert stop("a", "none", e) is not None
    assert stop("b", "none", e) is not None
    assert stop("a", "none", e) is None


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_claude_stop_script_prints_the_block_json(qdir, store, monkeypatch, capsys):
    launch_rows(monkeypatch, {})
    monkeypatch.setenv("LEVAIN_HOME", str(qdir.parent))
    monkeypatch.delenv("LEVAIN_SEAT", raising=False)
    ask(store)
    deliver(store, e=env(qdir))
    mod = _load(TEMPLATES / "activation" / "hooks" / "stop.py", "stop_cc")
    h = mod.hook
    payload = {"session_id": "s1", "last_assistant_message": "I dropped it"}
    monkeypatch.setattr(h, "read_stdin", lambda: payload)
    monkeypatch.setattr(h, "should_fire", lambda: True)
    monkeypatch.setattr(h, "consent_observe", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(h, "consent_ledger", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(h, "session_log", lambda *a, **k: None)
    assert mod.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block" and "NEEDS YOU" in out["reason"]
    assert mod.main() == 0 and capsys.readouterr().out == ""           # same view again: nothing (bounded)
    # a crashing verifier is silence
    monkeypatch.setattr(band_view, "hook_stop", lambda *_a, **_k: 1 / 0)
    assert mod.main() == 0 and capsys.readouterr().out == ""
    # a hook that cannot even read stdin exits 0 and prints nothing
    monkeypatch.setattr(h, "read_stdin", lambda: 1 / 0)
    assert mod.main() == 0 and capsys.readouterr().out == ""


# --- Codex parity ------------------------------------------------------------------------------------------
def test_codex_hooks_call_the_band_as_codex(qdir, store, monkeypatch):
    for name in ("user_prompt_submit.py", "session_start.py"):
        text = (TEMPLATES / "adapters" / "codex" / "activation" / "hooks" / name).read_text()
        assert "levain.band_view" in text and 'agent="codex"' in text
    launch_rows(monkeypatch, {})
    ask(store)
    e = env(qdir)
    assert deliver(store, "cx1", agent="codex", e=e) != ""
    assert band_view.last_view(store, "codex:cx1") is not None
    assert band_view.last_view(store, "claude_code:cx1") is None
    assert band_view.hook_contract({"session_id": "cx1"}, e, agent="codex") == band_view.CONTRACT
    assert band_view.hook_stop({"session_id": "cx1", "last_assistant_message": "x"}, e, agent="codex") is not None


def test_codex_prompt_hook_delivers_then_commits(qdir, store, monkeypatch):
    launch_rows(monkeypatch, {})
    monkeypatch.delenv("LEVAIN_SEAT", raising=False)
    monkeypatch.setenv("LEVAIN_HOME", str(qdir.parent))
    ask(store)
    mod = _load(TEMPLATES / "adapters" / "codex" / "activation" / "hooks" / "user_prompt_submit.py", "ups_cx")
    h = mod.hook
    monkeypatch.setattr(h, "read_stdin", lambda: {"session_id": "cx9", "prompt": "hi", "cwd": "/tmp"})
    for n in ("should_fire",):
        monkeypatch.setattr(h, n, lambda: True)
    monkeypatch.setattr(h, "read_blocks", lambda *_a, **_k: [])
    monkeypatch.setattr(h, "consent_observe", lambda *_a, **_k: None, raising=False)
    monkeypatch.setattr(h, "state_line_pending", lambda _s: (None, None), raising=False)
    monkeypatch.setattr(h, "open_spores", lambda **_k: [])
    monkeypatch.setattr(h, "crystal_recall", lambda *_a, **_k: [])
    monkeypatch.setattr(h, "wrap_state", lambda **_k: None, raising=False)
    monkeypatch.setattr(h, "session_touch", lambda *_a, **_k: None)
    monkeypatch.setattr(h, "session_log", lambda *_a, **_k: None)
    seen = []
    monkeypatch.setattr(h, "emit", lambda text, ev: seen.append(text) or True)
    assert mod.main() == 0
    assert seen and "NEEDS YOU" in seen[0]
    assert band_view.last_view(store, "codex:cx9") is not None


# --- the gate's fallback notice ------------------------------------------------------------------------------
def _hooks_dir(root, wired):
    d = root / "activation" / "hooks"
    d.mkdir(parents=True)
    for n in ("session_start.py", "user_prompt_submit.py", "stop.py"):
        (d / n).write_text("from levain.band_view import x\n" if wired else "pass\n")
    return d


def test_gate_notice_when_the_band_is_not_installed(qdir, store, monkeypatch, tmp_path):
    launch_rows(monkeypatch, {})
    proj = tmp_path / "proj"
    _hooks_dir(proj, wired=False)
    e = env(qdir, CLAUDE_PROJECT_DIR=str(proj))
    assert band_view.gate_notice(qdir, "claude_code", "s1", str(proj), e) is None      # nothing open
    ask(store), ask(store, "b")
    note = band_view.gate_notice(qdir, "claude_code", "s1", str(proj), e)
    assert note and "2 items" in note and "not installed" in note and "levain update" in note
    assert band_view.gate_notice(qdir, "claude_code", "s1", str(proj), e) is None      # once per session
    assert band_view.gate_notice(qdir, "claude_code", "s2", str(proj), e) is not None  # another session


def test_gate_notice_silent_when_wired_autonomous_or_broken(qdir, store, monkeypatch, tmp_path):
    launch_rows(monkeypatch, {"w1": "worker"})
    wired, bare = tmp_path / "wired", tmp_path / "bare"
    _hooks_dir(wired, wired=True)
    bare.mkdir()
    ask(store)
    assert band_view.gate_notice(qdir, "claude_code", "s1", str(wired), env(qdir)) is None
    assert band_view.gate_notice(qdir, "claude_code", "w1", str(bare), env(qdir)) is None
    assert band_view.gate_notice(qdir, "claude_code", None, str(bare), env(qdir)) is None
    assert band_view.gate_notice(tmp_path / "missing", "claude_code", "s9", str(bare), env(qdir)) is None
    assert "absent" in band_view.gate_notice(qdir, "claude_code", "s5", str(bare), env(qdir))


def test_gate_adds_the_notice_to_additional_context(qdir, store, monkeypatch, tmp_path):
    from levain.consent.adapters import codex as cx

    launch_rows(monkeypatch, {})
    proj = tmp_path / "proj"
    _hooks_dir(proj, wired=False)
    ask(store)
    out = cx._with_rulings(None, SimpleNamespace(directory=qdir), "cxs", str(proj), env(qdir))
    assert out and "NEEDS YOU band is not installed" in out["hookSpecificOutput"]["additionalContext"]


def test_codex_hooks_json_locates_the_install(tmp_path):
    proj = tmp_path / "p"
    d = _hooks_dir(proj, wired=True)
    home = tmp_path / "codexhome"
    home.mkdir()
    cmd = f'"/usr/bin/python3" -EP -m levain.hookrun "{d}/user_prompt_submit.py"'
    (home / "hooks.json").write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{"command": cmd}]}]}}))
    assert band_view._find_hooks_dir("codex", "/nowhere", {"CODEX_HOME": str(home)}) == d
    assert band_view._find_hooks_dir("codex", "/nowhere", {"CODEX_HOME": str(tmp_path / "none")}) is None


# --- the doctor row and the brief ---------------------------------------------------------------------------
def test_doctor_band_hooks_row(tmp_path):
    from levain import doctor

    _hooks_dir(tmp_path / "a", wired=True)
    r = doctor._check_band_hooks(tmp_path / "a", "claude_code")
    assert r.ok and not r.warn and "wired" in r.detail
    _hooks_dir(tmp_path / "b", wired=False)
    r = doctor._check_band_hooks(tmp_path / "b", "claude_code")
    assert r.ok and r.warn and r.detail.startswith("absent") and r.hint == "levain update"
    d = _hooks_dir(tmp_path / "c", wired=True)
    (d / "stop.py").write_text("pass\n")
    assert doctor._check_band_hooks(tmp_path / "c", "claude_code").detail.startswith("stale")
    assert doctor._check_band_hooks(tmp_path / "c", "codex").detail.startswith("stale")     # codex needs Stop too
    assert doctor._check_band_hooks(tmp_path / "zzz", "codex").detail.startswith("absent")


def test_shipped_templates_are_wired_for_both_agents():
    for agent, root in (("claude_code", TEMPLATES / "activation" / "hooks"),
                        ("codex", TEMPLATES / "adapters" / "codex" / "activation" / "hooks")):
        assert band_view.band_hooks_state(root, agent) == ("wired", [])


def test_coordinator_brief_carries_the_band_line(tmp_path, monkeypatch):
    from levain.coordinator import launch

    src = pathlib.Path(launch.__file__).read_text()
    assert "## The band" in src and "NEEDS YOU band is shown to you on every turn" in src


# --- round 1 fixes ---------------------------------------------------------------------------------------------
def test_codex_stop_script_prints_the_block_json_once(qdir, store, monkeypatch, capsys):
    launch_rows(monkeypatch, {})
    monkeypatch.setenv("LEVAIN_HOME", str(qdir.parent))
    monkeypatch.delenv("LEVAIN_SEAT", raising=False)
    ask(store)
    deliver(store, "cx1", agent="codex", e=env(qdir))
    mod = _load(TEMPLATES / "adapters" / "codex" / "activation" / "hooks" / "stop.py", "stop_cx")
    h = mod.hook
    payload = {"session_id": "cx1", "last_assistant_message": "I dropped it", "stop_hook_active": False}
    monkeypatch.setattr(h, "read_stdin", lambda: payload)
    monkeypatch.setattr(h, "should_fire", lambda: True)
    monkeypatch.setattr(h, "consent_observe", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(h, "consent_ledger", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(h, "session_log", lambda *a, **k: None)
    payload_active = dict(payload, stop_hook_active=True)
    monkeypatch.setattr(h, "read_stdin", lambda: payload_active)
    assert mod.main() == 0 and capsys.readouterr().out == ""           # a continuation: never blocked
    monkeypatch.setattr(h, "read_stdin", lambda: payload)
    assert mod.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block" and "NEEDS YOU" in out["reason"]
    assert mod.main() == 0 and capsys.readouterr().out == ""           # one reprint per view
    monkeypatch.setattr(band_view, "hook_stop", lambda *_a, **_k: 1 / 0)
    assert mod.main() == 0 and capsys.readouterr().out == ""           # fail open


def test_codex_needs_the_stop_script_to_be_wired(tmp_path):
    d = _hooks_dir(tmp_path / "c", wired=True)
    (d / "stop.py").write_text("pass\n")
    assert band_view.band_hooks_state(d, "codex") == ("stale", ["stop.py"])


def test_verifier_numbered_prose_after_a_bare_header_is_not_the_band(qdir, store, monkeypatch):
    launch_rows(monkeypatch, {})
    e = env(qdir)
    ask(store, "one?"), ask(store, "two?")
    deliver(store, e=e)
    fake = "I omitted band@2\n1. First result\n2. Second result"
    assert stop("s1", fake, e) is not None


def test_verifier_band_header_number_is_digit_bounded(qdir, store, monkeypatch):
    launch_rows(monkeypatch, {})
    e = env(qdir)
    ask(store)
    deliver(store, e=e)
    # a view kept before blocks existed: the legacy check, band@1 must not be satisfied by band@10
    sid = band_view._store_id_of(store)
    with store._write_by(__import__("time").monotonic() + 2) as conn:
        conn.execute("INSERT INTO band_views (viewer, seen_through, open_list, store_id, at, gen) "
                     "VALUES ('claude_code:old', 1, '[1]', ?, 'x', 5)", (sid,))
    got = band_view._view_row(store, "claude_code:old")
    assert got and got[2] is None
    assert band_view._reprint_check(got[0], None, "band@10\n1. a") == ([], True)
    assert band_view._reprint_check(got[0], None, "band@1\n1. a") == ([], False)


def test_verifier_checks_view_row_in_one_snapshot(qdir, store, monkeypatch):
    launch_rows(monkeypatch, {})
    e = env(qdir)
    ask(store, "one?")
    deliver(store, e=e)
    ask(store, "two?")
    band2 = deliver(store, e=e)
    view, gen, block = band_view._view_row(store, "claude_code:s1")
    assert view.open_list == (1, 2) and block == band2 and gen == band_view._view_row(store, "claude_code:s1")[1]


def test_verifier_accepts_the_reprint_with_the_viewer_delta_line(qdir, store, monkeypatch):
    launch_rows(monkeypatch, {})
    e = env(qdir)
    ask(store, "one?")
    deliver(store, e=e)
    ask(store, "two?")
    text = deliver(store, e=e)
    assert stop("s1", "ok\n\n" + text.replace("\n1.", "\n\n> 1.", 1), e) is None


def test_hooks_dir_for_codex_never_takes_a_neighbouring_claude_install(tmp_path):
    proj = tmp_path / "proj"
    _hooks_dir(proj, wired=True)
    e = {"CODEX_HOME": str(tmp_path / "none"), "CLAUDE_PROJECT_DIR": str(proj)}
    assert band_view._find_hooks_dir("codex", str(proj), e) is None
    assert band_view._find_hooks_dir("claude_code", str(proj), e) == proj / "activation" / "hooks"
    cx = tmp_path / "cx"
    cx.mkdir()
    d = tmp_path / "other" / "activation" / "hooks"
    cmd = f'python3 -m levain.hookrun "{d}/user_prompt_submit.py"'
    (cx / "hooks.json").write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{"command": cmd}]}]}}))
    assert band_view._find_hooks_dir("codex", str(proj), {"CODEX_HOME": str(cx)}) == d


def test_gate_notice_for_codex_with_only_a_claude_install_nearby(qdir, store, monkeypatch, tmp_path):
    launch_rows(monkeypatch, {})
    proj = tmp_path / "proj"
    _hooks_dir(proj, wired=True)
    ask(store)
    e = env(qdir, CODEX_HOME=str(tmp_path / "none"))
    assert "absent" in band_view.gate_notice(qdir, "codex", "cx1", str(proj), e)


def test_gate_notice_counts_authority_items_and_fails_open_per_source(qdir, store, monkeypatch, tmp_path):
    from levain import band_sources

    launch_rows(monkeypatch, {})
    bare = tmp_path / "bare"
    bare.mkdir()
    e = env(qdir)
    extra = band_sources.Extra(source="consent", ref="c1", filed="2026-01-01T00:00:00Z", tag="t", what="w",
                               options=("x",), rec="a", rec_letter="a", blocks="b", origin="o")
    monkeypatch.setattr(band_view, "read_extras", lambda *_a, **_k: band_sources.Extras(items=[extra]))
    note = band_view.gate_notice(qdir, "claude_code", "a1", str(bare), e)       # authority item only, no question
    assert note and "1 item needs" in note
    ask(store)
    assert "2 items" in band_view.gate_notice(qdir, "claude_code", "a2", str(bare), e)
    monkeypatch.setattr(band_view, "read_extras", lambda *_a, **_k: (_ for _ in ()).throw(OSError("x")))
    assert "at least 1 item" in band_view.gate_notice(qdir, "claude_code", "a3", str(bare), e)
    monkeypatch.setattr(band_view, "read_extras", lambda *_a, **_k: band_sources.Extras(items=[extra], failed=["s"]))
    assert "at least 2 items" in band_view.gate_notice(qdir, "claude_code", "a4", str(bare), e)


def test_gate_notice_not_claimed_when_undeliverable_and_checks_the_claim_first(qdir, store, monkeypatch, tmp_path):
    launch_rows(monkeypatch, {})
    bare = tmp_path / "bare"
    bare.mkdir()
    ask(store)
    e = env(qdir)
    assert band_view.gate_notice(qdir, "claude_code", "h1", str(bare), e, deliverable=False) is None
    assert band_view.gate_notice(qdir, "claude_code", "h1", str(bare), e) is not None   # still owed: not claimed
    # claimed: nothing heavy runs
    for name in ("is_autonomous", "_find_hooks_dir", "_read"):
        monkeypatch.setattr(band_view, name, lambda *_a, **_k: 1 / 0)
    assert band_view.gate_notice(qdir, "claude_code", "h1", str(bare), e) is None


def test_claude_gate_does_not_claim_the_notice_on_a_hard_stop():
    src = (pathlib.Path(levain.__file__).parent / "consent" / "adapters" / "claude_code.py").read_text()
    assert "deliverable=not stopped" in src
