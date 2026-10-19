"""levain.consent.band — the question band: ``levain ask``, ``levain question``, and the answer's way back (Q17).

An agent that needs the operator's judgement and can keep working meanwhile files a QUESTION
(:class:`~levain.consent.questions.QuestionStore`): what it needs, two to six options, the one it recommends, and
what the question blocks. ``levain ask`` files it, pings the operator through the agent's notify seam (the same
command and phone link a gated call pings with) and returns at once: the agent goes on with everything the question
does not block. The operator answers once, with an option's key or in his own words (``levain question answer``).
The answer's text then reaches the asking session ONCE, as ``additionalContext`` on its next tool call (the gate
hook carries it, :func:`question_context`, beside the session's late rulings).

An answer authorises nothing (Phill's 26a): it is information for the agent, unsigned, and every gated call the
agent then makes still goes through the gate. The delivered text says so. ``levain ask`` is not a boundary either:
it runs as the agent, and the agent, session and queue directory it names are the caller's word.

The answer is claimed by the session's MAIN thread only (a hook session key with no ``#agent:`` part), keyed by the
session id ``levain ask`` read from the agent's environment: a subagent's context is discarded when it ends, and
the answer has to reach the conversation that goes on.

Known edges (one line each, not built against):

- an answer is unsigned: any process of the operator's uid can file or answer a question, and the text the agent
  receives names the channel the answer came by, never a verified person.
- a question has no expiry: an unanswered one stays open in the listing until it is answered.
- a session that has ENDED is resumed with the answer as its prompt when the answer is recorded (or by ``levain
  question deliver``), on what ``levain ask`` recorded (:func:`ask_resume_meta`); one that is live but idle (a
  terminal, an idle background session) gets it at its next tool call, and so does one whose resume is refused.
- a main thread waiting on a long subagent gets the answer after the subagent returns.
- with no session in the environment the question names :data:`NO_SESSION`, which no hook's session matches: the
  answer is recorded and listed, never delivered.
- the hook claims the answer before Claude Code or Codex reads its output: a hook killed in between (its timeout
  after a long hold) loses that answer's text, as a ruling's.
- a resume is never started while any running process's command line names the session id
  (:func:`levain.consent.delivery.session_named_running`): an unrelated one that names it (``tail -f`` of its
  transcript) keeps the answer waiting for the session's next hook. That check reads every process's command line
  and keeps nothing but the yes or no.
- the resume record is read from a transcript's first 1 MiB and a Codex rollout's first line up to 1 MiB: past that
  no start directory is recorded and the answer waits for the next hook; so does a Codex session whose prompt or an
  option value is exactly ``exec``, ``app-server`` or ``mcp-server`` (read as that subcommand).
- Codex's hook gives a session id only (no subagent id is appended, as for its halts): if a Codex subagent's calls
  carry the parent's id, its hook can claim the answer [not measured].

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from levain.cockpit.consent import ConsentError
from levain.consent.questions import Question, QuestionStore, QuestionStoreError

__all__ = ["AGENT_LINE", "ANSWER_TAIL", "NO_SESSION", "QUOTED_NOTE", "add_parsers", "answer_line", "ask_resume_meta",
           "deliver_answer", "deliver_answers", "file_question", "question_context", "run_ask", "run_question",
           "transcript_facts"]

# the session a question names when the environment names none (no hook's session is ever this)
NO_SESSION = "levain-ask"
# what an agent is told so it asks instead of stopping. No template, hook or brief carries it today (DESIGN_QMCP
# amendment R-q23): it is the wording for a brief that wants it; the `ask` tool reaches the agent through the MCP
# server's own tools/list, not through this line.
AGENT_LINE = ("When you need the operator's judgement and can keep working on something else, run `levain ask "
              "--what \"<the question, one line>\" --option <key>=<label> --option <key>=<label> --rec <key> "
              "--blocks \"<what waits on the answer>\"` (2-6 options; keys lowercase a-z, 0-9, _ or -), or the "
              "levain_band `ask` tool with the same four fields, and carry on "
              "with what it does not block; the answer arrives in your context at a later tool call. Do not do what "
              "it blocks until then. If nothing else is unblocked, say so in your reply and end your turn: do not "
              "poll for the answer.")
# the line every delivered block of answers ends with (26a)
ANSWER_TAIL = ("An answer is information, not a permission: a gated call still goes through levain's gate, and "
               "levain has checked nothing about who recorded it.")
EXIT_OK, EXIT_REFUSED = 0, 2
# the agents a question can name (the parser lists them without importing either adapter)
_AGENT_NAMES = ("claude_code", "codex")
_SUBAGENT = "#agent:"


def _agents() -> dict[str, Any]:
    from levain.consent.adapters import claude_code, codex

    return {"claude_code": claude_code, "codex": codex}


def _quoted(text: str) -> str:
    """The agent's own words set in «…»: no closing mark or line break of its own can end the quote early (as
    ``ruling_line(quote=True)`` does; the store refuses the separators too, this is the second guard)."""
    return "«" + text.translate({ord(c): None for c in "«»\r\n\u0085\u2028\u2029"}) + "»"


def answer_line(question: Question) -> str | None:
    """The fixed text one answered question is delivered as, or ``None`` when it has no answer. It says an answer
    was RECORDED and by which channel, never that a person gave it: the answer is unsigned. An option answer names
    the key and the option's label; a free-text answer is JSON-escaped, every control and separator character too
    (:func:`_escaped`), so it arrives verbatim and nothing in it can pass for the line's own text or start a line
    of its own."""
    a = question.answer
    if a is None:
        return None
    head = f"levain: an answer was recorded for question #{question.n} {_quoted(question.what)} (via {a.channel})"
    if a.kind == "option":
        label = dict(question.options).get(a.value, "")
        return f"{head}: option {a.value} {_quoted(label)}."
    return f"{head}, in words: {_escaped(a.value)}"


def _escaped(text: str) -> str:
    """``text`` as a JSON string with every control, format and line or paragraph separator character escaped too
    (``json.dumps`` escapes only U+0000-U+001F): NEL (U+0085), U+2028 and a bidi override all arrive as ``\\uXXXX``,
    a character past U+FFFF as its UTF-16 surrogate pair (``json.dumps`` of the one character), so it round-trips."""
    return "".join(c if not (unicodedata.category(c)[0] == "C" or unicodedata.category(c) in ("Zl", "Zp"))
                   else json.dumps(c)[1:-1] for c in json.dumps(text, ensure_ascii=False))


def question_context(directory: Path | str, agent: str, session: str | None) -> str | None:
    """The answers not yet delivered to this session, one line each and :data:`ANSWER_TAIL` last (claimed: each
    reaches the session once), or ``None``. Only a main-thread session key claims; a store that does not exist yet
    is read as no answers and is not created."""
    if not isinstance(session, str) or not session or _SUBAGENT in session or session == NO_SESSION:
        return None
    store = QuestionStore(directory)
    if not store.path.exists():
        return None
    lines = [line for line in (answer_line(q) for q in store.claim_answers_for(agent, session)) if line]
    return "\n".join([*lines, ANSWER_TAIL]) if lines else None


def _ancestors() -> Iterator[tuple[int, list[str]]]:
    """``levain ask``'s ancestor processes, nearest first, each with its command line split as a shell would
    (``ps -o args=``), at most :data:`_HOPS` of them; a process that cannot be read ends the walk."""
    import shlex
    import subprocess

    pid = os.getppid()
    for _ in range(_HOPS):
        if pid <= 1:
            return
        try:
            line = subprocess.run(["ps", "-ww", "-o", "ppid=", "-o", "args=", "-p", str(pid)], capture_output=True,
                                  text=True, timeout=2, stdin=subprocess.DEVNULL,
                                  env={"PATH": "/bin:/usr/bin", "LC_ALL": "C"}).stdout.strip()
            parent_text, _sep, args_text = line.partition(" ")
            parent = int(parent_text)
            try:
                args = shlex.split(args_text)
            except ValueError:
                args = args_text.split()
        except (OSError, subprocess.SubprocessError, ValueError):
            return
        yield pid, args
        pid = parent


def _agent_of(args: list[str]) -> str | None:
    name = os.path.basename(args[0]) if args else ""
    return {"claude": "claude_code", "codex": "codex"}.get(name)


def _agent_and_session(agent: str | None, session: str | None) -> tuple[str, str]:
    """The agent and session the question names: the flags, else the agent's own environment. Naming only. With no
    ``--agent``, the nearest ancestor process that is ``claude`` or ``codex`` decides (a Codex run from a Claude Code
    shell inherits Claude Code's variables), else the environment."""
    env = os.environ
    if agent is None:
        agent = next((a for _pid, args in _ancestors() if (a := _agent_of(args)) is not None), None)
    if agent is None:
        if env.get("CODEX_THREAD_ID"):
            agent = "codex"
        elif env.get("CLAUDECODE") == "1" or env.get("CLAUDE_CODE_SESSION_ID"):
            agent = "claude_code"
        elif env.get("CODEX_SANDBOX"):
            agent = "codex"
        else:
            raise ValueError("cannot tell which agent is asking; pass --agent claude_code or --agent codex")
    if agent not in _agents():
        raise ValueError(f"--agent must be one of {sorted(_agents())}")
    if session is None:
        session = env.get("CLAUDE_CODE_SESSION_ID") if agent == "claude_code" else env.get("CODEX_THREAD_ID")
    return agent, session or NO_SESSION


def _options(raw: list[str]) -> list[tuple[str, str]]:
    out = []
    for item in raw:
        key, sep, label = item.partition("=")
        if not sep:
            raise ValueError(f"--option takes KEY=LABEL: {item!r}")
        out.append((key.strip(), label.strip()))
    return out


def _queue_dir(args: argparse.Namespace) -> Path:
    from levain.consent.queue import default_queue_dir

    return args.queue_dir.expanduser().resolve() if args.queue_dir is not None else default_queue_dir()


def _ping_text(agent: str, q: Question, qdir: Path) -> str:
    """The ping: its first line says who asks what in plain words (a lock screen shows only that). The answer
    command names the store's directory unless it is the default one, so it answers this question, not another
    store's question of the same number."""
    import shlex

    from levain.consent.queue import default_queue_dir

    opts = "; ".join(f"{k}) {label}{' (rec)' if k == q.rec else ''}" for k, label in q.options)
    where = "" if Path(qdir) == default_queue_dir() else f" --queue-dir {shlex.quote(str(qdir))}"
    return (f"levain: {agent} asks (question #{q.n}): {q.what}\nOptions: {opts}.\nBlocks: {q.blocks}.\n"
            f"Answer: levain question answer {q.n}{where} --option <key> (or --text \"...\").")


def file_question(agent: str, session: str, what: str, options: list[tuple[str, str]], rec: str, blocks: str,
                  qdir: Path, *, no_session_hint: str = "(pass --session)",
                  session_source: str = "--session", own_session: bool = False) -> tuple[Question | None, str]:
    """File one question, ping, and say so: the one path under ``levain ask`` and the ``ask`` MCP tool, so the
    refusals, the ping, the resume meta and the wording are the same for both. Returns the question (``None`` when
    it was refused or could not be written) and the text the caller shows the agent; ``no_session_hint`` is what
    that text says to do when the question names no session, and ``session_source`` what a refusal names as the
    session's source (the MCP door has no ``--session`` flag). ``own_session`` says the session is the caller's own
    though its environment does not name it (the MCP door's per-call ``_meta``, which the agent's client writes and
    the model cannot), so the resume meta is recorded for it."""
    try:
        if _SUBAGENT in session:
            raise ValueError(f"{session_source} must name the main session (no #agent: part): only its hooks "
                             "receive answers")
        try:
            resume = ask_resume_meta(agent, session, own=session if own_session else None)
        except Exception:  # noqa: BLE001 - no resume record: the answer then waits for the session's next hook
            resume = None
        q = QuestionStore(qdir).file(agent=agent, agent_session_id=session, what=what, options=options, rec=rec,
                                     blocks=blocks, cwd=os.path.abspath(os.getcwd()), resume=resume)
    except (ValueError, ConsentError, QuestionStoreError, OSError) as exc:
        return None, f"levain ask: refused, nothing was filed: {exc}"
    except sqlite3.Error as exc:                     # a store locked past its wait, or damaged: the write rolled back
        return None, f"levain ask: the question store could not be written, so nothing was filed ({exc}); try again"
    adapter = _agents()[agent]
    send = getattr(adapter, "notify", _agents()["claude_code"].notify)   # Codex shares Claude Code's notify seam
    try:
        pinged = send(adapter.load_settings(qdir), _ping_text(agent, q, qdir), cwd=qdir)
    except Exception:  # noqa: BLE001 - a ping is a ping: it never fails the question
        pinged = False
    where = ("the answer arrives in your context at a later tool call" if session != NO_SESSION else
             "no session was found in your environment, so the answer will be recorded but not delivered to you "
             + no_session_hint)
    ping = ("pinged" if pinged else
            "NOT pinged (no notify command is configured; the question is listed in `levain question list`)")
    return q, (f"levain ask: question #{q.n} filed ({q.id}); the operator was {ping}. Keep working on what it does "
               f"not block; do not do what it blocks ({q.blocks}) until the answer comes: {where}.")


def run_ask(args: argparse.Namespace, out: Any = None) -> int:
    """``levain ask``: file the question, ping, return at once."""
    out = out if out is not None else sys.stdout
    try:
        agent, session = _agent_and_session(args.agent, args.session)
        qdir = _queue_dir(args)
        options = _options(args.option)
    except (ValueError, ConsentError, QuestionStoreError, OSError) as exc:
        print(f"levain ask: refused, nothing was filed: {exc}", file=out, flush=True)
        return EXIT_REFUSED
    q, text = file_question(agent, session, args.what, options, args.rec, args.blocks, qdir)
    print(text, file=out, flush=True)
    return EXIT_OK if q is not None else EXIT_REFUSED


def _row(q: Question) -> dict[str, Any]:
    return {"n": q.n, "id": q.id, "agent": q.agent, "session": q.agent_session_id, "what": q.what,
            "options": [{"key": k, "label": label} for k, label in q.options], "rec": q.rec, "blocks": q.blocks,
            "cwd": q.cwd, "filed_at": q.filed_at,
            "answer": None if q.answer is None else {"kind": q.answer.kind, "value": q.answer.value,
                                                     "channel": q.answer.channel, "at": q.answer.answered_at}}


def _show(q: Question) -> str:
    opts = "\n".join(f"    {k}) {label}{'  <- rec' if k == q.rec else ''}" for k, label in q.options)
    state = ("open" if q.answer is None else
             f"answered {q.answer.answered_at} by {q.answer.channel}: "
             + (f"option {q.answer.value}" if q.answer.kind == "option" else json.dumps(q.answer.value,
                                                                                          ensure_ascii=False)))
    return f"#{q.n} [{q.agent}] {q.what}\n{opts}\n    blocks: {q.blocks}\n    {state}"


def run_question(args: argparse.Namespace, out: Any = None) -> int:
    """``levain question list|show|answer``."""
    out = out if out is not None else sys.stdout

    def say(text: str) -> None:
        print(text, file=out, flush=True)

    try:
        store = QuestionStore(_queue_dir(args))
        if args.question_cmd == "deliver":
            got = deliver_answers(store)
            for d in got:
                say(f"levain question: {d.request_id}: {d.route}: {d.reason}")
            if not got:
                say("levain question: no answer is waiting to be delivered.")
            return EXIT_OK
        if args.question_cmd == "list":
            rows = store.recent(limit=args.limit) if args.all else store.open_questions()
            if args.json:
                say(json.dumps([_row(q) for q in rows], ensure_ascii=False, indent=2))
            elif not rows:
                say("levain question: no open questions." if not args.all else "levain question: no questions.")
            else:
                say("\n".join(_show(q) for q in rows))
            return EXIT_OK
        q = store.get(args.ref)
        if q is None:
            raise ValueError(f"no question {args.ref!r}")
        if args.question_cmd == "show":
            say(json.dumps(_row(q), ensure_ascii=False, indent=2) if args.json else _show(q))
            return EXIT_OK
        if (args.option is None) == (args.text is None):
            raise ValueError("answer with exactly one of --option KEY or --text TEXT")
        store.answer(q.id, option=args.option, text=args.text, channel="cli")
        say(f"levain question: #{q.n} answered. The answer authorises nothing.")
        if args.deliver:
            try:
                d = deliver_answer(store, q.id)
            except Exception as exc:  # noqa: BLE001 - the answer is recorded whatever its delivery does
                say(f"levain question: the answer is recorded; its delivery raised {type(exc).__name__}: {exc} "
                    "(`levain question deliver` retries it).")
            else:
                say(f"levain question: delivery {d.route}: {d.reason}.")
        return EXIT_OK
    except (ValueError, ConsentError, QuestionStoreError, OSError, sqlite3.Error) as exc:
        say(f"levain question: refused: {exc}")
        return EXIT_REFUSED


def add_parsers(subparsers: argparse._SubParsersAction) -> None:
    """``levain ask`` and ``levain question``, under the top-level subparsers."""
    qdir_help = "The consent queue's directory (default: $LEVAIN_HOME/consent, else ~/.levain/consent)."
    a = subparsers.add_parser(
        "ask", help="Ask the operator a question and keep working (the question band).",
        description="File a question for the operator: what you need, 2-6 options (KEY=LABEL), the one you "
                    "recommend and what it blocks. The operator is pinged; this returns at once. The answer "
                    "arrives in the asking session's context at a later tool call, once. An answer authorises "
                    "nothing: a gated call still goes through levain's gate. Exit 0 filed, 2 refused.")
    a.add_argument("--what", required=True, help="The question, one line.")
    a.add_argument("--option", action="append", default=[], metavar="KEY=LABEL",
                   help="An option (repeat, 2-6). KEY: a-z, 0-9, _ or -, up to 16 characters.")
    a.add_argument("--rec", required=True, help="The KEY of the option you recommend.")
    a.add_argument("--blocks", required=True, help="What waits on the answer, one line.")
    a.add_argument("--agent", default=None, choices=_AGENT_NAMES,
                   help="Which agent asks (default: read from the agent's environment).")
    a.add_argument("--session", default=None,
                   help="The agent session the answer goes to (default: from the agent's environment).")
    a.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None, help=qdir_help)
    a.set_defaults(func=run_ask)

    q = subparsers.add_parser("question", help="List, show or answer the agents' questions (the question band).")
    q_sub = q.add_subparsers(dest="question_cmd", required=True)
    ls = q_sub.add_parser("list", help="The open questions (with --all, the recent ones, answered or not).")
    ls.add_argument("--all", action="store_true", help="Recent questions, answered or not, newest first.")
    ls.add_argument("--limit", type=int, default=50, help="With --all, at most this many (default 50).")
    ls.add_argument("--json", action="store_true", help="Machine-readable output.")
    sh = q_sub.add_parser("show", help="One question.")
    sh.add_argument("ref", help="The question's number (7 or #7) or id (q-...).")
    sh.add_argument("--json", action="store_true", help="Machine-readable output.")
    an = q_sub.add_parser("answer", help="Answer a question once, with an option's key or your own words.",
                          description="Record the answer to a question; the asking session gets it at its next "
                                      "tool call. A question is answered once. The answer authorises nothing.")
    an.add_argument("ref", help="The question's number (7 or #7) or id (q-...).")
    an.add_argument("--option", default=None, help="The KEY of the option you choose.")
    an.add_argument("--text", default=None, help="Your answer in your own words, delivered verbatim.")
    an.add_argument("--no-deliver", dest="deliver", action="store_false",
                    help="Record the answer only; its session gets it at its next tool call (or `levain question "
                         "deliver`).")
    dl = q_sub.add_parser("deliver", help="Deliver every recorded answer whose text has not reached its session: an "
                                          "ended session is resumed with it, any other gets it at its next tool call.")
    for p in (ls, sh, an, dl):
        p.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None, help=qdir_help)
        p.set_defaults(func=run_question)


