"""TEST ONLY: a DENY on the operator's REAL key (nothing enumerates what is allowed: scratch keys, tmp ``-f``, scratch agents pass).
Installed at import (collection is covered). Every Popen (``run``, ``os.popen``), ``os.posix_spawn*``/``spawn*``/``exec*``/``system``
is judged before anything starts; a refusal raises ``RealKeyReached`` naming the test, the reason and the argv, is recorded, and
also fails its test at teardown. Shell strings (``shell=True``, ``sh -c``, ``eval``) are re-tokenised with shlex (unbalanced quote: refused).
D1: any argv token (``~`` and ``$HOME`` read as the PASSWD home, realpath against the child's cwd) inside ``<home>/.ssh``,
``<home>/.levain/keys``, a key-named file in ``<home>/.levain``, or a known provider library. D2: ``sc_auth``; ``ssh-keygen``
-K/-w/-D; ``ssh-add`` unless SSH_AUTH_SOCK is under the temp dir (a fake of these by absolute path under the temp dir is fine).
Scrubbed for the run, restored at its end: SSH_AUTH_SOCK, SSH_SK_PROVIDER; git system config off, global config a scratch identity.
Known edges: ctypes/cffi/libc system; TOCTOU; a key copied out before the test; a process the test starts that itself reads the
home (a real levain CLI by subprocess: the scrub is the defence)."""

from __future__ import annotations

import atexit
import os
import pwd
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

