"""The seat's GitHub read door (PATHZERO Z2b, Phill's np-672fa792 (a)): a seat reads GitHub through two fixed verbs and
never holds, sees or can ask for the credential.

A seat's Bash has an empty network allowlist and anything a sandboxed process can read, the agent can read. So the read
token lives HOST-side, in ``<levain home>/seat_gh/<owner>`` (0600 in a 0700 directory, the seat profile denies the seat
that directory exactly as it denies the launcher key directory), and the seat reaches it only through the stdio MCP
server ``levain_gh`` (:mod:`levain.seat_gh_mcp`), which runs outside the sandbox and calls the two verbs here:

* :func:`gh_read`: ``GET`` only, to ``api.github.com``, on a path of the fixed allowlist (:data:`REPOS` x
  :data:`READ_SEGMENTS`). No redirect is followed. The token goes in one ``Authorization`` header, never in argv, never
  in an environment, and the returned text is scrubbed of it.
* :func:`git_fetch`: the host's git fetches the allowed repository's branches into a Levain-owned bare mirror
  (``<home>/gh_mirror/<owner>/<name>.git``); the seat then fetches from that LOCAL path. The git child's environment is
  built from scratch (a fresh empty HOME, no global or system config, no ``GIT_*`` from the caller), the credential
  reaches git through a per-URL credential helper that answers only ``github.com`` and only the allowed repository's
  ``.git`` path, and no remote transport but ``https`` is allowed.

Two tokens, not one: a fine-grained PAT has ONE resource owner and ``levainhq/levain`` and ``phillipclapham/flow`` have
different owners (Z2B_FRAME section 2). An absent, expired or unreadable token is ONE fixed refusal sentence and the
verb stops: there is no fallback to ``gh``'s hosts.yml, the keychain, ``GH_TOKEN`` in the environment or an ssh agent.
Every refusal is fixed words (the seat reads them and learns nothing about the token store).

Every call leaves one line in ``<home>/push/seat_gh.jsonl`` (verb, repository, outcome, status); it never holds a token.

Known edges (one line each): the token's own expiry is the one the operator recorded (or GitHub's response header gave)
and is enforced here, but GitHub revokes a token on its side first when the operator deletes it; a mirror fetch that is
killed mid-way leaves a half-updated mirror that the next fetch repairs (git's own atomic ref updates).
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import http.client
import ipaddress
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from levain.consent import push

__all__ = ["REPOS", "OWNERS", "READ_SEGMENTS", "ACTIONS_CHILDREN", "QUERY_KEYS", "API_HOST", "API_BASE", "GIT_BASE", "TOKEN_DIR", "MIRROR_DIR",
           "LOG_NAME", "GhRefused", "path_refusal", "query_refusal", "scrub", "token_fingerprint", "set_token",
           "clear_token", "token_status", "gh_read", "git_fetch", "credential_helper", "main"]

#: The allowed repositories and the resource owner whose token reads each. A code constant: no argument names another.
REPOS: Mapping[str, str] = {"levainhq/levain": "levainhq", "phillipclapham/flow": "phillipclapham"}
OWNERS = tuple(sorted(set(REPOS.values())))
#: The path segment right after ``/repos/<owner>/<repo>`` a seat may read (or none, the repository itself).
READ_SEGMENTS = frozenset({"contents", "pulls", "issues", "actions", "commits", "branches"})
#: The query names a seat may send, and what a value may be made of.
#: ``actions`` also carries the secrets, variables and runner registrations of a repository: only these children read.
ACTIONS_CHILDREN = frozenset({"runs", "jobs", "workflows", "artifacts"})
QUERY_KEYS = frozenset({"ref", "per_page", "page", "state", "sha", "path", "branch", "event", "status", "since",
                        "until", "head", "base", "sort", "direction"})
_QUERY_VALUE = re.compile(r"[A-Za-z0-9_.@/~+:,=-]{0,200}")
_QUERY_MAX = 10
API_HOST = "api.github.com"
API_BASE = f"https://{API_HOST}"
GIT_HOST = "github.com"
GIT_BASE = f"https://{GIT_HOST}/"
TOKEN_DIR = "seat_gh"
#: NOT inside :data:`TOKEN_DIR`: the seat is denied that directory and must read the mirror.
MIRROR_DIR = "gh_mirror"
LOG_NAME = "seat_gh.jsonl"
CRED_USER = "x-access-token"
MAX_BODY = 200_000
HTTP_TIMEOUT = 30
FETCH_TIMEOUT = 600
EXPIRY_HEADER = "github-authentication-token-expiration"

# fixed-words refusals: the whole text a seat ever reads about a refusal
R_METHOD = "levain_gh refuses: only GET."
R_PATH = "levain_gh refuses: that path is not a read path of an allowed repository."
R_REPO = "levain_gh refuses: that repository is not one a seat may read."
R_QUERY = "levain_gh refuses: that query is not an allowed one."
R_HOST = "levain_gh refuses: that host is not GitHub's API."
R_REDIRECT = "levain_gh refuses: GitHub answered with a redirect; none is followed."
R_TOKEN_ABSENT = "levain_gh refuses: no read token is installed for that repository's owner."
R_TOKEN_EXPIRED = "levain_gh refuses: the read token for that repository's owner has expired."
R_TOKEN_BAD = "levain_gh refuses: the read token for that repository's owner cannot be used."
R_TRANSPORT = "levain_gh could not reach GitHub."
R_FETCH = "levain_gh: the fetch into the mirror failed."

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{20,255}")
# the shapes GitHub issues, scrubbed from anything returned even when it is not the token on file
_TOKEN_SHAPE = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")
_PATH = re.compile(r"/repos/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)((?:/[A-Za-z0-9_.@~+=,-]+)*)")


class GhRefused(Exception):
    """A verb is refused. ``str()`` is a fixed sentence (plus, for a failed fetch, a scrubbed git detail)."""


# --- the judges: pure, no I/O -----------------------------------------------------------------------------------------

def path_refusal(path: Any) -> str | None:
    """None when ``path`` is ``/repos/<allowed repo>`` or ``/repos/<allowed repo>/<read segment>[/...]``, else the fixed
    refusal. The path alphabet has no ``%``, ``?``, ``#``, ``\\`` or space, so no encoded traversal or second query
    reaches the request; ``.`` and ``..`` as a segment are refused whatever follows."""
    if not isinstance(path, str) or len(path) > 1024:
        return R_PATH
    m = _PATH.fullmatch(path)
    if not m:
        return R_PATH
    repo, rest = m.groups()
    if repo not in REPOS:
        return R_REPO
    segs = rest.split("/")[1:] if rest else []
    if any(s in (".", "..") for s in segs):
        return R_PATH
    if segs and segs[0] not in READ_SEGMENTS:
        return R_PATH
    if segs[:1] == ["actions"] and len(segs) > 1 and segs[1] not in ACTIONS_CHILDREN:
        return R_PATH
    return None


def query_refusal(query: Any) -> str | None:
    if query is None:
        return None
    if not isinstance(query, Mapping) or len(query) > _QUERY_MAX:
        return R_QUERY
    for k, v in query.items():
        if k not in QUERY_KEYS or not isinstance(v, (str, int)) or isinstance(v, bool):
            return R_QUERY
        if not _QUERY_VALUE.fullmatch(str(v)):
            return R_QUERY
    return None


def scrub(text: str, tokens: Sequence[str] = ()) -> str:
    """``text`` with every given token (raw, url-quoted, and as the base64 of the basic-auth pairs) and every
    GitHub-token-shaped run replaced by ``[token]``."""
    for t in tokens:
        if not t:
            continue
        forms = {t, quote(t, safe=""), base64.b64encode(t.encode()).decode(),
                 base64.b64encode(f"{CRED_USER}:{t}".encode()).decode()}
        for f in sorted(forms, key=len, reverse=True):
            text = text.replace(f, "[token]")
    return _TOKEN_SHAPE.sub("[token]", text)


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


# --- the token store --------------------------------------------------------------------------------------------------

def token_dir(home: Path | str) -> Path:
    return Path(home) / TOKEN_DIR


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_expiry(text: str) -> datetime:
    """``2026-12-01``, ``2026-12-01T00:00:00Z`` or GitHub's ``2026-12-01 00:00:00 UTC``; a date is its first instant (UTC),
    the conservative reading."""
    s = text.strip()
    if s.endswith(" UTC"):
        s = s[:-4] + "+00:00"
    s = s.replace("Z", "+00:00").replace(" ", "T", 1)
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _private_dir(path: Path) -> None:
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise GhRefused(R_TOKEN_BAD)
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(path, 0o700)


def _read_record(home: Path | str, owner: str) -> dict[str, Any]:
    """The owner's token record, or :class:`GhRefused`. The directory and the file must be this user's own, not links,
    and closed to group and world: anything else is refused, never repaired (a loosened store is not trusted)."""
    d = token_dir(home)
    f = d / owner
    try:
        dst = os.lstat(d)
        if not stat.S_ISDIR(dst.st_mode) or dst.st_uid != os.getuid() or dst.st_mode & 0o077:
            raise GhRefused(R_TOKEN_BAD)
        fst = os.lstat(f)
    except FileNotFoundError:
        raise GhRefused(R_TOKEN_ABSENT) from None
    except OSError:
        raise GhRefused(R_TOKEN_BAD) from None
    if not stat.S_ISREG(fst.st_mode) or fst.st_uid != os.getuid() or fst.st_mode & 0o077:
        raise GhRefused(R_TOKEN_BAD)
    try:
        fd = os.open(f, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            rec = json.loads(fh.read(4096))
    except (OSError, ValueError):
        raise GhRefused(R_TOKEN_BAD) from None
    if not (isinstance(rec, dict) and rec.get("owner") == owner and isinstance(rec.get("token"), str)
            and _TOKEN_RE.fullmatch(rec["token"]) and isinstance(rec.get("expires_at"), str)):
        raise GhRefused(R_TOKEN_BAD)
    return rec


def _live_token(home: Path | str, owner: str, now: datetime) -> str:
    rec = _read_record(home, owner)
    try:
        expires = _parse_expiry(rec["expires_at"])
    except ValueError:
        raise GhRefused(R_TOKEN_BAD) from None
    if now >= expires:
        raise GhRefused(R_TOKEN_EXPIRED)
    return rec["token"]


def _write_record(home: Path | str, owner: str, token: str, expires: datetime) -> None:
    d = token_dir(home)
    Path(home).mkdir(parents=True, exist_ok=True)
    _private_dir(d)
    rec = {"v": 1, "owner": owner, "fingerprint": token_fingerprint(token), "expires_at": expires.isoformat(),
           "token": token}
    tmp = d / f".{owner}.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")
    os.replace(tmp, d / owner)


def clear_token(home: Path | str, owner: str) -> bool:
    """Delete the owner's token file; True when one was there. The next verb for that owner refuses."""
    if owner not in OWNERS:
        raise GhRefused(R_REPO)
    try:
        os.unlink(token_dir(home) / owner)
    except FileNotFoundError:
        return False
    return True


