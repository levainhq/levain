"""levain.phone_server: the old phone port's redirect, and the steps the cockpit borrows (W1b).

``levain consent phone --redirect-to <origin>`` (ONESURFACE OS-4) runs this port as a redirect ONLY
(:func:`run_phone_redirect`): ``/`` and ``/phone.html`` serve one page whose one script sends an old phone link
(``#phone_token=...&sign=<id>&v=...&choice=...``) to the cockpit's sheet, ``<origin>/sign/<id>#v=...&choice=...``,
dropping the token; every other path is 404, and no token is minted or read. ``levain consent phone`` without
``--redirect-to`` refuses: the run token, the token-gated data routes and the paired-device registry of the phone
port are gone (W1b), and a paired device's credential is the cockpit's (``levain cockpit pair``).

:class:`_PhoneServer` is never instantiated: the cockpit's sheets call its step methods unbound on their own ``self``
(``answer_step``, ``rule_step``, ``_source``, ``_deliver_later``, ``key_line``; ``levain.web_server``). Moving them
into a cockpit module is a later row.

Why a separate listener: a proxy's clients and the laptop's own processes share 127.0.0.1, so no header the cockpit
reads can tell them apart (measured through ``tailscale serve`` 2026-10-16: the Host is the client's, X-Forwarded-For
is the proxy's). A listener that serves nothing else needs no such test.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import json
import shlex
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any

from levain.http_guards import LISTEN_BACKLOG, GuardedHandler
from http.server import ThreadingHTTPServer

__all__ = ["DEFAULT_PHONE_PORT", "PhoneError", "make_redirect_server", "redirect_origin", "run_phone_redirect",
           "tailscale_name"]

DEFAULT_PHONE_PORT = 7428
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# the key a ruling's result carries its delivery's waiter under (taken off before the result is sent)
_AWAIT = "_await_delivery"



class PhoneError(Exception):
    """A refusal a route answers with: an HTTP status, an error word and the words the page shows."""

    def __init__(self, status: int, error: str, message: str, extra: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status, self.error, self.message, self.extra = status, error, message, extra or {}


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")



class _PhoneServer:
    """Never instantiated: the cockpit calls these steps unbound, on its own ``self`` (module docstring)."""

    def _source(self, root: Path) -> Any:
        from levain.authority import anchor_path
        from levain.consent.queue import authority_source

        return authority_source(root / "cockpit.db", anchor=anchor_path(root), cache=self.replay_cache)

    def answer_step(self, request_id: str, action: str, req: Any) -> dict[str, Any]:
        """``challenge`` ({decision, signer_fp}) -> the answer record and its WebAuthn challenge; ``answer``
        ({payload, authenticator_data, client_data_json, signature}, base64url) -> the queue's verdict. The
        ``rule-`` pair does the same for a late ruling on an undecided request (:meth:`rule_step`)."""
        if action in ("rule-challenge", "rule"):
            return self.rule_step(request_id, action, req)
        from levain.authority import AuthorityError, VerifierUnavailable
        from levain.cockpit.consent import ConsentError
        from levain.consent.answering import answer_root, passkey_keys, refuse_cut_display
        from levain.consent.passkey import PasskeyError, b64url_decode, challenge, to_sshsig
        from levain.consent.queue import AnswerRefused, ConsentQueue, QueueStoreError, authority_source, class_covers
        from levain.consent.record import DECISIONS, answer_message

        need = {"challenge": {"decision", "signer_fp"},
                "answer": {"payload", "authenticator_data", "client_data_json", "signature"}}[action]
        if not isinstance(req, dict) or set(req) != need:
            raise PhoneError(400, "bad_request", f"request keys must be {sorted(need)}")
        try:
            got = ConsentQueue(self.queue_dir, home=Path.home(),
                               authority=authority_source(self.queue_dir, anchor=None)).get(request_id)
            if got is None:
                raise PhoneError(404, "not_found", "no such consent request")
            request, state = got
            if state != "pending":
                raise PhoneError(409, "conflict", f"request {request_id} is already {state}")
            refuse_cut_display(request, "from a phone")
            source = self._source(answer_root(self.queue_dir, request))
            keys = {k.fingerprint: k for k in passkey_keys(source(), self.rp_id)}
            if action == "challenge":
                decision, fp = req["decision"], req["signer_fp"]
                if decision not in DECISIONS:
                    raise PhoneError(400, "bad_request", f"decision must be one of {list(DECISIONS)}")
                if not isinstance(fp, str) or fp not in keys:
                    raise PhoneError(422, "no_passkey", f"{str(fp)[:80]} is not an active passkey enrolled for "
                                                        f"{self.rp_id}")
                if decision == "allow" and not class_covers(keys[fp].key_class, request.tier):
                    raise PhoneError(422, "answer_refused", f"a {keys[fp].key_class} passkey cannot allow a "
                                                            f"{request.tier} request: allow it from the laptop")
                payload = ConsentQueue(self.queue_dir, home=Path.home(),
                                       authority=source).answer_payload(request_id, decision, fp)
                return {"payload": payload, "rp_id": self.rp_id, "challenge": _b64u(challenge(answer_message(payload)))}
            payload = req["payload"]
            fp = payload.get("signer_fp") if isinstance(payload, dict) else None
            key = keys.get(fp) if isinstance(fp, str) else None
            if key is None:
                raise PhoneError(422, "no_passkey", f"the answer's signer is not an active passkey enrolled for "
                                                    f"{self.rp_id}")
            signature = to_sshsig(key.public_key, b64url_decode(req["authenticator_data"], "authenticator_data"),
                                  b64url_decode(req["client_data_json"], "client_data_json"),
                                  b64url_decode(req["signature"], "signature"), page_origin=self.page_origin)
            decided = ConsentQueue(self.queue_dir, home=Path.home(),
                                   authority=source).answer(request_id, payload, signature)
        except (AnswerRefused, PasskeyError, ConsentError) as exc:
            raise PhoneError(422, "answer_refused", str(exc)) from None
        except (AuthorityError, VerifierUnavailable, QueueStoreError, OSError, sqlite3.Error) as exc:
            raise PhoneError(503, "unavailable", f"the consent queue cannot answer now: {exc}") from None
        return {"request_id": decided.request_id, "decision": decided.decision, "signer_fp": decided.signer_fp,
                "decided_at": decided.decided_at}

    def rule_step(self, request_id: str, action: str, req: Any, *, deliver: bool = True) -> dict[str, Any]:
        """``rule-challenge`` ({decision, signer_fp}) -> the ruling record and its WebAuthn challenge, in the ruling
        namespace; ``rule`` ({payload, authenticator_data, client_data_json, signature}) -> the queue's verdict on the
        ruling (:meth:`ConsentQueue.answer_late`: only an undecided request takes one), then its delivery (with
        ``deliver`` false none is started: the ruling's text then reaches the session at its next hook)."""
        from levain.authority import AuthorityError, VerifierUnavailable
        from levain.cockpit.consent import ConsentError
        from levain.consent.answering import answer_root, passkey_keys, refuse_cut_display
        from levain.consent.passkey import PasskeyError, b64url_decode, challenge, to_sshsig
        from levain.consent.queue import AnswerRefused, ConsentQueue, QueueStoreError, authority_source, class_covers
        from levain.consent.record import DECISIONS, RULING_NAMESPACE, ruling_message

        need = {"rule-challenge": {"decision", "signer_fp"},
                "rule": {"payload", "authenticator_data", "client_data_json", "signature"}}[action]
        if not isinstance(req, dict) or set(req) != need:
            raise PhoneError(400, "bad_request", f"request keys must be {sorted(need)}")
        try:
            got = ConsentQueue(self.queue_dir, home=Path.home(),
                               authority=authority_source(self.queue_dir, anchor=None)).get(request_id)
            if got is None:
                raise PhoneError(404, "not_found", "no such consent request")
            request, state = got
            if state != "undecided":
                raise PhoneError(409, "conflict", f"request {request_id} is {state}, not undecided: a ruling answers "
                                                  "only an undecided request")
            decision = req["decision"] if action == "rule-challenge" else (
                req["payload"].get("decision") if isinstance(req["payload"], dict) else None)
            if decision != "deny":
                refuse_cut_display(request, "from a phone")   # a deny is signed even over a cut display
            source = self._source(answer_root(self.queue_dir, request))
            keys = {k.fingerprint: k for k in passkey_keys(source(), self.rp_id)}
            queue = ConsentQueue(self.queue_dir, home=Path.home(), authority=source)
            if action == "rule-challenge":
                decision, fp = req["decision"], req["signer_fp"]
                if decision not in DECISIONS:
                    raise PhoneError(400, "bad_request", f"decision must be one of {list(DECISIONS)}")
                if not isinstance(fp, str) or fp not in keys:
                    raise PhoneError(422, "no_passkey", f"{str(fp)[:80]} is not an active passkey enrolled for "
                                                        f"{self.rp_id}")
                if decision == "allow" and not class_covers(keys[fp].key_class, request.tier):
                    raise PhoneError(422, "answer_refused", f"a {keys[fp].key_class} passkey cannot allow a "
                                                            f"{request.tier} request: rule it from the laptop")
                payload = queue.ruling_payload(request_id, decision, fp)
                return {"payload": payload, "rp_id": self.rp_id,
                        "challenge": _b64u(challenge(ruling_message(payload), RULING_NAMESPACE))}
            payload = req["payload"]
            fp = payload.get("signer_fp") if isinstance(payload, dict) else None
            key = keys.get(fp) if isinstance(fp, str) else None
            if key is None:
                raise PhoneError(422, "no_passkey", f"the ruling's signer is not an active passkey enrolled for "
                                                    f"{self.rp_id}")
            signature = to_sshsig(key.public_key, b64url_decode(req["authenticator_data"], "authenticator_data"),
                                  b64url_decode(req["client_data_json"], "client_data_json"),
                                  b64url_decode(req["signature"], "signature"), namespace=RULING_NAMESPACE,
                                  page_origin=self.page_origin)
            ruled = queue.answer_late(request_id, payload, signature)
        except (AnswerRefused, PasskeyError, ConsentError) as exc:
            raise PhoneError(422, "answer_refused", str(exc)) from None
        except (AuthorityError, VerifierUnavailable, QueueStoreError, OSError, sqlite3.Error) as exc:
            raise PhoneError(503, "unavailable", f"the consent queue cannot answer now: {exc}") from None
        out = {"request_id": ruled.request_id, "decision": ruled.ruling, "signer_fp": ruled.signer_fp,
               "decided_at": ruled.decided_at, "gap": ruled.gap}
        if not deliver:
            return out
        return {**out, "delivery": {"route": "pending", "reason": "delivery is running"},
                _AWAIT: self._deliver_later(queue, ruled.request_id, request.agent)}

    def _deliver_later(self, queue: Any, request_id: str, agent: str) -> Any:
        """Start the ruling's delivery on its own thread; returns ``wait(timeout)`` -> its route and reason, or that it
        is still running. A delivery that raises is reported ``failed``; the ruling stands either way."""
        from levain.consent.delivery import agent_notify, deliver

        box: dict[str, Any] = {}
        done = threading.Event()

        def run() -> None:
            try:
                d = deliver(queue, request_id, notify=agent_notify(self.queue_dir, agent, request_id))
                box["d"] = {"route": d.route, "reason": d.reason}
            except Exception as exc:  # noqa: BLE001 - the ruling stands; `levain consent deliver` retries it
                box["d"] = {"route": "failed", "reason": f"delivery raised {type(exc).__name__}; the ruling stands: "
                                                         "`levain consent deliver` delivers it"}
            finally:
                done.set()

        threading.Thread(target=run, name="levain-ruling-delivery", daemon=True).start()

        def wait(timeout: float) -> dict[str, Any]:
            if done.wait(timeout):
                return box["d"]
            return {"route": "pending", "reason": "delivery is still running; the ping says where it lands"}

        return wait

    def key_line(self, req: Any) -> dict[str, Any]:
        """``POST /passkey/key-line`` ({spki}: the new credential's ``getPublicKey()``, base64url; optionally
        ``authenticator_data``, its creation's ``getAuthenticatorData()``) -> its OpenSSH public key on this server's
        name, its fingerprint, and the laptop commands that enrol it. Nothing is enrolled or recorded here: a creation
        that attests this key rides the enrol command as ``--attested``, which the laptop's signed enrolment checks
        again and records (:mod:`levain.cockpit.keyfacts`); one that does not is refused (422)."""
        from levain.cockpit import keyfacts
        from levain.cockpit.keys import fingerprint_of
        from levain.consent.answering import hook_roots
        from levain.consent.passkey import PasskeyError, b64url_decode, public_key_line

        if not isinstance(req, dict) or not {"spki"} <= set(req) <= {"spki", "authenticator_data"}:
            raise PhoneError(400, "bad_request", "request keys must be ['spki'], and optionally 'authenticator_data'")
        try:
            line = public_key_line(b64url_decode(req["spki"], "spki"), self.rp_id)
        except PasskeyError as exc:
            raise PhoneError(422, "bad_passkey", str(exc)) from None
        attested = ""
        if "authenticator_data" in req:
            try:
                raw = b64url_decode(req["authenticator_data"], "authenticator_data")
                keyfacts.attested(raw, line)
            except (PasskeyError, keyfacts.KeyFactsError) as exc:
                raise PhoneError(422, "bad_passkey", str(exc)) from None
            # re-encoded from the bytes checked, so the command carries nothing but base64url; joined by "=" since
            # the value starts with "-" for some rp_ids, which argparse would read as an option
            attested = " --attested=" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        # one file per passkey: two devices enrolled on one name must not overwrite each other's file
        name = f"passkey-{self.rp_id.split('.')[0]}-{hashlib.sha256(line.encode('ascii')).hexdigest()[:8]}.pub"
        roots = hook_roots(self.queue_dir)
        enrol = [f"levain cockpit enroll {name} --class display-phone --custody passkey --data-root {shlex.quote(str(r))} "
                 f"--signing-key YOUR_LAPTOP_PRESENCE_KEY{attested}" for r in roots]
        return {"public_key": line, "fingerprint": fingerprint_of(line), "file": name,
                "commands": [f"printf '%s\\n' '{line}' > {name}", *enrol],
                "note": None if roots else ("no consent hook names a cockpit yet (run `levain init` with the consent "
                                            "hook first); then enrol into the cockpit it names")}



def _is_loopback_address(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


_TAILSCALE_SOCKETS = (None, "/var/run/tailscale/tailscaled.sock")   # the CLI's default, then the open-source daemon's


def tailscale_name(timeout: float = 5.0) -> "tuple[str | None, str]":
    """``(this machine's Tailscale DNS name, "")``, or ``(None, why not)``: read from ``tailscale status --json``
    (the default socket, then ``/var/run/tailscale/tailscaled.sock``). Nothing is changed in Tailscale."""
    import shutil
    import subprocess

    from levain.consent.passkey import is_host_name
    from levain.launch import child_env

    exe = shutil.which("tailscale")
    if exe is None:
        return None, "no `tailscale` command on PATH; pass --name for any https proxy instead"
    why = "tailscale is not running"
    for sock in _TAILSCALE_SOCKETS:
        argv = [exe, *([] if sock is None else [f"--socket={sock}"]), "status", "--json"]
        try:
            cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=child_env())
            data = json.loads(cp.stdout) if cp.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            data = None
        if not isinstance(data, dict):
            continue
        if data.get("BackendState") != "Running":
            why = f"tailscale is {data.get('BackendState')!r}, not Running"
            continue
        name = str((data.get("Self") or {}).get("DNSName") or "").rstrip(".").lower()
        if is_host_name(name):
            return name, ""
        why = "tailscale reports no DNS name for this machine (MagicDNS off?)"
    return None, why




def _drop_link_file(path: Path) -> None:
    """``--link-file`` removed when this run stops, so no caller builds a link to a server that is gone; left alone
    when it names another run (a later server wrote it). A kill -9 or a crash leaves it: a caller checks its pid."""
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError, AttributeError):
        pass


