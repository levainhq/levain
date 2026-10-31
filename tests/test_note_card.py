"""The note card (Phill 10-22, np-58287c09): an agent's reply the operator reads and dismisses. Its own store
(``notes.db``), the band's ``note`` MCP tool, rows in the decisions panel without the agent's words, a C1
``note_dismiss`` verb that wakes nothing, the phone's list and dismiss; and every path that answers, signs or rules
refuses a note by type."""
from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from levain.cockpit.consent import ConsentError
from levain.consent import band_mcp
from levain.consent.notes import NOTE_OPEN_MAX, NOTE_OPEN_PER_SESSION_MAX, NoteCapError, NoteStore
from levain.consent.questions import QuestionStore
from tests.test_cockpit_k2a import TOKEN, _get, _install, _post, _serve
from tests.test_cockpit_questions import _get_status

WHAT = "Merged the sweep fix; CI green on the exact sha"
DETAIL = "Tip 1a2b3c4.\nNothing waits on you: this is a reply."


@pytest.fixture
def lh(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    return tmp_path / "lh" / "consent"


def _file(store: NoteStore, **kw):
    args = dict(agent="claude_code", agent_session_id="sess-1", what=WHAT, detail=DETAIL, cwd="/w")
    args.update(kw)
    return store.file(**args)


def _row(base: str, n: int, token: str | None = TOKEN) -> dict:
    rows = _get(f"{base}/cockpit/panel/decisions.json", token)["rows"]
    return next(r for r in rows if r["id"] == f"note:{n}")


def _verb(base: str, verb: str, row: dict, params: dict | None = None, version: str | None = None):
    return _post(f"{base}/cockpit/verb", {"verb": verb, "panel_id": "decisions", "row_id": row["id"],
                                          "row_version": version or row["version"], "params": params or {}}, TOKEN)


# --- the store ---------------------------------------------------------------------------------------------------


def test_a_note_is_filed_listed_and_dismissed_once(lh):
    store = NoteStore(lh)
    n = _file(store)
    assert n.id.startswith("n-") and n.n == 1 and n.detail == DETAIL and n.dismissed is None
    assert [x.id for x in store.open_page(NOTE_OPEN_MAX)[0]] == [n.id]
    d = store.dismiss(n.id, channel="cockpit")
    assert d.channel == "cockpit"
    assert store.open_page(NOTE_OPEN_MAX)[0] == [] and store.get(n.n).dismissed is not None
    with pytest.raises(ConsentError, match="already dismissed"):
        store.dismiss(n.n, channel="phone")


def test_detail_is_optional(lh):
    n = _file(NoteStore(lh), detail=None)
    assert n.detail is None


@pytest.mark.parametrize("field,value,why", [
    ("what", "fine‮txt.exe", "U+202E"),                         # a bidi override
    ("what", "fine\U000e0041", "U+E0041"),                            # a TAG character
    ("what", "two\nlines", "U+000A"),                                 # what is one line
    ("what", "x" * 201, "1 to 200"),
    ("what", "   ", "1 to 200"),
    ("detail", "a\tb", "U+0009"),                                     # detail keeps newlines and nothing else
    ("detail", "a b", "U+2028"),
    ("detail", "a⁦b", "U+2066"),                                 # a bidi isolate
    ("detail", "x" * 4001, "1 to 4000"),
    ("agent", "flow", "agent must be one of"),
    ("cwd", "rel/dir", "absolute"),
])
def test_unshowable_and_oversize_text_is_refused_with_nothing_written(lh, field, value, why):
    store = NoteStore(lh)
    with pytest.raises(ConsentError, match=why.replace("+", r"\+")):
        _file(store, **{field: value})
    assert store.open_page(NOTE_OPEN_MAX)[0] == []


def test_the_tables_are_append_only(lh):
    store = NoteStore(lh)
    n = _file(store)
    store.dismiss(n.id, channel="cli")
    conn = sqlite3.connect(lh / "notes.db")
    for stmt in ("UPDATE notes SET what = 'x'", "DELETE FROM notes", "UPDATE note_dismissals SET channel = 'phone'",
                 "DELETE FROM note_dismissals", f"INSERT OR REPLACE INTO notes (n, id, agent, agent_session_id, what, "
                 f"cwd, filed_at) VALUES (1, '{n.id}', 'codex', 's', 'x', '/', 't')"):
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(stmt)
    conn.close()


def test_reads_create_nothing(lh):
    store = NoteStore(lh)
    assert store.open_page(NOTE_OPEN_MAX)[0] == [] and store.recent() == [] and store.get(1) is None
    assert not (lh / "notes.db").exists()


def test_a_note_is_not_a_question_by_type(lh):
    n = _file(NoteStore(lh))
    qs = QuestionStore(lh)
    with pytest.raises(ConsentError, match="no such question"):
        qs.answer(n.id, text="ok", channel="cli")
    assert qs.open_questions() == [] and qs.undelivered() == []


# --- the MCP door ------------------------------------------------------------------------------------------------


def _call(name: str, args: dict, meta: dict | None = None) -> dict:
    params = {"name": name, "arguments": args}
    if meta:
        params["_meta"] = meta
    return band_mcp.handle("claude_code", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})


