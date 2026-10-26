"""V13, the firewall self-probe (DESIGN_V13_REV2 §1-§5 + head rulings R1-R8, O1-O4, R3's N1-N10 and codex MEDs).

⛔ No Seatbelt profile, pf rule, netns or uid drop runs here (the 10-07 panic; ``LEVAIN_LIVE_SEATBELT`` stays off):
profiles are asserted as rendered BYTES, launches as argv and spec JSON, and connects through a fake socket. What a
real wall does to the probe is the VM's (M- and L-rows).
"""
from __future__ import annotations

import ast
import errno
import selectors
import socket
from pathlib import Path

import pytest

from levain.slot import Refused
from levain.slot import probe_child as PC
from levain.slot import runtime_grammar as rg
from levain.slot.runtime_grammar import DISPATCH, OPERATOR, parse_runtime_argv, parse_stdin

REPO = Path(__file__).resolve().parents[1]
S = "s-0123456789abcdef"
SLOTS = {"egress": 3, "none": 1}


def _refused(fn, *a, **kw) -> str:
    with pytest.raises(Refused) as e:
        fn(*a, **kw)
    return e.value.name


# --- T1 the verb -----------------------------------------------------------------------------------------------------

def test_t1_probe_verb_columns():
    assert "probe" in rg.VERBS and "probe" in rg.LEASE_VERBS and "probe" in rg.DISPATCH_FORBIDDEN
    assert "probe" not in rg.STDIN_VERBS
    req = parse_runtime_argv(["probe", S], configured_slots=SLOTS, caller=OPERATOR)
    assert (req.verb, req.session, req.needs_lease) == ("probe", S, True)
    assert _refused(parse_runtime_argv, ["probe"], configured_slots=SLOTS, caller=OPERATOR) == "missing-argv"
    assert _refused(parse_runtime_argv, ["probe", S, S], configured_slots=SLOTS, caller=OPERATOR) == "extra-argv"
    assert _refused(parse_runtime_argv, ["probe", "../x"], configured_slots=SLOTS, caller=OPERATOR) == "bad-token"
    assert _refused(parse_runtime_argv, ["probe", S], configured_slots=SLOTS,
                    caller=DISPATCH) == "verb-not-allowed-for-caller"
    assert parse_stdin("probe", b"") is None
    assert _refused(parse_stdin, "probe", b"{}") == "unexpected-stdin"


# --- T11 the connector (fake sockets) ----------------------------------------------------------------------------------

class FakeSock:
    """A socket whose connect answers are scripted: ``now`` is connect_ex's own code, ``later`` (when ``now`` is
    EINPROGRESS) the SO_ERROR once writable, or ``None`` = never writable (the bound passes)."""

    def __init__(self, fam, typ, script):
        self.fam, self.typ, self.script = fam, typ, script
        self.ip = None
        self.closed = False
        self.blocking = True

    def setblocking(self, flag):
        self.blocking = flag

    def connect_ex(self, addr):
        self.ip = addr[0]
        assert addr[1] == PC.PORT and not self.blocking
        return self.script[self.ip][0]

    def getsockopt(self, level, opt):
        assert (level, opt) == (socket.SOL_SOCKET, socket.SO_ERROR)
        return self.script[self.ip][1]

    def close(self):
        self.closed = True


class FakeSel:
    def __init__(self):
        self.reg = {}

    def register(self, s, ev, data):
        assert ev == selectors.EVENT_WRITE
        self.reg[id(s)] = (s, data)

    def unregister(self, s):
        del self.reg[id(s)]

    def select(self, timeout):
        ready = [(selectors.SelectorKey(s, 0, selectors.EVENT_WRITE, data), selectors.EVENT_WRITE)
                 for s, data in self.reg.values() if s.script[s.ip][1] is not None]
        if not ready:
            self.clock.t += timeout         # nothing will ever be writable: the bound passes
        return ready

    def close(self):
        pass


class T:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _connect(script, order=None, *, fail_make=()):
    made = []
    clock = T()
    sel = FakeSel()
    sel.clock = clock

    def make(fam, typ):
        if made and len(made) in fail_make:
            raise OSError(errno.EMFILE, "too many")
        # every socket exists before any connect starts (N2): no connect has been attempted yet
        assert all(s.ip is None for s in made)
        s = FakeSock(fam, typ, script)
        made.append(s)
        return s
    out = PC.connect_all(order or list(script), make_socket=make, monotonic=clock, selector=lambda: sel)
    assert all(s.closed for s in made)
    return out, made


def test_t11_connect_outcomes():
    script = {"192.0.2.1": (0, None), "192.0.2.2": (errno.EINPROGRESS, 0),
              "192.0.2.3": (errno.ECONNREFUSED, None), "2001:db8::1": (errno.EINPROGRESS, errno.EHOSTUNREACH),
              "192.0.2.5": (errno.EINPROGRESS, None), "192.0.2.6": (errno.EINPROGRESS, errno.ENOBUFS),
              "192.0.2.7": (errno.EADDRNOTAVAIL, None)}
    out, made = _connect(script)
    assert out == {"192.0.2.1": ("c", ""), "192.0.2.2": ("c", ""), "192.0.2.3": ("b", "ECONNREFUSED"),
                   "2001:db8::1": ("b", "EHOSTUNREACH"), "192.0.2.5": ("b", "timeout"),
                   "192.0.2.6": ("u", "ENOBUFS"), "192.0.2.7": ("u", "EADDRNOTAVAIL")}
    assert [s.fam for s in made].count(socket.AF_INET6) == 1


def test_t11_writable_past_the_deadline_is_connected_not_timeout():
    """L3 r1 X1 (codex HIGH): the handshake finished while the child was descheduled past the bound, so the loop
    never selects; a final non-blocking sweep must still read the socket as connected, never as ``b … timeout``."""
    script = {"192.0.2.1": (errno.EINPROGRESS, 0), "192.0.2.2": (errno.EINPROGRESS, errno.ECONNREFUSED),
              "192.0.2.3": (errno.EINPROGRESS, None)}
    clock = T()
    sel = FakeSel()
    sel.clock = clock
    timeouts = []
    real = sel.select

    def select(timeout):
        timeouts.append(timeout)
        return real(timeout)
    sel.select = select
    made = []

    def make(fam, typ):
        s = FakeSock(fam, typ, script)
        made.append(s)
        return s

    reads = []

    def monotonic():
        reads.append(clock.t)
        if len(reads) == 2:                        # after the deadline was fixed, before the first select:
            clock.t += PC.CONNECT_S + 60          # descheduled past it
        return clock()
    out = PC.connect_all(list(script), make_socket=make, monotonic=monotonic, selector=lambda: sel)
    assert out == {"192.0.2.1": ("c", ""), "192.0.2.2": ("b", "ECONNREFUSED"), "192.0.2.3": ("b", "timeout")}
    assert timeouts == [0]                         # the one sweep, non-blocking


