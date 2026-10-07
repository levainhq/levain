"""Pure, duck-typed extraction of an agent's reply text from OpenHands events.

ONE source of truth for the two consumers that must never diverge on the SDK's event
shapes: ``capture.render_turn`` (the persisted memory episode) and ``run._latest_agent_text``
(the on-screen REPL reply). Duck-typed — this module imports NO ``openhands`` — so
``levain.run`` keeps its SDK-free test tier and a single edit here moves both consumers in
lockstep (the DRY the apparatus flagged: display and capture drifting apart is a silent
memory-vs-screen mismatch).

Two SDK realities it encodes (verified against OpenHands 1.26.0, 2026-07-08):
  - a no-tool agent reply arrives as an ``ActionEvent`` whose ``.action`` is a
    ``FinishAction`` carrying ``.message`` — NOT a ``MessageEvent`` (:func:`finish_message`);
  - when a weak/open model returns an empty/reasoning-only response, the SDK injects a
    SYNTHETIC ``MessageEvent(source="user")`` corrective nudge
    (``agent/response_dispatch.py:_send_corrective_nudge``). It is shaped exactly like a
    human turn, so a naive ``source=="user"`` turn-boundary would capture the NUDGE as the
    human question and drop the real one (:func:`is_corrective_nudge`).
"""
from __future__ import annotations

import json
import re

from markdown_it import MarkdownIt

# The discriminator of the built-in ``finish`` tool's action (a stable pydantic ``.kind``).
FINISH_ACTION_KIND = "FinishAction"

# The built-in SDK actions that are NOT executor/workspace tool calls: ``finish`` is the assistant's
# reply (surfaced by ``finish_message``), ``think`` is the model's private scratchpad — the SDK adds
# BOTH to every agent (``think`` is present even with ``tools=None``). Neither is workspace activity,
# so the REPL's tool-activity render skips them; without the ``think`` skip, ``--no-tools`` would
# render ``⚙ think: ThinkAction`` every turn, contradicting the "tools: none" banner.
_BUILTIN_ACTION_KINDS = frozenset({FINISH_ACTION_KIND, "ThinkAction"})

# A stable, SPAN-of-two-sentences fragment of the SDK's corrective-nudge text
# (agent/response_dispatch.py). Long enough that a real human is vanishingly unlikely to type
# it verbatim (so a genuine turn is never mis-excluded), yet not the full string (trivial
# rewording of the tail won't break the guard). A drift-guard test (test_firing_capture)
# asserts the installed SDK still emits it, turning a future SDK change from a silent
# regression into a loud test failure.
CORRECTIVE_NUDGE_MARKER = "did not include a function call or a message. Please use a tool"

# levain's OWN synthetic nudge — the narrate-without-act backstop (spore-358 follow-through). The SDK's
# corrective nudge (above) fires only when the model returns NEITHER a function call NOR a message; it
# does NOT fire when a weak open model returns a MESSAGE that is an unexecuted PLAN ("I'll run the
# tests…") with no tool call — OpenHands reads that plan as a valid answer and ends the turn, task
# untouched (bake-off 2026-07-17: the dominant failure across glm-5.2 / kimi; an act-first prompt lifted
# glm 2/3 -> 3/3). `levain run` detects that stall (:func:`planned_without_acting`) and injects THIS
# nudge as one synthetic user turn. The marker lets capture + the display boundary skip it exactly as
# they skip the SDK's, so memory never records "the operator told me to act."
LEVAIN_ACT_NUDGE_MARKER = "[levain:act-now]"
LEVAIN_ACT_NUDGE = (
    f"{LEVAIN_ACT_NUDGE_MARKER} You described a plan but have not taken any action yet. Do not "
    "describe what you will do — DO it now: issue the tool calls (run the commands, view and edit "
    "the files) to carry out the task, then report the result."
)

