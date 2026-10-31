"""levain.dream.executor (D1-1a): the store-path core, the hooks, the progress.json schema and the compose minds.

Every test runs the REAL ``consolidate_store`` against a tiny scratch anneal store created under ``tmp_path`` (never
``~/.anneal-memory``). The ``claude`` mind runs against a stub executable that prints a canned result json; the
``ollama`` mind against a fake client. No model, no network. Each case was first run by hand against a scratch store
(/tmp/d11a/hand1.py in the lane's tmp): the refusal texts below are what anneal 1.0.2 printed there.
"""
from __future__ import annotations

import json
import os
import stat
import textwrap
import time
from pathlib import Path

import pytest
from anneal_memory import FLOW_SCHEMA, Store
from anneal_memory.schema import hard_max_chars

from levain import wrap as wrapmod
from levain.dream import executor as ex

_VALID = """\
## State
First consolidation of the test store.

## Active Threads
- Getting my bearings — pointer: this session.

## Patterns
Nothing graduated yet.

## Decisions
Nothing committed yet.

## Context
The first dream metabolized the opening episodes.

## Understanding
Early days.
"""

PINNED = "test-model-1"
# A TEST row: invented numbers so the arithmetic is checkable. Not a price. M = 200000×20 + 40000×25 = $5.00.
TEST_ROW = ex.PriceRow(context_window=200_000, max_output=40_000, input_micros_per_mtok=20_000_000,
                       output_micros_per_mtok=25_000_000, source="test fixture", read_on="2026-10-31")
M = 5_000_000


@pytest.fixture
def scratch(tmp_path):
    """A tiny anneal store (FLOW schema, 3 episodes) in ``<tmp>/store``."""
    d = tmp_path / "store"
    d.mkdir()
    with Store(str(d / "memory.db"), section_schema=FLOW_SCHEMA) as s:
        for i in range(3):
            s.record(content=f"Turn {i}: helped the operator.", episode_type="observation", source="t")
    return d / "memory.db"


STUB = textwrap.dedent('''\
    #!/bin/sh
    D="$STUB_DIR"
    n=$(cat "$D/n" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$D/n"
    printf '%s\\n' "$@" > "$D/argv-$n"
    cat > "$D/stdin-$n"
    occ=""; prev=""
    for a in "$@"; do [ "$prev" = "--add-dir" ] && occ="$a"; prev="$a"; done
    cp "$occ/progress.json" "$D/seen-$n.progress"
    [ -f "$D/sleep-$n" ] && sleep "$(cat "$D/sleep-$n")"
    cat "$D/result-$n.json" 2>/dev/null
    ''')


class Stub:
    """A stub ``claude``: prints ``result-<n>.json`` for call n, records argv, stdin and the progress.json it saw."""

    def __init__(self, root: Path, monkeypatch) -> None:
        self.dir = root / "stub"
        self.dir.mkdir()
        self.bin = root / "claude"
        self.bin.write_text(STUB)
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("STUB_DIR", str(self.dir))

    def result(self, n: int, text: str = _VALID, cost: float | None = 1.5, subtype: str = "success",
               models=(PINNED,)) -> None:
        doc = {"type": "result", "subtype": subtype, "is_error": subtype != "success", "result": text,
               "usage": {"input_tokens": 12, "output_tokens": 30, "cache_read_input_tokens": 5,
                         "cache_creation_input_tokens": 7},
               "modelUsage": {m: {"costUSD": cost} for m in models}}
        if cost is not None:
            doc["total_cost_usd"] = cost
        (self.dir / f"result-{n}.json").write_text(json.dumps(doc))

    def calls(self) -> int:
        f = self.dir / "n"
        return int(f.read_text()) if f.exists() else 0

    def argv(self, n: int) -> list[str]:
        return (self.dir / f"argv-{n}").read_text().split("\n")[:-1]

    def cap(self, n: int) -> str:
        a = self.argv(n)
        return a[a.index("--max-budget-usd") + 1]

    def stdin(self, n: int) -> str:
        return (self.dir / f"stdin-{n}").read_text()

    def seen_progress(self, n: int) -> dict:
        return json.loads((self.dir / f"seen-{n}.progress").read_text())


