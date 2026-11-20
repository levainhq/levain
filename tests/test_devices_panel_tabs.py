"""The Devices panel keeps revoked devices out of the main list: a Revoked tab (with its count) holds them, newest-revoked first,
the chosen tab survives the panel's re-reads, and the page does not scroll sideways on a phone."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from levain.cockpit.firejournal import revoke_digest
from levain.webpush.notify_tokens import URGENT_PER_HOUR
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import T, _call, _pair, _serve
from tests.test_cockpit_webpush import _signed
from tests.test_pair_card_w1b import _drive, needs_browser

# the Paired devices section is drawn on the Settings place only (levain#141 polish): the page is taken there while it is awaited
READY = "(document.querySelector('.ia-places [data-ia-place-btn=settings]').click(), document.querySelector('.dev-zone') && !document.querySelector('.dev-zone').hidden && document.querySelectorAll('.dev-row').length)"
ROWS = "JSON.stringify([...document.querySelectorAll('.dev-list .dev-name')].map((n) => n.textContent))"
TABS = "JSON.stringify([...document.querySelectorAll('.dev-tabs .ck-tab')].map((b) => [b.textContent, b.getAttribute('aria-selected')]))"
PICK = "[...document.querySelectorAll('.dev-tabs .ck-tab')].find((b) => b.textContent.startsWith('%s')).click() || 1"
OVERFLOW = "document.documentElement.scrollWidth - document.documentElement.clientWidth"


def _seed(port: int, httpd, active: list[str], revoked: list[str]) -> None:
    ids = {}
    for label in [*revoked, *active]:
        ids[label] = _pair(port, label=label)[0]
    for label in revoked:
        st, out, _ = _call(port, "POST", "/cockpit/pair/revoke", {"id": ids[label], "expect_digest": revoke_digest([ids[label]], "family"), "mode": "family"}, token=T)
        assert st == 200, out
    # the server stamps with one-second resolution: set distinct stamps directly (later in the list = newer)
    db = sqlite3.connect(str(Path(httpd.broker.data_root) / "cockpit.db"))
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=1)
    for k, label in enumerate(revoked):
        db.execute("UPDATE paired_devices SET revoked_at = ? WHERE id = ?", ((base + timedelta(hours=k)).isoformat(), ids[label]))
    db.commit()
    db.close()


@needs_browser
def test_revoked_devices_have_their_own_tab_and_the_choice_survives_a_refresh(tmp_path, monkeypatch) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (port, httpd):
        _seed(port, httpd, ["Laptop", "Phone"], ["Old A", "Old B", "Old C"])
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": READY, "ms": 20000},
            {"eval": ROWS},
            {"eval": TABS},
            {"eval": PICK % "Revoked"},
            {"eval": ROWS},
            {"eval": "document.querySelector('.dev-list .dev-pill').textContent.startsWith('revoked ')"},
            {"eval": "document.querySelectorAll('.dev-list .dev-btn').length"},
            {"eval": "(() => { const f = window.fetch.bind(window); window.__reads = 0;"
                     " window.fetch = (u, o) => { if (String(u).endsWith('/devices.json')) window.__reads++; return f(u, o); }; return 1; })()"},
            # a tab arrow key: roving tabindex, selection moves, panel labelled by the selected tab
            {"eval": "(() => { const t = document.querySelector('#dev-tab-revoked'); t.focus();"
                     " t.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowLeft', bubbles: true}));"
                     " const b = [...document.querySelectorAll('.dev-tabs .ck-tab')];"
                     " return JSON.stringify([b.map((x) => x.tabIndex), b.map((x) => x.getAttribute('aria-selected')),"
                     " document.querySelector('.dev-list').getAttribute('aria-labelledby'), document.activeElement.id,"
                     " document.querySelector('.dev-list').getAttribute('role')]); })()"},
            {"eval": PICK % "Revoked"},
            {"eval": "(() => { window.__reads = 0; window.__p1 = Number(document.querySelector('.dev-list').dataset.paints); return 1; })()"},
            # pair a device while Revoked is open: the refresh must COMPLETE (a paint after the request), and the tab lands on Active
            {"eval": "(() => { document.querySelector('.dev-label').value = 'Fresh'; document.querySelector('.dev-offer').requestSubmit(); return 1; })()"},
            {"until": "window.__reads >= 1 && Number(document.querySelector('.dev-list').dataset.paints) >= window.__p1 + 2", "ms": 10000},
            {"eval": TABS},
            {"eval": ROWS},
            {"eval": PICK % "Revoked"},
            {"eval": ROWS},
        ])
    print("VALUES", json.dumps(out["values"]))
    v = out["values"]
    assert sorted(json.loads(v[1])) == ["Laptop", "Phone"], v[1]
    assert json.loads(v[2]) == [["Paired (2)", "true"], ["Revoked (3)", "false"]], v[2]
    assert json.loads(v[4]) == ["Old C", "Old B", "Old A"], v[4]          # newest-revoked first
    assert v[5] is True and v[6] == 0, v[5:7]                            # a revoked row keeps its pill and has no Revoke button
    kb = json.loads(v[8])
    assert kb == [[0, -1], ["true", "false"], "dev-tab-active", "dev-tab-active", "tabpanel"], kb
    assert json.loads(v[13]) == [["Paired (2)", "true"], ["Revoked (3)", "false"]], v[13]   # pairing lands on Active
    assert sorted(json.loads(v[14])) == ["Laptop", "Phone"], v[14]
    assert json.loads(v[16]) == ["Old C", "Old B", "Old A"], v[16]


@needs_browser
def test_no_revoked_tab_until_something_is_revoked(tmp_path, monkeypatch) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (port, httpd):
        _seed(port, httpd, ["Laptop"], [])
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": READY, "ms": 20000},
            {"eval": "document.querySelector('.dev-tabs').hidden"},
            {"eval": "getComputedStyle(document.querySelector('.dev-tabs')).display"},
            {"eval": "document.querySelector('.dev-tabs').offsetParent === null"},
        ])
    assert out["values"][1:] == [True, "none", True], out["values"]


@needs_browser
def test_the_devices_panel_does_not_scroll_sideways_at_phone_width(tmp_path, monkeypatch) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (port, httpd):
        _seed(port, httpd, ["iPhone with a rather long name to wrap onto lines", "iPad"], ["MacBook"])
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 3, "mobile": True}},
            {"until": READY, "ms": 20000},
            {"eval": OVERFLOW},
            {"eval": PICK % "Revoked"},
            {"eval": OVERFLOW},
            {"eval": "JSON.stringify([...document.querySelectorAll('.dev-tabs .ck-tab')].map((b) => Math.round(b.getBoundingClientRect().height)))"},
        ])
    print("VALUES", json.dumps(out["values"]))
    v = out["values"]
    assert v[1] <= 0 and v[3] <= 0, v
    assert all(h >= 44 for h in json.loads(v[4])), v[4]


def _stamp(db: sqlite3.Connection, ids: dict[str, str], label: str, *, seen: "timedelta | None", made: timedelta) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    db.execute("UPDATE paired_devices SET created = ?, last_seen = ? WHERE id = ?",
               ((now - made).isoformat(), None if seen is None else (now - seen).isoformat(), ids[label]))


@needs_browser
def test_a_paired_row_not_seen_for_days_says_so_and_how_to_fix_it(tmp_path, monkeypatch) -> None:
    shots = os.environ.get("LEVAIN_PINGS_SHOTS", "")
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (port, httpd):
        ids = {label: _pair(port, label=label)[0] for label in ("Stale iPad", "Fresh phone", "Never used")}
        db = sqlite3.connect(str(Path(httpd.broker.data_root) / "cockpit.db"))
        _stamp(db, ids, "Stale iPad", seen=timedelta(days=3, hours=1), made=timedelta(days=9))
        _stamp(db, ids, "Fresh phone", seen=timedelta(minutes=5), made=timedelta(days=9))
        _stamp(db, ids, "Never used", seen=None, made=timedelta(days=5))
        db.commit()
        db.close()
        rows = ("JSON.stringify(Object.fromEntries([...document.querySelectorAll('.dev-list .dev-row')].map((r) => [r.querySelector('.dev-name').textContent,"
                " [(r.querySelector('.dev-stale') || {}).textContent || '', r.querySelector('.dev-meta').textContent]])))")
        fixes = "document.querySelectorAll('.dev-zone .dev-stale-note').length"
        fix_line = "(document.querySelector('.dev-list').firstChild.className + '|' + document.querySelector('.dev-list').firstChild.textContent)"
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": READY, "ms": 20000},
            {"eval": TABS},
            {"eval": rows},
            {"eval": fixes},
            {"eval": fix_line},
            *([{"shot": os.path.join(shots, "paired-stale-row.png"), "full": True}] if shots else []),
        ])
    v = out["values"]
    assert json.loads(v[1])[0] == ["Paired (3)", "true"], v[1]
    fix = "If this device asks for a pairing code, revoke it here and pair it again."
    got = json.loads(v[2])
    assert got["Stale iPad"][0] == "not seen for 3 days" and got["Never used"][0] == "never seen" and got["Fresh phone"][0] == "", got
    # the fix is ONE line at the top of the tab for two stale rows, and no row carries it
    assert v[3] == 1, v[3]
    assert v[4] == "dev-none dev-stale-note|" + fix, v[4]
    assert all(fix not in meta for _chip, meta in got.values()), got
    # when(): a known row reads in dates and ages, never a raw stamp
    month_day = r"[A-Z][a-z]{2} \d{1,2}"
    assert re.fullmatch(rf"paired {month_day} · last seen {month_day} · by \w+ · \S+ · pings off", got["Stale iPad"][1]), got["Stale iPad"][1]
    assert re.fullmatch(rf"paired {month_day} · last seen (just now|\d+ min ago) · by \w+ · \S+ · pings off", got["Fresh phone"][1]), got["Fresh phone"][1]
    assert not any(re.search(r"\d{10,}", meta) for _chip, meta in got.values()), got


@needs_browser
def test_a_row_with_a_not_seen_chip_keeps_its_name_readable_at_phone_width(tmp_path, monkeypatch) -> None:
    """levain#161: at 390 px the 'not seen for 3 days' chip sat beside the name in one flex line and squeezed it to ~69 px while a row
    without the chip got ~203 px. The chip wraps under the name instead, so every row's name keeps a readable width."""
    shots = os.environ.get("LEVAIN_PINGS_SHOTS", "")
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (port, httpd):
        labels = ("A long device label for the iPad", "A long device label for the phone")
        ids = {label: _pair(port, label=label)[0] for label in labels}
        db = sqlite3.connect(str(Path(httpd.broker.data_root) / "cockpit.db"))
        _stamp(db, ids, labels[0], seen=timedelta(days=3, hours=1), made=timedelta(days=9))
        _stamp(db, ids, labels[1], seen=timedelta(minutes=5), made=timedelta(days=9))
        db.commit()
        db.close()
        widths = ("JSON.stringify(Object.fromEntries([...document.querySelectorAll('.dev-list .dev-row')].map((r) => [r.querySelector('.dev-name').textContent,"
                  " [Math.round(r.querySelector('.dev-name').getBoundingClientRect().width), !!r.querySelector('.dev-stale')]])))")
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 3, "mobile": True}},
            {"until": READY, "ms": 20000},
            {"eval": widths},
            {"eval": OVERFLOW},
            *([{"shot": os.path.join(shots, "paired-stale-row-390.png"), "full": True}] if shots else []),
        ])
    v = out["values"]
    got = json.loads(v[1])
    stale, fresh = got[labels[0]], got[labels[1]]
    assert stale[1] and not fresh[1], got
    assert stale[0] >= 150 and stale[0] >= 0.8 * fresh[0], got
    assert v[2] <= 0, v[2]


