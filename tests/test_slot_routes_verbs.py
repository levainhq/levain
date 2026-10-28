"""3c-9 session B: the install helper's ``routes-base`` and ``routes-trust`` verbs (3c design §2.3, §4 row 3c-9, Q15 r3)
and setup's / repair's merge step. Real ``ssh-keygen`` keys; a scratch L standing for the store."""
from __future__ import annotations

import functools
import io
import json
import os
import pwd
import stat
import subprocess
from pathlib import Path

import pytest

from levain import authority
from levain.broker import policy as P
from levain.broker.vendor_prices import VENDOR
from levain.contracts import canon
from levain.slot import Refused, accounts, install_helper as IH, layout, routemaps
from levain.slot import runtime_helper as RH, vendor_routes as VR
from levain.slot.grammar import LEASE_EXEMPT, VERBS, parse_install_argv
from levain.slot.install_helper import Seams, main, operator_record_text

ME = os.getuid()
MY_NAME = pwd.getpwuid(ME).pw_name
ENV = {"SUDO_UID": str(ME)}
STORE_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture()
def L(tmp_path):
    L = tmp_path / "L"
    (L / "policy").mkdir(parents=True)
    (L / "routes").mkdir()
    (L / "policy" / "operator").write_text(operator_record_text(MY_NAME, ME))
    return L


class _Lock:
    def __init__(self):
        self.calls = []

    def __call__(self, host=None):
        lk = self

        class _Cm:
            def __enter__(self):
                lk.calls.append("lock")

            def __exit__(self, *a):
                lk.calls.append("unlock")
        return _Cm()


def _rs(L):
    return RH.Seams(host="darwin", store=L, trusted=lambda st: True)


def _no_gather(*a):
    raise AssertionError("reached the install plan's generic path")


def _main(argv, seams):
    out, err = io.StringIO(), io.StringIO()
    rc = main(argv, env=ENV, seams=seams, out=out, err=err)
    assert out.getvalue().count("\n") == 1
    return rc, json.loads(out.getvalue()), err.getvalue()


def _write_map(L, doc):
    (L / "routes" / "base.json").write_bytes(canon.canonical_bytes(doc))


def _read_map(L):
    return canon.parse((L / "routes" / "base.json").read_bytes())


#: a hand-written egress route on no host the shipped routes name (WF-1b: an egress host on another route is refused)
EGRESS = {"route": "crates", "type": "egress", "hosts": ["crates.io"]}


# --- the grammar --------------------------------------------------------------------------------------------------------

def test_both_verbs_parse_bare_are_built_and_lease_exempt():
    for verb in ("routes-base", "routes-trust"):
        assert verb in VERBS and verb in LEASE_EXEMPT and verb in IH.BUILT_VERBS
        assert parse_install_argv([verb]).verb == verb
        with pytest.raises(Refused) as e:
            parse_install_argv([verb, "--replace" if verb == "routes-base" else "--force"])
        assert e.value.name == "extra-argv"
    assert parse_install_argv(["routes-trust", "--replace"]).replace is True     # Q16 (3c-9)


def test_the_seams_defaults_are_the_real_verbs():
    assert Seams().routes_base is IH.run_routes_base and Seams().routes_trust is IH.run_routes_trust


def test_the_merge_format_is_the_layouts():
    assert VR.BASE_FORMAT == layout.BASE_ROUTES_FORMAT
    assert IH.TRUST_KEY_TYPES == authority._LAPTOP_KEY_TYPES


# --- routes-base: the merge ---------------------------------------------------------------------------------------------

def _parsed(L):
    return P.parse_base_map(_read_map(L))


def test_upgrade_of_the_empty_map_ends_with_the_vendor_routes(L):
    # the map an upgraded store holds: layout.base_routes_steps' empty /1 (main before R4) and /2 alike
    for fmt in layout.BASE_ROUTES_FORMATS:
        _write_map(L, {"format": fmt, "routes": []})
        ids, written = IH.merge_base_routes(L / "routes")
        assert (ids, written) == (["anthropic", "claude-subscription", "codex-subscription", "egress-default"], True)
        m = _parsed(L)
        assert set(m) == {"anthropic", "claude-subscription", "codex-subscription", "egress-default"} and m["anthropic"].type == "brokered"
        assert m["claude-subscription"].type == "subscription_passthrough"
        assert set(m["anthropic"].prices) == {k for k in VENDOR if k.startswith("claude-")}
        for model, price in m["anthropic"].prices.items():
            assert price == P.parse_price(model, VENDOR[model]["price"])
        assert _read_map(L)["format"] == "levain-routes-base/2"


