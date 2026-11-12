"""The agents graph does not keep its own count or its own marks of what waits on the operator (row 19, Phill 11-12).

The band (the Needs strip) is the one place that number and those items live. The graph used to carry a second copy,
read by a second definition: a filed dispatch contract was a task "waiting on you" in the graph and nothing on the band,
and a question store the band had cleaned could still be a double-digit count in the graph's header. A second copy can
only disagree with the first, so the live graph no longer draws one: no count in any header, and no session, task or
column marked "waiting on you". A REPLAY is the one place those marks remain, as history of an instant, labelled so.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from levain.cockpit.consent import utc_stamp
from levain.cockpit.providers import build_default_cockpit
from levain.dispatch.jobs import JobStore
from levain.webpush.needs import open_needs_count
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_topology import _Reader, _queue, _rig, _stem
from tests.test_dispatch_contract import POLICY, make_repo, registry, submission


def _file_contract(cdir, tmp_path):
    path, base = make_repo(tmp_path / "dispatch-repo")
    exp = utc_stamp(datetime.now(timezone.utc) + timedelta(hours=2))
    job, _cap = JobStore(cdir).file(submission(path, base, expires_at=exp), resolve_template=registry(), policy=POLICY,
                                    filer_session="nobody", idem_key=None)
    return job


def test_a_filed_contract_is_not_waiting_on_you_in_the_graph_when_the_band_counts_nothing(tmp_path, monkeypatch):
    """The reproduced mismatch: one contract filed and nothing else. The band counts 0 (a contract is neither a consent
    request nor a question); the old graph drew it as a task WAITING ON YOU and its header said 1."""
    cdir, _q, qs = _queue(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    ck = build_default_cockpit(src)
    ck.attach_consent_rows(_Reader(cdir))
    ck.attach_question_rows(qs)
    job = _file_contract(cdir, tmp_path)
    assert open_needs_count(cdir) == 0                                   # the band
    v = ck.panel("topology", credential_class="none")["value"]
    mine = next(t for t in v["tasks"] if t["id"] == f"dispatch:{job.id}")
    assert mine["state"] != "waiting" and mine["waits_on"] == [], mine
    assert "waiting" not in [t["state"] for t in v["tasks"]], v["tasks"]   # the graph's count would have been 1


def test_the_live_graph_marks_no_session_and_no_task_waiting_on_you(tmp_path, monkeypatch):
    """The rig has a pending consent request (a session waits on it) and an open question (another does): both ARE on the
    band, which is where they are shown. The graph draws neither as a mark: no node waiting, no card on a node, no
    consent or question task, no task waiting."""
    ck, _root, _src, cdir, _work = _rig(tmp_path, monkeypatch)
    _file_contract(cdir, tmp_path)
    assert open_needs_count(cdir) == 2                                   # the band has both rows
    v = ck.panel("topology", credential_class="none")["value"]
    assert "waiting" not in [n["state"] for n in v["nodes"]], v["nodes"]
    assert "waiting" not in [t["state"] for t in v["tasks"]], v["tasks"]
    assert all(n.get("card") is None for n in v["nodes"]), v["nodes"]
    assert not [t for t in v["tasks"] if t["kind"] in ("consent", "question")], v["tasks"]
    # the sessions those rows belong to are still nodes, in the state their own log says
    assert _stem("claude_code", "s-wait") in {n["id"] for n in v["nodes"]}
    # and the sources the live graph no longer reads are not reported as unreadable: nothing was lost, nothing was asked
    assert "decisions" not in v["unreadable"] and "questions" not in v["unreadable"], v["errors"]
    assert "waiting on you" not in json.dumps(v).lower()



# --- the page: no count in any header, no empty column, at desk and phone width -------------------------------------

from tests.test_cockpit_f12b_topology_view import NARROW, TV, _paint, _run, _value  # noqa: E402
from tests.test_cockpit_flip_parity import needs_browser  # noqa: E402
from tests.test_cockpit_k7_build import DESKTOP, _sessions_fixture  # noqa: E402

# every figure a header strip or metrics block draws, and the whole of the card's own text above the graph body
HEADER = ("JSON.stringify({figs: [...%s.querySelectorAll('.tv-stat, .tv-m')].map(s => s.textContent.replace(/\\s+/g, ' ')), "
          "text: %s.innerText.replace(/\\s+/g, ' '), "
          "overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth})") % (TV, TV)
WIDTHS = [("desk", DESKTOP), ("phone", NARROW)]


@needs_browser
def test_no_lens_header_carries_a_waiting_on_you_count_at_desk_and_phone_width(tmp_path, monkeypatch):
    """The literal value has a waiting SESSION and two waiting TASKS (what the old header turned into '1 waiting on you'); the
    sessions fixture has a pending consent request the band lists. Neither lens, at either width, says a number of it."""
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    for name, first in WIDTHS:
        for lens in ("work", "agents"):
            out = _run(tmp_path, src, [{"eval": _paint(_value(), lens)}, {"eval": HEADER}], first=first)
            got = json.loads(out[-1])
            figs = got["figs"]
            assert figs, (name, lens, got)                                                    # a header was drawn at all
            assert not [f for f in figs if "waiting" in f.lower()], (name, lens, figs)       # no figure of it ...
            # (a card a host sends as waiting still wears its own tag below the header; the header's text is the figures)
            assert got["overflow"] <= 0, (name, lens, got["overflow"])                       # ... and the narrower row still fits


@needs_browser
def test_the_real_page_says_no_number_of_what_waits_on_him_beside_the_band_that_does(tmp_path, monkeypatch):
    """Through the real provider and the real page: the Needs strip lists the pending request, and nothing in the graph card
    or the Sessions card's jump line carries a count of it."""
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = ("JSON.stringify({strip: document.querySelectorAll('#ck-needs .needs-row').length, "
             "hdr: [...%s.querySelectorAll('.tv-stat, .tv-m')].map(s => s.textContent.replace(/\\s+/g, ' ')), "
             "card: (document.querySelector('#k7-sessions') || {innerText: ''}).innerText.replace(/\\s+/g, ' ')})") % TV
    for name, first in WIDTHS:
        for lens in ("work", "agents"):
            out = _run(tmp_path, src, [{"eval": f"document.querySelector('#ck-hlens [data-tv={lens}]').click(); 1"},
                                       {"until": "document.querySelectorAll('#ck-needs .needs-row').length >= 1 && 1", "ms": 30000},
                                       {"eval": probe}], first=first)
            got = json.loads(out[-1])
            assert got["strip"] >= 1, (name, lens, got)                                      # the band has the row
            assert got["hdr"] and not [f for f in got["hdr"] if "waiting" in f.lower()], (name, lens, got["hdr"])
            assert not __import__("re").search(r"\d+\s+waiting on you", got["card"], __import__("re").I), (name, lens, got["card"])


