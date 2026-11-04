"""levain 27 slice S10c: ``levain.slot.dispatch_runtime`` (``HelperSlotRuntime`` + ``DispatchControlPort``).

The helper is a REAL subprocess: a scripted fake (``FAKE_HELPER``) that records its argv and stdin and prints the answer
a test scripts, so the order of the verbs and what each is given are read off a log, not off a mock. CONTROL is the REAL
``Control.handle`` (the tickets rig, as the dispatch uid) for ``LEASE-OPEN{ticket}``. No sudo, no VM.

UNVERIFIED here (VM rows): a real prepare/render/start/teardown as ``_levain_dispatch``; whether sudo's ``use_pty``
allocates with no tty (finding J); whether helper ``status`` can prove a RETAINED-and-scrubbed session (F6).
"""
from __future__ import annotations

import json
import sys
import tracemalloc
from pathlib import Path

import pytest

from levain.agents import session_launcher as L
from levain.contracts import canon
from levain.slot import Refused
from levain.slot import control as C
from levain.slot import dispatch_runtime as DR
from levain.slot.runtime_helper import PREPARE_RESULT_FORMAT
from tests.test_routemaps import DISPATCH_UID, st  # noqa: F401

SESSION = "s-00000000000000a1"
NONCE = "n" * 22
BRIEF = "the brief for the slot"

FAKE_HELPER = '''\
import json, os, sys
here = sys.argv[0]
cfg = json.load(open(here + ".cfg"))
verb = sys.argv[1]
stdin = sys.stdin.read() if verb in ("prepare", "render") else ""
rec = {"argv": sys.argv[1:], "stdin": stdin}
if verb == "render":
    rec["brief_staged"] = os.path.exists(cfg["brief_path"])
with open(here + ".log", "a") as f:
    f.write(json.dumps(rec) + "\\n")
spec = cfg.get(verb, {})
if verb == "start":
    chunk = b"X" * 65536
    left = spec.get("noise", 0)
    while left > 0:
        n = min(left, len(chunk))
        sys.stdout.buffer.write(chunk[:n])
        left -= n
    sys.stdout.buffer.flush()
    sys.stderr.write(spec.get("err", ""))
    sys.exit(spec.get("rc", 0))
sys.stdout.write(spec["line"] + "\\n")
sys.exit(spec.get("rc", 0))
'''


def line(**obj) -> str:
    return canon.canonical_bytes(obj).decode()


class Helper:
    def __init__(self, tmp: Path, store: Path) -> None:
        self.script = tmp / "fake_helper.py"
        self.script.write_text(FAKE_HELPER)
        self.store = store
        self.staging = store / "staging" / SESSION
        self.staging.mkdir(parents=True)
        self.cfg = {
            "brief_path": str(self.staging / "brief.md"),
            "prepare": {"line": line(ok=True, format=PREPARE_RESULT_FORMAT, session=SESSION,
                                     slot={"class": "egress", "index": 1}, staging=str(self.staging), gwport=47101)},
            "render": {"line": line(ok=True)},
            "teardown": {"line": line(ok=True, state="RETAINED", released=False)},
            "start": {"rc": 0},
        }
        self.write()

    def write(self) -> None:
        Path(str(self.script) + ".cfg").write_text(json.dumps(self.cfg))

    def script_(self, verb: str, **spec) -> None:
        self.cfg[verb] = spec
        self.write()

    @property
    def prefix(self):
        return (sys.executable, str(self.script))

    def calls(self) -> list:
        p = Path(str(self.script) + ".log")
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def verbs(self) -> list:
        return [c["argv"][0] for c in self.calls()]


class FakeLease:
    def __init__(self, answer=None, fail=None) -> None:
        self.calls, self.closed, self.answer, self.fail = [], 0, answer or {"lease_nonce": NONCE}, fail

    def call(self, op, **fields):
        self.calls.append((op, fields))
        if self.fail:
            raise self.fail
        return dict(self.answer)

    def close(self):
        self.closed += 1


def req(ticket="TICKET-SENTINEL-0001", *, brief=BRIEF, digest=None, hooks=L.HOOKS):
    import hashlib
    d = digest or "sha256:" + hashlib.sha256(brief.encode()).hexdigest()
    return L.SlotStart(ticket, "r-0123456789abcdef", "s-0123456789abcdef", "claude", "sonnet", brief, d, tuple(hooks))


