"""3b-8's journey as code: the expected-verdict table's own invariants, the runner against a fake guest (no VM, no ssh, no
vendor), and the raw-line formats the bound verdicts claim, checked against the product's own code."""
from __future__ import annotations

import hashlib
import inspect
import io
import re

import pytest

from levain.slot import adopt_view
from scripts.vm_harness import journey8 as J
from scripts.vm_harness import runbook as RB
from scripts.vm_harness.driver import GuestError
from scripts.vm_harness.host_profile import DARWIN, LINUX
from scripts.vm_harness.journey8 import Cell, Probe, Step, Verdict

SID = "s-" + "a" * 16
REPO = "/tmp/work"
OID = "1" * 40


class FakeGuest:
    """The guest as ``capture`` sees it: ``/bin/sh -c <script> sh <probe argv...>``. ``replies`` maps the probe's verb words
    (``"session adopt"``) to ``(output, rc)``; every call is kept."""

    def __init__(self, replies=None, on_call=None):
        self.calls = []
        self.on_call = on_call
        self.replies = replies if replies is not None else {
            "session adopt": (f"took head: refs/levain/{SID}/head -> {OID}", 0),
            "session discard": (f"discard {SID}: done (trash)", 0),
            "session restore": (f"restore {SID}: done (retained)", 0),
        }

    def __call__(self, argv, timeout=60):
        self.calls.append(list(argv))
        if self.on_call:
            self.on_call(argv)
        assert list(argv[:4]) == list(RB.CAPTURE_ARGV), argv
        probe = list(argv[4:])
        verb = " ".join(probe[probe.index("levain") + 1:][:2])
        out, rc = self.replies[verb]
        if isinstance(out, Exception):
            raise out
        return out + ("\n" if out else "") + f"{RB.RC_MARK} {rc}\n"


def run(tmp_path, guest=None, profile=DARWIN, **kw):
    ev = tmp_path / "ev"
    ev.mkdir(exist_ok=True)
    kw.setdefault("session", SID)
    kw.setdefault("repo", REPO)
    return J.run_journey(guest or FakeGuest(), profile, ev, **kw)


# ---- the table -----------------------------------------------------------------------------------------------------

def test_table_is_valid_and_has_every_step_for_both_oses():
    J.validate()
    assert [s.id for s in J.STEPS] == [f"J{i:02d}" for i in range(1, 20)]
    assert all(set(s.cells) == {"darwin", "linux"} for s in J.STEPS)


def test_no_verdict_is_an_or():
    for s in J.STEPS:
        for c in s.cells.values():
            if c.verdict is not None:
                assert "|" not in c.verdict.pattern, (s.id, c.verdict.pattern)
                assert " or " not in c.verdict.pattern.lower()


def test_alternation_in_a_verdict_is_refused():
    with pytest.raises(RB.RunbookError, match="no alternation"):
        RB.check_pattern("done|failed")
    bad = (Step("X1", "x", {o: Cell("vm", Verdict(0, "a|b"), Probe(("x",))) for o in J.OS_NAMES}),)
    with pytest.raises(RB.RunbookError):
        J.validate(bad)


@pytest.mark.parametrize("cell", [
    Cell("vm", Verdict(0, "x")),                       # a verdict without a probe
    Cell("vm", None, Probe(("x",)), "UNBOUND: why"),   # a probe without a verdict
    Cell("vm", note="no reason prefix"),               # unbound without saying what is missing
    Cell("record"),                                    # a record cell that cites nothing
    Cell("pending", Verdict(0, "x"), None, "later"),   # a cell that is not run carries no verdict
    Cell("nope", note="x"),
])
def test_validate_refuses_a_malformed_cell(cell):
    with pytest.raises(RB.RunbookError):
        J.validate((Step("X1", "x", {"darwin": cell, "linux": cell}),))


def test_validate_refuses_duplicate_ids_and_a_missing_os():
    ok = Cell("na", note="n/a")
    with pytest.raises(RB.RunbookError, match="duplicate"):
        J.validate((Step("X1", "a", {"darwin": ok, "linux": ok}), Step("X1", "b", {"darwin": ok, "linux": ok})))
    with pytest.raises(RB.RunbookError, match="one cell per OS"):
        J.validate((Step("X1", "a", {"darwin": ok}),))


def test_bound_steps_are_the_three_operator_verbs_on_both_oses():
    for os_name in J.OS_NAMES:
        bound = [s.id for s in J.STEPS if s.cells[os_name].verdict is not None]
        assert bound == ["J06", "J07", "J08"], (os_name, bound)


def test_every_unbound_cell_says_what_is_missing_and_sandbox_step_is_linux_only():
    unbound = [(s.id, o) for s in J.STEPS for o, c in s.cells.items() if c.state == "vm" and c.verdict is None]
    assert unbound, "the journey does have unbound steps today"
    assert J.STEPS[14].id == "J15" and J.STEPS[14].cells["darwin"].state == "na"
    assert J.STEPS[14].cells["linux"].state == "vm"


