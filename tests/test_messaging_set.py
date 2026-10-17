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


def test_an_update_that_errors_restores_the_previous_record(env, capsys, monkeypatch):
    ent = _init(env, capsys, "a", messaging="off")
    monkeypatch.setattr("levain.update.run_update", lambda *a, **k: 1)
    assert _set(ent, "governed") == 1
    assert _config(ent)["messaging"] == "off"
    out = capsys.readouterr().out
    assert "restored" in out
    # no previous record: the key is removed again
    cfg = _config(ent)
    cfg.pop("messaging")
    (ent / ".levain" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    assert _set(ent, "governed") == 1
    assert "messaging" not in _config(ent)
    # a raising update restores too
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr("levain.update.run_update", boom)
    with pytest.raises(RuntimeError):
        _set(ent, "unmanaged")
    assert "messaging" not in _config(ent)


def test_a_held_settings_file_keeps_the_record_and_says_so(env, capsys):
    ent = _init(env, capsys, "a")
    doc = json.loads((ent / SETTINGS).read_text(encoding="utf-8"))
    doc["myOwnKey"] = True  # an operator edit
    (ent / SETTINGS).write_text(json.dumps(doc), encoding="utf-8")
    rc = _set(ent, "governed")
    out = capsys.readouterr().out
    assert _config(ent)["messaging"] == "governed"
    assert rc == 1 and "held for review" in out and "Merge the staged copy" in out
    assert "crossSessionInbound" not in json.loads((ent / SETTINGS).read_text(encoding="utf-8"))


def test_loosening_is_not_applied_by_update_and_is_refused_with_the_record_restored(env, capsys):
    ent = _init(env, capsys, "a", messaging="governed")
    before = (ent / SETTINGS).read_bytes()
    assert _set(ent, "unmanaged") == 1
    assert "never loosens" in capsys.readouterr().out
    assert _config(ent)["messaging"] == "governed"
    assert (ent / SETTINGS).read_bytes() == before


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
