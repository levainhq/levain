"""ONESURFACE OS-2, the dispatch family on the cockpit: a dispatch job signed, cancelled or authorised with a passkey
through the cockpit server, by the phone port's OWN object (:class:`levain.dispatch.phone.DispatchPhone`, built by
:class:`levain.web_server.DispatchSheet` over the Signer's queue, keys, rpId and pinned page). These are the phone port's
dispatch route tests (tests/test_phone_dispatch.py) ported onto ``/cockpit/dispatch/<job>/...``, run against a real
authority log (a laptop key and a software passkey enrolled on ``phone.example``), a real job store and real
``ssh-keygen`` verification; plus the cockpit's own: the sign routes' gate, the sheet's read and listing, and the
refusal before any ceremony on a page the verification would refuse.

Reproduced red before the build: with a Signer attached, ``POST /cockpit/dispatch/<job>/challenge`` and
``GET /cockpit/dispatch.json`` were 404."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from levain import authority as auth
from levain.cockpit.firejournal import CockpitDB
from levain.cockpit.keys import fingerprint_of
from levain.consent.answering import passkey_keys
from levain.consent.queue import authority_source
from levain.consent.sign import Signer, passkey_key_source
from levain.dispatch.jobs import DB_NAME, JobStore
from tests.test_cockpit_sign_routes import _call, _serving
from tests.test_dispatch_phone import SoftPasskey, _b64u, file_job, repo  # noqa: F401  (repo is a fixture)
from tests.test_phone_server import _enrol

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

RP = "phone.example"
NS = "flow-needs-phill"
PAGE = f"https://{RP}"
PORTED = f"https://{RP}:8443"


@pytest.fixture
def world(tmp_path: Path, repo):  # noqa: F811
    """The phone dispatch world (tests/test_dispatch_phone.py's) with its passkey enrolled on ``phone.example``."""
    laptop = tmp_path / "laptop"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "laptop", "-f", str(laptop)], check=True)
    lpub = " ".join((tmp_path / "laptop.pub").read_text().split()[:2])
    lfp = fingerprint_of(lpub)
    root = tmp_path / "cockpit"
    root.mkdir()
    CockpitDB(root).set_install_class("reported")
    phone = SoftPasskey(RP)
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
    source = authority_source(root / "cockpit.db", anchor=auth.anchor_path(root))
    return {"qdir": qdir, "root": root, "repo": repo, "phone": phone, "source": source}


def _signer(world: Any, origin: str = PAGE, qdir: "Path | None" = None, keys: Any = None) -> Signer:
    source = world["source"]
    signer = Signer(qdir or world["qdir"], (NS,), RP,
                    keys or (lambda: {k.fingerprint: k for k in passkey_keys(source(), RP)}))
    signer.page_origin = origin
    return signer


def _serve(world: Any, tmp_path: Path, **kw: Any):
    return _serving(tmp_path / "inst", signer=kw.pop("signer", None) or _signer(world), extra_hosts=[RP], **kw)


def _state(world: Any, job_id: str) -> str:
    return JobStore(world["qdir"]).get(job_id).state


def test_the_listing_is_empty_and_makes_no_store_then_names_a_job_with_what_a_passkey_may_do(world, tmp_path) -> None:
    """Ported: test_phone_dispatch.test_the_listing_carries_the_three_dispatch_keys_and_makes_no_store."""
    key = world["phone"]
    with _serve(world, tmp_path) as (url, _httpd):
        status, body = _call(url, "/cockpit/dispatch.json")
        assert status == 200 and (body["requests"], body["note"], body["overflow"]) == ([], None, 0), body
        assert not (world["qdir"] / DB_NAME).exists()
        job = file_job(world)
        status, body = _call(url, "/cockpit/dispatch.json")
        (row,) = body["requests"]
        assert status == 200 and body["origin"] == PAGE and body["rp_id"] == RP
        assert row["id"] == job.id and row["kind"] == "dispatch" and row["signers"] == [key.fp], row
        assert [o["key"] for o in row["options"]] == ["sign", "cancel"] and len(row["sha256"]) == 64, row
        assert all(o["signers"] == [key.fp] for o in row["options"]) and row["reason"]["max"] > 0
        assert job.contract.goal.splitlines()[0] in row["message"]
        # the one-job read the sheet makes
        status, got = _call(url, f"/cockpit/dispatch/{job.id}/sheet.json")
        assert status == 200 and got["request"] == row and got["signers"] == [key.fp], got
        # the sheet's page is the same shell as the sign sheet's
        status, page = _call(url, f"/sign/dispatch/{job.id}", token=None)
        assert status == 200 and b"/sign.js" in page


