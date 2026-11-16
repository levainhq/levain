"""Root's host-grant verbs (board levain#66 slice S2, plan hgs2_plan_v2): ``egress-grant``, ``egress-grant-revoke``,
``egress-refusals``, ``egress-grants-list``, driven through :func:`runtime_helper.main` on a scratch store and run dir.

The signature check is replaced by a spy on ``egress_verify.verify_link_sig`` (a real ``ssh-keygen`` run is the
return's UNVERIFIED line, never a test here); everything else, the signer file, the lock files, the ledger, the
grants file and its modes, is real."""
from __future__ import annotations

import base64
import dataclasses
import io
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import egress_grants as eg
from levain.slot import install_helper as IH
from levain.slot import records as R
from levain.slot import runtime_grammar as RG
from levain.slot import runtime_helper as RH
from levain.slot.ids import SlotId

S = "s-" + "ab" * 8
OTHER = "s-" + "cd" * 8
SLOT = SlotId("egress", 3)
UID = os.getuid() if os.getuid() != 0 else 501
DISPATCH_UID = UID + 1
STRANGER_UID = UID + 2
FRONT_UID = 3989
SLOT_UID = 3990
STORE_ID = "0123456789abcdef0123456789abcdef"
NOW = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)
KEY_BLOB = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(32))).decode()
KEY = "ssh-ed25519 " + KEY_BLOB
FP = IH.blob_fingerprint(KEY)
SIG = "-----BEGIN SSH SIGNATURE-----\nAAAA\n-----END SSH SIGNATURE-----"


def _as_root(uid):
    """The scratch tree's files are the test user's: they stand for the root-owned files root writes."""
    return os.getuid() if uid == 0 else uid


class World:
    """One operator session ``S`` on egress slot 3, ACTIVE, with root's grant state present and ready."""

    def __init__(self, tmp: Path, *, state: str = R.ACTIVE) -> None:
        self.L, self.R = tmp / "L", tmp / "R"
        for d in ("alloc", "slots/e3", "policy", "sessions", "routes"):
            (self.L / d).mkdir(parents=True)
        (self.L / "policy" / "operator").write_text(IH.operator_record_text("operator", UID))
        (self.L / "routes" / "base.json").write_bytes(canon.canonical_bytes(
            {"format": "levain-routes-base/2", "routes": [
                {"route": "anthropic-api", "type": "brokered", "hosts": ["api.anthropic.com"]},
                {"route": "pypi", "type": "egress", "hosts": ["pypi.org", "files.pythonhosted.org"]}]}))
        g = self.L / "routes" / "grants"
        g.mkdir(mode=0o700)
        g.chmod(0o700)
        (g / "store_id").write_text(STORE_ID + "\n")
        (g / "store_id").chmod(0o600)
        (self.L / "policy" / "authority_signers").write_text(
            f'authority-signer namespaces="{eg.NAMESPACE}" {KEY}\n')
        (self.L / "policy" / "authority_signers").chmod(0o644)
        self.slot_dir = self.R / "grants" / "slot3"
        self.slot_dir.mkdir(parents=True)
        (self.R / "grants").chmod(0o755)
        self.slot_dir.chmod(0o750)
        for s, op in ((S, UID), (OTHER, STRANGER_UID)):
            a = R.Alloc(session=s, slot=SLOT, lease_seq=7, slot_rev=12, slot_uid=SLOT_UID, slot_gid=SLOT_UID,
                        kind="interactive", vendor="claude", auth="brokered", operator_uid=op, supervisor_uid=op,
                        staging_uid=op, created_at="2026-11-16T11:00:00Z",
                        boot_id="6f1c2a9e-1b2c-4d5e-8f90-0a1b2c3d4e5f")
            (self.L / "alloc" / f"{s}.json").write_bytes(R.alloc_bytes(a))
            self.set_state(state, s)
        self.clock_now = NOW
        self.chowned: list = []
        self.points: list = []
        self.hook = None
        names = dict(zip(SLOT.accounts, (SLOT_UID, FRONT_UID)))
        self.seams = RH.Seams(host="darwin", store=self.L, run=self.R, euid=lambda: 0, trusted=lambda st: True,
                              account_uid=lambda name: names.get(name, DISPATCH_UID), chowned_uid=_as_root,
                              group_gid=lambda name: os.getgid(), fsync=self._fsync,
                              chown=lambda fd, uid, gid, rel: self.chowned.append((uid, gid, rel)),
                              clock=lambda: self.clock_now.timestamp(), at=self._at)
        self.fsyncs: list = []

    def _fsync(self, fd):
        self.fsyncs.append(fd)
        os.fsync(fd)

    def _at(self, step):
        self.points.append(step)
        if self.hook is not None:
            self.hook(step)

    def set_state(self, state, s=S):
        (self.L / "sessions" / f"{s}.json").write_bytes(R.state_bytes(R.SessionState(
            session=s, generation=3, state=state, claims={}, lease_live=True, event_kind="state",
            updated_at="2026-11-16T11:30:00Z")))

    def main(self, argv, stdin: bytes = b"", uid: int = UID):
        out, err = io.StringIO(), io.StringIO()
        rc = RH.main(list(argv), stdin=stdin, env={"SUDO_UID": str(uid)}, seams=self.seams, out=out, err=err)
        self.err = err.getvalue()
        return rc, canon.parse(out.getvalue().strip().encode())


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def refused(rc_line, name):
    assert rc_line == (RH.EXIT_REFUSED, {"ok": False, "refused": name}), rc_line


