"""CH-D2 S7, the L3 r1 fix round (the head's triage `project_memory/1025-10/s7/HEAD_RULINGS_R1.md`, findings in
`project_memory/1025-6/s7/L3_r1.txt`).

  * codex MED 1 / 1025-6 ruling 1: `egress_state` is served, through a read-only `records.EgressReader` passed to
    `listing.entry_body` as `settled_lookup`; without one the body is as it was.
  * codex MED 2: an allow attempt in flight reads IN_FLIGHT, AMBIGUOUS only after a real stop.
  * codex MED 3: the bounds are on the display's JSON spelling (`"` and `\\` cost two).
  * codex MED 4 + complement LOW 4: `queue.file` checks an egress card whole, and its filing.
  * codex LOW 5: ENDED's words cover the client leaving and the launch or service closing.
  * complement MED 1: the agent's own `⟨` / `⟩` are marked; a bare one is refused.
  * complement LOW 2: a deny verdict over a pending or allowed row reads DENIED_OR_EXPIRED.
  * complement LOW 3: the page's hidden-character test is `unshowable`'s, code point by code point (node).
The page half (served states, the body cache, the decided row's header) is test_cards_states_page.py.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import sqlite3
import subprocess
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain import unshowable
from levain.chat_egress import cards, classify, parse, records as rec, strip, wire
from levain.chat_egress.cards import CardError, CardState, card_display, card_input, card_state, file_card
from levain.chat_egress.lifecycle import EGRESS_CARD_HOLD_S, Effect, Settled, Verdict
from levain.cockpit.consent import ConsentError
from levain.consent.listing import ConsentEntry, entry_body, filed_input
from levain.consent.queue import ConsentQueue, authority_source
from levain.consent.record import DISPLAY_MAX
from levain.coordinator.lease import LAUNCH_KINDS, _launch_event
from levain.coordinator.store import CoordStore
from tests.chat_egress.test_classify import frame, route_for
from tests.chat_egress.test_records import GET, make_launch
from tests._h4 import h4_home

JS = Path(cards.__file__).parents[1] / "templates" / "web" / "dashboard_core.js"
LAUNCH = "ln-" + "0" * 32


def _card(fr=None, **kw) -> dict:
    p = parse.parse(fr or frame(method="POST"))
    v = classify.decide(p, None, classify.GATED)
    return card_input(p, v, strip.strip(p).stripped, body_len=0, body_sha256=None, launch_id=kw.pop("launch", LAUNCH),
                      **kw)


def _queue(d: Path) -> ConsentQueue:
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return ConsentQueue(d, authority=authority_source(d, anchor=None), home=h4_home())


def _words() -> dict[str, str]:
    block = re.search(r"const EGRESS_WORDS = \{(.*?)\};", JS.read_text(), re.S).group(1)
    return dict(re.findall(r'^\s*(\w+): "([^"]*)"', block, re.M))


# --- ruling 1 / codex MED 1: egress_state is served from a read-only reader ----------------------------------------

def _store_with_launch(d: Path, *, live: bool = True) -> tuple[CoordStore, str]:
    store = CoordStore(d)
    lid = make_launch(store)
    conn = store._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = store._stamp()
        _launch_event(conn, now, lid, "pending", "test")
        _launch_event(conn, now, lid, "active", "test")
        if not live:
            _launch_event(conn, now, lid, "ended", "lost the lease")
        conn.execute("COMMIT")
    finally:
        conn.close()
    return store, lid


def _end_launch(store: CoordStore, lid: str) -> None:
    conn = store._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _launch_event(conn, store._stamp(), lid, "ended", "lost the lease")
        conn.execute("COMMIT")
    finally:
        conn.close()


def _entry(card: dict, state: str) -> ConsentEntry:
    r = file_card(_queue_for_entries(), card, agent_session_id="s", cwd="/w")
    return ConsentEntry(request=r, state=state, via=None, signer_fp=None, grant_id=None, decided_at=None)


_QDIR: list[Path] = []


def _queue_for_entries() -> ConsentQueue:
    return _queue(_QDIR[-1] / ("q" + uuid.uuid4().hex[:8]))


@pytest.fixture(autouse=True)
def _qdir(tmp_path):
    _QDIR.append(tmp_path)
    yield
    _QDIR.pop()


def test_entry_body_serves_what_the_egress_rows_say_through_a_read_only_lookup(tmp_path):
    """RED at 57cce6f3: entry_body took no lookup and served no egress_state (codex MED 1)."""
    store, lid = _store_with_launch(tmp_path / "consent")
    records = rec.EgressRecords(store)
    c = _card(launch=lid)
    records.write_attempt(lid, c["request"], Verdict.ALLOW, GET)
    records.write_outcome(lid, wire.ObsResponse(c["request"], 201, None, None), rec.CT_ABSENT)
    reader = rec.EgressReader(tmp_path / "consent")
    body = entry_body(_entry(c, "allowed"), settled_lookup=reader.reading)
    assert body["egress_state"] == "completed", body
    assert "egress_state" not in entry_body(_entry(_card(launch=lid), "allowed"))   # no lookup: as it was


def test_the_reader_never_creates_a_store_or_a_table(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    assert rec.EgressReader(d).reading(LAUNCH, str(uuid.uuid4())) == (None, False)
    assert not (d / "dispatch.db").exists()
    store, lid = _store_with_launch(d)                  # a dispatch store with no chat_egress_events yet
    before = sqlite3.connect(store.path).execute("SELECT name FROM sqlite_master ORDER BY name").fetchall()
    assert rec.EgressReader(d).reading(lid, str(uuid.uuid4())) == (None, False)
    after = sqlite3.connect(store.path).execute("SELECT name FROM sqlite_master ORDER BY name").fetchall()
    assert before == after and ("chat_egress_events",) not in after


def test_the_reader_opens_the_store_read_only(tmp_path, monkeypatch):
    store, lid = _store_with_launch(tmp_path / "consent")
    rec.EgressRecords(store).write_attempt(lid, rid := str(uuid.uuid4()), Verdict.DENY, GET)
    opened: list[str] = []
    real = sqlite3.connect

    def spy(target, *a, **k):
        opened.append(str(target))
        return real(target, *a, **k)
    monkeypatch.setattr(rec.sqlite3, "connect", spy)
    assert rec.EgressReader(tmp_path / "consent").reading(lid, rid) == (Settled.DENIED, False)
    assert opened and all(o.startswith("file:") and o.endswith("?mode=ro") for o in opened), opened


def test_an_unreadable_record_is_said_on_the_card_never_shown_as_the_queue_word(tmp_path):
    def broken(launch, request):
        raise sqlite3.DatabaseError("file is not a database")
    body = entry_body(_entry(_card(), "allowed"), settled_lookup=broken)
    assert "egress_state" not in body and "could not be read" in body["egress_note"], body


def test_another_tool_gets_no_egress_fields(tmp_path):
    q = _queue(tmp_path / "other")
    r = q.file(agent="claude_code", agent_session_id="s", tool_name="Bash", tool_input={"command": "ls"}, cwd="/w",
               hold=timedelta(minutes=5), event_id="x")
    e = ConsentEntry(request=r, state="pending", via=None, signer_fp=None, grant_id=None, decided_at=None)
    assert not {"egress_state", "egress_note"} & set(entry_body(e, settled_lookup=lambda a, b: (None, False)))


def test_the_dispatch_store_is_in_the_consent_directory_the_web_server_reads():
    from levain.consent.queue import default_queue_dir
    from levain.dispatch.jobs import default_dispatch_dir
    assert default_dispatch_dir() == default_queue_dir()


def test_the_live_launch_states_are_the_lease_modules_non_terminal_ones():
    assert set(rec._LIVE_LAUNCH) == set(LAUNCH_KINDS) - {"ended", "failed", "expired"}


# --- codex MED 2: in flight is not AMBIGUOUS ------------------------------------------------------------------------

def test_an_allow_in_flight_reads_in_flight_and_ambiguous_only_after_the_launch_stops(tmp_path):
    """RED at 57cce6f3: an ALLOW attempt with no end row read AMBIGUOUS ("Levain stopped") during a normal send."""
    store, lid = _store_with_launch(tmp_path / "consent")
    records = rec.EgressRecords(store)
    reader = rec.EgressReader(tmp_path / "consent")
    rid = str(uuid.uuid4())
    records.write_attempt(lid, rid, Verdict.ALLOW, GET)
    got = reader.reading(lid, rid)
    assert got == (Settled.AMBIGUOUS, True)
    assert card_state("allowed", *got[:1], in_flight=got[1]) is CardState.IN_FLIGHT
    _end_launch(store, lid)
    got = reader.reading(lid, rid)
    assert got == (Settled.AMBIGUOUS, False) and card_state("allowed", got[0], in_flight=got[1]) is CardState.AMBIGUOUS


def test_a_send_that_failed_is_ambiguous_even_while_its_launch_runs(tmp_path):
    store, lid = _store_with_launch(tmp_path / "consent")
    records = rec.EgressRecords(store)
    rid = str(uuid.uuid4())
    records.write_attempt(lid, rid, Verdict.ALLOW, GET)
    records.write_failed(lid, rid, Effect.SEND_ALLOW)
    assert rec.EgressReader(tmp_path / "consent").reading(lid, rid) == (Settled.AMBIGUOUS, False)


def test_an_outcome_and_an_ended_row_are_final_whatever_the_launch(tmp_path):
    store, lid = _store_with_launch(tmp_path / "consent")
    records = rec.EgressRecords(store)
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    for r in (a, b):
        records.write_attempt(lid, r, Verdict.ALLOW, GET)
    records.write_outcome(lid, wire.ObsResponse(a, 200, None, None), rec.CT_ABSENT)
    records.write_ended(lid, b)
    reader = rec.EgressReader(tmp_path / "consent")
    assert reader.reading(lid, a) == (Settled.COMPLETED, False)
    assert reader.reading(lid, b) == (Settled.ENDED, False)


def test_in_flight_only_changes_an_ambiguous_allowed_card():
    assert card_state("allowed", Settled.COMPLETED, in_flight=True) is CardState.COMPLETED
    assert card_state("pending", Settled.AMBIGUOUS, in_flight=True) is CardState.INCONSISTENT
    with pytest.raises(ValueError):
        card_state("allowed", Settled.AMBIGUOUS, in_flight=1)


# --- codex MED 3: bounds on the JSON spelling -----------------------------------------------------------------------

def test_quotes_and_backslashes_cannot_push_a_valid_card_over_the_display_limit():
    """RED at 57cce6f3: a card of maximal fields made of `"` serialized past DISPLAY_MAX and card_display refused it."""
    many = {f'x-"{i:03d}"' + '"\\' * 30: "v" for i in range(60)}
    p = parse.parse(frame(method="POST", path="/", query="a=1", headers=many))
    p = dataclasses.replace(p, path="/" + '"\\' * 1500, canonical_host='"' * 253,
                            params=tuple(dataclasses.replace(p.params[0], raw_key=f'k"{i:03d}' + '"' * 40)
                                         for i in range(60)))
    v = dataclasses.replace(classify.decide(p, None, classify.GATED), kind="card", method='"' * 40,
                            reattached='\\' * 300, why='override:"' + '"' * 60,
                            card_header_names=tuple(many))
    c = card_input(p, v, [f'cookie-"{i}' + '"' * 30 for i in range(190)], body_len=1 << 30, body_sha256="ab" * 32,
                   launch_id="L" * cards.LAUNCH_MAX)
    display = card_display(c)
    assert len(display) <= DISPLAY_MAX, len(display)
    assert filed_input(type("R", (), {"tool_name": cards.EGRESS_TOOL, "display": display})()) == c
    for k, limit in cards._LIMIT.items():
        for s in (c[k][:cards.NAMES_SHOWN] if isinstance(c[k], list) else [c[k]]):
            assert s is None or len(json.dumps(s, ensure_ascii=False)) - 2 <= limit, (k, s)


# --- codex MED 4 + complement LOW 4: the queue checks the card whole ------------------------------------------------

@pytest.mark.parametrize("change", [
    dict(scheme="gopher"), dict(hold="1 s"), dict(body_sha256="not-a-digest"), dict(request="not-a-request-id"),
    dict(port=0), dict(port=70000), dict(method="M" * 400), dict(why=""), dict(host=""),
    dict(stripped=[f"n{i}" for i in range(8)]), dict(header_names=["a", "b", "c", "d", "e", "f", "g"]),
    dict(query_keys=["n" * 31]), dict(launch="ln x"), dict(body_len=4, body_sha256=None),
    dict(path="/x⟨U+202E"), dict(path="/x⟩"), dict(path_chars=9, path_sha256="sha256:" + "ab" * 32),
])
def test_the_queue_refuses_a_card_whose_fields_are_not_a_cards(tmp_path, change):
    """RED at 57cce6f3: every one of these was filed (the check was key order, broad types and hidden characters)."""
    c = _card()
    bad: dict = {}
    for k, v in c.items():
        bad[k] = change.get(k, v)
        if k == "path":             # a cut path's two fields go after it, in the card's order
            bad.update({x: change[x] for x in ("path_chars", "path_sha256") if x in change})
    assert list(bad) == [k for k in cards._KEYS if k in bad]
    q = _queue(tmp_path / "q")
    with pytest.raises(ConsentError, match="must be a card"):
        q.file(agent="claude_code", agent_session_id="s", tool_name="levain_egress", tool_input=bad, cwd="/w",
               hold=timedelta(seconds=EGRESS_CARD_HOLD_S), tier="T2", event_id="chat:egress:" + c["request"])
    assert q.pending() == []


@pytest.mark.parametrize("filing", [
    dict(hold=timedelta(days=1)), dict(tier="T3"), dict(agent="codex"), dict(event_id="chat:egress:other"),
    dict(event_id=None)])
def test_the_queue_refuses_a_card_filed_with_another_hold_tier_agent_or_event(tmp_path, filing):
    """RED at 57cce6f3: a direct `queue.file` with a one-day hold showed the fixed 100 s hold line and was filed."""
    c = _card()
    args = dict(agent="claude_code", agent_session_id="s", tool_name="levain_egress", tool_input=c, cwd="/w",
                hold=timedelta(seconds=EGRESS_CARD_HOLD_S), tier="T2", event_id="chat:egress:" + c["request"])
    q = _queue(tmp_path / "q")
    with pytest.raises(ConsentError, match="must be a card"):
        q.file(**{**args, **filing})
    assert q.pending() == []
    assert q.file(**args).tool_name == "levain_egress"        # the same card, filed as file_card files it


# --- complement MED 1: the agent's own marker brackets ---------------------------------------------------------------

def test_a_literal_marker_in_agent_text_never_reads_as_a_real_one():
    """RED at 57cce6f3: `/x⟨U+202E⟩` from the agent was shown as itself, the same text as a real bidi override."""
    p = parse.parse(frame(method="POST", path="/p"))
    p = dataclasses.replace(p, path="/x⟨U+202E⟩")
    v = dataclasses.replace(classify.decide(p, None, classify.GATED), card_header_names=("x⟨U+0007⟩",))
    c = card_input(p, v, [], body_len=0, body_sha256=None, launch_id=LAUNCH)
    assert c["path"] == "/x⟨U+27E8⟩U+202E⟨U+27E9⟩", c["path"]
    assert "x⟨U+27E8⟩U+0007⟨U+27E9⟩" in c["header_names"]
    real = card_input(dataclasses.replace(p, path="/x‮"), v, [], body_len=0, body_sha256=None, launch_id=LAUNCH)
    assert real["path"] == "/x⟨U+202E⟩" != c["path"]
    card_display(c)                                                   # a marked bracket is a card's


def test_a_cut_never_splits_a_marked_bracket():
    text, cut = cards._bounded("a" * 20 + "⟨" * 20, 40)
    head = text.split("…(+")[0]
    assert cut and re.sub(r"⟨U\+[0-9A-F]{4,6}⟩", "", head).count("⟨") == 0, text


# --- complement LOW 2 and codex LOW 5: the words ----------------------------------------------------------------------

def test_a_deny_verdict_over_a_pending_or_allowed_row_reads_denied_or_expired():
    """RED at 57cce6f3: it read EXPIRED ("the hold ran out"), wrong when he had denied it."""
    for qs in ("pending", "allowed"):
        assert card_state(qs, Settled.DENIED) is CardState.DENIED_OR_EXPIRED
    assert _words()["denied_or_expired"] == "denied or expired; nothing was sent"


def test_ended_covers_a_client_leaving_and_the_launch_or_service_closing():
    """RED at 57cce6f3: UNDECIDED read ENDED, whose words said only that the client left."""
    assert card_state("pending", Settled.UNDECIDED) is CardState.ENDED
    w = _words()["ended"]
    assert "client left" in w and "launch" in w and "Levain closed" in w, w


def test_every_card_state_has_words_and_in_flight_is_not_ambiguous():
    w = _words()
    assert set(w) == {s.value for s in CardState} | {"unreadable"}     # page-only: a record the page could not read
    assert "stopped" not in w["in_flight"] and "stopped" in w["ambiguous"]


# --- complement LOW 3: the page's hidden-character test is unshowable's ------------------------------------------------

def _js_hidden_ranges() -> list[list[int]]:
    src = re.search(r"const EGRESS_HIDDEN = (/.*?/u);", JS.read_text()).group(1)
    prog = ("const re = new Function('return ' + process.argv[1])(); const out = []; let lo = -1;"
            "for (let cp = 0; cp <= 0x110000; cp++) { const hit = cp < 0x110000 && re.test(String.fromCodePoint(cp));"
            " if (hit && lo < 0) lo = cp; if (!hit && lo >= 0) { out.push([lo, cp - 1]); lo = -1; } }"
            "process.stdout.write(JSON.stringify(out));")
    run = subprocess.run([shutil.which("node"), "-e", prog, src], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_the_page_hidden_test_agrees_with_unshowable_on_every_code_point():
    """RED at 57cce6f3: the page's list missed U+2028/2029, U+00AD, U+034F, the tag characters, the variation
    selectors and the surrogates. Compared code point by code point, each alone; two kinds are left out on purpose:
    a combining mark (unshowable reads one alone as baseless, a context rule the server applies) and a code point
    this Python's Unicode leaves unassigned (the browser's Unicode may assign it)."""
    hit = set()
    for lo, hi in _js_hidden_ranges():
        hit.update(range(lo, hi + 1))
    wrong = []
    for cp in range(0x110000):
        cat = unicodedata.category(chr(cp))
        if cat in ("Mn", "Me", "Cn"):
            continue
        if (unshowable.first_hidden(chr(cp), selectors=False) is not None) != (cp in hit):
            wrong.append(f"U+{cp:04X} {cat}")
    assert not wrong, wrong[:40]


# --- the head's ruling on obs 4: the agent's own cut note, the same class as complement MED 1 ---------------------------

def test_a_literal_cut_note_in_agent_text_never_reads_as_a_real_cut():
    """RED at 9f6ddd69: a path holding a literal `…(+5 chars)…` was shown as itself, the same text as Levain's cut."""
    p = dataclasses.replace(parse.parse(frame(method="POST", path="/p")), path="/x…(+5 chars)…y")
    v = dataclasses.replace(classify.decide(p, None, classify.GATED), card_header_names=("a…b",), why="override:…")
    c = card_input(p, v, [], body_len=0, body_sha256=None, launch_id=LAUNCH)
    assert c["path"] == "/x⟨U+2026⟩(+5 chars)⟨U+2026⟩y" and "path_chars" not in c, c["path"]
    assert c["header_names"] == ["a⟨U+2026⟩b"] and c["why"] == "override:⟨U+2026⟩"
    long = card_input(dataclasses.replace(p, path="/" + "…" * 400), v, [], body_len=0, body_sha256=None,
                      launch_id=LAUNCH)
    assert re.sub(r"…\(\+[0-9]+ chars\)…", "", long["path"]).count("…") == 0, long["path"]   # one note, Levain's
    card_display(c)