@pytest.fixture
def stub(tmp_path, monkeypatch):
    return Stub(tmp_path, monkeypatch)


def _run(store: Path, tmp_path: Path, mind, **kw):
    kw.setdefault("occurrence_dir", tmp_path / "occ")
    kw.setdefault("max_seconds", 3000)
    return ex.consolidate_store(store, mind=mind, **kw)


def _claude(stub: Stub) -> ex.ClaudeMind:
    return ex.ClaudeMind(PINNED, claude_bin=str(stub.bin))


def _budget() -> ex.Budget:
    return ex.Budget.for_model(PINNED, {PINNED: TEST_ROW})


def _store_state(db: Path):
    with Store(str(db), section_schema=None) as s:
        return s.get_wrap_started_at(), s.last_wrap_id(), len(s.episodes_since_wrap())


# ---------- the progress.json schema and the hooks (§2.2), pinned ----------

def test_progress_schema_is_exactly_the_eight_keys(tmp_path):
    assert ex.PROGRESS_KEYS == ("wrap_token", "phase", "last_wrap_before", "composed_sha256", "wrap_id",
                                "child_pid", "child_started_at", "child_argv")
    p = ex.Progress(tmp_path)
    p.update()
    assert json.loads((tmp_path / "progress.json").read_text()) == dict.fromkeys(ex.PROGRESS_KEYS)
    p.update(phase="composing", wrap_token="t")
    got = json.loads((tmp_path / "progress.json").read_text())
    assert set(got) == set(ex.PROGRESS_KEYS) and got["phase"] == "composing" and got["child_pid"] is None
    assert not (tmp_path / "progress.json.tmp").exists()
    with pytest.raises(KeyError):
        p.update(child_pgid=1)
    with pytest.raises(ValueError):
        p.update(phase="cancelled")


def test_a_run_writes_the_phases_and_the_child_keys_while_the_child_lives(scratch, stub, tmp_path):
    stub.result(1)
    r = _run(scratch, tmp_path, _claude(stub), budget=_budget())
    assert r.outcome == "saved" and r.exit_code == 0 and r.wrap_id == 1
    seen = stub.seen_progress(1)                                  # progress.json AS THE CHILD SAW IT
    assert set(seen) == set(ex.PROGRESS_KEYS)
    assert seen["phase"] == "composing" and seen["wrap_token"] == r.wrap_token
    assert isinstance(seen["child_pid"], int) and seen["child_pid"] > 1
    assert isinstance(seen["child_started_at"], str) and seen["child_started_at"]
    occ = str(tmp_path / "occ")
    assert seen["child_argv"][0] == str(stub.bin) and "--add-dir" in seen["child_argv"]
    assert seen["child_argv"][seen["child_argv"].index("--add-dir") + 1] == occ
    final = json.loads((tmp_path / "occ" / "progress.json").read_text())
    assert final["phase"] == "saved" and final["wrap_id"] == 1 and final["last_wrap_before"] == 0
    assert final["composed_sha256"] and final["child_pid"] is None       # cleared once the child was reaped
    assert (tmp_path / "occ" / "composed.continuity.md").read_text() == _VALID.strip()


def test_the_hooks_are_called_with_their_signature_and_default_to_nothing(scratch, stub, tmp_path):
    stub.result(1)
    seen = {}

    def on_snapshot(store, occ_dir):
        seen["snap"] = (type(store).__name__, occ_dir, store.get_wrap_started_at())
        return {"files": []}

    def on_saved(store, occ_dir, wrap_id):
        seen["saved"] = (occ_dir, wrap_id)
        return ["post"]

    r = _run(scratch, tmp_path, _claude(stub), budget=_budget(), on_snapshot=on_snapshot, on_saved=on_saved)
    assert seen["snap"] == ("Store", tmp_path / "occ", None)      # before prepare: no wrap open yet
    assert seen["saved"] == (tmp_path / "occ", 1)
    assert r.manifest == {"files": []} and r.post_files == ["post"] and r.outcome == "saved"


def test_a_refused_snapshot_returns_before_prepare_and_writes_nothing(scratch, stub, tmp_path):
    def refuse(store, occ_dir):
        raise RuntimeError("format_version 3")

    r = _run(scratch, tmp_path, _claude(stub), budget=_budget(), on_snapshot=refuse)
    assert (r.outcome, r.exit_code) == ("snapshot-refused", 2) and "format_version 3" in r.detail
    assert stub.calls() == 0
    assert _store_state(scratch) == (None, 0, 3)                  # no wrap opened, nothing consolidated


