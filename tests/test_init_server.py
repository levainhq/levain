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
from levain.packs import PackError


class _OK:
    returncode = 0
    stdout = ""
    stderr = ""


class _Fail:
    returncode = 1
    stdout = ""
    stderr = "boom"


@contextmanager
def _serving(install: Path, *, adapter=None, force=False, packs=(), result=_OK):
    """A real init server on an ephemeral loopback port, with the anneal store
    subprocess mocked to ``result``. Yields ``(base_url, port)``."""
    with mock.patch.object(install_mod.subprocess, "run", lambda *a, **k: result()):
        httpd = make_init_server(install, adapter=adapter, force=force, packs=packs, port=0)
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


def _write_pack(
    root: Path,
    *,
    name: str,
    order: int = 10,
    render: list[str] | None = None,
    seed_files: dict[str, str],
    activation_files: dict[str, str] | None = None,
    docs_files: dict[str, str] | None = None,
) -> Path:
    """Write a minimal pack-layer (pack.toml + seed/ [+ activation/] [+ docs/]) at
    ``root``. Mirrors the pack convention the CLI --pack path consumes, so the web
    onboarding server composes a REAL pack, not a stub."""
    render_line = f"render = {render!r}\n" if render else ""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pack.toml").write_text(
        f'name = "{name}"\norder = {order}\n{render_line}', encoding="utf-8"
    )
    seed = root / "seed"
    seed.mkdir(parents=True, exist_ok=True)
    for fname, content in seed_files.items():
        (seed / fname).write_text(content, encoding="utf-8")
    for subdir, files in (("activation", activation_files), ("docs", docs_files)):
        if not files:
            continue
        for fname, content in files.items():
            dest = root / subdir / fname
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
    return root


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
            plan = json.loads(_req(base + "/init-plan.json")[2])
            status, _body = _post(
                base + "/init", {"adapter": "claude-code", "answers": _all_answers(plan)},
                headers={"Sec-Fetch-Site": "same-origin"},
            )
            assert status == 200  # empty install dir + complete answers → install runs

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
            plan = json.loads(_req(base + "/init-plan.json")[2])
            status, body = _post(
                base + "/init", {"adapter": "claude-code", "answers": _all_answers(plan)}
            )
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


