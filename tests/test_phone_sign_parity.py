"""The phone sign page shows a sign request whole: the boxed message, every option and the recommendation, at 390 px.

A needs-phill item filed as a sign request is read on this page when he is away, so what he reads here must be what
the desk would show him, and exactly what the passkey signs. Driven in headless Chrome against the real phone server
over a long multi-option request: the message box holds the filed message byte for byte, each option button holds its
filed text, the line under the options is the server's own choice line, the recommended option is marked, and nothing
is clipped, clamped, height-capped or scrolled sideways."""

from __future__ import annotations

import json

import threading
from contextlib import contextmanager
from pathlib import Path

from levain.consent.sign import _TEXT_MAX, choice_line, sign_dir
from levain.phone_server import make_phone_server
from tests.test_cockpit_flip_parity import _drive, needs_browser

NS = "flow-needs-phill"
TOK = "t" * 43
RID = "np-parity1-0badc0de"
# each option at the most text an option may hold, so a page-side cut of any length shows as a mismatch
OPTIONS = [{"key": k, "text": (f"option {k}: " + "a long reason that wraps on a phone " * 8)[: _TEXT_MAX - 4] + " END"}
           for k in ("a", "b", "c")]
MESSAGE = "\n".join(["flow needs-phill answer", "item: np-parity1", "repo: levain",
                     "asked: " + "the lead sentence and the analysis paragraph, long enough to wrap many times " * 12,
                     "stopped: " + "what stopped and why " * 20 + "END OF STOPPED",
                     "recommended: (b) " + OPTIONS[1]["text"], "nonce: 0badc0de", "filed: 2026-10-25T14:55:28Z"])


def _file(qdir: Path) -> None:
    d = sign_dir(qdir)
    d.mkdir(parents=True, mode=0o700)
    (d / f"{RID}.json").write_text(json.dumps({
        "format": "levain-sign-choice/1", "id": RID, "namespace": NS, "message": MESSAGE,
        "title": "levain: the lead sentence", "requester": "flow desk", "expires_at": "2099-01-01T00:00:00+00:00",
        "options": OPTIONS, "write_in": True, "recommended": "b"}))


@contextmanager
def _serving(qdir: Path):
    httpd = make_phone_server(qdir, "localhost", port=0, token=TOK, sign_namespaces=(NS,), dispatch_dir=qdir)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


PROBE = """(() => {
  const card = document.querySelector('article.req.sign');
  const pick = (n) => { const cs = getComputedStyle(n);
    return { text: n.textContent, inner: n.innerText, transform: cs.textTransform, textOverflow: cs.textOverflow,
             clamp: cs.webkitLineClamp, maxHeight: cs.maxHeight,
             clipped: n.scrollWidth > n.clientWidth + 1 || n.scrollHeight > n.clientHeight + 1 }; };
  return { display: pick(card.querySelector('.display')),
           options: [...card.querySelectorAll('.actions button.option')].filter(b => !b.textContent.startsWith('(*)')).map(b => Object.assign(pick(b),
             { rec: b.classList.contains('recommended'), label: (b.querySelector('.rec') || {}).textContent || '' })),
           lines: [...card.querySelectorAll('.lines .line')].map(pick),
           sideways: document.documentElement.scrollWidth > innerWidth };
})()"""


def _unclipped(got: dict) -> bool:
    return (not got["clipped"] and got["textOverflow"] == "clip" and got["clamp"] in ("none", "")
            and got["maxHeight"] == "none" and got["transform"] == "none")


@needs_browser
def test_a_long_multi_option_request_is_shown_whole_and_as_signed_at_390(tmp_path: Path) -> None:
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    _file(qdir)
    shot = tmp_path / "sign_390.png"
    with _serving(qdir) as url:
        out = _drive(tmp_path, f"{url}/#phone_token={TOK}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"until": "document.querySelector('article.req.sign .display') && 1"},
            {"eval": PROBE}, {"shot": str(shot), "full": True},
        ])
    got = out["values"][-2]
    assert len(MESSAGE) > 1500 and all(len(o["text"]) == _TEXT_MAX for o in OPTIONS)
    # the message box: the signed message, byte for byte, drawn whole
    assert got["display"]["text"] == MESSAGE and got["display"]["inner"] == MESSAGE
    assert _unclipped(got["display"]), got["display"]
    # every option, its whole filed text on its button, the recommended one marked
    assert len(got["options"]) == len(OPTIONS), got["options"]
    for o, b in zip(OPTIONS, got["options"]):
        want = f"({o['key']}) {o['text']}"
        assert b["text"] == want + ("recommended" if o["key"] == "b" else ""), (b["text"], want)
        assert b["inner"].startswith(want), (b["inner"], want)
        assert b["rec"] == (o["key"] == "b") and b["label"] == ("recommended" if o["key"] == "b" else "")
        assert not b["clipped"] and b["textOverflow"] == "clip" and b["clamp"] in ("none", ""), b
    # the appended lines shown are exactly the bytes each option's signature appends
    want_lines = [choice_line(o["key"], o["text"]) for o in OPTIONS] + [choice_line("*", "") + "<your words>"]
    assert [ln["text"] for ln in got["lines"]] == want_lines
    assert all(_unclipped(ln) for ln in got["lines"]), got["lines"]
    assert not got["sideways"], "the sign page scrolls sideways at 390"
    assert shot.stat().st_size > 0
