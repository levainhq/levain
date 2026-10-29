"""WF-1b (wfetch DESIGN §3 WF-1 item 5; RULINGS Q2 and the wf1a return rulings 1-3): the shipped ``egress-default``
route, the install helper's ``routes-egress`` verb, ``route_hosts`` filled from the base map, and an egress route with
no hosts refused rather than rendered with no rule."""
from __future__ import annotations

import asyncio
import functools
import io
import json
import os
import pwd
from pathlib import Path

import pytest

from levain.broker import policy as P
from levain.contracts import canon
from levain.slot import Refused, install_helper as IH, render as rd, runtime_helper as RH, vendor_routes as VR
from levain.slot import supervisor as SV
from levain.slot.grammar import LEASE_EXEMPT, VERBS, parse_install_argv
from levain.slot.install_helper import Seams, main, operator_record_text
from tests.test_slot_s2b_render import ROUTES, egress

ME = os.getuid()
MY_NAME = pwd.getpwuid(ME).pw_name
ENV = {"SUDO_UID": str(ME)}
F3 = ["api.github.com", "files.pythonhosted.org", "github.com", "pypi.org"]


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


def _read_map(L):
    return canon.parse((L / "routes" / "base.json").read_bytes())


def _req(hosts, **kw):
    return canon.canonical_bytes({"format": IH.ROUTES_EGRESS_REQUEST, "hosts": hosts, **kw})


def _egress(L, hosts=None, raw=None, err=None):
    data = raw if raw is not None else _req(hosts)
    return IH.run_routes_egress(ME, "darwin", read=io.BytesIO(data).read, lock=_Lock(), store_seams=_rs(L),
                                err=err or io.StringIO())


def _base(L):
    IH.run_routes_base(ME, "darwin", lock=_Lock(), store_seams=_rs(L), err=io.StringIO())


# --- the shipped egress-default route (Q2 (a): F3's four hosts) --------------------------------------------------------

def test_routes_base_on_the_empty_map_writes_anthropic_and_egress_default(L):
    got = IH.run_routes_base(ME, "darwin", lock=_Lock(), store_seams=_rs(L), err=io.StringIO())
    assert got == {"verb": "routes-base", "routes": ["anthropic", "claude-subscription", "codex-subscription", "egress-default"], "written": True}
    parsed = P.parse_base_map(_read_map(L))
    assert parsed["egress-default"].type == "egress" and list(parsed["egress-default"].hosts) == F3
    # the route the merge ships is replaced whole by id, as the vendor route is
    doc = _read_map(L)
    for e in doc["routes"]:
        if e["route"] == "egress-default":
            e["hosts"] = ["github.com"]
    (L / "routes" / "base.json").write_bytes(canon.canonical_bytes(doc))
    err = io.StringIO()
    IH.run_routes_base(ME, "darwin", lock=_Lock(), store_seams=_rs(L), err=err)
    assert list(P.parse_base_map(_read_map(L))["egress-default"].hosts) == F3
    assert "route egress-default" in err.getvalue() and "hosts" in err.getvalue()


def test_pick_routes_takes_both_egress_routes(L):
    _base(L)
    _egress(L, ["docs.example.org"])
    routes = _read_map(L)["routes"]
    assert SV.pick_routes(routes, "claude", "brokered") == ("egress-default", "egress-operator", "anthropic")


def test_merge_refuses_a_map_whose_egress_host_sits_on_another_route():
    """The front refuses a session policy whose CONNECT host is on two routes or on a brokered route
    (``front.load_policy``), and every egress route goes to every interactive session (``pick_routes``): a map like
    that would refuse every session at its front, so root never writes one. (A host of egress-default that the
    operator's own CONNECT route admits is no clash: the merge leaves it off egress-default,
    tests/test_slot_wf1b_f1_clash.py.)"""
    for clash in ("api.anthropic.com",):
        doc = {"format": VR.BASE_FORMAT, "routes": [{"route": "mine", "type": "egress", "hosts": [clash]}]}
        with pytest.raises(VR.MergeRefused) as e:
            VR.merge(doc)
        assert e.value.code == "base-map-unmergeable" and clash in e.value.detail


# --- the verb: grammar ---------------------------------------------------------------------------------------------------

