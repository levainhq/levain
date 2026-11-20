"""levain#79: the Desk view shows the live desk's context usage against its rotation lines (SOFT / HARD).

The tokens come from the desk session's transcript usage block, read from the transcript's tail only (levain.desk.desk_context); the
reading is held equal to flow's scripts/seat_context.py for the same transcript where flow's checkout is present. The route carries
it (levain.cockpit.desk_tab), the page draws it (desk_tab_view.js, the lease bar). LEVAIN_DESK_SHOTS=<dir> saves the screenshots.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from levain.cockpit import desk_tab
from levain.desk import desk_context
from levain.desk.desk_context import DEEP_HARD, HARD, MAX_TAIL, SOFT, ContextView, Named, NoReading, parse_command, read_context
from levain.desk.desk_port import DeskCommands, HostCommandDeskPort, ScriptedDeskPort
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_desk_pane_page import _OPEN, _TAP_DESK, _TOKENED, desk_page, put, until, wired

PHONE = {"emulate": {"w": 390, "h": 844, "dpr": 3, "mobile": True}}


def rec(inp=3, read=0, create=0, model="claude-opus-5", kind="assistant", ts="2026-11-16T14:00:00.000Z") -> str:
    return json.dumps({"type": kind, "timestamp": ts, "message": {"model": model, "role": kind, "content": [{"type": "text", "text": "x"}],
                                                                  "usage": {"input_tokens": inp, "cache_read_input_tokens": read,
                                                                            "cache_creation_input_tokens": create, "output_tokens": 7}}})


def transcript(tmp_path: Path, lines: list[str], name="s.jsonl") -> Path:
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n")
    return p


# --- the reading ------------------------------------------------------------------------------------------------------------------

def test_the_reading_is_the_newest_usage_sum_input_plus_cache_read_plus_cache_creation(tmp_path):
    p = transcript(tmp_path, [rec(5, 100_000, 2_000), json.dumps({"type": "user", "message": {"content": "hi"}}), rec(3, 212_000, 400, model="claude-opus-5-5"),
                              json.dumps({"type": "user", "message": {"content": "again"}})])
    got = read_context(p)
    assert got == ContextView(212_403, SOFT, HARD, "claude-opus-5-5"), got
    assert (got.soft, got.hard) == (350_000, 500_000)


def test_a_record_with_no_usage_or_a_zero_sum_is_skipped_and_none_at_all_is_unreadable_never_zero(tmp_path):
    p = transcript(tmp_path, [rec(1, 50_000, 0), rec(0, 0, 0), json.dumps({"type": "system", "subtype": "x"}), "not json at all {"])
    assert read_context(p).tokens == 50_001
    assert read_context(transcript(tmp_path, [rec(0, 0, 0), '{"type":"user"}'], "z.jsonl")) == NoReading("no usage record yet")      # a desk with no turn yet
    assert read_context(tmp_path / "missing.jsonl") is None
    assert read_context(transcript(tmp_path, [], "empty.jsonl")) == NoReading("no usage record yet")


def test_a_go_deep_desk_has_no_soft_line_and_the_deep_hard_line(tmp_path):
    p = transcript(tmp_path, [rec(1, 400_000, 0)])
    got = read_context(p, deep=True)
    assert (got.soft, got.hard) == (None, DEEP_HARD) and got.tokens == 400_001, got


def test_a_compaction_restarts_the_reading_at_the_newest_record(tmp_path):
    p = transcript(tmp_path, [rec(1, 960_000, 0), json.dumps({"type": "system", "subtype": "compact_boundary"}), rec(1, 17_000, 0)])
    assert read_context(p).tokens == 17_001


def _spy(monkeypatch):
    seen: list[int] = []
    real = desk_context._tail
    monkeypatch.setattr(desk_context, "_tail", lambda f, size, window: (seen.append(window), real(f, size, window))[1])
    return seen


def test_the_read_is_bounded_a_long_transcript_is_never_read_whole(tmp_path, monkeypatch):
    # 12 MB of records with no usage block, one usage record just before the last 100 KB: the first window (256 KB) holds it
    filler = json.dumps({"type": "user", "message": {"content": "y" * 900}})
    p = tmp_path / "big.jsonl"
    with open(p, "w") as f:
        for _ in range(13_000):
            f.write(filler + "\n")
        f.write(rec(2, 300_000, 0) + "\n")
        for _ in range(100):
            f.write(filler + "\n")
    assert p.stat().st_size > 12_000_000
    seen = _spy(monkeypatch)
    assert read_context(p).tokens == 300_002
    assert seen == [262_144], seen


def test_the_window_grows_to_find_a_usage_record_and_stops_at_the_cap(tmp_path, monkeypatch):
    filler = json.dumps({"type": "user", "message": {"content": "y" * 900}})
    near = tmp_path / "near.jsonl"
    with open(near, "w") as f:
        f.write(rec(2, 111_000, 0) + "\n")
        for _ in range(2_000):         # about 1.8 MB after it: past the first two windows
            f.write(filler + "\n")
    seen = _spy(monkeypatch)
    assert read_context(near).tokens == 111_002 and seen == [262_144, 1_048_576, 4_194_304], seen
    far = tmp_path / "far.jsonl"
    with open(far, "w") as f:
        f.write(rec(2, 111_000, 0) + "\n")
        for _ in range(9_500):         # about 8.5 MB after it: beyond the cap, so unreadable here (never zero, never a whole-file read)
            f.write(filler + "\n")
    seen.clear()
    assert far.stat().st_size > MAX_TAIL
    assert read_context(far) is None and max(seen) == MAX_TAIL, seen


def test_a_seek_that_lands_inside_a_record_drops_its_partial_head_and_one_on_a_record_start_keeps_it(tmp_path):
    lines = [rec(1, 10_000 + i, 0) for i in range(40)]
    p = transcript(tmp_path, lines)
    size = p.stat().st_size
    with open(p, "rb") as f:
        whole = desk_context._tail(f, size, 10 * size)
        assert whole == [l.encode() for l in lines]
        last = len(lines[-1]) + 1
        assert desk_context._tail(f, size, last) == [lines[-1].encode()]            # on the first byte of the last record: kept whole
        assert desk_context._tail(f, size, last + 5) == [lines[-1].encode()]        # inside the one before: its partial head is dropped


# (the three parity tests against flow's scripts/seat_context.py are in tests/test_desk_context_1116_flow_parity.py: they skip off the
# operator's machine, and a skip in this file fails the CI browser job)

# --- the host's command and the port ----------------------------------------------------------------------------------------------

def test_the_command_names_a_jsonl_by_absolute_path_and_nothing_else_is_taken():
    assert parse_command('{"transcript": "/a/b/s.jsonl"}') == Named(Path("/a/b/s.jsonl"), False, None)
    assert parse_command('{"transcript": "/a/b/s.jsonl", "deep": true, "extra": 1}') == Named(Path("/a/b/s.jsonl"), True, None)
    assert parse_command('{"transcript": "/a/b/s.jsonl", "deep": "yes", "holder": "desk 12"}') == Named(Path("/a/b/s.jsonl"), False, "desk 12")
    assert parse_command('{"transcript": "/a/b/s.jsonl", "holder": 7}').holder is None
    for bad in ("", "x", "[]", "{}", '{"transcript": 3}', '{"transcript": "rel/s.jsonl"}', '{"transcript": "/a/b/s.txt"}', '{"transcript": "/a/\\u0000b.jsonl"}'):
        assert parse_command(bad) is None, bad


def _port(tmp_path, out: str | None, *, code=0):
    script = tmp_path / "ctxcmd.py"
    script.write_text("import sys\nprint(open(sys.argv[1]).read(), end='')\nsys.exit(int(sys.argv[2]))\n")
    said = tmp_path / "said.json"
    said.write_text(out or "")
    cmds = DeskCommands(post=(sys.executable, "/x"), lease=(sys.executable, str(script), str(tmp_path / "lease.json"), "0"),
                        context=(sys.executable, str(script), str(said), str(code)) if out is not None else None)
    (tmp_path / "lease.json").write_text(json.dumps({"state": "held", "holder": "desk 12", "age_s": 30}))
    return HostCommandDeskPort(cmds, tmp_path / "claims.sqlite")


def test_the_real_port_reads_the_transcript_the_host_names_and_the_route_carries_it(tmp_path):
    t = transcript(tmp_path, [rec(5, 212_000, 400, model="claude-opus-5-5")], "desk.jsonl")
    port = _port(tmp_path, json.dumps({"transcript": str(t)}))
    assert port.has_context and port.context() == ContextView(212_405, SOFT, HARD, "claude-opus-5-5")
    status, body = desk_tab.serve_desk(port)
    assert status == 200 and body["context"] == {"state": "ok", "tokens": 212_405, "soft": SOFT, "hard": HARD, "model": "claude-opus-5-5", "holder": None}, body


def test_an_unreadable_context_is_said_unreadable_never_zero(tmp_path):
    t = transcript(tmp_path, [json.dumps({"type": "user"})], "nousage.jsonl")
    for out, code in (("not json", 0), (json.dumps({"transcript": str(tmp_path / "gone.jsonl")}), 0), (json.dumps({"transcript": str(t)}), 3)):
        d = tmp_path / f"c{abs(hash((out, code)))}"
        d.mkdir()
        port = _port(d, out, code=code)
        assert port.context() is None
        assert desk_tab.serve_desk(port)[1]["context"] == {"state": "unreadable"}, (out, code)


def test_the_route_carries_no_context_without_a_context_command_or_a_held_lease(tmp_path):
    assert "context" not in desk_tab.serve_desk(_port(tmp_path, None))[1]                       # a port with no context command
    s = ScriptedDeskPort()
    s.script_lease("held", "desk 12", 5)
    assert "context" not in desk_tab.serve_desk(s)[1]
    s.script_context(ContextView(1000, SOFT, HARD, None))
    assert desk_tab.serve_desk(s)[1]["context"]["tokens"] == 1000
    s.script_lease("none")
    assert "context" not in desk_tab.serve_desk(s)[1]                                            # no desk is live: no context to show
    s.script_lease("unknown")
    assert "context" not in desk_tab.serve_desk(s)[1]


def test_a_port_whose_context_raises_is_unreadable_not_a_500():
    class Boom(ScriptedDeskPort):
        def context(self):
            raise RuntimeError("x")
    s = Boom()
    s.script_lease("held", "desk 12", 5)
    s.script_context(None)
    assert desk_tab.serve_desk(s)[1]["context"] == {"state": "unreadable"}


def test_the_context_key_is_a_desk_command_key_and_the_json_keeps_its_old_shape_without_it():
    assert DeskCommands.from_env(json.dumps({"post": ["/p"], "context": ["/c"]})).context == ("/c",)
    assert "context" not in json.loads(DeskCommands(post=("/p",)).to_json())
    assert json.loads(DeskCommands(post=("/p",), context=("/c",)).to_json())["context"] == ["/c"]


# --- the L3 round-1 fixes (levain#79) ---------------------------------------------------------------------------------------------------

def _within(seconds, fn):
    """fn()'s result, or the string 'BLOCKED' when it has not returned in ``seconds`` (the blocked thread is a daemon)."""
    import threading
    box = []
    t = threading.Thread(target=lambda: box.append(fn()), daemon=True)
    t.start()
    t.join(seconds)
    return box[0] if box else "BLOCKED"


