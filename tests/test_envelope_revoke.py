"""Board row 153: the cockpit lists desk envelopes and ends one with a passkey-signed revoke
(:class:`levain.desk.envelope_revoke.EnvelopeRevoke`), the grant revoke's own ceremony and gate over the store the
Signer's queue names. Scratch stores only: envelopes are seeded straight into a store in tmp (the store's signature
checks are not what is under test), and the passkey is the dispatch tests' software authenticator."""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from levain import authority as auth
from levain.consent.answering import passkey_keys
from levain.consent.queue import authority_source
from levain.consent.sign import Signer
from levain.desk.envelopes import EnvelopeStore
from levain.desk.envelope_revoke import NAMESPACE, EnvelopeRevoke
from tests.test_cockpit_sign_routes import _call, _serving
from tests.test_grant_phone import RP, world  # noqa: F401  (world is a fixture)
from tests.test_dispatch_phone import SoftPasskey

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

NS = "flow-needs-phill"
PAGE = f"https://{RP}"
EID = "ev-" + "7b" * 16
EID2 = "ev-" + "a1" * 16


def _stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def _seed(qdir, eid: str = EID, *, hours: float = 24, words: str = "run the morning seats on flow",
          body_changes: "dict[str, Any] | None" = None, stop_at: "str | None" = None) -> EnvelopeStore:
    """One envelope row in a scratch store, as a signed envelope leaves it (body, digest and stop are what is read).
    ``body_changes`` and ``stop_at`` seed a malformed row (the store's signature checks are not what is under test)."""
    store = EnvelopeStore(qdir)
    drop, _ = store.accept_drop(words, surface="phone")
    now = datetime.now(timezone.utc)
    body = {"envelope_id": eid, "drop_id": drop.drop_id, "issued_at": _stamp(now), "stop_at": _stamp(now + timedelta(hours=hours)),
            "where": [{"repo": "/tmp/scratch/flow", "paths": ["."]}], "launches": [{"repo": "/tmp/scratch/flow", "count": 12}],
            "budget": {"hours": 8}, "origin": "structured"}
    body.update(body_changes or {})
    with store._write() as (conn, at):
        conn.execute("INSERT INTO envelopes (envelope_id, gseq, origin, drop_id, body, digest, payload, signature, "
                     "signer_fp, signer_class, supersedes, stop_at, accepted_at) VALUES (?, ?, 'structured', ?, ?, ?, '{}', "
                     "'sig', 'SHA256:x', 'display-phone', NULL, ?, ?)",
                     (eid, store._next_gseq(conn, at), drop.drop_id, json.dumps(body), "digest-" + eid[3:9],
                      body["stop_at"] if stop_at is None else stop_at, at))
    return store


def _signer(world, rp: str = RP, origin: str = PAGE) -> Signer:
    source = authority_source(world.root / "cockpit.db", anchor=auth.anchor_path(world.root))
    signer = Signer(world.qdir, (NS,), rp, lambda: {k.fingerprint: k for k in passkey_keys(source(), rp)})
    signer.page_origin = origin
    return signer


def _row(url: str, eid: str = EID) -> dict[str, Any]:
    status, listed = _call(url, "/cockpit/envelope/list.json")
    assert status == 200, listed
    return {e["id"]: e for e in listed["envelopes"]}[eid]


def test_list_shows_the_envelope_with_label_scope_and_state(world, tmp_path) -> None:
    _seed(world.qdir)
    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (url, _h):
        status, listed = _call(url, "/cockpit/envelope/list.json")
        assert status == 200 and listed["origin"] == PAGE and listed["signers"] == [world.phone.fp], listed
        row = _row(url)
        assert row["state"] == "live" and row["label"] == "run the morning seats on flow", row
        assert row["scope"] == "flow · 12 launches · 8 h" and row["expires"] > row["issued"], row
        # the gate is the grant listing's: a token on every bind, never a cross-site page
        assert _call(url, "/cockpit/envelope/list.json", token=None)[0] == 403
        assert _call(url, "/cockpit/envelope/list.json", sfs="cross-site")[0] == 403


