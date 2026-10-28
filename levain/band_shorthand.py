"""levain.band_shorthand — the band's shorthand parser (Q17-ALL S2, design §5, D6/D7). PURE: text in, a plan out.

Levain code parses the operator's shorthand; the agent never does (D6). The parser reads nothing and writes nothing:
it takes the text, the VIEW the operator saw (:class:`View`) and what each numbered item is (:class:`ItemInfo`), and
returns a :class:`Parse` of what would be recorded (``actions``), what is refused and why (``refused``), and the rest
of the message (``rest``) that goes to the agent untouched.

Grammar (``,`` ``;`` or white space between entries)::

    entry := NUM [":" | "." | ")"]? WS? pick | NUM ":" WS words | SWEEP
    pick  := a-f | "(" a-f ")" | rec | recs | your rec | you rec | r
    SWEEP := (all | rest | others | all others) WS REC
    note  := (because | but | - | —) WS text-to-end-of-line      # attaches to the entry before it

A message is the LEADING lines that each parse in full; the first line that does not, or a blank line, ends the
block and, with all after it, is ``rest``. A line that does not FULLY parse records nothing; a line over
:data:`LINE_MAX` characters is not read at all (the lexer is linear, the cap bounds it). Free text (``7: no longer needed``) takes the
rest of its line. A pick with no number (``(a) all recs``) is never guessed onto an item.
Rules (D7): an entry naming a number above the view's ``seen_through``, or not open in the view, is refused; a view
of another store (``store_id``) refuses everything; the SWEEP takes only seen, open, INFORMATION items no entry
names, never an authority item; the same pick twice is one; two different picks for one item refuse that item.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["Action", "ItemInfo", "Parse", "Refusal", "View", "parse"]

_NUM = r"[1-9][0-9]{0,6}(?![0-9])"
_REC = r"(?:your\s+rec|you\s+rec|recs|rec|r)(?![A-Za-z0-9])"
_SEP = re.compile(r"[,;\s]+")
_SWEEP = re.compile(rf"(?P<sweep>(?:all\s+others|all|rest|others)\s+{_REC})", re.I)
# `\s*(?:[:.)]\s*)?` is `\s*[:.)]?\s*` without two quantifiers splitting one run of spaces (no backtracking blow-up)
_ENTRY = re.compile(rf"(?P<num>{_NUM})\s*(?:[:.)]\s*)?(?:(?P<rec>{_REC})|\((?P<l1>[a-f])\)|(?P<l2>[a-f])(?![A-Za-z0-9]))",
                    re.I)
# the head of a free-text entry only (NUM ":" then a word): the text is the rest of the line, sliced once when taken,
# so trying it at every entry never scans to the end of the line
_TEXT = re.compile(rf"(?P<num>{_NUM})\s*:\s*(?=\S)", re.I)
# a line longer than this is never read as answers (the block ends there): the hook's work stays bounded
LINE_MAX = 2000
_NOTE = re.compile(r"\s+(?:(?:because|but)\s+|[-—]\s+)(?P<note>\S.*)$", re.I)
# a first line that was MEANT as an answer: NUM LETTER, NUM ":", a bare (a), or a leading sweep
_ATTEMPT = re.compile(rf"\s*(?:{_NUM}\s*(?:[a-f](?![A-Za-z])|:|\([a-f]\))|\([a-f]\)|(?:all|rest|others)\s+{_REC})", re.I)


@dataclass(frozen=True)
class View:
    """The band one viewer last saw: the highest number shown, the numbers open in it, the store it came from."""
    seen_through: int
    open_list: tuple[int, ...]
    store_id: str


@dataclass(frozen=True)
class ItemInfo:
    """What a numbered item is: ``kind`` is ``information`` (shorthand records an answer) or ``authority`` (it records
    nothing: S3 routes it to its signing sheet); ``options`` the count; ``rec_letter`` the display letter of its rec."""
    kind: str
    options: int
    rec_letter: str | None


@dataclass(frozen=True)
class Action:
    """One answer to record: option letter ``pick`` (``via_rec`` when it came from a rec) or free ``text``."""
    n: int
    pick: str | None = None
    text: str | None = None
    note: str | None = None
    via_rec: bool = False

    @property
    def label(self) -> str:
        if self.text is not None:
            return f"{self.n}: {self.text}"
        return f"{self.n} rec→{self.pick}" if self.via_rec else f"{self.n}{self.pick}"


@dataclass(frozen=True)
class Refusal:
    n: int | None
    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True)
class Parse:
    """``matched``: at least one leading line parsed in full. ``actions`` / ``refused``: what to record and what not.
    ``rest``: the message after the block, for the agent. ``hint``: set when the first line looked like an answer but
    did not fully parse (nothing was recorded)."""
    matched: bool
    actions: tuple[Action, ...] = ()
    refused: tuple[Refusal, ...] = ()
    rest: str = ""
    hint: str | None = None


@dataclass
class _Raw:
    kind: str                                  # pick | rec | text | sweep
    n: int = 0
    letter: str = ""
    text: str = ""
    note: str | None = None


@dataclass
class _Line:
    entries: list[_Raw] = field(default_factory=list)
    error: str | None = None


def _text(line: str, t: re.Match[str]) -> _Raw:
    return _Raw("text", int(t.group("num")), text=line[t.end():].rstrip())


def _lex(line: str) -> _Line:
    """One pass, left to right, linear in the line: each pattern is tried once per entry, anchored where the entry
    starts, and the line is sliced only for the one free text taken. An entry that reads both as a pick and as the
    start of free text (``3: a thing, more``) is the pick when the rest of the line lexes cleanly, else free text to
    the end of the line. Free text always lexes, so on an error only the LAST such entry decides: it becomes the
    text, and every entry before it stays (its own rest now lexes cleanly)."""
    out = _Line()
    pos = 0
    n = len(line)
    fork: tuple[int, re.Match[str]] | None = None    # the last pick-or-text entry: (its index, its free-text head)
    while True:
        m = _SEP.match(line, pos)
        if m:
            pos = m.end()
        if pos >= n:
            return out
        raw: _Raw | None = None
        m = _SWEEP.match(line, pos)
        if m:
            raw = _Raw("sweep")
        else:
            m = _ENTRY.match(line, pos)
            if m:
                if m.group("rec"):
                    raw = _Raw("rec", int(m.group("num")))
                else:
                    raw = _Raw("pick", int(m.group("num")), (m.group("l1") or m.group("l2")).lower())
        end = m.end() if m else pos
        note = _NOTE.match(line, end) if raw is not None else None
        if raw is not None and note is None and end < n and _SEP.match(line, end) is None:
            raw = None                                   # not a clean entry
        if raw is None:
            t = _TEXT.match(line, pos)
            if t:
                out.entries.append(_text(line, t))
                return out
            if fork is not None:                         # "3: a thing, more": the last pick-or-text entry is text
                del out.entries[fork[0]:]
                out.entries.append(_text(line, fork[1]))
                return out
            out.error = f"could not read {line[pos:pos + 24]!r}"
            return out
        if note:
            raw.note = note.group("note").rstrip()
            out.entries.append(raw)
            return out
        if raw.kind != "sweep" and (t := _TEXT.match(line, pos)):
            fork = (len(out.entries), t)
        out.entries.append(raw)
        pos = end


def parse(text: str, view: View | None, items: dict[int, ItemInfo], current_store_id: str | None = None) -> Parse:
    """The plan for ``text`` against ``view`` (``None``: this viewer was never shown a band). ``items`` describes the
    numbers of the view; ``current_store_id`` is the store answering now (``None``: not checked)."""
    lines = text.split("\n")
    raws: list[_Raw] = []
    used = 0
    hint: str | None = None
    for i, line in enumerate(lines):
        if not line.strip():
            break                                       # a blank line ends the block; it and all after are `rest`
        lexed = _lex(line) if len(line) <= LINE_MAX else _Line(error=f"the line is too long (over {LINE_MAX} "
                                                                     "characters)")
        if lexed.error is not None or not lexed.entries:
            if not raws and _ATTEMPT.match(line):
                reason = lexed.error or "empty"
                if line.lstrip().startswith("("):
                    reason = "a pick needs its number in front: retype it as `1a, all recs`"
                hint = f"Levain did not read this as a band answer ({reason}); nothing was recorded"
            elif raws and _ATTEMPT.match(line):
                hint = (f"Levain did not read the line {line.strip()[:40]!r} as a band answer "
                        f"({lexed.error or 'empty'}); nothing was recorded for it")
            break
        raws.extend(lexed.entries)
        used = i + 1
    rest = "\n".join(lines[used:])
    if not raws:
        return Parse(False, rest=text, hint=hint)
    refused: list[Refusal] = []
    if view is None:
        return Parse(True, (), (Refusal(None, "no band was shown to this viewer yet; nothing recorded: run `levain band`"),),
                     rest)
    if current_store_id is not None and view.store_id != current_store_id:
        return Parse(True, (), (Refusal(None, "the band you saw came from another question store; nothing recorded "
                                              "(ask for a fresh band)"),), rest)
    by_n: dict[int, list[_Raw]] = {}
    sweeps: list[_Raw] = []
    for r in raws:
        if r.kind == "sweep":
            sweeps.append(r)
        else:
            by_n.setdefault(r.n, []).append(r)
    actions: list[Action] = []
    open_set = set(view.open_list)
    for n, entries in by_n.items():
        if n > view.seen_through:
            refused.append(Refusal(n, f"{n} was filed after the band you saw; nothing recorded for it"))
            continue
        info = items.get(n)
        if n not in open_set or info is None:
            refused.append(Refusal(n, f"{n} was not open in the band you saw; nothing recorded for it"))
            continue
        if info.kind != "information":
            refused.append(Refusal(n, f"{n} needs your signature; nothing recorded"))
            continue
        picks: list[tuple[str, str, bool]] = []          # (kind, value, via_rec)
        bad: str | None = None
        for r in entries:
            if r.kind == "text":
                picks.append(("text", r.text, False))
            elif r.kind == "rec":
                if info.rec_letter is None:
                    bad = f"{n} has no rec to take"
                else:
                    picks.append(("pick", info.rec_letter, True))
            elif "abcdef".index(r.letter) >= info.options:
                bad = f"{n} has no option {r.letter}"
            else:
                picks.append(("pick", r.letter, False))
        if bad:
            refused.append(Refusal(n, bad))
            continue
        distinct = list(dict.fromkeys((k, v) for k, v, _ in picks))
        if len(distinct) > 1:
            shown = [f"{n}{v}" if k == "pick" else f"{n}: {v}" for k, v in distinct]
            refused.append(Refusal(n, "conflict: " + " and ".join(shown)))
            continue
        kind, value = distinct[0]
        note = next((r.note for r in entries if r.note), None)
        via_rec = all(v for k, _, v in picks) if kind == "pick" else False
        actions.append(Action(n, pick=value if kind == "pick" else None, text=value if kind == "text" else None,
                              note=note, via_rec=via_rec))
    taken = set(by_n)
    for sw in sweeps:
        covered = 0
        before = len(refused)
        for n in sorted(open_set):
            if n in taken or n > view.seen_through:
                continue
            info = items.get(n)
            taken.add(n)
            if info is None:
                refused.append(Refusal(n, f"{n} could not be read; the sweep left it alone"))
            elif info.kind != "information":
                refused.append(Refusal(n, f"{n} needs your signature; the sweep left it alone"))
            elif info.rec_letter is None:
                refused.append(Refusal(n, f"{n} has no rec for the sweep to take"))
            else:
                actions.append(Action(n, pick=info.rec_letter, note=sw.note, via_rec=True))
                covered += 1
        if not covered and len(refused) == before:
            refused.append(Refusal(None, "nothing open was left for the sweep to take"))
    return Parse(True, tuple(actions), tuple(refused), rest)
