"""levain.consent.hints (Q16): what a shell command LOOKS like, in plain words, beside its digest on a decision surface.

``shell_hints(command, cwd)`` describes; it never decides. It is pure (no I/O, no clock, no environment) and returns
short plain-English strings, empty for a command with nothing notable. A hint is a reading aid for the human who signs:
a wrong or missing hint costs a worse-informed human, never a granted command, because nothing on a decision path may
import this module (asserted by ``tests/test_consent_hints.py``). The rendering layer reaches it through
``levain.cockpit.hintview`` only.

It knows a few patterns only. Out of scope: a download written to a file and run in a later stage
(``curl -o x.sh URL; bash x.sh``), and anything a pattern list cannot see; the surfaces say "no pattern matched: read the
command" for an empty list, never "safe". The second source of hints (a model reading the command) is NOT built here; the
return type is a list so another source can add lines later.
"""
from __future__ import annotations

import json
import posixpath
import re
import shlex

__all__ = ["CUT_HINT", "NONE_HINT", "UNREAD_HINT", "shell_hints"]

UNREAD_HINT = "could not be read; read the command itself"
CUT_HINT = "the command was cut; hints cover the shown part only"
NONE_HINT = "no pattern matched: read the command"

_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh"})
_INTERPRETERS = frozenset({"python", "python2", "python3", "perl", "ruby", "node", "php", "lua", "deno", "bun"})
_EVAL_FLAGS = frozenset({"-c", "-e", "-E", "-pe", "-ne", "-lne", "-lpe", "-ape", "--eval", "-r"})
_FETCH = frozenset({"curl", "wget"})
_NET = frozenset({"nc", "ncat", "netcat", "socat", "telnet"})
_CONTROL = frozenset({"|", "||", "&&", ";", ";;", "&", "(", ")", "|&"})
_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>", ">&"})
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SUBST = re.compile(r"\$\(|`")
_HEREDOC = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?([A-Za-z_][A-Za-z0-9_]*))")
_WRAPPERS = frozenset({"env", "command", "exec", "nohup", "time", "nice", "xargs"})
_ELEVATORS = frozenset({"sudo", "doas"})
_SUDO_ARG_FLAGS = frozenset({"-u", "-g", "-h", "-p", "-C", "-r", "-t", "-U", "-D", "-R", "-T"})
_SSH_ARG_FLAGS = frozenset("bcDEeFIiJLlmOopQRSWw")
_MOVERS = frozenset({"cd", "pushd", "popd"})
_SAFE_SINKS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})
_MAX_SHOWN = 60


def _q(text: str) -> str:
    """Agent text inside a levain-voiced sentence: quoted (control characters escaped) and cut."""
    cut = text if len(text) <= _MAX_SHOWN else text[:_MAX_SHOWN] + "…"
    return json.dumps(cut, ensure_ascii=False)


def _scan(command: str) -> tuple[str, str, bool] | None:
    """One pass over the command text, quote-aware: returns ``(cleaned, subst_text, heredoc)`` or ``None`` when a quote
    is left open. ``cleaned`` has heredoc bodies cut out, comments dropped, line continuations joined and every unquoted
    newline made a ``;``. ``subst_text`` is what substitution is looked for in: no single-quoted span, but with the body
    of a heredoc whose terminator is unquoted (the shell expands that)."""
    out: list[str] = []
    sub: list[str] = []
    pending: list[tuple[str, bool, bool]] = []     # (terminator, strip tabs, expands)
    heredoc = False
    i, n = 0, len(command)
    quote = ""
    while i < n:
        c = command[i]
        if quote == "'":
            out.append(c)
            if c == "'":
                quote = ""
            i += 1
            continue
        if quote == '"':
            out.append(c)
            sub.append(c)
            if c == "\\" and i + 1 < n:
                out.append(command[i + 1])
                sub.append(command[i + 1])
                i += 2
                continue
            if c == '"':
                quote = ""
            i += 1
            continue
        if c == "\\":
            if i + 1 < n and command[i + 1] == "\n":
                out.append(" ")
                i += 2
                continue
            out.append(command[i:i + 2])
            sub.append(command[i:i + 2])
            i += 2
            continue
        if c in "'\"":
            quote = c
            out.append(c)
            if c == '"':
                sub.append(c)
            i += 1
            continue
        if c == "#" and (not out or out[-1] in " \t\n;|&()"):
            while i < n and command[i] != "\n":
                i += 1
            continue
        if c == "<" and command.startswith("<<", i) and not command.startswith("<<<", i):
            m = _HEREDOC.match(command, i)
            if m:
                word = m.group(2) or m.group(3) or m.group(4)
                pending.append((word, m.group(1) == "-", m.group(4) is not None and "\\" not in m.group(0)))
                heredoc = True
                out.append(m.group(0))
                sub.append(m.group(0))
                i = m.end()
                continue
        if c == "\n":
            out.append(" ; ")
            sub.append("\n")
            i += 1
            while pending:
                word, strip, expands = pending.pop(0)
                while i < n:
                    j = command.find("\n", i)
                    line = command[i:] if j < 0 else command[i:j]
                    i = n if j < 0 else j + 1
                    if (line.lstrip("\t") if strip else line) == word:
                        break
                    if expands:
                        sub.append(line + "\n")
            continue
        out.append(c)
        sub.append(c)
        i += 1
    if quote:
        return None
    return "".join(out), "".join(sub), heredoc


