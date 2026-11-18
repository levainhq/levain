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
REFUSED = "the dream program and model are not what the occurrence's spec binds"


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
    with pytest.raises(da.InstallRefused, match=REFUSED):
        da.check_bound(env["ag"], row, da.read_install(env["ag"]))
    with pytest.raises(da.InstallRefused, match=REFUSED):
        da.install_on_input(env["ag"])(row)
    monkeypatch.setattr(rd, "_read_occurrences", lambda agent_dir: [row])
    with pytest.raises(da.InstallRefused, match=REFUSED):
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
    signed = _enabled(env, capsys)                          # the bytes themselves: test_the_default_live_spec_has_...
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
    with pytest.raises(da.InstallRefused, match=REFUSED):
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
    assert (f"The dream program and the model are part of what you sign: the program by its name, {prog}, and the "
            "model claude-opus-5-5") in card


# --------------------------------------------------------------------------------------------- the bytes

def test_each_bound_fact_moves_the_live_source_and_a_shadow_source_is_never_read_as_live():
    assert da.live_binding(model=None, claude_bin=None) is None
    assert da.live_binding(model="claude-opus-5-5", claude_bin="claude") is None
    b0 = da.live_binding(model=None, claude_bin="/x/claude")
    assert json.loads(b0) == {"claude_bin": "/x/claude", "model": "claude-opus-5-5"}
    assert da.live_binding(model="claude-other", claude_bin="/x/claude") != b0
    assert da.live_binding(model=None, claude_bin="/y/claude") != b0
    src = da.dream_spec("r" * 64, live=b0)["input"]["source"]
    assert src == da.LIVE_SOURCE + da.LIVE_BOUND + b0
    assert not src.startswith(da.SHADOW_SOURCE)
    with pytest.raises(ValueError):
        da.dream_spec("r" * 64, "{}", live=b0)


# ------------------------------------------------------------------------------------------- 1118-27 fix round

#: taken on origin/main a2da68f4e, before the binding existed: sha256 of ``canonical_spec(dream_spec("r" * 64))``
BASE_DEFAULT_SPEC_SHA256 = "13032d89d3b2df007a5f6141d0587c73ca7d7b0e3628728ed9c7e089c0d1fab4"


def test_the_default_live_spec_has_the_bytes_it_had_before_the_binding():
    # L1 MED-1: the earlier assertion compared LIVE_SOURCE with itself; these are the literal text and the bytes of
    # the base's spec, so an edit to either moves this test and not only the constant
    import hashlib

    from levain.agents import spec as _spec

    spec = da.dream_spec("r" * 64)
    assert spec["input"]["source"] == "the install's anneal store: episodes since its last wrap"
    assert hashlib.sha256(_spec.canonical_spec(spec).encode()).hexdigest() == BASE_DEFAULT_SPEC_SHA256


def _refusal(env, row) -> str:
    with pytest.raises(da.InstallRefused) as got:
        da.check_bound(env["ag"], row, da.read_install(env["ag"]))
    return str(got.value)


def test_a_spec_from_before_the_binding_is_refused_without_saying_the_facts_changed(env, capsys):
    # L1 MED-2: the spec predates the binding (or was registered with the defaults); nothing "changed after" it
    da.write_install(env["ag"], **env["kw"])
    signed = _enabled(env, capsys)
    prog = str(env["tmp"] / "a" / "claude")
    da.write_install(env["ag"], **env["kw"], claude_bin=prog)
    why = _refusal(env, _row(signed))
    assert "changed after" not in why
    assert prog in why and "binds no dream program or model" in why
    assert "registered with the defaults, or before" in why


def test_a_spec_that_binds_another_program_is_refused_naming_both(env, capsys):
    a, b = str(env["tmp"] / "a" / "claude"), str(env["tmp"] / "b" / "claude")
    da.write_install(env["ag"], **env["kw"], claude_bin=a)
    signed = _enabled(env, capsys)
    _hand_edit(env, claude_bin=b)
    why = _refusal(env, _row(signed))
    assert f"the program {a!r}" in why and f"the program {b!r}" in why
    assert "changed after that spec was registered" in why


def test_the_refusal_names_the_enable_line_of_this_install(env, capsys):
    # L1 LOW-3: the agent store, cockpit root and coord dir here are not the defaults, so a bare
    # `levain dream enable --revise` would name another install
    da.write_install(env["ag"], **env["kw"], claude_bin=str(env["tmp"] / "a" / "claude"))
    signed = _enabled(env, capsys)
    _hand_edit(env, model="claude-other")
    why = _refusal(env, _row(signed))
    ag = str(env["ag"].resolve())
    assert f"`levain dream enable --revise --agent-store-dir {ag} --cockpit-root " in why