def test_a_failing_on_saved_is_recorded_not_raised(scratch, stub, tmp_path):
    stub.result(1)

    def boom(store, occ_dir, wrap_id):
        raise OSError("disk full")

    r = _run(scratch, tmp_path, _claude(stub), budget=_budget(), on_saved=boom)
    assert r.outcome == "saved" and r.hook_errors == ["on_saved: OSError: disk full"]


# ---------- the re-compose loop: one refusal class per compose, the check on a copy (10-21, 10-23) ----------

def test_one_refusal_class_per_compose_then_a_save_with_every_refusal_reprompted_verbatim(scratch, stub, tmp_path):
    no_decisions = _VALID.replace("## Decisions\nNothing committed yet.\n\n", "")
    over_budget = _VALID + "filler line.\n" * 4000                # 10-14: *"saved 30,403 chars against 'Stay within 25500'"*
    assert len(over_budget) > hard_max_chars(FLOW_SCHEMA)
    stub.result(1, no_decisions)
    stub.result(2, over_budget)
    stub.result(3, _VALID)
    live_before = scratch.read_bytes()
    r = _run(scratch, tmp_path, _claude(stub), budget=_budget())
    assert r.outcome == "saved" and len(r.composes) == 3 and stub.calls() == 3
    assert r.refusals[0].startswith("ValueError: Continuity must contain all sections")
    assert "hard maximum of 31875" in r.refusals[1] and "target 25500" in r.refusals[1]
    # compose 2 saw refusal 1, compose 3 saw BOTH, verbatim, and a pointer to the last draft
    assert r.refusals[0] in stub.stdin(2) and r.refusals[0] not in stub.stdin(1)
    assert r.refusals[0] in stub.stdin(3) and r.refusals[1] in stub.stdin(3)
    assert "draft-2.md" in stub.stdin(3)
    # the checks wrote nothing: the live store was untouched until the real save
    assert _store_state(scratch)[:2] == (None, 1)
    assert scratch.read_bytes() != live_before                    # the save did write it


def test_the_check_runs_on_a_copy_and_writes_nothing_to_the_live_store(scratch, tmp_path):
    from anneal_memory.continuity import prepare_wrap

    with Store(str(scratch), section_schema=None) as s:
        prepare_wrap(s, wrap_token="c" * 32)
    before = (scratch.with_name("memory.continuity.md").exists(), _store_state(scratch))
    assert ex.check_on_copy(scratch, _VALID, wrap_token="c" * 32, session_id=None, project_name="x") is None
    refusal = ex.check_on_copy(scratch, "## State\nonly", wrap_token="c" * 32, session_id=None, project_name="x")
    assert refusal and refusal.startswith("ValueError: Continuity must contain all sections")
    assert (scratch.with_name("memory.continuity.md").exists(), _store_state(scratch)) == before
    assert before[1][0] is not None                               # the wrap is still open, ours, untouched


def test_a_refused_final_compose_is_the_real_saves_refusal_and_fn_returns_it(scratch, stub, tmp_path):
    for n in (1, 2, 3):
        stub.result(n, "## State\nnot a memory")
    r = _run(scratch, tmp_path, _claude(stub), budget=_budget(), max_composes=3)
    assert (r.outcome, r.exit_code) == ("refused", 1)
    assert len(r.refusals) >= 3 and stub.calls() == 3
    assert _store_state(scratch) == (None, 0, 3)                  # our wrap cancelled, episodes still waiting


def test_fn_never_raises_on_a_refused_save(scratch, tmp_path):
    class Bad:
        name, source, unit, model = "x", "x", "tokens", "x"

        def compose(self, req):
            return ex.ComposeResult(text="## State\nbroken")

    r = ex.consolidate_store(scratch, mind=Bad(), max_composes=1, occurrence_dir=tmp_path / "occ")
    assert (r.outcome, r.exit_code) == ("refused", 1) and r.wrap_token
    assert (scratch.parent / f"rejected-wrap.{r.wrap_token}.md").read_text() == "## State\nbroken"
    assert _store_state(scratch)[0] is None
    assert json.loads((tmp_path / "occ" / "usage.json").read_text())["outcome"] == "refused"