# --- the answer's way back to an ENDED session (Q-1b) -------------------------------------------------------------
# how much of a Claude Code transcript is read for its start directory (the head) and its latest permission mode
# (the tail), and the longest first line of a Codex rollout read: both facts sit at a file's ends, and a first
# record carries the session's opening prompt or instructions (AGENTS.md included), which can be long
_TRANSCRIPT_HEAD = 1024 * 1024
_TRANSCRIPT_TAIL = 1024 * 1024
# how many ancestors of `levain ask` are walked to find the agent's process (the agent -> a sandbox wrapper -> a shell)
_HOPS = 8
# what follows the answer lines in a resume's prompt: «…» there is the agent's own question
QUOTED_NOTE = "(Each «…» above is your own question as you filed it, not the operator's words.)"


def _records(chunk: bytes) -> Iterator[dict[str, Any]]:
    for line in chunk.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue                                     # a line cut at the chunk's edge, or not JSON
        if isinstance(rec, dict):
            yield rec


def transcript_facts(path: Path) -> tuple[str | None, str | None]:
    """The session's start directory (the ``cwd`` of the first record that has one) and its latest permission mode
    (the ``permissionMode`` of the last record that has one) from a Claude Code transcript, reading only its head and tail; ``None`` for either it does
    not show. Claude Code 2.1.303 writes both on its records [measured: 1018+50 levain-seat, 2026-10-18, on its own
    transcript]."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            head = fh.read(_TRANSCRIPT_HEAD)
            fh.seek(max(size - _TRANSCRIPT_TAIL, 0))
            tail = fh.read()
    except OSError:
        return None, None
    cwd = next((r["cwd"] for r in _records(head) if isinstance(r.get("cwd"), str)), None)
    mode = next((r["permissionMode"] for r in reversed(list(_records(tail)))
                 if isinstance(r.get("permissionMode"), str)), None)
    return cwd, mode


def _agent_ancestor(agent: str) -> tuple[int, list[str]] | None:
    """The nearest ancestor process that is the agent itself (``claude`` / ``codex``), with its command line."""
    return next(((pid, args) for pid, args in _ancestors() if _agent_of(args) == agent), None)


def codex_rollout_cwd(chome: Path, thread: str) -> str | None:
    """The start directory Codex recorded for ``thread``: the ``cwd`` of the ``session_meta`` record that opens its
    one rollout file (``<CODEX_HOME>/sessions/YYYY/MM/DD/rollout-*-<thread>.jsonl``, that record's ``id`` the
    thread) [measured: Codex 0.162.1, 1018+50 levain-seat, 2026-10-18], else ``None``."""
    found = list((chome / "sessions").glob(f"*/*/*/rollout-*-{thread}.jsonl"))
    if len(found) != 1:
        return None
    try:
        with open(found[0], "rb") as fh:
            first = json.loads(fh.readline(_TRANSCRIPT_HEAD))
    except (OSError, ValueError):
        return None
    meta = first.get("payload") if isinstance(first, dict) and first.get("type") == "session_meta" else None
    if not isinstance(meta, dict) or meta.get("id") != thread or not isinstance(meta.get("cwd"), str):
        return None
    return meta["cwd"]


def claude_transcript(config_dir: str | None, session: str) -> Path | None:
    """The session's one transcript under its config directory, or ``None`` (none, or more than one)."""
    base = Path(config_dir) if config_dir else Path.home() / ".claude"
    found = sorted((base / "projects").glob(f"*/{session}.jsonl"))
    return found[0] if len(found) == 1 else None