def test_t11_blocked_errnos_are_n2s_six():
    assert PC.BLOCKED_ERRNOS == {errno.ECONNREFUSED, errno.ETIMEDOUT, errno.EHOSTUNREACH, errno.ENETUNREACH,
                                 errno.EACCES, errno.EPERM}
    for code in (errno.EMFILE, errno.ENOBUFS, errno.EADDRNOTAVAIL, errno.EINVAL, errno.ENOTSOCK):
        assert PC.classify(code)[0] == "u"


def test_t11_socket_creation_failure_is_unknown_never_blocked():
    script = {"192.0.2.1": (errno.ECONNREFUSED, None), "192.0.2.2": (errno.ECONNREFUSED, None)}
    out, _ = _connect(script, fail_make=(1,))
    assert out["192.0.2.1"] == ("b", "ECONNREFUSED") and out["192.0.2.2"] == ("u", "EMFILE")


def test_t11_child_main_alarm_first_then_lines_then_end():
    calls = []
    written = bytearray()
    outcomes = {"192.0.2.1": ("b", "timeout"), "2001:db8::1": ("c", ""), "192.0.2.3": ("u", "EINVAL")}

    def connect(addrs):
        calls.append(("connect", tuple(addrs)))
        return outcomes

    def write(fd, data):
        assert fd == 4
        written.extend(bytes(data[:5]))     # short writes: the loop must finish the line
        return min(5, len(data))

    rc = PC.main(["--result-fd", "4", "192.0.2.1", "2001:db8::1", "192.0.2.3"],
                 alarm=lambda s: calls.append(("alarm", s)), write=write,
                 close=lambda fd: calls.append(("close", fd)), connect=connect)
    assert rc == 0
    assert calls[0] == ("alarm", PC.PROBE_SELF_S) and calls[-1] == ("close", 4)
    assert bytes(written) == b"b 192.0.2.1 timeout\nc 2001:db8::1\nu 192.0.2.3 EINVAL\nend\n"


@pytest.mark.parametrize("n", [0, -1])
def test_t11_child_write_of_nothing_is_a_nonzero_exit_not_a_loop(n):
    """L3 r1 X6: a write that moves no byte ends the child with a nonzero code, never a spin until its alarm."""
    calls = []

    def write(fd, data):
        calls.append(len(data))
        if len(calls) > 3:
            raise AssertionError("the write loop spun")
        return n

    rc = PC.main(["--result-fd", "4", "192.0.2.1"], alarm=lambda s: None, write=write, close=lambda fd: None,
                 connect=lambda a: {"192.0.2.1": ("b", "timeout")})
    assert rc not in (0, 2) and len(calls) == 1


@pytest.mark.parametrize("argv", [[], ["--result-fd", "4"], ["--result-fd", "2", "192.0.2.1"],
                                  ["--fd", "4", "192.0.2.1"], ["--result-fd", "4", "192.0.2.1", "192.0.2.1"],
                                  ["--result-fd", "4", "api.anthropic.com"],
                                  ["--result-fd", "4"] + [f"192.0.2.{i}" for i in range(PC.MAX_ADDRS + 1)]])
def test_t11_child_argv_refused_writes_nothing(argv):
    wrote = []
    assert PC.main(argv, alarm=lambda s: None, write=lambda fd, d: wrote.append(d) or len(d),
                   close=lambda fd: None, connect=lambda a: {}) == 2
    assert wrote == []


def test_t11_worst_result_fits_the_cap():
    ips = [f"2001:db8:ffff:ffff:ffff:ffff:ffff:{i:04x}" for i in range(PC.MAX_ADDRS)]
    text = PC.result_text(ips, {ip: ("u", "EADDRNOTAVAIL") for ip in ips})
    assert len(text) <= PC.RESULT_MAX


# --- N3 the child imports the standard library only ----------------------------------------------------------------

def _imports(path: Path) -> set:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


def test_n3_child_imports_stdlib_only():
    import sys
    names = _imports(REPO / "levain" / "slot" / "probe_child.py")
    assert names, "no imports read"
    for n in names:
        assert n == "__future__" or n.split(".")[0] in sys.stdlib_module_names, n


# --- T8 the probe profile (R1): the session profile minus exactly its outbound deny ----------------------------------

def _lines(data: bytes) -> list:
    return data.decode("utf-8").split("\n")


@pytest.mark.parametrize("kw", [{}, {"allow_own_loopback": True}, {"hooks": frozenset()},
                                {"vendor": "codex", "auth": "passthrough"}])
def test_t8_probe_profile_is_the_session_profile_minus_the_outbound_deny(kw):
    from levain.slot import render as rd
    from tests.test_slot_s2b_render import egress

    blob, facts = egress(**kw)
    session, probe = rd.render_profile(blob, facts), rd.render_profile(blob, facts, probe=True)
    want = _lines(session)
    assert want.count("(deny network-outbound)") == 1
    want.remove("(deny network-outbound)")
    assert _lines(probe) == want
    rd.assert_no_syscall(probe.decode("utf-8"))
    from levain.slot.wall import check_profile_text
    check_profile_text(probe.decode("utf-8"))


def test_t8_probe_sb_rendered_beside_profile_sb_same_owner_and_mode():
    from levain.slot import render as rd
    from tests.test_slot_s2b_render import egress, verifier

    blob, facts = egress()
    assert facts.slot.slot_class == "egress"
    files = {f.path: f for f in rd.render(blob, facts).files}
    prof, probe = files["root/profile.sb"], files["root/probe.sb"]
    assert (probe.uid, probe.gid, probe.mode) == (prof.uid, prof.gid, prof.mode)
    assert probe.data == rd.render_profile(blob, facts, probe=True)
    blob, facts = egress(host="linux")
    assert not any(f.path.endswith(".sb") for f in rd.render(blob, facts).files)     # darwin only
    blob, facts = verifier()
    assert facts.slot.slot_class == "none" and facts.host == "darwin"      # X7: the none-class case
    paths = {f.path for f in rd.render(blob, facts).files}
    assert "root/profile.sb" in paths and "root/probe.sb" not in paths    # no probe runs in a none-class slot


# --- T7 the wall target (R1, R8, codex MED "a probe-labelled spec can run something else") -----------------------------

ADDRS = ("192.0.2.10", "2001:db8::10")


def _wspec(host, target="probe", **kw):
    from tests.test_slot_s2d_wall import spec

    if target == "probe":
        kw.setdefault("probe_addrs", ADDRS)
    return spec(host, target, **kw)


def _mutated(sp, **fields):
    """A spec's canonical JSON with ``fields`` replaced: what a root-side regression could hand the stage."""
    from levain.contracts import canon

    obj = canon.parse(sp.to_json())
    obj.update(fields)
    return canon.canonical_bytes(obj)


