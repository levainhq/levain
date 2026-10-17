"""`levain messaging` / `messaging set MODE`: one writer. The verb records the choice through
`_write_messaging_choice` and runs `levain update`; it adds no renderer of its own."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from levain import cli
from levain import host_guard as hg
from levain.install import run_init

SETTINGS = Path(".claude") / "settings.json"


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    monkeypatch.setattr(hg, "load_managed_settings", lambda platform=None: [])
    return tmp_path


def _answers(env: Path, capsys) -> Path:
    from tests.test_init_answers import _filled

    af = env / "a.json"
    af.write_text(json.dumps(_filled(capsys)), encoding="utf-8")
    return af


def _init(env: Path, capsys, name: str, messaging: str | None = None) -> Path:
    ent = env / name
    assert run_init(ent, "claude-code", force=False, answers_file=_answers(env, capsys), messaging=messaging) == 0
    capsys.readouterr()
    return ent


def _config(ent: Path) -> dict:
    return json.loads((ent / ".levain" / "config.json").read_text(encoding="utf-8"))


def _set(ent: Path, mode: str) -> int:
    return cli.main(["messaging", "set", mode, "--path", str(ent), "--no-pip"])


def test_set_governed_is_byte_identical_to_a_fresh_governed_init(env, capsys):
    ent = _init(env, capsys, "a")  # isolate by default
    fresh = _init(env, capsys, "b", messaging="governed")
    assert (ent / SETTINGS).read_bytes() != (fresh / SETTINGS).read_bytes()
    _set(ent, "governed")
    out = capsys.readouterr().out
    assert _config(ent)["messaging"] == "governed"
    assert "levain update" in out
    got, want = (ent / SETTINGS).read_bytes(), (fresh / SETTINGS).read_bytes()
    # the install path appears in no messaging key; the two files differ only where the workspace path does
    assert got.replace(str(ent).encode(), b"X") == want.replace(str(fresh).encode(), b"X")


def test_refusals_write_nothing(env, capsys):
    ent = _init(env, capsys, "a")
    before = (ent / ".levain" / "config.json").read_bytes(), (ent / SETTINGS).read_bytes()
    (ent / ".levain" / "config.json").write_text("[1]", encoding="utf-8")
    assert _set(ent, "governed") == 1
    assert "not a JSON object" in capsys.readouterr().out
    assert (ent / ".levain" / "config.json").read_text(encoding="utf-8") == "[1]"
    (ent / ".levain" / "config.json").write_bytes(before[0])
    assert (ent / SETTINGS).read_bytes() == before[1]
    # a codex install is refused with init's text
    cx = env / "cx"
    assert run_init(cx, "codex", force=False, answers_file=_answers(env, capsys)) == 0
    capsys.readouterr()
    cfg = (cx / ".levain" / "config.json").read_bytes() if (cx / ".levain" / "config.json").exists() else None
    assert _set(cx, "governed") == 1
    assert "claude-code adapter only" in capsys.readouterr().out
    after = (cx / ".levain" / "config.json").read_bytes() if (cx / ".levain" / "config.json").exists() else None
    assert after == cfg and (after is None or b"messaging" not in after)
    # a bogus mode is an argparse error
    with pytest.raises(SystemExit):
        cli.main(["messaging", "set", "bogus", "--path", str(ent)])


def test_the_install_lock_is_held_across_the_whole_verb_and_run_update_is_reentrant(env, capsys, monkeypatch):
    import threading

    from levain import update as upd
    from levain.install import InstallBusy, install_lock

    ent = _init(env, capsys, "a")
    real, seen = upd.run_update, {}

    def spy(*a, **k):
        def other():  # another thread opens its own fd: a held flock refuses it
            try:
                with install_lock(ent, create=False):
                    seen["other"] = "acquired"
            except InstallBusy:
                seen["other"] = "busy"
        t = threading.Thread(target=other)
        t.start()
        t.join()
        seen["rc"] = real(*a, **k)  # the nested acquire inside the held lock must not report busy
        return seen["rc"]

    monkeypatch.setattr(upd, "run_update", spy)
    assert _set(ent, "governed") == 0
    out = capsys.readouterr().out
    assert seen == {"other": "busy", "rc": 0} and "another levain process" not in out
    assert _config(ent)["messaging"] == "governed"


def test_an_exception_after_the_settings_write_applies_the_same_commit_gate_then_reraises(
        env, capsys, monkeypatch):
    from levain import update as upd

    real = upd.run_update

    def after_write(*a, **k):
        real(*a, **k)  # settings.json IS written and pinned
        raise RuntimeError("boom")

    ent = _init(env, capsys, "a", messaging="governed")
    monkeypatch.setattr(upd, "run_update", after_write)
    with pytest.raises(RuntimeError):
        _set(ent, "unmanaged")
    out = capsys.readouterr().out
    cfg = _config(ent)
    # the file carries unmanaged and the pin covers it: the record follows, with its stamp
    assert "crossSessionInbound" not in json.loads((ent / SETTINGS).read_text(encoding="utf-8"))
    assert cfg["messaging"] == "unmanaged" and cfg["messaging_changed"]["from"] == "governed"
    assert "LOOSENED governed -> unmanaged" in out
    # a failed pin write: the gate refuses, nothing is recorded, the exception still propagates
    ent2 = _init(env, capsys, "b", messaging="governed")
    cfg2 = _config(ent2)
    monkeypatch.setattr(hg, "repin_after_levain_write", lambda *a, **k: False)
    with pytest.raises(RuntimeError):
        _set(ent2, "unmanaged")
    assert _config(ent2) == cfg2


def test_an_exception_after_a_tightening_write_records_it_too(env, capsys, monkeypatch):
    from levain import update as upd

    real = upd.run_update

    def after_write(*a, **k):
        real(*a, **k)
        raise RuntimeError("boom")

    ent = _init(env, capsys, "c")
    monkeypatch.setattr(upd, "run_update", after_write)
    with pytest.raises(RuntimeError):
        _set(ent, "governed")
    assert _config(ent)["messaging"] == "governed"


def test_validation_runs_under_the_install_lock(env, capsys):
    import threading

    from levain.install import install_lock

    ent = _init(env, capsys, "a")
    (ent / ".levain" / "config.json").write_text("[1]", encoding="utf-8")  # a refusal that is not the lock's
    release, held = threading.Event(), threading.Event()

    def holder():
        with install_lock(ent, create=False):
            held.set()
            release.wait(10)
    t = threading.Thread(target=holder)
    t.start()
    held.wait(10)
    try:
        assert _set(ent, "governed") == 1
        out = capsys.readouterr().out
    finally:
        release.set()
        t.join()
    assert "another levain process" in out and "not a JSON object" not in out


def test_a_stale_pending_file_from_before_is_not_reported_as_held(env, capsys, monkeypatch):
    from levain import update as upd

    ent = _init(env, capsys, "a")
    staged = ent / ".levain" / "pending" / ".claude" / "settings.json"
    staged.parent.mkdir(parents=True)
    staged.write_text("{}", encoding="utf-8")  # left by an earlier update
    monkeypatch.setattr(upd, "run_update", lambda *a, **k: 0)  # this run stages nothing and renders nothing
    cfg0 = _config(ent)
    assert _set(ent, "governed") == 1
    out = capsys.readouterr().out
    assert "held for review" not in out and "NOT applied and NOT recorded" in out
    assert _config(ent) == cfg0


def test_a_held_settings_file_records_nothing_and_says_merge_then_rerun(env, capsys):
    ent = _init(env, capsys, "a")
    doc = json.loads((ent / SETTINGS).read_text(encoding="utf-8"))
    doc["myOwnKey"] = True  # an operator edit
    (ent / SETTINGS).write_text(json.dumps(doc), encoding="utf-8")
    cfg0 = _config(ent)
    rc = _set(ent, "governed")
    out = capsys.readouterr().out
    assert _config(ent) == cfg0
    assert rc == 1 and "held for review" in out and "NOT recorded" in out
    assert "merge it" in out and "levain messaging set governed" in out
    assert "crossSessionInbound" not in json.loads((ent / SETTINGS).read_text(encoding="utf-8"))


def test_success_stamps_the_change_and_doctor_shows_it_and_loosened_prints_one_way(env, capsys):
    from levain.doctor import _check_cc_messaging

    ent = _init(env, capsys, "a")
    assert _set(ent, "governed") == 0
    out = capsys.readouterr().out
    assert "LOOSENED" not in out  # isolate -> governed tightens
    ch = _config(ent)["messaging_changed"]
    assert ch["from"] == "isolate" and ch["to"] == "governed" and ch["at"].endswith("Z")
    assert _set(ent, "unmanaged") == 0
    out = capsys.readouterr().out
    assert "LOOSENED governed -> unmanaged" in out
    ch = _config(ent)["messaging_changed"]
    assert (ch["from"], ch["to"]) == ("governed", "unmanaged")
    row = _check_cc_messaging(ent)[0]
    assert f"(changed from governed {ch['at']})" in row.detail
    assert _set(ent, "governed") == 0
    assert "LOOSENED" not in capsys.readouterr().out


def test_show_infers_governed_from_the_file_when_nothing_is_recorded(env, capsys):
    ent = _init(env, capsys, "a", messaging="governed")
    cfg = _config(ent)
    cfg.pop("messaging")
    (ent / ".levain" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    assert cli.main(["messaging", "--path", str(ent)]) == 0
    out = capsys.readouterr().out
    assert "governed (inferred from .claude/settings.json; no record)" in out and "workspace" in out


def test_a_deeply_nested_config_json_is_a_clean_error_not_a_traceback(env, capsys):
    ent = _init(env, capsys, "a")
    (ent / ".levain" / "config.json").write_text("[" * 200000, encoding="utf-8")
    assert _set(ent, "governed") == 1
    assert "FAIL" in capsys.readouterr().out
    assert cli.main(["messaging", "--path", str(ent)]) in (0, 1)
    capsys.readouterr()


def test_set_loosens_to_a_fresh_init_and_a_plain_update_leaves_it(env, capsys):
    ent = _init(env, capsys, "a")
    fresh = _init(env, capsys, "b", messaging="unmanaged")
    assert _set(ent, "governed") == 0
    assert _set(ent, "unmanaged") == 0
    assert "never loosens" not in capsys.readouterr().out
    assert _config(ent)["messaging"] == "unmanaged"
    got, want = (ent / SETTINGS).read_bytes(), (fresh / SETTINGS).read_bytes()
    assert got.replace(str(ent).encode(), b"X") == want.replace(str(fresh).encode(), b"X")
    # a plain update (no explicit choice) changes nothing
    assert cli.main(["update", "--path", str(ent), "--no-pip"]) == 0
    assert (ent / SETTINGS).read_bytes() == got


def test_show_reads_recorded_default_and_file(env, capsys):
    ent = _init(env, capsys, "a", messaging="governed")
    assert cli.main(["messaging", "--path", str(ent)]) == 0
    out = capsys.readouterr().out
    assert "messaging: governed (recorded" in out and "carries the governed controls" in out
    ent = _init(env, capsys, "u", messaging="unmanaged")
    cfg = _config(ent)
    cfg.pop("messaging")
    (ent / ".levain" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    assert cli.main(["messaging", "show", "--path", str(ent)]) == 0
    out = capsys.readouterr().out
    assert "messaging: isolate (the default" in out and "does not carry the isolate controls" in out
    # read-only
    assert "messaging" not in _config(ent)


def test_another_file_held_for_review_does_not_stop_the_choice_being_recorded(env, capsys):
    ent = _init(env, capsys, "a", messaging="governed")
    (ent / "seed" / "partnership.md").unlink()  # the carrier imports it: update holds the carrier for review
    rc = _set(ent, "unmanaged")
    out = capsys.readouterr().out
    assert rc != 0 and "applied and recorded" in out and "LOOSENED governed -> unmanaged" in out
    cfg = _config(ent)
    assert cfg["messaging"] == "unmanaged" and cfg["messaging_changed"]["to"] == "unmanaged"
    assert cli.main(["update", "--path", str(ent), "--no-pip"]) != 0  # the carrier is still held
    capsys.readouterr()
    assert _config(ent)["messaging"] == "unmanaged"
    assert "crossSessionInbound" not in json.loads((ent / SETTINGS).read_text(encoding="utf-8"))


def test_a_pin_write_that_fails_after_the_settings_file_is_written_records_nothing(env, capsys, monkeypatch):
    ent = _init(env, capsys, "a", messaging="governed")
    cfg0 = _config(ent)
    monkeypatch.setattr(hg, "repin_after_levain_write", lambda *a, **k: False)
    _set(ent, "unmanaged")
    out = capsys.readouterr().out
    assert "crossSessionInbound" not in json.loads((ent / SETTINGS).read_text(encoding="utf-8"))
    assert _config(ent) == cfg0 and "NOT recorded" in out and "LOOSENED" not in out


def _drop_record(ent: Path) -> None:
    cfg = _config(ent)
    cfg.pop("messaging", None)
    (ent / ".levain" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


def test_show_with_no_record_infers_only_from_an_exact_match_else_custom(env, capsys):
    ent = _init(env, capsys, "d", messaging="governed")
    _drop_record(ent)
    doc = json.loads((ent / SETTINGS).read_text(encoding="utf-8"))
    for k in ("crossSessionInbound", "isolatePeerMachines"):
        doc.pop(k, None)
    doc["permissions"] = {"deny": ["SendMessage"]}  # a deny-only file matches no choice
    (ent / SETTINGS).write_text(json.dumps(doc), encoding="utf-8")
    assert cli.main(["messaging", "--path", str(ent)]) == 0
    out = capsys.readouterr().out
    assert "messaging: custom (no record" in out and "match no choice" in out and "inferred" not in out
    off = _init(env, capsys, "o", messaging="off")
    _drop_record(off)
    assert cli.main(["messaging", "--path", str(off)]) == 0
    out = capsys.readouterr().out
    assert "messaging: off (inferred from .claude/settings.json; no record)" in out


def test_a_changed_choice_drops_the_old_stamp_and_a_same_choice_rewrite_keeps_it(env, capsys):
    ent = _init(env, capsys, "a")
    assert _set(ent, "governed") == 0
    assert "messaging_changed" in _config(ent)
    capsys.readouterr()
    assert run_init(ent, "claude-code", force=True, answers_file=_answers(env, capsys), messaging="unmanaged") == 0
    assert "messaging_changed" not in _config(ent)  # fails on a63871b: the governed stamp survived
    capsys.readouterr()
    assert _set(ent, "governed") == 0
    stamp = _config(ent)["messaging_changed"]
    from levain.install import _write_messaging_choice
    _write_messaging_choice(ent, "governed", lambda m: None)
    assert _config(ent)["messaging_changed"] == stamp


def test_an_unrecorded_governed_install_moving_to_isolate_stamps_and_prints_loosened(env, capsys):
    ent = _init(env, capsys, "a", messaging="governed")
    _drop_record(ent)
    assert _set(ent, "isolate") == 0
    out = capsys.readouterr().out
    ch = _config(ent)["messaging_changed"]
    assert ch["from"] == "governed" and ch["to"] == "isolate"
    assert "LOOSENED governed -> isolate" in out


def test_no_pin_names_the_fix(env, capsys):
    ent = _init(env, capsys, "a")
    (ent / ".levain" / "agent_hook_pins.json").unlink()
    cfg0 = _config(ent)
    assert _set(ent, "governed") != 0
    out = capsys.readouterr().out
    assert _config(ent).get("messaging") == cfg0.get("messaging")
    assert "--repin-hooks" in out



def test_custom_controls_replaced_by_a_choice_stamp_from_custom(env, capsys, monkeypatch):
    # A hand-made custom file is held for review by update (the adapter receipt records levain's bytes), so the
    # gate is driven directly: when it does record over controls that matched no choice, it says so.
    import levain.host_guard as hg
    from levain.install import _commit_messaging_if_applied

    ent = _init(env, capsys, "c", messaging="governed")
    _drop_record(ent)
    monkeypatch.setattr(hg, "workspace_settings_pin_matches", lambda install: True)
    msgs: list[str] = []
    assert _commit_messaging_if_applied(ent, "governed", "custom", msgs.append)
    ch = _config(ent)["messaging_changed"]
    assert ch["from"] == "custom" and ch["to"] == "governed"
    assert "REPLACED custom messaging controls -> governed" in msgs and not any("LOOSENED" in m for m in msgs)