# A plan opener = an intent PREFIX ("I'll", "let me", …) followed by a tool-like ACTION verb
# ("run", "read", "edit", …). Deliberately the PRODUCT of the two — NOT bare "let me "/"i'll ", which
# also open conversational replies ("let me explain", "I'll be happy to help", "I'll summarize…") and
# clarifying questions ("I need to know which file"). Matching the action verb keeps a conceptual
# answer or a clarifying pause from being nudged (L1+L2 review 2026-07-17: bare openers false-fired on
# exactly those). Real stalls lead with a prefix+verb verbatim: "I'll run the tests and read the
# source file…", "Let me start by running the tests…".
_PLAN_INTENT_PREFIXES = (
    "i'll ", "i will ", "let me ", "let's ", "i'm going to ", "i am going to ", "i'm gonna ",
    "i need to ", "i want to ", "i should ", "then i'll ", "next i'll ", "going to ",
)
_PLAN_ACTION_VERBS = (
    "run", "start", "read", "view", "look", "open", "check", "edit", "fix", "modify", "update",
    "test", "examine", "inspect", "diagnose", "investigate", "make", "begin", "write", "add", "apply",
)
_PLAN_INTENT_MARKERS = tuple(
    f"{p}{v}" for p in _PLAN_INTENT_PREFIXES for v in _PLAN_ACTION_VERBS
) + ("first, i'll ", "first i'll ", "i'll first ", "let me first ", "start by ")


def _message_text(msg) -> str | None:
    """Join a Message's TextContent parts into one stripped string, or ``None`` if empty."""
    if msg is None:
        return None
    text = " ".join(
        c.text for c in (getattr(msg, "content", None) or []) if getattr(c, "text", None)
    ).strip()
    return text or None


def message_event_text(event) -> str | None:
    """The text of a ``MessageEvent`` (``event.llm_message`` content), or ``None``."""
    return _message_text(getattr(event, "llm_message", None))


def finish_message(event) -> str | None:
    """The agent's answer when it responded via the built-in ``finish`` tool —
    ``event.action.message`` iff ``event.action`` is a ``FinishAction``. ``None`` for any
    other action (a real bash/file tool call) or a non-action event, so real tool actions
    are never mistaken for assistant text."""
    action = getattr(event, "action", None)
    if action is None or getattr(action, "kind", None) != FINISH_ACTION_KIND:
        return None
    message = getattr(action, "message", None)
    return message.strip() if isinstance(message, str) and message.strip() else None


_ACTIVITY_COMMAND_CHARS = 160
"""A shell command longer than this (or spanning lines) is shown cut, ending in " …"."""


def tool_action_summary(event) -> tuple[str, str] | None:
    """``(tool_name, detail)`` for an agent ActionEvent that is a REAL tool call, else ``None``.

    Duck-typed (no ``openhands`` import), so the REPL's tool-activity render stays in the SDK-free
    test tier. Returns ``None`` for a non-action event, a message event, or a built-in control action
    (``finish`` — surfaced as the reply by :func:`finish_message`; ``think`` — the model's private
    scratchpad, on every agent even ``tools=None``); neither is workspace activity. ``detail`` is
    a compact ``"<command> <path>"`` for a file-editor action, the command's first line for a shell
    action (cut at :data:`_ACTIVITY_COMMAND_CHARS`), else the action ``kind`` — enough for the operator
    to SEE what the entity DID to its workspace (a file op must never be invisible, and neither may a
    shell command: until 2026-10-03 bash rendered as its kind, ``TerminalAction``, so a person
    watching the stream never saw a command)."""
    tool_name = getattr(event, "tool_name", None)
    action = getattr(event, "action", None)
    if not tool_name or action is None:
        return None
    if getattr(action, "kind", None) in _BUILTIN_ACTION_KINDS:
        return None  # finish is the reply; think is the model's scratchpad — neither is workspace activity
    command = getattr(action, "command", None)
    path = getattr(action, "path", None)
    if command and path:
        return tool_name, f"{command} {path}"
    if isinstance(command, str) and command.strip():
        lines = command.strip().splitlines()
        shown = lines[0]
        cut = len(shown) > _ACTIVITY_COMMAND_CHARS or len(lines) > 1
        return tool_name, shown[:_ACTIVITY_COMMAND_CHARS] + (" …" if cut else "")
    return tool_name, str(getattr(action, "kind", "") or "")


# The only calls :func:`humanize_finish_json` unwraps: the reply and the scratchpad, neither of which acts.
_UNWRAPPABLE_CALLS = frozenset({"finish", "think"})