def test_a_mind_that_raises_is_a_typed_outcome_not_an_exception(scratch, tmp_path, capsys):
    class Raises:
        name, source, unit, model = "x", "x", "tokens", "x"

        def compose(self, req):
            raise RuntimeError("endpoint down")

    r = ex.consolidate_store(scratch, mind=Raises(), occurrence_dir=tmp_path / "occ")
    assert (r.outcome, r.exit_code) == ("compose-failed", 1) and "endpoint down" in r.detail
    assert _store_state(scratch)[0] is None

    class Gone(Raises):
        def compose(self, req):
            raise ex.ComposeUnavailable("no extra")

    r = ex.consolidate_store(scratch, mind=Gone())
    assert (r.outcome, r.exit_code) == ("compose-unavailable", 2)


def test_the_other_classified_outcomes(scratch, tmp_path):
    class Never:
        name, source, unit, model = "x", "x", "tokens", "x"

        def compose(self, req):
            raise AssertionError("must not compose")

    fd = wrapmod._lock_wrap_file(scratch.parent / "wrap.lock")
    try:
        r = ex.consolidate_store(scratch, mind=Never())
        assert (r.outcome, r.exit_code) == ("lock-held", 2)
    finally:
        wrapmod._unlock_wrap(fd)
    r = ex.consolidate_store(scratch, mind=Never(), dry_run=True)
    assert (r.outcome, r.exit_code) == ("dry-run", 0) and _store_state(scratch)[0] is None
    empty = scratch.parent / "empty"
    empty.mkdir()
    with Store(str(empty / "memory.db"), section_schema=FLOW_SCHEMA):
        pass
    r = ex.consolidate_store(empty / "memory.db", mind=Never())
    assert (r.outcome, r.exit_code) == ("empty", 0)
    r = ex.consolidate_store(scratch.parent / "nope" / "memory.db", mind=Never())
    assert (r.outcome, r.exit_code) == ("precondition", 2)


def test_the_deadline_returns_deadline_after_cancelling_our_wrap(scratch, tmp_path):
    class Stalls:
        name, source, unit, model = "x", "x", "tokens", "x"

        def compose(self, req):
            time.sleep(10)

    t0 = time.monotonic()
    r = ex.consolidate_store(scratch, mind=Stalls(), max_seconds=1, save_reserve=0.0, occurrence_dir=tmp_path / "occ")
    assert (r.outcome, r.exit_code) == ("deadline", 5) and time.monotonic() - t0 < 8
    assert _store_state(scratch)[0] is None


def test_no_compose_starts_without_the_save_reserve(scratch, stub, tmp_path):
    r = _run(scratch, tmp_path, _claude(stub), budget=_budget(), max_seconds=500, save_reserve=960)
    assert r.outcome == "deadline" and stub.calls() == 0
    assert _store_state(scratch)[0] is None


# ---------- §2.4: the start rule, M, the price table ----------

def test_the_one_call_ceiling_is_w_times_p_in_plus_o_times_p_out_rounded_up():
    assert ex.one_call_ceiling(PINNED, {PINNED: TEST_ROW}) == M
    odd = ex.PriceRow(1, 1, 1, 1, "t", "d")                       # 2 micro-dollars-per-Mtok-tokens: rounds UP to 1
    assert ex.one_call_ceiling("o", {"o": odd}) == 1
    assert ex.one_call_ceiling("unpriced", {PINNED: TEST_ROW}) is None


def test_the_shipped_price_table_invents_no_price():
    assert ex.PRICE_TABLE == {}                                   # rows are added by whoever reads the price page
    assert ex.Budget.for_model(ex.DEFAULT_CLAUDE_MODEL).m_usd_micros is None