def test_the_band_lists_ask_then_note(lh):
    got = band_mcp.handle("claude_code", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = got["result"]["tools"]
    assert [t["name"] for t in tools] == ["ask", "note"]
    assert tools[1]["inputSchema"]["required"] == ["what"]
    assert set(tools[1]["inputSchema"]["properties"]) == {"what", "detail"}


def test_the_note_tool_files_a_note_with_the_calls_session_and_says_nothing_comes_back(lh, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    got = _call("note", {"what": WHAT, "detail": DETAIL}, {"sessionId": "sess-meta"})["result"]
    assert got["isError"] is False
    text = got["content"][0]["text"]
    assert text.startswith("levain note: note #1 filed (n-") and "Nothing comes back to you" in text
    (n,) = NoteStore(lh).open_page(NOTE_OPEN_MAX)[0]
    assert (n.agent, n.agent_session_id, n.what, n.detail) == ("claude_code", "sess-meta", WHAT, DETAIL)
    assert not (lh / "questions.db").exists()


def test_a_refused_note_is_a_tool_error_and_files_nothing(lh):
    got = _call("note", {"what": "fine‮txt"})["result"]
    assert got["isError"] is True and "U+202E" in got["content"][0]["text"]
    bad = _call("note", {"what": WHAT, "rec": "a"})
    assert bad["error"]["code"] == band_mcp.INVALID_PARAMS
    assert NoteStore(lh).open_page(NOTE_OPEN_MAX)[0] == []


def test_the_settings_template_allows_the_note_tool():
    from tests.test_messaging_optout import _base

    assert "mcp__levain_band__note" in json.loads(_base())["permissions"]["allow"]


# --- the cockpit -------------------------------------------------------------------------------------------------


def test_a_note_row_carries_no_words_and_sits_in_its_own_uncounted_group(tmp_path, lh):
    _root, src, _ids = _install(tmp_path)
    n = _file(NoteStore(lh))
    with _serve(src, write_token=TOKEN) as (base, _h):
        for token in (TOKEN, None):
            row = _row(base, n.n, token)
            dumped = json.dumps(row)
            assert WHAT not in dumped and "Tip 1a2b3c4" not in dumped
            f = row["facets"]
            assert f["agent"] == "claude_code" and f["note_n"] == n.n and f["note_state"] == "open"
            assert row["group"] == "notes" and row["title"] == f"note from claude_code #{n.n}"
        panel = _get(f"{base}/cockpit/panel/decisions.json", TOKEN)
        assert panel["status"] == "empty"               # a note never counts as something waiting on him


def test_the_note_body_route_needs_the_token(tmp_path, lh):
    _root, src, _ids = _install(tmp_path)
    n = _file(NoteStore(lh))
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, _ = _get_status(f"{base}/cockpit/note/{n.n}.json", None)
        assert status == 403
        status, body = _get_status(f"{base}/cockpit/note/{n.n}.json", TOKEN)
        assert status == 200 and body["what"] == WHAT and body["detail"] == DETAIL and body["dismissed"] is None
        status, _ = _get_status(f"{base}/cockpit/note/99.json", TOKEN)
        assert status == 404


def test_dismiss_is_c1_direct_records_once_and_wakes_nothing(tmp_path, lh, monkeypatch):
    from levain.consent import band

    monkeypatch.setattr(band, "deliver_answer", lambda *a, **k: pytest.fail("a dismiss must deliver nothing"))
    _root, src, _ids = _install(tmp_path)
    store = NoteStore(lh)
    n = _file(store)
    threads = set(threading.enumerate())
    with _serve(src, write_token=TOKEN) as (base, _h):
        row = _row(base, n.n)
        assert [a["verb"] for a in row["actions"]] == ["note_dismiss"]
        act = row["actions"][0]
        assert act["tier"] == "C1" and act["gesture"] == "direct"
        status, body = _verb(base, "note_dismiss", row)
        assert status == 200 and body["dismissed"] == n.n, body
        assert not [t for t in threading.enumerate() if t not in threads and t.name == "levain-answer-delivery"]
        assert store.get(n.n).dismissed.channel == "cockpit"
        status, body = _verb(base, "note_dismiss", row)
        assert status == 409
        after = _row(base, n.n)
        assert after["facets"]["note_state"] == "dismissed" and after["actions"] == []
    assert not (lh / "questions.db").exists()


def test_a_note_row_cannot_be_answered_and_a_question_row_cannot_be_dismissed(tmp_path, lh):
    _root, src, _ids = _install(tmp_path)
    n = _file(NoteStore(lh))
    q = QuestionStore(lh).file(agent="claude_code", agent_session_id="s", what="w?", options=[("a", "A"), ("b", "B")],
                               rec="a", blocks="b", cwd="/w")
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, body = _verb(base, "question_answer", _row(base, n.n), {"option": "a"})
        assert status == 409 and "no longer offers 'question_answer' on this row" in body["message"], body
        qrow = next(r for r in _get(f"{base}/cockpit/panel/decisions.json", TOKEN)["rows"]
                    if r["id"] == f"question:{q.n}")
        assert "note_dismiss" not in [a["verb"] for a in qrow["actions"]]
        status, body = _verb(base, "note_dismiss", qrow)
        assert status == 409 and "no longer offers 'note_dismiss' on this row" in body["message"], body
    assert NoteStore(lh).get(n.n).dismissed is None and QuestionStore(lh).get(q.n).answer is None


# --- the phone ---------------------------------------------------------------------------------------------------




# --- L3 r1: the open bound (F1) ----------------------------------------------------------------------------------


def _prefill(lh, count: int, session: str | None = None) -> None:
    """``count`` open notes written straight into an existing store (setup only: it skips filing, never the bound
    under test). A ``None`` session gives each its own, so only the total grows."""
    conn = sqlite3.connect(lh / "notes.db")
    with conn:
        base = conn.execute("SELECT COALESCE(MAX(n), 0) FROM notes").fetchone()[0]
        conn.executemany(
            "INSERT INTO notes (id, agent, agent_session_id, what, detail, cwd, filed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(f"n-pre{base + i:020d}", "codex", session or f"pre-{base + i}", f"pre {base + i}", None, "/w",
              "2026-10-22T00:00:00Z") for i in range(count)])
    conn.close()