@needs_browser
def test_no_fix_line_when_every_paired_row_is_fresh(tmp_path, monkeypatch) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (port, httpd):
        ids = {label: _pair(port, label=label)[0] for label in ("One", "Two")}
        db = sqlite3.connect(str(Path(httpd.broker.data_root) / "cockpit.db"))
        _stamp(db, ids, "One", seen=timedelta(minutes=5), made=timedelta(days=9))
        _stamp(db, ids, "Two", seen=timedelta(hours=2), made=timedelta(hours=3))
        db.commit()
        db.close()
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": READY, "ms": 20000},
            {"eval": "document.querySelectorAll('.dev-zone .dev-stale-note, .dev-zone .dev-stale').length"},
        ])
    assert out["values"][-1] == 0, out["values"]


# levain#10 (ND-7): the notify-token card sets the urgent per-hour cap through an optional field beside the Urgent box.
NF_READY = "(document.querySelector('.ia-places [data-ia-place-btn=settings]').click(), document.querySelector('.nf-panel') && !document.querySelector('.nf-panel').hidden && 1)"
# the page's fetch records each mint body it is asked to send, then lets the request through to the scratch server
NF_STUB = ("(() => { const f = window.fetch.bind(window); window.__mints = [];"
           " window.fetch = (u, o) => { if (String(u).endsWith('/webpush/notify/tokens') && o && o.method === 'POST') window.__mints.push(JSON.parse(o.body));"
           " return f(u, o); }; return 1; })()")


