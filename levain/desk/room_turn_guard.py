#!/usr/bin/env python3
"""levain.desk.room_turn_guard — the PreToolUse guard of the cockpit room's default (host) profile.

Run as a hook script, never imported by the room: the launcher reads this file's bytes once per launch, checks them
against the sha256 levain pins for it (:data:`levain.desk.room_default.ROOM_TURN_GUARD_SHA256`), copies them to its
run directory and names that copy in the child's ``--settings`` as ``<python> -I <copy>``. So it is standard library
only, and a change to this file is a deliberate re-pin.

PROVENANCE. A port of flow's ``scripts/room_turn_guard.py`` at flow commit e54ce5fc0 (2026-11-12), itself the Room-wording
fork of flowConnect's ``server/chat_turn_guard.py`` (flowConnect commit 9857570, file sha256 55d9f08be102...). The code
below this docstring is flow's, byte-for-byte, but for one change in REFUSAL: "tell Phill he can" reads "tell the user
they can" (levain is not one operator's); only that and this docstring are levain's. flowConnect's lane 6 (11-12) fixes
bypasses in that guard: when it lands, this copy is owed a re-sync from flowConnect (keep the "Room turn" refusal
wording and the "user" change), then a new pin.

What it refuses (flowConnect's rule, kept as written): work that cannot finish inside one chat turn. A Bash, Agent or
Task call with ``run_in_background``; CronCreate, Monitor, Workflow and ScheduleWakeup; and a Bash command that,
scanned as shell tokens (quotes, comments, heredoc data and ``$((...))`` kept as what they are), backgrounds with a
lone ``&``, runs ``nohup``/``setsid``/``disown``, or runs one of the long review scripts named in ``_LONG_SCRIPTS``
directly, through python, a wrapper, ``<shell> -c``, ``eval`` or a substitution. Those two names are flowConnect's
own scripts and are kept so the port stays identical; for any other host they simply never match. It prints a deny
decision and exits 2; anything else, including a payload it cannot parse, exits 0 (a hook crash must not brick the
chat). SCOPE: a model's own slip, not deliberate evasion (tmux, at, launchctl, a script it wrote, a long foreground
command are not guarded).
"""
import json
import re
import sys

REFUSAL = (
    "Refused: {what}. Long work is not allowed in a Room turn: nothing started in the background reports "
    "back here yet, and a review run here would hold the chat. Do not promise a later action. Hand it to a "
    "seat or the desk: tell the user they can drop it in the cockpit's Desk thread, or ask you to relay it to the desk, "
    "or open a seat for it."
)

_LATER_TOOLS = {"CronCreate", "Monitor", "Workflow", "ScheduleWakeup"}
HOOK_MATCHER = "|".join(["Bash", "Agent", "Task", *sorted(_LATER_TOOLS)])   # the one home of what the hook is loaded for
_LONG_SCRIPTS = {"deep_review.py", "consult.py"}
_DETACHERS = {"nohup", "setsid", "disown"}
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
_CONTROL = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}"}
# Wrappers that run the next command, and the options of each that take a value that is not the command.
_WRAPPERS = {"time", "env", "command", "exec", "sudo", "builtin", "nice", "timeout", "caffeinate", "stdbuf",
             "arch", "xargs", "uv", "poetry", "pipenv"}
_VALUE_OPTS = {"nice": {"-n"}, "sudo": {"-u", "-g", "-p"}, "timeout": {"-k", "-s"}, "env": {"-u"},
               "xargs": {"-n", "-I", "-P", "-d", "-L"}, "caffeinate": {"-t"}, "stdbuf": set()}
_PY_VALUE_OPTS = {"-W", "-X", "-Q"}
_DURATION = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_OPS = ("&&", "||", "|&", ";;", "&>>", "&>", "<<<", "<<-", "<<", ">>", "<&", ">&", ">|", "<>",
        "&", ";", "|", "(", ")", "<", ">")
_REDIRECTS = {"&>>", "&>", "<<<", "<<-", "<<", ">>", "<&", ">&", ">|", "<>", "<", ">"}
_SEPARATORS = {"&&", "||", "|&", ";;", ";", "|"}
_HEREDOC = re.compile(r"<<(-?)[ \t]*(['\"]?)([^\s'\"<>|&;()]+)\2[^\n]*\n(.*?)\n[ \t]*\3[ \t]*(?=\n|$)", re.S)
_SUBST = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")