def test_start_rule_is_r_minus_m_minus_spent_and_floors_at_the_cheapest_compose():
    b = ex.Budget(m_usd_micros=M)
    assert b.next_cap(0) == 15_000_000                            # R $20 − M $5
    assert b.next_cap(4_000_000) == 11_000_000
    assert b.next_cap(15_000_000 - ex.MIN_COMPOSE_USD_MICROS) == ex.MIN_COMPOSE_USD_MICROS
    assert b.next_cap(15_000_000 - ex.MIN_COMPOSE_USD_MICROS + 1) is None   # below $1.62: no compose starts
    assert ex.Budget(m_usd_micros=None).next_cap(0) is None       # no price row: nothing starts
    assert ex.Budget(m_usd_micros=19_000_000).next_cap(0) is None  # R − M below $1.62


def test_an_unreadable_cost_counts_as_the_cap_plus_m_and_a_readable_one_as_itself():
    assert ex.charged_after(15_000_000, M, None) == 20_000_000
    assert ex.charged_after(15_000_000, M, 5_150_000) == 5_150_000


def test_the_caps_follow_the_start_rule_across_composes(scratch, stub, tmp_path):
    bad = "## State\nx"
    for n in (1, 2, 3):
        stub.result(n, bad, cost=4.0)
    r = _run(scratch, tmp_path, _claude(stub), budget=_budget())
    assert [stub.cap(n) for n in (1, 2, 3)] == ["15.000000", "11.000000", "7.000000"]
    assert r.spent_usd_micros == 12_000_000
    use = json.loads((tmp_path / "occ" / "usage.json").read_text())
    assert use["actual_usd_micros"] == 12_000_000 and use["reserved_usd_micros"] == 20_000_000 and use["readable"]
    assert use["unit"] == "usd_list" and use["source"] == "dream:claude-p"


def test_a_one_turn_overshoot_closes_the_run_before_a_second_compose(scratch, stub, tmp_path):
    """R5's $5.15 single-turn compose, and R3: a compose may end up to M over its cap. Cap $15, charged $17.5:
    R − M − spent is negative, so the effect returns budget with no second compose, and the total stays under R."""
    stub.result(1, "## State\nx", cost=17.5)
    r = _run(scratch, tmp_path, _claude(stub), budget=_budget())
    assert r.outcome == "budget" and stub.calls() == 1 and r.spent_usd_micros == 17_500_000 <= 20_000_000
    assert _store_state(scratch) == (None, 0, 3)


def test_an_unreadable_cost_stops_the_run_and_usage_says_unreadable(scratch, stub, tmp_path):
    stub.result(1, "## State\nx", cost=None)                      # json without total_cost_usd
    r = _run(scratch, tmp_path, _claude(stub), budget=_budget())
    assert r.outcome == "budget" and stub.calls() == 1
    assert r.spent_usd_micros == 20_000_000                       # cap $15 + M $5
    use = json.loads((tmp_path / "occ" / "usage.json").read_text())
    assert use["readable"] is False and use["actual_usd_micros"] is None


def test_a_model_id_with_no_price_row_is_refused_and_no_child_starts(scratch, stub, tmp_path):
    b = ex.Budget.for_model("unpriced-model", {PINNED: TEST_ROW})
    r = _run(scratch, tmp_path, ex.ClaudeMind("unpriced-model", claude_bin=str(stub.bin)), budget=b)
    assert r.outcome == "budget" and stub.calls() == 0 and _store_state(scratch)[0] is None
    # and the mind itself refuses without a cap, whoever calls it
    with pytest.raises(ex.ComposeUnavailable, match="no price row"):
        _claude(stub).compose(ex.ComposeRequest("p", "i", occ_dir=tmp_path / "occ"))


def test_a_second_charging_model_is_recorded_as_a_first_run_fact(scratch, stub, tmp_path):
    stub.result(1, cost=1.0, models=(PINNED, "test-helper-model"))
    r = _run(scratch, tmp_path, _claude(stub), budget=_budget())
    row = r.composes[0]
    assert row["models_charged"] == ["test-helper-model", PINNED] and "test-helper-model" in row["warnings"][0]


def test_a_budget_error_result_is_budget_not_a_crash(scratch, stub, tmp_path):
    stub.result(1, "", cost=15.2, subtype="error_max_budget_usd")
    r = _run(scratch, tmp_path, _claude(stub), budget=_budget(), max_composes=1)
    assert r.outcome == "budget" and _store_state(scratch)[0] is None


# ---------- the claude child: argv, an unreaped group leader, ended through its handle ----------