def _nf_fill(name: str, *, urgent: bool, cap: str | None) -> dict:
    """Fill the card: the name, the Urgent box ticked or not, and the cap field set to ``cap`` (None leaves it empty and focuses it for typing)."""
    set_cap = "q('.nf-urgent-cap').value = ''; q('.nf-urgent-cap').focus();" if cap is None else f"q('.nf-urgent-cap').value = {json.dumps(cap)};"
    js = ("(() => { const q = (s) => document.querySelector(s);"
          f" q('.nf-name').value = {json.dumps(name)}; q('.nf-urgent').checked = {json.dumps(urgent)}; {set_cap} return 1; }})()")
    return {"eval": js}


NF_SUBMIT = {"eval": "(() => { document.querySelector('.nf-form').requestSubmit(); return 1; })()"}


def _nf_mint(name: str, *, urgent: bool, cap: str) -> list[dict]:
    """The steps that fill the card with ``cap`` set by script, then submit it."""
    return [_nf_fill(name, urgent=urgent, cap=cap), NF_SUBMIT]


def _nf_settle(name: str) -> dict:
    """A mint is settled when its new-token block shows (the card has cleared its fields by then) or the card has said why it sent nothing."""
    shown = f"document.querySelector('.nf-new') && !document.querySelector('.nf-new').hidden && document.querySelector('.nf-new').dataset.name === {json.dumps(name)}"
    return {"until": f"({shown}) || (document.querySelector('.nf-note') && !document.querySelector('.nf-note').hidden && 1)", "ms": 10000}


