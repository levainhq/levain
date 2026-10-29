"""The phone port's dispatch routes (``/dispatch/<job>/...`` and the ``dispatch_*`` listing keys): the same token,
JSON, size and origin gates as a consent answer, over the ONE cockpit the sign requests use. The token opens the
route; only a passkey assertion the job store verifies signs anything."""
from __future__ import annotations

import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from levain.consent.queue import ConsentQueue
from levain.dispatch.jobs import DB_NAME, JobStore
from levain.phone_server import make_phone_server
from tests.test_dispatch_phone import RP, file_job, phone_for, refused, repo, world  # noqa: F401, F811  (fixtures)
from tests.test_phone_server import _TOK, _call
from tests._h4 import h4_home

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def _serve(qdir: Path, dispatch_dir: Path):
    import threading
    from contextlib import contextmanager

    @contextmanager
    def cm():
        httpd = make_phone_server(qdir, "localhost", port=0, token=_TOK, dispatch_dir=dispatch_dir)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{httpd.server_address[1]}", httpd
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
    return cm()


def _v(base: str, job_id: str) -> str:
    """The version the phone's card for ``job_id`` shows: the listing's row ``sha256``."""
    (row,) = [r for r in _call(base + "/consent.json")[1]["dispatch_pending"] if r["id"] == job_id]
    return row["sha256"]


def test_the_listing_carries_the_three_dispatch_keys_and_makes_no_store(world) -> None:
    with _serve(world["qdir"], world["qdir"]) as (base, _):
        status, body = _call(base + "/consent.json")
    assert status == 200
    assert (body["dispatch_pending"], body["dispatch_note"], body["dispatch_overflow"]) == ([], None, 0)
    assert not (world["qdir"] / DB_NAME).exists()
    job = file_job(world)
    with _serve(world["qdir"], world["qdir"]) as (base, _):
        (row,) = _call(base + "/consent.json")[1]["dispatch_pending"]
    assert row["id"] == job.id and row["signers"] == [world["phone"].fp] and row["can_sign"] is True


def test_a_passkey_signs_and_cancels_over_http_and_a_replay_is_409(world) -> None:
    job = file_job(world)
    key = world["phone"]
    with _serve(world["qdir"], world["qdir"]) as (base, httpd):
        origin = httpd.page_origin
        status, c = _call(base + f"/dispatch/{job.id}/challenge", {"signer_fp": key.fp, "request_sha256": _v(base, job.id)})
        assert status == 200 and c["payload"]["job_id"] == job.id
        answer = {"payload": c["payload"], **key.assertion(c["challenge"], origin)}
        status, done = _call(base + f"/dispatch/{job.id}/answer", answer)
        assert (status, done["state"]) == (200, "signed")
        status, again = _call(base + f"/dispatch/{job.id}/answer", answer)
        assert (status, again["error"]) == (409, "conflict")
        status, cc = _call(base + f"/dispatch/{job.id}/cancel-challenge",
                           {"signer_fp": key.fp, "reason": "no", "request_sha256": _v(base, job.id)})
        assert status == 200
        status, done = _call(base + f"/dispatch/{job.id}/cancel",
                             {"payload": cc["payload"], **key.assertion(cc["challenge"], origin)})
        assert (status, done["state"]) == (200, "cancelled")
    assert JobStore(world["qdir"]).get(job.id).state == "cancelled"