def _show(env, capsys) -> str:
    from levain.cli import main

    capsys.readouterr()
    assert main(["agents", "--store-dir", str(env["ag"]), "show", "dream"]) == 0
    return capsys.readouterr().out


def test_agents_show_dream_says_before_the_night_that_every_fire_would_refuse(env, capsys):
    # L1 MED-3: the dream reads as armed (its link covers the spec) while every fire refuses the facts; reproduced on
    # 970bd1541, where nothing printed it before the night's tick notes
    da.write_install(env["ag"], **env["kw"])
    _enabled(env, capsys)
    assert "dream program" not in _show(env, capsys)
    da.write_install(env["ag"], **env["kw"], claude_bin=str(env["tmp"] / "a" / "claude"))
    out = _show(env, capsys)
    assert "dream install: a fire of this spec refuses with the facts as they stand: " + REFUSED in out
    assert "`levain dream enable --revise --agent-store-dir" in out


def test_the_card_says_the_program_is_bound_by_name_only(env, capsys):
    # L2 MED-1: the link covers the name the fire runs, not the file that name leads to
    prog = str(env["tmp"] / "a" / "claude")
    da.write_install(env["ag"], **env["kw"], claude_bin=prog)
    assert _enable(env, "--json") == 0
    card = json.loads(capsys.readouterr().out)["signing_card"]
    assert f"the program by its name, {prog}, and the model claude-opus-5-5" in card
    assert "bound by its name only" in card


def test_the_card_names_the_binding_that_was_registered_not_a_second_read(env, capsys, monkeypatch):
    # L2 MED-2: the card was built from a second read of the facts after the spec was registered
    prog = str(env["tmp"] / "a" / "claude")
    da.write_install(env["ag"], **env["kw"], claude_bin=prog)

    def unreadable(agent_dir):
        raise da.InstallRefused("the facts cannot be read now")

    monkeypatch.setattr(da, "read_install", unreadable)
    assert _enable(env, "--json") == 0
    card = json.loads(capsys.readouterr().out)["signing_card"]
    assert f"the program by its name, {prog}" in card


@pytest.mark.parametrize("edit", [dict(claude_bin=5), dict(model="")])
def test_enable_refuses_facts_it_cannot_read_the_program_from(env, capsys, edit):
    # L2 MED-2, fail closed: without --store-dir the facts were read as "the defaults" when the program could not be
    # read from them, so the default spec was registered for an install that names something else
    from levain.agents.store import open_agent_store

    da.write_install(env["ag"], **env["kw"])
    _hand_edit(env, **edit)
    assert _enable(env) == 1
    err = capsys.readouterr().err
    assert "nothing was registered" in err
    assert da.stored_time(open_agent_store(env["ag"])) is None


def test_the_default_program_and_model_have_one_source():
    # L1 LOW-2: the defaults the binding treats as "unbound" are the ones the compose runs with
    import inspect

    from levain.dream import executor as ex

    assert da.live_binding(model=ex.DEFAULT_CLAUDE_MODEL, claude_bin=ex.DEFAULT_CLAUDE_BIN) is None
    from pathlib import Path

    inst = da.DreamInstall(Path("/x"), Path("/x"), Path("/x"), ZONE)
    assert (inst.model, inst.claude_bin) == (ex.DEFAULT_CLAUDE_MODEL, ex.DEFAULT_CLAUDE_BIN)
    mind = inspect.signature(ex.ClaudeMind.__init__).parameters
    assert (mind["model"].default, mind["claude_bin"].default) == (ex.DEFAULT_CLAUDE_MODEL, ex.DEFAULT_CLAUDE_BIN)


def test_a_switch_back_to_the_defaults_names_the_new_signed_link(env, capsys):
    # L1 LOW-1: the held spec binds a program, the facts are the defaults again
    da.write_install(env["ag"], **env["kw"], claude_bin=str(env["tmp"] / "a" / "claude"))
    _enabled(env, capsys)
    _hand_edit(env, claude_bin=None)
    assert _enable(env) == 1
    assert "new signed link" in capsys.readouterr().err


