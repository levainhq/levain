"""The phone page's dispatch card, in headless Chrome at a phone's width (390).

The card shows what the operator signs (the goal in full, the facts, the digest, the read and write lists behind
"details" with their counts on the closed card) and offers Sign and Cancel at equal weight. Then the whole ceremony runs
through the page: Chrome's virtual authenticator holds the SAME software passkey the cockpit enrolled, so the page's
own challenge, ``navigator.credentials.get`` and answer reach the job store, which verifies with ssh-keygen."""
from __future__ import annotations

import base64
import re
import shutil
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization

from levain.dispatch.jobs import JobStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_dispatch_phone import file_job, repo, world  # noqa: F401, F811  (fixtures)
from tests.test_phone_dispatch import _serve
from tests.test_phone_server import _TOK

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

CARD = "document.querySelector('article.dispatch')"

PROBE = """(() => {
  const c = document.querySelector('article.dispatch');
  const det = c.querySelector('details');
  const btn = [...c.querySelectorAll('.acts button')];
  const small = [...document.querySelectorAll('button, summary')].filter(b => b.offsetParent !== null && b.getBoundingClientRect().height < 44).length;
  return { cls: c.className, title: c.querySelector('.title').textContent,
           chips: [...c.querySelectorAll('.chips .chip')].map(x => x.textContent),
           goal: c.querySelector('pre.display').textContent, facts: c.querySelector('.facts').textContent,
           sent: c.querySelector('.sent').textContent, bound: [...det.querySelectorAll('.bound')].map(x => x.textContent),
           reason: c.querySelector('input.reason').hidden,
           digest: c.querySelector('.dg-val').title, summary: det.querySelector('summary').textContent, open: det.open,
           paths: [...det.querySelectorAll('li')].map(x => x.textContent),
           buttons: btn.map(b => ({ text: b.textContent, cls: b.className, w: Math.round(b.getBoundingClientRect().width),
                                    h: Math.round(b.getBoundingClientRect().height), disabled: b.disabled })),
           sideways: document.documentElement.scrollWidth > innerWidth, small: small };
})()"""


@needs_browser
def test_the_card_shows_what_is_signed_and_offers_sign_and_cancel_at_equal_weight(world, tmp_path: Path) -> None:
    job = file_job(world)
    c = job.contract
    with _serve(world["qdir"], world["qdir"]) as (base, _):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={_TOK}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"until": CARD + " && 1"},
            {"eval": PROBE},
        ])
    got = out["values"][-1]
    assert "row" in got["cls"].split() and "dispatch" in got["cls"].split()
    assert got["chips"][0] == "dispatch" and "claude_code" in got["chips"]
    # unsigned: "sign by" its lease end, and the contract's expiry, both read as local time to the minute
    (sign_by,) = [ch for ch in got["chips"] if ch.startswith("sign by ")]
    (expires,) = [ch for ch in got["chips"] if ch.startswith("contract expires ")]
    for chip in (sign_by, expires):
        assert re.search(r"\d:\d\d", chip) and not re.search(r"\d{4}-\d\d-\d\dT|:\d\d\.\d|Z$", chip), chip
    assert got["goal"] == c.goal                                                 # in full, tabs and lines kept
    for fact in ("fix-lint", "R0", "glm-5, kimi-k2 (k=2)", "40 tool calls", "200000 tokens", "150 cents", "black-box"):
        assert fact in got["facts"], fact
    t = c.template
    # where the files leave the machine, on the closed card
    assert got["sent"] == f"sent to {t.provider} via {t.route} \u00b7 executor {t.executor}"
    bound = "\n".join(got["bound"])
    for field in (t.content_hash, t.verifier_hash, c.policy_hash, c.base_tree, "codex 0.50.0", "(not signed)",
                  *t.write_paths, job.filed_at, job.lease_end):
        assert field in bound, field
    assert any(line.startswith("history 1 filed") for line in got["bound"])
    assert got["reason"] is True                                                 # the reason field waits on Cancel
    assert got["digest"] == job.digest
    assert got["open"] is False and f"reads {len(c.read)}" in got["summary"] and f"writes {len(c.write)}" in got["summary"]
    assert any("src/new.py" in p and "new file" in p for p in got["paths"])
    texts = [b["text"] for b in got["buttons"]]
    assert texts == ["Sign with passkey", "Cancel with passkey"]
    a, b = got["buttons"]
    assert a["cls"] == b["cls"] and abs(a["w"] - b["w"]) <= 1 and a["h"] == b["h"]   # equal weight: no recommendation
    assert not got["sideways"] and got["small"] == 0


