"""levain.voice.meter: the ledger, the caps, the dated price table and the fallback decision.

Everything runs on a tmp_path ledger and an injected clock: no real home, no network, no key.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from levain.voice import meter as m
from levain.voice.meter import (
    DAY_CAP, DEFAULT_CAPS, DEFAULT_PRICES, MICROS_PER_USD, MONTH_CAP, OK, UNPRICED, Caps, Meter, MeterError,
    PriceEntry, PriceTable, choose_route, usd_to_micros)

NY = ZoneInfo("America/New_York")


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def utc(*a) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)


def local(*a) -> datetime:
    return datetime(*a, tzinfo=NY)


def make(tmp_path, *, caps=DEFAULT_CAPS, prices=DEFAULT_PRICES, at=None, tz=NY, name="voice.db") -> tuple[Meter, Clock]:
    clock = Clock(at or local(2026, 11, 5, 12, 0))
    return Meter(tmp_path / name, caps=caps, prices=prices, clock=clock, tz=tz), clock


# --- money and the price table -----------------------------------------------------------------------------------------

def test_usd_to_micros_is_exact_and_refuses_floats():
    assert usd_to_micros(10) == 10 * MICROS_PER_USD
    assert usd_to_micros("0.015") == 15_000
    assert usd_to_micros("0.000001") == 1
    for bad in (0.015, True, None, "abc", "0.0000001", "Infinity", "-Infinity", "NaN", "-5", -5):
        with pytest.raises(MeterError):
            usd_to_micros(bad)


def test_usd_to_micros_refuses_an_overflowing_or_unstorable_amount():
    for bad in ("1e999999", "1e30", str(2 ** 63), 2 ** 63, "4611686018427388"):    # last: 2**62 / 1e6 rounded up
        with pytest.raises(MeterError):
            usd_to_micros(bad)
    assert usd_to_micros("4611686018427") == 4611686018427 * MICROS_PER_USD       # under the bound still prices


def test_the_standing_cap_defaults_are_ten_a_month_and_two_a_day():
    assert DEFAULT_CAPS.month_micros == usd_to_micros(10)
    assert DEFAULT_CAPS.day_micros == usd_to_micros(2)


def test_price_table_is_dated_and_each_entry_prices_once():
    datetime.strptime(DEFAULT_PRICES.as_of, "%Y-%m-%d")
    keys = [(e.provider, e.use, e.model) for e in DEFAULT_PRICES.entries]
    assert len(keys) == len(set(keys))
    with pytest.raises(MeterError):
        PriceTable(as_of="Nov 4", entries=())
    with pytest.raises(MeterError):
        PriceTable(as_of="2026-11-04", entries=(PriceEntry("a", "tts", "x", 1, 1), PriceEntry("a", "tts", "x", 2, 1)))


@pytest.mark.parametrize("provider,use,model,units,expected_micros", [
    ("openai", "tts", "tts-1", 1_000_000, 15_000_000),            # $15 / 1M characters
    ("openai", "tts", "tts-1", 1_000, 15_000),
    ("openai", "tts", "tts-1-hd", 1_000, 30_000),
    ("openai", "stt", "gpt-4o-mini-transcribe", 60, 3_000),       # $0.003 / minute
    ("openai", "stt", "gpt-4o-mini-transcribe", 1, 50),
    ("elevenlabs", "tts", "eleven_flash_v2_5", 1_000, 40_000),    # $0.04 / 1K characters
    ("elevenlabs", "stt", "scribe", 3_600, 220_000),              # $0.22 / hour
    ("deepgram", "tts", "aura-2", 1_000, 30_000),                 # $0.030 / 1K characters
    ("deepgram", "stt", "nova-3", 60, 4_300),                     # $0.0043 / minute
])
def test_default_prices_match_the_dated_list_prices(tmp_path, provider, use, model, units, expected_micros):
    meter, _ = make(tmp_path, caps=Caps(10**12, 10**12))
    chk = meter.reserve(provider, use, model, units)
    assert chk.allowed and chk.estimate_micros == expected_micros


def test_estimates_round_up_never_down():
    e = PriceEntry("p", "stt", "m", micros=220_000, per=3_600)     # 61.11.. micros a second
    assert e.cost(1) == 62
    assert e.cost(0) == 0
    assert PriceEntry("p", "tts", "m", micros=15_000_000, per=1_000_000).cost(1) == 15


def test_a_model_the_table_does_not_price_is_refused_and_writes_nothing(tmp_path):
    meter, _ = make(tmp_path)
    for model in ("gpt-4o-mini-tts", "no-such-model"):
        chk = meter.reserve("openai", "tts", model, 100)
        assert (chk.allowed, chk.reason, chk.reservation_id) == (False, UNPRICED, None)
    assert meter.ledger() == []


def test_a_model_swap_is_a_data_edit_not_a_code_change(tmp_path):
    """OpenAI has announced tts-1 shuts down; the replacement is a row, priced the same machinery."""
    swapped = PriceTable(as_of="2027-01-06", entries=(
        PriceEntry("openai", "tts", "replacement-tts", micros=12_000_000, per=1_000_000),))
    meter, _ = make(tmp_path, prices=swapped)
    assert meter.reserve("openai", "tts", "tts-1", 10).reason == UNPRICED
    chk = meter.reserve("openai", "tts", "replacement-tts", 1_000)
    assert chk.allowed and chk.estimate_micros == 12_000
    assert meter.spent().prices_as_of == "2027-01-06"


# --- the ledger and the sums --------------------------------------------------------------------------------------------

def test_reserve_writes_one_row_and_the_sums_count_it(tmp_path):
    meter, clock = make(tmp_path)
    chk = meter.reserve("openai", "tts", "tts-1", 2_000)
    assert chk.allowed and chk.reason == OK and chk.estimate_micros == 30_000
    assert (chk.month_micros, chk.day_micros) == (0, 0)             # spent BEFORE this call
    [row] = meter.ledger()
    assert (row.id, row.provider, row.use, row.model, row.units, row.cost_micros, row.status) == (
        chk.reservation_id, "openai", "tts", "tts-1", 2_000, 30_000, "reserved")
    assert row.at == "2026-11-05T17:00:00.000000Z"
    spent = meter.spent()
    assert (spent.month_micros, spent.day_micros, spent.prices_as_of) == (30_000, 30_000, DEFAULT_PRICES.as_of)
    assert meter.settle(chk.reservation_id) is True
    assert meter.ledger()[0].status == "ok"
    assert meter.spent().day_micros == 30_000
    clock.at = local(2026, 11, 5, 13, 0)
    assert meter.reserve("openai", "tts", "tts-1", 1_000).day_micros == 30_000


def test_release_stops_the_row_counting_and_zeroes_its_cost(tmp_path):
    meter, _ = make(tmp_path)
    chk = meter.reserve("openai", "tts", "tts-1", 2_000)
    assert meter.release(chk.reservation_id) is True
    assert meter.spent().day_micros == 0
    [row] = meter.ledger()
    assert (row.status, row.cost_micros) == ("released", 0)


def test_settle_and_release_end_a_reservation_exactly_once(tmp_path):
    meter, _ = make(tmp_path)
    a = meter.reserve("openai", "tts", "tts-1", 1_000).reservation_id
    b = meter.reserve("openai", "tts", "tts-1", 1_000).reservation_id
    assert meter.settle(a) and not meter.settle(a) and not meter.release(a)
    assert meter.release(b) and not meter.release(b) and not meter.settle(b)
    assert not meter.settle(9999) and not meter.release(9999)
    assert [r.status for r in meter.ledger()] == ["released", "ok"]
    assert meter.spent().day_micros == 15_000


def test_an_unsettled_reservation_stays_counted(tmp_path):
    """A crash between the check and the call errs toward the cap, never past it."""
    meter, _ = make(tmp_path)
    meter.reserve("openai", "tts", "tts-1", 1_000)
    assert meter.spent().day_micros == 15_000
    assert Meter(tmp_path / "voice.db", clock=meter._clock, tz=NY).spent().day_micros == 15_000   # a fresh process


# --- the caps ------------------------------------------------------------------------------------------------------------

def test_a_call_that_lands_exactly_on_a_cap_is_allowed_and_one_micro_more_is_not(tmp_path):
    meter, _ = make(tmp_path, caps=Caps(month_micros=100_000, day_micros=30_000))
    assert meter.reserve("openai", "tts", "tts-1", 1_000).allowed                 # 15_000
    assert meter.reserve("openai", "tts", "tts-1", 1_000).allowed                 # 30_000 == the day cap
    chk = meter.reserve("openai", "tts", "tts-1", 1)                              # +15
    assert (chk.allowed, chk.reason, chk.reservation_id) == (False, DAY_CAP, None)
    assert chk.estimate_micros == 15 and chk.day_micros == 30_000
    assert len(meter.ledger()) == 2                                                # a refusal writes nothing


def test_a_zero_cap_turns_premium_off(tmp_path):
    meter, _ = make(tmp_path, caps=Caps(month_micros=0, day_micros=0))
    assert meter.reserve("openai", "tts", "tts-1", 1).reason == MONTH_CAP
    assert meter.reserve("openai", "tts", "tts-1", 0).reason == MONTH_CAP          # zero units is still a call
    free = PriceTable("2026-11-04", (PriceEntry("openai", "tts", "tts-1", micros=0, per=1),))
    meter, _ = make(tmp_path, caps=Caps(month_micros=10**6, day_micros=0), prices=free, name="free.db")
    assert meter.reserve("openai", "tts", "tts-1", 1_000).reason == DAY_CAP       # even a call priced at nothing
    assert meter.ledger() == []


def test_a_call_is_priced_at_no_less_than_one_unit(tmp_path):
    meter, _ = make(tmp_path)
    chk = meter.reserve("openai", "stt", "gpt-4o-mini-transcribe", 0)            # a sub-second clip estimated as 0 s
    assert chk.allowed and chk.estimate_micros == 50 and meter.ledger()[0].cost_micros == 50


def test_the_clock_is_read_while_the_write_lock_is_held(tmp_path):
    path, held = tmp_path / "voice.db", []

    def clock() -> datetime:
        probe = sqlite3.connect(path, timeout=0, isolation_level=None)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.execute("ROLLBACK")
            held.append(False)
        except sqlite3.OperationalError:
            held.append(True)
        finally:
            probe.close()
        return local(2026, 11, 5, 23, 59, 59)

    meter = Meter(path, clock=clock, tz=NY)
    meter.ledger()                                                                  # creates the file, reads no clock
    assert meter.reserve("openai", "tts", "tts-1", 10).allowed
    assert held == [True]


def test_spent_reads_the_month_and_the_day_in_one_statement(tmp_path, monkeypatch):
    meter, _ = make(tmp_path)
    meter.reserve("openai", "tts", "tts-1", 1_000)
    sums: list[str] = []
    real_open = Meter._open

    def traced(self):
        conn = real_open(self)
        conn.set_trace_callback(lambda sql: sums.append(sql) if "SUM(" in sql else None)
        return conn

    monkeypatch.setattr(Meter, "_open", traced)
    spend = meter.spent()
    assert len(sums) == 1 and (spend.month_micros, spend.day_micros) == (15_000, 15_000)


def test_month_cap_is_named_before_day_cap_when_both_are_over(tmp_path):
    meter, _ = make(tmp_path, caps=Caps(month_micros=10, day_micros=10))
    assert meter.reserve("openai", "tts", "tts-1", 1_000).reason == MONTH_CAP


def test_the_cap_is_across_providers_and_a_cheaper_provider_may_still_fit(tmp_path):
    meter, _ = make(tmp_path, caps=Caps(month_micros=10**9, day_micros=20_000))
    assert meter.reserve("elevenlabs", "tts", "eleven_flash_v2_5", 400).allowed    # 16_000 of 20_000
    assert meter.reserve("elevenlabs", "tts", "eleven_flash_v2_5", 400).reason == DAY_CAP
    assert meter.reserve("openai", "tts", "tts-1", 200).allowed                    # 3_000 fits the 4_000 left
    assert meter.reserve("openai", "stt", "gpt-4o-mini-transcribe", 60).reason == DAY_CAP   # 3_000 does not


def test_caps_are_validated():
    for bad in (-1, 1.5, True, None):
        with pytest.raises(MeterError):
            Caps(month_micros=bad)
        with pytest.raises(MeterError):
            Caps(day_micros=bad)


def test_a_bad_call_shape_is_an_error_not_a_free_call(tmp_path):
    meter, _ = make(tmp_path)
    for use, units in (("sing", 1), ("tts", -1), ("tts", 1.5), ("tts", True), ("tts", None)):
        with pytest.raises(MeterError):
            meter.reserve("openai", use, "tts-1", units)
    assert meter.ledger() == []


# --- periods: local month and day, DST included ---------------------------------------------------------------------------

def test_the_day_resets_at_local_midnight_not_utc_midnight(tmp_path):
    meter, clock = make(tmp_path, caps=Caps(10**9, 15_000))
    clock.at = local(2026, 11, 5, 22, 0)                    # 03:00Z the next UTC day
    assert meter.reserve("openai", "tts", "tts-1", 1_000).allowed
    clock.at = local(2026, 11, 5, 23, 59)
    assert meter.reserve("openai", "tts", "tts-1", 1_000).reason == DAY_CAP     # still the same local day
    clock.at = local(2026, 11, 6, 0, 1)
    assert meter.reserve("openai", "tts", "tts-1", 1_000).allowed               # a new local day, same UTC date


def test_the_month_rolls_at_local_midnight_on_the_first(tmp_path):
    meter, clock = make(tmp_path, caps=Caps(15_000, 10**9))
    clock.at = local(2026, 10, 31, 23, 59)
    assert meter.reserve("openai", "tts", "tts-1", 1_000).allowed
    clock.at = local(2026, 11, 1, 0, 1)
    assert meter.spent().month_micros == 0
    assert meter.reserve("openai", "tts", "tts-1", 1_000).allowed
    assert meter.reserve("openai", "tts", "tts-1", 1).reason == MONTH_CAP
    clock.at = local(2026, 12, 1, 0, 1)
    assert meter.spent().month_micros == 0


def test_the_december_to_january_roll(tmp_path):
    meter, clock = make(tmp_path, at=local(2026, 12, 31, 12, 0))
    meter.reserve("openai", "tts", "tts-1", 1_000)
    assert meter.spent().month_micros == 15_000
    clock.at = local(2027, 1, 1, 0, 5)
    assert meter.spent().month_micros == 0


def test_the_day_that_gains_an_hour_is_one_day(tmp_path):
    """2026-11-01 is 25 hours long in New York (DST ends): a row at 00:30 EDT and one at 23:30 EST share a day."""
    meter, clock = make(tmp_path, caps=Caps(10**9, 15_000))
    clock.at = local(2026, 11, 1, 0, 30)
    assert meter.reserve("openai", "tts", "tts-1", 1_000).allowed
    clock.at = datetime(2026, 11, 2, 4, 30, tzinfo=timezone.utc)                 # 23:30 EST on 11-01
    assert meter.reserve("openai", "tts", "tts-1", 1_000).reason == DAY_CAP
    clock.at = datetime(2026, 11, 2, 5, 30, tzinfo=timezone.utc)                 # 00:30 EST on 11-02
    assert meter.reserve("openai", "tts", "tts-1", 1_000).allowed


def test_with_no_tz_the_machines_own_zone_decides(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        meter = Meter(tmp_path / "voice.db", caps=Caps(10**9, 15_000), clock=(clock := Clock(local(2026, 11, 5, 22, 0))))
        assert meter.reserve("openai", "tts", "tts-1", 1_000).allowed
        clock.at = local(2026, 11, 5, 23, 30)
        assert meter.reserve("openai", "tts", "tts-1", 1_000).reason == DAY_CAP
        clock.at = local(2026, 11, 6, 0, 30)
        assert meter.reserve("openai", "tts", "tts-1", 1_000).allowed
    finally:
        monkeypatch.undo()
        time.tzset()


# --- one transaction: two callers cannot both pass under the cap ---------------------------------------------------------

@pytest.mark.parametrize("separate_meters", [False, True])
def test_racing_callers_cannot_both_pass_under_the_cap(tmp_path, separate_meters):
    """The cap holds exactly one call; N threads reach reserve() at the same instant. Exactly one may win, and the
    ledger holds exactly one row. Separate Meter objects over one file stand for separate processes."""
    n = 8
    clock = Clock(local(2026, 11, 5, 12, 0))
    caps = Caps(month_micros=10**9, day_micros=15_000)             # one 1_000-character tts-1 call
    shared = Meter(tmp_path / "voice.db", caps=caps, clock=clock, tz=NY)
    meters = [Meter(tmp_path / "voice.db", caps=caps, clock=clock, tz=NY) if separate_meters else shared
              for _ in range(n)]
    shared.spent()                                                  # the file exists before the race
    barrier = threading.Barrier(n)
    results: list = [None] * n
    errors: list = []

    def go(i: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[i] = meters[i].reserve("openai", "tts", "tts-1", 1_000)
        except BaseException as exc:      # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    assert sum(r.allowed for r in results) == 1
    assert {r.reason for r in results if not r.allowed} == {DAY_CAP}
    assert len(shared.ledger()) == 1 and shared.spent().day_micros == 15_000


# --- the file ------------------------------------------------------------------------------------------------------------

def test_the_ledger_holds_numbers_and_names_only(tmp_path):
    meter, _ = make(tmp_path)
    meter.reserve("openai", "tts", "tts-1", 1_000)
    with sqlite3.connect(tmp_path / "voice.db") as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(calls)")]
        meta = dict(conn.execute("SELECT key, value FROM meta"))
    assert cols == ["id", "at", "provider", "use", "model", "units", "cost_micros", "status"]
    assert meta == {"format": m.FORMAT}


def test_the_ledger_file_is_the_operators_alone(tmp_path):
    meter, _ = make(tmp_path)
    meter.spent()
    assert (tmp_path / "voice.db").stat().st_mode & 0o777 == 0o600


def test_a_ledger_file_someone_else_created_is_made_the_operators_alone(tmp_path):
    path = tmp_path / "voice.db"
    path.touch()
    path.chmod(0o644)                                     # an empty file left by an interrupted or racing first open
    Meter(path).spent()
    assert path.stat().st_mode & 0o777 == 0o600


def test_the_ledger_is_0600_before_sqlite_opens_it_so_its_wal_and_shm_start_0600(tmp_path, monkeypatch):
    path = tmp_path / "voice.db"
    with sqlite3.connect(path) as conn:                   # a WAL-mode ledger left 0644 with no -wal/-shm beside it
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES ('format', ?)", (m.FORMAT,))
    conn.close()
    for sibling in ("-wal", "-shm"):
        (tmp_path / f"voice.db{sibling}").unlink(missing_ok=True)
    path.chmod(0o644)
    seen: list[tuple[str, int]] = []
    real = sqlite3.connect

    def spy(*a, **k):
        seen.append(("db", path.stat().st_mode & 0o777))     # the mode at the moment SQLite first opens it
        c = real(*a, **k)
        c.execute("SELECT count(*) FROM sqlite_master").fetchone()
        for sibling in ("-wal", "-shm"):
            side = tmp_path / f"voice.db{sibling}"
            if side.exists():
                seen.append((sibling, side.stat().st_mode & 0o777))
        return c

    monkeypatch.setattr(m.sqlite3, "connect", spy)
    Meter(path).spent()
    assert seen[0] == ("db", 0o600)
    assert {name for name, _ in seen} >= {"db", "-wal", "-shm"}
    assert all(mode == 0o600 for _, mode in seen)


def test_a_ledger_that_cannot_be_made_private_is_a_meter_error_not_a_permission_error(tmp_path, monkeypatch):
    def refuse(fd, mode):
        raise PermissionError(1, "Operation not permitted")      # a file someone else owns

    monkeypatch.setattr(m.os, "fchmod", refuse)
    with pytest.raises(MeterError, match="cannot be opened"):
        Meter(tmp_path / "voice.db").spent()


def test_constructing_a_meter_creates_nothing_until_it_is_used(tmp_path):
    make(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_a_missing_directory_a_symlink_and_a_foreign_database_are_refused(tmp_path):
    with pytest.raises(MeterError, match="does not exist"):
        Meter(tmp_path / "nope" / "voice.db").spent()
    real = tmp_path / "real.db"
    real.touch()
    (tmp_path / "link.db").symlink_to(real)
    with pytest.raises(MeterError, match="symlink"):
        Meter(tmp_path / "link.db").spent()
    foreign = tmp_path / "foreign.db"
    with sqlite3.connect(foreign) as conn:
        conn.execute("CREATE TABLE other (x)")
    with pytest.raises(MeterError, match="not a voice ledger"):
        Meter(foreign).spent()
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not sqlite" * 50)
    with pytest.raises(MeterError):
        Meter(junk).spent()


def test_a_newer_format_marker_is_refused(tmp_path):
    meter, _ = make(tmp_path)
    meter.spent()
    with sqlite3.connect(tmp_path / "voice.db") as conn:
        conn.execute("UPDATE meta SET value = 'levain-voice-meter/9' WHERE key = 'format'")
    with pytest.raises(MeterError, match="format"):
        Meter(tmp_path / "voice.db").spent()


# --- the fallback decision (pure) ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("candidates,outcomes,on_device,kind,provider,reasons", [
    # the first provider is usable: it wins, nothing is passed over
    (["a", "b"], {}, True, "premium", "a", ()),
    (["a", "b"], {"a": OK}, False, "premium", "a", ()),
    # A over a cap or failing -> B
    (["a", "b"], {"a": MONTH_CAP}, True, "premium", "b", (("a", MONTH_CAP),)),
    (["a", "b"], {"a": DAY_CAP}, False, "premium", "b", (("a", DAY_CAP),)),
    (["a", "b"], {"a": "http_429"}, True, "premium", "b", (("a", "http_429"),)),
    (["a", "b"], {"a": "key_refused", "b": OK}, True, "premium", "b", (("a", "key_refused"),)),
    (["a", "b", "c"], {"a": "x", "b": "y"}, True, "premium", "c", (("a", "x"), ("b", "y"))),
    # both fail: the page's own voice, else text only
    (["a", "b"], {"a": MONTH_CAP, "b": DAY_CAP}, True, "on_device", None, (("a", MONTH_CAP), ("b", DAY_CAP))),
    (["a", "b"], {"a": MONTH_CAP, "b": DAY_CAP}, False, "text_only", None, (("a", MONTH_CAP), ("b", DAY_CAP))),
    (["a"], {"a": UNPRICED}, True, "on_device", None, (("a", UNPRICED),)),
    # no candidate at all (no live key): straight to the fallback
    ([], {}, True, "on_device", None, ()),
    ([], {}, False, "text_only", None, ()),
    # an outcome for a name that is not a candidate changes nothing
    (["a"], {"z": "boom"}, False, "premium", "a", ()),
])
def test_choose_route_table(candidates, outcomes, on_device, kind, provider, reasons):
    route = choose_route(candidates, outcomes, on_device_available=on_device)
    assert (route.kind, route.provider, route.reasons) == (kind, provider, reasons)


def test_the_reason_reported_is_about_the_provider_the_operator_chose():
    route = choose_route(["a", "b"], {"a": MONTH_CAP, "b": "http_503"}, on_device_available=True)
    assert route.reason == MONTH_CAP
    assert choose_route(["a"], {}, on_device_available=True).reason == OK


def test_choose_route_is_pure():
    cands, outs = ["a", "b"], {"a": MONTH_CAP}
    first = choose_route(cands, outs, on_device_available=True)
    assert choose_route(cands, outs, on_device_available=True) == first
    assert cands == ["a", "b"] and outs == {"a": MONTH_CAP}


def test_the_meter_outcomes_feed_the_route(tmp_path):
    """A cap refusal from reserve() is exactly the outcome string choose_route passes a provider over for."""
    meter, _ = make(tmp_path, caps=Caps(10**9, 20_000))
    meter.reserve("elevenlabs", "tts", "eleven_flash_v2_5", 400)
    outcomes = {
        "elevenlabs": meter.reserve("elevenlabs", "tts", "eleven_flash_v2_5", 400).reason,    # 16_000 more: over
        "openai": meter.reserve("openai", "tts", "tts-1", 200).reason,                        # 3_000: fits
    }
    route = choose_route(["elevenlabs", "openai"], outcomes, on_device_available=True)
    assert (route.kind, route.provider, route.reason) == ("premium", "openai", DAY_CAP)