def test_a_signed_revoke_kills_the_envelope(world, tmp_path) -> None:
    store = _seed(world.qdir)
    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (url, _h):
        row = _row(url)
        q = urllib.parse.quote(EID, safe="")
        status, c = _call(url, f"/cockpit/envelope/{q}/revoke-challenge",
                          {"signer_fp": world.phone.fp, "instance": row["instance"]})
        assert status == 200 and c["namespace"] == NAMESPACE and EID in c["line"] and c["rp_id"] == RP, c
        status, out = _call(url, f"/cockpit/envelope/{q}/revoke", world.phone.assertion(c["challenge"], origin=PAGE))
        assert (status, out["revoked"]) == (200, EID), out
        assert _row(url)["state"] == "killed"
        # a held message is used once, whatever the outcome
        status, out = _call(url, f"/cockpit/envelope/{q}/revoke", world.phone.assertion(c["challenge"], origin=PAGE))
        assert (status, out["error"]) == (409, "no_challenge"), out
    with store._write() as (conn, _at):
        assert conn.execute("SELECT kind, by FROM envelope_attenuations WHERE envelope_id = ?", (EID,)).fetchall() \
            == [("kill", "operator")]


def test_no_unsigned_path_kills(world, tmp_path) -> None:
    store = _seed(world.qdir)
    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (url, _h):
        row = _row(url)
        q = urllib.parse.quote(EID, safe="")
        challenge_body = {"signer_fp": world.phone.fp, "instance": row["instance"]}
        # no token, a wrong token, a non-JSON body: refused before the ceremony runs
        for token in (None, "wrong-token"):
            status, out = _call(url, f"/cockpit/envelope/{q}/revoke-challenge", challenge_body, token=token)
            assert (status, out.get("error")) == (403, "credential_required"), (token, status, out)
        assert _call(url, f"/cockpit/envelope/{q}/revoke-challenge", challenge_body, ctype="text/plain")[0] == 415
        # revoke with no challenge held: nothing to sign against
        status, out = _call(url, f"/cockpit/envelope/{q}/revoke", {"authenticator_data": "a", "client_data_json": "b",
                                                                    "signature": "c"})
        assert (status, out["error"]) == (409, "no_challenge"), out
        # a body that is not the assertion's three fields
        assert _call(url, f"/cockpit/envelope/{q}/revoke", {"kill": "yes"})[0] == 400
        # a challenge, then garbage in place of an assertion
        assert _call(url, f"/cockpit/envelope/{q}/revoke-challenge", challenge_body)[0] == 200
        status, out = _call(url, f"/cockpit/envelope/{q}/revoke", {"authenticator_data": "AAAA", "client_data_json": "AAAA",
                                                                    "signature": "AAAA"})
        assert status == 400, out
        assert _row(url)["state"] == "live"
    with store._write() as (conn, _at):
        assert conn.execute("SELECT count(*) FROM envelope_attenuations").fetchone() == (0,)


def test_a_wrong_signer_is_refused(world, tmp_path) -> None:
    _seed(world.qdir)
    stranger = SoftPasskey(RP)         # a passkey on the same name that is NOT enrolled
    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (url, _h):
        row = _row(url)
        q = urllib.parse.quote(EID, safe="")
        status, out = _call(url, f"/cockpit/envelope/{q}/revoke-challenge", {"signer_fp": stranger.fp, "instance": row["instance"]})
        assert (status, out["error"]) == (403, "not_a_signer"), out
        # the enrolled key named, the assertion made by the stranger's key: the verifier refuses it
        status, c = _call(url, f"/cockpit/envelope/{q}/revoke-challenge", {"signer_fp": world.phone.fp, "instance": row["instance"]})
        assert status == 200
        status, out = _call(url, f"/cockpit/envelope/{q}/revoke", stranger.assertion(c["challenge"], origin=PAGE))
        assert (status, out["error"]) == (403, "refused") and "nothing was revoked" in out["message"], out
        assert _row(url)["state"] == "live"
        # and an assertion over another challenge than the held one
        status, c = _call(url, f"/cockpit/envelope/{q}/revoke-challenge", {"signer_fp": world.phone.fp, "instance": row["instance"]})
        status, out = _call(url, f"/cockpit/envelope/{q}/revoke", world.phone.assertion("AAAA", origin=PAGE))
        assert status in (400, 403), out
        assert _row(url)["state"] == "live"