def test_a_literal_more_entry_in_agent_names_never_reads_as_a_real_one():
    """The list form of the same class: a name spelled `(+5 more)` among two names read as one name and five more."""
    p = parse.parse(frame(method="POST", path="/p"))
    v = dataclasses.replace(classify.decide(p, None, classify.GATED), card_header_names=("a", "(+5 more)"))
    c = card_input(p, v, [], body_len=0, body_sha256=None, launch_id=LAUNCH)
    assert c["header_names"] == ["a", "⟨U+0028⟩+5 more)"], c["header_names"]


@pytest.mark.parametrize("change", [dict(path="/x…(+5 chars)…y"), dict(why="a…"), dict(header_names=["a", "(+5 more)"])])
def test_the_queue_refuses_an_unmarked_cut_note_or_more_entry(tmp_path, change):
    c = {**_card(), **change}
    q = _queue(tmp_path / "q")
    with pytest.raises(ConsentError, match="must be a card"):
        q.file(agent="claude_code", agent_session_id="s", tool_name="levain_egress", tool_input=c, cwd="/w",
               hold=timedelta(seconds=EGRESS_CARD_HOLD_S), tier="T2", event_id="chat:egress:" + c["request"])


def test_every_card_card_input_makes_passes_the_check():
    """The cut-note rule refuses a note on a short text; it must never refuse a card `card_input` made. Every field
    at every length around its bound, from pieces that cost one, two (`"`) and ten (`⟨U+10FFFF⟩` from U+10FFFF)."""
    import random
    rnd = random.Random(7)
    pieces = ["a", '"', "\\", "\U0010ffff", "‮", "…", "⟨", "x" * 3]
    base = parse.parse(frame(method="POST", path="/p", query="a=1"))
    for n in range(1, 400, 3):
        for _ in range(3):
            text = "".join(rnd.choice(pieces) for _ in range(n))
            p = dataclasses.replace(base, path="/" + text, canonical_host="h" + text,
                                    params=(dataclasses.replace(base.params[0], raw_key="k" + text),))
            v = dataclasses.replace(classify.decide(base, None, classify.GATED), method="M" + text,
                                    why="w" + text, reattached="r" + text, card_header_names=("n" + text,))
            c = card_input(p, v, ["s" + text], body_len=0, body_sha256=None, launch_id=LAUNCH)
            cards._check(c)                                                     # raises on a refusal