_PROVIDERS = ("/usr/lib/ssh-keychain.dylib", "/usr/libexec/ssh-sk-helper", "/usr/local/lib/libsk-libfido2.dylib",
              "/opt/homebrew/lib/libsk-libfido2.dylib")
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash"})
_POPEN_POS = ("bufsize", "executable", "stdin", "stdout", "stderr", "preexec_fn", "close_fds", "shell", "cwd", "env")
_ENV_NAMES = ("SSH_AUTH_SOCK", "SSH_SK_PROVIDER", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM")
_PROVIDER_FLAG = re.compile(r"-[A-Za-z]*[KwD]")             # ssh-keygen -K / -w / -D, clustered (-qK) or attached (-D/lib)


def _real_home() -> str:
    """The account's home from the password database (as ``levain.cockpit.keys.pwd_key_dir``), never ``$HOME``."""
    return pwd.getpwuid(os.getuid()).pw_dir


def _inside(p: str, root: str) -> bool:
    return p == root or p.startswith(root.rstrip("/") + "/")


def _in_tmp(p: str) -> bool:
    return _inside(os.path.realpath(p), os.path.realpath(tempfile.gettempdir()))


def _hits_real_key(tok: str, cwd: str | None) -> bool:
    home = _real_home()
    h = os.path.realpath(home)
    for cand in {tok, tok.split("=", 1)[-1], tok[2:] if tok[:1] == "-" and tok[1:2] != "-" else tok}:
        s = cand.replace("${HOME}", home).replace("$HOME", home)
        s = home + s[1:] if s == "~" or s.startswith("~/") else s
        if not s or "\0" in s:
            continue
        p = os.path.realpath(s if os.path.isabs(s) else os.path.join(cwd or os.getcwd(), s))
        name = os.path.basename(p)
        if (_inside(p, h + "/.ssh") or _inside(p, h + "/.levain/keys") or cand in _PROVIDERS or p in _PROVIDERS
                or (os.path.dirname(p) == h + "/.levain" and (name == "terminal" or name.startswith("id_") or name.endswith(".pub")))):
            return True
    return False


def _split(text: str, depth: int = 0) -> list[str]:
    """shlex tokens of ``text`` plus, recursively, those of a nested ``sh -c STR``, ``eval ...`` or a token holding spaces."""
    lex = shlex.shlex(text.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    toks = list(lex)                                        # ValueError on an unbalanced quote
    return _flatten(toks, depth)


def _flatten(toks: list[str], depth: int = 0) -> list[str]:
    out = list(toks)
    for i, t in enumerate(toks):
        script = None
        if os.path.basename(t).lower() in _SHELLS:
            script = next((toks[k + 1] for k, a in enumerate(toks[i + 1:], i) if a[:1] == "-" and a[1:2] != "-" and "c" in a
                           and k + 1 < len(toks)), None)
        elif t == "eval":
            script = " ".join(toks[i + 1:])
        elif i and any(c.isspace() for c in t):
            script = t
        if script is not None:
            if depth >= 4:
                raise ValueError("a shell command nested too deeply to read")
            out += _split(script, depth + 1)
    return out


def judge(args, executable=None, env=None, cwd=None, shell: bool = False) -> str | None:
    """Why this spawn may not start (a sentence), or None. Anything unreadable is refused."""
    try:
        e = {os.fsdecode(k): os.fsdecode(v) for k, v in dict(os.environ if env is None else env).items()}
        c = os.fsdecode(cwd) if cwd is not None else None
        if isinstance(args, (str, bytes)):
            toks = _split(os.fsdecode(args))
        else:
            toks = [os.fsdecode(a) for a in ([args] if isinstance(args, os.PathLike) else args)]
            toks = _split(toks[0]) if shell and toks else _flatten(toks)
        if executable is not None and not shell:
            toks = [os.fsdecode(executable), *toks[1:]]
        sock = ([t.split("=", 1)[1] for t in toks if t.startswith("SSH_AUTH_SOCK=")] or [e.get("SSH_AUTH_SOCK", "")])[-1]
        for i, t in enumerate(toks):
            name = os.path.basename(t).lower()
            fake = os.path.isabs(t) and _in_tmp(t)
            if name == "sc_auth" and not fake:
                return "sc_auth (the Secure Enclave identity tool) is never run by a test"
            if name == "ssh-keygen" and not fake and any(_PROVIDER_FLAG.match(a) for a in toks[i + 1:]):
                return "ssh-keygen -K/-w/-D reaches a security-key provider"
            if name == "ssh-add" and not fake and not (sock and _in_tmp(sock)):
                return "ssh-add outside a throwaway agent (SSH_AUTH_SOCK is not under the temp dir)"
        hit = next((t for t in toks if _hits_real_key(t, c)), None)
        return None if hit is None else f"argv names the real account's key place or a security-key provider ({hit})"
    except ValueError as exc:
        return f"a command string that cannot be read ({exc}); refused"
    except Exception as exc:                                # noqa: BLE001 -- fail closed
        return f"a spawn the keyguard could not read ({type(exc).__name__}: {exc}); refused"


class AppendOnly(list):
    """The violation record: only ever appended, judged by index."""
    __delitem__ = __setitem__ = __iadd__ = __imul__ = pop = remove = clear = insert = sort = reverse = lambda *a, **k: 1 / 0


_VIOLATIONS: AppendOnly = AppendOnly()
_ACKNOWLEDGED: set[int] = set()
_SEEN = [0]


def acknowledge(since: int) -> None:
    """A test that provokes a refusal on purpose marks the entries from index ``since`` as expected."""
    _ACKNOWLEDGED.update(range(since, len(_VIOLATIONS)))


def _pending() -> list[str]:
    return [_VIOLATIONS[i] for i in range(_SEEN[0], len(_VIOLATIONS)) if i not in _ACKNOWLEDGED]


def _refuse(why: str, argv) -> None:
    from levain.cockpit.keys import RealKeyReached      # lazily: nothing of levain loads at conftest import
    msg = f"test {os.environ.get('PYTEST_CURRENT_TEST', '<no test>')} tried to start a spawn the keyguard denies: {why}. argv={argv!r}"
    _VIOLATIONS.append(msg)
    raise RealKeyReached(msg)


def spawn_args(n: str, rest: Sequence, kw: Mapping) -> tuple[list, object]:
    """(argv, env) of ``os.<n>(mode, file, *rest, **kw)``; env None where inherited (the ``e`` forms take it last or by keyword)."""
    rest, e = list(rest), n.endswith("e")
    if "v" in n:
        return list(rest[0] if rest else kw["args"]), ((rest[1] if len(rest) > 1 else kw["env"]) if e else None)
    return (rest[:-1], rest[-1]) if e else (rest, None)


def install():
    """Wrap Popen, ``os.posix_spawn*``, ``os.spawn*``, ``os.exec*`` and ``os.system`` while the generator is open."""
    real_init = subprocess.Popen.__init__
    names = ("posix_spawn", "posix_spawnp", "execv", "execve", "execvp", "execvpe", "system", "spawnl", "spawnle", "spawnlp",
             "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe")
    saved = {n: getattr(os, n) for n in names if hasattr(os, n)}

    def check(args, executable=None, env=None, cwd=None, shell=False):
        why = judge(args, executable, env, cwd, shell)
        if why:
            _refuse(why, args if isinstance(args, (str, bytes, os.PathLike)) else list(args))

    def popen_init(self, args, *a, **kw):
        args = args if isinstance(args, (str, bytes, os.PathLike)) else list(args)      # an iterator is read once
        b = {**dict(zip(_POPEN_POS, a)), **kw}
        check(args, b.get("executable"), b.get("env"), b.get("cwd"), bool(b.get("shell")))
        return real_init(self, args, *a, **kw)

    def wrap(n, real):
        def run(*a, **kw):
            if n == "system":
                check(a[0], shell=True)
            elif n.startswith("spawn"):
                argv, env = spawn_args(n, a[2:], kw)
                check(argv, a[1], env)
            else:
                env = (a[2] if len(a) > 2 else kw.get("env")) if n.endswith("e") or n.startswith("posix") else None
                check(a[1], a[0], env)
            return real(*a, **kw)
        return run

    subprocess.Popen.__init__ = popen_init
    wrapped = {n: wrap(n, r) for n, r in saved.items()}
    for n, w in wrapped.items():
        setattr(os, n, w)
    try:
        yield
    finally:
        if subprocess.Popen.__init__ is popen_init:
            subprocess.Popen.__init__ = real_init
        for n, r in saved.items():
            if getattr(os, n) is wrapped[n]:
                setattr(os, n, r)


_GITCONFIG: list[str] = []


def scrub_environ() -> dict[str, str | None]:
    """Delete the ambient signing capability and point git at an identity-only config; returns the previous values."""
    previous = {n: os.environ.get(n) for n in _ENV_NAMES}
    if not _GITCONFIG:
        d = tempfile.mkdtemp(prefix="levain-kg-")
        atexit.register(shutil.rmtree, d, True)
        Path(d, ".gitconfig").write_text("[user]\n\tname = Levain Test\n\temail = test@invalid\n", encoding="utf-8")
        _GITCONFIG.append(str(Path(d, ".gitconfig")))
    for n in ("SSH_AUTH_SOCK", "SSH_SK_PROVIDER"):
        os.environ.pop(n, None)
    os.environ.update(GIT_CONFIG_GLOBAL=_GITCONFIG[0], GIT_CONFIG_SYSTEM="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    return previous


def restore_environ(previous: Mapping[str, str | None]) -> None:
    for n, v in previous.items():
        os.environ.pop(n, None) if v is None else os.environ.__setitem__(n, v)


_ORIGINAL_ENV = scrub_environ()
_SESSION = install()
next(_SESSION)


@pytest.fixture(autouse=True, scope="session")
def _no_signing_spawn_in_tests():
    """The wall stands since import; the scrub is repeated here (conftest's session-home setup runs after import and drops
    GIT_CONFIG_GLOBAL); both end with the session, which fails on a refusal nobody judged."""
    scrub_environ()
    try:
        yield
    finally:
        restore_environ(_ORIGINAL_ENV)
        _SESSION.close()
    if _pending():
        pytest.fail("the keyguard refused a spawn that no test judged:\n" + "\n".join(_pending()), pytrace=False)


@pytest.fixture(autouse=True)
def _a_refused_spawn_fails_its_test():
    """A caller that catches the refusal still fails here. Judged by index, so a refusal in a wider-scoped fixture fails the
    first test to finish after it."""
    yield
    fresh = _pending()
    _SEEN[0] = len(_VIOLATIONS)
    if fresh:
        pytest.fail("the keyguard refused a spawn during this test (the code under test caught it):\n" + "\n".join(fresh), pytrace=False)
