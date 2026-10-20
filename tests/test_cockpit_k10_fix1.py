"""K10 FIX1 (L3 r1 findings): the Needs-you strip keeps a card whose item still waits (a typed answer survives a new consent)."""
from __future__ import annotations

import json
import threading
import time
from datetime import timedelta
from pathlib import Path

from levain.consent.queue import ConsentQueue, authority_source
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_k4a_needs import DESKTOP, READY, _waiting


@needs_browser
def test_a_typed_question_answer_survives_a_new_consent_arriving(tmp_path: Path, monkeypatch) -> None:
    home = _waiting(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)

    errs: list[BaseException] = []

    def late() -> None:
      try:
        time.sleep(4.0)
        q = ConsentQueue(home / "consent", authority=authority_source(home / "consent", anchor=None))
        assert q.withdraw(q.pending()[0].id)   # one leaves as one arrives: the count the manifest holds does not move
        q.file(agent="codex", agent_session_id="s3", tool_name="Edit", tool_input={"file_path": "/w/x.py"}, cwd="/w",
               hold=timedelta(minutes=40), event_id="c")
      except BaseException as e:  # noqa: BLE001
        errs.append(e)

    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        t = threading.Thread(target=late, daemon=True)
        t.start()
        out = _drive(tmp_path, f"{url}/#write_token=T", [DESKTOP, {"until": READY},
            {"until": "document.querySelector('#ck-needs .question-words')"},
            {"eval": "(() => { const ta = document.querySelector('#ck-needs .question-words'); ta.value = 'my half-typed ruling';"
                     " ta.dispatchEvent(new Event('input', {bubbles: true}));"
                     " window.__ta = ta; return document.querySelectorAll('#ck-needs .needs-card').length; })()"},
            {"eval": "new Promise((res) => { const t0 = Date.now(); const tick = async () => {"
                     " try { window.__r = String(await window.LevainCockpitView.refetchPanel('decisions')); } catch (e) { window.__r = String(e); }"
                     " const n = document.querySelectorAll('#ck-needs .needs-card').length;"
                     " if ([...document.querySelectorAll('#ck-needs .consent-tool')].some((e) => e.textContent === 'Edit') || Date.now() - t0 > 20000) return res(JSON.stringify({n,"
                     " same: document.querySelector('#ck-needs .question-words') === window.__ta,"
                     " value: (document.querySelector('#ck-needs .question-words') || {}).value, r: window.__r, tools: [...document.querySelectorAll('#ck-needs .consent-tool')].map((e) => e.textContent)}));"
                     " setTimeout(tick, 250); }; tick(); })"}])["values"]
        t.join(timeout=5)
    assert not errs, errs
    typed_with = out[-2]
    got = json.loads(out[-1])
    assert typed_with == 3, typed_with           # the answer was typed before the swap landed
    assert got["n"] == 3, got
    assert got["value"] == "my half-typed ruling" and got["same"] is True, got