def test_a_changed_instance_and_an_unknown_envelope_are_refused(world, tmp_path) -> None:
    _seed(world.qdir)
    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (url, _h):
        q = urllib.parse.quote(EID, safe="")
        status, out = _call(url, f"/cockpit/envelope/{q}/revoke-challenge", {"signer_fp": world.phone.fp, "instance": "other"})
        assert (status, out["error"]) == (409, "grant_changed"), out
        for bad in (EID2, "not-an-id"):
            status, out = _call(url, f"/cockpit/envelope/{bad}/revoke-challenge", {"signer_fp": world.phone.fp, "instance": "x"})
            assert (status, out["error"]) == (404, "not_found"), (bad, out)


def test_killed_and_expired_envelopes_answer_a_plain_refusal(world, tmp_path) -> None:
    store = _seed(world.qdir)
    _seed(world.qdir, EID2, hours=-1)
    store.attenuate(EID, "kill")
    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (url, _h):
        assert (_row(url)["state"], _row(url, EID2)["state"]) == ("killed", "expired")
        for eid, word in ((EID, "already_ended"), (EID2, "expired")):
            row = _row(url, eid)
            status, out = _call(url, f"/cockpit/envelope/{eid}/revoke-challenge",
                                {"signer_fp": world.phone.fp, "instance": row["instance"]})
            assert (status, out["error"]) == (409, word) and "nothing was signed" in out["message"], (eid, out)


def test_the_store_comes_from_the_signers_queue_not_the_readers_directory(world, tmp_path) -> None:
    """Flow's bridge swaps the consent reader for one with no ``directory`` (hide_desk_panels); the envelope routes
    must not depend on it."""
    _seed(world.qdir)

    class NoDirectory:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            if name == "directory":
                raise AttributeError(name)
            return getattr(self._inner, name)

    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (url, httpd):
        httpd.consent_reader = NoDirectory(httpd.consent_reader)
        assert not hasattr(httpd.consent_reader, "directory")
        row = _row(url)
        assert row["state"] == "live"
        q = urllib.parse.quote(EID, safe="")
        status, c = _call(url, f"/cockpit/envelope/{q}/revoke-challenge", {"signer_fp": world.phone.fp, "instance": row["instance"]})
        assert status == 200
        status, out = _call(url, f"/cockpit/envelope/{q}/revoke", world.phone.assertion(c["challenge"], origin=PAGE))
        assert (status, out["revoked"]) == (200, EID), out


def test_without_a_signer_no_envelope_route_exists(world, tmp_path) -> None:
    _seed(world.qdir)
    with _serving(tmp_path / "inst") as (url, _h):
        assert _call(url, "/cockpit/envelope/list.json")[0] == 404
        for action in ("revoke-challenge", "revoke"):
            assert _call(url, f"/cockpit/envelope/{EID}/{action}", {})[0] == 404


def test_an_empty_or_missing_store_lists_nothing_and_makes_nothing(world, tmp_path) -> None:
    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (url, _h):
        status, listed = _call(url, "/cockpit/envelope/list.json")
        assert (status, listed["envelopes"]) == (200, []), listed
    assert not (world.qdir / "dispatch.db").exists() or not sqlite3.connect(
        f"file:{world.qdir / 'dispatch.db'}?mode=ro", uri=True).execute(
        "SELECT name FROM sqlite_master WHERE name = 'envelopes'").fetchall()


def test_a_localhost_passkey_lists_no_signer(world) -> None:
    _seed(world.qdir)
    er = EnvelopeRevoke(world.qdir, "localhost", None, lambda: {})
    out = er.listing()
    assert out["signers"] == [] and "localhost" in out["note"] and out["envelopes"][0]["state"] == "live"


