"""1027+18 lane idg1: the identity gate's G1 slice (CONTRACT §7.6, §5.1 confirm-identity; 1027-18 RULINGS "Identity
gate" IG-2, IG-3, IG-4 G1, IG-5).

Built on lane F2/2f3's harness (``World`` + the fake launches): the REAL CONTROL over a real state store, owners
recorded, not applied. A login session's ``start`` runs the per-vendor identity call after the vendor exits and
CASes what root observed into the state record; ``confirm-identity yes`` writes ``L/slots/e<n>/baseline`` from that
record only. What only a VM run can show (root's real 0600 file under a real root-owned slot dir) is named where a test
stands in for it.
"""
from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import identity as ID
from levain.slot import records as R
from levain.slot import runtime_helper as H
from levain.slot import supervisor as SV
from levain.slot.ids import SlotId
from tests.test_slot_s2f2_runtime_helper import _blob
from tests.test_slot_s2f3_start import _world
from tests.vendor_tree import vendor_installed, vendor_prefix  # noqa: F401 - autouse, the real vendor check

E0 = SlotId("egress", 0)
GW0 = 898
PASS = ["claude-sub", "github"]


@pytest.fixture
def env(tmp_path):
    return _world(tmp_path)


def _login(w, la, vendor: str = "codex", *, exit_code: int = 0) -> str:
    """A login session through prepare, render and start (the fake vendor exits ``exit_code``)."""
    line = w.prepare("egress", "login", vendor, "passthrough")
    assert line["ok"], line
    s = line["session"]
    w.stage(s)
    w.session = s
    assert w.main(["render", s], _blob(vendor=vendor, routes=PASS))[1]["ok"]
    la.codes["vendor"] = exit_code
    assert w.main(["start", s])[1] == {"ok": True, "exit": exit_code}
    return s


def _baseline(w) -> Path:
    return E0.slot_dir(w.L) / "baseline"


# --- the identity call, run by start for a login session --------------------------------------------------------------

def test_codex_login_records_not_a_check(env):
    """IG-2: Codex has no server-validated call; root records the literal ``not-a-check`` as the observed id."""
    w, la, _ = env
    s = _login(w, la, "codex")
    assert w.state(s).fields["identity"] == {"observed_id": "not-a-check"}
    assert w.steps[-2:] == ["wait", "identity"]
    assert not _baseline(w).exists()                              # start never writes the baseline


def test_claude_login_records_identity_call_unbuilt(env):
    """IG-3 open: the Claude call is a stub that refuses ``identity-call-unbuilt``; the login itself still ran."""
    w, la, _ = env
    s = _login(w, la, "claude")
    assert w.state(s).fields["identity"] == {"result": "identity-call-unbuilt"}
    assert not _baseline(w).exists()


def test_failed_login_observes_nothing(env):
    w, la, _ = env
    s = _login(w, la, "codex", exit_code=1)
    assert w.state(s).fields["identity"] == {"result": "login-exit-nonzero"}


def test_interactive_start_runs_no_identity_step(env):
    w, la, _ = env
    s = w.prepare()["session"]
    w.stage(s)
    w.session = s
    assert w.main(["render", s], _blob())[1]["ok"]
    assert w.main(["start", s])[1]["ok"]
    assert "identity" not in w.steps and "identity" not in w.state(s).fields


def test_identity_call_table():
    assert ID.identity_call(None, "codex") == ID.NOT_A_CHECK
    with pytest.raises(Refused) as e:
        ID.identity_call(None, "claude")
    assert e.value.name == "identity-call-unbuilt"
    assert R.OBSERVED_ID_RE.fullmatch(ID.NOT_A_CHECK)


# --- confirm-identity --------------------------------------------------------------------------------------------------

