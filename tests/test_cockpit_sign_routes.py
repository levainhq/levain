"""D3 + D4 + D6 1-4 of the cockpit signing design (project_memory/1023-21/sign/DESIGN_SIGN.md): ``make_server`` hosts
levain's sign-request ceremony (:class:`levain.consent.sign.Signer`) behind the write gate, admits the https name a
reverse proxy forwards, and the write token alone never yields a signed result."""
from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from levain.consent.sign import Signer, SignRefused
from levain.dashboard import AnnealPaths, SubstrateSource
from levain.web_server import make_server
from levain.writes import WriteScope

from tests.test_cockpit_sign_origin import NS, PINNED, RP, _file, _Passkey

TOK = "sign-routes-token"
FC = "sign-routes-surface-token-0123456789"   # a named surface: honoured proxied, where the owner key is not (W1b L-1)
RID = "np-sign-route-0001"


def _source(root: Path) -> SubstrateSource:
    (root / ".levain").mkdir(parents=True, exist_ok=True)
    return SubstrateSource(anneal=AnnealPaths.from_db(root / ".levain" / "memory.db"),
                           write_scope=WriteScope.from_install_root(root))


@contextmanager
def _serving(root: Path, **kw: Any) -> Iterator[tuple[str, Any]]:
    kw.setdefault("surface_tokens", {"browser": TOK, "flowconnect": FC})
    httpd = make_server(_source(root), host="127.0.0.1", port=0, **kw)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _call(url: str, path: str, body: Any = None, *, token: "str | None" = TOK, host: "str | None" = None,
          sfs: "str | None" = None, ctype: str = "application/json",
          extra: "dict[str, str] | None" = None) -> "tuple[int, Any]":
    headers = dict(extra or {})
    if token is not None:
        headers["X-Levain-Write-Token"] = token
    if host is not None:
        headers["Host"] = host
    if sfs is not None:
        headers["Sec-Fetch-Site"] = sfs
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = ctype
    req = urllib.request.Request(url + path, data=data, headers=headers, method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 - loopback only
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


@pytest.fixture
def cockpit(tmp_path: Path) -> Iterator[dict[str, Any]]:
    pk = _Passkey()
    qdir = tmp_path / "q"
    signer = Signer(qdir, (NS,), RP, lambda: {pk.key.fingerprint: pk.key})
    signer.page_origin = PINNED
    _file(qdir, RID)
    (tmp_path / "allowed").write_text(f'x namespaces="{NS}" {pk.line}\n')
    with _serving(tmp_path / "inst", signer=signer, extra_hosts=[RP]) as (url, httpd):
        yield {"url": url, "httpd": httpd, "pk": pk, "qdir": qdir, "signer": signer, "allowed": tmp_path / "allowed"}


def _sha(c: dict[str, Any], rid: str = RID) -> str:
    status, body = _call(c["url"], "/cockpit/sign.json")
    assert status == 200, body
    return next(r["sha256"] for r in body["requests"] if r["id"] == rid)


def _challenge(c: dict[str, Any], choice: str = "b", rid: str = RID) -> "tuple[int, Any]":
    return _call(c["url"], f"/cockpit/sign/{rid}/challenge",
                 {"signer_fp": c["pk"].key.fingerprint, "request_sha256": _sha(c, rid), "choice": choice})


def _no_result(c: dict[str, Any], rid: str = RID) -> bool:
    d = c["qdir"] / "sign"
    return not (d / f"{rid}.signed.json").exists() and not (d / f"{rid}.sig").exists()


def _verifies(c: dict[str, Any], rid: str = RID) -> bool:
    rec = json.loads((c["qdir"] / "sign" / f"{rid}.signed.json").read_text())
    return subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(c["allowed"]), "-I", "x", "-n", NS, "-s",
                           str(c["qdir"] / "sign" / f"{rid}.sig")], input=rec["signed_message"].encode(),
                          capture_output=True).returncode == 0


# --- D4: the routes, and the gate they ride ----------------------------------------------------------------------


def test_without_a_signer_every_sign_route_is_404(tmp_path: Path) -> None:
    with _serving(tmp_path) as (url, _httpd):
        assert _call(url, "/cockpit/sign.json")[0] == 404
        for action in ("challenge", "answer", "deny"):
            assert _call(url, f"/cockpit/sign/{RID}/{action}", {})[0] == 404


