"""The shipped verifier driver template ``output_compare``, run the way CONTRACT §3.5 fixes it (``<python> -I -S
<template> <input.json> <run_dir>``) on hand-built run dirs in §5.6's shape, its stdout read through the report body
validator and the envelope check, with the ticket fields derived from a real signed verifier item."""
from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys

import pytest

from levain.contracts.canon import canonical_bytes
from levain.dispatch import registry
from levain.dispatch.verifier_report import VerifierRun, body_problem, check_envelope, contract_entry
from levain.verifier_drivers import output_compare as oc
from tests import test_dispatch_control as tc
from tests.test_dispatch_control import accept, vbody, ventry

keys, log, cdir, store = tc.keys, tc.log, tc.cdir, tc.store

TEMPLATE = registry.driver_template_path("output_compare")
OK = {"exit": 0, "signal": None, "wall_s": 3, "timed_out": False, "truncated_bytes": 0, "lingering_killed": 0}
STEPS = [{"argv": ["python3", "-m", "pytest", "-q"], "wall_s": 300}, {"argv": ["ruff", "check"], "wall_s": 60}]
INPUT = {"steps": [{"exit": 0, "stdout_equals": "2 passed\n", "stderr_empty": True},
                   {"exit": 0, "stdout_sha256": "sha256:" + hashlib.sha256(b"All checks passed!\n").hexdigest()}]}


def drive(tmp_path, records, driver_input=INPUT, *, raw_input=None):
    """Write ``input.json`` and a run dir of ``(exit record | None, out, err)`` per step, run the template under
    ``-I -S``; returns the completed process."""
    inp = tmp_path / "input.json"
    inp.write_bytes(raw_input if raw_input is not None else canonical_bytes(driver_input))
    run = tmp_path / "run"
    run.mkdir()
    for k, (rec, out, err) in enumerate(records):
        if rec is not None:
            (run / f"step{k}.exit").write_text(json.dumps(rec))
        (run / f"step{k}.out").write_bytes(out)
        (run / f"step{k}.err").write_bytes(err)
    return subprocess.run([sys.executable, "-I", "-S", str(TEMPLATE), str(inp), str(run)], capture_output=True,
                          timeout=60, cwd=str(tmp_path))


def body_of(p):
    assert p.returncode == 0, p.stderr
    body = json.loads(p.stdout)
    assert canonical_bytes(body) == p.stdout                 # canonical, no trailing newline
    assert body_problem(body) is None
    return body


def test_the_template_imports_only_the_standard_library():
    tree = ast.parse(TEMPLATE.read_bytes())
    names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert names == {"__future__", "hashlib", "json", "os", "re", "stat", "sys"}


def test_a_passing_run_reads_as_pass_through_the_envelope(store, log, keys, tmp_path):
    entry = ventry("pytest-bb", "black-box", steps=STEPS,
                   steps_digest="sha256:" + hashlib.sha256(canonical_bytes(STEPS)).hexdigest(), driver_input=INPUT,
                   driver_input_digest="sha256:" + hashlib.sha256(canonical_bytes(INPUT)).hexdigest())
    accept(store, log, keys, "verifier", 1, vbody(entry))
    ticket = registry.verifier_ticket(store.active(), verifier_id="pytest-bb")
    records = [(OK, b"2 passed\n", b""), (OK, b"All checks passed!\n", b"")]
    p = drive(tmp_path, records, ticket["entry"]["driver_input"])
    assert "sha256:" + hashlib.sha256((tmp_path / "input.json").read_bytes()).hexdigest() == \
        ticket["driver_input_digest"]
    body = body_of(p)
    assert body["result"] == "pass" and [s["verdict"] for s in body["steps"]] == ["pass", "pass"]
    run = VerifierRun(job_id="dj-" + "0" * 32, run=0, session="v-0", ticket="t-0",
                      verifier=contract_entry(ticket["entry"]), steps=ticket["verifier_steps"],
                      driver_input_digest=ticket["driver_input_digest"], input_bundle_digest="sha256:" + "e" * 64)
    env = canonical_bytes({"session": "v-0", "ticket": "t-0", "verifier": dict(run.verifier),
                           "driver_input_digest": run.driver_input_digest,
                           "input_bundle_digest": run.input_bundle_digest, "steps": [OK, OK], "body": body})
    rep = check_envelope(env, expected=run, event_digest="sha256:" + hashlib.sha256(env).hexdigest())
    assert rep.result == "pass" and rep.first_failure is None


