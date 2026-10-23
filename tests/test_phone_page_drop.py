"""The phone page's Drop tab, in headless Chrome at a phone's width (390), over the real phone server.

One run through the page, from a PAIRED DEVICE's token (the coordinator routes refuse this run's phone token): a drop
sent while its reply is lost (the page's fetch throws AFTER the server accepted), so it stays in the outbox and is sent
again under the same outbox id, and the store files it once; that accepted drop wakes the coordinator through the CLI's
own ``launch_session`` (here on an install whose hook is not live, so nothing starts) and the page shows the ``woken``
receipt. Then an envelope reviewed on the server's sheet and signed with Chrome's virtual authenticator holding the SAME
software passkey the cockpit enrolled, whose signature is made stale on the way (its ``signed_at`` minted five minutes
back, once): the store refuses it ``envelope-stale`` and files nothing, the outbox card offers Sign again, and the second
signature is stored with its envelope. Last, an outbox item bound to another store identity is shown as "from another
Levain" and never sent. Screenshots go to ``LEVAIN_SHOT_DIR`` when it is set (the lane's RUN), else the test's tmp dir."""
from __future__ import annotations

import json
import os
import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from levain.coordinator import envelope as env
from levain.coordinator.envelopes import EnvelopeStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_coordinator_phone import device_token
from tests.test_dispatch_phone import repo, world  # noqa: F401  (fixtures)
from tests.test_phone_dispatch import _serve
from tests.test_phone_page_dispatch import _authenticator

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

NOTE = "document.getElementById('drop-note').textContent"
# the reply to the FIRST /coord/drop is lost: the request reaches the server, the page sees a network failure
LOSE_FIRST_REPLY = """(() => { const f = window.fetch; let lost = false; window.fetch = (u, o) => {
  const p = f(u, o);
  if (!lost && String(u).endsWith('/coord/drop')) { lost = true; return p.then(() => { throw new TypeError('lost'); }); }
  return p; }; return 1; })()"""
OTHER_STORE = "st-" + "0" * 32
FOREIGN_ID = "ph-" + "f" * 32
# an outbox item another Levain's store made (the phone name now answers for another dispatch directory), written to
# storage as another tab would, and the storage event that tab's write fires here
INJECT_FOREIGN = f"""(() => {{ const k = 'levain-outbox:' + location.hostname;
  const all = JSON.parse(localStorage.getItem(k) || '[]');
  all.push({{ outbox_id: '{FOREIGN_ID}', words: 'made for the other Levain', sent_at: '2026-10-22T10:00:00.000000Z',
             state: 'waiting', envelope: null, store_id: '{OTHER_STORE}' }});
  localStorage.setItem(k, JSON.stringify(all));
  window.dispatchEvent(new StorageEvent('storage', {{ key: k }})); return 1; }})()"""
OUTBOX_CARD = "(() => { const c = document.querySelector('#drop-outbox .row'); return c && c.textContent; })()"


def _set(id_: str, value: str) -> str:
    return (f"(() => {{ const e = document.getElementById({json.dumps(id_)}); e.value = {json.dumps(value)};"
            " e.dispatchEvent(new Event('input', {bubbles: true})); return 1; })()")


def _click(text_re: str, scope: str = "#drop-review") -> str:
    return f"[...document.querySelectorAll('{scope} button')].find(b => /{text_re}/.test(b.textContent)).click() || 1"