def test_the_listing_is_the_signers_own_rows_and_its_enrolled_keys(cockpit) -> None:
    status, body = _call(cockpit["url"], "/cockpit/sign.json")
    assert status == 200
    assert set(body) == {"requests", "signers", "credentials", "origin"} and body["origin"] == PINNED
    assert body["credentials"] == {}      # no key facts held (levain.cockpit.keyfacts): the browser asks its own list
    assert body["signers"] == [cockpit["pk"].key.fingerprint]
    assert body["requests"] == cockpit["signer"].listing([cockpit["pk"].key.fingerprint])
    [row] = body["requests"]
    assert row["id"] == RID and [o["key"] for o in row["options"]] == ["a", "b"]


def test_every_sign_route_rides_the_write_gate(cockpit) -> None:
    url = cockpit["url"]
    routes = [("/cockpit/sign.json", None)] + [(f"/cockpit/sign/{RID}/{a}", {}) for a in ("challenge", "answer", "deny")]
    for path, body in routes:
        for token in (None, "wrong-token"):
            status, out = _call(url, path, body, token=token)
            assert status == 403, (path, token, status, out)
        assert _call(url, path, body, host="evil.example")[0] == 403
        assert _call(url, path, body, host="evil.example:8443")[0] == 403
        for sfs in ("cross-site", "same-site", "none"):
            assert _call(url, path, body, sfs=sfs)[0] == 403, (path, sfs)
        assert _call(url, path, body, sfs="same-origin")[0] in (200, 400, 409, 422), path
    for action in ("challenge", "answer", "deny"):
        assert _call(url, f"/cockpit/sign/{RID}/{action}", {}, ctype="text/plain")[0] == 415
    assert _call(url, "/cockpit/sign/nope/challenge", {"x": 1})[0] in (400, 404)
    assert _call(url, f"/cockpit/sign/{RID}/sign", {})[0] == 404
    assert _no_result(cockpit)


def test_a_refusal_carries_the_signers_status_and_words(cockpit) -> None:
    status, out = _call(cockpit["url"], f"/cockpit/sign/{RID}/answer",
                        {"authenticator_data": "AA", "client_data_json": "AA", "signature": "AA"})
    assert status == 409 and out["error"] == "conflict" and "no challenge is open" in out["message"]
    status, out = _call(cockpit["url"], "/cockpit/sign/no-such-request/challenge",
                        {"signer_fp": cockpit["pk"].key.fingerprint, "request_sha256": "x"})
    assert status == 404 and out == {"error": "not_found", "message": "no such sign request"}


# --- D6 1: the write token alone never yields a .signed.json ----------------------------------------------------


def test_the_write_token_with_a_forged_absent_or_unverified_assertion_signs_nothing(cockpit) -> None:
    url, pk = cockpit["url"], cockpit["pk"]
    status, ch = _challenge(cockpit)
    assert status == 200 and _no_result(cockpit)          # a challenge alone writes nothing
    good = pk.assertion(ch["challenge"], PINNED)
    for bad in ({}, {"authenticator_data": good["authenticator_data"]},
                {**good, "extra": "x"}):
        assert _call(url, f"/cockpit/sign/{RID}/answer", bad)[0] == 400
    forged = pk.assertion("bm90IHRoZSBjaGFsbGVuZ2U", PINNED)       # a real key over another challenge
    other = _Passkey()                                             # another key over the right challenge
    for bad in (forged, other.assertion(ch["challenge"], PINNED), pk.assertion(ch["challenge"], PINNED, flags=0x01),
                {**good, "signature": good["signature"][:-4] + "AAAA"}, {**good, "signature": None}):
        status, out = _call(url, f"/cockpit/sign/{RID}/answer", bad)
        assert status == 422, out
        assert _no_result(cockpit)
    assert "did not verify the user" in _call(url, f"/cockpit/sign/{RID}/answer",
                                              pk.assertion(ch["challenge"], PINNED, flags=0x01))[1]["message"]
    # the same token, the real gesture: signed, and stock ssh-keygen verifies it
    status, out = _call(url, f"/cockpit/sign/{RID}/answer", good)
    assert status == 200 and out["choice"] == "b" and _verifies(cockpit)


# --- D6 2: the page origin is exactly the pinned one ------------------------------------------------------------