def test_a_passkey_signs_and_cancels_over_http_and_a_replay_is_409(world, tmp_path) -> None:
    """Ported: test_phone_dispatch.test_a_passkey_signs_and_cancels_over_http_and_a_replay_is_409."""
    job = file_job(world)
    key = world["phone"]
    with _serve(world, tmp_path) as (url, _httpd):
        base = f"/cockpit/dispatch/{job.id}"
        status, c = _call(url, base + "/challenge", {"signer_fp": key.fp})
        assert status == 200 and c["payload"]["job_id"] == job.id
        assert c["rp_id"] == RP and c["line"].startswith("{") and job.id in c["line"]      # the exact text signed
        answer = {"payload": c["payload"], **key.assertion(c["challenge"], PAGE)}
        status, done = _call(url, base + "/answer", answer)
        assert (status, done["state"]) == (200, "signed"), done
        status, again = _call(url, base + "/answer", answer)
        assert (status, again["error"]) == (409, "conflict")
        status, cc = _call(url, base + "/cancel-challenge", {"signer_fp": key.fp, "reason": "no"})
        assert status == 200 and cc["payload"]["reason"] == "no" and "cancel" in cc["line"]
        status, done = _call(url, base + "/cancel", {"payload": cc["payload"], **key.assertion(cc["challenge"], PAGE)})
        assert (status, done["state"]) == (200, "cancelled"), done
        # a final job is not open to the sheet
        assert _call(url, base + "/sheet.json") == (404, {"error": "not_open", "message": "this dispatch job is not open"})
        assert _call(url, "/cockpit/dispatch.json")[1]["requests"] == []
    assert _state(world, job.id) == "cancelled"


def test_the_token_opens_the_route_and_never_signs(world, tmp_path) -> None:
    """Ported: test_phone_dispatch.test_the_token_opens_the_route_and_never_signs, with the cockpit's own gate
    (the sign routes': a token on every bind, JSON only, same-origin)."""
    job = file_job(world)
    key = world["phone"]
    with _serve(world, tmp_path) as (url, _httpd):
        base = f"/cockpit/dispatch/{job.id}"
        for token in (None, "wrong-token"):
            status, body = _call(url, base + "/answer", {"payload": {}}, token=token)
            assert (status, body.get("error")) == (403, "credential_required"), (token, status, body)
        assert _call(url, base + "/challenge", {"signer_fp": key.fp}, sfs="cross-site")[0] == 403
        assert _call(url, base + "/challenge", {"signer_fp": key.fp}, ctype="text/plain")[0] == 415
        assert _call(url, "/cockpit/dispatch/not-a-job/challenge", {"signer_fp": key.fp})[0] == 404
        assert _call(url, base + "/sign", {"signer_fp": key.fp})[0] == 404          # an action the phone does not serve
        assert _call(url, base + "/sheet.json", token=None)[0] == 403
        assert _call(url, base + "/sheet.json", sfs="cross-site")[0] == 403
        assert _call(url, "/cockpit/dispatch.json", token=None)[0] == 403
        # the run token with a payload the server issued, and no assertion the passkey made: nothing is signed
        c = _call(url, base + "/challenge", {"signer_fp": key.fp})[1]
        forged = {"payload": c["payload"], "authenticator_data": "AAAA", "client_data_json": "AAAA", "signature": "AAAA"}
        assert _call(url, base + "/answer", forged)[0] == 422
        # an assertion over the challenge, made on another page origin, is refused too
        status, _b = _call(url, base + "/answer", {"payload": c["payload"], **key.assertion(c["challenge"], PORTED)})
        assert status == 422
    assert _state(world, job.id) == "filed"