NF_STATE = ("JSON.stringify({mints: window.__mints, note: document.querySelector('.nf-note').hidden ? '' : document.querySelector('.nf-note').textContent,"
            " cap: document.querySelector('.nf-urgent-cap').value, box: document.querySelector('.nf-urgent').checked,"
            " bad: document.querySelector('.nf-urgent-cap').validity.badInput, aria: document.querySelector('.nf-urgent-cap').getAttribute('aria-invalid')})")
# the line the card shows for a cap it will not send, and for an unticked Urgent box over a typed cap
NF_WHOLE = "whole number"
NF_TICK = "tick Urgent"


def _nf_run(tmp_path, monkeypatch, steps_for_cases: list[dict]) -> tuple[list[dict], dict]:
    """Open the card on a scratch signed server, run the steps, and answer what each NF_STATE read said plus each token's stored cap."""
    _root, src, _ids = _install(tmp_path)
    q = tmp_path / "signq"
    q.mkdir(mode=0o700)
    with _signed(src, q) as (port, _httpd):
        steps = [{"until": NF_READY, "ms": 20000}, {"eval": NF_STUB}] + steps_for_cases
        out = _drive(tmp_path, monkeypatch, f"http://localhost:{port}/#write_token={T}", steps)
        stored = {r["name"]: r["urgent_per_hour"] for r in _call(port, "GET", "/webpush/notify/tokens", token=T)[1]["tokens"]}
    seen = [json.loads(v) for v in out["values"][2:] if isinstance(v, str) and v.startswith("{")]
    return seen, stored


@needs_browser
def test_the_notify_card_sends_the_urgent_cap_only_when_urgent_is_ticked_and_the_field_holds_a_positive_whole_number(tmp_path, monkeypatch) -> None:
    steps: list[dict] = []
    for name, urgent, cap in [("ok-cap", True, "20"), ("no-cap", True, ""), ("zero", True, "0"), ("negative", True, "-3"), ("fraction", True, "2.5")]:
        steps += _nf_mint(name, urgent=urgent, cap=cap) + [_nf_settle(name), {"eval": NF_STATE}]
    seen, stored = _nf_run(tmp_path, monkeypatch, steps)
    assert len(seen) == 5, seen
    ok, no_cap, zero, negative, fraction = seen
    assert ok["mints"] == [{"name": "ok-cap", "urgent": True, "urgent_per_hour": 20}], ok
    assert isinstance(ok["mints"][0]["urgent_per_hour"], int), ok
    assert ok["cap"] == "" and ok["box"] is False and ok["aria"] is None, ok         # cleared after a mint, like the other fields
    assert no_cap["mints"][-1] == {"name": "no-cap", "urgent": True}, no_cap          # an empty field sends none: the server's default applies
    for bad in (zero, negative, fraction):
        assert len(bad["mints"]) == 2, bad                                              # nothing was sent
        assert NF_WHOLE in bad["note"] and bad["cap"] != "" and bad["aria"] == "true", bad    # the card says so in words, marks the field, keeps what was typed
    assert stored["ok-cap"] == 20 and stored["no-cap"] == URGENT_PER_HOUR and "zero" not in stored, stored   # the scratch server kept the typed cap, and its default otherwise