def test_confirm_yes_writes_the_baseline_from_the_record(env):
    """§5.1 confirm-identity yes: root writes ``levain-baseline/1`` {vendor, observed_id, confirmed_at, session} from
    the state record's root-captured id, mode 0600 (root-owned on a real install: a VM row), and records the
    confirmation in the state record."""
    w, la, _ = env
    s = _login(w, la, "codex")
    n = len(w.calls)
    assert w.main(["confirm-identity", s, "yes"])[1] == {"ok": True}
    data = _baseline(w).read_bytes()
    assert canon.parse(data) == {"format": "levain-baseline/1", "vendor": "codex", "observed_id": "not-a-check",
                                 "confirmed_at": R.timestamp(w.seams.clock()), "session": s}
    assert canon.canonical_bytes(canon.parse(data)) == data
    assert os.stat(_baseline(w)).st_mode & 0o7777 == 0o600
    assert not (E0.slot_dir(w.L) / "baseline.tmp").exists()
    assert w.state(s).fields["identity"] == {"observed_id": "not-a-check", "result": "confirmed"}
    (cas,) = [f for op, f in w.calls[n:] if op == "CAS"]
    assert (cas["from"], cas["to"]) == (R.ACTIVE, R.ACTIVE)       # field-only: CONTROL checks the lease is live
    view = H.read_view(H.Store(w.seams), E0)
    assert view.baseline == "codex"


def test_confirm_no_writes_nothing(env):
    w, la, _ = env
    s = _login(w, la, "codex")
    before = w.state(s)
    assert w.main(["confirm-identity", s, "no"])[1] == {"ok": True}
    assert not _baseline(w).exists()
    assert w.state(s) == before


def test_claude_confirm_yes_refused_without_an_observed_id(env):
    """A Claude login reaches confirm with no observed id: yes is refused by name, nothing written."""
    w, la, _ = env
    s = _login(w, la, "claude")
    line = w.main(["confirm-identity", s, "yes"])[1]
    assert line == {"ok": False, "refused": "no-observed-id:identity-call-unbuilt"}
    assert not _baseline(w).exists()


def test_failed_login_confirm_yes_refused(env):
    w, la, _ = env
    s = _login(w, la, "codex", exit_code=2)
    assert w.main(["confirm-identity", s, "yes"])[1]["refused"] == "no-observed-id:login-exit-nonzero"
    assert not _baseline(w).exists()


def test_confirm_refused_for_a_non_login_kind(env):
    w, la, _ = env
    s = w.prepare()["session"]
    w.stage(s)
    w.session = s
    assert w.main(["render", s], _blob())[1]["ok"]
    assert w.main(["start", s])[1]["ok"]
    assert w.main(["confirm-identity", s, "yes"])[1]["refused"] == "confirm-kind:interactive"
    assert w.main(["confirm-identity", s, "no"])[1]["refused"] == "confirm-kind:interactive"
    assert not _baseline(w).exists()


def test_confirm_refused_for_dispatch(env):
    """§5.1 the ``_levain_dispatch`` rule excepts confirm-identity (the grammar refuses it before any read)."""
    w, la, _ = env
    s = _login(w, la, "codex")
    assert w.main(["confirm-identity", s, "yes"], uid=799)[1]["refused"] == "verb-not-allowed-for-caller"
    assert not _baseline(w).exists()


def test_confirm_refused_before_start(env):
    """No identity has been observed until the login ran: a RENDERED session is refused."""
    w, la, _ = env
    line = w.prepare("egress", "login", "codex", "passthrough")
    s = line["session"]
    w.stage(s)
    assert w.main(["render", s], _blob(vendor="codex", routes=PASS))[1]["ok"]
    assert w.main(["confirm-identity", s, "yes"])[1]["refused"] == "not-active:RENDERED"
    assert not _baseline(w).exists()


def test_confirm_after_lease_eof_refused(env):
    """The lease is the session's: once its EOF revoked the record, confirm writes nothing."""
    w, la, _ = env
    s = _login(w, la, "codex")
    w.control.lease_eof(w.conn.lease)
    w.control.join_eof(10)
    assert w.main(["confirm-identity", s, "yes"])[1]["refused"] == "not-active:REVOKED"
    assert not _baseline(w).exists()


def test_confirm_with_the_lease_dead_but_the_record_still_active_refused(env):
    """CONTROL's own check: a record still ACTIVE whose lease is gone refuses the field-only CAS ``lease-dead``, so
    root writes nothing."""
    w, la, _ = env
    s = _login(w, la, "codex")
    real = w.control._lease_live
    w.control._lease_live = lambda session: False if session == s else real(session)
    assert w.main(["confirm-identity", s, "yes"])[1]["refused"] == "lease-dead"
    assert not _baseline(w).exists()