def _authenticator(key) -> dict:
    pkcs8 = key.priv.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption())
    return {"authenticator": {"rpId": "localhost", "privateKey": base64.b64encode(pkcs8).decode(),
                              "credentialId": base64.b64encode(b"levain-test-cred").decode(),
                              "userHandle": base64.b64encode(b"levain-test-user").decode()}}


NOTE = "document.querySelector('article.dispatch .note').textContent"


@needs_browser
def test_the_page_signs_then_cancels_a_job_with_the_passkey(world, tmp_path: Path) -> None:
    job = file_job(world)
    store = JobStore(world["qdir"])
    with _serve(world["qdir"], world["qdir"]) as (base, _):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={_TOK}", [
            _authenticator(world["phone"]),
            {"until": CARD + " && 1"},
            {"eval": "[...document.querySelectorAll('article.dispatch .acts button')].find(b => /^Sign/.test(b.textContent)).click() || 1"},
            {"until": f"/^signed/.test({NOTE}) && {NOTE}"},
            # the card is redrawn signed: Sign is gone, Cancel stays
            {"until": "document.querySelector('article.dispatch') && ![...document.querySelectorAll('article.dispatch .acts button')].some(b => /^Sign/.test(b.textContent)) && 1", "ms": 8000},
            # Cancel's first tap opens the optional reason; nothing is asked of the server yet
            {"eval": CANCEL_TAP},
            {"eval": "(() => { const i = document.querySelector('article.dispatch input.reason'); i.value = 'wrong files';"
                     " i.dispatchEvent(new Event('input')); return !i.hidden; })()"},
            {"eval": CANCEL_TAP},
            {"until": f"/^cancelled/.test({NOTE}) && {NOTE}"},
        ])
    signed, opened, cancelled = out["values"][2], out["values"][5], out["values"][7]
    assert opened is True, out
    assert isinstance(signed, str) and world["phone"].fp in signed, out
    assert isinstance(cancelled, str) and cancelled.startswith("cancelled"), out
    routes = [p.split(" ", 1)[0] for p in out["posts"]]
    assert routes == [f"/dispatch/{job.id}/challenge", f"/dispatch/{job.id}/answer",
                      f"/dispatch/{job.id}/cancel-challenge", f"/dispatch/{job.id}/cancel"], routes
    assert '"reason":"wrong files"' in out["posts"][2], out["posts"]     # the typed reason is what is signed
    final = store.get(job.id)
    assert final.state == "cancelled" and final.signer_fp == world["phone"].fp


CANCEL_TAP = "[...document.querySelectorAll('article.dispatch .acts button')].find(b => /^Cancel/.test(b.textContent)).click() || 1"
SIGN_TAP = CANCEL_TAP.replace("^Cancel", "^Sign")


@needs_browser
def test_the_passkey_picker_names_the_act_it_signs(world, tmp_path: Path) -> None:
    import sqlite3

    from tests.test_dispatch_phone import SoftPasskey
    from tests.test_phone_server import _enrol

    job = file_job(world)
    other = SoftPasskey()
    conn = sqlite3.connect(world["root"] / "cockpit.db", isolation_level=None)
    _enrol(conn, world["root"], tmp_path / "laptop", world["laptop_fp"],
           {"op": "enrol", "fingerprint": other.fp, "public_key": other.line, "class": "display-phone", "custody": "passkey"})
    conn.close()
    with _serve(world["qdir"], world["qdir"]) as (base, _):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={_TOK}", [
            {"until": CARD + " && 1"},
            {"eval": SIGN_TAP},
            {"eval": NOTE},
            {"eval": CANCEL_TAP},
            {"eval": CANCEL_TAP},
            {"eval": NOTE},
        ])
    sign_pick, cancel_pick = out["values"][2], out["values"][5]
    assert sign_pick.startswith(f"pick a passkey to SIGN job {job.id}"), sign_pick
    assert cancel_pick.startswith(f"pick a passkey to CANCEL job {job.id}"), cancel_pick
    assert out["posts"] == []                                   # a pick is asked for before anything is sent


