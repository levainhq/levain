"""levain.init_server — ``levain init --web``: cockpit-native onboarding.

The browser peer of the scripted CLI interview (``levain init``). Serves a
ONE-PAGE, pre-filled form — every seed field at once, per-field controls — on a
localhost-bound process the operator runs; on submit it POSTs ``{adapter,
answers}`` to ``/init``, which runs the SAME ``apply_init`` write-half the CLI
uses (rendered templates → verbatim seeds → adapter wiring → store init). The
two surfaces share ONE field-plan derivation (``build_field_plan``) and ONE
write-half (``apply_init``), so the questions, order, and writes can't drift.

LOOPBACK-ONLY by construction. Onboarding writes operator-private seed/config
and (for the codex adapter) mutates the global ``~/.codex`` — it has no business
reachable off the machine, so unlike ``levain serve`` there is NO ``--host``
off-box mode and NO write token: the loopback bind IS the auth. It REUSES the
web-app's security primitives — the DNS-rebinding Host allowlist
(``host_header_allowed``), the Sec-Fetch-Site CSRF check, ``application/json``
enforcement, the CSP, and a concurrency bound — shared rather than
re-implemented so the two surfaces can't diverge on a security contract.

Init precedes install — there is no ``SubstrateSource`` / store yet — so this is
a SEPARATE server from ``make_server`` (which is built over an EXISTING
substrate), not an extra route on it. The install PATH and ``--force`` are fixed
at server start from the CLI flags; the browser POST supplies only ``{adapter,
answers}``, so a page can never redirect the install target.
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from levain.install import (
    InitError,
    _is_safe_install_target,
    _manifest_rows,
    _next_steps_lines,
    apply_init,
    open_init_templates,
)
from levain.interview import build_field_plan
from levain.web_server import (
    _CSP,
    _LOOPBACK_HOSTS,
    _WRITE_SEC_FETCH_ALLOWED,
    _is_loopback_host,
    host_header_allowed,
    load_web_asset,
)

__all__ = ["DEFAULT_INIT_HOST", "DEFAULT_INIT_PORT", "make_init_server", "run_init_web"]

DEFAULT_INIT_HOST = "127.0.0.1"
# A distinct default port from the dashboard's 7420 so an operator can run a
# `levain serve` cockpit and a `levain init --web` onboarding at once.
DEFAULT_INIT_PORT = 7430

# The two adapters a v1 install supports (mirrors install.py `_resolve_adapter`).
_ADAPTERS = ("claude-code", "codex")

# Cap the POST body. The answers payload is seed prose (a few KB total at most);
# 256 KiB is a generous ceiling that refuses an abusive body before reading it.
_MAX_INIT_BODY = 256 * 1024

# When refusing an over-cap body, drain up to this much so the client can finish its
# send and read the 413 cleanly on a kept-alive connection (mirrors web_server); an
# absurdly larger declared body isn't drained — we just close.
_DRAIN_CAP = _MAX_INIT_BODY * 4

# Bound concurrent /init-plan.json reads (cheap, but the store/template parse is
# the expensive part). The /init WRITE is serialized by its own lock below — two
# concurrent installs into one dir would race, so only one install runs at a time.
_MAX_INFLIGHT = 8

# The init page's static assets, by request path → (package-data filename,
# content-type). An explicit allowlist; no path → filesystem mapping anywhere.
_ASSETS: dict[str, tuple[str, str]] = {
    "/": ("init.html", "text/html; charset=utf-8"),
    "/init.css": ("init.css", "text/css; charset=utf-8"),
    "/init.js": ("init.js", "text/javascript; charset=utf-8"),
}


def _target_status(install: Path) -> str:
    """How the fixed install dir looks RIGHT NOW, for the form to warn on:
    ``nonexistent`` (will be created), ``empty`` (safe), or ``nonempty`` (needs
    --force). Mirrors ``_is_safe_install_target``'s classification."""
    if not install.exists():
        return "nonexistent"
    if not install.is_dir():
        return "not_a_directory"
    return "empty" if _is_safe_install_target(install) else "nonempty"