@needs_browser
def test_a_cap_typed_with_urgent_unticked_is_refused_in_words_and_kept_not_dropped(tmp_path, monkeypatch) -> None:
    # a person who types a cap and forgets the box must hear it: the token is not minted, nothing is sent, the typed value stays
    steps = _nf_mint("forgot-tick", urgent=False, cap="20") + [_nf_settle("forgot-tick"), {"eval": NF_STATE}]
    # the same field, then the box ticked and sent again: the refusal cleared and the typed value went out as typed
    steps += [{"eval": "(() => { document.querySelector('.nf-urgent').checked = true; document.querySelector('.nf-form').requestSubmit(); return 1; })()"},
              _nf_settle("forgot-tick"), {"eval": NF_STATE}]
    seen, stored = _nf_run(tmp_path, monkeypatch, steps)
    refused, sent = seen
    assert refused["mints"] == [], refused
    assert NF_TICK in refused["note"] and refused["cap"] == "20" and refused["box"] is False and refused["aria"] == "true", refused
    assert sent["mints"] == [{"name": "forgot-tick", "urgent": True, "urgent_per_hour": 20}] and sent["note"] == "" and sent["aria"] is None, sent
    assert stored == {"forgot-tick": 20}, stored


@needs_browser
def test_a_cap_the_page_cannot_hold_as_a_whole_number_is_refused_and_nothing_is_sent(tmp_path, monkeypatch) -> None:
    # "1e3" is a number the field accepts; 9007199254740993 is a digit string the field accepts and a double cannot hold exactly
    steps: list[dict] = []
    for name, cap in [("sci", "1e3"), ("past-exact", "9007199254740993")]:
        steps += _nf_mint(name, urgent=True, cap=cap) + [_nf_settle(name), {"eval": NF_STATE}]
    # 309 digits typed into the field (a real edit, as a person pastes): the engine's own number rules decide what the field then holds,
    # and the card sends nothing whichever way they go
    steps += [_nf_fill("huge", urgent=True, cap=None),
              {"eval": "(() => { document.querySelector('.nf-urgent-cap').focus(); return document.execCommand('insertText', false, '9'.repeat(309)) ? 1 : 0; })()"},
              NF_SUBMIT, _nf_settle("huge"), {"eval": NF_STATE}]
    seen, stored = _nf_run(tmp_path, monkeypatch, steps)
    assert len(seen) == 3, seen
    assert seen[2]["bad"] is True or len(seen[2]["cap"]) == 309, seen[2]       # the long digit string was taken into the field, then read as a bad input or kept whole
    for bad in seen:
        assert bad["mints"] == [], bad
        assert NF_WHOLE in bad["note"] and bad["aria"] == "true", bad
    assert stored == {}, stored


@needs_browser
def test_a_non_numeric_cap_typed_into_the_number_field_is_refused_and_nothing_is_sent(tmp_path, monkeypatch) -> None:
    # the field's value reads empty while it holds a typed letter; only validity.badInput says the person typed something
    steps = [_nf_fill("typed-e", urgent=True, cap=None), {"key": "e"}, {"eval": NF_STATE}, NF_SUBMIT, _nf_settle("typed-e"), {"eval": NF_STATE}]
    seen, stored = _nf_run(tmp_path, monkeypatch, steps)
    typed, refused = seen
    assert typed["cap"] == "" and typed["bad"] is True, typed                      # the setup is what it claims: an empty value over a bad input
    assert refused["mints"] == [] and NF_WHOLE in refused["note"] and refused["aria"] == "true", refused
    assert stored == {}, stored


@needs_browser
def test_a_cap_above_what_the_server_allows_reaches_the_error_line_in_the_servers_words_and_is_kept(tmp_path, monkeypatch) -> None:
    steps = _nf_mint("too-high", urgent=True, cap="100001") + [_nf_settle("too-high"), {"eval": NF_STATE}]
    seen, stored = _nf_run(tmp_path, monkeypatch, steps)
    (refused,) = seen
    assert refused["mints"] == [{"name": "too-high", "urgent": True, "urgent_per_hour": 100001}], refused   # the card left the bound to the server
    assert "whole number from 1 to 100000" in refused["note"] and refused["cap"] == "100001", refused      # the server's words, and the typed value kept
    assert stored == {}, stored