def _ev(i: int) -> str:
    return "ev-" + f"{i:032x}"


def test_a_live_envelope_older_than_the_newest_fifty_is_listed_and_revocable(world, tmp_path) -> None:
    """The list caps the ENDED ones; a live envelope is never hidden, and the challenge reads one envelope by id."""
    _seed(world.qdir, EID, hours=24)                      # the oldest, live
    for i in range(1, 60):
        _seed(world.qdir, _ev(i), hours=-1)               # 59 newer, all expired
    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (url, _h):
        status, listed = _call(url, "/cockpit/envelope/list.json")
        assert status == 200, listed
        by_id = {e["id"]: e for e in listed["envelopes"]}
        assert by_id[EID]["state"] == "live", "the oldest live envelope is listed"
        assert sum(1 for e in listed["envelopes"] if e["state"] != "live") == 50, "only the ended are capped"
        q = urllib.parse.quote(EID, safe="")
        status, c = _call(url, f"/cockpit/envelope/{q}/revoke-challenge",
                          {"signer_fp": world.phone.fp, "instance": by_id[EID]["instance"]})
        assert status == 200, c
        status, out = _call(url, f"/cockpit/envelope/{q}/revoke", world.phone.assertion(c["challenge"], origin=PAGE))
        assert (status, out["revoked"]) == (200, EID), out
        # an ended envelope past the cap is still answered by id, plainly
        status, out = _call(url, f"/cockpit/envelope/{_ev(1)}/revoke-challenge",
                            {"signer_fp": world.phone.fp, "instance": "x"})
        assert (status, out["error"]) == (409, "expired"), out


def test_a_thread_scoped_kill_is_named_not_shown_as_plain_live(world, tmp_path) -> None:
    _seed(world.qdir)
    raw = sqlite3.connect(world.qdir / "dispatch.db")      # a scratch store: foreign keys are off on a fresh connection
    raw.execute("INSERT INTO envelope_binds (envelope_id, thread_id, rev, gseq, at) VALUES (?, 'th-1', 1, 9001, 'now')", (EID,))
    raw.execute("INSERT INTO envelope_binds (envelope_id, thread_id, rev, gseq, at) VALUES (?, 'th-2', 1, 9002, 'now')", (EID,))
    raw.execute("INSERT INTO envelope_attenuations (envelope_id, thread_id, kind, value, by, gseq, at) "
                "VALUES (?, 'th-1', 'kill', '', 'operator', 9003, 'now')", (EID,))
    raw.commit()
    raw.close()
    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (url, _h):
        row = _row(url)
        assert row["state"] == "live" and (row["threads"], row["ended_threads"]) == (2, 1), row


def test_a_store_fault_in_the_ceremony_is_a_plain_refusal_not_a_500(world, tmp_path, monkeypatch) -> None:
    from levain.dispatch.jobs import JobStore, JobStoreError

    _seed(world.qdir)
    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (url, _h):
        row = _row(url)
        q = urllib.parse.quote(EID, safe="")

        def boom(conn) -> str:
            raise JobStoreError("the dispatch store has no valid store_id")

        # the challenge reads the store id in the envelope's own snapshot
        monkeypatch.setattr(JobStore, "_store_id", staticmethod(boom))
        status, out = _call(url, f"/cockpit/envelope/{q}/revoke-challenge",
                            {"signer_fp": world.phone.fp, "instance": row["instance"]})
        assert (status, out["error"]) == (503, "unavailable") and "nothing was signed" in out["message"], (status, out)
        monkeypatch.undo()

        def broken(self, *a, **k):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(EnvelopeStore, "open_readonly", broken)
        status, out = _call(url, f"/cockpit/envelope/{q}/revoke-challenge",
                            {"signer_fp": world.phone.fp, "instance": row["instance"]})
        assert (status, out["error"]) == (503, "unavailable"), (status, out)


