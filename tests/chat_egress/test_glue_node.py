"""CH-D2 S4: the glue's `node --test` suites, run from pytest, and the cross-language check that Python's frozen
`wire` readers accept, byte for byte, what the Node glue writes.

- the unit suite (no srt): tests/chat_egress/node/glue_unit.test.mjs
- the srt suite (srt 0.0.81 + the fake decider driver, fake_decider.py): tests/chat_egress/node/glue_srt.test.mjs
- wire.parse_glue_event / parse_obs_line read every line emit_samples.mjs writes with the glue's own functions, and
  re-encoding the parsed value with wire's reference encoder gives the same bytes; the JS escaper's vectors are
  wire.ESCAPE_VECTORS.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from levain.chat_egress import wire

from .fixtures import env as envmod

NODE_DIR = Path(__file__).resolve().parent / "node"


def _node() -> str:
    n = envmod.node_bin()
    if n is None:
        pytest.skip("node is not on PATH (set CHD2_NODE)")
    return n


def _run_node_test(path: Path, env: dict, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run([_node(), "--test", "--test-concurrency=1", "--test-timeout=120000", str(path)],
                          capture_output=True, text=True, timeout=timeout, env=env)


def _summary(out: str) -> str:
    return "\n".join(line for line in out.splitlines() if line.startswith(("ℹ tests", "ℹ pass", "ℹ fail", "ℹ todo",
                                                                            "ℹ skipped", "✖", "# C")))


def test_glue_unit_suite(tmp_path: Path) -> None:
    p = _run_node_test(NODE_DIR / "glue_unit.test.mjs", {"TMPDIR": str(tmp_path)}, 120)
    assert p.returncode == 0, _summary(p.stdout) + p.stdout[-3000:] + p.stderr[-2000:]
    assert "ℹ fail 0" in p.stdout


@pytest.mark.chat_egress_srt
def test_glue_srt_suite(tmp_path: Path) -> None:
    why = envmod.srt_skip_reason()
    if why:
        pytest.skip(why)
    env = {"TMPDIR": str(tmp_path), "CHD2_PYTHON": sys.executable,
           "CHD2_SRT_DIST": str(envmod.srt_package_dir()), "HOME": os.environ.get("HOME", str(tmp_path))}
    p = _run_node_test(NODE_DIR / "glue_srt.test.mjs", env, 600)
    print(_summary(p.stdout))
    assert p.returncode == 0, _summary(p.stdout) + p.stdout[-4000:] + p.stderr[-2000:]
    assert "ℹ fail 0" in p.stdout and "ℹ skipped 0" in p.stdout and "ℹ todo 0" in p.stdout


def _emit(tmp_path: Path) -> dict:
    p = subprocess.run([_node(), str(NODE_DIR / "emit_samples.mjs"), str(tmp_path)], capture_output=True, text=True,
                       timeout=60, env={"TMPDIR": str(tmp_path)})
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_python_reads_what_node_writes(tmp_path: Path) -> None:
    meta = _emit(tmp_path)
    events = (tmp_path / "events.jsonl").read_bytes().splitlines(keepends=True)
    obs = (tmp_path / "obs.jsonl").read_bytes().splitlines(keepends=True)
    assert len(events) == meta["events"] and len(obs) == meta["obs"]
    kinds = set()
    for line in events:
        e = wire.parse_glue_event(line)
        kinds.add(type(e).__name__ + (":final" if getattr(e, "final", False) else ""))
        assert wire.glue_event_line(e) == line, line
    assert kinds == {"GlueReq", "GlueWall", "GlueSummary", "GlueSummary:final"}
    for line in obs:
        o = wire.parse_obs_line(line)
        again = (wire.obs_response_line(o.request_id, o.status, o.content_type, o.content_length)
                 if isinstance(o, wire.ObsResponse) else wire.obs_ended_line(o.request_id))
        assert again == line, line
    assert {type(wire.parse_obs_line(x)).__name__ for x in obs} == {"ObsResponse", "ObsEnded"}


def test_js_escape_vectors_are_wires(tmp_path: Path) -> None:
    meta = _emit(tmp_path)
    assert [tuple(v) for v in meta["vectors"]] == list(wire.ESCAPE_VECTORS)
    for raw, limit, want in wire.ESCAPE_VECTORS:
        assert wire.escape_text(raw, limit) == want
