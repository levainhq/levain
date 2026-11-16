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



# --- layout rows (T13's production side): R/grants root-owned, R/grants/slot<n> root : the front's group 0750 ----------

@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_layout_has_the_root_owned_grants_dirs_for_egress_slots_only(host):
    from levain.slot import layout as LY
    R_ = LY.RUN[host]
    base = {r.path: r for r in LY.base_run_rows(host, "op")}
    assert base[R_ / "grants"].owner == "root" and base[R_ / "grants"].mode == 0o755
    rows = {r.path: r for r in LY.slot_run_rows(host, SLOT)}
    row = rows[R_ / "grants" / "slot3"]
    assert (row.owner, row.group, row.mode, row.kind) == ("root", "_levain_gw3", 0o750, "dir")
    assert not any("grants" in str(r.path) for r in LY.slot_run_rows(host, SlotId("none", 1)))
    assert rows[R_ / "gw" / "slot3"].owner == "_levain_gw3"          # the front's dir is untouched


# --- the writer: helpers ---------------------------------------------------------------------------------------------

class Verifier:
    """The spy standing for ``levain.authority_sig.verify_link_sig``: records every call, answers ``verdict``."""

    def __init__(self):
        self.calls, self.verdict, self.raises = [], True, None

    def __call__(self, message, signature, *, public_key, fingerprint, namespace="levain-authority", **kw):
        self.calls.append({"message": message, "signature": signature, "public_key": public_key,
                           "fingerprint": fingerprint, "namespace": namespace})
        if self.raises is not None:
            raise self.raises
        return self.verdict


@pytest.fixture
def verifier(monkeypatch):
    from levain.slot import egress_verify as EVY
    v = Verifier()
    monkeypatch.setattr(EVY, "verify_link_sig", v)
    return v


def gid_for(n):
    return "eh-%024x" % n


def raw_message(host="files.example.org", *, grant=None, session=S, slot=3, store=STORE_ID, issued=None, exp_s=None,
                exp_d=None, writable=None, **over):
    issued = issued or NOW - timedelta(minutes=1)
    f = {"store": store, "grant": grant or gid_for(1), "session": session, "slot": str(slot), "host": host,
         "port": "443", "issued": eg.stamp(issued), "expires-session": eg.stamp(exp_s or issued + timedelta(hours=1)),
         "expires-day": eg.stamp(exp_d or issued + timedelta(hours=24)), "rp": "phone.example.net",
         "writable": writable or ("yes" if eg.writable_host(host) else "no")}
    f.update(over)
    return eg.FORMAT_LINE + "\n" + "".join(f"{k}: {f[k]}\n" for k in eg._FIELDS)


def tap(world, host="files.example.org", choice="s", *, msg=None, sig=SIG, fp=FP, session=S, uid=UID, **kw):
    """One ``egress-grant`` delivery: the message ``kw`` builds (or ``msg``), the choice, the signature."""
    msg = msg if msg is not None else raw_message(host, **kw)
    stdin = canon.canonical_bytes({"message": msg, "sig": sig, "signer_fp": fp, "choice": choice})
    return world.main(["egress-grant", session], stdin, uid=uid)


def entries(world, slot_dir=None):
    got = eg.read_grants_file(str((slot_dir or world.slot_dir) / "grants.json"), owner_uid=_as_root(0),
                              group_gid=os.getgid())
    return None if got is None else {e.host: e for e in got.entries}


def rows(world, name):
    p = world.L / "routes" / "grants" / name
    return [canon.parse(line.encode()) for line in p.read_text().splitlines()] if p.exists() else []


def used_ids(world):
    return [r["grant"] for r in rows(world, "used.jsonl")]


def seed(world, hosts, *, expired=(), slot_dir=None, session=S, slot=3):
    """A root-written grants file holding ``hosts`` (and ``expired`` ones), with used rows for none of them."""
    es = [eg.Entry(grant=gid_for(100 + i), host=h, port=443, expires=NOW + timedelta(hours=1), choice="s")
          for i, h in enumerate(hosts)]
    es += [eg.Entry(grant=gid_for(200 + i), host=h, port=443, expires=NOW - timedelta(minutes=5), choice="s")
           for i, h in enumerate(expired)]
    p = (slot_dir or world.slot_dir) / "grants.json"
    p.write_bytes(eg.dump_grants(session, slot, es))
    p.chmod(0o440)
    return es


def ok(line, host, choice="s", grant=None):
    assert line[0] == RH.EXIT_OK, line
    assert line[1]["ok"] is True and line[1]["host"] == host and line[1]["choice"] == choice, line
    if grant:
        assert line[1]["grant"] == grant
    return line[1]


# --- the happy path, T7 (verified bytes), T14 (file mode), T28 (writable at root) ---------------------------------------

