"""3c-9 L3 r1 fix round (1025-23/3c9b/RULINGS_R1.md items 1-11): the trust pair's per-file durability and its
re-read, the trust files' mode repair, the merge step's failures, the reverted-field warning, the bounded loops, the
sudo refusal, config init's created line, keys idling on macOS."""
from __future__ import annotations

import io
import os
import pwd
import stat
import subprocess
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused, install_helper as IH, routemaps
from levain.slot import runtime_helper as RH
from levain.slot.install_helper import operator_record_text

ME = os.getuid()
MY_NAME = pwd.getpwuid(ME).pw_name
STORE_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture()
def L(tmp_path):
    L = tmp_path / "L"
    (L / "policy").mkdir(parents=True)
    (L / "routes").mkdir()
    (L / "policy" / "operator").write_text(operator_record_text(MY_NAME, ME))
    return L


class _Lock:
    def __call__(self, host=None):
        class _Cm:
            def __enter__(self):
                pass

            def __exit__(self, *a):
                pass
        return _Cm()


def _rs(L):
    return RH.Seams(host="darwin", store=L, trusted=lambda st: True)


@pytest.fixture(scope="module")
def key(tmp_path_factory):
    d = tmp_path_factory.mktemp("k")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "c", "-f", str(d / "a")], check=True)
    line = (d / "a.pub").read_text().split()
    fp = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(d / "a.pub")], check=True,
                        capture_output=True, text=True).stdout.split()[1]
    return f"{line[0]} {line[1]}", fp


def _req(key, fp):
    return canon.canonical_bytes({"format": IH.ROUTES_TRUST_REQUEST, "store_id": STORE_ID, "fingerprint": fp,
                                  "public_key": key})


def _trust(L, raw, err=None):
    return IH.run_routes_trust(ME, "darwin", read=io.BytesIO(raw).read, lock=_Lock(), store_seams=_rs(L), err=err)


def _not_durable_on(monkeypatch, name):
    """``write_durably`` as written, then NotDurable (the directory fsync failed) for the file called ``name``."""
    real = IH.R.write_durably

    def wd(p, *a, **k):
        real(p, *a, **k)
        if Path(p).name == name:
            raise IH.R.NotDurable(5, f"{p}: renamed, directory fsync failed")
    monkeypatch.setattr(IH.R, "write_durably", wd)


# --- item 1: the trust pair, per file ------------------------------------------------------------------------------------

def test_trust_pub_not_durable_refuses_and_trust_json_is_not_written(L, key, monkeypatch):
    _not_durable_on(monkeypatch, "trust.pub")
    with pytest.raises(Refused) as e:
        _trust(L, _req(*key))
    assert e.value.name == "routes-trust-write-failed"
    assert not (L / "routes" / "trust.json").exists()


def test_trust_json_not_durable_warns_and_succeeds_with_both_files(L, key, monkeypatch):
    _not_durable_on(monkeypatch, "trust.json")
    err = io.StringIO()
    got = _trust(L, _req(*key), err=err)
    assert got["fingerprint"] == key[1]
    assert "trust.json" in err.getvalue() and "not durable" in err.getvalue()
    t = routemaps.read_trust(L / "routes")
    assert (t.public_key, t.fingerprint, t.store_id) == (key[0], key[1], STORE_ID)


def test_success_is_claimed_only_after_the_pair_is_re_read(L, key, monkeypatch):
    # a write that lands nothing: the read-back, not the write call, decides the answer
    monkeypatch.setattr(IH.R, "write_durably", lambda *a, **k: None)
    with pytest.raises(Refused) as e:
        _trust(L, _req(*key))
    assert e.value.name == "routes-trust-write-failed" and "trust.pub" in e.value.detail


# --- item 2: the trust pair's mode --------------------------------------------------------------------------------------

def test_a_pair_with_the_right_bytes_and_the_wrong_mode_is_repaired(L, key):
    _trust(L, _req(*key))
    for name in ("trust.pub", "trust.json"):
        os.chmod(L / "routes" / name, 0o600)
    _trust(L, _req(*key))
    for name in ("trust.pub", "trust.json"):
        st = os.stat(L / "routes" / name)
        assert (st.st_uid, stat.S_IMODE(st.st_mode)) == (os.geteuid(), 0o644)
    assert routemaps.read_trust(L / "routes").fingerprint == key[1]


# --- items 5, 6, 10: the merge -------------------------------------------------------------------------------------------