@needs_browser
def test_drop_wake_stale_sign_again_and_another_levain_through_the_page(world, tmp_path: Path, monkeypatch) -> None:
    shots = Path(os.environ.get("LEVAIN_SHOT_DIR") or tmp_path)
    repo_path = str(world["repo"][0])
    install = tmp_path / "coord-install"
    install.mkdir()
    EnvelopeStore(world["qdir"]).coord_set_agent("claude", model="haiku", cwd=str(install.resolve()))
    store_id = EnvelopeStore(world["qdir"]).store_id()
    dev = device_token(world["qdir"])
    # the FIRST payload the server mints is dated five minutes back: the signature the passkey then makes is valid and
    # stale, as one held in the outbox past the store's skew is
    real, shifted = env.sign_payload, []

    def stale_once(body, *, signer_fp, signed_at):
        if not shifted:
            shifted.append(signed_at)
            signed_at = signed_at - timedelta(minutes=5)
        return real(body, signer_fp=signer_fp, signed_at=signed_at)

    monkeypatch.setattr(env, "sign_payload", stale_once)
    with _serve(world["qdir"], world["qdir"]) as (base, httpd):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={dev}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            _authenticator(world["phone"]),
            {"until": "document.getElementById('status').textContent !== 'reading the queue…' && 1"},
            {"eval": "document.getElementById('tab-drop').click() || 1"},
            {"shot": str(shots / "fix1_drop_before.png")},
            {"eval": LOSE_FIRST_REPLY},
            {"eval": _set("drop-words", "seeing suite gate errors from the couch, please get them fixed")},
            {"eval": "document.getElementById('drop-send').click() || 1"},
            # the lost reply: the drop stays in the outbox, said as not delivered yet
            {"until": "(() => { const n = document.querySelector('#drop-outbox .row .note'); return n && n.textContent; })()"},
            {"eval": "window.dispatchEvent(new Event('online')) || 1"},
            {"until": f"/^delivered/.test({NOTE}) && {NOTE}"},
            # the receipt the wake wrote after the reply (a thread the reply never waited on), as the page lists it
            {"until": "(() => { const r = [...document.querySelectorAll('#drop-recent .row.drop .receipt')]"
                      ".map(e => e.textContent).find(t => /^receipt: coordinator/.test(t)); return r || null; })()"},
            {"shot": str(shots / "fix1_drop_woken.png"), "full": True},
            # the envelope: reviewed on the server's sheet, signed at Sign (this first signature is made stale)
            {"eval": _set("drop-words", "build the fix and land it")},
            {"eval": "document.getElementById('drop-env').open = true; 1"},
            {"eval": _set("drop-repo", repo_path)},
            {"eval": _set("drop-onto", "main")},
            {"eval": _set("drop-calls", "40")},
            {"eval": "document.getElementById('drop-sign').click() || 1"},
            {"until": "(() => { const p = document.querySelector('#drop-review pre.display'); return p && p.textContent; })()"},
            {"eval": "document.querySelector('#drop-review .dg-val').title"},
            {"eval": _click("^Sign")},
            # refused, never filed plain: the card waits with Sign again and Send as a plain drop
            {"until": f"(() => {{ const t = {OUTBOX_CARD}; return t && /refused/.test(t) && /Sign again/.test(t) && t; }})()"},
            {"shot": str(shots / "fix1_stale_refused.png"), "full": True},
            {"eval": _click("^Sign again$", "#drop-outbox")},
            {"until": "(() => { const p = document.querySelector('#drop-review pre.display'); return p && p.textContent; })()"},
            {"eval": _click("^Sign")},
            {"until": f"/envelope ev-[0-9a-f]{{32}} taken/.test({NOTE}) && {NOTE}"},
            {"until": "document.querySelectorAll('#drop-recent .row.drop').length === 2 && "
                      "document.querySelector('#drop-outbox').children.length === 0 && 1"},
            {"shot": str(shots / "fix1_signed_again.png"), "full": True},
            # another Levain's item: shown, never sent
            {"eval": INJECT_FOREIGN},
            {"until": f"(() => {{ const t = {OUTBOX_CARD}; return t && /from another Levain/.test(t) && t; }})()"},
            {"eval": "window.dispatchEvent(new Event('online')) || 1"},
            {"until": "new Promise(r => setTimeout(() => r(1), 1500))"},
            {"eval": "JSON.parse(localStorage.getItem('levain-outbox:' + location.hostname)).map(x => x.outbox_id)"},
            {"eval": "document.documentElement.scrollWidth > innerWidth"},
            {"shot": str(shots / "fix1_another_levain.png"), "full": True},
        ])
        httpd.coord.wait_wakes(30)
    vals = out["values"]
    # every eval, until and shot collects one value, in step order (emulate and authenticator collect none)
    delivered, woken, digest, refused_card = vals[8], vals[9], vals[18], vals[20]
    taken, foreign_card, left, sideways = vals[25], vals[29], vals[32], vals[33]
    # the reply the page saw is the RESEND's, which wakes nothing; the lost first reply's drop woke the coordinator
    assert "(it had arrived before: filed once)" in delivered and "coordinator launch" not in delivered, delivered
    assert "not started" in woken, woken
    assert "Send as a plain drop" in refused_card and "sign it again" in refused_card, refused_card
    assert "from another Levain" in foreign_card and "Sign again" not in foreign_card, foreign_card
    assert left == [FOREIGN_ID], left
    posts = [(p.split(" ", 1)[0], json.loads(p.split(" ", 1)[1])) for p in out["posts"]]
    drops = [b for r, b in posts if r == "/coord/drop"]
    # lost reply + resend, the stale signed drop, the second signature: and never the foreign item
    assert len(drops) == 4 and all(b["store_id"] == store_id for b in drops), [b["outbox_id"] for b in drops]
    assert drops[0]["outbox_id"] == drops[1]["outbox_id"] and drops[2]["outbox_id"] == drops[3]["outbox_id"]
    assert FOREIGN_ID not in {b["outbox_id"] for b in drops}
    assert [r for r, _ in posts if r != "/coord/drop"] == ["/coord/envelope-review", "/coord/envelope-challenge"] * 2
    store = EnvelopeStore(world["qdir"])
    stored = store.drops()
    assert len(stored) == 2 and {d.surface for d in stored} == {"phone"}       # filed once each, nothing plain
    signed = next(d for d in stored if d.envelope_id)
    plain = next(d for d in stored if not d.envelope_id)
    assert signed.outbox_id == drops[2]["outbox_id"]
    e = store.envelope(signed.envelope_id)
    assert e.signer_fp == world["phone"].fp and e.signer_class == "display-phone"
    assert e.body["lands"][0]["onto_from"] == "inferred" and e.body["budget"]["tool_calls"] == 40
    assert signed.envelope_id in taken and digest and digest != e.digest       # signed again: a fresh review, new ids
    (w,) = [r.body for r in store.drop_receipts(plain.drop_id) if r.body.get("stage") == "woken"]
    assert w["started"] is False and w["launch_id"] in woken and w["said"] in woken
    assert sideways is False