def humanize_finish_json(text: str) -> str:
    """spore-297: a weak open model (minimax-m3, verified live 2026-07-09) sometimes emits its tool
    calls as JSON TEXT instead of structured tool calls — e.g.::

        {"name": "think", "arguments": {"summary": "...", "thought": "..."}}
        {"name": "finish", "arguments": {"summary": "...", "message": "the real reply"}}

    — so the REPL (and the captured episode) would show raw JSON instead of the reply. If ``text`` is
    one-or-more CONCATENATED tool-call JSON objects, return the ``finish`` call's ``arguments.message``
    (the human reply), dropping ``think`` (the scratchpad). Otherwise return ``text`` UNCHANGED — a
    normal reply that merely contains a brace or a JSON snippet is never mangled: trailing prose after
    a JSON object, a non-dict, or a JSON object that is not a ``finish`` tool call all leave it as-is.
    Conservative by design — it only unwraps a clean, entirely-tool-call-JSON payload that carries a
    ``finish`` message, so it fixes the observed failure without ever eating a legitimate answer. Every
    call in it must be ``think`` or ``finish``: a payload that also holds any other call (``terminal``, a
    name it does not know) is kept whole, because that call did not run, and answering with the finish
    message ("Created x") would hide it from the unreadable-call check that reads this function's output."""
    calls = _json_calls(text.strip())
    if not calls or any(name not in _UNWRAPPABLE_CALLS for name, _ in calls):
        return text  # not a clean payload of think/finish calls only → leave untouched
    for name, args in calls:
        if name == "finish":
            if isinstance(args, str):  # the OpenAI wire form carries the arguments as a JSON string
                try:
                    args = json.loads(args)
                except (ValueError, RecursionError):
                    args = None
            message = args.get("message") if isinstance(args, dict) else None
            if isinstance(message, str) and message.strip():
                return message.strip()
    return text  # no finish message found → don't fabricate a reply from the scratchpad


# What the panel and the REPL show instead of a reply that is a model's unreadable tool call (Phill, 2026-10-05).
UNREADABLE_CALL_NOTICE = (
    "The model tried to call a tool, but its call couldn't be read, so nothing ran. Ask again, or switch models."
)
# The same, for a turn in which other actions DID run (listed with it): "nothing ran" would be false there, and an
# operator who believed it could ask again and run an approved action twice.
UNREADABLE_CALL_AFTER_ACTIONS_NOTICE = (
    "The model tried to call a tool, but its last call couldn't be read, so that call did not run. The actions "
    "listed with this message did run. Ask again, or switch models."
)


def unreadable_call_notice(tool_activity) -> str:
    """The notice for a turn whose reply is an unreadable tool call, given the actions that turn ran."""
    return UNREADABLE_CALL_AFTER_ACTIONS_NOTICE if tool_activity else UNREADABLE_CALL_NOTICE

# GLM's argument markup: a key tag next to a value tag. A parse failure upstream can cut the reply anywhere, so
# either order and either tag half counts ("</arg_key><arg_value>", "</arg_value><arg_key>").
_GLM_ARG_PAIR = re.compile(r"</arg_key>\s*<arg_value>|</arg_value>\s*<arg_key>")
# The <tool_call> wrapper opening an actual call: a JSON object, a tool name followed by GLM argument markup, or
# Qwen3-Coder's <function=name>. The bare tag in a sentence ("a <tool_call> tag") is not a call. The space between
# may cross line and paragraph breaks.
_TOOL_CALL_OPEN = re.compile(r"<tool_call>\s*(?:\{|<function=|[A-Za-z_][\w.-]*\s*<arg_key>)")
# Qwen3-Coder's call without its wrapper: an upstream parser can consume "<tool_call>" and leave the rest as text
# (glm-5.2:cloud via Ollama, 2026-10-07). The function tag must be followed by a parameter tag or its own close.
_FUNCTION_CALL_OPEN = re.compile(r"<function=[A-Za-z_][\w.-]*>\s*(?:<parameter=|</function>)")
# A reply this large is not parsed as Markdown (Phill 2026-10-05, A'): a bound, so no input can make the parser slow.
# It is still searched for markup and call JSON, with no region counted as code, so a large leak is flagged rather
# than shown as an answer.
MAX_CLASSIFIED_BYTES = 200_000
# MiniMax-M2's call: plain-text tags, so a failed parse upstream leaves them verbatim.
_INVOKE_OPEN = re.compile(r"<invoke name=\"[A-Za-z_][\w.-]*\">\s*(?:<parameter name=|</invoke>)")
# GLM's call cut after its first key tag, before any value tag.
_GLM_ARG_KEY = re.compile(r"<arg_key>[\w.-]+</arg_key>")
_CALL_MARKUP = (_TOOL_CALL_OPEN, _FUNCTION_CALL_OPEN, _INVOKE_OPEN, _GLM_ARG_PAIR, _GLM_ARG_KEY)
# Markup that names its tool, counted only when the name is one of the entity's: GLM's call of a tool with no
# arguments (the wrapper around a bare name), and what Kimi's call leaves once its special tokens are stripped.
_NAMED_CALL_MARKUP = (
    re.compile(r"<tool_call>\s*([A-Za-z_][\w.-]*)\s*</tool_call>"),
    re.compile(r"functions\.([A-Za-z_][\w.-]*):\d+\s*\{"),
)
# Where a function-call JSON object may start inside other text.
_JSON_CALL_START = re.compile(r"\{\s*\"(?:name|type)\"\s*:")
_SPACE = re.compile(r"\s*")