@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_t7_probe_spec_shape(host):
    from levain.slot import wall
    from levain.slot.root_tree import PY_FLAGS, ROOT_PYTHON, boot_path_code
    from tests.test_slot_s2d_wall import IDS, S as SDIR

    sp = _wspec(host)
    boot = (f"{boot_path_code(host)}; import runpy; m = sys.argv.pop(1); "
            "runpy.run_module(m, run_name='__main__', alter_sys=True)")
    assert sp.argv == (ROOT_PYTHON[host], PY_FLAGS, "-c", boot, "levain.slot.probe_child",
                       "--result-fd", "4") + ADDRS
    assert (sp.target, sp.uid, sp.cwd, sp.keep_fds, sp.env_file, sp.extra_env, sp.exec_fd, sp.resolve) == \
        ("probe", IDS[host]["_levain_s0"], f"{SDIR[host]}/tmp", (4,), None, (), None, False)
    assert sp.profile == (f"{SDIR[host]}/root/probe.sb" if host == "darwin" else None)
    assert wall.parse_spec(sp.to_json()) == sp                 # the stage takes exactly what the plan built
    assert wall.PROBE_RESULT_FD == wall.EXEC_FD == wall.READY_FD


def test_t7_darwin_probe_forced_onto_probe_sb_and_launched_through_asuser():
    from levain.slot import wall
    from tests.test_slot_s2d_wall import S as SDIR

    assert _refused(_wspec, "darwin", profile=f"{SDIR['darwin']}/root/profile.sb") == "spec-malformed"
    assert _wspec("darwin", profile=f"{SDIR['darwin']}/root/probe.sb").profile == f"{SDIR['darwin']}/root/probe.sb"
    sp = _wspec("darwin")
    argv = wall.macos_launch_argv(sp)
    assert argv[:3] == (wall.LAUNCHCTL, "asuser", str(sp.uid)) and argv[3:] == wall.stage_argv("darwin")


def test_t7_probe_sb_never_reaches_another_target():
    from levain.slot import wall
    from tests.test_slot_s2d_wall import S as SDIR

    probe_sb = f"{SDIR['darwin']}/root/probe.sb"
    for target in ("vendor", "clone", "clone-check", "forwarder"):
        if target == "forwarder":
            continue                                         # Linux only: no profile path at all
        kw = {"argv": wall.ls_files_argv()} if target == "clone-check" else {}
        assert _refused(_wspec, "darwin", target, profile=probe_sb, **kw) == "spec-malformed", target
    vendor = _wspec("darwin", "vendor")
    assert _refused(wall.parse_spec, _mutated(vendor, profile=probe_sb)) == "spec-malformed"


def test_t7_stage_refuses_a_probe_labelled_spec_running_anything_else():
    from levain.slot import wall
    from tests.test_slot_s2d_wall import S as SDIR

    for host in ("darwin", "linux"):
        sp = _wspec(host)
        argv = list(sp.argv)
        bad_module = argv[:4] + ["levain.slot.forwarder"] + argv[5:]
        bad_exe = ["/bin/sh"] + argv[1:]
        other = {"darwin": "linux", "linux": "darwin"}[host]
        for fields in ({"argv": bad_module}, {"argv": bad_exe}, {"argv": argv + ["127.0.0.1"]},
                       {"argv": argv[:7]}, {"argv": argv + ["192.0.2.10"]}, {"argv": argv[:7] + ["fe80::1"]},
                       {"argv": argv[:7] + ["2001:DB8::10"]},
                       {"argv": list(wall.module_argv(other, wall.PROBE_MODULE, "--result-fd", "4", *ADDRS))},
                       {"keep_fds": []}, {"cwd": f"{SDIR[host]}/repo"}, {"env_file": f"{SDIR[host]}/root/env"},
                       {"nproc": None}, {"profile": f"{SDIR[host]}/root/profile.sb"},
                       {"profile": None if host == "darwin" else f"{SDIR[host]}/root/probe.sb"},
                       {"profile": "/private/var/levain/live/s-fedcba9876543210/root/probe.sb"}):
            assert _refused(wall.parse_spec, _mutated(sp, **fields)) == "spec-malformed", (host, fields)


def test_t7_probe_refusals_at_build():
    from levain.slot import wall
    from levain.slot.ids import SlotId

    assert _refused(_wspec, "darwin", argv=["/bin/sh"]) == "spec-malformed"
    assert _refused(_wspec, "linux", keep_fds=(4,)) == "spec-malformed"
    assert _refused(_wspec, "darwin", probe_addrs=()) == "spec-malformed"
    assert _refused(_wspec, "darwin", probe_addrs=("127.0.0.1",)) == "spec-malformed"
    assert _refused(_wspec, "darwin", probe_addrs=("api.anthropic.com",)) == "spec-malformed"
    assert _refused(_wspec, "darwin", "vendor", probe_addrs=ADDRS) == "spec-malformed"
    assert _refused(_wspec, "darwin", slot=SlotId("none", 0)) == "bad-token"     # a none-class slot has no probe


def test_t7_linux_probe_argv_is_the_forwarders_path():
    from levain.slot import wall
    from levain.slot.bwrap import LEVAIN_BWRAP
    from tests.test_slot_s2d_wall import E0, LINKS, SESSION

    sp = _wspec("linux")
    ll = wall.linux_launch_argv(sp, slot=E0, session=SESSION, kind="interactive",
                                netns_names=lambda: {"levain-e0"}, host_links=LINKS)
    fwd = wall.linux_launch_argv(_wspec("linux", "forwarder"), slot=E0, session=SESSION, kind="interactive",
                                 netns_names=lambda: {"levain-e0"}, host_links=LINKS)
    assert ll.argv[:4] == (wall.NSENTER, "--net=/run/netns/levain-e0", "--", str(LEVAIN_BWRAP))
    assert ll.argv == fwd.argv and ll.cgroup == fwd.cgroup == wall.cgroup_dir(E0, SESSION)
    assert _refused(wall.linux_launch_argv, sp, slot=E0, session=SESSION, kind="interactive",
                    netns_names=lambda: set(), host_links=LINKS) == "netns-missing"


def test_n8_probe_address():
    assert PC.probe_address("192.0.2.1") == "192.0.2.1"
    assert PC.probe_address("2001:DB8::1") == "2001:db8::1"
    for bad in ("127.0.0.1", "0.0.0.0", "::", "::1", "169.254.1.1", "fe80::1", "fe80::1%en0", "224.0.0.1",
                "ff02::1", "::ffff:127.0.0.1", "api.anthropic.com", "", "1.2.3"):
        assert PC.probe_address(bad) is None, bad


# --- T2 refusal names (R6) -------------------------------------------------------------------------------------------