NF_NAME_NOTE = "Give the notify token a name."
NF_CLICK_URGENT = {"eval": "(() => { document.querySelector('.nf-urgent').click(); return 1; })()"}
NF_SUBMIT_NAMELESS = {"eval": "(() => { document.querySelector('.nf-name').value = ''; document.querySelector('.nf-form').requestSubmit(); return 1; })()"}


@needs_browser
def test_ticking_urgent_clears_the_tick_urgent_refusal_and_the_field_mark_but_never_another_note(tmp_path, monkeypatch) -> None:
    steps = _nf_mint("forgot-tick", urgent=False, cap="20") + [_nf_settle("forgot-tick"), {"eval": NF_STATE}]       # 0: refused, the field marked
    # a nameless submit while the field is marked: the new note replaces the refusal, and the mark goes with the cap check that is no longer the news
    steps += [NF_SUBMIT_NAMELESS, {"eval": NF_STATE}]                                                                 # 1: the name note, no mark
    steps += [_nf_fill("forgot-tick", urgent=False, cap="20"), NF_SUBMIT, _nf_settle("forgot-tick"), {"eval": NF_STATE}]   # 2: refused again
    steps += [NF_CLICK_URGENT, {"eval": NF_STATE}]                                                                    # 3: the box ticked: the refusal is no longer true
    # another note on the same line is not the box's to clear, whichever way the box goes
    steps += [NF_SUBMIT_NAMELESS, NF_CLICK_URGENT, {"eval": NF_STATE}, NF_CLICK_URGENT, {"eval": NF_STATE}]          # 4, 5: the name note stays
    seen, stored = _nf_run(tmp_path, monkeypatch, steps)
    assert len(seen) == 6, seen
    refused, nameless, again, ticked, unticked, reticked = seen
    assert NF_TICK in refused["note"] and refused["aria"] == "true", refused
    assert nameless["note"] == NF_NAME_NOTE and nameless["aria"] is None, nameless
    assert NF_TICK in again["note"] and again["aria"] == "true", again
    assert ticked["note"] == "" and ticked["aria"] is None and ticked["cap"] == "20" and ticked["box"] is True and ticked["mints"] == [], ticked
    for kept in (unticked, reticked):
        assert kept["note"] == NF_NAME_NOTE and kept["mints"] == [], kept
    assert unticked["box"] is False and reticked["box"] is True, (unticked, reticked)
    assert stored == {}, stored


NF_HOLD = ("(() => { const g = window.fetch.bind(window); window.__posts = 0; window.__releases = [];"
           " window.fetch = (u, o) => { if (!(String(u).endsWith('/webpush/notify/tokens') && o && o.method === 'POST')) return g(u, o);"
           " window.__posts++; return new Promise((r) => window.__releases.push(() => r(g(u, o)))); };"
           " window.__release = () => { window.__releases.splice(0).forEach((f) => f()); return 1; }; return 1; })()")
NF_HELD_STATE = ("JSON.stringify({posts: window.__posts, note: document.querySelector('.nf-note').hidden ? '' : document.querySelector('.nf-note').textContent,"
                 " aria: document.querySelector('.nf-urgent-cap').getAttribute('aria-invalid')})")


@needs_browser
def test_a_second_submit_while_a_mint_is_in_flight_leaves_the_note_and_the_field_mark_untouched(tmp_path, monkeypatch) -> None:
    steps = [{"eval": NF_HOLD}, _nf_fill("first", urgent=True, cap=""), NF_SUBMIT, {"until": "window.__posts === 1", "ms": 10000}, {"eval": NF_HELD_STATE}]   # 0: the mint is held open
    steps += [NF_SUBMIT_NAMELESS, {"eval": NF_HELD_STATE}]                                                            # 1: a nameless second submit
    steps += [_nf_fill("second", urgent=True, cap="1.5"), NF_SUBMIT, {"eval": NF_HELD_STATE}]                         # 2: a second submit with a cap the card would refuse
    steps += [{"eval": "window.__release()"}, _nf_settle("first"), {"eval": NF_STATE}]                                # 3: the first mint let through and settled
    seen, stored = _nf_run(tmp_path, monkeypatch, steps)
    assert len(seen) == 4, seen
    held, nameless, capped, done = seen
    assert held == {"posts": 1, "note": "", "aria": None}, held
    assert nameless == held and capped == held, (held, nameless, capped)       # a dropped second submit leaves no trace
    assert done["mints"] == [{"name": "first", "urgent": True}], done
    assert done["note"] == "" and done["aria"] is None, done
    assert list(stored) == ["first"], stored