OUTBOX_META = "document.getElementById('drop-outbox-meta').textContent"


@needs_browser
def test_f2_a_fresh_install_signs_its_first_drop_through_the_page(world, tmp_path: Path) -> None:
    # L3 r2 (both seats): no store at all, so the listing names none and the REVIEW makes it; the review's reply names
    # the store, the page binds the signed drop to it, and the first-ever signed drop is stored with its envelope
    shots = Path(os.environ.get("LEVAIN_SHOT_DIR") or tmp_path)
    from levain.dispatch.jobs import DB_NAME

    assert not (world["qdir"] / DB_NAME).exists()
    repo_path = str(world["repo"][0])
    dev = device_token(world["qdir"])
    with _serve(world["qdir"], world["qdir"]) as (base, httpd):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={dev}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            _authenticator(world["phone"]),
            {"until": "document.getElementById('status').textContent !== 'reading the queue…' && 1"},
            {"eval": "document.getElementById('tab-drop').click() || 1"},
            {"eval": _set("drop-words", "the first drop this Levain ever gets")},
            {"eval": "document.getElementById('drop-env').open = true; 1"},
            {"eval": _set("drop-repo", repo_path)},
            {"eval": _set("drop-onto", "main")},
            {"eval": "document.getElementById('drop-sign').click() || 1"},
            {"until": "(() => { const p = document.querySelector('#drop-review pre.display'); return p && p.textContent; })()"},
            {"eval": _click("^Sign")},
            {"until": f"/envelope ev-[0-9a-f]{{32}} taken/.test({NOTE}) && {NOTE}"},
            {"until": "document.querySelectorAll('#drop-recent .row.drop').length === 1 && "
                      "document.querySelector('#drop-outbox').children.length === 0 && 1"},
            {"eval": OUTBOX_META},
            {"shot": str(shots / "fix2_fresh_signed.png"), "full": True},
        ])
    vals = out["values"]
    taken, meta = vals[9], vals[11]
    store = EnvelopeStore(world["qdir"])
    (d,) = store.drops()
    assert d.envelope_id and d.envelope_id in taken, taken
    posts = [(p.split(" ", 1)[0], json.loads(p.split(" ", 1)[1])) for p in out["posts"]]
    assert [r for r, _ in posts] == ["/coord/envelope-review", "/coord/envelope-challenge", "/coord/drop"]
    assert posts[2][1]["store_id"] == store.store_id()
    assert "one tab at a time" not in meta, meta          # this Chrome has Web Locks