def test_the_token_opens_the_route_and_never_signs(world) -> None:
    job = file_job(world)
    key = world["phone"]
    with _serve(world["qdir"], world["qdir"]) as (base, httpd):
        route = base + f"/dispatch/{job.id}/answer"
        status, body = _call(route, {"payload": {}}, token=None)
        assert (status, body["error"]) == (403, "credential_required")
        ver = _v(base, job.id)
        assert _call(base + f"/dispatch/{job.id}/challenge", {"signer_fp": key.fp, "request_sha256": ver},
                     headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert _call(base + f"/dispatch/{job.id}/challenge", {"signer_fp": key.fp}, content_type="text/plain")[0] == 415
        assert _call(base + "/dispatch/not-a-job/challenge", {"signer_fp": key.fp})[0] == 404
        assert _call(base + f"/dispatch/{job.id}/sign", {"signer_fp": key.fp})[0] == 404
        # the run token with a payload the server issued, and no assertion the passkey made: nothing is signed
        c = _call(base + f"/dispatch/{job.id}/challenge", {"signer_fp": key.fp, "request_sha256": ver})[1]
        forged = {"payload": c["payload"], "authenticator_data": "AAAA", "client_data_json": "AAAA", "signature": "AAAA"}
        status, body = _call(route, forged)
        assert status == 422
        # an assertion over the challenge, made on another page origin, is refused too
        status, body = _call(route, {"payload": c["payload"], **key.assertion(c["challenge"], "http://localhost:1")})
        assert status == 422
    assert JobStore(world["qdir"]).get(job.id).state == "filed"


def test_every_dispatch_route_is_503_unless_one_cockpit_is_named(world, tmp_path: Path) -> None:
    job = file_job(world)
    lone = tmp_path / "lone-queue"
    lone.mkdir(mode=0o700)
    with _serve(lone, world["qdir"]) as (base, _):
        status, body = _call(base + f"/dispatch/{job.id}/challenge",
                             {"signer_fp": world["phone"].fp, "request_sha256": _v(base, job.id)})
        assert (status, body["error"]) == (503, "unavailable") and "names no cockpit" in body["message"]
        (row,) = _call(base + "/consent.json")[1]["dispatch_pending"]
        assert row["signers"] == [] and "names no cockpit" in row["signer_note"]


def test_the_dispatch_dir_defaults_to_the_queue_dir(tmp_path: Path, monkeypatch) -> None:
    # L3 r1 C: the dispatch directory IS the consent directory, so the keys a card offers and the keys its land
    # verifies come from one directory's consent settings (`levain consent phone --queue-dir X` included)
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))
    qdir = tmp_path / "queue"
    qdir.mkdir()
    srv = make_phone_server(qdir, "localhost", port=0)
    try:
        assert srv.dispatch_dir == qdir
    finally:
        srv.server_close()


def test_a_dispatch_signature_is_never_a_consent_answer_and_a_consent_answer_never_a_dispatch_signature(world) -> None:
    from levain.phone_server import PhoneError, make_phone_server

    job = file_job(world)
    dp = phone_for(world, page_origin=None)
    key = world["phone"]
    queue = ConsentQueue(world["qdir"], authority=world["source"], home=h4_home())
    rid = queue.file(agent="claude_code", agent_session_id="s", tool_name="Write",
                     tool_input={"file_path": "/tmp/x.txt", "content": "hi"}, cwd="/w", hold=timedelta(minutes=5),
                     tier="T2", event_id="dispatch-phone-1").id
    srv = make_phone_server(world["qdir"], RP, port=0, dispatch_dir=world["qdir"])
    try:
        origin = srv.page_origin
        consent = srv.answer_step(rid, "challenge", {"decision": "allow", "signer_fp": key.fp})
        sc = dp.step(job.id, "challenge", {"signer_fp": key.fp})
        # a dispatch-sign assertion presented as the consent answer
        with pytest.raises(PhoneError) as exc:
            srv.answer_step(rid, "answer", {"payload": consent["payload"], **key.assertion(sc["challenge"], origin)})
        assert exc.value.status == 422
        # a consent answer's assertion presented as a dispatch signature
        err = refused(dp.step, job.id, "answer", {"payload": sc["payload"], **key.assertion(consent["challenge"], origin)})
        assert err.status == 422
        assert queue.get(rid)[1] == "pending" and JobStore(world["qdir"]).get(job.id).state == "filed"
        # and each, signed over its own challenge, is good where it belongs
        srv.answer_step(rid, "answer", {"payload": consent["payload"], **key.assertion(consent["challenge"], origin)})
        dp.step(job.id, "answer", {"payload": sc["payload"], **key.assertion(sc["challenge"], origin)})
    finally:
        srv.server_close()




def test_a_dispatch_dir_that_is_a_file_leaves_the_consent_listing_intact(world, tmp_path: Path) -> None:
    queue = ConsentQueue(world["qdir"], authority=world["source"], home=h4_home())
    rid = queue.file(agent="claude_code", agent_session_id="s", tool_name="Write",
                     tool_input={"file_path": "/tmp/x.txt", "content": "hi"}, cwd="/w", hold=timedelta(minutes=5),
                     tier="T2", event_id="dispatch-flat-1").id
    flat = tmp_path / "flat"
    flat.write_text("a file, not a directory")
    with _serve(world["qdir"], flat) as (base, _):
        status, body = _call(base + "/consent.json")
    assert status == 200
    assert [r["id"] for r in body["pending"]] == [rid]
    assert body["dispatch_pending"] == [] and body["dispatch_overflow"] == 0 and "cannot be read" in body["dispatch_note"]


# --- fix round 5 -----------------------------------------------------------------------------------------------------