# The rule (Phill 2026-10-05, A'): tool-call markup found anywhere OUTSIDE a code region is a leak, inside a
# heading, a list or a quote included. A model has no reason to write that markup in prose; a false flag still shows
# the text, under the notice, and nothing runs either way, but a headless run then exits 7 with no stdout payload. Code regions are the CommonMark parser's to decide
# (markdown-it-py, "commonmark" preset): fenced and indented code blocks and code spans. Everything else is kept, in
# document order, one region per line; an entity or a backslash escape ("&lt;", "\\<") and a code span become a
# placeholder that is neither a space nor markup, so "&lt;tool_call>" and "\\<tool_call>" are not "<tool_call>".
_MD = MarkdownIt("commonmark").disable("text_join")
# markdown-it-py compiles each rule chain on first use and publishes the empty cache before filling it, so two
# threads parsing their first reply at once could run without rules (codex L3 r5). Parse once here, single-threaded.
_MD.parse("warm *a* `b` [c](d)\n\n> e\n\n- f\n\n# g\n\n```\nh\n```\n")
_MARK = "\x00"


def _inline_text(token) -> str:
    parts: list[str] = []
    for c in token.children or ():
        if c.type in ("softbreak", "hardbreak"):
            parts.append("\n")
        elif c.type in ("text", "html_inline"):
            parts.append(c.content)
        elif c.type in ("text_special", "code_inline"):   # an escape or entity is quoted text, not markup
            parts.append(_MARK)
        elif c.children:
            parts.append(_inline_text(c))
    return "".join(parts)


def _read(text: str) -> tuple[str, str | None]:
    """One parse of ``text``: (``text`` with its code regions removed, see above; the inside of ``text`` when its only
    top-level block is one fenced or indented code block, else ``None``). If the parser fails, all of ``text`` counts as outside
    code and there is no fence: a check that cannot read the reply flags rather than hides."""
    try:
        tokens = _MD.parse(text)
    except Exception:  # noqa: BLE001
        return text, None
    regions = []
    for t in tokens:
        if t.type == "inline":
            regions.append(_inline_text(t))
        elif t.type == "html_block":
            regions.append(t.content)
    blocks = [t for t in tokens if t.level == 0 and not t.type.endswith("_close")]
    body = blocks[0].content if len(blocks) == 1 and blocks[0].type in ("fence", "code_block") else None
    return "\n".join(regions), body


def _as_call(obj: object) -> tuple[str, object] | None:
    """``(name, arguments)`` when ``obj`` is a function-call object (see :func:`_json_calls`), else ``None``."""
    if not isinstance(obj, dict):
        return None
    if obj.get("type") == "function" and isinstance(obj.get("function"), dict):
        obj = obj["function"]
    name = obj.get("name")
    if isinstance(name, str) and ("arguments" in obj or "parameters" in obj):
        return name, obj.get("arguments", obj.get("parameters"))
    return None


def _json_call_names(text: str) -> list[str] | None:
    """The tool names of ``text`` when ALL of it is one or more function-call JSON values, else ``None``."""
    calls = _json_calls(text)
    return [name for name, _ in calls] if calls else None