# --- fix round 2 -----------------------------------------------------------------------------------------------------

@needs_browser
def test_the_closed_card_shows_the_job_id_and_state_and_a_whole_blob_is_one_tap_away(world, tmp_path: Path) -> None:
    job = file_job(world)
    blob = next(e.blob for e in job.contract.read + job.contract.write if e.mode != "new")
    assert len(blob) >= 40
    with _serve(world["qdir"], world["qdir"]) as (base, _):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={_TOK}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"until": CARD + " && 1"},
            {"eval": "[...document.querySelectorAll('article.dispatch .chips .chip')].map(x => x.textContent)"},
            {"eval": "[...document.querySelectorAll('article.dispatch details li')].map(x => x.textContent)"},
            {"eval": "(() => { const t = document.querySelector('article.dispatch details li [role=button]'); t.click();"
                     " return document.querySelector('article.dispatch details').textContent; })()"},
        ])
    chips, lis, opened = out["values"][1], out["values"][2], out["values"][3]
    assert f"job {job.id}" in chips and "state filed" in chips, chips            # visible on the closed card
    assert not any(blob in x for x in lis)                                  # the closed form is the short one
    assert blob in opened, opened                                           # a tap shows it whole


FETCH_HOOK = """(() => { const f = window.fetch; window.__gets = 0; window.fetch = (u, o) => {
  if (String(u).includes('consent.json')) window.__gets++; return f(u, o); }; return 1; })()"""


@needs_browser
def test_a_missing_template_or_budget_field_draws_a_question_mark_never_undefined(world, tmp_path: Path) -> None:
    file_job(world)
    strip = """(() => { const f = window.fetch; window.fetch = (u, o) => f(u, o).then(r => !String(u).includes('consent.json') ? r :
      r.json().then(j => { for (const d of j.dispatch_pending) { delete d.template.k; delete d.template.tier;
        delete d.budget.tokens; delete d.budget.tool_calls; delete d.budget.dollars_cents; }
        return new Response(JSON.stringify(j), { status: 200 }); })); document.getElementById('refresh').click(); return 1; })()"""
    with _serve(world["qdir"], world["qdir"]) as (base, _):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={_TOK}", [
            {"until": CARD + " && 1"},
            {"eval": strip},
            {"until": CARD + ".querySelector('.facts').textContent.includes('?') && " + CARD + ".textContent"},
        ])
    text = out["values"][2]
    assert isinstance(text, str), out                                       # the redrawn card, not a timeout
    assert "undefined" not in text and "NaN" not in text and "null" not in text, text


# --- fix round 3 -----------------------------------------------------------------------------------------------------

@needs_browser
def test_a_template_and_budget_absent_entirely_draw_question_marks_never_a_throw(world, tmp_path: Path) -> None:
    file_job(world)
    strip = """(() => { const f = window.fetch; window.fetch = (u, o) => f(u, o).then(r => !String(u).includes('consent.json') ? r :
      r.json().then(j => { for (const d of j.dispatch_pending) { delete d.template; delete d.budget; }
        return new Response(JSON.stringify(j), { status: 200 }); })); document.getElementById('refresh').click(); return 1; })()"""
    with _serve(world["qdir"], world["qdir"]) as (base, _):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={_TOK}", [
            {"until": CARD + " && 1"},
            {"eval": strip},
            {"until": CARD + ".querySelector('.facts').textContent.includes('?') && " + CARD + ".textContent"},
        ])
    text = out["values"][2]
    assert isinstance(text, str), out
    assert "undefined" not in text and "NaN" not in text, text