def token_status(home: Path | str, owner: str, now: datetime | None = None) -> str:
    """One line for the operator: absent / unusable / expired / live, with the fingerprint and expiry, never the token."""
    if owner not in OWNERS:
        raise GhRefused(R_REPO)
    try:
        rec = _read_record(home, owner)
    except GhRefused as exc:
        return f"{owner}: " + ("no token installed" if str(exc) == R_TOKEN_ABSENT else "token unusable (not private, or damaged)")
    try:
        live = (now or _now()) < _parse_expiry(rec["expires_at"])
    except ValueError:
        return f"{owner}: token unusable (expiry unreadable)"
    return f"{owner}: {'live' if live else 'EXPIRED'}  fingerprint {rec.get('fingerprint')}  expires {rec['expires_at']}"


# --- the HTTP read ----------------------------------------------------------------------------------------------------

def _connection(base_url: str) -> http.client.HTTPConnection:
    """A connection to GitHub's API. ``https`` only to :data:`API_HOST`; plain ``http`` only to a loopback address (a
    test's local server): the token never travels in the clear to anything else, whatever ``base_url`` says."""
    u = urlsplit(base_url)
    host = u.hostname or ""
    if u.scheme == "https" and host == API_HOST and u.port in (None, 443) and not u.path.strip("/"):
        return http.client.HTTPSConnection(host, 443, timeout=HTTP_TIMEOUT)
    if u.scheme == "http" and not u.path.strip("/"):
        try:
            loop = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loop = False
        if loop:
            return http.client.HTTPConnection(host, u.port or 80, timeout=HTTP_TIMEOUT)
    raise GhRefused(R_HOST)


