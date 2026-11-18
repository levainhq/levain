"""levain#126: a LIVE dream install's compose program (``claude_bin``) and ``model`` are bound into the spec the link
signs, as a shadow install's facts are. A fact changed after the spec was registered (so after its link was signed) is
refused at the input hook and at the fire, never run under the old signature.

Residue no test here holds: a real install and a real signature (a person runs `levain dream enable --revise` on an
install whose facts name a program, then signs the new link)."""
from __future__ import annotations

import argparse
import json
from datetime import date
from types import SimpleNamespace

import pytest

from levain.dream import agent as da
from levain.dream import ready as rd
from levain.dream.cli import add_parsers

ZONE = "America/New_York"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    ag, ck, cdir, live = (tmp_path / n for n in ("ag", "ck", "cd", "live"))
    for d in (ag, ck, cdir, live):
        d.mkdir(mode=0o700)
    db = live / "memory.db"
    db.write_bytes(b"")
    return dict(ag=ag, tmp=tmp_path, kw=dict(store_path=db, cockpit_root=ck, coord_dir=cdir, zone=ZONE))


def _enable(env, *extra):
    from levain.agents.store import open_agent_store, set_default_zone

    set_default_zone(open_agent_store(env["ag"]), ZONE)
    p = argparse.ArgumentParser()
    add_parsers(p.add_subparsers())
    args = p.parse_args(["dream", "enable", "--agent-store-dir", str(env["ag"]), "--cockpit-root",
                         str(env["kw"]["cockpit_root"]), "--coord-dir", str(env["kw"]["coord_dir"]), *extra])
    return args.func(args)


def _enabled(env, capsys, *extra) -> str:
    """``levain dream enable --json`` over the facts file as it stands; the spec_hash it registered."""
    assert _enable(env, "--json", *extra) == 0, capsys.readouterr().err
    return json.loads(capsys.readouterr().out)["spec_hash"]


def _hand_edit(env, **facts):
    path = env["ag"] / da.INSTALL_FILE
    doc = json.loads(path.read_text(encoding="utf-8"))
    for k, v in facts.items():
        if v is None:
            doc.pop(k, None)
        else:
            doc[k] = v
    path.write_text(json.dumps(doc), encoding="utf-8")


def _row(spec_hash):
    return {"occurrence_id": "oc-x", "agent_id": "ag-1", "kind": "scheduled", "spec_hash": spec_hash,
            "window_key": rd.window_key_for(date(2026, 11, 2)),
            "launched_at": "2026-11-03T06:00:00Z"}


def _refused_everywhere(env, row, monkeypatch):
    """The hook, the bound check and the fire each refuse the occurrence, naming the program and model."""
    with pytest.raises(da.InstallRefused, match="program or model"):
        da.check_bound(env["ag"], row, da.read_install(env["ag"]))
    with pytest.raises(da.InstallRefused, match="program or model"):
        da.install_on_input(env["ag"])(row)
    monkeypatch.setattr(rd, "_read_occurrences", lambda agent_dir: [row])
    with pytest.raises(da.InstallRefused, match="program or model"):
        da.fire_factory(SimpleNamespace(directory=env["ag"]), row["occurrence_id"], 0)


# ------------------------------------------------------------------------------------------- the reproduced case

def test_a_claude_bin_swapped_after_the_spec_was_signed_is_refused_at_the_hook_and_the_fire(env, capsys, monkeypatch):
    # reproduced on origin/main a2da68f4e: check_bound returned early for a live install, so the hook and the fire ran
    # the swapped program under the link signed for the old one
    da.write_install(env["ag"], **env["kw"], claude_bin=str(env["tmp"] / "a" / "claude"))
    signed = _enabled(env, capsys)
    _hand_edit(env, claude_bin=str(env["tmp"] / "b" / "claude"))
    _refused_everywhere(env, _row(signed), monkeypatch)


def test_a_model_changed_after_signing_is_refused(env, capsys, monkeypatch):
    da.write_install(env["ag"], **env["kw"], model="claude-other")
    signed = _enabled(env, capsys)
    _hand_edit(env, model="claude-third")
    _refused_everywhere(env, _row(signed), monkeypatch)


