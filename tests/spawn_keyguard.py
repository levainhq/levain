"""TEST ONLY: a DENY on the operator's REAL key (nothing enumerates what is allowed: scratch keys, tmp ``-f``, scratch agents pass).
Installed at import (collection is covered). Every Popen (``run``, ``os.popen``), ``os.posix_spawn*``/``spawn*``/``exec*``/``system``
is judged before anything starts. MODE (env ``LEVAIN_KEYGUARD``): ``warn`` (the DEFAULT) records a denied spawn, with the test name
and argv, in a session report printed between ``KEYGUARD REPORT BEGIN``/``END`` in pytest's terminal summary (and written to the
file named by ``LEVAIN_KEYGUARD_REPORT``, when set) and lets the spawn run; ``enforce`` raises ``RealKeyReached`` before anything
starts, records it, and fails the test at teardown. Warn-only stays ON until CI shows an empty report, then ``_DEFAULT_MODE``
flips to ``enforce`` in that one line; local tests stay held while it is warn-only because warn does not stop a signing call.
Shell text is re-tokenised with shlex ONLY for ``shell=True``, a shell executable (sh|bash|zsh|dash|ksh|ash, any case) with ``-c`` and
its script, or ``eval``; a structured argv element is never shell-parsed, and a parse error in text that IS shell-parsed fails
OPEN (allowed). The key check: an argv token that, resolved (``~`` as the PASSWD home, ``$HOME`` as the judged child's own HOME,
realpath against the child's cwd), is an EXISTING file inside ``<passwd home>/.ssh`` or ``<passwd home>/.levain/keys`` (compared
case-folded and NFC on darwin); an embedded-NUL token is skipped. Also denied: ``sc_auth``; ``ssh-keygen`` -K/-w/-D; ``ssh-add``
unless the child's SSH_AUTH_SOCK is under the temp dir (an argv ``SSH_AUTH_SOCK=`` counts only after ``env`` or in a shell string;
a fake of these by absolute path under the temp dir is fine). Scrubbed for the run, restored at its end (also at
``pytest_unconfigure`` and ``atexit``, once): SSH_AUTH_SOCK, SSH_SK_PROVIDER (a provider library matters only as that variable or
the value of ``ssh-keygen -D/-K/-w``, never by its name as data). The git environment is NOT touched: tests/conftest.py removes
GIT_CONFIG_GLOBAL and XDG_CONFIG_HOME on purpose (a hermetic-home test asserts it), and git then reads the scratch HOME's config.
Known edges: ctypes/cffi/libc system; TOCTOU; a key copied out before the test; a process the test starts that itself reads the
home (a real levain CLI by subprocess: the scrub is the defence); an explicit child ``env=`` naming the real key on purpose;
``shell=True`` list form with the key as a positional ``$1``; the ``~user`` spelling; case games a case-sensitive volume allows;
a shell string that does not parse (``sh -c 'unbalanced``) is not judged; a key path inside ``git -c core.sshCommand=...`` text;
an ``ssh-add`` honoured through an ``env``/shell operand assignment; ``executable=`` whose argv[0] names a guarded tool is refused
as if it were the command; ``os.execl*``/``execle`` env as the last positional; a cleanup step that raises leaves the wall
installed; an ``eval`` token spelled in other case; ``HOME=""`` in an explicit env reads as the passwd home."""

from __future__ import annotations

import atexit
import functools
import inspect
import os
import pwd
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import unicodedata
from collections.abc import Mapping
from pathlib import Path

import pytest

_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash"})
_POPEN_POS = ("bufsize", "executable", "stdin", "stdout", "stderr", "preexec_fn", "close_fds", "shell", "cwd", "env")
_DEFAULT_MODE = "warn"                                        # flip to "enforce" once CI shows an empty report
_ENV_NAMES = ("SSH_AUTH_SOCK", "SSH_SK_PROVIDER")
_PROVIDER_FLAG = re.compile(r"-[A-Za-z]*[KwD]")             # ssh-keygen -K / -w / -D, clustered (-qK) or attached (-D/lib)


def _real_home() -> str:
    """The account's home from the password database (as ``levain.cockpit.keys.pwd_key_dir``), never ``$HOME``."""
    return pwd.getpwuid(os.getuid()).pw_dir


def _inside(p: str, root: str) -> bool:
    return p == root or p.startswith(root.rstrip("/") + "/")


