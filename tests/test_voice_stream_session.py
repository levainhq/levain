"""levain.voice.stream_session: the stream-session core, on a fake provider stream, a real tmp ledger and a fake clock.

No network, no key store, no real time."""
from __future__ import annotations

import logging
import threading
import traceback

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
    def __init__(self, *, fail_send=False, fail_close=False, text="hello world", partial="hel", poll=None) -> None:
        self.sent: list[bytes] = []
        self.closed = 0
        self.fail_send, self.fail_close = fail_send, fail_close
        self.text, self.partial = text, partial
        self.failed = False
        self.poll_fn = poll

    def send_audio(self, pcm: bytes) -> None:
        if self.fail_send:
            raise RuntimeError("provider down: SPOKEN-SECRET-TEXT")
        self.sent.append(pcm)

    def poll(self) -> StreamUpdate:
        if self.poll_fn is not None:
            return self.poll_fn()
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


def test_buffer_is_bounded_and_taken_once_after_close(rig):
    s = rig.open()
    s.feed(0, PCM_1S[:6400])
    assert len(s.take_audio()) == 6400
    assert len(s.take_audio()) == 6400                          # while live, a look leaves it in place
    s.close()
    assert s.take_audio() == PCM_1S[:6400]                     # the stop-time fallback still has it
    assert s.take_audio() == b""                               # taken once after the end, then dropped


def test_the_audio_survives_an_auto_end_for_the_stop_time_fallback(tmp_path):
    rig = Rig(tmp_path)
    fake = FakeStream(text="")
    s = rig.open(stream=fake)
    s.feed(0, PCM_1S[:3200])
    fake.failed = True
    with refused("provider_failed"):
        s.feed(1, PCM_1S[:3200])
    assert s.closed
    assert s.take_audio() == PCM_1S[:3200] * 2
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
    with refused("provider_failed"):          # was `== ""`: a close that did not finish now refuses (L3 4c r1 F2)
        s.close()
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

    def not_started(secret):
        raise ss.StreamNotStarted()

    with refused("provider_failed"):
        rig.open(factory=not_started)
    assert [r.status for r in rig.rows()] == ["released", "released"] and len(rig.registry) == 0
    rig.open()                                                    # the slot came back


def test_an_untyped_factory_failure_settles_one_unit(rig):
    def bad_factory(secret):
        raise ConnectionError("no route")                         # the handshake may have happened

    with refused("provider_failed"):
        rig.open(factory=bad_factory)
    with refused("provider_failed"):
        rig.open(factory=lambda secret: None)                     # not a provider stream at all
    assert [(r.status, r.units) for r in rig.rows()] == [("ok", 1), ("ok", 1)]
    assert len(rig.registry) == 0


@pytest.mark.parametrize("break_at", ["token", "session"])
def test_a_setup_failure_after_the_factory_closes_the_stream(rig, monkeypatch, break_at):
    def boom(*a, **k):
        raise RuntimeError("setup")

    if break_at == "token":
        monkeypatch.setattr(ss.secrets, "token_urlsafe", boom)
    else:
        monkeypatch.setattr(ss, "StreamSession", boom)
    with refused("internal"):
        rig.open()
    assert rig.streams[0].closed == 1                              # the provider socket does not outlive the refusal
    assert [r.status for r in rig.rows()] == ["released"]          # nothing was sent on it
    assert len(rig.registry) == 0


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
    assert s.mailbox() == StreamUpdate(partial="", final="hello world", ended="closed")


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


# --- exception chains carry nothing secret or spoken ----------------------------------------------------------------

SPOKEN = "SPOKEN-SECRET-TEXT"


def assert_clean(exc: BaseException) -> None:
    """No chained exception, and no frame of the traceback holds the key or the spoken text in a local."""
    assert exc.__context__ is None and exc.__cause__ is None
    blob = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) + str(exc) + repr(exc)
    tb = exc.__traceback__
    while tb is not None:
        for v in tb.tb_frame.f_locals.values():
            assert not isinstance(v, custody.Secret)
            blob += repr(v)
        tb = tb.tb_next
    for forbidden in (SECRET_VALUE, SPOKEN):
        assert forbidden not in blob