def test_a_program_set_back_to_the_default_under_a_spec_that_binds_one_is_refused(env, capsys, monkeypatch):
    da.write_install(env["ag"], **env["kw"], claude_bin=str(env["tmp"] / "a" / "claude"))
    signed = _enabled(env, capsys)
    _hand_edit(env, claude_bin=None)
    _refused_everywhere(env, _row(signed), monkeypatch)


def test_a_program_named_after_an_unbound_spec_was_registered_is_refused(env, capsys, monkeypatch):
    # the order an install that named its program before this change has: spec registered with default facts, the
    # program written after. Its next fire refuses until `enable --revise` and a new signed link
    da.write_install(env["ag"], **env["kw"])
    signed = _enabled(env, capsys)
    da.write_install(env["ag"], **env["kw"], claude_bin=str(env["tmp"] / "a" / "claude"))
    _refused_everywhere(env, _row(signed), monkeypatch)


# --------------------------------------------------------------------------------------------- what still runs

def test_the_facts_the_spec_binds_pass_the_check(env, capsys):
    da.write_install(env["ag"], **env["kw"], claude_bin=str(env["tmp"] / "a" / "claude"), model="claude-other")
    signed = _enabled(env, capsys)
    da.check_bound(env["ag"], _row(signed), da.read_install(env["ag"]))


def test_default_facts_keep_the_unbound_live_spec_so_an_existing_link_still_covers_them(env, capsys):
    da.write_install(env["ag"], **env["kw"])
    signed = _enabled(env, capsys)
    assert da.dream_spec("r" * 64)["input"]["source"] == da.LIVE_SOURCE
    da.write_install(env["ag"], **env["kw"], claude_bin="claude", model="claude-opus-5-5")   # the defaults, spelled
    da.check_bound(env["ag"], _row(signed), da.read_install(env["ag"]))
    assert _enabled(env, capsys) == signed                                  # a re-enable registers nothing new


# --------------------------------------------------------------------------------------------- the enable

def test_a_re_enable_after_a_swap_needs_revise_and_moves_the_spec(env, capsys):
    da.write_install(env["ag"], **env["kw"], claude_bin=str(env["tmp"] / "a" / "claude"))
    first = _enabled(env, capsys)
    _hand_edit(env, claude_bin=str(env["tmp"] / "b" / "claude"))
    assert _enable(env) == 1
    assert "new signed link" in capsys.readouterr().err
    second = _enabled(env, capsys, "--revise")
    assert second != first
    with pytest.raises(da.InstallRefused, match="program or model"):
        da.check_bound(env["ag"], _row(first), da.read_install(env["ag"]))
    da.check_bound(env["ag"], _row(second), da.read_install(env["ag"]))


def test_the_signing_card_names_a_bound_program_and_model_and_is_unchanged_for_the_defaults(env, capsys):
    da.write_install(env["ag"], **env["kw"])
    assert _enable(env, "--json") == 0
    plain = json.loads(capsys.readouterr().out)["signing_card"]
    assert "dream program" not in plain
    prog = str(env["tmp"] / "a" / "claude")
    da.write_install(env["ag"], **env["kw"], claude_bin=prog)
    assert _enable(env, "--json", "--revise") == 0
    card = json.loads(capsys.readouterr().out)["signing_card"]
    assert f"The dream program {prog} and the model claude-opus-5-5 are part of what you sign" in card


# --------------------------------------------------------------------------------------------- the bytes

def test_each_bound_fact_moves_the_live_source_and_a_shadow_source_is_never_read_as_live():
    assert da.live_binding(model=None, claude_bin=None) is None
    assert da.live_binding(model="claude-opus-5-5", claude_bin="claude") is None
    b0 = da.live_binding(model=None, claude_bin="/x/claude")
    assert json.loads(b0) == {"claude_bin": "/x/claude", "model": "claude-opus-5-5"}
    assert da.live_binding(model="claude-other", claude_bin="/x/claude") != b0
    assert da.live_binding(model=None, claude_bin="/y/claude") != b0
    src = da.dream_spec("r" * 64, live_binding=b0)["input"]["source"]
    assert src == da.LIVE_SOURCE + da.LIVE_BOUND + b0
    assert not src.startswith(da.SHADOW_SOURCE)
    with pytest.raises(ValueError):
        da.dream_spec("r" * 64, "{}", live_binding=b0)