def _two_passkeys(world, tmp_path):
    import sqlite3

    from tests.test_dispatch_phone import SoftPasskey
    from tests.test_phone_server import _enrol

    job = file_job(world)
    other = SoftPasskey()
    conn = sqlite3.connect(world["root"] / "cockpit.db", isolation_level=None)
    _enrol(conn, world["root"], tmp_path / "laptop", world["laptop_fp"],
           {"op": "enrol", "fingerprint": other.fp, "public_key": other.line, "class": "display-phone", "custody": "passkey"})
    conn.close()
    return job


# a poll is load(), which the Refresh button also calls; three of them stand in for more than two 10 s intervals
POLLS = "(() => { for (let i = 0; i < 3; i++) document.getElementById('refresh').click(); return 1; })()"
WAIT = "new Promise(r => setTimeout(() => r(1), 800))"
PICKS = "document.querySelectorAll('article.dispatch .pick').length"
TYPE_REASON = ("(() => { const i = document.querySelector('article.dispatch input.reason'); i.focus(); i.value = 'wrong files';"
               " i.dispatchEvent(new Event('input')); return 1; })()")
BLUR = "(() => { const i = document.querySelector('article.dispatch input.reason'); i.blur(); i.dispatchEvent(new Event('blur')); return 1; })()"


@needs_browser
def test_a_live_picker_survives_the_polls_and_a_pick_signs_then_the_card_refreshes(world, tmp_path: Path) -> None:
    job = _two_passkeys(world, tmp_path)
    with _serve(world["qdir"], world["qdir"]) as (base, _):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={_TOK}", [
            _authenticator(world["phone"]),
            {"until": CARD + " && 1"},
            {"eval": CANCEL_TAP},                                           # opens the reason box
            {"eval": TYPE_REASON},
            {"eval": BLUR},
            {"eval": CANCEL_TAP},                                           # opens the picker
            {"eval": POLLS},
            {"eval": WAIT},
            {"eval": POLLS},
            {"eval": WAIT},
            {"eval": PICKS},
            {"eval": "(() => { const ps = [...document.querySelectorAll('article.dispatch .pick')];"
                     " ps.find(x => x.textContent.startsWith('" + world["phone"].fp[:18] + "')).click(); return 1; })()"},
            {"until": f"/^cancelled/.test({NOTE}) && {NOTE}", "ms": 8000},
            {"until": "document.querySelectorAll('article.dispatch').length === 0 && 1", "ms": 8000},   # the next poll draws again
        ])
    assert out["values"][9] >= 2, out                                       # picker and its dismiss survived every poll
    assert out["values"][11].startswith("cancelled"), out
    assert JobStore(world["qdir"]).get(job.id).state == "cancelled"


@needs_browser
def test_a_get_in_flight_when_the_picker_opens_does_not_remove_it(world, tmp_path: Path) -> None:
    _two_passkeys(world, tmp_path)
    hold = """(() => { const f = window.fetch; window.__release = null; window.fetch = (u, o) => {
      if (!String(u).includes('consent.json')) return f(u, o);
      return new Promise((res) => { window.__release = () => res(f(u, o)); }); };
      document.getElementById('refresh').click(); return 1; })()"""
    with _serve(world["qdir"], world["qdir"]) as (base, _):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={_TOK}", [
            {"until": CARD + " && 1"},
            {"eval": hold},                                                 # a GET is now parked
            {"eval": CANCEL_TAP},
            {"eval": CANCEL_TAP},                                           # the picker opens while it is in flight
            {"eval": PICKS},
            {"eval": "(() => { window.__release(); return 1; })()"},
            {"eval": WAIT},
            {"eval": PICKS},
        ])
    assert out["values"][4] >= 2 and out["values"][7] >= 2, out


@needs_browser
def test_a_press_dragged_off_a_button_leaves_polling_working(world, tmp_path: Path) -> None:
    file_job(world)
    with _serve(world["qdir"], world["qdir"]) as (base, _):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={_TOK}", [
            {"until": CARD + " && 1"},
            {"eval": FETCH_HOOK},
            {"eval": "(() => { const b = document.querySelector('article.dispatch .acts button');"
                     " b.dispatchEvent(new Event('pointerdown', { bubbles: true })); return 1; })()"},   # no pointerup ever follows
            {"eval": POLLS},
            {"until": "window.__gets >= 3 && window.__gets", "ms": 4000},
        ])
    assert out["values"][4] >= 3, out


