"""Voice: premium audio is played from a ``blob:`` <audio>, and the shared CSP (``default-src 'none'``) names no
``media-src``, so a real browser refuses the load. The player tests stub the media element and cannot see it; this file
loads a real small WAV into a real <audio> on the real cockpit page under the served CSP (muted, volume 0, never played).

Revert: delete ``media-src blob:`` from ``_CSP`` in levain/http_guards.py (both tests go red)."""
from __future__ import annotations

import http.client
import json

from levain.http_guards import _CSP
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import T, _serve
from tests.test_pair_card_w1b import _drive, needs_browser

# a valid 8 kHz mono 16-bit PCM WAV, 400 samples of silence, built in the page; loaded, never played
PROBE = """
new Promise((resolve) => {
  const n = 400, b = new ArrayBuffer(44 + n * 2), v = new DataView(b);
  const s = (o, t) => { for (let i = 0; i < t.length; i++) v.setUint8(o + i, t.charCodeAt(i)); };
  s(0, 'RIFF'); v.setUint32(4, 36 + n * 2, true); s(8, 'WAVE'); s(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true);
  v.setUint16(22, 1, true); v.setUint32(24, 8000, true); v.setUint32(28, 16000, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  s(36, 'data'); v.setUint32(40, n * 2, true);
  const violations = [];
  document.addEventListener('securitypolicyviolation', (e) => violations.push(e.violatedDirective + ' ' + e.blockedURI.slice(0, 5)));
  const a = new Audio(); a.muted = true; a.volume = 0; a.preload = 'auto';
  const done = (how) => resolve(JSON.stringify({ how: how, violations: violations, error: a.error ? a.error.code : null, ready: a.readyState, duration: a.duration || 0 }));
  a.addEventListener('loadedmetadata', () => setTimeout(() => done('loaded'), 50));
  a.addEventListener('error', () => setTimeout(() => done('error'), 50));
  a.src = URL.createObjectURL(new Blob([b], { type: 'audio/wav' }));
  a.load();
  setTimeout(() => done('timeout'), 8000);
})
"""


def _csp_of(path: str, port: int) -> str:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    c.request("GET", path)
    r = c.getresponse()
    r.read()
    return r.getheader("Content-Security-Policy")


def test_the_csp_allows_blob_media_and_nothing_wider() -> None:
    """Revert: remove `media-src blob:`. It is one source in one directive: no `*`, no `data:`, no `'self'`, no other directive moves."""
    media = [d.strip() for d in _CSP.split(";") if d.strip().startswith("media-src")]
    assert media == ["media-src blob:"], _CSP
    assert "default-src 'none'" in _CSP and "script-src 'self'" in _CSP and "img-src 'self'" in _CSP
    assert _CSP.count("blob:") == 1


@needs_browser
def test_a_blob_audio_element_loads_under_the_served_cockpit_csp(tmp_path, monkeypatch) -> None:
    """The served page's own header carries media-src blob:, and a real WAV blob in a real <audio> raises no CSP violation."""
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (port, _httpd):
        csp = _csp_of("/", port)
        out = _drive(tmp_path, monkeypatch, "http://127.0.0.1:%d/#write_token=%s" % (port, T), [
            {"until": "document.readyState === 'complete'", "ms": 20000},
            {"eval": PROBE},
        ], flags=["--mute-audio", "--autoplay-policy=document-user-activation-required"])
    got = json.loads(out["values"][1])
    assert got["violations"] == [] and got["how"] == "loaded" and got["error"] is None, got
    assert "media-src blob:" in csp, csp
