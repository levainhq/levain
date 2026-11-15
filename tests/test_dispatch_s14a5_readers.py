"""S14 A5 (`follow_run.production_readers`, `verify_stage.ControlResolve`'s D5): two of the follower's three readers.

`envelope(session, ticket)` reads root's `<export>/<session>/verifier-report.json` without following a link; `answered(job,
run)` reads the lineage sink's rows for the run; `tree` stays unsourced until A6. What is NOT run here: the group read of
`L/export/dispatch` as `_levain_dispatch` in `levain-dispatch-export` (a root-side VM measurement)."""
from __future__ import annotations

import json
import os

import pytest

from levain.dispatch import follow_run as FR
from levain.dispatch import verify_stage as VS
from levain.dispatch.verifier_report import MAX_ENVELOPE_BYTES
from tests.test_dispatch_run_jobs import MODELS, World, cdir, keys, log, pytestmark, py, store, w  # noqa: F401
from tests.test_dispatch_run_jobs_verify import event, ticket_of, verifying

SESSION = "s-" + "9" * 16
NAME = "verifier-report.json"


@pytest.fixture
def export(tmp_path):
    d = tmp_path / "export"
    d.mkdir()
    return d


def put(export, data: bytes, session: str = SESSION) -> None:
    (export / session).mkdir(exist_ok=True)
    (export / session / NAME).write_bytes(data)


def readers(export, sink):
    return FR.production_readers(str(export), str(sink))


# ---- envelope ----------------------------------------------------------------------------------------------------------

def test_the_envelope_is_the_exported_files_bytes(export, tmp_path):
    put(export, b'{"a":1}')
    assert readers(export, tmp_path / "s").envelope(SESSION, "t") == b'{"a":1}'


def test_the_envelope_cap_is_exact(export, tmp_path):
    rd = readers(export, tmp_path / "s")
    put(export, b"x" * MAX_ENVELOPE_BYTES)
    assert rd.envelope(SESSION, "t") == b"x" * MAX_ENVELOPE_BYTES
    put(export, b"x" * (MAX_ENVELOPE_BYTES + 1))
    assert rd.envelope(SESSION, "t") is None                                   # over the cap: never read whole


def test_an_absent_envelope_or_session_is_none(export, tmp_path):
    rd = readers(export, tmp_path / "s")
    assert rd.envelope(SESSION, "t") is None
    (export / SESSION).mkdir()
    assert rd.envelope(SESSION, "t") is None
    assert readers(export / "nope", tmp_path / "s").envelope(SESSION, "t") is None


def test_the_envelope_follows_no_link_at_any_component(export, tmp_path):
    rd = readers(export, tmp_path / "s")
    secret = tmp_path / "secret"
    secret.write_bytes(b"{}")
    (export / SESSION).mkdir()
    os.symlink(secret, export / SESSION / NAME)                                # the file is a link
    assert rd.envelope(SESSION, "t") is None
    other = tmp_path / "other"
    other.mkdir()
    (other / NAME).write_bytes(b"{}")
    os.symlink(other, export / ("s-" + "8" * 16))                              # the session dir is a link
    assert rd.envelope("s-" + "8" * 16, "t") is None
    real = tmp_path / "real"
    real.mkdir()
    (real / SESSION).mkdir()
    (real / SESSION / NAME).write_bytes(b"{}")
    os.symlink(real, tmp_path / "linked")                                      # the export dir itself is a link
    assert readers(tmp_path / "linked", tmp_path / "s").envelope(SESSION, "t") is None


def test_the_envelope_is_a_regular_file(export, tmp_path):
    rd = readers(export, tmp_path / "s")
    (export / SESSION).mkdir()
    (export / SESSION / NAME).mkdir()                                          # a directory
    assert rd.envelope(SESSION, "t") is None
    (export / SESSION / NAME).rmdir()
    os.mkfifo(export / SESSION / NAME)                                         # a fifo: must not block
    assert rd.envelope(SESSION, "t") is None


@pytest.mark.parametrize("session", ["", ".", "..", "../x", "a/b", "/abs", "a\x00b", None, 7])
def test_a_session_that_is_not_one_name_reads_nothing(export, tmp_path, session):
    put(export, b"{}", "x")
    (export.parent / NAME).write_bytes(b"{}")
    assert readers(export, tmp_path / "s").envelope(session, "t") is None