def test_a_failing_factory_leaves_no_secret_in_the_refusal(rig):
    def leaky_factory(secret):
        key = secret.reveal()
        raise ConnectionError(f"handshake refused for {key}")

    with pytest.raises(StreamRefused) as e:
        rig.open(factory=leaky_factory)
    assert_clean(e.value)


def test_a_failing_key_reader_leaves_nothing_chained(rig):
    def leaky(provider, use, *, authority):
        raise OSError(f"keychain said {SECRET_VALUE}")

    with pytest.raises(StreamRefused) as e:
        rig.open(read_key=leaky)
    assert_clean(e.value)


def test_provider_faults_leave_no_spoken_text_in_the_refusal(tmp_path):
    rig = Rig(tmp_path)
    s = rig.open(stream=FakeStream(fail_send=True))
    with pytest.raises(StreamRefused) as e:
        s.feed(0, PCM_1S[:320])
    assert_clean(e.value)

    def poll_raises():
        raise RuntimeError(f"bad frame: {SPOKEN}")

    s = rig.open(stream=FakeStream(poll=poll_raises))
    with pytest.raises(StreamRefused) as e:
        s.feed(0, PCM_1S[:320])
    assert_clean(e.value)

    s = rig.open(stream=FakeStream(poll=lambda: StreamUpdate(partial=SPOKEN, final=SPOKEN, failed=True), text=""))
    with pytest.raises(StreamRefused) as e:
        s.mailbox()
    assert_clean(e.value)


# --- shutdown, and opens in progress ---------------------------------------------------------------------------------

def test_shutdown_during_an_open_ends_the_new_stream_and_releases(rig):
    def factory(secret):
        rig.registry.shutdown()                                   # the server stops while the provider connects
        s = FakeStream()
        rig.streams.append(s)
        return s

    with refused("stopping"):
        rig.open(factory=factory)
    assert rig.streams[0].closed == 1
    assert [r.status for r in rig.rows()] == ["released"] and len(rig.registry) == 0
    with refused("stopping"):
        rig.open()
    assert len(rig.rows()) == 1                                   # a refused open reserves nothing


def test_shutdown_waits_for_an_open_in_another_thread_to_end_its_reservation(rig):
    """S3 L3 r1 (codex 2): an open holding a reserved row inside the factory when the server stops. shutdown must not
    return before that open has ended its reservation (the process may exit right after). Red with: the bounded wait in
    StreamRegistry.shutdown removed, or the slot given back before the reservation is ended in _build's finally."""
    entered, go = threading.Event(), threading.Event()
    outcome: dict = {}

    def factory(secret):
        entered.set()
        go.wait(5)
        s = FakeStream()
        rig.streams.append(s)
        return s

    def opener():
        try:
            rig.open(factory=factory)
            outcome["got"] = "opened"
        except StreamRefused as exc:
            outcome["got"] = exc.code

    t = threading.Thread(target=opener)
    t.start()
    assert entered.wait(5)
    assert [r.status for r in rig.rows()] == ["reserved"]
    threading.Timer(0.2, go.set).start()
    rig.registry.shutdown()
    assert [r.status for r in rig.rows()] == ["released"]          # ended before shutdown returned
    t.join(5)
    assert outcome["got"] == "stopping" and rig.streams[0].closed == 1


def test_shutdown_wait_is_bounded(tmp_path):
    """A stuck open never hangs the server's close: shutdown gives up after ``open_wait``. Red with: an unbounded wait."""
    import time as _time

    r = Rig(tmp_path, open_wait=0.2)
    entered, go = threading.Event(), threading.Event()

    def factory(secret):
        entered.set()
        go.wait(5)
        return FakeStream()

    t = threading.Thread(target=lambda: pytest.raises(StreamRefused, r.open, factory=factory))
    t.start()
    assert entered.wait(5)
    t0 = _time.monotonic()
    r.registry.shutdown()
    assert _time.monotonic() - t0 < 2.0
    go.set()
    t.join(5)
    assert [row.status for row in r.rows()] == ["released"]          # the late open still ends its own reservation