NAMES = ("firewall-open", "firewall-probe-inconclusive:dns", "firewall-probe-inconclusive:no-control-connect",
         "firewall-probe-inconclusive:too-many", "firewall-probe-unknown:child", "firewall-probe-unknown:unreaped",
         "not-probeable:RENDERING", "not-probeable:BRAKED", "not-probeable:CREDENTIAL_PARKED".replace("_", "-"),
         "not-probeable:unrecorded", "not-probeable:class", "not-probeable:kind", "not-probeable:route",
         "not-probeable:alloc-changed", "slot-not-ours", "session-busy")


def test_t2_every_name_parses_and_no_address_ever_does():
    from levain.slot import records as R

    for n in NAMES:
        assert rg._NAME_RE.fullmatch(n) and len(n) <= 64, n
    for state in R.STATES:     # not-probeable:<state> carries the state's own name, "_" spelled "-"
        assert rg._NAME_RE.fullmatch(f"not-probeable:{state.replace('_', '-')}"), state
    # the reason for the spelling: the raw CREDENTIAL_PARKED would leave stdout as `internal`
    assert '"internal"' in rg.refusal_line(Refused("not-probeable:CREDENTIAL_PARKED"))
    assert rg.refusal_line(Refused("firewall-open:2001:db8::1")) == '{"ok":false,"refused":"internal"}\n'
    assert rg.refusal_line(Refused("firewall-open", "api.anthropic.com 2001:db8::1")) == \
        '{"ok":false,"refused":"firewall-open"}\n'


# --- T3 hosts (R4 + O3) ------------------------------------------------------------------------------------------------

def test_t3_probe_hosts_ordered_tuples_within_vendor_hosts_and_shipped_upstreams():
    from levain.slot import probe as PB
    from levain.slot import supervisor as SV
    from levain.slot import vendor_routes as VR

    upstreams = {r["upstream"]["host"] for r in VR.vendor_routes() if "upstream" in r}
    assert upstreams, "no shipped upstream read"
    for vendor, hosts in PB.PROBE_HOSTS.items():
        assert type(hosts) is tuple and len(set(hosts)) == len(hosts)
        assert set(hosts) <= SV.VENDOR_HOSTS[vendor] | upstreams, vendor
    assert set(PB.PROBE_HOSTS) == set(SV.VENDOR_HOSTS)
    for r in VR.vendor_routes():          # every shipped route's upstream IS probed for each vendor it serves
        for vendor in [v for v in SV.VENDOR_HOSTS if SV._hosts(r) & SV.VENDOR_HOSTS[v]]:
            assert r["upstream"]["host"] in PB.PROBE_HOSTS[vendor], (r["route"], vendor)


def _all_imports(path: Path) -> set:
    return _imports(path)      # ast.walk reaches function bodies too: a lazy import counts


def test_t3_probe_modules_never_import_the_supervisor_or_launch():
    for name in ("probe.py", "probe_child.py"):
        names = _all_imports(REPO / "levain" / "slot" / name)
        assert not {n for n in names if n in ("levain.slot.supervisor", "levain.launch")
                    or n.startswith("levain.launch.")}, name
        assert "levain.slot" not in names or name != "probe_child.py"


# --- T4 host order -----------------------------------------------------------------------------------------------------

def test_t4_order_upstream_then_route_hosts_then_api_host():
    from levain.slot.probe import probe_hosts

    assert probe_hosts("claude", "api.anthropic.com", ["api.anthropic.com"]) == ("api.anthropic.com",)
    assert probe_hosts("claude", "platform.claude.com", ["claude.ai", "evil.example", "claude.ai"]) == \
        ("platform.claude.com", "claude.ai", "api.anthropic.com")
    assert probe_hosts("claude", None, ["claude.ai"]) == ("claude.ai", "api.anthropic.com")
    assert probe_hosts("codex", "proxy.example", ["chatgpt.com", "auth.openai.com"]) == \
        ("chatgpt.com", "auth.openai.com", "api.openai.com")


def test_t4_same_order_under_two_hash_seeds():
    import os
    import subprocess
    import sys

    code = ("from levain.slot.probe import probe_hosts, PROBE_HOSTS;"
            "print([probe_hosts(v, None, list(reversed(h))) for v, h in sorted(PROBE_HOSTS.items())])")
    outs = set()
    for seed in ("0", "1"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=str(REPO))
        outs.add(subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                                check=True, cwd=str(REPO)).stdout)
    assert len(outs) == 1


# --- T5 the verdict and the child's result -----------------------------------------------------------------------------

A = [("api.anthropic.com", "192.0.2.10"), ("api.anthropic.com", "2001:db8::10")]


def test_t5_verdict_branches():
    from levain.slot.probe import verdict

    yes = {"192.0.2.10": True, "2001:db8::10": True}
    no = {"192.0.2.10": False, "2001:db8::10": False}
    assert _refused(verdict, [], {}, {}, {}) == "firewall-probe-inconclusive:dns"
    with pytest.raises(Refused) as e:
        verdict(A, yes, {"192.0.2.10": "b", "2001:db8::10": "c"}, yes)
    assert e.value.name == "firewall-open" and e.value.detail == "api.anthropic.com 2001:db8::10"
    assert _refused(verdict, A, no, {"192.0.2.10": "c", "2001:db8::10": "b"}, no) == "firewall-open"
    assert verdict(A, {"192.0.2.10": False, "2001:db8::10": True}, {"192.0.2.10": "b", "2001:db8::10": "b"}, yes) \
        == {"probe": "closed", "address": "2001:db8::10", "host": "api.anthropic.com", "addresses": 2}
    assert _refused(verdict, A, no, {"192.0.2.10": "b", "2001:db8::10": "b"}, no) == \
        "firewall-probe-inconclusive:no-control-connect"
    # codex MED: connected before, lost after, slot blocked = not evidence
    assert _refused(verdict, A, yes, {"192.0.2.10": "b", "2001:db8::10": "b"}, no) == \
        "firewall-probe-inconclusive:no-control-connect"
    assert _refused(verdict, A, no, {"192.0.2.10": "b", "2001:db8::10": "b"}, yes) == \
        "firewall-probe-inconclusive:no-control-connect"


@pytest.mark.parametrize("raw", [b"", b"c 192.0.2.10\n", b"c 192.0.2.10\nend\n",
                                 b"b 192.0.2.10 timeout\nb 2001:db8::10 ENOBUFS\nend\n",
                                 b"b 192.0.2.10 timeout\nu 2001:db8::10 EINVAL\nend\n",
                                 b"b 192.0.2.10 timeout\nb 192.0.2.10 timeout\nend\n",
                                 b"b 192.0.2.10 timeout\nb 192.0.2.99 timeout\nend\n",
                                 b"b 192.0.2.10 timeout\nc 2001:db8::10\nend\nmore\n",
                                 b"b 192.0.2.10  timeout\nc 2001:db8::10\nend\n",
                                 b"\xff\nend\n", b"!", b"c 192.0.2.10\nc 2001:db8::10\n" + b"x" * 5000 + b"\nend\n"])