@pytest.mark.skipif(__import__("shutil").which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_a_tick_that_cannot_state_the_exposure_still_names_the_binding_refusal(tmp_path, capsys):
    # L1 LOW-4: the tick builds the executor (the exposure check) before the launch runs the input hook (the bound
    # check), so a model with no price row hid the binding refusal behind the exposure one
    from tests.test_agents_tick_launch import Spawns, _boundary, _claim_the_slot
    from tests.test_dream_start import _live_rig

    rig = _live_rig(tmp_path)
    path = rig.store.directory / da.INSTALL_FILE
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["model"] = "claude-with-no-price-row"
    path.write_text(json.dumps(doc), encoding="utf-8")
    spawn = Spawns()
    _claim_the_slot(rig, spawn, _boundary(rig))
    err = capsys.readouterr().err
    assert "cannot state its exposure" in err
    assert "The exposure is judged first; the install facts are refused too: " + REFUSED in err
    assert spawn.calls == []


# ------------------------------------------------------------------------------------------- 1118-27 fix round 2

def _store(env):
    from levain.agents.store import open_agent_store

    return open_agent_store(env["ag"])


def _register_with_source(env, source: str) -> str:
    """A dream spec whose ``input.source`` is ``source``, registered as the next version; its spec_hash."""
    from levain.agents import role as _role, spec as _spec

    store = _store(env)
    spec = da.dream_spec(_role.role_hash(_role.parse_role(da.dream_role())))
    spec["input"]["source"] = source
    return _spec.register_spec(store, _spec.parse_spec(spec), ZONE, revise=True)[1]


@pytest.mark.parametrize("source", ["another input", da.LIVE_SOURCE + " (edited)",
                                    da.LIVE_SOURCE + da.LIVE_BOUND + "not json",
                                    da.LIVE_SOURCE + da.LIVE_BOUND + '{"claude_bin": "claude", "model": "claude-opus-5-5"}'])
def test_a_live_source_this_build_does_not_write_is_refused_under_the_default_install(env, capsys, source):
    # codex MED1, reproduced on dbaccc28e: every source but a parseable bound one read as "binds nothing", which the
    # default install's facts match, so the dream ran under a spec whose source was never one a live install has
    da.write_install(env["ag"], **env["kw"])
    _enabled(env, capsys)
    why = _refusal(env, _row(_register_with_source(env, source)))
    assert "changed after" not in why


def _commands(why: str) -> list[list[str]]:
    import re
    import shlex

    return [shlex.split(c)[1:] for c in re.findall(r"`(levain [^`]*)`", why)]


def _run_advice(env, capsys, why: str) -> None:
    from levain.cli import main

    cmds = _commands(why)
    assert cmds, why
    for argv in cmds:
        capsys.readouterr()
        assert main(argv) == 0, (argv, capsys.readouterr().err)


@pytest.mark.parametrize("back", ["defaults", "a"])
def test_the_advice_after_a_revert_to_an_earlier_version_is_a_command_that_works(env, capsys, back):
    # codex MED2, reproduced on dbaccc28e: the refusal said `levain dream enable --revise`, which register_spec refuses
    # because the spec for these facts is an earlier version of the agent (a revert is never the next version)
    a, b = str(env["tmp"] / "a" / "claude"), str(env["tmp"] / "b" / "claude")
    first = None if back == "defaults" else a
    da.write_install(env["ag"], **env["kw"], claude_bin=first)
    _enabled(env, capsys)
    da.write_install(env["ag"], **env["kw"], claude_bin=b)
    second = _enabled(env, capsys, "--revise")
    if first is None:
        _hand_edit(env, claude_bin=None)
    else:
        _hand_edit(env, claude_bin=first)
    why = _refusal(env, _row(second))
    assert "earlier version" in why
    _run_advice(env, capsys, why)
    from levain.agents import spec as _spec

    now = _spec.get_agent(_store(env), "dream")["spec_hash"]
    da.check_bound(env["ag"], _row(now), da.read_install(env["ag"]))


def test_the_advice_for_new_facts_is_still_revise_and_works(env, capsys):
    da.write_install(env["ag"], **env["kw"], claude_bin=str(env["tmp"] / "a" / "claude"))
    signed = _enabled(env, capsys)
    _hand_edit(env, claude_bin=str(env["tmp"] / "b" / "claude"))
    why = _refusal(env, _row(signed))
    assert "--revise" in why and "earlier version" not in why
    _run_advice(env, capsys, why)


def test_enable_reads_the_facts_once_so_a_rewrite_between_reads_is_never_a_raw_error(env, capsys, monkeypatch):
    # codex MED3 + deepseek MED2, reproduced on dbaccc28e: the binding and the live binding came from two reads of the
    # file; a shadow read on one and a live custom one on the other made both set, and dream_spec's ValueError escaped
    # as "cannot register (ValueError ...)"
    from pathlib import Path

    prog = str(env["tmp"] / "a" / "claude")
    da.write_install(env["ag"], **env["kw"], claude_bin=prog)
    path = env["ag"] / da.INSTALL_FILE
    live_text = path.read_text(encoding="utf-8")
    shadow = dict(json.loads(live_text), shadow_flow_root=str(env["tmp"]), flow_instructions=str(env["tmp"] / "i.md"))
    real = Path.read_text
    reads = []

    def flip(self, *a, **k):
        if self == path:                    # every other read sees a shadow install a concurrent enable wrote
            reads.append(1)
            return json.dumps(shadow) if len(reads) % 2 == 0 else live_text
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", flip)
    code = _enable(env, "--json")
    out, err = capsys.readouterr()
    assert "ValueError" not in err
    assert code == 0, err
    from levain.agents import spec as _spec

    held = _spec.stored_specs(_store(env))[da.NAME]["input"]["source"]
    # the card's install is the one read the spec binds, whichever of the two it was
    assert json.loads(out)["install_kind"] == ("shadow" if held.startswith(da.SHADOW_SOURCE) else "live")


def test_register_dream_turns_both_bindings_into_a_refusal(env):
    with pytest.raises(da.EnableRefused):
        da.register_dream(_store(env), binding="{}", live='{"claude_bin": "x", "model": "y"}')


def test_bound_refusal_now_never_raises(env, capsys, monkeypatch):
    # deepseek MED1, reproduced on dbaccc28e: an OSError from the spec-store read escaped the advisory reader, which
    # the tick calls inside an `except ExposureRefused` handler, so the pass lost its other rows
    da.write_install(env["ag"], **env["kw"])
    signed = _enabled(env, capsys)

    def broken(*a, **k):
        raise OSError("the store is not there")

    monkeypatch.setattr(da, "_spec_source", broken)
    assert da.bound_refusal_now(env["ag"], signed) is None
    monkeypatch.setattr(da, "check_bound", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert da.bound_refusal_now(env["ag"], signed) is None


def _show_json(env, capsys) -> dict:
    from levain.cli import main

    capsys.readouterr()
    assert main(["agents", "--store-dir", str(env["ag"]), "show", "dream", "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_agents_show_json_carries_the_install_refusal(env, capsys):
    # codex LOW4, reproduced on dbaccc28e: --json returned before the refusal was computed
    da.write_install(env["ag"], **env["kw"])
    _enabled(env, capsys)
    assert _show_json(env, capsys)["dream_install_refusal"] is None
    da.write_install(env["ag"], **env["kw"], claude_bin=str(env["tmp"] / "a" / "claude"))
    assert _show_json(env, capsys)["dream_install_refusal"].startswith(REFUSED)


def test_a_zone_refusal_on_a_live_bound_install_carries_no_program_advice(env, capsys):
    # gemini MED, reproduced on dbaccc28e: the program-and-model advice was added to any refusal once a binding existed
    from levain.agents.store import set_default_zone

    da.write_install(env["ag"], **env["kw"], claude_bin=str(env["tmp"] / "a" / "claude"))
    _enabled(env, capsys)
    set_default_zone(_store(env), "Europe/Paris")
    p = argparse.ArgumentParser()
    add_parsers(p.add_subparsers())
    args = p.parse_args(["dream", "enable", "--agent-store-dir", str(env["ag"])])
    assert args.func(args) == 1
    err = capsys.readouterr().err
    assert "rule 4" in err
    assert "dream program and model are bound" not in err


def test_an_old_occurrence_under_facts_the_current_version_binds_names_the_current_version(env, capsys):
    da.write_install(env["ag"], **env["kw"])
    first = _enabled(env, capsys)
    da.write_install(env["ag"], **env["kw"], claude_bin=str(env["tmp"] / "a" / "claude"))
    _enabled(env, capsys, "--revise")
    why = _refusal(env, _row(first))
    assert "current version 2 already binds these facts" in why
    _run_advice(env, capsys, why)


@pytest.mark.parametrize("key", ["model", "claude_bin"])
@pytest.mark.parametrize("value", ["", "  "])
def test_a_blank_program_or_model_is_refused_by_every_reader(env, capsys, key, value):
    # complement LOW: one rule for a blank model or program, at enable, at read_install and at the live binding
    da.write_install(env["ag"], **env["kw"])
    _enabled(env, capsys)
    _hand_edit(env, **{key: value})
    with pytest.raises(da.InstallRefused, match="not a non-empty string"):
        da.read_install(env["ag"])
    with pytest.raises(da.InstallRefused, match="not a non-empty string"):
        da.current_live_binding(env["ag"])
    assert _enable(env) == 1
    with pytest.raises(da.InstallRefused, match="not a non-empty string"):
        da.write_install(env["ag"], **env["kw"], **{key: value})