def test_a_fifo_named_as_the_transcript_is_refused_at_once_not_waited_on(tmp_path):
    fifo = tmp_path / "pipe.jsonl"
    os.mkfifo(fifo)
    assert _within(3, lambda: read_context(fifo)) is None          # an open() with no writer would block here for ever
    assert _within(3, lambda: read_context("/dev/null")) is None   # a device is not a regular file either (and /dev/null is no .jsonl, but the read refuses on the mode)
    assert read_context(transcript(tmp_path, [rec(2, 9_000, 0)])).tokens == 9_002     # a regular file still reads


def test_a_fifo_cannot_hold_both_desk_read_slots(tmp_path):
    fifo = tmp_path / "pipe.jsonl"
    os.mkfifo(fifo)
    port = _port(tmp_path, json.dumps({"transcript": str(fifo)}))
    for _ in range(3):
        assert _within(5, lambda: desk_tab.serve_desk(port)[1]["context"]) == {"state": "unreadable"}


def test_a_compaction_boundary_newer_than_the_newest_usage_record_is_no_reading_yet_not_the_old_number(tmp_path):
    boundary = json.dumps({"type": "system", "subtype": "compact_boundary", "compactMetadata": {"trigger": "auto", "preTokens": 966_749}})
    p = transcript(tmp_path, [rec(1, 960_000, 0), '{"type":"user"}', boundary, '{"type":"user"}'])
    assert read_context(p) == NoReading("compacted")
    assert read_context(transcript(tmp_path, [rec(1, 960_000, 0), boundary, rec(1, 17_000, 0)], "after.jsonl")).tokens == 17_001
    # a record that only MENTIONS the marker is not a boundary
    assert read_context(transcript(tmp_path, [rec(1, 5_000, 0), json.dumps({"type": "user", "message": {"content": '"compact_boundary"'}})], "m.jsonl")).tokens == 5_001