def test_t5_unreadable_child_is_unknown_never_closed_or_open(raw):
    from levain.slot.probe import parse_result

    assert _refused(parse_result, raw, ["192.0.2.10", "2001:db8::10"]) == "firewall-probe-unknown:child"


def test_t5_readable_child():
    from levain.slot.probe import parse_result

    assert parse_result(b"b 192.0.2.10 EPERM\nc 2001:db8::10\nend\n", ["192.0.2.10", "2001:db8::10"]) == \
        {"192.0.2.10": "b", "2001:db8::10": "c"}


def test_t5_a_connect_dominates_an_unknown_line():
    """L3 r1 X2 (codex MED): IPv4 connected and IPv6 unsupported on the host is a confirmed open wall, never unknown."""
    from levain.slot.probe import parse_result, verdict

    got = parse_result(b"c 192.0.2.10\nu 2001:db8::10 EAFNOSUPPORT\nend\n", ["192.0.2.10", "2001:db8::10"])
    assert got == {"192.0.2.10": "c", "2001:db8::10": "u"}
    with pytest.raises(Refused) as e:
        verdict(A, {}, got, {})
    assert e.value.name == "firewall-open" and e.value.detail == "api.anthropic.com 192.0.2.10"
    assert parse_result(b"u 192.0.2.10 errno999\nc 2001:db8::10\nend\n", ["192.0.2.10", "2001:db8::10"]) == \
        {"192.0.2.10": "u", "2001:db8::10": "c"}


@pytest.mark.parametrize("raw", [b"u 192.0.2.10\nc 2001:db8::10\nend\n",
                                 b"u 192.0.2.10 not-an-errno\nc 2001:db8::10\nend\n",
                                 b"u 192.0.2.10 EAFNOSUPPORT x\nc 2001:db8::10\nend\n",
                                 b"u 192.0.2.10 EAFNOSUPPORT\nu 192.0.2.10 EAFNOSUPPORT\nc 2001:db8::10\nend\n",
                                 b"u 192.0.2.10 EAFNOSUPPORT\nb 2001:db8::10 timeout\nend\n",
                                 b"u 192.0.2.10 bad-address\nu 2001:db8::10 EINVAL\nend\n"])
def test_t5_malformed_or_unanswered_u_stays_unknown(raw):
    from levain.slot.probe import parse_result

    assert _refused(parse_result, raw, ["192.0.2.10", "2001:db8::10"]) == "firewall-probe-unknown:child"


def test_n6_the_whole_hold_is_under_ends_lock_wait():
    from levain.slot import end as E
    from levain.slot import proc as PR
    from levain.slot import probe as PB

    assert PC.CONNECT_S < PC.PROBE_SELF_S < PB.PROBE_REAP_S
    assert PB.PROBE_REAP_S + PR.REAP_GRACE_S < E.END_LOCK_WAIT_S


# --- T6 the fence (R3, N6, N7, codex MED reap) on the start harness: a real store, the real CONTROL ---------------------

IP4, IP6 = "192.0.2.10", "2001:db8::10"


def _base_v2():
    from levain.slot import vendor_routes as VR

    return {"format": "levain-routes-base/2", "routes": [
        VR.anthropic_route(), {"route": "github", "type": "egress", "hosts": ["github.com"]},
        {"route": "claude-sub", "type": "subscription_passthrough", "hosts": ["claude.ai"]}]}


class Probe:
    """The probe's seams: a resolver, CONTROL's two rounds and a probe child that writes a scripted result on its
    result pipe. ``held`` records whether the session END lock was held at the spawn (a try of it was refused)."""

    def __init__(self, w, la):
        self.w, self.la = w, la
        self.answers = {"api.anthropic.com": [IP4, IP6]}
        self.before = {IP4: True, IP6: True}
        self.after = {IP4: True, IP6: True}
        self.rounds = 0
        self.result = None           # None = every address blocked by timeout
        self.status = 0
        self.hold_open = False       # keep a copy of the write end: no EOF ever arrives
        self.no_exit = False         # the result arrives, but the child never exits
        self.raise_after_reap = None  # an exception wait_within raises right after its reap (Y1)
        self.spawns, self.killpgs, self.kills, self.waits, self.held = [], [], [], [], []
        self.events = []             # ("killpg" | "wait" | "kill" | "exited", pid) in call order
        self.kept = []
        self.next_pid = 72000

    def getaddrinfo(self, host, port, type=0):
        assert port == 443 and type == socket.SOCK_STREAM
        if host not in self.answers:
            raise socket.gaierror(socket.EAI_NONAME, "no")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 (ip, 443, 0, 0) if ":" in ip else (ip, 443)) for ip in self.answers[host]]

    def connect(self, ips):
        self.rounds += 1
        table = self.before if self.rounds == 1 else self.after
        return {ip: (PC.CONNECTED, "") if table.get(ip) else (PC.BLOCKED, "timeout") for ip in ips}

    def spawn(self, argv, spec_json, cgroup, exec_w, detach):
        from levain.contracts import canon
        from levain.slot import runtime_helper as H

        spec = canon.parse(spec_json)
        if spec["target"] != "probe":
            return self.la.spawn(argv, spec_json, cgroup, exec_w, detach)
        try:
            ctx = H.Ctx(self.w.seams, H.Store(self.w.seams), 0, "operator", 0, {})
            with H.session_lock(ctx, self.w.session):
                self.held.append(False)
        except Refused as e:
            self.held.append(e.name == "session-busy")
        pid = self.next_pid
        self.next_pid += 1
        self.spawns.append({"pid": pid, "argv": tuple(argv), "spec": spec, "cgroup": cgroup, "detach": detach,
                            "exec_w": exec_w})
        ips = spec["argv"][7:]
        data = self.result if self.result is not None else \
            "".join(f"b {ip} timeout\n" for ip in ips).encode() + b"end\n"
        os.write(exec_w, data[:65536])
        if self.hold_open:
            self.kept.append(os.dup(exec_w))
        return pid

    def exited(self, pid, timeout):
        self.events.append(("exited", pid))
        return not self.no_exit

    def wait_within(self, pid, bound):
        if pid not in {s["pid"] for s in self.spawns}:
            return self.la.wait_within(pid, bound)
        self.waits.append(pid)
        self.events.append(("wait", pid))
        if self.raise_after_reap is not None:
            raise self.raise_after_reap
        return self.status

    def kill(self, pid):
        if pid in {s["pid"] for s in self.spawns}:
            self.kills.append(pid)
            self.events.append(("kill", pid))
        else:
            self.la.kill(pid)

    def close(self):
        for fd in self.kept:
            os.close(fd)


import os  # noqa: E402  (the harness section's own imports)


