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
from levain.consent.notes import NoteStore
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
    assert [x.id for x in store.open_notes()] == [n.id]
    d = store.dismiss(n.id, channel="cockpit")
    assert d.channel == "cockpit"
    assert store.open_notes() == [] and store.get(n.n).dismissed is not None
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
    assert store.open_notes() == []


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
    assert store.open_notes() == [] and store.recent() == [] and store.get(1) is None
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
    (n,) = NoteStore(lh).open_notes()
    assert (n.agent, n.agent_session_id, n.what, n.detail) == ("claude_code", "sess-meta", WHAT, DETAIL)
    assert not (lh / "questions.db").exists()


def test_a_refused_note_is_a_tool_error_and_files_nothing(lh):
    got = _call("note", {"what": "fine‮txt"})["result"]
    assert got["isError"] is True and "U+202E" in got["content"][0]["text"]
    bad = _call("note", {"what": WHAT, "rec": "a"})
    assert bad["error"]["code"] == band_mcp.INVALID_PARAMS
    assert NoteStore(lh).open_notes() == []


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


def test_the_phone_lists_open_notes_and_dismisses_one_without_delivery(tmp_path, monkeypatch):
    from levain.consent import band
    from tests.test_phone_questions import _call, _serving

    monkeypatch.setattr(band, "deliver_answer", lambda *a, **k: pytest.fail("a dismiss must deliver nothing"))
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    n = _file(NoteStore(qdir))
    with _serving(qdir) as base:
        status, listing = _call(f"{base}/consent.json")
        assert status == 200
        (row,) = listing["notes"]
        assert (row["n"], row["what"], row["detail"], row["agent"]) == (n.n, WHAT, DETAIL, "claude_code")
        assert listing["notes_overflow"] == 0 and listing["notes_note"] == ""
        assert _call(f"{base}/note/{n.n}/dismiss", {}, token=None)[0] == 403
        assert _call(f"{base}/note/{n.n}/dismiss", {}) == (200, {"dismissed": n.n})
        assert _call(f"{base}/consent.json")[1]["notes"] == []
        assert _call(f"{base}/note/{n.n}/dismiss", {})[0] == 409
        assert _call(f"{base}/note/99/dismiss", {})[0] == 404
        assert _call(f"{base}/question/{n.n}/answer", {"text": "ok"})[0] == 404   # a note's number is no question
    assert NoteStore(qdir).get(n.n).dismissed.channel == "phone"
    assert not (qdir / "questions.db").exists()
