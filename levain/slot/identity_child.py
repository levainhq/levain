"""The Claude identity call's slot side (CONTRACT §7.6; P7-A spec N-A1, Phill's np-26f9a7f9 (a)): run AS THE SLOT, it
asks the unmodified Claude Code binary ``claude auth status --json`` and reports the account that answer names.

Levain's code never opens the slot's login file and never speaks to a sign-in server: the binary reads its own
credential and decides for itself whether to ask anyone (U-A5 in the spec: whether ``auth status`` asks the server or
reads local data is MEASURED on the operator's signed-in VM slot, not assumed). The answer is therefore reported as a
LOCAL-DATA claim (:data:`CLAIM`), "local data says this account", until that measurement says otherwise; root
(:func:`levain.slot.identity.parse_auth_answer`) holds the same word.

This replaces the earlier child that refreshed the OAuth token itself: that refresh was a first-party-only act.

The child runs through wall's ``identity`` target, inside the session's tunnel and sandbox, with the session's env file
as its environment (``CLAUDE_CONFIG_DIR``, ``HOME``, the proxy). It prints ONE line on stdout:

* ``{"claim":"local-data","logged_in":true,"org_id":"<uuid>"}``: the binary's JSON said ``loggedIn`` true and named an
  ``orgId`` that is a UUID. The output has no account-uuid key (measured on Claude Code's own output); ``orgId`` is the
  stable id it carries, and the email is never read.
* ``{"claim":"local-data","logged_in":false}``: the binary said ``loggedIn`` false.
* ``{"error":"<name>"}``: ``no-binary``, ``spawn-failed``, ``timeout``, ``exit-nonzero``, ``output-too-large``,
  ``not-json`` or ``bad-shape`` (valid JSON of any shape but the two above: fail closed).

Exit status 0 only for a logged-in answer. Nothing the binary printed beyond those two fields is passed on.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

#: what the answer is: the binary's own report from its local state, until U-A5 shows it asks the server.
CLAIM = "local-data"
#: the origins the identity child's tunnel traffic may reach (the supervisor's allowlist names these hosts); the child
#: itself makes no request.
ORIGIN = ("https", "api.anthropic.com", 443)
TOKEN_ORIGIN = ("https", "platform.claude.com", 443)
#: the vendor binary root installed (``root_tree.VENDOR_PREFIX`` + ``bin/claude``); a test holds the two equal.
VENDOR_BINARY = {"darwin": "/usr/local/lib/levain-vendor/bin/claude", "linux": "/usr/lib/levain-vendor/bin/claude"}
AUTH_ARGS = ("auth", "status", "--json")
#: the most of the binary's stdout read.
OUT_MAX = 48 * 1024
#: the binary's whole run; inside root's capture bound.
DEADLINE_S = 60.0
_UUID_RE = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}", re.ASCII)

#: ``runner(argv, env, timeout_s) -> (exit status, stdout bytes)``; raises ``TimeoutError`` when the run outlasts
#: ``timeout_s`` and ``OSError`` when it cannot start. Tests inject their own.
Runner = Callable[[Sequence[str], Mapping[str, str], float], Tuple[int, bytes]]


def _run(argv: Sequence[str], env: Mapping[str, str], timeout_s: float) -> Tuple[int, bytes]:
    try:
        p = subprocess.run(list(argv), env=dict(env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=timeout_s, close_fds=True, check=False)
    except subprocess.TimeoutExpired:
        raise TimeoutError from None
    return p.returncode, p.stdout


def _binary() -> str:
    return VENDOR_BINARY["darwin" if sys.platform == "darwin" else "linux"]


def _parse(raw: bytes) -> Optional[Dict[str, object]]:
    """The binary's stdout as a dict, or None when it is not JSON object text."""
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def fetch(env: Mapping[str, str], *, runner: Optional[Runner] = None, binary: Optional[str] = None) -> Dict[str, object]:
    """The binary's ``auth status --json``, reduced to this process's answer object (never raises; never carries a
    value the binary printed beyond ``org_id``). ``runner`` and ``binary`` are for tests: with no ``runner`` the binary
    must exist and be executable, else ``no-binary``."""
    exe = binary or _binary()
    if runner is None:
        if not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
            return {"error": "no-binary"}
        runner = _run
    try:
        code, raw = runner((exe, *AUTH_ARGS), env, DEADLINE_S)
    except TimeoutError:
        return {"error": "timeout"}
    except Exception:   # noqa: BLE001 - an error text could quote the environment
        return {"error": "spawn-failed"}
    if not isinstance(raw, bytes) or len(raw) > OUT_MAX:
        return {"error": "output-too-large"}
    obj = _parse(raw)
    if obj is None:
        return {"error": "exit-nonzero" if code != 0 else "not-json"}
    logged = obj.get("loggedIn")
    if logged is False:
        return {"claim": CLAIM, "logged_in": False}
    if code != 0:
        return {"error": "exit-nonzero"}
    org = obj.get("orgId")
    if logged is True and isinstance(org, str) and _UUID_RE.fullmatch(org):
        return {"claim": CLAIM, "logged_in": True, "org_id": org.lower()}
    return {"error": "bad-shape"}


def main(argv=None, *, env: Optional[Mapping[str, str]] = None, out=None, runner: Optional[Runner] = None) -> int:
    ans = fetch(os.environ if env is None else env, runner=runner)
    (out or sys.stdout).write(json.dumps(ans, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n")
    return 0 if ans.get("logged_in") is True else 1


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main())
