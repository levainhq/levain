"""The Claude identity call's slot side (CONTRACT §7.6; 1027-18 RULINGS IG-3): run AS THE SLOT, it reads the slot's
own OAuth access token from ``$CLAUDE_CONFIG_DIR/.credentials.json`` (``claudeAiOauth.accessToken``) and makes ONE
request, ``GET`` :data:`PROFILE_URL` with ``Authorization: Bearer <token>`` and ``anthropic-beta`` :data:`OAUTH_BETA`,
through the session's passthrough tunnel (the ``HTTPS_PROXY`` its env file names). Its stdout is ONE canonical JSON line
``{"status": <http status>, "body": <the response text>}`` for root to judge (:func:`levain.slot.identity.
parse_profile_answer`); a failure before an answer is ``{"error": <a fixed name>}``. The token stays inside this
process: it is never printed, logged or put in an error. Stdlib only and 3.9-valid (it runs under the root tree's
interpreter).

NOT WIRED YET: no wall target launches this module (handoff in 1027-18/idg1/RETURN.md); the Claude call still refuses
``identity-call-unbuilt``.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Callable, Dict, Mapping, Optional

PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
OAUTH_BETA = "oauth-2025-04-20"
CREDENTIALS_NAME = ".credentials.json"
#: the most of the response body read; with the JSON wrapper the line stays under root's 64 KiB read.
BODY_MAX = 48 * 1024
CRED_MAX = 64 * 1024
TIMEOUT_S = 20.0


def read_token(env: Mapping[str, str]) -> Optional[str]:
    """The access token from the slot's own credential file, or None (no config dir, no file, not the shape)."""
    cfg = env.get("CLAUDE_CONFIG_DIR")
    if not cfg:
        return None
    try:
        with open(os.path.join(cfg, CREDENTIALS_NAME), "rb") as f:
            raw = f.read(CRED_MAX + 1)
    except OSError:
        return None
    if len(raw) > CRED_MAX:
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    tok = obj.get("claudeAiOauth", {}).get("accessToken") if isinstance(obj, dict) else None
    if not isinstance(tok, str) or not tok or any(c in tok for c in "\r\n\x00"):
        return None
    return tok


def _opener(env: Mapping[str, str]) -> urllib.request.OpenerDirector:
    """The tunnel only: ``HTTPS_PROXY`` from the env file, never the host's own proxy settings."""
    proxy = env.get("HTTPS_PROXY") or env.get("https_proxy")
    return urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy} if proxy else {}))


def fetch(env: Mapping[str, str], *, url: str = PROFILE_URL,
          opener: Optional[Callable[[Mapping[str, str]], urllib.request.OpenerDirector]] = None) -> Dict[str, object]:
    """The one request, as this process's answer object (never raises; never carries the token)."""
    tok = read_token(env)
    if tok is None:
        return {"error": "no-token"}
    req = urllib.request.Request(url, method="GET", headers={
        "Authorization": f"Bearer {tok}", "anthropic-beta": OAUTH_BETA, "Accept": "application/json"})
    try:
        resp = (opener or _opener)(env).open(req, timeout=TIMEOUT_S)
    except urllib.error.HTTPError as e:
        e.close()
        return {"status": int(e.code), "body": ""}
    except Exception:   # noqa: BLE001 - the name only: an error text could quote the request
        return {"error": "request-failed"}
    try:
        status = int(resp.status)
        body = resp.read(BODY_MAX + 1)
    except Exception:   # noqa: BLE001
        return {"error": "request-failed"}
    finally:
        resp.close()
    if len(body) > BODY_MAX:
        return {"error": "body-too-large"}
    return {"status": status, "body": body.decode("utf-8", "replace")}


def main(argv=None, *, env: Optional[Mapping[str, str]] = None, out=None) -> int:
    ans = fetch(os.environ if env is None else env)
    (out or sys.stdout).write(json.dumps(ans, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")
    return 0 if "status" in ans else 1


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main())