def test_the_signed_kill_leaves_its_proof_in_the_audit_line(world, tmp_path, caplog) -> None:
    _seed(world.qdir)
    caplog.set_level(logging.INFO, logger="levain.desk.envelope_revoke")
    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (url, _h):
        row = _row(url)
        q = urllib.parse.quote(EID, safe="")
        status, c = _call(url, f"/cockpit/envelope/{q}/revoke-challenge",
                          {"signer_fp": world.phone.fp, "instance": row["instance"]})
        nonce = c["line"].rstrip("\n").rsplit("nonce: ", 1)[1]
        status, out = _call(url, f"/cockpit/envelope/{q}/revoke", world.phone.assertion(c["challenge"], origin=PAGE))
        assert status == 200 and out["signer"] == world.phone.fp and out["nonce"] == nonce, out
    lines = [r.getMessage() for r in caplog.records if "passkey-signed revoke" in r.getMessage()]
    assert len(lines) == 1 and world.phone.fp in lines[0] and nonce in lines[0] and EID in lines[0], lines


def test_the_list_read_never_migrates_the_store(world, tmp_path, monkeypatch) -> None:
    _seed(world.qdir)

    def no_ensure(self, *a, **k):
        raise AssertionError("the list read ran the store's ensure/migrate path")

    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (url, _h):
        monkeypatch.setattr(EnvelopeStore, "_ensure", no_ensure)
        assert _row(url)["state"] == "live"


# --- L3 r1 fix round ---------------------------------------------------------------------------------------------------

def _move_store(src_dir, dst_dir) -> None:
    """Move a store's file (and any side files) from one directory to another, replacing what is there."""
    import os

    for suffix in ("", "-wal", "-shm"):
        src = src_dir / ("dispatch.db" + suffix)
        dst = dst_dir / ("dispatch.db" + suffix)
        if src.exists():
            os.replace(src, dst)
        elif dst.exists():
            dst.unlink()


def _challenged(url: str, world) -> dict[str, Any]:
    row = _row(url)
    q = urllib.parse.quote(EID, safe="")
    status, c = _call(url, f"/cockpit/envelope/{q}/revoke-challenge", {"signer_fp": world.phone.fp, "instance": row["instance"]})
    assert status == 200, c
    return c


def _kills(path) -> int:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT count(*) FROM envelope_attenuations").fetchone()[0]
    finally:
        conn.close()


def test_a_store_removed_between_challenge_and_revoke_refuses_and_makes_no_store(world, tmp_path) -> None:
    """codex HIGH-1: the kill lands only in the store that was signed for, and a missing one is never made."""
    _seed(world.qdir)
    aside = tmp_path / "aside"
    aside.mkdir(mode=0o700)
    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (url, _h):
        c = _challenged(url, world)
        _move_store(world.qdir, aside)
        q = urllib.parse.quote(EID, safe="")
        status, out = _call(url, f"/cockpit/envelope/{q}/revoke", world.phone.assertion(c["challenge"], origin=PAGE))
        assert status == 409 and "nothing was revoked" in out["message"], (status, out)
        assert not (world.qdir / "dispatch.db").exists(), "a revoke made a store"
    assert _kills(aside / "dispatch.db") == 0


def test_a_store_swapped_between_challenge_and_revoke_refuses_and_writes_neither(world, tmp_path) -> None:
    """codex HIGH-1: another store holding the same envelope id is not the one the operator signed for."""
    _seed(world.qdir)
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    _seed(other)                                           # the same ev- id, the same digest, a different store_id
    aside = tmp_path / "aside"
    aside.mkdir(mode=0o700)
    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (url, _h):
        c = _challenged(url, world)
        _move_store(world.qdir, aside)
        _move_store(other, world.qdir)
        q = urllib.parse.quote(EID, safe="")
        status, out = _call(url, f"/cockpit/envelope/{q}/revoke", world.phone.assertion(c["challenge"], origin=PAGE))
        assert status == 409 and "nothing was revoked" in out["message"], (status, out)
    assert _kills(aside / "dispatch.db") == 0 and _kills(world.qdir / "dispatch.db") == 0