NF_RETYPE = ("(() => { const c = document.querySelector('.nf-urgent-cap'); c.focus(); c.select();"
             " return document.execCommand('insertText', false, %s) ? 1 : 0; })()")


@needs_browser
def test_editing_the_cap_field_clears_the_mark_and_the_cap_note_together_and_never_clears_another_note(tmp_path, monkeypatch) -> None:
    # a refused cap edited to another cap the card would refuse: assistive technology is read the mark, the sighted reader the note,
    # so an edit takes both away (levain#192), and only the input listener does it; the edit is a real one, through the engine's own input event
    retype = lambda text: {"eval": NF_RETYPE % json.dumps(text)}
    steps = [_nf_fill("whole", urgent=True, cap=None), retype("1.5"), NF_SUBMIT, _nf_settle("whole"), {"eval": NF_STATE}]    # 0: refused, marked
    steps += [retype("2.5"), {"eval": NF_STATE}]                                                                          # 1: another invalid value
    steps += [NF_SUBMIT, _nf_settle("whole"), {"eval": NF_STATE}]                                                         # 2: refused again, marked
    steps += [_nf_fill("tick", urgent=False, cap="20"), NF_SUBMIT, _nf_settle("tick"), {"eval": NF_STATE}]                # 3: the unticked box, refused, marked
    steps += [retype("21"), {"eval": NF_STATE}]                                                                           # 4: edited, the box still unticked
    steps += [NF_SUBMIT_NAMELESS, {"eval": NF_STATE}, retype("22"), {"eval": NF_STATE}]                                   # 5, 6: the name note is not the field's to clear
    seen, stored = _nf_run(tmp_path, monkeypatch, steps)
    assert len(seen) == 7, seen
    for state in seen:
        assert state["mints"] == [], state
    refused, edited, again, tick_refused, tick_edited, nameless, kept = seen
    assert NF_WHOLE in refused["note"] and refused["aria"] == "true", refused
    assert edited["cap"] == "2.5" and edited["note"] == "" and edited["aria"] is None, edited                      # the edit took the mark and the note, both
    assert NF_WHOLE in again["note"] and again["aria"] == "true", again
    assert NF_TICK in tick_refused["note"] and tick_refused["aria"] == "true", tick_refused
    assert tick_edited["cap"] == "21" and tick_edited["note"] == "" and tick_edited["aria"] is None, tick_edited
    assert nameless["note"] == NF_NAME_NOTE and nameless["aria"] is None, nameless
    assert kept["cap"] == "22" and kept["note"] == NF_NAME_NOTE and kept["aria"] is None, kept                    # another note stays through an edit
    assert stored == {}, stored


# the Revoke button writes the same shared note (cleared at the start of a call, replaced by a failure), so the cap field's mark goes with it
NF_REVOKE = lambda name: {"eval": f"(() => {{ document.querySelector('.nf-row[data-name=' + {json.dumps(json.dumps(name))} + '] .nf-revoke').click(); return 1; }})()"}
NF_REVOKE_FAILS = {"eval": ("(() => { const f = window.fetch.bind(window);"
                            " window.fetch = (u, o) => String(u).endsWith('/webpush/notify/tokens/revoke')"
                            " ? Promise.resolve(new Response(JSON.stringify({message: 'refused for the test'}), {status: 500})) : f(u, o); return 1; })()")}
NF_ROWS_STATE = ("JSON.stringify({rows: Array.from(document.querySelectorAll('.nf-row')).map((r) => r.dataset.name),"
                 " note: document.querySelector('.nf-note').hidden ? '' : document.querySelector('.nf-note').textContent,"
                 " aria: document.querySelector('.nf-urgent-cap').getAttribute('aria-invalid')})")


def _nf_row_gone(name: str) -> dict:
    return {"until": f"!document.querySelector('.nf-row[data-name=' + {json.dumps(json.dumps(name))} + ']') && 1", "ms": 10000}