def _words(command: str) -> list[str]:
    """The command's words, quotes removed. An operator is only an operator when it was not quoted: a quoted ``";"`` or
    ``">"`` is an argument, so it is returned with a NUL after it and matches no operator."""
    lex = shlex.shlex(command, posix=False, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    ops = _CONTROL | _REDIRECTS
    out: list[str] = []
    for t in lex:
        if t in ops or t.startswith("<") or all(ch in "();<>|&" for ch in t):
            out.append(t)
            continue
        got = shlex.split(t)
        word = "".join(got) if got else ""
        out.append(word + "\x00" if word in ops else word)
    return out


def _outside(path: str, cwd: str) -> str:
    """How a written path relates to the working directory: ``"out"`` (surely outside), ``"maybe"`` (``~`` or ``$VAR``:
    unknown), or ``""`` (inside, or a harmless sink)."""
    if path in _SAFE_SINKS or not path:
        return ""
    if path.startswith(("~", "$")):
        return "maybe"
    base = posixpath.normpath(cwd) if cwd.startswith("/") else ""
    full = posixpath.normpath(path if path.startswith("/") else posixpath.join(base or "/", path))
    if not base:
        return "out" if path.startswith("/") or full.startswith("..") else ""
    return "" if (full == base or full.startswith(base.rstrip("/") + "/")) else "out"


def _hints(command: str, cwd: str, depth: int) -> list[str] | None:
    scanned = _scan(command)
    if scanned is None:
        return None
    cleaned, subst_text, heredoc = scanned
    try:
        words = _words(cleaned)
    except ValueError:
        return None
    out: list[str] = []

    def add(text: str) -> None:
        if text not in out:
            out.append(text)

    if heredoc:
        add("heredoc: the text after the command is part of what runs")
    if _SUBST.search(subst_text):
        add("command substitution ($() or backticks) runs another command inside this one")
    for k, w in enumerate(words):
        if w.endswith("<(") or (w == "<" and k + 1 < len(words) and words[k + 1] == "("):
            depth_p, hit = 0, False
            for x in words[k + 1:]:
                if x == ")" and depth_p == 0:
                    break
                if x == "(" or x.endswith("<("):
                    depth_p += 1
                elif x == ")":
                    depth_p -= 1
                if posixpath.basename(x) in _FETCH:
                    hit = True
            if hit:
                add("runs what a download produces (process substitution of a fetch)")
    stage: list[str] = []
    stages: list[tuple[str, list[str]]] = []
    for w in [*words, ";"]:
        if w in _CONTROL:
            if stage:
                stages.append((w, stage))
            stage = []
        else:
            stage.append(w)

    def writes(path: str, moved: bool, verb: str) -> None:
        if not path or path in _SAFE_SINKS:
            return
        if moved and not path.startswith(("/", "~", "$")):
            add(f"{verb} {_q(path)} (a cd earlier: paths are relative to where it moved)")
            return
        where = _outside(path, cwd)
        if where == "maybe":
            add(f"{verb} {_q(path)} (may be outside the working directory)")
        elif where:
            add(f"{verb} {_q(path)}, outside the working directory")

    fetched = False
    moved = False
    for i, (sep, st) in enumerate(stages):
        j = 0
        while j < len(st) and _ASSIGN.match(st[j]):
            j += 1
        if j:
            add(f"env prefix {_q(st[0].split('=', 1)[0])}= changes how the command runs")
        rest = st[j:]
        while rest and posixpath.basename(rest[0]) in _WRAPPERS | _ELEVATORS:
            w0 = posixpath.basename(rest[0])
            if w0 in _ELEVATORS:
                add("runs with sudo (elevated rights)")
                k = 1
                while k < len(rest) and rest[k].startswith("-"):
                    k += 2 if rest[k] in _SUDO_ARG_FLAGS else 1
                rest = rest[k:]
            elif w0 == "env":
                rest = [w for w in rest[1:] if not w.startswith("-") and not _ASSIGN.match(w)]
            else:
                rest = rest[1:]
        prog = posixpath.basename(rest[0]) if rest else ""
        args = rest[1:]
        if prog in _FETCH:
            fetched = fetched or sep in ("|", "|&")
        prev_pipes = i > 0 and stages[i - 1][0] in ("|", "|&")
        if prev_pipes and prog in _SHELLS | _INTERPRETERS:
            came = "downloads, then " if any(posixpath.basename(w) in _FETCH for w in stages[i - 1][1]) else ""
            add(f"pipes into a {'shell' if prog in _SHELLS else 'interpreter'}: {came}runs what arrives ({_q(prog)})")
        elif prev_pipes and fetched and prog:
            add(f"a network fetch is piped onward into {_q(prog)}")
        if prog in _SHELLS | _INTERPRETERS:
            for k, a in enumerate(args):
                is_c = (re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", a) is not None) if prog in _SHELLS else a in _EVAL_FLAGS
                if is_c:
                    add(f"{'a shell' if prog in _SHELLS else 'an interpreter'} runs a string as a command")
                    if prog in _SHELLS and depth < 1 and k + 1 < len(args):
                        inner = _hints(args[k + 1], cwd, depth + 1)
                        for h in inner or []:
                            add(h)
                    break
        if prog == "eval":
            add("eval runs text as a command")
        if prog == "rm" and any((w.startswith("-") and not w.startswith("--") and ("r" in w or "R" in w))
                                or w == "--recursive" for w in args):
            names = [w for w in args if not w.startswith("-")]
            shown = ", ".join(_q(w) for w in names) if names and len(names) <= 3 and sum(map(len, names)) <= _MAX_SHOWN else ""
            add("rm -r removes a directory tree" + (f": {shown}" if shown else ""))
        if prog == "tee":
            for w in args:
                if not w.startswith("-"):
                    writes(w, moved, "tee writes")
        if prog in _NET:
            add(f"{_q(prog)} sends data over the network")
        if prog == "git" and "push" in args and any(
                w in ("-f", "--force") or w.startswith("--force-with-lease") or w.startswith("--force-if-includes")
                for w in args):
            add("force-push rewrites the remote branch")
        if prog == "dd":
            for w in args:
                if w.startswith("of="):
                    add(f"dd writes raw bytes to {_q(w[3:])}")
        if prog in ("chmod", "chown", "chgrp") and any(
                w == "--recursive" or (w.startswith("-") and not w.startswith("--") and "R" in w) for w in args):
            add("changes permissions or owner of a whole tree")
        if prog == "ssh":
            words_left, k = [], 0
            while k < len(args):
                a = args[k]
                if a.startswith("-") and len(a) == 2 and a[1] in _SSH_ARG_FLAGS:
                    k += 2
                    continue
                if not a.startswith("-"):
                    words_left.append(a)
                k += 1
            if len(words_left) > 1:
                add("ssh runs a command on another machine")
        for k, w in enumerate(rest):
            if w in _REDIRECTS and k + 1 < len(rest):
                writes(rest[k + 1], moved, "writes")
        if prog in _MOVERS:
            moved = True
    return out


def shell_hints(command: str, cwd: str = "") -> list[str]:
    """What this command looks like, as short strings (empty when nothing notable). Never raises."""
    if not isinstance(command, str):
        return [UNREAD_HINT]
    try:
        got = _hints(command, cwd if isinstance(cwd, str) else "", 0)
    except Exception:  # noqa: BLE001 - a reading aid never takes a decision surface down
        return [UNREAD_HINT]
    return [UNREAD_HINT] if got is None else got