def test_routes_egress_parses_bare_is_built_and_lease_exempt():
    assert "routes-egress" in VERBS and "routes-egress" in LEASE_EXEMPT and "routes-egress" in IH.BUILT_VERBS
    assert parse_install_argv(["routes-egress"]).verb == "routes-egress"
    for extra in (["--replace"], ["github.com"]):
        with pytest.raises(Refused) as e:
            parse_install_argv(["routes-egress", *extra])
        assert e.value.name == "extra-argv"
    assert Seams().routes_egress is IH.run_routes_egress


# --- the verb: the act ---------------------------------------------------------------------------------------------------

def test_routes_egress_writes_and_removes_the_operator_route(L):
    _base(L)
    got = _egress(L, ["docs.example.org", "crates.io"])
    assert got == {"verb": "routes-egress", "hosts": ["crates.io", "docs.example.org"], "written": True}
    parsed = P.parse_base_map(_read_map(L))
    assert parsed["egress-operator"].type == "egress"
    assert list(parsed["egress-operator"].hosts) == ["crates.io", "docs.example.org"]
    assert {"anthropic", "egress-default"} <= set(parsed)
    # the same list again writes nothing
    assert _egress(L, ["crates.io", "docs.example.org"])["written"] is False
    # it replaces the list whole
    _egress(L, ["crates.io"])
    assert list(P.parse_base_map(_read_map(L))["egress-operator"].hosts) == ["crates.io"]
    # a later routes-base keeps it (not a shipped id)
    _base(L)
    assert "egress-operator" in P.parse_base_map(_read_map(L))
    # an empty list removes it
    assert _egress(L, []) == {"verb": "routes-egress", "hosts": [], "written": True}
    assert "egress-operator" not in P.parse_base_map(_read_map(L))
    assert _egress(L, [])["written"] is False
    assert oct((L / "routes" / "base.json").stat().st_mode & 0o777) == "0o644"


def test_routes_egress_refuses_by_name_and_leaves_the_map(L):
    _base(L)
    _egress(L, ["crates.io"])
    before = (L / "routes" / "base.json").read_bytes()
    cases = [
        (_req(["127.0.0.1"]), "routes-egress-request:hosts"),
        (_req(["*.github.com"]), "routes-egress-request:hosts"),
        (_req(["GitHub.example"]), "routes-egress-request:hosts"),
        (_req(["localhost"]), "routes-egress-request:hosts"),
        (_req(["0x7f.0x1"]), "routes-egress-request:hosts"),
        (_req(["a.example", "a.example"]), "routes-egress-request:hosts"),
        (_req("a.example"), "routes-egress-request:hosts"),
        (_req([f"h{i}.example" for i in range(IH.ROUTES_EGRESS_MAX_HOSTS + 1)]), "routes-egress-request:hosts"),
        (_req(["github.com"]), "routes-egress-host-on-route"),           # egress-default's
        (_req(["api.anthropic.com"]), "routes-egress-host-on-route"),    # the brokered vendor route's
        (_req(["a.example"], extra=1), "routes-egress-request:keys"),
        (canon.canonical_bytes({"format": "levain-routes-egress-request/2", "hosts": []}),
         "routes-egress-request:keys"),
        (b'{"hosts":[],"format":"levain-routes-egress-request/1"}', "routes-egress-request:json"),
        (b"not json", "routes-egress-request:json"),
        (b" " * (IH.ROUTES_EGRESS_REQUEST_MAX + 1), "routes-egress-request:size"),
    ]
    for raw, name in cases:
        with pytest.raises(Refused) as e:
            _egress(L, raw=raw)
        assert e.value.name == name, (raw[:60], e.value.name)
        assert (L / "routes" / "base.json").read_bytes() == before


def test_routes_egress_needs_a_map_and_the_operator(L):
    with pytest.raises(Refused) as e:
        _egress(L, ["crates.io"])
    assert e.value.name == "base-map-missing" and not (L / "routes" / "base.json").exists()
    (L / "routes" / "base.json").write_bytes(b"[]")
    with pytest.raises(Refused) as e:
        _egress(L, ["crates.io"])
    assert e.value.name == "base-map-malformed" and (L / "routes" / "base.json").read_bytes() == b"[]"
    (L / "policy" / "operator").write_text(operator_record_text("someone-else", ME + 1))
    with pytest.raises(Refused) as e:
        _egress(L, ["crates.io"])
    assert e.value.name == "operator-mismatch"