def _base(L, routes=()):
    (L / "routes" / "base.json").write_bytes(canon.canonical_bytes({"format": "levain-routes-base/2",
                                                                    "routes": list(routes)}))


def test_the_merge_step_never_stops_the_install_on_a_write_failure(tmp_path, monkeypatch):
    (tmp_path / "routes").mkdir()
    (step,) = IH.base_routes_merge_steps("darwin", store=tmp_path)

    def full(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(IH.R, "write_durably", full)
    ok, why = step.call()
    assert not ok and why.startswith("base-map-write-failed") and "routes-base" in why


def test_the_merge_step_on_not_durable_is_ok_and_says_so(tmp_path, monkeypatch):
    (tmp_path / "routes").mkdir()
    (step,) = IH.base_routes_merge_steps("darwin", store=tmp_path)
    _not_durable_on(monkeypatch, "base.json")
    ok, why = step.call()
    assert ok and "anthropic" in why and "not durable" in why


def test_routes_base_names_the_merged_ids_on_not_durable(L, monkeypatch):
    _not_durable_on(monkeypatch, "base.json")
    err = io.StringIO()
    got = IH.run_routes_base(ME, "darwin", lock=_Lock(), store_seams=_rs(L), err=err)
    assert got == {"verb": "routes-base", "routes": ["anthropic", "claude-subscription", "egress-default"],
                   "written": True}
    assert "not durable" in err.getvalue()


def test_an_operator_edit_to_the_shipped_route_is_reverted_with_a_warning(L, tmp_path):
    from levain.slot import vendor_routes as VR
    edited = dict(VR.anthropic_route(), max_session_microdollars=1, credential="mine")
    _base(L, [edited])
    err = io.StringIO()
    IH.run_routes_base(ME, "darwin", lock=_Lock(), store_seams=_rs(L), err=err)
    assert canon.parse((L / "routes" / "base.json").read_bytes())["routes"] == VR.shipped_routes()
    assert "anthropic" in err.getvalue() and "credential, max_session_microdollars" in err.getvalue()
    _base(L, [edited])                                       # setup's / repair's step says the same in its line
    (step,) = IH.base_routes_merge_steps("darwin", store=L)
    ok, why = step.call()
    assert ok and "credential, max_session_microdollars" in why
    err = io.StringIO()                                      # the shipped entry as shipped: no warning
    IH.run_routes_base(ME, "darwin", lock=_Lock(), store_seams=_rs(L), err=err)
    assert err.getvalue() == ""


# --- item 9: the bounded loops --------------------------------------------------------------------------------------------

def test_the_request_is_read_to_eof_across_short_reads(L, key):
    raw = _req(*key)
    pos = [0]

    def read(n):                                             # a raw pipe handing over a few bytes per read
        chunk = raw[pos[0]:pos[0] + min(n, 5)]
        pos[0] += len(chunk)
        return chunk
    got = IH.run_routes_trust(ME, "darwin", read=read, lock=_Lock(), store_seams=_rs(L))
    assert got["fingerprint"] == key[1]


def test_the_request_read_stops_at_the_cap(L):
    calls = []

    def read(n):
        calls.append(n)
        return b"x" * min(n, 1000)
    with pytest.raises(Refused) as e:
        IH.read_trust_request(read)
    assert e.value.name == "routes-trust-request:size"
    assert sum(min(n, 1000) for n in calls) == IH.ROUTES_TRUST_REQUEST_MAX + 1


def test_the_key_reaches_ssh_keygen_whole_across_short_writes(key, monkeypatch):
    real = os.write
    monkeypatch.setattr(os, "write", lambda fd, b: real(fd, bytes(b)[:7]))
    assert IH.key_fingerprint(key[0]) == key[1]


# --- items 3, 4: keys on a missing account ---------------------------------------------------------------------------------

def test_keys_on_macos_idles_on_a_missing_account_then_runs(monkeypatch, capsys):
    from levain.broker import keys as K
    from levain.slot.services import keys as SK
    from levain.slot import units
    import plistlib
    assert plistlib.loads(units.launchd_plist_text(units.by_name("keys")).encode())["KeepAlive"] is True
    looks = iter([frozenset(), frozenset(), frozenset(), frozenset({601})])
    monkeypatch.setattr(K, "_gw_uids", lambda: next(looks))
    monkeypatch.setattr(K, "_account_uid", lambda name: 602)
    ran, slept = [], []
    monkeypatch.setattr(K, "main", lambda argv: ran.append(argv) or 0)
    assert SK.main(["keys"], host="darwin", sleep=slept.append) == 0
    assert slept == [SK.IDLE_RECHECK_S] * 2 and len(ran) == 1      # never an exit launchd would restart
    assert "idle until the accounts exist" in capsys.readouterr().err
    looks = iter([frozenset({601})] * 3)           # the CONTROL account missing idles the same way
    accounts = iter([None, None, 602])
    monkeypatch.setattr(K, "_account_uid", lambda name: next(accounts))
    assert SK.main(["keys"], host="darwin", sleep=slept.append) == 0 and len(slept) == 3 and len(ran) == 2


def test_setup_starts_keys_after_every_account_it_makes(monkeypatch):
    """Item 4, Linux: ``systemctl enable --now`` starts a unit left ``failed`` by an EX_CONFIG exit; setup's comes
    after every account step. (macOS: keys idles and runs once the accounts exist, above.)"""
    from levain.firing import hands
    from levain.slot.grammar import parse_install_argv
    real = hands.shutil.which
    monkeypatch.setattr(hands.shutil, "which", lambda n, path=None: real(n, path=path) or f"/usr/sbin/{n}")
    facts = IH.HostFacts("linux", caller_uid=501, caller_name="alice", operator_uid=501,
                         tool_present=lambda p: True, xcode_ready=lambda: True)
    plan = IH.plan_setup(parse_install_argv(["setup", "--slots", "2", "--verifier-slots", "1", "--operator", "alice"]),
                         facts)
    whys = [s.why for s in plan.steps]
    start = whys.index("enable and start levain-keys.service")
    made = [i for i, w in enumerate(whys) if w.startswith("create the account ") or w.startswith("put ")]
    assert made and max(made) < start
    assert plan.steps[start].argv[-3:] == ("enable", "--now", "levain-keys.service")


def test_repair_starts_keys_after_its_membership_steps(tmp_path):
    """Item 4, repair (macOS host facts): keys is stopped at R5 and started at R12, after R7's memberships."""
    from tests.test_repair_plan import _plan, _pre_change
    whys = [s.why for s in _plan(*_pre_change(tmp_path)).steps]
    stop, start = whys.index("stop com.levain.keys"), whys.index("start com.levain.keys")
    puts = [i for i, w in enumerate(whys) if w.startswith("put ")]
    assert stop < max(puts) < start


def _replenish_plan(host, monkeypatch):
    from levain.firing import hands
    from levain.slot.grammar import parse_install_argv
    from levain.slot.ids import SINGLETONS
    from tests.test_slot_install_plan import _facts, _set_up, _setup
    real = hands.shutil.which
    monkeypatch.setattr(hands.shutil, "which", lambda n, path=None: real(n, path=path) or f"/usr/sbin/{n}")
    setup = IH.plan_setup(_setup(), _facts(host))
    accounts = dict(setup.allocation.ids)
    gone = {"_levain_s0", "_levain_gw0"}
    return IH.plan_replenish(
        parse_install_argv(["replenish"]),
        _set_up(host, used_ids=frozenset(accounts.values()), existing_names=frozenset(accounts)),
        slots=8, verifier_slots=2, live={"egress": range(1, 8), "none": range(2)},
        accounts={k: v for k, v in accounts.items() if k not in gone and k not in SINGLETONS})


def test_linux_replenish_starts_keys_after_its_account_steps(monkeypatch):
    """Round 3: a keys unit left ``failed`` by an EX_CONFIG exit stays down until the gw accounts exist AND something
    starts it; replenish's start comes after every account step, and is the argv setup and repair use."""
    plan = _replenish_plan("linux", monkeypatch)
    whys = [s.why for s in plan.steps]
    start = whys.index("enable and start levain-keys.service")
    made = [i for i, w in enumerate(whys) if w.startswith("create the account ") or w.startswith("put ")]
    assert made and max(made) < start
    assert plan.steps[start].argv[-3:] == ("enable", "--now", "levain-keys.service")
    assert [w for w in whys if "levain-keys" in w].count("enable and start levain-keys.service") == 1


def test_macos_replenish_plan_has_no_keys_start(monkeypatch):
    """macOS keys re-checks its accounts every IDLE_RECHECK_S itself (item 3), so replenish adds no launchctl step."""
    assert not [s for s in _replenish_plan("darwin", monkeypatch).steps if "com.levain.keys" in s.why]
