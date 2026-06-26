"""Tests for levain.init_server — `levain init --web`, the browser onboarding server.

Exercised as a REAL bound server over loopback (urllib), like ``test_web_server``.
The anneal store subprocess is mocked so the write-half (``apply_init``) runs
without a live anneal binary; the filesystem writes (seed render, adapter wiring)
are REAL and asserted on disk. Load-bearing guards: the loopback-only bind
refusal, the DNS-rebinding Host allowlist + CSRF + content-type boundary on the
write route, input-boundary validation of ``{adapter, answers}``, the fail-closed
safe-target gate, the install-path-is-server-fixed boundary, and the
InitResult/manifest/emit response shape.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest

import levain.install as install_mod
from levain.init_server import DEFAULT_INIT_PORT, make_init_server, run_init_web


class _OK:
    returncode = 0
    stdout = ""
    stderr = ""


class _Fail:
    returncode = 1
    stdout = ""
    stderr = "boom"


@contextmanager
def _serving(install: Path, *, adapter=None, force=False, result=_OK):
    """A real init server on an ephemeral loopback port, with the anneal store
    subprocess mocked to ``result``. Yields ``(base_url, port)``."""
    with mock.patch.object(install_mod.subprocess, "run", lambda *a, **k: result()):
        httpd = make_init_server(install, adapter=adapter, force=force, port=0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        host, port = httpd.server_address[0], httpd.server_address[1]
        try:
            yield f"http://{host}:{port}", port
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


def _req(url: str, *, method: str = "GET", headers: dict | None = None, data: bytes | None = None):
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 — loopback only
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _post(url: str, payload, *, headers: dict | None = None, content_type="application/json"):
    h = dict(headers or {})
    if content_type is not None:
        h["Content-Type"] = content_type
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    status, _hd, raw = _req(url, method="POST", headers=h, data=body)
    try:
        return status, json.loads(raw)
    except (ValueError, TypeError):
        return status, raw


def _all_answers(plan: dict) -> dict[str, str]:
    return {f["slot"]: f"VAL_{f['slot']}" for f in plan["fields"]}


# --------------------------------------------------------------------------
# bind / boundary
# --------------------------------------------------------------------------

class TestBind:
    def test_refuses_wildcard(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="loopback-only"):
            make_init_server(tmp_path / "i", host="0.0.0.0", port=0)

    def test_refuses_lan_address(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="loopback-only"):
            make_init_server(tmp_path / "i", host="192.168.1.9", port=0)

    def test_refuses_public_address(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="loopback-only"):
            make_init_server(tmp_path / "i", host="8.8.8.8", port=0)

    def test_loopback_binds(self, tmp_path: Path) -> None:
        for host in ("127.0.0.1", "localhost"):
            httpd = make_init_server(tmp_path / "i", host=host, port=0)
            try:
                assert httpd.server_address[0].startswith("127.")
            finally:
                httpd.server_close()

    def test_rejects_unknown_adapter_arg(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="unknown adapter"):
            make_init_server(tmp_path / "i", adapter="emacs", port=0)


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------

class TestReads:
    def test_shell_served(self, tmp_path: Path) -> None:
        with _serving(tmp_path / "i") as (base, _port):
            status, headers, body = _req(base + "/")
            assert status == 200
            assert b"Levain" in body
            assert "text/html" in headers["Content-Type"]

    def test_assets_served(self, tmp_path: Path) -> None:
        with _serving(tmp_path / "i") as (base, _port):
            for path, ctype in (("/init.css", "text/css"), ("/init.js", "text/javascript")):
                status, headers, body = _req(base + path)
                assert status == 200
                assert ctype in headers["Content-Type"]
                assert len(body) > 0

    def test_security_headers_on_every_response(self, tmp_path: Path) -> None:
        with _serving(tmp_path / "i") as (base, _port):
            for path in ("/", "/init-plan.json"):
                _s, headers, _b = _req(base + path)
                assert "default-src 'none'" in headers["Content-Security-Policy"]
                assert headers["X-Content-Type-Options"] == "nosniff"
                assert headers["X-Frame-Options"] == "DENY"
                assert headers["Cache-Control"] == "no-store"

    def test_unknown_route_404(self, tmp_path: Path) -> None:
        with _serving(tmp_path / "i") as (base, _port):
            status, _hd, _b = _req(base + "/nope")
            assert status == 404

    def test_init_plan_shape(self, tmp_path: Path) -> None:
        with _serving(tmp_path / "i") as (base, _port):
            _s, _hd, raw = _req(base + "/init-plan.json")
            plan = json.loads(raw)
            assert plan["adapters"] == ["claude-code", "codex"]
            assert plan["install"].endswith("i")
            assert plan["force"] is False
            assert plan["target_status"] == "nonexistent"
            assert len(plan["fields"]) >= 1
            f = plan["fields"][0]
            for key in ("slot", "style", "guidance", "section_guidance",
                        "section_title", "section_index", "current"):
                assert key in f

    def test_init_plan_carries_default_adapter(self, tmp_path: Path) -> None:
        with _serving(tmp_path / "i", adapter="codex") as (base, _port):
            plan = json.loads(_req(base + "/init-plan.json")[2])
            assert plan["default_adapter"] == "codex"

    def test_target_status_empty(self, tmp_path: Path) -> None:
        d = tmp_path / "empty"
        d.mkdir()
        with _serving(d) as (base, _port):
            assert json.loads(_req(base + "/init-plan.json")[2])["target_status"] == "empty"

    def test_target_status_nonempty(self, tmp_path: Path) -> None:
        d = tmp_path / "full"
        d.mkdir()
        (d / "x").write_text("y", encoding="utf-8")
        with _serving(d) as (base, _port):
            assert json.loads(_req(base + "/init-plan.json")[2])["target_status"] == "nonempty"


# --------------------------------------------------------------------------
# security boundary
# --------------------------------------------------------------------------

class TestSecurity:
    def test_bad_host_rejected(self, tmp_path: Path) -> None:
        with _serving(tmp_path / "i") as (base, _port):
            status, _hd, _b = _req(base + "/init-plan.json", headers={"Host": "evil.com"})
            assert status == 403

    def test_cross_site_get_rejected(self, tmp_path: Path) -> None:
        with _serving(tmp_path / "i") as (base, _port):
            status, _hd, _b = _req(
                base + "/init-plan.json", headers={"Sec-Fetch-Site": "cross-site"}
            )
            assert status == 403

    def test_post_cross_site_rejected(self, tmp_path: Path) -> None:
        with _serving(tmp_path / "i") as (base, _port):
            status, body = _post(
                base + "/init", {"adapter": "claude-code", "answers": {}},
                headers={"Sec-Fetch-Site": "cross-site"},
            )
            assert status == 403
            assert body["error"] == "forbidden"

    def test_post_same_origin_allowed(self, tmp_path: Path) -> None:
        # same-origin is the one allowed Sec-Fetch-Site value — it must pass the CSRF gate
        # (reaching validation), not be refused like cross-site.
        with _serving(tmp_path / "i") as (base, _port):
            status, _body = _post(
                base + "/init", {"adapter": "claude-code", "answers": {}},
                headers={"Sec-Fetch-Site": "same-origin"},
            )
            assert status == 200  # empty install dir → install runs

    def test_post_bad_host_rejected(self, tmp_path: Path) -> None:
        with _serving(tmp_path / "i") as (base, _port):
            status, body = _post(
                base + "/init", {"adapter": "claude-code", "answers": {}},
                headers={"Host": "evil.com"},
            )
            assert status == 403

    def test_post_non_json_rejected(self, tmp_path: Path) -> None:
        with _serving(tmp_path / "i") as (base, _port):
            status, body = _post(base + "/init", b"not json", content_type="text/plain")
            assert status == 415

    def test_post_wrong_route_404(self, tmp_path: Path) -> None:
        with _serving(tmp_path / "i") as (base, _port):
            status, _body = _post(base + "/edit", {"adapter": "claude-code", "answers": {}})
            assert status == 404

    def test_post_oversize_body_413(self, tmp_path: Path) -> None:
        with _serving(tmp_path / "i") as (base, _port):
            huge = {"adapter": "claude-code", "answers": {"OPERATOR_NAME": "x" * 300_000}}
            status, body = _post(base + "/init", huge)
            assert status == 413

    def test_post_bad_json_400(self, tmp_path: Path) -> None:
        with _serving(tmp_path / "i") as (base, _port):
            status, body = _post(base + "/init", b"{not json", content_type="application/json")
            assert status == 400
            assert body["error"] == "bad_json"


# --------------------------------------------------------------------------
# input-boundary validation
# --------------------------------------------------------------------------

class TestValidation:
    def test_bad_adapter_rejected(self, tmp_path: Path) -> None:
        with _serving(tmp_path / "i") as (base, _port):
            status, body = _post(base + "/init", {"adapter": "nope", "answers": {}})
            assert status == 400
            assert body["error"] == "bad_adapter"

    def test_non_object_body_rejected(self, tmp_path: Path) -> None:
        with _serving(tmp_path / "i") as (base, _port):
            status, body = _post(base + "/init", [1, 2, 3])
            assert status == 400

    def test_non_dict_answers_rejected(self, tmp_path: Path) -> None:
        with _serving(tmp_path / "i") as (base, _port):
            status, body = _post(base + "/init", {"adapter": "claude-code", "answers": "x"})
            assert status == 400
            assert body["error"] == "bad_answers"

    def test_non_string_answer_value_rejected(self, tmp_path: Path) -> None:
        with _serving(tmp_path / "i") as (base, _port):
            status, body = _post(
                base + "/init", {"adapter": "claude-code", "answers": {"OPERATOR_NAME": 5}}
            )
            assert status == 400
            assert body["error"] == "bad_answers"

    def test_unknown_field_rejected(self, tmp_path: Path) -> None:
        with _serving(tmp_path / "i") as (base, _port):
            status, body = _post(
                base + "/init", {"adapter": "claude-code", "answers": {"NOT_A_SLOT": "x"}}
            )
            assert status == 400
            assert body["error"] == "unknown_field"

    def test_nonempty_dir_without_force_rejected(self, tmp_path: Path) -> None:
        d = tmp_path / "full"
        d.mkdir()
        (d / "x").write_text("y", encoding="utf-8")
        with _serving(d, force=False) as (base, _port):
            status, body = _post(base + "/init", {"adapter": "claude-code", "answers": {}})
            assert status == 409
            assert body["error"] == "not_empty"

    def test_nonempty_dir_with_force_allowed(self, tmp_path: Path) -> None:
        d = tmp_path / "full"
        d.mkdir()
        (d / "x").write_text("y", encoding="utf-8")
        with _serving(d, force=True) as (base, _port):
            status, body = _post(base + "/init", {"adapter": "claude-code", "answers": {}})
            assert status == 200


# --------------------------------------------------------------------------
# the install (write-half through the web POST)
# --------------------------------------------------------------------------

class TestInstall:
    def test_claude_code_install_success(self, tmp_path: Path) -> None:
        install = tmp_path / "newentity"
        with _serving(install) as (base, _port):
            plan = json.loads(_req(base + "/init-plan.json")[2])
            status, body = _post(
                base + "/init", {"adapter": "claude-code", "answers": _all_answers(plan)}
            )
        assert status == 200
        assert body["ok"] is True
        assert body["partial"] is False
        assert body["adapter"] == "claude-code"
        assert body["install"].endswith("newentity")
        assert any(f["path"].endswith("CLAUDE.md") for f in body["files"])
        assert isinstance(body["messages"], list)
        assert any("adapter installed" in m for m in body["messages"])  # emit captured
        assert body["next_steps"]
        # the writes really happened on disk, rendered from the answers
        world = (install / "seed" / "world.md").read_text(encoding="utf-8")
        assert "{{" not in world
        assert "VAL_" in world
        assert (install / "CLAUDE.md").is_file()
        assert (install / ".mcp.json").is_file()

    def test_install_path_is_server_fixed_not_body(self, tmp_path: Path) -> None:
        """A POST body cannot redirect the install target — the path is fixed at
        server start. A body that even TRIES to carry an `install`/`path` key is now
        rejected outright (unknown top-level field, codex MED), and nothing is
        written; a clean install lands ONLY in the server-fixed dir."""
        server_dir = tmp_path / "real"
        attacker_dir = tmp_path / "attacker"
        with _serving(server_dir) as (base, _port):
            plan = json.loads(_req(base + "/init-plan.json")[2])
            answers = _all_answers(plan)
            # the path-carrying body is REFUSED, and nothing is written anywhere
            status, body = _post(base + "/init", {
                "adapter": "claude-code",
                "answers": answers,
                "install": str(attacker_dir),
                "path": str(attacker_dir),
            })
            assert status == 400
            assert body["error"] == "unknown_field"
            assert not attacker_dir.exists()
            assert not (server_dir / "CLAUDE.md").exists()
            # a clean install (no path keys) lands ONLY in the server's fixed dir
            status2, body2 = _post(base + "/init", {"adapter": "claude-code", "answers": answers})
        assert status2 == 200
        assert body2["install"].endswith("real")
        assert (server_dir / "CLAUDE.md").is_file()
        assert not attacker_dir.exists()

    def test_store_failure_reports_partial(self, tmp_path: Path) -> None:
        install = tmp_path / "newentity"
        with _serving(install, result=_Fail) as (base, _port):
            plan = json.loads(_req(base + "/init-plan.json")[2])
            status, body = _post(
                base + "/init", {"adapter": "claude-code", "answers": _all_answers(plan)}
            )
        assert status == 200
        assert body["ok"] is False
        assert body["partial"] is True
        # files still laid down (the store is the last step)
        assert (install / "seed" / "world.md").is_file()

    def test_codex_install_redirects_global(self, tmp_path: Path, monkeypatch) -> None:
        """The codex adapter path through the web POST — CODEX_HOME redirected so the
        real ~/.codex is never touched."""
        codex_home = tmp_path / "codex_home"
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        install = tmp_path / "newentity"
        with _serving(install) as (base, _port):
            plan = json.loads(_req(base + "/init-plan.json")[2])
            status, body = _post(
                base + "/init", {"adapter": "codex", "answers": _all_answers(plan)}
            )
        assert status == 200
        assert body["ok"] is True
        assert (install / "AGENTS.md").is_file()
        assert (codex_home / "hooks.json").is_file()


# --------------------------------------------------------------------------
# run_init_web entry (error paths — the happy path blocks in serve_forever)
# --------------------------------------------------------------------------

class TestEntry:
    def test_refuses_non_loopback_host(self, tmp_path: Path) -> None:
        assert run_init_web(tmp_path / "i", host="0.0.0.0", open_browser=False) == 1

    def test_refuses_path_that_is_a_file(self, tmp_path: Path) -> None:
        f = tmp_path / "afile"
        f.write_text("x", encoding="utf-8")
        assert run_init_web(f, open_browser=False) == 1

    def test_default_port_constant(self) -> None:
        assert DEFAULT_INIT_PORT == 7430


# --------------------------------------------------------------------------
# apparatus fixes (L3 codex/nemotron + L1) — regression pins
# --------------------------------------------------------------------------

class TestApparatusFixes:
    def test_unknown_top_level_key_rejected(self, tmp_path: Path) -> None:
        # codex MED: the body contract is exactly {adapter, answers}; an extra
        # top-level key (e.g. a future-trusted field) is refused, not ignored.
        with _serving(tmp_path / "i") as (base, _port):
            status, body = _post(
                base + "/init",
                {"adapter": "claude-code", "answers": {}, "surprise": "x"},
            )
            assert status == 400
            assert body["error"] == "unknown_field"

    def test_head_request_ok(self, tmp_path: Path) -> None:
        with _serving(tmp_path / "i") as (base, _port):
            status, headers, body = _req(base + "/", method="HEAD")
            assert status == 200
            assert body == b""  # HEAD carries no body
            assert "default-src 'none'" in headers["Content-Security-Policy"]

    def test_csp_on_404_and_framework_error(self, tmp_path: Path) -> None:
        # codex MED: the security headers ride EVERY response, including a
        # framework-generated send_error (an unsupported method never reaches _send).
        with _serving(tmp_path / "i") as (base, _port):
            _s, h404, _b = _req(base + "/nope")
            assert "default-src 'none'" in h404["Content-Security-Policy"]
            # OPTIONS has no handler → BaseHTTPRequestHandler.send_error (501); the
            # end_headers override must STILL stamp the CSP on it.
            _s2, hopt, _b2 = _req(base + "/init", method="OPTIONS")
            assert "Content-Security-Policy" in hopt

    def test_install_lock_serializes_concurrent_installs(self, tmp_path: Path) -> None:
        # L1 MED: the install_lock is THE "two installs into one dir would race" guard.
        # Hold it and confirm a concurrent POST gets a clean 503, never a second install.
        install = tmp_path / "i"
        with mock.patch.object(install_mod.subprocess, "run", lambda *a, **k: _OK()):
            httpd = make_init_server(install, port=0)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            port = httpd.server_address[1]
            base = f"http://127.0.0.1:{port}"
            try:
                httpd.install_lock.acquire()  # simulate an install in progress
                try:
                    status, body = _post(base + "/init", {"adapter": "claude-code", "answers": {}})
                finally:
                    httpd.install_lock.release()
                assert status == 503
                assert body["error"] == "busy"
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)

    def test_partial_failure_carries_progress_log(self, tmp_path: Path) -> None:
        # L1 LOW: a store-init failure is partial:true AND carries the captured emit
        # log so the operator can see how far it got (no rollback).
        install = tmp_path / "newentity"
        with _serving(install, result=_Fail) as (base, _port):
            plan = json.loads(_req(base + "/init-plan.json")[2])
            status, body = _post(
                base + "/init", {"adapter": "claude-code", "answers": _all_answers(plan)}
            )
        assert status == 200  # store-fail is reported as partial, not a 500
        assert body["partial"] is True
        assert isinstance(body["messages"], list) and body["messages"]

    def test_init_plan_section_grouping_is_contiguous(self, tmp_path: Path) -> None:
        # The JS groups fields into cards by section_index; that only works if each
        # section_index appears in ONE contiguous run. Pin the contract the form relies on.
        with _serving(tmp_path / "i") as (base, _port):
            plan = json.loads(_req(base + "/init-plan.json")[2])
            runs = []
            for f in plan["fields"]:
                if not runs or runs[-1] != f["section_index"]:
                    runs.append(f["section_index"])
            assert len(runs) == len(set(runs))  # no section_index split across runs
            # first_in_section marks exactly the start of each run
            firsts = [f["section_index"] for f in plan["fields"] if f["first_in_section"]]
            assert firsts == runs