def test_routes_egress_reads_stdin_before_the_lock_and_checks_the_operator_twice(L):
    _base(L)
    order = []

    def read(n, _src=io.BytesIO(_req(["crates.io"]))):
        order.append("read")
        return _src.read(n)

    class Lock:
        def __call__(self):
            class _Cm:
                def __enter__(self_):
                    order.append("lock")

                def __exit__(self_, *a):
                    order.append("unlock")
            return _Cm()

    IH.run_routes_egress(ME, "darwin", read=read, lock=Lock(), store_seams=_rs(L), err=io.StringIO())
    assert order.index("lock") > max(i for i, x in enumerate(order) if x == "read")


def test_routes_egress_through_main(L):
    _base(L)
    lock = _Lock()
    run = functools.partial(IH.run_routes_egress, store_seams=_rs(L))
    out, err = io.StringIO(), io.StringIO()
    rc = main(["routes-egress"], env=ENV, out=out, err=err,
              seams=Seams(euid=lambda: 0, host=lambda: "darwin", lock=lock, routes_egress=run, gather=_no_gather,
                          stdin=io.BytesIO(_req(["crates.io"])).read))
    assert rc == 0, err.getvalue()
    assert json.loads(out.getvalue()) == {"ok": True, "verb": "routes-egress", "hosts": ["crates.io"],
                                          "written": True}
    assert lock.calls == ["lock", "unlock"]


def test_routes_egress_write_failure_names_the_old_map(L, monkeypatch):
    _base(L)
    before = (L / "routes" / "base.json").read_bytes()

    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(IH.R, "write_durably", boom)
    with pytest.raises(Refused) as e:
        _egress(L, ["crates.io"])
    assert e.value.name == "base-map-write-failed" and "earlier map stands" in e.value.detail
    assert (L / "routes" / "base.json").read_bytes() == before


def test_routes_egress_not_durable_answers_ok_and_warns(L, monkeypatch):
    _base(L)
    real = IH.R.write_durably

    def half(path, data, **kw):
        real(path, data, **kw)
        raise IH.R.NotDurable(5, "Input/output error")
    monkeypatch.setattr(IH.R, "write_durably", half)
    err = io.StringIO()
    got = _egress(L, ["crates.io"], err=err)
    assert got["written"] is True and "not durable" in err.getvalue() and "routes-egress" in err.getvalue()


# --- route_hosts from the base map (wf1a return ruling 1) ----------------------------------------------------------------

class _Store:
    def __init__(self, data):
        self.data = data

    def read(self, parts, name, **kw):
        assert tuple(parts) == ("routes",) and name == "base.json"
        return self.data


def test_base_routes_carry_the_hosts_of_every_route(L):
    _base(L)
    _egress(L, ["crates.io"])
    types, hosts = RH.base_routes(_Store((L / "routes" / "base.json").read_bytes()))
    assert types == {"anthropic": "brokered", "claude-subscription": "subscription_passthrough",
                     "codex-subscription": "subscription_passthrough", "egress-default": "egress", "egress-operator": "egress"}
    assert hosts["egress-default"] == tuple(F3) and hosts["egress-operator"] == ("crates.io",)
    assert hosts["anthropic"] == ("api.anthropic.com",)
    assert RH.base_route_types(_Store((L / "routes" / "base.json").read_bytes())) == types


def test_base_routes_refuse_an_egress_route_with_no_hosts():
    for entry in ({"route": "g", "type": "egress"}, {"route": "g", "type": "egress", "hosts": []},
                  {"route": "g", "type": "egress", "hosts": "github.com"},
                  {"route": "g", "type": "egress", "hosts": ["github.com", 7]}):
        data = canon.canonical_bytes({"format": "levain-routes-base/1", "routes": [entry]})
        with pytest.raises(Refused) as e:
            RH.base_routes(_Store(data))
        assert e.value.name == "base-map-malformed", entry


# --- render: an egress route with no hosts is a refusal (wf1a return ruling 2) -------------------------------------------