def test_one_malformed_envelope_never_empties_the_listing(world, tmp_path) -> None:
    """deepseek HIGH-1: a bad count and a bad stop_earlier stamp are each one row, never the whole list."""
    eid3 = "ev-" + "c3" * 16
    _seed(world.qdir)
    _seed(world.qdir, EID2, body_changes={"launches": [{"repo": "/tmp/scratch/flow", "count": None}]})
    _seed(world.qdir, eid3)
    raw = sqlite3.connect(world.qdir / "dispatch.db")
    raw.execute("INSERT INTO envelope_attenuations (envelope_id, thread_id, kind, value, by, gseq, at) "
                "VALUES (?, NULL, 'stop_earlier', 'not-a-stamp', 'operator', 9101, 'now')", (eid3,))
    raw.commit()
    raw.close()
    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (url, _h):
        status, listed = _call(url, "/cockpit/envelope/list.json")
        assert status == 200, listed
        by_id = {e["id"]: e for e in listed["envelopes"]}
        assert set(by_id) == {EID, EID2, eid3}, listed
        assert by_id[EID]["state"] == "live"
        assert by_id[EID2]["state"] == "live" and by_id[EID2]["scope"] == "flow · 8 h", by_id[EID2]
        assert by_id[eid3]["state"] == "live-unknown", by_id[eid3]
        assert listed["signers"] == [world.phone.fp], listed
        # the live-unknown one is still revocable
        q = urllib.parse.quote(eid3, safe="")
        status, c = _call(url, f"/cockpit/envelope/{q}/revoke-challenge",
                          {"signer_fp": world.phone.fp, "instance": by_id[eid3]["instance"]})
        assert status == 200, c
        status, out = _call(url, f"/cockpit/envelope/{q}/revoke", world.phone.assertion(c["challenge"], origin=PAGE))
        assert (status, out.get("revoked")) == (200, eid3), out


def test_an_unreadable_envelope_is_a_row_with_its_revoke(world, tmp_path) -> None:
    """deepseek HIGH-1: a row that fails to read is listed as unreadable, by id, never dropped."""
    _seed(world.qdir)
    _seed(world.qdir, EID2, stop_at="garbage")
    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (url, _h):
        status, listed = _call(url, "/cockpit/envelope/list.json")
        assert status == 200, listed
        by_id = {e["id"]: e for e in listed["envelopes"]}
        assert by_id[EID]["state"] == "live" and by_id[EID2]["state"] == "unreadable", listed
        assert by_id[EID2]["note"] and by_id[EID2]["instance"], by_id[EID2]
        q = urllib.parse.quote(EID2, safe="")
        status, c = _call(url, f"/cockpit/envelope/{q}/revoke-challenge",
                          {"signer_fp": world.phone.fp, "instance": by_id[EID2]["instance"]})
        assert status == 200, c
        status, out = _call(url, f"/cockpit/envelope/{q}/revoke", world.phone.assertion(c["challenge"], origin=PAGE))
        assert (status, out.get("revoked")) == (200, EID2), out


def test_a_second_challenge_returns_the_held_message(world, tmp_path) -> None:
    """deepseek MED-2: two tabs or a double tap sign the same bytes; the held message is still used once."""
    _seed(world.qdir)
    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (url, _h):
        c1 = _challenged(url, world)
        c2 = _challenged(url, world)
        assert (c1["line"], c1["challenge"]) == (c2["line"], c2["challenge"])
        q = urllib.parse.quote(EID, safe="")
        status, out = _call(url, f"/cockpit/envelope/{q}/revoke", world.phone.assertion(c1["challenge"], origin=PAGE))
        assert (status, out.get("revoked")) == (200, EID), out
        status, out = _call(url, f"/cockpit/envelope/{q}/revoke", world.phone.assertion(c2["challenge"], origin=PAGE))
        assert (status, out["error"]) == (409, "no_challenge"), out


