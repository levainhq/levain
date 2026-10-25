"""S1 3c-6b: CONTROL's lineage sink (`levain.slot.lineage`) and keys' `LINEAGE-APPEND {envelope}` (CONTRACT §3.3 keys
row; SCHEMAS §2.1 `levain-dispatch-lineage/3`; 3c design r3 §2.4 read through R4 §3; head rulings H6-2, H6-4, H6-9,
H6-11, H6-14..H6-17).

In-process: a real :class:`levain.slot.control.Control` over a real state store and a real sink in a temp dir, driven
through ``Control.handle`` on a keys connection that holds the attachment. The CONTROL-crash rows of the crash table
are proved at host level through the sink's one fault seam (H6-9); CONTROL-as-a-process is 3c-10's residue. The VM half
of `test_lineage_unreadable_to_slot_and_gw` (real uids, EACCES) is 3c-10's."""
from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import accounts as A
from levain.slot import control as C
from levain.slot import layout
from levain.slot import lineage as LN
from levain.slot.ids import SHARED_GROUPS
from tests.test_slot_s2a_control import OP_UID

KEYS_UID = OP_UID + 3000
LEDGER = "a" * 32
EPOCH = "b" * 32
OTHER = "c" * 32


class Crash(Exception):
    """The fault seam's stand-in for CONTROL dying at a point (H6-9)."""


def record(ref: int, *, ledger: str = LEDGER, epoch: str = EPOCH, **over) -> dict:
    """A well-formed `/3` record as keys sends it (no `seq`): every field present (H6-15)."""
    rec = {"format": LN.FORMAT, "source": {"ledger": ledger, "epoch": epoch, "ref": ref},
           "at": "2026-10-24T12:00:00Z", "ended_at": "2026-10-24T12:00:03Z",
           "run_id": "r-0123456789abcdef", "job_id": "dj-1", "run_index": 0, "contract_digest": "sha256:" + "1" * 64,
           "route": "r", "routemap_generation": 1, "route_policy_digest": "sha256:" + "2" * 64,
           "model_requested": "m1", "model_answered": "m1-2026", "provider_answered": "Google AI Studio",
           "tokens_in": 10, "tokens_out": 20, "cost_microdollars": 300, "cost_reported": None,
           "hold_microdollars": 5000, "run_cap_microdollars": 100000, "settled": "yes", "overrun": None,
           "unproven": False, "status": 200, "refused": "", "count": None}
    rec.update(over)
    return rec


def short_record(ref: int) -> dict:
    """A well-formed record with every free string at one character: 64 of them fit H6-19's 48 KiB (49,053 bytes),
    where 64 of :func:`record` (51,229) do not."""
    return record(ref, job_id="d", route="r", model_requested="m", model_answered="m", provider_answered="p",
                  tokens_in=1, tokens_out=1, cost_microdollars=1, hold_microdollars=1, run_cap_microdollars=None)


def envelope(*records: dict, ledger: str = LEDGER, epoch: str = EPOCH) -> dict:
    return {"ledger_id": ledger, "epoch": epoch, "records": list(records)}


def triple(ref: int) -> dict:
    return {"ledger": LEDGER, "epoch": EPOCH, "ref": ref}


class World:
    """A Control with a lineage sink at `<d>/lineage/lineage.jsonl`; `restart()` is a new Control and a new sink on the
    same files (what a CONTROL restart sees)."""

    def __init__(self, d: Path, **sink_kw) -> None:
        self.d = d
        (d / "s").mkdir(mode=0o700, exist_ok=True)
        (d / "lineage").mkdir(mode=0o750, exist_ok=True)
        self.path = d / "lineage" / LN.SINK
        self.control: C.Control
        self.sink: LN.LineageSink
        self._open(**sink_kw)

    def _open(self, **sink_kw) -> None:
        self.sink = LN.LineageSink(self.path, **sink_kw)
        self.control = C.Control(C.StateStore(self.d / "s"), OP_UID, keys_uid=KEYS_UID, lineage=self.sink)
        self.control.start()
        self.keys = C.Conn(KEYS_UID)
        ans = self.call("KEYS-ATTACH", generation=None, item_sha256=None)
        assert ans["ok"], ans

    def restart(self, **sink_kw) -> None:
        self.control.stop()
        self._open(**sink_kw)

    def call(self, op: str, conn: C.Conn | None = None, **args) -> dict:
        return self.control.handle(conn or self.keys, {"v": 1, "op": op, "id": 7, **args})

    def append(self, env: dict) -> dict:
        return self.call("LINEAGE-APPEND", envelope=env)

    def rows(self) -> list:
        return [canon.parse(line) for line in self.path.read_bytes().split(b"\n")[:-1]]