@pytest.fixture
def penv(tmp_path):
    from levain.contracts import canon
    from levain.slot import probe as PB
    from tests.test_slot_s2f3_start import _world

    w, la, table = _world(tmp_path)
    pr = Probe(w, la)
    s = w.seams
    s.spawn, s.wait_within, s.kill = pr.spawn, pr.wait_within, pr.kill
    s.probe = PB.ProbeSeams(getaddrinfo=pr.getaddrinfo, connect=pr.connect,
                            killpg=lambda pid, sig: pr.killpgs.append((pid, sig)) or pr.events.append(("killpg", pid)),
                            exited=pr.exited, resolve_s=2.0, reap_s=2.0)
    (w.L / "routes" / "base.json").write_bytes(canon.canonical_bytes(_base_v2()))
    yield w, la, table, pr
    pr.close()


def _v1_entry(r):
    """A /2 entry cut to what render reads: its id, its type and, for an egress route, its hosts (WF-1b: an egress
    route with no hosts is refused ``base-map-malformed``)."""
    return {"route": r["route"], "type": r["type"], **({"hosts": r["hosts"]} if r["type"] == "egress" else {})}


def _rendered_probe(w):
    from tests.test_slot_s2f3_start import _rendered

    return _rendered(w)       # on the full /2 map: render copies the session's entries into the front's file (3b-8a)


def _record(w, slot="e0"):
    from levain.contracts import canon

    p = w.L / "slots" / slot / "probe"
    return canon.parse(p.read_bytes()) if p.exists() else None


PROBE_STEPS = ["probe-read", "probe-resolve", "probe-control", "probe-lock", "probe-spawn", "probe-reaped",
               "probe-release", "probe-control-after"]


def test_t6_rendered_probe_closed_in_order_spawned_under_the_lock(penv, monkeypatch):
    from levain.slot import proc as PR
    from levain.slot import start as SS

    w, la, table, pr = penv
    s = _rendered_probe(w)
    for name in ("_kill_quiet", "_abandon", "kill_domain"):      # the probe never touches the slot's domain
        monkeypatch.setattr(SS, name, lambda *a, **k: pytest.fail("domain kill"))
    monkeypatch.setattr(PR, "end_domain", lambda *a, **k: pytest.fail("end_domain"))
    w.steps.clear()
    code, line = w.main(["probe", s])
    assert line == {"ok": True, "probe": "closed", "address": IP4, "host": "api.anthropic.com", "addresses": 2}
    assert [x for x in w.steps if x.startswith("probe-")] == PROBE_STEPS
    assert pr.held == [True] and len(pr.spawns) == 1 and pr.waits == [pr.spawns[0]["pid"]]
    sp = pr.spawns[0]
    assert sp["detach"] is True and sp["spec"]["target"] == "probe" and sp["spec"]["argv"][7:] == [IP4, IP6]
    assert sp["cgroup"] == w.alloc(s).cgroup and w.cgroups[-1] == w.alloc(s).cgroup
    import signal
    # the group is killed on every path (nothing it forked outlives the lock); the reaped child is not re-killed
    assert pr.rounds == 2 and pr.killpgs == [(sp["pid"], signal.SIGKILL)] and pr.kills == []
    # X4: the group is killed while its leader is still unreaped (its pid and group id cannot be reused), then reaped
    assert pr.events == [("exited", sp["pid"]), ("killpg", sp["pid"]), ("wait", sp["pid"])]
    rec = _record(w)
    assert (rec["format"], rec["session"], rec["outcome"], rec["cause"]) == \
        ("levain-probe-result/1", s, "closed", "closed")
    assert (w.L / "slots" / "e0" / "probe").stat().st_mode & 0o777 == 0o644


def test_x5_overlapping_record_writes_are_serialized(penv, monkeypatch):
    """L3 r1 X5 (codex MED): two probes' record writes overlap (A is inside its fsync when B starts). Each must
    publish a whole record in turn; neither may unlink the other's temp and fail or publish the other's file."""
    import threading

    from levain.slot import probe as PB
    from levain.slot import runtime_helper as H
    from levain.slot import start as SS

    w, la, table, pr = penv
    s = _rendered_probe(w)
    a = w.alloc(s)
    notes = []
    monkeypatch.setattr(SS, "_note", notes.append)
    a_in, b_done = threading.Event(), threading.Event()
    real = w.seams.fsync
    first = []

    def fsync(fd):
        if not first:
            first.append(fd)
            a_in.set()
            b_done.wait(0.5)          # B runs here unless the write is serialized (then B waits for A)
        return real(fd)
    monkeypatch.setattr(w.seams, "fsync", fsync)

    def write(outcome):
        ctx = H.Ctx(w.seams, H.Store(w.seams), 0, "operator", 0, {})
        try:
            PB._record(ctx, a, outcome, outcome)
        finally:
            ctx.store.close()

    ta = threading.Thread(target=write, args=("open",))
    ta.start()
    assert a_in.wait(5)

    def b():
        write("closed")
        b_done.set()
    tb = threading.Thread(target=b)
    tb.start()
    ta.join(5)
    tb.join(5)
    assert notes == []
    assert _record(w)["outcome"] == "closed"
    assert sorted(p.name for p in (w.L / "slots" / "e0").iterdir() if p.name.startswith("probe")) == ["probe"]


def test_t6_active_session_is_probeable(penv):
    from tests.test_slot_s2f3_start import _started

    w, la, table, pr = penv
    s = _started(w, la, table)                  # on penv's full /2 map (render copies its entries, 3b-8a)
    assert w.state(s).state == "ACTIVE"
    assert w.main(["probe", s])[1]["probe"] == "closed"


def test_t6_lock_held_is_session_busy_nothing_spawned(penv):
    from levain.slot import runtime_helper as H

    w, la, table, pr = penv
    s = _rendered_probe(w)
    ctx = H.Ctx(w.seams, H.Store(w.seams), 0, "operator", 0, {})
    with H.session_lock(ctx, s):
        assert w.main(["probe", s])[1]["refused"] == "session-busy"
    assert pr.spawns == [] and _record(w) is None


def test_t6_alloc_changed_under_the_lock(penv):
    from dataclasses import replace

    from levain.slot import records as R

    w, la, table, pr = penv
    s = _rendered_probe(w)

    def change(step):
        if step == "probe-lock":
            a = w.alloc(s)
            (w.L / "alloc" / f"{s}.json").write_bytes(R.alloc_bytes(replace(a, slot_rev=a.slot_rev + 1)))
    w.on_step = change
    assert w.main(["probe", s])[1]["refused"] == "not-probeable:alloc-changed"
    assert pr.spawns == [] and _record(w) is None


