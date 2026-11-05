"""The Claude identity call's slot side (CONTRACT §7.6; P7-A spec N-A1, Phill's np-26f9a7f9 (a)): run AS THE SLOT, it
asks the unmodified Claude Code binary ``claude auth status --json`` and reports a digest of the sign-in that answer
names.

Levain's code never opens the slot's login file and never speaks to a sign-in server: the binary reads its own
credential and decides for itself whether to ask anyone. U-A5 in the spec (does ``auth status`` ask the server or read
local data?) was measured on the operator's signed-in VM slot 2026-11-05 (levain 38, RETURN_c1p addendum): it reads
local data. The answer is therefore a LOCAL-DATA claim (:data:`CLAIM`), "local data says this sign-in"; root
(:func:`levain.slot.identity.parse_auth_answer`) holds the same word. The binary takes the account (email, orgId)
from the ``oauthAccount`` block of the session's ``.claude.json``, which render seeds from the block the slot's login
parked (np-8645041e (a), :func:`levain.slot.end.park_move`). So the account is a recorded LABEL: the parked record
names an account, it does not tell which account the credential belongs to, and it does not see an expired login
(``loggedIn`` comes from the credential's access token, with no expiry check and no server call).

This replaced the earlier child that refreshed the OAuth token itself: that refresh was a first-party-only act.

The observed id is :func:`id_digest`: the SHA-256 (hex) of the binary's ``orgId`` and its lower-cased ``email``,
joined by a NUL. ``orgId`` alone names an organisation, and every member of one Team or Enterprise organisation shares
it (L3 r1, 1102-5): it would read a switch from one member to another as the same sign-in. The email is read here,
hashed here, and never written: the line carries the digest only. A binary output with no email fails closed
(``bad-shape``): an organisation-only id never passes as a per-person one.

Before ``auth status`` the child asks the binary ``--version`` and refuses one older than :data:`MIN_VERSION`
(``binary-too-old``): those builds could lose a sign-in when a short command exited during a token refresh.

The child runs through wall's ``identity`` target, inside the session's tunnel and sandbox, with the session's env file
as its environment (``CLAUDE_CONFIG_DIR``, ``HOME``, the proxy). It prints ONE line on stdout:

* ``{"claim":"local-data","id_digest":"<64 hex>","logged_in":true}``: the binary's JSON said ``loggedIn`` true and
  named an ``orgId`` that is a UUID and an ``email``.
* ``{"claim":"local-data","logged_in":false}``: the binary said ``loggedIn`` false.
* ``{"error":"<name>"}``: ``no-binary``, ``spawn-failed``, ``timeout``, ``exit-nonzero``, ``output-too-large``,
  ``not-json``, ``bad-shape`` (valid JSON of any shape but the two above: fail closed), ``version-unparsable`` or
  ``binary-too-old``.

Exit status 0 only for a logged-in answer. Nothing the binary printed is passed on but the digest.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

#: what the answer is: the binary's own report from its local state (U-A5 measured: local data, never the server).
CLAIM = "local-data"
#: the origins the identity child's tunnel traffic may reach (the supervisor's allowlist names these hosts); the child
#: itself makes no request.
ORIGIN = ("https", "api.anthropic.com", 443)
#: the OAuth token host: the vendor binary refreshes and revokes its own token there, through the same tunnel, so the
#: passthrough route must reach it. Levain itself makes no refresh request.
TOKEN_ORIGIN = ("https", "platform.claude.com", 443)
#: the vendor binary root installed (``root_tree.VENDOR_PREFIX`` + ``bin/claude``); a test holds the two equal.
VENDOR_BINARY = {"darwin": "/usr/local/lib/levain-vendor/bin/claude", "linux": "/usr/lib/levain-vendor/bin/claude"}
AUTH_ARGS = ("auth", "status", "--json")
VERSION_ARGS = ("--version",)
#: The oldest Claude Code this child runs ``auth status`` on. Anthropic's 2.1.301 release notes
#: (https://github.com/anthropics/claude-code/releases/tag/v2.1.301, and the repo's CHANGELOG.md under 2.1.301):
#: "Fixed sign-in refresh timeouts and unexpected sign-outs after a short command such as `claude auth status` or
#: `claude plugin list` exited in the middle of a token refresh". An older binary asked here could spend the slot's
#: refresh token.
MIN_VERSION = (2, 1, 301)
#: the most of the binary's stdout kept; one byte past it is an overflow.
OUT_MAX = 48 * 1024
#: each run of the binary; inside root's capture bound.
DEADLINE_S = 60.0
_UUID_RE = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}", re.ASCII)
_VERSION_RE = re.compile(rb"\s*v?(\d{1,6})\.(\d{1,6})\.(\d{1,6})(?=\s|\Z)", re.ASCII)
_EMAIL_MAX = 320
_READ_CHUNK = 64 * 1024

#: ``runner(argv, env, timeout_s) -> (exit status, stdout bytes)``; raises ``TimeoutError`` when the run outlasts
#: ``timeout_s`` and ``OSError`` when it cannot start. Stdout past :data:`OUT_MAX` comes back as ``OUT_MAX + 1`` bytes.
#: Tests inject their own.
Runner = Callable[[Sequence[str], Mapping[str, str], float], Tuple[int, bytes]]


def _kill_group(p: subprocess.Popen) -> None:
    """SIGKILL the run's process group (its own session, so the binary's helpers die with it), then reap it."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        p.kill()
    except ProcessLookupError:
        pass
    try:
        p.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pass


