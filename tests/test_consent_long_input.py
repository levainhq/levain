"""Row 1704: a long consent input is shown whole and signed from the phone.

Reproduced red before the build (ds2 tip ab7732e35): a ``Bash`` request whose input passed 2000 characters was filed
with its display cut at that limit, the cockpit sheet read ``refused: "its input is too long to show here, so it is not
signed from this page; the agent's own dialog stands"`` and offered no answer, so any long command sent the operator to
the laptop. The rule behind the cut stays (a signer reads all of what the signature covers); the cut is gone: the queue
keeps the whole redacted input up to a named bound in bytes, past it the display names the size (never a prefix), and a
request filed under the old cut stays refused in words that say so.

The sheet tests run the phone port's own consent steps through the cockpit server against a real authority log, a
software passkey and real ``ssh-keygen`` verification (the rig of tests/test_cockpit_consent_sheet.py)."""
from __future__ import annotations

import json
import os
import shutil
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from levain import authority as auth
from levain.consent import record
from levain.consent.answering import passkey_keys, refuse_cut_display
from levain.consent.listing import UNSHOWN, entry_body, filed_input, find_entry, read_listing, unshown_why
from levain.consent.passkey import challenge
from levain.consent.queue import AnswerRefused, ConsentQueue, authority_source
from levain.consent.record import (DISPLAY_MAX, LEGACY_DISPLAY_CUT, answer_message, redact_display,
                                   tool_input_digest)
from levain.consent.sign import Signer
from tests._h4 import h4_home
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_sign_routes import _call, _serving
from tests.test_dispatch_phone import _b64u
from tests.test_grant_phone import RP, world  # noqa: F401  (world is a fixture)