@pytest.mark.parametrize("rec, out, err, note", [
    (dict(OK, exit=1), b"2 passed\n", b"", "exit 1, expected 0"),
    (dict(OK, exit=None, timed_out=True, wall_s=300), b"", b"", "timed out after 300 s"),
    (dict(OK, exit=None, signal=9), b"", b"", "killed by signal 9"),
    (OK, b"1 passed\n", b"", "stdout is not the pinned text"),
    (OK, b"2 passed\n", b"warn\n", "stderr is not empty (5 bytes)"),
    (dict(OK, truncated_bytes=7), b"2 passed\n", b"", "output truncated by 7 bytes"),
])
def test_a_step_that_misses_its_pin_fails_with_a_fixed_note(tmp_path, rec, out, err, note):
    body = body_of(drive(tmp_path, [(rec, out, err), (OK, b"All checks passed!\n", b"")]))
    assert body["result"] == "fail"
    assert body["steps"][0]["verdict"] == "fail" and body["steps"][0]["note"].startswith(note)
    assert body["steps"][1]["verdict"] == "pass"


def test_a_stdout_digest_pin_is_checked(tmp_path):
    body = body_of(drive(tmp_path, [(OK, b"2 passed\n", b""), (OK, b"Found 1 error.\n", b"")]))
    assert body["steps"][1] == {"step": 1, "verdict": "fail", "note": "stdout is not the pinned sha256"}


def test_an_exit_only_pin_ignores_the_outputs(tmp_path):
    body = body_of(drive(tmp_path, [(dict(OK, truncated_bytes=9), b"x", b"y")], {"steps": [{"exit": 0}]}))
    assert body["steps"] == [{"step": 0, "verdict": "pass", "note": "exit 0 as pinned"}]


@pytest.mark.parametrize("records, raw, why", [
    ([(OK, b"2 passed\n", b""), (None, b"", b"")], None, "step1.exit cannot be opened"),
    ([(dict(OK, extra=1), b"", b""), (OK, b"", b"")], None, "step0.exit is not the contract's exit record"),
    ([(dict(OK, exit=True), b"", b""), (OK, b"", b"")], None, "step0.exit is not the contract's exit record"),
    ([(OK, b"", b"")], b'{"steps":[{"exit":0,"exit":1}]}', "a duplicate key"),
    ([(OK, b"", b"")], b'{"steps":[{"exit":0.0}]}', "not an integer"),
    ([(OK, b"", b"")], b'{"steps":[{"exit":0,"stdout_regex":"."}]}', "expectation 0"),
    ([(OK, b"", b"")], b'{"steps":[]}', "a list of 1 to 16"),
    ([(OK, b"", b"")], b'{"steps":[{"exit":256}]}', "from 0 to 255"),
    ([(OK, b"", b"")], b'{"steps":[{"exit":0,"stdout_sha256":"abc"}]}', "sha256:<64 hex>"),
    ([(OK, b"", b"")], b"not json", "input.json is not JSON"),
])
def test_an_input_or_capture_out_of_shape_is_infrastructure_never_a_verdict(tmp_path, records, raw, why):
    p = drive(tmp_path, records, raw_input=raw)
    assert p.returncode == 2 and p.stdout == b"" and why in p.stderr.decode()


def test_an_oversized_capture_is_infrastructure(tmp_path):
    p = drive(tmp_path, [(OK, b"x" * (oc.MAX_CAPTURE_BYTES + 1), b"")], {"steps": [{"exit": 0}]})
    assert p.returncode == 2 and "step0.out is over" in p.stderr.decode()


def test_the_wrong_argv_is_a_usage_error(tmp_path):
    p = subprocess.run([sys.executable, "-I", "-S", str(TEMPLATE)], capture_output=True, timeout=60)
    assert p.returncode == 2 and p.stdout == b"" and b"usage" in p.stderr


@pytest.mark.parametrize("note", ["exit 0 as pinned", "café — 日本", 'quote " and \\ back',
                                  "tab\tnewline\ncontrol\x01", "  <>&\x7f"])
def test_the_hand_written_canon_is_levains(note):
    body = {"format": oc.REPORT_FORMAT, "result": "pass", "steps": [{"step": 0, "verdict": "pass", "note": note}]}
    assert oc.canonical(body) == canonical_bytes(body)