# -- ONESURFACE OS-4: the old phone origin as a redirect only ---------------------------------------------------------

_REDIRECT_PAGES = frozenset({"/", "/phone.html"})
_REDIRECT_SCRIPT = "/phone_redirect.js"


def redirect_origin(text: object) -> str:
    """``--redirect-to``: the cockpit's bare origin by :func:`levain.consent.sign.cockpit_origin`'s one rule (the
    rule sign links are built by). The page sends every link it is opened with there, so it is configured whole,
    never read from a link."""
    from levain.consent.sign import cockpit_origin

    try:
        return cockpit_origin(text)
    except ValueError:
        raise ValueError(f"--redirect-to: {str(text)[:80]!r} is not the cockpit's origin (https://<name>[:<port>], "
                         "nothing after it)") from None


def _redirect_page(origin: str) -> str:
    """The page the old phone link opens: no data, one script, the target in a meta the script reads."""
    import html

    esc = html.escape(origin, quote=True)
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="referrer" content="no-referrer">'
            f'<meta name="levain-redirect-to" content="{esc}">'
            f'<title>levain</title><script src="{_REDIRECT_SCRIPT}"></script></head>'
            f'<body><p>This page moved to the cockpit.</p><p><a href="{esc}/">Open the cockpit</a></p></body></html>\n')