def test_egress_route_without_hosts_is_refused():
    for host in ("darwin", "linux"):
        blob, facts = egress(host, "claude", route_hosts={})
        assert "github" in blob.routes and ROUTES["github"] == "egress"
        for fn in (rd.render, rd.render_cc_settings, rd.expected_effective_cc):
            with pytest.raises(Refused) as e:
                fn(blob, facts)
            assert e.value.name == "egress-route-no-hosts" and "github" in e.value.detail


# --- the front's off-list line names the remedy (wf1a return ruling 3) ---------------------------------------------------

def test_off_list_refusal_names_routes_egress():
    from tests.test_broker_front import _connect, _with_front

    async def body(ask, log: Path, front, up_bytes) -> None:
        before = len(log.read_text().splitlines()) if log.exists() else 0
        assert (await ask(_connect("evil.example:443")))[:2] == (b"403", 0)
        rec = json.loads(log.read_text().splitlines()[before])
        assert rec["detail"] == "off-list" and rec["host"] == "evil.example"
        assert rec["remedy"] == "sudo install-helper routes-egress"
        before = len(log.read_text().splitlines())
        assert (await ask(_connect("ok.example:22")))[:2] == (b"403", 0)
        rec = json.loads(log.read_text().splitlines()[before])
        assert rec["detail"] == "port" and "remedy" not in rec

    asyncio.run(_with_front(body))


def test_the_front_refuses_what_connect_host_clashes_names():
    """The claim :func:`levain.slot.vendor_routes.connect_host_clashes` rests on: a session policy whose CONNECT host is
    on two routes, or is also a brokered route's host, is refused by the front's loader."""
    from levain.broker import front as F

    def pol(routes):
        return json.dumps({"format": F.POLICY_FORMAT, "kind": "interactive", "session": "s-0123456789abcdef",
                           "routes": routes}).encode()
    brokered = {k: v for k, v in VR.anthropic_route().items() if k not in ("credential", "prices")}
    brokered["max_call_microdollars"] = 1_000_000          # a session file's brokered route is per-call capped
    F.load_policy(pol([VR.egress_default_route(), brokered]))                        # positive control
    for clash in ({"route": "mine", "type": "egress", "hosts": ["github.com"]},
                  {"route": "mine", "type": "egress", "hosts": ["api.anthropic.com"]}):
        with pytest.raises(P.PolicyError):
            F.load_policy(pol([VR.egress_default_route(), brokered, clash]))
        parsed = P.parse_base_map({"format": VR.BASE_FORMAT,
                                   "routes": [VR.egress_default_route(), VR.anthropic_route(), clash]})
        want = {"github.com": [("github.com", "egress-default", "mine"), ("github.com", "mine", "egress-default")],
                "api.anthropic.com": [("api.anthropic.com", "mine", "anthropic")]}[clash["hosts"][0]]
        assert VR.connect_host_clashes(parsed) == want


def test_the_render_verb_writes_the_allow_rule_from_the_base_map(tmp_path):
    """The production path: the runtime helper's ``render`` reads the base map (``render_facts``), and the session's
    ``root/settings.json`` holds one ``WebFetch(domain:)`` allow per host of its egress route. A map whose egress route
    has no hosts is refused at render, by name."""
    from tests.test_slot_s2f2_runtime_helper import BASE
    from tests.test_slot_s2f3_start import _S, _blob, _prepared, _world

    w, la, table = _world(tmp_path)
    s = _prepared(w)
    w.session = s
    code, line = w.main(["render", s], _blob())
    assert line["ok"], line
    settings = json.loads((_S(w, s) / "root" / "settings.json").read_bytes())
    assert settings["permissions"]["allow"] == ["WebFetch(domain:github.com)"]
    assert [r for r in BASE["routes"] if r["route"] == "github"] == [
        {"route": "github", "type": "egress", "hosts": ["github.com"]}]

    w2, _, _ = _world(tmp_path / "two")
    (w2.L / "routes" / "base.json").write_bytes(canon.canonical_bytes(
        {"format": BASE["format"], "routes": [{k: v for k, v in r.items() if k != "hosts"} for r in BASE["routes"]]}))
    s2 = _prepared(w2)
    w2.session = s2
    code, line = w2.main(["render", s2], _blob())
    assert line["refused"] == "base-map-malformed", line
