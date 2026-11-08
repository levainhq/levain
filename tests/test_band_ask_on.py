"""TS-11: ``levain ask --on <date>`` and the ``ask`` tool's ``on``: ONE agenda spore, NO question. Temp dirs only."""
from __future__ import annotations

import io
import json
from datetime import date, datetime, timedelta

import pytest
from anneal_memory import SporeStore

from levain.cli import main
from levain.consent import band, band_mcp
from levain.consent.questions import QuestionStore

FUT = (date.today() + timedelta(days=3)).isoformat()
TODAY = date.today().isoformat()
PAST = (date.today() - timedelta(days=2)).isoformat()


@pytest.fixture
def inst(tmp_path, monkeypatch):
    root = tmp_path / "inst"
    (root / ".levain").mkdir(parents=True)
    (root / ".levain" / "memory.spores.json").write_text("{}")
    monkeypatch.chdir(root)
    return root


def _spores(root):
    return SporeStore(root / ".levain" / "memory.spores.json").list_open()


def _qdir(tmp_path):
    d = tmp_path / "q"
    d.mkdir(mode=0o700)
    return d


def _ask(tmp_path, *extra):
    qd = _qdir(tmp_path) if not (tmp_path / "q").exists() else tmp_path / "q"
    out = io.StringIO()
    args = ["ask", "--what", "Which plan?", "--blocks", "the rollout", "--queue-dir", str(qd), *extra]
    import contextlib
    with contextlib.redirect_stdout(out):
        rc = main(args)
    return rc, out.getvalue(), qd


def test_on_future_writes_one_agenda_and_no_question(tmp_path, inst):
    rc, text, qd = _ask(tmp_path, "--on", FUT)
    assert rc == 0, text
    assert f"filed for {FUT}" in text and "reaches the operator then" in text
    [s] = _spores(inst)
    assert (s["disposition"], s["next"], s["target"]) == ("agenda", FUT, "band")
    assert "attention" not in s or s["attention"] is None
    assert "Which plan?" in s["text"] and "the rollout" in s["text"]
    assert QuestionStore(qd).open_questions() == []


def test_attention_talk_is_a_nudge_agenda(tmp_path, inst):
    rc, text, _ = _ask(tmp_path, "--on", FUT, "--attention", "talk")
    assert rc == 0, text
    [s] = _spores(inst)
    assert (s["disposition"], s["target"], s["attention"]) == ("agenda", "nudge", "talk")


@pytest.mark.parametrize("when", [TODAY, PAST])
def test_today_or_past_is_refused_with_its_reason(tmp_path, inst, when):
    rc, text, qd = _ask(tmp_path, "--on", when)
    assert rc == 2
    assert "today or past" in text and "without --on" in text and "nothing was filed" in text
    assert _spores(inst) == [] and QuestionStore(qd).open_questions() == []


def test_bad_date_options_and_no_install_are_refused(tmp_path, inst, monkeypatch):
    rc, text, _ = _ask(tmp_path, "--on", "next week")
    assert rc == 2 and "YYYY-MM-DD" in text
    rc, text, _ = _ask(tmp_path, "--on", FUT, "--option", "a=A", "--option", "b=B", "--rec", "a")
    assert rc == 2 and "files no question" in text
    rc, text, _ = _ask(tmp_path, "--attention", "talk")
    assert rc == 2 and "goes with --on" in text
    assert _spores(inst) == []
    monkeypatch.chdir(tmp_path)
    rc, text, _ = _ask(tmp_path, "--on", FUT)
    assert rc == 2 and "no Levain install" in text


def test_timed_future_is_refused_by_the_anneal_write_path(tmp_path, inst):
    when = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%dT09:00")
    rc, text, _ = _ask(tmp_path, "--on", when)
    assert rc == 2 and "cannot be stored yet" in text and f"--on {when[:10]}" in text
    assert _spores(inst) == []