def test_a_valid_tap_is_published_and_the_answer_comes_from_the_verified_parse(world, verifier):
    line = tap(world, "files.example.org", "s")
    ans = ok(line, "files.example.org", "s", gid_for(1))
    assert ans["expires"] == eg.stamp(NOW - timedelta(minutes=1) + timedelta(hours=1))
    es = entries(world)
    assert set(es) == {"files.example.org"} and es["files.example.org"].grant == gid_for(1)
    assert used_ids(world) == [gid_for(1)]
    row, = rows(world, "used.jsonl")
    assert row["host"] == "files.example.org" and row["session"] == S and row["issued"] == eg.stamp(NOW - timedelta(minutes=1))
    assert row["keep_until"] == eg.stamp(NOW - timedelta(minutes=1) + timedelta(seconds=eg.DAY_TTL_S + eg.SKEW_S))


@pytest.mark.parametrize("choice,expiry", [("s", timedelta(hours=1)), ("d", timedelta(hours=24))])
def test_the_bytes_verified_are_the_bytes_parsed_and_the_choice_picks_the_expiry(world, verifier, choice, expiry):
    """T7: the spy gets exactly ``signed_bytes(message, choice)`` under namespace levain-egress-host, with the enrolled
    key; the entry published carries the choice and expiry those bytes name, not another's."""
    msg = raw_message("files.example.org")
    line = tap(world, "files.example.org", choice, msg=msg)
    ok(line, "files.example.org", choice)
    call, = verifier.calls
    assert call["message"] == eg.signed_bytes(msg, choice) and call["namespace"] == "levain-egress-host"
    assert (call["public_key"], call["fingerprint"], call["signature"]) == (KEY, FP, SIG)
    e = entries(world)["files.example.org"]
    assert e.choice == choice and e.expires == NOW - timedelta(minutes=1) + expiry


def test_the_published_file_is_root_owned_group_the_fronts_0440_and_the_front_reader_accepts_it(world, verifier,
                                                                                                monkeypatch):
    from levain.slot import egress_store as ES
    order = []
    real_fchmod, real_replace = os.fchmod, os.replace
    monkeypatch.setattr(ES.os, "fchmod", lambda fd, m: (order.append(("fchmod", oct(m))), real_fchmod(fd, m))[1])
    monkeypatch.setattr(ES.os, "replace", lambda *a, **k: (order.append(("replace",)), real_replace(*a, **k))[1])
    ok(tap(world), "files.example.org")
    p = world.slot_dir / "grants.json"
    assert stat.S_IMODE(p.stat().st_mode) == 0o440
    assert eg.read_grants_file(str(p), owner_uid=os.getuid(), group_gid=os.getgid()) is not None
    assert (0, os.getgid(), "grants/slot3") in [(u, g, r) for u, g, r in world.chowned] or any(
        u == 0 and g == os.getgid() for u, g, r in world.chowned), world.chowned
    assert order.index(("fchmod", "0o440")) < order.index(("replace",))
    assert sorted(x.name for x in world.slot_dir.iterdir()) == ["grants.json"]


def test_writable_is_checked_one_way_at_root(world, verifier):
    """T28: signed ``yes`` for a host not on the list is admitted; signed ``no`` for a listed host is writable-stale."""
    ok(tap(world, "example.org", writable="yes", grant=gid_for(2)), "example.org")
    refused(tap(world, "github.com", writable="no", grant=gid_for(3)), "writable-stale")
    assert used_ids(world) == [gid_for(2)]


# --- T1 / T2 signature, T30 verify lock ----------------------------------------------------------------------------------

def test_a_signature_that_does_not_verify_is_refused_and_nothing_is_written_or_burned(world, verifier):
    verifier.verdict = False
    refused(tap(world), "egress-grant-sig")
    assert entries(world) is None and used_ids(world) == []


def test_an_unavailable_verifier_is_never_a_verdict(world, verifier):
    from levain.authority_sig import VerifierUnavailable
    verifier.raises = VerifierUnavailable("no ssh-keygen")
    refused(tap(world), "egress-grant-verifier")
    assert entries(world) is None and used_ids(world) == []


def test_a_key_not_enrolled_for_this_namespace_is_unenrolled_and_ssh_keygen_is_never_asked(world, verifier):
    signers = world.L / "policy" / "authority_signers"
    other = "SHA256:" + "B" * 43
    refused(tap(world, fp=other), "egress-grant-unenrolled")                            # no line for that key
    for line in (f'authority-signer namespaces="levain-authority" {KEY}\n',             # other namespace only
                 f"authority-signer {KEY}\n",                                           # no options = every namespace
                 f'authority-signer no-touch-required {KEY}\n',                         # an option that is not namespaces
                 f'authority-signer namespaces="levain-authority,levain-confirm" {KEY}\n'):
        signers.write_text(line)
        refused(tap(world), "egress-grant-unenrolled")
    signers.write_text(f'authority-signer namespaces="levain-authority,{eg.NAMESPACE}" {KEY}\n')
    ok(tap(world), "files.example.org")                                                 # widened line: accepted
    assert len(verifier.calls) == 1 and used_ids(world) == [gid_for(1)]