class _InitServer(ThreadingHTTPServer):
    """A ``ThreadingHTTPServer`` carrying the fixed install config, cached static
    assets, the Host allowlist, and the install serialization lock. Typed so the
    handler's access is checked and can't silently break."""

    install: Path
    force: bool
    default_adapter: str | None
    python_path: str
    anneal_path: str
    assets: dict[str, bytes]
    allowed_hosts: frozenset[str]
    request_gate: threading.BoundedSemaphore
    install_lock: threading.Lock

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Swallow the benign client-disconnect family (idle keep-alive resets)
        instead of dumping a traceback; defer genuine errors to the base so real
        bugs still surface (mirrors ``_LevainHTTPServer``, spore-178)."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class _InitHandler(BaseHTTPRequestHandler):
    """Serves the init form (GET/HEAD) + the one governed write route
    (``POST /init``). Behind the same DNS-rebinding Host allowlist + CSRF +
    application/json boundary the dashboard server uses."""

    protocol_version = "HTTP/1.1"
    server_version = "levain-init"
    timeout = 30
    server: _InitServer  # narrow the type for typed attribute access

    # ---- shared response discipline (CSP + security headers, like the dashboard) ----

    def end_headers(self) -> None:
        """Stamp the security headers on EVERY response — structurally, so the
        invariant can't be skipped. ``_send`` is the normal path, but the stdlib's
        ``send_error`` (an unsupported method, an OPTIONS preflight, a malformed
        request) builds its own response that never passes through ``_send``; putting
        the headers here covers those framework-generated responses too (codex L3
        MED). Nothing else sets these (``_send`` deliberately does not), so a plain
        add is exactly-once per response."""
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _send(
        self, body: bytes, content_type: str, status: int = 200, *, head: bool = False
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _send_json(self, payload: dict[str, object], status: int = 200) -> None:
        self._send(
            json.dumps(payload).encode("utf-8"),
            "application/json; charset=utf-8",
            status=status,
        )

    def version_string(self) -> str:
        return self.server_version

    def _host_ok(self) -> bool:
        return host_header_allowed(self.headers.get("Host"), self.server.allowed_hosts)

    # ---- reads (GET/HEAD) ----

    def _route(self, *, head: bool) -> None:
        if not self._host_ok():
            self._send(b"forbidden\n", "text/plain; charset=utf-8", status=403, head=head)
            return
        # Defense-in-depth: refuse a cross-site browser read (a same-origin fetch
        # sends same-origin; a top-level nav sends none; non-browser clients omit
        # it). Same cheap layer the dashboard read path uses.
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._send(b"forbidden\n", "text/plain; charset=utf-8", status=403, head=head)
            return

        path = self.path.split("?", 1)[0]

        if path == "/init-plan.json":
            if not self.server.request_gate.acquire(blocking=False):
                self._send(b"busy\n", "text/plain; charset=utf-8", status=503, head=head)
                return
            try:
                body = self._build_plan_json()
            except Exception as exc:  # noqa: BLE001 — never 500 on a runtime fault
                body = json.dumps(
                    {"errors": {"server": f"{type(exc).__name__}: {exc}"}}
                ).encode("utf-8")
            finally:
                self.server.request_gate.release()
            self._send(body, "application/json; charset=utf-8", head=head)
            return

        asset = _ASSETS.get(path)
        if asset is not None:
            filename, content_type = asset
            self._send(self.server.assets[filename], content_type, head=head)
            return

        self._send(b"not found\n", "text/plain; charset=utf-8", status=404, head=head)

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler contract
        self._route(head=False)

    def do_HEAD(self) -> None:  # noqa: N802 — same routing, headers only
        self._route(head=True)

    def _build_plan_json(self) -> bytes:
        """The ``/init-plan.json`` body: the shared interview field plan projected
        to JSON (every field, pre-filled `current`, section grouping), plus the
        adapter choices, the FIXED install path + force flag, and the live
        target-dir status the form warns on. Pure read — parses the packaged
        templates; writes nothing."""
        with open_init_templates() as (_templates_root, specs, _verbatim):
            fields = build_field_plan(specs)
        payload = {
            "fields": [asdict(f) for f in fields],
            "adapters": list(_ADAPTERS),
            "default_adapter": self.server.default_adapter,
            "install": str(self.server.install),
            "force": self.server.force,
            "target_status": _target_status(self.server.install),
        }
        return json.dumps(payload).encode("utf-8")

    # ---- the write route (POST /init) ----

    def _reject(self, status: int, error: str, message: str) -> None:
        """Refuse a write BEFORE its body is read: close the connection (so the
        unread body can't desync a kept-alive socket) and send the error JSON."""
        self.close_connection = True
        self._send_json({"error": error, "message": message}, status)

    def _drain(self, n: int) -> None:
        """Read and discard up to ``n`` bytes of the request body in bounded chunks,
        so a rejected oversize request's body doesn't dangle on a kept-alive
        connection — the client can finish its send and read the error cleanly."""
        remaining = n
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler contract
        """``POST /init`` — run the install from the submitted ``{adapter,
        answers}``. The cheap fail-closed checks (Host → CSRF → Content-Type →
        route → Content-Length) run BEFORE any body read and each closes the
        connection on rejection. The install itself is serialized by
        ``install_lock`` (one install at a time — two into one dir would race) and
        owns its try/except → HTTP + partial-install reporting (no rollback; codex
        installs touch global ~/.codex)."""
        if not self._host_ok():
            return self._reject(403, "forbidden", "bad Host header")
        # CSRF layer 1: same-origin (our page) or a non-browser client only.
        sfs = self.headers.get("Sec-Fetch-Site")
        if sfs is not None and sfs != _WRITE_SEC_FETCH_ALLOWED:
            return self._reject(403, "forbidden", "cross-origin write refused")
        # CSRF layer 2: require application/json (a cross-origin page can't send it
        # without a CORS preflight this server never answers).
        ctype = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            return self._reject(
                415, "unsupported_media_type", "Content-Type must be application/json"
            )
        if self.path.split("?", 1)[0] != "/init":
            return self._reject(404, "not_found", "no such route")
        clen_raw = self.headers.get("Content-Length")
        if clen_raw is None or not clen_raw.isdigit():
            return self._reject(411, "length_required", "Content-Length required")
        clen = int(clen_raw)
        if clen > _MAX_INIT_BODY:
            # Refuse oversize: ALWAYS close the connection (never keep-alive after a
            # rejected body — a truncated/lying body would otherwise desync the next
            # request on the socket), and drain a bounded amount first so the client
            # can read the 413 cleanly. Guard the drain so a stalled/short body can't
            # strand the thread past the socket timeout (codex LOW / nemotron).
            self.close_connection = True
            if clen <= _DRAIN_CAP:
                try:
                    self._drain(clen)
                except OSError:
                    pass
            self._send_json(
                {"error": "too_large", "message": f"body exceeds {_MAX_INIT_BODY} bytes"}, 413
            )
            return

        # Serialize installs: only one runs at a time (concurrent installs into the
        # same fixed dir would race on the filesystem + the global codex config).
        if not self.server.install_lock.acquire(blocking=False):
            return self._reject(503, "busy", "an install is already in progress")
        try:
            try:
                raw = self.rfile.read(clen)
            except OSError:  # slow/stalled client hit the socket timeout → drop it
                self.close_connection = True
                return
            try:
                req = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._send_json({"error": "bad_json", "message": "body is not valid JSON"}, 400)
                return
            self._run_install(req)
        finally:
            self.server.install_lock.release()

    def _run_install(self, req: object) -> None:
        """Validate the submitted ``{adapter, answers}`` at the input boundary,
        then run ``apply_init`` and report. Holds ``install_lock``."""
        if not isinstance(req, dict):
            self._send_json({"error": "bad_request", "message": "body must be a JSON object"}, 400)
            return

        # Reject UNKNOWN top-level keys at the boundary — the body's contract is exactly
        # {adapter, answers}. The install path is server-fixed, so an `install`/`path` key
        # is already ignored; rejecting it (and any other extra key) loudly closes the
        # footgun of a future change accidentally trusting a previously-ignored key
        # (codex L3 MED).
        unknown_keys = sorted(set(req) - {"adapter", "answers"})
        if unknown_keys:
            self._send_json(
                {"error": "unknown_field",
                 "message": f"unknown request field(s): {unknown_keys}"}, 400
            )
            return

        # The captured install progress/remediation — hoisted out of the `with` so it is
        # in scope for the partial-failure path below (the operator most needs to know how
        # far an install got when it fails mid-way, since there is no rollback).
        messages: list[str] = []

        # Validate the adapter choice at the input boundary (matching where the CLI
        # validates), NOT inside apply_init.
        chosen = req.get("adapter")
        if chosen not in _ADAPTERS:
            self._send_json(
                {"error": "bad_adapter",
                 "message": f"adapter must be one of {list(_ADAPTERS)}"}, 400
            )
            return

        answers = req.get("answers")
        if not isinstance(answers, dict):
            self._send_json(
                {"error": "bad_answers", "message": "answers must be an object of slot → string"},
                400,
            )
            return
        # Every value must be a string (render_template substitutes raw text).
        if any(not isinstance(v, str) for v in answers.values()):
            self._send_json(
                {"error": "bad_answers", "message": "every answer value must be a string"}, 400
            )
            return

        try:
            with open_init_templates() as (templates_root, specs, verbatim):
                # Drive validation from the shared field plan: render_template
                # silently renders a MISSING slot to "" and ignores an UNKNOWN one,
                # so reject any answer key that isn't a real slot at the boundary
                # (a stale/forged form, never a silent partial render).
                valid_slots = {f.slot for f in build_field_plan(specs)}
                unknown = sorted(k for k in answers if k not in valid_slots)
                if unknown:
                    self._send_json(
                        {"error": "unknown_field",
                         "message": f"unknown answer field(s): {unknown}"}, 400
                    )
                    return

                install = self.server.install
                # Fail-closed safe-target gate at the WRITE boundary (the form's
                # target_status is only a hint — re-check here authoritatively).
                if not _is_safe_install_target(install) and not self.server.force:
                    self._send_json(
                        {"error": "not_empty",
                         "message": f"{install} is not empty; restart with --force to overwrite"},
                        409,
                    )
                    return
                install.mkdir(parents=True, exist_ok=True)

                # Capture the write-half's progress + remediation so it reaches the
                # browser, not just the server console (messages is hoisted above).
                result = apply_init(
                    install,
                    chosen,
                    answers,
                    templates_root,
                    self.server.python_path,
                    self.server.anneal_path,
                    specs,
                    verbatim,
                    emit=messages.append,
                )
                store = install / ".levain" / "memory.db"
                rows = _manifest_rows(install, chosen, store, result.store_ok)
                next_steps = _next_steps_lines(install, chosen, result.store_ok)
        except InitError as exc:
            self._send_json({"error": "templates", "message": exc.message}, 500)
            return
        except Exception as exc:  # noqa: BLE001 — never leak a traceback to the client
            # A mid-install fault: files may already be on disk (no rollback; codex
            # installs touch global ~/.codex). Report it as a partial install, and
            # INCLUDE the captured progress so the operator can see how far it got
            # (the renderError surface shows messages) — L1 review.
            self._send_json(
                {"error": "install_failed",
                 "message": f"{type(exc).__name__}: {exc}",
                 "partial": True,
                 "messages": messages}, 500
            )
            return

        self._send_json(
            {
                "ok": result.store_ok,
                "store_ok": result.store_ok,
                "complete": result.complete,
                # files laid down but the store init failed = a partial install.
                "partial": not result.store_ok,
                "adapter": chosen,
                "install": str(install),
                "messages": messages,
                "files": [{"label": label, "path": str(path)} for label, path in rows],
                "next_steps": next_steps,
            },
            200,
        )

    def log_message(self, fmt: str, *args: object) -> None:
        """Quiet by default; set ``LEVAIN_SERVE_VERBOSE`` to restore the access log."""
        import os

        if os.environ.get("LEVAIN_SERVE_VERBOSE"):
            super().log_message(fmt, *args)


def make_init_server(
    install: Path,
    *,
    adapter: str | None = None,
    force: bool = False,
    host: str = DEFAULT_INIT_HOST,
    port: int = DEFAULT_INIT_PORT,
) -> _InitServer:
    """Build a configured, bound (not-yet-serving) init server.

    LOOPBACK-ONLY: a non-loopback ``host`` is refused before binding — onboarding
    writes operator-private seed/config (and mutates global ~/.codex for codex),
    so it must never be reachable off the machine. The install ``path`` + ``force``
    are fixed HERE (from the CLI), not from any browser request. Raises
    ``ValueError`` on a non-loopback host; ``OSError`` if the bind fails. Separated
    from ``run_init_web`` so tests can drive a real bound server without the
    print/browser/serve_forever wrapper."""
    if not _is_loopback_host(host):
        raise ValueError(
            f"refusing to bind {host!r}: `levain init --web` is loopback-only "
            "(127.0.0.1 / localhost). Onboarding writes operator-private seed/config "
            "and must not be reachable off this machine — there is no off-box init."
        )
    if adapter is not None and adapter not in _ADAPTERS:
        raise ValueError(f"unknown adapter {adapter!r}: must be one of {list(_ADAPTERS)}")

    assets = {fn: load_web_asset(fn).encode("utf-8") for fn, _ in _ASSETS.values()}
    httpd = _InitServer((host, port), _InitHandler)
    httpd.install = install
    httpd.force = force
    httpd.default_adapter = adapter
    httpd.python_path = sys.executable
    httpd.anneal_path = shutil.which("anneal-memory") or "anneal-memory"
    httpd.assets = assets
    # Re-verify the ACTUAL bound address is loopback (verify-the-bound-address, not
    # the resolver — a tampered hosts file could map 'localhost' off-box). Refuse
    # otherwise: an off-box init server would expose the GET form + run installs.
    bound = str(httpd.server_address[0])
    if not _is_loopback_host(bound):
        httpd.server_close()
        raise ValueError(
            f"refusing to serve: requested host {host!r} bound a non-loopback address "
            f"({bound}). `levain init --web` is loopback-only."
        )
    # Allow the canonical loopback names + the exact bound address (covers a
    # 127.0.0.x bind), lowercased to match the Host check; any other Host → 403.
    httpd.allowed_hosts = _LOOPBACK_HOSTS | {bound.lower()}
    httpd.request_gate = threading.BoundedSemaphore(_MAX_INFLIGHT)
    httpd.install_lock = threading.Lock()
    return httpd


def run_init_web(
    path: Path,
    *,
    adapter: str | None = None,
    force: bool = False,
    host: str = DEFAULT_INIT_HOST,
    port: int = DEFAULT_INIT_PORT,
    open_browser: bool = True,
) -> int:
    """``levain init --web`` entry point — serve the onboarding form on localhost.

    Returns nonzero only if the bind fails or the host is refused; the install
    itself happens per-POST and reports inline. Blocks in ``serve_forever`` until
    interrupted (Ctrl+C → clean exit 0)."""
    install = Path(str(path)).expanduser().resolve()
    if install.exists() and not install.is_dir():
        print(
            f"FAIL: {install} exists but is not a directory.\n"
            f"      Pass --path pointing at a directory (or a non-existent path).",
            file=sys.stderr,
        )
        return 1

    try:
        httpd = make_init_server(install, adapter=adapter, force=force, host=host, port=port)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        # The web assets load (from package data) BEFORE the bind — a missing asset is a
        # packaging failure, NOT a port-in-use bind error (L1 review: don't misreport it).
        print(
            f"Levain web assets not found in the installed package ({exc}). The wheel may "
            f"be corrupt; reinstall with `pip install --force-reinstall levain`.",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(
            f"Could not bind {host}:{port} — {exc}.\n"
            "The port may be in use; try `levain init --web --port <N>`.",
            file=sys.stderr,
        )
        return 1

    bound_host, bound_port = str(httpd.server_address[0]), httpd.server_address[1]
    host_for_url = f"[{bound_host}]" if ":" in bound_host else bound_host
    url = f"http://{host_for_url}:{bound_port}/"
    print(f"Levain init → {url}")
    print(f"  install target: {install}{'  (--force)' if force else ''}")
    print("  loopback-only · governed · Ctrl+C to stop")

    if open_browser:
        import webbrowser

        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — a headless box without a browser is fine
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()
    return 0
