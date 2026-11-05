"""The grant page's row for a voice key (kind ``voice-key``, levain.consent.grants.grant_view).

A voice key is listed beside the consent grants so a person can revoke it, but it is not a consent grant: it has no expiry
and answers no request. Its row names the provider, says it is live until it is revoked, and carries no "answered nothing
yet" line; a consent row is drawn exactly as it was. The cockpit's grant renderer is ``grantCard`` in dashboard_core.js (the
old phone page, the other renderer, was deleted with ONESURFACE OS-6).

Two kinds of test. The fixture tests answer ``/cockpit/consent/grants.json`` with a fixed body, so a consent row can be held
string-equal to the row the page drew before the voice-key branch existed. The real-data test signs a real voice-key record
into a real cockpit and reads the page's row for it."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from levain.chat_egress.key_setup import KeychainItems
from levain.chat_egress.keys import KeychainStore
from levain.voice import custody
from levain.voice.custody import Secret, SecurityStdinWriter
from tests.test_cockpit_consent_rows import OPEN_DECISIONS, TILE
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_narrow_layout_rig import PHONE_TOUCH, SETTLE, drive_touch
from tests.test_grant_phone import world  # noqa: F401  (a fixture)
from tests.test_voice_custody import FakeSecurity
from tests._grant_helpers import _grant

pytestmark = pytest.mark.skipif(subprocess.run(["which", "ssh-keygen"], capture_output=True).returncode != 0,
                                reason="needs ssh-keygen")

_CARDS = "[...document.querySelectorAll('#levain-modal [data-consent-grant]')]"
_REVOKE = "levain consent grant revoke --data-root /cockpit --signing-key <key> -- "
_REVOKE_HTML = _REVOKE.replace("<", "&lt;").replace(">", "&gt;")      # as outerHTML spells it
_EMPTY = {"count": 0, "last_at": "", "recent": [], "refused_count": 0}


def _consent_row(gid: str = "cg-0000000000aa") -> dict:
    return {"id": gid, "id_shown": gid, "kind": "consent", "expires_at": "2026-12-01T00:00:00Z", "link_seq": 4,
            "instance": "inst-consent", "answered": dict(_EMPTY), "revoke": _REVOKE + gid, "state": "live",
            "agent": "claude_code", "tools": ["Write"], "workspace": "/work/proj", "postures": ["default"]}


def _voice_row(provider: str, gid: str) -> dict:
    # the shape grant_view gives a voice key: no expiry, no agent, no tools, no workspace, no postures
    return {"id": gid, "id_shown": gid, "kind": "voice-key", "expires_at": "", "link_seq": 7,
            "instance": "inst-" + provider, "answered": dict(_EMPTY), "revoke": _REVOKE + gid, "state": "live",
            "provider": provider}


def _stub(rows: list[dict]) -> str:
    """The page's fetch answers the grants read with ``rows``; every other read is the real one."""
    body = json.dumps({"root": "/cockpit", "note": "", "grants": rows, "inert_note": "held inert"})
    return ("(() => { const real = window.fetch.bind(window); window.fetch = (u, o) => {"
            " if (String(u).includes('/consent/grants.json')) return Promise.resolve(new Response(%s, {status: 200,"
            " headers: {'Content-Type': 'application/json'}})); return real(u, o); }; return 1; })()" % json.dumps(body))


def _drive_cards(tmp_path, world, monkeypatch, rows: list[dict]):
    monkeypatch.setenv("LEVAIN_HOME", str(world.qdir.parent))
    _root, src, _ids = _install(tmp_path)
    cards = [f"{_CARDS}.map(c => c.dataset.consentGrant)", f"{_CARDS}.map(c => c.outerHTML)",
             f"{_CARDS}.map(c => c.innerText)"]
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": TILE}, {"eval": _stub(rows)}, {"eval": OPEN_DECISIONS},
            {"until": f"{_CARDS}.length === {len(rows)}"}, *({"eval": e} for e in cards)])
    ids, html, text = out["values"][-3:]
    return dict(zip(ids, zip(html, text)))


# the consent row the page drew for _consent_row() before the voice-key branch was added (outerHTML, recorded from the
# unchanged renderer)
CONSENT_BEFORE = (
    '<div class="row consent-row grant-row" data-consent-grant="cg-0000000000aa"><div class="consent-head">'
    '<span class="consent-agent">claude_code</span><b class="consent-tool">Write</b>'
    '<span class="ext-meta">standing grant · live</span><span class="consent-left">answered nothing yet</span></div>'
    '<div class="consent-facts"><span class="ext-meta">in /work/proj</span><span class="ext-meta">default mode</span>'
    '<span class="ext-meta">until 2026-12-01T00:00:00Z</span><span class="ext-meta">consent cg-0000000000aa</span></div>'
    '<div class="consent-cmd"><code class="consent-code">' + _REVOKE_HTML + 'cg-0000000000aa</code>'
    '<button class="undo-btn consent-copy" type="button" data-label="revoke" title="copy the revoke command; run it in a '
    'terminal with your signing key. This page signs nothing">revoke</button></div></div>')