def test_a_true_in_a_usage_field_is_not_a_token_and_a_deeply_nested_line_is_not_a_crash(tmp_path):
    lie = json.dumps({"type": "assistant", "message": {"model": "m", "usage": {"input_tokens": True, "cache_read_input_tokens": 0}}})
    assert read_context(transcript(tmp_path, [rec(1, 40_000, 0), lie])).tokens == 40_001          # the lie is skipped, never 1
    assert read_context(transcript(tmp_path, [lie], "only.jsonl")) == NoReading("no usage record yet")
    deep = '{"usage":' + "[" * 100_000 + "]" * 100_000 + "}"
    assert read_context(transcript(tmp_path, [rec(1, 40_000, 0), deep], "deep.jsonl")).tokens == 40_001
    assert parse_command("[" * 100_000 + "]" * 100_000) is None


def _slow_port(tmp_path, delay: float, *, holder=None, stderr=False):
    """A port whose context command sleeps ``delay`` s (and writes stderr on a successful exit when asked); counts its runs in runs.txt."""
    t = transcript(tmp_path, [rec(5, 212_000, 400, model="claude-opus-5-5")], "desk.jsonl")
    said = {"transcript": str(t), **({"holder": holder} if holder else {})}
    script = tmp_path / "slow.py"
    script.write_text("import sys, time\nopen(sys.argv[1], 'a').write('x')\ntime.sleep(float(sys.argv[2]))\n"
                      + ("sys.stderr.write('a warning the host printed\\n')\n" if stderr else "") + "print(sys.argv[3])\n")
    lease = tmp_path / "lease.json"
    lease.write_text(json.dumps({"state": "held", "holder": "desk 12", "age_s": 30}))
    echo = tmp_path / "echo.py"
    echo.write_text("import sys\nprint(open(sys.argv[1]).read(), end='')\n")
    runs = tmp_path / "runs.txt"
    runs.write_text("")
    cmds = DeskCommands(post=(sys.executable, "/x"), lease=(sys.executable, str(echo), str(lease)),
                        context=(sys.executable, str(script), str(runs), str(delay), json.dumps(said)))
    return HostCommandDeskPort(cmds, tmp_path / "claims.sqlite"), runs