def _claude_facts(config_dir: str | None, session: str) -> tuple[str | None, str | None]:
    """(start directory, latest permission mode) from the session's transcript; the directory only when it names
    the directory the transcript is kept under."""
    path = claude_transcript(config_dir, session)
    if path is None:
        return None, None
    cwd, mode = transcript_facts(path)
    if cwd is not None and re.sub(r"[^A-Za-z0-9]", "-", cwd) != path.parent.name:
        cwd = None
    return cwd, mode


def ask_resume_meta(agent: str, session: str, *, own: str | None = None) -> dict[str, Any] | None:
    """What a later resume of the asking session needs, read where ``levain ask`` runs (a child of the agent's shell),
    or ``None`` when its session id is not a UUID. Unsigned, never authority: :func:`levain.consent.delivery.route`
    refuses a resume on anything it cannot trust, and the answer then waits for the session's next hook.

    Claude Code gives its shell ``CLAUDE_CODE_SESSION_ID``, ``CLAUDE_PID`` and ``CLAUDE_CODE_ENTRYPOINT`` but not the
    project directory or the permission mode [measured: 1018+50, Claude Code 2.1.303]; those two come from the
    session's one transcript (:func:`transcript_facts`), and the start directory only when it names the directory
    the transcript is kept under; the session's process is the nearest ``claude`` ancestor of this command. Codex: the nearest ``codex`` ancestor (none for an app-server or MCP host, which outlives its threads),
    the start directory from the thread's rollout (:func:`codex_rollout_cwd`) and ``CODEX_HOME``. Nothing is
    recorded for a session other than the caller's own (``--session`` naming another): the environment's session,
    or ``own`` when the caller knows its session another way (the MCP door's per-call ``_meta``)."""
    from levain.consent.delivery import option_names, process_identity, session_ok

    if not session_ok(session) or _SUBAGENT in session:
        return None
    env = os.environ
    if own is None:
        own = env.get("CLAUDE_CODE_SESSION_ID") if agent == "claude_code" else env.get("CODEX_THREAD_ID")
    if session != own:
        return None                       # a session named by --session is not this process's: nothing to trust
    found = _agent_ancestor(agent)
    if agent == "codex":
        chome = env.get("CODEX_HOME", "")
        config_dir = chome if os.path.isabs(chome) else None
        # the subcommand is read as any argument naming one (a top-level option's value can stand before it)
        words = set(found[1][1:]) if found else set()
        return {"invocation": "-p" if "exec" in words else "interactive",
                "codex_pid": found[0] if found and not words & {"app-server", "mcp-server"} else None,
                "cwd": codex_rollout_cwd(Path(config_dir) if config_dir else Path.home() / ".codex", session),
                "config_dir": config_dir,
                # the options a resume refuses on (delivery.codex_unrestored); never their values
                "codex_flags": option_names(" ".join(found[1])) if found else None}
    config = env.get("CLAUDE_CONFIG_DIR", "")
    config_dir = config if os.path.isabs(config) else None
    cwd, mode = _claude_facts(config_dir, session)
    # the session's process is the claude this command runs under, its nearest claude ancestor; never CLAUDE_PID,
    # which a nested claude inherits from the one that started it [measured: 1018+50, Claude Code 2.1.303]
    pid = found[0] if found is not None else None
    seen = process_identity(pid) if pid is not None else None
    return {"invocation": "-p" if env.get("CLAUDE_CODE_ENTRYPOINT", "").startswith("sdk") else "interactive",
            "permission_mode": mode, "cwd": cwd, "config_dir": config_dir, "claude_pid": pid,
            "claude_started": seen[0] if seen and seen[0] else None,
            "claude_flags": option_names(seen[1]) if seen and seen[1] else None}