def test_a_faulting_keys_source_keeps_the_consent_cards_and_lists_the_jobs_unsignable(world, monkeypatch) -> None:
    """The server's one keys source faults (the cockpit's keys db): the listing still answers 200, the consent cards
    are all there, and the dispatch jobs are shown with no signer and the fault as their note, so no Sign can start."""
    import sqlite3

    import levain.consent.answering as answering

    job = file_job(world)
    queue = ConsentQueue(world["qdir"], authority=world["source"], home=h4_home())
    rid = queue.file(agent="claude_code", agent_session_id="s", tool_name="Write",
                     tool_input={"file_path": "/tmp/x.txt", "content": "hi"}, cwd="/w", hold=timedelta(minutes=5),
                     tier="T2", event_id="dispatch-keys-fault-1").id

    def broken(*_a, **_k):
        raise sqlite3.OperationalError("keys db")

    monkeypatch.setattr(answering, "passkey_keys", broken)
    with _serve(world["qdir"], world["qdir"]) as (base, _):
        status, body = _call(base + "/consent.json")
    assert status == 200
    assert [r["id"] for r in body["pending"]] == [rid]                       # the consent card survives the fault
    (row,) = body["dispatch_pending"]
    assert row["id"] == job.id and row["signers"] == [] and "keys db" in row["signer_note"]
    assert row["can_sign"] is False and row["can_cancel"] is True          # no signer: nothing offers to sign
    assert body["dispatch_overflow"] == 0


def test_the_authorise_routes_are_served_behind_the_same_gates(world) -> None:
    # F11-s2b §5: authorise-challenge and authorise reach DispatchPhone.step; a filed job has no card to authorise
    job = file_job(world)
    key = world["phone"]
    with _serve(world["qdir"], world["qdir"]) as (base, _):
        assert _call(base + f"/dispatch/{job.id}/authorise-challenge", {"signer_fp": key.fp}, token=None)[0] == 403
        status, body = _call(base + f"/dispatch/{job.id}/authorise-challenge",
                             {"signer_fp": key.fp, "request_sha256": _v(base, job.id)})
        assert (status, body["error"]) == (409, "conflict") and "from the laptop" in body["message"]
        status, body = _call(base + f"/dispatch/{job.id}/authorise", {"payload": {}})
        assert (status, body["error"]) == (400, "bad_request")
        (row,) = _call(base + "/consent.json")[1]["dispatch_pending"]
        assert row["authorise"] is None
    assert JobStore(world["qdir"]).get(job.id).state == "filed"


# --- 1029-5: the phone's dispatch challenge names the version its card showed (as the cockpit sheet's does) ------------

def _amended(httpd: Any, job: Any, monkeypatch: Any, digest: str = "d" * 64) -> None:
    """The job read as amended (its contract's digest another) from here on; the version is over the record."""
    phone = httpd.dispatch
    real = phone.record

    def record(j: Any, now: str, signers: "list[str]", note: str) -> "dict[str, Any]":
        rec = real(j, now, signers, note)
        if j.id == job.id:
            rec["digest"] = digest
            rec["sha256"] = phone.version(rec)      # the listed version is over the record, as record() makes it
        return rec

    monkeypatch.setattr(phone, "record", record)


def _no_step(httpd: Any, monkeypatch: Any) -> "list[Any]":
    calls: "list[Any]" = []
    real = httpd.dispatch.step
    monkeypatch.setattr(httpd.dispatch, "step", lambda *a: calls.append(a) or real(*a))
    return calls


def test_a_job_amended_between_the_card_and_the_tap_is_refused_at_challenge_time(world, monkeypatch) -> None:
    """Reproduced red before the fix: the phone posted only {signer_fp}, so a job amended after the card was drawn was
    challenged in its NEW state. Now the challenge names the listing's sha256 and a job that moved is a 409, every act."""
    job = file_job(world)
    key = world["phone"]
    with _serve(world["qdir"], world["qdir"]) as (base, httpd):
        shown = _v(base, job.id)
        _amended(httpd, job, monkeypatch)
        assert _v(base, job.id) != shown
        for action, body in (("challenge", {"signer_fp": key.fp}), ("cancel-challenge", {"signer_fp": key.fp, "reason": ""}),
                             ("authorise-challenge", {"signer_fp": key.fp})):
            status, out = _call(base + f"/dispatch/{job.id}/{action}", {**body, "request_sha256": shown})
            assert (status, out["error"]) == (409, "conflict"), (action, out)
            assert "changed since it was shown" in out["message"] and "challenge" not in out, (action, out)
        assert "NOT cancelled" in _call(base + f"/dispatch/{job.id}/cancel-challenge",
                                        {"signer_fp": key.fp, "reason": "", "request_sha256": shown})[1]["message"]
        # read again, the new version is challenged
        status, c = _call(base + f"/dispatch/{job.id}/challenge", {"signer_fp": key.fp, "request_sha256": _v(base, job.id)})
        assert status == 200 and c["payload"]["job_id"] == job.id
    assert JobStore(world["qdir"]).get(job.id).state == "filed"


