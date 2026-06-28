"""Tests for levain.web_server — the Slice-1 local sovereign web-app.

The server is exercised as a REAL bound ``ThreadingHTTPServer`` serving over a
loopback socket against a real (temp) anneal store — routes are hit with
``urllib``, response bodies + headers + the read-only invariant are asserted, not
mocked. The one thing NOT machine-checkable here is the in-browser render (needs
a live browser); that is the documented L4 manual canary.

Load-bearing guards: ``/substrate.json`` carries the full SubstrateView shape; the
route set is a closed allowlist (no path-traversal surface); requests never mutate
the store; the server binds loopback.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from levain.dashboard import AnnealPaths, SubstrateSource
from levain.web_server import (
    build_substrate_json,
    load_web_asset,
    make_server,
    run_web_server,
)
from levain.writes import WriteScope


def _store_with_data(tmp_path: Path) -> SubstrateSource:
    """A populated substrate: a graduated association (live write-path), one open
    spore, and a two-section continuity. Mirrors the app_server fixture so the two
    surfaces are tested against the same shape. Returned as a ``SubstrateSource``
    (no install root — these temp stores aren't a full ``.levain`` install, so the
    seed/config tier is simply absent)."""
    from anneal_memory import Store
    from anneal_memory.spores import SporeStore

    db = tmp_path / "memory.db"
    with Store(db) as store:
        a = store.record("decided X because Y", "decision")
        b = store.record("noticed Z", "observation")
        store.record_associations({(a.id, b.id)})
    SporeStore(tmp_path / "memory.spores.json").add(
        type="task", text="an open loop", tier="hot", salience=2
    )
    (tmp_path / "memory.continuity.md").write_text(
        "## State\ncurrent focus line\n\n## Active Threads\n- thread one\n",
        encoding="utf-8",
    )
    return SubstrateSource(anneal=AnnealPaths.from_db(db))


@contextmanager
def _serving(source: SubstrateSource):
    """Bring up a real server on an ephemeral loopback port, yield its base URL,
    and tear it down cleanly — the live integration harness for the route tests."""
    httpd = make_server(source, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[0], httpd.server_address[1]
    try:
        yield f"http://{host}:{port}", httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 — loopback only
        return r.status, dict(r.headers), r.read()


def _request(url: str, *, method: str = "GET", headers: dict | None = None):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 — loopback only
        return r.status, dict(r.headers), r.read()


# --- the data endpoint: shape parity with the SubstrateView ----------------

class TestSubstrateJson:
    def test_build_substrate_json_shape(self, tmp_path: Path) -> None:
        data = json.loads(build_substrate_json(_store_with_data(tmp_path)))
        for key in ("paths", "health", "graph", "crystal_index", "open_spores",
                    "sections", "errors"):
            assert key in data
        assert data["health"]["total_episodes"] == 2
        assert data["health"]["write_path_live"] is True
        assert len(data["open_spores"]) == 1
        assert data["sections"][0]["heading"] == "State"

    def test_extra_panels_merge_into_layout_and_data(self, tmp_path: Path) -> None:
        # the inline extra-panel seam: a provider returns full panel dicts; build_substrate_json
        # splits each into a kind:"external" layout entry + its data under payload["extra_panels"].
        src = _store_with_data(tmp_path)
        provider = lambda: [{  # noqa: E731
            "id": "inbox", "zone": "operate", "title": "Inbox (1 unread)",
            "note": "1 unread", "lines": [{"meta": "x", "text": "hi", "accent": True}],
        }]
        data = json.loads(build_substrate_json(src, provider))
        ext = [p for p in data["layout"] if p.get("kind") == "external"]
        assert len(ext) == 1 and ext[0]["id"] == "inbox" and ext[0]["zone"] == "operate"
        assert data["extra_panels"]["inbox"]["lines"][0]["text"] == "hi"
        assert data["extra_panels"]["inbox"]["note"] == "1 unread"

    def test_extra_panels_insert_grouped_by_zone(self, tmp_path: Path) -> None:
        # an operate external panel must land WITHIN the operate run, not appended past the
        # Mind block — else the all-view re-emits a duplicate zone divider (L1 LOW-1). Lock
        # the invariant: every zone forms ONE contiguous run in the layout order.
        src = _store_with_data(tmp_path)
        provider = lambda: [{"id": "inbox", "zone": "operate", "title": "Inbox", "lines": []}]  # noqa: E731
        data = json.loads(build_substrate_json(src, provider))
        zones = [p["zone"] for p in data["layout"] if p.get("zone")]
        runs = [z for i, z in enumerate(zones) if i == 0 or zones[i - 1] != z]
        assert len(runs) == len(set(runs)), f"zones not contiguous (duplicate divider): {zones}"
        ext = next(p for p in data["layout"] if p.get("kind") == "external")
        assert ext["zone"] == "operate"

    def test_extra_panels_malformed_return_is_failsoft(self, tmp_path: Path) -> None:
        # a provider returning a NON-list (int / None / dict / generator) must degrade to no
        # panels, never raise out of the iteration into the degraded handler path [codex L3 MED].
        src = _store_with_data(tmp_path)
        for bad in (lambda: 42, lambda: None, lambda: {"id": "x"}, lambda: (x for x in [1])):  # noqa: E731
            data = json.loads(build_substrate_json(src, bad))
            assert not [p for p in data["layout"] if p.get("kind") == "external"]
            assert data["extra_panels"] == {}

    def test_extra_panels_dedupes_ids_and_coerces_unknown_zone(self, tmp_path: Path) -> None:
        provider = lambda: [  # noqa: E731
            {"id": "dup", "zone": "mind", "title": "first", "lines": [{"text": "A"}]},
            {"id": "dup", "zone": "operate", "title": "second", "lines": [{"text": "B"}]},  # dup → skip
            {"id": None, "zone": "operate", "title": "nil", "lines": []},   # None id → skip (not "None")
            {"id": "weird", "zone": "frobnicate", "title": "w", "lines": []},  # unknown zone → operate
        ]
        data = json.loads(build_substrate_json(_store_with_data(tmp_path), provider))
        ext = [p for p in data["layout"] if p.get("kind") == "external"]
        assert [p["id"] for p in ext].count("dup") == 1                     # dup id deduped [codex MED]
        assert "None" not in [p["id"] for p in ext]                         # None id skipped, not coerced [codex LOW]
        assert data["extra_panels"]["dup"]["lines"][0]["text"] == "A"       # FIRST kept, not clobbered
        assert next(p for p in ext if p["id"] == "weird")["zone"] == "operate"  # unknown zone coerced [codex LOW]

    def test_extra_panels_provider_fault_is_failsoft(self, tmp_path: Path) -> None:
        # a provider that raises must NOT break /substrate.json — it degrades to no extra panels.
        def boom():
            raise RuntimeError("provider down")

        data = json.loads(build_substrate_json(_store_with_data(tmp_path), boom))
        assert not [p for p in data["layout"] if p.get("kind") == "external"]
        assert data["extra_panels"] == {}

    def test_extra_panels_none_adds_nothing(self, tmp_path: Path) -> None:
        # the base product (no provider) carries no external panels and no extra_panels key.
        data = json.loads(build_substrate_json(_store_with_data(tmp_path)))
        assert not [p for p in data["layout"] if p.get("kind") == "external"]
        assert "extra_panels" not in data

    def test_action_verbs_exposed_for_compose_affordance(self, tmp_path: Path) -> None:
        # the external-panel-ACTION seam: when verbs are registered, build_substrate_json exposes
        # each verb's confirm_required + idempotent + label so the frontend sources the confirm
        # step + the idempotency-key requirement from the REGISTRY (the single source of truth),
        # never a panel-declared flag that could drift.
        from levain.writes import ActionVerb

        verbs = {"send_inbox": ActionVerb(handler=lambda p: {"ok": True},
                                          confirm_required=True, label="Send to inbox")}
        data = json.loads(build_substrate_json(_store_with_data(tmp_path), extra_verbs=verbs))
        assert data["action_verbs"]["send_inbox"] == {
            "confirm_required": True, "idempotent": False, "label": "Send to inbox"}

    def test_action_verbs_exposes_idempotent_flag(self, tmp_path: Path) -> None:
        # an idempotent verb advertises idempotent:true so the frontend mints + sends a client
        # idempotency_key for the at-most-once retry token.
        from levain.writes import ActionVerb

        verbs = {"send_relay": ActionVerb(handler=lambda p: {"ok": True}, confirm_required=True,
                                          idempotent=True, label="Send to relay")}
        data = json.loads(build_substrate_json(_store_with_data(tmp_path), extra_verbs=verbs))
        assert data["action_verbs"]["send_relay"]["idempotent"] is True

    def test_no_verbs_omits_action_verbs(self, tmp_path: Path) -> None:
        # a read-only serve passes no verbs (None or {}) → the key is absent → the frontend renders
        # NO compose affordance at all (NO THEATER), even if a panel still declares an `action`.
        assert "action_verbs" not in json.loads(build_substrate_json(_store_with_data(tmp_path)))
        assert "action_verbs" not in json.loads(
            build_substrate_json(_store_with_data(tmp_path), extra_verbs={}))

    def test_panel_action_folds_into_extra_panel_data(self, tmp_path: Path) -> None:
        # a panel's optional `action` (the compose spec) rides through into its extra_panels data,
        # like lines/note — the panel author's contract; the frontend gates the actual render.
        action = {"verb": "send_inbox", "fields": [{"name": "message", "kind": "textarea"}]}
        provider = lambda: [{"id": "inbox", "zone": "operate", "title": "Inbox",  # noqa: E731
                             "lines": [], "action": action}]
        data = json.loads(build_substrate_json(_store_with_data(tmp_path), provider))
        assert data["extra_panels"]["inbox"]["action"] == action

    def test_panel_without_action_carries_none(self, tmp_path: Path) -> None:
        # a panel that declares no action carries action=None — a uniform data shape; the frontend
        # treats falsy as "no affordance" (the gate is `act && commitAction && verb-registered`).
        provider = lambda: [{"id": "x", "zone": "operate", "title": "X", "lines": []}]  # noqa: E731
        data = json.loads(build_substrate_json(_store_with_data(tmp_path), provider))
        assert data["extra_panels"]["x"]["action"] is None

    def test_malformed_panel_action_dropped_to_none(self, tmp_path: Path) -> None:
        # defense-in-depth (codex L3): a malformed adopter `action` is dropped to None (the compose
        # box just doesn't render) rather than reaching the frontend — blocks the prototype-pollution
        # + bad-kind class at the source. A dunder field name is the load-bearing case.
        for bad in (
            {"verb": "v", "fields": [{"name": "__proto__", "kind": "text"}]},   # dangerous name
            {"verb": "v", "fields": [{"name": "a", "kind": "text"}, {"name": "a", "kind": "csv"}]},  # dup name
            {"verb": "v", "fields": [{"name": "a", "kind": "bogus"}]},          # unknown kind
            {"verb": "", "fields": []},                                          # empty verb
            {"fields": [{"name": "a", "kind": "text"}]},                         # no verb
            {"verb": "v", "fields": "notalist"},                                 # fields not a list
            "notadict",                                                          # not a dict
        ):
            provider = lambda b=bad: [{"id": "x", "zone": "operate", "title": "X",  # noqa: E731
                                       "lines": [], "action": b}]
            data = json.loads(build_substrate_json(_store_with_data(tmp_path), provider))
            assert data["extra_panels"]["x"]["action"] is None, bad
        # a well-formed action survives intact
        good = {"verb": "send_inbox", "fields": [{"name": "message", "kind": "textarea"},
                                                 {"name": "for", "kind": "csv"}]}
        provider = lambda: [{"id": "x", "zone": "operate", "title": "X", "lines": [], "action": good}]  # noqa: E731
        data = json.loads(build_substrate_json(_store_with_data(tmp_path), provider))
        assert data["extra_panels"]["x"]["action"] == good

    def test_multiselect_action_field_validated(self, tmp_path: Path) -> None:
        # a multiselect field must carry a non-empty options list of valid string values (or
        # {value,label} dicts) — else the action is dropped, so a malformed chooser never renders.
        for bad in (
            {"verb": "v", "fields": [{"name": "r", "kind": "multiselect"}]},                  # no options
            {"verb": "v", "fields": [{"name": "r", "kind": "multiselect", "options": []}]},    # empty options
            {"verb": "v", "fields": [{"name": "r", "kind": "multiselect", "options": [123]}]}, # non-str opt
            {"verb": "v", "fields": [{"name": "r", "kind": "multiselect",
                                      "options": [{"value": ""}]}]},                            # empty value
            {"verb": "v", "fields": [{"name": "r", "kind": "multiselect", "options": ["  "]}]},  # whitespace value
            {"verb": "v", "fields": [{"name": "r", "kind": "multiselect",
                                      "options": ["cli", "cli"]}]},                              # duplicate value
            {"verb": "v", "fields": [{"name": "r", "kind": "multiselect",
                                      "options": [{"value": "cli", "label": 5}]}]},              # non-str label
        ):
            provider = lambda b=bad: [{"id": "x", "zone": "operate", "title": "X",  # noqa: E731
                                       "lines": [], "action": b}]
            data = json.loads(build_substrate_json(_store_with_data(tmp_path), provider))
            assert data["extra_panels"]["x"]["action"] is None, bad
        # both option shapes (bare string + {value,label}) survive
        good = {"verb": "send_inbox", "fields": [
            {"name": "message", "kind": "textarea"},
            {"name": "for", "kind": "multiselect",
             "options": ["cli", {"value": "daemon", "label": "daemon"}]}]}
        provider = lambda: [{"id": "x", "zone": "operate", "title": "X", "lines": [], "action": good}]  # noqa: E731
        data = json.loads(build_substrate_json(_store_with_data(tmp_path), provider))
        assert data["extra_panels"]["x"]["action"] == good

    def test_make_server_rejects_non_callable_extra_panels(self, tmp_path: Path) -> None:
        import pytest

        from levain.web_server import make_server

        with pytest.raises(ValueError, match="extra_panels must be a zero-arg callable"):
            make_server(_store_with_data(tmp_path), port=0, extra_panels=[{"id": "x"}])

    def test_writable_flag_tracks_write_scope(self, tmp_path: Path) -> None:
        # The frontend gates every edit affordance on `writable` (NO THEATER). A
        # source with no write_scope (a read-only inspection cockpit, e.g. the flow
        # self-ops cockpit served read-only) is non-writable, matching the server's
        # POST /edit 422; a source WITH a write_scope is writable.
        src = _store_with_data(tmp_path)  # write_scope defaults to None
        assert json.loads(build_substrate_json(src))["writable"] is False
        writable = SubstrateSource(
            anneal=src.anneal, install_root=tmp_path,
            write_scope=WriteScope.from_install_root(tmp_path),
        )
        assert json.loads(build_substrate_json(writable))["writable"] is True

    def test_no_install_source_flags_readonly_and_refuses_write(self, tmp_path: Path) -> None:
        # The NO-THEATER coupling end-to-end: a source with no write_scope BOTH
        # serves writable:false AND 422s POST /edit. Both read the same
        # source.write_scope, so they can't diverge — this locks that they don't
        # (catches a future do_POST/flag predicate split with all else green).
        src = _store_with_data(tmp_path)  # write_scope defaults to None
        with _serving(src) as (base, _):
            _s, _h, body = _get(base + "/substrate.json")
            assert json.loads(body)["writable"] is False
            status, resp = _post(base + "/edit", {"kind": "entity_name", "value": "x"})
        assert status == 422
        assert resp["error"] == "read_only"

    def test_nonloopback_bind_gated_on_read_only(self, tmp_path: Path) -> None:
        # Non-loopback binding (a Tailscale/LAN IP) is allowed for a READ-ONLY source
        # (no write surface to expose; the private mesh is the boundary) but REFUSED
        # for a writable one (localhost-sovereign write auth assumes loopback).
        # 192.0.2.1 = TEST-NET-1 (RFC 5737): non-loopback, never a real interface.
        import pytest

        ro = _store_with_data(tmp_path)  # write_scope=None → read-only
        writable = SubstrateSource(
            anneal=ro.anneal, install_root=tmp_path,
            write_scope=WriteScope.from_install_root(tmp_path),
        )
        with pytest.raises(ValueError, match="loopback"):
            make_server(writable, host="192.0.2.1", port=0)  # guard fires BEFORE bind
        # the read-only source clears the guard, then fails to BIND the unroutable
        # address (OSError, not ValueError) — proving the guard was bypassed for it.
        with pytest.raises(OSError):
            make_server(ro, host="192.0.2.1", port=0)

    def test_nonloopback_refused_for_install_bearing_readonly(self, tmp_path: Path) -> None:
        # [codex L3 MED] An install-bearing but READ-ONLY source (install_root set,
        # write_scope None) still holds seed/config = operator-PRIVATE data, so it must
        # stay loopback-only too — matching the pre-WriteScope boundary that gated on
        # install presence. Only a no-install no-write source may bind the mesh.
        import pytest

        src = _store_with_data(tmp_path)
        install_ro = SubstrateSource(anneal=src.anneal, install_root=tmp_path)  # write_scope=None
        with pytest.raises(ValueError, match="loopback"):
            make_server(install_ro, host="192.0.2.1", port=0)

    def test_wildcard_and_public_bind_refused_for_any_source(self, tmp_path: Path) -> None:
        # A wildcard / public bind is refused for ANY source (read-only included) —
        # exposure is ONE specific private/mesh interface, never every interface or
        # the internet (L1/L2 HIGH). Includes the libc-vs-ipaddress parser-differential
        # bypasses (L3): legacy-numeric IPv4 (0 / 134744072 / 0x.. / octal) and
        # IPv4-mapped IPv6 (::ffff:x) that bind dangerously but ipaddress mis-classifies,
        # plus a hostname — all must raise ValueError, never reach a bind.
        import pytest

        ro = _store_with_data(tmp_path)  # install_root=None → read-only
        bad = (
            "0.0.0.0", "::", "8.8.8.8",               # canonical wildcard / public
            "::ffff:0.0.0.0", "::ffff:8.8.8.8",       # IPv4-mapped (complement L3)
            "0", "000.000.000.000", "134744072", "0x08080808", "010.010.010.010",  # legacy (codex L3)
            "example.com",                            # hostname
        )
        for h in bad:
            with pytest.raises(ValueError, match="refusing to bind"):
                make_server(ro, host=h, port=0)

    def test_endpoint_serves_the_view(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _):
            status, headers, body = _get(base + "/substrate.json")
        assert status == 200
        assert headers["Content-Type"] == "application/json; charset=utf-8"
        data = json.loads(body)
        assert data["health"]["total_episodes"] == 2
        assert len(data["open_spores"]) == 1

    def test_endpoint_ignores_query_string(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _):
            status, _h, body = _get(base + "/substrate.json?cachebust=1")
        assert status == 200
        assert json.loads(body)["health"]["total_episodes"] == 2

    def test_missing_store_degrades_not_crashes(self, tmp_path: Path) -> None:
        """A bare source with no db yields a renderable, degraded view — the
        endpoint must 200 with an errors entry, never 500 / dead-connection."""
        source = SubstrateSource(anneal=AnnealPaths.from_db(tmp_path / "nope" / "memory.db"))
        with _serving(source) as (base, _):
            status, _h, body = _get(base + "/substrate.json")
        assert status == 200
        assert "store" in json.loads(body)["errors"]

    def test_handle_error_swallows_client_disconnects(self, tmp_path: Path, capsys) -> None:
        """A kept-alive client RESET (HTTP/1.1 + a browser connection pool on a mesh
        bind — spore-178) raises ConnectionResetError up through socketserver; the
        ``_LevainHTTPServer.handle_error`` override swallows that family silently, while
        a genuine error still delegates to the base so real bugs surface."""
        httpd = make_server(_store_with_data(tmp_path), host="127.0.0.1", port=0)
        try:
            try:
                raise ConnectionResetError(54, "Connection reset by peer")
            except ConnectionResetError:
                httpd.handle_error(object(), ("100.71.105.99", 58063))
            assert capsys.readouterr().err == ""  # swallowed — no traceback flood
            try:
                raise ValueError("a real server bug")
            except ValueError:
                httpd.handle_error(object(), ("127.0.0.1", 1234))
            assert "ValueError" in capsys.readouterr().err  # genuine error surfaces
        finally:
            httpd.server_close()


# --- the static assets ------------------------------------------------------

class TestAssets:
    def test_load_web_asset_reads_each(self) -> None:
        html = load_web_asset("dashboard.html")
        core = load_web_asset("dashboard_core.js")
        boot = load_web_asset("dashboard_boot.js")
        css = load_web_asset("dashboard.css")
        assert "<!DOCTYPE html>" in html
        # the page wires both served scripts + the served stylesheet, NO external/CDN
        # import and NO inline <style> (Slice 2a moved CSS out → style-src 'self').
        assert '/dashboard_core.js' in html
        assert '/dashboard_boot.js' in html
        assert '/dashboard.css' in html
        assert "<style>" not in html
        assert ".panel" in css and ".edit-btn" in css  # the moved sheet + the 2a affordances
        assert "unpkg.com" not in html and "modelcontextprotocol" not in html
        # the core exposes the one render entry point the boot shim calls
        assert "window.LevainDashboard" in core
        # the boot shim is THIS surface's transport: fetch from the localhost JSON
        # endpoint, NOT the MCP-Apps `new App(...)` / ontoolresult lifecycle
        assert "/substrate.json" in boot
        assert "fetch(" in boot
        assert "new App(" not in boot and "app.connect(" not in boot

    def test_frontend_is_textcontent_only(self) -> None:
        """Structural invariant (`structural_invariants_beat_discipline`): the render
        core paints store data with textContent ONLY — never innerHTML — so a hostile
        store value paints as inert text, never markup. The markdown renderer
        (renderMarkdown, the highest-risk addition) lives under this same contract.
        A regression that reaches for innerHTML/outerHTML/insertAdjacentHTML/
        document.write in CODE (not a comment, not a string literal) fails the suite,
        forever."""
        import re

        # Blank out /* … */ block comments (whole-file, newlines preserved so line numbers
        # stay accurate) and string-literal CONTENTS (so a `//` inside a "http://…" URL is
        # not mistaken for a comment start), THEN cut at the first real `//` line comment,
        # THEN assert no banned token survives in the remaining code. [L1 + complement L3 — tighten]
        block_re = re.compile(r"/\*.*?\*/", re.S)
        str_re = re.compile(r'"(?:[^"\\]|\\.)*"' r"|'(?:[^'\\]|\\.)*'" r"|`(?:[^`\\]|\\.)*`")
        banned = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")
        for name in ("dashboard_core.js", "dashboard_boot.js"):
            js = load_web_asset(name)
            js = block_re.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), js)
            for lineno, line in enumerate(js.splitlines(), 1):
                code = str_re.sub('""', line)
                ci = code.find("//")
                if ci >= 0:
                    code = code[:ci]
                for tok in banned:
                    assert tok not in code, (
                        f"{name}:{lineno} uses {tok} in code (not a comment/string) — "
                        "breaks the textContent-only render contract"
                    )

    def test_external_panel_action_seam_wired(self) -> None:
        """The external-panel-ACTION seam (the compose affordance) is wired in BOTH layers:
        the boot transport (commitAction → POST /action, the write-peer of commit → /edit) and
        the render core (buildActionBox + the renderExternal gate + the commitAction injection).
        Structural guard that the seam exists end-to-end; the textContent-only contract above
        already covers the new DOM for safety."""
        core = load_web_asset("dashboard_core.js")
        boot = load_web_asset("dashboard_boot.js")
        # boot: a dedicated /action transport, the peer of commit's /edit, injected into render
        assert "function commitAction" in boot and '"/action"' in boot
        assert "{ commit, commitAction }" in boot
        # core: the compose box + its gate, the pure coercion, and commitAction stored from opts
        for token in ("function buildActionBox", "function collectActionParams",
                      "ACTION-EXTRACT-START", "opts.commitAction",
                      "view.action_verbs", "spec.confirm_required"):
            assert token in core, f"dashboard_core.js missing {token!r} (action seam)"

    def test_action_params_coercion_behavioral(self, tmp_path: Path) -> None:
        """Behavioral oracle (not source-presence) for the compose-affordance field→params
        coercion (collectActionParams) — the bug-class L1+L2 flagged (a divergent/stale params
        send) lives in this logic, so drive the REAL extracted function through node: csv →
        trimmed non-empty list or omitted; text → trimmed or omitted; required-empty → {error};
        and the params equal EXACTLY the given field values (the confirm-step freeze guarantees
        given == displayed → WYSIWYG). Skips if node is unavailable (the structural seam test is
        the in-source backstop)."""
        import shutil
        import subprocess
        from importlib.resources import files

        node = shutil.which("node")
        if node is None:
            import pytest

            pytest.skip("node not available — JS behavioral oracle skipped")

        asset = str(files("levain") / "templates" / "web" / "dashboard_core.js")
        driver = tmp_path / "action_params_check.js"
        driver.write_text(
            r"""