# ---- answered ----------------------------------------------------------------------------------------------------------

def row(job="j1", run=0, answered="glm-5", **kw):
    return {"run_id": "r-" + "a" * 16, "job_id": job, "run_index": run, "model_answered": answered, **kw}


def sink_of(tmp_path, rows, tail=b""):
    p = tmp_path / "sink.jsonl"
    p.write_bytes(b"".join(json.dumps(r).encode() + b"\n" for r in rows) + tail)
    return p


class J:
    def __init__(self, id):
        self.id = id


def test_answered_is_the_last_row_that_names_a_model(export, tmp_path):
    sink = sink_of(tmp_path, [row(answered="old"), row(answered=None), row(answered="unknown"), row(answered="glm-5"),
                              row(answered="unknown"), row(answered=None)])
    assert readers(export, sink).answered(J("j1"), 0) == "glm-5"


def test_answered_is_none_when_no_row_names_one(export, tmp_path):
    sink = sink_of(tmp_path, [row(answered=None), row(answered="unknown")])
    rd = readers(export, sink)
    assert rd.answered(J("j1"), 0) is None
    assert rd.answered(J("j1"), 1) is None                                      # another run's rows are not this run's
    assert rd.answered(J("j2"), 0) is None


def test_answered_ignores_other_runs_and_jobs(export, tmp_path):
    sink = sink_of(tmp_path, [row(job="j2", answered="other-job"), row(run=1, answered="other-run"),
                              row(answered="mine")])
    assert readers(export, sink).answered(J("j1"), 0) == "mine"


def test_answered_reads_a_value_only_when_it_is_a_string(export, tmp_path):
    sink = sink_of(tmp_path, [row(answered="mine"), row(answered=5), row(answered=""), row(run=True, answered="bool")])
    assert readers(export, sink).answered(J("j1"), 0) == "mine"


def test_answered_tolerates_a_torn_last_line_and_a_bad_middle_one(export, tmp_path):
    p = sink_of(tmp_path, [row(answered="mine")], tail=b'{"job_id":"j1","run_in')
    assert readers(export, p).answered(J("j1"), 0) == "mine"
    p.write_bytes(b"{bad j1\n" + json.dumps(row(answered="later")).encode() + b"\n")
    assert readers(export, p).answered(J("j1"), 0) == "later"


def test_an_absent_or_unreadable_sink_answers_none(export, tmp_path):
    assert readers(export, tmp_path / "absent").answered(J("j1"), 0) is None
    assert readers(export, tmp_path).answered(J("j1"), 0) is None               # a directory


def test_answered_follows_no_link_and_never_blocks_on_a_fifo(export, tmp_path):
    """S14 fix 4 (L2 M1): the sink is opened O_NOFOLLOW|O_NONBLOCK and must be a regular file; anything else answers
    None (fail closed: the model is optional). RED on the old code: `open()` followed the link (answering "mine") and
    blocked for ever opening the fifo (this test would hang)."""
    real = sink_of(tmp_path, [row(answered="mine")])
    link = tmp_path / "link.jsonl"
    os.symlink(real, link)
    assert readers(export, real).answered(J("j1"), 0) == "mine"
    assert readers(export, link).answered(J("j1"), 0) is None
    fifo = tmp_path / "fifo.jsonl"
    os.mkfifo(fifo)
    assert readers(export, fifo).answered(J("j1"), 0) is None


def test_answered_refuses_a_sink_over_the_cap(export, tmp_path, monkeypatch):
    """S14 fix 4: a sink over ANSWERED_SINK_MAX is not read (None), so memory per event is bounded. RED on the old code:
    there was no cap, the whole file was read and "mine" answered."""
    sink = sink_of(tmp_path, [row(answered="mine")])
    size = sink.stat().st_size
    monkeypatch.setattr(FR, "ANSWERED_SINK_MAX", size)
    assert readers(export, sink).answered(J("j1"), 0) == "mine"                  # at the cap: read
    monkeypatch.setattr(FR, "ANSWERED_SINK_MAX", size - 1)
    assert readers(export, sink).answered(J("j1"), 0) is None                    # one byte over: refused