def test_shutdown_drops_ended_sessions_too(rig):
    s = rig.open()
    s.feed(0, PCM_1S[:3200])
    rig.clock.t += ss.IDLE_TIMEOUT_S + 1
    rig.registry.reap()
    rig.registry.shutdown()
    with refused("unknown_session"):
        rig.registry.get(s.sid, "owner-a")
    assert s.take_audio() == b""


def test_an_open_in_progress_holds_its_slot(tmp_path):
    rig = Rig(tmp_path, max_sessions=1)
    inner = []

    def factory(secret):
        with pytest.raises(StreamRefused) as e:
            rig.open()                                            # the first open's slot is taken while it connects
        inner.append(e.value.code)
        return FakeStream()

    s = rig.open(factory=factory)
    assert inner == ["too_many_sessions"]
    s.close()
    rig.open().close()                                            # counted once, given back once


# --- ended sessions stay readable for a while ------------------------------------------------------------------------

def test_an_auto_ended_session_stays_readable_by_its_owner_with_its_reason(rig):
    s = rig.open()
    s.feed(0, PCM_1S[:3200])
    rig.clock.t += ss.IDLE_TIMEOUT_S + 1
    assert rig.registry.reap() == 1
    got = rig.registry.get(s.sid, "owner-a")
    assert got is s and len(rig.registry) == 0                    # addressable, but no longer a live slot
    assert got.mailbox() == StreamUpdate(partial="", final="hello world", ended="idle_timeout")
    with refused("unknown_session"):
        rig.registry.get(s.sid, "owner-b")
    assert s.close() == "hello world"                             # the owner collects it ...
    with refused("unknown_session"):
        rig.registry.get(s.sid, "owner-a")                        # ... and then it is gone


def test_an_ended_session_is_dropped_after_the_retention_time(rig):
    s = rig.open()
    s.feed(0, PCM_1S[:3200])
    with refused("bad_chunk"):
        s.feed(1, b"")
    rig.clock.t += ss.IDLE_TIMEOUT_S + 1
    with refused("idle_timeout"):
        s.feed(1, PCM_1S[:320])
    rig.registry.get(s.sid, "owner-a")
    rig.clock.t += ss.ENDED_RETAIN_S + 1
    with refused("unknown_session"):
        rig.registry.get(s.sid, "owner-a")
    assert s.take_audio() == b"" and s._final == ""               # dropped: its audio and text go with it
    with refused("unknown_session"):                               # was `mailbox().final == ""` (L3 4c r1 F3)
        s.mailbox()


def test_retained_ended_sessions_are_capped_oldest_first(tmp_path):
    rig = Rig(tmp_path, max_sessions=2)
    ended = []
    for _ in range(3):
        s = rig.open()
        s.feed(0, PCM_1S[:320])
        rig.clock.t += ss.IDLE_TIMEOUT_S + 1
        rig.registry.reap()
        ended.append(s)
    with refused("unknown_session"):
        rig.registry.get(ended[0].sid, "owner-a")
    assert [rig.registry.get(s.sid, "owner-a") for s in ended[1:]] == ended[1:]
    rig.open(); rig.open()                                        # ended sessions hold no live slot


# --- intake hardening ------------------------------------------------------------------------------------------------

def test_a_buffer_whose_len_lies_is_measured_by_its_bytes(rig):
    class Liar(bytearray):
        def __len__(self):
            return 2

    class Swapper(bytes):
        def __bytes__(self):
            return b"\x09\x00" * 4

    s = rig.open()
    with refused("bad_chunk"):
        s.feed(0, Liar(b"\x00" * (ss.MAX_CHUNK_BYTES + 2)))
    s.feed(0, Liar(b"\x01\x00" * 1600))
    s.feed(1, Swapper(b"\x02\x00" * 1600))
    assert s.streamed_bytes == 6400
    assert rig.streams[0].sent == [b"\x01\x00" * 1600, b"\x02\x00" * 1600]
    assert all(type(x) is bytes for x in rig.streams[0].sent)