def test_an_assertion_from_another_page_is_refused_and_writes_nothing(cockpit) -> None:
    url, pk = cockpit["url"], cockpit["pk"]
    for origin, cross in ((f"https://{RP}:9999", False), (f"http://{RP}:8443", False), (f"https://{RP}", False),
                          ("https://evil.example:8443", False), (PINNED, True)):
        _status, ch = _challenge(cockpit)
        status, out = _call(url, f"/cockpit/sign/{RID}/answer", pk.assertion(ch["challenge"], origin, cross=cross))
        assert status == 422, (origin, out)
        assert _no_result(cockpit)
    _status, ch = _challenge(cockpit)
    assert _call(url, f"/cockpit/sign/{RID}/answer", pk.assertion(ch["challenge"], PINNED))[0] == 200
    assert _verifies(cockpit)


# --- D6 3: a changed file is never signed -----------------------------------------------------------------------


def test_a_file_changed_since_listing_or_challenge_is_refused(cockpit) -> None:
    url, pk = cockpit["url"], cockpit["pk"]
    shown = _sha(cockpit)
    _file(cockpit["qdir"], RID, "I attest something else.\nnonce: 41f0")
    status, out = _call(url, f"/cockpit/sign/{RID}/challenge",
                        {"signer_fp": pk.key.fingerprint, "request_sha256": shown, "choice": "a"})
    assert status == 409 and "changed since it was shown" in out["message"]
    status, ch = _challenge(cockpit, "a")
    assert status == 200
    _file(cockpit["qdir"], RID, "I attest a third thing.\nnonce: 41f0")
    status, out = _call(url, f"/cockpit/sign/{RID}/answer", pk.assertion(ch["challenge"], PINNED))
    assert status == 409 and "changed after its challenge" in out["message"]
    assert _no_result(cockpit)
    status, out = _call(url, f"/cockpit/sign/{RID}/deny", {"request_sha256": shown})
    assert status == 409 and not (cockpit["qdir"] / "sign" / f"{RID}.denied").exists()


# --- D6 4: a phone-side Signer and the cockpit's on one queue ---------------------------------------------------


def _phone(c: dict[str, Any]) -> Signer:
    pk = c["pk"]
    phone = Signer(c["qdir"], (NS,), RP, lambda: {pk.key.fingerprint: pk.key})
    phone.page_origin = PINNED
    return phone


def _phone_challenge(phone: Signer, c: dict[str, Any], choice: str) -> str:
    [row] = phone.listing([c["pk"].key.fingerprint])
    return phone.challenge(RID, {"signer_fp": c["pk"].key.fingerprint, "request_sha256": row["sha256"],
                                 "choice": choice})["challenge"]


@pytest.mark.parametrize("first", ["cockpit", "phone"])
def test_the_phone_and_the_cockpit_answering_one_request_sign_it_once(cockpit, first: str) -> None:
    url, pk, phone = cockpit["url"], cockpit["pk"], _phone(cockpit)
    p_ch = _phone_challenge(phone, cockpit, "a")
    _status, c_ch = _challenge(cockpit, "b")
    if first == "cockpit":
        assert _call(url, f"/cockpit/sign/{RID}/answer", pk.assertion(c_ch["challenge"], PINNED))[0] == 200
        with pytest.raises(SignRefused) as late:
            phone.answer(RID, pk.assertion(p_ch, PINNED))
        assert late.value.status == 409
    else:
        assert phone.answer(RID, pk.assertion(p_ch, PINNED))["choice"] == "a"
        status, out = _call(url, f"/cockpit/sign/{RID}/answer", pk.assertion(c_ch["challenge"], PINNED))
        assert status == 409 and out["error"] == "conflict", out
    rec = json.loads((cockpit["qdir"] / "sign" / f"{RID}.signed.json").read_text())
    assert rec["choice"] == ("b" if first == "cockpit" else "a") and _verifies(cockpit)
    assert sorted(p.name for p in (cockpit["qdir"] / "sign").iterdir() if not p.name.startswith(".")) == [
        f"{RID}.json", f"{RID}.sig", f"{RID}.signed.json"]


