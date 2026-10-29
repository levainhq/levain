"""The Claude identity call's slot side (CONTRACT §7.6; 1027-18 RULINGS IG-3; PIN_REFRESH_DESIGN): run AS THE SLOT, it
reads the slot's own OAuth login from ``$CLAUDE_CONFIG_DIR/.credentials.json`` (``claudeAiOauth``), and does, in order,
through the session's passthrough tunnel (the ``HTTPS_PROXY`` its env file names; none is ``no-proxy``, no request):

1. REFRESH the login once: ``POST`` :data:`TOKEN_URL` (JSON ``grant_type`` / ``refresh_token`` / ``client_id`` /
   ``scope``, the body and the one header Claude Code's own refresh sends; M0, PIN_REFRESH_M0.md). The refresh token
   goes to :data:`TOKEN_ORIGIN` only. ONE attempt, never a retry: replaying a rotated token is what reuse detection
   punishes. The temp file is created BEFORE the request, so a failure to write cannot come after the token is spent.
2. WRITE the answer back before anything else: temp ``.credentials.json.levain-refresh`` (``O_EXCL|O_NOFOLLOW``, 0600)
   in the same directory, ``fsync``, ``rename`` onto the credential, directory ``fsync``. Only ``accessToken``,
   ``refreshToken`` (when the server returned one) and ``expiresAt`` (milliseconds, as Claude Code writes it) change.
3. ``GET`` :data:`PROFILE_URL` with ``Authorization: Bearer <the NEW access token, held in memory>`` and
   ``anthropic-beta`` :data:`OAUTH_BETA`. The bearer goes to :data:`ORIGIN` only. No redirect is followed anywhere.

Only the account that owns the file's refresh token can produce that access token, so the profile it names is the
file's real account (a file holding one account's access token beside another's refresh token refreshes into the
second).

Its stdout is ONE JSON line for root to judge (:func:`levain.slot.identity.parse_profile_answer`):
``{"refresh": 200, "status": <profile http status>, "body": <the profile text>}``; a refresh answered other than 200 is
``{"refresh": <http status>[, "oauth_error": <the answer's OAuth ``error`` word>]}``; a refresh with no answer, or an
answer unusable or unwritable, is ``{"refresh": <a fixed name>}``; any failure before a refresh is attempted is
``{"error": <a fixed name>}``. No token appears in it: they stay inside this process and are never printed, logged or
put in an error. Stdlib only and 3.9-valid (it runs under the root tree's interpreter). Launched by wall's
``identity`` target (:data:`levain.slot.wall.IDENTITY_MODULE`), from start's :func:`levain.slot.start.identity_capture`.
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, Mapping, Optional, Tuple

PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
#: (scheme, host, port): the one origin the bearer is sent to.
ORIGIN = ("https", "api.anthropic.com", 443)
OAUTH_BETA = "oauth-2025-04-20"
#: M0 (Claude Code 2.1.312's own strings): ``TOKEN_URL:"https://platform.claude.com/v1/oauth/token"``.
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
#: (scheme, host, port): the one origin the refresh token is sent to.
TOKEN_ORIGIN = ("https", "platform.claude.com", 443)
#: M0: ``CLIENT_ID:"9d1c250a-e61b-44d9-88ed-5944d1962f5e"``, the id Claude Code's refresh sends.
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CREDENTIALS_NAME = ".credentials.json"
TEMP_NAME = ".credentials.json.levain-refresh"
#: the most of the response body read. JSON escaping can grow it past root's read bound
#: (:data:`levain.slot.identity.PROFILE_ANSWER_MAX`); root refuses such a line ``identity-unparsable``.
BODY_MAX = 48 * 1024
#: the most of a refresh answer read.
REFRESH_BODY_MAX = 16 * 1024
CRED_MAX = 64 * 1024
TIMEOUT_S = 20.0
_OAUTH_WORD_RE = re.compile(r"[a-z_]{1,40}", re.ASCII)
_SCOPE_RE = re.compile(r"[A-Za-z0-9:_.\-]{1,64}", re.ASCII)
#: seam: tests inject a fault between the 200 and the rename.
_replace = os.replace


def _clean(tok: object) -> Optional[str]:
    """``tok`` when it is a non-empty string with no CR, LF or NUL (a header or body value), else None."""
    if not isinstance(tok, str) or not tok or any(c in tok for c in "\r\n\x00"):
        return None
    return tok


def _read_cred(env: Mapping[str, str]) -> Optional[Tuple[str, Dict[str, object]]]:
    """``(config dir, the parsed credential object)`` from the slot's own file, or None (no config dir, no file, over
    the cap, not JSON, not an object holding a ``claudeAiOauth`` object)."""
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
    if not isinstance(obj, dict) or not isinstance(obj.get("claudeAiOauth"), dict):
        return None
    return cfg, obj


def read_token(env: Mapping[str, str]) -> Optional[str]:
    """The access token from the slot's own credential file, or None (no config dir, no file, not the shape)."""
    cred = _read_cred(env)
    return _clean(cred[1]["claudeAiOauth"].get("accessToken")) if cred is not None else None   # type: ignore[union-attr]