def test_the_context_command_runs_at_most_once_per_ttl_not_once_per_poll(tmp_path):
    import time
    port, runs = _slow_port(tmp_path, 0.6)
    t0 = time.monotonic()
    first = desk_tab.serve_desk(port)[1]["context"]
    cold = time.monotonic() - t0
    t1 = time.monotonic()
    for _ in range(5):
        assert desk_tab.serve_desk(port)[1]["context"] == first
    warm = (time.monotonic() - t1) / 5
    assert first["state"] == "ok" and first["tokens"] == 212_405
    assert len(runs.read_text()) == 1, runs.read_text()
    assert cold >= 0.5 and warm < 0.4, (cold, warm)        # the slow command is paid once; the other polls pay the lease command only


def test_a_receipt_poll_never_waits_on_the_context_command_and_the_next_poll_has_the_refreshed_reading(tmp_path):
    import time
    port, runs = _slow_port(tmp_path, 1.0)
    t0 = time.monotonic()
    body = desk_tab.serve_desk(port, "thread=t1")[1]            # a poll that names a thread: the receipt path
    took = time.monotonic() - t0
    assert took < 0.8 and body["context"] == {"state": "none", "why": "not read yet", "holder": None}, (took, body["context"])
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and not (port._ctx_held and not port._ctx_busy):
        time.sleep(0.05)
    assert desk_tab.serve_desk(port, "thread=t1")[1]["context"]["tokens"] == 212_405
    assert len(runs.read_text()) == 1


def test_an_aged_reading_is_served_at_once_while_one_refresh_runs_behind_the_poll(tmp_path, monkeypatch):
    import time
    port, runs = _slow_port(tmp_path, 0.5)
    assert desk_tab.serve_desk(port)[1]["context"]["state"] == "ok"            # the first ask runs the command in the caller
    monkeypatch.setattr("levain.desk.desk_port.CONTEXT_TTL", 0.0)               # every reading is now aged
    t0 = time.monotonic()
    assert desk_tab.serve_desk(port)[1]["context"]["tokens"] == 212_405
    assert time.monotonic() - t0 < 0.4                                         # the aged reading came back without waiting for the command
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and len(runs.read_text()) < 2:
        time.sleep(0.05)
    assert len(runs.read_text()) == 2


