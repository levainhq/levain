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
                                                                    anchor=anchor_path(self.root)))

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
                                now=datetime.now(timezone.utc)) == "not a consent grant"


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
    assert f"{PUSH}  kind push  until" in out and "held inert" in out
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