const fs = require("fs");
const text = fs.readFileSync(process.argv[2], "utf8");
const a = text.indexOf("ACTION-EXTRACT-START"), b = text.indexOf("ACTION-EXTRACT-END");
const block = text.slice(text.lastIndexOf("\n", a) + 1, text.indexOf("\n", b) + 1);
const { collectActionParams } = new Function(block + "\nreturn { collectActionParams };")();
const eq = (got, want, label) => {
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    console.error("FAIL " + label + ": got " + JSON.stringify(got) + " want " + JSON.stringify(want));
    process.exit(1);
  }
};
// csv splits + trims + drops empties → a real list, never a coerced string
eq(collectActionParams([{name:"for",kind:"csv",required:false,value:" chip , cli ,"}]),
   {params:{for:["chip","cli"]}}, "csv split/trim");
// csv all-blank → key OMITTED (broadcast), not [] and not ""
eq(collectActionParams([{name:"for",kind:"csv",required:false,value:" , "}]),
   {params:{}}, "csv blank omitted");
// required csv empty → error (never a silent broadcast)
eq(collectActionParams([{name:"for",kind:"csv",required:true,label:"Readers",value:""}]),
   {error:"Readers is required"}, "csv required empty");
// text trims; optional empty omitted; required empty → error
eq(collectActionParams([{name:"message",kind:"textarea",required:true,value:"  hi  "}]),
   {params:{message:"hi"}}, "text trim");
