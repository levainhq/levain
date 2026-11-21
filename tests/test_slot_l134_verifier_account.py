"""levain#134 (VM sitting 11-18, J06): a fresh install's ``session adopt`` refused ``account-missing: _levain_verify``
after the whole import had run, with no word of why (``project_memory/1118-vm/j06_by_hand.txt``).

Root cause: adopt-verify runs its confined verifier as ``_levain_verify`` (``wall.EXPORT_ACCOUNT["verifier"]``), and
setup never creates it on macOS (``ids.provisioned_singletons`` does not list it there). That is RULED, not an oversight: PREL3_RULINGS 2a-12
(levain 74, 11-08) keeps adopt-verify GATED, failing closed on ``account-missing``, until the macOS pf verifier check
(5g) is built, and provisions the account only then. So the fix is the precondition named, early, on both sides:

* root: ``adopt-verify`` checks the account right after binding the caller, before any lock, root fact or wait, and
  its detail is :data:`wall.VERIFIER_UNPROVISIONED` (the wire name stays ``account-missing``);
The CLI has no pre-check of its own: root allows the report-only ``adopt-nothing-new`` path (it exits before any
adopt-verify call), and a CLI-side lookup would refuse it.

No account is created or touched here: every account lookup is a seam.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import io
import os
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import adopt_view as AVW
from levain.slot import ids
from levain.slot import records as R
from levain.slot import runtime_grammar as RG
from levain.slot import runtime_helper as RH
from levain.slot import wall

S = "s-" + "c2" * 8                       # the 5c-ii-c fixture's session id
DIGEST = "sha256:" + "ab" * 32
_spec = importlib.util.spec_from_file_location("adopt_fx_l134", Path(__file__).with_name("test_slot_s5c2c_adopt_open.py"))
fx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fx)
GIT_ONLY = pytest.mark.skipif(fx.GIT is None, reason="the fixture store needs a git")


def _absent(name: str) -> int:
    """The real ``_account_uid``'s refusal for a name the account database lacks."""
    raise Refused("account-missing", name)


# --- the precondition text -------------------------------------------------------------------------------------------

def test_the_precondition_names_the_account_the_setup_gap_and_the_flag_it_is_not():
    t = wall.VERIFIER_UNPROVISIONED
    assert t.startswith(wall.EXPORT_ACCOUNT["verifier"] + ":")
    assert "setup does not create it" in t and "cannot run on this install yet" in t
    assert "Nothing was taken into your repo" in t and "preview" not in t and "unchanged" not in t
    assert "--verifier-slots" in t and "_levain_v<n>" in t and "_levain_c<n>" in t
    assert "\n" not in t and "\r" not in t       # one line on root's stderr and in the CLI's refusal


def test_the_gate_and_the_provisioning_move_together():
    # Ruling 2a-12: the account is provisioned only when 5g (the live pf check) is built. While the pf default still
    # refuses, setup must not create _levain_verify; whoever builds 5g changes both, and this precondition text.
    assert RH.VerifySeams().pf_live is RH._pf_unbuilt
    # macOS only (levain#223: Linux provisions both, `test_macos_provisions_no_verifier_account`).
    assert "_levain_verify" not in ids.provisioned_singletons("darwin")
    assert "levain-verify" not in ids.provisioned_shared_groups("darwin")


# --- root: verifier_uid --------------------------------------------------------------------------------------------

def _ctx(account_uid):
    seams = RH.Seams(host="darwin", account_uid=account_uid)
    return RH.Ctx(seams, None, os.getuid(), RG.OPERATOR, os.getuid(), {})


def test_verifier_uid_absent_is_account_missing_naming_the_precondition():
    asked = []

    def lookup(name):
        asked.append(name)
        return _absent(name)
    with pytest.raises(Refused) as e:
        RH.verifier_uid(_ctx(lookup))
    assert asked == ["_levain_verify"]
    assert e.value.name == "account-missing" and e.value.detail == wall.VERIFIER_UNPROVISIONED


def test_verifier_uid_present_is_its_uid_and_other_refusals_pass_through():
    assert RH.verifier_uid(_ctx({"_levain_verify": 3797}.__getitem__)) == 3797

    def odd(name):
        raise Refused("account-out-of-range", name)
    with pytest.raises(Refused) as e:
        RH.verifier_uid(_ctx(odd))
    assert (e.value.name, e.value.detail) == ("account-out-of-range", "_levain_verify")


# --- root: the verb ----------------------------------------------------------------------------------------------

