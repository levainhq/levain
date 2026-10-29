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
from tests.test_dispatch_phone import SoftPasskey, _b64u, file_job, lwp, recommended, repo  # noqa: F401  (fixtures)
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


def _v(url: str, job_id: str) -> str:
    """The version the sheet's read shows for ``job_id`` now: the ``request_sha256`` its challenge names."""
    status, got = _call(url, f"/cockpit/dispatch/{job_id}/sheet.json")
    assert status == 200, got
    return got["request"]["sha256"]


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
        status, c = _call(url, base + "/challenge", {"signer_fp": key.fp, "request_sha256": _v(url, job.id)})
        assert status == 200 and c["payload"]["job_id"] == job.id
        assert c["rp_id"] == RP and c["line"].startswith("{") and job.id in c["line"]      # the exact text signed
        answer = {"payload": c["payload"], **key.assertion(c["challenge"], PAGE)}
        status, done = _call(url, base + "/answer", answer)
        assert (status, done["state"]) == (200, "signed"), done
        status, again = _call(url, base + "/answer", answer)
        assert (status, again["error"]) == (409, "conflict")
        status, cc = _call(url, base + "/cancel-challenge", {"signer_fp": key.fp, "reason": "no",
                                                             "request_sha256": _v(url, job.id)})
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
        c = _call(url, base + "/challenge", {"signer_fp": key.fp, "request_sha256": _v(url, job.id)})[1]
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
        status, body = _call(url, f"/cockpit/dispatch/{job.id}/challenge", {"signer_fp": world["phone"].fp,
                                                                             "request_sha256": _v(url, job.id)})
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
        sc = _call(url, base + "/challenge", {"signer_fp": key.fp, "request_sha256": _v(url, job.id)})[1]
        cc = _call(url, base + "/cancel-challenge", {"signer_fp": key.fp, "reason": "", "request_sha256": _v(url, job.id)})[1]
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
        status, body = _call(url, base + "/authorise-challenge", {"signer_fp": key.fp, "request_sha256": _v(url, job.id)})
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


# --- fix round 1 (L3 os2d r1): never sign or authorise from a truncated summary; the authorise bound to the card shown ---

def _file_wide(world: Any, paths: "list[str]") -> Any:
    """A filed job whose contract writes every path in ``paths`` (a template whose write set holds them)."""
    from datetime import datetime, timedelta, timezone

    from levain.cockpit.consent import utc_stamp
    from tests.test_dispatch_contract import POLICY, registry, submission, template

    path, base = world["repo"]
    sub = submission(path, base, write=paths, expires_at=utc_stamp(datetime.now(timezone.utc) + timedelta(hours=2)))
    job, _cap = JobStore(world["qdir"]).file(sub, resolve_template=registry(template(write_paths=frozenset(paths))),
                                             policy=POLICY, filer_session="sess-1", filer_agent="claude_code")
    return job


def _card(changes: "list[dict[str, Any]]", *, run: int = 1, onto: str = "main", tier: str = "T2", overflow: int = 0,
          signers: "list[str]") -> "dict[str, Any]":
    """An authorise card in DispatchPhone._authorise_card's exact shape."""
    return {"onto": onto, "run": run, "tier": tier, "changes": changes, "change_overflow": overflow,
            "files": len(changes) + overflow, "changed_lines": sum(c["added"] + c["deleted"] for c in changes),
            "signers": signers, "note": "", "problem": None, "staged": "f" * 40, "base": "e" * 40}


def _changes(n: int) -> "list[dict[str, Any]]":
    return [{"path": f"src/c{i:02d}.py", "status": "M" if i % 2 else "A", "added": i + 1, "deleted": i,
             "binary": False, "gitlink": False} for i in range(n)]


def _recommended(httpd: Any, job: Any, card: "dict[str, Any]", monkeypatch: Any) -> None:
    """The listing's job read as recommended with ``card`` as its authorise card (the phone's record, the card swapped)."""
    phone = httpd.dispatch_sheet.phone
    real = phone.record

    def record(j: Any, now: str, signers: "list[str]", note: str) -> "dict[str, Any]":
        rec = real(j, now, signers, note)
        if j.id == job.id:
            rec.update({"state": "recommended", "can_sign": False, "authorise": card})
        return rec

    monkeypatch.setattr(phone, "record", record)