def test_concurrent_polls_share_one_run_of_the_context_command(tmp_path):
    import threading
    port, runs = _slow_port(tmp_path, 0.5)
    outs = []
    ts = [threading.Thread(target=lambda: outs.append(desk_tab.serve_desk(port)[1]["context"])) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(outs) == 4 and all(o["tokens"] == 212_405 for o in outs) and len(runs.read_text()) == 1


def test_a_context_command_that_exits_zero_with_stderr_is_still_read(tmp_path):
    port, _runs = _slow_port(tmp_path, 0.0, stderr=True)
    assert desk_tab.serve_desk(port)[1]["context"]["tokens"] == 212_405


def test_a_context_that_names_another_desk_than_the_lease_is_unreadable_and_one_that_matches_carries_its_holder(tmp_path):
    other, _ = _slow_port(tmp_path, 0.0, holder="desk 13")
    assert desk_tab.serve_desk(other)[1]["context"] == {"state": "unreadable", "holder": "desk 13"}
    d2 = tmp_path / "b"
    d2.mkdir()
    same, _ = _slow_port(d2, 0.0, holder="desk 12")
    c = desk_tab.serve_desk(same)[1]["context"]
    assert c["state"] == "ok" and c["holder"] == "desk 12"
    d3 = tmp_path / "c"
    d3.mkdir()
    unnamed, _ = _slow_port(d3, 0.0)
    assert desk_tab.serve_desk(unnamed)[1]["context"]["state"] == "ok"        # a host that does not say cannot be checked: taken as the lease's


def test_a_no_reading_desk_is_said_none_and_caps_that_cannot_be_asked_carry_no_context():
    s = ScriptedDeskPort()
    s.script_lease("held", "desk 12", 5)
    s.script_context(NoReading("no usage record yet"))
    assert desk_tab.serve_desk(s)[1]["context"] == {"state": "none", "why": "no usage record yet", "holder": None}

    class NoCaps(ScriptedDeskPort):
        def capabilities(self):
            raise RuntimeError("x")
    n = NoCaps()
    n.script_lease("held", "desk 12", 5)
    n.script_context(ContextView(1000, SOFT, HARD, None))
    body = desk_tab.serve_desk(n)[1]
    assert body["readable"] is False and "context" not in body


def test_the_context_tail_is_not_re_read_while_the_transcript_is_unchanged(tmp_path, monkeypatch):
    port, runs = _slow_port(tmp_path, 0.0)
    calls = []
    real = desk_context.read_context
    monkeypatch.setattr(desk_context, "read_context", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    for _ in range(3):
        port._read_context_now()
    assert len(runs.read_text()) == 3 and len(calls) == 1, (runs.read_text(), calls)       # the command ran each time, the 8 MB tail once


# --- the page ---------------------------------------------------------------------------------------------------------------------

def _shot(name: str) -> list[dict]:
    d = os.environ.get("LEVAIN_DESK_SHOTS")
    if not d:
        return []
    Path(d).mkdir(parents=True, exist_ok=True)
    return [{"shot": str(Path(d) / name), "full": False}]


_CTX = """(() => { const z = document.querySelector('.dv-zone'), c = z.querySelector('.dv-ctx');
  const fill = c && c.querySelector('.dv-ctxfill'), tick = c && c.querySelector('.dv-ctxtick');
  return JSON.stringify({ ctx: !!c, cls: c ? c.className : null, words: c ? c.querySelector('.dv-ctxwords').textContent : null,
    fill: fill ? fill.style.width : null, tick: tick ? tick.style.left : null, bar: z.querySelector('.dv-state').innerText,
    pageX: document.documentElement.scrollWidth > innerWidth + 1, ctxMark: 1 }); })()"""
CTX = {"eval": _CTX}


def ctxs(out) -> list[dict]:
    return [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{") and '"ctxMark"' in v]


def ok(tokens, soft=SOFT, hard=HARD):
    return {"state": "ok", "tokens": tokens, "soft": soft, "hard": hard, "model": "claude-opus-5-5"}


@needs_browser
def test_the_desk_view_draws_the_context_against_its_soft_and_hard_lines(tmp_path):
    with desk_page(tmp_path) as (stub, base):
        out = _drive(tmp_path, base + "/", [PHONE, *_TOKENED, _TAP_DESK, _OPEN,
            put(get={"status": 200, "json": wired(context=ok(212_403))}), until("document.querySelector('.dv-ctx')"), CTX, *_shot("desk_ctx_ok_390.png"),
            put(get={"status": 200, "json": wired(context=ok(380_000))}), until("document.querySelector('.dv-ctx-soft')"), CTX, *_shot("desk_ctx_soft_390.png"),
            put(get={"status": 200, "json": wired(context=ok(512_345))}), until("document.querySelector('.dv-ctx-hard')"), CTX, *_shot("desk_ctx_hard_390.png"),
            put(get={"status": 200, "json": wired(context=ok(600_000, soft=None, hard=DEEP_HARD))}), until("document.querySelector('.dv-ctx-ok')"), CTX,
            put(get={"status": 200, "json": wired(context={"state": "unreadable"})}), until("document.querySelector('.dv-ctx-unreadable')"), CTX,
            put(get={"status": 200, "json": wired()}), until("document.querySelector('.dv-lease-held') && !document.querySelector('.dv-ctx')"), CTX])
    low, soft, hard, deep, unreadable, absent = ctxs(out)
    assert low["cls"] == "dv-ctx dv-ctx-ok" and low["words"] == "Context 212,403 tokens · SOFT 350,000 · HARD 500,000", low
    assert low["fill"] == "42.5%" and low["tick"] == "70%" and not low["pageX"], low
    assert soft["cls"] == "dv-ctx dv-ctx-soft" and soft["words"].endswith("HARD 500,000 · rotate at the next natural seam"), soft
    assert hard["cls"] == "dv-ctx dv-ctx-hard" and hard["words"].endswith("· rotate now") and hard["fill"] == "100%", hard
    assert deep["words"] == "Context 600,000 tokens · HARD 800,000" and deep["tick"] is None and deep["fill"] == "75%", deep      # go deep: no SOFT line
    assert unreadable["words"] == "The desk's context could not be read just now." and unreadable["fill"] is None, unreadable
    assert not absent["ctx"], absent                                                                                              # a host with no context command: no line


@needs_browser
def test_a_lease_that_is_not_held_draws_no_context_even_if_the_server_sent_one(tmp_path):
    with desk_page(tmp_path) as (stub, base):
        out = _drive(tmp_path, base + "/", [PHONE, *_TOKENED, _TAP_DESK, _OPEN,
            put(get={"status": 200, "json": wired(lease={"state": "none"}, context=ok(212_403))}), until("document.querySelector('.dv-lease-none')"), CTX])
    assert not ctxs(out)[0]["ctx"]


@needs_browser
def test_a_hostile_context_object_draws_as_unreadable_text_only(tmp_path):
    with desk_page(tmp_path) as (stub, base):
        out = _drive(tmp_path, base + "/", [PHONE, *_TOKENED, _TAP_DESK, _OPEN,
            put(get={"status": 200, "json": wired(context={"state": "ok", "tokens": "<img src=x onerror=window.__pwn=1>", "soft": 1, "hard": 2})}),
            until("document.querySelector('.dv-ctx')"), CTX, {"eval": "JSON.stringify({pwn: window.__pwn === undefined, imgs: document.querySelectorAll('.dv-zone img').length, ctxMark: 2})"}])
    g = ctxs(out)
    assert g[0]["cls"] == "dv-ctx dv-ctx-unreadable" and g[0]["words"] == "The desk's context could not be read just now.", g[0]
    assert g[1] == {"pwn": True, "imgs": 0, "ctxMark": 2}, g


@needs_browser
def test_a_desk_with_no_reading_yet_says_so_and_is_not_drawn_as_unreadable_or_zero(tmp_path):
    with desk_page(tmp_path) as (stub, base):
        out = _drive(tmp_path, base + "/", [PHONE, *_TOKENED, _TAP_DESK, _OPEN,
            put(get={"status": 200, "json": wired(context={"state": "none", "why": "no usage record yet", "holder": None})}), until("document.querySelector('.dv-ctx-none')"), CTX,
            *_shot("desk_ctx_none_390.png")])
    g = ctxs(out)[0]
    assert g["cls"] == "dv-ctx dv-ctx-none" and g["words"].startswith("No context reading yet") and g["fill"] is None and not g["pageX"], g


@needs_browser
def test_the_lease_line_points_to_start_desk_only_when_the_button_is_drawn(tmp_path):
    words = "document.querySelector('.dv-zone .dv-leasewords').textContent"
    with desk_page(tmp_path) as (stub, base):
        out = _drive(tmp_path, base + "/", [PHONE, *_TOKENED, _TAP_DESK, _OPEN,
            put(get={"status": 200, "json": wired(lease={"state": "none", "open_desk": True})}), until("document.querySelector('.dv-lease-none .dv-open')"),
            {"eval": "JSON.stringify({w: " + words + ", marker: 'lw'})"},
            put(get={"status": 200, "json": wired()}), until("document.querySelector('.dv-lease-held')"),
            {"eval": "(() => { window.LevainSignSheet.openDrop = null; return 1; })()"},
            put(get={"status": 200, "json": wired(lease={"state": "none", "open_desk": True})}), until("document.querySelector('.dv-lease-none')"),
            {"eval": "JSON.stringify({w: " + words + ", start: !!document.querySelector('.dv-zone .dv-open'), marker: 'lw'})"}])
    with_btn, without = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{") and '"marker":"lw"' in v]
    assert with_btn["w"].endswith("To start one, use Open a Desk below."), with_btn
    assert "Open a Desk" not in without["w"] and not without["start"] and without["w"].endswith("A typed line never starts a desk."), without


def test_the_context_model_never_makes_a_non_finite_bar_and_the_desk_commands_keep_their_positions(tmp_path):
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("no node")
    js = Path(__file__).resolve().parents[1] / "levain" / "templates" / "web" / "desk_tab_view.js"
    chk = tmp_path / "chk.js"
    chk.write_text("const m=require(process.argv[2]).contextModel;const v=[0,1,-1,0.5,NaN,Infinity,'5',null,undefined,true,1e308];let bad=0,ok=0;"
                   "for(const t of v)for(const h of v)for(const s of v){const r=m({state:'ok',tokens:t,hard:h,soft:s});"
                   "if(r.state==='ok'){ok++;if(!(Number.isFinite(r.fill)&&(r.tick===null||Number.isFinite(r.tick))))bad++;}}"
                   "console.log(JSON.stringify({ok,bad}))\n")
    got = json.loads(subprocess.run([node, str(chk), str(js)], capture_output=True, text=True, timeout=30, check=True).stdout)
    assert got["bad"] == 0 and got["ok"] > 0, got
    c = DeskCommands.from_env(json.dumps({"post": ["/p"], "lease": ["/l"], "events": ["/e"], "words_max": 50, "context": ["/c"]}))
    assert (c.post, c.lease, c.events, c.words_max, c.context) == (("/p",), ("/l",), ("/e",), 50, ("/c",))
    assert DeskCommands.from_env(c.to_json()) == c


def test_the_view_draws_the_bar_by_style_and_text_only():
    src = (Path(__file__).resolve().parents[1] / "levain" / "templates" / "web" / "desk_tab_view.js").read_text()
    seg = src[src.index("function contextModel"):src.index("function eventModel")]
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "createContextualFragment"):
        assert banned not in seg, banned


# --- the L3 round-2 fixes -----------------------------------------------------------------------------------------------------------

def test_a_genuine_type_error_in_the_port_is_unreadable_and_the_context_is_asked_once():
    calls = []

    class Boom(ScriptedDeskPort):
        def context(self, *, block=True):
            calls.append(block)
            raise TypeError("internal")
    b = Boom()
    b.script_lease("held", "desk 12", 5)
    b.script_context(None)
    assert desk_tab.serve_desk(b)[1]["context"] == {"state": "unreadable"} and calls == [True], calls


def test_a_failed_tail_read_is_not_kept_by_its_key_and_the_next_run_reads_again(tmp_path, monkeypatch):
    t = transcript(tmp_path, [rec(5, 212_000, 400)], "desk.jsonl")
    port = _port(tmp_path, json.dumps({"transcript": str(t)}))
    real, calls = desk_context.read_context, []

    def flaky(*a, **k):
        calls.append(1)
        return None if len(calls) == 1 else real(*a, **k)
    monkeypatch.setattr(desk_context, "read_context", flaky)
    assert port._read_context_now() is None
    got = port._read_context_now()
    assert isinstance(got, ContextView) and got.tokens == 212_405 and len(calls) == 2, (got, calls)


def test_a_file_that_changes_while_it_is_read_is_not_kept_by_its_key(tmp_path, monkeypatch):
    t = transcript(tmp_path, [rec(5, 100_000, 0)], "desk.jsonl")
    port = _port(tmp_path, json.dumps({"transcript": str(t)}))
    real, calls = desk_context.read_context, []

    def moving(*a, **k):
        calls.append(1)
        got = real(*a, **k)
        if len(calls) == 1:                 # the desk files a turn between the read and the end of it
            with open(t, "a") as f:
                f.write(rec(5, 300_000, 0) + "\n")
        return got
    monkeypatch.setattr(desk_context, "read_context", moving)
    assert port._read_context_now().tokens == 100_005
    assert port._read_context_now().tokens == 300_005 and len(calls) == 2
    port._read_context_now()
    assert len(calls) == 2                  # a file that stood still is kept again


def test_a_blocking_first_ask_and_a_waiting_refresh_run_the_command_once(tmp_path):
    import threading
    import time
    port, runs = _slow_port(tmp_path, 0.6)
    first = threading.Thread(target=lambda: port.context(block=True))
    first.start()
    time.sleep(0.2)
    port.context(block=False)               # a receipt poll mid-run: it starts the refresh, which waits behind the run
    first.join()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and port._ctx_busy:
        time.sleep(0.05)
    assert len(runs.read_text()) == 1, runs.read_text()


def test_a_thread_that_cannot_start_leaves_no_refresh_wedged(tmp_path, monkeypatch):
    import threading
    port, runs = _slow_port(tmp_path, 0.0)

    def refuse(self):
        raise RuntimeError("can't start new thread")
    real_start = threading.Thread.start
    monkeypatch.setattr(threading.Thread, "start", refuse)
    assert port.context(block=False) == NoReading("not read yet") and port._ctx_busy is False
    monkeypatch.setattr(threading.Thread, "start", real_start)
    port.context(block=False)               # the next poll starts the refresh
    import time
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and not (port._ctx_held and not port._ctx_busy):
        time.sleep(0.05)
    assert port.context(block=False).tokens == 212_405


@needs_browser
def test_a_context_not_read_yet_says_it_is_being_read_not_that_there_was_no_turn(tmp_path):
    with desk_page(tmp_path) as (stub, base):
        out = _drive(tmp_path, base + "/", [PHONE, *_TOKENED, _TAP_DESK, _OPEN,
            put(get={"status": 200, "json": wired(context={"state": "none", "why": "not read yet", "holder": None})}), until("document.querySelector('.dv-ctx-pending')"), CTX,
            *_shot("desk_ctx_pending_390.png")])
    g = ctxs(out)[0]
    assert g["cls"] == "dv-ctx dv-ctx-pending" and g["words"] == "Reading the desk's context." and g["fill"] is None and not g["pageX"], g


def _idle_then_changed(tmp_path):
    """A port that read a desk once, then sat idle past CONTEXT_STALE while the transcript became another desk's (a smaller number)."""
    port, runs = _slow_port(tmp_path, 0.0)
    assert port.context().tokens == 212_405
    t = tmp_path / "desk.jsonl"
    t.write_text(rec(2, 1_000, 10, model="claude-opus-5-5") + "\n")
    held = port._ctx_held
    port._ctx_held = (held[0] - 600.0, held[1])
    return port


def test_a_reading_left_idle_past_the_stale_bound_is_never_the_first_answer_for_a_possibly_new_desk(tmp_path):
    port = _idle_then_changed(tmp_path)
    assert port.context().tokens == 1_012          # read afresh in the asking call, not the old desk's 212_405


def test_a_receipt_poll_on_an_idle_stale_reading_says_not_read_yet_never_the_old_number(tmp_path):
    import time
    port = _idle_then_changed(tmp_path)
    assert port.context(block=False) == NoReading("not read yet")
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and port._ctx_busy:
        time.sleep(0.05)
    assert not port._ctx_busy, "the background context refresh did not finish"
    assert port.context(block=False).tokens == 1_012


def test_the_stale_first_read_is_what_the_bound_removes(tmp_path, monkeypatch):
    monkeypatch.setattr("levain.desk.desk_port.CONTEXT_STALE", 1e9)       # the old behaviour: any aged reading is answered as it is
    port = _idle_then_changed(tmp_path)
    assert port.context().tokens == 212_405