# --- fix round 5 -----------------------------------------------------------------------------------------------------

# two activations of one picker button queued before the first one's redraw: `close()` runs twice
TWICE = ("(label) => { const b = [...document.querySelectorAll('article.dispatch .pick')]"
         ".find(x => x.textContent.startsWith(label)); b.click(); b.click(); return 1; }")


@needs_browser
def test_a_double_dismiss_leaves_busy_at_zero_and_polling_working(world, tmp_path: Path) -> None:
    _two_passkeys(world, tmp_path)
    with _serve(world["qdir"], world["qdir"]) as (base, _):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={_TOK}", [
            {"until": CARD + " && 1"},
            {"eval": SIGN_TAP},                                             # opens the picker (it holds busy)
            {"eval": "(" + TWICE + ")('dismiss')"},
            {"eval": FETCH_HOOK},
            {"eval": POLLS},
            {"until": "window.__gets >= 3 && window.__gets", "ms": 4000},
            {"eval": PICKS},
        ])
    assert out["values"][5] >= 3, out                                       # busy went back to 0, not below it
    assert out["values"][6] == 0, out


@needs_browser
def test_a_double_pick_runs_one_ceremony_and_polling_resumes(world, tmp_path: Path) -> None:
    job = _two_passkeys(world, tmp_path)
    with _serve(world["qdir"], world["qdir"]) as (base, _):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={_TOK}", [
            _authenticator(world["phone"]),
            {"until": CARD + " && 1"},
            {"eval": SIGN_TAP},
            {"eval": "(" + TWICE + ")('" + world["phone"].fp[:18] + "')"},
            {"until": f"/^signed/.test({NOTE}) && {NOTE}", "ms": 8000},
            {"eval": WAIT},
            {"eval": WAIT},
            {"eval": FETCH_HOOK},
            {"eval": POLLS},
            {"until": "window.__gets >= 3 && window.__gets", "ms": 4000},
        ])
    routes = [p.split(" ", 1)[0] for p in out["posts"]]
    assert routes == [f"/dispatch/{job.id}/challenge", f"/dispatch/{job.id}/answer"], routes   # one ceremony, not two
    assert out["values"][8] >= 3, out


@needs_browser
def test_words_typed_while_a_get_is_in_flight_are_not_redrawn_away(world, tmp_path: Path) -> None:
    file_job(world)
    hold = """(() => { const f = window.fetch; window.__release = null; window.fetch = (u, o) => {
      if (!String(u).includes('consent.json')) return f(u, o);
      return new Promise((res) => { window.__release = () => res(f(u, o)); }); };
      document.getElementById('refresh').click(); return 1; })()"""
    # a question's words box gets text while the GET is parked; the card is marked so a redraw would show
    type_words = """(() => { const t = document.createElement('textarea'); t.className = 'words';
      document.getElementById('requests').prepend(t); t.value = 'half a sentence';
      document.querySelector('article.dispatch').dataset.mark = '1'; return 1; })()"""
    with _serve(world["qdir"], world["qdir"]) as (base, _):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={_TOK}", [
            {"until": CARD + " && 1"},
            {"eval": hold},
            {"eval": type_words},
            {"eval": "(() => { window.__release(); return 1; })()"},
            {"eval": WAIT},
            {"eval": "[document.querySelector('textarea.words') && document.querySelector('textarea.words').value,"
                     " document.querySelector('article.dispatch').dataset.mark || null]"},
        ])
    assert out["values"][5] == ["half a sentence", "1"], out


# --- B5's review (1019+45): a picker left open while the page is put away held the whole list still -----------------

HIDE = ("(() => { Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });"
        " document.dispatchEvent(new Event('visibilitychange')); return 1; })()")