# --- T17: the verbs are operator-only, dispatch refused (grammar + the sudoers equality) -------------------------------

@pytest.mark.parametrize("verb,extra", [("egress-grant", []), ("egress-grant-revoke", ["all"]),
                                        ("egress-refusals", ["0"]), ("egress-grants-list", [])])
def test_dispatch_and_strangers_are_refused_before_any_work(world, verb, extra):
    refused(world.main([verb, S, *extra], uid=DISPATCH_UID), "verb-not-allowed-for-caller")
    refused(world.main([verb, S, *extra], uid=STRANGER_UID), "unknown-caller")


@pytest.mark.parametrize("verb,extra", [("egress-grant-revoke", ["all"]), ("egress-refusals", ["0"]),
                                        ("egress-grants-list", [])])
def test_another_operators_session_and_an_unknown_session_are_no_session(world, verb, extra):
    refused(world.main([verb, OTHER, *extra]), "no-session")
    refused(world.main([verb, "s-" + "00" * 8, *extra]), "no-session")


def test_the_sudoers_dispatch_specs_still_equal_the_grammar_without_the_egress_verbs():
    from levain.slot.sudoers import DISPATCH_VERBS
    admitted = tuple(v for v in RG.VERBS if v not in RG.DISPATCH_FORBIDDEN)
    assert set(DISPATCH_VERBS) <= set(admitted) | {"status"}
    assert not set(RG.EGRESS_VERBS) & set(DISPATCH_VERBS)
    assert set(RG.EGRESS_VERBS) <= set(RG.DISPATCH_FORBIDDEN) and set(RG.EGRESS_VERBS) <= set(RH.VERB_HANDLERS)


# --- T18: stdin reaches the verb, typed, through the root entry --------------------------------------------------------

def test_the_root_entry_reads_stdin_for_egress_grant_and_no_other_new_verb():
    assert RH.wants_stdin(["egress-grant", S]) and RH.wants_stdin(["prepare"]) and RH.wants_stdin(["render", S])
    for v in ("egress-grant-revoke", "egress-refusals", "egress-grants-list", "status", "teardown"):
        assert not RH.wants_stdin([v, S])
    assert not RH.wants_stdin([])


def test_the_handler_gets_an_egress_grant_input_not_a_render_blob(world, monkeypatch):
    seen = []
    monkeypatch.setitem(RH.VERB_HANDLERS, "egress-grant", lambda ctx, req, blob: seen.append((req, blob)) or {"x": 1})
    stdin = canon.canonical_bytes({"message": "m", "sig": SIG, "signer_fp": FP, "choice": "d"})
    assert world.main(["egress-grant", S], stdin) == (RH.EXIT_OK, {"ok": True, "x": 1})
    (req, blob), = seen
    assert type(blob) is RG.EgressGrantInput and (blob.choice, blob.signer_fp, blob.sig) == ("d", FP, SIG)
    assert (req.verb, req.session) == ("egress-grant", S)
    refused(world.main(["egress-grant", S], b""), "missing-stdin")
    refused(world.main(["egress-grants-list", S], stdin), "unexpected-stdin")
    refused(world.main(["egress-grant", S], b'{"message":"m"}'), "missing-key")