def test_an_absent_map_is_merged_as_the_empty_one(L):
    assert IH.merge_base_routes(L / "routes") == (["anthropic", "claude-subscription", "codex-subscription", "egress-default"], True)
    assert set(_parsed(L)) == {"anthropic", "claude-subscription", "codex-subscription", "egress-default"}


def test_the_merge_is_by_route_id_and_keeps_every_other_entry(L):
    stale = dict(VR.anthropic_route(), max_session_microdollars=1)
    _write_map(L, {"format": "levain-routes-base/2", "routes": [EGRESS, stale]})
    IH.merge_base_routes(L / "routes")
    routes = _read_map(L)["routes"]
    assert routes[0] == EGRESS
    assert routes[1:] == VR.shipped_routes() and len(routes) == 5


def test_a_second_merge_writes_nothing(L):
    IH.merge_base_routes(L / "routes")
    before = os.stat(L / "routes" / "base.json").st_ino
    assert IH.merge_base_routes(L / "routes") == (["anthropic", "claude-subscription", "codex-subscription", "egress-default"], False)
    assert os.stat(L / "routes" / "base.json").st_ino == before


@pytest.mark.parametrize("raw,name", [
    (b"{not json", "base-map-malformed"),
    (canon.canonical_bytes({"format": "levain-routes-base/9", "routes": []}), "base-map-malformed"),
    # a /1 brokered route of the operator's own: no price table, so it cannot be carried to /2
    (canon.canonical_bytes({"format": "levain-routes-base/1", "routes": [dict(
        {k: v for k, v in VR.anthropic_route().items() if k != "prices"}, route="mine",
        max_call_microdollars=1000)]}), "base-map-unmergeable"),
])
def test_a_map_that_cannot_merge_is_refused_and_left(L, raw, name):
    (L / "routes" / "base.json").write_bytes(raw)
    with pytest.raises(Refused) as e:
        IH.merge_base_routes(L / "routes")
    assert e.value.name == name
    assert (L / "routes" / "base.json").read_bytes() == raw


def test_base_map_root_writable_only(L):
    """CONTRACT §2.1: ``L/routes/base.json`` root : root 0644 in ``L/routes`` root 0755. The merge writes as the helper's
    euid (root in the helper) at 0644, and puts back the mode of a file whose bytes are already the merged map."""
    for host in ("darwin", "linux"):
        row = next(r for r in layout.base_store_rows(host, "op") if r.path == layout.STORE[host] / "routes")
        assert (row.owner, row.mode) == ("root", 0o755)
        assert layout.base_routes_steps(host)[0].write[2] == 0o644
    IH.merge_base_routes(L / "routes")
    os.chmod(L / "routes" / "base.json", 0o666)
    assert IH.merge_base_routes(L / "routes")[1] is True
    st = os.stat(L / "routes" / "base.json")
    assert (st.st_uid, stat.S_IMODE(st.st_mode)) == (os.geteuid(), 0o644)


def _can_traverse(row, name, groups, want):
    """The permission class ``name`` (with ``groups``) falls in for ``row`` grants ``want`` (an rwx bit set)."""
    if row.owner == name:
        bits = row.mode >> 6
    elif row.group in groups:
        bits = row.mode >> 3
    else:
        bits = row.mode
    return bits & want == want


def test_route_paths_traversable_by_their_readers():
    """CONTRACT §2.1 + 3c §2.3: CONTROL reads base.json, trust.pub, trust.json and writes dispatch/; keys reads base.json
    and dispatch/. Every directory on each path is searchable by its reader, and each root-written file is 0644."""
    readers = {"_levain_control": ("base.json", "trust.pub", "trust.json", "dispatch", "verifiers"),
               "_levain_keys": ("base.json", "dispatch")}
    for host in ("darwin", "linux"):
        rows = {r.path: r for r in layout.base_store_rows(host, "op")}
        L = layout.STORE[host]
        for name, paths in readers.items():
            groups = {name, *accounts.supplementary_groups(name)}
            for leaf in paths:
                for d in (L, L / "routes"):
                    assert _can_traverse(rows[d], name, groups, 0o1), (host, name, d)
                if leaf in ("dispatch", "verifiers"):
                    assert _can_traverse(rows[L / "routes" / leaf], name, groups, 0o5), (host, name, leaf)
    # the files root writes there are other-readable (0644: test_base_map_root_writable_only, and the trust pair in
    # test_routes_trust_pins_the_pair_and_routemaps_reads_it), so neither reader needs a group for them


