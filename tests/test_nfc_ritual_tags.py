"""The NFC ritual tags (levain#111): a BED tap token also sets one fixed state line, and the drop sheet's words box can be
pre-filled from a link's fragment (the MORNING tag). Nothing here lets a tap start a desk, arm an order or sign: a tap token
keeps its named allowed set, and the pre-fill is only words in a box the passkey then signs."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from levain.webpush import presence, presence_tokens
from tests.test_cockpit_pairing_http import T, _call
from tests.test_cockpit_presence import NEW, SET, TAP, _as_operator, _mint, _tap, rig  # noqa: F401 - fixtures
from tests.test_cockpit_webpush import _signed

ROOT = Path(__file__).resolve().parent.parent
SIGN_JS = ROOT / "levain/templates/web/sign.js"


def _state(src):
    return json.loads(Path(src.context_json).read_text())


def _mint_bed(port, **over):
    body = {"name": "bed", "words": ["sleep"], "state": "Bed", **over}
    return _call(port, "POST", NEW, body, token=T)


def test_a_bed_tap_sets_its_word_and_its_fixed_state_line(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        st, got, _ = _mint_bed(port)
        assert st == 200 and got["record"]["state"] == "Bed" and got["record"]["words"] == ["sleep"]
        tok = got["token"]
        st, got, _ = _call(port, "POST", SET, {"word": "sleep"}, headers=_tap(tok))
        assert st == 200 and got["word"] == "sleep" and got["state_line"] == {"text": "Bed", "set": True}
        ctx = _state(src)
        assert ctx["state"] == "Bed" and ctx["state_source"] == "phone"
        assert presence.read_word(q)["word"] == "sleep"
        assert tok not in json.dumps(got) and tok not in Path(src.context_json).read_text()


def test_the_allowed_set_refuses_every_word_but_its_own_and_leaves_the_state_alone(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        tok = _mint_bed(port)[1]["token"]
        for body in ({"word": "away"}, {"preset": "quiet"}, {"word": "desk"}, {"word": "loud"}):
            st, got, _ = _call(port, "POST", SET, body, headers=_tap(tok))
            assert st == 403 and "sleep" in got["message"], (body, got)
        assert _state(src)["state"] == "fine" and presence.read_word(q) is None
        for body in ({"devices": "off"}, {"lets_through": "nothing"}, {"word": "sleep", "ending": "input"}):
            assert _call(port, "POST", SET, body, headers=_tap(tok))[0] in (403, 400)
        assert _state(src)["state"] == "fine"


def test_the_state_text_is_fixed_at_creation_and_no_call_can_inject_one(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        tok = _mint_bed(port)[1]["token"]
        for extra in ({"state": "Evil"}, {"text": "Evil"}, {"state_line": "Evil"}):
            st, got, _ = _call(port, "POST", SET, {"word": "sleep", **extra}, headers=_tap(tok))
            assert st == 400 and got["error"] == "bad_request", (extra, got)
        assert _state(src)["state"] == "fine" and presence.read_word(q) is None
        # a header cannot carry one either: the only text a tap sets is the record's
        st, got, _ = _call(port, "POST", SET, {"word": "sleep"}, headers={**_tap(tok), "X-Levain-State": "Evil"})
        assert st == 200 and _state(src)["state"] == "Bed"
        # and a tap token cannot mint a token or set one on the list
        assert _call(port, "POST", NEW, {"name": "x", "words": ["sleep"], "state": "x"}, headers=_tap(tok))[0] in (401, 403)


@pytest.mark.parametrize("body", [
    {"words": ["sleep", "away"], "state": "Bed"},       # a text needs exactly one word
    {"words": ["sleep"], "state": ""},
    {"words": ["sleep"], "state": "   "},
    {"words": ["sleep"], "state": "x" * (presence_tokens.STATE_MAX + 1)},
    {"words": ["sleep"], "state": "two\nlines"},
    {"words": ["sleep"], "state": "bell\x07"},
    {"words": ["sleep"], "state": "a‮b"},          # bidi override
    {"words": ["sleep"], "state": "a b"},          # line separator
])
def test_a_state_text_that_is_not_one_short_line_on_one_word_is_not_minted(rig, body):
    src, q = rig
    with _signed(src, q) as (port, _):
        st, got, _ = _call(port, "POST", NEW, {"name": "bed", **body}, token=T)
        assert st == 422 and presence_tokens.list_public(q) == [], got


def test_a_token_with_no_state_never_touches_the_state_line(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        tok, _ = _mint(port)
        st, got, _ = _call(port, "POST", SET, {"word": "away"}, headers=_tap(tok))
        assert st == 200 and "state_line" not in got and _state(src)["state"] == "fine"


def test_a_revoked_bed_token_sets_neither_the_word_nor_the_line(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        tok = _mint_bed(port)[1]["token"]
        assert _call(port, "POST", "/webpush/presence/token/revoke", {"name": "bed"}, token=T)[0] == 200
        assert _call(port, "POST", SET, {"word": "sleep"}, headers=_tap(tok))[0] == 403
        assert _state(src)["state"] == "fine" and presence.read_word(q) is None


def test_a_state_write_that_fails_never_fails_the_tap_and_leaks_no_detail(rig, monkeypatch):
    from levain import writes

    def boom(*a, **k):
        raise RuntimeError("secret path /Users/x/context.json")
    src, q = rig
    with _signed(src, q) as (port, _):
        tok = _mint_bed(port)[1]["token"]
        monkeypatch.setattr(writes, "apply_edit", boom)
        st, got, _ = _call(port, "POST", SET, {"word": "sleep"}, headers=_tap(tok))
        assert st == 200 and got["word"] == "sleep" and got["state_line"]["set"] is False
        assert "secret" not in json.dumps(got) and _state(src)["state"] == "fine"


def test_an_edit_refusal_detail_never_reaches_the_taps_caller(rig, monkeypatch):
    from levain import writes

    def refuse(*a, **k):
        raise writes.EditError("store_unavailable", 503, "state file unavailable: /Users/x/.levain/context.json")
    src, q = rig
    with _signed(src, q) as (port, _):
        tok = _mint_bed(port)[1]["token"]
        monkeypatch.setattr(writes, "apply_edit", refuse)
        st, got, _ = _call(port, "POST", SET, {"word": "sleep"}, headers=_tap(tok))
        assert st == 200 and got["state_line"]["set"] is False and "/Users" not in json.dumps(got)


def test_a_cockpit_with_no_state_file_mints_no_state_token(rig, monkeypatch):
    src, q = rig
    with _signed(src, q) as (port, _):
        keep = src.context_json
        object.__setattr__(src, "context_json", None)       # the source is a frozen dataclass: this test alone unsets it
        try:
            st, got, _ = _mint_bed(port)
        finally:
            object.__setattr__(src, "context_json", keep)
        assert st == 422 and presence_tokens.list_public(q) == [], got


def test_the_text_is_on_the_record_and_the_public_list_but_the_secret_is_not(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        tok = _mint_bed(port)[1]["token"]
        f = q / "webpush" / presence_tokens.FILE_NAME
        assert '"state": "Bed"' in f.read_text() and tok not in f.read_text()
        listed = _call(port, "GET", "/webpush/presence", token=T)[1]["tokens"]
        assert listed[0]["state"] == "Bed" and "sha256" not in json.dumps(listed)


def test_the_drop_sheet_prefill_only_fills_the_words_box():
    """`#words=` is read into one constant and assigned to the words box and nowhere else: no send, sign or fetch follows it."""
    js = SIGN_JS.read_text()
    uses = [m.start() for m in re.finditer(r"\bhashWords\b", js)]
    lines = [js[js.rfind("\n", 0, u) + 1: js.find("\n", u)].strip() for u in uses]
    assigned = sorted({l.split("//")[0].strip() for l in lines if l.startswith("if (hashWords) {")})
    assert len(assigned) == 1 and assigned[0].count("words.value = hashWords") == 1
    assert not any(re.search(r"send|sign|fetch|post|submit|click\(", l) for l in assigned), assigned
    assert "location.pathname === \"/sign/drop\" && wdm" in js        # the drop sheet's own page alone
    assert ".slice(0, 500)" in js                                      # bounded


def test_the_drop_sheet_prefill_is_decoded_and_bounded_by_a_real_run():
    node = shutil.which("node")
    if node is None:
        pytest.skip("needs node")
    js = SIGN_JS.read_text()
    m = re.search(r"const wdm = .*?\n  \}\n", js, re.S)
    assert m, "the prefill block moved"
    prog = ("const run = (hash, path) => { const location = {hash, pathname: path};\n" + m.group(0)
            + "\n  return hashWords; };\n"
              "const out = [run('#words=%2Ffanin%20broad', '/sign/drop'), run('#words=%2Ffanin%20broad', '/sign/x'),\n"
              "  run('#words=%E0%A4%A', '/sign/drop'), run('#words=' + 'a'.repeat(900), '/sign/drop').length,\n"
              "  run('#words=a%00b%07c', '/sign/drop'), run('#thread=abc&words=hi', '/sign/drop'),\n"
              "  run('#words=a%E2%80%AEb%E2%80%A8c', '/sign/drop')];\n"
              "console.log(JSON.stringify(out));")
    got = subprocess.run([node, "-e", prog], capture_output=True, text=True, timeout=20)
    assert got.returncode == 0, got.stderr
    assert json.loads(got.stdout) == ["/fanin broad", "", "", 500, "abc", "hi", "abc"]
