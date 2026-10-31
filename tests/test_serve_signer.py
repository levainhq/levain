"""OS-5b (levain project_memory/1030-59-os5b/BRIEF.md): ``levain serve --sign-queue-dir DIR --sign-host NAME
[--sign-port PORT]`` hosts the cockpit's own passkey signing pages by building a Signer for the proxied https name.
Each test names the brief rule it pins. The real-request test runs ``run_web_server`` itself on a bound port 0 over a
real install and asks the running server; a real phone ceremony through a real proxy is not run here."""
from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from typing import Any, Iterator

import pytest

import levain.web_server as ws
from levain.cli import main
from levain.consent.sign import Signer
from tests.test_cockpit_k2a import _install

HOST = "box.example"


class _NoWatcher:
    """``serve --write`` supervises the coordinator over the default queue; these tests do not."""

    def __init__(self, *_a: Any, **_k: Any) -> None:
        pass

    def start(self) -> "_NoWatcher":
        return self

    def stop(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    import levain.coordinator.supervise as supervise

    monkeypatch.setattr(supervise, "Watcher", _NoWatcher)
    monkeypatch.setattr(supervise, "default_store", lambda directory=None: None)
    monkeypatch.setattr(ws, "_open_browser", lambda *a, **k: None)


@contextmanager
def _running(root: Path, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> Iterator[tuple[Any, dict[str, Any], list[int]]]:
    """``run_web_server`` on its own thread over ``root`` (port 0), a spy on ``make_server`` recording what it was
    given. Yields (the bound server, the recorded keywords, the return code once the serve ends)."""
    real = ws.make_server
    seen: dict[str, Any] = {}
    made = threading.Event()
    rc: list[int] = []

    def spy(*a: Any, **k: Any) -> Any:
        seen.update(k)
        httpd = real(*a, **k)
        seen["httpd"] = httpd
        made.set()
        return httpd

    monkeypatch.setattr(ws, "make_server", spy)
    t = threading.Thread(target=lambda: rc.append(ws.run_web_server(root, port=0, open_browser=False, **kw)), daemon=True)
    t.start()
    assert made.wait(30), "the server never bound"
    httpd = seen.pop("httpd")
    try:
        yield httpd, seen, rc
    finally:
        httpd.shutdown()
        t.join(timeout=15)
        assert not t.is_alive()


def _get(httpd: Any, path: str, *, host: "str | None" = None, token: "str | None" = None) -> "tuple[int, Any]":
    headers = {}
    if host is not None:
        headers["Host"] = host
    if token is not None:
        headers["X-Levain-Write-Token"] = token
    req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 - loopback only
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


# --- rule 1: the three options and their pairing (argparse level) ------------------------------------------------


def test_the_three_flags_parse_and_reach_the_serve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    got: dict[str, Any] = {}
    monkeypatch.setattr(ws, "run_web_server", lambda path, **k: got.update(k) or 0)
    q = tmp_path / "q"
    assert main(["serve", "--path", str(tmp_path), "--sign-queue-dir", str(q), "--sign-host", HOST,
                 "--sign-port", "8443"]) == 0
    assert (got["sign_queue_dir"], got["sign_host"], got["sign_port"]) == (q, HOST, 8443)
    got.clear()
    assert main(["serve", "--path", str(tmp_path), "--sign-queue-dir", str(q), "--sign-host", HOST]) == 0
    assert got["sign_port"] == 443          # the default: the origin leaves the port out
    got.clear()
    assert main(["serve", "--path", str(tmp_path)]) == 0
    assert (got["sign_queue_dir"], got["sign_host"], got["sign_port"]) == (None, None, 443)


@pytest.mark.parametrize("argv, words", [
    (["--sign-queue-dir", "q"], "go together"),
    (["--sign-host", HOST], "go together"),
    (["--sign-port", "8443"], "--sign-port needs --sign-host"),
    (["--sign-queue-dir", "q", "--sign-port", "8443"], "go together"),
    (["--sign-host", HOST, "--sign-port", "0"], "port from 1 to 65535"),
    (["--sign-host", HOST, "--sign-queue-dir", "q", "--sign-port", "99999"], "port from 1 to 65535"),
])
def test_an_illegal_combination_exits_2_in_plain_words(argv: list[str], words: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws, "run_web_server", lambda *a, **k: pytest.fail("the serve must not start"))
    err = io.StringIO()
    with redirect_stderr(err), pytest.raises(SystemExit) as exc:
        main(["serve", *argv])
    assert exc.value.code == 2 and words in err.getvalue()


# --- rule 2, 3: the Signer run_web_server builds, against a real bound server ------------------------------------


@pytest.mark.parametrize("port, origin", [(443, f"https://{HOST}"), (8443, f"https://{HOST}:8443")])
def test_the_signer_make_server_is_given(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, port: int, origin: str) -> None:
    root = _install(tmp_path)[0]
    q = tmp_path / "signq"
    with _running(root, monkeypatch, write=True, sign_queue_dir=q, sign_host=HOST, sign_port=port) as (httpd, seen, _rc):
        signer = seen["signer"]
        assert isinstance(signer, Signer)
        assert signer.rp_id == HOST and signer.page_origin == origin
        assert signer.namespaces == () and signer.queue_dir == q.resolve()
        assert seen["extra_hosts"] == frozenset({HOST})
        assert httpd.signer is signer and httpd.signer_page_origin == origin


def test_without_the_flags_make_server_gets_no_signer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _install(tmp_path)[0]
    with _running(root, monkeypatch, write=True) as (_httpd, seen, _rc):
        assert "signer" not in seen and "extra_hosts" not in seen      # not passed at all: the call is today's


# --- rule 4 + the REAL request ------------------------------------------------------------------------------------


def test_a_real_request_for_the_signing_listing_and_the_startup_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                     capsys: pytest.CaptureFixture[str]) -> None:
    """Through the proxied name the owner key opens nothing (W1b L-1), so the request carries a PAIRED device's token,
    the credential the phone holds; the listing is the Signer's own, behind the same gate as every sign route."""
    from tests.test_cockpit_pairing_http import _pair

    root = _install(tmp_path)[0]
    q = tmp_path / "signq"
    with _running(root, monkeypatch, write=True, install_class="reported", sign_queue_dir=q, sign_host=HOST,
                  sign_port=8443) as (httpd, _seen, _rc):
        owner = httpd.write_token
        assert owner
        _device, paired = _pair(httpd.server_address[1], token=owner)
        status, body = _get(httpd, "/cockpit/sign.json", host=f"{HOST}:8443", token=paired)
        assert status == 200, body
        assert body["origin"] == f"https://{HOST}:8443"            # the listing carries origin; rp_id rides the one-request payloads
        assert {"requests", "signers", "credentials", "origin"} <= set(body)
        assert "no consent hook names a cockpit" in body["note"]     # a bare temp queue: honest about why none can sign
        assert body["requests"] == [] and body["signers"] == []       # an empty queue, no passkey enrolled
        # the gate is unchanged: no token, or the owner key through the proxied name, is refused
        assert _get(httpd, "/cockpit/sign.json", host=f"{HOST}:8443")[0] == 403
        assert _get(httpd, "/cockpit/sign.json", host=f"{HOST}:8443", token=owner)[0] == 403
        # and the owner key on the loopback page reads the same listing
        assert _get(httpd, "/cockpit/sign.json", token=owner)[0] == 200
    out = capsys.readouterr().out
    assert f"  signing: https://{HOST}:8443 (queue {q.resolve()})\n" in out


def test_without_the_flags_the_same_route_is_404_and_the_output_has_no_signing_line(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    root = _install(tmp_path)[0]
    with _running(root, monkeypatch, write=True) as (httpd, _seen, _rc):
        assert _get(httpd, "/cockpit/sign.json", token=httpd.write_token)[0] == 404
    assert "signing:" not in capsys.readouterr().out


def test_no_file_is_made_in_the_queue_by_starting_the_signing_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _install(tmp_path)[0]
    q = tmp_path / "signq"
    q.mkdir(mode=0o700)
    with _running(root, monkeypatch, write=True, sign_queue_dir=q, sign_host=HOST):
        pass
    assert sorted(p.name for p in q.rglob("*")) == []


# --- rule 2: make_server's own refusals reach stderr and return 1 -------------------------------------------------


def test_chat_beside_a_sign_host_is_refused_with_make_servers_words(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                           capsys: pytest.CaptureFixture[str]) -> None:
    root = _install(tmp_path)[0]
    entity = tmp_path / "chat-entity"      # never opened: _build_chat_host is stubbed below
    sentinel = object()
    monkeypatch.setattr(ws, "_build_chat_host", lambda *a, **k: (sentinel, ""))
    # entity validation is not what is under test: a built chat host stands in, and make_server's own check refuses it
    rc = ws.run_web_server(root, port=0, open_browser=False, write=True, chat=[entity],
                           sign_queue_dir=tmp_path / "signq", sign_host=HOST)
    assert rc == 1
    assert "refusing extra_hosts with --chat" in capsys.readouterr().err


def test_a_read_only_serve_with_a_sign_host_is_make_servers_refusal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No --write means no write token, and make_server refuses a proxied name with none: not loosened here."""
    root = _install(tmp_path)[0]
    rc = ws.run_web_server(root, port=0, open_browser=False, write=False,
                           sign_queue_dir=tmp_path / "signq", sign_host=HOST)
    assert rc == 1
    assert "refusing extra_hosts" in capsys.readouterr().err


def test_a_name_make_server_cannot_take_is_its_refusal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _install(tmp_path)[0]
    rc = ws.run_web_server(root, port=0, open_browser=False, write=True,
                           sign_queue_dir=tmp_path / "signq", sign_host="not a host name")
    assert rc == 1 and "refusing" in capsys.readouterr().err