def deliver_answer(store: QuestionStore, ref: Any, *, listing: Any = None, running: Any = None,
                   launch: Any = None) -> Any:
    """Get one answered question's text to its session (``levain question answer``, ``levain question deliver``):
    an ENDED session is resumed with the session's undelivered answers as its prompt (leased, then settled by the
    resume's outcome, as a ruling's: :func:`levain.consent.delivery.deliver`); any other is left to its next hook,
    the reason saying why. A Claude Code resume also needs the session's transcript to show, NOW, the start directory
    and permission mode ``levain ask`` recorded (:func:`_claude_facts`): a mode changed since the question was asked
    is never resumed into. Returns a :class:`levain.consent.delivery.Delivery` (``request_id`` = the question id).
    ``listing``, ``running`` and ``launch`` are the delivery module's seams."""
    from levain.consent import delivery as dv

    listing = listing if listing is not None else dv.claude_listing
    running = running if running is not None else dv.process_running
    launch = launch if launch is not None else dv._launch
    q = store.get(ref)
    if q is None:
        return dv.Delivery(str(ref), "", "", "none", "no question of that id")
    out = dv.Delivery(q.id, q.agent, q.agent_session_id, "hook", "")
    if q.answer is None:
        return replace(out, route="none", reason=f"question #{q.n} has no answer yet")
    if q.agent_session_id == NO_SESSION:
        return replace(out, route="none", reason="it was filed with no session, so it reaches no agent")
    state, when = store.delivery_state(q.id)
    if state == "delivered":
        return replace(out, route="delivered", reason=f"its text reached the session at {when}")
    if state == "unconfirmed":
        return replace(out, route="unconfirmed", reason=f"a resume was started for it at {when} and never confirmed: "
                                                        "it may have run, so it is not started again")
    with _session_lock(store, q.agent, q.agent_session_id) as held:
        if not held:
            return replace(out, reason=f"another delivery for its session held the lock for {SESSION_LOCK_WAIT:g} s, "
                                       "so this one started nothing; the answer lands at its next step")
        return _deliver_locked(store, q, out, listing=listing, running=running, launch=launch)