def _get(base_url: str, target: str, token: str) -> tuple[int, dict[str, str], str]:
    conn = _connection(base_url)
    try:
        conn.request("GET", target, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "levain-seat-gh"})
        resp = conn.getresponse()
        status = resp.status
        if 300 <= status < 400:
            raise GhRefused(R_REDIRECT)
        raw = resp.read(MAX_BODY + 1)
        headers = {k.lower(): v for k, v in resp.getheaders()}
    except GhRefused:
        raise
    except (OSError, http.client.HTTPException):
        raise GhRefused(R_TRANSPORT) from None
    finally:
        conn.close()
    return status, headers, raw.decode("utf-8", errors="replace")


def _log(home: Path | str, row: dict[str, Any]) -> None:
    """One line in ``<home>/push/seat_gh.jsonl``; never a token (rows are built from fixed fields only). A record that
    cannot be written is dropped, never raised."""
    try:
        push._private_dir(Path(home) / "push")
        push.append_line(Path(home) / "push" / LOG_NAME, {**row, "at": time.time()})
    except Exception:  # noqa: BLE001
        pass


def gh_read(home: Path | str, path: Any, query: Any = None, *, method: str = "GET", api_base: str = API_BASE,
            now: datetime | None = None) -> tuple[int, str]:
    """GET ``path`` (+ ``query``) from GitHub's API as the path's owner. Returns ``(status, scrubbed body)``; raises
    :class:`GhRefused` (fixed words) for anything outside the allowlist or a token that is absent or expired."""
    base: dict[str, Any] = {"verb": "gh_read", "path": path if isinstance(path, str) else None}
    try:
        if method != "GET":
            raise GhRefused(R_METHOD)
        problem = path_refusal(path) or query_refusal(query)
        if problem:
            raise GhRefused(problem)
        repo = _PATH.fullmatch(path).group(1)               # type: ignore[union-attr]  # path_refusal vouched for it
        token = _live_token(home, REPOS[repo], now or _now())
        target = path + (("?" + urlencode(sorted((str(k), str(v)) for k, v in query.items()))) if query else "")
        status, _headers, body = _get(api_base, target, token)
        body = scrub(body[:MAX_BODY], (token,))
        _log(home, {**base, "outcome": "read", "status": status})
        return status, body
    except GhRefused as exc:
        _log(home, {**base, "outcome": "refused", "detail": str(exc)[:200]})
        raise