def test_the_open_bound_refuses_past_note_open_max_with_nothing_written(lh):
    store = NoteStore(lh)
    _file(store, agent_session_id="first")
    _prefill(lh, NOTE_OPEN_MAX - 2)
    last = _file(store, agent_session_id="last")                  # the NOTE_OPEN_MAX-th open note is filed
    assert store.open_page(1)[1] == NOTE_OPEN_MAX
    with pytest.raises(NoteCapError, match=f"NOTE_OPEN_MAX is {NOTE_OPEN_MAX}"):
        _file(store, agent_session_id="one-more")
    assert store.open_page(1)[1] == NOTE_OPEN_MAX and store.recent(1)[0].id == last.id
    store.dismiss(last.id, channel="cli")                          # a dismissal frees its place
    assert _file(store, agent_session_id="one-more").n == last.n + 1


def test_the_per_session_bound_binds_one_session_and_never_the_no_session_bucket(lh):
    from levain.consent.band import NO_SESSION

    store = NoteStore(lh)
    _file(store, agent_session_id="loop")
    _prefill(lh, NOTE_OPEN_PER_SESSION_MAX - 1, session="loop")
    with pytest.raises(NoteCapError, match=f"NOTE_OPEN_PER_SESSION_MAX is {NOTE_OPEN_PER_SESSION_MAX}"):
        _file(store, agent_session_id="loop")
    assert _file(store, agent_session_id="another").agent_session_id == "another"
    _prefill(lh, NOTE_OPEN_PER_SESSION_MAX, session=NO_SESSION)     # names no session: only the total binds it
    assert _file(store, agent_session_id=NO_SESSION).agent_session_id == NO_SESSION