@pytest.mark.parametrize("result", [None, {"partial": "x"}, StreamUpdate(partial=None), StreamUpdate(failed="no")])
def test_a_malformed_poll_result_ends_and_settles(tmp_path, result):
    rig = Rig(tmp_path)
    s = rig.open(stream=FakeStream(poll=lambda: result))
    with refused("provider_failed"):
        s.feed(0, PCM_1S[:16_000])
    assert s.closed
    [row] = rig.rows()
    assert (row.status, row.units) == ("ok", 1)


def test_a_failed_update_does_not_set_the_final_text(tmp_path):
    rig = Rig(tmp_path)
    s = rig.open(stream=FakeStream(poll=lambda: StreamUpdate(final="not-final", failed=True), text=""))
    with refused("provider_failed"):
        s.feed(0, PCM_1S[:320])
    assert s.mailbox().final == ""


def test_an_absolute_age_ends_a_trickling_session(rig):
    s = rig.open()
    seq = 0
    with refused("ceiling"):
        while True:
            rig.clock.t += ss.IDLE_TIMEOUT_S - 1                  # never idle ...
            s.feed(seq, b"\x00\x00")                              # ... and nowhere near the byte ceiling
            seq += 1
    assert s.closed and rig.clock.t - 1000.0 > ss.MAX_AGE_S
    assert rig.rows()[0].status == "ok"


def test_the_age_limit_holds_at_mailbox_and_reap(tmp_path):
    rig = Rig(tmp_path, max_age=30.0)
    a, b = rig.open(), rig.open()
    for t in range(1, 4):
        rig.clock.t += 10
        a.feed(t - 1, b"\x00\x00")
        b.feed(t - 1, b"\x00\x00")
    rig.clock.t += 1
    with refused("ceiling"):
        a.mailbox()
    assert rig.registry.reap() == 1 and b.closed
    assert b.mailbox().ended == "ceiling"


# --- reap and settle -------------------------------------------------------------------------------------------------

def test_reap_rechecks_idleness_under_the_session_lock(rig, monkeypatch):
    s = rig.open()
    s.feed(0, PCM_1S[:320])
    real_limit, held = s._limit, []

    def limit(now):
        held.append(s._lock._is_owned())
        return real_limit(now)

    monkeypatch.setattr(s, "_limit", limit)
    monkeypatch.setattr(s, "expired", lambda: True)               # a stale outside read says idle; it is not
    assert rig.registry.reap() == 0 and not s.closed              # ended only on a limit read under the lock
    rig.clock.t += ss.IDLE_TIMEOUT_S + 1
    assert rig.registry.reap() == 1 and s.end_reason == "idle_timeout"
    assert held and all(held)


def test_reap_skips_a_session_busy_in_a_provider_call(rig):
    gate, entered = threading.Event(), threading.Event()

    class Hung(FakeStream):
        def send_audio(self, pcm):
            entered.set()
            gate.wait(10)
            super().send_audio(pcm)

    busy = rig.open(stream=Hung())
    feeder = threading.Thread(target=busy.feed, args=(0, PCM_1S[:320]))
    feeder.start()
    try:
        assert entered.wait(5)
        rig.clock.t += ss.IDLE_TIMEOUT_S + 1                      # idle by the clock, but mid-call: not idle
        result = []
        reaper = threading.Thread(target=lambda: result.append(rig.registry.reap()))
        reaper.start()
        reaper.join(2)
        assert not reaper.is_alive() and result == [0]            # one hung provider never blocks reap (or open)
        assert not busy.closed
    finally:
        gate.set()
        feeder.join(5)
    assert rig.registry.reap() == 1 and busy.closed


def test_closing_a_live_session_never_evicts_a_retained_ended_one(tmp_path):
    rig = Rig(tmp_path, max_sessions=2)
    ended = []
    for _ in range(2):
        s = rig.open()
        s.feed(0, PCM_1S[:320])
        rig.clock.t += ss.IDLE_TIMEOUT_S + 1
        rig.registry.reap()
        ended.append(s)
    live = rig.open()
    live.close()
    assert [rig.registry.get(s.sid, "owner-a") for s in ended] == ended
    with refused("unknown_session"):
        rig.registry.get(live.sid, "owner-a")


