"""OS-5b (levain project_memory/1030-59-os5b/BRIEF.md, FIXBRIEF_r1.md): ``levain serve --sign-queue-dir DIR --sign-host
NAME`` hosts the cockpit's own passkey signing pages by building a Signer for the proxied https name, always on
``https://NAME`` (the proxy serves 443). Each test names the rule it pins. The real-request tests run
``run_web_server`` itself on a bound port 0 over a real install and ask the running server; a real phone ceremony
through a real proxy is not run here."""
from __future__ import annotations

import dataclasses
import io
import json
import os
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from typing import Any, Iterator

import pytest

import levain.consent.sign as sign_mod
import levain.web_server as ws
from levain.cli import main
from levain.consent.sign import Signer
from tests.test_cockpit_k2a import _install

HOST = "box.example"
SHEETS_ON = "consent and grant sheets on"
SHEETS_OFF = "consent and grant sheets off: this queue is not the one the cockpit's consent cards are read from"
NAME_WORDS = "--sign-host is the name as the browser address bar shows it: lowercase, no trailing dot, no port"


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


@pytest.fixture
def settled(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every queue a Signer was built over: its constructor settles orphaned attempts there, a write to the queue."""
    calls: list[Path] = []
    real = sign_mod.settle_orphan_attempts

    def spy(queue_dir: Path) -> int:
        calls.append(Path(queue_dir))
        return real(queue_dir)

    monkeypatch.setattr(sign_mod, "settle_orphan_attempts", spy)
    return calls


@pytest.fixture
def no_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal that must come before make_server: reaching it fails the test instead of serving forever."""
    monkeypatch.setattr(ws, "make_server", lambda *a, **k: pytest.fail("make_server must not be reached"))


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


def _queue(tmp_path: Path, name: str = "signq") -> Path:
    q = tmp_path / name
    q.mkdir(mode=0o700)
    return q


def _default_queue() -> Path:
    """The queue the cockpit's consent cards are read from: ``$LEVAIN_HOME/consent`` (conftest sets a fresh home)."""
    from levain.consent.queue import default_queue_dir

    q = default_queue_dir()
    q.mkdir(parents=True, mode=0o700)
    return q


# --- the two options and their pairing (argparse level); no port option --------------------------------------------


def test_the_two_flags_parse_and_reach_the_serve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    got: dict[str, Any] = {}
    monkeypatch.setattr(ws, "run_web_server", lambda path, **k: got.update(k) or 0)
    q = tmp_path / "q"
    assert main(["serve", "--path", str(tmp_path), "--sign-queue-dir", str(q), "--sign-host", HOST]) == 0
    assert (got["sign_queue_dir"], got["sign_host"]) == (q, HOST)
    assert "sign_port" not in got
    got.clear()
    assert main(["serve", "--path", str(tmp_path)]) == 0
    assert (got["sign_queue_dir"], got["sign_host"]) == (None, None)


@pytest.mark.parametrize("argv, words", [
    (["--sign-queue-dir", "q"], "go together"),
    (["--sign-host", HOST], "go together"),
    # r1 finding 1: a non-443 origin makes every consent/dispatch sheet refuse while the listing still lists, so
    # the option is gone: the origin is always https://NAME
    (["--sign-queue-dir", "q", "--sign-host", HOST, "--sign-port", "8443"], "unrecognized arguments: --sign-port"),
])
def test_an_illegal_combination_exits_2_in_plain_words(argv: list[str], words: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws, "run_web_server", lambda *a, **k: pytest.fail("the serve must not start"))
    err = io.StringIO()
    with redirect_stderr(err), pytest.raises(SystemExit) as exc:
        main(["serve", *argv])
    assert exc.value.code == 2 and words in err.getvalue()


def test_the_readme_example_parses_as_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """r1 finding 4: the README's serve line is what the code accepts, with the install class pairing needs (without
    it a cockpit with no governed install marker answers pairing with install_disabled)."""
    import shlex

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    lines = [ln for ln in readme.splitlines() if ln.startswith("levain serve ") and "--sign-host" in ln]
    assert len(lines) == 1, lines
    line = lines[0].replace("<levain home>", str(tmp_path)).replace("<name>", HOST)
    got: dict[str, Any] = {}
    monkeypatch.setattr(ws, "run_web_server", lambda path, **k: got.update(k) or 0)
    assert main(shlex.split(line)[1:] + ["--path", str(tmp_path)]) == 0
    assert got["write"] is True and got["install_class"] == "reported"
    assert (got["sign_queue_dir"], got["sign_host"]) == (tmp_path / "consent", HOST)
    assert "tailscale serve --bg --https=443 http://127.0.0.1:7420" in readme


def test_run_web_server_takes_no_port() -> None:
    import inspect

    assert "sign_port" not in inspect.signature(ws.run_web_server).parameters
    assert "port" not in inspect.signature(ws._serve_signer).parameters


# --- the Signer run_web_server builds, against a real bound server --------------------------------------------------


def test_the_signer_make_server_is_given(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _install(tmp_path)[0]
    q = _queue(tmp_path)
    with _running(root, monkeypatch, write=True, sign_queue_dir=q, sign_host=HOST) as (httpd, seen, _rc):
        signer = seen["signer"]
        assert isinstance(signer, Signer)
        assert signer.rp_id == HOST and signer.page_origin == f"https://{HOST}"
        assert signer.namespaces == () and signer.queue_dir == q.resolve()
        assert seen["extra_hosts"] == frozenset({HOST})
        assert httpd.signer is signer and httpd.signer_page_origin == f"https://{HOST}"
        # the origin every consent/dispatch sheet accepts: a sheet on this server can sign
        assert httpd.dispatch_sheet.elsewhere() is None


def test_without_the_flags_make_server_gets_no_signer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _install(tmp_path)[0]
    with _running(root, monkeypatch, write=True) as (_httpd, seen, _rc):
        assert "signer" not in seen and "extra_hosts" not in seen      # not passed at all: the call is today's


# --- the REAL request and the start-up line ------------------------------------------------------------------------


def test_a_real_request_for_the_signing_listing_and_the_startup_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                     capsys: pytest.CaptureFixture[str]) -> None:
    """Through the proxied name the owner key opens nothing (W1b L-1), so the request carries a PAIRED device's token,
    the credential the phone holds; the listing is the Signer's own, behind the same gate as every sign route."""
    from tests.test_cockpit_pairing_http import _pair

    root = _install(tmp_path)[0]
    q = _queue(tmp_path)
    with _running(root, monkeypatch, write=True, install_class="reported", sign_queue_dir=q,
                  sign_host=HOST) as (httpd, _seen, _rc):
        owner = httpd.write_token
        assert owner
        _device, paired = _pair(httpd.server_address[1], token=owner)
        status, body = _get(httpd, "/cockpit/sign.json", host=HOST, token=paired)
        assert status == 200, body
        assert body["origin"] == f"https://{HOST}"                  # the listing carries origin; rp_id rides the one-request payloads
        assert {"requests", "signers", "credentials", "origin"} <= set(body)
        assert "no consent hook names a cockpit" in body["note"]     # a bare temp queue: honest about why none can sign
        assert body["requests"] == [] and body["signers"] == []       # an empty queue, no passkey enrolled
        # the gate is unchanged: no token, or the owner key through the proxied name, is refused
        assert _get(httpd, "/cockpit/sign.json", host=HOST)[0] == 403
        assert _get(httpd, "/cockpit/sign.json", host=HOST, token=owner)[0] == 403
        # and the owner key on the loopback page reads the same listing
        assert _get(httpd, "/cockpit/sign.json", token=owner)[0] == 200
    out = capsys.readouterr().out
    assert f"  signing: https://{HOST} (queue {q.resolve()}; {SHEETS_OFF})\n" in out


def test_without_the_flags_the_same_route_is_404_and_the_output_has_no_signing_line(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    root = _install(tmp_path)[0]
    with _running(root, monkeypatch, write=True) as (httpd, _seen, _rc):
        assert _get(httpd, "/cockpit/sign.json", token=httpd.write_token)[0] == 404
    assert "signing:" not in capsys.readouterr().out


# r1 finding 3: the signing line says whether the consent family is on, read from the server make_server built


def test_the_signing_line_says_sheets_on_for_the_default_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                               capsys: pytest.CaptureFixture[str]) -> None:
    root = _install(tmp_path)[0]
    q = _default_queue()
    with _running(root, monkeypatch, write=True, sign_queue_dir=q, sign_host=HOST) as (httpd, _seen, _rc):
        assert httpd.consent_sheet is not None and httpd.grant_phone is not None
    out = capsys.readouterr().out
    assert f"  signing: https://{HOST} (queue {q.resolve()}; {SHEETS_ON})\n" in out


def test_the_signing_line_says_sheets_off_for_another_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    root = _install(tmp_path)[0]
    _default_queue()
    q = _queue(tmp_path, "elsewhere")
    with _running(root, monkeypatch, write=True, sign_queue_dir=q, sign_host=HOST) as (httpd, _seen, _rc):
        assert httpd.consent_sheet is None and httpd.grant_phone is None
    out = capsys.readouterr().out
    assert f"  signing: https://{HOST} (queue {q.resolve()}; {SHEETS_OFF})\n" in out


# r1 finding 6: on the RUNNING server, not after the stop withdrew what it published


def test_a_running_signing_server_has_published_only_its_origin_in_the_queue(tmp_path: Path,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    root = _install(tmp_path)[0]
    q = _queue(tmp_path)
    with _running(root, monkeypatch, write=True, sign_queue_dir=q, sign_host=HOST):
        # while up: exactly the published origin (an empty queue has no orphaned attempt to settle)
        assert sorted(p.name for p in q.rglob("*")) == ["cockpit-origin.json"]
        assert json.loads((q / "cockpit-origin.json").read_text()) == {"cockpit_origin": f"https://{HOST}"}
    # stopped: the stop withdrew its own origin and nothing else was ever made
    assert sorted(p.name for p in q.rglob("*")) == []


# --- r1 finding 2: the queue is validated, never created, before anything is built ---------------------------------


def test_a_missing_queue_exits_1_in_one_line_and_is_not_created(tmp_path: Path, settled: list[Path], no_bind: None,
                                                                capsys: pytest.CaptureFixture[str]) -> None:
    root = _install(tmp_path)[0]
    q = tmp_path / "no-such-queue"
    rc = ws.run_web_server(root, port=0, open_browser=False, write=True, sign_queue_dir=q, sign_host=HOST)
    assert rc == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and str(q) in err and "does not exist" in err and "Traceback" not in err
    assert not q.exists() and settled == []


def test_a_missing_queue_through_the_cli_exits_1(tmp_path: Path, no_bind: None, capsys: pytest.CaptureFixture[str]) -> None:
    root = _install(tmp_path)[0]
    q = tmp_path / "no-such-queue"
    assert main(["serve", "--path", str(root), "--write", "--no-open", "--port", "0", "--sign-queue-dir", str(q),
                 "--sign-host", HOST]) == 1
    assert "does not exist" in capsys.readouterr().err and not q.exists()


def test_a_queue_that_is_a_file_exits_1_in_one_line(tmp_path: Path, settled: list[Path], no_bind: None,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    root = _install(tmp_path)[0]
    q = tmp_path / "a-file"
    q.write_text("not a queue\n")
    rc = ws.run_web_server(root, port=0, open_browser=False, write=True, sign_queue_dir=q, sign_host=HOST)
    assert rc == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and str(q) in err and "not a directory" in err
    assert q.read_text() == "not a queue\n" and settled == []


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads any directory")
def test_an_unreadable_queue_exits_1_in_one_line(tmp_path: Path, settled: list[Path], no_bind: None,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    root = _install(tmp_path)[0]
    q = _queue(tmp_path)
    q.chmod(0o000)
    try:
        rc = ws.run_web_server(root, port=0, open_browser=False, write=True, sign_queue_dir=q, sign_host=HOST)
    finally:
        q.chmod(0o700)
    assert rc == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and str(q) in err and "not readable" in err and "Traceback" not in err
    assert sorted(p.name for p in q.rglob("*")) == [] and settled == []


def test_an_os_error_building_the_signer_is_one_line_not_a_traceback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    root = _install(tmp_path)[0]
    q = _queue(tmp_path)

    def refuse(*_a: Any, **_k: Any) -> None:
        raise PermissionError(13, "Permission denied", str(q))

    monkeypatch.setattr(sign_mod, "passkey_key_source", refuse)
    rc = ws.run_web_server(root, port=0, open_browser=False, write=True, sign_queue_dir=q, sign_host=HOST)
    assert rc == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "Permission denied" in err and "bind" not in err


# --- r1 finding 2: --chat or a read-only serve is refused before the Signer touches the queue ----------------------


def _make_servers_words(root: Path, *, chat: bool) -> str:
    """What make_server itself raises for a proxied name beside a chat host / on a read-only source: the early
    refusal must say exactly this."""
    source = ws._resolve_source(root)
    kw: dict[str, Any] = {"chat_host": object()} if chat else {}
    if not chat:
        source = dataclasses.replace(source, write_scope=None)
    with pytest.raises(ValueError) as exc:
        ws.make_server(source, host="127.0.0.1", port=0, extra_hosts=frozenset({HOST}), **kw)
    return str(exc.value)


def test_chat_beside_a_sign_host_is_refused_with_make_servers_words(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                   settled: list[Path],
                                                                   capsys: pytest.CaptureFixture[str]) -> None:
    root = _install(tmp_path)[0]
    q = _queue(tmp_path)
    entity = tmp_path / "chat-entity"      # never opened: _build_chat_host is stubbed below
    sentinel = object()
    monkeypatch.setattr(ws, "_build_chat_host", lambda *a, **k: (sentinel, ""))
    rc = ws.run_web_server(root, port=0, open_browser=False, write=True, chat=[entity], sign_queue_dir=q, sign_host=HOST)
    assert rc == 1
    assert capsys.readouterr().err == _make_servers_words(root, chat=True) + "\n"
    assert settled == [] and not (q / "sign").exists() and sorted(q.iterdir()) == []


def test_a_read_only_serve_with_a_sign_host_is_make_servers_refusal(tmp_path: Path, settled: list[Path],
                                                                   capsys: pytest.CaptureFixture[str]) -> None:
    """No --write means no write token, and make_server refuses a proxied name with none: not loosened here."""
    root = _install(tmp_path)[0]
    q = _queue(tmp_path)
    rc = ws.run_web_server(root, port=0, open_browser=False, write=False, sign_queue_dir=q, sign_host=HOST)
    assert rc == 1
    assert capsys.readouterr().err == _make_servers_words(root, chat=False) + "\n"
    assert settled == [] and not (q / "sign").exists() and sorted(q.iterdir()) == []


# --- r1 finding 5: a name the passkey cannot be made for is refused in plain words, never normalised ----------------


@pytest.mark.parametrize("name", ["Box.Example", "box.example.", f"{HOST}:8443", "not a host name", "10.0.0.1"])
def test_a_name_a_browser_would_not_show_is_refused_in_plain_words(name: str, tmp_path: Path, settled: list[Path],
                                                                   capsys: pytest.CaptureFixture[str]) -> None:
    root = _install(tmp_path)[0]
    q = _queue(tmp_path)
    rc = ws.run_web_server(root, port=0, open_browser=False, write=True, sign_queue_dir=q, sign_host=name)
    assert rc == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and NAME_WORDS in err and repr(name) in err
    assert settled == [] and sorted(q.iterdir()) == []