def test_every_dispatch_route_is_503_unless_one_cockpit_is_named(world, tmp_path) -> None:
    """Ported: test_phone_dispatch.test_every_dispatch_route_is_503_unless_one_cockpit_is_named."""
    # the dispatch directory is the Signer's queue, so the job is filed in the queue whose hooks name no cockpit
    lone = tmp_path / "lone-queue"
    lone.mkdir(mode=0o700)
    lone_world = {**world, "qdir": lone}
    job = file_job(lone_world)
    signer = _signer(world, qdir=lone, keys=passkey_key_source(lone, RP))
    with _serve(world, tmp_path, signer=signer) as (url, _httpd):
        status, body = _call(url, f"/cockpit/dispatch/{job.id}/challenge", {"signer_fp": world["phone"].fp})
        assert (status, body["error"]) == (503, "unavailable") and "names no cockpit" in body["message"], body
        (row,) = _call(url, "/cockpit/dispatch.json")[1]["requests"]
        assert row["signers"] == [] and "names no cockpit" in row["note"], row
    assert _state(lone_world, job.id) == "filed"


def test_the_dispatch_dir_is_the_signers_queue(world, tmp_path) -> None:
    """Ported: test_phone_dispatch.test_the_dispatch_dir_defaults_to_the_queue_dir: the keys a card offers and the keys
    its land verifies come from one directory's consent settings."""
    with _serve(world, tmp_path) as (_url, httpd):
        assert httpd.dispatch_sheet.phone.dispatch_dir == world["qdir"] == httpd.dispatch_sheet.queue_dir


def test_a_dispatch_signature_is_never_another_namespaces(world, tmp_path) -> None:
    """Ported (the dispatch half of) test_phone_dispatch.test_a_dispatch_signature_is_never_a_consent_answer...: a sign
    assertion presented as a cancel and a cancel assertion presented as a signature are the verifier's refusals."""
    job = file_job(world)
    key = world["phone"]
    with _serve(world, tmp_path) as (url, _httpd):
        base = f"/cockpit/dispatch/{job.id}"
        sc = _call(url, base + "/challenge", {"signer_fp": key.fp})[1]
        cc = _call(url, base + "/cancel-challenge", {"signer_fp": key.fp, "reason": ""})[1]
        status, out = _call(url, base + "/cancel", {"payload": cc["payload"], **key.assertion(sc["challenge"], PAGE)})
        assert status == 422, out
        status, out = _call(url, base + "/answer", {"payload": sc["payload"], **key.assertion(cc["challenge"], PAGE)})
        assert status == 422, out
        assert _state(world, job.id) == "filed"
        status, out = _call(url, base + "/answer", {"payload": sc["payload"], **key.assertion(sc["challenge"], PAGE)})
        assert (status, out["state"]) == (200, "signed"), out


def test_a_dispatch_dir_that_is_a_file_lists_nothing_and_says_why(world, tmp_path) -> None:
    """Ported: test_phone_dispatch.test_a_dispatch_dir_that_is_a_file_leaves_the_consent_listing_intact (the dispatch
    half: the listing answers 200 with the store's fault as its note, never an empty list alone)."""
    flat = tmp_path / "flat"
    flat.write_text("a file, not a directory")
    keys = lambda: {k.fingerprint: k for k in passkey_keys(world["source"](), RP)}  # noqa: E731
    with _serve(world, tmp_path, signer=_signer(world, qdir=flat, keys=keys)) as (url, _httpd):
        status, body = _call(url, "/cockpit/dispatch.json")
    assert status == 200 and body["requests"] == [] and body["overflow"] == 0
    assert "cannot be read" in body["note"], body