def _json_calls(text: str) -> list[tuple[str, object]] | None:
    """``(name, arguments)`` of each call when ALL of ``text`` is one or more function-call JSON values, else ``None``.
    A call is an object with a string ``name`` and an ``arguments`` or ``parameters`` key, or an OpenAI
    ``{"type": "function", "function": {...}}`` wrapper of one; a top-level JSON array of call objects counts too.
    Never raises: input the decoder cannot take (malformed, or nested past its recursion limit) is not a call."""
    decoder = json.JSONDecoder()
    calls: list[tuple[str, object]] = []
    idx, n = 0, len(text)

    def call(obj: object) -> bool:
        c = _as_call(obj)
        if c is not None:
            calls.append(c)
        return c is not None

    while idx < n:
        idx = _SPACE.match(text, idx).end()
        if idx >= n:
            break
        try:
            obj, idx = decoder.raw_decode(text, idx)
        except (ValueError, RecursionError):
            return None
        items = obj if isinstance(obj, list) and obj else [obj]
        if not all(call(x) for x in items):
            return None
    return calls or None


def unreadable_tool_call(text: str | None, tool_names: frozenset[str] | set[str]) -> bool:
    """Whether ``text``, an agent's reply, is a model's raw tool-call syntax rather than an answer.

    An open model's call that fails to parse upstream reaches levain as reply TEXT, and that call did not run.
    Three shapes are recognised. Two are markup found anywhere outside Markdown code (see :func:`_read`):
    GLM argument markup (a key tag beside a value tag), and a ``<tool_call>`` wrapper that opens a call or a
    Qwen3-Coder ``<function=name>`` tag that opens one without it (or MiniMax's ``<invoke name=...>``, or a wrapper
    around nothing but one of ``tool_names``); markup
    written in code is an answer, unless the reply is nothing but one fenced block, which is read as the call it
    holds (an indented block likewise). The third is function-call JSON naming ``tool_names``, the entity's own tools:
    all of the reply (bare, or the whole of one code block) when every call names one, or one such call among other
    text outside code; with no tool names known, that shape is not flagged. A reply over
    :data:`MAX_CLASSIFIED_BYTES` is not parsed as Markdown: all of it is searched as if no part were code. It reads
    the shape only: the call is never repaired or run."""
    if not text:
        return False
    # Characters first: UTF-8 spends at least one byte per character, so more characters than the bound means more
    # bytes, and a reply that size is never encoded just to be measured (codex L3 r7).
    if len(text) > MAX_CLASSIFIED_BYTES or len(text.encode("utf-8", "surrogatepass")) > MAX_CLASSIFIED_BYTES:
        prose, body = text, None
    else:
        prose, body = _read(text)
    # No strip: it would copy a reply over the bound, and the JSON reader skips the space at either end itself.
    names = _json_call_names(body if body is not None else text)
    if names and all(n in tool_names for n in names):
        return True
    regions = [r for r in (prose, body) if r]
    return (
        any(p.search(r) for r in regions for p in _CALL_MARKUP)
        or any(m.group(1) in tool_names for r in regions for p in _NAMED_CALL_MARKUP for m in p.finditer(r))
        or any(_embedded_call(r, tool_names) for r in regions)
    )


def _embedded_call(text: str, tool_names) -> bool:
    """Whether ``text`` holds, among other text, a function-call JSON object naming one of ``tool_names`` (the head's
    ruling, 2026-10-07: the notice is true then, and the text is still shown under it). Each attempt resumes where the
    last one ended or failed, so nested or unterminated objects cost one pass, not one per brace; a call nested inside
    another JSON value that decodes whole is therefore not looked for."""
    decoder = json.JSONDecoder()
    pos = 0
    while (m := _JSON_CALL_START.search(text, pos)) is not None:
        try:
            obj, pos = decoder.raw_decode(text, m.start())
        except json.JSONDecodeError as exc:
            pos = max(exc.pos, m.start() + 1)
            continue
        except RecursionError:
            # Nested past the decoder's limit, with no position to resume from: retrying at each inner brace would be
            # quadratic, and nothing that deep is a call a model meant to make.
            return False
        except ValueError:
            pos = m.start() + 1
            continue
        call = _as_call(obj)
        if call is not None and call[0] in tool_names:
            return True
    return False


