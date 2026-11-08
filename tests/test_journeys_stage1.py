"""Stage-1 golden journeys (levain 74, key 1108-39; Phill np-3d6e2bc0 (a): "build the 5 as proposed"). Five runs of what the
operator actually does on the cockpit, picked from 30 days of his usage ledgers (flow: projects/levain/reference/golden_paths_1108).
Each one drives the real page in the session's shared headless Chrome (``tests/_page_drive.mjs`` through ``_drive``) over the real
server and real stores, and asserts what HE sees at the end (the card closed, the signed line, the landed card, the receipt and the
thread row), with the store read only as the cross-check.

  1. sign a band answer in the cockpit's sign sheet with a passkey, an option picked
  2. the NEEDS band at phone width (390x844 by CDP device metrics), a tap on an option answers it
  3. a push arrives, its notification is tapped, the app lands on the pending card
  4. consent allow from the needs card, one tap
  5. a drop from the cockpit compose, then its receipt and its thread row

NEVER HIS LIVE STORES: every server, store and queue runs under the test's scratch ``LEVAIN_HOME`` / ``HOME`` in pytest's tmp, and
``_scratch`` refuses to go on if any resolved path is under one of his real stores. The push transport is stubbed at its seam
(``sender._post``), so no push ever leaves the machine. Not wired into CI (row 144 owns that)."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from levain.consent import band
from levain.consent.questions import QuestionStore
from tests import _shared_chrome
from tests.test_band_pages import OPTS, PHONE, _answer, _file
from tests.test_cockpit_consent_sheet_page import _ConsentRig
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_cockpit_sign_card import DESKTOP, QRID, STRIP_FACET, TOK, _Rig, needs_ssh
from tests.test_msgslot_hero import READY, RECEIPT_DONE, _sent
from tests.test_cockpit_narrow_layout_rig import NEEDS, FullRig

pytestmark = pytest.mark.journey

# --- the live-store guard ----------------------------------------------------------------------------------------------------------

_REAL_HOME = _shared_chrome._real_home()
_LIVE = (_REAL_HOME / ".levain", _REAL_HOME / ".levain-sign", _REAL_HOME / "Briefcase" / "flow" / "state")


def _under(path: Path, root: Path) -> bool:
    p, r = os.path.realpath(path), os.path.realpath(root)
    return p == r or p.startswith(r + os.sep) or p.startswith(str(root) + os.sep)


def _scratch(tmp_path: Path, *paths: Any) -> None:
    """Refuse to go on if LEVAIN_HOME, HOME or any store path the rig resolved is one of his real stores, or is outside this
    test's scratch dir. Called at fixture start (the environment) and again after each rig is built (its own paths)."""
    named = [Path(os.environ[k]) for k in ("LEVAIN_HOME", "HOME") if os.environ.get(k)] + [Path(str(p)) for p in paths if p is not None]
    scratch_roots = (tmp_path.parent, Path("/private/tmp"), Path("/tmp"), Path(os.environ.get("TMPDIR", "/nonexistent")))
    for p in named:
        for live in _LIVE:
            assert not _under(p, live), f"JOURNEY GUARD: {p} is under his live store {live}"
        assert any(_under(p, r) for r in scratch_roots) or _under(p, tmp_path), f"JOURNEY GUARD: {p} is not under a scratch root"


@pytest.fixture(autouse=True)
def _journey_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "levain_home"))
    _scratch(tmp_path)
    monkeypatch.setattr(band, "deliver_answer", lambda store, ref, **kw: type(
        "D", (), {"route": "hook", "reason": "next hook delivers it"})())
    yield


def test_the_guard_refuses_each_of_his_live_stores(tmp_path: Path) -> None:
    """The positive control: the guard fires on every live path it names, and passes a scratch one."""
    _scratch(tmp_path, tmp_path / "levain_home" / "consent")
    for live in _LIVE:
        with pytest.raises(AssertionError, match="JOURNEY GUARD"):
            _scratch(tmp_path, live / "queue.db")