def test_two_signers_racing_the_last_write_leave_a_sig_that_matches_its_record(cockpit, monkeypatch) -> None:
    """Reproduced on main: signer A wrote its ``.sig``; B, past its own lock (a different process's in real use),
    found the request open; A linked ``.signed.json``; B then wrote its ``.sig`` over A's and got its 409. The record
    said A's choice and the ``.sig`` was B's signature, so the result never verified. The per-process lock cannot
    order two Signers; the result's last write must be held across them."""
    import levain.consent.sign as sign

    a, b = cockpit["signer"], _phone(cockpit)
    pk = cockpit["pk"]
    a_ch = _phone_challenge(a, cockpit, "a")
    b_ch = _phone_challenge(b, cockpit, "b")
    a_linking, b_writing, a_linked = threading.Event(), threading.Event(), threading.Event()
    real_new, real_over = sign._write_new, sign._write_over

    def write_new(path: Path, text: str) -> None:
        if threading.current_thread().name == "A" and path.name.endswith(".signed.json"):
            a_linking.set()
            b_writing.wait(3)          # never set once the last write is held across signers: A goes on alone
            try:
                real_new(path, text)
            finally:
                a_linked.set()
            return
        real_new(path, text)

    def write_over(path: Path, text: str) -> None:
        if threading.current_thread().name == "B":
            b_writing.set()
            a_linked.wait(3)
        real_over(path, text)

    monkeypatch.setattr(sign, "_write_new", write_new)
    monkeypatch.setattr(sign, "_write_over", write_over)
    out: dict[str, Any] = {}

    def run(name: str, signer: Signer, ch: str) -> None:
        if name == "B":
            a_linking.wait(10)
        try:
            out[name] = signer.answer(RID, pk.assertion(ch, PINNED))
        except SignRefused as exc:
            out[name] = exc.status

    threads = [threading.Thread(target=run, args=(n, s, ch), name=n) for n, s, ch in (("A", a, a_ch), ("B", b, b_ch))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert out["A"]["choice"] == "a" and out["B"] == 409, out
    assert json.loads((cockpit["qdir"] / "sign" / f"{RID}.signed.json").read_text())["choice"] == "a"
    assert _verifies(cockpit)


# --- D3: the https name a reverse proxy forwards ----------------------------------------------------------------


def test_an_extra_host_is_admitted_with_or_without_its_port_and_nothing_else_is(tmp_path: Path) -> None:
    """tailscale serve forwards the browser's own Host (measured 10-23 against ``tailscale serve --https=8447``: Host
    and X-Forwarded-Host were ``<name>:8447``, Origin unchanged), so the name must pass with its port; the gate
    compares host names only, as it does for loopback. The ENTRY is a bare name: the port is the request's, never the
    caller's (a port in an entry is refused below)."""
    name = "missinglayer.tail1c8687.ts.net"
    with _serving(tmp_path, extra_hosts=[name.upper() + ".", "other.example"]) as (url, httpd):
        assert {name, "other.example"} <= httpd.allowed_hosts
        for host in (name, f"{name}:8443", f"{name.upper()}:8443", "other.example", "other.example:8443"):
            assert _call(url, "/", host=host)[0] == 200, host
        for host in ("evil.example", f"{name}.evil.example", f"x{name}", f"{name}:x", ""):
            assert _call(url, "/", host=host)[0] == 403, host
    with _serving(tmp_path) as (url, _httpd):
        assert _call(url, "/", host=f"{name}:8443")[0] == 403      # not admitted by default


@pytest.mark.parametrize("junk", ["https://box.example", "box.example/", "box.example/x", "*.box.example",
                                  "box example", "", "box.example:99999", "box.example:0", "box.example:x",
                                  "box.example:", "box.example:8443", " box.example", "[::1]", "u@box.example",
                                  "box..example", "-box.example", 7, None])
def test_junk_extra_hosts_are_refused_before_binding(tmp_path: Path, junk: Any) -> None:
    with pytest.raises(ValueError, match="extra_hosts"):
        make_server(_source(tmp_path), host="127.0.0.1", port=0, write_token=TOK, extra_hosts=[junk])


def test_a_bare_string_is_not_a_list_of_hosts(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="extra_hosts"):
        make_server(_source(tmp_path), host="127.0.0.1", port=0, write_token=TOK, extra_hosts="box.example")


def test_an_extra_host_relaxes_no_off_box_bind_rule(tmp_path: Path) -> None:
    """A writable source still refuses a non-loopback bind without a write token, extra hosts or not (the refusal is
    before the bind, so no tailnet address is needed here)."""
    import dataclasses

    src = _source(tmp_path)
    no_install = dataclasses.replace(src, write_scope=dataclasses.replace(src.write_scope, install_root=None))
    with pytest.raises(ValueError, match="ONLY with a write_token"):
        make_server(no_install, host="100.64.0.7", port=0, extra_hosts=[RP])
    with pytest.raises(ValueError, match="install-bearing substrate is loopback-only"):
        make_server(src, host="100.64.0.7", port=0, write_token=TOK, extra_hosts=[RP])


def test_a_request_through_an_extra_host_is_off_box_for_the_read_gate(tmp_path: Path) -> None:
    """A proxy on this machine (tailscale serve) delivers every tailnet node's request from 127.0.0.1, so the bound
    address cannot tell them from a local one; the Host can. A request naming an extra host meets the off-box read
    gate exactly as an off-box bind does: no token, no substrate, manifest or panel. The same server still serves
    them token-free to a loopback Host, and the app shell stays token-free to both."""
    name = "missinglayer.tail1c8687.ts.net"
    with _serving(tmp_path, extra_hosts=[name]) as (url, httpd):
        assert httpd.is_loopback_bind
        st, manifest = _call(url, "/cockpit/manifest.json", token=None, host="127.0.0.1")
        assert st == 200, manifest
        panel = "/cockpit/panel/" + next(iter(manifest["panels"])) + ".json"
        for path in ("/substrate.json", "/cockpit/manifest.json", panel):
            assert _call(url, path, token=None, host="127.0.0.1")[0] == 200, path
            for host in (name, f"{name}:8443", f"{name.upper()}.:8443"):
                st, body = _call(url, path, token=None, host=host)
                assert st == 403 and "token" in body["message"], (path, host, st)
                assert _call(url, path, token="wrong", host=host)[0] == 403, (path, host)
                assert _call(url, path, host=host)[0] == 403, (path, host)         # the owner key opens nothing proxied
                assert _call(url, path, token=FC, host=host)[0] == 200, (path, host)
        assert _call(url, "/", token=None, host=name)[0] == 200
        st, sub = _call(url, "/substrate.json", token=FC, host=name)
        assert sub["write_token_required"] is True           # the page keeps sending the token through the proxy


def test_a_signer_without_a_valid_page_origin_is_refused_before_binding(tmp_path: Path) -> None:
    pk = _Passkey()
    for origin in (None, f"http://{RP}:8443", "https://other.example:8443", f"https://{RP}:8443/"):
        signer = Signer(tmp_path / "q", (NS,), RP, lambda: {pk.key.fingerprint: pk.key})
        signer.page_origin = origin
        with pytest.raises(ValueError, match="page_origin"):
            make_server(_source(tmp_path), host="127.0.0.1", port=0, write_token=TOK, signer=signer)
    with pytest.raises(ValueError, match="signer"):
        make_server(_source(tmp_path), host="127.0.0.1", port=0, write_token=TOK, signer=object())


# --- L3 r1 on cafcac36 (item 5) ---------------------------------------------------------------------------------


def test_a_signer_on_a_server_with_no_token_to_carry_is_refused_before_binding(tmp_path: Path) -> None:
    """codex MED: every sign route needs a surface token, so a signer on a server that has none (a read-only source
    given no write_token and no surface_tokens) would be a permanent 403. It is refused, saying why."""
    pk = _Passkey()
    signer = Signer(tmp_path / "q", (NS,), RP, lambda: {pk.key.fingerprint: pk.key})
    signer.page_origin = PINNED
    read_only = SubstrateSource(anneal=AnnealPaths.from_db(tmp_path / "memory.db"), write_scope=None)
    with pytest.raises(ValueError, match="refusing signer: .*no surface token"):
        make_server(read_only, host="127.0.0.1", port=0, signer=signer)
    httpd = make_server(read_only, host="127.0.0.1", port=0, signer=signer, surface_tokens={"browser": TOK})
    httpd.server_close()                                   # a token given: served


def test_a_page_origin_cannot_move_once_a_server_holds_the_signer(cockpit) -> None:
    """codex L3 r2 item 2: the Signer's page_origin is pinned when make_server takes it (the one source of truth: the
    Signer verifies against it, and nothing can change it under a running ceremony). A later set is refused, the
    pin keeps verifying, and setting the same value again is harmless."""
    signer, pk = cockpit["signer"], cockpit["pk"]
    status, ch = _challenge(cockpit, "a")
    assert status == 200
    for moved in (f"https://{RP}:9999", "https://evil.example", None):
        with pytest.raises(ValueError, match="page_origin"):
            signer.page_origin = moved
        assert signer.page_origin == PINNED
    signer.page_origin = PINNED                            # the same page: no change, no refusal
    status, out = _call(cockpit["url"], f"/cockpit/sign/{RID}/answer", pk.assertion(ch["challenge"], f"https://{RP}:9999"))
    assert status == 422 and _no_result(cockpit), out      # the pin, not a moved origin, judged the assertion
    status, ch = _challenge(cockpit, "a")
    assert _call(cockpit["url"], f"/cockpit/sign/{RID}/answer", pk.assertion(ch["challenge"], PINNED))[0] == 200
    assert _verifies(cockpit)


def test_a_refused_signer_is_not_pinned_and_a_pinned_one_is_checked_again(tmp_path: Path) -> None:
    """make_server pins only a page_origin it accepted: one refused before binding can still be corrected. A Signer
    already pinned by one server is checked again by the next, and pins nothing new."""
    pk = _Passkey()
    signer = Signer(tmp_path / "q", (NS,), RP, lambda: {pk.key.fingerprint: pk.key})
    signer.page_origin = f"http://{RP}:8443"
    with pytest.raises(ValueError, match="page_origin"):
        make_server(_source(tmp_path), host="127.0.0.1", port=0, write_token=TOK, signer=signer)
    signer.page_origin = PINNED
    httpd = make_server(_source(tmp_path), host="127.0.0.1", port=0, write_token=TOK, signer=signer)
    httpd.server_close()
    with pytest.raises(ValueError, match="page_origin"):
        signer.page_origin = f"https://{RP}"
    httpd = make_server(_source(tmp_path), host="127.0.0.1", port=0, write_token=TOK, signer=signer)
    httpd.server_close()
    assert signer.page_origin == PINNED


def test_a_held_or_failing_queue_lock_is_a_503_and_never_an_unlocked_write(cockpit, monkeypatch) -> None:
    """complement LOW 1 + codex LOW: the queue lock is polled for a bounded wait, then a 503; flock's own OSError
    (ENOLCK, ENOTSUP) is a 503 too; the lock's descriptor is closed on every path."""
    import errno
    import fcntl
    import os
    import time

    from levain.consent import sign

    monkeypatch.setattr(sign, "LOCK_WAIT_S", 0.3)
    (cockpit["qdir"] / "sign").mkdir(exist_ok=True)
    holder = os.open(cockpit["qdir"] / "sign" / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        t0 = time.monotonic()
        status, out = _call(cockpit["url"], f"/cockpit/sign/{RID}/deny", {"request_sha256": _sha(cockpit)})
        waited = time.monotonic() - t0
        assert status == 503 and "lock" in out["message"], out
        assert 0.25 <= waited < 5, waited
        assert not (cockpit["qdir"] / "sign" / f"{RID}.denied").exists()
    finally:
        os.close(holder)

    opened: list[int] = []
    closed: list[int] = []
    real_open, real_close = os.open, os.close

    def track_open(*a: Any, **k: Any) -> int:
        fd = real_open(*a, **k)
        if str(a[0]).endswith(".lock"):
            opened.append(fd)
        return fd

    def track_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    def no_locks(fd: int, op: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(sign.os, "open", track_open)
    monkeypatch.setattr(sign.os, "close", track_close)
    monkeypatch.setattr(sign.fcntl, "flock", no_locks)
    status, out = _call(cockpit["url"], f"/cockpit/sign/{RID}/deny", {"request_sha256": _sha(cockpit)})
    assert status == 503 and "No locks available" in out["message"], out
    assert opened and set(opened) <= set(closed)
    assert not (cockpit["qdir"] / "sign" / f"{RID}.denied").exists()
    monkeypatch.undo()
    status, _ = _call(cockpit["url"], f"/cockpit/sign/{RID}/deny", {"request_sha256": _sha(cockpit)})
    assert status == 200 and (cockpit["qdir"] / "sign" / f"{RID}.denied").exists()


def test_extra_hosts_with_a_chat_host_is_refused_before_binding(tmp_path: Path) -> None:
    """Head ruling on S1b Q1: a chat host is loopback-only (it drives an entity's hands with no off-box factor), and
    extra_hosts puts a loopback server on the tailnet through a proxy, so the two together are refused. An
    install-bearing source with extra_hosts stays allowed (flow's cockpit shape; the off-box read gate covers it)."""
    from tests.test_chat import _Factory, _host
    from tests.test_chat import _source as _chat_source

    with pytest.raises(ValueError, match="extra_hosts.*chat"):
        make_server(_chat_source(tmp_path), host="127.0.0.1", port=0, chat_host=_host(tmp_path, _Factory([])),
                    extra_hosts=[RP])
    httpd = make_server(_chat_source(tmp_path), host="127.0.0.1", port=0, chat_host=_host(tmp_path, _Factory([])),
                        extra_hosts=[])
    httpd.server_close()                                   # an empty list names no proxy
    with _serving(tmp_path / "inst", extra_hosts=[RP]) as (_url, httpd):
        assert httpd.levain_source.write_scope.install_root is not None and RP in httpd.proxied_hosts


# --- L3 r2 on 0867f2c0: ONE off-box rule (item 1), redundant extra hosts (item 3) ---------------------------------

_PROXY_HEADERS = ({"X-Forwarded-For": "100.101.102.103"}, {"X-Forwarded-Host": f"{RP}:8447"},
                  {"Forwarded": "for=100.101.102.103"}, {"Tailscale-User-Login": "someone@example.com"},
                  {"tailscale-user-name": "Someone"})


def test_a_proxy_header_makes_a_loopback_host_off_box_for_every_route(cockpit) -> None:
    """complement MED (L3 r2): a tailnet peer through tailscale serve can send ``Host: 127.0.0.1``, which the Host gate
    admits and the bind cannot tell from a local request. A proxy header says it came through a proxy: every route
    then needs the token, reads included (tailscale serve adds X-Forwarded-Host and Tailscale-User-*, measured
    10-23). Plain loopback with no proxy header keeps token-free reads."""
    url = cockpit["url"]
    st, manifest = _call(url, "/cockpit/manifest.json", token=None, host="127.0.0.1")
    assert st == 200, manifest
    panel = "/cockpit/panel/" + next(iter(manifest["panels"])) + ".json"
    reads = ("/substrate.json", "/cockpit/manifest.json", panel, "/cockpit/sign.json")
    posts = {f"/cockpit/sign/{RID}/challenge": {"signer_fp": cockpit["pk"].key.fingerprint, "request_sha256": "x"},
             f"/cockpit/sign/{RID}/answer": {"authenticator_data": "", "client_data_json": "", "signature": ""},
             f"/cockpit/sign/{RID}/deny": {"request_sha256": "x"}}
    for proxy in _PROXY_HEADERS:
        for host in ("127.0.0.1", "localhost", f"127.0.0.1:{url.rsplit(':', 1)[1]}"):
            for path in reads:
                st, body = _call(url, path, token=None, host=host, extra=proxy)
                assert st == 403 and "token" in body["message"], (proxy, host, path, st, body)
                assert _call(url, path, host=host, extra=proxy)[0] == 403, (proxy, host, path)   # owner key: refused proxied
                assert _call(url, path, token=FC, host=host, extra=proxy)[0] == 200, (proxy, host, path)
            for path, req in posts.items():
                assert _call(url, path, req, token=None, host=host, extra=proxy)[0] == 403, (proxy, host, path)
                assert _call(url, path, req, host=host, extra=proxy)[0] == 403, (proxy, host, path)
                assert _call(url, path, req, token=FC, host=host, extra=proxy)[0] != 403, (proxy, host, path)
    for path in reads[:3]:
        assert _call(url, path, token=None, host="127.0.0.1")[0] == 200, path      # today's local reads
    st, sub = _call(url, "/substrate.json", token=FC, extra=_PROXY_HEADERS[0])
    assert sub["write_token_required"] is True


def _read_only(root: Path) -> SubstrateSource:
    return SubstrateSource(anneal=AnnealPaths.from_db(root / "memory.db"), write_scope=None)


def test_a_read_only_source_through_an_extra_host_needs_the_token(tmp_path: Path) -> None:
    """codex item 7 (L3 r2): off-box is off-box whether or not the source is writable. A read-only source served
    through an extra host (or with a proxy header) needs the surface token on every route, reads included."""
    httpd = make_server(_read_only(tmp_path), host="127.0.0.1", port=0,
                        surface_tokens={"browser": TOK, "flowconnect": FC}, extra_hosts=[RP])
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        for path in ("/substrate.json", "/cockpit/manifest.json"):
            assert _call(url, path, token=None, host="127.0.0.1")[0] == 200, path       # loopback: as today
            for host, extra in ((RP, None), (f"{RP}:8447", None), ("127.0.0.1", _PROXY_HEADERS[0])):
                st, body = _call(url, path, token=None, host=host, extra=extra)
                assert st == 403 and "token" in body["message"], (path, host, st, body)
                assert _call(url, path, host=host, extra=extra)[0] == 403, (path, host)   # the owner key: refused proxied
                assert _call(url, path, token=FC, host=host, extra=extra)[0] == 200, (path, host)
        # a write route on a read-only source: the token first off-box, then its own read-only refusal
        assert _call(url, "/edit", {}, token=None, host=RP)[0] == 403
        assert _call(url, "/edit", {}, token=FC, host=RP)[0] == 422
        assert _call(url, "/", token=None, host=RP)[0] == 200                         # the app shell stays open
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_the_read_only_mesh_carve_out_does_not_reach_a_proxied_request(tmp_path: Path) -> None:
    """Head ruling on Q1 (L3 r2, spore-220): a read-only no-install source bound off-loopback stays token-free for
    reads (iPad viewing, test_web_server's carve-out test), but the carve-out is for the bare bind only. The same
    source given extra_hosts and no surface token is refused before binding: every proxied request needs one, so
    that server would answer every proxied request 403. With a token it binds; with no proxy named it binds as today."""
    with pytest.raises(ValueError, match="extra_hosts.*no surface token"):
        make_server(_read_only(tmp_path), host="127.0.0.1", port=0, extra_hosts=[RP])
    httpd = make_server(_read_only(tmp_path), host="127.0.0.1", port=0, extra_hosts=[RP],
                        surface_tokens={"browser": TOK})
    httpd.server_close()
    httpd = make_server(_read_only(tmp_path), host="127.0.0.1", port=0)
    assert httpd.surface_tokens == {}
    httpd.server_close()                                   # loopback, no proxy named: served as today


def test_a_proxied_request_to_a_read_only_mesh_bind_needs_the_token(tmp_path: Path) -> None:
    """The bare read-only mesh bind is token-free (spore-220); a request to it carrying a proxy header is not."""
    httpd = make_server(_read_only(tmp_path), host="127.0.0.1", port=0,
                        surface_tokens={"browser": TOK, "flowconnect": FC})
    httpd.is_loopback_bind = False                          # what a real tailnet bind sets (no mesh interface here)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        assert _call(url, "/substrate.json", token=None)[0] == 200                     # the carve-out, as today
        assert _call(url, "/edit", {}, token=None)[0] == 422                            # its honest read_only refusal
        for proxy in _PROXY_HEADERS:
            assert _call(url, "/substrate.json", token=None, extra=proxy)[0] == 403, proxy
            assert _call(url, "/substrate.json", extra=proxy)[0] == 403, proxy             # the owner key, off-box
            assert _call(url, "/substrate.json", token=FC, extra=proxy)[0] == 200, proxy
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("name", ["localhost", "LOCALHOST.", "127.0.0.1"])
def test_a_loopback_name_in_extra_hosts_is_refused_as_redundant(tmp_path: Path, name: str) -> None:
    """codex LOW (L3 r2): a loopback name in extra_hosts would turn every ordinary local request into an off-box one.
    The gate already admits it, so it is refused before binding as redundant. (``::1`` is refused by the name rule.)"""
    with pytest.raises(ValueError, match="extra_hosts.*already"):
        make_server(_source(tmp_path), host="127.0.0.1", port=0, write_token=TOK, extra_hosts=[name])


def test_the_bound_name_in_extra_hosts_is_refused_as_redundant(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="extra_hosts.*already"):
        make_server(_source(tmp_path), host="localhost", port=0, write_token=TOK, extra_hosts=["localhost"])
    import dataclasses

    src = _source(tmp_path)
    no_install = dataclasses.replace(src, install_root=None,
                                     write_scope=dataclasses.replace(src.write_scope, install_root=None))
    with pytest.raises(ValueError, match="extra_hosts.*already"):
        make_server(no_install, host="100.64.0.7", port=0, write_token=TOK, extra_hosts=["100.64.0.7"])
