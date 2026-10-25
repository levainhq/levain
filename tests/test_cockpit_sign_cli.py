"""Row 12, slice B (project_memory/1015-consent/DESIGN_ROW12B_COCKPIT_SIGN_CLI.md): ``levain cockpit sign``
signs a WAITING cockpit pending from the terminal and the running server decides it. Each test is from the
slice's RUN (``ROW12B_RUN/``): a real ``make_server`` over a real install, real ssh-keygen keys, in a
throwaway directory under the home directory (a signers file is a trust anchor; a world-writable ancestor
is refused)."""

from __future__ import annotations

import json
import shutil
import threading
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest
from anneal_memory.spores import SporeStore

from levain import authority as auth
from levain import cli
from levain.cockpit import keys as cockpit_keys
from levain.cockpit.firejournal import CockpitDB
from levain.cockpit.sign_client import journal, post_signature
from tests.cockpit_authority_rig import RealKey, enrol
from tests.test_cockpit_k2a import _get, _install, _post, _serve

TOKEN = "row12b-token"


from tests.autonomic_confirm_keys import anchor_scratch

@pytest.fixture
def home() -> Iterator[Path]:
    d = anchor_scratch("row12b-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def served(home: Path) -> Iterator[dict[str, Any]]:
    """A reported cockpit served over a real install: a laptop-presence and a display-phone key enrolled,
    ``spore_seed`` raised to T3, so every seed is a ``sign`` pending only a laptop-presence key decides."""
    root, src, _ids = _install(home)
    data_root = src.write_scope.ledger_root / "cockpit"
    with _serve(src, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        _get(f"{url}/cockpit/manifest.json", token=TOKEN)        # the broker starts: cockpit.db exists
        db = CockpitDB(data_root)
        laptop, phone = RealKey(home, "laptop"), RealKey(home, "phone")
        enrol(db, laptop, "laptop-presence")
        enrol(db, phone, "display-phone", by=laptop)
        cockpit_keys.policy_raise(data_root, "spore_seed", "T3", signer_fp=laptop.fingerprint,
                                  sign=laptop.sign_link, clock=lambda: datetime.now(timezone.utc))
        yield {"url": url, "data_root": data_root, "laptop": laptop, "phone": phone,
               "spores": root / ".levain" / "memory.spores.json"}


def _issue(url: str, text: str) -> str:
    status, out = _post(f"{url}/cockpit/verb", {
        "verb": "spore_seed", "panel_id": "tray", "params": {"text": text, "type": "thought", "disposition": "seed"},
        "intent": "sign", "idempotency_key": text, "tier_seen": "T3", "gesture_seen": "laptop-only"}, token=TOKEN)
    assert (status, out.get("state"), out.get("tier")) == (200, "WAITING", "T3"), out
    return out["pending_id"]


def _sign(s: dict[str, Any], pid: str, key: RealKey, url: str | None = None) -> int:
    return cli.main(["cockpit", "sign", pid, "--signing-key", str(key.path), "--data-root", str(s["data_root"]),
                     "--url", url or s["url"]])


def _state(s: dict[str, Any], pid: str) -> str:
    return CockpitDB(s["data_root"]).load(pid).state


def _watch_signer(monkeypatch) -> list[bytes]:
    """The messages ``ssh_signer``'s sign callable is handed: empty means nothing was signed."""
    signed: list[bytes] = []
    real = cockpit_keys.ssh_signer

    def watched(*a: Any, **kw: Any):
        fp, sign = real(*a, **kw)
        return fp, lambda m: signed.append(m) or sign(m)

    monkeypatch.setattr(cockpit_keys, "ssh_signer", watched)
    return signed


@pytest.fixture
def listener() -> Iterator[Any]:
    """Start loopback HTTP listeners that record every request and give one canned answer:
    ``listener(status, body, headers)`` returns ``(url, seen)``."""
    started: list[ThreadingHTTPServer] = []

    def start(status: int, body: dict[str, Any], headers: dict[str, str] | None = None):
        seen: list[dict[str, Any]] = []

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                seen.append({"path": self.path, "headers": dict(self.headers)})
                raw = json.dumps(body).encode()
                self.send_response(status)
                for k, v in {"Content-Type": "application/json", **(headers or {})}.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = _answer

            def log_message(self, *a: Any) -> None:
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append(httpd)
        return f"http://127.0.0.1:{httpd.server_port}", seen

    try:
        yield start
    finally:
        for httpd in started:
            httpd.shutdown()
            httpd.server_close()


def test_a_default_namespace_signature_is_refused_and_the_cli_signs_the_same_pending(served, monkeypatch, capsys):
    """RUN 15 (probe, bad_signature, unspent) then RUN 16: ssh_signer's default namespace is the authority
    log's, so a CLI that forgot ``namespace=COCKPIT_NAMESPACE`` would sign every pending in vain."""
    s = served
    pid = _issue(s["url"], "signed from the terminal")
    view = journal(s["data_root"]).load(pid)
    probe = post_signature(s["url"], pid, s["laptop"].sign(view.record_bytes, namespace=auth.NAMESPACE), TOKEN)
    assert (probe.status, (probe.body or {}).get("error")) == (400, "bad_signature"), probe.raw
    assert _state(s, pid) == "WAITING"

    monkeypatch.setenv("LEVAIN_WRITE_TOKEN", TOKEN)
    rc, captured = _sign(s, pid, s["laptop"]), capsys.readouterr()
    assert rc == 0, captured
    out = captured.out
    assert f"signing {pid}: spore_seed (sign, T3)" in out and "COMMITTED: decision ALLOWED" in out, out
    assert _state(s, pid) == "COMMITTED"
    assert SporeStore(s["spores"]).get_by_origin_key(f"ck.{pid[2:]}.0")["text"] == "signed from the terminal"


def test_a_key_whose_class_does_not_cover_the_tier_is_refused_before_signing(served, monkeypatch, capsys):
    """RUN 14: a display-phone key on a T3 pending. Left to the broker, the decision would end the pending
    REFUSED (terminal); refused here, nothing is signed and the pending stays WAITING."""
    s = served
    pid = _issue(s["url"], "the phone may not sign this")
    monkeypatch.setenv("LEVAIN_WRITE_TOKEN", TOKEN)
    signed = _watch_signer(monkeypatch)
    assert _sign(s, pid, s["phone"]) == 1
    assert "key class display-phone does not cover T3" in capsys.readouterr().err
    assert signed == [] and _state(s, pid) == "WAITING"


def test_no_server_at_the_url_is_refused_before_signing(served, monkeypatch, capsys):
    """RUN 25a (signed, then refused: a touch spent on a POST nobody received) -> 25b: refused before signing."""
    s = served
    pid = _issue(s["url"], "nobody is listening")
    monkeypatch.setenv("LEVAIN_WRITE_TOKEN", TOKEN)
    signed = _watch_signer(monkeypatch)
    assert _sign(s, pid, s["laptop"], url="http://127.0.0.1:9") == 1
    assert "no server answers at http://127.0.0.1:9" in capsys.readouterr().err
    assert signed == [] and _state(s, pid) == "WAITING"


def test_a_redirect_is_refused_and_no_proxy_is_used(served, listener, monkeypatch, capsys):
    """B1: urlopen honours HTTP_PROXY and follows a 302 (re-sent as a GET that keeps the token header), so
    the write token and the signature went wherever either pointed. The CLI sends to --url only: no proxy,
    and a 3xx is a refusal."""
    s = served
    pid = _issue(s["url"], "stay on this machine")
    proxy, via_proxy = listener(200, {"state": "COMMITTED", "decision_kind": "ALLOWED", "replay": False})
    elsewhere, redirected = listener(200, {"state": "COMMITTED", "decision_kind": "ALLOWED", "replay": False})
    moved, _ = listener(302, {"error": "moved"}, {"Location": f"{elsewhere}/cockpit/pending/{pid}/signature"})
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("http_proxy", proxy)
    monkeypatch.setattr(urllib.request, "_opener", None)    # urlopen caches its proxies at first use
    monkeypatch.setenv("LEVAIN_WRITE_TOKEN", TOKEN)
    rc = _sign(s, pid, s["laptop"], url=moved)
    assert via_proxy == [] and redirected == []
    assert rc == 1 and "HTTP 302" in capsys.readouterr().err
    assert _state(s, pid) == "WAITING"


def test_an_answer_the_journal_does_not_show_exits_1(served, listener, monkeypatch, capsys):
    """B2: a listener that is not the journal's server answers COMMITTED/ALLOWED; the pending stays WAITING
    in the journal the operator chose, so the CLI says so and exits 1."""
    s = served
    pid = _issue(s["url"], "an unrelated listener")
    fake, seen = listener(200, {"state": "COMMITTED", "decision_kind": "ALLOWED", "replay": False})
    monkeypatch.setenv("LEVAIN_WRITE_TOKEN", TOKEN)
    assert _sign(s, pid, s["laptop"], url=fake) == 1
    assert len(seen) == 1
    assert "is WAITING" in capsys.readouterr().err
    assert _state(s, pid) == "WAITING"


def test_a_token_that_cannot_be_a_header_is_refused_before_signing(served, monkeypatch, capsys):
    """B4: a newline inside the token made http.client raise ValueError after the signature was made (a
    touch spent, then a traceback). It is refused before anything is signed."""
    s = served
    pid = _issue(s["url"], "a token with a newline")
    signed = _watch_signer(monkeypatch)
    monkeypatch.setenv("LEVAIN_WRITE_TOKEN", "row12b\ntoken")
    assert _sign(s, pid, s["laptop"]) == 1
    assert "sign refused: the write token" in capsys.readouterr().err
    assert signed == [] and _state(s, pid) == "WAITING"