@needs_browser
def test_a_live_work_lens_with_nothing_to_list_has_no_empty_column_or_dangling_heading(tmp_path, monkeypatch):
    """With the records gone the first column gives its width back: no 'Waiting on you' heading, no empty slot, the task cards
    start at the left edge of the card, at desk and phone width (and a host-sent waiting record does not bring the column back)."""
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    live = _value()
    live["nodes"] = [dict(n, state=("running" if n["state"] == "waiting" else n["state"]), card=None) for n in live["nodes"]]
    live["tasks"] = [t for t in live["tasks"] if t["state"] != "waiting"]
    probe = ("JSON.stringify({heads: [...%s.querySelectorAll('.tv-colh')].filter(h => !h.hidden && h.offsetParent !== null).map(h => h.textContent), "
             "wl: !!%s.querySelector('.tv-wl') && %s.querySelector('.tv-wl').offsetParent !== null, "
             "text: %s.innerText, "
             "left: Math.round(%s.querySelector('.tv-task').getBoundingClientRect().left - %s.querySelector('.tv-work').getBoundingClientRect().left)})") % ((TV,) * 6)
    for name, first in WIDTHS:
        out = _run(tmp_path, src, [{"eval": _paint(live)}, {"eval": probe}], first=first)
        got = json.loads(out[-1])
        assert "Waiting on you" not in got["heads"] and got["wl"] is False, (name, got)
        assert "waiting on you" not in got["text"].lower() and "nothing is waiting" not in got["text"].lower(), (name, got["text"])
        assert got["left"] <= 4, (name, got)                                                  # the tasks begin at the card's own left edge
    out = _run(tmp_path, src, [{"eval": _paint(_value())}, {"eval": probe}])
    kept = json.loads(out[-1])
    assert kept["wl"] is False and "Waiting on you" not in kept["heads"], kept               # control: a host that sends a waiting record gets no column either (the Needs strip lists it)


def test_the_jump_line_and_the_compact_figure_carry_no_waiting_count(tmp_path):
    """The page's per-panel line (the jump box, the filter and a phone read it) used to end '... K waiting on you'; the value here has
    a waiting session and two waiting tasks."""
    from tests._ke_dom import NODE, run_node

    if NODE is None:
        import pytest
        pytest.skip("node is needed")
    body = ("const val = %s;\n"
            "const v = V.fromManifest(fixture({ t: head('t', 'topology', 'Topology') }, { t: head('t', 'topology', 'Topology', { value: val }) }));\n"
            "out.lines = v.extra_panels.t.lines;\n"
            "const compact = { as_of: val.as_of, nodes: val.nodes.length, tasks: val.tasks.length };\n"
            "const w = V.fromManifest(fixture({ t: head('t', 'topology', 'Topology') }, { t: head('t', 'topology', 'Topology', { value: compact }) }));\n"
            "out.compact = w.extra_panels.t.lines;\n") % json.dumps(_value())
    got = run_node(body, tmp_path)
    assert got["lines"] == [{"meta": "topology", "text": "5 sessions · 7 tasks"}], got
    assert got["compact"] == [{"meta": "topology", "text": "5 sessions · 7 tasks"}], got
    assert "waiting" not in json.dumps(got).lower()
