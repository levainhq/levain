"""The useManifest flip's parity with the legacy page. Reproduced in headless Chrome on 8564d9f over a copy
of a real substrate: the State section drew with no edit button on `/`. These tests submit the page's own
forms through the DevTools protocol and assert on the store, not on the DOM."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from anneal_memory import Store

from levain.writes import ActionVerb
from tests._inbox_fake import inbox_downstream
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_r1_write import INBOX_PANELS

CHROME = os.environ.get("CHROME") or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
NODE = shutil.which("node")
DRIVE = Path(__file__).with_name("_page_drive.mjs")
needs_browser = pytest.mark.skipif(NODE is None or not Path(CHROME).exists(), reason="node and Chrome are needed")


def _drive(tmp_path: Path, url: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    f = tmp_path / "steps.json"
    f.write_text(json.dumps(steps))
    res = subprocess.run([NODE, str(DRIVE), CHROME, url, str(f)], capture_output=True, text=True, timeout=300)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _eventually(read: Any, want: Any, seconds: float = 20.0) -> Any:
    """The store read once it equals `want`, or the last read when the time runs out."""
    deadline = time.monotonic() + seconds
    got = read()
    while got != want and time.monotonic() < deadline:
        time.sleep(0.1)
        got = read()
    return got


def _verb_posts(out: dict[str, Any]) -> list[dict[str, Any]]:
    return [json.loads(p.split(" ", 1)[1]) for p in out["posts"] if p.startswith("/cockpit/verb ")]


# the State panel on the page: a panel whose title is "State" (the masthead's state line is another element)
STATE_PANEL = ('[...document.querySelectorAll("[data-edit-class]")].find(p => '
               '/^\\s*State\\b/.test((p.querySelector("h2,h3,.ptitle,.title")||{}).textContent||""))')


@needs_browser
def test_the_default_page_edits_the_state_section_through_the_broker(tmp_path: Path) -> None:
    root, src, _ids = _install(tmp_path)
    db = Path(src.anneal.episodic_db)
    new = "the State as edited on the default page"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": f"{STATE_PANEL} && [{STATE_PANEL}.dataset.editClass, !!{STATE_PANEL}.querySelector('.edit-btn')]"},
            {"eval": f"{STATE_PANEL}.querySelector('.edit-btn').click(); 1"},
            {"until": f"(() => {{ const ta = {STATE_PANEL}.querySelector('.edit-ta'); if (!ta) return null; ta.value = {json.dumps(new)};"
                      " ta.dispatchEvent(new Event('input', {bubbles: true})); return 1; })()"},
            # one click: the dry run asks the tier and the fire follows it
            {"eval": f"{STATE_PANEL}.querySelector('.edit-save').click(); 1"},
            {"posts": 2},
        ])
        state = _eventually(lambda: _state(db), new)
    assert out["values"][0] == ["A", True], out
    assert state == new
    posts = _verb_posts(out)
    assert [p.get("dry_run") is True for p in posts] == [True, False], posts
    fired = posts[1]
    assert fired["verb"] == "section_edit" and fired["panel_id"] == "section:state"
    assert fired["params"] == {"new_body": new}
    assert isinstance(fired.get("panel_version"), str) and fired["panel_version"]


def _state(db: Path) -> str:
    with Store(str(db), read_only=True) as st:
        return st.read_section("State")[0].strip()


@needs_browser
def test_a_state_section_the_page_cannot_fire_draws_no_edit_button(tmp_path: Path) -> None:
    """Read without the write token (its prompt declined), the server offers section_edit as `refused`:
    the page must not draw an editor whose save cannot post (L3 r2 codex MED)."""
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"until": f"{STATE_PANEL} && [{STATE_PANEL}.dataset.editClass, !!{STATE_PANEL}.querySelector('.edit-btn')]"},
        ])
    assert out["values"][0] == ["", False], out


@needs_browser
@pytest.mark.parametrize("job", [False, True])
def test_the_default_page_submits_a_downstream_send_through_the_broker(tmp_path: Path, job: bool) -> None:
    """L3 r1's codex MED feared a `job=True` verb launched from this page goes unpolled and a retry starts
    a second job. Measured: no job starts. The broker applies the verb's effect once per idempotency key
    and never runs the handler, job or not (`/action`, the only path that runs a handler, refuses a T2
    downstream verb)."""
    _root, src, _ids = _install(tmp_path)
    inbox, effect = inbox_downstream(tmp_path / "inbox.json")
    ran: list = []
    verb = ActionVerb(handler=lambda p: ran.append(p) or {"summary": "sent"}, label="send",
                      fields=("body", "for"), job=job)  # the fake inbox's own field
    box = '[...document.querySelectorAll("[aria-label=body]")][0].closest("[data-edit-class]")'
    with _serve(src, extra_verbs={"send_inbox": verb}, extra_panels=lambda: INBOX_PANELS,
                extra_effects={"send_inbox": effect}, surface_tokens={"browser": "T"},
                install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": "(() => { const m = document.querySelector('[aria-label=body]'); if (!m) return null;"
                      " m.value = 'hello from the page'; m.dispatchEvent(new Event('input', {bubbles: true})); return 1; })()"},
            # send, then the fat-finger confirm: the dry run asks the tier and the fire follows it
            {"eval": f"{box}.querySelector('.tray-dump-btn').click(); 1"},
            {"until": f"{box}.querySelector('.verb-confirm') && ({box}.querySelector('.verb-confirm').click(), 1)"},
            {"posts": 2},
        ])
        bodies = _eventually(lambda: [n.get("body") for n in inbox.notes.values()], ["hello from the page"])
    posts = _verb_posts(out)
    assert [p.get("dry_run") is True for p in posts] == [True, False], (posts, out["values"])
    assert posts[1]["verb"] == "send_inbox" and posts[1]["params"].get("body") == "hello from the page"
    assert bodies == ["hello from the page"], inbox.notes
    assert ran == []
