"""CH-D2 S7: the `levain_egress` card (chat_egress.cards) and the queue's two edits for it.

Rows: R15-10-A16 (control / bidi / long path; the removed names and the key line never pushed off), R15-23-5 and
R15-22-3 (query NAMES as sent, never a value), R15-24-2 (no `x-levain` name among the removed), K3 (`reattached`),
R15-13-Q4 (the effective method), C9 / R15-10-A3 (the card says the tool call ends at its timeout), R15-10-A8 and C0
(ENDED, AMBIGUOUS, FAILED as the card reads them), Q-R15-7 (only a signed answer decides one: no standing grant).
The page half (a real card drawn by the real cockpit) is test_cards_page.py.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest

from levain import unshowable
from levain.chat_egress import cards, classify, parse, strip
from levain.chat_egress.cards import CardError, CardState, card_display, card_input, card_state, file_card
from levain.chat_egress.lifecycle import EGRESS_CARD_HOLD_S, Settled
from levain.cockpit.consent import ConsentError
from levain.consent import queue as queue_mod
from levain.consent.listing import UNSHOWN, entry_body, filed_input, read_listing
from levain.consent.queue import ConsentQueue, authority_source
from levain.consent.record import DISPLAY_MAX, _secret_named
from tests.chat_egress.test_classify import frame, route_for
from tests._h4 import h4_home

LAUNCH = "launch-7f3a"
SHA = "ab" * 32


def _card(fr, *, keyed=False, mode=classify.GATED, body_len=0, body_sha256=None, verdict=None, parsed=None, **kw):
    p = parsed if parsed is not None else parse.parse(fr)
    v = verdict if verdict is not None else classify.decide(p, route_for(p) if keyed else None, mode)
    assert v.kind == "card", v
    return card_input(p, v, strip.strip(p).stripped, body_len=body_len, body_sha256=body_sha256, launch_id=LAUNCH,
                      **kw)


def _queue(tmp_path: Path) -> ConsentQueue:
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return ConsentQueue(d, authority=authority_source(d, anchor=None), home=h4_home())


def _shown(card: dict) -> list[str]:
    return [s for v in card.values() for s in (v if isinstance(v, list) else [v]) if isinstance(s, str)]


# --- R15-10-A16: the long path ------------------------------------------------------------------------------------

def test_a_long_path_cannot_push_the_removed_names_or_the_key_off_and_the_card_stays_answerable(tmp_path):
    """RED first (reproduced on the queue as it stood): the queue's sorted, cut display put `path` before
    `reattached` / `stripped` / `why`, and a 2,500-character path cut them off the display and made it unanswerable."""
    long_path = "/up/" + "a" * 2500 + "/delete"
    c = _card(frame(method="POST", host="api.github.com", path=long_path, headers={"authorization": "Bearer X"}),
              keyed=True, body_len=3, body_sha256=SHA)
    keys = list(c)
    for k in ("method", "host", "reattached", "stripped", "why", "hold"):
        assert keys.index(k) < keys.index("path"), keys
    assert c["path"].startswith("/up/aaa") and c["path"].endswith("a/delete") and "…(+" in c["path"], c["path"]
    assert len(c["path"]) <= cards.PATH_HEAD + cards.PATH_TAIL
    assert c["path_chars"] == len(long_path)
    assert c["path_sha256"] == "sha256:" + hashlib.sha256(long_path.encode()).hexdigest()
    r = file_card(_queue(tmp_path), c, agent_session_id="s", cwd="/w")
    assert len(r.display) <= DISPLAY_MAX
    shown = json.loads(r.display.split(" ", 1)[1])
    assert shown["stripped"] == ["authorization"] and shown["reattached"] == "api.github.com"
    assert list(shown) == keys                                          # the display keeps the card's order
    assert filed_input(r) == c                                          # read back whole: the terminal can answer
    entry = read_listing(tmp_path / "consent", now=_now()).pending[0]
    assert entry_body(entry)["answer_allow"]


def test_a_short_path_is_never_cut_and_carries_no_path_digest():
    c = _card(frame(method="POST", path="/repos/o/r/issues"))
    assert c["path"] == "/repos/o/r/issues" and "path_chars" not in c and "path_sha256" not in c


def test_the_worst_case_card_is_still_under_the_display_limit():
    many = {f"x-h{i:03d}-" + "n" * 40: "v" for i in range(60)}
    q = "&".join(f"k{i:03d}" + "q" * 40 + "=v" for i in range(60))
    p = parse.parse(frame(method="POST", path="/" + "p" * 3000, query=q, headers=many))
    p = dataclasses.replace(p, canonical_host="h" * 63 + "." + "i" * 63 + "." + "j" * 63 + "." + "k" * 61)
    # the rig's sizes wall in the classifier; the display bound is the card's, so the verdict is set to a card
    v = dataclasses.replace(classify.decide(p, None, classify.GATED), kind="card", method="‮" * 40,
                            reattached="r" * 300, why="override:" + "⁦" * 40)
    c = card_input(p, v, [f"cookie-{i}" + "​" * 30 for i in range(190)], body_len=1 << 30, body_sha256=SHA,
                   launch_id="L" * cards.LAUNCH_MAX)
    assert len(card_display(c)) <= DISPLAY_MAX
    assert c["stripped"][-1] == f"(+{190 - cards.NAMES_SHOWN} more)" and len(c["stripped"]) == cards.NAMES_SHOWN + 1
    assert c["header_names"][-1].startswith("(+") and c["query_keys"][-1].startswith("(+")


# --- R15-10-A16: control and bidi characters ------------------------------------------------------------------------

HOSTILE = "‮evil⁦\x07\x1b[2J​﻿"


def _hostile_card(bad: str) -> dict:
    p = parse.parse(frame(method="POST", path="/p", query="a=1"))
    p = dataclasses.replace(
        p, path="/p" + bad, canonical_host="x" + bad,
        params=(dataclasses.replace(p.params[0], raw_key="k" + bad),))
    v = dataclasses.replace(classify.decide(parse.parse(frame(method="POST")), None, classify.GATED),
                            method="P" + bad, card_header_names=("x" + bad,), reattached="r" + bad)
    return card_input(p, v, ["s" + bad], body_len=0, body_sha256=None, launch_id=LAUNCH)


AGENT_FIELDS = ("method", "host", "path", "reattached", "stripped", "header_names", "query_keys")


def _field(c: dict, k: str) -> str:
    return c[k][0] if isinstance(c[k], list) else c[k]


def test_a_bidi_override_and_a_control_in_every_agent_field_show_as_markers_never_raw_or_dropped():
    c = _hostile_card("‮\x07")
    for k in AGENT_FIELDS:
        assert _field(c, k).endswith("⟨U+202E⟩⟨U+0007⟩"), (k, _field(c, k))


def test_a_long_run_of_hidden_characters_is_marked_and_a_field_it_overruns_says_it_was_cut():
    c = _hostile_card(HOSTILE)
    for s in _shown(c):
        assert unshowable.first_hidden(s, selectors=False) is None, s
    for k in AGENT_FIELDS:
        f = _field(c, k)
        assert "⟨U+202E⟩evil⟨U+2066⟩⟨U+0007⟩⟨U+001B⟩[2J⟨U+200B⟩⟨U+FEFF⟩" in f or "…(+" in f, (k, f)
    assert "…(+" in c["method"] and c["method"] != "P"              # a cut method never reads as a plain one
    display = card_display(c)
    assert not re.search(r"\\u00[0-9a-f]{2}", display)                  # nothing the page's JSON.parse could decode
    assert not any(ord(ch) < 0x20 or 0x7f <= ord(ch) <= 0x9f for ch in display)


def test_a_cut_never_splits_a_marker():
    text, cut = cards._bounded("a" * 10 + "‮" * 20, 30)
    assert cut and "…(+" in text
    head = text.split("…(+")[0]
    assert head.count("⟨") == head.count("⟩"), text


def test_markup_in_a_field_is_carried_as_text_for_the_page_to_draw_as_text():
    v = dataclasses.replace(classify.decide(parse.parse(frame(method="POST")), None, classify.GATED),
                            method="<img src=x onerror=alert(1)>")
    c = card_input(parse.parse(frame(method="POST")), v, [], body_len=0, body_sha256=None, launch_id=LAUNCH)
    assert c["method"].startswith("<img src") and "…(+" in c["method"]     # carried as text, its cut said
    assert "<img" in card_display(c)                                    # the page draws it with textContent (page test)


# --- names, never values (R15-23-5, R15-22-3, R15-24-2) and the other fields ---------------------------------------

def test_the_query_names_are_on_the_card_as_sent_and_no_value_is_anywhere_on_it():
    c = _card(frame(method="POST", path="/p", query="q=SECRETQUERYVALUE&%71x=2&q=again",
                    headers={"authorization": "Bearer TOPSECRETTOKEN", "cookie": "s=COOKIEVALUE",
                             "x-levain-thing": "1", "content-type": "text/plain"}))
    assert c["query_keys"] == ["q", "%71x"]
    display = card_display(c)
    for value in ("SECRETQUERYVALUE", "again", "TOPSECRETTOKEN", "COOKIEVALUE", "text/plain"):
        assert value not in display, value
    assert c["stripped"] == ["authorization", "cookie"]                 # removed names, in the order carried
    assert not any(n.startswith("x-levain") for n in c["stripped"])     # R15-24-2
    assert {"authorization", "cookie", "content-type"} <= set(c["header_names"])


def test_a_keyed_write_shows_the_key_line_and_an_unkeyed_one_shows_none():
    assert _card(frame(method="POST", host="api.github.com"), keyed=True)["reattached"] == "api.github.com"
    assert _card(frame(method="POST", host="api.github.com"))["reattached"] is None


def test_an_override_shows_the_effective_method_and_its_why():
    c = _card(frame(method="GET", headers={"x-http-method-override": "DELETE"}))
    assert c["method"] == "DELETE" and c["why"] == "override:x-http-method-override"


def test_the_card_says_the_hold_and_that_the_tool_call_ends_at_its_timeout():
    c = _card(frame(method="POST"))
    assert c["hold"].startswith(f"{EGRESS_CARD_HOLD_S} s") and "tool call ends at its own timeout" in c["hold"]


def test_the_body_digest_and_the_ids_are_the_cards():
    c = _card(frame(method="POST"), body_len=5, body_sha256=SHA)
    assert (c["body_len"], c["body_sha256"]) == (5, "sha256:" + SHA)
    assert c["launch"] == LAUNCH and c["request"] == parse.parse(frame(method="POST")).request_id
    assert not any(_secret_named(k) for k in c)                         # the queue would mask a secret-named key


@pytest.mark.parametrize("bad", [
    dict(body_len=3, body_sha256=None), dict(body_len=-1, body_sha256=None), dict(body_len=0, body_sha256="AB" * 32),
    dict(body_len=1, body_sha256="ab" * 31), dict(body_len=True, body_sha256=None)])
def test_a_malformed_body_pair_is_refused(bad):
    with pytest.raises(CardError):
        _card(frame(method="POST"), **bad)


def test_only_a_card_verdict_with_a_request_id_files_a_card():
    p = parse.parse(frame(method="GET"))
    with pytest.raises(CardError, match="card verdict"):
        card_input(p, classify.decide(p, None, classify.GATED), [], body_len=0, body_sha256=None, launch_id=LAUNCH)
    fr = frame(method="POST")
    del fr["headers"]["x-levain-audit"]
    p = parse.parse(fr)
    with pytest.raises(CardError, match="request id"):
        card_input(p, classify.decide(p, None, classify.GATED), [], body_len=0, body_sha256=None, launch_id=LAUNCH)


# --- the queue: filing (queue.py edit 1) ----------------------------------------------------------------------------

def test_file_card_files_one_t2_request_per_egress_request(tmp_path):
    q = _queue(tmp_path)
    c = _card(frame(method="POST"))
    r = file_card(q, c, agent_session_id="sess", cwd="/w")
    assert (r.agent, r.tool_name, r.tier) == ("claude_code", "levain_egress", "T2")
    assert r.event_id == "hook:chat:egress:" + c["request"]
    from datetime import datetime
    held = datetime.fromisoformat(r.expires_at.replace("Z", "+00:00")) - \
        datetime.fromisoformat(r.received_at.replace("Z", "+00:00"))
    assert held == timedelta(seconds=EGRESS_CARD_HOLD_S)
    with pytest.raises(ConsentError, match="filed once"):
        file_card(q, c, agent_session_id="sess", cwd="/w")


def test_the_queue_refuses_an_egress_request_that_is_not_a_card(tmp_path):
    q = _queue(tmp_path)
    c = _card(frame(method="POST"))
    raw = dict(c, method="POST‮")                                  # an unmarked bidi character
    reordered = {k: c[k] for k in reversed(c)}
    for bad in (raw, reordered, {"host": "x", "method": "POST"}, dict(c, extra=1), dict(c, port="443"), ["no"]):
        with pytest.raises(ConsentError, match="must be a card"):
            q.file(agent="claude_code", agent_session_id="s", tool_name="levain_egress", tool_input=bad, cwd="/w",
                   hold=timedelta(seconds=100))
    assert q.pending() == []


def test_the_queue_constant_is_the_cards():
    assert queue_mod.EGRESS_TOOL == cards.EGRESS_TOOL


# --- Q-R15-7: no standing grant decides a card (queue.py edit 2) ----------------------------------------------------

class _Policy:
    grants = {"g1": object()}


class _Authority:
    policy = _Policy()

    def grant_link(self, gid):
        return (1, "h")


def _grant_everything(q, monkeypatch):
    monkeypatch.setattr(q, "_fresh_authority", lambda: _Authority())
    monkeypatch.setattr(q, "_matching_grant", lambda *a, **k: ("g1", 1, "h"))
    monkeypatch.setattr(queue_mod, "grant_refusal", lambda *a, **k: None)


def test_a_grant_that_covers_everything_answers_a_shell_call_but_never_an_egress_card(tmp_path, monkeypatch):
    q = _queue(tmp_path)
    shell = q.file(agent="claude_code", agent_session_id="s", tool_name="Bash", tool_input={"command": "ls"},
                   cwd="/w", hold=timedelta(seconds=100), event_id="sh")
    c = _card(frame(method="POST"))
    card = file_card(q, c, agent_session_id="s", cwd="/w")
    _grant_everything(q, monkeypatch)
    assert q.answer_by_grant(shell.id, tool_input={"command": "ls"}, posture="p") is not None   # the rig reaches
    assert q.answer_by_grant(card.id, tool_input=c, posture="p") is None
    assert [r.id for r in q.pending()] == [card.id]


def test_a_grant_answer_written_raw_onto_an_egress_card_delivers_nothing(tmp_path, monkeypatch):
    from levain.consent.record import grant_answer_payload
    q = _queue(tmp_path)
    c = _card(frame(method="POST"))
    card = file_card(q, c, agent_session_id="s", cwd="/w")
    shell = q.file(agent="claude_code", agent_session_id="s", tool_name="Bash", tool_input={"command": "ls"},
                   cwd="/w", hold=timedelta(seconds=100), event_id="sh")
    _grant_everything(q, monkeypatch)
    row = {"state": "allowed", "signer_fp": None, "decided_at": card.received_at}
    for req, ti, want in ((shell, {"command": "ls"}, True), (card, c, False)):
        payload = grant_answer_payload(queue_id=q.queue_id, request=req, grant_id="g1", grant_seq=1, grant_hash="h",
                                       posture="p")
        got = q._grant_decision(row, req, q.queue_id, payload, tool_input=ti, posture="p")
        assert (got is not None) is want, (req.tool_name, got)


# --- what the card says it became -----------------------------------------------------------------------------------

QUEUE_STATES = ("pending", "allowed", "denied", "expired", "withdrawn")
I = CardState.INCONSISTENT
TABLE = {
    # settled:          pending              allowed              denied               expired              withdrawn
    None:               (CardState.PENDING,  CardState.ALLOWED,   CardState.DENIED,    CardState.EXPIRED,   CardState.ENDED),
    Settled.COMPLETED:  (I,                  CardState.COMPLETED, I,                   I,                   I),
    Settled.AMBIGUOUS:  (I,                  CardState.AMBIGUOUS, I,                   I,                   I),
    Settled.ENDED:      (CardState.ENDED,) * 5,
    Settled.UNDECIDED:  (CardState.ENDED,) * 5,
    Settled.DENIED:     (CardState.DENIED_OR_EXPIRED, CardState.DENIED_OR_EXPIRED, CardState.DENIED, CardState.EXPIRED, I),
    Settled.FAILED:     (CardState.FAILED,) * 5,
    Settled.INCONSISTENT: (I,) * 5,
}


def test_card_state_reads_every_queue_state_against_every_settled_reading():
    assert set(TABLE) == {None, *Settled}
    for settled, row in TABLE.items():
        for qs, want in zip(QUEUE_STATES, row):
            assert card_state(qs, settled) is want, (qs, settled)
        assert card_state("undecided", settled) is I


def test_card_state_refuses_what_it_does_not_know():
    with pytest.raises(ValueError):
        card_state("allowd", None)
    with pytest.raises(ValueError):
        card_state("allowed", "ambiguous")


def test_the_page_draws_words_for_every_card_state_and_reads_the_queue_state_as_card_state_does():
    js = (Path(cards.__file__).parents[1] / "templates" / "web" / "dashboard_core.js").read_text()
    words = re.search(r"const EGRESS_WORDS = \{(.*?)\};", js, re.S).group(1)
    # The page alone draws "unreadable" (an egress record it could not read; S7 header fix r2): no CardState is it.
    assert set(re.findall(r"^\s*(\w+):", words, re.M)) == {s.value for s in CardState} | {"unreadable"}
    from_queue = dict(re.findall(r'(\w+): "(\w+)"', re.search(r"const EGRESS_FROM_QUEUE = \{(.*?)\};", js).group(1)))
    assert from_queue == {qs: card_state(qs, None).value for qs in QUEUE_STATES}
    assert re.search(r'const EGRESS_TOOL = "levain_egress";', js)


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)