def _command_index(seg: list[str]) -> int | None:
    """Index of the program a segment runs, past env assignments, control keywords and wrappers (with their
    option values); None when the segment runs nothing, or only asks about a command (`command -v x`)."""
    i = 0
    while i < len(seg):
        tok = seg[i]
        base = tok.rsplit("/", 1)[-1]
        if _ENV_ASSIGN.match(tok) or tok in _CONTROL:
            i += 1
        elif base in _WRAPPERS:
            i += 1
            while i < len(seg) and (seg[i].startswith("-") or _ENV_ASSIGN.match(seg[i])):
                if base == "command" and seg[i] in {"-v", "-V"}:
                    return None
                i += 2 if seg[i] in _VALUE_OPTS.get(base, ()) else 1
            if base == "timeout" and i < len(seg) and _DURATION.match(seg[i]):
                i += 1
            if base in {"uv", "poetry", "pipenv"} and i < len(seg) and seg[i] == "run":
                i += 1
        else:
            return i
    return None


def _first_word(text: str) -> str:
    """The program a shell fragment runs: its last command's program, past env assignments and wrappers."""
    words = re.split(r"[;|(]|&&|\|\|", text)[-1].split()
    i = _command_index(words)
    return words[i].rsplit("/", 1)[-1] if i is not None else ""


def _heredocs(command: str) -> str:
    """A heredoc body is data (dropped) unless the command that reads it runs it as shell: a shell keeps
    its body as commands (a python body is not shell, so scanning it would only invent launches), and an unquoted body keeps only its `$(...)`/backtick substitutions, which run."""
    def repl(m: re.Match) -> str:
        header = m.group(0)[:m.group(0).index("\n")]
        head = command[command.rfind("\n", 0, m.start()) + 1:m.start()]
        reader = _first_word(head)
        if reader in _SHELLS or reader in {"source", ".", "eval"}:
            return header + "\n" + m.group(4) + "\n"
        if m.group(2) == "":
            return header + "\n" + "\n".join(a or b for a, b in _SUBST.findall(m.group(4))) + "\n"
        return header
    return _HEREDOC.sub(repl, command.replace("\\\n", " "))


def _balanced(text: str, i: int) -> int:
    """Index just past the `)` closing the `(` at text[i-1]."""
    depth = 1
    while i < len(text) and depth:
        depth += {"(": 1, ")": -1}.get(text[i], 0)
        i += 1
    if depth:
        raise ValueError("unbalanced $(")
    return i


def _arithmetic(text: str, i: int, subs: list[str]) -> int:
    """Skip the `$((...))` at text[i] and return the index past it. Its operators are arithmetic (a `&` is bitwise),
    but a `$(...)` or backtick inside it still runs: then the body is kept as a substitution to scan (an `&` beside
    such a substitution then reads as a launch, a false refusal the guard accepts over a missed one)."""
    end = _balanced(text, i + 2)
    body = text[i + 3:end - 2]
    if "$(" in body or "`" in body:
        subs.append(body)
    return end


def _scan(command: str) -> tuple[list[tuple[str, str]], list[str]]:
    """(tokens, substitutions): tokens are ("word", text) or ("op", operator); operators count only outside
    quotes, a `#` comment is dropped, a newline is a `;`, and each `$(...)` / backtick body is returned for its
    own scan. Raises ValueError on an unbalanced quote."""
    text = _heredocs(command)
    tokens: list[tuple[str, str]] = []
    subs: list[str] = []
    word: list[str] = []
    has_word = False

    def flush():
        nonlocal has_word
        if has_word:
            tokens.append(("word", "".join(word)))
        word.clear()
        has_word = False

    i = 0
    while i < len(text):
        c = text[i]
        if c in " \t\r":
            flush()
            i += 1
        elif c == "\n":
            flush()
            tokens.append(("op", ";"))
            i += 1
        elif c == "#" and not has_word:
            while i < len(text) and text[i] != "\n":
                i += 1
        elif c == "\\":
            word.append(text[i + 1] if i + 1 < len(text) else "")
            has_word = True
            i += 2
        elif c == "'":
            end = text.find("'", i + 1)
            if end < 0:
                raise ValueError("unbalanced '")
            word.append(text[i + 1:end])
            has_word = True
            i = end + 1
        elif c == '"':
            i += 1
            has_word = True
            while i < len(text) and text[i] != '"':
                if text[i] == "\\" and i + 1 < len(text):
                    word.append(text[i + 1])
                    i += 2
                elif text.startswith("$((", i):   # arithmetic, quoted as unquoted: its `&` is bitwise
                    i = _arithmetic(text, i, subs)
                    word.append("0")
                elif text.startswith("$(", i):
                    end = _balanced(text, i + 2)
                    subs.append(text[i + 2:end - 1])
                    i = end
                else:
                    word.append(text[i])
                    i += 1
            if i >= len(text):
                raise ValueError('unbalanced "')
            i += 1
        elif text.startswith("$((", i):   # arithmetic: its `&` is bitwise, not a background
            i = _arithmetic(text, i, subs)
            word.append("0")
            has_word = True
        elif text.startswith("$(", i):
            end = _balanced(text, i + 2)
            subs.append(text[i + 2:end - 1])
            word.append("$()")
            has_word = True
            i = end
        elif c == "`":
            end = text.find("`", i + 1)
            if end < 0:
                raise ValueError("unbalanced `")
            subs.append(text[i + 1:end])
            word.append("$()")
            has_word = True
            i = end + 1
        elif c in ";&|()<>":
            flush()
            op = next(o for o in _OPS if text.startswith(o, i))
            tokens.append(("op", op))
            i += len(op)
        else:
            word.append(c)
            has_word = True
            i += 1
    flush()
    return tokens, subs