@pytest.fixture
def rt_rig(tmp_path):
    store = tmp_path / "store"
    h = Helper(tmp_path, store)
    lease = FakeLease()

    def make(**kw):
        return DR.HelperSlotRuntime(host="darwin", helper=h.prefix, store=store, routes=("pypi", "pass-1"),
                                    client_factory=lambda _p: lease, kill_grace_s=0.5, **kw)

    return h, lease, make


# ---- the order, and what each verb is given -------------------------------------------------------------------------

def test_the_order_is_prepare_brief_render_start_and_there_is_no_probe(rt_rig):
    h, lease, make = rt_rig
    rt = make()
    handle = rt.start(req())
    try:
        assert handle.session == SESSION and h.verbs() == ["prepare", "render"]        # start waits for the release
        prep, render = h.calls()
        assert prep["argv"] == ["prepare", "egress", "dispatch", "claude", "passthrough"]
        assert json.loads(prep["stdin"]) == {"lease_nonce": NONCE}
        assert render["argv"] == ["render", SESSION] and render["brief_staged"] is True   # the brief BEFORE render
        blob = json.loads(render["stdin"])
        assert blob == {"format": "levain-session-render/1", "vendor": "claude", "routes": ["pypi", "pass-1"],
                        "model": "sonnet", "hooks": ["levain_gate", "levain_queue"]}
        assert (rt._store / "staging" / SESSION / "brief.md").read_text() == BRIEF
        assert lease.calls == [("LEASE-OPEN", {"ticket": "TICKET-SENTINEL-0001"})] and lease.closed == 0
        handle.child.release()
        assert handle.child.process.wait(30) == 0
        assert h.verbs() == ["prepare", "render", "start"] and h.calls()[2]["argv"] == ["start", SESSION]
        assert "probe" not in h.verbs()
    finally:
        handle.child.abort()


def test_the_workspace_seam_runs_between_the_brief_and_render(rt_rig):
    h, _lease, make = rt_rig
    seen = []

    def ws(r, staging):
        seen.append((h.verbs(), (staging / "brief.md").exists()))

    rt = make(workspace=ws)
    handle = rt.start(req())
    handle.child.abort()
    assert seen == [(["prepare"], True)]


def test_the_ticket_is_in_the_lease_frame_and_nowhere_else(rt_rig):
    h, lease, make = rt_rig
    rt = make()
    handle = rt.start(req())
    handle.child.release()
    handle.child.process.wait(30)
    blob = json.dumps(h.calls()) + Path(str(h.script) + ".cfg").read_text()
    assert "TICKET-SENTINEL" not in blob
    assert [c for c in lease.calls if "TICKET-SENTINEL-0001" in json.dumps(c)] == lease.calls


# ---- a failure at each step -----------------------------------------------------------------------------------------

def test_a_lease_refusal_runs_no_helper_and_closes_the_lease(rt_rig):
    h, lease, make = rt_rig
    lease.fail = Refused("ticket-bound")
    with pytest.raises(Refused):
        make().start(req())
    assert h.verbs() == [] and lease.closed == 1


def test_a_prepare_refusal_or_malformed_answer_closes_the_lease_with_no_teardown(rt_rig):
    h, lease, make = rt_rig
    h.script_("prepare", line=line(ok=False, refused="no-free-slot"))
    with pytest.raises(Refused) as e:
        make().start(req())
    assert e.value.name == "no-free-slot" and h.verbs() == ["prepare"] and lease.closed == 1
    h.script_("prepare", line=line(ok=True, format=PREPARE_RESULT_FORMAT, session="nope"))
    with pytest.raises(Refused) as e2:
        make().start(req())
    assert e2.value.name == "prepare-answer-malformed" and "teardown" not in h.verbs() and lease.closed == 2