@pytest.fixture
def w():
    d = Path(tempfile.mkdtemp(prefix="ln", dir="/tmp"))
    world = World(d)
    try:
        yield world
    finally:
        world.control.stop()
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def tmp():
    d = Path(tempfile.mkdtemp(prefix="ln", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def acked(*refs: int, quarantined=()) -> dict:
    return {"id": 7, "ok": True, "acked": [triple(r) for r in refs],
            "quarantined": [{"source": triple(r), "field": f} for r, f in quarantined]}


# --- the DONE rows -------------------------------------------------------------------------------------------------

def test_lineage_resend_deduped(w):
    # r3 §2.4 DONE: the same source twice is ONE row; the resend is acked by membership without appending
    assert w.append(envelope(record(5))) == acked(5)
    assert w.append(envelope(record(5))) == acked(5)
    rows = w.rows()
    assert [(r["seq"], r["source"]) for r in rows] == [(1, triple(5))]
    want = dict(record(5), seq=1)
    assert rows[0] == want                    # the row is exactly the record plus CONTROL's seq
    # r1 F7: inside one envelope a triple twice is a keys bug, refused whole `lineage-record:envelope`
    assert w.append(envelope(record(6), record(6))) == {"id": 7, "ok": False, "refused": "lineage-record:envelope"}
    assert [r["source"]["ref"] for r in w.rows()] == [5]


def test_lineage_out_of_order_kept(w):
    # refs 7 and 9 delivered 9 first: both appended (membership, never a high-water: r1's Q8 drop)
    assert w.append(envelope(record(9))) == acked(9)
    assert w.append(envelope(record(7))) == acked(7)
    assert [(r["seq"], r["source"]["ref"]) for r in w.rows()] == [(1, 9), (2, 7)]
    # and across epochs of the same ledger: a new epoch's ref 7 is a new triple
    ans = w.append(envelope(record(7, epoch=OTHER), epoch=OTHER))
    assert ans["ok"] and ans["acked"] == [{"ledger": LEDGER, "epoch": OTHER, "ref": 7}], ans
    assert len(w.rows()) == 3


@pytest.mark.parametrize("bad, field", [
    ({"cost_reported": "unknown"}, "cost_reported"),        # H6-15: int|null, never "unknown"
    ({"unproven": 0}, "unproven"),                          # H6-15: a boolean
    ({"tokens_in": -1}, "tokens_in"),
    ({"settled": "maybe"}, "settled"),
    ({"run_id": "r-XYZ"}, "run_id"),
    ({"seq": 3}, "seq"),                                    # seq is CONTROL's
    ({"color": "red"}, "color"),                            # a key the schema does not hold
    ({"Bad Key!": 1}, "record"),                            # ... named only when it reads as a field name
    ({"model_answered": 5}, "model_answered"),              # H6-21: string | "unknown" | null, nothing else
    ({"provider_answered": ""}, "provider_answered"),
    ({"at": None}, "at"),                                   # a field SCHEMAS §2.1 does not mark |null stays non-null
])
def test_lineage_poison_row_quarantined(w, bad, field):
    # r3 c2-F4 / R4 §3: a malformed record inside a VALID envelope is a quarantine row, acked, so the outbox drains and
    # the exchange still has exactly one row; the good record beside it is appended as usual
    ans = w.append(envelope(record(1), record(2, **bad)))
    assert ans == acked(1, 2, quarantined=[(2, field)]), ans
    rows = w.rows()
    # H6-20: the quarantine row is {format, source, quarantined} (+ CONTROL's seq); no `session`
    assert rows[1] == {"format": LN.FORMAT, "seq": 2, "source": triple(2), "quarantined": field}
    assert rows[0]["seq"] == 1 and rows[0]["source"] == triple(1) and "quarantined" not in rows[0]
    # the resend (keys lost the ack): acked by membership, its quarantine stated again, nothing appended
    assert w.append(envelope(record(2, **bad))) == acked(2, quarantined=[(2, field)])
    assert len(w.rows()) == 2


def test_lineage_answered_null_vs_unknown(w):
    # H6-21: `null` = no upstream call (a refused or released row); "unknown" = the call happened, the response did not
    # say. Both are well-formed records, appended whole, never quarantined.
    refused = record(1, model_answered=None, provider_answered=None, tokens_in=None, tokens_out=None,
                     cost_microdollars=None, cost_reported=None, hold_microdollars=0, settled="no", status=0,
                     refused="keys-unavailable", count=3)
    released = record(2, model_answered=None, provider_answered=None, settled="no", status=0)
    unknown = record(3, model_answered="unknown", provider_answered="unknown")
    assert w.append(envelope(refused, released, unknown)) == acked(1, 2, 3)
    rows = w.rows()
    assert [(r["model_answered"], r["provider_answered"]) for r in rows] == \
        [(None, None), (None, None), ("unknown", "unknown")]
    assert all("quarantined" not in r for r in rows)


def test_lineage_missing_field_quarantined(w):
    # H6-15: every field always present; a record without one is quarantined on that field's name
    rec = record(3)
    del rec["count"]
    assert w.append(envelope(rec)) == acked(3, quarantined=[(3, "count")])


@pytest.mark.parametrize("env", [
    envelope(record(1), record(2, epoch=OTHER)),                    # H6-14: an element's epoch is not the envelope's
    envelope(record(1, ledger=OTHER)),                              # ... nor its ledger
    envelope(),                                                     # no records
    envelope(*[record(i) for i in range(LN.ENVELOPE_MAX + 1)]),     # over 64 (H6-11)
    {"ledger_id": LEDGER, "epoch": EPOCH},                          # not exactly {ledger_id, epoch, records}
    {"ledger_id": LEDGER, "epoch": EPOCH, "records": [record(1)], "x": 1},
    envelope(record(1), ledger="A" * 32),                           # 32 lower-case hex
    envelope(dict(record(1), source={"ledger": LEDGER, "epoch": EPOCH})),               # no ref
    envelope(dict(record(1), source={"ledger": LEDGER, "epoch": EPOCH, "ref": True})),  # a bool is no ref
    envelope("not a record"),
    [record(1)],
    envelope(record(1), record(2), record(1)),                      # r1 F7: the same triple twice
], ids=["epoch", "ledger", "empty", "65", "keys-", "keys+", "hex", "noref", "boolref", "nondict", "list", "dup"])
def test_lineage_envelope_refused(w, env):
    # H6-2 / H6-14 / R4 §3: a malformed envelope is refused WHOLE `lineage-record:envelope` (a keys bug): nothing is
    # appended, not even its good elements
    assert w.append(env) == {"id": 7, "ok": False, "refused": "lineage-record:envelope"}
    assert w.path.read_bytes() == b""
    assert w.sink.next_seq == 1


ENVELOPE_BYTES = 48 * 1024        # H6-19: FRAME_MAX 64 KiB less headroom


def sized_envelope(n_bytes: int, n_records: int = 60) -> dict:
    """An envelope of `n_records` well-formed records whose canonical encoding is exactly `n_bytes`, padded through
    `provider_answered` (printable ASCII up to 128)."""
    recs = [record(i, provider_answered="p") for i in range(n_records)]
    short = n_bytes - len(canon.canonical_bytes(envelope(*recs)))
    assert 0 <= short <= 127 * n_records, short
    for rec in recs:
        pad = min(short, 127)
        rec["provider_answered"] += "p" * pad
        short -= pad
    env = envelope(*recs)
    assert len(canon.canonical_bytes(env)) == n_bytes
    return env


def test_lineage_envelope_byte_bound(w):
    # H6-19: at most 48 KiB encoded beside the 64-record bound; over it the WHOLE envelope is refused
    # `lineage-record:envelope` and nothing is appended (keys never sends one: it packs greedily)
    over = sized_envelope(ENVELOPE_BYTES + 1)
    assert len(over["records"]) <= LN.ENVELOPE_MAX
    assert w.append(over) == {"id": 7, "ok": False, "refused": "lineage-record:envelope"}
    assert w.path.read_bytes() == b"" and w.sink.next_seq == 1
    # exactly at the bound: appended whole
    at = sized_envelope(ENVELOPE_BYTES)
    assert w.append(at) == acked(*range(len(at["records"])))
    assert len(w.rows()) == len(at["records"])
    assert LN.ENVELOPE_BYTES_MAX == ENVELOPE_BYTES


def test_lineage_append_needs_the_attachment(w):
    other = C.Conn(KEYS_UID)                                  # keys, but not the connection holding KEYS-ATTACH
    assert w.call("LINEAGE-APPEND", other, envelope=envelope(record(1)))["refused"] == "not-attached"
    op = C.Conn(OP_UID)
    assert w.call("LINEAGE-APPEND", op, envelope=envelope(record(1)))["refused"] == "verb-not-allowed-for-peer"
    assert w.path.read_bytes() == b""


def test_lineage_no_sink_is_unavailable(tmp):
    (tmp / "s").mkdir()
    control = C.Control(C.StateStore(tmp / "s"), OP_UID, keys_uid=KEYS_UID)
    control.start()
    keys = C.Conn(KEYS_UID)
    assert control.handle(keys, {"v": 1, "op": "KEYS-ATTACH", "id": 1, "generation": None, "item_sha256": None})["ok"]
    ans = control.handle(keys, {"v": 1, "op": "LINEAGE-APPEND", "id": 2, "envelope": envelope(record(1))})
    assert ans["refused"] == "lineage-unavailable"
    # r1 F6: with no sink the answer is `lineage-unavailable` before the envelope is checked, so a malformed one
    # is not counted as a keys bug while CONTROL holds no sink
    ans = control.handle(keys, {"v": 1, "op": "LINEAGE-APPEND", "id": 3, "envelope": envelope()})
    assert ans["refused"] == "lineage-unavailable"
    control.stop()


# --- the CONTROL-crash rows (H6-9: one fault seam, host level) ------------------------------------------------------

def _crash_at(point: str):
    def fault(p: str) -> None:
        if p == point:
            raise Crash(p)
    return fault


def test_lineage_control_crash_after_fsync_before_ack(tmp):
    # crash row "CONTROL after 3, before 4": the rows are durable in the sink, keys never got the ack. The dead CONTROL
    # answers nothing more (poisoned); the restarted one rebuilds membership from the sink and acks keys' resend
    # WITHOUT appending: exactly one row per triple
    w = World(tmp, fault=_crash_at("after_fsync"))
    assert w.append(envelope(record(1), record(2))) == {"id": 7, "ok": False, "refused": "lineage-unavailable"}
    assert w.append(envelope(record(3)))["refused"] == "lineage-unavailable"       # poisoned until restart
    w.restart()
    assert w.append(envelope(record(1), record(2))) == acked(1, 2)
    assert [(r["seq"], r["source"]["ref"]) for r in w.rows()] == [(1, 1), (2, 2)]
    w.control.stop()


@pytest.mark.parametrize("point, lost", [("before_write", False), ("before_fsync", True)],
                         ids=["before-write", "before-fsync-cache-lost"])
def test_lineage_control_crash_before_fsync(tmp, point, lost):
    # crash row "CONTROL after 2, before 3": nothing durable at CONTROL. Before the write nothing is on disk; before
    # the fsync the bytes may be gone with the page cache (a power loss), which the test stands for by cutting them.
    # The resend appends once.
    w = World(tmp)
    assert w.append(envelope(record(1))) == acked(1)
    size = w.path.stat().st_size
    w.sink.fault = _crash_at(point)
    assert w.append(envelope(record(2), record(3)))["refused"] == "lineage-unavailable"
    if lost:
        assert w.path.stat().st_size > size
        os.truncate(w.path, size)
    else:
        assert w.path.stat().st_size == size
    w.restart()
    assert w.append(envelope(record(2), record(3))) == acked(2, 3)
    assert [(r["seq"], r["source"]["ref"]) for r in w.rows()] == [(1, 1), (2, 2), (3, 3)]
    w.control.stop()


def test_lineage_control_crash_before_fsync_bytes_survive(tmp):
    # the same crash with the OS alive: the written rows reach the disk anyway, so the restart reads them as members
    # and the resend is acked by membership. Either way: one row per triple.
    w = World(tmp, fault=_crash_at("before_fsync"))
    assert w.append(envelope(record(1), record(2)))["refused"] == "lineage-unavailable"
    w.restart()
    assert w.append(envelope(record(2), record(1))) == acked(2, 1)
    assert [r["source"]["ref"] for r in w.rows()] == [1, 2]
    w.control.stop()


def test_lineage_torn_tail_dropped_and_resend_appended(tmp):
    # a crash inside the write leaves a torn last line: never acked, so the start cuts it and the resend is appended at
    # the next seq (the complete lines before it are kept as members)
    w = World(tmp)
    assert w.append(envelope(record(1), record(2))) == acked(1, 2)
    w.control.stop()
    whole = w.path.read_bytes()
    third = canon.canonical_bytes(dict(record(3), seq=3)) + b"\n"
    w.path.write_bytes(whole + third[: len(third) // 2])
    w.restart()
    assert w.path.read_bytes() == whole                      # cut at the start, before anything was appended
    assert w.sink.members() == 2 and w.sink.next_seq == 3
    assert w.append(envelope(record(1), record(3))) == acked(1, 3)
    assert [(r["seq"], r["source"]["ref"]) for r in w.rows()] == [(1, 1), (2, 2), (3, 3)]
    w.control.stop()


def test_lineage_membership_rebuilt_from_the_sink(tmp):
    # R4 §3: CONTROL's membership is rebuilt at start from its own sink (never pruned): quarantine rows included, and
    # the next seq follows the last
    w = World(tmp)
    assert w.append(envelope(record(1), record(2, unproven="no"), record(3))) == acked(1, 2, 3,
                                                                                         quarantined=[(2, "unproven")])
    w.restart()
    assert w.sink.members() == 3 and w.sink.next_seq == 4
    assert w.append(envelope(record(3), record(2), record(1), record(4))) == acked(3, 2, 1, 4,
                                                                                   quarantined=[(2, "unproven")])
    assert [(r["seq"], r["source"]["ref"]) for r in w.rows()] == [(1, 1), (2, 2), (3, 3), (4, 4)]
    w.control.stop()


@pytest.mark.parametrize("damage", ["garbage", "gap", "dup", "foreign", "schema", "noncanonical", "quarantine-noncanonical"])
def test_lineage_corrupt_middle_refuses_start(tmp, damage):
    # a line that does not parse, a seq gap, a repeated triple or a row CONTROL does not write, anywhere but a torn
    # tail, refuses the start `lineage-corrupt`: CONTROL never serves a sink it cannot read whole. r1 F4: a full row
    # that fails the record check a fresh record gets (`tokens_in: "corrupt"`), or any line that is not its own
    # canonical encoding, is one CONTROL does not write: refused too, never taken into membership
    w = World(tmp)
    assert w.append(envelope(record(1), record(2))) == acked(1, 2)
    w.control.stop()
    lines = w.path.read_bytes().split(b"\n")[:-1]
    if damage == "garbage":
        lines.insert(1, b"{not json")
    elif damage == "gap":
        lines[1] = canon.canonical_bytes(dict(record(2), seq=3))
    elif damage == "dup":
        lines[1] = canon.canonical_bytes(dict(record(1), seq=2))
    elif damage == "foreign":
        lines.insert(1, canon.canonical_bytes({"format": LN.FORMAT, "seq": 2, "source": triple(9)}))
    elif damage == "schema":
        lines[1] = canon.canonical_bytes(dict(record(2), seq=2, tokens_in="corrupt"))
    elif damage == "noncanonical":
        lines[1] = json.dumps(dict(record(2), seq=2), indent=None).encode()     # ", " and ": ": not canonical
        assert canon.parse(lines[1])["seq"] == 2                                 # it parses: only the bytes differ
    else:
        q = {"format": LN.FORMAT, "seq": 2, "source": triple(2), "quarantined": "tokens_in"}
        lines[1] = json.dumps(q).encode()
        assert canon.parse(lines[1]) == q
    w.path.write_bytes(b"".join(x + b"\n" for x in lines))
    sink = LN.LineageSink(w.path)
    control = C.Control(C.StateStore(tmp / "s"), OP_UID, keys_uid=KEYS_UID, lineage=sink)
    with pytest.raises(Refused) as e:
        control.start()
    assert e.value.name == "lineage-corrupt"
    # the failed start holds nothing: a fixed sink starts in the same process
    w.path.write_bytes(b"")
    control.start()
    control.stop()


@pytest.mark.parametrize("existing", [None, 0o600, 0o644])
def test_lineage_sink_mode_is_set_whatever_the_umask(tmp, existing):
    # r1 F2: under umask 0077 `os.open(..., 0o640)` alone made the sink 0600 (EACCES for _levain_dispatch, group
    # levain-lineage), and an existing wrong-mode sink was never repaired: the start sets SINK_MODE on the open fd
    d = tmp / "lineage"
    d.mkdir()
    if existing is not None:
        (d / LN.SINK).write_bytes(b"")
        os.chmod(d / LN.SINK, existing)
    old = os.umask(0o077)
    try:
        sink = LN.LineageSink(d / LN.SINK)
        sink.start()
        sink.close()
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(d / LN.SINK).st_mode) == LN.SINK_MODE == 0o640


def test_lineage_one_control_per_sink(tmp):
    w = World(tmp)
    with pytest.raises(Refused) as e:
        LN.LineageSink(w.path).start()
    assert e.value.name == "control-running"
    w.control.stop()
    LN.LineageSink(w.path).start()                           # released by the stop


def test_lineage_sink_symlink_refused(tmp):
    (tmp / "lineage").mkdir()
    (tmp / "elsewhere").write_bytes(b"")
    os.symlink(tmp / "elsewhere", tmp / "lineage" / LN.SINK)
    with pytest.raises(Refused) as e:
        LN.LineageSink(tmp / "lineage" / LN.SINK).start()
    assert e.value.name == "lineage-unavailable"


# --- group commit (H6-11) -------------------------------------------------------------------------------------------

def test_lineage_64_record_envelope_is_one_fsync(tmp):
    # H6-11: one LINEAGE-APPEND envelope of up to 64 records is appended with ONE full fsync (and one write), and a
    # resend of it (all members) with none
    calls = []
    w = World(tmp, fsync=lambda fd: calls.append(fd) or LN.full_fsync(fd))
    calls.clear()                                            # the start's own flushes (directory) are not counted
    writes = []
    real_write = os.write

    def counting_write(fd, data):
        writes.append(fd)
        return real_write(fd, data)

    os.write = counting_write
    try:
        ans = w.append(envelope(*[short_record(i) for i in range(LN.ENVELOPE_MAX)]))
        appended = (len(calls), len([x for x in writes if x == w.sink._fd]))
        calls.clear()
        again = w.append(envelope(*[short_record(i) for i in range(LN.ENVELOPE_MAX)]))
    finally:
        os.write = real_write
    assert ans == acked(*range(LN.ENVELOPE_MAX)) and again == ans
    assert appended == (1, 1), appended
    assert calls == []
    assert [r["seq"] for r in w.rows()] == list(range(1, LN.ENVELOPE_MAX + 1))
    w.control.stop()


def test_lineage_failed_fsync_poisons(tmp):
    # tickets.py's rule: any failure while the rows are written or flushed poisons the sink (the rows may be on disk)
    def bad(fd):
        raise OSError(5, "EIO")
    w = World(tmp)
    w.sink._fsync = bad
    assert w.append(envelope(record(1)))["refused"] == "lineage-unavailable"
    w.sink._fsync = LN.full_fsync
    assert w.append(envelope(record(2)))["refused"] == "lineage-unavailable"
    w.restart()
    assert w.append(envelope(record(1), record(2))) == acked(1, 2)
    assert [r["source"]["ref"] for r in w.rows()] == [1, 2]
    w.control.stop()


# --- the row's host half: who can read the sink ---------------------------------------------------------------------

@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_lineage_unreadable_to_slot_and_gw(host, tmp):
    # CONTRACT §2.1 row + H6-4, host half: `L/lineage` is _levain_control:levain-lineage 0o2750 (setgid: the sink CONTROL
    # creates takes levain-lineage on Linux too), the sink 0640, and levain-lineage's only member is _levain_dispatch,
    # so a slot uid and every _levain_gw<n> read EACCES. The RUN with real uids (`sudo -u _levain_s1 cat` = EACCES) is
    # 3c-10's VM residue.
    rows = {r.path: r for r in layout.base_store_rows(host, "op")}
    row = rows[layout.STORE[host] / "lineage"]
    assert (row.owner, row.group, row.mode) == ("_levain_control", "levain-lineage", 0o2750)
    assert "levain-lineage" in SHARED_GROUPS
    members = {n for n in ("_levain_keys", "_levain_export", "_levain_control", "_levain_qbroker", "_levain_dispatch",
                           "_levain_s0", "_levain_s1", "_levain_v0", "_levain_c0", "_levain_gw0", "_levain_gw1")
               if "levain-lineage" in A.supplementary_groups(n)}
    assert members == {"_levain_dispatch"}
    assert "levain-lineage" not in A.OPERATOR_GROUPS
    # the sink as CONTROL creates it: 0640 under the setgid directory, the directory's group
    d = tmp / "lineage"
    d.mkdir()
    os.chmod(d, 0o2750)
    old = os.umask(0o022)
    try:
        sink = LN.LineageSink(d / LN.SINK)
        sink.start()
        sink.close()
    finally:
        os.umask(old)
    st = os.stat(d / LN.SINK)
    assert stat.S_IMODE(st.st_mode) == LN.SINK_MODE == 0o640
    assert st.st_gid == os.stat(d).st_gid
    assert not st.st_mode & 0o007                            # nothing for other