def test_held_login_session_cannot_confirm(env):
    """IG-5: a HELD login session (END not proven) is past live and its lease is gone: confirm is refused."""
    w, la, table = env
    s = _login(w, la, "codex")
    for pid in w.fronts.alive():
        table.add(pid, GW0)
    table.add(70001, 899, immortal=True, cg=True)
    assert w.main(["teardown", s])[1] == {"ok": True, "state": R.HELD}
    assert w.main(["confirm-identity", s, "yes"])[1]["refused"] == "not-active:HELD"
    assert not _baseline(w).exists()


def test_confirm_refused_when_the_slot_left_the_lease(env):
    w, la, _ = env
    s = _login(w, la, "codex")
    st = w.slot_state(E0)
    (E0.slot_dir(w.L) / "state").write_bytes(R.slot_state_bytes(
        R.SlotState(E0, st.rev + 1, st.lease_seq + 1, "leased", True, "2026-10-20T00:00:00Z", "s-00000000000000ff")))
    assert w.main(["confirm-identity", s, "yes"])[1]["refused"] == "slot-lease-moved"
    assert not _baseline(w).exists()


def test_a_crash_between_the_cas_and_the_write_is_finished_by_a_second_yes(env, monkeypatch):
    w, la, _ = env
    s = _login(w, la, "codex")
    real = H.Store.replace

    def boom(self, parts, name, data, *, mode):
        raise OSError(5, "EIO")
    monkeypatch.setattr(H.Store, "replace", boom)
    assert w.main(["confirm-identity", s, "yes"])[1]["refused"] == "internal"
    assert not _baseline(w).exists()
    assert w.state(s).fields["identity"]["result"] == "confirmed"
    monkeypatch.setattr(H.Store, "replace", real)
    assert w.main(["confirm-identity", s, "yes"])[1] == {"ok": True}
    assert canon.parse(_baseline(w).read_bytes())["session"] == s


def test_the_slot_cannot_supply_the_observed_id(env):
    """Root takes the id from CONTROL's record only: whatever the slot left in its own home names nothing."""
    w, la, _ = env
    s = _login(w, la, "codex")
    home = w.L / "live" / s
    (home / "config" / "codex" / "auth.json").write_text('{"tokens":{"account_id":"attacker"}}')
    assert w.main(["confirm-identity", s, "yes"])[1]["ok"]
    assert canon.parse(_baseline(w).read_bytes())["observed_id"] == "not-a-check"


# --- the baseline reader -----------------------------------------------------------------------------------------------

def _bl(**kw) -> bytes:
    obj = {"format": "levain-baseline/1", "vendor": "codex", "observed_id": "not-a-check",
           "confirmed_at": "2026-10-27T00:00:00Z", "session": "s-0000000000000001"}
    obj.update(kw)
    return canon.canonical_bytes({k: v for k, v in obj.items() if v is not None})


@pytest.mark.parametrize("data,want", [
    (None, None),
    (_bl(), "codex"),
    (_bl(vendor="claude", observed_id="acct-1"), "claude"),
    # the contract's earlier vendor-only shape (no ``format``): its vendor is still read
    (canon.canonical_bytes({"vendor": "claude", "account_id": "acct-1", "confirmed_at": "2026-10-20T00:00:00Z"}),
     "claude"),
    (canon.canonical_bytes({"vendor": "codex"}), "codex"),
    (_bl(vendor="gemini"), "?"),
    (_bl(observed_id="has space"), "?"),
    (_bl(observed_id=None), "?"),
    (_bl(session="nope"), "?"),
    (_bl(confirmed_at="yesterday"), "?"),
    (_bl(extra="x"), "?"),
    (_bl(format="levain-baseline/2"), "?"),
    (canon.canonical_bytes({"vendor": "gemini"}), "?"),
    (b"not json", "?"),
    (b"[]", "?"),
])
def test_baseline_vendor_reads_both_shapes(data, want):
    assert H._baseline_vendor(data) == want