@needs_browser
def test_putting_the_page_away_dismisses_an_open_picker_and_polling_resumes(world, tmp_path: Path) -> None:
    _two_passkeys(world, tmp_path)
    with _serve(world["qdir"], world["qdir"]) as (base, _):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={_TOK}", [
            {"until": CARD + " && 1"},
            {"eval": SIGN_TAP},                                             # opens the picker (it holds busy)
            {"eval": PICKS},
            {"eval": HIDE},
            {"eval": PICKS},
            {"eval": FETCH_HOOK},
            {"eval": POLLS},
            {"until": "window.__gets >= 3 && window.__gets", "ms": 4000},
            {"eval": WAIT},
            {"eval": NOTE},
        ])
    assert out["values"][2] >= 2 and out["values"][4] == 0, out             # the picker was up, then gone
    assert out["values"][7] >= 3, out                                       # busy released: the list polls again
    assert not out["values"][9].startswith("pick a passkey"), out           # no instruction left over a gone picker


# --- 1029-5: the card's challenge names the version it showed; any 409 redraws ---------------------------------

def _counting(httpd) -> "list[int]":
    """The listing reads the server serves from here on (the card's redraw is one more)."""
    reads: "list[int]" = []
    real = httpd.dispatch.pending
    httpd.dispatch.pending = lambda: reads.append(1) or real()
    return reads


@needs_browser
def test_the_challenge_carries_the_listed_version_and_a_stale_409_redraws_the_card(world, tmp_path: Path) -> None:
    """The job changed between the card and the tap (the server's current version is another): the page sent the version
    the listing showed, was refused 'changed since it was shown', signed nothing, and read the queue again."""
    job = file_job(world)
    with _serve(world["qdir"], world["qdir"]) as (base, httpd):
        reads = _counting(httpd)
        httpd.dispatch._current = lambda _job_id: {"state": "filed", "sha256": "f" * 64}
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={_TOK}", [
            _authenticator(world["phone"]),
            {"until": CARD + " && 1"},
            {"eval": f"JSON.stringify({{n: 0}})"},
            {"eval": SIGN_TAP},
            {"until": f"/changed since it was shown/.test({NOTE}) && {NOTE}", "ms": 8000},
            {"eval": "new Promise(r => setTimeout(() => r(1), 2600))"},          # past the redraw's 1.5 s
        ])
        after = len(reads)
    assert [p.split(" ", 1)[0] for p in out["posts"]] == [f"/dispatch/{job.id}/challenge"], out["posts"]
    (sent,) = re.findall(r'"request_sha256":"([0-9a-f]{64})"', out["posts"][0])
    assert sent != "f" * 64                                                       # the listing's version, not the server's now
    assert reads and after >= 2, (after, out)                                     # the first read, and the redraw's
    assert JobStore(world["qdir"]).get(job.id).state == "filed"


def _a_409_with(world, tmp_path: Path, message: str) -> "tuple[dict, int]":
    from levain.dispatch.phone import DispatchRefused

    file_job(world)
    with _serve(world["qdir"], world["qdir"]) as (base, httpd):
        reads = _counting(httpd)

        def refuse(*_a):
            raise DispatchRefused(409, "conflict", message)

        httpd.dispatch.step = refuse
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={_TOK}", [
            _authenticator(world["phone"]),
            {"until": CARD + " && 1"},
            {"eval": SIGN_TAP},
            {"until": f"/^Not signed/i.test({NOTE}) && {NOTE}", "ms": 8000},
            {"eval": "new Promise(r => setTimeout(() => r(1), 2600))"},
        ])
        return out, len(reads)


@needs_browser
def test_a_different_409_also_redraws_the_list(world, tmp_path: Path) -> None:
    out, after = _a_409_with(world, tmp_path, "dispatch job is not filed: something else is the matter")
    assert after >= 2, (after, out)                                               # the load's read, and the redraw's


@needs_browser
def test_a_409_that_the_job_was_cancelled_meanwhile_rereads_the_list(world, tmp_path: Path) -> None:
    """The card was shown as filed, the job was cancelled meanwhile: the challenge's 409 says so and the list is re-read."""
    out, after = _a_409_with(world, tmp_path, "dispatch job is cancelled, which is final")
    assert after >= 2, (after, out)