def _verb(L, account_uid):
    log: list = []
    seams = RH.Seams(host="darwin", store=L, euid=lambda: 0, trusted=lambda st: True, account_uid=account_uid,
                     at=log.append)
    out, err = io.StringIO(), io.StringIO()
    rc = RH.main(["adopt-verify", S, "--bundle-digest", DIGEST], stdin=b"", env={"SUDO_UID": str(fx.UID)},
                 seams=seams, out=out, err=err)
    return rc, canon.parse(out.getvalue().strip().encode()), err.getvalue(), log


@GIT_ONLY
def test_adopt_verify_without_the_account_refuses_before_any_lock_or_root_fact(tmp_path):
    # Built red: the account was first read inside verify_sealed_run, after the session lock, the root facts, the
    # adopt lock and the git floor, and its detail was the bare name.
    L = fx.make_store(tmp_path, "delta")
    rc, out, err, log = _verb(L, _absent)
    assert rc == RH.EXIT_REFUSED and out == {"ok": False, "refused": "account-missing"}
    assert "setup does not create it" in err and "_levain_verify" in err
    assert "verify:account" not in log and "verify:2" not in log and "verify:3" not in log, log


@GIT_ONLY
def test_adopt_verify_refuses_after_the_caller_is_bound_and_before_any_lock(tmp_path, monkeypatch):
    order: list = []
    real = RH.verify_caller

    def caller(ctx, s):
        order.append("caller")
        return real(ctx, s)

    def lock(*a, **k):
        order.append("lock")
        raise AssertionError("the session lock was taken")

    def lookup(name):
        order.append("account")
        return _absent(name)
    monkeypatch.setattr(RH, "verify_caller", caller)
    monkeypatch.setattr(RH, "session_lock", lock)
    L = fx.make_store(tmp_path, "delta")
    rc, out, err, log = _verb(L, lookup)
    assert out == {"ok": False, "refused": "account-missing"} and order == ["caller", "account"], order


@GIT_ONLY
def test_adopt_verify_binds_the_caller_before_the_account_check(tmp_path):
    # A uid that operates no such session still learns only no-session, whatever the verifier account's state.
    L = fx.make_store(tmp_path, "delta")
    a = R.parse_alloc((L / "alloc" / f"{S}.json").read_bytes())          # the session is another operator's
    (L / "alloc" / f"{S}.json").write_bytes(R.alloc_bytes(dataclasses.replace(a, operator_uid=fx.UID + 1)))
    rc, out, err, log = _verb(L, _absent)
    assert out == {"ok": False, "refused": "no-session"} and "setup does not create it" not in err


@GIT_ONLY
def test_adopt_verify_with_the_account_runs_on_past_the_check(tmp_path):
    L = fx.make_store(tmp_path, "delta")
    rc, out, err, log = _verb(L, lambda name: fx.UID)
    assert "verify:account" in log and "verify:2" in log and out.get("refused") != "account-missing", (out, log)


# --- the CLI: refused before adopt-open's import -------------------------------------------------------------------

BASE = {"oid": "d" * 40, "object_format": "sha1"}


def test_cli_has_no_account_precheck_and_a_report_only_adopt_exits_0_without_the_account(tmp_path, monkeypatch):
    # Root allows the report-only path (adopt-nothing-new exits before any adopt-verify call), so the CLI must not
    # look the account up itself: no pre-check, and no pwd lookup at all on this path.
    import pwd

    assert not hasattr(AVW, "_account_present") and "account_present" not in AVW.Seams.__dataclass_fields__
    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(AssertionError("looked up " + name)))
    calls: list = []
    store = tmp_path / "store"

    def call_helper(seams, *args, err, timeout=None):
        calls.append(args[0])
        if args[0] == "status":
            return {"records": [{"base": dict(BASE)}]}
        assert args[0] == "adopt-open", args
        return {"result": "adopt-nothing-new", "base_oid": BASE["oid"], "object_format": "sha1",
                "repo": str(store / "export" / AVW.EXPORT_CLASS / S), "refs": []}
    monkeypatch.setattr(AVW, "call_helper", call_helper)
    monkeypatch.setattr(AVW, "check_repo", lambda seams, repo, base: Path(os.path.abspath(repo)))
    monkeypatch.setattr(AVW, "_copy_report", lambda *a, **k: {})
    monkeypatch.setattr(AVW.adopt_preview, "report_section", lambda report: ["(report)"])
    tmpd = tmp_path / "tmp"
    tmpd.mkdir()
    seams = AVW.Seams(helper=("FAKE",), store=store, git="git", tmpdir=tmpd,
                      spawn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    out, err = io.StringIO(), io.StringIO()
    rc = AVW.main(["adopt", S, "--repo", str(tmp_path), "--take", "none"], seams=seams, out=out, err=err)
    assert rc == 0, (out.getvalue(), err.getvalue())
    assert calls == ["status", "adopt-open"] and "nothing new since its base" in out.getvalue()
    assert "account-missing" not in err.getvalue()