eq(collectActionParams([{name:"x",kind:"text",required:false,value:"   "}]),
   {params:{}}, "text optional empty omitted");
eq(collectActionParams([{name:"message",kind:"textarea",required:true,label:"Message",value:""}]),
   {error:"Message is required"}, "text required empty");
// multi-field: message + for together, EXACTLY the given values (params == input → WYSIWYG)
eq(collectActionParams([{name:"message",kind:"textarea",required:true,value:"deploy"},
                        {name:"for",kind:"csv",required:false,value:"chip"}]),
   {params:{message:"deploy",for:["chip"]}}, "multi-field");
// null-proto params (codex L3): a field named __proto__ can't pollute the prototype + silently
// drop from JSON — the params object's prototype stays null.
const rp = collectActionParams([{name:"__proto__",kind:"csv",required:false,value:"x"}]);
if (Object.getPrototypeOf(rp.params) !== null) { console.error("FAIL: params prototype not null"); process.exit(1); }
// duplicate field names → error, never a silent overwrite
eq(collectActionParams([{name:"a",kind:"text",required:false,value:"1"},
                        {name:"a",kind:"text",required:false,value:"2"}]),
   {error:"duplicate field a"}, "duplicate name");
// multiselect: value is the array of checked options → a real list, omitted when none checked
eq(collectActionParams([{name:"for",kind:"multiselect",required:false,value:["cli","daemon"]}]),
   {params:{for:["cli","daemon"]}}, "multiselect list");
eq(collectActionParams([{name:"for",kind:"multiselect",required:false,value:[]}]),
   {params:{}}, "multiselect none omitted (broadcast)");
eq(collectActionParams([{name:"r",kind:"multiselect",required:true,label:"Readers",value:[]}]),
   {error:"Readers is required"}, "multiselect required empty");
// fails closed on a non-array value (a read() regression must error, not silently broadcast)
eq(collectActionParams([{name:"for",kind:"multiselect",required:false,value:"cli"}]),
   {error:"for is invalid"}, "multiselect non-array fails closed");
// trims, drops blanks, dedupes
eq(collectActionParams([{name:"for",kind:"multiselect",required:false,value:[" cli ","cli","  ","daemon"]}]),
   {params:{for:["cli","daemon"]}}, "multiselect trim/dedupe/drop-blank");