def test_the_report_constants_mirror_dispatch():
    from levain.dispatch import verifier_report as vr
    from levain.slot import runtime_grammar as rg

    assert oc.REPORT_FORMAT == vr.REPORT_FORMAT and oc.MAX_STEPS == vr.MAX_STEPS == rg.MAX_STEPS
    from levain.slot import stage

    # the driver reads input.json up to V25; S1 3c-8 r1: dispatch accepts a driver_input only up to what one
    # CONFIG-SUBMIT frame carries, which is below it
    assert oc.MAX_CAPTURE_BYTES == vr.MAX_REPORT_BYTES and oc.MAX_INPUT_BYTES == stage.DRIVER_INPUT_CAP
    assert registry.MAX_DRIVER_INPUT_BYTES <= oc.MAX_INPUT_BYTES
    assert oc.EXIT_KEYS == vr.EXIT_KEYS


# --- W2-b L3 r1 C, D, ruling (3) -----------------------------------------------------------------------------------

def test_the_docstring_says_step_numbering_is_0_based():
    assert "0-BASED" in oc.__doc__ and "step0.exit" in oc.__doc__


def test_a_capture_that_is_a_directory_is_infrastructure(tmp_path):
    inp = tmp_path / "input.json"
    inp.write_bytes(canonical_bytes({"steps": [{"exit": 0}]}))
    run = tmp_path / "run"
    run.mkdir()
    (run / "step0.exit").write_text(json.dumps(OK))
    (run / "step0.out").mkdir()
    (run / "step0.err").write_bytes(b"")
    p = subprocess.run([sys.executable, "-I", "-S", str(TEMPLATE), str(inp), str(run)], capture_output=True,
                       timeout=60)
    assert p.returncode == 2 and p.stdout == b"" and "step0.out is not a regular file" in p.stderr.decode()


def test_a_capture_that_is_a_fifo_neither_blocks_nor_is_read(tmp_path):
    import os

    inp = tmp_path / "input.json"
    inp.write_bytes(canonical_bytes({"steps": [{"exit": 0}]}))
    run = tmp_path / "run"
    run.mkdir()
    (run / "step0.exit").write_text(json.dumps(OK))
    os.mkfifo(run / "step0.out")             # no writer: a blocking open would hang here until the timeout
    (run / "step0.err").write_bytes(b"")
    p = subprocess.run([sys.executable, "-I", "-S", str(TEMPLATE), str(inp), str(run)], capture_output=True,
                       timeout=30)
    assert p.returncode == 2 and p.stdout == b"" and "step0.out is not a regular file" in p.stderr.decode()


def test_a_read_fault_is_infrastructure_and_closes_the_fd(tmp_path, monkeypatch):
    import os

    f = tmp_path / "x"
    f.write_bytes(b"abc")
    closed = []
    real_close = os.close

    def bad_read(fd, n):
        raise OSError(5, "Input/output error")

    def spy_close(fd):
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(oc.os, "read", bad_read)
    monkeypatch.setattr(oc.os, "close", spy_close)
    with pytest.raises(oc.Infrastructure, match=r"x cannot be read \(OSError\)"):
        oc._read(str(f), 10, "x")
    assert len(closed) == 1


def test_a_non_regular_file_closes_the_fd(tmp_path, monkeypatch):
    import os

    closed = []
    real_close = os.close
    monkeypatch.setattr(oc.os, "close", lambda fd: (closed.append(fd), real_close(fd)))
    with pytest.raises(oc.Infrastructure, match="is not a regular file"):
        oc._read(str(tmp_path), 10, "dir")
    assert len(closed) == 1


def test_a_stdout_equals_that_is_not_utf8_encodable_is_infrastructure(tmp_path):
    raw = b'{"steps":[{"exit":0,"stdout_equals":"\\ud800"}]}'
    with pytest.raises(oc.Infrastructure, match="not encodable as UTF-8"):
        oc.expectations(raw)
    p = drive(tmp_path, [(OK, b"", b"")], raw_input=raw)
    assert p.returncode == 2 and p.stdout == b"" and "stdout_equals is not encodable" in p.stderr.decode()


def test_expectations_hand_judge_the_encoded_bytes():
    (e,) = oc.expectations(b'{"steps":[{"exit":0,"stdout_equals":"caf\\u00e9\\n"}]}')
    assert e["stdout_equals"] == "café\n".encode("utf-8")
    assert oc.judge(e, OK, "café\n".encode("utf-8"), b"")[0] == "pass"
    assert oc.judge(e, OK, b"cafe\n", b"")[0] == "fail"