def _run(argv: Sequence[str], env: Mapping[str, str], timeout_s: float) -> Tuple[int, bytes]:
    """The binary run in a new session, its stdout drained as it arrives under one deadline. At most ``OUT_MAX + 1``
    bytes are ever held: on the byte past :data:`OUT_MAX` the group is killed and those bytes returned (the caller
    names the overflow). Past the deadline (a descendant that keeps the pipe open included) the group is killed and
    ``TimeoutError`` raised. A run that ends on its own is not signalled."""
    deadline = time.monotonic() + timeout_s
    p = subprocess.Popen(list(argv), env=dict(env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
    buf = bytearray()
    try:
        fd = p.stdout.fileno()
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                _kill_group(p)
                raise TimeoutError
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
                continue
            chunk = os.read(fd, min(_READ_CHUNK, OUT_MAX + 1 - len(buf)))
            if not chunk:
                break
            buf += chunk
            if len(buf) > OUT_MAX:
                _kill_group(p)
                return (p.returncode if p.returncode is not None else -signal.SIGKILL), bytes(buf)
        try:
            code = p.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _kill_group(p)
            raise TimeoutError from None
        return code, bytes(buf)
    except BaseException:
        if p.returncode is None:
            _kill_group(p)
        raise
    finally:
        p.stdout.close()


def _binary() -> str:
    return VENDOR_BINARY["darwin" if sys.platform == "darwin" else "linux"]


def _parse(raw: bytes) -> Optional[Dict[str, object]]:
    """The binary's stdout as a dict, or None when it is not JSON object text."""
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def parse_version(raw: bytes) -> Optional[Tuple[int, int, int]]:
    """The ``major.minor.patch`` that leads ``claude --version``'s output (``2.1.316 (Claude Code)``), or None."""
    m = _VERSION_RE.match(raw)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def id_digest(org_id: str, email: str) -> Optional[str]:
    """SHA-256 hex of ``orgId`` (lower-cased) + NUL + ``email`` (lower-cased), or None when either does not fit: an
    ``orgId`` that is not a UUID, an email that is not a non-empty string holding ``@`` and no NUL, within 320
    characters."""
    if not (isinstance(org_id, str) and _UUID_RE.fullmatch(org_id)):
        return None
    if not (isinstance(email, str) and 0 < len(email) <= _EMAIL_MAX and "@" in email and "\0" not in email
            and email.strip() == email):
        return None
    try:
        data = org_id.lower().encode("ascii") + b"\0" + email.lower().encode("utf-8")
    except UnicodeEncodeError:
        return None
    return hashlib.sha256(data).hexdigest()


def _ask(runner: Runner, argv: Sequence[str], env: Mapping[str, str]):
    """One run: ``(code, raw)``, or an error answer by name."""
    try:
        code, raw = runner(argv, env, DEADLINE_S)
    except TimeoutError:
        return {"error": "timeout"}
    except Exception:   # noqa: BLE001 - an error text could quote the environment
        return {"error": "spawn-failed"}
    if not isinstance(raw, bytes) or len(raw) > OUT_MAX:
        return {"error": "output-too-large"}
    return code, raw


def fetch(env: Mapping[str, str], *, runner: Optional[Runner] = None, binary: Optional[str] = None) -> Dict[str, object]:
    """The binary's version checked, then its ``auth status --json`` reduced to this process's answer object (never
    raises; never carries a value the binary printed). ``runner`` and ``binary`` are for tests: with no ``runner``
    the binary must exist and be executable, else ``no-binary``."""
    exe = binary or _binary()
    if runner is None:
        if not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
            return {"error": "no-binary"}
        runner = _run
    got = _ask(runner, (exe, *VERSION_ARGS), env)
    if isinstance(got, dict):
        return got
    code, raw = got
    version = parse_version(raw) if code == 0 else None
    if version is None:
        return {"error": "version-unparsable"}
    if version < MIN_VERSION:
        return {"error": "binary-too-old"}
    got = _ask(runner, (exe, *AUTH_ARGS), env)
    if isinstance(got, dict):
        return got
    code, raw = got
    obj = _parse(raw)
    if obj is None:
        return {"error": "exit-nonzero" if code != 0 else "not-json"}
    logged = obj.get("loggedIn")
    if logged is False:
        return {"claim": CLAIM, "logged_in": False}
    if code != 0:
        return {"error": "exit-nonzero"}
    digest = id_digest(obj.get("orgId"), obj.get("email")) if logged is True else None
    if digest is None:
        return {"error": "bad-shape"}
    return {"claim": CLAIM, "logged_in": True, "id_digest": digest}


def main(argv=None, *, env: Optional[Mapping[str, str]] = None, out=None, runner: Optional[Runner] = None) -> int:
    ans = fetch(os.environ if env is None else env, runner=runner)
    (out or sys.stdout).write(json.dumps(ans, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n")
    return 0 if ans.get("logged_in") is True else 1


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main())