# --- 1. sign a band answer in the cockpit's sign sheet with a passkey ----------------------------------------------------------

FRAME = "document.getElementById('levain-sign-sheet-frame')"
IN = f"({FRAME} && {FRAME}.contentDocument)"
OPEN_SHEET = "document.querySelector('#levain-modal .needs-question .sign-facet .sign-sheet-open')"
STRIP_Q = "document.querySelector('#ck-needs .needs-row.needs-question')"


@needs_browser
@needs_ssh
def test_j1_a_band_answer_is_signed_in_the_sheet_with_a_passkey_and_the_cockpit_is_back_under_it(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    _scratch(tmp_path, rig.qdir, rig.allowed)
    version = rig.signer.row(QRID, [rig.pk.key.fingerprint])["sha256"][:16]
    signed_file = rig.qdir / "sign" / f"{QRID}.signed.json"
    assert not signed_file.exists()                                          # nothing signed yet: any record after the run is this run's
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(),
            {"until": f"{STRIP_Q} && 1", "ms": 30000},
            {"eval": f"{STRIP_Q}.innerText"},                                    # what he sees waiting: the question
            {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"tap": OPEN_SHEET},
            {"until": f"{IN} && {IN}.getElementById('sheet') && {IN}.getElementById('sheet').dataset.state", "ms": 30000},
            {"eval": f"{IN}.getElementById('sheet-title').textContent"},
            {"tap": f"{IN}.querySelector('#sheet-actions [data-choice=\"a\"]')"},   # option (a), "Ship it tonight"
            {"until": f"/^signed/.test({IN}.getElementById('sheet-note').textContent) && {IN}.getElementById('sheet-note').textContent", "ms": 30000},
            {"eval": f"{FRAME}.getAttribute('src')"},
            {"tap": f"{IN}.getElementById('sheet-close')"},
            {"until": f"!{FRAME} && 1", "ms": 5000},
            {"eval": f"JSON.stringify({{modal: !!document.querySelector('#levain-modal .needs-question'), strip: !!{STRIP_Q}}})"},
        ])
    v = out["values"]
    assert "Ship the sweep fix tonight or hold it?" in v[1], v
    assert v[5] == "t", v                                                    # the sheet opened on the rig's request
    assert str(v[7]).startswith("signed (a)"), v                             # what he reads at the end
    assert v[8] == f"/sign/{QRID}#v={version}", v                            # the card's listed version rode the link
    assert json.loads(v[-1]) == {"modal": True, "strip": True}, v           # he is back on the cockpit's card (the desk's reader, not the page, closes it)
    assert signed_file.exists()                                              # the record THIS run wrote (it was absent before the tap): signing did something
    assert rig.verifies(QRID)                                                # cross-check: ssh-keygen accepts the signature
    signed = json.loads(signed_file.read_text())
    assert signed["signer_fp"] == rig.pk.key.fingerprint and signed["namespace"] == "flow-needs-phill", signed   # his passkey signed it
    assert signed["choice"] == "a" and "Ship it tonight" in signed["signed_message"], signed   # the option he tapped is the one signed


# --- 2. the NEEDS band at phone width, a tap on an option answers it -------------------------------------------------------------