def test_a_thirteen_path_contract_is_shown_whole_never_cut_with_an_ellipsis(world, tmp_path) -> None:
    """codex HIGH 1: the digest binds every write path; a sheet that showed 12 and an ellipsis let a passkey sign a
    contract whose scope it did not show. Every write path, the read set and each digest-bound field the phone shows."""
    paths = [f"src/n{i:02d}.py" for i in range(13)]
    job = _file_wide(world, paths)
    with _serve(world, tmp_path) as (url, _httpd):
        (row,) = _call(url, "/cockpit/dispatch.json")[1]["requests"]
        got = _call(url, f"/cockpit/dispatch/{job.id}/sheet.json")[1]["request"]
    assert got == row
    msg = row["message"]
    assert "…" not in msg and "..." not in msg, msg
    for p in paths:
        assert p in msg, (p, msg)
    c = job.contract
    t = c.template
    for e in c.read:
        assert e.path in msg and e.blob in msg, msg
    for shown in (c.goal.split("\n")[0], c.base, c.base_tree, c.policy_hash, c.expires_at, job.digest, t.content_hash,
                  t.verifier_hash, t.verifier_kind, t.route, t.provider, t.executor, *t.models, *c.grant,
                  *sorted(t.write_paths), str(c.budget["tokens"])):
        assert shown in msg, (shown, msg)
    assert [o["key"] for o in row["options"]] == ["sign", "cancel"], row


def test_a_sixteen_change_run_is_shown_change_by_change_with_its_counts(world, tmp_path, monkeypatch) -> None:
    """codex HIGH 2: a recommended run of 16 changes showed 15 and Authorise landed all 16. Every change, its status
    and its line counts, is on the sheet the Authorise is offered under."""
    job = file_job(world)
    key = world["phone"]
    changes = _changes(16)
    with _serve(world, tmp_path) as (url, httpd):
        _recommended(httpd, job, _card(changes, signers=[key.fp]), monkeypatch)
        (row,) = _call(url, "/cockpit/dispatch.json")[1]["requests"]
    msg = row["message"]
    assert "authorise" in [o["key"] for o in row["options"]], row
    assert "…" not in msg, msg
    for c in changes:
        assert f"{c['status']}  +{c['added']} -{c['deleted']}  {c['path']}" in msg, (c, msg)
    assert row["authorise"] == {"run": 1, "onto": "main", "tier": "T2"}, row


def test_a_change_set_past_the_phones_cap_offers_nothing_and_says_review_it_on_the_laptop(world, tmp_path,
                                                                                         monkeypatch) -> None:
    """codex HIGH 2: ``change_overflow`` set means the card does not hold every change: no act is offered from a set
    the sheet cannot show whole, and the row says so in the server's words."""
    job = file_job(world)
    key = world["phone"]
    with _serve(world, tmp_path) as (url, httpd):
        _recommended(httpd, job, _card(_changes(3), overflow=2, signers=[key.fp]), monkeypatch)
        (row,) = _call(url, "/cockpit/dispatch.json")[1]["requests"]
        got = _call(url, f"/cockpit/dispatch/{job.id}/sheet.json")[1]
    assert row["options"] == [] and row["signers"] == [] and row["review"] is True, row
    assert "on the laptop" in row["note"] and "levain dispatch show " + job.id in row["note"], row
    assert got["request"]["options"] == [] and got["signers"] == [] and got["note"] == row["note"], got


def test_the_version_binds_the_cards_run_branch_and_tier(world, tmp_path, monkeypatch) -> None:
    """complement MED 3: a card whose recommendation moved (another run, another branch, another tier) reads as a
    changed version, so a pick made on the old card is never offered on the new one."""
    job = file_job(world)
    key = world["phone"]
    seen = []
    with _serve(world, tmp_path) as (url, httpd):
        for run, onto, tier in ((1, "main", "T2"), (0, "main", "T2"), (1, "work", "T2"), (1, "main", "T1")):
            _recommended(httpd, job, _card(_changes(2), run=run, onto=onto, tier=tier, signers=[key.fp]), monkeypatch)
            seen.append(_call(url, f"/cockpit/dispatch/{job.id}/sheet.json")[1]["request"]["sha256"])
    assert len(set(seen)) == 4, seen