def test_print_table_needs_no_vm(capsys):
    assert J.main(["--print-table", "--profile", "linux"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "3b-8 journey: expected verdicts, os=linux"
    assert "J06 | adopt | vm | rc=0 /" in out and "J15 | sandbox-unavailable refuses | UNBOUND" in out
    assert out == J.expected_text("linux") + "\n"


# ---- the runner ----------------------------------------------------------------------------------------------------

def test_all_bound_pass_but_the_run_is_incomplete_because_steps_are_unbound(tmp_path):
    g = FakeGuest()
    res = run(tmp_path, g)
    by = {r.id: r.status for r in res.steps}
    assert (by["J06"], by["J07"], by["J08"]) == ("PASS", "PASS", "PASS")
    assert by["J05"] == "SKIP" and by["J02"] == "SKIP" and by["J03"] == "UNBOUND"
    assert res.status == "INCOMPLETE" and not res.ok
    text = res.result_path.read_text()
    assert text.splitlines()[0] == "RESULT: INCOMPLETE" and "PASS=3" in text
    assert len(g.calls) == 3                                       # only the three bound probes reached the guest


def test_a_table_with_every_step_bound_and_matching_passes(tmp_path):
    cells = {o: Cell("vm", Verdict(0, "ok-.*"), Probe(("session", "adopt", "{session}"))) for o in J.OS_NAMES}
    steps = (Step("A1", "one", cells),)
    g = FakeGuest({"session adopt": ("ok-1", 0)})
    res = run(tmp_path, g, steps=steps)
    assert res.status == "PASS" and res.ok


def test_expected_table_is_on_disk_before_the_first_guest_call_and_digest_matches(tmp_path):
    seen = {}

    def first(argv):
        seen.setdefault("expected", (tmp_path / "ev" / "EXPECTED.txt").read_text())
        seen.setdefault("result_yet", (tmp_path / "ev" / "RESULT.txt").exists())

    res = run(tmp_path, FakeGuest(on_call=first))
    body, _, tail = seen["expected"].rpartition("\nsha256: ")
    assert seen["result_yet"] is False
    assert tail.strip() == hashlib.sha256(body.encode()).hexdigest()
    assert body == J.expected_text("darwin")
    assert f"sha256 {tail.strip()}" in res.result_path.read_text()


def test_a_wrong_raw_line_fails(tmp_path):
    g = FakeGuest({"session adopt": ("took wip: r -> " + OID, 0), "session discard": (f"discard {SID}: done (trash)", 0),
                   "session restore": (f"restore {SID}: done (retained)", 0)})
    res = run(tmp_path, g)
    by = {r.id: r for r in res.steps}
    assert by["J06"].status == "FAIL" and "took wip" in by["J06"].raw and res.status == "FAIL"


def test_a_right_line_with_the_wrong_exit_code_fails(tmp_path):
    g = FakeGuest({"session adopt": (f"took head: r -> {OID}", 1), "session discard": (f"discard {SID}: done (x)", 0),
                   "session restore": (f"restore {SID}: done (x)", 0)})
    r = {x.id: x for x in run(tmp_path, g).steps}["J06"]
    assert r.status == "FAIL" and r.detail == "rc=1"


def test_a_refusal_line_is_not_the_verdict(tmp_path):
    g = FakeGuest({"session adopt": ("levain session adopt: refused repo-shallow: /tmp/work", 1),
                   "session discard": (f"discard {SID}: done (x)", 0), "session restore": (f"restore {SID}: done (x)", 0)})
    assert {x.id: x for x in run(tmp_path, g).steps}["J06"].status == "FAIL"


def test_discard_verdict_is_bound_to_the_session_given(tmp_path):
    other = "s-" + "b" * 16
    g = FakeGuest({"session adopt": (f"took head: r -> {OID}", 0), "session discard": (f"discard {other}: done (trash)", 0),
                   "session restore": (f"restore {SID}: done (retained)", 0)})
    assert {x.id: x for x in run(tmp_path, g).steps}["J07"].status == "FAIL"


def test_a_transport_error_is_a_failed_step_not_a_crash(tmp_path):
    g = FakeGuest({"session adopt": (GuestError(255, "ssh down"), 0), "session discard": (f"discard {SID}: done (x)", 0),
                   "session restore": (f"restore {SID}: done (x)", 0)})
    r = {x.id: x for x in run(tmp_path, g).steps}["J06"]
    assert r.status == "FAIL" and "transport" in r.detail


def test_missing_session_or_repo_refuses_before_any_guest_call(tmp_path):
    g = FakeGuest()
    ev = tmp_path / "ev"
    ev.mkdir()
    with pytest.raises(RB.RunbookError, match="--session"):
        J.run_journey(g, DARWIN, ev, repo=REPO)
    assert g.calls == [] and list(ev.iterdir()) == []
    with pytest.raises(RB.RunbookError, match="--repo"):
        J.run_journey(g, DARWIN, ev, session=SID)


def test_only_runs_the_named_steps_and_refuses_an_unknown_one(tmp_path):
    g = FakeGuest()
    res = run(tmp_path, g, only=["J07"])
    assert [r.id for r in res.steps] == ["J07"] and len(g.calls) == 1
    (tmp_path / "x").mkdir()
    with pytest.raises(RB.RunbookError, match="unknown step"):
        run(tmp_path / "x", only=["J99"])


def test_a_used_evidence_dir_is_refused(tmp_path):
    (tmp_path / "ev").mkdir()
    (tmp_path / "ev" / "old").write_text("x")
    with pytest.raises(RB.RunbookError, match="not empty"):
        J.run_journey(FakeGuest(), DARWIN, tmp_path / "ev", session=SID, repo=REPO)


def test_linux_run_marks_the_sandbox_step_unbound_and_darwin_na(tmp_path):
    (tmp_path / "l").mkdir()
    (tmp_path / "d").mkdir()
    lin = {r.id: r.status for r in run(tmp_path / "l", FakeGuest(), LINUX).steps}
    dar = {r.id: r.status for r in run(tmp_path / "d", FakeGuest(), DARWIN).steps}
    assert lin["J15"] == "UNBOUND" and dar["J15"] == "NA"


def test_probe_argv_is_one_levain_verb_under_the_profiles_python():
    argv = J.levain_argv(DARWIN, ("session", "adopt", SID))
    assert argv[:3] == ("/usr/bin/env", f"PYTHONPATH={J.LEVAIN_ROOT}", DARWIN.python)
    assert argv[3:] == ("-m", "levain", "session", "adopt", SID)
    assert J.levain_argv(LINUX, ("x",))[2] == LINUX.python


# ---- the runbook helpers -------------------------------------------------------------------------------------------

def test_capture_returns_lines_and_exit_code_and_demands_the_marker():
    g = lambda argv, timeout=60: f"a\nb\n{RB.RC_MARK} 7\n"        # noqa: E731
    assert RB.capture(g, ["x"]) == (["a", "b"], 7)
    with pytest.raises(RB.RunbookError):
        RB.capture(lambda argv, timeout=60: "no marker\n", ["x"])


def test_pick_line_and_judge():
    assert RB.pick_line(["a", "", "b", ""], None) == "b"
    assert RB.pick_line(["noise", "took x"], r"^took ") == "took x"
    assert RB.pick_line(["noise"], r"^took ") is None
    assert RB.judge([], 0, Verdict(0, "x")) == (False, "")
    assert RB.judge(["xy"], 0, Verdict(0, "x")) == (False, "xy")        # fullmatch, never a prefix
    assert RB.judge(["x"], 9, Verdict(None, "x")) == (True, "x")


def test_overall_ranks_fail_over_incomplete_over_pass_and_nothing_run_is_not_a_pass():
    assert RB.overall(["PASS", "FAIL", "UNBOUND"]) == "FAIL"
    assert RB.overall(["PASS", "UNBOUND"]) == "INCOMPLETE"
    assert RB.overall(["SKIP", "NA"]) == "INCOMPLETE"
    assert RB.overall(["PASS", "SKIP"]) == "PASS"


def test_unfilled_placeholder_is_an_error_not_a_literal():
    with pytest.raises(RB.RunbookError, match="--session"):
        RB.render_placeholders(["{session}"])
    assert RB.render_placeholders(["a", "{x}"], x="1") == ("a", "1")


# ---- the raw-line formats the bound verdicts claim, against the product's own code ---------------------------------

@pytest.mark.parametrize("verb,verdict", [("discard", J.V_DISCARD), ("restore", J.V_RESTORE)])
def test_store_verb_prints_the_line_the_verdict_expects(monkeypatch, verb, verdict):
    monkeypatch.setattr(adopt_view, "call_helper", lambda seams, v, session, **kw: {"state": "trash"})
    out = io.StringIO()
    assert adopt_view.store_verb(verb, SID, object(), out, io.StringIO()) == 0
    line = out.getvalue().rstrip("\n")
    assert re.fullmatch(J.verdict_for(Cell("vm", verdict), SID).pattern, line), line


def test_adopt_prints_took_in_the_shape_the_verdict_expects():
    src = inspect.getsource(adopt_view.adopt)
    assert 'f"took {which}: {t.ref} -> {t.oid}\\n"' in src
    assert re.fullmatch(J.V_ADOPT.pattern, f"took head: refs/x/y -> {OID}")
    assert re.fullmatch(J.V_ADOPT.pattern, "took head: refs/x/y -> " + "2" * 64)
    assert not re.fullmatch(J.V_ADOPT.pattern, f"took wip: refs/x/y -> {OID}")