class _RedirectHandler(GuardedHandler):
    """``/`` and ``/phone.html`` (the page), ``/phone_redirect.js`` (its script); 404 for everything else, and no
    credential of any kind is read or minted."""

    server_version = "levain-phone"

    def _cross_site_read(self) -> bool:
        """The cross-site refusal, less a top-level navigation to the page: the old link opened from another app
        arrives cross-site. Its script and any fetch of the page stay refused."""
        if not super()._cross_site_read():
            return False
        return not (self.command == "GET" and self.path.split("?", 1)[0] in _REDIRECT_PAGES
                    and self.headers.get("Sec-Fetch-Mode") == "navigate"
                    and self.headers.get("Sec-Fetch-Dest") == "document")

    def _route(self, *, head: bool) -> None:
        if self._refuse_read(head=head):
            return
        route = self.path.split("?", 1)[0]
        no_referrer = [("Referrer-Policy", "no-referrer")]
        if route in _REDIRECT_PAGES:
            self._send(self.server.page, "text/html; charset=utf-8", head=head, headers=no_referrer)
        elif route == _REDIRECT_SCRIPT:
            self._send(self.server.script, "text/javascript; charset=utf-8", head=head, headers=no_referrer)
        else:
            self._send(b"not found: this port only opens the cockpit\n", "text/plain; charset=utf-8", status=404,
                       head=head)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._reject(404, "not_found", "this port only opens the cockpit")