def test_a_localhost_cockpit_with_no_pinned_page_is_elsewhere(world) -> None:
    """deepseek MED 6: ``localhost`` short-circuited the pin; a sheet with no page_origin can verify nothing."""
    from levain.web_server import DispatchSheet

    signer = _signer(world)
    sheet = DispatchSheet(world["qdir"], "localhost", None, signer)
    assert sheet.elsewhere() is not None and "not pinned" in sheet.elsewhere()
    assert DispatchSheet(world["qdir"], "localhost", "http://localhost:7428", signer).elsewhere() is None
    assert DispatchSheet(world["qdir"], RP, PAGE, signer).elsewhere() is None
    job = file_job(world)
    from levain.dispatch.phone import DispatchRefused

    with pytest.raises(DispatchRefused) as exc:
        sheet.step(job.id, "challenge", {"signer_fp": world["phone"].fp})
    assert exc.value.status == 422 and "nothing was signed" in exc.value.message
    assert _state(world, job.id) == "filed"


def test_a_record_that_cannot_be_read_is_a_row_with_nothing_offered(world, tmp_path, monkeypatch) -> None:
    """complement LOW 5: row() guards DispatchPhone.record as pending() does: a job whose record cannot be read is a
    row that offers nothing and says why, never a 500."""
    job = file_job(world)
    with _serve(world, tmp_path) as (url, httpd):
        def broken(*_a: Any, **_k: Any) -> Any:
            raise KeyError("template")

        monkeypatch.setattr(httpd.dispatch_sheet.phone, "record", broken)
        status, got = _call(url, f"/cockpit/dispatch/{job.id}/sheet.json")
    assert status == 200 and got["request"]["options"] == [] and got["signers"] == [], got
    assert "could not be read" in got["note"] and "KeyError" in got["note"], got


def test_a_challenge_reply_carries_the_line_for_cancel_as_for_sign(world, tmp_path) -> None:
    job = file_job(world)
    with _serve(world, tmp_path) as (url, _httpd):
        c = _call(url, f"/cockpit/dispatch/{job.id}/cancel-challenge", {"signer_fp": world["phone"].fp, "reason": "x",
                                                                         "request_sha256": _v(url, job.id)})[1]
        assert c["challenge"] and _b64u(c["line"].encode())       # a line is text
        # a reason is one line of plain text, as the phone port refuses it
        status, body = _call(url, f"/cockpit/dispatch/{job.id}/cancel-challenge",
                             {"signer_fp": world["phone"].fp, "reason": "a\nb", "request_sha256": _v(url, job.id)})
        assert status == 422 and body["error"] == "sign_refused", body


# --- fix 1029-2 (complement, L3 of 62b45ef6..680fee95): the dispatch challenge names the version the sheet showed ------

def _amended(httpd: Any, job: Any, monkeypatch: Any, digest: str = "d" * 64) -> None:
    """The job read as amended (its contract's digest another) from here on: the phone's record, the digest swapped. The
    store has no amend verb; the version the sheet binds is over the record, so this is the change it must catch."""
    phone = httpd.dispatch_sheet.phone
    real = phone.record

    def record(j: Any, now: str, signers: "list[str]", note: str) -> "dict[str, Any]":
        rec = real(j, now, signers, note)
        if j.id == job.id:
            rec["digest"] = digest
        return rec

    monkeypatch.setattr(phone, "record", record)


def test_a_job_amended_between_the_sheet_read_and_the_tap_is_refused_at_challenge_time(world, tmp_path, monkeypatch) -> None:
    """Reproduced red before the fix: the sheet posted only {signer_fp}, so a job amended after the sheet's read was
    challenged in its NEW state and the passkey signed a change set the operator never read. Now the challenge names the
    row's sha256 the sheet showed and a job that changed since is the plain sign route's 409, for every act."""
    job = file_job(world)
    key = world["phone"]
    with _serve(world, tmp_path) as (url, httpd):
        base = f"/cockpit/dispatch/{job.id}"
        shown = _v(url, job.id)
        _amended(httpd, job, monkeypatch)
        assert _v(url, job.id) != shown
        for action, body in (("challenge", {"signer_fp": key.fp}), ("cancel-challenge", {"signer_fp": key.fp, "reason": ""}),
                             ("authorise-challenge", {"signer_fp": key.fp})):
            status, out = _call(url, f"{base}/{action}", {**body, "request_sha256": shown})
            assert (status, out["error"]) == (409, "conflict"), (action, out)
            assert "changed since it was shown" in out["message"] and "reload" in out["message"], (action, out)
            assert "challenge" not in out and "payload" not in out, (action, out)
        assert "NOT cancelled" in _call(url, f"{base}/cancel-challenge",
                                        {"signer_fp": key.fp, "reason": "", "request_sha256": shown})[1]["message"]
        # read again, the sheet's new version is challenged
        status, c = _call(url, base + "/challenge", {"signer_fp": key.fp, "request_sha256": _v(url, job.id)})
        assert status == 200 and c["payload"]["job_id"] == job.id, c
    assert _state(world, job.id) == "filed"