# --- the git fetch ----------------------------------------------------------------------------------------------------

def mirror_path(home: Path | str, repo: str) -> Path:
    owner, name = repo.split("/")
    return Path(home) / MIRROR_DIR / owner / f"{name}.git"


def _helper_command(python: str, home: Path | str) -> str:
    return "!" + " ".join([shlex.quote(python), "-EP", "-B", "-m", "levain.seat_gh", "credential-helper",
                           "--home", shlex.quote(str(home))])


def _fetch_argv(git: str, mirror: Path, url: str, helper: str) -> list[str]:
    return [*push.git_isolation_args(git, str(mirror),
                                     extra_config=("gc.auto=0", "credential.helper=",
                                                   f"credential.{GIT_BASE.rstrip('/')}.helper={helper}",
                                                   "credential.useHttpPath=true", "http.followRedirects=false",
                                                   "protocol.allow=never", "protocol.https.allow=always",
                                                   "protocol.file.allow=always")),
            "fetch", "--quiet", "--prune", "--no-tags", "--no-write-fetch-head", url, "+refs/heads/*:refs/heads/*"]


def _git_env(home_dir: str, scheme: str) -> dict[str, str]:
    """Rebuilt from scratch, nothing inherited: a fresh empty HOME, no global or system config, no prompt, one transport."""
    return {"PATH": push.TRUSTED_PATH, "HOME": home_dir, "LANG": "C", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1", "GIT_ALLOW_PROTOCOL": scheme}


def _ensure_mirror(git: str, home: Path | str, repo: str, env: dict[str, str]) -> Path:
    m = mirror_path(home, repo)
    Path(home).mkdir(parents=True, exist_ok=True)
    _private_dir(Path(home) / MIRROR_DIR)
    _private_dir(m.parent)
    if not os.path.lexists(m):
        r = subprocess.run([git, "init", "--quiet", "--bare", "--template=", str(m)], env=env, cwd="/",
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        if r.returncode:
            raise GhRefused(R_FETCH)
    _private_dir(m)
    hooks = m / "hooks"
    if os.path.lexists(hooks):
        import shutil
        if os.path.islink(hooks) or not os.path.isdir(hooks):
            os.unlink(hooks)
        else:
            shutil.rmtree(hooks)
    tmp = m / "config.levain-tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(push.MIRROR_CONFIG)
    os.replace(tmp, m / "config")
    return m


def git_fetch(home: Path | str, repo: Any, *, git_base: str = GIT_BASE, python: str | None = None,
              now: datetime | None = None) -> str:
    """Fetch every branch of the allowed repository ``repo`` into its Levain-owned bare mirror as the repository's
    owner, and return the mirror path and its branch tips (scrubbed). The token is checked FIRST: an absent or expired one
    refuses before any git runs."""
    base: dict[str, Any] = {"verb": "git_fetch", "repo": repo if isinstance(repo, str) else None}
    try:
        if not isinstance(repo, str) or repo not in REPOS:
            raise GhRefused(R_REPO)
        token = _live_token(home, REPOS[repo], now or _now())
        scheme = urlsplit(git_base).scheme
        if scheme not in ("https", "file") or not git_base.endswith("/"):
            raise GhRefused(R_HOST)
        git = push.git_bin()
        url = f"{git_base}{repo}.git"
        with tempfile.TemporaryDirectory(prefix="levain-gh-") as scratch:
            env = _git_env(scratch, scheme)
            mirror = _ensure_mirror(git, home, repo, env)
            argv = _fetch_argv(git, mirror, url, _helper_command(python or sys.executable, home))
            try:
                r = subprocess.run(argv, env=env, cwd="/", stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=FETCH_TIMEOUT)
            except subprocess.TimeoutExpired:
                raise GhRefused(R_FETCH) from None
            if r.returncode:
                detail = scrub((r.stderr or "").strip()[-300:], (token,))
                raise GhRefused(f"{R_FETCH} {detail}".strip())
            refs = subprocess.run(push.git_isolation_args(git, str(mirror)) + [
                "for-each-ref", "--count=200", "--format=%(objectname:short) %(refname:short)", "refs/heads"],
                env=env, cwd="/", stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        out = scrub(f"mirror: {mirror}\n{refs.stdout}", (token,))
        _log(home, {**base, "outcome": "fetched"})
        return out
    except GhRefused as exc:
        _log(home, {**base, "outcome": "refused", "detail": str(exc)[:200]})
        raise


# --- the credential helper (git's, run by the host's fetch) -------------------------------------------------------------

def credential_helper(action: str, stdin_text: str, home: Path | str, out: Any, *, now: datetime | None = None) -> int:
    """git's credential-helper protocol, ``get`` only. It answers when the request is ``https``, ``github.com`` and the
    path is exactly an allowed repository's ``.git``; for anything else it prints nothing (git then has no credential and
    the prompt is disabled). The token reaches git's stdin pipe and nothing else."""
    if action != "get":
        return 0
    req: dict[str, str] = {}
    for line in stdin_text.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            req[k] = v
    path = req.get("path", "")
    repo = path[:-4] if path.endswith(".git") else ""
    if req.get("protocol") != "https" or req.get("host") != GIT_HOST or repo not in REPOS:
        return 0
    try:
        token = _live_token(home, REPOS[repo], now or _now())
    except GhRefused:
        return 0
    out.write(f"username={CRED_USER}\npassword={token}\n")
    return 0


# --- the operator's token commands -------------------------------------------------------------------------------------

def set_token(home: Path | str, owner: str, token: str, *, expires: str | None = None, api_base: str = API_BASE,
              now: datetime | None = None) -> str:
    """Install the owner's read token: shape-check it, check it with a host GET ``/repos/<the owner's repo>`` (it must
    answer 200), take the expiry from GitHub's response header (else ``expires``; neither is a refusal), and write the
    record 0600. Returns one line with the fingerprint and expiry, never the token."""
    token = token.strip()
    if owner not in OWNERS:
        raise GhRefused("owner must be levainhq or phillipclapham")
    if not _TOKEN_RE.fullmatch(token):
        raise GhRefused("that is not a GitHub token (letters, digits and underscores, 20 to 255 characters)")
    repo = next(r for r, o in REPOS.items() if o == owner)
    status, headers, _body = _get(api_base, f"/repos/{repo}", token)
    if status != 200:
        raise GhRefused(f"GitHub answered {status} for /repos/{repo} with that token: nothing was stored")
    raw = headers.get(EXPIRY_HEADER) or expires
    if not raw:
        raise GhRefused("the token's expiry is unknown: pass --expires YYYY-MM-DD (the date on its mint page)")
    try:
        when = _parse_expiry(raw)
    except ValueError:
        raise GhRefused(f"the expiry {raw!r} is not a date") from None
    if when <= (now or _now()):
        raise GhRefused("that token has already expired: nothing was stored")
    _write_record(home, owner, token, when)
    return f"installed: {owner}  fingerprint {token_fingerprint(token)}  expires {when.isoformat()}  (checked {repo}: 200)"


def read_token_stdin(stream: Any = None) -> str:
    """The token from STDIN only: a pipe is read whole; a terminal is asked for with no echo. Never an argument."""
    stream = stream or sys.stdin
    if stream.isatty():
        return getpass.getpass("GitHub token (not shown): ")
    return stream.read(4096)


def main(argv: Sequence[str] | None = None) -> int:
    """``python -EP -B -m levain.seat_gh credential-helper --home <dir> <get|store|erase>`` (git's helper protocol)."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 4 and args[0] == "credential-helper" and args[1] == "--home":
        return credential_helper(args[3], sys.stdin.read(65536), args[2], sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