console.log("OK");
""",
            encoding="utf-8",
        )
        result = subprocess.run([node, str(driver), asset], capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_clause_rides_full_with_per_item_expand(self) -> None:
        """DATA-SAFETY + UX (the truncation fix): row text rides FULL (the server no longer
        caps it) and the surface offers a per-item expand so a long clause is readable AND
        the editor reads the whole thing. Structural guard that the mechanism is wired in
        BOTH the JS (the shared appendClause/measureClauses + the .row-expanded state + the
        editor seeding from the full ``s.text``) and the CSS (line-clamp for density with a
        .row-expanded escape)."""
        core = load_web_asset("dashboard_core.js")
        css = load_web_asset("dashboard.css")
        for token in ("function appendClause", "function measureClauses",
                      "row-expanded", "clause-toggle", "clause clampable"):
            assert token in core, f"dashboard_core.js missing {token!r} (per-item expand)"
        # park = pin an OPEN loop to Keep via a tier change (mirror of reactivate) — NOT a
        # compost/descend kind (park is pause-not-resolve) [Phill 2026-06-19].
        assert '"park"' in core and 'tier: "parked"' in core, "Open-Loops 'park' verb missing"
        # Open-Loops client-side filter (text OR spore-id), persisted across re-renders
        assert "openLoopsQuery" in core, "Open-Loops filter (openLoopsQuery) missing"
        assert "ta.value = s.text" in core, "openTextEdit must seed the editor from the full s.text"
        assert "-webkit-line-clamp" in css, "dashboard.css missing the clause line-clamp"
        # the clamp is SCOPED to .clampable (only toggle-backed clauses) so a bare .clause
        # (crystal/wrap label) never clamps without an expand affordance [codex L3 MED]
        assert ".clause.clampable" in css, "dashboard.css clamp must be scoped to .clause.clampable"

    def test_markdown_renderer_link_scheme_gated(self) -> None:
        """The markdown prose renderer is wired into the section/config display and
        gates link hrefs through an explicit scheme allowlist (mdSafeHref). Source-
        level guard that the allowlist exists, names exactly the safe schemes, and is
        the only path to a link href."""
        core = load_web_asset("dashboard_core.js")
        assert "function renderMarkdown" in core
        assert "function sectionDisplay" in core
        assert "sectionDisplay(" in core  # wired into the section/config panels
        assert "function mdSafeHref" in core
        for safe in ('"http"', '"https"', '"mailto"'):
            assert safe in core, f"mdSafeHref allowlist missing {safe}"

    def test_markdown_block_parser_does_not_spin(self, tmp_path: Path) -> None:
        """L1 HIGH regression (behavioral, not source-presence): a fence-marker line
        that no fence-opener accepts (``mdIsBlockStart`` flags it, but the fence regex
        rejects an info string containing a backtick/tilde) used to leave ``i`` unmoved
        → the block loop spun forever, freezing the tab. Drive the REAL extracted
        renderMarkdown through node on those vectors with a hard timeout: a spin =
        ``TimeoutExpired`` = test failure. Skips if node is unavailable (the behavioral
        oracle needs a JS engine; the structural backstop in-source is the guarantee)."""
        import shutil
        import subprocess
        from importlib.resources import files

        node = shutil.which("node")
        if node is None:
            import pytest

            pytest.skip("node not available — JS behavioral oracle skipped")

        asset = str(files("levain") / "templates" / "web" / "dashboard_core.js")
        driver = tmp_path / "spin_check.js"
        driver.write_text(
            r"""
const fs = require("fs");
const text = fs.readFileSync(process.argv[2], "utf8");
const a = text.indexOf("MD-EXTRACT-START"), b = text.indexOf("MD-EXTRACT-END");
const block = text.slice(text.lastIndexOf("\n", a) + 1, text.indexOf("\n", b) + 1);
function mkEl(t){return {tagName:t,nodeType:1,className:"",_text:null,attrs:{},children:[],
  set textContent(v){this._text=String(v);this.children=[];},get textContent(){return this._text;},
  appendChild(c){this.children.push(c);return c;},setAttribute(k,v){this.attrs[k]=String(v);}};}
const document={createElement:t=>mkEl(t),createTextNode:t=>({nodeType:3,_text:String(t)}),
  createDocumentFragment:()=>({nodeType:11,tagName:null,children:[],appendChild(c){this.children.push(c);return c;}})};
