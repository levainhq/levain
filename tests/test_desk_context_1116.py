"""levain#79: the Desk view shows the live desk's context usage against its rotation lines (SOFT / HARD).

The tokens come from the desk session's transcript usage block, read from the transcript's tail only (levain.desk.desk_context); the
reading is held equal to flow's scripts/seat_context.py for the same transcript where flow's checkout is present. The route carries
it (levain.cockpit.desk_tab), the page draws it (desk_tab_view.js, the lease bar). LEVAIN_DESK_SHOTS=<dir> saves the screenshots.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pwd
import shutil
import sys
from pathlib import Path

import pytest

from levain.cockpit import desk_tab
from levain.desk import desk_context
from levain.desk.desk_context import DEEP_HARD, HARD, MAX_TAIL, SOFT, ContextView, parse_command, read_context
from levain.desk.desk_port import DeskCommands, HostCommandDeskPort, ScriptedDeskPort
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_desk_pane_page import _OPEN, _TAP_DESK, _TOKENED, desk_page, put, until, wired

# the account's real home, not $HOME (the test rigs repoint HOME)
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
SEAT_CONTEXT = Path(os.environ.get("FLOW_SEAT_CONTEXT") or REAL_HOME / "Briefcase" / "flow" / "scripts" / "seat_context.py")
PHONE = {"emulate": {"w": 390, "h": 844, "dpr": 3, "mobile": True}}


def rec(inp=3, read=0, create=0, model="claude-opus-5", kind="assistant", ts="2026-11-16T14:00:00.000Z") -> str:
    return json.dumps({"type": kind, "timestamp": ts, "message": {"model": model, "role": kind, "content": [{"type": "text", "text": "x"}],
                                                                  "usage": {"input_tokens": inp, "cache_read_input_tokens": read,
                                                                            "cache_creation_input_tokens": create, "output_tokens": 7}}})


def transcript(tmp_path: Path, lines: list[str], name="s.jsonl") -> Path:
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n")
    return p


def flow_scan():
    if not SEAT_CONTEXT.is_file():
        pytest.skip("flow's scripts/seat_context.py is not on this machine")
    spec = importlib.util.spec_from_file_location("seat_context_ref", SEAT_CONTEXT)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SEAT_CONTEXT.parent))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(SEAT_CONTEXT.parent))
    return mod


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
    assert read_context(transcript(tmp_path, [rec(0, 0, 0), '{"type":"user"}'], "z.jsonl")) is None
    assert read_context(tmp_path / "missing.jsonl") is None
    assert read_context(transcript(tmp_path, [], "empty.jsonl")) is None


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


def test_the_desk_lines_are_flows_rotation_lines():
    sc = flow_scan()
    assert sc.LIMITS["desk"] == (SOFT, HARD) and sc.DEEP_HARD == DEEP_HARD and sc.limits_for("desk", None)[:2] == (SOFT, HARD)
    assert sc.limits_for("desk", {"granted": "x"})[:2] == (None, DEEP_HARD)


def test_the_reading_matches_seat_contexts_for_the_same_transcript(tmp_path):
    sc = flow_scan()
    p = transcript(tmp_path, [rec(5, 100_000, 2_000), '{"type":"user"}', rec(0, 0, 0), rec(3, 212_000, 400, model="claude-opus-5-5"), "garbage", '{"type":"user"}'])
    ctx, _samples, model = sc.scan(str(p))
    got = read_context(p)
    assert got.tokens == ctx == 212_403 and got.model == model


def test_the_reading_matches_seat_contexts_on_a_real_session_transcript(tmp_path):
    sc = flow_scan()
    root = REAL_HOME / ".claude" / "projects"
    live = sorted((x for x in root.glob("*/*.jsonl") if 50_000 < x.stat().st_size < 60_000_000), key=lambda x: x.stat().st_mtime) if root.is_dir() else []
    if not live:
        pytest.skip("no real session transcript here")
    copy = tmp_path / "real.jsonl"
    shutil.copyfile(live[-1], copy)          # a copy: a live file grows between the two reads
    ctx, _samples, _model = sc.scan(str(copy))
    got = read_context(copy)
    assert (got.tokens if got else 0) == ctx, (got, ctx)


# --- the host's command and the port ----------------------------------------------------------------------------------------------

def test_the_command_names_a_jsonl_by_absolute_path_and_nothing_else_is_taken():
    assert parse_command('{"transcript": "/a/b/s.jsonl"}') == (Path("/a/b/s.jsonl"), False)
    assert parse_command('{"transcript": "/a/b/s.jsonl", "deep": true, "extra": 1}') == (Path("/a/b/s.jsonl"), True)
    assert parse_command('{"transcript": "/a/b/s.jsonl", "deep": "yes"}') == (Path("/a/b/s.jsonl"), False)
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
    assert status == 200 and body["context"] == {"state": "ok", "tokens": 212_405, "soft": SOFT, "hard": HARD, "model": "claude-opus-5-5"}, body


def test_an_unreadable_context_is_said_unreadable_never_zero(tmp_path):
    t = transcript(tmp_path, [json.dumps({"type": "user"})], "nousage.jsonl")
    for out, code in ((json.dumps({"transcript": str(t)}), 0), ("not json", 0), (json.dumps({"transcript": str(tmp_path / "gone.jsonl")}), 0),
                      (json.dumps({"transcript": str(t)}), 3)):
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


def test_the_view_draws_the_bar_by_style_and_text_only():
    src = (Path(__file__).resolve().parents[1] / "levain" / "templates" / "web" / "desk_tab_view.js").read_text()
    seg = src[src.index("function contextModel"):src.index("function eventModel")]
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "createContextualFragment"):
        assert banned not in seg, banned
