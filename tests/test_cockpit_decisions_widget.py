"""Decisions waiting is a glance widget with a count, and the list is behind it (Phill 10-14: "995 of the time it's going to be
empty or 1-2 items at most ... make the widget clickable and open up to a modal with everything listed and organized").

The kernel's own decisions panel installs as a count widget, not a Work-zone card; a click on the tile opens the focus modal,
Esc closes it and the tile gets focus back."""

from __future__ import annotations

import time
from pathlib import Path

from levain.cockpit import Cockpit, ProviderSpec, Read, RowIn
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_k2b_decisions import NOW, _journal
from tests.test_cockpit_k2b_labels import make_record, PHONE, T0
from datetime import timedelta

TILE = "[...document.querySelectorAll('.ck-widgets .ck-card.w')].find(c => /^Decisions waiting/i.test(c.innerText))"
WORK_CARD = "[...document.querySelectorAll('.ck-zgrid .panel h2')].some(h => /^Decisions waiting/i.test(h.textContent))"
ESC = "document.activeElement.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))"


def test_the_kernels_own_decisions_panel_is_a_count_widget_not_a_work_card() -> None:
    ck = Cockpit()
    ck.attach_decision_rows(lambda now: None)
    m = ck.manifest({"class": "token"})
    assert m["panels"]["decisions"]["face"] == "count" and "decisions" in m["regions"]["widgets"]
    assert not any("decisions" in z["panels"] for z in m["regions"]["zones"])


def test_a_long_list_of_outcomes_gets_the_filter_box_though_two_decisions_wait(tmp_path: Path) -> None:
    j = _journal(tmp_path)
    for i in range(12):                      # expired at T0 + 1 minute, so outcomes at NOW; they are listed, never counted
        _pid, raw = make_record(intent="sign", ttl=timedelta(minutes=1))
        j.issue(record_bytes=raw, principal=PHONE, idempotency_key=f"x{i}", now=T0, cap_exempt=True)
    for i in range(2):
        _pid, raw = make_record(intent="sign")
        j.issue(record_bytes=raw, principal=PHONE, idempotency_key=f"w{i}", now=T0, cap_exempt=True)
    ck = Cockpit(clock=lambda: NOW)
    ck.attach_decision_rows(j.decision_rows)
    head = ck.manifest({"class": "token"})["panels"]["decisions"]
    assert head["count"] == 2 and head["searchable"] is True, {k: head[k] for k in ("count", "searchable")}


def _rows() -> tuple[RowIn, ...]:
    return tuple(RowIn(id=f"np:{i}", title=t, body=f"stopped: {t}", facets={"stopped": t, "at": "2026-10-14T09:00:00Z"},
                       stored={"what": t, "np_version": "v1"}) for i, t in enumerate(["cut the release", "merge the branch"]))


@needs_browser
def test_an_empty_decisions_tile_says_so_plainly_and_opens_and_closes(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": f"{TILE}"},
            {"eval": f"(() => {{ const t = {TILE}; return [t.innerText.replace(/\\s+/g, ' ').trim(), t.getAttribute('role'), {WORK_CARD}]; }})()"},
            {"eval": f"(() => {{ {TILE}.click(); const m = document.getElementById('levain-modal'); return [!!m, m && m.innerText.replace(/\\s+/g, ' ')]; }})()"},
            {"eval": f"(async () => {{ {ESC}; await new Promise(r => setTimeout(r, 200)); return [!document.getElementById('levain-modal'), "
                     f"document.body.classList.contains('modal-open'), document.activeElement === {TILE}]; }})()"},
        ])["values"]
    tile, opened, closed = out[1], out[2], out[3]
    assert "0" in tile[0] and "Nothing is waiting on you" in tile[0] and tile[1] == "button" and tile[2] is False, tile
    assert opened[0] is True and "Nothing is waiting on you" in opened[1], opened
    assert closed == [True, False, True], closed


@needs_browser
def test_the_tile_counts_the_rows_and_the_modal_lists_them(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    switch = time.monotonic() + 7.0       # the rows change under the page at this instant, the count unchanged
    first = _rows()
    second = (first[0], RowIn(id="np:9", title="a new one", body="stopped: a new one", facets={"stopped": "x", "at": "2026-10-14T09:00:00Z"},
                              stored={"what": "n", "np_version": "v1"}))

    def rows() -> tuple[RowIn, ...]:
        return first if time.monotonic() < switch else second
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, httpd):
        httpd.get_cockpit().attach_decision_source(ProviderSpec(
            "decisions", "triage-list", "Decisions waiting", "gate", lambda c: Read(rows=rows()), region="widgets", face="count", rank=-1,
            order="decision.queue", facets=frozenset({"stopped", "at"}), version_fields=("what", "np_version"), rowset="needs_phill",
            read_one=lambda c, rid: Read(rows=tuple(r for r in rows() if r.id == rid)), search_fields=("title", "body"),
            empty="nothing is waiting on you."))
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": f"{TILE}"},
            {"eval": f"{TILE}.querySelector('.big').textContent"},
            {"eval": f"(() => {{ {TILE}.click(); return document.querySelector('#levain-modal .modal-body').innerText.replace(/\\s+/g, ' ').toLowerCase(); }})()"},
            {"eval": f"(() => {{ {ESC}; return !document.getElementById('levain-modal'); }})()"},
            # the rows changed with the count the same: a refetch repaints in place, and an expand opened after it lists the new row
            {"eval": "(async () => { await new Promise(r => setTimeout(r, 8000)); return await LevainCockpitKinds.refetchPanel('decisions'); })()"},
            {"eval": f"(() => {{ {TILE}.click(); return document.querySelector('#levain-modal .modal-body').innerText.replace(/\\s+/g, ' ').toLowerCase(); }})()"},
            # a re-render replaces the tile that opened the view: Esc still hands focus to its successor
            {"eval": f"(async () => {{ const t = {TILE}; t.replaceWith(t.cloneNode(true)); {ESC}; await new Promise(r => setTimeout(r, 200)); "
                     f"return [!document.getElementById('levain-modal'), document.activeElement === {TILE}]; }})()"},
        ])["values"]
    assert out[1] == "2", out
    assert "waiting on you" in out[2] and "cut the release" in out[2] and "merge the branch" in out[2], out
    assert out[3] is True, out
    assert out[4] in ("applied", False), out        # False is the answer for a changed shape; the page then reloads the board itself
    assert "a new one" in out[5] and "merge the branch" not in out[5], out
    assert out[6] == [True, True], out
