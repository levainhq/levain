"""The phone's drop sheet (levain.coordinator.phone): a phone drop, its outbox, and an envelope signed with a passkey.

The world is test_dispatch_phone's: a real authority log holding a laptop key and a SOFTWARE passkey that answers a
WebAuthn challenge as a phone's authenticator does, a consent dir whose hook names that cockpit, and the dispatch store
(the coordinator's file) in that same dir. Every envelope signature here is one the server issued a challenge for and
the store's one verifier (``levain.coordinator.envelope.refusal``, ssh-keygen underneath) accepts or refuses."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.cli import main
from levain.contracts.canon import canonical
from levain.coordinator import envelope as env
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.phone import CoordPhone, CoordRefused
from levain.coordinator.store import Part
from levain.dispatch.jobs import DB_NAME
from tests.test_dispatch_phone import ORIGIN, RP, phone_for, repo, world  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

SENT = "2026-01-01T00:00:00.000000Z"      # a sent_at in the store's past: the store keeps it as the phone's claim


DEV = "d-0123456789ab"                     # the paired device a request carries (the route resolves it from its token)


def coord_for(world, *, page_origin: str | None = ORIGIN, **kw) -> CoordPhone:
    d = phone_for(world, page_origin=page_origin)          # the same keys callable the dispatch card uses
    return CoordPhone(world["qdir"], RP, page_origin, kw.pop("keys", d._keys), **kw)


def sid(cp: CoordPhone):
    """The store identity the listing reports (None while there is no store), which the page binds an outbox item to."""
    return cp.listing()["coord_store_id"]


def drop(cp: CoordPhone, words: str, outbox_id: str, **extra):
    extra.setdefault("store_id", sid(cp))
    return cp.step("drop", {"words": words, "outbox_id": outbox_id, "sent_at": SENT, **extra}, device=DEV)


def review(world, cp: CoordPhone, words: str, outbox_id: str, spec: dict, **extra):
    extra.setdefault("store_id", sid(cp))     # the identity the page read, as the drop carries it
    return cp.step("envelope-review", {"words": words, "outbox_id": outbox_id, "spec": spec,
                                       "signer_fp": world["phone"].fp, **extra}, device=DEV)


def sign(world, cp: CoordPhone, r: dict, **assert_kw):
    """Sign tapped: the server mints the payload now, the passkey answers its challenge."""
    c = cp.step("envelope-challenge", {"review_id": r["review_id"]}, device=DEV)
    a = world["phone"].assertion(c["challenge"], **assert_kw)
    return c, {"body_text": r["body_text"], "payload": c["payload"], **a}


def signed_drop(world, cp: CoordPhone, words: str, outbox_id: str, spec: dict):
    """The page's calls: the review, Sign (the challenge and the passkey's assertion), then the drop carrying it."""
    r = review(world, cp, words, outbox_id, spec)
    c, envelope = sign(world, cp, r)
    return {**r, **c}, envelope, drop(cp, words, outbox_id, envelope=envelope)


# --- 1. a phone drop reaches the store `levain drop` writes, and shows its receipt ------------------------------------

def test_a_phone_drop_is_stored_by_the_cli_store_with_its_delivered_receipt(world) -> None:
    cp = coord_for(world)
    got = drop(cp, "  fix the hub suite gate\nthen tell me  ", "ph-" + "a" * 32)
    assert got["created"] is True
    d = got["drop"]
    store = EnvelopeStore(world["qdir"])                          # the CLI's own opener, on the same directory
    stored = store.drop(d["drop_id"])
    assert stored.surface == "phone" and stored.words == "  fix the hub suite gate\nthen tell me  "   # verbatim
    assert stored.outbox_id == "ph-" + "a" * 32 and stored.sent_at == SENT and stored.source.how == "none"
    assert [r["stage"] for r in d["receipts"]] == ["delivered"]
    # the CLI reads it back as any drop
    assert main(["drops", "show", d["drop_id"], "--queue-dir", str(world["qdir"])]) == 0


def test_the_listing_shows_phone_drops_and_the_split_receipt_naming_its_threads(world) -> None:
    cp = coord_for(world)
    assert cp.listing()["coord_drops"] == [] and not (world["qdir"] / DB_NAME).exists()   # a read makes no store
    d = drop(cp, "widget ping stuck, and hub suite gate errors", "ph-" + "b" * 32)["drop"]
    EnvelopeStore(world["qdir"]).split(d["drop_id"], [Part(kind="work", title="widget ping"),
                                                     Part(kind="work", title="hub suite gate")])
    listed = cp.listing()
    assert listed["coord_note"] is None and listed["coord_signers"] == [world["phone"].fp]
    (row,) = listed["coord_drops"]
    assert [r["stage"] for r in row["receipts"]] == ["delivered", "split"]
    assert [t["title"] for t in row["receipts"][1]["threads"]] == ["widget ping", "hub suite gate"]
    assert {t["state"] for t in row["threads"]} == {"open"}


# --- 2. the outbox: a retry after a lost reply never files twice (MANDATORY row) ---------------------------------------

def test_outbox_retry_after_a_lost_reply_files_once_and_says_delivered(world) -> None:
    cp = coord_for(world)
    oid = "ph-" + "c" * 32
    first = drop(cp, "capture and close the main head", oid)
    # the reply was lost on the way back: the phone still holds the drop and sends it again, same id, same words
    again = drop(cp, "capture and close the main head", oid)
    assert first["created"] is True and again["created"] is False
    assert again["drop"]["drop_id"] == first["drop"]["drop_id"]
    store = EnvelopeStore(world["qdir"])
    assert [d.drop_id for d in store.drops()] == [first["drop"]["drop_id"]]
    assert [r.body["stage"] for r in store.drop_receipts(first["drop"]["drop_id"])] == ["delivered"]


def test_outbox_id_reused_for_other_words_is_a_conflict_and_writes_nothing(world) -> None:
    cp = coord_for(world)
    oid = "ph-" + "d" * 32
    drop(cp, "one thing", oid)
    with pytest.raises(CoordRefused) as exc:
        drop(cp, "another thing", oid)
    assert exc.value.status == 409
    assert len(EnvelopeStore(world["qdir"]).drops()) == 1


def test_outbox_retry_of_a_signed_drop_returns_the_stored_drop_and_its_envelope_once(world) -> None:
    cp = coord_for(world)
    repo_path = str(world["repo"][0])
    spec = {"where": [{"repo": repo_path, "paths": ["."]}], "lands": [{"repo": repo_path, "onto": "main"}],
            "stop": "2h", "budget": {"tool_calls": 40}}
    _c, envelope, first = signed_drop(world, cp, "build it and land it", "ph-" + "e" * 32, spec)
    again = drop(cp, "build it and land it", "ph-" + "e" * 32, envelope=envelope)
    assert first["envelope"]["taken"] is True and again["envelope"]["taken"] is True
    assert again["created"] is False and again["drop"]["drop_id"] == first["drop"]["drop_id"]
    store = EnvelopeStore(world["qdir"])
    assert len(store.drops()) == 1
    assert store.envelope(first["envelope"]["envelope_id"]).drop_id == first["drop"]["drop_id"]


def test_a_challenge_for_an_outbox_id_already_delivered_signs_nothing(world) -> None:
    cp = coord_for(world)
    drop(cp, "plain words", "ph-" + "f" * 32)
    with pytest.raises(CoordRefused) as exc:
        review(world, cp, "plain words", "ph-" + "f" * 32, {"where": []})
    assert exc.value.status == 409


# --- 3. the digest cross-check table: CLI-signed == phone-signed == stored == what the reader prints (MANDATORY) --------

def _spec_rows(repo_path: str) -> list[tuple[str, dict]]:
    return [
        ("where only", {"where": [{"repo": repo_path, "paths": ["."]}], "stop": "1h"}),
        ("a land onto main", {"where": [{"repo": repo_path, "paths": ["."]}],
                              "lands": [{"repo": repo_path, "onto": "main"}], "stop": "2h"}),
        ("paths and a budget", {"where": [{"repo": repo_path, "paths": ["src/", "README"]}],
                                "budget": {"tool_calls": 50, "tokens": 200000, "hours": 3}, "stop": "4h"}),
    ]


_IDS = ("envelope_id", "drop_id", "stop_at", "issued_at")


def _show_digest(qdir: Path, capsys, envelope_id: str) -> str:
    capsys.readouterr()
    assert main(["envelope", "show", envelope_id, "--json", "--queue-dir", str(qdir)]) == 0
    return json.loads(capsys.readouterr().out)["digest"]


@pytest.mark.parametrize("row", range(3))
def test_digest_cross_check_table(world, tmp_path: Path, capsys, row: int) -> None:
    repo_path = str(world["repo"][0])
    name, spec = _spec_rows(repo_path)[row]
    words = f"synthetic drop for the cross-check row {name!r}: ünïcode kept"
    cp = coord_for(world)
    # the phone leg: the digest the page shows, the digest the passkey's payload names, what the store records
    c, _env, got = signed_drop(world, cp, words, f"ph-row{row}-" + "0" * 24, spec)
    assert got["envelope"]["taken"] is True, got
    phone_body = env.parse_body(c["body_text"])
    store = EnvelopeStore(world["qdir"])
    stored_p = store.envelope(got["envelope"]["envelope_id"])
    # the CLI leg: the same spec and words through `levain drop --envelope`, signed by the laptop key
    spec_file = tmp_path / f"spec{row}.json"
    spec_file.write_text(json.dumps({**spec, "lands": [{**land, "from": "named"} for land in spec.get("lands", [])]}))
    capsys.readouterr()
    rc = main(["drop", words, "--no-source", "--envelope", str(spec_file), "--signing-key", str(tmp_path / "laptop"),
               "--data-root", str(world["root"]), "--yes", "--json", "--queue-dir", str(world["qdir"])])
    out = capsys.readouterr()
    assert rc == 0, out.err
    cli_drop = json.loads(out.out[out.out.index("{"):])
    stored_c = store.envelope(cli_drop["envelope"]["envelope_id"])
    table = {
        "phone page digest": c["digest"],
        "phone payload digest": c["payload"]["digest"],
        "phone body re-derived": env.envelope_digest(phone_body),
        "phone body bytes": "sha256:" + hashlib.sha256(c["body_text"].encode("utf-8")).hexdigest(),
        "phone stored": stored_p.digest,
        "phone, levain envelope show": _show_digest(world["qdir"], capsys, stored_p.envelope_id),
        # the CLI's envelope for the same spec and words differs only in its ids and times: given the phone's, its
        # canonical body IS the phone's, byte for byte, so the CLI would have signed the same digest
        "CLI body with the phone's ids": env.envelope_digest({**stored_c.body,
                                                              **{k: phone_body[k] for k in _IDS}}),
    }
    assert set(table.values()) == {c["digest"]}, table
    # and each leg's own record holds: the CLI-signed digest == its stored digest == what show prints
    assert stored_c.payload["digest"] == stored_c.digest == env.envelope_digest(stored_c.body) \
        == _show_digest(world["qdir"], capsys, stored_c.envelope_id)
    assert stored_p.signer_class == "display-phone" and stored_c.signer_class == "laptop-presence"
    assert canonical(stored_p.body) == c["body_text"]


# --- 4. the inferred mark is inside what the passkey signs -------------------------------------------------------------

def test_a_phone_land_is_signed_as_inferred_and_says_so_in_the_view(world) -> None:
    cp = coord_for(world)
    repo_path = str(world["repo"][0])
    c = review(world, cp, "and land it", "ph-" + "1" * 32, {"where": [{"repo": repo_path, "paths": ["."]}],
                                                             "lands": [{"repo": repo_path, "onto": "main"}]})
    (land,) = env.parse_body(c["body_text"])["lands"]
    assert (land["from"], land["onto_from"]) == ("named", "inferred")
    assert any("onto main" in line and "inferred, not where you dropped from" in line for line in c["view"]), c["view"]
    assert any(line.startswith("NOT IN FORCE YET") for line in c["view"])


@pytest.mark.parametrize("land_extra", [{"onto_from": "drop"}, {"from": "source"}, {"onto_from": "picker"}])
def test_the_phone_cannot_claim_a_land_is_where_it_dropped_from(world, land_extra) -> None:
    cp = coord_for(world)
    repo_path = str(world["repo"][0])
    with pytest.raises(CoordRefused) as exc:
        review(world, cp, "land it", "ph-" + "2" * 32, {"where": [{"repo": repo_path, "paths": ["."]}],
                                                        "lands": [{"repo": repo_path, "onto": "main", **land_extra}]})
    assert exc.value.status == 422


def test_a_widened_body_under_the_phone_signature_is_refused_and_never_stored_plain(world) -> None:
    # fix1 ruling 3: a drop that CARRIES a signed envelope is never stored plain (r1 stored it plain and said 200)
    cp = coord_for(world)
    repo_path = str(world["repo"][0])
    r = review(world, cp, "narrow work", "ph-" + "3" * 32, {"where": [{"repo": repo_path, "paths": ["docs/"]}]})
    _c, envelope = sign(world, cp, r)
    body = env.parse_body(r["body_text"])
    wide = canonical({**body, "where": [{"repo": repo_path, "paths": ["."]}]})
    with pytest.raises(CoordRefused) as exc:
        drop(cp, "narrow work", "ph-" + "3" * 32, envelope={**envelope, "body_text": wide})
    assert exc.value.status == 422 and "digest" in exc.value.message
    assert EnvelopeStore(world["qdir"]).drops() == []
    # "Send as a plain drop": the same outbox id, no envelope, the words unchanged, is then delivered
    got = drop(cp, "narrow work", "ph-" + "3" * 32, envelope=None)
    assert got["created"] is True and got["envelope"] is None
    assert EnvelopeStore(world["qdir"]).drop(got["drop"]["drop_id"]).envelope_id is None


def test_an_assertion_from_another_origin_signs_nothing_and_writes_nothing(world) -> None:
    cp = coord_for(world)
    repo_path = str(world["repo"][0])
    r = review(world, cp, "w", "ph-" + "4" * 32, {"where": [{"repo": repo_path, "paths": ["."]}]})
    _c, envelope = sign(world, cp, r, origin="http://localhost:9999")
    with pytest.raises(CoordRefused) as exc:
        drop(cp, "w", "ph-" + "4" * 32, envelope=envelope)
    assert exc.value.status == 422
    assert EnvelopeStore(world["qdir"]).drops() == []


# --- 5. the routes, through the real server ----------------------------------------------------------------------------

def _post(base, path, body, token):
    import urllib.request

    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Levain-Phone-Token": token,
                                          "Origin": base})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _listing(base, token):
    import urllib.request

    with urllib.request.urlopen(urllib.request.Request(base + "/consent.json",
                                                       headers={"X-Levain-Phone-Token": token}), timeout=10) as r:
        return json.loads(r.read())






# --- 6. fix1 (coordphone L3 r1), each row fail-first --------------------------------------------------------------------



def test_r1_a_step_with_no_device_is_refused(world) -> None:
    cp = coord_for(world)
    with pytest.raises(CoordRefused) as exc:
        cp.step("drop", {"words": "w", "outbox_id": "ph-" + "6" * 32, "sent_at": SENT, "store_id": None}, device=None)
    assert exc.value.status == 403
    assert not (world["qdir"] / DB_NAME).exists()


def test_r2_an_outbox_item_for_another_store_is_refused_and_writes_nothing(world) -> None:
    cp = coord_for(world)
    first = drop(cp, "made here", "ph-" + "7" * 32)
    here = first["store_id"]
    assert here == sid(cp) == EnvelopeStore(world["qdir"]).store_id()
    for other in ("st-" + "f" * 32, None):            # another store's id, and an item made before any store existed
        with pytest.raises(CoordRefused) as exc:
            drop(cp, "made for another Levain", "ph-" + "8" * 32, store_id=other)
        assert (exc.value.status, exc.value.error) == (409, "other_store"), exc.value.message
    assert len(EnvelopeStore(world["qdir"]).drops()) == 1


def test_r2_an_item_bound_to_a_store_meets_no_store_and_is_refused(world) -> None:
    # the dispatch directory was replaced (no store now): an item bound to the old one never creates a new one
    cp = coord_for(world)
    with pytest.raises(CoordRefused) as exc:
        drop(cp, "old store", "ph-" + "9" * 32, store_id="st-" + "a" * 32)
    assert exc.value.status == 409 and not (world["qdir"] / DB_NAME).exists()


class _Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def _land_spec(repo_path: str) -> dict:
    return {"where": [{"repo": repo_path, "paths": ["."]}], "stop": "2h"}


def test_r3_signed_at_is_minted_when_sign_is_tapped_not_at_review(world) -> None:
    from datetime import datetime, timedelta, timezone

    from levain.consent.queue import parse_stamp

    clock = _Clock(datetime.now(timezone.utc))
    cp = coord_for(world, clock=clock)
    r = review(world, cp, "reviewed slowly", "ph-" + "a1" * 16, _land_spec(str(world["repo"][0])))
    clock.t += timedelta(minutes=5)                   # a long read of the sheet, past the store's 2-minute skew
    c, envelope = sign(world, cp, r)
    assert parse_stamp(c["payload"]["signed_at"]) == clock.t
    got = drop(cp, "reviewed slowly", "ph-" + "a1" * 16, envelope=envelope)
    assert got["envelope"]["taken"] is True, got


def test_r3_a_stale_signature_is_refused_envelope_stale_then_signed_again_and_stored(world) -> None:
    from datetime import datetime, timedelta, timezone

    clock = _Clock(datetime.now(timezone.utc))
    cp = coord_for(world, clock=clock)
    spec = _land_spec(str(world["repo"][0]))
    oid = "ph-" + "a2" * 16
    r = review(world, cp, "signed offline", oid, spec)
    _c, envelope = sign(world, cp, r)
    # the store's clock is the real one: a signature older than its skew is what a long offline wait looks like
    EnvelopeStore(world["qdir"])        # the store exists with its real clock
    late = coord_for(world, clock=lambda: datetime.now(timezone.utc) + timedelta(minutes=5))
    with pytest.raises(CoordRefused) as exc:
        late.step("drop", {"words": "signed offline", "outbox_id": oid, "sent_at": SENT, "store_id": sid(cp),
                           "envelope": envelope}, device=DEV)
    assert (exc.value.status, exc.value.error) == (422, "envelope-stale"), exc.value.message
    assert EnvelopeStore(world["qdir"]).drops() == []
    # "Sign again": a fresh review and Sign over the same words and outbox id, then it is stored with its envelope
    _c2, env2, got = signed_drop(world, coord_for(world), "signed offline", oid, spec)
    assert got["created"] is True and got["envelope"]["taken"] is True


def test_r3_a_verifier_that_is_unavailable_is_503_and_writes_nothing(world, monkeypatch) -> None:
    from levain.authority import VerifierUnavailable

    cp = coord_for(world)
    r = review(world, cp, "verify me", "ph-" + "a3" * 16, _land_spec(str(world["repo"][0])))
    _c, envelope = sign(world, cp, r)

    def down(*a, **k):
        raise VerifierUnavailable("ssh-keygen did not answer")

    monkeypatch.setattr(env, "refusal", down)
    with pytest.raises(CoordRefused) as exc:
        drop(cp, "verify me", "ph-" + "a3" * 16, envelope=envelope)
    assert exc.value.status == 503
    assert EnvelopeStore(world["qdir"]).drops() == []


def test_r4_a_resend_returns_the_stored_receipt_without_revalidating_the_old_carrier(world) -> None:
    cp = coord_for(world)
    oid = "ph-" + "a4" * 16
    _c, envelope, first = signed_drop(world, cp, "delivered, reply lost", oid, _land_spec(str(world["repo"][0])))
    assert first["envelope"]["taken"] is True
    # the passkey is revoked (no active key) after the store accepted the drop and before the resend arrives
    gone = coord_for(world, keys=lambda: {})
    again = drop(gone, "delivered, reply lost", oid, envelope=envelope)
    assert again["created"] is False and again["drop"]["drop_id"] == first["drop"]["drop_id"]
    assert again["envelope"]["taken"] is True
    # the same id with other words is still the conflict
    with pytest.raises(CoordRefused) as exc:
        drop(gone, "other words", oid, envelope=envelope)
    assert exc.value.status == 409


def test_r6_sign_refused_from_the_keys_is_a_structured_503(world) -> None:
    from levain.consent.sign import SignRefused

    def broken():
        raise SignRefused("two cockpits are named by consent hooks", status=503)

    good = coord_for(world)
    r = review(world, good, "keys", "ph-" + "a6" * 16, _land_spec(str(world["repo"][0])))
    _c, envelope = sign(world, good, r)
    cp = coord_for(world, keys=broken)
    for action, req in (("envelope-review", {"words": "keys", "outbox_id": "ph-" + "b6" * 16, "signer_fp": "x", "store_id": sid(cp),
                                             "spec": _land_spec(str(world["repo"][0]))}),
                        ("drop", {"words": "keys", "outbox_id": "ph-" + "a6" * 16, "sent_at": SENT,
                                  "store_id": sid(cp), "envelope": envelope})):
        with pytest.raises(CoordRefused) as exc:
            cp.step(action, req, device=DEV)
        assert exc.value.status == 503, (action, exc.value.message)


def test_r7_a_signed_envelope_replayed_under_another_outbox_id_is_409_never_filed_plain(world) -> None:
    cp = coord_for(world)
    _c, envelope, first = signed_drop(world, cp, "once only", "ph-" + "a7" * 16, _land_spec(str(world["repo"][0])))
    assert first["envelope"]["taken"] is True
    with pytest.raises(CoordRefused) as exc:
        drop(cp, "once only", "ph-" + "b7" * 16, envelope=envelope)
    assert exc.value.status == 409
    assert len(EnvelopeStore(world["qdir"]).drops()) == 1


# --- 7. an accepted phone drop wakes the coordinator through the CLI's own call (HEAD Q1) -------------------------------

class _Launcher:
    """Stands in for launch_session(store, launch_id): records the call, answers like an Outcome."""

    def __init__(self, *, started=True, raises=None, gate=None):
        import threading

        self.calls, self.started, self.raises, self.gate = [], started, raises, gate
        self.done = threading.Event()

    def __call__(self, store, launch_id):
        from levain.coordinator.launch import Outcome

        self.calls.append(launch_id)
        try:
            if self.gate is not None:
                assert self.gate.wait(10)
            if self.raises is not None:
                raise self.raises
            return Outcome(launch_id, self.started, attach="claude attach h-1" if self.started else None,
                           refusal=None if self.started else "the consent hook is not live")
        finally:
            self.done.set()


def _coord_set(world, tmp_path) -> None:
    d = tmp_path / "coord-install"
    d.mkdir()
    EnvelopeStore(world["qdir"]).coord_set_agent("claude", model="haiku", cwd=str(d.resolve()))


def _wakes(world, drop_id):
    return [r.body for r in EnvelopeStore(world["qdir"]).drop_receipts(drop_id) if r.body.get("stage") == "woken"]


def test_r8_an_accepted_drop_wakes_once_and_the_reply_does_not_wait_on_it(world, tmp_path) -> None:
    import threading

    _coord_set(world, tmp_path)
    gate = threading.Event()
    launcher = _Launcher(gate=gate)
    cp = coord_for(world, launcher=launcher)
    oid = "ph-" + "a8" * 16
    got = drop(cp, "wake up", oid)                     # returns while the launcher is still blocked on the gate
    assert got["created"] is True and got["woken"] is not None
    assert not launcher.done.is_set()
    gate.set()
    cp.wait_wakes(10)
    assert launcher.calls == [got["woken"]]
    (w,) = _wakes(world, got["drop"]["drop_id"])
    assert w["launch_id"] == got["woken"] and w["started"] is True
    # a resend returns the stored receipt and wakes nothing again
    again = drop(cp, "wake up", oid)
    cp.wait_wakes(10)
    assert again["created"] is False and again["woken"] is None and launcher.calls == [got["woken"]]
    # a refusal (the same id, other words) wakes nothing
    with pytest.raises(CoordRefused):
        drop(cp, "other words", oid)
    cp.wait_wakes(10)
    assert launcher.calls == [got["woken"]] and len(_wakes(world, got["drop"]["drop_id"])) == 1


def test_r8_a_failed_wake_is_recorded_on_the_drops_receipt_never_a_500(world, tmp_path) -> None:
    _coord_set(world, tmp_path)
    for launcher, oid in ((_Launcher(raises=OSError("spawn failed")), "ph-" + "a9" * 16),):
        cp = coord_for(world, launcher=launcher)
        got = drop(cp, "wake, but it fails", oid)
        cp.wait_wakes(10)
        assert got["created"] is True and launcher.calls == [got["woken"]]
        (w,) = _wakes(world, got["drop"]["drop_id"])
        assert w["started"] is False and "spawn failed" in w["said"]
        assert any(r["stage"] == "woken" and r["started"] is False
                   for r in cp.listing()["coord_drops"][0]["receipts"])


def test_r8_the_default_launcher_is_the_cli_s_own_launch_session(world) -> None:
    from levain.coordinator.launch import launch_session

    assert coord_for(world)._launcher is launch_session




def test_r4_the_store_strict_carry_answers_a_resend_before_checking_its_carrier(world) -> None:
    # EnvelopeStore.accept_drop(keep_plain=False) itself, not only the phone route: a resend is looked up first
    cp = coord_for(world)
    oid = "ph-" + "d4" * 16
    _c, envelope, first = signed_drop(world, cp, "strict resend", oid, _land_spec(str(world["repo"][0])))
    store = EnvelopeStore(world["qdir"])
    again, created = store.accept_drop("strict resend", surface="phone", outbox_id=oid, sent_at=SENT,
                                       envelope=(envelope["body_text"], envelope["payload"], "not a signature"),
                                       authority=world["source"](), keep_plain=False)
    assert created is False and again.drop_id == first["drop"]["drop_id"]


# --- 8. L3 r2: the first use of a store and a lost reply (coordphone fix2) ---------------------------------------------

def test_f2_a_fresh_install_signs_its_first_drop_bound_to_the_store_its_review_made(world) -> None:
    # head ruling 1: the review creates the store, so its reply names the store and says it made it; the page binds
    # the signed item (and every item it made while there was no store) to that id, and the drop is stored
    cp = coord_for(world)
    assert sid(cp) is None and not (world["qdir"] / DB_NAME).exists()
    oid = "ph-" + "f1" * 16
    r = review(world, cp, "the first drop ever", oid, _land_spec(str(world["repo"][0])))
    here = EnvelopeStore(world["qdir"]).store_id()
    assert (r["store_id"], r["store_created"]) == (here, True)
    c, envelope = sign(world, cp, r)
    assert (c["store_id"], c["store_created"]) == (here, False)
    got = drop(cp, "the first drop ever", oid, envelope=envelope, store_id=r["store_id"])
    assert got["created"] is True and got["envelope"]["taken"] is True and got["store_id"] == here
    # a review on a store that already exists names it and made nothing
    r2 = review(world, cp, "a second drop", "ph-" + "f2" * 16, _land_spec(str(world["repo"][0])))
    assert (r2["store_id"], r2["store_created"]) == (here, False)
    # the store's rule is unchanged: a null-bound drop on a store this request did not make is another Levain's
    with pytest.raises(CoordRefused) as exc:
        drop(cp, "a second drop", "ph-" + "f2" * 16, store_id=None)
    assert (exc.value.status, exc.value.error) == (409, "other_store")
    assert len(EnvelopeStore(world["qdir"]).drops()) == 1


def test_f2_a_lost_reply_of_the_drop_that_made_the_store_is_answered_by_the_resend_lookup(world) -> None:
    # head ruling 2: the resend lookup runs BEFORE the store-id check, so the null-bound retry gets its receipt
    cp = coord_for(world)
    oid = "ph-" + "f3" * 16
    first = drop(cp, "this one made the store", oid, store_id=None)
    assert first["created"] is True and first["store_id"] == sid(cp)
    again = drop(cp, "this one made the store", oid, store_id=None)        # the first reply was lost
    assert again["created"] is False and again["drop"]["drop_id"] == first["drop"]["drop_id"]
    assert again["store_id"] == first["store_id"]
    with pytest.raises(CoordRefused) as exc:                               # the same id, other words
        drop(cp, "other words", oid, store_id=None)
    assert exc.value.status == 409 and exc.value.error == "conflict"
    # a null-bound item that matches nothing on this store stays another Levain's (head ruling Q1, unchanged)
    with pytest.raises(CoordRefused) as exc:
        drop(cp, "never sent here", "ph-" + "f4" * 16, store_id=None)
    assert (exc.value.status, exc.value.error) == (409, "other_store")
    assert [d.drop_id for d in EnvelopeStore(world["qdir"]).drops()] == [first["drop"]["drop_id"]]


def test_f2_a_wake_thread_that_cannot_start_is_a_woken_receipt_and_the_reply_stands(world, tmp_path,
                                                                                   monkeypatch) -> None:
    # head ruling 3: Thread.start() raising after the drop committed is the wake's receipt, never a 500
    import threading

    class _NoStart(threading.Thread):
        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    _coord_set(world, tmp_path)
    launcher = _Launcher()
    cp = coord_for(world, launcher=launcher)
    monkeypatch.setattr(threading, "Thread", _NoStart)
    got = drop(cp, "wake, but no thread starts", "ph-" + "f5" * 16)
    monkeypatch.undo()
    assert got["created"] is True and got["woken"] is not None and launcher.calls == []
    (w,) = _wakes(world, got["drop"]["drop_id"])
    assert w["launch_id"] == got["woken"] and w["started"] is False and "can't start new thread" in w["said"], w
    assert cp._wakes == []                                  # the unstarted thread is not held for wait_wakes


def test_f2_two_concurrent_first_sends_of_one_outbox_id_store_one_drop_and_wake_once(world, tmp_path,
                                                                                    monkeypatch) -> None:
    # complement's note, confirmed on disk: CoordStore.accept_drop looks the outbox id up INSIDE its write
    # transaction, so two first sends that both missed the read-only resend lookup still file one drop
    import threading

    from levain.coordinator.store import CoordStore

    _coord_set(world, tmp_path)
    launcher = _Launcher()
    cp = coord_for(world, launcher=launcher)
    here = sid(cp)
    oid = "ph-" + "f6" * 16
    both = threading.Barrier(2, timeout=10)
    real = CoordStore.resent_drop

    def both_miss(self, *a, **k):
        got = real(self, *a, **k)
        both.wait()                       # neither send writes until both lookups have missed
        return got

    monkeypatch.setattr(CoordStore, "resent_drop", both_miss)
    out, errs = [], []

    def send() -> None:
        try:
            out.append(drop(cp, "raced from two tabs", oid, store_id=here))
        except Exception as exc:  # noqa: BLE001 - the assertion below names it
            errs.append(exc)

    ts = [threading.Thread(target=send) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    cp.wait_wakes(10)
    assert errs == [] and sorted(o["created"] for o in out) == [False, True], (errs, out)
    assert len({o["drop"]["drop_id"] for o in out}) == 1
    (won,) = [o for o in out if o["created"]]
    assert [d.outbox_id for d in EnvelopeStore(world["qdir"]).drops()] == [oid]
    assert launcher.calls == [won["woken"]] and len(_wakes(world, won["drop"]["drop_id"])) == 1


def test_f3_a_sent_at_ahead_of_the_store_clock_is_refused_with_the_gap_named(world) -> None:
    # L3 r3 LOW 5: the 422 names how far ahead the sending clock runs, so the person can tell a clock gap from a bad drop
    cp = coord_for(world)
    ahead = (datetime.now(timezone.utc) + timedelta(minutes=47)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    with pytest.raises(CoordRefused) as exc:
        drop(cp, "from a fast clock", "ph-" + "f3" * 16, sent_at=ahead)
    assert exc.value.status == 422 and "ahead of the store's clock" in str(exc.value)
    assert re.search(r"4[5-8] minutes ahead", str(exc.value)), str(exc.value)


# --- 9. L3 r3 codex HIGH: the review is bound to the page's store (coordphone fix4) ------------------------------------

def test_f4_a_store_replaced_between_listing_and_review_is_409_and_writes_nothing(world, tmp_path) -> None:
    # the page read store A; the name now answers for replacement store B (same passkeys). The review carries A, and
    # B refuses it other_store with nothing kept and nothing written, so no envelope is ever signed for B
    a = coord_for(world)
    drop(a, "made against A", "ph-" + "a4" * 16)
    store_a = sid(a)
    other = tmp_path / "replacement"
    other.mkdir(mode=0o700)
    b = CoordPhone(other, RP, ORIGIN, phone_for(world, page_origin=ORIGIN)._keys)
    store_b = EnvelopeStore(other).store_id()
    assert store_b != store_a
    oid = "ph-" + "b4" * 16
    with pytest.raises(CoordRefused) as exc:
        review(world, b, "signed for A", oid, _land_spec(str(world["repo"][0])), store_id=store_a)
    assert (exc.value.status, exc.value.error) == (409, "other_store"), exc.value.message
    assert b._reviews == {} and EnvelopeStore(other).drops() == []
    # the drop of that item meets the same rule (nothing passes the review rule that the drop rule would refuse)
    with pytest.raises(CoordRefused) as exc:
        drop(b, "signed for A", oid, store_id=store_a)
    assert (exc.value.status, exc.value.error) == (409, "other_store")
    assert EnvelopeStore(other).drops() == []


def test_f4_a_null_bound_review_takes_the_store_only_when_it_made_it(world) -> None:
    # null may become the returned identity only when that review created the store; on a store it did not make, a
    # null-bound review is another Levain's, as the drop is
    cp = coord_for(world)
    first = review(world, cp, "the first review makes the store", "ph-" + "c4" * 16,
                   _land_spec(str(world["repo"][0])), store_id=None)
    here = EnvelopeStore(world["qdir"]).store_id()
    assert (first["store_id"], first["store_created"]) == (here, True)
    with pytest.raises(CoordRefused) as exc:
        review(world, cp, "a page that never saw the store made", "ph-" + "d4" * 16,
               _land_spec(str(world["repo"][0])), store_id=None)
    assert (exc.value.status, exc.value.error) == (409, "other_store")
    bound = review(world, cp, "bound to it", "ph-" + "e4" * 16, _land_spec(str(world["repo"][0])), store_id=here)
    assert (bound["store_id"], bound["store_created"]) == (here, False)
    with pytest.raises(CoordRefused) as exc:
        review(world, cp, "bad id", "ph-" + "f5" * 16, _land_spec(str(world["repo"][0])), store_id=7)
    assert exc.value.status == 400


# --- 10. L3 r4 (both seats): the challenge is bound to the store its review was (coordphone fix5) ----------------------

def _move_store_away(qdir: Path, aside: Path) -> None:
    """The store file (and its sqlite sidecars) leaves the directory, so the name answers for no store, or for a new one."""
    aside.mkdir(mode=0o700)
    for p in qdir.iterdir():
        if p.name.startswith(DB_NAME):
            p.rename(aside / p.name)


def test_f5_a_store_replaced_between_review_and_sign_is_409_and_signs_nothing(world, tmp_path) -> None:
    # the review was bound to store A; before Sign is tapped the directory answers for replacement store B. The
    # challenge is refused other_store: no payload, no passkey challenge, and nothing is written to B
    cp = coord_for(world)
    drop(cp, "made against A", "ph-" + "a5" * 16)
    store_a = sid(cp)
    oid = "ph-" + "b5" * 16
    r = review(world, cp, "reviewed for A", oid, _land_spec(str(world["repo"][0])))
    assert r["store_id"] == store_a
    _move_store_away(world["qdir"], tmp_path / "aside")
    store_b = EnvelopeStore(world["qdir"]).store_id()
    assert store_b != store_a
    with pytest.raises(CoordRefused) as exc:
        cp.step("envelope-challenge", {"review_id": r["review_id"]}, device=DEV)
    assert (exc.value.status, exc.value.error) == (409, "other_store"), exc.value.message
    assert EnvelopeStore(world["qdir"]).drops() == [] and sid(cp) == store_b


def test_f5_a_store_gone_between_review_and_sign_makes_no_new_store(world, tmp_path) -> None:
    # the challenge never creates a store: with A gone it is refused other_store and the directory still holds none
    cp = coord_for(world)
    drop(cp, "made against A", "ph-" + "c5" * 16)
    r = review(world, cp, "reviewed for A", "ph-" + "d5" * 16, _land_spec(str(world["repo"][0])))
    _move_store_away(world["qdir"], tmp_path / "aside")
    with pytest.raises(CoordRefused) as exc:
        cp.step("envelope-challenge", {"review_id": r["review_id"]}, device=DEV)
    assert (exc.value.status, exc.value.error) == (409, "other_store"), exc.value.message
    assert not any(p.name.startswith(DB_NAME) for p in world["qdir"].iterdir()) and sid(cp) is None


def test_f5_the_store_a_fresh_install_s_review_made_is_the_one_its_challenge_binds(world, tmp_path) -> None:
    # the review on a fresh install made the store, so its kept identity is that store's (never the request's None):
    # the challenge passes on it, and a store made in its place after the review is refused
    cp = coord_for(world)
    r = review(world, cp, "the first drop ever", "ph-" + "e5" * 16, _land_spec(str(world["repo"][0])), store_id=None)
    assert r["store_created"] is True
    c, _ = sign(world, cp, r)
    assert c["store_id"] == r["store_id"]
    _move_store_away(world["qdir"], tmp_path / "aside")
    EnvelopeStore(world["qdir"])
    with pytest.raises(CoordRefused) as exc:
        cp.step("envelope-challenge", {"review_id": r["review_id"]}, device=DEV)
    assert (exc.value.status, exc.value.error) == (409, "other_store")