@needs_browser
def test_f2_a_lost_first_reply_that_made_the_store_clears_from_the_outbox(world, tmp_path: Path) -> None:
    # L3 r2 codex MED: the first plain drop makes the store and its reply is lost; the next listing names a store the
    # item (bound to none) never saw, and its one receipt-only retry is answered from the resend lookup
    shots = Path(os.environ.get("LEVAIN_SHOT_DIR") or tmp_path)
    dev = device_token(world["qdir"])
    with _serve(world["qdir"], world["qdir"]) as (base, httpd):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={dev}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"until": "document.getElementById('status').textContent !== 'reading the queue…' && 1"},
            {"eval": "document.getElementById('tab-drop').click() || 1"},
            {"eval": LOSE_FIRST_REPLY},
            {"eval": _set("drop-words", "the drop that made the store")},
            {"eval": "document.getElementById('drop-send').click() || 1"},
            {"until": "(() => { const n = document.querySelector('#drop-outbox .row .note'); return n && n.textContent; })()"},
            # the listing now names the store this drop made
            {"eval": "document.getElementById('refresh').click() || 1"},
            {"until": f"/^delivered/.test({NOTE}) && {NOTE}"},
            {"until": "document.querySelector('#drop-outbox').children.length === 0 && "
                      "document.querySelectorAll('#drop-recent .row.drop').length === 1 && 1"},
            {"shot": str(shots / "fix2_lost_first_reply.png"), "full": True},
        ])
    delivered = out["values"][7]
    assert "(it had arrived before: filed once)" in delivered, delivered
    store = EnvelopeStore(world["qdir"])
    (d,) = store.drops()
    drops = [json.loads(p.split(" ", 1)[1]) for p in out["posts"] if p.startswith("/coord/drop ")]
    assert len(drops) == 2 and {b["outbox_id"] for b in drops} == {d.outbox_id}
    assert [b["store_id"] for b in drops] == [None, None]


FUTURE = "2099-01-01T00:00:00.000000Z"       # a sent_at far past any store clock: the store refuses it, whatever it signed
SIGNED_ID = "ph-" + "a3" * 16
PLAIN_ID = "ph-" + "b3" * 16
# a refused signed item whose sent_at runs ahead of the store's clock, as storage and the storage event of another tab
INJECT_REFUSED = """(() => {{ const k = 'levain-outbox:' + location.hostname;
  const all = JSON.parse(localStorage.getItem(k) || '[]');
  all.push({{ outbox_id: {oid}, words: 'signed on a fast clock', sent_at: '{future}', state: 'refused', envelope: {{ body_text: 'x' }},
             why: 'sent_at is ahead of the store clock', error: 'refused', store_id: {sid}, signer_fp: {fp}, raw_id: null,
             spec: {{ where: [{{ repo: {repo}, paths: ['.'] }}], lands: [{{ repo: {repo}, onto: 'main' }}], stop: '2h',
                     budget: {{ tool_calls: 0, tokens: 0, hours: 0 }} }} }});
  localStorage.setItem(k, JSON.stringify(all));
  window.dispatchEvent(new StorageEvent('storage', {{ key: k }})); return 1; }})()"""


@needs_browser
def test_f3_both_retries_of_a_clock_ahead_refusal_take_a_fresh_sent_at(world, tmp_path: Path) -> None:
    # L3 r3 LOW 5: a signed drop refused because its sent_at is ahead of the store clock repeated the same 422 on both
    # "Sign again" and "Send as a plain drop"; each now takes a fresh sent_at
    repo_path = str(world["repo"][0])
    store_id = EnvelopeStore(world["qdir"]).store_id()
    dev = device_token(world["qdir"])

    def inject(oid: str) -> str:
        return INJECT_REFUSED.format(oid=json.dumps(oid), future=FUTURE, sid=json.dumps(store_id),
                                     fp=json.dumps(world["phone"].fp), repo=json.dumps(repo_path))

    with _serve(world["qdir"], world["qdir"]) as (base, httpd):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={dev}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            _authenticator(world["phone"]),
            {"until": "document.getElementById('status').textContent !== 'reading the queue…' && 1"},
            {"eval": "document.getElementById('tab-drop').click() || 1"},
            {"eval": inject(SIGNED_ID)},
            {"until": f"(() => {{ const t = {OUTBOX_CARD}; return t && /Sign again/.test(t) && t; }})()"},
            {"eval": _click("^Sign again$", "#drop-outbox")},
            {"until": "(() => { const p = document.querySelector('#drop-review pre.display'); return p && p.textContent; })()"},
            {"eval": _click("^Sign")},
            {"until": f"/envelope ev-[0-9a-f]{{32}} taken/.test({NOTE}) && {NOTE}", "ms": 20000},
            {"until": "document.querySelector('#drop-outbox').children.length === 0 && 1"},
            {"eval": inject(PLAIN_ID)},
            {"until": f"(() => {{ const t = {OUTBOX_CARD}; return t && /Send as a plain drop/.test(t) && t; }})()"},
            {"eval": _click("^Send as a plain drop$", "#drop-outbox")},
            {"until": "document.querySelector('#drop-outbox').children.length === 0 && "
                      "document.querySelectorAll('#drop-recent .row.drop').length === 2 && 1", "ms": 20000},
        ])
    posts = [(p.split(" ", 1)[0], json.loads(p.split(" ", 1)[1])) for p in out["posts"]]
    drops = [b for r, b in posts if r == "/coord/drop"]
    assert len(drops) == 2 and all(b["sent_at"] < "2030" for b in drops), [b["sent_at"] for b in drops]
    assert [b["outbox_id"] for b in drops] == [SIGNED_ID, PLAIN_ID]
    stored = EnvelopeStore(world["qdir"]).drops()
    assert len(stored) == 2 and sum(1 for d in stored if d.envelope_id) == 1