def _launch(command: str, depth: int = 0) -> str | None:
    """What the command launches that cannot survive the phone turn, or None."""
    tokens, subs = _scan(command)
    segment: list[str] = []
    segments: list[list[str]] = []
    skip_target = False        # the word after a redirect is a file or delimiter, not an argument
    for kind, val in tokens:
        if kind == "word":
            if skip_target:
                skip_target = False
            else:
                segment.append(val)
        elif val == "&":   # a lone `&` is the background operator (`&&`, `&>`, `>&`, `|&` are other operators)
            return "a command detached into the background with &"
        elif val in _REDIRECTS:
            skip_target = True
        else:
            segments.append(segment)
            segment = []
    segments.append(segment)
    for seg in segments:
        found = _segment_launch(seg, depth)
        if found:
            return found
    if depth < 2:
        for inner in subs:
            try:
                found = _launch(inner, depth + 1)
            except ValueError:
                found = None
            if found:
                return found
    return None


def _segment_launch(seg: list[str], depth: int) -> str | None:
    i = _command_index(seg)
    if i is None:
        return None
    prog = seg[i].rsplit("/", 1)[-1]
    rest = seg[i + 1:]
    if prog in _DETACHERS:
        return f"a command detached into the background with {prog}"
    if prog in _LONG_SCRIPTS:
        return f"{prog} is a review/consultation that runs for minutes"
    if prog.startswith("python"):
        script = _python_script(rest)
        if script in _LONG_SCRIPTS:
            return f"{script} is a review/consultation that runs for minutes"
    if depth < 2 and prog == "eval":
        return _launch(" ".join(rest), depth + 1)
    if depth < 2 and prog in _SHELLS:
        flag = next((n for n, a in enumerate(rest) if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", a)), None)
        if flag is not None and flag + 1 < len(rest):
            return _launch(rest[flag + 1], depth + 1)
    return None


def _python_script(args: list[str]) -> str:
    """Basename of the script python runs: its first non-option argument (-m and -c run no script file)."""
    n = 0
    while n < len(args):
        if args[n] in {"-m", "-c"}:
            return ""
        if args[n] in _PY_VALUE_OPTS:
            n += 1
        elif not args[n].startswith("-"):
            return args[n].rsplit("/", 1)[-1]
        n += 1
    return ""


def refusal_for(event: dict) -> str | None:
    """The refusal reason for this tool call, or None when the turn may run it."""
    tool = event.get("tool_name")
    args = event.get("tool_input") or {}
    if tool in ("Bash", "Agent", "Task") and args.get("run_in_background"):
        return REFUSAL.format(what=f"a background {tool} call")
    if tool in _LATER_TOOLS:   # work that reports back only in a later turn, or a watch past this one
        return REFUSAL.format(what=f"{tool} schedules work for a later turn")
    if tool != "Bash":
        return None
    try:
        what = _launch(str(args.get("command") or ""))
    except ValueError:   # an unbalanced quote (an apostrophe in a comment): fall back to the plain-text shapes
        what = _fallback(str(args.get("command") or ""))
    return REFUSAL.format(what=what) if what else None


_FALLBACK = re.compile(r"\bpython[\d.]*\s+(?:-[\w-]+\s+)*\S*(?:deep_review|consult)\.py\b|(?<![&>])&\s*$|\bnohup\b|\bsetsid\b")


def _fallback(command: str) -> str | None:
    return "a long-running or background command" if _FALLBACK.search(command) else None


def main() -> int:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            return 0
    except (ValueError, OSError):
        return 0
    reason = refusal_for(event)
    if reason is None:
        return 0
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    sys.stdout.flush()
    sys.stderr.write(reason + "\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
