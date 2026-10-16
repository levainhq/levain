"""Two Codex installs share one CODEX_HOME hooks.json (Codex reads one per CODEX_HOME). Reproduced through the
real CLI on 17ad2c5 (1016+1): B's `levain init --adapter codex` turned A's pin row into a FAIL that blamed "an
agent or another program", although levain itself had repointed the file; B's own init left B unpinned; and A
taking the file back with `init --force` from an upgraded levain stayed red until a manual --repin-hooks.
The wiring rows, which say A is no longer wired, are true and stay FAILs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from levain import doctor
from levain import host_guard as hg
from levain import install as install_mod
from levain.install import run_init
from tests.test_repin_on_install import _pin_check, make_install  # noqa: F401  (fixture)


@pytest.fixture
def cj(tmp_path) -> Path:
    return tmp_path / "codex-home" / "hooks.json"


def _wiring(install: Path) -> list:
    return [r for r in doctor._check_codex(install) if r.name.startswith("~/.codex/hooks.json") and "→" in r.name]


def test_a_second_codex_install_repoints_without_a_tamper_fail(make_install, tmp_path):
    a = make_install("a", adapter="codex")
    b = make_install("b", adapter="codex")
    res = _pin_check(a)
    assert res.ok and res.warn, res  # not compared is never a clean OK (L1 on 6087443)
    assert str(b) in res.detail and "NOT compared" in res.detail
    assert hg.agent_pin_status(a) == "match"
    assert hg.codex_hooks_owner(a) == b and hg.codex_hooks_owner(b) is None
    # B's init pinned its own write: no first-run trust-on-first-use warning
    rb = _pin_check(b)
    assert rb.ok and not rb.warn and "match the pin" in rb.detail, rb
    # A really is unwired, and the row says by whom and how to take it back
    rows = _wiring(a)
    assert rows and all(not r.ok for r in rows)
    assert all(str(b) in r.detail and "--force" in r.hint and "answers.json" in r.hint for r in rows)


def test_taking_back_over_from_an_upgraded_levain_re_pins(make_install, monkeypatch, capsys, tmp_path, cj):
    a = make_install("a", adapter="codex")
    b = make_install("b", adapter="codex")
    real = install_mod._codex_hooks_json
    monkeypatch.setattr(install_mod, "_codex_hooks_json",
                        lambda *args: real(*args).replace('"timeout": 30', '"timeout": 31'))
    before = cj.read_bytes()
    assert run_init(a, "codex", force=True, answers_file=tmp_path / "a-answers.json") == 0
    capsys.readouterr()
    assert cj.read_bytes() != before and b'"timeout": 31' in cj.read_bytes()
    ra = _pin_check(a)
    assert ra.ok and not ra.warn and "match the pin" in ra.detail and "NOT compared" not in ra.detail, ra
    rb = _pin_check(b)
    assert rb.ok and str(a) in rb.detail, rb
    assert all(r.ok for r in _wiring(a))


def test_an_edit_of_a_repointed_file_still_fails_both(make_install, cj):
    a = make_install("a", adapter="codex")
    b = make_install("b", adapter="codex")
    doc = json.loads(cj.read_text(encoding="utf-8"))
    doc["hooks"]["SessionStart"][0]["hooks"].append({"type": "command", "command": "/tmp/planted.sh"})
    cj.write_text(json.dumps(doc), encoding="utf-8")
    for inst in (a, b):
        res = _pin_check(inst)
        assert not res.ok and "codex hooks.json" in res.detail, (inst, res)
    assert hg.codex_hooks_owner(a) is None


def test_bytes_the_owner_receipt_does_not_record_are_not_levains(make_install, cj):
    a = make_install("a", adapter="codex")
    make_install("b", adapter="codex")
    cj.write_text(cj.read_text(encoding="utf-8") + "\n", encoding="utf-8")  # same commands, other bytes
    assert hg.codex_hooks_owner(a) is None
    assert not _pin_check(a).ok


def test_commands_naming_a_dir_with_no_receipt_are_not_levains(make_install, tmp_path, cj):
    a = make_install("a", adapter="codex")
    b = make_install("b", adapter="codex")
    fake = tmp_path / "fake"
    shutil.copytree(b / "activation", fake / "activation")
    cj.write_text(cj.read_text(encoding="utf-8").replace(str(b), str(fake)), encoding="utf-8")
    assert hg.codex_hooks_owner(a) is None
    assert not _pin_check(a).ok


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs")
def test_a_fifo_where_the_receipt_should_be_does_not_hang(make_install, tmp_path, cj):
    a = make_install("a", adapter="codex")
    b = make_install("b", adapter="codex")
    rec = b.joinpath(*install_mod.ADAPTER_RECEIPT_REL)
    rec.unlink()
    os.mkfifo(rec)
    t = time.monotonic()
    assert hg.codex_hooks_owner(a) is None
    assert time.monotonic() - t < 5


# L1 on 6087443, reproduced: dropping every gone "hook command" key hid the deletion of a script a SETTINGS file
# names, and the next init (status "match") would have re-pinned the deletion.
def test_a_deleted_script_a_settings_file_names_still_fails_while_repointed(make_install, cj):
    a = make_install("a", adapter="codex")
    tool = a / "tools" / "hook.sh"
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    (a / ".claude").mkdir(exist_ok=True)
    (a / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": f"/bin/sh {tool}"}]}]}}), encoding="utf-8")
    hg.repin_agent_hooks(a)
    make_install("b", adapter="codex")
    assert hg.agent_pin_status(a) == "match"
    tool.unlink()
    assert hg.agent_pin_status(a) == "changed"
    res = _pin_check(a)
    assert not res.ok and "hook.sh" in res.detail, res


def test_a_forged_owner_with_a_receipt_is_a_warning_never_a_clean_ok(make_install, tmp_path, cj):
    a = make_install("a", adapter="codex")
    evil = tmp_path / "evil"
    (evil / "activation" / "hooks").mkdir(parents=True)
    (evil / "activation" / "hooks" / "session_start.py").write_text("print('planted')\n", encoding="utf-8")
    text = json.dumps({"hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": f'"/usr/bin/python3" "{evil}/activation/hooks/session_start.py"'}]}]}})
    cj.write_text(text, encoding="utf-8")
    (evil / ".levain").mkdir()
    (evil / ".levain" / "adapter-receipt.json").write_text(json.dumps(
        {"schema": 1, "files": {"codex-home/hooks.json": hashlib.sha256(text.encode()).hexdigest()}}), encoding="utf-8")
    assert hg.codex_hooks_owner(a) == evil  # the receipt is same-uid writable: a known edge, stated
    res = _pin_check(a)
    assert res.warn and str(evil) in res.detail, res
    rows = _wiring(a)
    assert rows and all(not r.ok and "did not run levain init" in r.hint for r in rows)


def test_repin_while_another_install_owns_it_says_the_file_was_not_pinned(make_install):
    a = make_install("a", adapter="codex")
    b = make_install("b", adapter="codex")
    (res,) = doctor._check_agent_hook_pins(a, repin=True)
    assert "NOT pinned" in res.detail and str(b) in res.detail, res