@needs_browser
def test_a_voice_key_row_names_its_provider_and_says_it_is_live_until_revoked(world, tmp_path, monkeypatch):
    """Red with the voice-key branch of grantCard reverted: the row said "voice-key <id>" for a name, "answered nothing
    yet", and an empty "until " (both renderers drew every grant as an expiring consent grant)."""
    rows = [_voice_row("openai", "vk-0000000000aa"), _voice_row("deepgram", "vk-0000000000bb")]
    got = _drive_cards(tmp_path, world, monkeypatch, rows)
    assert set(got) == {"vk-0000000000aa", "vk-0000000000bb"}, got
    for gid, label in (("vk-0000000000aa", "OpenAI voice key"), ("vk-0000000000bb", "Deepgram voice key")):
        html, text = got[gid]
        assert label in text, text
        assert "Live until you revoke it" in text, text
        assert "answered" not in text, text                       # no "answered nothing yet", and no count
        assert "until" not in text.replace("Live until you revoke it", ""), text   # no empty "until <date>" beside it
        assert "standing grant · live" in text and "voice-key " + gid in text, text
        assert "consent-left" not in html and "consent-tool" not in html, html
        # the revoke control is the consent row's own: the same command line and the same button
        assert '<code class="consent-code">' + _REVOKE_HTML + gid + "</code>" in html, html
        assert 'class="undo-btn consent-copy"' in html and ">revoke</button>" in html, html


@needs_browser
def test_a_consent_row_is_drawn_exactly_as_it_was_beside_a_voice_key(world, tmp_path, monkeypatch):
    """The consent row is string-equal to the row the unchanged renderer drew for the same fixture. Red with the branch
    keyed on anything wider than ``kind === "voice-key"`` (a consent row would lose its answered line or its date)."""
    rows = [_consent_row(), _voice_row("openai", "vk-0000000000aa")]
    got = _drive_cards(tmp_path, world, monkeypatch, rows)
    assert got["cg-0000000000aa"][0] == CONSENT_BEFORE, got["cg-0000000000aa"][0]
    assert "OpenAI voice key" in got["vk-0000000000aa"][1]


def _voice_key_in(world, tmp_path) -> dict:
    """A real voice-key record, signed into ``world``'s cockpit by its laptop key, over a fake keychain."""
    sec = FakeSecurity(tmp_path)
    record, _warning = custody.add_key(
        world.root, provider="openai", uses=("stt", "tts"), account="levain-voice", value=Secret("vkv-GRANTPAGE-0123456789"),
        source="levain-voice-openai-grantpage", confirm=lambda preview: True, writer=SecurityStdinWriter(sec.path),
        items=KeychainItems(sec.path), store=KeychainStore(sec.path), signer_fp=world.lfp, sign=world.laptop_sign,
        clock=lambda: datetime.now(timezone.utc))
    return record


@needs_browser
def test_the_page_draws_a_real_voice_key_the_way_the_stub_does(world, tmp_path, monkeypatch):
    """Over the real grants read (grant_view): the consent grant and the voice key both draw, and the key's row is the
    voice-key row. Red with the branch reverted, as the stubbed test is."""
    monkeypatch.setenv("LEVAIN_HOME", str(world.qdir.parent))
    _grant(world, "cg-0000000000aa", "Write")
    record = _voice_key_in(world, tmp_path)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": TILE}, {"eval": OPEN_DECISIONS},
            {"until": f"{_CARDS}.length === 2"},
            {"eval": f"{_CARDS}.map(c => c.dataset.consentGrant)"},
            {"eval": f"{_CARDS}.map(c => c.innerText)"}])
    ids, texts = out["values"][-2:]
    by = dict(zip(ids, texts))
    assert set(by) == {"cg-0000000000aa", record["id"]}, ids
    voice, consent = by[record["id"]], by["cg-0000000000aa"]
    assert "OpenAI voice key" in voice and "Live until you revoke it" in voice and "answered" not in voice, voice
    assert "until" in consent and "answered nothing yet" in consent and "Live until you revoke it" not in consent, consent


@needs_browser
def test_the_grant_slot_shots(world, tmp_path, monkeypatch):
    """The grants slot with both kinds of row, as a phone shows it and as a desktop does. ``LEVAIN_GRANTPAGE_SHOTS=<dir>``
    says where the PNGs go and ``LEVAIN_GRANTPAGE_PREFIX`` names them (before or after); else they land in the tmp dir and
    the test only proves the page draws."""
    out_dir = Path(os.environ.get("LEVAIN_GRANTPAGE_SHOTS") or tmp_path)
    prefix = os.environ.get("LEVAIN_GRANTPAGE_PREFIX") or "after"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = [_consent_row(), _voice_row("openai", "vk-0000000000aa"), _voice_row("deepgram", "vk-0000000000bb")]
    monkeypatch.setenv("LEVAIN_HOME", str(world.qdir.parent))
    _root, src, _ids = _install(tmp_path)
    look = ("(() => { const h = [...document.querySelectorAll('#levain-modal .ext-grouphead')].find(e => /standing grants/"
            ".test(e.textContent)); if (!h) return 'missing'; h.scrollIntoView({block: 'start'}); return 'ok'; })()")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        open_steps = [{"until": TILE}, {"eval": _stub(rows)}, {"eval": OPEN_DECISIONS},
                      {"until": f"{_CARDS}.length === 3", "ms": 30000}, SETTLE, {"eval": look}, SETTLE]
        phone = drive_touch(tmp_path, f"{url}/#write_token=T",
                            [*PHONE_TOUCH, *open_steps, {"shot": str(out_dir / f"{prefix}_390.png"), "full": False}])
        desk = _drive(tmp_path, f"{url}/#write_token=T",
                      [{"emulate": {"w": 1440, "h": 900, "dpr": 1}}, *open_steps,
                       {"shot": str(out_dir / f"{prefix}_1440.png"), "full": False}])
    assert "missing" not in phone["values"] and "missing" not in desk["values"]
    for name in ("390", "1440"):
        assert (out_dir / f"{prefix}_{name}.png").stat().st_size > 1000, name
