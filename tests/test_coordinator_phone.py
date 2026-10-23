"""The phone's drop sheet (levain.coordinator.phone): a phone drop, its outbox, and an envelope signed with a passkey.

The world is test_dispatch_phone's: a real authority log holding a laptop key and a SOFTWARE passkey that answers a
WebAuthn challenge as a phone's authenticator does, a consent dir whose hook names that cockpit, and the dispatch store
(the coordinator's file) in that same dir. Every envelope signature here is one the server issued a challenge for and
the store's one verifier (``levain.coordinator.envelope.refusal``, ssh-keygen underneath) accepts or refuses."""
from __future__ import annotations

import hashlib
import json
import shutil
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


def coord_for(world, *, page_origin: str | None = ORIGIN) -> CoordPhone:
    d = phone_for(world, page_origin=page_origin)          # the same keys callable the dispatch card uses
    return CoordPhone(world["qdir"], RP, page_origin, d._keys)


def drop(cp: CoordPhone, words: str, outbox_id: str, **extra):
    return cp.step("drop", {"words": words, "outbox_id": outbox_id, "sent_at": SENT, **extra})


def signed_drop(world, cp: CoordPhone, words: str, outbox_id: str, spec: dict):
    """The page's two calls: the challenge, the passkey's assertion over it, then the drop carrying the envelope."""
    c = cp.step("envelope-challenge", {"words": words, "outbox_id": outbox_id, "spec": spec,
                                       "signer_fp": world["phone"].fp})
    a = world["phone"].assertion(c["challenge"])
    envelope = {"body_text": c["body_text"], "payload": c["payload"], **a}
    return c, envelope, drop(cp, words, outbox_id, envelope=envelope)


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
        cp.step("envelope-challenge", {"words": "plain words", "outbox_id": "ph-" + "f" * 32,
                                       "spec": {"where": []}, "signer_fp": world["phone"].fp})
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
    c = cp.step("envelope-challenge", {"words": "and land it", "outbox_id": "ph-" + "1" * 32,
                                       "spec": {"where": [{"repo": repo_path, "paths": ["."]}],
                                                "lands": [{"repo": repo_path, "onto": "main"}]},
                                       "signer_fp": world["phone"].fp})
    (land,) = env.parse_body(c["body_text"])["lands"]
    assert (land["from"], land["onto_from"]) == ("named", "inferred")
    assert any("onto main" in line and "inferred, not where you dropped from" in line for line in c["view"]), c["view"]
    assert any(line.startswith("NOT IN FORCE YET") for line in c["view"])


@pytest.mark.parametrize("land_extra", [{"onto_from": "drop"}, {"from": "source"}, {"onto_from": "picker"}])
def test_the_phone_cannot_claim_a_land_is_where_it_dropped_from(world, land_extra) -> None:
    cp = coord_for(world)
    repo_path = str(world["repo"][0])
    with pytest.raises(CoordRefused) as exc:
        cp.step("envelope-challenge", {"words": "land it", "outbox_id": "ph-" + "2" * 32,
                                       "spec": {"where": [{"repo": repo_path, "paths": ["."]}],
                                                "lands": [{"repo": repo_path, "onto": "main", **land_extra}]},
                                       "signer_fp": world["phone"].fp})
    assert exc.value.status == 422


def test_a_widened_body_under_the_phone_signature_keeps_the_words_plain(world) -> None:
    cp = coord_for(world)
    repo_path = str(world["repo"][0])
    c = cp.step("envelope-challenge", {"words": "narrow work", "outbox_id": "ph-" + "3" * 32,
                                       "spec": {"where": [{"repo": repo_path, "paths": ["docs/"]}]},
                                       "signer_fp": world["phone"].fp})
    a = world["phone"].assertion(c["challenge"])
    body = env.parse_body(c["body_text"])
    wide = canonical({**body, "where": [{"repo": repo_path, "paths": ["."]}]})
    got = drop(cp, "narrow work", "ph-" + "3" * 32, envelope={"body_text": wide, "payload": c["payload"], **a})
    assert got["created"] is True and got["envelope"]["taken"] is False
    assert "digest" in got["envelope"]["refused"]
    stored = EnvelopeStore(world["qdir"]).drop(got["drop"]["drop_id"])
    assert stored.words == "narrow work" and stored.envelope_id is None


def test_an_assertion_from_another_origin_signs_nothing_and_writes_nothing(world) -> None:
    cp = coord_for(world)
    repo_path = str(world["repo"][0])
    c = cp.step("envelope-challenge", {"words": "w", "outbox_id": "ph-" + "4" * 32,
                                       "spec": {"where": [{"repo": repo_path, "paths": ["."]}]},
                                       "signer_fp": world["phone"].fp})
    a = world["phone"].assertion(c["challenge"], origin="http://localhost:9999")
    with pytest.raises(CoordRefused) as exc:
        drop(cp, "w", "ph-" + "4" * 32, envelope={"body_text": c["body_text"], "payload": c["payload"], **a})
    assert exc.value.status == 422
    assert EnvelopeStore(world["qdir"]).drops() == []


# --- 5. the routes, through the real server ----------------------------------------------------------------------------

def test_the_drop_route_needs_the_token_and_delivers_through_the_server(world) -> None:
    import urllib.request

    from tests.test_phone_dispatch import _serve
    from tests.test_phone_server import _TOK

    def post(base, path, body, token):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Levain-Phone-Token": token,
                                              "Origin": base})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    body = {"words": "from the couch", "outbox_id": "ph-" + "5" * 32, "sent_at": SENT}
    with _serve(world["qdir"], world["qdir"]) as (base, _):
        assert post(base, "/coord/drop", body, "wrong")[0] == 403
        status, got = post(base, "/coord/drop", body, _TOK)
        assert status == 200 and got["created"] is True and got["drop"]["surface"] == "phone"
        status, again = post(base, "/coord/drop", body, _TOK)
        assert status == 200 and again["created"] is False
        with urllib.request.urlopen(urllib.request.Request(base + "/consent.json",
                                                           headers={"X-Levain-Phone-Token": _TOK}), timeout=10) as r:
            listed = json.loads(r.read())
    assert [d["drop_id"] for d in listed["coord_drops"]] == [got["drop"]["drop_id"]]
