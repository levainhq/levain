"""levain.voice.stream_session: the stream-session core, on a fake provider stream, a real tmp ledger and a fake clock.

No network, no key store, no real time."""
from __future__ import annotations

import logging

import pytest

from levain.voice import custody
from levain.voice import meter as vmeter
from levain.voice import stream_session as ss
from levain.voice.stream_session import (BYTES_PER_SECOND, StreamRefused, StreamRegistry, StreamUpdate)

PCM_1S = b"\x01\x00" * 16_000          # exactly one second
SECRET_VALUE = "dg-test-key-0123456789abcdef"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class FakeStream:
    def __init__(self, *, fail_send=False, fail_close=False, text="hello world", partial="hel") -> None:
        self.sent: list[bytes] = []
        self.closed = 0
        self.fail_send, self.fail_close = fail_send, fail_close
        self.text, self.partial = text, partial
        self.failed = False

    def send_audio(self, pcm: bytes) -> None:
        if self.fail_send:
            raise RuntimeError("provider down: SPOKEN-SECRET-TEXT")
        self.sent.append(pcm)

    def poll(self) -> StreamUpdate:
        return StreamUpdate(partial=self.partial, final="", failed=self.failed)

    def close(self) -> str:
        self.closed += 1
        if self.fail_close:
            raise RuntimeError("close failed")
        return self.text


class Rig:
    def __init__(self, tmp_path, **kw) -> None:
        self.clock = Clock()
        self.meter = vmeter.Meter(tmp_path / "voice.db")
        self.registry = StreamRegistry(self.meter, clock=self.clock, **kw)
        self.streams: list[FakeStream] = []
        self.revealed: list[str] = []
        self.tmp = tmp_path

    def open(self, owner="owner-a", *, stream=None, read_key=None, factory=None):
        def default_read_key(provider, use, *, authority):
            assert (provider, use) == ("deepgram", "stt")
            return custody.Secret(SECRET_VALUE)

        def default_factory(secret):
            self.revealed.append(secret.reveal())
            s = stream or FakeStream()
            self.streams.append(s)
            return s

        return self.registry.open(owner, "deepgram", "nova-3", data_root=self.tmp,
                                  read_key=read_key or default_read_key, factory=factory or default_factory)

    def rows(self):
        return self.meter.ledger()


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def refused(code):
    return pytest.raises(StreamRefused, match=code)


# --- meter: settle with actual seconds -------------------------------------------------------------------------------

def test_settle_actual_lowers_a_reservation_to_the_seconds_streamed(tmp_path):
    meter = vmeter.Meter(tmp_path / "v.db")
    check = meter.reserve("deepgram", "stt", "nova-3", 120)
    reserved = meter.ledger()[0].cost_micros
    assert meter.settle_actual(check.reservation_id, 7)
    [row] = meter.ledger()
    assert (row.status, row.units) == ("ok", 7)
    assert row.cost_micros == vmeter.DEFAULT_PRICES.find("deepgram", "stt", "nova-3").cost(7) < reserved
    assert not meter.settle_actual(check.reservation_id, 7)         # ended once


def test_settle_actual_never_raises_a_reservation_and_prices_at_least_one_unit(tmp_path):
    meter = vmeter.Meter(tmp_path / "v.db")
    a = meter.reserve("deepgram", "stt", "nova-3", 10)
    cost = meter.ledger()[0].cost_micros
    meter.settle_actual(a.reservation_id, 9999)
    by_id = {r.id: r for r in meter.ledger()}
    assert by_id[a.reservation_id].units == 10 and by_id[a.reservation_id].cost_micros == cost
    b = meter.reserve("deepgram", "stt", "nova-3", 10)
    meter.settle_actual(b.reservation_id, 0)
    assert {r.id: r for r in meter.ledger()}[b.reservation_id].units == 1
    with pytest.raises(vmeter.MeterError):
        meter.settle_actual(b.reservation_id, -1)


# --- sequence --------------------------------------------------------------------------------------------------------

def test_seq_must_start_at_zero_and_rise_by_one(rig):
    s = rig.open()
    with refused("bad_sequence"):
        s.feed(1, PCM_1S[:3200])              # gap at the start
    s.feed(0, PCM_1S[:3200])
    with refused("bad_sequence"):
        s.feed(0, PCM_1S[:3200])              # duplicate
    with refused("bad_sequence"):
        s.feed(2, PCM_1S[:3200])              # gap
    s.feed(1, PCM_1S[:3200])
    with refused("bad_sequence"):
        s.feed(0, PCM_1S[:3200])              # replay of an old one
    assert s.streamed_bytes == 6400 and len(rig.streams[0].sent) == 2
    assert not s.closed                       # a refused chunk does not end the session


def test_a_non_int_seq_is_refused(rig):
    s = rig.open()
    for bad in (True, "0", 0.0, None):
        with refused("bad_sequence"):
            s.feed(bad, PCM_1S[:320])


def test_bad_chunks_are_refused(rig):
    s = rig.open()
    for bad in (b"", b"\x00" * 3, b"\x00" * (ss.MAX_CHUNK_BYTES + 2), "text", None):
        with refused("bad_chunk"):
            s.feed(0, bad)
    assert s.streamed_bytes == 0


