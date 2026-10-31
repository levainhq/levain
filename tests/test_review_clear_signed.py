"""c4-mut session 2: the passkey-signed clear of a "yours to clear" coordinator review item (head rulings R1-R5,
project_memory/1025-32/c4mut/RULINGS.md). An authority-relevant item is cleared only with the operator's passkey
signature over the item's id, state and last event, verified at the door, and the store records it as signed."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from levain.contracts.namespaces import LEVAIN_SIGN_REQUESTS, REVIEW_CLEAR_NAMESPACE
from levain.consent import sign as sg
from levain.consent.sign import Signer, SignRefused, file_levain_request, namespace_problem, read_request

from tests.test_cockpit_sign_origin import RP, _Passkey

ITEM = "rv-" + "ab" * 16
MSG = f"levain review clear {ITEM} open none"


def _write(qdir: Path, rid: str, body: dict[str, Any]) -> None:
    d = qdir / "sign"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps(body))


def _caller_request(qdir: Path, rid: str, message: str, **extra: Any) -> None:
    body = {"format": "levain-sign-request/1", "id": rid, "namespace": REVIEW_CLEAR_NAMESPACE, "message": message,
            "title": "t", "requester": "a caller",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), **extra}
    _write(qdir, rid, body)


# --- R2: the reserved namespace -----------------------------------------------------------------------------------

def test_a_caller_naming_the_reserved_namespace_is_still_refused() -> None:
    """The refusal exists against callers: the flag, and every name beginning levain, stay refused."""
    assert REVIEW_CLEAR_NAMESPACE.startswith("levain") and REVIEW_CLEAR_NAMESPACE in LEVAIN_SIGN_REQUESTS
    assert "begins 'levain'" in namespace_problem(REVIEW_CLEAR_NAMESPACE)
    with pytest.raises(ValueError, match="begins 'levain'"):
        Signer(Path("/nonexistent"), (REVIEW_CLEAR_NAMESPACE,), RP, lambda: {})


def test_levain_files_its_own_request_and_every_signer_serves_it_by_id(tmp_path: Path) -> None:
    pk = _Passkey()
    rid = file_levain_request(tmp_path, REVIEW_CLEAR_NAMESPACE, MSG, title="Clear a review item", ttl_s=600)
    req = read_request(tmp_path, rid)
    assert req.refused is None and req.namespace == REVIEW_CLEAR_NAMESPACE and req.requester == "levain"
    for namespaces in ((), ("flow-needs-phill",)):
        s = Signer(tmp_path, namespaces, RP, lambda: {pk.key.fingerprint: pk.key})
        row = s.row(rid, [pk.key.fingerprint])
        assert row is not None and row["message"] == MSG and not row.get("refused")
        assert s.state(rid) == {"id": rid, "state": "open"}
        # served by id, never listed: the surface that filed it shows it
        assert all(r["id"] != rid for r in s.page([pk.key.fingerprint])["rows"])
        ch = s.challenge(rid, {"signer_fp": pk.key.fingerprint, "request_sha256": req.sha256})
        assert ch["rp_id"] == RP


def test_levain_files_nothing_outside_its_namespaces_or_grammar(tmp_path: Path) -> None:
    for ns, msg in (("flow-needs-phill", MSG), ("levain-consent-answer", MSG), (REVIEW_CLEAR_NAMESPACE, "hello"),
                    (REVIEW_CLEAR_NAMESPACE, MSG + "\nand more")):
        with pytest.raises(ValueError):
            file_levain_request(tmp_path, ns, msg, title="t", ttl_s=600)
    assert not (tmp_path / "sign").exists() or not list((tmp_path / "sign").glob("*.json"))


@pytest.mark.parametrize("message, extra", [
    ("please approve the harmless thing", {}),
    (f"levain review clear {ITEM} open none\n", {}),
    (MSG, {"format": "levain-sign-choice/1", "options": [{"key": "a", "text": "x"}, {"key": "b", "text": "y"}]}),
])
def test_a_caller_written_request_in_the_reserved_namespace_is_refused_and_never_signed(tmp_path: Path, message: str,
                                                                                        extra: dict) -> None:
    pk = _Passkey()
    _caller_request(tmp_path, "np-forged-0001", message, **extra)
    req = read_request(tmp_path, "np-forged-0001")
    assert req.refused is not None and "levain's own" in req.refused
    s = Signer(tmp_path, ("flow-needs-phill",), RP, lambda: {pk.key.fingerprint: pk.key})
    with pytest.raises(SignRefused, match="refused"):
        s.challenge("np-forged-0001", {"signer_fp": pk.key.fingerprint, "request_sha256": req.sha256})
    # listed as refused (never skipped) to a Signer that lists at all, and it may be denied
    rows = [r for r in s.page([pk.key.fingerprint])["rows"] if r["id"] == "np-forged-0001"]
    assert len(rows) == 1 and rows[0]["refused"]


def test_a_well_formed_caller_request_in_the_reserved_namespace_is_served_as_levains_own(tmp_path: Path) -> None:
    """Known edge, written down: a same-uid writer can file exactly levain's message. It is then levain's own words
    (the operator reads "levain review clear <item> open <at>" and signs that); the cockpit's send also needs the
    compose binding, and the door recomputes the message from the store."""
    _caller_request(tmp_path, "np-sameuid-0001", MSG)
    assert read_request(tmp_path, "np-sameuid-0001").refused is None


def test_the_registry_names_the_reserved_namespace_and_its_grammar_matches_what_levain_writes() -> None:
    import re

    from levain.contracts.namespaces import REGISTRY

    assert REGISTRY[REVIEW_CLEAR_NAMESPACE][0] == "shipped"
    from levain.coordinator import envelope, review as rv

    assert rv.SIGN_TIER == envelope.SIGN_TIER       # the comment on SIGN_TIER names this constant
    assert re.fullmatch(LEVAIN_SIGN_REQUESTS[REVIEW_CLEAR_NAMESPACE], MSG)
    assert sg.request_id_ok("lv-" + "0" * 32)


# --- R3 + R4: the door, and what the store records -----------------------------------------------------------------

import dataclasses  # noqa: E402
import sqlite3  # noqa: E402

from levain.coordinator import review  # noqa: E402
from levain.coordinator.review import SignedClearRefused  # noqa: E402
from levain.coordinator.store import CoordError, CoordStore  # noqa: E402

ORIGIN = f"https://{RP}"


def _q(tmp_path: Path) -> tuple[Path, CoordStore]:
    d = tmp_path / "consent"
    d.mkdir(mode=0o700, exist_ok=True)
    return d, CoordStore(d)


def _mine(store: CoordStore, key: str = "m") -> review.Item:
    return review.file_item(store, "doubt", {"text": "the receipt does not match"}, dedupe_key=key,
                            authority_relevant=True)[0]


def _signer(qdir: Path, pk: _Passkey) -> Signer:
    return Signer(qdir, (), RP, lambda: {pk.key.fingerprint: pk.key})


def _sign(qdir: Path, pk: _Passkey, rid: str) -> None:
    s = _signer(qdir, pk)
    ch = s.challenge(rid, {"signer_fp": pk.key.fingerprint, "request_sha256": read_request(qdir, rid).sha256})
    s.answer(rid, pk.assertion(ch["challenge"], ORIGIN))


def _events(store: CoordStore, item_id: str) -> list[tuple]:
    with sqlite3.connect(store.path) as conn:
        return conn.execute("SELECT op, mode, by, sign_request_id FROM review_events WHERE item_id = ? ORDER BY seq",
                            (item_id,)).fetchall()


def _clear(store: CoordStore, qdir: Path, item: review.Item, rid: object, pk: _Passkey, **kw: Any) -> review.Item:
    keys = kw.pop("keys", None) or (lambda: {pk.key.fingerprint: pk.key})
    return review.clear_signed(store, item.item_id, queue_dir=qdir, request_id=rid, keys=keys, **kw)


def test_a_signed_clear_is_recorded_signed_with_its_request_and_the_cli_still_clears_manual(tmp_path: Path) -> None:
    qdir, store = _q(tmp_path)
    pk, item, other = _Passkey(), _mine(store), _mine(store, "n")
    rid = review.file_clear_request(store, item.item_id, queue_dir=qdir)
    assert read_request(qdir, rid).message == f"levain review clear {item.item_id} open none"
    _sign(qdir, pk, rid)
    done = _clear(store, qdir, item, rid, pk)
    assert done.state == "cleared" and done.last[:3] == ("cleared", "signed", "operator")
    assert _events(store, item.item_id) == [("cleared", "signed", "operator", rid)]
    # Obs 3: the laptop CLI clears unsigned, and its row says so
    assert review.clear_item(store, other.item_id, mode="manual").last[:3] == ("cleared", "manual", "operator")
    assert _events(store, other.item_id) == [("cleared", "manual", "operator", None)]
    # the same signature never clears twice: the item is cleared, so its message no longer matches
    with pytest.raises(SignedClearRefused) as exc:
        _clear(store, qdir, item, rid, pk)
    assert exc.value.status == 409


def test_no_caller_writes_the_signed_mode(tmp_path: Path) -> None:
    qdir, store = _q(tmp_path)
    item = _mine(store)
    with pytest.raises(CoordError, match="review mode is one of"):
        review.clear_item(store, item.item_id, mode=review.SIGNED)
    with sqlite3.connect(store.path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO review_events (item_id, seq, op, mode, by, at) VALUES (?, 1, 'cleared', 'signed', "
                     "'operator', 'x')", (item.item_id,))     # a signed event names its request, or it is refused
    with sqlite3.connect(store.path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO review_events (item_id, seq, op, mode, by, at, sign_request_id) VALUES (?, 1, "
                     "'cleared', 'manual', 'operator', 'x', 'lv-x')", (item.item_id,))
    assert review.get_item(store, item.item_id).state == "open"


def _refused(store: CoordStore, qdir: Path, item: review.Item, rid: object, pk: _Passkey, match: str,
             status: int = 403, **kw: Any) -> None:
    with pytest.raises(SignedClearRefused, match=match) as exc:
        _clear(store, qdir, item, rid, pk, **kw)
    assert exc.value.status == status
    assert review.get_item(store, item.item_id).state == "open" and _events(store, item.item_id) == []


def test_an_absent_open_or_denied_request_clears_nothing(tmp_path: Path) -> None:
    qdir, store = _q(tmp_path)
    pk, item = _Passkey(), _mine(store)
    _refused(store, qdir, item, None, pk, "no sign request was named")
    _refused(store, qdir, item, "lv-" + "0" * 32, pk, "there is no sign request")
    rid = review.file_clear_request(store, item.item_id, queue_dir=qdir)
    _refused(store, qdir, item, rid, pk, "is not signed")
    _signer(qdir, pk).deny(rid, {"request_sha256": read_request(qdir, rid).sha256})
    _refused(store, qdir, item, rid, pk, "is denied")


def test_an_expired_signed_request_clears_nothing(tmp_path: Path, monkeypatch) -> None:
    qdir, store = _q(tmp_path)
    pk, item = _Passkey(), _mine(store)
    rid = review.file_clear_request(store, item.item_id, queue_dir=qdir)
    _sign(qdir, pk, rid)

    class Later(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:      # type: ignore[override]
            return datetime.now(tz) + review.SIGN_REQUEST_TTL + timedelta(seconds=1)

    monkeypatch.setattr(review, "datetime", Later)
    _refused(store, qdir, item, rid, pk, "expired")


def test_a_forged_signature_or_record_clears_nothing(tmp_path: Path) -> None:
    import hashlib

    qdir, store = _q(tmp_path)
    pk, item = _Passkey(), _mine(store)
    d = qdir / "sign"
    # a same-uid writer forges the result files: a signature that does not verify, under the enrolled key's name
    rid = review.file_clear_request(store, item.item_id, queue_dir=qdir)
    msg = read_request(qdir, rid).message.encode()
    rec = {"signer_fp": pk.key.fingerprint, "public_key": pk.key.public_key, "namespace": REVIEW_CLEAR_NAMESPACE,
           "message_sha256": hashlib.sha256(msg).hexdigest(), "signed_at": "x"}
    (d / f"{rid}.sig").write_text("-----BEGIN SSH SIGNATURE-----\nAAAA\n-----END SSH SIGNATURE-----\n")
    (d / f"{rid}.signed.json").write_text(json.dumps(rec))
    _refused(store, qdir, item, rid, pk, "does not verify")
    # a record over other words
    (d / f"{rid}.signed.json").write_text(json.dumps({**rec, "message_sha256": "0" * 64}))
    _refused(store, qdir, item, rid, pk, "not over its message")
    # a real signature, by a passkey not enrolled here
    rid2 = review.file_clear_request(store, item.item_id, queue_dir=qdir)
    stranger = _Passkey()
    _sign(qdir, stranger, rid2)
    _refused(store, qdir, item, rid2, pk, "not an active passkey enrolled")
    # an enrolled passkey whose class does not cover the clear
    weak = dataclasses.replace(pk.key, key_class="presence")
    rid3 = review.file_clear_request(store, item.item_id, queue_dir=qdir)
    _sign(qdir, pk, rid3)
    _refused(store, qdir, item, rid3, pk, "does not cover T2", keys=lambda: {weak.fingerprint: weak})


def test_a_caller_namespace_request_with_the_same_words_clears_nothing(tmp_path: Path) -> None:
    """The door takes only levain's own namespace: a signature over the same words in a caller's namespace is none."""
    from tests.test_cockpit_sign_origin import NS

    qdir, store = _q(tmp_path)
    pk, item = _Passkey(), _mine(store)
    msg = review.clear_message(item)
    _write(qdir, "np-callerns-0001", {"format": "levain-sign-request/1", "id": "np-callerns-0001", "namespace": NS,
                                      "message": msg, "title": "t", "requester": "x",
                                      "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()})
    s = Signer(qdir, (NS,), RP, lambda: {pk.key.fingerprint: pk.key})
    ch = s.challenge("np-callerns-0001", {"signer_fp": pk.key.fingerprint,
                                          "request_sha256": read_request(qdir, "np-callerns-0001").sha256})
    s.answer("np-callerns-0001", pk.assertion(ch["challenge"], ORIGIN))
    _refused(store, qdir, item, "np-callerns-0001", pk, "not a review clear levain filed")


def test_a_signature_over_an_older_version_is_refused_after_a_reopen(tmp_path: Path) -> None:
    qdir, store = _q(tmp_path)
    pk, item = _Passkey(), _mine(store)
    rid = review.file_clear_request(store, item.item_id, queue_dir=qdir)
    _sign(qdir, pk, rid)
    review.clear_item(store, item.item_id, mode="manual")       # the laptop cleared it, then it was reopened
    review.reopen_item(store, item.item_id, mode="manual")
    assert review.get_item(store, item.item_id).state == "open"
    with pytest.raises(SignedClearRefused, match="another version") as exc:
        _clear(store, qdir, item, rid, pk)
    assert exc.value.status == 409
    assert [e[:2] for e in _events(store, item.item_id)] == [("cleared", "manual"), ("reopened", "manual")]
    # a fresh request names the version as it is now, and clears it
    rid2 = review.file_clear_request(store, item.item_id, queue_dir=qdir)
    now = review.get_item(store, item.item_id).last
    assert read_request(qdir, rid2).message.endswith(f"{now[3]}#{now[4]}")
    _sign(qdir, pk, rid2)
    assert _clear(store, qdir, item, rid2, pk).last[1] == "signed"


def test_the_signed_clear_files_only_for_an_open_yours_to_clear_item(tmp_path: Path) -> None:
    qdir, store = _q(tmp_path)
    plain, _ = review.file_item(store, "seat", {"text": "x"}, dedupe_key="p")
    with pytest.raises(CoordError, match="not yours to clear"):
        review.file_clear_request(store, plain.item_id, queue_dir=qdir)
    mine = _mine(store)
    review.clear_item(store, mine.item_id)
    with pytest.raises(CoordError, match="already cleared"):
        review.file_clear_request(store, mine.item_id, queue_dir=qdir)
    with pytest.raises(CoordError, match="no review item"):
        review.file_clear_request(store, "rv-" + "0" * 32, queue_dir=qdir)


def test_a_signed_clear_lands_on_a_store_made_before_r4(tmp_path: Path, monkeypatch) -> None:
    """R4's migration ran when the store was opened (test_review_clear_combined holds the migration itself); the
    signed clear then writes its event beside the rows the old store already held."""
    from tests.test_review_clear_combined import _pre_r4_store

    qdir, items = _pre_r4_store(tmp_path, monkeypatch)
    pk = _Passkey()
    store = CoordStore(qdir)
    item = _mine(store)
    rid = review.file_clear_request(store, item.item_id, queue_dir=qdir)
    _sign(qdir, pk, rid)
    assert _clear(CoordStore(qdir), qdir, item, rid, pk).last[1] == "signed"
    assert _events(store, item.item_id) == [("cleared", "signed", "operator", rid)]
    assert _events(store, items[0].item_id) == [("cleared", "manual", "operator", None)]


# --- R1: the cockpit, through the real routes ----------------------------------------------------------------------

from levain.cockpit.coordinator_panels import REVIEW_ID  # noqa: E402
from tests.test_cockpit_k2a import _install  # noqa: E402
from tests.test_cockpit_pairing_http import PROXY, T, _call, _pair, _serve  # noqa: E402
FC = "review-clear-surface-token-0123456789"      # a named surface: honoured proxied, where the owner key is not (W1b L-1)
from tests.test_cockpit_sign_origin import PINNED  # noqa: E402


@pytest.fixture
def cockpit(tmp_path: Path, monkeypatch):
    """A real make_server with a Signer on the install's consent directory (which also holds the coordinator store),
    one plain item and one "yours to clear" item, and a flow-like compose_signer that must never be asked for a kernel
    verb: a dict of what the tests need."""
    home = tmp_path / "home"
    (home / "consent").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    src = _install(tmp_path)[1]
    store = CoordStore(home / "consent")
    plain, _ = review.file_item(store, "seat", {"text": "seat lane-a died"}, dedupe_key="a")
    mine = _mine(store, "b")
    pk = _Passkey()
    signer = Signer(home / "consent", ("flow-needs-phill",), RP, lambda: {pk.key.fingerprint: pk.key})
    signer.page_origin = PINNED
    asked: list = []

    def flow_compose(verb: str, params: dict) -> str:      # flow's own: send_inbox only
        asked.append(verb)
        raise ValueError("flow composes send_inbox only")

    with _serve(src, surface_tokens={"browser": T, "flowconnect": FC}, signer=signer, compose_signer=flow_compose, extra_hosts=[RP]) as (port, httpd):
        yield {"port": port, "store": store, "plain": plain, "mine": mine, "pk": pk, "qdir": home / "consent",
               "asked": asked}


def _row(c: dict, item: review.Item, token: str = FC) -> dict:
    st, panel, _ = _call(c["port"], "GET", f"/cockpit/panel/{REVIEW_ID}.json?profile=full", token=token, headers=PROXY)
    assert st == 200, panel
    return next(r for r in panel["rows"] if r["id"] == f"review:{item.item_id}")


def _compose(c: dict, item: review.Item, txn: str = "draft-0001") -> tuple[int, dict]:
    st, out, _ = _call(c["port"], "POST", "/cockpit/sign/compose",
                       {"verb": "review_clear_signed", "params": {"item": item.item_id}, "client_txn": txn},
                       token=FC, headers=PROXY)
    return st, out


def _ceremony(c: dict, rid: str) -> None:
    st, one, _ = _call(c["port"], "GET", f"/cockpit/sign/{rid}.json", token=FC, headers=PROXY)
    assert st == 200 and one["request"]["message"].startswith("levain review clear "), one
    fp = c["pk"].key.fingerprint
    st, ch, _ = _call(c["port"], "POST", f"/cockpit/sign/{rid}/challenge",
                      {"signer_fp": fp, "request_sha256": one["request"]["sha256"]}, token=FC, headers=PROXY)
    assert st == 200, ch
    st, ans, _ = _call(c["port"], "POST", f"/cockpit/sign/{rid}/answer", c["pk"].assertion(ch["challenge"], PINNED),
                       token=FC, headers=PROXY)
    assert st == 200, ans


def _send(c: dict, item: review.Item, rid: "str | None", txn: str = "draft-0001", token: str = FC) -> tuple[int, dict]:
    row = _row(c, item, token)
    params = {"item": item.item_id} if rid is None else {"item": item.item_id, "sign_request": rid}
    st, out, _ = _call(c["port"], "POST", "/cockpit/verb",
                       {"verb": "review_clear_signed", "panel_id": REVIEW_ID, "row_id": row["id"],
                        "row_version": row["version"], "params": params, "idempotency_key": "sign-" + txn},
                       token=token, headers=PROXY)
    return st, out


def test_the_cockpit_offers_the_signed_clear_only_on_an_open_yours_to_clear_row(cockpit) -> None:
    c = cockpit
    verbs = lambda r: {a["verb"] for a in r.get("actions") or ()}   # noqa: E731
    assert verbs(_row(c, c["mine"])) == {"review_clear_signed"}
    assert "review_clear_signed" not in verbs(_row(c, c["plain"]))


def test_levain_files_the_kernel_request_and_a_signed_clear_lands_signed(cockpit) -> None:
    c = cockpit
    st, out = _compose(c, c["mine"])
    assert st == 200 and sg.request_id_ok(out["request"]), out
    assert c["asked"] == []                        # R1: the embedder's compose_signer is never asked for a kernel verb
    rid = out["request"]
    assert _compose(c, c["mine"])[1]["request"] == rid       # one draft, one request
    _ceremony(c, rid)
    st, done = _send(c, c["mine"], rid)
    assert (st, done.get("review_state"), done.get("mode")) == (200, "cleared", "signed"), done
    assert _events(c["store"], c["mine"].item_id) == [("cleared", "signed", "operator", rid)]


def test_the_cockpit_never_sends_an_unsigned_or_unbound_clear(cockpit) -> None:
    c = cockpit
    st, out = _send(c, c["mine"], None)                       # no sign request at all
    assert (st, out.get("error")) == (403, "sign_required"), out
    rid = _compose(c, c["mine"])[1]["request"]
    st, out = _send(c, c["mine"], rid)                        # filed, not signed
    assert (st, out.get("error")) == (403, "sign_required"), out
    # a request levain filed but no compose bound to this draft (as a same-uid writer could file one)
    loose = review.file_clear_request(c["store"], c["mine"].item_id, queue_dir=c["qdir"])
    _ceremony(c, loose)
    st, out = _send(c, c["mine"], loose, txn="draft-0002")
    assert (st, out.get("error")) == (403, "sign_required"), out
    assert review.get_item(c["store"], c["mine"].item_id).state == "open"


def test_a_cockpit_signature_over_an_older_version_is_refused_after_a_reopen(cockpit) -> None:
    c = cockpit
    rid = _compose(c, c["mine"])[1]["request"]
    _ceremony(c, rid)
    review.clear_item(c["store"], c["mine"].item_id)          # the laptop clears it, and it is reopened
    review.reopen_item(c["store"], c["mine"].item_id)
    st, out = _send(c, c["mine"], rid)
    assert st == 409 and out.get("error") == "stale" and "another version" in out["message"], out
    assert review.get_item(c["store"], c["mine"].item_id).state == "open"


def test_the_signed_compose_refuses_a_plain_item_with_words(cockpit) -> None:
    c = cockpit
    st, out = _compose(c, c["plain"])
    assert st == 400 and "not yours to clear" in out["message"], out


def test_a_paired_device_sends_the_signed_clear_too(cockpit) -> None:
    c = cockpit
    _dev, tok = _pair(c["port"], proxied=True)
    rid = _compose(c, c["mine"])[1]["request"]
    _ceremony(c, rid)
    st, done = _send(c, c["mine"], rid, token=tok)
    assert (st, done.get("mode")) == (200, "signed"), done


# --- R5: the phone port: file the request, sign it on /sign/<id>/, clear with it -----------------------------------

from tests.test_dispatch_phone import repo, world  # noqa: E402,F401  (fixtures)


def _phone_sign(base: str, port: int, world: dict, row: dict, dev: str) -> None:
    from tests.test_coordinator_phone import _post

    origin = f"http://localhost:{port}"
    st, ch = _post(base, f"/sign/{row['id']}/challenge", {"signer_fp": world["phone"].fp,
                                                           "request_sha256": row["sha256"]}, dev)
    assert st == 200, ch
    st, ans = _post(base, f"/sign/{row['id']}/answer", world["phone"].assertion(ch["challenge"], origin), dev)
    assert st == 200 and ans["namespace"] == REVIEW_CLEAR_NAMESPACE, ans


def test_the_phone_files_signs_and_clears_a_yours_to_clear_item_signed(world) -> None:
    from tests.test_coordinator_phone import _post, device_token
    from tests.test_phone_dispatch import _serve

    store = CoordStore(world["qdir"])
    mine = _mine(store)
    dev = device_token(world["qdir"])
    with _serve(world["qdir"], world["qdir"]) as (base, httpd):
        st, got = _post(base, f"/coord/review/{mine.item_id}/sign-request", {}, dev)
        assert st == 200 and got["request"]["message"] == review.clear_message(mine), got
        assert got["signers"] == [world["phone"].fp]
        # the run token never files one, nor clears with one
        from tests.test_phone_server import _TOK

        assert _post(base, f"/coord/review/{mine.item_id}/sign-request", {}, _TOK)[0] == 403
        rid = got["request"]["id"]
        st, out = _post(base, f"/coord/review/{mine.item_id}/clear", {"sign_request": rid}, dev)
        assert (st, out.get("error")) == (403, "sign_required"), out             # filed, not yet signed
        _phone_sign(base, httpd.server_address[1], world, got["request"], dev)
        st, out = _post(base, f"/coord/review/{mine.item_id}/clear", {"sign_request": rid}, dev)
        assert (st, out.get("state"), out.get("mode")) == (200, "cleared", "signed"), out
    assert _events(store, mine.item_id) == [("cleared", "signed", "operator", rid)]


def test_the_phone_refuses_a_denied_or_older_version_signature(world) -> None:
    from tests.test_coordinator_phone import _post, device_token
    from tests.test_phone_dispatch import _serve

    store = CoordStore(world["qdir"])
    mine = _mine(store)
    dev = device_token(world["qdir"])
    with _serve(world["qdir"], world["qdir"]) as (base, httpd):
        got = _post(base, f"/coord/review/{mine.item_id}/sign-request", {}, dev)[1]
        st, _ = _post(base, f"/sign/{got['request']['id']}/deny", {"request_sha256": got["request"]["sha256"]}, dev)
        assert st == 200
        st, out = _post(base, f"/coord/review/{mine.item_id}/clear", {"sign_request": got["request"]["id"]}, dev)
        assert (st, out.get("error")) == (403, "sign_required") and "denied" in out["message"], out
        got = _post(base, f"/coord/review/{mine.item_id}/sign-request", {}, dev)[1]
        _phone_sign(base, httpd.server_address[1], world, got["request"], dev)
        review.clear_item(store, mine.item_id)
        review.reopen_item(store, mine.item_id)
        st, out = _post(base, f"/coord/review/{mine.item_id}/clear", {"sign_request": got["request"]["id"]}, dev)
        assert (st, out.get("error")) == (409, "stale"), out
        # a plain item files no request; a reopen takes no sign request
        plain, _ = review.file_item(store, "seat", {"text": "x"}, dedupe_key="p")
        st, out = _post(base, f"/coord/review/{plain.item_id}/sign-request", {}, dev)
        assert st == 409 and "not yours to clear" in out["message"], out
        st, out = _post(base, f"/coord/review/{mine.item_id}/reopen", {"sign_request": "lv-x"}, dev)
        assert st == 400, out
    assert review.get_item(store, mine.item_id).state == "open"


# --- both pages, in headless Chrome, with a CDP virtual authenticator holding the enrolled passkey ------------------

from tests.test_cockpit_flip_parity import _drive, needs_browser  # noqa: E402


def _authenticator(priv: Any) -> dict:
    import base64

    from cryptography.hazmat.primitives import serialization

    pkcs8 = priv.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                               serialization.NoEncryption())
    return {"authenticator": {"rpId": "localhost", "privateKey": base64.b64encode(pkcs8).decode(),
                              "credentialId": base64.b64encode(b"levain-review-cred").decode(),
                              "userHandle": base64.b64encode(b"levain-review-user").decode()}}


SIGNED_BTN = "[...document.querySelectorAll('[data-ck-verb]')].find(b => b.textContent === 'clear with passkey')"


@needs_browser
def test_the_cockpit_page_clears_a_yours_to_clear_item_with_the_passkey(tmp_path: Path, monkeypatch) -> None:
    import threading

    from levain.web_server import make_server
    from tests.test_cockpit_sign_card import _free_port

    shots = Path(os.environ.get("LEVAIN_SHOT_DIR") or tmp_path)
    home = tmp_path / "home"
    (home / "consent").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    src = _install(tmp_path)[1]
    store = CoordStore(home / "consent")
    mine = _mine(store)
    port = _free_port()
    pk = _Passkey("localhost")
    signer = Signer(home / "consent", (), "localhost", lambda: {pk.key.fingerprint: pk.key})
    signer.page_origin = f"http://localhost:{port}"
    httpd = make_server(src, host="127.0.0.1", port=port, signer=signer, surface_tokens={"browser": T},
                        install_class="reported")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token={T}", [
            {"emulate": {"w": 1440, "h": 1000, "dpr": 1, "mobile": False}}, _authenticator(pk.priv),
            {"until": f"{SIGNED_BTN} && 1", "ms": 30000},
            {"eval": "[...document.querySelectorAll('.review-mine')].map(m => m.textContent)"},
            {"shot": str(shots / "c4mut2_cockpit_before.png"), "full": True},
            {"eval": f"{SIGNED_BTN}.click() || 1"},
            {"until": "[...document.querySelectorAll('[data-ck-verb]')].some(b => b.textContent === 'reopen') && 1",
             "ms": 30000},
            {"shot": str(shots / "c4mut2_cockpit_after.png"), "full": True},
            {"eval": "document.body.innerText.includes('did not finish')"},
        ])
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
    lines = next(v for v in out["values"] if isinstance(v, list))
    assert out["values"][-1] is False, "the page read the clear's answer as a write that did not finish"
    assert any(t.startswith("or on the laptop") for t in lines), out["values"]
    posts = out["posts"]
    assert [p for p in posts if p.startswith("/cockpit/sign/compose")] and \
        [p for p in posts if "/answer" in p], posts
    ev = _events(store, mine.item_id)
    assert len(ev) == 1 and ev[0][:3] == ("cleared", "signed", "operator") and ev[0][3].startswith("lv-"), ev


@needs_browser
def test_the_phone_page_clears_a_yours_to_clear_item_with_the_passkey(world, tmp_path: Path) -> None:
    from tests.test_coordinator_phone import device_token
    from tests.test_phone_dispatch import _serve

    shots = Path(os.environ.get("LEVAIN_SHOT_DIR") or tmp_path)
    store = CoordStore(world["qdir"])
    mine = _mine(store)
    dev = device_token(world["qdir"])
    with _serve(world["qdir"], world["qdir"]) as (base, _):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={dev}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}, _authenticator(world["phone"].priv),
            {"until": "document.getElementById('status').textContent !== 'reading the queue…' && 1"},
            {"eval": "document.getElementById('tab-review').click() || 1"},
            {"until": "[...document.querySelectorAll('#review-list button')].some(b => b.textContent === "
                      "'Clear with passkey') && 1"},
            {"shot": str(shots / "c4mut2_phone_before.png"), "full": True},
            {"eval": "[...document.querySelectorAll('#review-list button')].find(b => b.textContent === "
                     "'Clear with passkey').click() || 1"},
            {"until": "document.querySelectorAll('#review-cleared .review-item').length === 1 && 1", "ms": 30000},
            {"shot": str(shots / "c4mut2_phone_after.png"), "full": True},
        ])
    posts = [p for p in out["posts"] if "/coord/review/" in p or "/sign/" in p]
    ends = [p.split(" ")[0].rsplit("/", 1)[-1] for p in posts]
    assert ends == ["sign-request", "challenge", "answer", "clear"], posts
    ev = _events(store, mine.item_id)
    assert len(ev) == 1 and ev[0][:3] == ("cleared", "signed", "operator"), ev