SESSION_LOCK_WAIT = 15.0       # seconds a delivery waits on another delivery of the same session before routing hook


@contextmanager
def _session_lock(store: QuestionStore, agent: str, session: str) -> Iterator[bool]:
    """One delivery per session at a time, from the route through the resume's settle (an exclusive ``flock`` on
    ``<store>/resumes/<digest of agent and session>.lock``): two answers delivered at once (the phone page delivers
    each on its own thread) would otherwise both pass the route's checks and start two resumes; the second now
    routes after the first resume has started, whose process names the session. Yields False, never proceeding
    unlocked, when the lock stays held past :data:`SESSION_LOCK_WAIT` (a hung or suspended holder): the caller then
    starts nothing and the answer stays deliverable."""
    import fcntl
    import hashlib
    import time

    folder = Path(store.directory) / "resumes"
    folder.mkdir(mode=0o700, exist_ok=True)
    name = hashlib.sha256(f"{agent}\0{session}".encode()).hexdigest()[:32]
    fd = os.open(folder / f"{name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + SESSION_LOCK_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.1)
        yield True
    finally:
        os.close(fd)


def _deliver_locked(store: QuestionStore, q: Question, out: Any, *, listing: Any, running: Any, launch: Any) -> Any:
    from levain.consent import delivery as dv

    state, when = store.delivery_state(q.id)
    if state in ("delivered", "unconfirmed"):                       # settled meanwhile, under another's lock
        return replace(out, route=state, reason=f"another delivery took it first ({state} at {when})")
    # a question asked with nothing recorded borrows the session's newest record: one resume carries every answer
    meta = store.resume_meta(q.id) or store.session_resume_meta(q.agent, q.agent_session_id) or {}
    where, why, argv, cwd = dv.route(q.agent, q.agent_session_id, meta, noun="answer", listing=listing,
                                     running=running)
    if where == "stop_resume":
        stopped = dv._stop(dv.base_session(q.agent_session_id), meta, listing)
        where, why, argv, cwd = (("hook", f"the background session was not stopped ({stopped}); the answer lands "
                                          "at its next step", (), None) if stopped is not None else
                                 dv.route(q.agent, q.agent_session_id, meta, noun="answer", listing=listing,
                                          running=running))
    out = replace(out, route=where, reason=why, argv=argv, cwd=cwd)
    if where == "resumed" and q.agent == "claude_code":
        now = _claude_facts(meta.get("config_dir") if isinstance(meta.get("config_dir"), str) else None,
                            dv.base_session(q.agent_session_id))
        if now != (meta.get("cwd"), meta.get("permission_mode")):
            return replace(out, route="hook", argv=(), cwd=None,
                           reason="the session's transcript no longer shows the start directory and permission mode "
                                  "recorded when it asked, so it is not resumed; the answer lands at its next step")
    if where != "resumed":
        return out
    if not os.path.isdir(cwd or ""):
        return replace(out, route="hook", argv=(), reason=f"the session's start directory {cwd} is gone; the answer "
                                                          "lands at its next step")
    leased = store.lease_answers_for(q.agent, q.agent_session_id)
    if not leased:
        state, when = store.delivery_state(q.id)
        if state in ("delivered", "unconfirmed"):
            return replace(out, route=state, argv=(), reason=f"another delivery took it first ({state} at {when})")
        return replace(out, route="hook", argv=(), reason="nothing could be leased for its session now; the answer "
                                                          "lands at its next step")
    ids = tuple(x.id for x in leased)
    full = list(argv)
    try:
        prompt = "\n".join([*(line for line in (answer_line(x) for x in leased) if line), ANSWER_TAIL, QUOTED_NOTE])
        full = [*argv, prompt]
        env = dv._env(q.agent, meta)
        log = Path(store.directory) / "resumes" / f"{ids[0]}.log"
    except Exception as exc:  # noqa: BLE001 - nothing was started: the lease settles failed, so it is retried
        failed: str | None = f"the resume could not be prepared ({type(exc).__name__}: {exc})"
    else:
        failed = launch(full, cwd or "", env, log)
    store.settle_resume(list(ids), agent_session_id=q.agent_session_id, argv=full, cwd=cwd or "",
                        outcome="started" if failed is None else "failed", detail=failed)
    if failed is None:
        return replace(out, argv=tuple(full), rulings=ids)
    return replace(out, route="failed", argv=tuple(full), rulings=ids,
                   reason=f"the resume did not start ({failed}); the answer stays deliverable: the session's next "
                          "step, or `levain question deliver`")


def deliver_answers(store: QuestionStore, **kw: Any) -> list[Any]:
    """:func:`deliver_answer` every answered question whose text is not known to have reached its session; one
    question's unexpected error is reported as its ``failed`` delivery and never stops the rest."""
    from levain.consent import delivery as dv

    out = []
    tried: set[tuple[str, str]] = set()                          # one resume attempt per session per run
    for q in store.undelivered():
        if store.delivery_state(q.id)[0] == "delivered":       # a resume above may have carried it
            continue
        if (q.agent, q.agent_session_id) in tried:
            continue
        try:
            got = deliver_answer(store, q.id, **kw)
        except Exception as exc:  # noqa: BLE001 - the next answer is still delivered
            got = dv.Delivery(q.id, q.agent, q.agent_session_id, "failed",
                              f"delivery raised {type(exc).__name__}: {exc}")
        if got.route in ("resumed", "failed"):
            tried.add((q.agent, q.agent_session_id))
        out.append(got)
    return out