# every /coord/drop answers 409 other_store, as a store that was replaced since the listing would (nothing is written)
ANSWER_OTHER_STORE = """(() => { const f = window.fetch; window.fetch = (u, o) => {
  if (String(u).endsWith('/coord/drop')) return Promise.resolve(new Response(JSON.stringify(
    { error: 'other_store', message: 'this drop was made for another Levain' }), { status: 409 }));
  return f(u, o); }; return 1; })()"""
BOUND_PLAIN = """(() => {{ const k = 'levain-outbox:' + location.hostname;
  const all = JSON.parse(localStorage.getItem(k) || '[]');
  all.push({{ outbox_id: {oid}, words: 'made for this store, refused by it', sent_at: '2026-10-22T10:00:00.000000Z',
             state: 'waiting', envelope: null, store_id: {sid} }});
  localStorage.setItem(k, JSON.stringify(all));
  window.dispatchEvent(new StorageEvent('storage', {{ key: k }})); return 1; }})()"""


@needs_browser
def test_f3_an_other_store_answer_on_an_item_the_page_sees_as_its_own_is_a_refusal(world, tmp_path: Path) -> None:
    # L3 r3 LOW: an item bound to the identity the page reads, answered other_store, stayed "waiting" with no card of
    # refusal; only a null-bound item gets the receipt-only retry. A bound item is refused, with the server's words
    store_id = EnvelopeStore(world["qdir"]).store_id()
    dev = device_token(world["qdir"])
    with _serve(world["qdir"], world["qdir"]) as (base, httpd):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={dev}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"until": "document.getElementById('status').textContent !== 'reading the queue…' && 1"},
            {"eval": "document.getElementById('tab-drop').click() || 1"},
            {"eval": ANSWER_OTHER_STORE},
            {"eval": BOUND_PLAIN.format(oid=json.dumps(PLAIN_ID), sid=json.dumps(store_id))},
            {"until": f"(() => {{ const t = {OUTBOX_CARD}; return t && /refused/.test(t) && t; }})()", "ms": 15000},
        ])
    card = out["values"][-1]
    assert "refused: this drop was made for another Levain" in card and "waiting to be delivered" not in card, card
    assert EnvelopeStore(world["qdir"]).drops() == []


# the next /consent.json reply is held back (its request reaches the server now) until window.__release() is called
HOLD_LISTING = """(() => { const f = window.fetch; window.__hold = false; window.fetch = (u, o) => {
  const p = f(u, o);
  if (window.__hold && String(u).endsWith('/consent.json')) { window.__hold = false;
    return p.then((r) => new Promise((go) => { window.__release = () => go(r); })); }
  return p; }; return 1; })()"""


