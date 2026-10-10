"""Tests for the governed ACTION-verb seam — apply_action + the POST /action route.

The write-peer of extra_panels: a downstream control plane registers
make_server(extra_verbs={name: ActionVerb(...)}) and the kernel dispatches each through
POST /action under the SAME auth + confirm + audit envelope as POST /edit. Two layers:
the pure dispatcher (apply_action) + the live route (make_server / POST /action).
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from levain.cockpit.verbs import VerbSpec
from levain.dashboard import SubstrateSource
from levain.web_server import make_server
from levain.writes import ActionVerb, EditError, WriteScope, apply_action, recent_edits


def _scope(tmp_path: Path) -> WriteScope:
    """A WriteScope whose ledger_root (tmp/.levain) holds the action audit trail."""
    return WriteScope.from_install_root(tmp_path)


def _writable_source(tmp_path: Path) -> SubstrateSource:
    """A minimal full install → a WRITABLE SubstrateSource (extra_verbs requires one)."""
    root = tmp_path / "install"
    (root / "seed").mkdir(parents=True)
    (root / "activation").mkdir(parents=True)
    (root / ".levain").mkdir()
    for fn in ("world.md", "origin.md", "partnership.md", "memory.md", "spore_instructions.md"):
        (root / "seed" / fn).write_text("# seed\n\n## Identity\n\nx\n", encoding="utf-8")
    (root / "activation" / "posture.md").write_text("Slow is fast.\n", encoding="utf-8")
    (root / "activation" / "recency_directives.md").write_text("No gatekeeping.\n", encoding="utf-8")
    return SubstrateSource.local(root)


def _readonly_source(tmp_path: Path) -> SubstrateSource:
    """A read-only source (no write_scope) — for the 'verbs need a writable source' refusal."""
    from anneal_memory import Store
    from levain.dashboard import AnnealPaths

    db = tmp_path / "memory.db"
    with Store(db) as store:
        store.record("decided X", "decision")
    return SubstrateSource(anneal=AnnealPaths.from_db(db))


def _noinstall_writable_source(tmp_path: Path) -> SubstrateSource:
    """A NON-install writable source (write_scope set, install_root None) — flow's actual Bridge
    shape (anneal store + a ledger, no .levain install). Unlike an install-bearing source (which
    is loopback-only UNCONDITIONALLY), this one's off-box bind is governed by the write_token."""
    from anneal_memory import Store
    from levain.dashboard import AnnealPaths

    db = tmp_path / "memory.db"
    with Store(db) as store:
        store.record("decided X", "decision")
    anneal = AnnealPaths.from_db(db)
    scope = WriteScope(anneal=anneal, ledger_root=tmp_path / "ledger", install_root=None)
    return SubstrateSource(anneal=anneal, write_scope=scope)


# K2a: every POST to /action on a writable source carries a surface credential on EVERY bind, and an
# ActionVerb with no tier function is T2, which this build refuses "needs_broker" over HTTP (the broker
# is K2b). A downstream verb that may fire over HTTP registers a VerbSpec whose tier_fn answers "C1".
_TOK = "test-surface-token"
_TOK_HEADER = "X-Levain-Write-Token"


def _c1_spec(name: str, fire, *, confirm_required: bool = False, label: str = "C1 test verb") -> VerbSpec:
    """A downstream verb the kernel will fire over /action: tier_fn -> C1, panel-bound, one param."""
    return VerbSpec(name, label, label, "panel", fields=("text",), floor="C1",
                    tier_fn=lambda params, row, provenance, today: "C1",
                    confirm_required=confirm_required, fire=fire)


@contextmanager
def _serving_verbs(source: SubstrateSource, verbs: dict):
    # each verb offered by one external panel (ext:p_<verb>): a downstream VerbSpec fires only on
    # /cockpit/verb, bound to the panel that offers it (K2a)
    panels = [{"id": f"p_{n}", "title": n, "lines": [],
               "action": {"verb": n, "fields": [{"name": "text", "kind": "text"}]}} for n in verbs]
    httpd = make_server(source, host="127.0.0.1", port=0, extra_verbs=verbs, write_token=_TOK,
                        extra_panels=lambda: panels)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{httpd.server_address[0]}:{httpd.server_address[1]}", httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _post(url: str, payload, *, headers: dict | None = None, token: str | None = _TOK):
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if token is not None:
        h[_TOK_HEADER] = token
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, method="POST", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 — loopback only
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# --- the pure dispatcher --------------------------------------------------------------

