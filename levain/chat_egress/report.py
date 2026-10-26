"""K1's report (CH-D2 S9): what one chat launch's egress did, read from its stored rows and the glue's event file.

Pure: it takes rows (`records.EgressRow`), the glue event lines (bytes, read with `wire.parse_glue_event`) and the
decider's `duplicate_rows` counter, and returns a `LaunchReport`; it opens no file and no database. It reads NAMES and
counts only: the rows carry no value (R15-17-8) and the event file carries no path, query value or header value
(R15-21-2), so a report built from them cannot carry one.

What it counts, and the row that asks for it:
  * the glue's wall total, from the launch-end flush (R15-29-2); a crash is marked (R15-30-3): before any summary it
    reads `unknown (launch crashed before its first summary; at least N)`, never the detailed count as if exact; after
    one it is the last summary's total, marked as from a crashed launch.
  * per-reason counters and the bounded host map from that summary (R15-28-1, R15-30-2).
  * `duplicate_rows` (R15-28-5, R15-29-3): K1 only.
  * the decider's `credential_in_query` walls and cards (R15-17-10), the signed-URL set names on
    an attempt (a read an exemption lifted stores the lifted parameter NAMES, never a value: the signed-URL set under
    `signed_url_names`, Azure SAS's `sig` under `azure_sas_names`) and the names the backstop alone matched (the `backstop` flag of
    `shape.segment_token`), the cap walls and cards (R15-19-3) and the path walls (R15-10-A17), from the attempt
    rows' `deciding_step` and `names`.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from levain.chat_egress import strip, wire
from levain.chat_egress.records import EgressRow

#: `deciding_step` values of a request the decider stopped at one of its bounds, as it records them (a row exists for
#: each: the audit-cap deny is owed outside the machine and has none). Pinned against a live launch in test_k1_report.
CAP_STEPS = frozenset({"wall:card_cap", "wall:body_cap", "wall:body_readers", "wall:body_timeout", "decider_busy"})
_BUSY_STEP = "decider_busy"
_UPLOAD_PACK_CAP_CARD = "upload_pack_over_cap"   # decider.UPLOAD_PACK_OVER_CAP: the card reason of an over-cap pack body

_SIGNED = strip.SIGNED_URL_READ_EXEMPT           # folded names, the form the rows store
_AZURE_SIG = "sig"                               # the Azure SAS read exemption's lifted name; a marked `sig` is stored as a token
_MALFORMED_CARD = "malformed_card_step"
_CRED_WALL = "wall:credential_in_query"
_PATH_WALL = "wall:path"
_CARD_PREFIX = "card:"
_WALL_PREFIX = "wall:"
_BACKSTOP_FLAG = ":backstop>"


@dataclass(frozen=True, slots=True)
class WallTotal:
    """The glue's wall total. `exact` is the clean end's flush; otherwise `total` is a floor (None: unknown) and `text` says what is known."""
    total: int | None
    exact: bool
    crashed: bool
    text: str


@dataclass(frozen=True, slots=True)
class LaunchReport:
    walls: WallTotal
    by_reason: dict[str, int]
    hosts: dict[str, int]
    host_overflow: int
    duplicate_rows: int
    verdicts: dict[str, int]
    decider_walls: dict[str, int]
    credential_in_query_walls: int
    credential_in_query_cards: int
    cap_walls: int
    cap_cards: int
    path_walls: int
    cards: dict[str, int]
    signed_url_names: dict[str, int]
    azure_sas_names: int
    backstop_names: int
    attempts: int
    audit_missing: int
    unreadable_event_lines: int = 0
    socks_uncounted: int = 0          # SOCKS refusals whose wall write failed: in no glue total
    notes: tuple[str, ...] = field(default_factory=tuple)

    def lines(self) -> list[str]:
        """The report as text, one fact per line; every number is a count of names, rows or events."""
        out = [f"glue walls: {self.walls.text}"]
        if self.by_reason:
            out.append("  by reason: " + ", ".join(f"{k}={v}" for k, v in sorted(self.by_reason.items())))
        if self.hosts or self.host_overflow:
            out.append(f"  hosts: {len(self.hosts)} named, {self.host_overflow} walls on other hosts")
        out.append(f"attempts: {self.attempts} ("
                   + ", ".join(f"{k}={v}" for k, v in sorted(self.verdicts.items())) + ")")
        out.append(f"decider walls: {sum(self.decider_walls.values())}"
                   + ("; " + ", ".join(f"{k}={v}" for k, v in sorted(self.decider_walls.items()))
                      if self.decider_walls else ""))
        out.append(f"credential_in_query: {self.credential_in_query_walls} walls, "
                   f"{self.credential_in_query_cards} cards")
        out.append(f"path walls: {self.path_walls}")
        out.append(f"cap walls: {self.cap_walls}, cap cards: {self.cap_cards}")
        out.append("cards: " + (", ".join(f"{k}={v}" for k, v in sorted(self.cards.items())) or "none"))
        out.append("signed-URL names marked: "
                   + (", ".join(f"{k}={v}" for k, v in sorted(self.signed_url_names.items())) or "none"))
        out.append(f"Azure SAS sig names marked: {self.azure_sas_names}")
        out.append(f"backstop-only names: {self.backstop_names}")
        out.append(f"audit-missing denies: {self.audit_missing}")
        out.append(f"duplicate_rows: {self.duplicate_rows}")
        if self.unreadable_event_lines:
            out.append(f"unreadable glue event lines: {self.unreadable_event_lines}")
        if self.socks_uncounted:
            out.append(f"{self.socks_uncounted} SOCKS refusals not counted (their wall write failed; the glue total is an undercount)")
        out.extend(self.notes)
        return out

    def text(self) -> str:
        return "\n".join(self.lines()) + "\n"


def wall_total(events: Iterable[wire.GlueEvent]) -> tuple[WallTotal, wire.GlueSummary | None]:
    """R15-29-2 / R15-30-3 over the parsed glue events, in file order."""
    last: wire.GlueSummary | None = None
    detailed = after_last = 0
    for e in events:
        if isinstance(e, wire.GlueSummary):
            last = e
            after_last = 0
        elif isinstance(e, wire.GlueWall):
            detailed += 1
            after_last += 1
    if last is not None and last.final:
        return WallTotal(last.total, True, False, str(last.total)), last
    if last is not None and last.total is None:       # typed int; a hand-built summary may not carry one
        return WallTotal(None, False, True,
                         f"unknown (a crashed launch's last summary has no total; at least {after_last} detailed walls after it)"), last
    if last is not None:
        text = f"{last.total} (from a crashed launch: the last summary, no final flush)"
        if after_last:                    # walls the glue wrote after that summary are in no total: a floor, not a count
            text = f"at least {last.total + after_last} (last summary {last.total}, plus {after_last} detailed walls after it; crashed launch, no final flush)"
        return WallTotal(last.total + after_last, False, True, text), last
    return WallTotal(None, False, True,
                     f"unknown (launch crashed before its first summary; at least {detailed})"), None


def _names(row: EgressRow) -> list[str]:
    try:
        v = json.loads(row.names or "[]")
    except ValueError:
        return []
    return [n for n in v if isinstance(n, str)] if isinstance(v, list) else []


def build(rows: Sequence[EgressRow], glue_lines: Iterable[bytes], duplicate_rows: int = 0,
          socks_wall_failures: int = 0) -> LaunchReport:
    """K1's report for one launch. A glue line `wire.parse_glue_event` refuses is counted, never skipped silently."""
    events: list[wire.GlueEvent] = []
    unreadable = 0
    for ln in glue_lines:
        if not ln.strip():
            continue
        try:
            events.append(wire.parse_glue_event(ln))
        except wire.WireError:
            unreadable += 1
    walls, summary = wall_total(events)

    signed = _SIGNED
    verdicts: Counter[str] = Counter()
    dwalls: Counter[str] = Counter()
    cards: Counter[str] = Counter()
    sig_names: Counter[str] = Counter()
    cred_walls = cred_cards = cap = cap_cards = path = backstop = azure = attempts = missing = 0
    for r in rows:
        if r.kind != "egress_attempt":
            continue
        attempts += 1
        verdicts[r.verdict.value if r.verdict is not None else "none"] += 1
        step = r.deciding_step or ""
        if step == "audit-missing":
            missing += 1
        if step == _BUSY_STEP:                # stored bare, no `wall:` prefix; it is a deny the decider decided
            dwalls[step] += 1
            cap += 1
        elif step.startswith(_WALL_PREFIX):
            dwalls[step[len(_WALL_PREFIX):]] += 1
            if step == _CRED_WALL:
                cred_walls += 1
            elif step == _PATH_WALL:
                path += 1
            if step in CAP_STEPS:
                cap += 1
        elif step.startswith(_CARD_PREFIX):
            _, _, rest = step.partition(":")
            why, _, answer = rest.rpartition(":")
            if not why or not answer:         # the row's names are still counted below
                cards[_MALFORMED_CARD] += 1
            else:
                cards[f"{why}:{answer}"] += 1
                if why == "credential_in_query":
                    cred_cards += 1
                elif why == _UPLOAD_PACK_CAP_CARD:
                    cap_cards += 1
        for n in _names(r):
            if n in signed:
                sig_names[n] += 1
            elif n == _AZURE_SIG:
                azure += 1
            if n.endswith(_BACKSTOP_FLAG):
                backstop += 1
    return LaunchReport(
        walls=walls, by_reason=dict(summary.by_reason) if summary else {},
        hosts=dict(summary.hosts) if summary else {}, host_overflow=summary.overflow if summary else 0,
        duplicate_rows=duplicate_rows, verdicts=dict(verdicts), decider_walls=dict(dwalls),
        credential_in_query_walls=cred_walls, credential_in_query_cards=cred_cards, cap_walls=cap, cap_cards=cap_cards, path_walls=path,
        cards=dict(cards), signed_url_names=dict(sig_names), azure_sas_names=azure, backstop_names=backstop, attempts=attempts,
        audit_missing=missing, unreadable_event_lines=unreadable,
        socks_uncounted=socks_wall_failures)


def build_from_files(rows: Sequence[EgressRow], events_path, duplicate_rows: int = 0,
                     socks_wall_failures: int = 0) -> LaunchReport:
    """`build` over the glue's event file; a missing or unreadable file reads as no lines (a crash before any)."""
    try:
        with open(events_path, "rb") as f:
            data = f.read().splitlines(keepends=True)
    except OSError:
        data = []
    return build(rows, data, duplicate_rows, socks_wall_failures)