class _RedirectServer(ThreadingHTTPServer):
    request_queue_size = LISTEN_BACKLOG
    daemon_threads = True

    def __init__(self, address: tuple[str, int], *, name: str, origin: str) -> None:
        from levain.web_server import load_web_asset

        # both built before binding: a fault leaves no socket
        script = load_web_asset("phone_redirect.js").encode("utf-8")
        page = _redirect_page(origin).encode("utf-8")
        super().__init__(address, _RedirectHandler)
        self.allowed_hosts = _LOOPBACK_HOSTS | {str(self.server_address[0]).lower(), name}
        self.origin = origin
        self.page = page
        self.script = script


def make_redirect_server(name: str, redirect_to: str, *, host: str = "127.0.0.1",
                         port: int = DEFAULT_PHONE_PORT) -> _RedirectServer:
    """A bound (not yet serving) redirect-only phone port for the https name ``name``, sending every old phone link to
    the cockpit at ``redirect_to`` (:func:`redirect_origin`). Loopback only, as the full port."""
    from levain.consent.passkey import is_host_name

    origin = redirect_origin(redirect_to)
    if host != "localhost" and not _is_loopback_address(host):
        raise ValueError(f"the phone port binds loopback only (a proxy you run puts it on https), not {host!r}")
    if not is_host_name(name):
        raise ValueError(f"refusing {str(name)[:80]!r}: the https name must be a lowercase DNS name a proxy serves "
                         "this port on (no scheme, port or path, not an address)")
    return _RedirectServer((host, port), name=name, origin=origin)