const el=(t,c,x)=>{const n=document.createElement(t);if(c)n.className=c;if(x!=null)n.textContent=String(x);return n;};
const mod=new Function("document","el",block+"\nreturn {renderMarkdown};")(document,el);
for (const v of ["```~foo","```js`x","~~~`","alpha\n```~x\nbeta","# ok\n```~\nmore"]) { mod.renderMarkdown(v); }
console.log("OK");
""",
            encoding="utf-8",
        )
        # timeout kills a spin; the fix renders the stuck line as text and returns fast.
        result = subprocess.run(
            [node, str(driver), asset],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_index_route(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _):
            status, headers, body = _get(base + "/")
        assert status == 200
        assert headers["Content-Type"] == "text/html; charset=utf-8"
        # the page identifies as Levain AND serves the Identity·Operate·Mind tab bar
        assert b"Levain" in body
        assert b'class="tabs"' in body and b'data-zone="mind"' in body

    def test_script_routes(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _):
            for route in ("/dashboard_core.js", "/dashboard_boot.js"):
                status, headers, body = _get(base + route)
                assert status == 200, route
                assert headers["Content-Type"] == "text/javascript; charset=utf-8"
                assert body, route

    def test_stylesheet_route(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _):
            status, headers, body = _get(base + "/dashboard.css")
        assert status == 200
        assert headers["Content-Type"] == "text/css; charset=utf-8"
        assert b".panel" in body


# --- security boundary: closed allowlist, headers, loopback ----------------

class TestSecurityBoundary:
    def test_unknown_route_404(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _):
            try:
                _get(base + "/nope")
                raised = None
            except urllib.error.HTTPError as e:
                raised = e
        assert raised is not None and raised.code == 404

    def test_no_path_traversal(self, tmp_path: Path) -> None:
        """No path → filesystem mapping exists, so a traversal attempt is just an
        unknown route → 404 (never reads a file off disk)."""
        with _serving(_store_with_data(tmp_path)) as (base, _):
            for evil in ("/../../etc/passwd", "/../web_server.py", "/levain/cli.py"):
                try:
                    _get(base + evil)
                    code = 200
                except urllib.error.HTTPError as e:
                    code = e.code
                assert code == 404, evil

    def test_security_headers_present(self, tmp_path: Path) -> None:
        # the data-bearing route must carry the headers too, not just `/`
        with _serving(_store_with_data(tmp_path)) as (base, _):
            for route in ("/", "/substrate.json"):
                _s, headers, _b = _get(base + route)
                csp = headers["Content-Security-Policy"]
                assert "default-src 'none'" in csp, route
                assert "script-src 'self'" in csp, route
                # Slice 2a: CSS is a served sheet now → style-src 'self', no inline.
                assert "style-src 'self'" in csp, route
                assert "unsafe-inline" not in csp, route
                assert "frame-ancestors 'none'" in csp, route
                assert headers["X-Content-Type-Options"] == "nosniff", route
                assert headers["X-Frame-Options"] == "DENY", route
                assert headers["Cache-Control"] == "no-store", route

    def test_binds_loopback(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (_base, httpd):
            assert httpd.server_address[0] == "127.0.0.1"

    def test_rejects_non_loopback_host_header(self, tmp_path: Path) -> None:
        """DNS-rebinding guard: a request whose Host isn't a loopback name (the
        shape a rebinding attacker's page sends) is refused 403 — the substrate is
        never disclosed cross-origin."""
        with _serving(_store_with_data(tmp_path)) as (base, _):
            try:
                _request(base + "/substrate.json", headers={"Host": "evil.com"})
                code = 200
            except urllib.error.HTTPError as e:
                code = e.code
        assert code == 403

    def test_localhost_host_header_allowed(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, httpd):
            port = httpd.server_address[1]
            status, _h, body = _request(
                base + "/substrate.json", headers={"Host": f"localhost:{port}"}
            )
        assert status == 200
        assert json.loads(body)["health"]["total_episodes"] == 2

    def test_host_check_is_case_and_dot_insensitive(self, tmp_path: Path) -> None:
        """RFC 7230: host comparison is case-insensitive, and a trailing FQDN dot
        is equivalent — neither should false-reject a legitimate loopback Host."""
        with _serving(_store_with_data(tmp_path)) as (base, httpd):
            port = httpd.server_address[1]
            for h in (f"LOCALHOST:{port}", f"localhost.:{port}", f"  localhost:{port}"):
                status, _hd, _b = _request(base + "/", headers={"Host": h})
                assert status == 200, h

    def test_malformed_bracket_host_rejected(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _):
            try:
                _request(base + "/", headers={"Host": "[::1]evil"})
                code = 200
            except urllib.error.HTTPError as e:
                code = e.code
        assert code == 403

    def test_cross_site_request_rejected(self, tmp_path: Path) -> None:
        """A hostile cross-site page's fetch (Sec-Fetch-Site: cross-site) is refused
        before it can even trigger the per-request store read."""
        with _serving(_store_with_data(tmp_path)) as (base, _):
            try:
                _request(
                    base + "/substrate.json",
                    headers={"Sec-Fetch-Site": "cross-site"},
                )
                code = 200
            except urllib.error.HTTPError as e:
                code = e.code
        assert code == 403

    def test_same_origin_fetch_allowed(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _):
            status, _h, _b = _request(
                base + "/substrate.json", headers={"Sec-Fetch-Site": "same-origin"}
            )
        assert status == 200

    def test_server_header_hides_python_version(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _):
            _s, headers, _b = _get(base + "/")
        assert headers["Server"] == "levain-serve"
        assert "Python/" not in headers["Server"]


# --- HTTP wire contract: framing + HEAD ------------------------------------

class TestHttpContract:
    def test_content_length_matches_body(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _):
            for route in ("/", "/dashboard_core.js", "/substrate.json"):
                _s, headers, body = _get(base + route)
                assert int(headers["Content-Length"]) == len(body), route

    def test_head_sends_headers_and_framing_no_body(self, tmp_path: Path) -> None:
        """HEAD must report the Content-Length the GET WOULD return, with no body —
        or an HTTP/1.1 keep-alive connection desyncs."""
        with _serving(_store_with_data(tmp_path)) as (base, _):
            _gs, ghead, gbody = _get(base + "/substrate.json")
            hs, hhead, hbody = _request(base + "/substrate.json", method="HEAD")
        assert hs == 200
        assert hbody == b""  # no body on HEAD
        # but the framing matches what the GET body would have been
        assert hhead["Content-Length"] == ghead["Content-Length"]
        assert int(hhead["Content-Length"]) == len(gbody)
        assert hhead["Content-Type"] == "application/json; charset=utf-8"

    def test_head_on_unknown_route_404(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _):
            try:
                _request(base + "/nope", method="HEAD")
                code = 200
            except urllib.error.HTTPError as e:
                code = e.code
        assert code == 404

    def test_head_on_asset_matches_get_framing(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _):
            _gs, gh, gbody = _get(base + "/dashboard_core.js")
            hs, hh, hbody = _request(base + "/dashboard_core.js", method="HEAD")
        assert hs == 200 and hbody == b""
        assert hh["Content-Length"] == gh["Content-Length"]
        assert int(hh["Content-Length"]) == len(gbody)


# --- the loopback-bind boundary (unauthenticated read surface) -------------

class TestLoopbackBindBoundary:
    def test_make_server_refuses_non_loopback(self, tmp_path: Path) -> None:
        import pytest

        from levain.web_server import make_server

        # A WRITABLE source (write_scope set) refuses non-loopback binds — its
        # no-token localhost-sovereign write auth assumes loopback. (A READ-ONLY
        # source MAY bind non-loopback — TestSubstrateJson covers that half.)
        src = _store_with_data(tmp_path)
        writable = SubstrateSource(
            anneal=src.anneal, install_root=tmp_path,
            write_scope=WriteScope.from_install_root(tmp_path),
        )
        # non-wildcard, non-public, non-loopback → hits the writable loopback-only
        # guard (wildcard/public are refused for any source — separate test).
        for bad in ("192.168.1.5", "10.0.0.1", "::1"):  # ::1 = IPv6, Slice-2+ nicety
            with pytest.raises(ValueError, match="loopback"):
                make_server(writable, host=bad)

    def test_make_server_allows_loopback(self, tmp_path: Path) -> None:
        from levain.web_server import make_server

        for ok in ("127.0.0.1", "localhost"):  # both reliably bind everywhere
            httpd = make_server(_store_with_data(tmp_path), host=ok, port=0)
            try:
                assert httpd.server_address[0].startswith("127.")
            finally:
                httpd.server_close()

    def test_is_loopback_host_predicate(self) -> None:
        from levain.web_server import _is_loopback_host

        assert _is_loopback_host("127.0.0.1")
        assert _is_loopback_host("127.0.0.2")  # all of 127.0.0.0/8 is loopback
        assert _is_loopback_host("localhost")
        assert not _is_loopback_host("0.0.0.0")
        assert not _is_loopback_host("192.168.1.5")
        assert not _is_loopback_host("::1")  # IPv6 bind deferred to Slice 2
        assert not _is_loopback_host("evil.com")

    def test_run_web_server_refuses_non_loopback(self, tmp_path: Path, capsys) -> None:
        from anneal_memory import Store

        levain_dir = tmp_path / ".levain"
        levain_dir.mkdir()
        with Store(levain_dir / "memory.db"):
            pass
        rc = run_web_server(tmp_path, host="192.168.1.5", open_browser=False)
        assert rc == 1
        assert "loopback-only" in capsys.readouterr().err


# --- the handler's own fault path (untested defensive code) ----------------

class TestHandlerFaultPath:
    def test_unexpected_fault_degrades_to_error_json(self, tmp_path: Path, monkeypatch) -> None:
        """If building the snapshot raises an UNEXPECTED fault (past the data-layer
        degradation), the handler must still 200 with an errors.server body — never
        a 500 / dead connection."""
        import levain.web_server as ws

        def boom(_paths, _extra_panels=None, *, write_token_required=False, extra_verbs=None):
            # mirror build_substrate_json's real signature (gained extra_panels, then the
            # keyword-only write_token_required for spore-129, then extra_verbs for the
            # action-seam) so the stub is CALLED, not TypeError'd on an unexpected kwarg —
            # the fault under test is the RuntimeError.
            raise RuntimeError("kaboom")

        monkeypatch.setattr(ws, "build_substrate_json", boom)
        with _serving(_store_with_data(tmp_path)) as (base, _):
            status, headers, body = _get(base + "/substrate.json")
        assert status == 200
        assert headers["Content-Type"] == "application/json; charset=utf-8"
        data = json.loads(body)
        assert "kaboom" in data["errors"]["server"]


# --- the read-only invariant ------------------------------------------------

class TestReadOnly:
    def test_requests_do_not_mutate_store(self, tmp_path: Path) -> None:
        source = _store_with_data(tmp_path)
        db = source.anneal.episodic_db
        before = db.stat().st_mtime_ns
        with _serving(source) as (base, _):
            _get(base + "/substrate.json")
            _get(base + "/substrate.json")
            _get(base + "/")
        assert db.stat().st_mtime_ns == before


# --- the startup contract (run_web_server / CLI) ---------------------------

class TestStartupContract:
    def test_missing_store_returns_1(self, tmp_path: Path, capsys) -> None:
        # no .levain/memory.db under tmp_path → returns before binding
        rc = run_web_server(tmp_path, open_browser=False)
        assert rc == 1
        assert "No anneal store" in capsys.readouterr().err

    def test_via_cli_main_missing_store(self, tmp_path: Path, capsys) -> None:
        from levain.cli import main

        rc = main(["serve", "--path", str(tmp_path), "--no-open"])
        assert rc == 1
        assert "No anneal store" in capsys.readouterr().err

    def test_port_in_use_returns_1(self, tmp_path: Path, capsys) -> None:
        """Store present but the chosen port is taken → rc=1 with a clear hint,
        not a traceback."""
        from anneal_memory import Store

        levain_dir = tmp_path / ".levain"
        levain_dir.mkdir()
        with Store(levain_dir / "memory.db"):
            pass

        # occupy a port, then ask the server to bind the same one
        blocker = make_server(
            SubstrateSource(anneal=AnnealPaths.from_db(levain_dir / "memory.db")), port=0
        )
        busy_port = blocker.server_address[1]
        try:
            rc = run_web_server(
                tmp_path, host="127.0.0.1", port=busy_port, open_browser=False
            )
        finally:
            blocker.server_close()
        assert rc == 1
        err = capsys.readouterr().err
        assert "Could not bind" in err and "--port" in err


class TestServeWriteFlag:
    """`levain serve` is READ-ONLY by default; --write opts into the governed writable
    cockpit. The command was historically documented read-only while its resolved source
    (`SubstrateSource.local`) carries a write_scope for `levain tui` — so serve inherited
    writability silently. These lock the now-EXPLICIT posture: read-only by default, the
    governed write surface only on --write."""

    def _real_install(self, tmp_path: Path) -> Path:
        from anneal_memory import Store

        levain_dir = tmp_path / ".levain"
        levain_dir.mkdir()
        with Store(levain_dir / "memory.db"):
            pass
        return tmp_path

    def _source_served(self, tmp_path: Path, monkeypatch, *, write: bool):
        # Capture the source run_web_server hands to make_server, then bail before
        # serve_forever (raise OSError → the clean rc=1 bind-failure path).
        import levain.web_server as ws

        captured: dict = {}

        def fake_make_server(source, **kw):
            captured["source"] = source
            raise OSError("captured — bail before serve")

        monkeypatch.setattr(ws, "make_server", fake_make_server)
        rc = run_web_server(self._real_install(tmp_path), open_browser=False, write=write)
        assert rc == 1  # we never meant to serve; the OSError path returns 1
        return captured["source"]

    def test_readonly_by_default(self, tmp_path: Path, monkeypatch) -> None:
        src = self._source_served(tmp_path, monkeypatch, write=False)
        assert src.write_scope is None  # READ-ONLY: no governed write surface → POST /edit 422s
        assert src.install_root is not None  # install_root KEPT → seed/config still render

    def test_write_flag_keeps_write_scope(self, tmp_path: Path, monkeypatch) -> None:
        src = self._source_served(tmp_path, monkeypatch, write=True)
        assert src.write_scope is not None  # --write: the governed writable cockpit

    def test_cli_serve_threads_write(self, tmp_path: Path, monkeypatch) -> None:
        import levain.web_server as ws
        from levain.cli import main

        seen: dict = {}
        monkeypatch.setattr(ws, "run_web_server", lambda **kw: seen.update(kw) or 0)
        main(["serve", "--path", str(tmp_path), "--no-open"])
        assert seen["write"] is False  # default → read-only
        main(["serve", "--path", str(tmp_path), "--no-open", "--write"])
        assert seen["write"] is True  # --write opts in


# --- Slice 2a: the governed write boundary (POST /edit) ---------------------

_W_WORLD = (
    "# Who Your Operator Is\n\n> Seed material — operator template.\n\n"
    "## Identity\n\nPhill. 46. Columbus, OH.\n\n"
    "## Communication\n\nDirect, profanity welcome.\n"
)
_W_ORIGIN = "# Who You Are — Aria\n\nA new entity.\n"
_W_CONST = "# Constitution\n\nUniversal core.\n"


def _make_full_install(tmp_path: Path) -> SubstrateSource:
    """A full Levain install (seed + activation + .levain) so the write route has a
    real install_root + Class-A files to edit. No anneal store is needed for
    Class-A FILE edits (make_server doesn't require the db to exist)."""
    root = tmp_path / "install"
    (root / "seed").mkdir(parents=True)
    (root / "activation").mkdir(parents=True)
    (root / ".levain").mkdir()
    (root / "seed" / "world.md").write_text(_W_WORLD, encoding="utf-8")
    (root / "seed" / "origin.md").write_text(_W_ORIGIN, encoding="utf-8")
    (root / "seed" / "partnership.md").write_text(_W_CONST, encoding="utf-8")
    (root / "seed" / "memory.md").write_text(_W_CONST, encoding="utf-8")
    (root / "seed" / "spore_instructions.md").write_text(_W_CONST, encoding="utf-8")
    (root / "activation" / "posture.md").write_text("Slow is fast.\n", encoding="utf-8")
    (root / "activation" / "recency_directives.md").write_text("No gatekeeping.\n", encoding="utf-8")
    return SubstrateSource.local(root)


def _post(url: str, payload, *, headers: dict | None = None, content_type: str = "application/json"):
    data = json.dumps(payload).encode("utf-8")
    h = {} if content_type is None else {"Content-Type": content_type}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, method="POST", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 — loopback only
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class TestWriteBoundary:
    def test_world_section_edit_happy_path(self, tmp_path: Path) -> None:
        src = _make_full_install(tmp_path)
        with _serving(src) as (base, _httpd):
            status, body = _post(base + "/edit", {
                "kind": "config", "source": "seed/world.md", "heading": "Identity",
                "expected_body": "Phill. 46. Columbus, OH.", "new_body": "Topological mind.",
            })
        assert status == 200 and body["ok"] is True
        out = (src.install_root / "seed" / "world.md").read_text(encoding="utf-8")
        assert "Topological mind." in out
        assert "Direct, profanity welcome." in out  # sibling preserved

    def test_entity_name_edit(self, tmp_path: Path) -> None:
        src = _make_full_install(tmp_path)
        with _serving(src) as (base, _httpd):
            status, body = _post(base + "/edit", {"kind": "entity_name", "value": "Sol"})
        assert status == 200
        cfg = json.loads((src.install_root / ".levain" / "config.json").read_text("utf-8"))
        assert cfg["entity_name"] == "Sol"

    def test_state_section_edit_end_to_end(self, tmp_path: Path) -> None:
        # Slice 2b: the neocortex State section edits through the same governed route;
        # the felt layer is preserved, and a non-State section is refused 403.
        src = _make_full_install(tmp_path)
        cont = src.install_root / ".levain" / "memory.continuity.md"
        cont.write_text(
            "# Memory\n\n## State\n\nFocus: A.\n\n## Patterns\n\nfelt layer.\n",
            encoding="utf-8",
        )
        with _serving(src) as (base, _httpd):
            ok_status, ok_body = _post(base + "/edit", {
                "kind": "state", "heading": "State",
                "expected_body": "Focus: A.", "new_body": "Focus: B.",
            })
            bad_status, bad_body = _post(base + "/edit", {
                "kind": "state", "heading": "Patterns",
                "expected_body": "felt layer.", "new_body": "hacked",
            })
        assert ok_status == 200 and ok_body["ok"] is True
        out = cont.read_text(encoding="utf-8")
        assert "Focus: B." in out and "felt layer." in out  # State changed, felt kept
        assert bad_status == 403 and bad_body["error"] == "not_editable"

    def test_cross_site_refused(self, tmp_path: Path) -> None:
        src = _make_full_install(tmp_path)
        with _serving(src) as (base, _httpd):
            status, body = _post(base + "/edit", {
                "kind": "config", "source": "seed/world.md", "heading": "Identity",
                "expected_body": "Phill. 46. Columbus, OH.", "new_body": "x",
            }, headers={"Sec-Fetch-Site": "cross-site"})
        assert status == 403 and body["error"] == "forbidden"
        # the file is untouched
        assert "Phill. 46." in (src.install_root / "seed" / "world.md").read_text("utf-8")

    def test_same_site_refused(self, tmp_path: Path) -> None:
        src = _make_full_install(tmp_path)
        with _serving(src) as (base, _httpd):
            status, _ = _post(base + "/edit", {"kind": "entity_name", "value": "x"},
                              headers={"Sec-Fetch-Site": "same-site"})
        assert status == 403

    def test_same_origin_allowed(self, tmp_path: Path) -> None:
        src = _make_full_install(tmp_path)
        with _serving(src) as (base, _httpd):
            status, _ = _post(base + "/edit", {"kind": "entity_name", "value": "Ok"},
                              headers={"Sec-Fetch-Site": "same-origin"})
        assert status == 200

    def test_wrong_content_type_415(self, tmp_path: Path) -> None:
        src = _make_full_install(tmp_path)
        with _serving(src) as (base, _httpd):
            status, body = _post(base + "/edit", {"kind": "entity_name", "value": "x"},
                                 content_type="text/plain")
        assert status == 415 and body["error"] == "unsupported_media_type"

    def test_class_c_origin_refused(self, tmp_path: Path) -> None:
        src = _make_full_install(tmp_path)
        with _serving(src) as (base, _httpd):
            status, body = _post(base + "/edit", {
                "kind": "config", "source": "seed/origin.md", "heading": None,
                "expected_body": _W_ORIGIN, "new_body": "hacked",
            })
        assert status == 403 and body["error"] == "not_editable"
        assert (src.install_root / "seed" / "origin.md").read_text("utf-8") == _W_ORIGIN

    def test_stale_409(self, tmp_path: Path) -> None:
        src = _make_full_install(tmp_path)
        with _serving(src) as (base, _httpd):
            status, body = _post(base + "/edit", {
                "kind": "config", "source": "seed/world.md", "heading": "Identity",
                "expected_body": "WRONG", "new_body": "x",
            })
        assert status == 409 and body["error"] == "stale"

    def test_bad_host_refused(self, tmp_path: Path) -> None:
        src = _make_full_install(tmp_path)
        with _serving(src) as (base, _httpd):
            status, _ = _post(base + "/edit", {"kind": "entity_name", "value": "x"},
                              headers={"Host": "evil.com"})
        assert status == 403

    def test_unknown_route_404(self, tmp_path: Path) -> None:
        src = _make_full_install(tmp_path)
        with _serving(src) as (base, _httpd):
            status, _ = _post(base + "/wat", {"kind": "entity_name", "value": "x"})
        assert status == 404

    def test_bad_json_400(self, tmp_path: Path) -> None:
        src = _make_full_install(tmp_path)
        with _serving(src) as (base, _httpd):
            data = b"{not json"
            req = urllib.request.Request(
                base + "/edit", data=data, method="POST",
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
                    status = r.status
            except urllib.error.HTTPError as e:
                status = e.code
        assert status == 400

    def test_oversize_413(self, tmp_path: Path) -> None:
        from levain.web_server import _MAX_POST_BYTES

        src = _make_full_install(tmp_path)
        big = "z" * (_MAX_POST_BYTES + 1024)
        with _serving(src) as (base, _httpd):
            status, body = _post(base + "/edit", {
                "kind": "config", "source": "seed/world.md", "heading": "Identity",
                "expected_body": "Phill. 46. Columbus, OH.", "new_body": big,
            })
        assert status == 413

    def test_get_to_edit_is_404(self, tmp_path: Path) -> None:
        # /edit is POST-only; a GET falls through the read allowlist to 404.
        src = _make_full_install(tmp_path)
        with _serving(src) as (base, _httpd):
            try:
                status, _, _ = _request(base + "/edit", method="GET")
            except urllib.error.HTTPError as e:
                status = e.code
        assert status == 404

    def test_rate_gate_503(self, tmp_path: Path) -> None:
        # Saturate the bounded gate, then a store read returns a clean 503.
        from levain.web_server import _MAX_INFLIGHT

        src = _make_full_install(tmp_path)
        with _serving(src) as (base, httpd):
            held = [httpd.request_gate.acquire(blocking=False) for _ in range(_MAX_INFLIGHT)]
            assert all(held)
            try:
                status, _, _ = _request(base + "/substrate.json", method="GET")
            except urllib.error.HTTPError as e:
                status = e.code
            finally:
                for _ in range(_MAX_INFLIGHT):
                    httpd.request_gate.release()
        assert status == 503


# --- Slice 2b-ii: the Class-B verb route (spores + episode tombstone) --------

def _seed_anneal(src: SubstrateSource) -> tuple[str, str]:
    """Plant an open spore + an episode in the install's `.levain/` anneal store so
    the Class-B verb route has real lifecycle data to act on."""
    from anneal_memory import Store
    from anneal_memory.spores import SporeStore

    root = src.install_root
    s = SporeStore(root / ".levain" / "memory.spores.json").add(type="task", text="ship 2b-ii")
    with Store(str(root / ".levain" / "memory.db")) as store:
        ep = store.record("built the writable handle", "observation")
    return str(s["id"]), ep.id


class TestClassBRoute:
    def test_spore_descend_confirm_flow(self, tmp_path: Path) -> None:
        # the destructive verb is refused without confirm, then succeeds with it —
        # the per-write confirm gate enforced end-to-end through the HTTP boundary.
        src = _make_full_install(tmp_path)
        sid, _eid = _seed_anneal(src)
        with _serving(src) as (base, _httpd):
            s1, b1 = _post(base + "/edit", {
                "kind": "spore_descend", "spore_id": sid, "spore_kind": "done",
            })
            s2, b2 = _post(base + "/edit", {
                "kind": "spore_descend", "spore_id": sid, "spore_kind": "done", "confirm": True,
            })
        assert s1 == 409 and b1["error"] == "confirm_required"
        assert s2 == 200 and b2["ok"] is True

    def test_spore_touch_and_episode_tombstone(self, tmp_path: Path) -> None:
        src = _make_full_install(tmp_path)
        sid, eid = _seed_anneal(src)
        with _serving(src) as (base, _httpd):
            st, bt = _post(base + "/edit", {"kind": "spore_touch", "spore_id": sid})
            se, be = _post(base + "/edit", {
                "kind": "episode_tombstone", "episode_id": eid, "confirm": True,
            })
        assert st == 200 and bt["action"] == "touch"
        assert se == 200 and be["action"] == "tombstone"


class TestRecallJson:
    """/recall.json — the read-only episode keyword-search route (spore-107)."""

    def test_recall_matches_and_carries_no_writable_bit(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _httpd):
            status, headers, body = _get(base + "/recall.json?keyword=decided")
        assert status == 200
        assert headers["Content-Type"].startswith("application/json")
        data = json.loads(body)
        assert data["keyword"] == "decided"
        assert data["count"] == 1
        assert len(data["episodes"]) == 1
        assert "decided" in data["episodes"][0]["content"]
        # the read-only route carries NO write-capability bit (unlike /substrate.json)
        assert "writable" not in data

    def test_recall_no_match(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _httpd):
            _s, _h, body = _get(base + "/recall.json?keyword=zzznotpresent")
        data = json.loads(body)
        assert data["count"] == 0 and data["episodes"] == []

    def test_recall_empty_keyword_is_noop(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _httpd):
            _s, _h, body = _get(base + "/recall.json?keyword=")
        assert json.loads(body)["count"] == 0

    def test_recall_absent_keyword_param(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _httpd):
            _s, _h, body = _get(base + "/recall.json")
        assert json.loads(body)["count"] == 0

    def test_recall_caps_at_limit(self, tmp_path: Path) -> None:
        from anneal_memory import Store

        db = tmp_path / "memory.db"
        with Store(db) as store:
            for i in range(105):
                store.record(f"capword episode {i}", "observation")
        src = SubstrateSource(anneal=AnnealPaths.from_db(db))
        with _serving(src) as (base, _httpd):
            _s, _h, body = _get(base + "/recall.json?keyword=capword")
        # the route pins limit to _RECALL_LIMIT (100); the client cannot raise it
        assert json.loads(body)["count"] == 100

    def test_recall_surfaces_data_layer_error(self, tmp_path: Path, monkeypatch) -> None:
        # a store/data fault from the data layer must reach the payload as
        # errors.episodes (the box shows 'search unavailable'), never a 500
        import levain.web_server as ws

        monkeypatch.setattr(ws, "recall_episode_rows", lambda *a, **k: ([], "boom"))
        with _serving(_store_with_data(tmp_path)) as (base, _httpd):
            _s, _h, body = _get(base + "/recall.json?keyword=anything")
        data = json.loads(body)
        assert data["count"] == 0
        assert data["errors"]["episodes"] == "boom"

    def test_recall_rejects_post(self, tmp_path: Path) -> None:
        # /recall.json is GET/HEAD only — the sole POST route is /edit. A POST must be
        # refused (4xx), never silently handled.
        import urllib.error

        with _serving(_store_with_data(tmp_path)) as (base, _httpd):
            try:
                _request(base + "/recall.json", method="POST",
                         headers={"Content-Type": "application/json"})
                raised = None
            except urllib.error.HTTPError as e:
                raised = e.code
        assert raised is not None and raised >= 400

    def test_recall_drops_partial_rows_on_error(self, tmp_path: Path, monkeypatch) -> None:
        # MED-1 (L3): a mid-iteration data fault returns partial rows + an error; the
        # search surface must show the error and DROP the partial rows (a partial result
        # reads as 'all the matches', misleading). error XOR rows, enforced at the web layer.
        import levain.web_server as ws

        monkeypatch.setattr(ws, "recall_episode_rows", lambda *a, **k: (["dropme"], "midfault"))
        with _serving(_store_with_data(tmp_path)) as (base, _httpd):
            _s, _h, body = _get(base + "/recall.json?keyword=anything")
        data = json.loads(body)
        assert data["episodes"] == [] and data["count"] == 0
        assert data["errors"]["episodes"] == "midfault"


@contextmanager
def _serving_extra(source: SubstrateSource, *, extra_assets=None, extra_json=None):
    """A real server carrying downstream-registered read-only extra routes — the live
    harness for the FleetView extension point (make_server extra_assets/extra_json)."""
    httpd = make_server(
        source, host="127.0.0.1", port=0,
        extra_assets=extra_assets, extra_json=extra_json,
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[0], httpd.server_address[1]
    try:
        yield f"http://{host}:{port}", httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class TestExtraRoutes:
    """The read-only extra-route extension point — the FleetView seam. A downstream
    (the flow Bridge) registers additional GET views that ride the SAME security
    envelope; they cannot shadow a built-in route, cannot become a write surface, and a
    builder fault degrades to JSON rather than a 500."""

    def test_static_extra_asset_served_with_security_headers(self, tmp_path: Path) -> None:
        assets = {"/fleet": ("text/html; charset=utf-8", b"<!doctype html><title>x</title>")}
        with _serving_extra(_store_with_data(tmp_path), extra_assets=assets) as (base, _h):
            status, headers, body = _get(base + "/fleet")
        assert status == 200
        assert headers["Content-Type"] == "text/html; charset=utf-8"
        assert body == b"<!doctype html><title>x</title>"
        # Rides the full envelope: CSP + nosniff + frame-deny + no-store, same as built-ins.
        assert "default-src 'none'" in headers["Content-Security-Policy"]
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Cache-Control"] == "no-store"

    def test_dynamic_extra_json_served_per_request(self, tmp_path: Path) -> None:
        calls = {"n": 0}

        def builder() -> bytes:
            calls["n"] += 1
            return json.dumps({"hit": calls["n"]}).encode("utf-8")

        with _serving_extra(_store_with_data(tmp_path), extra_json={"/fleet.json": builder}) as (base, _h):
            s1, h1, b1 = _get(base + "/fleet.json")
            s2, _h2, b2 = _get(base + "/fleet.json")
        assert s1 == 200 and s2 == 200
        assert h1["Content-Type"] == "application/json; charset=utf-8"
        # Built per request (live), not cached — the count advances.
        assert json.loads(b1)["hit"] == 1 and json.loads(b2)["hit"] == 2

    def test_extra_json_builder_fault_degrades_to_json_not_500(self, tmp_path: Path) -> None:
        def boom() -> bytes:
            raise RuntimeError("kaboom")

        with _serving_extra(_store_with_data(tmp_path), extra_json={"/fleet.json": boom}) as (base, _h):
            status, headers, body = _get(base + "/fleet.json")
        assert status == 200  # never a 500 — surfaced as JSON the surface can render
        assert headers["Content-Type"] == "application/json; charset=utf-8"
        assert "RuntimeError" in json.loads(body)["errors"]["server"]

    def test_extra_route_is_not_a_write_surface(self, tmp_path: Path) -> None:
        # POST only ever routes /edit; an extra GET route is unreachable by POST (404),
        # so registering a view can never open a write path. [read-only by construction]
        assets = {"/fleet": ("text/html; charset=utf-8", b"x")}
        json_routes = {"/fleet.json": lambda: b"{}"}
        with _serving_extra(_store_with_data(tmp_path), extra_assets=assets,
                            extra_json=json_routes) as (base, _h):
            for path in ("/fleet", "/fleet.json"):
                try:
                    _request(base + path, method="POST",
                             headers={"Content-Type": "application/json"})
                    code = None
                except urllib.error.HTTPError as e:
                    code = e.code
                assert code == 404, f"POST {path} should 404, got {code}"

    def test_extra_route_honors_host_allowlist(self, tmp_path: Path) -> None:
        # A DNS-rebinding page's request carries a non-loopback Host; the extra route
        # must refuse it (403) exactly like the built-in routes — same _route gate.
        assets = {"/fleet": ("text/html; charset=utf-8", b"x")}
        with _serving_extra(_store_with_data(tmp_path), extra_assets=assets) as (base, _h):
            try:
                _request(base + "/fleet", headers={"Host": "evil.example.com"})
                code = None
            except urllib.error.HTTPError as e:
                code = e.code
        assert code == 403

    def test_base_product_carries_no_extra_routes(self, tmp_path: Path) -> None:
        # Regression: with no extras registered, /fleet is just a 404 — the extension
        # point adds nothing to the base product.
        with _serving(_store_with_data(tmp_path)) as (base, _httpd):
            try:
                _get(base + "/fleet")
                code = None
            except urllib.error.HTTPError as e:
                code = e.code
        assert code == 404

    def test_reserved_path_collision_refused(self, tmp_path: Path) -> None:
        source = _store_with_data(tmp_path)
        for reserved in ("/", "/substrate.json", "/recall.json", "/edit"):
            try:
                make_server(source, port=0, extra_assets={reserved: ("text/html", b"x")})
                raised = False
            except ValueError:
                raised = True
            assert raised, f"{reserved} should be refused as a collision"

    def test_dup_path_in_both_mappings_refused(self, tmp_path: Path) -> None:
        source = _store_with_data(tmp_path)
        try:
            make_server(source, port=0, extra_assets={"/x": ("text/html", b"a")},
                        extra_json={"/x": lambda: b"{}"})
            raised = False
        except ValueError:
            raised = True
        assert raised

    def test_malformed_extra_path_refused(self, tmp_path: Path) -> None:
        source = _store_with_data(tmp_path)
        # Non-canonical forms are refused for the public seam (codex L3): query/fragment,
        # percent-encoding, backslash, empty/double/trailing segments, ';' params, dot-segments.
        for bad in ("noslash", "/has?query", "/has#frag", "/pct%2e", "/back\\slash",
                    "/double//slash", "/trailing/", "/semi;colon", "/dot/./seg",
                    "/dot/../seg"):
            try:
                make_server(source, port=0, extra_json={bad: lambda: b"{}"})
                raised = False
            except ValueError:
                raised = True
            assert raised, f"{bad!r} should be refused"

    def test_case_insensitive_reserved_collision_refused(self, tmp_path: Path) -> None:
        # /EDIT must be refused as a near-collision with /edit even though exact-string
        # routing would treat them as distinct — defense against a future case-fold.
        source = _store_with_data(tmp_path)
        for clash in ("/EDIT", "/Substrate.json", "/RECALL.json"):
            try:
                make_server(source, port=0, extra_assets={clash: ("text/html", b"x")})
                raised = False
            except ValueError:
                raised = True
            assert raised, f"{clash!r} should be refused (case-insensitive reserved)"

    def test_extra_asset_bad_value_shape_refused(self, tmp_path: Path) -> None:
        source = _store_with_data(tmp_path)
        for bad_value in (
            "not-a-tuple",
            ("text/html",),                 # wrong arity
            (b"bytes-ct", b"body"),         # content_type must be str
            ("text/html", "str-body"),      # body must be bytes
        ):
            try:
                make_server(source, port=0, extra_assets={"/x": bad_value})
                raised = False
            except ValueError:
                raised = True
            assert raised, f"asset value {bad_value!r} should be refused"

    def test_extra_asset_content_type_crlf_refused(self, tmp_path: Path) -> None:
        # content_type flows into send_header — CR/LF would inject response headers.
        source = _store_with_data(tmp_path)
        for bad_ct in ("text/html\r\nX-Evil: 1", "text/html\nSet-Cookie: x"):
            try:
                make_server(source, port=0, extra_assets={"/x": (bad_ct, b"body")})
                raised = False
            except ValueError:
                raised = True
            assert raised, f"content_type {bad_ct!r} should be refused"

    def test_extra_json_non_bytes_return_degrades_not_breaks_framing(self, tmp_path: Path) -> None:
        # A builder that returns str (not bytes) must NOT emit a Content-Length then
        # TypeError on the wire — it degrades to a JSON error like any other fault.
        with _serving_extra(_store_with_data(tmp_path),
                            extra_json={"/fleet.json": lambda: "i am a str, not bytes"}) as (base, _h):
            status, headers, body = _get(base + "/fleet.json")
        assert status == 200
        assert headers["Content-Type"] == "application/json; charset=utf-8"
        assert "TypeError" in json.loads(body)["errors"]["server"]


# --- spore-129: the OFF-BOX write-token (Tailscale-write) boundary ----------

class TestOffBoxWriteToken:
    """A WRITABLE source MAY bind off-loopback ONLY with a ``write_token`` (the off-box
    governance factor that replaces loopback-is-auth); POST /edit then requires the
    ``X-Levain-Write-Token`` header (constant-time compared); loopback stays token-free.

    The off-loopback BIND itself can't be exercised in a test (no real mesh interface →
    OSError on the unroutable TEST-NET addr), so the bind-LOGIC is asserted via the
    pre-bind ValueError, and the token ENFORCEMENT via a loopback server whose
    ``is_loopback_bind`` is forced False — exactly what a real Tailscale bind sets."""

    def _writable_noinstall(self, tmp_path: Path) -> SubstrateSource:
        # flow's bridge shape: write_scope set, install_root=None (no Levain seed). The
        # WriteScope itself is install-less too (ledger-only), mirroring scripts/bridge.py.
        src = _store_with_data(tmp_path)
        return SubstrateSource(
            anneal=src.anneal, install_root=None,
            write_scope=WriteScope(
                anneal=src.anneal, ledger_root=tmp_path / "ledger", install_root=None
            ),
        )

    def test_writable_offloopback_without_token_refused_at_bind(self, tmp_path: Path) -> None:
        import pytest
        # writable + no-install + non-loopback + NO token → refused BEFORE any bind.
        with pytest.raises(ValueError, match="write_token"):
            make_server(self._writable_noinstall(tmp_path), host="192.0.2.1", port=0)

    def test_writable_offloopback_with_token_clears_bind_guard(self, tmp_path: Path) -> None:
        import pytest
        # WITH a token the writable bind-guard is RELAXED → it proceeds to actually bind the
        # unroutable TEST-NET addr and fails THERE (OSError, not ValueError) — proving the
        # guard was cleared for it (same proof shape as the read-only mesh test).
        with pytest.raises(OSError):
            make_server(self._writable_noinstall(tmp_path), host="192.0.2.1", port=0,
                        write_token="s3cret")

    def test_install_bearing_offloopback_token_does_not_relax(self, tmp_path: Path) -> None:
        import pytest
        # A token MUST NOT relax the install-bearing loopback-only rule — its seed/config is
        # operator-private, a DIFFERENT concern than spore-129. Refused even WITH a token.
        src = _store_with_data(tmp_path)
        install_writable = SubstrateSource(
            anneal=src.anneal, install_root=tmp_path,
            write_scope=WriteScope.from_install_root(tmp_path),
        )
        with pytest.raises(ValueError, match="install-bearing"):
            make_server(install_writable, host="192.0.2.1", port=0, write_token="s3cret")

    def test_loopback_writable_is_token_free(self, tmp_path: Path) -> None:
        # The localhost-sovereign path is UNCHANGED: a loopback writable bind needs no token,
        # write_token_required is False, and a tokenless write is NOT the token-403.
        with _serving(self._writable_noinstall(tmp_path)) as (base, httpd):
            assert httpd.is_loopback_bind is True
            assert httpd.write_token is None
            v = json.loads(_get(base + "/substrate.json")[2])
            assert v["writable"] is True
            assert v["write_token_required"] is False
            _st, resp = _post(base + "/edit", {"kind": "entity_name", "value": "x"})
            assert resp.get("message") != "missing or invalid write token"

    def test_offbox_substrate_json_flags_token_required(self, tmp_path: Path) -> None:
        # Simulate an off-box bind (is_loopback_bind False + a token): the JSON tells the
        # frontend to attach the token. The predicate mirrors do_POST exactly.
        with _serving(self._writable_noinstall(tmp_path)) as (base, httpd):
            httpd.is_loopback_bind = False
            httpd.write_token = "s3cret"
            assert json.loads(_get(base + "/substrate.json")[2])["write_token_required"] is True

    def test_offbox_write_requires_correct_token(self, tmp_path: Path) -> None:
        # The enforcement: off-box, POST /edit demands X-Levain-Write-Token == the server's
        # token. Missing or wrong → 403 token error; correct → clears the gate (reaches the
        # write layer, where no-install yields some OTHER error, never the token message).
        with _serving(self._writable_noinstall(tmp_path)) as (base, httpd):
            httpd.is_loopback_bind = False
            httpd.write_token = "s3cret"
            st, resp = _post(base + "/edit", {"kind": "entity_name", "value": "x"})
            assert st == 403 and resp["message"] == "missing or invalid write token"
            st, resp = _post(base + "/edit", {"kind": "entity_name", "value": "x"},
                             headers={"X-Levain-Write-Token": "wrong"})
            assert st == 403 and resp["message"] == "missing or invalid write token"
            _st, resp = _post(base + "/edit", {"kind": "entity_name", "value": "x"},
                              headers={"X-Levain-Write-Token": "s3cret"})
            assert resp.get("message") != "missing or invalid write token"

    def test_offbox_with_no_server_token_fails_closed(self, tmp_path: Path) -> None:
        # Defense-in-depth: a server somehow off-loopback yet token-less (make_server refuses
        # to BIND that, but do_POST must not TRUST that) refuses every write, fail-closed.
        with _serving(self._writable_noinstall(tmp_path)) as (base, httpd):
            httpd.is_loopback_bind = False
            httpd.write_token = None
            st, resp = _post(base + "/edit", {"kind": "entity_name", "value": "x"},
                             headers={"X-Levain-Write-Token": "anything"})
            assert st == 403 and resp["message"] == "missing or invalid write token"

    def test_readonly_offbox_post_is_422_not_token_403(self, tmp_path: Path) -> None:
        # codex/L2: a READ-ONLY off-box surface (write_scope None) must fall through to the
        # honest 422 'read_only', NOT a token-403 — the token gate also requires writability, so
        # it mirrors _write_token_required exactly. (write_token_required is False here.)
        with _serving(_store_with_data(tmp_path)) as (base, httpd):  # write_scope=None
            httpd.is_loopback_bind = False
            assert json.loads(_get(base + "/substrate.json")[2])["write_token_required"] is False
            st, resp = _post(base + "/edit", {"kind": "entity_name", "value": "x"})
            assert st == 422 and resp["error"] == "read_only"

    def test_writescope_install_root_divergence_refused(self, tmp_path: Path) -> None:
        import pytest
        # L1 MED-1: source.install_root=None but the WRITE_SCOPE's install_root is SET. The write
        # path keys off write_scope.install_root, so this IS install-bearing and must stay
        # loopback-only — refused off-loopback EVEN WITH a token (a source-only guard misses this).
        src = _store_with_data(tmp_path)
        diverged = SubstrateSource(
            anneal=src.anneal, install_root=None,
            write_scope=WriteScope.from_install_root(tmp_path),  # install_root SET on the scope
        )
        with pytest.raises(ValueError, match="install-bearing"):
            make_server(diverged, host="192.0.2.1", port=0, write_token="s3cret")

    def test_install_bearing_postbind_drift_refused(self, tmp_path: Path, monkeypatch) -> None:
        import pytest

        import levain.web_server as ws
        # codex L3 MED: a loopback-CLASSIFIED requested host that BINDS off-loopback (hosts-file
        # drift) must STILL refuse an install-bearing source — verified against the ACTUAL bound
        # address post-bind. Simulate the drift: _is_loopback_host answers True for the requested
        # gate (1st call) and False for the post-bind reality check (2nd call).
        src = _store_with_data(tmp_path)
        install = SubstrateSource(anneal=src.anneal, install_root=tmp_path)  # write_scope None
        calls = {"n": 0}

        def fake(_h: str) -> bool:
            calls["n"] += 1
            return calls["n"] == 1  # 1st = requested-host gate (loopback) → 2nd = post-bind (off-box)

        monkeypatch.setattr(ws, "_is_loopback_host", fake)
        with pytest.raises(ValueError, match="operator-private"):
            ws.make_server(install, host="127.0.0.1", port=0)

    def test_loopback_bind_ignores_passed_token(self, tmp_path: Path) -> None:
        # L1 LOW-2: exercise the REAL make_server computation — a 127.0.0.1 bind yields
        # is_loopback_bind True even with a write_token passed, and do_POST IGNORES the token
        # (the localhost-sovereign token-free path). (The other tests force the attr; this one
        # proves make_server derives it from the actual bound address.)
        httpd = make_server(self._writable_noinstall(tmp_path), host="127.0.0.1", port=0,
                            write_token="ignored-on-loopback")
        assert httpd.is_loopback_bind is True
        assert httpd.write_token == "ignored-on-loopback"
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://{httpd.server_address[0]}:{httpd.server_address[1]}"
            assert json.loads(_get(base + "/substrate.json")[2])["write_token_required"] is False
            _st, resp = _post(base + "/edit", {"kind": "entity_name", "value": "x"})
            assert resp.get("message") != "missing or invalid write token"
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
