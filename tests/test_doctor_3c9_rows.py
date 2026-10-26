"""The 3c-9 doctor rows (design §4 row 3c-9): route trust, the keys service, the spend ledger's size, the invoice
ceiling (the OpenRouter ``limit_remaining`` read through a stub transport; no real credential and no network), and the
slot rows' reading of ``records.OUT_OF_SERVICE`` instead of a literal."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from levain import doctor
from levain.broker import creds, policy
from levain.slot import layout
from levain.slot import records as R
from levain.slot import routemaps as RM
from levain.slot import vendor_routes as VR
from levain.slot.ids import SlotId

NOW = 1_800_000_000.0
STORE_ID = "0123456789abcdef0123456789abcdef"
def _pub_line(seed: bytes = b"\x01") -> tuple[str, str]:
    """A well-formed ssh-ed25519 public key line and its SHA256 fingerprint (the OpenSSH wire form)."""
    import base64
    import hashlib
    import struct
    t = b"ssh-ed25519"
    blob = struct.pack(">I", len(t)) + t + struct.pack(">I", 32) + (seed * 32)[:32]
    fp = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} test", fp


PUB, FP = _pub_line()
SECRET = "sk-or-v1-not-a-real-secret"


def _store(tmp: Path) -> Path:
    store = tmp / "store"
    (store / "policy").mkdir(parents=True)
    (store / "slots").mkdir()
    (store / "routes").mkdir()
    os.chmod(store / "routes", 0o755)
    return store


def _slot(store: Path, tag: str, state: str) -> None:
    d = store / "slots" / tag
    d.mkdir()
    slot = SlotId("egress" if tag[0] == "e" else "none", int(tag[1:]))
    rec = R.SlotState(slot, 1, 1, state, True, R.timestamp(NOW), "s-a1b2c3d4e5f60718" if state == "leased" else None)
    (d / "state").write_bytes(R.slot_state_bytes(rec))


def _trust(store: Path) -> None:
    (store / "routes" / RM.TRUST_PUB).write_text(PUB + "\n")
    (store / "routes" / RM.TRUST_JSON).write_text(
        json.dumps({"format": RM.TRUST_FORMAT, "store_id": STORE_ID, "fingerprint": FP}, sort_keys=True,
                   separators=(",", ":")))


def _openrouter_route() -> dict:
    r = VR.anthropic_route()
    r.update({"route": "orouter", "hosts": ["openrouter.ai"], "credential": "orkey",
              "upstream": {"scheme": "https", "host": "openrouter.ai", "base_path": ""}})
    return r


def _base(store: Path, routes: list[dict]) -> None:
    (store / "routes" / "base.json").write_text(json.dumps({"format": VR.BASE_FORMAT, "routes": routes}))


def _cred(store: Path, name: str, host: str, mode: int = 0o600) -> None:
    d = store / "keys" / "cred"
    d.mkdir(parents=True, exist_ok=True)
    raw, _rev = creds.render({"format": creds.FORMAT, "name": name, "kind": "static", "hosts": [host],
                              "inject": {"header": "authorization", "scheme": "Bearer"}, "secret": SECRET}, name)
    p = d / f"{name}.json"
    p.write_bytes(raw)
    os.chmod(p, mode)


def _rows(store: Path, **kw) -> dict:
    kw.setdefault("running", lambda host, name: True)
    rows = doctor._check_slot_keys(host="linux", store=store, **kw)
    return {r.name: r for r in rows}


# --- the isolation install gate ---------------------------------------------------------------------------------

def test_no_rows_before_the_isolation_install(tmp_path):
    assert doctor._check_slot_keys(host="linux", store=tmp_path / "none") == []


# --- route trust ------------------------------------------------------------------------------------------------

def test_trust_pinned_names_the_fingerprint_and_store(tmp_path):
    store = _store(tmp_path)
    _trust(store)
    r = _rows(store)["route trust"]
    assert r.ok and not r.warn and FP in r.detail and STORE_ID in r.detail


def test_trust_absent_is_a_warning_naming_the_command(tmp_path):
    r = _rows(_store(tmp_path))["route trust"]
    assert r.ok and r.warn and "levain dispatch config init" in (r.hint or "")


def test_trust_malformed_is_a_failure(tmp_path):
    store = _store(tmp_path)
    _trust(store)
    (store / "routes" / RM.TRUST_JSON).write_text("{}")
    r = _rows(store)["route trust"]
    assert not r.ok and "trust.json" in r.detail


# --- the spend ledger -------------------------------------------------------------------------------------------

def test_ledger_size_is_reported(tmp_path):
    store = _store(tmp_path)
    (store / "keys").mkdir()
    (store / "keys" / "ledger.jsonl").write_bytes(b"x" * 1234)
    r = _rows(store)["spend ledger"]
    assert r.ok and "1,234 bytes" in r.detail


def test_ledger_absent_is_said_not_failed(tmp_path):
    r = _rows(_store(tmp_path))["spend ledger"]
    assert r.ok and not r.warn and "not created" in r.detail


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 directory")
def test_ledger_in_keys_own_directory_is_unreadable_not_failed(tmp_path):
    store = _store(tmp_path)
    (store / "keys").mkdir()
    (store / "keys" / "ledger.jsonl").write_bytes(b"x")
    os.chmod(store / "keys", 0)
    try:
        r = _rows(store)["spend ledger"]
    finally:
        os.chmod(store / "keys", 0o700)
    assert r.ok and not r.warn and "held by keys" in r.detail and "once a session has started" in r.detail


# --- the keys service -------------------------------------------------------------------------------------------

def test_keys_without_an_egress_slot_is_not_a_failure_whatever_the_unit_does(tmp_path):
    store = _store(tmp_path)
    _slot(store, "v1", "reusable")
    for up in (True, False, None):
        r = _rows(store, running=lambda host, name, up=up: up)["keys service"]
        assert r.ok and not r.warn, up
        assert "no egress slot" in r.detail and "not a failure" in r.detail


def test_keys_with_an_egress_slot_and_a_stopped_unit_fails(tmp_path):
    store = _store(tmp_path)
    _slot(store, "e1", "reusable")
    assert _rows(store)["keys service"].ok
    r = _rows(store, running=lambda host, name: False)["keys service"]
    assert not r.ok and "not running" in r.detail
    r = _rows(store, running=lambda host, name: None)["keys service"]
    assert r.ok and r.warn and "could not be checked" in r.detail


def test_a_retired_egress_slot_is_not_an_egress_slot(tmp_path):
    store = _store(tmp_path)
    _slot(store, "e1", "retired")
    r = _rows(store, running=lambda host, name: False)["keys service"]
    assert r.ok and "no egress slot" in r.detail


# --- the invoice ceiling ----------------------------------------------------------------------------------------

def test_anthropic_and_openai_limits_are_named_as_operator_settings(tmp_path):
    store = _store(tmp_path)
    oa = VR.anthropic_route()
    oa.update({"route": "oai", "hosts": ["api.openai.com"], "credential": "openai",
               "upstream": {"scheme": "https", "host": "api.openai.com", "base_path": ""}})
    _base(store, [VR.anthropic_route(), oa])
    rows = _rows(store)
    a, o = rows["invoice ceiling (anthropic)"], rows["invoice ceiling (openai)"]
    assert a.ok and "not readable" in a.detail and "workspace" in a.detail
    assert o.ok and "not readable" in o.detail and "project" in o.detail


def _fetch_returning(status: int, doc: object, calls: list):
    def fetch(url, headers, timeout):
        calls.append((url, dict(headers), timeout))
        return status, json.dumps(doc).encode()
    return fetch


def test_openrouter_limit_remaining_is_read_through_the_transport(tmp_path):
    store = _store(tmp_path)
    _base(store, [_openrouter_route()])
    _cred(store, "orkey", "openrouter.ai")
    calls: list = []
    r = _rows(store, fetch=_fetch_returning(200, {"data": {"limit": 25, "limit_remaining": 18.5}}, calls))[
        "invoice ceiling (orkey)"]
    assert r.ok and not r.warn and "$25.00" in r.detail and "$18.50" in r.detail
    assert SECRET not in r.detail and SECRET not in (r.hint or "")
    (url, headers, timeout), = calls
    assert url == "https://openrouter.ai/api/v1/key" and 0 < timeout <= 10
    assert headers == {"authorization": f"Bearer {SECRET}"}


def test_openrouter_key_with_no_limit_is_a_warning(tmp_path):
    store = _store(tmp_path)
    _base(store, [_openrouter_route()])
    _cred(store, "orkey", "openrouter.ai")
    r = _rows(store, fetch=_fetch_returning(200, {"data": {"limit": None, "limit_remaining": None}}, []))[
        "invoice ceiling (orkey)"]
    assert r.ok and r.warn and "no credit limit" in r.detail


def test_openrouter_unreachable_is_its_own_row_text_and_never_a_failure(tmp_path):
    store = _store(tmp_path)
    _base(store, [_openrouter_route()])
    _cred(store, "orkey", "openrouter.ai")

    def boom(url, headers, timeout):
        raise TimeoutError("timed out")

    r = _rows(store, fetch=boom)["invoice ceiling (orkey)"]
    assert r.ok and r.warn and "unreachable" in r.detail and "TimeoutError" in r.detail


@pytest.mark.parametrize("status,doc", [(401, {"error": "x"}), (200, {"data": {"limit": "lots"}}), (200, [1])])
def test_openrouter_answers_not_understood_are_a_warning(tmp_path, status, doc):
    store = _store(tmp_path)
    _base(store, [_openrouter_route()])
    _cred(store, "orkey", "openrouter.ai")
    r = _rows(store, fetch=_fetch_returning(status, doc, []))["invoice ceiling (orkey)"]
    assert r.ok and r.warn and SECRET not in r.detail


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 directory")
def test_openrouter_credential_held_by_keys_is_named_not_read(tmp_path):
    store = _store(tmp_path)
    _base(store, [_openrouter_route()])
    _cred(store, "orkey", "openrouter.ai")
    os.chmod(store / "keys", 0)
    calls: list = []
    try:
        r = _rows(store, fetch=_fetch_returning(200, {}, calls))["invoice ceiling (orkey)"]
    finally:
        os.chmod(store / "keys", 0o700)
    assert r.ok and r.warn and "keys holds orkey" in r.detail and "openrouter.ai/settings/keys" in (r.hint or "")
    assert calls == []


def test_a_secret_is_never_sent_to_a_host_it_is_not_bound_to(tmp_path):
    store = _store(tmp_path)
    _base(store, [_openrouter_route()])
    _cred(store, "orkey", "api.example.com")
    calls: list = []
    r = _rows(store, fetch=_fetch_returning(200, {}, calls))["invoice ceiling (orkey)"]
    assert calls == [] and r.ok and r.warn


def _serve(handler_cls):
    import http.server
    import threading
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_the_default_transport_never_follows_a_redirect():
    import http.server
    seen: list = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/second")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = _serve(H)
    try:
        status, _body = doctor._fetch_key_limit(f"http://127.0.0.1:{srv.server_port}/first", {}, 3.0)
    finally:
        srv.shutdown()
    assert status == 302 and seen == ["/first"]


def test_the_default_transport_has_a_total_deadline_against_a_slow_drip():
    import http.server
    import time

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "100000")
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.2)
            except OSError:
                pass

        def log_message(self, *a):
            pass

    srv = _serve(H)
    t0 = time.monotonic()
    try:
        with pytest.raises(Exception):
            doctor._fetch_key_limit(f"http://127.0.0.1:{srv.server_port}/", {}, 1.0)
    finally:
        srv.shutdown()
    assert time.monotonic() - t0 < 3.0


def test_no_base_map_no_invoice_rows(tmp_path):
    assert not [n for n in _rows(_store(tmp_path)) if n.startswith("invoice ceiling")]


# --- records.OUT_OF_SERVICE, not a literal ----------------------------------------------------------------------

def test_slot_rows_follow_records_out_of_service(tmp_path, monkeypatch):
    store = _store(tmp_path)
    (store / "slots" / layout.POOL_LOCK_NAME).write_text("")
    (store / "policy" / "pool").write_text(layout.pool_config_text(2, 0))
    _slot(store, "e1", "reusable")
    _slot(store, "e2", "scrubbed")
    rows = {r.name: r for r in doctor._check_slot_isolation(host="linux", store=store, free_bytes=lambda p: 100 << 30)}
    assert rows["agent slots"].ok and "2 usable of 2" in rows["agent slots"].detail
    monkeypatch.setattr(R, "OUT_OF_SERVICE", frozenset({"retired", "scrubbed"}))
    rows = {r.name: r for r in doctor._check_slot_isolation(host="linux", store=store, free_bytes=lambda p: 100 << 30)}
    assert not rows["agent slots"].ok and "egress 1 of 2" in rows["agent slots"].detail
    assert "1 retired, not counted" in rows["agent slots"].detail


def test_doctor_py_holds_no_retired_literal():
    src = Path(doctor.__file__).read_text()
    assert '"retired"' not in src


# --- 3c-9 fix r1 (L3 r1 rulings) --------------------------------------------------------------------------------

def _or_ready(store: Path) -> None:
    _base(store, [_openrouter_route()])
    _cred(store, "orkey", "openrouter.ai")


def _ok_fetch(extra: dict | None = None):
    d = {"limit": 25, "limit_remaining": 18.5, "limit_reset": None, "include_byok_in_limit": True}
    d.update(extra or {})
    return _fetch_returning(200, {"data": d}, [])


def test_r1_1_a_trust_failure_survives_an_overflowing_provider_number(tmp_path):
    store = _store(tmp_path)
    _trust(store)
    (store / "routes" / RM.TRUST_JSON).write_text("{}")
    _or_ready(store)
    rows = _rows(store, fetch=_ok_fetch({"limit": 10 ** 400, "limit_remaining": 10 ** 400}))
    assert not rows["route trust"].ok
    assert rows["invoice ceiling (orkey)"].warn or not rows["invoice ceiling (orkey)"].ok
    assert not all(r.ok for r in rows.values())


def test_r1_1_a_row_builder_that_raises_is_its_own_failure_row(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _trust(store)

    def boom(_store, *_told):
        raise RuntimeError("boom")

    monkeypatch.setattr(doctor, "_check_spend_ledger", boom)
    rows = _rows(store)
    assert not rows["spend ledger"].ok and "RuntimeError" in rows["spend ledger"].detail
    assert rows["route trust"].ok and "keys service" in rows


def test_r1_1_base_json_as_a_directory_is_a_row_not_a_crash(tmp_path):
    store = _store(tmp_path)
    _trust(store)
    (store / "routes" / "base.json").mkdir()
    rows = _rows(store)
    assert rows["route trust"].ok and rows["invoice ceiling"].warn


class _FakeRoute:
    def __init__(self, host, cred):
        self.policy = policy.RoutePolicy(route="dyn", type="brokered", hosts=(host,),
                                         upstream=policy.Upstream("https", host, ""), credential=cred)


def _checked(gen: int, routes: dict) -> RM.Checked:
    return RM.Checked(gen, f"{gen:064x}", STORE_ID, routes, {rid: f"sha256:{gen:064x}" for rid in routes})


def _head(*gens: int) -> dict:
    return {"format": RM.KEYS_HEAD_FORMAT, "store_id": STORE_ID, "generation": gens[-1],
            "item_sha256": f"{gens[-1]:064x}", "served": [{"generation": g, "item_sha256": f"{g:064x}"} for g in gens]}


def _fake_dispatch(monkeypatch, store, routes):
    (store / "keys").mkdir(exist_ok=True)
    (store / "keys" / "routemap.head").write_text("x")
    checked = _checked(1, {f"r{i}": r for i, r in enumerate(routes)})
    monkeypatch.setattr(RM.KeysRoutes, "load", lambda self: (_head(1), {1: checked}, None))


def test_r1_2_an_openrouter_route_only_in_the_signed_map_gets_its_row(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _base(store, [VR.anthropic_route()])
    _cred(store, "orkey", "openrouter.ai")
    _fake_dispatch(monkeypatch, store, [_FakeRoute("openrouter.ai", "orkey")])
    r = _rows(store, fetch=_ok_fetch())["invoice ceiling (orkey)"]
    assert "$25.00" in r.detail


def test_r1_2_an_unreadable_dispatch_state_is_a_row_not_silence(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _base(store, [VR.anthropic_route()])
    (store / "keys").mkdir()
    (store / "keys" / "routemap.head").write_text("x")
    monkeypatch.setattr(RM.KeysRoutes, "load", lambda self: (None, {}, "routemap-head-unverifiable"))
    r = _rows(store)["invoice ceiling (dispatch routes)"]
    assert r.warn and "routemap-head-unverifiable" in r.detail


def test_r1_3_a_resetting_cap_is_said_to_be_per_period(tmp_path):
    store = _store(tmp_path)
    _or_ready(store)
    r = _rows(store, fetch=_ok_fetch({"limit_reset": "daily"}))["invoice ceiling (orkey)"]
    assert r.warn and "per day" in r.detail and "not a total" in r.detail


def test_r1_3_byok_outside_the_limit_warns(tmp_path):
    store = _store(tmp_path)
    _or_ready(store)
    r = _rows(store, fetch=_ok_fetch({"include_byok_in_limit": False}))["invoice ceiling (orkey)"]
    assert r.warn and "BYOK" in r.detail


def test_r1_3_an_exhausted_key_warns(tmp_path):
    store = _store(tmp_path)
    _or_ready(store)
    r = _rows(store, fetch=_ok_fetch({"limit_remaining": 0}))["invoice ceiling (orkey)"]
    assert r.warn and "exhausted" in r.detail


def test_r1_3_a_total_limit_with_byok_inside_is_green(tmp_path):
    store = _store(tmp_path)
    _or_ready(store)
    r = _rows(store, fetch=_ok_fetch())["invoice ceiling (orkey)"]
    assert r.ok and not r.warn and "total" in r.detail


def test_r1_4_a_trust_pub_that_is_another_key_fails(tmp_path):
    store = _store(tmp_path)
    _trust(store)
    other, _fp = _pub_line(b"\x02")
    (store / "routes" / RM.TRUST_PUB).write_text(other + "\n")
    r = _rows(store)["route trust"]
    assert not r.ok and "fingerprint" in r.detail


def test_r1_4_a_trust_pub_that_is_not_a_key_fails(tmp_path):
    store = _store(tmp_path)
    _trust(store)
    (store / "routes" / RM.TRUST_PUB).write_text("ssh-ed25519 AAAA\n")
    r = _rows(store)["route trust"]
    assert not r.ok and "public key" in r.detail


def test_r1_5_the_openrouter_reads_share_one_total_deadline(tmp_path, monkeypatch):
    store = _store(tmp_path)
    second = _openrouter_route()
    second.update({"route": "orouter2", "credential": "orkey2"})
    _base(store, [_openrouter_route(), second])
    _cred(store, "orkey", "openrouter.ai")
    _cred(store, "orkey2", "openrouter.ai")
    clock = [0.0]
    monkeypatch.setattr(doctor, "_clock", lambda: clock[0])     # r2 item 5: doctor's own clock, never time's
    monkeypatch.setattr(doctor, "_unit_running", lambda host, name: True)

    def slow(url, headers, timeout):
        clock[0] += doctor._LIMIT_TOTAL_S + 1
        raise TimeoutError("slow")

    rows = _rows(store, fetch=slow)
    assert "unreachable" in rows["invoice ceiling (orkey)"].detail
    late = rows["invoice ceiling (orkey2)"]
    assert late.warn and "time" in late.detail


def test_r1_6_a_base_map_keys_would_refuse_is_not_scanned(tmp_path):
    store = _store(tmp_path)
    good = json.dumps({"format": VR.BASE_FORMAT, "routes": [VR.anthropic_route()]})
    dup = good[:-1] + ', "routes": []}'
    (store / "routes" / "base.json").write_text(dup)
    r = _rows(store)["invoice ceiling"]
    assert r.warn and "base.json" in r.detail
    (store / "routes" / "base.json").write_text(good + " " * (RM.BASE_MAP_MAX + 1))
    assert "base.json" in _rows(store)["invoice ceiling"].detail


def test_r1_7_another_provider_is_a_warning_row(tmp_path):
    store = _store(tmp_path)
    m = VR.anthropic_route()
    m.update({"route": "mis", "hosts": ["api.mistral.ai"], "credential": "mistral",
              "upstream": {"scheme": "https", "host": "api.mistral.ai", "base_path": ""}})
    _base(store, [m])
    r = _rows(store)["invoice ceiling (mistral)"]
    assert r.warn and "api.mistral.ai" in r.detail and "unknown" in r.detail


def test_r1_8_a_missing_credential_is_not_held_by_keys(tmp_path):
    store = _store(tmp_path)
    _or_ready(store)
    (store / "keys" / "cred" / "orkey.json").unlink()
    r = _rows(store, fetch=_ok_fetch())["invoice ceiling (orkey)"]
    assert r.warn and "held by keys" not in r.detail and "no credential file" in r.detail


def test_r1_8_an_unparseable_credential_is_named_as_such(tmp_path):
    store = _store(tmp_path)
    _or_ready(store)
    (store / "keys" / "cred" / "orkey.json").write_text("not json")
    os.chmod(store / "keys" / "cred" / "orkey.json", 0o600)
    r = _rows(store, fetch=_ok_fetch())["invoice ceiling (orkey)"]
    assert r.warn and "held by keys" not in r.detail


def test_r1_9_status_is_checked_before_the_body(tmp_path):
    store = _store(tmp_path)
    _or_ready(store)
    r = _rows(store, fetch=_fetch_returning(429, {"error": "x"}, []))["invoice ceiling (orkey)"]
    assert "429" in r.detail and "not a key status" not in r.detail


def test_r1_9_linux_unit_not_found_is_not_running(monkeypatch):
    from types import SimpleNamespace
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=4, stdout="", stderr=""))
    assert doctor._unit_running("linux", "keys") is False


def test_r1_9_one_credential_on_two_hosts_has_two_distinct_row_names(tmp_path):
    store = _store(tmp_path)
    a = VR.anthropic_route()
    a["credential"] = "shared"
    o = _openrouter_route()
    o["credential"] = "shared"
    _base(store, [a, o])
    names = [r.name for r in doctor._check_slot_keys(host="linux", store=store, running=lambda h, n: True,
                                                     fetch=_ok_fetch())
             if r.name.startswith("invoice ceiling")]
    assert len(names) == 2 and len(set(names)) == 2


def test_r1_10_unreadable_slots_with_a_stopped_unit_is_a_warning_not_a_failure(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(doctor, "_egress_in_service", lambda s: None)
    r = _rows(store, running=lambda h, n: False)["keys service"]
    assert r.ok and r.warn


def test_r1_10_a_ledger_that_is_a_directory_is_not_a_size(tmp_path):
    store = _store(tmp_path)
    (store / "keys" / "ledger.jsonl").mkdir(parents=True)
    r = _rows(store)["spend ledger"]
    assert r.warn


# --- 3c-9 fix r2 (L3 r1 rulings R2: one home for what the runtime owns) -----------------------------------------

def test_r2_1_a_truncated_key_blob_pinned_by_its_hash_fails(tmp_path):
    # codex HIGH: a blob cut short still hashed to the pin, so the row read green while keys refuses every route
    import base64
    import hashlib
    store = _store(tmp_path)
    kt, b64 = PUB.split()[:2]
    blob = base64.b64decode(b64)[:-3]
    (store / "routes" / RM.TRUST_PUB).write_text(f"{kt} {base64.b64encode(blob).decode()}\n")
    fp = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    (store / "routes" / RM.TRUST_JSON).write_text(
        json.dumps({"format": RM.TRUST_FORMAT, "store_id": STORE_ID, "fingerprint": fp}, sort_keys=True,
                   separators=(",", ":")))
    r = _rows(store)["route trust"]
    assert not r.ok and "public key" in r.detail


def test_r2_1_route_trust_reads_the_key_through_authority_sig(tmp_path, monkeypatch):
    from levain import authority_sig
    store = _store(tmp_path)
    _trust(store)
    seen = []
    real = authority_sig.public_key_fingerprint
    monkeypatch.setattr(authority_sig, "public_key_fingerprint", lambda line, **k: seen.append(line) or real(line, **k))
    assert _rows(store)["route trust"].ok and seen == [PUB]
    assert not hasattr(doctor, "_pub_fingerprint")


def test_r2_1_no_verifier_is_its_own_warning(tmp_path, monkeypatch):
    from levain import authority_sig
    store = _store(tmp_path)
    _trust(store)

    def gone(line, **k):
        raise authority_sig.VerifierUnavailable("ssh-keygen is missing")

    monkeypatch.setattr(authority_sig, "public_key_fingerprint", gone)
    r = _rows(store)["route trust"]
    assert r.ok and r.warn and "ssh-keygen" in r.detail


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 directory")
def test_r2_2a_dispatch_maps_held_by_keys_say_so_and_name_the_remedy(tmp_path):
    store = _store(tmp_path)
    _base(store, [VR.anthropic_route()])
    (store / "keys").mkdir()
    (store / "keys" / "routemap.head").write_text("x")
    os.chmod(store / "keys", 0)
    try:
        r = _rows(store)["invoice ceiling (dispatch routes)"]
    finally:
        os.chmod(store / "keys", 0o700)
    assert r.ok and r.neutral and "held by keys" in r.detail and "sudo levain doctor" in (r.hint or "")


def test_r2_2b_only_routes_in_the_head_generation_are_served(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _base(store, [VR.anthropic_route()])
    _cred(store, "newkey", "openrouter.ai")
    _cred(store, "oldkey", "openrouter.ai")
    (store / "keys").mkdir(exist_ok=True)
    (store / "keys" / "routemap.head").write_text("x")
    maps = {1: _checked(1, {"old": _FakeRoute("openrouter.ai", "oldkey")}),
            2: _checked(2, {"new": _FakeRoute("openrouter.ai", "newkey")})}
    monkeypatch.setattr(RM.KeysRoutes, "load", lambda self: (_head(1, 2), maps, None))
    rows = _rows(store, fetch=_ok_fetch())
    assert "invoice ceiling (newkey)" in rows and "invoice ceiling (oldkey)" not in rows


def test_r2_2b_a_refused_head_serves_nothing_and_is_the_warning_only(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _base(store, [VR.anthropic_route()])
    _cred(store, "orkey", "openrouter.ai")
    (store / "keys").mkdir(exist_ok=True)
    (store / "keys" / "routemap.head").write_text("x")
    maps = {1: _checked(1, {"r": _FakeRoute("openrouter.ai", "orkey")})}
    monkeypatch.setattr(RM.KeysRoutes, "load", lambda self: (_head(1, 2), maps, "routemap-head-unverifiable"))
    rows = _rows(store, fetch=_ok_fetch())
    assert "invoice ceiling (orkey)" not in rows
    r = rows["invoice ceiling (dispatch routes)"]
    assert r.warn and "routemap-head-unverifiable" in r.detail and "every dispatch route" in r.detail


def test_r2_4_a_dispatch_load_that_raises_keeps_the_base_rows(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _or_ready(store)
    (store / "keys" / "routemap.head").write_text("x")

    def boom(self):
        raise FileNotFoundError("gone between the look and the read")

    monkeypatch.setattr(RM.KeysRoutes, "load", boom)
    rows = _rows(store, fetch=_ok_fetch())
    assert rows["invoice ceiling (orkey)"].ok and "$25.00" in rows["invoice ceiling (orkey)"].detail
    r = rows["invoice ceiling (dispatch routes)"]
    assert r.warn and "FileNotFoundError" in r.detail


def test_v2_a_dispatch_policy_that_raises_keeps_the_base_rows(tmp_path, monkeypatch):
    # verify r2 deepseek MED 1: install and the policy walk are inside the row's guard too, not only the load
    store = _store(tmp_path)
    _or_ready(store)
    (store / "keys" / "routemap.head").write_text("x")
    monkeypatch.setattr(RM.KeysRoutes, "load", lambda self: (None, {}, None))

    def boom(self, *a, **k):
        raise KeyError("a route renamed under the walk")

    monkeypatch.setattr(RM.KeysRoutes, "install", boom)
    rows = _rows(store, fetch=_ok_fetch())
    assert rows["invoice ceiling (orkey)"].ok and "$25.00" in rows["invoice ceiling (orkey)"].detail
    r = rows["invoice ceiling (dispatch routes)"]
    assert r.warn and "KeyError" in r.detail


def test_r2_2c_the_dispatch_load_is_inside_the_total_deadline(tmp_path, monkeypatch):
    import threading
    import time
    store = _store(tmp_path)
    _base(store, [VR.anthropic_route()])
    (store / "keys").mkdir()
    (store / "keys" / "routemap.head").write_text("x")
    release = threading.Event()

    def slow(self):
        release.wait(5)
        return None, {}, None

    monkeypatch.setattr(RM.KeysRoutes, "load", slow)
    monkeypatch.setattr(doctor, "_LIMIT_TOTAL_S", 0.3)
    t0 = time.monotonic()
    try:
        rows = _rows(store)
    finally:
        release.set()
    assert time.monotonic() - t0 < 2.0
    r = rows["invoice ceiling (dispatch routes)"]
    assert r.warn and "time" in r.detail


def test_r2_2c_a_missing_verifier_is_its_own_dispatch_warning(tmp_path, monkeypatch):
    from levain import trust_anchor
    store = _store(tmp_path)
    _base(store, [VR.anthropic_route()])
    (store / "keys").mkdir()
    (store / "keys" / "routemap.head").write_text("x")
    monkeypatch.setattr(trust_anchor, "DEFAULT_SSH_KEYGEN", tmp_path / "no-ssh-keygen")
    monkeypatch.setattr(RM.KeysRoutes, "load", lambda self: (_ for _ in ()).throw(AssertionError("not reached")))
    r = _rows(store)["invoice ceiling (dispatch routes)"]
    assert r.warn and "ssh-keygen" in r.detail and "every dispatch route" in r.detail


def test_r2_3_the_base_map_is_read_by_routemaps_one_reader(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _or_ready(store)
    seen = []
    real = RM.read_base_map
    monkeypatch.setattr(RM, "read_base_map", lambda *a, **k: seen.append(a) or real(*a, **k))
    assert "$25.00" in _rows(store, fetch=_ok_fetch())["invoice ceiling (orkey)"].detail
    assert seen == [(str(store),)]


def test_r2_3_a_linked_base_map_is_read_as_keys_reads_it(tmp_path):
    # keys opens base.json following a link (its runtime behaviour, kept); doctor reports what keys does
    store = _store(tmp_path)
    _or_ready(store)
    real = store / "routes" / "base.real.json"
    os.replace(store / "routes" / "base.json", real)
    os.symlink(real, store / "routes" / "base.json")
    assert "$25.00" in _rows(store, fetch=_ok_fetch())["invoice ceiling (orkey)"].detail


def test_r2_6_no_credential_directory_is_said_once(tmp_path):
    store = _store(tmp_path)
    second = _openrouter_route()
    second.update({"route": "orouter2", "credential": "orkey2"})
    _base(store, [_openrouter_route(), second])
    rows = doctor._check_slot_keys(host="linux", store=store, running=lambda h, n: True, fetch=_ok_fetch())
    ceiling = [r for r in rows if r.name.startswith("invoice ceiling")]
    assert len(ceiling) == 1 and ceiling[0].warn and "no credential is set for any route" in ceiling[0].detail


def test_r2_2b_a_real_signed_map_through_keys_own_load(tmp_path):
    # the r1 residue: a real ssh-keygen-signed map, accepted by CONTROL and RELOADed by keys, read back by doctor
    import shutil
    import tempfile
    from tests import test_routemaps as T
    d = Path(tempfile.mkdtemp(prefix="c9", dir="/tmp"))
    try:
        st = T.Store(d, T.Signer(d, "op"))
        (d / "policy").mkdir()
        cr = RM.ControlRoutes(st.routes)
        old = T.route("old")
        old["policy"]["credential"] = "oldkey"
        one, two = st.signed(T.item(1, [T.route(), old])), st.signed(T.item(2, [T.route()]))
        k = T.keys_of(st)
        T.submit(cr, *one)
        assert T.reload(k, 1, T.sha(one[0]))["generation"] == 1
        T.submit(cr, *two)
        assert T.reload(k, 2, T.sha(two[0]))["generation"] == 2
        assert [e["generation"] for e in st.keys_head()["served"]] == [1, 2]
        _cred(d, "or-key", "openrouter.ai")
        _cred(d, "oldkey", "openrouter.ai")
        rows = _rows(d, fetch=_ok_fetch())
        assert rows["route trust"].ok and not rows["route trust"].warn
        assert "$25.00" in rows["invoice ceiling (or-key)"].detail
        assert "invoice ceiling (oldkey)" not in rows and "invoice ceiling (dispatch routes)" not in rows
    finally:
        shutil.rmtree(d, ignore_errors=True)