def test_a_faulting_keys_source_lists_the_jobs_unsignable(world, tmp_path) -> None:
    """Ported: test_phone_dispatch.test_a_faulting_keys_source_keeps_the_consent_cards_and_lists_the_jobs_unsignable:
    the job is listed with no signer and the fault as its note, so no Sign can start."""
    job = file_job(world)

    def broken() -> Any:
        raise sqlite3.OperationalError("keys db")

    with _serve(world, tmp_path, signer=_signer(world, keys=broken)) as (url, _httpd):
        status, body = _call(url, "/cockpit/dispatch.json")
        (row,) = body["requests"]
        assert status == 200 and row["id"] == job.id and row["signers"] == [] and row["options"] == []
        assert "keys db" in row["note"], row
        status, got = _call(url, f"/cockpit/dispatch/{job.id}/sheet.json")
        assert status == 200 and got["signers"] == [] and "keys db" in got["note"], got
    assert _state(world, job.id) == "filed"


def test_the_authorise_routes_are_served_behind_the_same_gates(world, tmp_path) -> None:
    """Ported: test_phone_dispatch.test_the_authorise_routes_are_served_behind_the_same_gates: a filed job has no card
    to authorise, and the sheet offers no such option."""
    job = file_job(world)
    key = world["phone"]
    with _serve(world, tmp_path) as (url, _httpd):
        base = f"/cockpit/dispatch/{job.id}"
        assert _call(url, base + "/authorise-challenge", {"signer_fp": key.fp}, token=None)[0] == 403
        assert _call(url, base + "/authorise", {"payload": {}}, sfs="cross-site")[0] == 403
        status, body = _call(url, base + "/authorise-challenge", {"signer_fp": key.fp})
        assert (status, body["error"]) == (409, "conflict") and "from the laptop" in body["message"], body
        status, body = _call(url, base + "/authorise", {"payload": {}})
        assert (status, body["error"]) == (400, "bad_request")
        (row,) = _call(url, "/cockpit/dispatch.json")[1]["requests"]
        assert "authorise" not in [o["key"] for o in row["options"]]
    assert _state(world, job.id) == "filed"


def test_without_a_signer_no_dispatch_route_exists(world, tmp_path) -> None:
    job = file_job(world)
    with _serving(tmp_path / "inst") as (url, _httpd):
        assert _call(url, "/cockpit/dispatch.json")[0] == 404
        assert _call(url, f"/cockpit/dispatch/{job.id}/sheet.json")[0] == 404
        assert _call(url, f"/sign/dispatch/{job.id}")[0] == 404
        for action in ("challenge", "answer", "cancel-challenge", "cancel", "authorise-challenge", "authorise"):
            assert _call(url, f"/cockpit/dispatch/{job.id}/{action}", {})[0] == 404


def test_a_cockpit_pinned_to_a_ported_page_refuses_every_step_before_a_ceremony(world, tmp_path) -> None:
    """The verification takes an answer made on https://<rp> with no port, so a cockpit pinned to :8443 lists no signer
    and refuses each step before any challenge, naming where it can be signed."""
    job = file_job(world)
    key = world["phone"]
    with _serve(world, tmp_path, signer=_signer(world, origin=PORTED)) as (url, _httpd):
        base = f"/cockpit/dispatch/{job.id}"
        status, body = _call(url, base + "/challenge", {"signer_fp": key.fp})
        assert (status, body["error"]) == (422, "sign_refused") and PAGE in body["message"], body
        assert "nothing was signed" in body["message"]
        (row,) = _call(url, "/cockpit/dispatch.json")[1]["requests"]
        assert row["signers"] == [] and row["options"] == [] and PAGE in row["note"], row
    assert _state(world, job.id) == "filed"


def test_a_challenge_reply_carries_the_line_for_cancel_as_for_sign(world, tmp_path) -> None:
    job = file_job(world)
    with _serve(world, tmp_path) as (url, _httpd):
        c = _call(url, f"/cockpit/dispatch/{job.id}/cancel-challenge", {"signer_fp": world["phone"].fp, "reason": "x"})[1]
        assert c["challenge"] and _b64u(c["line"].encode())       # a line is text
        # a reason is one line of plain text, as the phone port refuses it
        status, body = _call(url, f"/cockpit/dispatch/{job.id}/cancel-challenge",
                             {"signer_fp": world["phone"].fp, "reason": "a\nb"})
        assert status == 422 and body["error"] == "sign_refused", body
