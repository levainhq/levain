"""M17 S-a: a standing grant revoked by the phone's passkey (the one authority link a passkey may sign), and the
read the cockpit page and the phone show (each grant, what it answered, and a revoke line whose id is one shell word).
Real ``ssh-keygen`` signs the laptop's links and verifies every link, the passkey's included; the passkey is the
dispatch tests' software authenticator (:class:`tests.test_dispatch_phone.SoftPasskey`)."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain import authority as auth
from levain.cockpit.firejournal import CockpitDB
from levain.cockpit.keys import fingerprint_of
from levain.consent import policy
from levain.consent.answering import passkey_keys
from levain.consent.grant_phone import GrantPhone, GrantRefused
from levain.consent.grants import create_grant, grant_listing
from levain.consent.ledger import Ledger
from levain.consent.passkey import challenge
from levain.consent.queue import ConsentQueue, authority_source
from tests.test_dispatch_phone import ORIGIN, RP, SoftPasskey, _b64u
from tests.test_phone_server import _enrol

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

HOSTILE = "x; touch /tmp/pwned $(id) `id` 'q' -n"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def world(tmp_path: Path):
    laptop = tmp_path / "laptop"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "laptop", "-f", str(laptop)], check=True)
    lpub = " ".join((tmp_path / "laptop.pub").read_text().split()[:2])
    lfp = fingerprint_of(lpub)
    root = tmp_path / "cockpit"
    root.mkdir()
    CockpitDB(root).set_install_class("reported")
    phone = SoftPasskey()
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    _enrol(conn, root, laptop, lfp, {"op": "enrol", "fingerprint": lfp, "public_key": lpub,
                                     "class": "laptop-presence", "custody": "passphrase"})
    _enrol(conn, root, laptop, lfp, {"op": "enrol", "fingerprint": phone.fp, "public_key": phone.line,
                                     "class": "display-phone", "custody": "passkey"})
    conn.close()
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    os.chmod(qdir, 0o700)
    (qdir / "claude_code.json").write_text(json.dumps({"cockpit_data_root": str(root), "away_mode": False,
                                                       "away_hold_seconds": 0}))
    ws = Path(os.path.realpath(tmp_path / "proj"))
    ws.mkdir()

    def laptop_sign(m: bytes) -> str:
        return subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(laptop), "-n", auth.NAMESPACE], input=m,
                              capture_output=True, check=True).stdout.decode()

    def grant(gid: str) -> str:
        create_grant(root, agent="claude_code", tools=["Write"], workspace=ws, postures=["default"],
                     expires=timedelta(days=1), signer_fp=lfp, sign=laptop_sign, clock=_now, grant_id=gid)
        return gid

    source = authority_source(root / "cockpit.db", anchor=auth.anchor_path(root))
    gp = GrantPhone(qdir, RP, ORIGIN, lambda: {k.fingerprint: k for k in passkey_keys(source(), RP)}, lambda: root)
    queue = ConsentQueue(qdir, authority=source)

    def ask(name: str):
        tool_input = {"file_path": str(ws / name), "content": "x"}
        r = queue.file(agent="claude_code", agent_session_id="s1", tool_name="Write", tool_input=tool_input,
                       cwd=str(ws), hold=timedelta(minutes=5))
        return r, policy.guarded_policy(r, queue, posture="default", tool_input=tool_input)

    return SimpleNamespace(root=root, qdir=qdir, phone=phone, lfp=lfp, grant=grant, gp=gp, ask=ask, ws=ws)


def test_a_passkey_revokes_a_grant_from_the_phone_and_signs_no_other_link(world) -> None:
    """Reproduced red before the authority rule widened: the challenge was refused 409 "a passkey key signs consent
    answers only, never an authority link" (prepare runs the replay's rule)."""
    gid = world.grant("cg-0123456789ab")
    r, d = world.ask("a.txt")
    assert d is not None and d.via == "grant" and d.grant_id == gid
    gp, phone = world.gp, world.phone
    # a surface-token holder's assertion over OTHER bytes: the held link is refused by the log's own verify
    gp.step(gid, "revoke-challenge", {"signer_fp": phone.fp})
    forged = phone.assertion(_b64u(challenge(b"some other message", auth.NAMESPACE)))
    with pytest.raises(GrantRefused) as exc:
        gp.step(gid, "revoke", forged)
    assert exc.value.status == 403 and "does not verify" in exc.value.message
    assert [g.id for g in grant_listing(world.root, now=_now())[0]] == [gid]
    # the real ceremony: what is shown (``line``) is the link that lands
    c = gp.step(gid, "revoke-challenge", {"signer_fp": phone.fp})
    assert json.loads(c["line"])["op"] == "revoke_grant" and json.loads(c["line"])["grant_id"] == gid
    got = gp.step(gid, "revoke", phone.assertion(c["challenge"]))
    assert got["revoked"] == gid and got["warning"] == ""
    conn = sqlite3.connect(world.root / "cockpit.db", isolation_level=None)
    try:
        snap = auth.snapshot(conn, cache=auth.ReplayCache())          # a fresh replay verifies every link again
        assert snap.seq == got["seq"] and gid not in snap.policy.grants
        # every other op a passkey signs is still refused, with the old words
        for op in ({"op": "enrol", "fingerprint": world.lfp, "public_key": "x", "class": "presence",
                    "custody": "passphrase"},
                   {"op": "create_grant", "grant": {"id": "g2", "kind": "consent", "agent": "claude_code",
                                                    "tools": ["Write"], "workspace": str(world.ws),
                                                    "postures": ["default"],
                                                    "expires_at": auth._stamp(_now() + timedelta(days=1))}},
                   {"op": "raise", "verb": "spore_touch", "tier": "T3"}):
            with pytest.raises(auth.AuthorityError, match="a passkey key signs consent answers only, never an "
                                                          "authority link"):
                auth.prepare(conn, op, signer_fp=phone.fp, now=_now(), cache=auth.ReplayCache())
    finally:
        conn.close()
    # the grant answers nothing from the next decision on, and a used challenge cannot be answered twice
    assert world.ask("b.txt")[1] is None
    with pytest.raises(GrantRefused) as exc:
        gp.step(gid, "revoke", phone.assertion(c["challenge"]))
    assert exc.value.status == 409