def is_corrective_nudge(event) -> bool:
    """True iff ``event`` is a SYNTHETIC ``source="user"`` nudge — the SDK's own corrective nudge OR
    levain's act-now nudge (:data:`LEVAIN_ACT_NUDGE`) — neither of which is a genuine human turn (as a
    turn boundary either would fabricate the wrong ``[user]`` line and drop the real question, and
    capture would record it as the operator's words). Recognized by source + a stable text marker."""
    if getattr(event, "source", None) != "user":
        return False
    text = message_event_text(event)
    if text is None:
        return False
    # levain's OWN nudge is a fixed string we emit verbatim — match it by PREFIX, not substring, so a
    # genuine user message that merely QUOTES the marker ("why did [levain:act-now] show up?") is NOT
    # silently reclassified as synthetic and dropped from the turn boundary / capture / recall (codex +
    # gpt-oss L3 2026-07-17). The SDK fragment stays substring-matched: it is the SDK's own long,
    # distinctive text placed mid-message, not ours to reshape.
    return CORRECTIVE_NUDGE_MARKER in text or text.lstrip().startswith(LEVAIN_ACT_NUDGE_MARKER)


def turn_start(events) -> int:
    """Index of the current turn's start: the last genuine user MESSAGE (``0`` if there is none).

    A user message, not any user-sourced event: the SDK's ``PauseEvent`` is source ``"user"`` too,
    and reading a pause as the start of the turn dropped every action the turn ran before a
    wall-clock stop (K1p2 run, 2026-10-03). The SDK's synthetic corrective nudge is skipped, so a
    weak-model turn keys on the real question. The ONE home of this rule: activity, reply text and
    the act-first check all read the turn from here. Duck-typed; no ``openhands`` import."""
    for i in range(len(events) - 1, -1, -1):
        e = events[i]
        if (getattr(e, "source", None) == "user" and hasattr(e, "llm_message")
                and not is_corrective_nudge(e)):
            return i
    return 0


def planned_without_acting(events) -> bool:
    """True iff the just-completed agent turn took ZERO real tool actions AND its reply reads as a
    forward PLAN (intent to act), not an answer — the narrate-first stall where a weak open model ENDS
    a turn by describing what it will do ("I'll run the tests…") with no tool call, so OpenHands treats
    that plan as a valid answer and stops with the task untouched.

    Structural signal FIRST — the turn contained no real tool ActionEvent (``tool_action_summary`` skips
    the builtin ``finish``/``think``); a turn that DID act is never a stall. Gated by a forward-intent
    text check on the reply's OPENING so a genuine no-tool ANSWER is not mis-fired. ``levain.run`` uses
    this to inject ONE :data:`LEVAIN_ACT_NUDGE` and re-run — the structural equivalent of the act-first
    prompt that measured glm 2/3 -> 3/3 (bake-off 2026-07-17). Duck-typed; no ``openhands`` import."""
    evs = list(events)
    start = turn_start(evs)
    reply: str | None = None
    for e in evs[start:]:
        if getattr(e, "source", None) != "agent":
            continue
        if tool_action_summary(e) is not None:
            return False  # it DID act this turn — not a stall (checked for EVERY agent event)
        if reply is None:
            text = message_event_text(e) or finish_message(e)
            if text:
                reply = text  # keep the FIRST agent text — a stall is signaled by how the turn OPENS
                # (its opening move is a plan). Keeping the LAST text missed a plan-opener trailed by a
                # filler line ("I'll run the tests." then "One moment.") — a real zero-tool stall the
                # detector let slip (codex L3 2026-07-17). The tool early-return above still scans ALL
                # agent events, so a later real action still cancels the stall.
    if not reply:
        return False
    reply = humanize_finish_json(reply)  # parity with the other consumers — unwrap a JSON-wrapped plan
    if "?" in reply:
        return False  # a clarifying question ("which file did you mean?") is a legitimate pause, never
        # a stall — nudging it would override the agent's correct decision to wait for the human (L2).
    # Normalize curly apostrophes to straight — measured: kimi-k2.7-code writes "I’ll" with U+2019, so
    # a straight-apostrophe marker misses the stall and the backstop never fires (bake-off 2026-07-17).
    low = reply.lower().replace("’", "'").replace("‘", "'")
    opening = low.lstrip("*#>- ").lstrip()[:80]
    return any(m in opening for m in _PLAN_INTENT_MARKERS)
