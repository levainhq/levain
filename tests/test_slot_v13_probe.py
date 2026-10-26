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
    files = {f.path: f for f in rd.render(blob, facts).files}
    prof, probe = files["root/profile.sb"], files["root/probe.sb"]
    assert (probe.uid, probe.gid, probe.mode) == (prof.uid, prof.gid, prof.mode)
    assert probe.data == rd.render_profile(blob, facts, probe=True)
    blob, facts = egress(host="linux")
    assert not any(f.path.endswith(".sb") for f in rd.render(blob, facts).files)     # darwin only
    blob, facts = verifier()
    paths = {f.path for f in rd.render(blob, facts).files}
    assert "root/profile.sb" in paths and "root/probe.sb" not in paths    # no probe runs in a none-class slot
