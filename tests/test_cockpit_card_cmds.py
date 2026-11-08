"""The needs-phill card with commands in it (Phill 11-05, np-c92ec798: two sign-in commands run into prose, STEP 1 cut mid-sentence
under a "Url" heading). The browser half: the row as flow filed it (tests/card_cmds_row.json, the flat ``stopped`` and the
multi-line ``stopped_full`` beside it) drawn in the cockpit's Needs-you modal, as the open-ended talk question it was.
``CARDFMT_SHOTS=<dir>`` keeps the shot as ``<CARDFMT_STEM>.png``. The parsing half runs in node: tests/card_cmds_check.js."""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from levain.consent.questions import Question
from tests.test_cockpit_card_format import PHONE
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_cockpit_questions import _file, _get_status
from tests.test_cockpit_sign_card import DESKTOP, NS, OPEN_Q, QRID, _Rig, _write_request, needs_ssh
from tests.test_cockpit_sign_facet import _Signed

HERE = Path(__file__).resolve().parent
ROW = json.loads((HERE / "card_cmds_row.json").read_text())["np-c92ec798"]
M = "#levain-modal .needs-question"
HAS_FULL = "blocks_full" in {f.name for f in dataclasses.fields(Question)}


class _Row(_Signed):
    """The rig's question answering as flow's desk reader files this row: the row's own title and flat stopped text, open-ended
    (no options, as the row was filed), with the multi-line text beside it where this levain's Question has the field."""

    def _mark(self, rows: Any) -> Any:
        return [self._row(r) for r in super()._mark(rows)]

    def _row(self, q: Question) -> Question:
        extra = {"blocks_full": ROW["stopped_full"]} if HAS_FULL else {}
        return dataclasses.replace(q, what="TALK: levain: " + ROW["what"], blocks=ROW["stopped"], options=(), rec="", **extra)

    def get(self, ref: Any) -> Any:
        q = self.inner.get(ref)
        return None if q is None else self._row(q)


def _shot(tmp_path: Path) -> list[dict]:
    folder = Path(os.environ.get("CARDFMT_SHOTS") or tmp_path)
    folder.mkdir(parents=True, exist_ok=True)
    return [{"until": "new Promise((r) => setTimeout(() => r('settled'), 900))"},
            {"shot": str(folder / f"{os.environ.get('CARDFMT_STEM', 'cardfmt_after')}.png"), "full": False}]


def _drawn(rig: _Rig, tmp_path: Path, viewport: dict, tail: list[dict], *, signed: bool = False, card: bool = False) -> dict:
    with rig.serving() as (url, httpd):
        # signed: the question names the rig's sign request, filed here in the needs-phill grammar with the row's flat words
        wrapped = _Row(httpd.question_store.inner, {rig.question.n: QRID} if signed else {})
        httpd.question_store = wrapped
        rig.ck.attach_question_rows(wrapped)
        keep = "window.localStorage.setItem('levain_write_token', 'T'); "
        return _drive(tmp_path, f"{rig.origin}/nope", [
            viewport,
            {"eval": f"(() => {{ {keep}location.href = '{rig.origin}/'; return 1; }})()"},
            # a signed card's head replaces the question's body once its sign request is read: wait for its Detail instead
            {"until": f"{OPEN_Q} && document.querySelector('{M} {'.card-detail' if card else '.needs-q-body'}')", "ms": 30000},
            *tail])