def test_a_full_settle_backlog_refuses_opens_and_forgets_nothing(rig, monkeypatch):
    monkeypatch.setattr(ss, "MAX_UNSETTLED", 2)
    real = rig.meter.settle_actual

    def locked(*a):
        raise vmeter.MeterError("locked")

    monkeypatch.setattr(rig.meter, "settle_actual", locked)
    sessions = []
    for _ in range(3):
        s = rig.open()
        s.feed(0, PCM_1S)
        sessions.append(s)
    for s in sessions:
        s.close()
    with refused("settle_backlog"):                               # an inconsistent ledger stops spending
        rig.open()
    assert [r.status for r in rig.rows()] == ["reserved"] * 3
    monkeypatch.setattr(rig.meter, "settle_actual", real)
    fresh = rig.open()                                            # the reap drained the backlog, oldest first
    by_id = sorted(rig.rows(), key=lambda r: r.id)
    assert [(r.status, r.units) for r in by_id] == [("ok", 1)] * 3 + [("reserved", 120)]
    assert fresh.streamed_bytes == 0


def test_a_factory_result_that_is_not_a_stream_is_still_closed(rig):
    class HalfStream:
        closed = 0

        def close(self):
            HalfStream.closed += 1

    with refused("provider_failed"):
        rig.open(factory=lambda secret: HalfStream())
    assert HalfStream.closed == 1
    assert [(r.status, r.units) for r in rig.rows()] == [("ok", 1)]
    assert len(rig.registry) == 0


def test_an_int_subclass_seq_is_refused(rig):
    class Liar(int):
        def __ne__(self, other):
            return False

    s = rig.open()
    with refused("bad_sequence"):
        s.feed(Liar(5), PCM_1S[:320])
    assert s.streamed_bytes == 0


def test_a_failed_settle_is_retried_then_handed_to_the_registry(rig, monkeypatch):
    s = rig.open()
    s.feed(0, PCM_1S)
    real, calls = rig.meter.settle_actual, []

    def flaky(*a):
        calls.append(a)
        raise vmeter.MeterError("locked")

    monkeypatch.setattr(rig.meter, "settle_actual", flaky)
    assert s.close() == "hello world"                             # never raises past the caller
    assert len(calls) == 2                                        # tried, then retried once
    assert rig.rows()[0].status == "reserved"                     # still counted: errs toward the cap
    monkeypatch.setattr(rig.meter, "settle_actual", real)
    rig.registry.reap()
    [row] = rig.rows()
    assert (row.status, row.units) == ("ok", 1)


# --- owner binding edge ----------------------------------------------------------------------------------------------

def test_an_owner_with_a_lone_surrogate_is_compared_not_crashed(rig):
    s = rig.open("owner-\udc80")
    assert rig.registry.get(s.sid, "owner-\udc80") is s
    for other in ("owner-\udc81", "owner-?", "owner-a", None, 7):
        with refused("unknown_session"):
            rig.registry.get(s.sid, other)


# --- L3 batch4c r1 fixes ----------------------------------------------------------------------------------------------

class ClosePollFails(FakeStream):
    """close() returns text, but the adapter reports failed afterwards (OpenAI's final commit timed out; Deepgram sent no
    closing metadata): both real adapters say so through poll().failed."""

    def poll(self) -> StreamUpdate:
        return StreamUpdate(partial=self.partial, final="", failed=bool(self.closed))


def test_a_provider_close_that_did_not_finish_refuses_and_still_settles(tmp_path):
    """codex F2. Red with: `_end` not reading the post-close poll (close() returns the partial text)."""
    rig = Rig(tmp_path)
    s = rig.open(stream=ClosePollFails())
    s.feed(0, PCM_1S)
    with refused("provider_failed"):
        s.close()
    [row] = rig.rows()
    assert (row.status, row.units) == ("ok", 1)
    with refused("unknown_session"):
        rig.registry.get(s.sid, "owner-a")                         # collected all the same


def test_a_provider_close_that_raises_refuses_and_still_settles(tmp_path):
    rig = Rig(tmp_path)
    s = rig.open(stream=FakeStream(fail_close=True))
    s.feed(0, PCM_1S)
    with refused("provider_failed"):
        s.close()
    assert rig.rows()[0].status == "ok"