@pytest.mark.parametrize("step", ["brief", "workspace", "render", "gate"])
def test_a_failure_after_prepare_ends_once_and_closes_the_lease_on_proof(rt_rig, monkeypatch, step):
    h, lease, make = rt_rig
    kw, r = {}, req()
    if step == "brief":
        r = req(digest="sha256:" + "0" * 64)
    elif step == "workspace":
        def boom(*_a):
            raise Refused("workspace-refused")
        kw["workspace"] = boom
    elif step == "render":
        h.script_("render", line=line(ok=False, refused="hook-template-missing"))
    else:
        def boom_gate(*_a, **_k):
            raise OSError("no fork")
        monkeypatch.setattr(DR._ctl, "GatedChild", boom_gate)
    rt = make(**kw)
    with pytest.raises(Exception) as e:
        rt.start(r)
    assert not isinstance(e.value, AssertionError)
    assert h.verbs().count("teardown") == 1 and "start" not in h.verbs()
    assert lease.closed == 1                                       # RETAINED proved: the lease is closed
    assert rt.end(SESSION) is True and h.verbs().count("teardown") == 1     # a repeat END is not a second teardown


@pytest.mark.parametrize("bad", [line(ok=True, state="HELD"), line(ok=False, refused="end-busy"), "garbage"])
def test_an_unproven_end_after_a_failure_keeps_the_lease_open(rt_rig, bad):
    h, lease, make = rt_rig
    h.script_("render", line=line(ok=False, refused="bad-value"))
    h.script_("teardown", line=bad)
    rt = make()
    with pytest.raises(Refused):
        rt.start(req())
    assert h.verbs().count("teardown") == 1 and lease.closed == 0   # closing it would hand the session to the reaper
    assert rt.end(SESSION) is False and h.verbs().count("teardown") == 2   # still unproven: asked again, not remembered
    h.script_("teardown", line=line(ok=True, state="RETAINED", released=False))
    assert rt.end(SESSION) is True and lease.closed == 1


def test_a_hung_helper_is_ended_and_refused_by_name(rt_rig):
    h, lease, make = rt_rig
    Path(str(h.script)).write_text("import time\ntime.sleep(60)\n")
    rt = DR.HelperSlotRuntime(host="darwin", helper=h.prefix, store=h.store, routes=("pass-1",),
                              client_factory=lambda _p: lease, deadlines={"prepare": 0.5}, kill_grace_s=0.5)
    with pytest.raises(Refused) as e:
        rt.start(req())
    assert e.value.name == "helper-timeout" and lease.closed == 1


def test_hooks_the_blob_must_name_are_refused_before_anything_is_allocated(rt_rig):
    h, lease, make = rt_rig
    with pytest.raises(L.LauncherRefused) as e:
        make().start(req(hooks=("levain_gate",)))
    assert e.value.name == "hooks-incomplete" and h.verbs() == [] and lease.calls == []


# ---- END is end_by_teardown and nothing else ------------------------------------------------------------------------

def test_end_is_end_by_teardown_once_in_the_source():
    text = Path(DR.__file__).read_text()
    assert text.count("end_by_teardown(") == 1
    assert "teardown_proves(" not in text


def test_end_answers_true_only_on_retained(rt_rig):
    h, _lease, make = rt_rig
    rt = make()
    h.script_("teardown", line=line(ok=True, state="HELD"))
    assert rt.end(SESSION) is False
    h.script_("teardown", line=line(ok=False, refused="session-busy"))
    assert rt.end(SESSION) is False
    h.script_("teardown", line=line(ok=True, state="RETAINED", released=False))
    assert rt.end(SESSION) is True


# ---- the drain ------------------------------------------------------------------------------------------------------

class Firehose:
    """A stream that reads as ``total`` bytes of one shared chunk: no 100 MiB buffer is ever built."""

    def __init__(self, total, chunk=65536):
        self.left, self.chunk = total, b"Y" * chunk

    def read1(self, n):
        if self.left <= 0:
            return b""
        k = min(n, self.left, len(self.chunk))
        self.left -= k
        return self.chunk[:k] if k < len(self.chunk) else self.chunk


def test_the_drain_discards_100_mib_in_bounded_memory():
    total = 100 * 2**20
    d = DR.StreamDrain(Firehose(total), keep=0)
    tracemalloc.start()
    try:
        d.run()
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert d.total == total and d.tail == b"" and peak < 2 * 2**20


def test_the_drain_keeps_only_the_last_bytes_of_stderr():
    d = DR.StreamDrain(Firehose(5 * 2**20), keep=1000)
    d.run()
    assert d.total == 5 * 2**20 and len(d.tail) == 1000 and d.peak_tail <= 1000