def test_routes_base_through_main_writes_under_the_store(L):
    lock = _Lock()
    run = functools.partial(IH.run_routes_base, store_seams=_rs(L))
    rc, out, err = _main(["routes-base"], Seams(euid=lambda: 0, host=lambda: "darwin", lock=lock, routes_base=run,
                                                gather=_no_gather))
    assert rc == 0, err
    assert out == {"ok": True, "verb": "routes-base", "routes": ["anthropic", "claude-subscription",
                                                                 "codex-subscription", "egress-default"],
                   "written": True}
    assert lock.calls == ["lock", "unlock"] and set(_parsed(L)) == {"anthropic", "claude-subscription",
                                                                    "codex-subscription", "egress-default"}


def test_routes_base_refuses_a_caller_who_is_not_the_operator(L):
    (L / "policy" / "operator").write_text(operator_record_text("someone-else", ME + 1))
    with pytest.raises(Refused) as e:
        IH.run_routes_base(ME, "darwin", lock=_Lock(), store_seams=_rs(L))
    assert e.value.name == "operator-mismatch" and not (L / "routes" / "base.json").exists()


def test_setup_and_repair_carry_the_merge_after_the_empty_map(tmp_path):
    """The step setup and repair add (``base_routes_merge_steps``) runs the same merge, best-effort, and a map that
    cannot merge is named and left."""
    (tmp_path / "routes").mkdir()
    (step,) = IH.base_routes_merge_steps("darwin", store=tmp_path)
    assert step.allow_fail and step.call is not None
    assert step.call() == (True, "merged the shipped routes anthropic, claude-subscription, codex-subscription, egress-default")
    assert step.call() == (True, "already holds the shipped routes anthropic, claude-subscription, codex-subscription, egress-default")
    (tmp_path / "routes" / "base.json").write_bytes(b"[]")
    ok, why = step.call()
    assert not ok and why.startswith("base-map-malformed")
    import inspect
    src = inspect.getsource(IH)
    assert "*base_routes_steps(host),\n        *base_routes_merge_steps(host)," in src                    # setup
    assert "*base_routes_steps(host),     # R8\n                      *base_routes_merge_steps(host)," in src  # repair


# --- routes-trust (Q15 r3) ----------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("k")
    out = {}
    for name, typ in (("a", "ed25519"), ("b", "ed25519"), ("rsa", "rsa")):
        subprocess.run(["ssh-keygen", "-q", "-t", typ, "-N", "", "-C", "c", "-f", str(d / name)], check=True)
        line = (d / f"{name}.pub").read_text().split()
        fp = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(d / f"{name}.pub")], check=True,
                            capture_output=True, text=True).stdout.split()[1]
        out[name] = (f"{line[0]} {line[1]}", fp)
    return out


def _req(key, fp, **over):
    obj = {"format": IH.ROUTES_TRUST_REQUEST, "store_id": STORE_ID, "fingerprint": fp, "public_key": key}
    obj.update(over)
    return canon.canonical_bytes(obj)


def _trust(L, raw, **kw):
    return IH.run_routes_trust(ME, "darwin", read=io.BytesIO(raw).read, lock=_Lock(), store_seams=_rs(L), **kw)


def test_routes_trust_pins_the_pair_and_routemaps_reads_it(L, keys, monkeypatch):
    key, fp = keys["a"]
    order = []
    real = IH.R.write_durably
    monkeypatch.setattr(IH.R, "write_durably", lambda p, *a, **k: (order.append(Path(p).name), real(p, *a, **k)))
    got = _trust(L, _req(key, fp))
    assert got["store_id"] == STORE_ID and got["fingerprint"] == fp
    assert order == ["trust.pub", "trust.json"]                     # trust.json LAST (Q15 r3, §2.3)
    t = routemaps.read_trust(L / "routes")
    assert (t.public_key, t.fingerprint, t.store_id) == (key, fp, STORE_ID)
    for name in ("trust.pub", "trust.json"):
        assert stat.S_IMODE(os.stat(L / "routes" / name).st_mode) == 0o644
    order.clear()
    _trust(L, _req(key, fp))                                         # again, the same values: nothing written
    assert order == []