def test_a_job_that_moved_on_in_the_store_is_refused_on_the_old_version(world, tmp_path) -> None:
    """The real store: the job is signed after the sheet read it, so a cancel on the filed version is refused (its
    version moved with its state and lease); on the version read now it is challenged."""
    job = file_job(world)
    key = world["phone"]
    with _serve(world, tmp_path) as (url, _httpd):
        base = f"/cockpit/dispatch/{job.id}"
        filed = _v(url, job.id)
        c = _call(url, base + "/challenge", {"signer_fp": key.fp, "request_sha256": filed})[1]
        assert _call(url, base + "/answer", {"payload": c["payload"], **key.assertion(c["challenge"], PAGE)})[0] == 200
        status, out = _call(url, base + "/cancel-challenge", {"signer_fp": key.fp, "reason": "", "request_sha256": filed})
        assert (status, out["error"]) == (409, "conflict") and "changed since it was shown" in out["message"], out
        status, out = _call(url, base + "/cancel-challenge", {"signer_fp": key.fp, "reason": "",
                                                              "request_sha256": _v(url, job.id)})
        assert status == 200 and out["payload"]["reason"] == "", out
    assert _state(world, job.id) == "signed"


def test_a_challenge_that_names_no_version_is_refused(world, tmp_path) -> None:
    """As a plain sign request's: a challenge with no listed version (or an empty one, or not a string) is a 409."""
    job = file_job(world)
    key = world["phone"]
    with _serve(world, tmp_path) as (url, _httpd):
        base = f"/cockpit/dispatch/{job.id}"
        for extra in ({}, {"request_sha256": ""}, {"request_sha256": 7}):
            status, out = _call(url, base + "/challenge", {"signer_fp": key.fp, **extra})
            assert (status, out["error"]) == (409, "conflict") and "names no listed version" in out["message"], out
        # the key is the cockpit's, never the phone port's body: a stray key beside it is still the phone's 400
        status, out = _call(url, base + "/challenge", {"signer_fp": key.fp, "request_sha256": _v(url, job.id), "x": 1})
        assert (status, out["error"]) == (400, "bad_request"), out
    assert _state(world, job.id) == "filed"


def test_a_job_amended_while_its_challenge_is_built_is_refused(world, tmp_path, monkeypatch) -> None:
    """The version is compared again after the payload is built: an amendment that lands between the first compare and
    the build is refused, and the challenge built is never handed out."""
    job = file_job(world)
    key = world["phone"]
    with _serve(world, tmp_path) as (url, httpd):
        sheet = httpd.dispatch_sheet
        real = sheet.phone.step

        def step(job_id: str, action: str, req: Any) -> Any:
            out = real(job_id, action, req)
            _amended(httpd, job, monkeypatch)          # the job changes while this challenge was built
            return out

        monkeypatch.setattr(sheet.phone, "step", step)
        status, out = _call(url, f"/cockpit/dispatch/{job.id}/challenge",
                            {"signer_fp": key.fp, "request_sha256": _v(url, job.id)})
        assert (status, out["error"]) == (409, "conflict") and "changed since it was shown" in out["message"], out
        assert "challenge" not in out, out
    assert _state(world, job.id) == "filed"


# --- fix 1029-2 L3 round 1 (deepseek, complement on 2be30145) ---------------------------------------------------------

def _no_phone_step(httpd: Any, monkeypatch: Any) -> "list[Any]":
    """Every call the phone port's step receives from here on (none is the expected count)."""
    calls: "list[Any]" = []
    real = httpd.dispatch_sheet.phone.step
    monkeypatch.setattr(httpd.dispatch_sheet.phone, "step", lambda *a: calls.append(a) or real(*a))
    return calls


