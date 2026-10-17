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


@pytest.mark.parametrize("fail", ["rc", "raise"])
def test_a_forced_update_error_records_nothing_and_a_plain_update_repairs_toward_the_record(
        env, capsys, monkeypatch, fail):
    from levain import update as upd

    real = upd.run_update

    def after_write(*a, **k):
        real(*a, **k)  # settings.json IS written
        if fail == "raise":
            raise RuntimeError("boom")
        return 1

    # looser new file: recorded governed, set unmanaged, update errors after writing the loose file
    ent = _init(env, capsys, "a", messaging="governed")
    before_cfg = _config(ent)
    monkeypatch.setattr(upd, "run_update", after_write)
    if fail == "raise":
        with pytest.raises(RuntimeError):
            _set(ent, "unmanaged")
    else:
        assert _set(ent, "unmanaged") == 1
    cfg = _config(ent)
    assert cfg == before_cfg and "messaging_changed" not in cfg
    loose = json.loads((ent / SETTINGS).read_text(encoding="utf-8"))
    assert "crossSessionInbound" not in loose  # the loose file is on disk
    assert "restored" not in capsys.readouterr().out
    monkeypatch.setattr(upd, "run_update", real)
    assert cli.main(["update", "--path", str(ent), "--no-pip"]) == 0
    capsys.readouterr()
    fresh = _init(env, capsys, "f", messaging="governed")
    assert ((ent / SETTINGS).read_bytes().replace(str(ent).encode(), b"X")
            == (fresh / SETTINGS).read_bytes().replace(str(fresh).encode(), b"X"))  # re-tightened
    # tighter new file: recorded isolate, set governed, update errors: the tighter file is kept
    ent2 = _init(env, capsys, "b")
    cfg2 = _config(ent2)
    monkeypatch.setattr(upd, "run_update", after_write)
    if fail == "raise":
        with pytest.raises(RuntimeError):
            _set(ent2, "governed")
    else:
        assert _set(ent2, "governed") == 1
    assert _config(ent2) == cfg2
    monkeypatch.setattr(upd, "run_update", real)
    assert cli.main(["update", "--path", str(ent2), "--no-pip"]) == 0
    assert json.loads((ent2 / SETTINGS).read_text(encoding="utf-8"))["crossSessionInbound"] == "hold"


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