@needs_browser
@needs_ssh
def test_the_talk_card_shows_three_steps_each_command_one_copyable_block(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    out = _drawn(rig, tmp_path, DESKTOP, [
        {"eval": f"[...document.querySelectorAll('{M} .needs-q-body .card-cmd .consent-code')].map(c => c.textContent)"},
        {"eval": f"[...document.querySelectorAll('{M} .card-label')].map(h => h.textContent)"},
        {"eval": f"[...document.querySelectorAll('{M} .card-step')].map(p => p.textContent)"},
        {"eval": f"document.querySelectorAll('{M} .needs-q-body .card-cmd .consent-copy').length"},
        {"eval": "document.documentElement.scrollWidth <= window.innerWidth"},
        *_shot(tmp_path),
    ])
    cmds, labels, steps, copies, fits = [v for v in out["values"] if not (isinstance(v, dict) and "shot" in v) and v != "settled"][-5:]
    assert len(cmds) == 3 and all(c.startswith("/usr/bin/python3 -I /Users/phillipclapham/") for c in cmds), cmds
    assert cmds[0].endswith("--vendor claude --slot 8") and cmds[1].endswith("--vendor codex --slot 9"), cmds
    assert cmds[2].endswith("after-login --vm vend-real --vendor claude"), cmds
    assert labels == ["What", "Blocks"], labels
    assert [s.split(" ")[:2] for s in steps] == [["STEP", "1"], ["STEP", "2"], ["STEP", "3"]], steps
    assert copies == 3 and fits is True


def _file_signed_row(rig: _Rig) -> None:
    """The rig's sign request carries the row as flow signs it: the needs-phill grammar, the flat `stopped` (line breaks are
    never part of what is signed)."""
    msg = "\n".join(["flow needs-phill answer", "item: np-c92ec798", "row_version: " + "a" * 64, "repo: ROW_REPO",
                     "asked: " + ROW["what"], "stopped: " + ROW["stopped"], "nonce: 9a1f", "filed: 2026-11-05T15:29:00Z"]
                    ).replace("ROW_REPO", ROW["repo"])
    _write_request(rig.qdir, QRID, msg, options=[{"key": "a", "text": "Ship it tonight"}, {"key": "b", "text": "Hold for the morning"}],
                   write_in=True)


@needs_browser
@needs_ssh
def test_a_signed_card_reads_the_lines_but_never_makes_a_copy_button(tmp_path: Path, monkeypatch) -> None:
    """Line breaks are not signed, so on a card whose words are signed the lines are only a reading: the three commands stay
    monospace and selectable, and none is a copy button (the unsigned test above is the contrast: same row, three buttons)."""
    rig = _Rig(tmp_path, monkeypatch)
    _file_signed_row(rig)
    out = _drawn(rig, tmp_path, DESKTOP, [
        {"until": f"document.querySelector('{M} .card-detail')", "ms": 30000},
        {"eval": f"document.querySelector('{M} .card-detail').open = true"},
        {"eval": f"[...document.querySelectorAll('{M} .card-cmd')].map(c => c.className + '|' + c.textContent)"},
        {"eval": f"document.querySelectorAll('{M} .card-detail .consent-copy, {M} .card-detail .consent-cmd').length"},
        {"eval": f"document.querySelectorAll('{M} .card-detail .card-step').length"},
        {"eval": f"(document.querySelector('{M}') || document.body).querySelectorAll('.card-detail button').length"},
        *_shot(tmp_path),
    ], signed=True, card=True)
    cmds, copies, steps, buttons = [v for v in out["values"] if not (isinstance(v, dict) and "shot" in v) and v != "settled"][-4:]
    assert len(cmds) == 3 and all(c.startswith("card-cmd card-cmd-plain|/usr/bin/python3 -I /Users/phillipclapham/") for c in cmds), cmds
    assert copies == 0 and buttons == 0 and steps == 3, (copies, buttons, steps)


@needs_browser
@needs_ssh
def test_a_signed_question_body_never_makes_a_copy_button_either(tmp_path: Path, monkeypatch) -> None:
    """The question body of a signed row that is not a needs-phill message (so no card replaces it) is the other place the lines
    are drawn on a signed row: read as lines, commands monospace, no copy button."""
    rig = _Rig(tmp_path, monkeypatch)
    out = _drawn(rig, tmp_path, DESKTOP, [
        {"eval": f"[...document.querySelectorAll('{M} .needs-q-body .card-cmd')].map(c => c.className)"},
        {"eval": f"document.querySelectorAll('{M} .needs-q-body .consent-copy, {M} .needs-q-body .consent-cmd').length"},
    ], signed=True)
    assert out["values"][-2:] == [["card-cmd card-cmd-plain"] * 3, 0], out["values"]


@needs_browser
@needs_ssh
def test_the_talk_card_on_a_phone_keeps_the_commands_inside_the_screen(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    out = _drawn(rig, tmp_path, PHONE, [
        {"eval": f"document.querySelectorAll('{M} .needs-q-body .card-cmd').length"},
        {"eval": "document.documentElement.scrollWidth <= window.innerWidth"},
    ])
    assert out["values"][-2:] == [3, True], out["values"]


def test_the_card_reads_commands_and_lines_on_the_real_row() -> None:
    """The parsing half, in node (tests/card_cmds_check.js): three steps and three copyable commands, no URL section, known labels
    only, fenced / indented / path / command lines, the flat legacy text as before, and the lines used only when they are the
    flat text's own words."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("needs node")
    out = subprocess.run([node, str(HERE / "card_cmds_check.js")], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "FAIL" not in out.stdout


@pytest.fixture
def lh(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    return tmp_path / "lh" / "consent"


def test_a_question_body_carries_blocks_full_only_when_its_store_has_one(tmp_path, lh) -> None:
    """The body route (what the card reads) adds ``blocks_full`` beside ``blocks`` for a host store whose question carries the
    lines, and leaves the key out for one that does not (the kernel's store, an older host): ``blocks`` is never changed."""
    from levain.consent.questions import QuestionStore

    _root, src, _ids = _install(tmp_path)
    q = _file(QuestionStore(lh))
    assert q.blocks_full == ""
    lines = dataclasses.replace(q, blocks_full="line one\nline two")

    class Host:
        def __init__(self, row: Question) -> None:
            self.row = row

        def get(self, n: int) -> Any:
            return self.row if n == self.row.n else None

    with _serve(src, write_token=TOKEN) as (base, httpd):
        httpd.question_store = Host(lines)
        body = _get_status(f"{base}/cockpit/question/{q.n}.json", TOKEN)[1]
        assert body["blocks_full"] == "line one\nline two" and body["blocks"] == q.blocks
        httpd.question_store = Host(q)
        plain = _get_status(f"{base}/cockpit/question/{q.n}.json", TOKEN)[1]
        assert "blocks_full" not in plain and plain["blocks"] == q.blocks
        httpd.question_store = Host(dataclasses.replace(q, blocks_full=7))     # not a string: not carried
        assert "blocks_full" not in _get_status(f"{base}/cockpit/question/{q.n}.json", TOKEN)[1]


# Phill 11-07 (296): a pending consent row stops drawing the terminal "copy allow" / "copy deny" lines where a passkey answers;
# they stay where it cannot (the sheet only says why, or the cockpit has no sheet for the row). The body route is real; only the
# sheet it reads is a stand-in that returns the row (or none) the case needs.
_COPY_LINES = ("[...document.querySelectorAll('#levain-modal [data-consent-row] .consent-body .consent-cmd .consent-copy')]"
               ".map(b => b.textContent)")


class _Sheet:
    def __init__(self, keys: "list[str] | None", queue_dir: Path, signers: bool = True) -> None:
        self.keys, self.queue_dir, self.signers = keys, queue_dir, signers

    def row(self, _rid: str) -> "dict | None":
        if self.keys is None:
            return None
        return {"options": [{"key": k} for k in self.keys], "signers": ["fp"] if self.signers else [], "sha256": "v" * 64}


def _pending_consent_copy_lines(tmp_path: Path, monkeypatch, keys: "list[str] | None", signers: bool = True) -> list:
    from datetime import timedelta

    from levain.consent.queue import ConsentQueue, authority_source
    from tests._h4 import h4_home
    from tests.test_cockpit_consent_rows import OPEN_DECISIONS, TILE

    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    q = ConsentQueue(home / "consent", authority=authority_source(home / "consent", anchor=None), home=h4_home())
    q.file(agent="claude_code", agent_session_id="s", tool_name="Bash", tool_input={"command": "ls /w"}, cwd="/w",
           hold=timedelta(minutes=5), event_id="cc1")
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, httpd):
        httpd.consent_sheet = None if keys is None else _Sheet(keys, home / "consent", signers)
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": TILE},
            {"eval": OPEN_DECISIONS},
            {"until": "document.querySelector('#levain-modal [data-consent-row] .consent-display')", "ms": 30000},
            {"eval": "new Promise(r => setTimeout(() => r(1), 600))"},
            {"eval": _COPY_LINES},
        ])
    return out["values"][-1]


@needs_browser
def test_a_pending_consent_row_with_a_passkey_answer_draws_no_copy_lines(tmp_path, monkeypatch) -> None:
    assert _pending_consent_copy_lines(tmp_path, monkeypatch, ["allow", "deny"]) == []


@needs_browser
def test_a_pending_t3_row_with_deny_only_choices_keeps_only_the_allow_copy_line(tmp_path, monkeypatch) -> None:
    """A passkey never signs a T3 allow, so the terminal key is the only allow route: its line stays, the deny line goes."""
    assert _pending_consent_copy_lines(tmp_path, monkeypatch, ["deny"]) == ["copy allow"]


@needs_browser
def test_a_pending_consent_row_no_passkey_can_answer_keeps_the_copy_lines(tmp_path, monkeypatch) -> None:
    assert _pending_consent_copy_lines(tmp_path, monkeypatch, ["allow", "deny"], signers=False) == ["copy allow", "copy deny"]


@needs_browser
def test_a_pending_consent_row_with_no_sheet_keeps_the_copy_lines(tmp_path, monkeypatch) -> None:
    assert _pending_consent_copy_lines(tmp_path, monkeypatch, None) == ["copy allow", "copy deny"]