class TestApplyAction:
    def test_unknown_verb_404(self, tmp_path: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_action(_scope(tmp_path), {}, {"verb": "nope"})
        assert ei.value.code == "unknown_verb" and ei.value.http_status == 404

    def test_confirm_required_blocks_without_confirm(self, tmp_path: Path) -> None:
        called: list = []
        reg = {"send": ActionVerb(handler=lambda p: called.append(p) or {"ok": True})}  # confirm default True
        scope = _scope(tmp_path)
        with pytest.raises(EditError) as ei:
            apply_action(scope, reg, {"verb": "send", "params": {"to": "x"}})
        assert ei.value.code == "confirm_required" and ei.value.http_status == 409
        assert called == []                              # handler NEVER ran
        assert recent_edits(scope.ledger_root) == []     # NO audit — nothing happened

    def test_confirm_true_executes_and_audits(self, tmp_path: Path) -> None:
        reg = {"send": ActionVerb(handler=lambda p: {"ok": True, "summary": "sent to argushub"},
                                  label="Send to inbox")}
        scope = _scope(tmp_path)
        result = apply_action(scope, reg, {"verb": "send", "params": {"to": "x"}, "confirm": True})
        assert result["ok"] and result["verb"] == "send" and result["outcome"] == "ok"
        recs = recent_edits(scope.ledger_root)
        assert len(recs) == 1
        r = recs[0]
        assert r["kind"] == "action" and r["action"] == "send" and r["actor"] == "operator"
        assert r["outcome"] == "ok" and r["detail"] == "sent to argushub"  # the summary is the trace
        assert r["undoable"] is False                    # a sent message has no undo

    def test_confirm_not_required_runs_without_confirm(self, tmp_path: Path) -> None:
        reg = {"peek": ActionVerb(handler=lambda p: {"ok": True}, confirm_required=False)}
        result = apply_action(_scope(tmp_path), reg, {"verb": "peek"})
        assert result["ok"] and result["outcome"] == "ok"

    def test_handler_raises_audits_error_and_502(self, tmp_path: Path) -> None:
        def _boom(p):
            raise RuntimeError("argushub down")

        reg = {"send": ActionVerb(handler=_boom, confirm_required=False)}
        scope = _scope(tmp_path)
        with pytest.raises(EditError) as ei:
            apply_action(scope, reg, {"verb": "send"})
        assert ei.value.code == "action_failed" and ei.value.http_status == 502
        recs = recent_edits(scope.ledger_root)               # the ATTEMPT is still audited
        assert len(recs) == 1 and recs[0]["outcome"] == "error" and recs[0]["detail"] == "RuntimeError"

    def test_non_dict_result_is_502(self, tmp_path: Path) -> None:
        reg = {"bad": ActionVerb(handler=lambda p: "not a dict", confirm_required=False)}  # type: ignore[arg-type,return-value]
        with pytest.raises(EditError) as ei:
            apply_action(_scope(tmp_path), reg, {"verb": "bad"})
        assert ei.value.code == "action_failed" and ei.value.http_status == 502

    def test_handler_editerror_passes_through_with_its_status_no_audit(self, tmp_path: Path) -> None:
        # a handler raising EditError signals a TYPED pre-execution refusal (e.g. bad input → 400):
        # re-raised AS-IS (the handler picked its status, distinct from a 502 execution failure)
        # and — by contract, raised before any side effect — NOT audited (nothing happened).
        def _reject(p):
            raise EditError("bad_request", 400, "bad input")

        reg = {"v": ActionVerb(handler=_reject, confirm_required=False)}
        scope = _scope(tmp_path)
        with pytest.raises(EditError) as ei:
            apply_action(scope, reg, {"verb": "v"})
        assert ei.value.code == "bad_request" and ei.value.http_status == 400   # status preserved
        assert recent_edits(scope.ledger_root) == []                            # NO audit line

    def test_params_omitted_is_empty_dict_but_malformed_is_400(self, tmp_path: Path) -> None:
        seen: list = []
        reg = {"p": ActionVerb(handler=lambda p: seen.append(p) or {"ok": True}, confirm_required=False)}
        apply_action(_scope(tmp_path), reg, {"verb": "p"})            # OMITTED params → {}
        assert seen == [{}]
        # PRESENT but non-dict → 400, NOT silently coerced to {} (a mutation seam must not turn
        # malformed JSON into a valid default action — codex L3).
        with pytest.raises(EditError) as ei:
            apply_action(_scope(tmp_path), reg, {"verb": "p", "params": "nope"})
        assert ei.value.code == "bad_request" and ei.value.http_status == 400
        assert seen == [{}]                                          # handler NOT re-called


# --- the at-most-once idempotency path (irreversible verbs) ---------------------------

T0 = "2026-06-27T12:00:00+00:00"
T_SOON = "2026-06-27T12:05:00+00:00"          # within the dedup window


def _counting_verb(results=None):
    """An idempotent verb whose handler counts calls; each call returns a distinct id so a replay
    (which must NOT re-fire) is detectable by the id staying the original."""
    calls: list[dict] = []

    def _h(p):
        calls.append(p)
        return {"summary": f"fired #{len(calls)}", "n": len(calls)}

    return ActionVerb(handler=_h, confirm_required=False, idempotent=True, label="Send"), calls


class TestApplyActionIdempotency:
    def test_idempotent_verb_requires_a_key(self, tmp_path: Path) -> None:
        verb, calls = _counting_verb()
        scope = _scope(tmp_path)
        with pytest.raises(EditError) as ei:
            apply_action(scope, {"v": verb}, {"verb": "v"}, now=T0)   # no idempotency_key
        assert ei.value.code == "bad_request" and ei.value.http_status == 400
        assert calls == []                                           # handler never ran
        assert recent_edits(scope.ledger_root) == []                # nothing happened, nothing audited

    def test_oversize_key_rejected(self, tmp_path: Path) -> None:
        verb, _ = _counting_verb()
        with pytest.raises(EditError) as ei:
            apply_action(_scope(tmp_path), {"v": verb},
                         {"verb": "v", "idempotency_key": "x" * 5000}, now=T0)
        assert ei.value.http_status == 400

    def test_replay_returns_recorded_response_without_refire(self, tmp_path: Path) -> None:
        verb, calls = _counting_verb()
        scope = _scope(tmp_path)
        req = {"verb": "v", "params": {"to": "chip"}, "idempotency_key": "key-1"}
        first = apply_action(scope, {"v": verb}, req, now=T0)
        assert first["result"]["n"] == 1 and "replayed" not in first
        # the retry: SAME key + params → the original response verbatim, no re-fire
        second = apply_action(scope, {"v": verb}, dict(req), now=T_SOON)
        assert second["replayed"] is True
        assert second["id"] == first["id"] and second["result"] == first["result"]
        assert len(calls) == 1                                       # handler fired exactly ONCE
        # the receipt corpus: the original `ok` line + a `replay` line, both carrying the key
        recs = recent_edits(scope.ledger_root)
        oks = [r for r in recs if r["outcome"] == "ok"]
        replays = [r for r in recs if r["outcome"] == "replay"]
        assert len(oks) == 1 and oks[0]["idempotency_key"] == "key-1"
        assert len(replays) == 1 and replays[0]["idempotency_key"] == "key-1"

    def test_key_reuse_for_a_different_request_is_422(self, tmp_path: Path) -> None:
        verb, calls = _counting_verb()
        scope = _scope(tmp_path)
        apply_action(scope, {"v": verb},
                     {"verb": "v", "params": {"to": "chip"}, "idempotency_key": "key-1"}, now=T0)
        with pytest.raises(EditError) as ei:                         # same key, DIFFERENT params
            apply_action(scope, {"v": verb},
                         {"verb": "v", "params": {"to": "daemon"}, "idempotency_key": "key-1"},
                         now=T_SOON)
        assert ei.value.code == "idempotency_key_reuse" and ei.value.http_status == 422
        assert len(calls) == 1                                       # the second never fired

    def test_concurrent_duplicate_in_flight_is_409(self, tmp_path: Path) -> None:
        # seed an in_flight reservation (a duplicate still executing) → a second dispatch refuses
        from levain.idempotency import IdempotencyStore, request_fingerprint

        verb, calls = _counting_verb()
        scope = _scope(tmp_path)
        store = IdempotencyStore(scope.ledger_root / "idempotency.json")
        fp = request_fingerprint("v", {"to": "chip"})
        assert store.claim_or_replay("key-1", fp, T0).kind == "fresh"   # reserve, never finalize
        with pytest.raises(EditError) as ei:
            apply_action(scope, {"v": verb},
                         {"verb": "v", "params": {"to": "chip"}, "idempotency_key": "key-1"},
                         now=T_SOON)
        assert ei.value.code == "in_flight" and ei.value.http_status == 409
        assert calls == []                                          # did not re-fire

    def test_pre_fire_editerror_releases_the_key_so_a_retry_refires(self, tmp_path: Path) -> None:
        # the handler raises EditError on the FIRST call (a pre-side-effect refusal), succeeds on
        # the SECOND — the release must let the same key re-fire (nothing happened the first time).
        state = {"n": 0}

        def _h(p):
            state["n"] += 1
            if state["n"] == 1:
                raise EditError("bad_request", 400, "transient validation")
            return {"summary": "ok now"}

        verb = ActionVerb(handler=_h, confirm_required=False, idempotent=True)
        scope = _scope(tmp_path)
        req = {"verb": "v", "idempotency_key": "key-1"}
        with pytest.raises(EditError):
            apply_action(scope, {"v": verb}, dict(req), now=T0)     # released
        out = apply_action(scope, {"v": verb}, dict(req), now=T_SOON)  # re-fires with the same key
        assert out["ok"] and state["n"] == 2

    def test_execution_fault_poisons_the_key_so_a_replay_does_not_refire(self, tmp_path: Path) -> None:
        # an execution fault is AMBIGUOUS (the side effect may have happened) → at-most-once POISONS
        # the reservation (faulted); a retry of the same key gets 409 faulted, never a second fire —
        # and a faulted key never expires (a far-future retry STILL refuses, never re-fires).
        from levain.idempotency import IdempotencyStore

        state = {"n": 0}

        def _h(p):
            state["n"] += 1
            raise RuntimeError("relay transport down")

        verb = ActionVerb(handler=_h, confirm_required=False, idempotent=True)
        scope = _scope(tmp_path)
        req = {"verb": "v", "idempotency_key": "key-1"}
        with pytest.raises(EditError) as ei:
            apply_action(scope, {"v": verb}, dict(req), now=T0)
        assert ei.value.http_status == 502
        # the error attempt is audited WITH the key; the record is now `faulted`
        errs = [r for r in recent_edits(scope.ledger_root) if r["outcome"] == "error"]
        assert len(errs) == 1 and errs[0]["idempotency_key"] == "key-1"
        rec = IdempotencyStore(scope.ledger_root / "idempotency.json").get("key-1")
        assert rec is not None and rec.status == "faulted"
        # the retry refuses with the distinct faulted code — NO second fire — even far in the future
        with pytest.raises(EditError) as ei2:
            apply_action(scope, {"v": verb}, dict(req), now="2099-01-01T00:00:00+00:00")
        assert ei2.value.code == "faulted" and ei2.value.http_status == 409 and state["n"] == 1

    def test_finalize_failure_does_not_turn_success_into_a_500(self, tmp_path: Path, monkeypatch) -> None:
        # L1 MED-1: a finalize write-fault on the SUCCESS path must NOT turn a real 200 into a false
        # 500 (which the operator would read as failure → recompose → a NEW key → double-send). The
        # response stands; a same-key replay simply 409s (no cache) instead of re-firing.
        from levain import idempotency

        fired: list = []
        verb = ActionVerb(handler=lambda p: fired.append(p) or {"summary": "sent"},
                          confirm_required=False, idempotent=True)
        scope = _scope(tmp_path)

        def _boom(self, *a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(idempotency.IdempotencyStore, "finalize", _boom)
        with pytest.warns(RuntimeWarning, match="finalize failed"):
            out = apply_action(scope, {"v": verb}, {"verb": "v", "idempotency_key": "key-1"}, now=T0)
        assert out["ok"] is True and len(fired) == 1     # the send succeeded; the operator gets 200

    def test_corrupt_record_fails_closed(self, tmp_path: Path) -> None:
        import json as _json

        verb, calls = _counting_verb()
        scope = _scope(tmp_path)
        ledger = scope.ledger_root
        ledger.mkdir(parents=True, exist_ok=True)
        # a matching record with an unknown status → the store returns `corrupt` → 500, never refire
        (ledger / "idempotency.json").write_text(_json.dumps([
            {"key": "key-1", "fingerprint": "whatever", "status": "bogus",
             "created_at": T0, "result": None, "completed_at": None}]), encoding="utf-8")
        with pytest.raises(EditError) as ei:
            apply_action(scope, {"v": verb},
                         {"verb": "v", "params": {"to": "chip"}, "idempotency_key": "key-1"},
                         now=T_SOON)
        assert ei.value.code == "idempotency_corrupt" and ei.value.http_status == 500
        assert calls == []

    def test_non_idempotent_verb_ignores_a_key(self, tmp_path: Path) -> None:
        # a non-idempotent verb (the legacy path) does not require/route through the store even if
        # a key is present — backward-compatible, no idempotency.json written.
        calls: list = []
        verb = ActionVerb(handler=lambda p: calls.append(p) or {"ok": True}, confirm_required=False)
        scope = _scope(tmp_path)
        apply_action(scope, {"v": verb}, {"verb": "v", "idempotency_key": "ignored"}, now=T0)
        apply_action(scope, {"v": verb}, {"verb": "v", "idempotency_key": "ignored"}, now=T_SOON)
        assert len(calls) == 2                                        # both fired (no dedup)
        assert not (scope.ledger_root / "idempotency.json").exists()  # store untouched


# --- make_server validation + the live POST /action route -----------------------------

class TestActionRegistration:
    def test_refuses_verbs_on_readonly_source(self, tmp_path: Path) -> None:
        # action verbs are mutations → require a writable source; a read-only one is refused
        # (so the off-box write-token governance, keyed on write_scope, always covers /action).
        with pytest.raises(ValueError, match="WRITABLE"):
            make_server(_readonly_source(tmp_path), port=0,
                        extra_verbs={"send": ActionVerb(handler=lambda p: {})})

    def test_refuses_non_actionverb_spec(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="ActionVerb"):
            make_server(_writable_source(tmp_path), port=0,
                        extra_verbs={"send": (lambda p: {})})  # a bare callable, not an ActionVerb

    def test_refuses_empty_verb_name(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            make_server(_writable_source(tmp_path), port=0,
                        extra_verbs={"": ActionVerb(handler=lambda p: {})})

    def test_refuses_bad_actionverb_field_types(self, tmp_path: Path) -> None:
        # codex L3: an ActionVerb whose runtime fields are the wrong type passes the isinstance
        # check but later breaks dispatch / serialization (label is JSON-emitted in action_verbs).
        # Each must be refused AT REGISTRATION, before bind. (One source — validation raises before
        # any bind, so it's reused across the three cases.)
        src = _writable_source(tmp_path)
        with pytest.raises(ValueError, match="label must be a string"):
            make_server(src, port=0,
                        extra_verbs={"v": ActionVerb(handler=lambda p: {}, label=object())})  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="confirm_required must be a bool"):
            make_server(src, port=0,
                        extra_verbs={"v": ActionVerb(handler=lambda p: {}, confirm_required="yes")})  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="handler must be callable"):
            make_server(src, port=0,
                        extra_verbs={"v": ActionVerb(handler="nope")})  # type: ignore[arg-type]

    def test_action_is_a_reserved_route(self, tmp_path: Path) -> None:
        # /action can't be shadowed by a downstream extra_json route (it's a write route).
        with pytest.raises(ValueError, match="built-in route"):
            make_server(_writable_source(tmp_path), port=0,
                        extra_json={"/action": lambda: b"{}"})

    def test_offbox_writable_with_verbs_needs_token(self, tmp_path: Path) -> None:
        # the security claim: extra_verbs ⟹ write_scope ⟹ the existing off-box refusal covers
        # /action. A NON-install writable source (flow's Bridge shape) bound off-loopback without
        # a write_token is refused BEFORE binding — the SAME refusal substrate writes get, so the
        # action route inherits the off-box governance with no second, weaker auth path. (An
        # install-bearing source is loopback-only unconditionally — a stronger, separate rule.)
        with pytest.raises(ValueError, match="write_token"):
            make_server(_noinstall_writable_source(tmp_path), host="10.0.0.5", port=0,
                        extra_verbs={"send": ActionVerb(handler=lambda p: {})})

    def test_offbox_writable_empty_token_refused_not_bricked(self, tmp_path: Path) -> None:
        # an EMPTY-string write_token must REFUSE the off-box bind (not bind-then-403-every-write):
        # the bind-refusal keys on `not write_token`, matching the per-request gate's `not expected`
        # — a clean refusal beats a silently bricked write surface [L2 LOW].
        with pytest.raises(ValueError, match="write_token"):
            make_server(_noinstall_writable_source(tmp_path), host="10.0.0.5", port=0,
                        write_token="", extra_verbs={"send": ActionVerb(handler=lambda p: {})})


class TestActionRoute:
    def test_plain_action_verb_is_refused_needs_broker_and_never_runs(self, tmp_path: Path) -> None:
        # K2a: an ActionVerb with no tier function is T2 -> 403 needs_broker over /action even with a
        # valid credential and confirm:true. The handler NEVER ran and nothing was audited. (The
        # handler's own happy path is covered by TestApplyAction above, apply_action direct.)
        sent: list = []
        verbs = {"send_test": ActionVerb(
            handler=lambda p: sent.append(p) or {"ok": True, "summary": "did it"}, label="Send")}
        src = _writable_source(tmp_path)
        with _serving_verbs(src, verbs) as (base, _httpd):
            status, body = _post(base + "/action",
                                 {"verb": "send_test", "params": {"x": 1}, "confirm": True})
        assert status == 403 and body["error"] == "needs_broker"
        assert sent == []                                 # the handler never ran
        assert recent_edits(src.write_scope.ledger_root) == []

    def test_c1_downstream_verb_fires_over_the_route(self, tmp_path: Path) -> None:
        # a downstream VerbSpec whose tier_fn answers C1 fires over /cockpit/verb (on the panel that
        # offers it) with the token, and receives (scope, params, target, confirm); /action refuses it.
        got: list = []

        def fire(scope, params, target, confirm):
            got.append((scope, params, target, confirm))
            return {"ok": True, "summary": "fired"}

        src = _writable_source(tmp_path)
        with _serving_verbs(src, {"c1_test": _c1_spec("c1_test", fire)}) as (base, _httpd):
            status, body = _post(base + "/cockpit/verb", {"verb": "c1_test", "panel_id": "ext:p_c1_test",
                                                           "params": {"text": "hi"}, "confirm": True})
            st2, body2 = _post(base + "/action", {"verb": "c1_test", "params": {"text": "hi"}, "confirm": True})
        assert status == 200 and body["ok"] is True, body
        assert (st2, body2["error"]) == (404, "unknown_verb")
        assert len(got) == 1 and got[0][1] == {"text": "hi"} and got[0][3] is True
        assert got[0][0] is src.write_scope

    def test_c1_downstream_verb_rejects_a_field_outside_its_allowlist(self, tmp_path: Path) -> None:
        got: list = []
        with _serving_verbs(_writable_source(tmp_path),
                            {"c1_test": _c1_spec("c1_test", lambda *a: got.append(a) or {"ok": True})}
                            ) as (base, _httpd):
            status, body = _post(base + "/cockpit/verb",
                                 {"verb": "c1_test", "panel_id": "ext:p_c1_test", "params": {"other": 1}})
        assert status == 400 and body["error"] == "field_not_allowed"
        assert got == []

    def test_c1_downstream_confirm_required_409(self, tmp_path: Path) -> None:
        # the confirm gate still holds for a C1 downstream verb: no confirm -> 409, handler never ran.
        ran: list = []
        spec = _c1_spec("c1_test", lambda *a: ran.append(a) or {"ok": True}, confirm_required=True)
        with _serving_verbs(_writable_source(tmp_path), {"c1_test": spec}) as (base, _httpd):
            req = {"verb": "c1_test", "panel_id": "ext:p_c1_test", "params": {"text": "x"}}
            status, body = _post(base + "/cockpit/verb", req)
            assert status == 409 and body["error"] == "confirm_required"
            assert ran == []                              # NO execution without confirm
            status, _body = _post(base + "/cockpit/verb", dict(req, confirm=True))
        assert status == 200 and len(ran) == 1

    def test_action_without_a_credential_is_403_before_anything_runs(self, tmp_path: Path) -> None:
        ran: list = []
        spec = _c1_spec("c1_test", lambda *a: ran.append(a) or {"ok": True})
        with _serving_verbs(_writable_source(tmp_path), {"c1_test": spec}) as (base, _httpd):
            for tok in (None, "wrong"):
                status, body = _post(base + "/action", {"verb": "c1_test", "params": {}}, token=tok)
                assert status == 403 and body["error"] == "credential_required"
        assert ran == []

    def test_idempotent_action_verb_is_refused_needs_broker_over_the_live_route(self, tmp_path: Path) -> None:
        # K2a: the idempotency envelope (same key twice fires once, replayed:true) lives in apply_action
        # and stays covered by TestApplyAction. Over the live route a plain irreversible ActionVerb is T2:
        # BOTH posts are refused needs_broker, the handler never runs, and no ledger record is written.
        fired: list = []
        verbs = {"send_relay": ActionVerb(
            handler=lambda p: fired.append(p) or {"ok": True, "summary": "relay -> chip"},
            confirm_required=True, idempotent=True, label="Send to relay")}
        body1 = {"verb": "send_relay", "params": {"message": "hi"}, "confirm": True,
                 "idempotency_key": "retry-key-1"}
        src = _writable_source(tmp_path)
        with _serving_verbs(src, verbs) as (base, _httpd):
            s1, b1 = _post(base + "/action", body1)
            s2, b2 = _post(base + "/action", dict(body1))
        assert s1 == 403 and b1["error"] == "needs_broker"
        assert s2 == 403 and b2["error"] == "needs_broker" and "replayed" not in b2
        assert fired == []
        assert recent_edits(src.write_scope.ledger_root) == []

    def test_idempotent_missing_key_is_refused_needs_broker_over_the_route(self, tmp_path: Path) -> None:
        # the missing-key 400 is apply_action's (covered above, direct); over the route the T2 tier
        # refuses first, so the claim here is only that the handler cannot run keyless.
        ran: list = []
        verbs = {"send_relay": ActionVerb(handler=lambda p: ran.append(p) or {"ok": True},
                                          confirm_required=False, idempotent=True)}
        with _serving_verbs(_writable_source(tmp_path), verbs) as (base, _httpd):
            status, body = _post(base + "/action", {"verb": "send_relay", "params": {}})
        assert status == 403 and body["error"] == "needs_broker"
        assert ran == []

    def test_unknown_verb_404(self, tmp_path: Path) -> None:
        with _serving_verbs(_writable_source(tmp_path), {}) as (base, _httpd):
            status, body = _post(base + "/action", {"verb": "ghost", "confirm": True})
        assert status == 404 and body["error"] == "unknown_verb"

    def test_a_kernel_verb_is_not_an_action_alias(self, tmp_path: Path) -> None:
        # /action reaches DOWNSTREAM verbs only; a kernel verb name is 404 unknown_verb, not a bypass.
        with _serving_verbs(_writable_source(tmp_path), {}) as (base, _httpd):
            status, body = _post(base + "/action", {"verb": "spore_touch", "params": {}})
        assert status == 404 and body["error"] == "unknown_verb"

    def test_action_shares_the_edit_auth_gate_cross_origin_refused(self, tmp_path: Path) -> None:
        # /action rides the SAME CSRF gate as /edit: a cross-site write is refused (403), proving
        # there's no second, weaker auth path for the action route.
        verbs = {"send_test": ActionVerb(handler=lambda p: {"ok": True}, confirm_required=False)}
        with _serving_verbs(_writable_source(tmp_path), verbs) as (base, _httpd):
            status, _body = _post(base + "/action", {"verb": "send_test"},
                                  headers={"Sec-Fetch-Site": "cross-site"})
        assert status == 403


# --- the async-job (consult) seam: apply_action job dispatch + the /job.json poll route ----

import time as _time  # noqa: E402

from levain.jobs import JobRuntime, JobStore  # noqa: E402
from levain.web_server import build_job_json  # noqa: E402


def _get(url: str):
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 — loopback only
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _wait_job(store: JobStore, job_id: str, status: str, timeout: float = 3.0) -> dict:
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        st = store.read_status(job_id, _now_aware())
        if st.get("status") == status:
            return st
        _time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach {status}")


def _now_aware() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


class TestApplyActionJobPath:
    def test_job_verb_without_runtime_500_and_releases_key(self, tmp_path: Path) -> None:
        # a job verb but no runtime is a config bug → fail-closed 500; the idempotency key is
        # released (pre-fire, nothing ran) so a fixed-config retry can re-attempt.
        scope = _scope(tmp_path)
        reg = {"consult": ActionVerb(handler=lambda p: {"text": "x"}, idempotent=True, job=True,
                                     confirm_required=False)}
        with pytest.raises(EditError) as ei:
            apply_action(scope, reg, {"verb": "consult", "idempotency_key": "k1"}, job_runtime=None)
        assert ei.value.http_status == 500 and ei.value.code == "job_unavailable"

    def test_job_propose_returns_handle_and_polls_done(self, tmp_path: Path) -> None:
        scope = _scope(tmp_path)
        rt = JobRuntime(JobStore(tmp_path / "jobs.json"), max_concurrent=2)
        reg = {"consult": ActionVerb(handler=lambda p: {"text": "REVIEW " + p["q"], "summary": "s"},
                                     idempotent=True, job=True, confirm_required=False)}
        r = apply_action(scope, reg, {"verb": "consult", "params": {"q": "hi"},
                                      "idempotency_key": "k1"}, job_runtime=rt)
        assert r["ok"] and r["status"] == "pending" and "job_id" in r
        st = _wait_job(rt.store, r["job_id"], "done")
        assert st["result"]["text"] == "REVIEW hi"

    def test_replay_returns_same_job_no_duplicate_launch(self, tmp_path: Path) -> None:
        # the core safety property: a deduped retry replays the SAME job_id and the handler fires
        # EXACTLY ONCE (no duplicate expensive job).
        scope = _scope(tmp_path)
        rt = JobRuntime(JobStore(tmp_path / "jobs.json"), max_concurrent=2)
        calls: list = []
        reg = {"consult": ActionVerb(handler=lambda p: calls.append(p) or {"text": "ok"},
                                     idempotent=True, job=True, confirm_required=False)}
        req = {"verb": "consult", "params": {"q": "x"}, "idempotency_key": "k1"}
        r1 = apply_action(scope, reg, dict(req), job_runtime=rt)
        r2 = apply_action(scope, reg, dict(req), job_runtime=rt)
        assert r2["job_id"] == r1["job_id"] and r2.get("replayed") is True
        _wait_job(rt.store, r1["job_id"], "done")
        assert len(calls) == 1  # the replay did NOT launch a second job

    def test_back_pressure_503_releases_key(self, tmp_path: Path) -> None:
        scope = _scope(tmp_path)
        rt = JobRuntime(JobStore(tmp_path / "jobs.json"), max_concurrent=1)
        gate = threading.Event()
        reg = {"consult": ActionVerb(handler=lambda p: (gate.wait(2), {"text": "x"})[1],
                                     idempotent=True, job=True, confirm_required=False)}
        r1 = apply_action(scope, reg, {"verb": "consult", "params": {"q": "a"},
                                       "idempotency_key": "k1"}, job_runtime=rt)
        _wait_job(rt.store, r1["job_id"], "running")
        with pytest.raises(EditError) as ei:
            apply_action(scope, reg, {"verb": "consult", "params": {"q": "b"},
                                      "idempotency_key": "k2"}, job_runtime=rt)
        assert ei.value.http_status == 503 and ei.value.code == "busy"
        gate.set()

    def test_queued_then_ok_audit_trail(self, tmp_path: Path) -> None:
        scope = _scope(tmp_path)
        rt = JobRuntime(JobStore(tmp_path / "jobs.json"), max_concurrent=1)
        reg = {"consult": ActionVerb(handler=lambda p: {"text": "x", "summary": "did it"},
                                     idempotent=True, job=True, confirm_required=False)}
        r = apply_action(scope, reg, {"verb": "consult", "params": {"q": "x"},
                                      "idempotency_key": "k1"}, job_runtime=rt)
        _wait_job(rt.store, r["job_id"], "done")
        _time.sleep(0.1)  # let the worker's completion audit land
        outcomes = [rec["outcome"] for rec in recent_edits(scope.ledger_root)]
        assert "queued" in outcomes and "ok" in outcomes  # propose audits queued, worker audits ok


class TestJobPollRoute:
    def test_live_propose_is_refused_needs_broker_and_no_job_is_created(self, tmp_path: Path) -> None:
        # K2a: a job ActionVerb is T2, so POST /action refuses to propose it (needs_broker) and no job
        # is queued. The propose semantics stay covered by TestApplyActionJobPath (apply_action direct).
        ran: list = []
        verbs = {"consult": ActionVerb(handler=lambda p: ran.append(p) or {"text": "synthesis"},
                                       idempotent=True, job=True, confirm_required=True,
                                       label="Consult")}
        src = _writable_source(tmp_path)
        with _serving_verbs(src, verbs) as (base, _httpd):
            status, body = _post(base + "/action",
                                 {"verb": "consult", "params": {"q": "x"}, "confirm": True,
                                  "idempotency_key": "k1"})
        assert status == 403 and body["error"] == "needs_broker"
        assert ran == []
        assert recent_edits(src.write_scope.ledger_root) == []

    def test_live_poll_route_serves_a_job_to_done(self, tmp_path: Path) -> None:
        # the GET /job.json poll route, end to end over the REAL server: the job is proposed through
        # apply_action against the server's own runtime (as the broker will in K2b), then polled over
        # HTTP until done.
        verbs = {"consult": ActionVerb(handler=lambda p: {"text": "synthesis", "summary": "s"},
                                       idempotent=True, job=True, confirm_required=True,
                                       label="Consult")}
        src = _writable_source(tmp_path)
        with _serving_verbs(src, verbs) as (base, httpd):
            assert httpd.job_runtime is not None          # a job verb auto-creates the runtime
            r = apply_action(src.write_scope, verbs,
                             {"verb": "consult", "params": {"q": "x"}, "confirm": True,
                              "idempotency_key": "k1"}, job_runtime=httpd.job_runtime)
            assert r["status"] == "pending"
            jid = r["job_id"]
            # poll until terminal
            for _ in range(150):
                st, pb = _get(base + "/job.json?id=" + jid)
                assert st == 200
                if pb["status"] in ("done", "failed"):
                    break
                _time.sleep(0.05)
            assert pb["status"] == "done" and pb["result"]["text"] == "synthesis"

    def test_poll_unknown_id(self, tmp_path: Path) -> None:
        verbs = {"consult": ActionVerb(handler=lambda p: {"text": "x"}, idempotent=True, job=True,
                                       confirm_required=False, label="Consult")}
        with _serving_verbs(_writable_source(tmp_path), verbs) as (base, _httpd):
            st, body = _get(base + "/job.json?id=ghost")
            assert st == 200 and body["status"] == "unknown"

    def test_poll_missing_id_400(self, tmp_path: Path) -> None:
        verbs = {"consult": ActionVerb(handler=lambda p: {"text": "x"}, idempotent=True, job=True,
                                       confirm_required=False, label="Consult")}
        with _serving_verbs(_writable_source(tmp_path), verbs) as (base, _httpd):
            st, body = _get(base + "/job.json")
            assert st == 400 and body["error"] == "bad_request"


def test_build_job_json_no_runtime_unknown() -> None:
    # a server with no job runtime → unknown (it has no jobs); never a crash.
    assert json.loads(build_job_json(None, "anything")) == {"job_id": "anything", "status": "unknown"}