# --- limits ----------------------------------------------------------------------------------------------------------

def test_rate_limit_ends_a_session_that_outruns_the_clock(rig):
    s = rig.open()
    chunk = b"\x00\x00" * (ss.MAX_CHUNK_BYTES // 2)             # 2.048 s per chunk
    seq = 0
    with refused("too_fast"):
        while True:
            s.feed(seq, chunk)
            seq += 1
    assert s.closed
    # 4 s burst * 2 = 8 s of audio allowed at t=0: 3 chunks of 2.048 s fit, the 4th does not
    assert seq == 3


def test_a_stalled_phone_may_flush_within_the_rate_allowance(rig):
    s = rig.open()
    rig.clock.t += 5
    chunk = b"\x00\x00" * (ss.MAX_CHUNK_BYTES // 2)
    for i in range(6):                                           # 12.3 s <= (5+4)*2 = 18 s
        s.feed(i, chunk)
    assert not s.closed


def test_ceiling_ends_the_session_and_settles_at_120_s(rig):
    s = rig.open()
    seq = 0
    chunk = PCM_1S * 2
    with refused("ceiling"):
        while True:
            rig.clock.t += 2                                       # real-time pace
            s.feed(seq, chunk)
            seq += 1
    assert s.streamed_bytes == ss.CEILING_SECONDS * BYTES_PER_SECOND
    [row] = rig.rows()
    assert (row.status, row.units) == ("ok", 120)


def test_buffer_is_bounded_and_dropped_at_end(rig):
    s = rig.open()
    s.feed(0, PCM_1S[:6400])
    assert len(s.take_audio()) == 6400
    assert len(s.take_audio()) <= ss.CEILING_SECONDS * BYTES_PER_SECOND
    s.close()
    assert s.take_audio() == b""


# --- registry: cap, owner, ids ---------------------------------------------------------------------------------------

def test_hard_cap_on_concurrent_sessions_and_slot_returns_on_close(tmp_path):
    rig = Rig(tmp_path, max_sessions=2)
    a, b = rig.open(), rig.open()
    with refused("too_many_sessions"):
        rig.open()
    assert len(rig.rows()) == 2                                  # the refused open reserved nothing
    a.close()
    c = rig.open()
    assert len(rig.registry) == 2
    b.close(); c.close()


def test_owner_binding_and_unknown_look_the_same(rig):
    s = rig.open("owner-a")
    assert rig.registry.get(s.sid, "owner-a") is s
    with refused("unknown_session"):
        rig.registry.get(s.sid, "owner-b")
    with refused("unknown_session"):
        rig.registry.get("nope", "owner-a")
    with refused("unknown_session"):
        rig.registry.get(None, "owner-a")
    with refused("unknown_session"):
        rig.open("")


def test_ids_are_unguessable_and_distinct(tmp_path):
    rig = Rig(tmp_path, max_sessions=40)
    sids = {rig.open().sid for _ in range(30)}
    assert len(sids) == 30 and all(len(x) >= 32 for x in sids)


def test_an_ended_session_is_no_longer_addressable(rig):
    s = rig.open()
    s.close()
    with refused("unknown_session"):
        rig.registry.get(s.sid, "owner-a")
    with refused("closed"):
        s.feed(0, PCM_1S[:320])


# --- settle on every exit path ---------------------------------------------------------------------------------------

def test_open_reserves_the_ceiling(rig):
    rig.open()
    [row] = rig.rows()
    assert (row.status, row.use, row.units) == ("reserved", "stt", 120)


def test_close_settles_exact_seconds(rig):
    s = rig.open()
    for i in range(3):
        rig.clock.t += 1
        s.feed(i, PCM_1S)
    assert s.close() == "hello world"
    [row] = rig.rows()
    assert (row.status, row.units) == ("ok", 3)
    assert s.close() == "hello world"                            # idempotent
    assert rig.streams[0].closed == 1 and len(rig.rows()) == 1


def test_partial_second_rounds_up(rig):
    s = rig.open()
    s.feed(0, PCM_1S + b"\x00\x00")                               # 1.0000625 s
    s.close()
    assert rig.rows()[0].units == 2


def test_idle_timeout_on_reap_settles_actual_seconds(rig):
    s = rig.open()
    rig.clock.t += 1
    s.feed(0, PCM_1S)
    rig.clock.t += ss.IDLE_TIMEOUT_S + 1
    assert rig.registry.reap() == 1
    assert s.closed and rig.streams[0].closed == 1
    [row] = rig.rows()
    assert (row.status, row.units) == ("ok", 1)
    assert len(rig.registry) == 0


def test_idle_timeout_detected_at_feed_refuses_and_settles(rig):
    s = rig.open()
    s.feed(0, PCM_1S[:32_000])
    rig.clock.t += ss.IDLE_TIMEOUT_S + 1
    with refused("idle_timeout"):
        s.feed(1, PCM_1S[:320])
    assert rig.rows()[0].status == "ok" and rig.rows()[0].units == 1


def test_idle_timeout_detected_at_mailbox_read(rig):
    s = rig.open()
    s.feed(0, PCM_1S[:320])
    rig.clock.t += ss.IDLE_TIMEOUT_S + 1
    with refused("idle_timeout"):
        s.mailbox()
    assert rig.rows()[0].status == "ok"


def test_provider_send_failure_settles_the_audio_that_went(tmp_path):
    rig = Rig(tmp_path)
    s = rig.open(stream=FakeStream(fail_send=True))
    with refused("provider_failed"):
        s.feed(0, PCM_1S)                                         # counted before the send: it may have been billed
    assert s.closed
    [row] = rig.rows()
    assert (row.status, row.units) == ("ok", 1)


def test_provider_reported_failure_on_poll_settles(tmp_path):
    rig = Rig(tmp_path)
    fake = FakeStream()
    s = rig.open(stream=fake)
    s.feed(0, PCM_1S[:16_000])
    fake.failed = True
    with refused("provider_failed"):
        s.feed(1, PCM_1S[:16_000])
    assert rig.rows()[0].status == "ok" and rig.rows()[0].units == 1


def test_failed_provider_close_still_settles(tmp_path):
    rig = Rig(tmp_path)
    s = rig.open(stream=FakeStream(fail_close=True))
    s.feed(0, PCM_1S)
    assert s.close() == ""
    assert rig.rows()[0].status == "ok"


def test_shutdown_settles_every_session(rig):
    a, b = rig.open(), rig.open()
    a.feed(0, PCM_1S)
    rig.registry.shutdown()
    assert a.closed and b.closed and len(rig.registry) == 0
    got = sorted((r.status, r.units) for r in rig.rows())
    assert got == [("ok", 1), ("released", 120)]                  # b streamed nothing: it costs nothing


def test_a_session_that_streamed_nothing_is_released(rig):
    s = rig.open()
    s.close()
    [row] = rig.rows()
    assert (row.status, row.cost_micros) == ("released", 0)


# --- open failures give everything back ------------------------------------------------------------------------------

def test_key_unavailable_releases_the_reservation(rig):
    def no_key(provider, use, *, authority):
        raise custody.VoiceKeyUnavailable("not_set", "no key")

    with pytest.raises(StreamRefused) as e:
        rig.open(read_key=no_key)
    assert (e.value.code, e.value.key_code) == ("key_unavailable", "not_set")
    assert [r.status for r in rig.rows()] == ["released"] and len(rig.registry) == 0


def test_unexpected_key_reader_fault_and_factory_fault_release(rig):
    def boom(provider, use, *, authority):
        raise OSError("keychain")

    with refused("key_unavailable"):
        rig.open(read_key=boom)

    def bad_factory(secret):
        raise ConnectionError("no route")

    with refused("provider_failed"):
        rig.open(factory=bad_factory)
    assert [r.status for r in rig.rows()] == ["released", "released"] and len(rig.registry) == 0
    rig.open()                                                    # the slot came back


def test_cap_refusal_reads_no_key(tmp_path):
    rig = Rig(tmp_path)
    rig.meter.caps = vmeter.Caps(month_micros=0, day_micros=0)
    called = []

    def read_key(provider, use, *, authority):
        called.append(1)
        return custody.Secret(SECRET_VALUE)

    with refused("month_cap"):
        rig.open(read_key=read_key)
    assert not called and rig.rows() == []


def test_unusable_meter_refuses(rig, monkeypatch):
    def boom(*a, **k):
        raise vmeter.MeterError("x")

    monkeypatch.setattr(rig.meter, "reserve", boom)
    with refused("meter_unavailable"):
        rig.open()


# --- the key and the log ---------------------------------------------------------------------------------------------

def test_the_key_goes_only_to_the_factory_and_is_no_attribute_of_the_session(rig):
    s = rig.open()
    assert rig.revealed == [SECRET_VALUE]
    seen = [s, rig.registry, s._stream]
    for obj in seen:
        for v in vars(obj).values():
            assert SECRET_VALUE not in repr(v)
            assert not isinstance(v, custody.Secret)


def test_mailbox_carries_partial_and_final(rig):
    s = rig.open()
    up = s.feed(0, PCM_1S[:3200])
    assert up.partial == "hel"
    assert s.mailbox().partial == "hel"
    s.close()
    assert s.mailbox() == StreamUpdate(partial="", final="hello world")


def test_nothing_spoken_or_secret_is_logged(rig, caplog):
    caplog.set_level(logging.DEBUG, logger="levain.voice.stream_session")
    s = rig.open(stream=FakeStream(fail_send=True, text="SPOKEN-SECRET-TEXT"))
    with refused("provider_failed"):
        s.feed(0, b"\x07\x00" * 16_000)
    t = rig.open(stream=FakeStream(text="SPOKEN-SECRET-TEXT"))
    t.feed(0, b"\x07\x00" * 800)
    t.close()
    assert caplog.records
    blob = " ".join(r.getMessage() + repr(r.args) + str(r.exc_info) for r in caplog.records)
    for forbidden in ("SPOKEN-SECRET-TEXT", SECRET_VALUE, "\x07", "hello world"):
        assert forbidden not in blob
