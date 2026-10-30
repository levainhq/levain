"""The Sessions modal's transcript button (Q8 C): the token-gated body drawn as text only, and the wording without a token."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _serve
from tests.test_cockpit_sessions import SID_CC, _fixture

HOSTILE = "<img src=x onerror=alert(1)>"
TILE = "[...document.querySelectorAll('.ck-widgets .ck-card.w')].find(c => /^Sessions/i.test(c.innerText))"
ROW_CC = "[...document.querySelectorAll('#levain-modal .row')].find(r => r.querySelector('.sess-tx-btn') && /claude_code/.test(r.innerText))"


def _log(root: Path, events: list[dict]) -> None:
    stem = hashlib.sha1(f"claude_code:{SID_CC}".encode()).hexdigest()[:24]
    p = root / ".levain" / "sessions" / f"{stem}.log.jsonl"
    p.write_text("".join(json.dumps({"v": 1, "cut": 0, **e}) + "\n" for e in events))


def _open_modal(url: str, tmp_path: Path, steps: list[dict]) -> list:
    return _drive(tmp_path, url, [
        {"until": f"{TILE} && /2/.test({TILE}.innerText)"},
        {"eval": f"{TILE}.click(); 1"},
        {"until": f"{ROW_CC}"},
        *steps,
    ])["values"]


@needs_browser
def test_the_transcript_is_drawn_as_text_and_no_element_is_made_from_it(tmp_path, monkeypatch):
    root, src = _fixture(tmp_path, monkeypatch)
    _log(root, [
        {"at": "2026-10-15T10:00:00Z", "kind": "prompt", "text": HOSTILE},
        {"at": "2026-10-15T10:00:05Z", "kind": "tool", "text": "Bash: ls"},
        {"at": "2026-10-15T10:00:09Z", "kind": "reply", "text": "done <b>bold</b>", "cut": 12},
    ])
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _open_modal(f"{url}/#write_token=T", tmp_path, [
            {"eval": f"{ROW_CC}.querySelector('.sess-tx-btn').click(); 1"},
            {"until": f"{ROW_CC}.querySelector('.sess-ev')"},
            {"eval": f"JSON.stringify({{text: {ROW_CC}.querySelector('.sess-tx').innerText, "
                     f"imgs: document.querySelectorAll('#levain-modal img, #levain-modal b').length, "
                     f"kinds: [...{ROW_CC}.querySelectorAll('.sess-kind')].map(k => k.textContent)}})"},
        ])
    got = json.loads(out[-1])
    assert HOSTILE in got["text"] and "done <b>bold</b>" in got["text"], got
    assert "12 more characters not kept" in got["text"] and "claude --resume" in got["text"], got
    assert got["imgs"] == 0 and got["kinds"] == ["you", "tool", "agent", "consent"], got     # the consent line merged in, last (filed later)


@needs_browser
def test_a_page_without_its_token_says_so_and_fetches_nothing(tmp_path, monkeypatch):
    _root, src = _fixture(tmp_path, monkeypatch)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _open_modal(f"{url}/", tmp_path, [
            {"eval": f"{ROW_CC}.querySelector('.sess-tx-btn').click(); 1"},
            {"until": f"/carries no token/.test({ROW_CC}.innerText)"},
            {"eval": f"{ROW_CC}.querySelector('.sess-tx').innerText"},
        ])
    assert "carries no token" in out[-1] and "claude --resume" not in out[-1]


STUB_KIND = ("window.fetch = (f => (u, o) => String(u).includes('/cockpit/session/') ? f(u, o).then(r => r.json().then(b => "
             "{ b.events.push({at: '2026-10-15T10:01:00Z', kind: 'constructor', text: 'proto', cut: 0}); "
             "return new Response(JSON.stringify(b), {status: 200}); })) : f(u, o))(window.fetch); 1")


@needs_browser
def test_an_event_kind_that_names_an_object_prototype_key_draws_as_a_tool_line(tmp_path, monkeypatch):
    root, src = _fixture(tmp_path, monkeypatch)
    _log(root, [{"at": "2026-10-15T10:00:00Z", "kind": "prompt", "text": "hi"}])
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _open_modal(f"{url}/#write_token=T", tmp_path, [
            {"eval": STUB_KIND},
            {"eval": f"{ROW_CC}.querySelector('.sess-tx-btn').click(); 1"},
            {"until": f"{ROW_CC}.querySelector('.sess-ev')"},
            {"eval": f"JSON.stringify([...{ROW_CC}.querySelectorAll('.sess-kind')].map(k => k.textContent))"},
        ])
    assert json.loads(out[-1]) == ["you", "tool", "consent"], out[-1]      # "constructor" is not a kind: it reads as a tool line, not "function Object()"


@needs_browser
def test_a_throwing_transport_ends_in_words_and_leaves_the_button_usable(tmp_path, monkeypatch):
    _root, src = _fixture(tmp_path, monkeypatch)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _open_modal(f"{url}/#write_token=T", tmp_path, [
            {"eval": "window.LevainCockpitTransport.headers = () => { throw new Error('boom'); }; 1"},
            {"eval": f"{ROW_CC}.querySelector('.sess-tx-btn').click(); 1"},
            {"until": f"/could not be read/.test({ROW_CC}.innerText)"},
            {"eval": f"JSON.stringify({{text: {ROW_CC}.querySelector('.sess-tx').innerText, off: {ROW_CC}.querySelector('.sess-tx-btn').disabled}})"},
        ])
    got = json.loads(out[-1])
    assert "could not be read" in got["text"] and "reading" not in got["text"] and got["off"] is False, got


@needs_browser
def test_an_open_transcript_survives_a_redraw_of_the_modal(tmp_path, monkeypatch):
    root, src = _fixture(tmp_path, monkeypatch)
    _log(root, [{"at": "2026-10-15T10:00:00Z", "kind": "prompt", "text": "still here"}])
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _open_modal(f"{url}/#write_token=T", tmp_path, [
            {"eval": f"{ROW_CC}.querySelector('.sess-tx-btn').click(); 1"},
            {"until": f"{ROW_CC}.querySelector('.sess-ev')"},
            {"eval": "window.__box = document.querySelector('#levain-modal .modal'); window.LevainCockpitTransport.reload(); 1"},
            {"until": "document.querySelector('#levain-modal .modal') && document.querySelector('#levain-modal .modal') !== window.__box"},   # the modal was rebuilt
            {"eval": f"JSON.stringify({{text: {ROW_CC}.querySelector('.sess-tx') ? {ROW_CC}.querySelector('.sess-tx').innerText : '', btn: {ROW_CC}.querySelector('.sess-tx-btn').textContent}})"},
        ])
    got = json.loads(out[-1])
    assert "still here" in got["text"] and got["btn"] == "refresh", got


@needs_browser
def test_a_coordinator_wake_and_its_reply_are_labelled_as_such_never_as_his_words(tmp_path, monkeypatch):
    """C11 S1 (codex MED 3): the two kinds were unknown to the page and drew as tool lines."""
    root, src = _fixture(tmp_path, monkeypatch)
    _log(root, [
        {"at": "2026-10-15T10:00:00Z", "kind": "wake", "text": "[levain wake ln-0]\nYou are Levain's coordinator"},
        {"at": "2026-10-15T10:00:05Z", "kind": "wake_reply", "text": "wake: nothing needs you"},
    ])
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _open_modal(f"{url}/#write_token=T", tmp_path, [
            {"eval": f"{ROW_CC}.querySelector('.sess-tx-btn').click(); 1"},
            {"until": f"{ROW_CC}.querySelector('.sess-ev')"},
            {"eval": f"JSON.stringify([...{ROW_CC}.querySelectorAll('.sess-kind')].map(k => k.textContent))"},
        ])
    assert json.loads(out[-1])[:2] == ["wake", "wake reply"], out[-1]