@needs_browser
def test_j2_the_needs_band_at_phone_width_a_tap_on_an_option_answers_it(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    lh = tmp_path / "levain_home" / "consent"
    lh.mkdir(parents=True, mode=0o700)
    store = QuestionStore(lh)
    _scratch(tmp_path, lh, store.path)
    q1, q2 = _file(store, "Which retry policy?"), _file(store, "Ship the sweep fix tonight?", rec="fix")
    pick = "document.querySelector('#levain-modal .needs-question .question-option[title=\"answer with option exp\"]')"
    with _serve(src, write_token=TOKEN, install_class="reported") as (base, _h):
        out = _drive(tmp_path, f"{base}/#write_token={TOKEN}", [
            PHONE,
            {"until": "document.querySelectorAll('#ck-needs .needs-row.needs-question').length === 2 && 1", "ms": 30000},
            {"eval": "JSON.stringify({w: innerWidth, h: innerHeight, over: document.documentElement.scrollWidth > innerWidth, "
                     "strip: document.querySelector('#ck-needs').innerText})"},
            {"eval": "document.querySelector('#ck-needs .needs-row.needs-question').click() || 1"},
            {"until": f"{pick} && 1", "ms": 30000},
            {"eval": "document.querySelector('#levain-modal .needs-q-title').textContent"},
            {"tap": pick},
            {"until": "document.querySelectorAll('#ck-needs .needs-row.needs-question').length === 1 && 1", "ms": 30000},
            {"eval": "document.querySelector('#ck-needs').innerText"},
        ])
    v = out["values"]
    seen = json.loads(v[1])
    assert (seen["w"], seen["h"]) == (390, 844) and not seen["over"], seen   # the phone frame is real, and nothing runs off the side
    assert "Which retry policy?" in seen["strip"] and "Ship the sweep fix tonight?" in seen["strip"], seen
    assert v[4] == "Which retry policy?" or v[4] == "Ship the sweep fix tonight?", v
    waiting = v[7]
    assert v[4] not in waiting, v                                            # the card he answered left the strip
    answered, other = (q1, q2) if v[4] == "Which retry policy?" else (q2, q1)
    a = _answer(store, answered.n)
    assert (a.kind, a.value, a.channel) == ("option", "exp", "cockpit"), a
    assert _answer(store, other.n) is None


# --- 3. a push arrives, the notification is tapped, the app lands on the pending card ---------------------------------------------

NP_ID = "np-1a2b3c4d"


def _sub(n: int = 1) -> dict[str, Any]:
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    def b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).decode().rstrip("=")
    point = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"endpoint": f"https://fcm.googleapis.com/fcm/send/journey{n}", "keys": {"p256dh": b64(point), "auth": b64(b"a" * 16)}}