def test_a_confirmed_baseline_makes_the_slot_hold_the_vendor(env):
    """With the parked credential in ``keep/`` (G2/END's), the pick finds the slot by credential + baseline."""
    w, la, _ = env
    s = _login(w, la, "codex")
    assert w.main(["confirm-identity", s, "yes"])[1]["ok"]
    (E0.slot_dir(w.L) / "keep" / H.CREDENTIAL_NAME["codex"]).write_bytes(b"{}")
    assert H.read_view(H.Store(w.seams), E0).holds("codex")


# --- the supervisor's call site ----------------------------------------------------------------------------------------

class _Sup(SV.Supervisor):
    def __init__(self, vendor: str, answer):
        super().__init__(SV.Request(vendor=vendor, repo=Path("/nonexistent"), auth="passthrough", kind="login"),
                         SV.Seams(host="linux", helper=("helper",), store=Path("/L"), control_socket=Path("/c"),
                                  base_map=Path("/b"), client_factory=lambda p: None,
                                  confirm=lambda prompt: (self.prompts.append(prompt), answer)[1]))
        self.prompts: list[str] = []
        self.calls: list[tuple] = []
        self.said: list[str] = []
        self.reply: object = {}
        self._vendor_exit = 0

    def helper(self, *args, stdin=b"", stoppable=True):
        self.calls.append(args)
        if isinstance(self.reply, Refused):
            raise self.reply
        return self.reply

    def say(self, msg):
        self.said.append(msg)


def test_supervisor_codex_yes_calls_confirm_yes():
    sup = _Sup("codex", True)
    sup._confirm_identity("s-0000000000000001")
    assert sup.calls == [("confirm-identity", "s-0000000000000001", "yes")]
    assert "not a check" in sup.prompts[0]


def test_supervisor_codex_no_calls_confirm_no():
    sup = _Sup("codex", False)
    sup._confirm_identity("s-0000000000000001")
    assert sup.calls == [("confirm-identity", "s-0000000000000001", "no")]


def test_ctrlc_at_confirm_no_baseline():
    """§5.1's T:2/test_ctrlc_at_confirm_no_baseline at the call site: no answer (a stop signal, EOF, the deadline)
    sends no confirm at all, so no baseline can be written."""
    sup = _Sup("codex", None)
    sup._confirm_identity("s-0000000000000001")
    assert sup.calls == []
    sup = _Sup("codex", True)
    sup.stop_signal = signal.SIGINT
    sup._confirm_identity("s-0000000000000001")
    assert sup.calls == [] and sup.prompts == []


def test_supervisor_claude_is_not_asked():
    """IG-3 open: no Claude identity call exists, so the operator is told and nothing is confirmed."""
    sup = _Sup("claude", True)
    sup._confirm_identity("s-0000000000000001")
    assert sup.calls == [] and sup.prompts == []
    assert any("identity-call-unbuilt" in m for m in sup.said)


def test_supervisor_confirm_refusal_is_said_not_raised():
    sup = _Sup("codex", True)
    sup.reply = Refused("lease-dead", "x")
    sup._confirm_identity("s-0000000000000001")
    assert any("lease-dead" in m for m in sup.said)


def test_supervisor_failed_login_is_not_asked():
    sup = _Sup("codex", True)
    sup._vendor_exit = 1
    sup._confirm_identity("s-0000000000000001")
    assert sup.calls == [] and sup.prompts == []


def test_ask_reads_one_line_from_a_terminal(tmp_path):
    """The real prompt on a pty: ``y`` is yes, anything else no, EOF none."""
    import io
    for typed, want in ((b"y\n", True), (b"YES\n", True), (b"n\n", False), (b"sure\n", False)):
        master, slave = os.openpty()
        try:
            sup = SV.Supervisor(SV.Request(vendor="codex", repo=tmp_path, auth="passthrough", kind="login"),
                                SV.Seams(host="linux", helper=("helper",), store=Path("/L"), control_socket=Path("/c"),
                                         base_map=Path("/b"), client_factory=lambda p: None, stdin=slave,
                                         err=io.StringIO()))
            os.write(master, typed)
            assert sup._ask("q? ") is want
            assert sup.err.getvalue() == "q? "
        finally:
            os.close(master)
            os.close(slave)


# --- IG-3: the Claude profile call's two halves (not wired to a wall target yet) -------------------------------------

