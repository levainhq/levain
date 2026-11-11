"""Phill 11-11, on his phone: a count tile whose rows are long markdown (flow's desk morning card) drew the first row whole,
literal `**` and all, and the tile ran 844 px tall. The face is a count and the provider's one-line note; the rows are the
modal's, drawn as markdown with the row's kind ahead of its meta line. This drives the real page at 390 px in headless Chrome with a
flow-shaped count tile (the same shape `flow_cockpit.morning_read` registers: a title "kind: words", the whole text in the body)."""
from __future__ import annotations

import dataclasses

import levain.cockpit.desk_panels as dp
from levain.cockpit.results import Read, RowIn
from levain.desk import morning
from levain.desk.store import CoordStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

pytestmark = [needs_browser]

LONG = ("inferred supersession, his words not verified: **FLOW BECOMES A LEVAIN INSTALL, AFTER 1.0** (2026-11-08) replaced by: "
        "[decided, 2026-11-10, Phill] **THE COORDINATOR IS THE DESK, IN CODE TOO** (*\"we need to get this migrated ASAP\"*) "
        "supersedes **FLOW BECOMES A LEVAIN INSTALL**·**A LATER RULING SUPERSEDES**·**SPORE BECOMES INTENTION, EVERYWHERE**")
NOTE = "What waits on you for a decision is in the Decisions panel."
ITEMS = [
    ("contradiction", LONG, "dream 2026-11-10-eacb3a72"),
    ("felt", "He said *\"this is the one\"* about the `desk` rename", "dream 2026-11-09-59f0c938"),
    ("pattern", "**absence_of_signal_rendered_as_health** (61x) re-grounded", "dream 2026-11-10-eacb3a72"),
]
TILE = 'document.querySelector("[data-ck-panel=coord-morning]")'


def _flow_like_read_with(note):
    def read(ctx):
        # flow's title is "kind: words" cut by the kernel; the whole text rides in the body
        rows = tuple(RowIn(id=f"dream:{n}", title=f"{k}: {t[:60]}", body=t, facets={"legacy_meta": m}, stored={"id": str(n), "kind": k})
                     for n, (k, t, m) in enumerate(ITEMS))
        return Read(rows=rows, note=note)
    return read


def _drive_tile(tmp_path, monkeypatch, note):
    monkeypatch.setattr(morning, "card", lambda store: {"away": {"claude_code": {"away": False}, "codex": {"away": None}}, "undecided": [],
                                                       "budgets": [], "waiting": {"threads": {}, "loose": [], "unresolved": []},
                                                       "delivery_ran": None, "errors": []})
    orig = dp.desk_discoverer

    def patched(*a, **k):
        disc = orig(*a, **k)

        def d2(ctx):
            return [dataclasses.replace(sp, title="Desk morning card", region="widgets", rank=0, face="count", read=_flow_like_read_with(note),
                                        facets=frozenset({"legacy_meta"}), search_fields=("title", "body")) if sp.id == dp.MORNING_ID else sp
                    for sp in disc(ctx)]
        return d2
    monkeypatch.setattr(dp, "desk_discoverer", patched)
    import levain.cockpit.providers as prov
    if hasattr(prov, "desk_discoverer"):
        monkeypatch.setattr(prov, "desk_discoverer", patched)

    root, src, _ids = _install(tmp_path)
    monkeypatch.setenv("LEVAIN_HOME", str(root / ".levain"))
    (root / ".levain" / "consent").mkdir(mode=0o700, exist_ok=True)
    CoordStore(root / ".levain" / "consent").threads()
    steps = [
        {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
        {"until": f"!!{TILE}", "ms": 25000},
        {"eval": f"(() => {{ const t = {TILE}; const subs = [...t.querySelectorAll('.sub')]; "
                 "return {h: Math.round(t.getBoundingClientRect().height), text: t.textContent, subs: subs.map(x => x.textContent)}; })()"},
        {"tap": TILE, "ms": 8000},
        {"until": "!!document.getElementById('levain-modal') && document.querySelectorAll('#levain-modal .row').length === 3", "ms": 8000},
        {"eval": "(() => { const m = document.getElementById('levain-modal'); const rows = [...m.querySelectorAll('.row')]; "
                 "return {raw: m.textContent.indexOf('**') >= 0, strong: m.querySelectorAll('strong.md-b').length, em: m.querySelectorAll('em.md-i').length, "
                 "code: m.querySelectorAll('code.md-code-i').length, meta: rows.map(r => r.querySelector('.ext-meta').textContent), "
                 "toggles: rows.map(r => !!r.querySelector('.clause-toggle')), dates: rows[0].querySelectorAll('.clause .nowrap').length, "
                 "clamp: getComputedStyle(rows[0].querySelector('.clause')).webkitLineClamp}; })()"},
        {"key": "Escape"},
        {"until": "!document.getElementById('levain-modal')", "ms": 4000},
    ]
    with _serve(src, install_class="reported") as (url, _httpd):
        got = _drive(tmp_path, url, steps)
    return (next(v for v in got["values"] if isinstance(v, dict) and "subs" in v),
            next(v for v in got["values"] if isinstance(v, dict) and "strong" in v))


def test_a_count_tile_is_a_count_and_its_modal_draws_markdown(tmp_path, monkeypatch):
    tile, modal = _drive_tile(tmp_path, monkeypatch, NOTE)
    # the face: the count and the provider's note, never a row's words (it was 844 px with the first row whole)
    assert tile["subs"] == [NOTE], tile
    assert "**" not in tile["text"] and "FLOW BECOMES" not in tile["text"], tile
    assert tile["h"] < 260, tile
    # the modal: markdown drawn, no literal markers, the kind leads the meta line, a long row clamps with a caret
    assert modal["raw"] is False and modal["strong"] >= 3 and modal["em"] >= 2 and modal["code"] == 1, modal
    assert [m.split(" · ")[0] for m in modal["meta"]] == ["contradiction", "felt", "pattern"], modal
    assert modal["toggles"] == [True, True, True] and modal["clamp"] == "2", modal
    assert modal["dates"] == 2, modal      # the two dates in the first row: a date inside a markdown row never breaks at its hyphens


def test_a_count_tile_with_no_note_names_its_first_row_as_one_clean_line(tmp_path, monkeypatch):
    tile, _modal = _drive_tile(tmp_path, monkeypatch, "")
    # no note: the first row, paired markers dropped, cut at a word within the tile's character cap, then the count of the rest
    assert len(tile["subs"]) == 1, tile
    line = tile["subs"][0]
    assert line.endswith("\u2026 +2 more") and "**" not in line and "FLOW BECOMES A LEVAIN INSTALL" in line, tile
    assert len(line) <= 90 + len(" +2 more"), tile
    assert tile["h"] < 260, tile


def test_a_note_keeps_a_lone_marker_and_drops_nested_pairs(tmp_path, monkeypatch):
    tile, _modal = _drive_tile(tmp_path, monkeypatch, "Run **use `desk` now** on lib/* and *this*")
    assert tile["subs"] == ["Run use desk now on lib/* and this"], tile