NS = "flow-needs-phill"
PAGE = f"https://{RP}"
T0 = datetime(2026, 11, 12, 22, 30, 0, tzinfo=timezone.utc)
needs_ssh = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def _long_command(size: int, *, at: int | None = None, mark: str = "") -> str:
    """A long shell command of the shape that hit the defect (a ``sed -i`` over a heredoc-fed log append), ``size``
    characters, with ``mark`` placed at character ``at`` when given. Its first and last lines differ from its middle,
    so a view that showed only the head would not show the tail."""
    head = "/Users/p/flow/venv/bin/python3 /Users/p/flow/scripts/fanin_log.py append --body-file - <<'EOF'\n"
    tail = "\nEOF\nsed -i '' -e 's/^OLD-TAIL-LINE$/NEW-TAIL-LINE/' /Users/p/flow/state/fanin_log.md # the decisive end"
    body = ("line of the log body, quoted \"words\" and a \\ backslash\n" * (size // 40 + 2))
    text = head + body[:size - len(head) - len(tail)] + tail
    if at is not None:
        text = text[:at] + mark + text[at + len(mark):]
    assert len(text) == size
    return text


def _signer(world: Any) -> Signer:
    source = authority_source(world.root / "cockpit.db", anchor=auth.anchor_path(world.root))
    signer = Signer(world.qdir, (NS,), RP, lambda: {k.fingerprint: k for k in passkey_keys(source(), RP)})
    signer.page_origin = PAGE
    return signer


def _queue(world: Any) -> ConsentQueue:
    return ConsentQueue(world.qdir, authority=authority_source(world.root / "cockpit.db",
                                                               anchor=auth.anchor_path(world.root)), home=h4_home())


def _q(rid: str) -> str:
    return urllib.parse.quote(rid, safe="")


@pytest.fixture
def home(world, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(world.qdir.parent))
    return world


# --- the queue keeps the whole input -------------------------------------------------------------------------------

def test_a_20kb_input_is_filed_whole_and_read_back_whole(tmp_path) -> None:
    command = _long_command(20_000)
    display = redact_display("Bash", {"command": command, "description": "append the log"})
    assert filed_input(SimpleNamespace(tool_name="Bash", display=display)) == {"command": command,
                                                                              "description": "append the log"}
    assert 'the decisive end", "description"' in display and "…" not in display


def test_the_bound_is_in_bytes_and_past_it_the_display_names_the_size_never_a_prefix() -> None:
    # the largest command whose display is exactly DISPLAY_MAX bytes is whole; one byte more is over
    frame = len(redact_display("Bash", {"command": ""}).encode())
    at_bound = "a" * (DISPLAY_MAX - frame)
    whole = redact_display("Bash", {"command": at_bound})
    assert len(whole.encode()) == DISPLAY_MAX and json.loads(whole[len("Bash "):]) == {"command": at_bound}
    over = redact_display("Bash", {"command": at_bound + "a"})
    assert over == "Bash " + record.OVER_BOUND.format(size=DISPLAY_MAX + 1, limit=DISPLAY_MAX)
    assert record.over_bound("Bash", over) == (DISPLAY_MAX + 1, DISPLAY_MAX)
    assert "aaaa" not in over                                       # no part of the input stands in for the whole
    req = SimpleNamespace(id="r1", tool_name="Bash", display=over)
    assert filed_input(req) is UNSHOWN
    with pytest.raises(AnswerRefused) as got:
        refuse_cut_display(req, "from a phone")
    assert f"{DISPLAY_MAX + 1} bytes, over the {DISPLAY_MAX}-byte bound" in str(got.value)
    assert "agent's own dialog stands" in str(got.value) and "too long to show here" not in str(got.value)
    # multi-byte text counts in bytes: 3 bytes a character
    wide = redact_display("Bash", {"command": "€" * (DISPLAY_MAX // 3)})
    assert record.over_bound("Bash", wide) is not None


def test_no_input_can_read_as_the_over_bound_words() -> None:
    # the words are not JSON, and every display under the bound is JSON after its tool name, so the two never meet
    for value in ("[not shown: the input is 9 bytes, over the 9-byte bound a signer is shown whole]",
                  ["not shown"], {"not shown": 1}):
        assert record.over_bound("Bash", redact_display("Bash", value)) is None


def test_a_request_filed_under_the_old_cut_stays_refused_and_says_so() -> None:
    old = "Bash " + json.dumps({"command": "x" * 5000})
    old = old[:LEGACY_DISPLAY_CUT - 1] + "…"
    req = SimpleNamespace(id="r-old", tool_name="Bash", display=old)
    assert filed_input(req) is UNSHOWN
    why = unshown_why(req)
    assert why is not None and "old display cut" in why and str(LEGACY_DISPLAY_CUT) in why
    with pytest.raises(AnswerRefused, match="old display cut.*agent's own dialog stands"):
        refuse_cut_display(req, "from the terminal")


def test_a_bidi_override_deep_in_a_long_input_is_marked_as_in_a_short_one(tmp_path) -> None:
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    q = ConsentQueue(qdir, authority=authority_source(qdir, anchor=None), clock=lambda: T0, home=h4_home())
    command = _long_command(60_000, at=50_000, mark="‮")
    r = q.file(agent="claude_code", agent_session_id="s", tool_name="Bash", tool_input={"command": command}, cwd="/w",
               hold=timedelta(minutes=5), event_id="e-bidi")
    body = entry_body(find_entry(read_listing(q.directory, now=T0 + timedelta(minutes=1)), r.id))
    assert "‮" not in body["display"] and "⟨U+202E⟩" in body["display"]
    assert body["display"].endswith('the decisive end"}')               # the whole input, past the mark
    assert body["answer_allow"] is not None and body["note"] == ""


def test_the_slot_path_bounds_its_display_inside_the_brokers_frame_and_row() -> None:
    from levain.consent.broker_client import GATE_DISPLAY_MAX, gate_request
    from levain.qbroker import wire
    from levain.qbroker.store import ROW_MAX

    def req(command: str):
        return gate_request(agent="claude_code", session="s", tool_name="Bash", tool_input={"command": command},
                            cwd="/w", event_id="e1", tier="T2", hold=60.0)

    assert filed_input(req(_long_command(20_000))) == {"command": _long_command(20_000)}
    # the worst case at the bound: every byte of the display a quote, which the frame's own JSON escapes again
    frame = len(redact_display("Bash", {"command": ""}).encode())
    quotes = '"' * ((GATE_DISPLAY_MAX - frame) // 2)
    worst = req(quotes)
    assert filed_input(worst) == {"command": quotes}
    encoded = wire.encode({"op": "request", "request": worst.fields()})
    assert len(encoded) < min(wire.MAX_FRAME, ROW_MAX), len(encoded)
    assert record.over_bound("Bash", req(quotes + '"').display) is not None


# --- the phone signs it, and the signature binds the exact input -----------------------------------------------------

@needs_ssh
def test_a_20kb_bash_request_is_shown_whole_on_the_sheet_and_signed_with_a_passkey(home, tmp_path) -> None:
    command = _long_command(20_000)
    tool_input = {"command": command, "description": "append the fan-in log"}
    request = _queue(home).file(agent="claude_code", agent_session_id="s1", tool_name="Bash", tool_input=tool_input,
                                cwd=str(home.ws), hold=timedelta(minutes=5), tier="T2")
    rid, fp = request.id, home.phone.fp
    with _serving(tmp_path / "inst", signer=_signer(home), extra_hosts=[RP]) as (url, _httpd):
        status, got = _call(url, f"/cockpit/consent/{_q(rid)}/sheet.json")
        assert status == 200, got
        r = got["request"]
        assert "refused" not in r, r.get("refused")
        assert [o["key"] for o in r["options"]] == ["allow", "deny"], r
        assert json.loads(r["message"][len("Bash "):]) == tool_input           # the whole input, tail included
        status, c = _call(url, f"/cockpit/consent/{_q(rid)}/challenge", {"decision": "allow", "signer_fp": fp})
        assert status == 200, c
        # the challenge binds the request's digest, never the input: it stays small however long the input is
        signed = answer_message(c["payload"])
        assert len(signed) < 1024 and command[:200] not in signed.decode()
        assert c["challenge"] == _b64u(challenge(signed))
        assert c["payload"]["request_digest"] == request.digest
        status, out = _call(url, f"/cockpit/consent/{_q(rid)}/answer",
                            {"payload": c["payload"], **home.phone.assertion(c["challenge"], origin=PAGE)})
        assert status == 200 and out["decision"] == "allow", out
    filed, state = _queue(home).get(rid)
    assert state == "allowed"
    # what the hook checks before it delivers: the digest of the exact input the agent will run
    assert filed.tool_input_digest == tool_input_digest(tool_input)
    assert filed.tool_input_digest != tool_input_digest({"command": command[:LEGACY_DISPLAY_CUT],
                                                         "description": "append the fan-in log"})


@needs_ssh
def test_an_over_bound_request_is_refused_on_the_sheet_with_its_size(home, tmp_path) -> None:
    request = _queue(home).file(agent="claude_code", agent_session_id="s1", tool_name="Bash",
                                tool_input={"command": "y" * (DISPLAY_MAX + 10)}, cwd=str(home.ws),
                                hold=timedelta(minutes=5), tier="T2")
    with _serving(tmp_path / "inst", signer=_signer(home), extra_hosts=[RP]) as (url, _httpd):
        r = _call(url, f"/cockpit/consent/{_q(request.id)}/sheet.json")[1]["request"]
        assert r["options"] == [] and f"over the {DISPLAY_MAX}-byte bound" in r["refused"], r
        assert "agent's own dialog stands" in r["refused"]
        size = record.over_bound("Bash", request.display)[0]
        assert f"{size} bytes" in r["refused"]
        status, out = _call(url, f"/cockpit/consent/{_q(request.id)}/challenge",
                            {"decision": "allow", "signer_fp": home.phone.fp})
        assert (status, out["error"]) == (422, "answer_refused") and f"{size} bytes" in out["message"], out


# --- the real page, at phone width ------------------------------------------------------------------------------------

PHONE = {"emulate": {"w": 390, "h": 844, "dpr": 3, "mobile": True}}
INPUT = "document.getElementById('sheet-input')"
FULL = f"{INPUT}.querySelector('.long-whole > pre.long-full')"
ACTIONS_TOP = "Math.round(document.getElementById('sheet-actions').getBoundingClientRect().top + window.scrollY)"


@needs_browser
@needs_ssh
def test_on_a_phone_a_20kb_input_shows_its_head_and_tail_expands_whole_and_signs(tmp_path, monkeypatch) -> None:
    """Headless Chrome at 390 px on the real sheet page, a real cockpit server and Chrome's virtual authenticator holding
    the enrolled passkey: the card shows the head and the tail, the whole input opens in its own scrolling block (its
    text equal to the filed display, no second cut), the answer buttons stay within a few screens, the passkey signs,
    and the queue holds the answer over the digest of the exact input. ``LEVAIN_LONG_SHOTS`` names where the
    screenshots go (default: the test's tmp dir)."""
    from tests.test_cockpit_consent_sheet_page import NOTE, _ConsentRig
    from tests.test_consent_long_input_coverage_page import probed_drive, stepped

    rig = _ConsentRig(tmp_path, monkeypatch)
    command = _long_command(20_000)
    tool_input = {"command": command, "description": "append the fan-in log"}
    filed = rig.queue.file(agent="claude_code", agent_session_id="s1", tool_name="Bash", tool_input=tool_input, cwd="/w",
                           hold=timedelta(minutes=30), tier="T2")
    shots = Path(os.environ.get("LEVAIN_LONG_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    with rig.serving():
        out = probed_drive(tmp_path, f"{rig.origin}/nope", [
            PHONE, rig.authenticator(), *rig.open_top(filed.id),
            {"eval": f"{INPUT}.className"},                                                                   # 2
            {"eval": f"JSON.stringify({{head: {INPUT}.querySelector('.long-head').textContent, "
                     f"tail: {INPUT}.querySelector('.long-tail').textContent, "
                     f"gap: {INPUT}.querySelector('.long-gap').textContent, "
                     f"summary: {INPUT}.querySelector('.long-whole > summary').textContent}})"},              # 3
            {"eval": f"{FULL}.textContent"},                                                                  # 4
            {"eval": f"JSON.stringify({{actions: {ACTIONS_TOP}, input: Math.round({INPUT}.getBoundingClientRect()"
                     f".top + window.scrollY), vh: window.innerHeight, sw: document.documentElement.scrollWidth, "
                     f"w: window.innerWidth}})"},                                                            # 5
            {"shot": str(shots / "1_card_390.png")},                                                          # 6
            {"shot": str(shots / "2_card_390_full_page.png"), "full": True},                                 # 7
            {"tap": f"{INPUT}.querySelector('.long-whole > summary')"},                                       # 8
            {"until": f"{INPUT}.classList.contains('open') && 1", "ms": 10000},                               # 9
            {"eval": f"JSON.stringify({{full_h: {FULL}.clientHeight, full_sh: {FULL}.scrollHeight, "
                     f"head_shown: !!{INPUT}.querySelector('.long-head').offsetParent, actions: {ACTIONS_TOP}, "
                     f"vh: window.innerHeight, sw: document.documentElement.scrollWidth, w: window.innerWidth}})"},  # 10
            {"shot": str(shots / "3_expanded_390.png")},                                                      # 11
            # in steps under a view (fix 2: a jump to the end leaves the middle unshown, and Allow shut)
            {"eval": f"({stepped(FULL, 0.8, down=True)}).then(() => {FULL}.scrollTop > 0)"},                  # 12
            {"shot": str(shots / "4_expanded_390_scrolled_to_the_tail.png")},                                 # 13
            {"tap": "document.querySelector('#sheet-actions [data-choice=\"allow\"]')"},                      # 14
            {"until": f"/^signed/.test({NOTE}) && {NOTE}", "ms": 30000},                                      # 15
            {"shot": str(shots / "5_signed_390.png")},                                                        # 16
        ])
    v = out["values"]
    assert v[1] == "open" and v[2] == "long-text", v[:3]
    parts = json.loads(v[3])
    assert parts["head"] == filed.display[:400] and parts["tail"] == filed.display[-400:], parts
    assert parts["tail"].endswith('# the decisive end", "description": "append the fan-in log"}'), parts["tail"][-80:]
    assert "more characters here" in parts["gap"] and f"({len(filed.display):,} characters)" in parts["summary"], parts
    assert v[4] == filed.display                                    # the whole input as filed: no second cut
    closed = json.loads(v[5])
    assert closed["sw"] <= closed["w"], closed                      # no sideways scroll at phone width
    assert closed["actions"] - closed["input"] < 2 * closed["vh"], closed   # the answers are near: not past 20 KB of text
    opened = json.loads(v[10])
    assert opened["full_sh"] > opened["full_h"] > 0 and opened["full_h"] <= opened["vh"] * 0.5 + 1, opened
    assert opened["head_shown"] is False and opened["sw"] <= opened["w"], opened
    assert opened["actions"] - closed["actions"] < opened["vh"], (closed, opened)
    assert v[12] is True
    assert str(v[15]).startswith("signed (allow)"), v[15]
    got, state = rig.queue.get(filed.id)
    assert state == "allowed" and got.tool_input_digest == tool_input_digest(tool_input)
    (shots / "transcript.json").write_text(json.dumps({
        "request_id": filed.id, "input_chars": len(command), "display_chars": len(filed.display),
        "tool_input_digest": filed.tool_input_digest, "request_digest": filed.digest, "state_after": state,
        "card_closed": closed, "card_open": opened, "note_after_sign": v[15], "posts": out["posts"]}, indent=2))