import http.server
import json
import threading

from levain.slot import identity_child as IC

UUID = "4f6c1a2e-0b7d-4c9e-8a31-2d5e6f708192"


class _Profile(http.server.BaseHTTPRequestHandler):
    status = 200
    body = json.dumps({"account": {"uuid": UUID, "email": "x@example.com"}, "organization": {"uuid": "o"}})
    seen: list = []

    def do_GET(self):
        type(self).seen.append((self.path, dict(self.headers)))
        data = self.body.encode()
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def profile_server():
    _Profile.seen = []
    _Profile.status = 200
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Profile)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}/api/oauth/profile"
    srv.shutdown()
    srv.server_close()


def _creds(tmp_path, token="tok-SECRET-123"):
    cfg = tmp_path / "claude"
    cfg.mkdir()
    (cfg / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}))
    return {"CLAUDE_CONFIG_DIR": str(cfg)}


def _run_child(env, url):
    """The child's main (its exit and stdout line) aimed at ``url`` instead of the real profile endpoint."""
    import io
    out = io.StringIO()
    real = IC.fetch
    IC.fetch = lambda e, **kw: real(e, url=url)
    try:
        code = IC.main(env=env, out=out)
    finally:
        IC.fetch = real
    return code, out.getvalue().encode()


def test_child_sends_the_bearer_and_beta_and_root_takes_the_uuid(tmp_path, profile_server):
    code, out = _run_child(_creds(tmp_path), profile_server)
    ((path, headers),) = _Profile.seen
    assert path == "/api/oauth/profile"
    h = {k.lower(): v for k, v in headers.items()}
    assert h["authorization"] == "Bearer tok-SECRET-123" and h["anthropic-beta"] == IC.OAUTH_BETA
    assert b"tok-SECRET-123" not in out                            # the token never reaches stdout
    assert ID.parse_profile_answer(code, out) == UUID


@pytest.mark.parametrize("status", [401, 403, 500])
def test_non_200_is_unparsable(tmp_path, profile_server, status):
    _Profile.status = status
    code, out = _run_child(_creds(tmp_path), profile_server)
    assert json.loads(out) == {"status": status, "body": ""}
    with pytest.raises(Refused) as e:
        ID.parse_profile_answer(code, out)
    assert e.value.name == "identity-unparsable"


@pytest.mark.parametrize("body", ["not json", json.dumps({"account": {}}), json.dumps({"account": {"uuid": "a b"}}),
                                  json.dumps({"account": {"uuid": 7}}), json.dumps([]), json.dumps({"uuid": UUID})])
def test_a_bad_profile_body_is_unparsable(tmp_path, profile_server, body, monkeypatch):
    monkeypatch.setattr(_Profile, "body", body)
    code, out = _run_child(_creds(tmp_path), profile_server)
    with pytest.raises(Refused) as e:
        ID.parse_profile_answer(code, out)
    assert e.value.name == "identity-unparsable"


def test_no_credential_no_request(tmp_path, profile_server):
    code, out = _run_child({"CLAUDE_CONFIG_DIR": str(tmp_path)}, profile_server)
    assert (code, json.loads(out)) == (1, {"error": "no-token"}) and _Profile.seen == []
    with pytest.raises(Refused):
        ID.parse_profile_answer(code, out)


def test_an_unreachable_server_names_no_token(tmp_path):
    code, out = _run_child(_creds(tmp_path), "http://127.0.0.1:9/api/oauth/profile")
    assert (code, json.loads(out)) == (1, {"error": "request-failed"})


@pytest.mark.parametrize("code,out", [(0, b"x" * (64 * 1024 + 1)), (1, b'{"status":200,"body":"{}"}'),
                                      (0, b"\xff"), (0, b'{"body":"{}"}'), (0, b'{"status":"200","body":"{}"}')])
def test_root_parser_refuses_the_rest(code, out):
    with pytest.raises(Refused) as e:
        ID.parse_profile_answer(code, out)
    assert e.value.name == "identity-unparsable"


def test_claude_call_still_refuses_until_wired():
    with pytest.raises(Refused) as e:
        ID.identity_call(None, "claude")
    assert e.value.name == "identity-call-unbuilt"
