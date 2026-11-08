"""Board row 153: the cockpit lists coordinator envelopes and ends one with a passkey-signed revoke
(:class:`levain.coordinator.envelope_revoke.EnvelopeRevoke`), the grant revoke's own ceremony and gate over the store the
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
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.envelope_revoke import NAMESPACE, EnvelopeRevoke
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


def _seed(qdir, eid: str = EID, *, hours: float = 24, words: str = "run the morning seats on flow") -> EnvelopeStore:
    """One envelope row in a scratch store, as a signed envelope leaves it (body, digest and stop are what is read)."""
    store = EnvelopeStore(qdir)
    drop, _ = store.accept_drop(words, surface="phone")
    now = datetime.now(timezone.utc)
    body = {"envelope_id": eid, "drop_id": drop.drop_id, "issued_at": _stamp(now), "stop_at": _stamp(now + timedelta(hours=hours)),
            "where": [{"repo": "/tmp/scratch/flow", "paths": ["."]}], "launches": [{"repo": "/tmp/scratch/flow", "count": 12}],
            "budget": {"hours": 8}, "origin": "structured"}
    with store._write() as (conn, at):
        conn.execute("INSERT INTO envelopes (envelope_id, gseq, origin, drop_id, body, digest, payload, signature, "
                     "signer_fp, signer_class, supersedes, stop_at, accepted_at) VALUES (?, ?, 'structured', ?, ?, ?, '{}', "
                     "'sig', 'SHA256:x', 'display-phone', NULL, ?, ?)",
                     (eid, store._next_gseq(conn, at), drop.drop_id, json.dumps(body), "digest-" + eid[3:9], body["stop_at"], at))
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
    """Flow's bridge swaps the consent reader for one with no ``directory`` (hide_coordinator_panels); the envelope routes
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
    from levain.dispatch.jobs import JobStoreError

    _seed(world.qdir)
    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (url, _h):
        row = _row(url)
        q = urllib.parse.quote(EID, safe="")

        def boom(self) -> str:
            raise JobStoreError("the dispatch store has no valid store_id")

        monkeypatch.setattr(EnvelopeStore, "store_id", boom)
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
    caplog.set_level(logging.INFO, logger="levain.coordinator.envelope_revoke")
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