def _in_tmp(p: str) -> bool:
    return _inside(os.path.realpath(p), os.path.realpath(tempfile.gettempdir()))


_FOLD = sys.platform == "darwin"                            # APFS/HFS+ are case-insensitive and normalisation-insensitive


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s).casefold() if _FOLD else s


def _hits_real_key(tok: str, cwd: str | None, env_home: str | None = None) -> bool:
    home = _real_home()
    h = _norm(os.path.realpath(home))
    eh = env_home or home                                   # ``$HOME`` is the child's HOME (unset: a shell falls back to passwd)
    for cand in {tok, tok.split("=", 1)[-1], tok[2:] if tok[:1] == "-" and tok[1:2] != "-" else tok}:
        s = cand.replace("${HOME}", eh).replace("$HOME", eh)
        s = home + s[1:] if s == "~" or s.startswith("~/") else s
        if not s or "\0" in s:
            continue
        raw = os.path.realpath(s if os.path.isabs(s) else os.path.join(cwd or os.getcwd(), s))
        p = _norm(raw)
        if (_inside(p, h + "/.ssh") or _inside(p, h + "/.levain/keys")) and os.path.isfile(raw):
            return True
    return False


def _split(text: str, depth: int = 0) -> list[str]:
    """shlex tokens of ``text`` plus, recursively, those of a nested ``sh -c STR`` or ``eval ...``; unreadable: ``[]`` (fail open)."""
    try:
        lex = shlex.shlex(text.replace("\n", " ; "), posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return _flatten(list(lex), depth)
    except ValueError:
        return []


def _flatten(toks: list[str], depth: int = 0) -> list[str]:
    out = list(toks)
    for i, t in enumerate(toks):
        script = None
        if os.path.basename(t).lower() in _SHELLS:
            script = next((toks[k + 1] for k, a in enumerate(toks[i + 1:], i + 1) if a[:1] == "-" and a[1:2] != "-" and "c" in a
                           and k + 1 < len(toks)), None)
        elif t == "eval":
            script = " ".join(toks[i + 1:])
        if script is not None and depth < 4:
            out += _split(script, depth + 1)
    return out


def _holds_nul(args, executable) -> bool:
    """True when the executable or any argv element holds a NUL byte (a spawn that can never start). Unreadable input is False."""
    try:
        parts = [executable] if executable is not None else []
        parts += [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
        return any("\0" in os.fsdecode(p) for p in parts)
    except Exception:                                       # noqa: BLE001 -- not readable here: the judge below fails closed on it
        return False


def judge(args,executable=None, env=None, cwd=None, shell: bool = False) -> str | None:
    """Why this spawn may not start (a sentence), or None."""
    try:
        if _holds_nul(args, executable):                    # Popen itself raises ValueError: this spawn can never start, so it cannot reach a key
            return None
        e = {os.fsdecode(k): os.fsdecode(v) for k, v in dict(os.environ if env is None else env).items()}
        c = os.fsdecode(cwd) if cwd is not None else None
        exe = os.fsdecode(executable) if executable is not None else None
        if isinstance(args, (str, bytes)):
            toks, shelly = _split(os.fsdecode(args)), True
            toks = [exe, *toks] if exe else toks
        else:
            raw = [os.fsdecode(a) for a in ([args] if isinstance(args, os.PathLike) else args)]
            if shell:
                toks, shelly = (_split(raw[0]) if raw else []), True
                toks = [exe, *toks] if exe else toks
            else:
                toks, shelly = _flatten([exe, *raw] if exe else raw), False     # the executable AND every argv element
        honour = shelly or os.path.basename(toks[0]) == "env" if toks else False
        honour = honour or any(os.path.basename(t).lower() in _SHELLS or t == "eval" for t in toks)
        assigned = [t.split("=", 1)[1] for t in toks if t.startswith("SSH_AUTH_SOCK=")] if honour else []
        sock = (assigned or [e.get("SSH_AUTH_SOCK", "")])[-1]
        for i, t in enumerate(toks):
            name = os.path.basename(t).lower()
            fake = os.path.isabs(t) and _in_tmp(t)
            if name == "sc_auth" and not fake:
                return "sc_auth (the Secure Enclave identity tool) is never run by a test"
            if name == "ssh-keygen" and not fake and any(_PROVIDER_FLAG.match(a) for a in toks[i + 1:]):
                return "ssh-keygen -K/-w/-D reaches a security-key provider"
            if name == "ssh-add" and not fake and not (sock and _in_tmp(sock)):
                return "ssh-add outside a throwaway agent (SSH_AUTH_SOCK is not under the temp dir)"
        hit = next((t for t in toks if _hits_real_key(t, c, e.get("HOME"))), None)
        return None if hit is None else f"argv names a real key file of the account ({hit})"
    except Exception as exc:                                # noqa: BLE001 -- fail closed
        return f"a spawn the keyguard could not read ({type(exc).__name__}: {exc}); refused"


class AppendOnly(list):
    """The violation record: only ever appended, judged by index."""
    __delitem__ = __setitem__ = __iadd__ = __imul__ = pop = remove = clear = insert = sort = reverse = lambda *a, **k: 1 / 0


_VIOLATIONS: AppendOnly = AppendOnly()
_ACKNOWLEDGED: set[int] = set()
_SEEN = [0]
_LOCK = threading.RLock()                                   # worker threads may refuse while a teardown drains


def acknowledge(since: int) -> None:
    """A test that provokes a refusal on purpose marks the entries from index ``since`` as expected."""
    with _LOCK:
        _ACKNOWLEDGED.update(range(since, len(_VIOLATIONS)))


def _pending() -> list[str]:
    with _LOCK:
        return [_VIOLATIONS[i] for i in range(_SEEN[0], len(_VIOLATIONS)) if i not in _ACKNOWLEDGED]


def _drain() -> list[str]:
    """The refusals nobody judged yet, marked as seen, under one lock."""
    with _LOCK:
        fresh = _pending()
        _SEEN[0] = len(_VIOLATIONS)
        return fresh


_REPORT: list[str] = []                                     # warn mode: what enforce would have refused


def mode() -> str:
    return "enforce" if os.environ.get("LEVAIN_KEYGUARD", _DEFAULT_MODE).strip().lower() == "enforce" else "warn"


def _refuse(why: str, argv) -> None:
    from levain.cockpit.keys import RealKeyReached      # lazily: nothing of levain loads at conftest import
    msg = f"test {os.environ.get('PYTEST_CURRENT_TEST', '<no test>')} tried to start a spawn the keyguard denies: {why}. argv={argv!r}"
    with _LOCK:
        if mode() == "warn":
            _REPORT.append(msg)
            return
        _VIOLATIONS.append(msg)
    raise RealKeyReached(msg)


def _report_text(extra=()) -> str:
    return "\n".join([*_REPORT, *extra])


def pytest_sessionfinish(session):
    """Under xdist a worker hands its report to the controller (``pytest_testnodedown``)."""
    out = getattr(session.config, "workeroutput", None)
    if out is not None:
        out["keyguard_report"] = list(_REPORT)


_NODES: list[str] = []


def pytest_testnodedown(node, error):
    _NODES.extend(getattr(node, "workeroutput", {}).get("keyguard_report", []))


def pytest_terminal_summary(terminalreporter):
    if getattr(terminalreporter.config, "workeroutput", None) is not None:
        return
    text = _report_text(_NODES)
    path = os.environ.get("LEVAIN_KEYGUARD_REPORT")
    if path:
        Path(path).write_text(text + ("\n" if text else ""), encoding="utf-8")
    terminalreporter.write_line("KEYGUARD REPORT BEGIN")
    terminalreporter.write_line(text if text else "KEYGUARD REPORT: 0 denied spawns")
    terminalreporter.write_line("KEYGUARD REPORT END")


def _listed(args):
    if isinstance(args, (str, bytes, os.PathLike)):
        return args
    try:
        return list(args)
    except TypeError:
        return args


def _posix_spawn_signature(path, argv, env, /, **keywords):         # the C signature of ``os.posix_spawn*`` (inspect cannot read it)
    """Stand-in for the builtin's ``(path, argv, env, /, *, file_actions=..., ...)``."""


def real_signature(fn, name: str):
    """``inspect.signature(fn)``; the posix_spawn builtins have ``<unrepresentable>`` defaults and fall back to a stand-in; None if
    nothing can be read (the caller refuses the call)."""
    try:
        return inspect.signature(fn)
    except (TypeError, ValueError):
        return inspect.signature(_posix_spawn_signature) if name.startswith("posix_spawn") else None


def call_parts(n: str, bound: Mapping) -> tuple[object, object, object]:
    """(file, argv, env) of a call of ``os.<n>`` already bound to the real signature; env None where inherited."""
    file = bound.get("path", bound.get("file"))
    if n.startswith("spawnl"):                              # spawnl*(mode, file, *args): the ``e`` forms take env as the last arg
        args = list(bound.get("args", ()))
        return file, (args[:-1] if n.endswith("e") else args), (args[-1] if n.endswith("e") and args else None)
    return file, bound.get("argv", bound.get("args")), (bound.get("env") if (n.endswith("e") or n.startswith("posix")) else None)


def install():
    """Wrap Popen, ``os.posix_spawn*``, ``os.spawn*``, ``os.exec*`` and ``os.system`` while the generator is open."""
    real_init = subprocess.Popen.__init__
    names = ("posix_spawn", "posix_spawnp", "execv", "execve", "execvp", "execvpe", "system", "spawnl", "spawnle", "spawnlp",
             "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe")
    saved = {n: getattr(os, n) for n in names if hasattr(os, n)}

    def check(args, executable=None, env=None, cwd=None, shell=False):
        why = judge(args, executable, env, cwd, shell)
        if why:
            _refuse(why, _listed(args))                  # warn mode returns and the spawn runs

    def popen_init(self, args, *a, **kw):
        self._child_created = False                         # a refusal raises before the real __init__; __del__ reads this
        args = args if isinstance(args, (str, bytes, os.PathLike)) else list(args)      # an iterator is read once
        b = {**dict(zip(_POPEN_POS, a)), **kw}
        check(args, b.get("executable"), b.get("env"), b.get("cwd"), bool(b.get("shell")))
        return real_init(self, args, *a, **kw)

    def wrap(n, real):
        sig = real_signature(real, n)

        @functools.wraps(real)
        def run(*a, **kw):
            if sig is None:
                _refuse("a spawn function whose signature cannot be read", [n])
            try:
                bound = sig.bind(*a, **kw).arguments        # judged from the REAL signature, positional or keyword
            except TypeError:
                return real(*a, **kw)                       # a malformed call: the real function raises, nothing starts
            if n == "system":
                check(bound["command"], shell=True)
            else:
                file, argv, env = call_parts(n, bound)
                check(argv, file, env)
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


def scrub_environ() -> dict[str, str | None]:
    """Delete the ambient signing capability and point git at an identity-only config; returns the previous values."""
    previous = {n: os.environ.get(n) for n in _ENV_NAMES}
    for n in ("SSH_AUTH_SOCK", "SSH_SK_PROVIDER"):
        os.environ.pop(n, None)
    return previous


def restore_environ(previous: Mapping[str, str | None]) -> None:
    for n, v in previous.items():
        os.environ.pop(n, None) if v is None else os.environ.__setitem__(n, v)


def make_cleanup(previous, session):
    """The one end-of-run act (close the wall, restore the environment), idempotent: the session fixture, ``pytest_unconfigure``
    (a run that collected nothing, or failed collection, never starts the fixture) and ``atexit`` may all call it."""
    done: list[bool] = []

    def cleanup() -> None:
        if done:
            return
        done.append(True)
        restore_environ(previous)
        session.close()
    return cleanup


_ORIGINAL_ENV = scrub_environ()
_SESSION = install()
next(_SESSION)
cleanup = make_cleanup(_ORIGINAL_ENV, _SESSION)
atexit.register(lambda: cleanup())


def keyguard_cleanup() -> None:
    cleanup()


@pytest.fixture(autouse=True, scope="session")
def _no_signing_spawn_in_tests():
    """The wall stands since import; the scrub is repeated here (conftest's session-home setup runs after import and drops
    GIT_CONFIG_GLOBAL); both end with the session, which fails on a refusal nobody judged."""
    scrub_environ()
    try:
        yield
    finally:
        cleanup()
    if _pending():
        pytest.fail("the keyguard refused a spawn that no test judged:\n" + "\n".join(_pending()), pytrace=False)


@pytest.fixture(autouse=True)
def _a_refused_spawn_fails_its_test():
    """A caller that catches the refusal still fails here. Judged by index, so a refusal in a wider-scoped fixture fails the
    first test to finish after it."""
    yield
    fresh = _drain()
    if fresh:
        pytest.fail("the keyguard refused a spawn during this test (the code under test caught it):\n" + "\n".join(fresh), pytrace=False)