class TestPackCompose:
    """`levain init --web --pack …` — the browser interview composes packs, the
    parity path to the CLI `--pack` interview (the gap Chris's install surfaced)."""

    def test_bad_pack_rejected_at_server_start(self, tmp_path: Path) -> None:
        # A pack that does not compose (no pack.toml) is refused BEFORE the bind —
        # ValueError, no dangling port, no 500 on the first request.
        with pytest.raises(ValueError, match="pack composition failed"):
            make_init_server(tmp_path / "i", packs=[tmp_path / "no-such-pack"], port=0)

    def test_base_only_plan_has_empty_packs(self, tmp_path: Path) -> None:
        # No --pack → the readout list is empty (base-only onboarding, unchanged).
        with _serving(tmp_path / "i") as (base, _port):
            plan = json.loads(_req(base + "/init-plan.json")[2])
        assert plan["packs"] == []

    def test_plan_composes_pack_render_field_and_names(self, tmp_path: Path) -> None:
        # A pack that ADDS a render file surfaces its slot in the form's field plan,
        # and the pack's manifest name reaches the "composing" readout.
        pack = _write_pack(
            tmp_path / "pdomain",
            name="pdomain",
            render=["role.md"],
            seed_files={
                "role.md": "# Role\n\nFocus: {{ROLE_FOCUS}}\n",
                "domain.md": "# Domain\n\nHosting + audit doctrine.\n",
            },
        )
        with _serving(tmp_path / "i", packs=[pack]) as (base, _port):
            plan = json.loads(_req(base + "/init-plan.json")[2])
        assert plan["packs"] == ["pdomain"]
        assert any(f["slot"] == "ROLE_FOCUS" for f in plan["fields"])

    def test_install_composes_pack_seed_and_import(self, tmp_path: Path) -> None:
        # The write-half composes the pack: its verbatim seed lands byte-exact, its
        # render file renders from the browser answers, and BOTH load into the
        # adapter @import block (Slice-3) — i.e. the doctrine actually loads, the
        # STOP-TIME-BAR observable, not just files-on-disk.
        pack = _write_pack(
            tmp_path / "pdomain",
            name="pdomain",
            render=["role.md"],
            seed_files={
                "role.md": "# Role\n\nFocus: {{ROLE_FOCUS}}\n",
                "domain.md": "# Domain\n\nHosting + audit doctrine.\n",
            },
        )
        install = tmp_path / "newentity"
        with _serving(install, packs=[pack]) as (base, _port):
            plan = json.loads(_req(base + "/init-plan.json")[2])
            status, body = _post(
                base + "/init", {"adapter": "claude-code", "answers": _all_answers(plan)}
            )
        assert status == 200
        assert body["ok"] is True
        # verbatim pack seed composed byte-exact
        assert (install / "seed" / "domain.md").read_text(encoding="utf-8") == (
            "# Domain\n\nHosting + audit doctrine.\n"
        )
        # pack render file rendered from the browser answers
        role = (install / "seed" / "role.md").read_text(encoding="utf-8")
        assert "{{" not in role
        assert "VAL_ROLE_FOCUS" in role
        # both pack seeds LOAD (the adapter @import block references them)
        claude_md = (install / "CLAUDE.md").read_text(encoding="utf-8")
        assert "seed/domain.md" in claude_md
        assert "seed/role.md" in claude_md

    def test_install_composes_pack_activation_override(self, tmp_path: Path) -> None:
        # A pack's activation/posture.md overrides base through the web path too —
        # proving activation_roots is threaded (Slice-4 parity with the CLI).
        pack = _write_pack(
            tmp_path / "pact",
            name="pact",
            seed_files={"extra.md": "# Extra\n\ndoctrine\n"},
            activation_files={"posture.md": "PACK POSTURE OVERRIDE\n"},
        )
        install = tmp_path / "newentity"
        with _serving(install, packs=[pack]) as (base, _port):
            plan = json.loads(_req(base + "/init-plan.json")[2])
            status, body = _post(
                base + "/init", {"adapter": "claude-code", "answers": _all_answers(plan)}
            )
        assert status == 200
        assert body["ok"] is True
        posture = (install / "activation" / "posture.md").read_text(encoding="utf-8")
        assert "PACK POSTURE OVERRIDE" in posture

    def test_install_composes_pack_docs(self, tmp_path: Path) -> None:
        # A pack shipping docs/*.md composes them into .levain/docs/ so `levain docs`
        # renders a self-contained composed manual (the web path calls _copy_pack_docs
        # like the CLI). L1 #1: this integration was untested.
        pack = _write_pack(
            tmp_path / "pdocs",
            name="pdocs",
            seed_files={"extra.md": "# Extra\n\ndoctrine\n"},
            docs_files={"chapter.md": "# Pack Chapter\n\nthe operator manual chapter\n"},
        )
        install = tmp_path / "newentity"
        with _serving(install, packs=[pack]) as (base, _port):
            plan = json.loads(_req(base + "/init-plan.json")[2])
            status, body = _post(
                base + "/init", {"adapter": "claude-code", "answers": _all_answers(plan)}
            )
        assert status == 200 and body["ok"] is True
        docs_root = install / ".levain" / "docs"
        assert any(p.name == "chapter.md" for p in docs_root.rglob("*.md"))
        assert any("pack chapter" in m for m in body["messages"])

    def test_docs_copy_failure_is_nonfatal(self, tmp_path: Path) -> None:
        # A docs-copy failure AFTER a successful install must NOT report the install as
        # failed — the manual is a read surface. ok:true, partial:false, note in the log
        # (L1 #1: the riskiest seam, previously unexercised).
        pack = _write_pack(
            tmp_path / "pdocs",
            name="pdocs",
            seed_files={"extra.md": "# Extra\n\ndoctrine\n"},
            docs_files={"chapter.md": "# Ch\n\nx\n"},
        )
        install = tmp_path / "newentity"
        with _serving(install, packs=[pack]) as (base, _port):
            plan = json.loads(_req(base + "/init-plan.json")[2])
            with mock.patch(
                "levain.init_server._copy_pack_docs", side_effect=OSError("disk full")
            ):
                status, body = _post(
                    base + "/init", {"adapter": "claude-code", "answers": _all_answers(plan)}
                )
        assert status == 200
        assert body["ok"] is True
        assert body["partial"] is False
        assert any("could not refresh pack docs" in m for m in body["messages"])
        assert (install / "CLAUDE.md").is_file()  # the install itself really landed

    def test_force_reinstall_dropping_pack_clears_stale_docs(self, tmp_path: Path) -> None:
        # The IP-boundary guard: a --force reinstall that DROPS a pack must CLEAR that
        # pack's stale (possibly company-private) chapters — _copy_pack_docs runs even
        # base-only. (complement L3 CRITICAL on the CLI path; verify the web path too.)
        pack = _write_pack(
            tmp_path / "pdocs",
            name="pdocs",
            seed_files={"extra.md": "# Extra\n\ndoctrine\n"},
            docs_files={"chapter.md": "# Ch\n\nx\n"},
        )
        install = tmp_path / "entity"
        with _serving(install, packs=[pack]) as (base, _port):
            plan = json.loads(_req(base + "/init-plan.json")[2])
            _post(base + "/init", {"adapter": "claude-code", "answers": _all_answers(plan)})
        assert (install / ".levain" / "docs").exists()  # pack chapters landed
        # Re-onboard base-only with --force → the dropped pack's docs are cleared.
        with _serving(install, force=True) as (base, _port):
            plan = json.loads(_req(base + "/init-plan.json")[2])
            status, body = _post(
                base + "/init", {"adapter": "claude-code", "answers": _all_answers(plan)}
            )
        assert status == 200 and body["ok"] is True
        assert not (install / ".levain" / "docs").exists()

    def test_packs_body_key_is_rejected(self, tmp_path: Path) -> None:
        # The browser can no more inject a pack than redirect the install target: a
        # `packs` key in the POST body is refused at the unknown-top-level-key boundary,
        # and nothing composes it (L1 #5 / L2 point 1 — make the invariant explicit).
        pack = _write_pack(
            tmp_path / "evil",
            name="evil",
            seed_files={"evil.md": "# Evil\n\ninjected doctrine\n"},
        )
        install = tmp_path / "entity"
        with _serving(install) as (base, _port):  # server started base-only
            plan = json.loads(_req(base + "/init-plan.json")[2])
            status, body = _post(base + "/init", {
                "adapter": "claude-code",
                "answers": _all_answers(plan),
                "packs": [str(pack)],
            })
        assert status == 400
        assert body["error"] == "unknown_field"
        assert not (install / "seed" / "evil.md").exists()

    def test_missing_slot_rejected_as_stale_form(self, tmp_path: Path) -> None:
        # The form POSTs every current slot (blank as ""); a valid-but-ABSENT slot
        # means a stale/forged form whose field set predates a pack's render slot.
        # Reject 400 rather than silently render it "" — the 0.3.7 phantom-slot
        # desync class (entity never learns it, doctor stays green). codex L3 HIGH.
        pack = _write_pack(
            tmp_path / "prole",
            name="prole",
            render=["role.md"],
            seed_files={"role.md": "# Role\n\nFocus: {{ROLE_FOCUS}}\n"},
        )
        install = tmp_path / "entity"
        with _serving(install, packs=[pack]) as (base, _port):
            plan = json.loads(_req(base + "/init-plan.json")[2])
            answers = _all_answers(plan)
            answers.pop("ROLE_FOCUS")  # a stale form that predates the pack's slot
            status, body = _post(
                base + "/init", {"adapter": "claude-code", "answers": answers}
            )
        assert status == 400
        assert body["error"] == "stale_form"
        assert "ROLE_FOCUS" in body["message"]
        assert not (install / "seed" / "role.md").exists()  # nothing rendered

    def test_post_time_pack_error_reports_clean_pack_500(self, tmp_path: Path) -> None:
        # order_activation_roots raising PackError mid-POST (e.g. a pack's activation
        # tree vanishing after bind) is a PRE-write fault → a clean 500 error:"pack"
        # with NO spurious partial flag (the L1 #3 branch; complement L3 flagged it
        # as untested — the bind-time path was covered but not the POST-time one).
        pack = _write_pack(
            tmp_path / "pact2",
            name="pact2",
            seed_files={"extra.md": "# Extra\n\nx\n"},
            activation_files={"posture.md": "override\n"},
        )
        install = tmp_path / "entity"
        with _serving(install, packs=[pack]) as (base, _port):
            plan = json.loads(_req(base + "/init-plan.json")[2])
            with mock.patch(
                "levain.init_server.order_activation_roots",
                side_effect=PackError("activation tree vanished"),
            ):
                status, body = _post(
                    base + "/init", {"adapter": "claude-code", "answers": _all_answers(plan)}
                )
        assert status == 500
        assert body["error"] == "pack"
        assert "partial" not in body  # pre-write fault → no partial leak
        assert not (install / "CLAUDE.md").exists()  # nothing written