def test_a_chatty_vendor_cannot_block_or_forge_the_gate(rt_rig):
    h, _lease, make = rt_rig
    fake_answer = line(ok=True, state="RETAINED")
    h.script_("start", noise=3 * 2**20, err="vendor said " + fake_answer, rc=2)
    rt = make()
    handle = rt.start(req())
    handle.child.release()
    assert handle.child.process.wait(60) == 2                      # the helper's exit status, never stdout
    out_drain, err_drain = rt._drains[SESSION]
    out_drain.thread.join(10)
    assert out_drain.total == 3 * 2**20 and fake_answer.encode() in rt.stderr_tail(SESSION)
    handle.child.abort()


# ---- the control port -----------------------------------------------------------------------------------------------

def test_the_control_port_is_a_fresh_connection_per_call_and_only_mint_cancel_status():
    made = []

    class Client:
        def __init__(self, path, timeout):
            self.closed = False
            made.append(self)

        def call(self, op, **f):
            return {"op": op, **f}

        def close(self):
            self.closed = True

    port = DR.DispatchControlPort("/sock", client_factory=Client)
    assert port.call("RUN-TICKET-STATUS", attempt_id="a") == {"op": "RUN-TICKET-STATUS", "attempt_id": "a"}
    port.call("RUN-TICKET-CANCEL", ticket="t")
    assert len(made) == 2 and all(c.closed for c in made)
    for op in ("LEASE-OPEN", "ATTACH", "KEYS-ATTACH", "SECRETS"):
        with pytest.raises(Refused) as e:
            port.call(op)
        assert e.value.name == "op-not-allowed"
    assert len(made) == 2                                           # a refused op opens no connection


# ---- the REAL Control.handle for LEASE-OPEN{ticket} -----------------------------------------------------------------

class RealLease:
    """A lease connection into the real ``Control.handle`` as the dispatch uid: one ``Conn`` for its whole life."""

    def __init__(self, control):
        self.control, self.conn, self.closed = control, C.Conn(DISPATCH_UID), 0
        self.n = 0

    def call(self, op, **fields):
        self.n += 1
        ans = self.control.handle(self.conn, {"v": 1, "op": op, "id": self.n, **fields})
        if ans.get("ok") is not True:
            raise Refused(ans["refused"], op)
        return {k: v for k, v in ans.items() if k not in ("id", "ok")}

    def close(self):
        self.closed += 1
        if self.conn.lease is not None:
            self.control.lease_eof(self.conn.lease)


def test_start_opens_a_real_lease_on_the_ticket_and_holds_it(st, tmp_path):
    from tests.test_tickets import Clock, Rig, ticket_of

    rig = Rig(st, Clock())
    ticket = ticket_of(rig.mint("a-real"))
    h = Helper(tmp_path, tmp_path / "store")
    leases = []

    def factory(_p):
        leases.append(RealLease(rig.control))
        return leases[-1]

    rt = DR.HelperSlotRuntime(host="darwin", helper=h.prefix, store=tmp_path / "store", routes=("pass-1",),
                              client_factory=factory, kill_grace_s=0.5)
    handle = rt.start(req(ticket))
    try:
        (lease,) = leases
        assert lease.conn.lease is not None and lease.conn.lease.ticket == ticket and lease.conn.lease.live
        assert json.loads(h.calls()[0]["stdin"]) == {"lease_nonce": lease.conn.lease.nonce}   # the nonce CONTROL issued
        # one ticket, one lease: a second start on the same ticket is refused by CONTROL, before any helper verb
        with pytest.raises(Refused) as e:
            rt.start(req(ticket))
        assert e.value.name == "ticket-bound" and h.verbs() == ["prepare", "render"]
        assert rt.end(SESSION) is True and lease.closed == 1 and not lease.conn.lease.live   # proof closed the lease
    finally:
        handle.child.abort()


def test_a_real_lease_refusal_without_a_known_ticket(st, tmp_path):
    from tests.test_tickets import Clock, Rig

    rig = Rig(st, Clock())
    h = Helper(tmp_path, tmp_path / "store")
    rt = DR.HelperSlotRuntime(host="darwin", helper=h.prefix, store=tmp_path / "store", routes=("pass-1",),
                              client_factory=lambda _p: RealLease(rig.control), kill_grace_s=0.5)
    with pytest.raises(Refused) as e:
        rt.start(req("A" * 43))
    assert e.value.name == "ticket-unknown" and h.verbs() == []