def test_a_missing_signers_file_is_unenrolled(world, verifier):
    (world.L / "policy" / "authority_signers").unlink()
    refused(tap(world), "egress-grant-unenrolled")
    assert verifier.calls == []


def test_a_held_verify_lock_answers_busy_and_no_verifier_runs(world, verifier):
    """T30"""
    import fcntl
    fd = os.open(world.L / "routes" / "grants" / "verify.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        refused(tap(world), "egress-grant-busy")
    finally:
        os.close(fd)
    assert verifier.calls == [] and entries(world) is None
    ok(tap(world), "files.example.org")                      # released: the retry goes through


def test_the_verify_lock_is_released_before_the_session_lock_is_taken(world, verifier):
    import fcntl
    fd = os.open(world.L / "alloc" / RH.session_lock_name(S), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        refused(tap(world), "session-busy")
    finally:
        os.close(fd)
    again = os.open(world.L / "routes" / "grants" / "verify.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(again, fcntl.LOCK_EX | fcntl.LOCK_NB)     # not held by the refused call
    finally:
        os.close(again)
    assert used_ids(world) == []


# --- T3 freshness, T4 bound and expiry --------------------------------------------------------------------------------

@pytest.mark.parametrize("age_s,good", [(600, True), (601, False), (660, False), (-300, True), (-301, False),
                                        (-360, False), (0, True)])
def test_freshness_edges(world, verifier, age_s, good):
    issued = NOW - timedelta(seconds=age_s)
    line = tap(world, issued=issued, exp_s=issued + timedelta(hours=1) if age_s != 600 else NOW + timedelta(hours=1))
    if good:
        ok(line, "files.example.org")
    else:
        refused(line, "fresh")
        assert entries(world) is None and used_ids(world) == [] and verifier.calls == []


def test_a_grant_that_expired_before_now_is_refused_not_written_not_burned(world, verifier):
    """T4's reproducer: issued 9 minutes ago with expires-session one minute after issue passes S1's bounds and
    freshness but is over; root refuses it ``egress-grant-expired``."""
    issued = NOW - timedelta(minutes=9)
    refused(tap(world, issued=issued, exp_s=issued + timedelta(minutes=1)), "egress-grant-expired")
    assert entries(world) is None and used_ids(world) == [] and verifier.calls == []
    # exactly now is not after now
    refused(tap(world, issued=issued, exp_s=NOW), "egress-grant-expired")
    ok(tap(world, issued=issued, exp_s=NOW + timedelta(seconds=1)), "files.example.org")


@pytest.mark.parametrize("over,name", [
    ({"exp_s": NOW + timedelta(hours=9)}, "bound"),                                 # past issued + 8 h
    ({"exp_d": NOW + timedelta(hours=30)}, "bound"),                                # past issued + 24 h
    ({"port": "80"}, "shape"), ({"host": "localhost"}, "host"), ({"host": "metadata.example.org"}, "never-grant"),
])
def test_the_s1_refusals_come_out_by_their_own_names(world, verifier, over, name):
    host = over.pop("host", "files.example.org")
    refused(tap(world, host, **over), name)
    assert entries(world) is None and used_ids(world) == []


def test_a_message_for_another_session_or_slot_is_refused(world, verifier):
    refused(tap(world, msg=raw_message(session=OTHER_SESSION)), "egress-grant-session")   # names another session
    refused(tap(world, msg=raw_message(slot=4)), "egress-grant-session")
    assert entries(world) is None and used_ids(world) == [] and verifier.calls == []


OTHER_SESSION = "s-" + "ee" * 8


# --- T29 the store id --------------------------------------------------------------------------------------------------

def test_the_message_store_must_be_roots_store_id(world, verifier):
    refused(tap(world, store="f" * 32), "egress-grant-store")
    assert verifier.calls == [] and used_ids(world) == []


def test_an_absent_store_id_is_not_ready_and_no_verb_creates_it(world, verifier):
    (world.L / "routes" / "grants" / "store_id").unlink()
    refused(tap(world), "egress-grant-not-ready")
    assert not (world.L / "routes" / "grants" / "store_id").exists() and verifier.calls == []
    for argv in (["egress-grant-revoke", S, "all"], ["egress-grants-list", S]):
        world.main(argv)
    assert not (world.L / "routes" / "grants" / "store_id").exists()


def test_a_store_id_that_is_not_root_0600_hex_is_not_ready(world, verifier):
    sid = world.L / "routes" / "grants" / "store_id"
    for bad, mode in (("XYZ\n", 0o600), (STORE_ID[:-1] + "\n", 0o600), (STORE_ID.upper() + "\n", 0o600),
                      (STORE_ID + "\n", 0o640), (STORE_ID + "\n", 0o666)):
        sid.write_text(bad)
        sid.chmod(mode)
        refused(tap(world), "egress-grant-not-ready")
    sid.unlink()
    sid.symlink_to(world.L / "policy" / "operator")
    refused(tap(world), "egress-grant-not-ready")
    assert used_ids(world) == []


def test_a_missing_grants_ledger_dir_is_not_ready(world, verifier):
    import shutil
    shutil.rmtree(world.L / "routes" / "grants")
    refused(tap(world), "egress-grant-not-ready")


# --- T16 host class -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["api.anthropic.com",            # a brokered route's host
                                  "pypi.org", "files.pythonhosted.org",     # an operator egress route's hosts
                                  "chatgpt.com", "platform.claude.com"])    # VENDOR_HOSTS with no route in the base map
def test_a_host_the_base_map_or_a_vendor_already_names_is_never_granted(world, verifier, host):
    refused(tap(world, host), "egress-grant-host-routed")
    assert entries(world) is None and used_ids(world) == [] and verifier.calls == []


def test_never_grant_hosts_are_refused_by_s1_not_written(world, verifier):
    for host in ("metadata.google.internal", "foo.local", "metadata.example.org"):
        line = tap(world, host)
        assert line[0] == RH.EXIT_REFUSED and line[1]["refused"] in ("never-grant", "host")
    assert entries(world) is None and used_ids(world) == []


# --- T5 single use ------------------------------------------------------------------------------------------------------

def test_the_same_bytes_twice_are_used_and_a_choice_swap_of_one_message_is_used_too(world, verifier):
    msg = raw_message("files.example.org")
    ok(tap(world, msg=msg, choice="s"), "files.example.org")
    refused(tap(world, msg=msg, choice="s"), "egress-grant-used")
    refused(tap(world, msg=msg, choice="d"), "egress-grant-used")               # the grant id is the single use
    assert used_ids(world) == [gid_for(1)] and entries(world)["files.example.org"].choice == "s"


def test_a_revoked_id_is_never_admitted_and_an_unknown_one_recorded_before_delivery_blocks_it(world, verifier):
    ok(tap(world, "a.example.org", grant=gid_for(5)), "a.example.org")
    assert world.main(["egress-grant-revoke", S, gid_for(5)]) == (
        RH.EXIT_OK, {"ok": True, "removed": [gid_for(5)], "recorded": []})
    assert entries(world) == {}
    refused(tap(world, "a.example.org", grant=gid_for(5)), "egress-grant-used")
    # an id never delivered: recorded, then those bytes refused
    assert world.main(["egress-grant-revoke", S, gid_for(6)]) == (
        RH.EXIT_OK, {"ok": True, "removed": [], "recorded": [gid_for(6)]})
    refused(tap(world, "b.example.org", grant=gid_for(6)), "egress-grant-revoked")
    assert entries(world) == {}
    assert gid_for(6) not in used_ids(world)                                    # refused before it was burned


# --- T9 replace by host, T24 latest issued, T8 prune, T10 cap ---------------------------------------------------------

def test_a_later_issued_grant_for_the_same_host_replaces_the_entry(world, verifier):
    t1, t2 = NOW - timedelta(minutes=5), NOW - timedelta(minutes=1)
    ok(tap(world, "a.example.org", "s", grant=gid_for(1), issued=t1), "a.example.org")
    ok(tap(world, "a.example.org", "d", grant=gid_for(2), issued=t2), "a.example.org", "d")
    es = entries(world)
    assert list(es) == ["a.example.org"] and es["a.example.org"].grant == gid_for(2) and es["a.example.org"].choice == "d"
    assert used_ids(world) == [gid_for(1), gid_for(2)]
    parsed = eg.parse_grants((world.slot_dir / "grants.json").read_bytes())
    assert len(parsed.entries) == 1


def test_a_stale_card_does_not_override_a_newer_decision_and_is_not_burned(world, verifier):
    """T24"""
    t1, t2 = NOW - timedelta(minutes=5), NOW - timedelta(minutes=1)
    ok(tap(world, "a.example.org", "s", grant=gid_for(2), issued=t2), "a.example.org")
    refused(tap(world, "a.example.org", "d", grant=gid_for(1), issued=t1), "egress-grant-superseded")
    es = entries(world)
    assert es["a.example.org"].grant == gid_for(2) and es["a.example.org"].choice == "s"
    assert used_ids(world) == [gid_for(2)]
    # the same issue instant is not later either
    refused(tap(world, "a.example.org", "d", grant=gid_for(3), issued=t2), "egress-grant-superseded")


def test_a_live_entry_without_its_used_row_is_a_ledger_invariant_break(world, verifier):
    seed(world, ["a.example.org"])
    refused(tap(world, "a.example.org", grant=gid_for(9)), "egress-grant-ledger")
    assert used_ids(world) == []


def test_expired_entries_are_pruned_before_the_cap_and_the_new_host_is_admitted(world, verifier):
    """T8: 16 entries of which 3 expired: the new host is admitted and 14 are written (13 live + 1)."""
    seed(world, [f"h{i}.example.org" for i in range(13)], expired=[f"x{i}.example.org" for i in range(3)])
    assert len(eg.parse_grants((world.slot_dir / "grants.json").read_bytes()).entries) == 16
    ok(tap(world, "new.example.org", grant=gid_for(77)), "new.example.org")
    es = entries(world)
    assert len(es) == 14 and "new.example.org" in es and not any(h.startswith("x") for h in es)


def test_a_seventeenth_live_grant_is_too_many_and_changes_nothing(world, verifier):
    """T10"""
    seed(world, [f"h{i}.example.org" for i in range(16)])
    before = (world.slot_dir / "grants.json").read_bytes()
    refused(tap(world, "new.example.org", grant=gid_for(77)), "too-many")
    assert (world.slot_dir / "grants.json").read_bytes() == before and used_ids(world) == []
    assert sorted(x.name for x in world.slot_dir.iterdir()) == ["grants.json"]


def test_a_sixteenth_replacing_a_host_is_not_too_many(world, verifier):
    seed(world, [f"h{i}.example.org" for i in range(15)])
    ok(tap(world, "new.example.org", grant=gid_for(77)), "new.example.org")
    assert len(entries(world)) == 16


# --- T6 / T11 failure semantics, T12 temps ---------------------------------------------------------------------------

def _fault(world, step, exc):
    def hook(s):
        if s == step:
            raise exc
    world.hook = hook


@pytest.mark.parametrize("step", ["egress-grant:stage", "egress-grant:write", "egress-grant:staged"])
def test_a_failure_before_the_ledger_append_keeps_the_old_file_leaves_no_temp_and_burns_nothing(world, verifier, step):
    """T6 (fault before the append) + T11 (temp write / fsync raise)."""
    seed(world, ["old.example.org"])
    before = (world.slot_dir / "grants.json").read_bytes()
    _fault(world, step, OSError(5, "EIO"))
    refused(tap(world, "new.example.org", grant=gid_for(8)), "egress-grant-write-failed")
    assert (world.slot_dir / "grants.json").read_bytes() == before
    assert sorted(x.name for x in world.slot_dir.iterdir()) == ["grants.json"] and used_ids(world) == []
    world.hook = None
    ok(tap(world, "new.example.org", grant=gid_for(8)), "new.example.org")            # redelivery admitted


@pytest.mark.parametrize("step,exc", [("egress-grant:committed", OSError(5, "EIO")),
                                      ("egress-grant:rename", OSError(5, "EIO")),
                                      ("egress-grant:committed", RuntimeError("process dies here"))])
def test_a_failure_after_the_append_and_before_the_rename_keeps_the_old_file_and_burns_the_tap(world, verifier, step, exc):
    """T6: the old file stays in force, the temp is gone, and the redelivery is ``egress-grant-used``."""
    seed(world, ["old.example.org"])
    before = (world.slot_dir / "grants.json").read_bytes()
    _fault(world, step, exc)
    line = tap(world, "new.example.org", grant=gid_for(8))
    assert line[0] != RH.EXIT_OK
    assert (world.slot_dir / "grants.json").read_bytes() == before
    assert sorted(x.name for x in world.slot_dir.iterdir()) == ["grants.json"]
    assert used_ids(world) == [gid_for(8)]
    world.hook = None
    refused(tap(world, "new.example.org", grant=gid_for(8)), "egress-grant-used")


def test_a_directory_sync_failure_after_the_rename_is_unsure_the_file_is_in_force_and_the_tap_burned(world, verifier):
    _fault(world, "egress-grant:dirsync", OSError(5, "EIO"))
    refused(tap(world, "new.example.org", grant=gid_for(8)), "egress-grant-unsure")
    assert "new.example.org" in entries(world) and used_ids(world) == [gid_for(8)]


def test_the_ledger_append_is_the_last_step_before_the_rename(world, verifier):
    ok(tap(world), "files.example.org")
    pts = [p for p in world.points if p.startswith("egress-grant:")]
    assert pts == ["egress-grant:stage", "egress-grant:write", "egress-grant:staged", "egress-grant:ledger",
                   "egress-grant:committed", "egress-grant:rename", "egress-grant:dirsync"]


def test_stale_temps_from_a_killed_run_are_swept_up_to_sixteen_a_call(world, verifier):
    """T12"""
    for i in range(17):
        (world.slot_dir / f"grants.json.{i:016x}.new").write_bytes(b"x")
    (world.slot_dir / "unrelated.txt").write_bytes(b"keep")
    ok(tap(world), "files.example.org")
    left = sorted(x.name for x in world.slot_dir.iterdir())
    assert "grants.json" in left and "unrelated.txt" in left
    assert len([n for n in left if n.endswith(".new")]) == 1


# --- T13 directory trust -------------------------------------------------------------------------------------------------

def _break(world, how):
    d = world.slot_dir
    if how == "absent":
        d.rmdir()
    elif how == "mode-0770":
        d.chmod(0o770)
    elif how == "mode-0755":
        d.chmod(0o755)
    elif how == "mode-2750":
        d.chmod(0o2750)
    elif how == "symlink":
        d.rmdir()
        target = d.parent / "elsewhere"
        target.mkdir(mode=0o750)
        d.symlink_to(target)
    elif how == "gid":
        world.seams = dataclasses.replace(world.seams, group_gid=lambda name: os.getgid() + 12345)
    elif how == "uid":
        world.seams = dataclasses.replace(world.seams, chowned_uid=lambda u: u + 12345)
    elif how == "grants-symlink":
        import shutil
        shutil.rmtree(d.parent)
        elsewhere = d.parent.parent / "elsewhere2"
        (elsewhere / "slot3").mkdir(parents=True, mode=0o750)
        d.parent.symlink_to(elsewhere)


@pytest.mark.parametrize("how", ["absent", "mode-0770", "mode-0755", "mode-2750", "symlink", "gid", "uid",
                                 "grants-symlink"])
def test_a_slot_dir_that_is_not_roots_with_the_fronts_group_and_exactly_0750_is_not_ready(world, verifier, how):
    _break(world, how)
    refused(tap(world), "egress-grant-not-ready")
    assert used_ids(world) == []
    for p in world.R.rglob("grants.json*"):
        raise AssertionError(f"something was written: {p}")


# --- T15 scope ------------------------------------------------------------------------------------------------------------

def test_a_none_class_session_has_no_grants_and_a_stranger_gets_no_session(world, verifier):
    a = R.Alloc(session=OTHER_SESSION, slot=SlotId("none", 1), lease_seq=1, slot_rev=1, slot_uid=3980, slot_gid=3980,
                kind="verifier", vendor="claude", auth="none", operator_uid=UID, supervisor_uid=UID, staging_uid=UID,
                created_at="2026-11-16T11:00:00Z", boot_id="6f1c2a9e-1b2c-4d5e-8f90-0a1b2c3d4e5f", child_uid=3979)
    (world.L / "alloc" / f"{OTHER_SESSION}.json").write_bytes(R.alloc_bytes(a))
    refused(tap(world, session=OTHER_SESSION, msg=raw_message(session=OTHER_SESSION)), "egress-grant-class")
    refused(tap(world, session=OTHER), "no-session")
    assert verifier.calls == []


# --- T25 the old location is ignored ----------------------------------------------------------------------------------

def test_a_front_planted_file_in_the_fronts_own_dir_is_never_read_or_merged(world, verifier):
    gw = world.R / "gw" / "slot3"
    gw.mkdir(parents=True)
    seed(world, ["evil.example.org"], slot_dir=gw)
    ok(tap(world, "y.example.org", grant=gid_for(4)), "y.example.org")
    assert set(entries(world)) == {"y.example.org"}
    assert (gw / "grants.json").exists()                                         # untouched, ignored


def test_an_old_sessions_file_in_the_dir_is_not_merged_into_this_sessions(world, verifier):
    seed(world, ["old.example.org"], session=OTHER_SESSION)
    ok(tap(world, "y.example.org", grant=gid_for(4)), "y.example.org")
    assert set(entries(world)) == {"y.example.org"}


@pytest.mark.parametrize("junk", [b"not json", b"{}", b""])
def test_a_grants_file_that_does_not_parse_is_an_empty_rebuild_never_a_merge(world, verifier, junk):
    (world.slot_dir / "grants.json").write_bytes(junk)
    (world.slot_dir / "grants.json").chmod(0o440)
    ok(tap(world, "y.example.org", grant=gid_for(4)), "y.example.org")
    assert set(entries(world)) == {"y.example.org"}


# --- state under the session lock: Q4 ---------------------------------------------------------------------------------

@pytest.mark.parametrize("state", [R.REVOKED, R.BRAKED, R.ALLOCATED, R.RENDERED, R.DEAD])
def test_only_an_active_session_takes_a_grant(world, verifier, state):
    world.set_state(state)
    refused(tap(world), "egress-grant-not-active")
    assert entries(world) is None and used_ids(world) == []


def test_a_missing_state_record_is_not_active(world, verifier):
    (world.L / "sessions" / f"{S}.json").unlink()
    refused(tap(world), "egress-grant-not-active")


# --- T27 the global ledger ------------------------------------------------------------------------------------------------

def _second_session(world):
    SLOT4 = SlotId("egress", 4)
    S2 = "s-" + "12" * 8
    a = R.Alloc(session=S2, slot=SLOT4, lease_seq=7, slot_rev=12, slot_uid=3988, slot_gid=3988, kind="interactive",
                vendor="claude", auth="brokered", operator_uid=UID, supervisor_uid=UID, staging_uid=UID,
                created_at="2026-11-16T11:00:00Z", boot_id="6f1c2a9e-1b2c-4d5e-8f90-0a1b2c3d4e5f")
    (world.L / "alloc" / f"{S2}.json").write_bytes(R.alloc_bytes(a))
    world.set_state(R.ACTIVE, S2)
    d = world.R / "grants" / "slot4"
    d.mkdir()
    d.chmod(0o750)
    (world.L / "slots" / "e4").mkdir()
    names = {SLOT.accounts[1]: FRONT_UID, SLOT4.accounts[1]: 3987, SLOT.accounts[0]: SLOT_UID,
             SLOT4.accounts[0]: 3988}
    world.seams = dataclasses.replace(world.seams, account_uid=lambda n: names.get(n, DISPATCH_UID))
    return S2, d


def test_two_sessions_publishing_at_once_both_land_in_the_ledger_and_neither_row_is_lost(world, verifier):
    import threading
    S2, d2 = _second_session(world)
    gate, entered = threading.Event(), threading.Event()
    first = {"done": False}

    def hook(step):
        if step == "egress-grant:staged" and not first["done"]:
            first["done"] = True
            entered.set()
            assert gate.wait(10)

    world.hook = hook
    results = {}
    t = threading.Thread(target=lambda: results.update(a=tap(world, "a.example.org", grant=gid_for(1))))
    t.start()
    assert entered.wait(10)
    results["b"] = tap(world, "b.example.org", grant=gid_for(2), session=S2,
                       msg=raw_message("b.example.org", grant=gid_for(2), session=S2, slot=4))
    gate.set()
    t.join(10)
    assert results["a"][0] == RH.EXIT_OK and results["b"][0] == RH.EXIT_OK, results
    assert sorted(used_ids(world)) == [gid_for(1), gid_for(2)]
    assert set(entries(world)) == {"a.example.org"} and set(entries(world, d2)) == {"b.example.org"}


def test_a_ledger_that_does_not_parse_fails_closed_and_nothing_is_admitted(world, verifier):
    ledger = world.L / "routes" / "grants" / "used.jsonl"
    for junk in (b"not json\n", b'{"grant":"x"}\n', b'{"grant":"%s","host":"a.example.org","issued":"x",'
                 b'"keep_until":"y","session":"%s"}\n' % (gid_for(1).encode(), S.encode()), b"\xff\xfe\n"):
        ledger.write_bytes(junk)
        refused(tap(world), "egress-grant-ledger")
        assert entries(world) is None
        assert ledger.read_bytes() == junk                                       # never rewritten as empty
    ledger.unlink()
    ledger.symlink_to(world.L / "policy" / "operator")
    refused(tap(world), "egress-grant-ledger")


def test_a_ledger_past_its_cap_refuses_new_taps_by_name(world, verifier):
    from levain.slot import egress_ledger as EL
    keep = eg.stamp(NOW + timedelta(days=2))
    lines = b"".join(canon.canonical_bytes({"grant": gid_for(1000 + i), "host": "z.example.org", "issued": eg.stamp(NOW),
                                            "keep_until": keep, "session": S}) + b"\n" for i in range(EL.USED_MAX))
    (world.L / "routes" / "grants" / "used.jsonl").write_bytes(lines)
    (world.L / "routes" / "grants" / "used.jsonl").chmod(0o600)
    refused(tap(world), "egress-grant-ledger-full")
    assert entries(world) is None


def test_used_rows_past_keep_until_are_pruned_and_a_backward_clock_cannot_readmit_a_live_one(world, verifier):
    ok(tap(world, "a.example.org", grant=gid_for(1)), "a.example.org")
    world.clock_now = NOW - timedelta(hours=3)                                   # a backward step: the row is kept
    refused(tap(world, "b.example.org", grant=gid_for(1), issued=NOW - timedelta(hours=3, minutes=1)),
            "egress-grant-used")
    world.clock_now = NOW + timedelta(days=3)                                    # far past keep_until: pruned
    ok(tap(world, "z.example.org", grant=gid_for(2), issued=NOW + timedelta(days=3, minutes=-1)), "z.example.org")
    assert used_ids(world) == [gid_for(2)]


# --- T19 revoke ------------------------------------------------------------------------------------------------------------

def test_revoke_all_empties_the_file_and_leaves_the_used_ledger_alone(world, verifier):
    ok(tap(world, "a.example.org", grant=gid_for(1)), "a.example.org")
    ok(tap(world, "b.example.org", grant=gid_for(2)), "b.example.org")
    line = world.main(["egress-grant-revoke", S, "all"])
    assert line == (RH.EXIT_OK, {"ok": True, "removed": [gid_for(1), gid_for(2)], "recorded": []})
    assert entries(world) == {} and used_ids(world) == [gid_for(1), gid_for(2)]
    assert world.main(["egress-grant-revoke", S, "all"]) == (RH.EXIT_OK, {"ok": True, "removed": [], "recorded": []})


def test_revoking_a_known_id_keeps_the_others(world, verifier):
    ok(tap(world, "a.example.org", grant=gid_for(1)), "a.example.org")
    ok(tap(world, "b.example.org", grant=gid_for(2)), "b.example.org")
    assert world.main(["egress-grant-revoke", S, gid_for(1)])[1]["removed"] == [gid_for(1)]
    assert set(entries(world)) == {"b.example.org"}


def test_three_hundred_unknown_ids_are_recorded_the_oldest_evicted_and_it_never_refuses(world, verifier):
    from levain.slot import egress_ledger as EL
    for i in range(EL.REVOKED_MAX + 44):
        line = world.main(["egress-grant-revoke", S, gid_for(5000 + i)])
        assert line == (RH.EXIT_OK, {"ok": True, "removed": [], "recorded": [gid_for(5000 + i)]})
    ids = [r["grant"] for r in rows(world, "revoked.jsonl")]
    assert len(ids) == EL.REVOKED_MAX and ids[-1] == gid_for(5000 + EL.REVOKED_MAX + 43)
    assert gid_for(5000) not in ids and gid_for(5000 + 44) in ids
    # the freshest unknown id still blocks its bytes
    refused(tap(world, "q.example.org", grant=gid_for(5000 + EL.REVOKED_MAX + 43)), "egress-grant-revoked")


def test_revoked_rows_past_their_keep_until_are_pruned_before_the_cap_is_applied(world, verifier):
    from levain.slot import egress_ledger as EL
    world.main(["egress-grant-revoke", S, gid_for(1)])
    row, = rows(world, "revoked.jsonl")
    assert row["keep_until"] == eg.stamp(NOW + timedelta(seconds=eg.FRESH_S + eg.SKEW_S))
    world.clock_now = NOW + timedelta(seconds=eg.FRESH_S + eg.SKEW_S + 1)
    world.main(["egress-grant-revoke", S, gid_for(2)])
    assert [r["grant"] for r in rows(world, "revoked.jsonl")] == [gid_for(2)]


def test_revoke_with_a_ledger_that_does_not_parse_fails_closed(world, verifier):
    (world.L / "routes" / "grants" / "revoked.jsonl").write_bytes(b"garbage\n")
    refused(world.main(["egress-grant-revoke", S, gid_for(1)]), "egress-grant-ledger")


def test_revoke_takes_the_session_lock_then_the_ledger_lock(world, verifier):
    import fcntl
    fd = os.open(world.L / "alloc" / RH.session_lock_name(S), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        refused(world.main(["egress-grant-revoke", S, "all"]), "session-busy")
    finally:
        os.close(fd)


# --- egress-grants-list -----------------------------------------------------------------------------------------------------

def test_the_list_answers_the_store_id_and_the_live_entries_after_pruning(world, verifier):
    assert world.main(["egress-grants-list", S]) == (RH.EXIT_OK, {"ok": True, "store": STORE_ID, "entries": []})
    seed(world, ["a.example.org"], expired=["gone.example.org"])
    line = world.main(["egress-grants-list", S])
    assert line[1]["store"] == STORE_ID
    assert line[1]["entries"] == [{"grant": gid_for(100), "host": "a.example.org",
                                   "expires": eg.stamp(NOW + timedelta(hours=1)), "choice": "s"}]



def test_the_module_imports_under_the_root_interpreter():
    """T23: ``/usr/bin/python3`` (3.9) imports the verb module; a runtime that lacks it is REASONED, not passed."""
    import subprocess
    py = "/usr/bin/python3"
    if not os.path.exists(py):
        pytest.skip("no /usr/bin/python3 on this host")
    repo = str(Path(__file__).resolve().parents[1])
    cp = subprocess.run([py, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                         "import levain.slot.egress_verbs, levain.slot.egress_verify, levain.slot.egress_store, "
                         "levain.slot.egress_ledger", repo], capture_output=True, timeout=60)
    assert cp.returncode == 0, cp.stderr.decode()[-400:]
