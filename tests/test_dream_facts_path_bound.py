"""levain#130 (follow-up of levain#126): a LIVE dream install's ``facts_path`` (the first-run facts file the
executor's exposure check reads) is bound into the spec the link signs, as a shadow install's is. Before this, a
same-user edit of ``facts_path`` to a crafted facts file turned an ``exposure_refusal`` into a pass with no re-sign:
the hook and the fire ran under the link signed while the exposure still refused.

What is bound is the PATH, as for a shadow; the file's contents are not (a same-user rewrite of the named file is
outside this binding, as it is for a shadow install).

Residue no test here holds: a real install and a real signature (a person runs `levain dream enable --revise` on an
install whose facts name a first-run facts file, then signs the new link)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from levain.dream import agent as da
from levain.dream import executor as ex
from levain.dream import ready as rd
from tests.test_dream_live_binding import (_enable, _enabled, _hand_edit, _refusal, _row, _run_advice,  # noqa: F401
                                           env)

MODEL = ex.DEFAULT_CLAUDE_MODEL
FACTS_REFUSED = "the dream's first-run facts file is not what the occurrence's spec binds"


def _refusing_facts(path):
    """A first-run facts file the exposure check refuses (fact 1 not met: a second model charged)."""
    ex.write_first_run_facts(path, model=MODEL, models_charged=[MODEL, "claude-other"],
                             m_usd_micros=ex.one_call_ceiling(MODEL))
    return path


def _passing_facts(path):
    """A crafted first-run facts file the exposure check passes."""
    ex.write_first_run_facts(path, model=MODEL, models_charged=[MODEL], m_usd_micros=ex.one_call_ceiling(MODEL))
    return path


def _refused_at_hook_and_fire(env, row, monkeypatch, match):
    with pytest.raises(da.InstallRefused, match=match):
        da.check_bound(env["ag"], row, da.read_install(env["ag"]))
    with pytest.raises(da.InstallRefused, match=match):
        da.install_on_input(env["ag"])(row)
    monkeypatch.setattr(rd, "_read_occurrences", lambda agent_dir: [row])
    with pytest.raises(da.InstallRefused, match=match):
        da.fire_factory(SimpleNamespace(directory=env["ag"]), row["occurrence_id"], 0)


# ------------------------------------------------------------------------------------------- the reproduced case

def test_a_facts_path_pointed_at_a_crafted_file_after_signing_is_refused_at_the_hook_and_the_fire(env, capsys,
                                                                                                   monkeypatch):
    # reproduced on 1aaaadd58 (#126's tip): the live spec bound only claude_bin and model, so after this edit the
    # exposure check passed and check_bound passed too, under the link signed while the exposure refused
    real = _refusing_facts(env["tmp"] / "real_facts.json")
    da.write_install(env["ag"], **env["kw"], facts_path=real)
    signed = _enabled(env, capsys)
    assert ex.exposure_refusal(MODEL, da.read_install(env["ag"]).facts_path) is not None      # the signed one refuses
    crafted = _passing_facts(env["tmp"] / "crafted_facts.json")
    _hand_edit(env, facts_path=str(crafted))
    assert ex.exposure_refusal(MODEL, da.read_install(env["ag"]).facts_path) is None          # the edit relaxes it
    _refused_at_hook_and_fire(env, _row(signed), monkeypatch, FACTS_REFUSED)


def test_a_facts_path_named_after_an_unbound_spec_was_registered_is_refused(env, capsys, monkeypatch):
    # the order of an install enabled before facts_path was bound and given its facts file afterwards
    da.write_install(env["ag"], **env["kw"])
    signed = _enabled(env, capsys)
    _hand_edit(env, facts_path=str(_passing_facts(env["tmp"] / "crafted_facts.json")))
    _refused_at_hook_and_fire(env, _row(signed), monkeypatch, FACTS_REFUSED)
    why = _refusal(env, _row(signed))
    assert "nor a first-run facts file" in why and "changed after" not in why


def test_a_facts_path_removed_after_signing_is_refused(env, capsys, monkeypatch):
    da.write_install(env["ag"], **env["kw"], facts_path=_passing_facts(env["tmp"] / "f.json"))
    signed = _enabled(env, capsys)
    _hand_edit(env, facts_path=None)
    _refused_at_hook_and_fire(env, _row(signed), monkeypatch, FACTS_REFUSED)


def test_the_refusal_names_both_files_and_its_revise_advice_works(env, capsys):
    a = _refusing_facts(env["tmp"] / "a.json")
    b = _passing_facts(env["tmp"] / "b.json")
    da.write_install(env["ag"], **env["kw"], facts_path=a)
    signed = _enabled(env, capsys)
    _hand_edit(env, facts_path=str(b))
    why = _refusal(env, _row(signed))
    assert f"the first-run facts file {str(a)!r}" in why and f"the first-run facts file {str(b)!r}" in why
    assert "changed after that spec was registered" in why
    assert "--revise" in why and "earlier version" not in why
    _run_advice(env, capsys, why)
    from levain.agents import spec as _spec
    from levain.agents.store import open_agent_store

    now = _spec.get_agent(open_agent_store(env["ag"]), "dream")["spec_hash"]
    assert now != signed
    da.check_bound(env["ag"], _row(now), da.read_install(env["ag"]))


def test_a_program_change_still_leads_with_the_program_words(env, capsys, monkeypatch):
    from tests.test_dream_live_binding import REFUSED

    f = _passing_facts(env["tmp"] / "f.json")
    da.write_install(env["ag"], **env["kw"], facts_path=f, claude_bin=str(env["tmp"] / "a" / "claude"))
    signed = _enabled(env, capsys)
    _hand_edit(env, claude_bin=str(env["tmp"] / "b" / "claude"))
    _refused_at_hook_and_fire(env, _row(signed), monkeypatch, REFUSED)


# --------------------------------------------------------------------------------------------- what still runs

def test_the_facts_path_the_spec_binds_passes(env, capsys):
    f = _passing_facts(env["tmp"] / "f.json")
    da.write_install(env["ag"], **env["kw"], facts_path=f)
    signed = _enabled(env, capsys)
    da.check_bound(env["ag"], _row(signed), da.read_install(env["ag"]))
    assert da.bound_refusal_now(env["ag"], signed) is None


def test_a_plain_re_enable_after_a_facts_path_change_needs_revise(env, capsys):
    da.write_install(env["ag"], **env["kw"], facts_path=_refusing_facts(env["tmp"] / "a.json"))
    _enabled(env, capsys)
    _hand_edit(env, facts_path=str(_passing_facts(env["tmp"] / "b.json")))
    assert _enable(env) == 1
    err = capsys.readouterr().err
    assert "first-run facts file" in err and "new signed link" in err


def test_the_card_names_the_bound_facts_file(env, capsys):
    f = _passing_facts(env["tmp"] / "f.json")
    da.write_install(env["ag"], **env["kw"], facts_path=f)
    assert _enable(env, "--json") == 0
    card = json.loads(capsys.readouterr().out)["signing_card"]
    assert f"It reads its first-run facts from {f}" in card
    assert "the path is part of what you sign" in card


# --------------------------------------------------------------------------------------------- the bytes

def test_the_live_binding_carries_the_facts_path_as_the_shadow_binding_writes_it():
    assert da.live_binding(model=None, claude_bin=None, facts_path=None) is None
    b = da.live_binding(model=None, claude_bin=None, facts_path="~/f.json")
    shadow = json.loads(da.shadow_binding(shadow_flow_root="/r", store_path="/s", coord_dir="/c", cockpit_root="/k",
                                          flow_instructions="/i", zone="UTC", facts_path="~/f.json"))
    assert json.loads(b) == {"claude_bin": ex.DEFAULT_CLAUDE_BIN, "model": MODEL, "facts_path": shadow["facts_path"]}
    assert da.live_binding(model=None, claude_bin=None, facts_path="/g.json") != b
    assert da._well_formed_bound(b)
    # with no facts file the key is not written, so #126's binding keeps its bytes; a null key is not one this build
    # writes, so a spec carrying it is refused as an unknown source
    plain = json.dumps({"claude_bin": "/x/claude", "model": MODEL}, sort_keys=True)
    assert da.live_binding(model=None, claude_bin="/x/claude", facts_path=None) == plain
    assert da._well_formed_bound(plain)
    assert not da._well_formed_bound(json.dumps({"claude_bin": "/x/claude", "model": MODEL, "facts_path": None},
                                                sort_keys=True))