@needs_browser
def test_a_revoke_while_the_cap_refusal_shows_takes_the_field_mark_with_the_note_and_so_does_a_failed_revoke(tmp_path, monkeypatch) -> None:
    steps = _nf_mint("keep-a", urgent=True, cap="") + [_nf_settle("keep-a")]
    steps += _nf_mint("keep-b", urgent=True, cap="") + [_nf_settle("keep-b")]
    refuse = [_nf_fill("refused", urgent=True, cap="1.5"), NF_SUBMIT, _nf_settle("refused"), {"eval": NF_ROWS_STATE}]
    steps += refuse + [NF_REVOKE("keep-a"), _nf_row_gone("keep-a"), {"eval": NF_ROWS_STATE}]       # 0: refused and marked; 1: a revoke that went through
    steps += refuse + [NF_REVOKE_FAILS, NF_REVOKE("keep-b"),                                      # 2: refused and marked again; 3: a revoke the server turns down
              {"until": "document.querySelector('.nf-note') && document.querySelector('.nf-note').textContent.startsWith('Could not change') && 1", "ms": 10000},
              {"eval": NF_ROWS_STATE}]
    seen, stored = _nf_run(tmp_path, monkeypatch, steps)
    assert len(seen) == 4, seen
    refused, revoked, refused_again, failed = seen
    for r in (refused, refused_again):
        assert NF_WHOLE in r["note"] and r["aria"] == "true", r
    assert revoked["rows"] == ["keep-b"] and revoked["note"] == "" and revoked["aria"] is None, revoked        # the note went, and the mark with it
    assert failed["rows"] == ["keep-b"] and "refused for the test" in failed["note"] and failed["aria"] is None, failed   # another note, no mark
    assert "keep-a" not in stored and "keep-b" in stored and "refused" not in stored, stored


# levain#196: the refused cap field names the reason to assistive technology (aria-describedby to the note, the note a polite live region)
# and shows it without colour (a thicker border and an inset marker, read here as computed style)
NF_LINK = ("(() => { const c = document.querySelector('.nf-urgent-cap'); const id = c.getAttribute('aria-describedby');"
           " const n = id ? document.getElementById(id) : null; const cs = getComputedStyle(c);"
           " return JSON.stringify({aria: c.getAttribute('aria-invalid'), id: id, same: !!n && n === document.querySelector('.nf-note'),"
           " text: n ? n.textContent : null, shown: !!n && !n.hidden && getComputedStyle(n).display !== 'none',"
           " live: document.querySelector('.nf-note').getAttribute('role') || document.querySelector('.nf-note').getAttribute('aria-live'),"
           " width: cs.borderTopWidth, shadow: cs.boxShadow}); })()")


@needs_browser
def test_the_refused_cap_field_is_described_by_the_visible_refusal_and_looks_invalid_without_colour(tmp_path, monkeypatch) -> None:
    retype = {"eval": NF_RETYPE % json.dumps("21")}
    steps = [{"eval": NF_LINK}]                                                                                         # 0: the unmarked field
    steps += _nf_mint("forgot-tick", urgent=False, cap="20") + [_nf_settle("forgot-tick"), {"eval": NF_LINK}]            # 1: refused
    steps += [retype, {"eval": NF_LINK}]                                                                                # 2: edited
    steps += _nf_mint("whole", urgent=True, cap="1.5") + [_nf_settle("whole"), {"eval": NF_LINK}]                        # 3: refused as not whole
    steps += [NF_SUBMIT_NAMELESS, {"eval": NF_LINK}]                                                                    # 4: another note replaces it
    seen, _stored = _nf_run(tmp_path, monkeypatch, steps)
    assert len(seen) == 5, seen
    plain, refused, edited, whole, nameless = seen
    assert plain["aria"] is None and plain["id"] is None, plain
    for marked, words in ((refused, NF_TICK), (whole, NF_WHOLE)):
        assert marked["aria"] == "true" and marked["id"], marked
        assert marked["same"] is True and marked["shown"] is True and words in marked["text"], marked     # the link resolves to the visible refusal
        assert marked["live"] in ("status", "polite"), marked                                              # the note is a live region
        assert float(marked["width"].removesuffix("px")) > float(plain["width"].removesuffix("px")), (plain, marked)   # a thicker border, not a colour
        assert marked["shadow"] != plain["shadow"] and marked["shadow"] != "none", (plain, marked)          # and an inset marker
    for gone in (edited, nameless):
        assert gone["aria"] is None and gone["id"] is None, gone                                           # the link goes with the mark
        assert gone["width"] == plain["width"] and gone["shadow"] == plain["shadow"], (plain, gone)