def test_two_filings_at_the_bound_cannot_both_pass(lh):
    store = NoteStore(lh)
    _file(store, agent_session_id="first")
    _prefill(lh, NOTE_OPEN_MAX - 2)                                # one place left
    results: list[object] = []
    gate = threading.Barrier(2)

    def go(i: int) -> None:
        gate.wait()
        try:
            results.append(_file(NoteStore(lh), agent_session_id=f"race-{i}"))
        except NoteCapError as exc:
            results.append(exc)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(type(r).__name__ for r in results) == ["Note", "NoteCapError"]
    assert store.open_page(1)[1] == NOTE_OPEN_MAX


def test_the_note_tool_names_the_bound_in_its_tool_error(lh):
    _file(NoteStore(lh), agent_session_id="sess-meta")
    _prefill(lh, NOTE_OPEN_PER_SESSION_MAX - 1, session="sess-meta")
    got = _call("note", {"what": WHAT}, {"sessionId": "sess-meta"})["result"]
    text = got["content"][0]["text"]
    assert got["isError"] is True and text.startswith("levain note: refused, nothing was filed: ")
    assert f"NOTE_OPEN_PER_SESSION_MAX is {NOTE_OPEN_PER_SESSION_MAX}" in text
    assert NoteStore(lh).open_page(1)[1] == NOTE_OPEN_PER_SESSION_MAX


def test_open_page_reads_one_page_and_the_surfaces_say_how_many_they_left_out(tmp_path, lh):
    from levain.consent.listing import PENDING_CAP

    store = NoteStore(lh)
    _file(store)
    over = 3
    _prefill(lh, PENDING_CAP + over - 1)          # a store filled past the page (filed before the bound existed)
    page, total = store.open_page(PENDING_CAP)
    assert len(page) == PENDING_CAP and total == PENDING_CAP + over and page[0].n == 1
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        panel = _get(f"{base}/cockpit/panel/decisions.json", TOKEN)
        open_rows = [r for r in panel["rows"] if r["id"].startswith("note:")]
        assert len(open_rows) == PENDING_CAP
        assert {r["facets"]["note_open_total"] for r in open_rows} == {PENDING_CAP + over}
        assert panel["status"] == "partial" and f"{over} more notes are waiting than are shown" in panel["note"]




# --- L3 r1: a kernel row is a decisions row with its facet (F2) ---------------------------------------------------


def _probe_rig(tmp_path):
    from levain.cockpit import verbs as V
    from levain.cockpit.engine import Read, RowIn
    from tests.test_cockpit_k1 import _simple
    from tests.test_cockpit_k2a import Rig

    fired: list[str] = []
    probe = V.VerbSpec("probe", "probe", "e", "row", floor="C1", tier_fn=lambda *a: "C1",
                       fire=lambda scope, params, target, confirm: fired.append(target["row"]["id"]) or {"ok": 1})
    rig = Rig(tmp_path)
    rig.reg._add(probe)     # a row verb with no applies_to (a downstream verb binds to its panel, never a row)
    # a downstream panel whose own rows happen to be named note:/question:, even carrying the kernel's facets
    rows = [RowIn("note:x", "x", {"at": "2026-10-22", "note_n": 1, "note_state": "open"}, stored={"id": "note:x"}),
            RowIn("question:x", "y", {"at": "2026-10-22", "question_n": 1, "question_state": "open"},
                  stored={"id": "question:x"})]
    rig.ck.register(_simple("p", rows=rows, verbs=("probe", "note_dismiss", "question_answer"),
                            read_one=lambda ctx, rid: Read(rows=tuple(r for r in rows if r.id == rid)),
                            facets=frozenset({"at", "note_n", "note_state", "question_n", "question_state"})))
    return rig, fired


def test_a_downstream_panels_note_and_question_rows_keep_their_own_verbs_and_are_not_skipped(tmp_path):
    from levain.cockpit.verbs import dispatch
    from levain.writes import EditError
    from tests.test_cockpit_k2a import CRED, INSTALL_CLASS

    rig, fired = _probe_rig(tmp_path)
    panel = rig.ck.panel("p", credential_class="token")
    assert panel["status"] == "ok" and not panel.get("skipped")
    by_id = {r["id"]: r for r in panel["rows"]}
    assert set(by_id) == {"note:x", "question:x"}
    for rid in by_id:
        assert [a["verb"] for a in by_id[rid]["actions"]] == ["probe"], by_id[rid]["actions"]
    for rid, kernel_verb, params in (("note:x", "note_dismiss", {}), ("question:x", "question_answer", {"text": "ok"})):
        row = rig.ck.read_one("p", rid).value
        req = {"verb": "probe", "panel_id": "p", "row_id": rid, "row_version": row["version"], "params": {}}
        dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, req=req, credential=CRED,
                 install_class=INSTALL_CLASS, route="cockpit")
        with pytest.raises(EditError) as ei:      # the kernel's verb never fires on another panel's row
            dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope,
                     req={**req, "verb": kernel_verb, "params": params}, credential=CRED,
                     install_class=INSTALL_CLASS, route="cockpit")
        assert ei.value.code == "stale", ei.value
    assert fired == ["note:x", "question:x"]