@needs_browser
def test_j3_a_push_is_tapped_and_the_app_lands_on_the_pending_card(tmp_path: Path, monkeypatch) -> None:
    from levain.webpush import presence, sender
    from tests.test_cockpit_pairing_http import _pair
    from tests.test_cockpit_webpush import T as PUSH_T, _call, _signed

    monkeypatch.setenv("LEVAIN_DRIVE_FULL_CHROME", "1")                      # a service worker and Push: not in the session's shell
    _root, src, _ids = _install(tmp_path)
    qdir = tmp_path / "signq"                                                # the signer's dir: the push store and the pairing journal live here
    qdir.mkdir(mode=0o700)
    lh = tmp_path / "levain_home" / "consent"                                # the questions the cockpit reads
    lh.mkdir(parents=True, mode=0o700)
    store = QuestionStore(lh)
    _scratch(tmp_path, qdir, lh, store.path)
    _file(store, "Ship the sweep fix tonight?", rec="fix")                   # a decoy card, number 1
    q = _file(store, "Which retry policy?")                                  # the card the push is about, number 2
    # flow's band rows carry its own `np-<8 hex>` item id, and the band read pairs it with the card number: here the target is filed under
    # such an id (no card number is 1 + its hex), so only the page's band-read lookup can find the card the link names
    with sqlite3.connect(store.path) as c:
        c.execute("DROP TRIGGER questions_frozen")                           # the scratch copy only: the store refuses a rewrite of its own id
        c.execute("UPDATE questions SET id = ? WHERE n = ?", (NP_ID, q.n))
    # the push, through the server's own sender: a live subscription, the transport stubbed at its seam (no push leaves the machine),
    # the payload bytes read at build_payload (the JSON the worker would decrypt). The server builds the link itself: push_url runs
    # unpatched on flow's real ref shape.
    sub = _sub()
    posted: list[str] = []
    built: list[bytes] = []
    real_build = sender.build_payload
    monkeypatch.setattr(sender, "_post", lambda req: posted.append(req.full_url) or 201)
    monkeypatch.setattr(sender, "build_payload", lambda *a, **k: built.append(real_build(*a, **k)) or built[-1])
    monkeypatch.setattr(presence, "should_push", lambda *a, **k: presence.Decision(True, "pushing", "journey"))
    with _signed(src, qdir) as (port, _httpd):
        _dev, dev_tok = _pair(port, label="iPhone")                          # his paired phone, then its push subscription
        assert _call(port, "POST", "/webpush/subscribe", sub, token=dev_tok)[0] == 200
        sender.send(qdir, kind="attention", ref=NP_ID, title="Which retry policy?", body="the feed PR is waiting on you", url="/")
        assert posted == [sub["endpoint"]], posted                           # the push went to the stub, once, and nowhere else
        payload = json.loads(built[-1])
        assert payload["url"] == f"/?view=needs&item={NP_ID}" and isinstance(payload["ping"], int), payload
        out = _phone_tap(tmp_path, f"http://localhost:{port}/#write_token={PUSH_T}", payload)
        quiet = _phone_tap(tmp_path, f"http://localhost:{port}/#write_token={PUSH_T}", payload, tap=False)
    # the control: the same push, no tap: the notification is shown and the app stays where it was (so the landing above is the tap's)
    assert json.loads(quiet["values"][4]) == {"shown": 1, "tapped": False} and quiet["values"][5] == "still home", quiet["values"]
    v = out["values"]
    tapped = json.loads(v[4])
    assert tapped["shown"] == 1 and tapped["title"] == "Which retry policy?" and tapped["closed"] == 1, tapped
    assert tapped["url"] == payload["url"], tapped                           # the notification carries the server's link
    assert v[6] == "Which retry policy?", v                                  # the app landed on that pending card, open
    assert v[3] == "home", v                                                 # and it did so from the tap, not because it was already open
    assert _answer(store, q.n) is None                                       # landing answers nothing


def _phone_tap(tmp_path: Path, url: str, payload: dict, *, tap: bool = True) -> dict:
    worker = ("(async () => { const shown = []; const work = []; const reg = self.registration;"
              " reg.showNotification = async (title, o) => { shown.push({title, tag: o.tag, data: o.data}); };"      # permission is not granted headless
              " reg.getNotifications = async () => []; "
              " const ev = new PushEvent('push', {data: %s}); ev.waitUntil = (p) => { work.push(Promise.resolve(p)); };"
              " self.dispatchEvent(ev); await Promise.allSettled(work);"
              " if (!%s) return JSON.stringify({shown: shown.length, tapped: false});"
              " const n = shown[0]; const click = new Event('notificationclick'); const closed = [];"
              " click.notification = {close: () => closed.push(1), data: n.data, tag: n.tag}; click.action = '';"
              " const w2 = []; click.waitUntil = (p) => { w2.push(Promise.resolve(p)); };"
              " self.dispatchEvent(click); await Promise.allSettled(w2);"
              " return JSON.stringify({shown: shown.length, title: n.title, url: n.data.url, closed: closed.length}); })()" % (json.dumps(json.dumps(payload)), "true" if tap else "false"))
    return _drive(tmp_path, url, [
            PHONE,
            {"until": "document.querySelector('#ck-needs .needs-row') && 1", "ms": 30000},
            {"eval": "navigator.serviceWorker.register('/sw.js', {scope: '/'}).then(() => navigator.serviceWorker.ready).then(() => 1)"},
            {"until": "navigator.serviceWorker.controller && 1", "ms": 20000},   # the page is under the worker, as the installed app is
            {"eval": "document.querySelector('#levain-modal') ? 'modal' : 'home'"},
            {"sw": worker, "ms": 40000},                                      # the real push handler, then the real notificationclick handler
            *([{"until": "document.querySelector('#levain-modal .needs-question') && 1", "ms": 30000},
               {"eval": "document.querySelector('#levain-modal .needs-q-title').textContent"}] if tap else
              [{"eval": "new Promise(r => setTimeout(r, 4000)).then(() => document.querySelector('#levain-modal .needs-question') ? 'landed' : 'still home')"}]),
    ])