def run_phone_redirect(redirect_to: str, *, name: str | None, tailscale: bool, port: int,
                       link_file: Path | None = None) -> int:
    """``levain consent phone --redirect-to <origin>``: serve only the redirect page until interrupted. With
    ``link_file``, that file (0600) holds ``{"page": "<origin>/", "pid"}``, no token, and is removed when this run
    stops."""
    if name is None:
        if not tailscale:
            print("name the https name the old phone links name: --name NAME, or --tailscale", file=sys.stderr)
            return 2
        name, why = tailscale_name()
        if name is None:
            print(f"--tailscale: {why}", file=sys.stderr)
            return 2
    try:
        httpd = make_redirect_server(name, redirect_to, port=port)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    bound = httpd.server_address[1]
    print(f"levain phone port (redirect only) on http://127.0.0.1:{bound}/ for https://{name}/: every old phone "
          f"link opens on {httpd.origin}/", flush=True)
    import signal

    def _stop(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    before = (signal.signal(signal.SIGTERM, _stop)
              if link_file is not None and threading.current_thread() is threading.main_thread() else None)
    try:
        if link_file is not None:
            from levain.consent.sign import _write_over

            try:
                _write_over(link_file, json.dumps({"page": httpd.origin + "/", "pid": os.getpid()}) + "\n")
            except OSError as exc:
                print(f"--link-file: cannot write {link_file}: {exc.strerror or exc}", file=sys.stderr)
                return 1
            print(f"  the sign links' origin is in {link_file}", flush=True)
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if link_file is not None:
            _drop_link_file(link_file)
        if before is not None:
            signal.signal(signal.SIGTERM, before)
    return 0
