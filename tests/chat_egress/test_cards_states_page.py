"""CH-D2 S7 L3 r1 fix round, the page half: what a card became, SERVED by the real web server from real egress rows.

A real consent queue answered by real signed answers, a real dispatch store with real `chat_egress_events` and launch
events, the real web server, the cockpit page in headless Chrome at phone width. Nothing in the page is rewritten: the
`egress_state` each card shows is the server's (`listing.entry_body` with `records.EgressReader`).

  * codex MED 1 / ruling 1: PENDING, IN FLIGHT, COMPLETED, AMBIGUOUS, DENIED, EXPIRED and ENDED, each from its rows.
  * codex MED 2: the body cache keeps only a final egress_state: a pending card whose rows end while it is up reads
    ENDED in its focus view (the same id and queue state that the board's read was cached under).
  * 1025-6 ruling 5: a decided egress row's header says the card's state word before its details are read.

`LEVAIN_S7_SHOTS=<dir>` keeps one phone-width screenshot per state there (the default is the test's tmp dir). How the
card reads to him on his phone is S10's (residue, not here).
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.chat_egress import classify, parse, records as rec, strip, wire
from levain.chat_egress.cards import card_input, file_card
from levain.chat_egress.lifecycle import Verdict
from levain.consent.queue import ConsentQueue
from levain.coordinator.lease import _launch_event
from levain.coordinator.store import CoordStore
from tests.chat_egress.test_classify import frame
from tests.chat_egress.test_records import GET, make_launch
from tests.test_cockpit_consent_rows import OPEN_DECISIONS
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_consent_queue import Key, Log, signed
from tests._h4 import h4_home

NARROW = {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}
needs_ssh = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen signs the answers")


def _card(lid: str, host: str) -> dict:
    p = parse.parse(frame(method="POST", host=host, path="/repos/o/r/issues",
                          headers={"authorization": "Bearer X", "x-levain-audit": str(uuid.uuid4())}))
    v = classify.decide(p, None, classify.GATED)
    return card_input(p, v, strip.strip(p).stripped, body_len=12, body_sha256="ab" * 32, launch_id=lid)


def _launch(store: CoordStore, *, live: bool) -> str:
    lid = make_launch(store)
    conn = store._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = store._stamp()
        for kind in ("pending", "active") + (() if live else ("ended",)):
            _launch_event(conn, now, lid, kind, "lost the lease" if kind == "ended" else "test")
        conn.execute("COMMIT")
    finally:
        conn.close()
    return lid


def _row(rid: str) -> str:
    return f"document.querySelector('#levain-modal .consent-row[data-card=\"consent:{rid}\"]')"


@needs_browser
@needs_ssh
def test_every_card_state_is_served_from_the_rows_and_drawn(tmp_path, monkeypatch):
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    consent = home / "consent"
    consent.mkdir(parents=True, mode=0o700)
    keydir = tmp_path / "keys"
    keydir.mkdir()
    (tmp_path / "auth").mkdir()
    log = Log(tmp_path / "auth", {"laptop": Key(keydir, "laptop")})
    log.enrol("laptop", "laptop-presence")
    q = ConsentQueue(consent, authority=log.source, home=h4_home())
    store = CoordStore(consent)
    records = rec.EgressRecords(store)
    live, stopped = _launch(store, live=True), _launch(store, live=False)

    def filed(lid: str, host: str, queue: ConsentQueue = q):
        c = _card(lid, host)
        return c, file_card(queue, c, agent_session_id="s", cwd="/w")

    def answer(r, decision: str) -> None:
        payload, sig = signed(q, r, log.keys["laptop"], decision)
        q.answer(r.id, payload, sig)

    cards: dict[str, tuple[dict, object]] = {}
    cards["pending"] = filed(live, "pending.example")
    for name, lid in (("in_flight", live), ("completed", live), ("ambiguous", stopped)):
        cards[name] = filed(lid, name.replace("_", "-") + ".example")
        answer(cards[name][1], "allow")
        records.write_attempt(lid, cards[name][0]["request"], Verdict.ALLOW, GET)
    records.write_outcome(live, wire.ObsResponse(cards["completed"][0]["request"], 201, None, None), rec.CT_ABSENT)
    cards["denied"] = filed(live, "denied.example")
    answer(cards["denied"][1], "deny")
    records.write_attempt(live, cards["denied"][0]["request"], Verdict.DENY, GET)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)       # filed five minutes ago: its 100 s hold ran out
    cards["expired"] = filed(live, "expired.example", ConsentQueue(consent, authority=log.source, clock=lambda: past, home=h4_home()))
    records.write_attempt(live, cards["expired"][0]["request"], Verdict.DENY, GET)
    cards["ended"] = filed(live, "ended.example")
    assert q.withdraw(cards["ended"][1].id, note="egress: the client closed")   # C0's CARD_ENDED
    records.write_attempt(live, cards["ended"][0]["request"], Verdict.UNDECIDED, GET)
    records.write_ended(live, cards["ended"][0]["request"])

    # the pending card's client leaves right after the board first reads its BODY: the body route's next read says
    # ENDED. (The decisions listing reads the rows too, for its row words; only the body route's reading is tripped.)
    pending_req = cards["pending"][0]["request"]
    from levain.web_server import _Handler
    real_lookup = _Handler._egress_lookup
    left: list[int] = []

    def lookup(self):
        real = real_lookup(self)

        def reading(launch_id, request_id):
            got = real(launch_id, request_id)
            if request_id == pending_req and not left:
                left.append(1)
                records.write_attempt(launch_id, request_id, Verdict.UNDECIDED, GET)
                records.write_ended(launch_id, request_id)
            return got
        return reading
    monkeypatch.setattr(_Handler, "_egress_lookup", lookup)

    shots = Path(os.environ.get("LEVAIN_S7_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    pid = cards["pending"][1].id
    board = (f"document.querySelector('#ck-needs .needs-consent[data-needs-id=\"consent:{pid}\"]')")
    state_of = "(n) => { const s = n && n.querySelector('.egress-state'); return s ? s.dataset.egressState : null; }"
    decided = [n for n in cards if n != "pending"]
    steps = [
        NARROW,
        {"until": f"{board} && {board}.querySelector('.egress-state')"},
        {"eval": f"(() => {{ {board}.scrollIntoView({{block: 'start'}}); return ({state_of})({board}); }})()"},
        {"shot": str(shots / "s7r1_pending.png")},
        {"eval": f"(() => {{ {board}.querySelector('.needs-open').click(); return 1; }})()"},
        {"until": "document.querySelector('#levain-modal .needs-consent.is-full .egress-state')"},
        {"eval": "new Promise((res) => setTimeout(() => res(null), 500))"},     # the modal's fade-in, for the shot
        {"eval": f"({state_of})(document.querySelector('#levain-modal .needs-consent.is-full'))"},
        {"shot": str(shots / "s7r1_pending_then_ended_focus.png")},
        {"eval": "(() => { document.querySelector('#levain-modal .modal-close').click(); return 1; })()"},
        {"until": "!document.getElementById('levain-modal')"},
        {"eval": OPEN_DECISIONS},
        {"until": " && ".join(f"{_row(cards[n][1].id)}" for n in decided)},
        # ruling 5: each decided row's header word, before any details are read
        {"eval": "JSON.stringify({" + ", ".join(
            f"{n}: ({_row(cards[n][1].id)}.querySelector('.consent-head .consent-audit') || {{}}).textContent"
            for n in decided) + "})"},
    ]
    for n in decided:
        steps.append({"eval": f"(() => {{ {_row(cards[n][1].id)}.querySelector('.consent-details').click(); return 1; }})()"})
        steps.append({"until": f"{_row(cards[n][1].id)}.querySelector('.egress-state')"})
    steps.append({"eval": "JSON.stringify({" + ", ".join(
        f"{n}: [({state_of})({_row(cards[n][1].id)}), {_row(cards[n][1].id)}.querySelector('.consent-head .consent-audit')"
        f".textContent, {_row(cards[n][1].id)}.querySelector('.egress-state').textContent]" for n in decided) + "})"})
    for n in decided:
        # the row at the top of the modal, its header clear of the sticky filter bar
        steps.append({"eval": f"new Promise((res) => {{ const r = {_row(cards[n][1].id)}; r.scrollIntoView({{block: 'start'}});"
                              " let p = r.parentElement; while (p && p.scrollHeight <= p.clientHeight) p = p.parentElement;"
                              " if (p) p.scrollTop -= 72; setTimeout(() => res(1), 400); })"})
        steps.append({"shot": str(shots / f"s7r1_{n}.png")})
    with _serve(_install(tmp_path)[1], surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", steps)["values"]
    vals = [v for v in out if v is not None]
    assert "pending" in vals, out                                   # the board's card, as first read
    assert left, "the server never read the pending card's rows"
    assert "ended" in vals, out                                     # its focus view: read again, not the cached body
    heads = json.loads(next(v for v in vals if isinstance(v, str) and v.startswith('{"in_flight"') and "[" not in v))
    assert heads == {"in_flight": "IN FLIGHT", "completed": "COMPLETED", "ambiguous": "AMBIGUOUS", "denied": "DENIED",
                     "expired": "EXPIRED", "ended": "ENDED"}, heads   # the card's word from the listing, not the queue's
    drawn = json.loads(next(v for v in vals if isinstance(v, str) and v.startswith('{"in_flight":[')))
    want = {"in_flight": ("in_flight", "IN FLIGHT"), "completed": ("completed", "COMPLETED"),
            "ambiguous": ("ambiguous", "AMBIGUOUS"), "denied": ("denied", "DENIED"), "expired": ("expired", "EXPIRED"),
            "ended": ("ended", "ENDED")}
    for n, (state, label) in want.items():
        assert drawn[n][0] == state and drawn[n][1] == label and drawn[n][2].startswith(label), (n, drawn[n])


@needs_browser
@needs_ssh
def test_a_card_whose_dispatch_store_cannot_be_read_heads_unreadable(tmp_path, monkeypatch):
    # L3 r1 ruling 3: the header says UNREADABLE, never the queue's ALLOWED, for a card whose record cannot be read
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    consent = home / "consent"
    consent.mkdir(parents=True, mode=0o700)
    keydir = tmp_path / "keys"
    keydir.mkdir()
    (tmp_path / "auth").mkdir()
    log = Log(tmp_path / "auth", {"laptop": Key(keydir, "laptop")})
    log.enrol("laptop", "laptop-presence")
    q = ConsentQueue(consent, authority=log.source, home=h4_home())
    live = _launch(CoordStore(consent), live=True)
    c = _card(live, "unreadable.example")
    r = file_card(q, c, agent_session_id="s", cwd="/w")
    payload, sig = signed(q, r, log.keys["laptop"], "allow")
    q.answer(r.id, payload, sig)
    (consent / "dispatch.db").write_bytes(b"this is not a sqlite file" * 400)
    steps = [NARROW, {"eval": OPEN_DECISIONS}, {"until": _row(r.id)},
             {"eval": f"{_row(r.id)}.querySelector('.consent-head .consent-audit').textContent"}]
    with _serve(_install(tmp_path)[1], surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", steps)["values"]
    assert "UNREADABLE" in out, out


@needs_browser
@needs_ssh
def test_every_card_state_and_unreadable_heads_its_own_label(tmp_path, monkeypatch):
    # L3 r2 (codex 6, complement LOW 1): every word the listing can serve goes through the real header draw. One decided
    # row per word, each carrying that egress_state as the listing would serve it (the word is set per row here; how
    # the rows decide it is the first test above), each allowed in the queue so a fallback to the queue word reads ALLOWED.
    from levain.chat_egress.cards import CardState
    from levain.consent import listing

    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    consent = home / "consent"
    consent.mkdir(parents=True, mode=0o700)
    keydir = tmp_path / "keys"
    keydir.mkdir()
    (tmp_path / "auth").mkdir()
    log = Log(tmp_path / "auth", {"laptop": Key(keydir, "laptop")})
    log.enrol("laptop", "laptop-presence")
    q = ConsentQueue(consent, authority=log.source, home=h4_home())
    live = _launch(CoordStore(consent), live=True)
    words = [s.value for s in CardState if s is not CardState.PENDING] + [listing.UNREADABLE]   # a pending row carries none
    rows: dict[str, object] = {}
    by_request: dict[str, str] = {}
    for w in words:
        c = _card(live, w.replace("_", "-") + ".example")
        r = file_card(q, c, agent_session_id="s", cwd="/w")
        payload, sig = signed(q, r, log.keys["laptop"], "allow")
        q.answer(r.id, payload, sig)
        rows[w] = r
        by_request[c["request"]] = w
    monkeypatch.setattr(listing, "egress_state_of",
                        lambda entry, lookup: by_request[listing.filed_input(entry.request)["request"]])
    more = "document.querySelector('#levain-modal .xmore')"      # the modal draws the first ten rows, then "show all N"
    steps = [NARROW, {"eval": OPEN_DECISIONS},
             {"until": more},
             {"eval": f"(() => {{ {more}.click(); return 1; }})()"},
             {"until": " && ".join(_row(rows[w].id) for w in words)},
             {"eval": "JSON.stringify({" + ", ".join(
                 f"{w}: ({_row(rows[w].id)}.querySelector('.consent-head .consent-audit') || {{}}).textContent"
                 for w in words) + "})"}]
    with _serve(_install(tmp_path)[1], surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", steps)["values"]
    heads = json.loads(next(v for v in out if isinstance(v, str) and v.startswith("{") and "unreadable" in v))
    assert heads == {w: w.replace("_", " ").upper() for w in words}, heads