def test_note_and_question_rows_in_decisions_gate_as_before():
    from levain.cockpit import verbs as V

    probe = V.VerbSpec("probe", "probe", "e", "row", floor="C1", tier_fn=lambda *a: "C1")
    reg = V.VerbRegistry()
    dismiss, answer = reg.get("note_dismiss"), reg.get("question_answer")
    note = {"id": "note:3", "facets": {"note_n": 3, "note_state": "open"}}
    question = {"id": "question:3", "facets": {"question_n": 3, "question_state": "open"}}
    bare_note, bare_question = {"id": "note:4", "facets": {}}, {"id": "question:4", "facets": {}}
    ap = V._applies_to_row
    assert ap(dismiss, "decisions", note) and not ap(answer, "decisions", note) and not ap(probe, "decisions", note)
    assert ap(answer, "decisions", question) and not ap(dismiss, "decisions", question)
    assert not ap(probe, "decisions", question)
    # a decisions row of the prefix without the kernel's facet offers nothing (fail closed), the kernel verb included
    for verb in (probe, dismiss, answer):
        assert not ap(verb, "decisions", bare_note) and not ap(verb, "decisions", bare_question)
    # a dismissed note: the verb's own applies_to still refuses it
    assert not ap(dismiss, "decisions", {"id": "note:3", "facets": {"note_n": 3, "note_state": "dismissed"}})


# --- L3 r1: F4-F6 ------------------------------------------------------------------------------------------------


def test_a_note_read_one_on_an_untrusted_store_is_a_fault_not_a_raise(tmp_path, lh):
    from levain.cockpit.results import Fault
    from tests.test_cockpit_k2a import Rig

    rig = Rig(tmp_path)
    _file(NoteStore(lh))
    rig.ck._dec.notes = NoteStore(lh)
    lh.chmod(0o755)                                     # a directory others can reach: NoteStoreError
    try:
        got = rig.ck._note_decision_one(None, "note:1")
    finally:
        lh.chmod(0o700)
    assert isinstance(got, Fault) and "NoteStoreError" in got.message


def test_a_question_read_one_on_an_untrusted_store_is_a_fault_not_a_raise(tmp_path, lh):
    from levain.cockpit.results import Fault
    from tests.test_cockpit_k2a import Rig

    rig = Rig(tmp_path)
    QuestionStore(lh).file(agent="claude_code", agent_session_id="s", what="w?", options=[("a", "A"), ("b", "B")],
                           rec="a", blocks="b", cwd="/w")
    rig.ck._dec.questions = QuestionStore(lh)
    lh.chmod(0o755)
    try:
        got = rig.ck._question_decision_one(None, "question:1")
    finally:
        lh.chmod(0o700)
    assert isinstance(got, Fault) and "QuestionStoreError" in got.message


@pytest.mark.parametrize("route,make", [
    ("note", lambda lh: _file(NoteStore(lh))),
    ("question", lambda lh: QuestionStore(lh).file(agent="claude_code", agent_session_id="s", what="w?",
                                                   options=[("a", "A"), ("b", "B")], rec="a", blocks="b", cwd="/w")),
])
def test_a_body_route_500_names_no_path(tmp_path, lh, route, make, caplog):
    _root, src, _ids = _install(tmp_path)
    make(lh)
    with _serve(src, write_token=TOKEN) as (base, _h):
        lh.chmod(0o755)
        try:
            status, body = _get_status(f"{base}/cockpit/{route}/1.json", TOKEN)
        finally:
            lh.chmod(0o700)
    assert status == 500 and str(lh) not in json.dumps(body) and "StoreError" in body["message"], body
    assert any(str(lh) in r.getMessage() for r in caplog.records)      # the detail is logged, never sent


def test_a_null_detail_is_no_detail(lh):
    got = _call("note", {"what": WHAT, "detail": None})["result"]
    assert got["isError"] is False, got
    (n,) = NoteStore(lh).open_page(NOTE_OPEN_MAX)[0]
    assert n.detail is None
    bad = _call("note", {"what": WHAT, "detail": 7})
    assert bad["error"]["code"] == band_mcp.INVALID_PARAMS