def test_t6_slot_moved_under_the_lock(penv):
    from tests.test_slot_s2f2_runtime_helper import _slot_state
    from levain.slot.ids import SlotId

    w, la, table, pr = penv
    s = _rendered_probe(w)

    def move(step):
        if step == "probe-lock":
            (w.L / "slots" / "e0" / "state").write_bytes(_slot_state(SlotId("egress", 0), "leased", rev=9, seq=9,
                                                                      session="s-fedcba9876543210"))
    w.on_step = move
    assert w.main(["probe", s])[1]["refused"] == "slot-not-ours"
    assert pr.spawns == []


def test_t6_end_between_the_unlocked_read_and_the_spawn_never_spawns(penv):
    """F7: END runs after the probe's unlocked read (here: while CONTROL runs); the probe then takes the lock, sees
    what END changed, and refuses with nothing spawned into a slot END has proven empty."""
    w, la, table, pr = penv
    s = _rendered_probe(w)
    ended = []

    def end_now(step):
        if step == "probe-control" and not ended:
            ended.append(w.main(["teardown", s])[1])
    w.on_step = end_now
    line = w.main(["probe", s])[1]
    assert ended == [{"ok": True, "state": "RETAINED"}]
    assert line == {"ok": False, "refused": "not-probeable:RETAINED"}
    assert pr.spawns == []


def test_t6_not_probeable_before_render(penv):
    from tests.test_slot_s2f2_runtime_helper import _prepared

    w, la, table, pr = penv
    s = _prepared(w)
    w.session = s
    assert w.main(["probe", s])[1]["refused"] == "not-probeable:ALLOCATED"
    assert pr.rounds == 0 and _record(w) is None


def test_t6_firewall_open_names_host_and_address_on_stderr_only(penv):
    w, la, table, pr = penv
    s = _rendered_probe(w)
    pr.result = f"b {IP4} ECONNREFUSED\nc {IP6}\nend\n".encode()
    code, line = w.main(["probe", s])
    assert line == {"ok": False, "refused": "firewall-open"}
    assert "api.anthropic.com 2001:db8::10" in w.last_err
    assert _record(w)["outcome"] == "open" and len(pr.killpgs) == 1 and pr.kills == []


@pytest.mark.parametrize("other", [f"b {IP6} timeout", f"u {IP6} EAFNOSUPPORT"])
def test_t6_open_is_decided_before_control_after(penv, other):
    """L3 r1 X3 (codex MED): a confirmed ``c`` is ``firewall-open`` (recorded) even when the second CONTROL would
    raise (EMFILE from the selector); the second CONTROL is never run once the slot connected."""
    w, la, table, pr = penv
    s = _rendered_probe(w)
    pr.result = f"c {IP4}\n{other}\nend\n".encode()
    real = pr.connect

    def connect(ips):
        if pr.rounds >= 1:
            raise OSError(errno.EMFILE, "too many open files")
        return real(ips)
    w.seams.probe.connect = connect
    w.steps.clear()
    assert w.main(["probe", s])[1] == {"ok": False, "refused": "firewall-open"}
    assert "probe-control-after" not in w.steps
    assert _record(w)["outcome"] == "open"


def test_t6_no_eof_kills_the_group_then_reaps_before_release(penv):
    import signal

    w, la, table, pr = penv
    s = _rendered_probe(w)
    pr.hold_open = True
    w.seams.probe.reap_s = 0.3
    order = []
    w.on_step = lambda step: order.append((step, list(pr.kills)))
    assert w.main(["probe", s])[1]["refused"] == "firewall-probe-unknown:unreaped"
    pid = pr.spawns[0]["pid"]
    assert pr.killpgs == [(pid, signal.SIGKILL)] and pr.kills == [pid] and pr.waits == []
    assert ("probe-release", [pid]) in order                     # killed and reaped BEFORE the lock was released
    assert _record(w)["outcome"] == "unknown"


def test_t6_result_without_exit_kills_the_group_then_the_child(penv):
    """X4: EOF arrived but the leader never exited inside the bound: unknown, the group killed while the leader is
    unreaped, then the leader killed and reaped; never a wait that reaps first."""
    w, la, table, pr = penv
    s = _rendered_probe(w)
    pr.no_exit = True
    assert w.main(["probe", s])[1]["refused"] == "firewall-probe-unknown:unreaped"
    pid = pr.spawns[0]["pid"]
    assert pr.events == [("exited", pid), ("killpg", pid), ("kill", pid)]


@pytest.mark.parametrize("exc", [RuntimeError("a signal handler raised"), KeyboardInterrupt()])
def test_y1_an_exception_right_after_the_reap_never_signals_again(penv, exc):
    """L3 r2 Y1 (codex): wait_within reaps the leader and then raises (a signal handler firing while it restores its
    handlers). The pid may already name another process: nothing after the reap may signal it or its group."""
    w, la, table, pr = penv
    s = _rendered_probe(w)
    pr.raise_after_reap = exc
    try:
        w.main(["probe", s])
    except BaseException:            # KeyboardInterrupt may leave main; the order below is the test
        pass
    pid = pr.spawns[0]["pid"]
    assert pr.events == [("exited", pid), ("killpg", pid), ("wait", pid)]
    assert pr.kills == []


def _fork(body):
    pid = os.fork()
    if pid == 0:                     # pragma: no cover - the child
        try:
            body()
        finally:
            os._exit(0)
    return pid


@pytest.mark.skipif(not hasattr(__import__("select"), "kqueue") and not hasattr(os, "pidfd_open"),
                    reason="no exit watch on this host")
def test_x4_exited_unreaped_on_real_children():
    """The real watch on this host's own children (no network, no profile): a zombie is seen as exited and is
    still ours to reap afterwards; a child exiting inside the bound is seen; one that outlives it is not."""
    import time

    from levain.slot.probe import exited_unreaped

    pid = _fork(lambda: None)
    time.sleep(0.2)                                     # a zombie by now
    assert exited_unreaped(pid, 1.0) is True
    assert os.waitpid(pid, 0)[0] == pid                 # not reaped by the watch
    pid = _fork(lambda: time.sleep(0.2))
    assert exited_unreaped(pid, 3.0) is True
    assert os.waitpid(pid, 0)[0] == pid
    pid = _fork(lambda: time.sleep(5))
    t0 = time.monotonic()
    try:
        assert exited_unreaped(pid, 0.2) is False
        assert time.monotonic() - t0 < 2
    finally:
        os.kill(pid, 9)
        os.waitpid(pid, 0)


def _hide_watches(monkeypatch, waitid=None, pidfd_open=None):
    """Y2's seam: the probe module sees a host with no kqueue and (unless given) no pidfd_open; ``waitid`` stands in
    for the host's. Only ``exited_unreaped`` runs while it is in place."""
    import select as real_select
    import types

    from levain.slot import probe as PB

    fake_os = types.SimpleNamespace(close=os.close, P_PID=os.P_PID, WEXITED=os.WEXITED, WNOHANG=os.WNOHANG,
                                    WNOWAIT=os.WNOWAIT)
    if waitid is not None:
        fake_os.waitid = waitid
    if pidfd_open is not None:
        fake_os.pidfd_open = pidfd_open
    monkeypatch.setattr(PB, "os", fake_os)
    monkeypatch.setattr(PB, "select", types.SimpleNamespace(poll=real_select.poll, POLLIN=real_select.POLLIN))
    return PB.exited_unreaped