def test_a_missing_trust_pub_is_written_again(L, keys):
    key, fp = keys["a"]
    _trust(L, _req(key, fp))
    os.unlink(L / "routes" / "trust.pub")
    _trust(L, _req(key, fp))
    assert routemaps.read_trust(L / "routes").public_key == key


@pytest.mark.parametrize("other", ["store", "key"])
def test_another_store_or_key_is_routes_trust_exists(L, keys, other):
    key, fp = keys["a"]
    _trust(L, _req(key, fp))
    before = {n: (L / "routes" / n).read_bytes() for n in ("trust.pub", "trust.json")}
    raw = _req(key, fp, store_id="f" * 32) if other == "store" else _req(*keys["b"])
    with pytest.raises(Refused) as e:
        _trust(L, raw)
    assert e.value.name == "routes-trust-exists"
    assert {n: (L / "routes" / n).read_bytes() for n in before} == before


def test_a_fingerprint_the_key_does_not_have_is_refused_before_any_write(L, keys):
    key, _ = keys["a"]
    with pytest.raises(Refused) as e:
        _trust(L, _req(key, keys["b"][1]))
    assert e.value.name == "routes-trust-fingerprint" and not (L / "routes" / "trust.pub").exists()


@pytest.mark.parametrize("mutate,name", [
    (lambda k, f: json.dumps({"format": IH.ROUTES_TRUST_REQUEST, "store_id": STORE_ID, "fingerprint": f,
                              "public_key": k}, indent=1).encode(), "routes-trust-request:json"),
    (lambda k, f: _req(k, f, extra=1), "routes-trust-request:keys"),
    (lambda k, f: _req(k, f, format="levain-routes-trust-request/2"), "routes-trust-request:keys"),
    (lambda k, f: _req(k, f, store_id=STORE_ID.upper()), "routes-trust-request:store-id"),
    (lambda k, f: _req(k, f, fingerprint="SHA256:short"), "routes-trust-request:fingerprint"),
    (lambda k, f: _req(k + " comment", f), "routes-trust-request:public-key"),
    (lambda k, f: _req('command="x" ' + k, f), "routes-trust-request:public-key"),
    (lambda k, f: _req(k + "\n" + k, f), "routes-trust-request:public-key"),
    (lambda k, f: b"x" * (IH.ROUTES_TRUST_REQUEST_MAX + 1), "routes-trust-request:size"),
])
def test_a_bad_request_is_refused_by_name(L, keys, mutate, name):
    with pytest.raises(Refused) as e:
        _trust(L, mutate(*keys["a"]))
    assert e.value.name == name and not (L / "routes" / "trust.pub").exists()


def test_a_key_type_outside_the_laptop_table_is_refused(L, keys):
    with pytest.raises(Refused) as e:
        _trust(L, _req(*keys["rsa"]))
    assert e.value.name == "routes-trust-request:public-key"


def test_routes_trust_refuses_a_caller_who_is_not_the_operator_before_stdin(L, keys):
    (L / "policy" / "operator").write_text(operator_record_text("someone-else", ME + 1))

    def read(n):
        raise AssertionError("stdin read before the operator check")
    with pytest.raises(Refused) as e:
        IH.run_routes_trust(ME, "darwin", read=read, lock=_Lock(), store_seams=_rs(L))
    assert e.value.name == "operator-mismatch"


def test_routes_trust_through_main(L, keys):
    key, fp = keys["a"]
    raw = _req(key, fp)
    run = functools.partial(IH.run_routes_trust, store_seams=_rs(L))
    rc, out, err = _main(["routes-trust"], Seams(euid=lambda: 0, host=lambda: "darwin", lock=_Lock(),
                                                 stdin=io.BytesIO(raw).read, routes_trust=run, gather=_no_gather))
    assert rc == 0, err
    assert out["verb"] == "routes-trust" and out["fingerprint"] == fp
    rc, out, err = _main(["routes-trust"], Seams(euid=lambda: 0, host=lambda: "darwin", lock=_Lock(),
                                                 stdin=io.BytesIO(b"{}").read, routes_trust=run, gather=_no_gather))
    assert (rc, out) == (1, {"ok": False, "refused": "routes-trust-request:keys"})


def test_ssh_keygen_that_cannot_run_is_the_verifier_refusal(keys):
    with pytest.raises(Refused) as e:
        IH.key_fingerprint(keys["a"][0], ssh_keygen="/nonexistent/ssh-keygen")
    assert e.value.name == "routes-trust-verifier"
    assert IH.key_fingerprint(keys["a"][0]) == keys["a"][1]