@needs_browser
def test_f3_a_listing_that_arrives_after_adoptstore_does_not_unset_the_adopted_store(world, tmp_path: Path) -> None:
    # L3 r3 LOW: a listing read before the review made the store (so it names none) and answered after the review
    # adopted it set the identity back to none; the signed item bound to the new store then read "from another Levain"
    from levain.dispatch.jobs import DB_NAME

    assert not (world["qdir"] / DB_NAME).exists()
    repo_path = str(world["repo"][0])
    dev = device_token(world["qdir"])
    with _serve(world["qdir"], world["qdir"]) as (base, httpd):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={dev}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            _authenticator(world["phone"]),
            {"until": "document.getElementById('status').textContent !== 'reading the queue…' && 1"},
            {"eval": "document.getElementById('tab-drop').click() || 1"},
            {"eval": HOLD_LISTING},
            {"eval": "window.__hold = true; document.getElementById('refresh').click() || 1"},
            {"until": "new Promise(r => setTimeout(() => r(1), 700))"},
            {"eval": _set("drop-words", "signed while an old listing is in flight")},
            {"eval": "document.getElementById('drop-env').open = true; 1"},
            {"eval": _set("drop-repo", repo_path)},
            {"eval": _set("drop-onto", "main")},
            {"eval": "document.getElementById('drop-sign').click() || 1"},
            {"until": "(() => { const p = document.querySelector('#drop-review pre.display'); return p && p.textContent; })()"},
            {"eval": _click("^Sign")},
            {"until": f"/envelope ev-[0-9a-f]{{32}} taken/.test({NOTE}) && {NOTE}", "ms": 20000},
            # the review made the store and the page adopted it, the sheet is closed; now the older listing (it names no
            # store: it was read before the review) arrives
            {"eval": "window.__release() || 1"},
            {"until": "new Promise(r => setTimeout(() => r(1), 700))"},
            {"eval": "localStorage.getItem('levain-store-id:' + location.hostname)"},
        ])
    vals = out["values"]
    kept = vals[-1]
    assert kept == EnvelopeStore(world["qdir"]).store_id(), kept


# every /consent.json reply names store A (OTHER_STORE) while the server holds store B: the page read A, and the name
# now answers for a replacement store
LISTING_SAYS_A = f"""(() => {{ const f = window.fetch; window.fetch = (u, o) => {{
  const p = f(u, o);
  if (!String(u).endsWith('/consent.json')) return p;
  return p.then((r) => r.json().then((j) => new Response(JSON.stringify(
    Object.assign(j, {{ coord_store_id: '{OTHER_STORE}' }})), {{ status: r.status, headers: r.headers }}))); }};
  return 1; }})()"""


@needs_browser
def test_f4_a_store_replaced_between_listing_and_review_signs_nothing_and_keeps_the_page_s_store(world,
                                                                                               tmp_path: Path) -> None:
    # L3 r3 codex HIGH: the review request carried no store identity and the page took the review reply's store over
    # the one it had read, so a signed drop made against store A was bound to (and filed in) replacement store B. The
    # review now carries A, B refuses it other_store, nothing is signed or written, and the page keeps A
    repo_path = str(world["repo"][0])
    store_b = EnvelopeStore(world["qdir"]).store_id()
    dev = device_token(world["qdir"])
    with _serve(world["qdir"], world["qdir"]) as (base, httpd):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={dev}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            _authenticator(world["phone"]),
            {"until": "document.getElementById('status').textContent !== 'reading the queue…' && 1"},
            {"eval": "document.getElementById('tab-drop').click() || 1"},
            {"eval": LISTING_SAYS_A},
            {"eval": "document.getElementById('refresh').click() || 1"},
            {"until": f"localStorage.getItem('levain-store-id:' + location.hostname) === '{OTHER_STORE}' && 1"},
            {"eval": _set("drop-words", "signed for the store this page read")},
            {"eval": "document.getElementById('drop-env').open = true; 1"},
            {"eval": _set("drop-repo", repo_path)},
            {"eval": _set("drop-onto", "main")},
            {"eval": "document.getElementById('drop-sign').click() || 1"},
            {"until": f"/^nothing to sign/.test({NOTE}) && {NOTE}", "ms": 15000},
            {"until": "new Promise(r => setTimeout(() => r(1), 700))"},
            {"eval": "localStorage.getItem('levain-store-id:' + location.hostname)"},
            {"eval": "document.querySelector('#drop-review').hidden && "
                     "document.querySelector('#drop-outbox').children.length"},
        ])
    vals = out["values"]
    note, kept, outbox = vals[-4], vals[-2], vals[-1]
    assert "another Levain" in note, note
    assert kept == OTHER_STORE, kept
    assert outbox == 0, outbox
    posts = [(p.split(" ", 1)[0], json.loads(p.split(" ", 1)[1])) for p in out["posts"]]
    assert [r for r, _ in posts] == ["/coord/envelope-review"]
    assert posts[0][1]["store_id"] == OTHER_STORE
    assert EnvelopeStore(world["qdir"]).store_id() == store_b and EnvelopeStore(world["qdir"]).drops() == []