def _proxy(env: Mapping[str, str]) -> Optional[str]:
    """The env file's ``HTTPS_PROXY`` (or ``https_proxy``), or None when neither is set to anything."""
    for name in ("HTTPS_PROXY", "https_proxy"):
        v = env.get(name)
        if v and v.strip():
            return v.strip()
    return None


def _origin(url: str) -> Tuple[str, Optional[str], Optional[int]]:
    u = urllib.parse.urlsplit(url)
    return (u.scheme, u.hostname, u.port if u.port is not None else {"https": 443, "http": 80}.get(u.scheme))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Every 3xx is the final answer: no second request, so a token is never resent anywhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):   # noqa: D102
        return None


def _opener(env: Mapping[str, str]) -> urllib.request.OpenerDirector:
    """The tunnel only: the env file's proxy, never the host's own proxy settings, and no redirect followed."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({"https": _proxy(env)}), _NoRedirect())


# --- the refresh (step 1 and 2) ----------------------------------------------------------------------------------------

def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _open_temp(cfg: str) -> int:
    return os.open(os.path.join(cfg, TEMP_NAME), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                   0o600)


def _unlink_temp(cfg: str) -> None:
    try:
        os.unlink(os.path.join(cfg, TEMP_NAME))
    except OSError:
        pass


def _scope_of(oauth: Mapping[str, object]) -> Optional[str]:
    """The space-joined scopes the login was granted (what Claude Code's refresh sends for a stored list), or None
    (omitted: the server keeps the original grant)."""
    sc = oauth.get("scopes")
    if isinstance(sc, list) and sc and all(isinstance(s, str) and _SCOPE_RE.fullmatch(s) for s in sc):
        return " ".join(sc)
    return None


def _refresh(env: Mapping[str, str], cfg: str, obj: Dict[str, object], token_url: str,
             opener: Optional[Callable[[Mapping[str, str]], urllib.request.OpenerDirector]]) -> Dict[str, object]:
    """One refresh and its write-back. Returns ``{"access": <the new access token>}`` (the file already holds it), or
    the line to print: ``{"refresh": <status>[, "oauth_error": w]}`` / ``{"refresh": <name>}``. Never raises."""
    oauth = obj["claudeAiOauth"]
    assert isinstance(oauth, dict)
    rt = _clean(oauth.get("refreshToken"))
    if rt is None:
        return {"error": "no-token"}
    try:
        fd = _open_temp(cfg)       # before the request: a file that cannot be written must not spend the token
    except OSError:
        return {"refresh": "write-failed"}
    fd_open = True
    try:
        body_obj = {"grant_type": "refresh_token", "refresh_token": rt, "client_id": CLIENT_ID}
        scope = _scope_of(oauth)
        if scope is not None:
            body_obj["scope"] = scope
        try:
            req = urllib.request.Request(token_url, data=json.dumps(body_obj).encode("utf-8"), method="POST",
                                         headers={"Content-Type": "application/json", "Accept": "application/json"})
            resp = (opener or _opener)(env).open(req, timeout=TIMEOUT_S)
        except urllib.error.HTTPError as e:
            out: Dict[str, object] = {"refresh": int(e.code)}
            try:
                word = json.loads(e.read(REFRESH_BODY_MAX)).get("error")
                if isinstance(word, str) and _OAUTH_WORD_RE.fullmatch(word):
                    out["oauth_error"] = word
            except Exception:   # noqa: BLE001 - not an OAuth JSON body: the bare status stands
                pass
            e.close()
            return out
        except Exception:   # noqa: BLE001 - the name only: an error text could quote the request
            return {"refresh": "request-failed"}
        try:
            status = int(resp.status)
            raw = resp.read(REFRESH_BODY_MAX + 1)
        except Exception:   # noqa: BLE001
            return {"refresh": "request-failed"}
        finally:
            resp.close()
        if status != 200:
            return {"refresh": status}
        try:
            ans = json.loads(raw) if len(raw) <= REFRESH_BODY_MAX else None
        except ValueError:
            ans = None
        access = _clean(ans.get("access_token")) if isinstance(ans, dict) else None
        exp = ans.get("expires_in") if isinstance(ans, dict) else None
        if access is None or isinstance(exp, bool) or not isinstance(exp, (int, float)) or not exp > 0:
            return {"refresh": "bad-response"}
        new_rt = ans.get("refresh_token")       # type: ignore[union-attr]
        new_rt = _clean(new_rt) if new_rt is not None else None
        # a server that returns no refresh token keeps the old one (Claude Code: ``refresh_token: j = e``)
        new = copy.deepcopy(obj)
        no = new["claudeAiOauth"]
        assert isinstance(no, dict)
        no["accessToken"] = access
        no["refreshToken"] = new_rt if new_rt is not None else rt
        no["expiresAt"] = int(time.time() * 1000 + exp * 1000)
        try:
            data = json.dumps(new, separators=(",", ":")).encode("utf-8")
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
            os.close(fd)
            fd_open = False
            _replace(os.path.join(cfg, TEMP_NAME), os.path.join(cfg, CREDENTIALS_NAME))
            _fsync_dir(cfg)
        except Exception:   # noqa: BLE001
            return {"refresh": "write-failed"}
        return {"access": access}
    finally:
        if fd_open:
            os.close(fd)
        _unlink_temp(cfg)      # a no-op once the rename moved it


def fetch(env: Mapping[str, str], *, url: str = PROFILE_URL, token_url: str = TOKEN_URL, refresh: bool = True,
          opener: Optional[Callable[[Mapping[str, str]], urllib.request.OpenerDirector]] = None) -> Dict[str, object]:
    """The refresh and the one profile request, as this process's answer object (never raises; never carries a
    token). Refused before any request: ``bad-origin`` (``url`` is not :data:`ORIGIN`, or ``token_url`` is not
    :data:`TOKEN_ORIGIN`), ``no-proxy``, ``no-token``. ``refresh=False`` is the profile call alone, for tests of its
    own mechanics; :func:`main` never passes it."""
    try:
        if _origin(url) != tuple(ORIGIN) or (refresh and _origin(token_url) != tuple(TOKEN_ORIGIN)):
            return {"error": "bad-origin"}
    except ValueError:
        return {"error": "bad-origin"}
    if _proxy(env) is None:
        return {"error": "no-proxy"}
    cred = _read_cred(env)
    tok = _clean(cred[1]["claudeAiOauth"].get("accessToken")) if cred is not None else None   # type: ignore[union-attr]
    if cred is None or (tok is None and not refresh):
        return {"error": "no-token"}
    line: Dict[str, object] = {}
    if refresh:
        got = _refresh(env, cred[0], cred[1], token_url, opener)
        if "access" not in got:
            return got
        tok = str(got["access"])
        line["refresh"] = 200
    try:
        req = urllib.request.Request(url, method="GET", headers={
            "Authorization": f"Bearer {tok}", "anthropic-beta": OAUTH_BETA, "Accept": "application/json"})
        resp = (opener or _opener)(env).open(req, timeout=TIMEOUT_S)
    except urllib.error.HTTPError as e:
        e.close()
        line.update({"status": int(e.code), "body": ""})
        return line
    except Exception:   # noqa: BLE001 - the name only: an error text could quote the request
        line["error"] = "request-failed"
        return line
    try:
        status = int(resp.status)
        body = resp.read(BODY_MAX + 1)
    except Exception:   # noqa: BLE001
        line["error"] = "request-failed"
        return line
    finally:
        resp.close()
    if len(body) > BODY_MAX:
        line["error"] = "body-too-large"
        return line
    line.update({"status": status, "body": body.decode("utf-8", "replace")})
    return line


def main(argv=None, *, env: Optional[Mapping[str, str]] = None, out=None) -> int:
    ans = fetch(os.environ if env is None else env)
    (out or sys.stdout).write(json.dumps(ans, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n")
    return 0 if "status" in ans else 1


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main())