def test_the_claude_argv_is_flows_with_the_cap_and_no_session_persistence(scratch, stub, tmp_path):
    stub.result(1)
    _run(scratch, tmp_path, _claude(stub), budget=_budget())
    a = stub.argv(1)
    assert a[:1] == ["-p"]
    for flag, val in (("--tools", "Read,Glob,Grep"), ("--allowedTools", "Read,Glob,Grep"),
                      ("--permission-mode", "dontAsk"), ("--output-format", "json"), ("--model", PINNED),
                      ("--add-dir", str(tmp_path / "occ")), ("--max-budget-usd", "15.000000")):
        assert a[a.index(flag) + 1] == val
    assert {"--restricted", "--strict-mcp-config", "--no-session-persistence"} <= set(a)
    assert (tmp_path / "occ" / "package.md").read_text().strip()   # the package is a file the child Reads


def test_the_child_leads_its_own_group_and_is_gone_and_reaped_after_the_compose(scratch, stub, tmp_path):
    stub.result(1)
    _run(scratch, tmp_path, _claude(stub), budget=_budget())
    pid = stub.seen_progress(1)["child_pid"]
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)                                          # group gone
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)                                            # leader reaped


def test_a_stalled_child_is_ended_through_the_handle_at_the_timeout(tmp_path, stub):
    (stub.dir / "sleep-1").write_text("30")
    stub.result(1)
    occ = tmp_path / "occ"
    occ.mkdir()
    prog = ex.Progress(occ)
    t0 = time.monotonic()
    res = _claude(stub).compose(ex.ComposeRequest("pkg", "ins", occ_dir=occ, cap_usd_micros=1_000_000, timeout_s=1.5,
                                                  progress=prog))
    assert res.error == "timeout" and res.usd_micros is None and time.monotonic() - t0 < 20
    pid = stub.seen_progress(1)["child_pid"]
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)
    assert prog.data["child_pid"] is None


def test_an_unparseable_child_output_is_an_error_with_an_unreadable_cost(tmp_path):
    res = ex.ClaudeMind(PINNED).parse(b"not json", 1, 0.1)
    assert res.error.startswith("child-exit-1") and res.usd_micros is None and res.text == ""


def test_usd_to_micros_reads_only_a_sane_number():
    assert ex.usd_to_micros(3.11) == 3_110_000 and ex.usd_to_micros(0) == 0
    for bad in (None, "3", -1, float("nan"), float("inf"), True):
        assert ex.usd_to_micros(bad) is None


# ---------- the ollama mind: tokens reported, dollars unmapped (R1) ----------

class _Resp:
    def __init__(self, text, usage):
        class _M:
            content = text
        self.message = _M()
        if usage is not None:
            self.usage = usage


def test_ollama_returns_response_usage_and_never_reports_dollars_as_zero(scratch, tmp_path):
    seen = {}

    def client(system, user):
        seen["system"], seen["user"] = system, user
        return _Resp(_VALID, {"prompt_tokens": 1234, "completion_tokens": 567})

    mind = ex.OllamaMind("glm-test:cloud", client=client)
    r = ex.consolidate_store(scratch, mind=mind, occurrence_dir=tmp_path / "occ", constitution="I AM THE ENTITY")
    assert r.outcome == "saved" and r.spent_usd_micros == 0       # nothing was charged AGAINST a dollar budget
    assert seen["system"].startswith("I AM THE ENTITY")
    row = r.composes[0]
    assert (row["input_tokens"], row["output_tokens"], row["usd_micros"]) == (1234, 567, None)
    assert "dollars unmapped" in row["warnings"][0]
    use = json.loads((tmp_path / "occ" / "usage.json").read_text())
    assert use["unit"] == "tokens" and use["usd_mapped"] is False and use["actual_usd_micros"] is None
    assert (use["input_tokens"], use["output_tokens"], use["source"]) == (1234, 567, "dream:ollama")
    assert use["reserved_usd_micros"] is None and use["readable"] is True


def test_ollama_with_no_usage_says_unreadable_not_zero(scratch, tmp_path):
    mind = ex.OllamaMind("glm-test:cloud", client=lambda s, u: _Resp(_VALID, None))
    r = ex.consolidate_store(scratch, mind=mind, occurrence_dir=tmp_path / "occ")
    use = json.loads((tmp_path / "occ" / "usage.json").read_text())
    assert r.outcome == "saved" and use["readable"] is False and use["input_tokens"] is None