def test_an_auto_ended_session_whose_close_failed_refuses_its_owners_close(rig):
    s = rig.open(stream=ClosePollFails())
    s.feed(0, PCM_1S)
    rig.clock.t += ss.IDLE_TIMEOUT_S + 1
    assert rig.registry.reap() == 1
    with refused("provider_failed"):
        s.close()


def test_a_clean_close_still_returns_the_text(rig):
    s = rig.open()
    s.feed(0, PCM_1S)
    assert s.close() == "hello world"


def _two_ended(tmp_path):
    """max_sessions=1: A ends (retained), then B ends and evicts A."""
    rig = Rig(tmp_path, max_sessions=1)
    a = rig.open()
    a.feed(0, PCM_1S)
    rig.clock.t += ss.IDLE_TIMEOUT_S + 1
    assert rig.registry.reap() == 1
    return rig, a


def test_an_evicted_session_is_not_collected_as_an_empty_success(tmp_path):
    """codex F3. A's owner got A from the registry; B's end evicts A before A.close() runs. Red with: close() on a
    dropped session returning '' (a 200 with empty text, the page sends nothing and skips recovery)."""
    rig, a = _two_ended(tmp_path)
    b = rig.open()
    b.feed(0, PCM_1S)
    rig.clock.t += ss.IDLE_TIMEOUT_S + 1
    assert rig.registry.reap() == 1                               # B ends; A is evicted
    with refused("unknown_session"):
        a.close()
    assert a.take_audio() == b""


def test_an_evicted_session_held_by_a_caller_is_not_cleared_under_it(tmp_path):
    """codex F3. A caller holds A's lock (a close in flight) while B's end evicts A: A's text and audio are not cleared
    under it, and are cleared once it lets go. Red with: `_discard` rebinding without the session lock."""
    rig, a = _two_ended(tmp_path)
    b = rig.open()
    b.feed(0, PCM_1S)
    held, release, seen = threading.Event(), threading.Event(), {}

    def in_flight():
        with a._lock:
            held.set()
            release.wait(5)
            seen["final"], seen["audio"] = a._final, len(a._buffer)

    t = threading.Thread(target=in_flight)
    t.start()
    assert held.wait(5)
    rig.clock.t += ss.IDLE_TIMEOUT_S + 1
    assert rig.registry.reap() == 1                               # B ends and evicts A while A is held
    release.set()
    t.join(5)
    assert seen == {"final": "hello world", "audio": len(PCM_1S)}
    assert a.take_audio() == b""                                  # dropped: nothing is handed out ...
    assert a._final == "" and len(a._buffer) == 0                 # ... and the next holder clears it on its way out


class SlowClose(FakeStream):
    def __init__(self, gate: threading.Event, **kw) -> None:
        super().__init__(**kw)
        self.gate = gate

    def close(self) -> str:
        self.gate.wait(3.0)
        return super().close()


def test_a_revoke_answers_without_waiting_for_a_slow_provider_close_and_still_settles(tmp_path):
    """complement F4 (deepseek #1). Red with: end_revoked closing the provider inline (it returns after the 3 s close)."""
    import time as _time
    rig = Rig(tmp_path)
    gate = threading.Event()
    s = rig.open(stream=SlowClose(gate))
    s.feed(0, PCM_1S)
    t0 = _time.monotonic()
    s.end_revoked()
    took = _time.monotonic() - t0
    assert s.closed and s.end_reason == "revoked"
    assert took < 1.5, took
    with refused("closed"):
        s.feed(1, PCM_1S)                                         # ended at once: no more audio goes
    gate.set()
    for _ in range(100):
        if rig.rows()[0].status == "ok":
            break
        _time.sleep(0.05)
    [row] = rig.rows()
    assert (row.status, row.units) == ("ok", 1)
    assert rig.streams[0].closed == 1


# --- L3 batch4c r2: an owner's close while a revoke's worker is still closing the provider --------------------------

def _count_collects(session):
    calls = []
    inner = session._on_collect

    def counted(sid):
        calls.append(sid)
        inner(sid)

    session._on_collect = counted
    return calls