def test_threads_counts_distinct_threads_across_a_rebind(world, tmp_path) -> None:
    """deepseek MED-3: one thread is one thread however often it is bound. The bind table's key is (envelope, thread), so
    a second row for one thread is refused by the schema; the count reads distinct threads all the same."""
    _seed(world.qdir)
    raw = sqlite3.connect(world.qdir / "dispatch.db")
    raw.execute("INSERT INTO envelope_binds (envelope_id, thread_id, rev, gseq, at) VALUES (?, 'th-1', 1, 9001, 'now')", (EID,))
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("INSERT INTO envelope_binds (envelope_id, thread_id, rev, gseq, at) VALUES (?, 'th-1', 2, 9002, 'now')",
                    (EID,))
    raw.commit()
    raw.close()
    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (url, _h):
        assert _row(url)["threads"] == 1


def test_an_envelope_that_ends_after_the_signature_says_so_and_logs_the_nonce(world, tmp_path, caplog) -> None:
    """deepseek LOW-5 / complement MED-LOW-1: after a verified signature the refusal never says nothing was signed."""
    store = _seed(world.qdir)
    caplog.set_level(logging.INFO, logger="levain.desk.envelope_revoke")
    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (url, _h):
        c = _challenged(url, world)
        nonce = c["line"].rstrip("\n").rsplit("nonce: ", 1)[1]
        store.attenuate(EID, "kill")                        # the terminal's kill lands first
        q = urllib.parse.quote(EID, safe="")
        status, out = _call(url, f"/cockpit/envelope/{q}/revoke", world.phone.assertion(c["challenge"], origin=PAGE))
        assert status == 409, out
        assert "ended before your signature landed" in out["message"] and "nothing was signed" not in out["message"], out
    assert any(nonce in r.getMessage() and EID in r.getMessage() for r in caplog.records), \
        [r.getMessage() for r in caplog.records]


def test_the_no_signer_note_names_the_envelope_kill(world) -> None:
    """codex LOW-3: the laptop fallback for an envelope is `levain envelope kill`, never the grant revoke."""
    _seed(world.qdir)
    note = EnvelopeRevoke(world.qdir, "localhost", None, lambda: {}).listing()["note"]
    assert "levain envelope kill" in note and "consent grant revoke" not in note, note
    note = EnvelopeRevoke(world.qdir, RP, PAGE, lambda: {}).listing()["note"]
    assert "levain envelope kill" in note and "consent grant revoke" not in note, note


def test_a_store_without_envelope_tables_answers_not_found(world, tmp_path) -> None:
    """codex LOW-4 / complement LOW-2: a desk store never migrated for envelopes holds no envelope: 404."""
    from levain.desk.store import CoordStore

    CoordStore(world.qdir).accept_drop("a drop", surface="phone")
    raw = sqlite3.connect(world.qdir / "dispatch.db")
    assert not raw.execute("SELECT 1 FROM sqlite_master WHERE name = 'envelopes'").fetchall(), "the scratch store has envelopes"
    raw.close()
    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (url, _h):
        status, out = _call(url, f"/cockpit/envelope/{EID}/revoke-challenge", {"signer_fp": world.phone.fp, "instance": "x"})
        assert (status, out["error"]) == (404, "not_found"), (status, out)


def test_the_list_builds_full_rows_only_for_what_it_shows(world, tmp_path, monkeypatch) -> None:
    """complement LOW-3: ended envelopes past the cap are never built in full."""
    _seed(world.qdir, EID, hours=24)
    for i in range(1, 60):
        _seed(world.qdir, _ev(i), hours=-1)
    built: list[str] = []
    real = EnvelopeStore._envelope

    def counting(conn, envelope_id):
        built.append(envelope_id)
        return real(conn, envelope_id)

    monkeypatch.setattr(EnvelopeStore, "_envelope", staticmethod(counting))
    rows = EnvelopeRevoke(world.qdir, RP, PAGE, lambda: {})._rows()
    assert len(rows) == 51 and len(built) == 51, (len(rows), len(built))