# --- 4. consent allow from the needs card, one tap -----------------------------------------------------------------------------

@needs_browser
@needs_ssh
def test_j4_consent_allow_from_the_needs_card_is_one_tap(tmp_path: Path, monkeypatch) -> None:
    rig = _ConsentRig(tmp_path, monkeypatch)
    _scratch(tmp_path, rig.qdir)
    allow = "document.querySelector('#levain-modal .needs-consent [data-consent-sheet=\"allow\"]')"
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(),
            {"until": "document.querySelector('#ck-needs .needs-consent')", "ms": 30000},
            {"eval": "document.querySelector('#ck-needs .needs-consent').innerText"},
            {"eval": "document.querySelector('#ck-needs .needs-consent').click() || 1"},
            {"until": f"{allow} && 1", "ms": 30000},
            {"eval": f"{allow}.click() || 1"},
            {"until": f"{IN} && {IN}.getElementById('sheet') && {IN}.getElementById('sheet').dataset.state", "ms": 30000},
            {"eval": f"!!{IN}.getElementById('sheet-picked') && {IN}.getElementById('sheet-picked').textContent"},
            {"tap": f"{IN}.getElementById('sheet-picked')"},                    # the one tap
            {"until": f"/^signed/.test({IN}.getElementById('sheet-note').textContent) && {IN}.getElementById('sheet-note').textContent", "ms": 30000},
        ])
    v = out["values"]
    assert "Write" in str(v[1]), v                                           # the card names the tool call that is waiting
    assert v[6] == "Sign (allow)", v                                         # allow was already picked: one tap is all that is left
    assert str(v[8]).startswith("signed (allow)"), v                         # what he reads
    assert rig.state() == "allowed"                                          # cross-check: the queue verified the signature
    posts = [p.split(" ", 1)[0] for p in out["posts"]]
    assert posts[-2:] == [f"/cockpit/consent/{rig.enc()}/challenge", f"/cockpit/consent/{rig.enc()}/answer"], posts


# --- 5. a drop from the cockpit compose, then its receipt and its thread -------------------------------------------------------

@needs_browser
@needs_ssh
def test_j5_a_drop_from_the_compose_shows_its_receipt_and_its_thread_row(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    _scratch(tmp_path, rig.qdir)
    words = "tell the coordinator: land c-12"
    after = ("JSON.stringify({box: window.LevainDashboard.dropBoxText(document.getElementById('ck-drop-words')),"
             " receipt: document.querySelector('#ck-drop .drop-receipt').textContent,"
             " rows: [...document.querySelectorAll('#ck-drop .drop-recent-row')].map(r => r.textContent)})")
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            {"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000},
            *_sent(words, posts=1),
            {"until": RECEIPT_DONE + " && document.querySelectorAll('#ck-drop .drop-recent-row').length === 1", "ms": 20000},
            {"eval": after},
        ])
    a = json.loads(out["values"][-1])
    assert a["box"] == "", a                                                 # the compose emptied: it was sent
    assert a["receipt"].startswith(f"Sent to the desk: “{words}”"), a        # the receipt he reads
    assert len(a["rows"]) == 1 and words in a["rows"][0] and "unsigned" in a["rows"][0], a   # the thread row, read back from the store
    assert [p.split(" ", 1)[0] for p in out["posts"]] == ["/cockpit/coord/drop"], out["posts"]
