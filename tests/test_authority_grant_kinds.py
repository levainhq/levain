"""Grant kinds dispatch through one registry, and a kind this levain does not know is held INERT (head ruling
1017+4, lane E's DESIGN §4): replayed, revocable, answering nothing, its changes never narrower than a renew. Each case
is from the 1017+4 lane A RUN: a newer writer (this build with a ``push`` kind registered) signs a push grant into a
real anchored log; a fresh reader replays it, its consent grant still answers, its own acts on the push grant are
judged."""
from __future__ import annotations

import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType

import pytest

import levain.authority as auth
from levain.authority import ReplayCache, anchor_path
from levain.cli import main
from levain.cockpit.consent import utc_stamp
from levain.cockpit.firejournal import CockpitDB
from levain.consent import policy
from levain.consent.queue import ConsentQueue, authority_source
from tests._h4 import h4_home

READ = "mcp__fsw__read_text_file"
PUSH = "pg-0123456789ab"


class Scratch:
    """A reported cockpit whose anchored log enrols one laptop-presence key, a workspace, and the hook's queue."""

    def __init__(self, tmp_path: Path) -> None:
        keys = tmp_path / "keys"
        keys.mkdir()
        self.key = keys / "laptop"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "laptop", "-f", str(self.key)],
                       check=True, capture_output=True, timeout=30)
        pub = " ".join((keys / "laptop.pub").read_text().split()[:2])
        self.fp = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(self.key)], check=True,
                                 capture_output=True, text=True).stdout.split()[1]
        self.root = tmp_path / "cockpit"
        self.root.mkdir()
        CockpitDB(self.root).set_install_class("reported")
        self.ws = Path(os.path.realpath(tmp_path / "proj"))
        self.ws.mkdir()
        self.link({"op": "enrol", "fingerprint": self.fp, "public_key": pub, "class": "laptop-presence",
                   "custody": "passphrase"})
        qdir = tmp_path / "home" / "consent"
        qdir.mkdir(parents=True, mode=0o700)
        self.queue = ConsentQueue(qdir, authority=authority_source(self.root / "cockpit.db",
                                                                    anchor=anchor_path(self.root)), home=h4_home())

    def sign(self, m: bytes) -> str:
        return subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(self.key), "-n", auth.NAMESPACE],
                              input=m, capture_output=True, check=True).stdout.decode()

    def link(self, op, *, at: datetime | None = None):
        conn = sqlite3.connect(self.root / "cockpit.db", isolation_level=None)
        try:
            return auth.sign_and_append(conn, op, signer_fp=self.fp, sign=self.sign,
                                        clock=lambda: at or datetime.now(timezone.utc), cache=ReplayCache(),
                                        anchor=anchor_path(self.root))
        finally:
            conn.close()

    def state(self) -> auth.AuthoritySnapshot:
        conn = sqlite3.connect(self.root / "cockpit.db", isolation_level=None)
        try:
            return auth.snapshot(conn, cache=ReplayCache())
        finally:
            conn.close()

    def ask(self, tool_input, *, tool: str = READ):
        r = self.queue.file(agent="claude_code", agent_session_id="s", tool_name=tool, tool_input=tool_input,
                            cwd=str(self.ws), hold=timedelta(minutes=5))
        return policy.guarded_policy(r, self.queue, posture="default", tool_input=tool_input)


def _push(ws: Path, gid: str = PUSH, hours: int = 2) -> dict:
    """Lane E's push record shape (DESIGN §2)."""
    return {"id": gid, "kind": "push", "workspace": str(ws), "remote_url": "file:///tmp/nowhere.git",
            "branches": ["claude/*"], "expires_at": utc_stamp(datetime.now(timezone.utc) + timedelta(hours=hours))}