def test_response_usage_reads_usage_then_raw_response_then_metrics():
    class Raw:
        usage = {"prompt_tokens": 3, "completion_tokens": 4}

    class R2:
        raw_response = Raw()

    class Acc:
        prompt_tokens, completion_tokens = 5, 6

    class Met:
        accumulated_token_usage = Acc()

    class R3:
        metrics = Met()

    assert ex._response_usage(_Resp("", {"prompt_tokens": 1, "completion_tokens": 2})) == (1, 2)
    assert ex._response_usage(R2()) == (3, 4) and ex._response_usage(R3()) == (5, 6)
    assert ex._response_usage(object()) == (None, None)


# ---------- levain wrap still goes through the core ----------

def test_wrap_entity_calls_the_core_and_keeps_its_exit_codes(tmp_path, monkeypatch):
    calls = []
    real = ex.consolidate_store

    def spy(*a, **k):
        calls.append(k)
        return real(*a, **k)

    monkeypatch.setattr(ex, "consolidate_store", spy)
    ent = tmp_path / "ent"
    (ent / ".levain").mkdir(parents=True)
    (ent / ".levain" / "config.json").write_text(json.dumps({"adapter": "openhands"}))
    with Store(str(ent / ".levain" / "memory.db"), section_schema=FLOW_SCHEMA) as s:
        s.record(content="Turn: a thing happened.", episode_type="observation", source="t")
    monkeypatch.setattr(wrapmod, "_compose", lambda *a, **k: _VALID)
    assert wrapmod.wrap_entity(ent) == 0
    assert len(calls) == 1 and calls[0]["max_composes"] == 1 and calls[0]["own_deadline"] is False
    assert calls[0]["label"] == "levain wrap"


# ---------- levain dream run ----------

def test_dream_run_on_a_store_copy_exits_0_and_writes_usage(scratch, stub, tmp_path, monkeypatch, capsys):
    from levain import cli

    monkeypatch.setitem(ex.PRICE_TABLE, PINNED, TEST_ROW)
    stub.result(1)
    occ = tmp_path / "occ-cli"
    rc = cli.main(["dream", "run", "--store-dir", str(scratch.parent), "--mind", "claude", "--model", PINNED,
                   "--claude-bin", str(stub.bin), "--occurrence-dir", str(occ), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out.strip().splitlines()[-1])["outcome"] == "saved"
    use = json.loads((occ / "usage.json").read_text())
    assert use["actual_usd_micros"] == 1_500_000 and set(json.loads((occ / "progress.json").read_text())) == set(ex.PROGRESS_KEYS)


def test_dream_run_ollama_route_with_a_fake_client(scratch, tmp_path, monkeypatch, capsys):
    from levain import cli

    monkeypatch.setattr(ex.OllamaMind, "_default_client",
                        lambda self, s, u: _Resp(_VALID, {"prompt_tokens": 9, "completion_tokens": 8}))
    occ = tmp_path / "occ-o"
    assert cli.main(["dream", "run", "--store-dir", str(scratch.parent), "--mind", "ollama",
                     "--occurrence-dir", str(occ)]) == 0
    use = json.loads((occ / "usage.json").read_text())
    assert (use["input_tokens"], use["output_tokens"], use["usd_mapped"]) == (9, 8, False)


def test_dream_run_refuses_the_live_store_dir_and_a_model_with_no_row(scratch, tmp_path, monkeypatch, capsys):
    from levain import cli

    fake_home = tmp_path / "home"
    live = fake_home / ".anneal-memory"
    live.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    assert cli.main(["dream", "run", "--store-dir", str(live), "--mind", "ollama"]) == 2
    assert "COPY" in capsys.readouterr().err
    assert cli.main(["dream", "run", "--store-dir", str(scratch.parent), "--mind", "claude"]) == 2
    assert "no price row" in capsys.readouterr().err
    assert _store_state(scratch) == (None, 0, 3)
    assert cli.main(["dream"]) == 1                                # the bare command is still "not built"
    assert "not built" in capsys.readouterr().err
