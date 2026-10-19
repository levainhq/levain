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
- a session that makes no further tool call gets the answer only when it is resumed (Q-1b) or makes one.
- a main thread waiting on a long subagent gets the answer after the subagent returns.
- with no session in the environment the question names :data:`NO_SESSION`, which no hook's session matches: the
  answer is recorded and listed, never delivered.

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from levain.cockpit.consent import ConsentError
from levain.consent.questions import Question, QuestionStore, QuestionStoreError

__all__ = ["AGENT_LINE", "ANSWER_TAIL", "NO_SESSION", "add_parsers", "answer_line", "question_context", "run_ask",
           "run_question"]

# the session a question names when the environment names none (no hook's session is ever this)
NO_SESSION = "levain-ask"
# what an agent is told so it asks instead of stopping (the line a template or brief carries)
AGENT_LINE = ("When you need the operator's judgement and can keep working on something else, run `levain ask "
              "--what \"<the question, one line>\" --option <key>=<label> --option <key>=<label> --rec <key> "
              "--blocks \"<what waits on the answer>\"` and carry on with what it does not block; the answer "
              "arrives in your context at a later step. Do not do what it blocks until then.")
# the line every delivered block of answers ends with (26a)
ANSWER_TAIL = "An answer authorises nothing: a gated call still goes through levain's gate."
EXIT_OK, EXIT_REFUSED = 0, 2
_SUBAGENT = "#agent:"


def _agents() -> dict[str, Any]:
    from levain.consent.adapters import claude_code, codex

    return {"claude_code": claude_code, "codex": codex}


def _quoted(text: str) -> str:
    """The agent's own words set in «…»: no closing mark or line break of its own can end the quote early (as
    ``ruling_line(quote=True)`` does)."""
    return "«" + text.translate({ord(c): None for c in "«»\r\n"}) + "»"


def answer_line(question: Question) -> str | None:
    """The fixed text one answered question is delivered as, or ``None`` when it has no answer. An option answer
    names the key and the option's label; a free-text answer is the operator's words JSON-escaped, so they arrive
    verbatim and nothing in them can pass for the line's own text."""
    a = question.answer
    if a is None:
        return None
    head = f"levain: the operator answered question #{question.n} {_quoted(question.what)} (by {a.channel})"
    if a.kind == "option":
        label = dict(question.options).get(a.value, "")
        return f"{head}: option {a.value} {_quoted(label)}."
    return f"{head} in their own words: {json.dumps(a.value, ensure_ascii=False)}"


def question_context(directory: Path | str, agent: str, session: str | None) -> str | None:
    """The answers not yet delivered to this session, one line each and :data:`ANSWER_TAIL` last (claimed: each
    reaches the session once), or ``None``. Only a main-thread session key claims; a store that does not exist yet
    is read as no answers and is not created."""
    if not isinstance(session, str) or not session or _SUBAGENT in session:
        return None
    store = QuestionStore(directory)
    if not store.path.exists():
        return None
    lines = [line for line in (answer_line(q) for q in store.claim_answers_for(agent, session)) if line]
    return "\n".join([*lines, ANSWER_TAIL]) if lines else None


def _agent_and_session(agent: str | None, session: str | None) -> tuple[str, str]:
    """The agent and session the question names: the flags, else the agent's own environment. Naming only."""
    env = os.environ
    if agent is None:
        if env.get("CLAUDECODE") == "1" or env.get("CLAUDE_CODE_SESSION_ID"):
            agent = "claude_code"
        elif env.get("CODEX_THREAD_ID") or env.get("CODEX_SANDBOX"):
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


def _ping_text(agent: str, q: Question) -> str:
    """The ping: its first line says who asks what in plain words (a lock screen shows only that)."""
    opts = "; ".join(f"{k}) {label}{' (rec)' if k == q.rec else ''}" for k, label in q.options)
    return (f"levain: {agent} asks (question #{q.n}): {q.what}\nOptions: {opts}.\nBlocks: {q.blocks}.\n"
            f"Answer: levain question answer {q.n} --option <key> (or --text \"...\").")


def run_ask(args: argparse.Namespace, out: Any = None) -> int:
    """``levain ask``: file the question, ping, return at once."""
    out = out if out is not None else sys.stdout

    def say(text: str) -> None:
        print(text, file=out, flush=True)

    try:
        agent, session = _agent_and_session(args.agent, args.session)
        qdir = _queue_dir(args)
        q = QuestionStore(qdir).file(agent=agent, agent_session_id=session, what=args.what,
                                     options=_options(args.option), rec=args.rec, blocks=args.blocks,
                                     cwd=os.path.abspath(os.getcwd()))
    except (ValueError, ConsentError, QuestionStoreError, OSError) as exc:
        say(f"levain ask: refused, nothing was filed: {exc}")
        return EXIT_REFUSED
    adapter = _agents()[agent]
    send = getattr(adapter, "notify", _agents()["claude_code"].notify)   # Codex shares Claude Code's notify seam
    try:
        pinged = send(adapter.load_settings(qdir), _ping_text(agent, q), cwd=qdir)
    except Exception:  # noqa: BLE001 - a ping is a ping: it never fails the question
        pinged = False
    where = ("the answer arrives in your context at a later tool call" if session != NO_SESSION else
             "no session was found in your environment, so the answer will be recorded but not delivered to you "
             "(pass --session)")
    say(f"levain ask: question #{q.n} filed ({q.id}); the operator was "
        f"{'pinged' if pinged else 'NOT pinged (no notify command is configured)'}. Keep working on what it does "
        f"not block; do not do what it blocks ({q.blocks}) until the answer comes: {where}.")
    return EXIT_OK


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
        say(f"levain question: #{q.n} answered; the asking session gets it at its next tool call. "
            "The answer authorises nothing.")
        return EXIT_OK
    except (ValueError, ConsentError, QuestionStoreError, OSError) as exc:
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
    a.add_argument("--agent", default=None, choices=sorted(_agents()),
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
    for p in (ls, sh, an):
        p.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None, help=qdir_help)
        p.set_defaults(func=run_question)
