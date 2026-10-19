"""The phone port's dispatch routes (``/dispatch/<job>/...`` and the ``dispatch_*`` listing keys): the same token,
JSON, size and origin gates as a consent answer, over the ONE cockpit the sign requests use. The token opens the
route; only a passkey assertion the job store verifies signs anything."""
from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from levain.consent.queue import ConsentQueue
from levain.dispatch.jobs import DB_NAME, JobStore
from levain.phone_server import make_phone_server
from tests.test_dispatch_phone import RP, file_job, phone_for, refused, repo, world  # noqa: F401, F811  (fixtures)
from tests.test_phone_server import _TOK, _call

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
        status, c = _call(base + f"/dispatch/{job.id}/challenge", {"signer_fp": key.fp})
        assert status == 200 and c["payload"]["job_id"] == job.id
        answer = {"payload": c["payload"], **key.assertion(c["challenge"], origin)}
        status, done = _call(base + f"/dispatch/{job.id}/answer", answer)
        assert (status, done["state"]) == (200, "signed")
        status, again = _call(base + f"/dispatch/{job.id}/answer", answer)
        assert (status, again["error"]) == (409, "conflict")
        status, cc = _call(base + f"/dispatch/{job.id}/cancel-challenge", {"signer_fp": key.fp, "reason": "no"})
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
        assert _call(base + f"/dispatch/{job.id}/challenge", {"signer_fp": key.fp},
                     headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert _call(base + f"/dispatch/{job.id}/challenge", {"signer_fp": key.fp}, content_type="text/plain")[0] == 415
        assert _call(base + "/dispatch/not-a-job/challenge", {"signer_fp": key.fp})[0] == 404
        assert _call(base + f"/dispatch/{job.id}/sign", {"signer_fp": key.fp})[0] == 404
        # the run token with a payload the server issued, and no assertion the passkey made: nothing is signed
        c = _call(base + f"/dispatch/{job.id}/challenge", {"signer_fp": key.fp})[1]
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
        status, body = _call(base + f"/dispatch/{job.id}/challenge", {"signer_fp": world["phone"].fp})
        assert (status, body["error"]) == (503, "unavailable") and "names no cockpit" in body["message"]
        (row,) = _call(base + "/consent.json")[1]["dispatch_pending"]
        assert row["signers"] == [] and "names no cockpit" in row["signer_note"]


def test_the_dispatch_dir_defaults_to_the_dispatch_stores_own_default(tmp_path: Path, monkeypatch) -> None:
    from levain.dispatch.jobs import default_dispatch_dir

    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))
    srv = make_phone_server(tmp_path, "localhost", port=0)
    try:
        assert srv.dispatch_dir == default_dispatch_dir() == (tmp_path / "home" / "consent").resolve()
    finally:
        srv.server_close()


def test_a_dispatch_signature_is_never_a_consent_answer_and_a_consent_answer_never_a_dispatch_signature(world) -> None:
    from levain.phone_server import PhoneError, make_phone_server

    job = file_job(world)
    dp = phone_for(world, page_origin=None)
    key = world["phone"]
    queue = ConsentQueue(world["qdir"], authority=world["source"])
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