@pytest.fixture
def log(tmp_path, monkeypatch, capsys):
    """A consent grant on READ, then a push grant signed by a newer writer (``push`` registered only for that
    link): what a newer levain leaves in a log this build then reads."""
    s = Scratch(tmp_path)
    assert main(["consent", "grant", "create", "--data-root", str(s.root), "--agent", "claude_code", "--tool", READ,
                 "--workspace", str(s.ws), "--posture", "default", "--expires", "2h", "--id", "undeclared-read",
                 "--signing-key", str(s.key)]) == 0
    with monkeypatch.context() as m:
        m.setattr(auth, "_GRANT_KINDS", MappingProxyType({**auth._GRANT_KINDS, "push": auth._GrantKind(
            validate=auth._validate_inert_grant, change=auth._inert_grant_change)}))
        s.link({"op": "create_grant", "grant": _push(s.ws)})
    capsys.readouterr()
    return s


def test_a_log_holding_an_unknown_kind_replays_and_that_grant_answers_nothing(log):
    state = log.state()
    assert sorted(state.policy.grants) == [PUSH, "undeclared-read"]
    push = state.policy.grants[PUSH]
    assert auth.inert_grant(push) and not auth.consent_grant(push)
    request = log.queue.file(agent="claude_code", agent_session_id="s", tool_name=READ,
                             tool_input={"path": str(log.ws / "a")}, cwd=str(log.ws), hold=timedelta(minutes=5))
    assert policy.grant_refusal(push, request, tool_input={"path": str(log.ws / "a")}, posture="default",
                                now=datetime.now(timezone.utc), home=h4_home()) == "not a consent grant"


def test_every_other_grant_still_answers(log):
    d = log.ask({"path": str(log.ws / "README.md")})
    assert d is not None and (d.decision, d.grant_id) == ("allow", "undeclared-read")
    assert log.ask({"path": "/etc/hosts"}) is None


def test_the_inert_grant_renews_and_revokes_but_is_never_created_or_changed_here(log, capsys):
    t = datetime.now(timezone.utc)
    log.link({"op": "renew", "grant_id": PUSH, "expires_at": utc_stamp(t + timedelta(hours=3)),
              "signed_at": utc_stamp(t)}, at=t)
    pol = auth._thaw(log.state().policy.as_dict())
    pol["grants"][PUSH]["branches"] = ["claude/*", "main"]
    with pytest.raises(auth.AuthorityError, match="does not know the grant kind 'push'"):
        log.link({"op": "loosen", "policy": pol})
    with pytest.raises(auth.AuthorityError, match="does not know the grant kind 'push'"):
        log.link({"op": "create_grant", "grant": _push(log.ws, gid="pg-ffffffffffff")})
    pol = auth._thaw(log.state().policy.as_dict())
    pol["grants"]["undeclared-read"]["postures"] = ["acceptEdits", "default"]
    log.link({"op": "loosen", "policy": pol})            # the push grant carried unchanged
    assert main(["consent", "grant", "list", "--data-root", str(log.root)]) == 0
    out = capsys.readouterr().out
    assert f"{PUSH!r}  kind push  until" in out and "held inert" in out
    assert main(["consent", "grant", "revoke", PUSH, "--data-root", str(log.root), "--signing-key",
                 str(log.key)]) == 0
    assert PUSH not in log.state().policy.grants
    assert log.ask({"path": str(log.ws / "README.md")}) is not None


def _policy(**grants):
    return auth.StandingPolicy.from_dict({"raise_to": {}, "timeouts": {}, "grants": grants})


def test_an_unknown_kind_classifies_generically(tmp_path):
    g = _push(tmp_path)
    later = dict(g, expires_at=utc_stamp(datetime.now(timezone.utc) + timedelta(hours=5)))
    narrower = dict(g, branches=[])
    assert auth.classify(_policy(), _policy(pg=g)) == auth.LOOSEN
    assert auth.classify(_policy(pg=g), _policy(pg=later)) == auth.RENEW
    assert auth.classify(_policy(pg=g), _policy(pg=narrower)) == auth.LOOSEN     # never tight: not ours to judge
    assert auth.classify(_policy(pg=g), _policy()) == auth.GRANT_REVOKE


