"""levain#127 (ruling np-389e829d (a)): a non-default dream ``claude_bin`` is shown by ``agents show dream`` (text and
--json) and by the doctor row ``dream program`` together with whether the signed link binds it (levain#126 made the
link bind it, so an unbound program is a refused fire, never a program that runs); a default one prints nothing."""
from __future__ import annotations

import json
from pathlib import Path

from levain import doctor
from levain.dream import agent as da
from tests.test_dream_live_binding import _enabled, _hand_edit, _show_json, env  # noqa: F401 - env is a fixture

FALSE_CLAIM = "does not bind a live install's program"


def _show_text(env, capsys) -> str:
    from levain.cli import main

    capsys.readouterr()
    assert main(["agents", "--store-dir", str(env["ag"]), "show", "dream"]) == 0
    return capsys.readouterr().out


def _stub(env) -> str:
    p = env["tmp"] / "stubclaude"
    p.write_text("#!/bin/sh\n")
    p.chmod(0o700)
    return str(p)


def _signed_with(env, capsys, bin_) -> None:
    da.write_install(env["ag"], claude_bin=bin_, **env["kw"])
    _enabled(env, capsys)


def test_a_default_program_is_not_shown(env, capsys):
    da.write_install(env["ag"], **env["kw"])
    _enabled(env, capsys)
    assert da.dream_program_view(env["ag"]).line() is None
    assert "dream install:" not in _show_text(env, capsys)
    j = _show_json(env, capsys)
    assert j["dream_claude_bin"] is None and j["dream_program_line"] is None
    assert doctor._check_dream_claude_bin(env["ag"]) == []


def test_a_program_the_link_binds_says_a_fire_runs_it(env, capsys):
    bin_ = _stub(env)
    _signed_with(env, capsys, bin_)
    text = _show_text(env, capsys)
    assert f"a fire runs {bin_!r} (bound by the signed link)" in text
    assert FALSE_CLAIM not in text and "refuses" not in text
    j = _show_json(env, capsys)
    assert j["dream_claude_bin"] == bin_ and j["dream_install_refusal"] is None
    (row,) = doctor._check_dream_claude_bin(env["ag"])
    assert row.ok and row.warn and f"a fire runs {bin_!r} (bound by the signed link)" in row.detail
    assert FALSE_CLAIM not in row.detail


def test_a_program_the_link_does_not_bind_is_a_refusal_never_a_run(env, capsys):
    # signed with the default, then the install file is edited to a stub: the fire refuses (levain#126)
    da.write_install(env["ag"], **env["kw"])
    _enabled(env, capsys)
    bin_ = _stub(env)
    _hand_edit(env, claude_bin=bin_)
    text = _show_text(env, capsys)
    assert "a fire of this spec refuses with the facts as they stand" in text
    assert (f"the install names {bin_!r} but the signed link does not bind it: a fire is refused until the dream is "
            "re-signed") in text
    assert "a fire runs" not in text and FALSE_CLAIM not in text
    j = _show_json(env, capsys)
    assert j["dream_install_refusal"] and j["dream_claude_bin"] == bin_ and "a fire runs" not in j["dream_program_line"]
    (row,) = doctor._check_dream_claude_bin(env["ag"])
    assert row.ok and row.warn and "does not bind it: a fire is refused" in row.detail and "a fire runs" not in row.detail
    # the real restore: re-running enable keeps the program, so the hint names removing it and --revise
    assert "remove `claude_bin`" in row.hint and "enable --revise" in row.hint and "keeps the program" in row.hint


def test_the_program_is_shown_from_one_read_of_the_install(env, capsys, monkeypatch):
    da.write_install(env["ag"], **env["kw"])
    _enabled(env, capsys)
    _hand_edit(env, claude_bin=_stub(env))
    reads = []
    real = Path.read_text

    def counting(self, *a, **k):
        if self.name == da.INSTALL_FILE:
            reads.append(self)
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", counting)
    assert da.dream_program_view(env["ag"]).line() is not None
    assert len(reads) == 1


def test_an_adversarial_program_is_marked_and_capped_in_text_json_and_the_doctor(env, capsys):
    da.write_install(env["ag"], **env["kw"])
    _enabled(env, capsys)
    evil = "/x\x1b[31m‮evil​" + "a" * 5000
    _hand_edit(env, claude_bin=evil)
    shown = da.shown_program(evil)
    assert len(shown) < 300 and "\x1b" not in shown and "‮" not in shown and "​" not in shown
    assert "U+202E" in shown and "more characters" in shown
    text = _show_text(env, capsys)
    out = _show_json(env, capsys)
    (row,) = doctor._check_dream_claude_bin(env["ag"])
    for s in (text, row.detail, json.dumps(out, ensure_ascii=False)):
        assert "\x1b" not in s and "‮" not in s and "a" * 300 not in s
    assert out["dream_claude_bin"] == shown
    assert shown in row.detail.replace("\\\\", "\\") or da.shown_program(evil)[:40] in row.detail


def test_an_empty_or_non_string_program_is_shown_not_read_as_the_default(env, capsys):
    da.write_install(env["ag"], **env["kw"])
    _enabled(env, capsys)
    for bad in ("", 7, ["claude"]):
        shown = repr(da.shown_program(bad))
        _hand_edit(env, claude_bin=bad)
        v = da.dream_program_view(env["ag"])
        assert v.program == da.shown_program(bad) and v.line() is not None
        assert f"names the program {shown}" in v.line() and "a fire is refused" in v.line()
        (row,) = doctor._check_dream_claude_bin(env["ag"])
        assert row.warn and shown in row.detail
        assert _show_json(env, capsys)["dream_claude_bin"] == da.shown_program(bad)  # "" is not null (the default)


def test_an_unrelated_bad_fact_does_not_hide_the_program(env, capsys):
    bin_ = _stub(env)
    _signed_with(env, capsys, bin_)
    _hand_edit(env, store_path=str(env["tmp"] / "gone.db"))
    v = da.dream_program_view(env["ag"])
    assert v.program == bin_ and v.note and "cannot read the install" in v.line() and repr(bin_) in v.line()
    assert "a fire runs" not in v.line()
    assert f"names the program {bin_!r}" in _show_text(env, capsys)
    (row,) = doctor._check_dream_claude_bin(env["ag"])
    assert row.warn and "cannot read the install" in row.detail


def test_an_unreadable_install_says_so_through_the_cli_and_the_doctor(env, capsys):
    da.write_install(env["ag"], **env["kw"])
    _enabled(env, capsys)
    (env["ag"] / da.INSTALL_FILE).write_text("{not json", encoding="utf-8")
    assert "dream install: cannot read the install: the facts file cannot be read (JSONDecodeError)" in \
        _show_text(env, capsys)
    assert "cannot read the install" in _show_json(env, capsys)["dream_program_line"]
    (row,) = doctor._check_dream_claude_bin(env["ag"])
    assert row.warn and "cannot read the install" in row.detail
    (env["ag"] / da.INSTALL_FILE).write_text("[1]", encoding="utf-8")
    assert "not a JSON object" in da.dream_program_view(env["ag"]).line()


def test_no_install_prints_nothing(tmp_path):
    assert da.dream_program_view(tmp_path) is None
    assert doctor._check_dream_claude_bin(tmp_path) == []