def test_a_challenge_without_a_usable_version_is_the_plain_409_before_the_phone_port_is_asked(world, monkeypatch) -> None:
    job = file_job(world)
    key = world["phone"]
    with _serve(world["qdir"], world["qdir"]) as (base, httpd):
        calls = _no_step(httpd, monkeypatch)
        for action in ("challenge", "cancel-challenge", "authorise-challenge"):
            for body in ({"signer_fp": key.fp}, {"signer_fp": key.fp, "request_sha256": ""},
                         {"signer_fp": key.fp, "request_sha256": 7}, [key.fp], "x", 7):
                status, out = _call(base + f"/dispatch/{job.id}/{action}", body)
                assert (status, out["error"]) == (409, "conflict"), (action, body, out)
                assert "names no listed version" in out["message"], (action, body, out)
        # the key is the port's, never part of the dispatch body: a stray key beside it is still the 400
        status, out = _call(base + f"/dispatch/{job.id}/challenge",
                            {"signer_fp": key.fp, "request_sha256": _v(base, job.id), "x": 1})
        assert (status, out["error"]) == (400, "bad_request")
    assert len(calls) == 1 and JobStore(world["qdir"]).get(job.id).state == "filed"


def test_a_job_amended_while_its_phone_challenge_is_built_is_refused(world, monkeypatch) -> None:
    job = file_job(world)
    key = world["phone"]
    with _serve(world["qdir"], world["qdir"]) as (base, httpd):
        real = httpd.dispatch.step

        def step(job_id: str, action: str, req: Any) -> Any:
            out = real(job_id, action, req)
            _amended(httpd, job, monkeypatch)          # the job changes while this challenge was built
            return out

        monkeypatch.setattr(httpd.dispatch, "step", step)
        status, out = _call(base + f"/dispatch/{job.id}/challenge", {"signer_fp": key.fp, "request_sha256": _v(base, job.id)})
        assert (status, out["error"]) == (409, "conflict") and "changed since it was shown" in out["message"], out
        assert "challenge" not in out
    assert JobStore(world["qdir"]).get(job.id).state == "filed"


def test_a_phone_store_fault_at_the_version_compare_names_its_type_never_its_text(world, monkeypatch) -> None:
    job = file_job(world)
    with _serve(world["qdir"], world["qdir"]) as (base, httpd):
        ver = _v(base, job.id)

        def broken(_job_id: str) -> Any:
            raise sqlite3.OperationalError("/Users/someone/.levain/consent/dispatch.db: no such table: jobs")

        monkeypatch.setattr(httpd.dispatch, "_current", broken)
        calls = _no_step(httpd, monkeypatch)
        status, out = _call(base + f"/dispatch/{job.id}/challenge", {"signer_fp": world["phone"].fp, "request_sha256": ver})
    assert (status, out["error"]) == (503, "unavailable"), out
    assert "OperationalError" in out["message"] and "nothing was signed" in out["message"], out
    assert "/Users" not in out["message"] and "dispatch.db" not in out["message"] and "jobs" not in out["message"], out
    assert calls == []


def test_an_unreadable_phone_row_is_never_challenged_even_on_its_own_version(world, monkeypatch) -> None:
    job = file_job(world)
    with _serve(world["qdir"], world["qdir"]) as (base, httpd):
        ver = _v(base, job.id)
        calls = _no_step(httpd, monkeypatch)

        def broken(*_a: Any, **_k: Any) -> Any:
            raise KeyError("template")

        monkeypatch.setattr(httpd.dispatch, "record", broken)
        for action, body in (("challenge", {}), ("cancel-challenge", {"reason": ""})):
            status, out = _call(base + f"/dispatch/{job.id}/{action}",
                                {"signer_fp": world["phone"].fp, "request_sha256": ver, **body})
            assert (status, out["error"]) == (409, "conflict"), (action, out)
            assert "could not be read" in out["message"] and "challenge" not in out, (action, out)
    assert calls == [] and JobStore(world["qdir"]).get(job.id).state == "filed"


def test_the_phone_and_the_cockpit_name_one_job_by_one_version(world) -> None:
    """The phone's listed sha256 is the very version the cockpit sheet binds (one function, not two)."""
    from levain.dispatch.phone import DispatchPhone
    from levain.web_server import DispatchSheet

    rec = {"id": "dj-x", "digest": "a" * 64, "state": "filed", "lease_end": "t", "authorise": {"run": 1, "onto": "main", "tier": "T2"}}
    assert DispatchSheet.version(rec) == DispatchPhone.version(rec)