def test_the_grants_read_shows_what_each_grant_answered_and_a_revoke_line_of_one_id(world, monkeypatch) -> None:
    """Reproduced red before the route: GET /cockpit/consent/grants.json was 404 "no such consent request listed"."""
    import levain.web_server as wsrv

    gid = world.grant(HOSTILE)
    world.grant("cg-quiet0000000")
    r, d = world.ask("a.txt")
    assert d is not None and d.grant_id in (gid, "cg-quiet0000000")
    answered_by = d.grant_id
    ledger = Ledger(world.qdir)
    for phase in ("pre", "post"):
        ledger.record_call(agent="claude_code", agent_session_id="s1", tool_name="Write",
                           tool_input={"file_path": str(world.ws / "a.txt"), "content": "x"}, cwd=str(world.ws),
                           phase=phase, tool_use_id="tu-1", stamp="grant", request_id=r.id if phase == "pre" else None)

    def route(token: bool) -> tuple[int, dict]:
        h = wsrv._Handler.__new__(wsrv._Handler)
        sent: list = []
        h.server = SimpleNamespace(request_gate=threading.Lock(), consent_reader=wsrv._ConsentReader(world.qdir))
        h._off_box_token_valid = lambda: token
        h._send = lambda body, ctype, **kw: sent.append((kw.get("status", 200), json.loads(body)))
        h._serve_consent_body("/cockpit/consent/grants.json", head=False)
        return sent[0]

    status, body = route(False)
    assert status == 403 and "grants" not in body
    status, body = route(True)
    assert status == 200 and body["note"] == "" and body["root"] == str(world.root)
    rows = {g["id"]: g for g in body["grants"]}
    assert set(rows) == {gid, "cg-quiet0000000"} and all(g["state"] == "live" for g in rows.values())
    hit, other = rows[answered_by], rows[next(k for k in rows if k != answered_by)]
    assert hit["answered"]["count"] == 1 and other["answered"] == {"count": 0, "last_at": "", "recent": []}
    assert [(c["request_id"], c["tool"], c["outcome"]) for c in hit["answered"]["recent"]] == [(r.id, "Write",
                                                                                                "accepted")]
    words = shlex.split(rows[gid]["revoke"])
    assert words[-2:] == ["--", HOSTILE] and words[words.index("--data-root") + 1] == str(world.root)
    # the phone's listing: the same rows without the terminal line, with the passkeys that may revoke
    phone_rows = world.gp.listing()
    assert phone_rows["grants_note"] == "" and all("revoke" not in g and g["signers"] == [world.phone.fp]
                                                   for g in phone_rows["grants"])