def test_answered_reads_in_chunks_and_skips_an_over_long_line(export, tmp_path, monkeypatch):
    """S14 fix 4: the bounded read finds a row split across chunks, and a line longer than the line cap is skipped
    to its newline, never held, with the rows after it still read."""
    monkeypatch.setattr(FR, "_ANSWERED_CHUNK", 7)                 # every row spans many chunks
    monkeypatch.setattr(FR, "_ANSWERED_LINE_MAX", 200)            # an ordinary row (~90 bytes) fits; the long one not
    first = json.dumps(row(answered="first")).encode() + b"\n"
    long = json.dumps(row(answered="long", pad="x" * 500)).encode() + b"\n"
    after = json.dumps(row(answered="after-long")).encode() + b"\n"
    assert len(first) < 200 and len(after) < 200 and len(long) > 200
    p = tmp_path / "sink.jsonl"
    p.write_bytes(first + long + after)
    assert readers(export, p).answered(J("j1"), 0) == "after-long"
    p.write_bytes(first + long)
    assert readers(export, p).answered(J("j1"), 0) == "first"                    # the long row is skipped, not taken


def test_the_tree_reader_stays_unsourced_until_a6(export, tmp_path):
    assert readers(export, tmp_path / "s").tree(J("j1"), 0) is None


# ---- D5 and the resolve ------------------------------------------------------------------------------------------------

def test_an_unknown_answered_model_does_not_wedge_the_resolve(w):
    """D5: a report whose lineage names no answering model still resolves; the record then holds none."""
    job, _ = verifying(w)
    w.runner.verify_job(job.id)
    rd = VS.Readers(tree=lambda _j, _r: "a" * 40, envelope=lambda _s, _t: b"{}", answered=lambda _j, _r: None)
    ev = event(ticket_of(w, job, 0), verifier_report_digest="sha256:" + "d" * 64)
    call = VS.ControlResolve(w.port, w.jobs, rd)(ev)
    assert call is not None and call.model == {} and call.envelope == b"{}"


def test_the_production_readers_resolve_a_report_event_once_a_tree_is_supplied(w, export, tmp_path):
    """The design's p3 re-run: with a tree the production readers' envelope and answered give a VerifierCall."""
    job, _ = verifying(w)
    w.runner.verify_job(job.id)
    sink = sink_of(tmp_path, [row(job=job.id, run=0, answered="glm-5")])
    put(export, b'{"env":1}')
    rd = readers(export, sink)
    rd = VS.Readers(tree=lambda _j, _r: "a" * 40, envelope=rd.envelope, answered=rd.answered)
    ev = event(ticket_of(w, job, 0), verifier_report_digest="sha256:" + "d" * 64)
    call = VS.ControlResolve(w.port, w.jobs, rd)(ev)
    assert call is not None and call.envelope == b'{"env":1}' and call.model == {"answered": "glm-5"}
    # and with its own (unsourced) tree, until A6, the call carries tree None: the store fails the job
    # verifier-tree-unavailable and the follower acks (S14 fix 1; the old code answered None here and wedged the follower)
    own = VS.ControlResolve(w.port, w.jobs, readers(export, sink))(ev)
    assert own is not None and own.tree is None and own.envelope == b'{"env":1}'


# ---- main installs them ------------------------------------------------------------------------------------------------

def test_main_installs_the_production_readers(tmp_path, monkeypatch):
    seen = {}

    class Stop(Exception):
        pass

    def spy(socket, jobs, rd=None):
        seen["readers"] = rd
        raise Stop

    monkeypatch.setattr(FR, "control_resolve", spy)
    store_dir = tmp_path / "store"
    export_dir = tmp_path / "export"
    sink = tmp_path / "lineage.jsonl"
    with pytest.raises(Stop):
        FR.main(["--store-dir", str(store_dir), "--socket", str(tmp_path / "c.sock"), "--export-dir", str(export_dir),
                 "--lineage-sink", str(sink)], supervisor=None)
    rd = seen["readers"]
    put(export_dir if export_dir.exists() else (export_dir.mkdir() or export_dir), b"{}")
    assert rd is not None and rd.envelope(SESSION, "t") == b"{}"                # reads the --export-dir it was given
    sink_of(tmp_path, [row(answered="m")]).rename(sink)
    assert rd.answered(J("j1"), 0) == "m"                                       # and the --lineage-sink