def test_a_challenge_body_that_is_not_an_object_is_the_no_version_409_and_asks_the_phone_nothing(world, tmp_path,
                                                                                              monkeypatch) -> None:
    """deepseek: the version gate ran only for a dict body, so a list or a string went straight to the phone port.
    Reproduced red: a list body was the phone's 400, the gate never ran."""
    job = file_job(world)
    with _serve(world, tmp_path) as (url, httpd):
        calls = _no_phone_step(httpd, monkeypatch)
        for action in ("challenge", "cancel-challenge", "authorise-challenge"):
            for body in ([world["phone"].fp], "x", 7):
                status, out = _call(url, f"/cockpit/dispatch/{job.id}/{action}", body)
                assert (status, out["error"]) == (409, "conflict"), (action, body, out)
                assert "names no listed version" in out["message"], (action, body, out)
        for req in (None, [1], "x"):
            from levain.dispatch.phone import DispatchRefused

            with pytest.raises(DispatchRefused) as exc:
                httpd.dispatch_sheet.step(job.id, "challenge", req)
            assert exc.value.status == 409 and "names no listed version" in exc.value.message
    assert calls == [] and _state(world, job.id) == "filed"


def test_a_store_fault_at_the_version_compare_names_its_type_never_its_text(world, tmp_path, monkeypatch) -> None:
    """deepseek: the 503 carried str(exc), which names this machine's paths and tables. Reproduced red: the path was in
    the message."""
    job = file_job(world)
    with _serve(world, tmp_path) as (url, httpd):
        def broken(_job_id: str) -> Any:
            raise sqlite3.OperationalError("/Users/someone/.levain/consent/dispatch.db: no such table: jobs")

        monkeypatch.setattr(httpd.dispatch_sheet, "row", broken)
        calls = _no_phone_step(httpd, monkeypatch)
        status, out = _call(url, f"/cockpit/dispatch/{job.id}/challenge",
                            {"signer_fp": world["phone"].fp, "request_sha256": "a" * 64})
    assert (status, out["error"]) == (503, "unavailable"), out
    assert "OperationalError" in out["message"] and "nothing was signed" in out["message"], out
    assert "/Users" not in out["message"] and "dispatch.db" not in out["message"] and "jobs" not in out["message"], out
    assert calls == []


def test_an_unreadable_row_is_never_challenged_even_on_its_own_version(world, tmp_path, monkeypatch) -> None:
    """complement LOW 4: an unreadable row carries a version of its own; a challenge naming it passed the compare and the
    phone port built a sign challenge for a job the sheet could not show. Reproduced red: 200 and a challenge."""
    job = file_job(world)
    with _serve(world, tmp_path) as (url, httpd):
        def broken(*_a: Any, **_k: Any) -> Any:
            raise KeyError("template")

        monkeypatch.setattr(httpd.dispatch_sheet.phone, "record", broken)
        shown = _v(url, job.id)
        for action, body in (("challenge", {}), ("cancel-challenge", {"reason": ""})):
            status, out = _call(url, f"/cockpit/dispatch/{job.id}/{action}",
                                {"signer_fp": world["phone"].fp, "request_sha256": shown, **body})
            assert (status, out["error"]) == (409, "conflict"), (action, out)
            assert "could not be read" in out["message"] and "challenge" not in out, (action, out)
    assert _state(world, job.id) == "filed"


def test_a_run_pins_its_staged_change_set(lwp) -> None:  # noqa: F811
    """complement LOW 3, verified: the version binds the card's run, not its change list, because the change list is the
    diff of the run record's base and staged commit and a run record is written once. The store refuses both ways a
    run's change set could move under the same run number."""
    job = recommended(lwp, {"src/a.py": b"a = 2\n"}, onto="main")
    assert lwp.dp.pending()[0][0]["authorise"]["run"] == 0
    conn = sqlite3.connect(lwp.jstore.directory / DB_NAME, isolation_level=None)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="written once"):
            conn.execute("UPDATE run_records SET record = record || ' ' WHERE job_id = ? AND run = 0", (job.id,))
        with pytest.raises(sqlite3.DatabaseError, match="never replaced"):
            conn.execute("INSERT INTO run_records SELECT job_id, run, record, digest, envelope_digest, at, ticket "
                         "FROM run_records WHERE job_id = ? AND run = 0", (job.id,))
    finally:
        conn.close()