def test_y2_no_kqueue_no_pidfd_falls_back_to_a_waitid_poll(monkeypatch):
    """L3 r2 Y2 (codex MED): a Linux kernel without pidfd_open (Debian 10, 4.19) must still see the exit, by
    ``waitid(P_PID, pid, WEXITED | WNOHANG | WNOWAIT)`` polled within the bound (it observes the exit, never reaps)."""
    calls = []

    def waitid(idtype, pid, options):
        calls.append((idtype, pid, options))
        return None if len(calls) < 3 else object()     # not yet, not yet, exited

    watch = _hide_watches(monkeypatch, waitid=waitid)
    assert watch(4242, 2.0) is True
    assert calls[0] == (os.P_PID, 4242, os.WEXITED | os.WNOHANG | os.WNOWAIT) and len(calls) == 3


def test_y2_waitid_poll_stops_at_the_bound_and_on_no_child(monkeypatch):
    import time

    calls = []
    watch = _hide_watches(monkeypatch, waitid=lambda *a: calls.append(a))
    t0 = time.monotonic()
    assert watch(4242, 0.1) is False
    assert 0.1 <= time.monotonic() - t0 < 1.0 and len(calls) >= 2

    def gone(*a):
        raise ChildProcessError(10, "no child")

    watch = _hide_watches(monkeypatch, waitid=gone)
    assert watch(4242, 1.0) is False


def test_y2_pidfd_open_unsupported_falls_back_to_waitid(monkeypatch):
    """Python built with pidfd_open, running on a kernel without it: ENOSYS from the call, then the waitid poll."""
    import errno

    def no_pidfd(pid):
        raise OSError(errno.ENOSYS, "Function not implemented")

    watch = _hide_watches(monkeypatch, waitid=lambda *a: object(), pidfd_open=no_pidfd)
    assert watch(4242, 1.0) is True


def test_y2_no_watch_at_all_is_false(monkeypatch):
    assert _hide_watches(monkeypatch)(4242, 0.05) is False


@pytest.mark.skipif(not hasattr(os, "waitid"), reason="no waitid on this host (macOS): the seam tests above hold it")
def test_y2_waitid_fallback_on_real_children(monkeypatch):
    import time

    watch = _hide_watches(monkeypatch, waitid=os.waitid)
    pid = _fork(lambda: None)
    time.sleep(0.2)
    assert watch(pid, 1.0) is True
    assert os.waitpid(pid, 0)[0] == pid                 # not reaped by the watch
    pid = _fork(lambda: time.sleep(5))
    try:
        assert watch(pid, 0.2) is False
    finally:
        os.kill(pid, 9)
        os.waitpid(pid, 0)


@pytest.mark.parametrize("result,status", [(b"garbage\n", 0), (None, 3), (b"x" * 5000, 0)])
def test_t6_bad_result_is_unknown_and_leaves_no_group(penv, result, status):
    import signal

    w, la, table, pr = penv
    s = _rendered_probe(w)
    pr.result, pr.status = result, status
    assert w.main(["probe", s])[1]["refused"] == "firewall-probe-unknown:child"
    pid = pr.spawns[0]["pid"]
    assert (pid, signal.SIGKILL) in pr.killpgs
    assert pr.waits == [pid] or pr.kills == [pid]                # reaped by its wait, or killed and reaped


def test_t6_fork_failure_is_one_unknown(penv):
    import errno as E

    w, la, table, pr = penv
    s = _rendered_probe(w)

    def fail(argv, spec_json, cgroup, exec_w, detach):
        raise OSError(E.EAGAIN, "fork")
    w.seams.spawn = fail
    assert w.main(["probe", s])[1]["refused"] == "firewall-probe-unknown:child"
    assert pr.killpgs == [] and _record(w)["outcome"] == "unknown"


def test_t6_dns_and_control_inconclusives_are_recorded(penv):
    w, la, table, pr = penv
    s = _rendered_probe(w)
    pr.answers = {}
    assert w.main(["probe", s])[1]["refused"] == "firewall-probe-inconclusive:dns"
    assert pr.rounds == 0 and pr.spawns == [] and _record(w)["outcome"] == "inconclusive"
    pr.answers = {"api.anthropic.com": [IP4]}
    pr.after = {}
    assert w.main(["probe", s])[1]["refused"] == "firewall-probe-inconclusive:no-control-connect"
    assert _record(w)["cause"] == "firewall-probe-inconclusive:no-control-connect"


def test_n1_a_hung_resolver_is_dns_inside_the_bound(penv):
    import threading
    import time

    w, la, table, pr = penv
    s = _rendered_probe(w)
    gate = threading.Event()
    w.seams.probe.getaddrinfo = lambda *a, **k: gate.wait(30) and []
    w.seams.probe.resolve_s = 0.2
    t0 = time.monotonic()
    try:
        assert w.main(["probe", s])[1]["refused"] == "firewall-probe-inconclusive:dns"
    finally:
        gate.set()
    assert time.monotonic() - t0 < 5


def test_n8_resolver_answers_filtered(penv):
    w, la, table, pr = penv
    s = _rendered_probe(w)
    pr.answers = {"api.anthropic.com": ["127.0.0.1", "0.0.0.0", "fe80::1", IP4, IP4, "224.0.0.1"]}
    assert w.main(["probe", s])[1]["addresses"] == 1
    assert pr.spawns[0]["spec"]["argv"][7:] == [IP4]
    pr.answers = {"api.anthropic.com": ["127.0.0.1"]}
    assert w.main(["probe", s])[1]["refused"] == "firewall-probe-inconclusive:dns"
    assert "127.0.0.1" in w.last_err


def test_too_many_addresses(penv):
    w, la, table, pr = penv
    s = _rendered_probe(w)
    pr.answers = {"api.anthropic.com": [f"198.51.100.{i}" for i in range(1, 66)]}
    assert w.main(["probe", s])[1]["refused"] == "firewall-probe-inconclusive:too-many"
    assert pr.spawns == []


def test_route_missing_from_the_base_map(penv):
    from levain.contracts import canon

    w, la, table, pr = penv
    s = _rendered_probe(w)
    base = _base_v2()
    base["routes"] = [r for r in base["routes"] if r["route"] != "anthropic"]
    (w.L / "routes" / "base.json").write_bytes(canon.canonical_bytes(base))
    assert w.main(["probe", s])[1]["refused"] == "not-probeable:route"
    assert _record(w) is None