def _wait_for(pred, timeout=5.0):
    import time as _time
    end = _time.monotonic() + timeout
    while _time.monotonic() < end:
        if pred():
            return True
        _time.sleep(0.02)
    return pred()


def test_a_close_during_a_pending_revoke_close_keeps_the_session_counted(tmp_path, monkeypatch):
    """codex r2 MED. The page posts close right after the revoked chunk while the worker still holds the provider
    socket and the reservation. Red with: close() collecting at once (the registry let go, so an open past the cap ran)."""
    monkeypatch.setattr(ss, "REVOKE_COLLECT_WAIT_S", 0.2, raising=False)
    rig = Rig(tmp_path, max_sessions=1)
    gate = threading.Event()
    s = rig.open(stream=SlowClose(gate))
    s.feed(0, PCM_1S)
    s.end_revoked(wait=0.0)
    calls = _count_collects(s)
    with refused("provider_failed"):
        s.close()                                                 # the bound ran out: unfinished, so no text
    assert calls == []                                            # ... and not collected
    assert len(rig.registry) == 1                                 # the closing session still holds its live slot
    with refused("too_many_sessions"):
        rig.open()
    assert rig.rows()[0].status == "reserved"
    gate.set()
    assert _wait_for(lambda: s._finished.is_set())
    assert calls == [s.sid]                                       # the worker collected it, once
    assert len(rig.registry) == 0
    assert s.sid not in rig.registry._sessions and s.sid not in rig.registry._ended_at
    [row] = rig.rows()
    assert (row.status, row.units) == ("ok", 1)
    rig.open().close()                                            # the slot is free again


def test_a_close_that_outwaits_the_revoke_close_answers_normally_and_collects_once(tmp_path, monkeypatch):
    """deepseek r2 MED (collect then a later _on_end). The worker finishes inside close()'s bounded wait: close answers
    as a normal close would (clean here, so the text), collects once, and no sid is left behind. Red with: close()
    refusing provider_failed whenever the worker had not finished when close() was called."""
    monkeypatch.setattr(ss, "REVOKE_COLLECT_WAIT_S", 5.0, raising=False)
    rig = Rig(tmp_path, max_sessions=1)
    gate = threading.Event()
    s = rig.open(stream=SlowClose(gate))
    s.feed(0, PCM_1S)
    s.end_revoked(wait=0.0)
    calls = _count_collects(s)
    threading.Timer(0.2, gate.set).start()
    assert s.close() == "hello world"
    assert s.close() == "hello world"                             # idempotent, and still one collect
    assert calls == [s.sid]
    assert s.sid not in rig.registry._sessions and s.sid not in rig.registry._ended_at
    assert len(rig.registry) == 0
    [row] = rig.rows()
    assert (row.status, row.units) == ("ok", 1)


def test_a_close_that_outwaits_an_unclean_revoke_close_refuses_and_collects_once(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "REVOKE_COLLECT_WAIT_S", 5.0, raising=False)
    rig = Rig(tmp_path, max_sessions=1)
    gate = threading.Event()
    s = rig.open(stream=SlowClose(gate, fail_close=True))
    s.feed(0, PCM_1S)
    s.end_revoked(wait=0.0)
    calls = _count_collects(s)
    threading.Timer(0.2, gate.set).start()
    with refused("provider_failed"):
        s.close()
    assert calls == [s.sid]
    assert len(rig.registry) == 0
    assert s.sid not in rig.registry._ended_at


def test_an_ordinary_close_is_unchanged_by_the_revoke_wait(tmp_path, monkeypatch):
    """Control: no revoke, so close() never waits on a worker and collects once, as before."""
    import time as _time
    monkeypatch.setattr(ss, "REVOKE_COLLECT_WAIT_S", 5.0, raising=False)
    rig = Rig(tmp_path, max_sessions=1)
    s = rig.open()
    s.feed(0, PCM_1S)
    calls = _count_collects(s)
    t0 = _time.monotonic()
    assert s.close() == "hello world"
    assert _time.monotonic() - t0 < 1.0
    assert calls == [s.sid]
    assert len(rig.registry) == 0
    rig.open().close()
