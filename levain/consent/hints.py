"""levain.consent.hints (Q16): what a shell command LOOKS like, in plain words, beside its digest on a decision surface.

``shell_hints(command, cwd)`` describes; it never decides. It is pure (no I/O, no clock, no environment) and returns
short plain-English strings, empty for a command with nothing notable. A hint is a reading aid for the human who signs:
a wrong or missing hint costs a worse-informed human, never a granted command, because nothing on a decision path may
import this module (asserted by ``tests/test_consent_hints.py``). The rendering layer reaches it through
``levain.cockpit.hintview`` only.

The second source of hints (a model reading the command) is NOT built here; the return type is a list so another source can
add lines later.
"""
from __future__ import annotations

import posixpath
import re
import shlex

__all__ = ["CUT_HINT", "UNREAD_HINT", "shell_hints"]

UNREAD_HINT = "could not be read; read the command itself"
CUT_HINT = "the command was cut; hints cover the shown part only"

_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh"})
_INTERPRETERS = frozenset({"python", "python2", "python3", "perl", "ruby", "node", "php", "lua", "deno", "bun"})
_FETCH = frozenset({"curl", "wget"})
_CONTROL = frozenset({"|", "||", "&&", ";", ";;", "&", "(", ")", "|&"})
_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>"})
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SUBST = re.compile(r"\$\(|`")
_WRAPPERS = frozenset({"env", "command", "exec", "nohup", "time", "nice", "xargs"})
_SAFE_SINKS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})


def _words(command: str) -> list[str]:
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    return list(lex)


def _outside(path: str, cwd: str) -> bool:
    """A path the command writes that is not under the working directory: absolute elsewhere, a ``~`` path, or a
    relative path that climbs out of it. ``$VAR`` paths are unknown, and said so by being treated as outside."""
    if path in _SAFE_SINKS or not path:
        return False
    if path.startswith(("~", "$")):
        return True
    base = posixpath.normpath(cwd) if cwd.startswith("/") else ""
    full = posixpath.normpath(path if path.startswith("/") else posixpath.join(base or "/", path))
    if not base:
        return path.startswith("/") or full.startswith("..")
    return not (full == base or full.startswith(base.rstrip("/") + "/"))


def shell_hints(command: str, cwd: str = "") -> list[str]:
    """What this command looks like, as short strings (empty when nothing notable). Never raises."""
    if not isinstance(command, str):
        return [UNREAD_HINT]
    try:
        words = _words(command)
    except ValueError:
        return [UNREAD_HINT]
    out: list[str] = []

    def add(text: str) -> None:
        if text not in out:
            out.append(text)

    if _SUBST.search(command):
        add("command substitution ($() or backticks) runs another command inside this one")
    # split into simple commands at the control operators; each one's first non-assignment word is its program
    stage: list[str] = []
    stages: list[tuple[str, list[str]]] = []
    for w in [*words, ";"]:
        if w in _CONTROL:
            if stage:
                stages.append((w, stage))
            stage = []
        else:
            stage.append(w)
    fetched = False
    for i, (sep, st) in enumerate(stages):
        j = 0
        while j < len(st) and _ASSIGN.match(st[j]):
            j += 1
        if j:
            add(f"env prefix {st[0].split('=', 1)[0]}= changes how the command runs")
        rest = st[j:]
        while rest and posixpath.basename(rest[0]) in _WRAPPERS:
            rest = [w for w in rest[1:] if not w.startswith("-") and not _ASSIGN.match(w)] if rest[0] == "env" else rest[1:]
        prog = posixpath.basename(rest[0]) if rest else ""
        if prog in _FETCH:
            fetched = fetched or sep in ("|", "|&")
        prev_pipes = i > 0 and stages[i - 1][0] in ("|", "|&")
        if prev_pipes and prog in _SHELLS | _INTERPRETERS:
            came = "downloads, then " if any(posixpath.basename(w) in _FETCH for w in stages[i - 1][1]) else ""
            add(f"pipes into a {'shell' if prog in _SHELLS else 'interpreter'}: {came}runs what arrives ({prog})")
        elif prev_pipes and fetched and prog:
            add(f"a network fetch is piped onward into {prog}")
        if prog == "sudo":
            add("runs with sudo (elevated rights)")
        if prog == "eval":
            add("eval runs text as a command")
        if prog == "rm" and any((w.startswith("-") and not w.startswith("--") and ("r" in w or "R" in w))
                                or w == "--recursive" for w in rest[1:]):
            add("rm -r removes a whole directory tree")
        if prog == "tee":
            for w in rest[1:]:
                if not w.startswith("-") and _outside(w, cwd):
                    add(f"tee writes {w}, outside the working directory")
        for k, w in enumerate(rest):
            if w.startswith("<<") and not w.startswith("<<<"):
                add("heredoc: the text after the command is part of what runs")
            if w in _REDIRECTS and k + 1 < len(rest) and _outside(rest[k + 1], cwd):
                add(f"writes {rest[k + 1]}, outside the working directory")
    return out