@pytest.mark.parametrize("over, said", [
    ({"kind": "Push"}, "grant kind name"), ({"kind": ""}, "grant kind name"), ({"kind": 7}, "grant kind name"),
    ({"id": ""}, ".id"), ({"expires_at": "soon"}, "not a timestamp"),
    ({"expires_at": utc_stamp(datetime(2026, 12, 1, tzinfo=timezone.utc))}, "at most 30 days"),
    ({"branches": [float("nan")]}, "finite JSON"),
])
def test_an_inert_grant_s_envelope_is_checked(tmp_path, over, said):
    g = dict(_push(tmp_path), **over)
    with pytest.raises(auth.AuthorityError, match=said):
        auth._validate_grant(g, "grant", at=datetime(2026, 10, 17, tzinfo=timezone.utc))


def test_a_known_kind_stays_closed(tmp_path):
    g = {"id": "g", "kind": "consent", "agent": "claude_code", "tools": ["Read"], "workspace": str(tmp_path),
         "postures": ["default"], "expires_at": utc_stamp(datetime(2026, 10, 18, tzinfo=timezone.utc)), "extra": 1}
    with pytest.raises(auth.AuthorityError, match="must carry exactly"):
        auth._validate_grant(g, "grant")


def test_a_tighten_declared_change_of_an_inert_grant_is_refused_at_replay(log, monkeypatch):
    """A newer writer that narrows a grant of a kind this reader does not know, under an op declaring TIGHTEN, is
    read here as a loose change: the op/delta mismatch stops the log at that seq (fail-closed; the rule is on
    :func:`levain.authority._inert_grant_change`)."""
    def narrow_as_tight(o, n, o_exp, n_exp):
        return "tight" if len(n["branches"]) < len(o["branches"]) else auth._inert_grant_change(o, n, o_exp, n_exp)

    with monkeypatch.context() as m:
        m.setitem(auth._OP_FIELDS, "narrow", auth._OP_FIELDS["loosen"])
        m.setitem(auth._OP_DELTAS, "narrow", {auth.TIGHTEN})
        real = auth._apply_policy_op
        m.setattr(auth, "_apply_policy_op", lambda p, rec: real(p, dict(rec, op="loosen")) if rec["op"] == "narrow"
                  else real(p, rec))
        with monkeypatch.context() as w:                  # the newer writer knows push, and calls a narrowing tight
            w.setattr(auth, "_GRANT_KINDS", MappingProxyType({**auth._GRANT_KINDS, "push": auth._GrantKind(
                validate=auth._validate_inert_grant, change=narrow_as_tight)}))
            pol = auth._thaw(log.state().policy.as_dict())
            pol["grants"][PUSH]["branches"] = []
            log.link({"op": "narrow", "policy": pol})
        with pytest.raises(auth.ReplayError, match="narrow link computes to a loosen change"):
            log.state()


def test_one_grant_list_is_one_snapshot(log, monkeypatch):
    """``consent grant list`` partitions ONE capture into (consent, inert): a link landing between two captures
    cannot show a grant twice or not at all."""
    from levain.consent import grants as g

    calls = []
    real = g._capture
    monkeypatch.setattr(g, "_capture", lambda *a, **k: calls.append(1) or real(*a, **k))
    consent, inert = g.grant_listing(log.root, now=datetime.now(timezone.utc))
    assert [c.id for c in consent] == ["undeclared-read"] and [i[0] for i in inert] == [PUSH]
    assert len(calls) == 1
    calls.clear()
    g.list_grants(log.root, now=datetime.now(timezone.utc))
    g.list_inert_grants(log.root, now=datetime.now(timezone.utc))
    assert len(calls) == 2                                # each public function is one capture of its own


def test_an_inert_grant_id_is_printed_as_a_repr(log, monkeypatch, capsys):
    from levain.consent import grants as g
    real = g.grant_listing
    monkeypatch.setattr("levain.consent.grants.grant_listing", lambda *a, **k: (
        lambda c, i: (c, [("bad\x1b[31mid", *i[0][1:])]))(*real(*a, **k)))
    assert main(["consent", "grant", "list", "--data-root", str(log.root)]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "'bad\\x1b[31mid'" in out


def test_a_replay_key_error_names_the_missing_key(log, monkeypatch):
    def boom(*a, **k):
        raise KeyError("branches")
    monkeypatch.setattr(auth, "_apply_op", boom)
    with pytest.raises(auth.ReplayError, match=r"malformed field \(missing key 'branches'\)"):
        log.state()
