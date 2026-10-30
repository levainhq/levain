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

The coordinator reads the band first (C5, :mod:`levain.coordinator.first_reader`). Its answer is a KNOW in its own
store, never an answer here: the hook delivers it labelled as the coordinator's (:func:`know_line`, :data:`KNOW_TAIL`)
while the question stays open, and ``levain question answer`` refuses a session Levain started as the coordinator.
While a coordinator holds a live lease, ``levain ask`` holds its ping (C5-1, Phill's *"4a"*): the operator is pinged
when the coordinator escalates the question or has not read it within 10 minutes (:func:`release_held_pings`, checked
by the gate hook, ``levain coord watch``, ``levain ask`` and ``levain coord escalate``; the first reader's known
edges say what that does not cover).

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
- a resume is never started while any running process's command line resumes the session id
  (:func:`levain.consent.delivery.session_named_running`: ``--resume <id>``, ``codex exec resume <id>``); one that
  only mentions it (``tail -f`` of its transcript) does not count. That check reads every process's command line and
  keeps nothing but the yes or no.
- the resume record is read from a transcript's first 1 MiB and a Codex rollout's first line up to 1 MiB: past that
  no start directory is recorded and the answer waits for the next hook; so does a Codex session whose prompt or an
  option value is exactly ``exec``, ``app-server`` or ``mcp-server`` (read as that subcommand).
- Codex's hook gives a session id only (no subagent id is appended, as for its halts): if a Codex subagent's calls
  carry the parent's id, its hook can claim the answer [not measured].
- ``--would-push``: a commit on the branch after the ask makes the push a different call, which asks again; the push
  must run from the session that asked (``--session`` on both when given); the request it files takes the session's
  next gap number though the agent was never refused, and its undecided receipt reads timed out (the receipt has
  no word for filed ahead; the request's note says why); asking the same push twice files two requests, and the one
  not ruled waits as an undecided gap; a question that fails to file after its push request did leaves that request
  undecided, and the refusal names it; a ruling made in the moment between levain reading the request's state and
  telling the agent leaves that text describing the earlier state (the push itself still takes only what was ruled); a push run before the operator taps holds and halts the session on its own
  request, and the later tap, a ruling on the question's request, does not release that halt; an answer of another
  option recorded while the tap is signing makes the tap's ruling apply with that other answer (the page says the
  answer was not recorded).

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from levain import unshowable
from levain.cockpit.consent import ConsentError
from levain.consent.questions import ANSWER_CHANNELS, Link, Question, QuestionStore, QuestionStoreError

__all__ = ["AGENT_LINE", "ANSWER_TAIL", "KNOW_TAIL", "NO_SESSION", "QUOTED_NOTE", "add_parsers", "answer_line", "ask_resume_meta",
           "deliver_answer", "deliver_answers", "file_question", "know_line", "question_context", "release_held_pings",
           "run_ask", "run_question", "transcript_facts"]

# the session a question names when the environment names none (no hook's session is ever this)
NO_SESSION = "levain-ask"
# what an agent is told so it asks instead of stopping. No template, hook or brief carries it today (DESIGN_QMCP
# amendment R-q23): it is the wording for a brief that wants it; the `ask` tool reaches the agent through the MCP
# server's own tools/list, not through this line.
AGENT_LINE = ("When you need the operator's judgement and can keep working on something else, run `levain ask "
              "--what \"<the question, one line>\" --option <key>=<label> --option <key>=<label> --rec <key> "
              "--blocks \"<what waits on the answer>\"` (2-6 options; keys lowercase a-z, 0-9, _ or -), or the "
              "levain_band `ask` tool with the same four fields (or, when the answer is not a choice you can list, "
              "`--open` with no options and no --rec: the operator answers in words), and carry on "
              "with what it does not block; the answer arrives in your context at a later tool call. Do not do what "
              "it blocks until then. If nothing else is unblocked, say so in your reply and end your turn: do not "
              "poll for the answer.")
# the line every delivered block of answers ends with (26a)
ANSWER_TAIL = ("An answer is information, not a permission: a gated call still goes through levain's gate, and "
               "levain has checked nothing about who recorded it.")
# after the coordinator's KNOW answers (C5, gap 7: "its answers are KNOWs labelled as its own")
KNOW_TAIL = ("Each coordinator answer above is its own reading, filed for the operator's review: the question stays "
             "open, and an answer the operator records later reaches you too and is the one that counts.")
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


def _hand_escaped(cp: int) -> str:
    """``\\uXXXX`` for one code point, a UTF-16 surrogate pair of them past U+FFFF: written by hand, because
    ``json.dumps`` leaves DEL (U+007F) raw even with ``ensure_ascii``."""
    if cp > 0xFFFF:
        cp -= 0x10000
        return f"\\u{0xD800 + (cp >> 10):04x}\\u{0xDC00 + (cp & 0x3FF):04x}"
    return f"\\u{cp:04x}"


def _escaped(text: str) -> str:
    """``text`` as a JSON string with every unshowable character (:mod:`levain.unshowable`; newline and tab
    included) written as ``\\uXXXX`` (:func:`_hand_escaped`): DEL, NEL (U+0085), U+2028 and a bidi override all
    arrive escaped, a character past U+FFFF as its surrogate pair, so ``json.loads`` gives ``text`` back (except a
    lone high surrogate directly before a lone low one: JSON reads that escaped pair as one character, and has no
    other spelling for it). Every other character is ``json.dumps``'s own spelling of it (a quote and a backslash
    escaped, the rest raw)."""
    # judged on ``text`` itself, never on the JSON: there a selector after a newline would follow the letter of ``\n``.
    # The runs between hidden characters hold no control and no lone surrogate (both are hidden), so json.dumps
    # escapes only a quote or a backslash in them.
    parts: list[str] = []
    last = 0
    for i in unshowable.hidden(text):
        parts.append(json.dumps(text[last:i], ensure_ascii=False)[1:-1])
        parts.append(_hand_escaped(ord(text[i])))
        last = i + 1
    parts.append(json.dumps(text[last:], ensure_ascii=False)[1:-1])
    return '"' + "".join(parts) + '"'


def question_context(directory: Path | str, agent: str, session: str | None) -> str | None:
    """The answers not yet delivered to this session, one line each and :data:`ANSWER_TAIL` last (claimed: each
    reaches the session once), or ``None``. Only a main-thread session key claims; a store that does not exist yet
    is read as no answers and is not created."""
    _release_quietly(directory)          # every hook checks the held pings (C5-1), whoever's session it is
    if not isinstance(session, str) or not session or _SUBAGENT in session or session == NO_SESSION:
        return None
    store = QuestionStore(directory)
    if not store.path.exists():
        return None
    lines = [line for line in (answer_line(q) for q in store.claim_answers_for(agent, session)) if line]
    knows = _coordinator_lines(directory, agent, session)
    if knows:
        lines += [*knows, KNOW_TAIL]
    return "\n".join([*lines, ANSWER_TAIL]) if lines else None


def know_line(question: Question, body: dict[str, Any]) -> str:
    """The fixed text one coordinator KNOW is delivered as (C5): it names the COORDINATOR as the one who answered, on
    its own reading, never the operator, and the value arrives quoted or escaped exactly as an answer's does."""
    head = (f"levain: the coordinator, not the operator, answered question #{question.n} {_quoted(question.what)} "
            "on its own reading")
    if body.get("kind") == "option":
        key = str(body.get("value", ""))
        return f"{head}: option {key} {_quoted(dict(question.options).get(key, ''))}."
    return f"{head}, in its own words: {_escaped(str(body.get('value', '')))}"


def _coordinator_caller(directory: Path) -> bool:
    """True when this process runs inside a session Levain started as the coordinator (its harness session id, read
    from the environment, is a coordinator launch's). A label check, like every actor before S1: it keeps the
    coordinator's pick from being recorded as the operator's answer (C5's run: ``via cli``, and the operator locked
    out), it does not stop a process that hides its environment."""
    from levain.coordinator.first_reader import is_coordinator_session

    env = os.environ
    return any(is_coordinator_session(directory, env.get(name)) for name in ("CLAUDE_CODE_SESSION_ID",
                                                                             "CODEX_THREAD_ID"))


def agent_process() -> str | None:
    """The agent this process runs under (``claude_code`` / ``codex``), or ``None`` for none: the ONE detector, used
    by :func:`agent_session_caller` (the Q1 fence) and by :func:`_agent_and_session` (``levain ask``'s naming), so
    the fence refuses every process the naming would call an agent. The nearest ancestor that is ``claude`` or
    ``codex`` decides (a Codex run from a Claude Code shell inherits Claude Code's variables); with none found
    (no such ancestor, or a process tree that cannot be read, as in a sandbox), the environment: ``CODEX_THREAD_ID``,
    then ``CLAUDECODE=1`` / ``CLAUDE_CODE_SESSION_ID``, then ``CODEX_SANDBOX``."""
    agent = next((a for _pid, args in _ancestors() if (a := _agent_of(args)) is not None), None)
    if agent is not None:
        return agent
    env = os.environ
    if env.get("CODEX_THREAD_ID"):
        return "codex"
    if env.get("CLAUDECODE") == "1" or env.get("CLAUDE_CODE_SESSION_ID"):
        return "claude_code"
    if env.get("CODEX_SANDBOX"):
        return "codex"
    return None


def agent_session_caller() -> bool:
    """True when this process runs inside ANY agent session (a Claude Code or Codex Bash tool, a child of one with
    no marker variable, ...): :func:`agent_process` found an agent. Q17-ALL D9, Q1 ruled (a): an information answer
    is the operator's only through a human channel, so `levain question answer` and `levain band answer` refuse
    here; inside a seat the human answers by typing in the seat. A label check, like :func:`_coordinator_caller`
    (which it widens from the coordinator to every agent): it does not stop a process that hides its environment
    and its process tree, and what it keeps out is text the agent reads, never authority.

    It over-fences on purpose (fail-closed): a human's shell that inherited an agent's variables (a tmux server
    started from inside Claude Code keeps ``CLAUDECODE=1``) or that runs under an agent process is refused, with no
    override. Answer from a fresh terminal, the cockpit or the phone, or type the answer in the seat."""
    return agent_process() is not None


AGENT_ANSWER_REFUSAL = ("this runs inside an agent session, and an agent's answer is never the operator's: type the "
                        "answer in the seat itself, or answer from a terminal outside any agent session, the cockpit "
                        "or the phone (the item stays open)")


def _coordinator_lines(directory: Path | str, agent: str, session: str) -> list[str]:
    """The coordinator's KNOW answers for this session (claimed once), or none. Never raises: a store that cannot be
    read now is read at the session's next hook."""
    try:
        from levain.coordinator.first_reader import claim_knows

        # each line is formatted inside the claim, before its KNOW is marked delivered (never marked unseen)
        return claim_knows(directory, agent, session, know_line)
    except Exception:  # noqa: BLE001 - the hook carries the operator's answers whatever the coordinator's store does
        return []


def _send_ping(agent: str, q: Question, qdir: Path, extra: str = "", kind: str = "question") -> bool:
    """Ping the operator about question ``q`` through the agent's notify seam; False when nothing was sent. ``kind`` is
    ``question`` for a seat's item and ``attention`` for a row the coordinator raises (it passes presence ``quiet``).
    Never raises: a ping is a ping."""
    try:
        adapter = _agents()[agent]
        send = getattr(adapter, "notify", _agents()["claude_code"].notify)   # Codex shares Claude Code's notify seam
        # The ping is the first time the operator sees the item, so it numbers it (Q17-ALL D3; S5a decision B (a)):
        # a bounded write, and a busy store or a failed write sends the old `#n` text (None).
        from levain import band_view
        from levain.consent.questions import QuestionStore

        n = band_view.number_for_ping(QuestionStore(qdir), q.id)
        return bool(send(adapter.load_settings(qdir), _ping_text(agent, q, qdir, n, extra), cwd=qdir,
                         kind=kind, ref=f"question:{q.n}"))
    except Exception:  # noqa: BLE001 - see the docstring
        return False


def release_held_pings(directory: Path | str) -> list[str]:
    """Send every held ping now due (C5-1, :func:`levain.coordinator.first_reader.claim_held`), each once; one line per
    release. Never raises: a store that cannot be read now is checked at the next call."""
    try:
        from levain.coordinator.first_reader import UNREAD_AFTER, claim_held

        due = claim_held(directory)
    except Exception:  # noqa: BLE001 - see the docstring
        return []
    minutes = int(UNREAD_AFTER.total_seconds() // 60)
    lines = []
    for q, cause in due:
        extra = ("\nThe coordinator escalated it to you." if cause == "escalated" else
                 f"\nThe coordinator has not read it in {minutes} minutes; it waits on you.")
        # an escalation is the coordinator putting the row in front of him (attention); an unread release is not
        pinged = _send_ping(q.agent, q, Path(directory), extra, "attention" if cause == "escalated" else "question")
        lines.append(f"question #{q.n} ({cause}): the operator was {'pinged' if pinged else 'NOT pinged'}")
    return lines


def _release_quietly(directory: Path | str) -> None:
    try:
        release_held_pings(directory)
    except Exception:  # noqa: BLE001 - the hook carries the answers whatever the held pings do
        pass


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
    ``--agent``, :func:`agent_process` (the same detector the Q1 fence uses) decides."""
    env = os.environ
    if agent is None:
        agent = agent_process()
    if agent is None:
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


_FULL_TEXT = "… (full text on the card)"


def _ping_fits() -> Callable[[str], bool]:
    """Whether a ping's whole text reaches the phone uncut, measured the way the push layer measures it: the
    serialized UTF-8 bytes :func:`levain.webpush.sender.build_payload` fits the body into (with the title, tag and
    url ``claude_code.notify`` sends and the widest tag and ping id), and the ASCII-escaped JSON
    :func:`levain.webpush.sender.spawn` writes to its child, which it halves past its own limit."""
    from levain.webpush import ece, sender

    envelope = {"title": "levain", "body": "", "tag": "question:" + "9" * 12, "url": "/?view=needs", "ping": 10 ** 12}
    body_bytes = ece.MAX_PLAINTEXT - len(sender._dumps(envelope))
    stdin = {"title": "levain", "body": "", "ref": "question:" + "9" * 12, "url": "/?view=needs", "command": False,
             "push": True, "hold": "active"}
    stdin_room = sender._SPAWN_STDIN_MAX - len(json.dumps(stdin))

    def fits(text: str) -> bool:
        body = text[len("levain: "):] if text.startswith("levain: ") else text   # what notify sends as the body
        return (len(body) <= 4000 and sender._jlen(body) <= body_bytes
                and len(json.dumps(body)) - 2 <= stdin_room)
    return fits


def _cut(text: str, n: int) -> str:
    return text if len(text) <= n else text[:max(n - 1, 0)].rstrip() + "…"


def _longest(build: Callable[[int], str], top: int, fits: Callable[[str], bool]) -> int:
    """The largest n in 0..top with ``fits(build(n))``, else 0 (``build`` grows with n)."""
    lo, hi = 0, top
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(build(mid)):
            lo = mid
        else:
            hi = mid - 1
    return lo


def _ping_text(agent: str, q: Question, qdir: Path, band_n: int | None = None, extra: str = "") -> str:
    """The ping: its first line says who asks what in plain words (a lock screen shows only that), with the band
    number first when it has one (``band_n``; the source tag ``question #n`` stays). The answer command names the
    store's directory unless it is the default one, so it answers this question, not another store's question of
    the same number. ``extra`` ends it. The options and the answer line always reach the phone: ``what`` and the
    labels are shortened to one common length only when they would not fit with them, and Blocks goes last with
    only the room left (:func:`_ping_fits`). The store and every card keep the whole text."""
    import shlex

    from levain.consent.queue import default_queue_dir

    fits = _ping_fits()
    where = "" if Path(qdir) == default_queue_dir() else f" --queue-dir {shlex.quote(str(qdir))}"
    tag = f"band {band_n}, question #{q.n}" if band_n is not None else f"question #{q.n}"
    if q.open_ended:
        answer = ("An open question: answer in your own words.\n"
                  f"Answer: levain question answer {q.n}{where} --text \"...\".")
    else:
        answer = f"Answer: levain question answer {q.n}{where} --option <key> (or --text \"...\")."

    def core(n: int) -> str:
        head = f"levain: {agent} asks ({tag}): {_cut(q.what, n)}"
        if q.open_ended:
            return f"{head}\n{answer}"
        shown = "; ".join(f"{k}) {_cut(label, n)}{' (rec)' if k == q.rec else ''}" for k, label in q.options)
        return f"{head}\nOptions: {shown}.\n{answer}"

    longest = max([len(q.what), *(len(label) for _, label in q.options)])
    text = core(longest)
    if not fits(text + "\nBlocks:" + _FULL_TEXT + extra):
        text = core(_longest(lambda n: core(n) + "\nBlocks:" + _FULL_TEXT + extra, longest, fits))
    whole = f"{text}\nBlocks: {q.blocks}.{extra}"
    if fits(whole):
        return whole
    cut = _longest(lambda n: f"{text}\nBlocks: {q.blocks[:n].rstrip()}{_FULL_TEXT}{extra}", len(q.blocks), fits)
    if cut >= 40:
        return f"{text}\nBlocks: {q.blocks[:cut].rstrip()}{_FULL_TEXT}{extra}"
    return f"{text}\nBlocks:{_FULL_TEXT}{extra}"


def _coordinator_session(qdir: Path, session: str) -> bool | None:
    """True when ``session`` is a harness session Levain started as the coordinator, False when it is not, None when
    the lookup faulted (the caller neither grants the coordinator's `attention` nor holds the ping behind a first read)."""
    try:
        from levain.coordinator.first_reader import is_coordinator_session

        return is_coordinator_session(qdir, session)
    except Exception:  # noqa: BLE001 - a fault is its own answer: sent at once as a plain question
        return None


def _hold(qdir: Path, question_id: str) -> bool:
    try:
        from levain.coordinator.first_reader import hold_ping

        return hold_ping(qdir, question_id)
    except Exception:  # noqa: BLE001 - a fault pings, as before C5-1
        return False


class _PushFiled(ValueError):
    """A ``--would-push`` refusal raised AFTER the push's request was filed: the words name the request."""


def _file_push_request(agent: str, session: str, option: str, push: tuple[str, str, str], qdir: Path
                       ) -> tuple[Link, str, str, dict[str, str]]:
    """File the consent request for the push an option leads to (Q-3, ``--would-push``), undecided at once, and
    return its link with the push in the operator's words, its gap label and the call filed. The call is derived by
    :func:`levain.consent.push_gate.push_call`, the one derivation ``levain push`` itself runs, so the push run later
    from this session takes a ruling signed on this request while the branch tip is unchanged. Never a halt: nothing
    was refused to the agent. Raises ``ValueError`` (and what ``push_call`` raises) for a push that cannot be filed."""
    from datetime import timedelta

    from levain.consent import push_gate
    from levain.consent.queue import class_covers, gap_label

    if session == NO_SESSION:
        raise ValueError("--would-push needs the session the push will run from, and none was found: pass --session")
    call = push_gate.push_call(*push)
    settings = _agents()[agent].load_settings(qdir)
    if not push_gate._signable(settings):
        raise ValueError(f"no enrolled key can rule on the push (the queue's {_agents()[agent].SETTINGS_NAME} names "
                         "no cockpit with an active key)")
    queue = push_gate._queue(qdir, settings)
    snapshot = queue._fresh_authority()
    if snapshot is None or not any(class_covers(k.key_class, push_gate.PUSH_TIER) for k in snapshot.active_keys()):
        raise ValueError(f"no active key can allow a {push_gate.PUSH_TIER} push (the authority log holds none of a "
                         "class that covers it), so the option could never allow it")
    queue_id = queue.queue_id                    # read before filing: nothing after the filing may fail unnamed
    request = queue.file(agent=agent, agent_session_id=session, tool_name=push_gate.PUSH_TOOL, tool_input=call,
                         cwd=call["repo"], hold=timedelta(seconds=1), tier=push_gate.PUSH_TIER)
    try:
        marked = queue.mark_undecided(request.id, halt=False,
                                      note=f"filed undecided by levain ask: the push option {option} of a question "
                                           "leads to")
    except Exception as exc:  # noqa: BLE001 - the request exists now: say so, never "nothing was filed"
        raise _PushFiled(f"the push request {request.id} was filed but not made undecided ({type(exc).__name__}: "
                         f"{exc}); it stays pending until its hold expires and allows nothing") from exc
    if marked.state != "undecided" or marked.gap is None:
        raise _PushFiled(f"the push request {request.id} was filed but is {marked.state}, not undecided "
                         f"({marked.reason}); no question was tied to it")
    return (Link(option, "push", request.id, queue_id), push_gate.push_what(call),
            gap_label(marked.gap, session), call)


def _push_state(agent: str, qdir: Path, request_id: str) -> str:
    """The linked push request's state now, or ``unknown`` when the queue cannot say."""
    from levain.consent import push_gate

    try:
        got = push_gate._queue(qdir, _agents()[agent].load_settings(qdir)).get(request_id)
    except Exception:  # noqa: BLE001 - only the words to the agent depend on it
        return "unknown"
    return "missing" if got is None else got[1]


_OPEN_PUSH = ("--open files an open question, which has no option for --would-push to tie a push to: file a "
              "question with options to tie one, or ask in words without --would-push")


def file_question(agent: str, session: str, what: str, options: list[tuple[str, str]], rec: str, blocks: str,
                  qdir: Path, *, no_session_hint: str = "(pass --session)",
                  session_source: str = "--session", own_session: bool = False,
                  would_push: tuple[str, tuple[str, str, str]] | None = None, open_ended: bool = False
                  ) -> tuple[Question | None, str]:
    """File one question, ping, and say so: the one path under ``levain ask`` and the ``ask`` MCP tool, so the
    refusals, the ping, the resume meta and the wording are the same for both. Returns the question (``None`` when
    it was refused or could not be written) and the text the caller shows the agent; ``no_session_hint`` is what
    that text says to do when the question names no session, and ``session_source`` what a refusal names as the
    session's source (the MCP door has no ``--session`` flag). ``own_session`` says the session is the caller's own
    though its environment does not name it (the MCP door's per-call ``_meta``, which the agent's client writes and
    the model cannot), so the resume meta is recorded for it. ``would_push`` = (option key, (repo, remote, branch)):
    that option leads to that ``levain push``, whose consent request is filed undecided FIRST
    (:func:`_file_push_request`) and linked to the question in the question's own transaction; only a late ruling
    signed on that request ever allows the push, never the answer."""
    import subprocess

    from levain.authority import AuthorityError, VerifierUnavailable
    from levain.consent.queue import QueueStoreError

    link: Link | None = None
    try:
        if _SUBAGENT in session:
            raise ValueError(f"{session_source} must name the main session (no #agent: part): only its hooks "
                             "receive answers")
        if would_push is not None and open_ended:
            raise ValueError(_OPEN_PUSH)
        if would_push is not None and would_push[0] not in {k for k, _ in options}:
            raise ValueError(f"--would-push names {would_push[0]!r}, which is not one of the options")
        try:
            resume = ask_resume_meta(agent, session, own=session if own_session else None)
        except Exception:  # noqa: BLE001 - no resume record: the answer then waits for the session's next hook
            resume = None
        if would_push is not None:
            # validated here before the push request is filed, so a malformed question files nothing at all
            QuestionStore.check(agent=agent, agent_session_id=session, what=what, options=options, rec=rec,
                                blocks=blocks, cwd=os.path.abspath(os.getcwd()), open_ended=open_ended)
            link, push_words, gap, call = _file_push_request(agent, session, would_push[0], would_push[1], qdir)
    except (ValueError, ConsentError, QuestionStoreError, OSError, subprocess.SubprocessError, QueueStoreError,
            AuthorityError, VerifierUnavailable) as exc:
        if isinstance(exc, _PushFiled):
            return None, f"levain ask: refused, no question was filed: {exc}"
        return None, f"levain ask: refused, nothing was filed: {exc}"
    except sqlite3.Error as exc:                     # a store locked past its wait, or damaged: the write rolled back
        return None, f"levain ask: a store could not be written, so nothing was filed ({exc}); try again"
    try:
        q = QuestionStore(qdir).file(agent=agent, agent_session_id=session, what=what, options=options, rec=rec,
                                     blocks=blocks, cwd=os.path.abspath(os.getcwd()), resume=resume, link=link,
                                     open_ended=open_ended)
    except Exception as exc:  # noqa: BLE001 - whatever stopped it, a push filed before it is named, never hidden
        if link is None and not isinstance(exc, (ValueError, ConsentError, QuestionStoreError, OSError,
                                                  sqlite3.Error)):
            raise
        if link is None:
            if isinstance(exc, sqlite3.Error):       # a store locked past its wait, or damaged: rolled back
                return None, (f"levain ask: the question store could not be written, so nothing was filed ({exc}); "
                              "try again")
            return None, f"levain ask: refused, nothing was filed: {exc}"
        return None, (f"levain ask: the question was NOT filed ({exc}), but the consent request {link.request_id} "
                      f"(gap {gap}) for the {push_words} was filed undecided before it; it moves only on a ruling "
                      "the operator signs. Ask again without --would-push, or tell the operator.")
    # the linked push's state now, read before the ping: one ruled while the question waited on its store's lock
    # is never announced as a one-tap option
    state = None if link is None else _push_state(agent, qdir, link.request_id)
    tappable = link is not None and state in ("undecided", "unknown")
    _release_quietly(qdir)                       # earlier held pings now due go first (C5-1)
    # C5-1: while a coordinator holds a live lease it reads first and the ping is held; a question tied to a consent
    # request is the operator's ruling alone and is never held
    # a question the coordinator files about its own work is never held behind its own first read: it pings at once
    # as `attention`
    own = _coordinator_session(qdir, session)
    # an open question is never held either: the coordinator can only escalate it, so a hold would only delay the ping
    held = link is None and own is False and not q.open_ended and _hold(qdir, q.id)
    pinged = False
    if not held:
        extra = ""
        if state == "undecided":                 # the ping claims the tap only on a state actually read
            extra = (f"\nOption {link.option} is tied to the {push_words}: picking it on the phone page with a "
                     "passkey also allows that push (or rule it with `levain consent rule`).")
        pinged = _send_ping(agent, q, qdir, extra, "attention" if own and link is None else "question")
    where = ("the answer arrives in your context at a later tool call" if session != NO_SESSION else
             "no session was found in your environment, so the answer will be recorded but not delivered to you "
             + no_session_hint)
    ping = ("not pinged yet: a coordinator holds the live lease and reads it first; the operator is pinged if it "
            "escalates the question or has not read it within 10 minutes" if held else "pinged" if pinged else
            "NOT pinged (no notify command is configured and no phone has pings on; the question is listed in "
            "`levain question list`)")
    text = (f"levain ask: question #{q.n} filed ({q.id}); the operator was {ping}. Keep working on what it does "
            f"not block; do not do what it blocks ({q.blocks}) until the answer comes: {where}.")
    if link is not None:
        import shlex

        # the call as filed (the repository's real path, the canonical remote): the push names the same call
        # from whatever directory the agent runs it in
        command = (f"`levain push --repo {shlex.quote(call['repo'])} --remote {shlex.quote(call['remote'])} "
                   f"--branch {shlex.quote(call['branch'])}`")
        if not tappable:
            text += (f" Option {link.option} leads to the {push_words}: its consent request {link.request_id} is "
                     f"already {state}; the option's one tap no longer rules it. Run {command} from this same "
                     "session only if the operator's answer says to; it asks if nothing allows it.")
            return q, text
        text += (f" Option {link.option} leads to the {push_words}: levain filed it as consent request "
                 f"{link.request_id}, undecided (gap {gap}). While it is still undecided, if the operator picks that "
                 "option on the phone page with a passkey, that same tap signs a ruling that allows the push (with no passkey enrolled the "
                 f"operator rules it with `levain consent rule`); then, from this same session, run {command} and "
                 f"it runs once without asking again. A new commit on {call['branch']} before then is a different "
                 "push, which asks.")
    return q, text


# the coordinator's own item. Its key is this base, a colon and the stop episode (the death that began the run that
# stopped it), so one episode files one item: an item the operator acknowledged is not filed again while that
# episode stands, and a later stop is a new item. Closing takes the base, so every episode's open item closes.
# An answer here records only: the panel's Start, Stop and Hold are what act, so the one option is an acknowledgement
COORDINATOR_STOPPED_KEY = "levain-coordinator-stopped"
_STOPPED_OPTIONS = (("seen", "Seen: I will press Stop or Hold on the cockpit's Coordinator panel"),)


def file_coordinator_stopped(qdir: Path | str, agent: str, why: str, episode: str,
                             still: Callable[[], bool] | None = None) -> Question | None:
    """Put the coordinator's stop on the band: one item per stop episode, however many deaths follow the third and
    however often this runs, and none again for an episode the operator has answered. ``agent`` is the band's name
    for the coordinator's harness, ``why`` the panel's own text for the state, ``episode`` what names the stop.
    ``still``: asked once after the item is filed and before its ping; False (the coordinator left ``stopped`` while
    this filed) sends no ping. The item then stays open: only a person's answer closes an item (no answer channel is
    Levain's own), so the operator's acknowledgement or the panel's next verb closes it. The item is numbered by the
    band when it is first shown, as every item is. Its ping goes out once, with the item. Returns the item when this
    call filed it and pinged, ``None`` otherwise; never raises (the death that called it is already recorded)."""
    try:
        text = _plain_line(why)
        q, filed = QuestionStore(qdir).file_once(
            agent=agent, key=f"{COORDINATOR_STOPPED_KEY}:{episode}",
            what="The coordinator stopped after three deaths in a row; nothing launches until you act on the "
                 "Coordinator panel.",
            options=list(_STOPPED_OPTIONS), rec="seen",
            blocks=f"every new coordinator launch, until you press Stop or Hold on the cockpit's Coordinator "
                   f"panel (an answer here only records). {text}"[:300],
            cwd=os.path.abspath(str(qdir)), held_by=ANSWER_CHANNELS)
        if not filed:
            return None
        if still is not None and not still():
            return None
        _send_ping(agent, q, Path(qdir), kind="attention")
        return q
    except Exception:  # noqa: BLE001 - see the docstring
        return None


def _plain_line(text: str) -> str:
    return " ".join(str(text).split())[:600]


def close_coordinator_stopped(qdir: Path | str, verb: str) -> list[Question]:
    """Close every open stop item as the operator's own act from the cockpit, through the band's answer write, with
    text naming the panel ``verb`` he pressed (the item's one option is an acknowledgement, not a verb). Returns the
    items closed. Never raises: the verb that called it has already changed the coordinator."""
    try:
        return QuestionStore(qdir).close_open(
            COORDINATOR_STOPPED_KEY, text=f"{verb} pressed on the cockpit's Coordinator panel", channel="cockpit")
    except Exception:  # noqa: BLE001 - see the docstring
        return []


def run_ask(args: argparse.Namespace, out: Any = None) -> int:
    """``levain ask``: file the question, ping, return at once."""
    out = out if out is not None else sys.stdout
    try:
        agent, session = _agent_and_session(args.agent, args.session)
        qdir = _queue_dir(args)
        options = _options(args.option)
        if args.open:
            # the same rule as QuestionStore.check: an empty --rec is no rec
            if options or args.rec not in (None, ""):
                raise ValueError("--open files an open question: give no --option and no --rec (it is answered in "
                                 "words)")
            if args.would_push is not None:
                raise ValueError(_OPEN_PUSH)
        elif args.rec is None:
            raise ValueError("--rec KEY is required (the key of the option you recommend), or file an open question "
                             "with --open and no options")
        push = (args.repo, args.remote, args.branch)
        if args.would_push is None and any(v is not None for v in push):
            raise ValueError("--repo, --remote and --branch go with --would-push KEY")
        if args.would_push is not None and (args.remote is None or args.branch is None):
            raise ValueError("--would-push KEY needs --remote and --branch (and --repo, default the current directory)")
        would_push = None if args.would_push is None else (args.would_push, (args.repo or ".", args.remote,
                                                                              args.branch))
    except (ValueError, ConsentError, QuestionStoreError, OSError) as exc:
        print(f"levain ask: refused, nothing was filed: {exc}", file=out, flush=True)
        return EXIT_REFUSED
    q, text = file_question(agent, session, args.what, options, args.rec or "", args.blocks, qdir,
                            would_push=would_push, open_ended=args.open)
    print(text, file=out, flush=True)
    return EXIT_OK if q is not None else EXIT_REFUSED


def _row(q: Question) -> dict[str, Any]:
    return {"n": q.n, "id": q.id, "agent": q.agent, "session": q.agent_session_id, "what": q.what,
            "options": [{"key": k, "label": label} for k, label in q.options], "rec": q.rec, "blocks": q.blocks,
            "open_ended": q.open_ended, "cwd": q.cwd, "filed_at": q.filed_at,
            "answer": None if q.answer is None else {"kind": q.answer.kind, "value": q.answer.value,
                                                     "channel": q.answer.channel, "at": q.answer.answered_at},
            "link": None if q.link is None else {"option": q.link.option, "kind": q.link.kind,
                                                 "request_id": q.link.request_id}}


def _show(q: Question) -> str:
    linked = q.link.option if q.link is not None else None
    opts = "\n".join(f"    {k}) {label}{'  <- rec' if k == q.rec else ''}"
                     f"{f'  (leads to the push of consent request {q.link.request_id})' if k == linked else ''}"
                     for k, label in q.options)
    state = ("open" if q.answer is None else
             f"answered {q.answer.answered_at} by {q.answer.channel}: "
             + (f"option {q.answer.value}" if q.answer.kind == "option" else json.dumps(q.answer.value,
                                                                                          ensure_ascii=False)))
    if q.open_ended:
        opts = "    (an open question: answer it in your own words, `--text`)"
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
        if _coordinator_caller(store.directory):
            raise ValueError("this session is the coordinator: its answer is its own, never the operator's, so it "
                             "goes `levain coord answer` (a KNOW the operator reviews, the question left open) or "
                             "`levain coord escalate`")
        if agent_session_caller():
            raise ValueError(AGENT_ANSWER_REFUSAL)
        store.answer(q.id, option=args.option, text=args.text, channel="cli")
        say(f"levain question: #{q.n} answered. The answer authorises nothing.")
        if q.link is not None and args.option == q.link.option:
            say(f"levain question: option {q.link.option} leads to a push whose consent request {q.link.request_id} "
                "is still UNDECIDED: this answer did not allow it. To allow it, sign a ruling: `levain consent rule` "
                "lists it with its gap.")
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
                    "recommend and what it blocks (or --open: no options, answered in words). The operator is pinged; this returns at once. The answer "
                    "arrives in the asking session's context at a later tool call, once. An answer authorises "
                    "nothing: a gated call still goes through levain's gate. Exit 0 filed, 2 refused.")
    a.add_argument("--what", required=True, help="The question, one line.")
    a.add_argument("--option", action="append", default=[], metavar="KEY=LABEL",
                   help="An option (repeat, 2-6). KEY: a-z, 0-9, _ or -, up to 16 characters.")
    a.add_argument("--rec", default=None, help="The KEY of the option you recommend (required unless --open).")
    a.add_argument("--open", action="store_true",
                   help="File an OPEN question: no options and no --rec, answered only in the operator's own words. "
                        "Use it when the answer is not a choice among options you can list.")
    a.add_argument("--blocks", required=True, help="What waits on the answer, one line.")
    a.add_argument("--agent", default=None, choices=_AGENT_NAMES,
                   help="Which agent asks (default: read from the agent's environment).")
    a.add_argument("--session", default=None,
                   help="The agent session the answer goes to (default: from the agent's environment).")
    a.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None, help=qdir_help)
    a.add_argument("--would-push", dest="would_push", default=None, metavar="KEY",
                   help="Option KEY leads to `levain push --repo --remote --branch` (given below): levain files that "
                        "exact push as an undecided consent request tied to the option, so the operator's pick of "
                        "it on the phone page with a passkey also signs a ruling that allows that one push (or they "
                        "rule it with `levain consent rule`). The answer itself allows nothing. Refused unless an "
                        "active key can allow a push.")
    a.add_argument("--repo", default=None, help="With --would-push: the repository (default: the current directory).")
    a.add_argument("--remote", default=None, help="With --would-push: the remote URL, as `levain push` takes it.")
    a.add_argument("--branch", default=None, help="With --would-push: the branch, as `levain push` takes it.")
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