def test_mcp_schema_lists_on_and_the_tool_files_an_agenda(tmp_path, inst):
    props = band_mcp.TOOL["inputSchema"]["properties"]
    assert props["on"]["type"] == "string" and props["attention"]["enum"] == ["talk", "none"]
    assert band_mcp.TOOL["inputSchema"]["required"] == ["what", "blocks"]
    listed = band_mcp.handle("claude_code", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "on" in listed["result"]["tools"][0]["inputSchema"]["properties"]

    def call(**a):
        r = band_mcp.handle("claude_code", {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                            "params": {"name": "ask", "arguments": a}})
        return r["result"]
    ok = call(what="Ship it?", blocks="the release", on=FUT)
    assert ok["isError"] is False and f"filed for {FUT}" in ok["content"][0]["text"]
    [s] = _spores(inst)
    assert (s["disposition"], s["next"], s["target"]) == ("agenda", FUT, "band")
    bad = call(what="x", blocks="y", on=TODAY)
    assert bad["isError"] is True and "today or past" in bad["content"][0]["text"]
    bad = call(what="x", blocks="y", on=FUT, rec="a")
    assert bad["isError"] is True and "no question" in bad["content"][0]["text"]
    assert len(_spores(inst)) == 1


# --- TS-11 L3 fixes ---------------------------------------------------------------------------------------------

def _call(**a):
    r = band_mcp.handle("claude_code", {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                        "params": {"name": "ask", "arguments": a}})
    return r


def test_attention_none_is_accepted_cli_and_mcp(tmp_path, inst):
    rc, text, _ = _ask(tmp_path, "--on", FUT, "--attention", "none")
    assert rc == 0, text
    ok = _call(what="n", blocks="b", on=FUT, attention="none")["result"]
    assert ok["isError"] is False, ok
    rows = _spores(inst)
    assert len(rows) == 2 and all(s["target"] == "band" for s in rows)


def test_file_agenda_with_none_what_or_blocks_is_a_clear_refusal(inst):
    for what, blocks in ((None, "b"), ("w", None), (None, None)):
        item, text = band.file_agenda(what, blocks, FUT)
        assert item is None and "nothing was filed" in text and "must be text" in text
    assert _spores(inst) == []


def test_timed_is_refused_before_the_store_is_called(tmp_path, inst, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the store must not be reached")
    monkeypatch.setattr(band, "_store_add", boom, raising=False)
    import levain.spores as sp
    monkeypatch.setattr(sp, "guarded_spore_store", boom)
    when = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%dT09:00")
    item, text = band.file_agenda("w", "b", when)
    assert item is None and "cannot be stored yet" in text


def test_corrupt_spore_store_is_a_clean_refusal(tmp_path, inst):
    (inst / ".levain" / "memory.spores.json").write_text("{not json")
    rc, text, _ = _ask(tmp_path, "--on", FUT)
    assert rc == 2 and "nothing was filed" in text and "spore store" in text
    r = _call(what="w", blocks="b", on=FUT)
    assert "error" not in r and r["result"]["isError"] is True
    assert "nothing was filed" in r["result"]["content"][0]["text"]


def test_mcp_attention_without_on_is_refused(inst):
    r = _call(what="w", blocks="b", options=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}], rec="a",
              attention="talk")
    assert "error" not in r and r["result"]["isError"] is True
    assert "goes with on" in r["result"]["content"][0]["text"]


def test_mcp_zero_options_is_not_ignored(inst):
    r = _call(what="w", blocks="b", on=FUT, open=0)
    assert r["result"]["isError"] is True


def test_a_retry_is_idempotent(tmp_path, inst):
    for _ in range(2):
        rc, text, _q = _ask(tmp_path, "--on", FUT)
        assert rc == 0, text
    assert len(_spores(inst)) == 1


def test_a_differing_ask_files_its_own_spore(tmp_path, inst):
    _ask(tmp_path, "--on", FUT)
    rc, _t, _q = _ask(tmp_path, "--on", FUT, "--attention", "talk")
    assert rc == 0 and len(_spores(inst)) == 2
    item, _x = band.file_agenda("Which plan?", "another block", FUT)
    assert item is not None and len(_spores(inst)) == 3


def test_a_global_home_levain_dir_is_not_an_install(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".levain").mkdir(parents=True)
    work = home / "work"
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(work)
    item, text = band.file_agenda("w", "b", FUT)
    assert item is None and "no Levain install" in text
    assert not (home / ".levain" / "memory.spores.json").exists()


def test_fresh_install_without_a_store_is_found_and_nested_resolves_innermost(tmp_path, monkeypatch):
    outer = tmp_path / "outer"
    (outer / ".levain").mkdir(parents=True)
    (outer / ".levain" / "memory.spores.json").write_text("{}")
    inner = outer / "sub" / "inner"
    (inner / ".levain").mkdir(parents=True)          # fresh: no store file yet
    monkeypatch.chdir(inner / ".levain".replace(".levain", ""))
    item, text = band.file_agenda("w", "b", FUT)
    assert item is not None, text
    assert (inner / ".levain" / "memory.spores.json").is_file()
    assert _spores(outer) == []
    assert len(SporeStore(inner / ".levain" / "memory.spores.json").list_open()) == 1


def test_explicit_path_selects_the_install(tmp_path, inst, monkeypatch):
    other = tmp_path / "other"
    (other / ".levain").mkdir(parents=True)
    r = _call(what="w", blocks="b", on=FUT, path=str(other))
    assert r["result"]["isError"] is False, r
    assert _spores(inst) == [] and len(SporeStore(other / ".levain" / "memory.spores.json").list_open()) == 1
    rc, text, _ = _ask(tmp_path, "--on", FUT, "--path", str(other))
    assert rc == 0 and len(SporeStore(other / ".levain" / "memory.spores.json").list_open()) == 2  # --path honoured
