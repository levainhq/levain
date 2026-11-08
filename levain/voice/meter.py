"""levain.voice.meter: what premium voice has cost, the caps on it, and what to do when a cap or a provider says no.

Levain cannot read a provider's bill, so this is an ESTIMATE and is labelled as one: every call is priced from a dated
table of list prices, written to a ledger, and summed per calendar month and day. Two jobs follow from that.

1. A check before each call. The check and the row that records it are ONE ``BEGIN IMMEDIATE`` transaction
   (:meth:`Meter.reserve`), so two callers racing for the last of a cap cannot both pass: the second one reads the first
   one's row. A reservation is then settled (the call went out) or released (it failed, so it costs nothing that Levain
   knows of). A reservation nobody settles stays counted, so a crash between the check and the call errs toward the cap,
   never past it.
2. A routing decision (:func:`choose_route`) that is a pure function of what each candidate provider answered, so the
   table of cases is the whole behaviour: first usable provider, else the on-device voice, else text only.

Money is integer micro-dollars (:data:`MICROS_PER_USD` per dollar) and every division rounds UP, so the estimate can only
err high and no float drift can walk a sum across a cap. The price table is DATA: a model id is a field of an entry, never
a branch in code, so a provider retiring a model is an edit to :data:`DEFAULT_PRICES` and nothing else. A model the table
does not price is refused, not guessed (:data:`UNPRICED`).

The ledger holds numbers and names only: no text that was spoken, no audio, no key, and no prefix or hash of one. Its path
is the caller's; this module reads no environment and creates nothing outside that one file.

Periods are the operator's local calendar month and day (``tz``, default the machine's own zone), converted to UTC for
the query, because a day cap that resets at 20:00 local is a cap nobody can reason about.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path

__all__ = [
    "MICROS_PER_USD", "USES", "FORMAT", "DB_NAME",
    "OK", "MONTH_CAP", "DAY_CAP", "UNPRICED",
    "PriceEntry", "PriceTable", "DEFAULT_PRICES", "PRICES_AS_OF",
    "Caps", "DEFAULT_CAPS", "DEFAULT_MONTH_CAP_MICROS", "DEFAULT_DAY_CAP_MICROS",
    "Check", "Spend", "LedgerRow", "Meter", "MeterError",
    "Route", "choose_route", "usd_to_micros",
]

MICROS_PER_USD = 1_000_000
USES = ("tts", "stt")          # tts counts characters, stt counts seconds of audio
FORMAT = "levain-voice-meter/1"
DB_NAME = "voice.db"           # the name callers conventionally give the ledger file inside their directory

# Outcome codes. The meter produces the three that are about money; any other string a caller passes to
# choose_route is a failure of that provider, whatever it is called.
OK = "ok"
MONTH_CAP = "month_cap"
DAY_CAP = "day_cap"
UNPRICED = "unpriced"

_BUSY_WAIT_S = 10.0
_MAX_MICROS = 2 ** 62          # a cap or price above this could not be stored in SQLite's signed 64-bit integer with room to sum


class MeterError(Exception):
    """The ledger could not be used: a bad path, a foreign file, arguments that cannot be priced, or any SQLite error
    on a read or a write (a lock held past the busy wait, a file that is not a ledger); the message names the SQLite
    error's type only."""


def usd_to_micros(usd: str | int) -> int:
    """Whole dollars or a decimal string ("0.015") to micro-dollars, exactly. Floats are refused: a float price is how a
    cap ends up a rounding error away from where it was meant to be."""
    if isinstance(usd, bool) or not isinstance(usd, (str, int)):
        raise MeterError(f"a dollar amount is a string or an int, not {type(usd).__name__}")
    try:
        value = Decimal(usd) * MICROS_PER_USD     # decimal.Overflow (an exponent like 1e999999) is an ArithmeticError
    except ArithmeticError:                       # InvalidOperation is one too
        raise MeterError(f"{usd!r} is not a dollar amount") from None
    if not value.is_finite() or value < 0:
        raise MeterError(f"{usd!r} is not a dollar amount")
    if value > _MAX_MICROS:
        raise MeterError(f"{usd!r} is more than a ledger can hold")
    if value != value.to_integral_value():
        raise MeterError(f"{usd!r} has finer than micro-dollar precision")
    return int(value)


# --- the price table (data) -------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class PriceEntry:
    """One priced (provider, use, model): ``micros`` micro-dollars buy ``per`` units, where a unit is a character for
    ``tts`` and a second of audio for ``stt``. ``per`` is large for TTS (a per-million-characters list price) and 60 or
    3600 for STT (a per-minute or per-hour one), so the list price is written as the vendor prints it."""
    provider: str
    use: str
    model: str
    micros: int
    per: int

    def __post_init__(self) -> None:
        if self.use not in USES:
            raise MeterError(f"use {self.use!r} is not one of {USES}")
        if not self.provider or not self.model:
            raise MeterError("a price entry names its provider and model")
        for name in ("micros", "per"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0 or (name == "per" and v == 0):
                raise MeterError(f"price entry {name} must be a non-negative int (per: positive), got {v!r}")

    def cost(self, units: int) -> int:
        """The estimate for ``units``, rounded up to a whole micro-dollar."""
        return -(-units * self.micros // self.per)


@dataclass(frozen=True)
class PriceTable:
    """A dated set of :class:`PriceEntry`. ``as_of`` is the date the list prices were read, shown beside any total so the
    reader knows how old the estimate's rates are."""
    as_of: str
    entries: tuple[PriceEntry, ...]

    def __post_init__(self) -> None:
        try:
            datetime.strptime(self.as_of, "%Y-%m-%d")
        except ValueError:
            raise MeterError(f"a price table is dated YYYY-MM-DD, got {self.as_of!r}") from None
        keys = [(e.provider, e.use, e.model) for e in self.entries]
        if len(set(keys)) != len(keys):
            raise MeterError("a price table prices each (provider, use, model) once")

    def find(self, provider: str, use: str, model: str) -> PriceEntry | None:
        for e in self.entries:
            if e.provider == provider and e.use == use and e.model == model:
                return e
        return None


PRICES_AS_OF = "2026-11-07"

# List prices as the vendors' pages printed them on PRICES_AS_OF. Not here on purpose: OpenAI's gpt-4o-mini-tts, which is
# priced per token with no per-character or per-minute figure to convert, so it is refused as unpriced until someone
# measures it and adds an entry. OpenAI has announced tts-1, tts-1-hd and gpt-4o-mini-tts shut down on 2027-01-06: that
# is an edit to these rows, not to any code.
DEFAULT_PRICES = PriceTable(
    as_of=PRICES_AS_OF,
    entries=(
        PriceEntry("openai", "tts", "tts-1", micros=15_000_000, per=1_000_000),          # $15 / 1M characters
        PriceEntry("openai", "tts", "tts-1-hd", micros=30_000_000, per=1_000_000),       # $30 / 1M characters
        PriceEntry("openai", "stt", "gpt-4o-mini-transcribe", micros=3_000, per=60),     # $0.003 / minute
        # https://developers.openai.com/api/docs/pricing, read 2026-11-07: gpt-live-transcribe, estimated per minute
        PriceEntry("openai", "stt", "gpt-live-transcribe", micros=17_000, per=60),       # $0.017 / minute
        PriceEntry("elevenlabs", "tts", "eleven_flash_v2_5", micros=40_000, per=1_000),  # $0.04 / 1K characters
        PriceEntry("elevenlabs", "stt", "scribe", micros=220_000, per=3_600),            # $0.22 / hour
        PriceEntry("deepgram", "tts", "aura-2", micros=30_000, per=1_000),               # $0.030 / 1K characters
        PriceEntry("deepgram", "stt", "nova-3", micros=4_300, per=60),                   # $0.0043 / minute, pre-recorded
        # https://deepgram.com/pricing, read 2026-11-08: Nova-3 Monolingual pay-as-you-go, Streaming $0.0077 / min
        PriceEntry("deepgram", "stt", "nova-3-stream", micros=7_700, per=60),            # $0.0077 / minute, live WebSocket
        # https://inworld.ai/pricing, read 2026-11-05: Realtime TTS-2 Flash on the On-Demand plan, per 1M characters
        PriceEntry("inworld", "tts", "inworld-tts-2-flash", micros=15_000_000, per=1_000_000),   # $15 / 1M characters
    ),
)


# --- caps -------------------------------------------------------------------------------------------------------------

DEFAULT_MONTH_CAP_MICROS = 10 * MICROS_PER_USD
DEFAULT_DAY_CAP_MICROS = 2 * MICROS_PER_USD


@dataclass(frozen=True)
class Caps:
    """The most that may be spent, across all providers, in a calendar month and in a calendar day (micro-dollars). A cap
    of 0 turns premium voice off. These are the defaults the design recommends; a caller overrides them by passing its own
    :class:`Caps` to :class:`Meter`."""
    month_micros: int = DEFAULT_MONTH_CAP_MICROS
    day_micros: int = DEFAULT_DAY_CAP_MICROS

    def __post_init__(self) -> None:
        for name in ("month_micros", "day_micros"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise MeterError(f"cap {name} must be a non-negative int, got {v!r}")


DEFAULT_CAPS = Caps()


# --- results ----------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Spend:
    """What the ledger counts as spent in the period holding ``at``: reserved and settled rows, never released ones."""
    month_micros: int
    day_micros: int
    month_start: str    # UTC stamps of the local period starts the sums run from
    day_start: str
    prices_as_of: str


@dataclass(frozen=True)
class Check:
    """The answer to :meth:`Meter.reserve`. ``allowed`` is true only when a row now holds ``reservation_id``; otherwise
    ``reason`` is :data:`MONTH_CAP`, :data:`DAY_CAP` or :data:`UNPRICED` and nothing was written. ``month_micros`` and
    ``day_micros`` are what was already spent BEFORE this call, so a refusal can say how far over it would have gone."""
    allowed: bool
    reason: str
    reservation_id: int | None
    estimate_micros: int
    month_micros: int
    day_micros: int
    caps: Caps


@dataclass(frozen=True)
class LedgerRow:
    id: int
    at: str
    provider: str
    use: str
    model: str
    units: int
    cost_micros: int
    status: str          # reserved | ok | released


# --- the ledger -------------------------------------------------------------------------------------------------------

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS calls (
         id INTEGER PRIMARY KEY AUTOINCREMENT,
         at TEXT NOT NULL,
         provider TEXT NOT NULL,
         use TEXT NOT NULL CHECK (use IN ('tts', 'stt')),
         model TEXT NOT NULL,
         units INTEGER NOT NULL CHECK (units >= 0),
         cost_micros INTEGER NOT NULL CHECK (cost_micros >= 0),
         status TEXT NOT NULL CHECK (status IN ('reserved', 'ok', 'released')))""",
    "CREATE INDEX IF NOT EXISTS calls_at ON calls (at)",
)

_STAMP = "%Y-%m-%dT%H:%M:%S.%fZ"     # fixed width, so string order is time order
_COUNTED = "status IN ('reserved', 'ok')"


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_STAMP)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Meter:
    """The ledger at ``path`` (the caller's file; its directory must already exist). Every method opens its own short
    connection, so any number of threads or processes may share one file; the only lock is SQLite's write lock, taken
    with ``BEGIN IMMEDIATE`` by :meth:`reserve`, :meth:`settle` and :meth:`release`."""

    def __init__(self, path: Path | str, *, prices: PriceTable = DEFAULT_PRICES, caps: Caps = DEFAULT_CAPS,
                 clock: Callable[[], datetime] = _utcnow, tz: tzinfo | None = None) -> None:
        self.path = Path(path)
        self.prices = prices
        self.caps = caps
        self._clock = clock
        self._tz = tz
        self._ready = False
        self._ready_lock = threading.Lock()

    # --- connection ---------------------------------------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        if not self._ready:
            with self._ready_lock:
                if not self._ready:
                    self._create()
                    self._ready = True
        return self._open()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=_BUSY_WAIT_S, isolation_level=None)
        conn.execute("PRAGMA busy_timeout = %d" % int(_BUSY_WAIT_S * 1000))
        return conn

    def _create(self) -> None:
        if os.path.islink(self.path):
            raise MeterError(f"voice ledger {self.path} is a symlink")
        if not self.path.parent.is_dir():
            raise MeterError(f"voice ledger directory {self.path.parent} does not exist")
        try:   # created 0600 from its first byte (umask can only remove bits), never through a symlink, and made 0600
            # on the descriptor BEFORE SQLite opens it, so the -wal and -shm files it derives from this one start 0600
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
        except OSError as exc:
            raise MeterError(f"voice ledger {self.path} cannot be opened: {type(exc).__name__}") from None
        try:
            with closing(self._open()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                    if tables and "meta" not in tables:
                        raise MeterError(f"{self.path} is a database but not a voice ledger")
                    for stmt in _SCHEMA:
                        conn.execute(stmt)
                    row = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
                    if row is None:
                        conn.execute("INSERT INTO meta (key, value) VALUES ('format', ?)", (FORMAT,))
                    else:
                        _need_format(row[0])
                    conn.execute("COMMIT")
                except BaseException:
                    _rollback(conn)
                    raise
        except sqlite3.DatabaseError as exc:
            raise MeterError(f"{self.path} is not a usable voice ledger: {type(exc).__name__}") from None

    # --- periods ------------------------------------------------------------------------------------------------------
    def _periods(self, now: datetime) -> tuple[str, str, str, str]:
        """(month_start, month_end, day_start, day_end) as UTC stamps, for the local month and day holding ``now``."""
        local = now.astimezone(self._tz)    # tz None: the machine's own zone
        first = local.date().replace(day=1)
        nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        today = local.date()
        return (self._midnight(first), self._midnight(nxt), self._midnight(today),
                self._midnight(today + timedelta(days=1)))

    def _midnight(self, day: date) -> str:
        # a NAIVE midnight is interpreted in the zone itself (the machine's when tz is None), so each boundary takes the
        # UTC offset in force on its own date and a DST change inside the month cannot shift the period
        return _stamp(datetime(day.year, day.month, day.day).astimezone() if self._tz is None
                      else datetime(day.year, day.month, day.day, tzinfo=self._tz))

    def _sums(self, conn: sqlite3.Connection, now: datetime) -> tuple[int, int, str, str]:
        # ONE statement, so the month and the day are read from one snapshot even outside a transaction (the day
        # always lies inside its month)
        m0, m1, d0, d1 = self._periods(now)
        month, day = conn.execute(
            "SELECT COALESCE(SUM(cost_micros), 0), "
            "COALESCE(SUM(CASE WHEN at >= ? AND at < ? THEN cost_micros ELSE 0 END), 0) "
            f"FROM calls WHERE {_COUNTED} AND at >= ? AND at < ?", (d0, d1, m0, m1)).fetchone()
        return month, day, m0, d0

    # --- the check, and the rows --------------------------------------------------------------------------------------
    def reserve(self, provider: str, use: str, model: str, units: int) -> Check:
        """Price a call, check it against both caps and, if it fits, record it, all in one write transaction.

        ``units`` is characters for ``tts`` and estimated seconds for ``stt``; a call is priced at no less than one
        unit, since a call that goes out is never free. A call fits when what is already counted plus its estimate is
        AT MOST each cap, and a cap of 0 refuses every call. On a refusal nothing is written. The clock is read AFTER the
        write lock is held, so a call that waited for the lock across midnight counts in the day it was let through.
        The caller then makes the call and ends with :meth:`settle` or :meth:`release`; the returned
        ``reservation_id`` names the row."""
        if use not in USES:
            raise MeterError(f"use {use!r} is not one of {USES}")
        if isinstance(units, bool) or not isinstance(units, int) or units < 0:
            raise MeterError(f"units must be a non-negative int, got {units!r}")
        entry = self.prices.find(provider, use, model)
        try:
            return self._reserve(entry, provider=provider, use=use, model=model, units=units)
        except sqlite3.Error as exc:
            failed = type(exc).__name__
        # raised outside the handler, so a caller's traceback shows no SQLite chain (HANDOFF_33: wrap raw sqlite errors)
        raise MeterError(f"the voice ledger could not be used: {failed}")

    def _reserve(self, entry: PriceEntry | None, *, provider: str, use: str, model: str, units: int) -> Check:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                now = self._clock()
                month, day, _, _ = self._sums(conn, now)
                if entry is None:
                    conn.execute("ROLLBACK")
                    return Check(False, UNPRICED, None, 0, month, day, self.caps)
                estimate = entry.cost(max(units, 1))
                if self.caps.month_micros == 0 or month + estimate > self.caps.month_micros:
                    reason = MONTH_CAP
                elif self.caps.day_micros == 0 or day + estimate > self.caps.day_micros:
                    reason = DAY_CAP
                else:
                    cur = conn.execute(
                        "INSERT INTO calls (at, provider, use, model, units, cost_micros, status) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'reserved')",
                        (_stamp(now), provider, use, model, units, estimate))
                    conn.execute("COMMIT")
                    return Check(True, OK, int(cur.lastrowid), estimate, month, day, self.caps)
                conn.execute("ROLLBACK")
                return Check(False, reason, None, estimate, month, day, self.caps)
            except BaseException:
                _rollback(conn)
                raise

    def settle(self, reservation_id: int) -> bool:
        """The call went out: the reserved row stays counted. True when a reserved row was moved, False when the id is
        unknown or already ended (so a second settle changes nothing)."""
        return self._end(reservation_id, "UPDATE calls SET status = 'ok' WHERE id = ? AND status = 'reserved'")

    def settle_actual(self, reservation_id: int, units: int) -> bool:
        """The call went out and ran for ``units`` (a streamed session's actual seconds): the reserved row stays counted
        at the cost of ``units``, priced at no less than one unit, and never above what was reserved (a zero-unit
        reservation counts as the one unit :meth:`reserve` priced it at), so a reservation is a ceiling that settling
        can only lower. True when a reserved row was moved, False when the id is unknown or
        already ended. A row whose model the table no longer prices keeps its reserved cost."""
        if isinstance(units, bool) or not isinstance(units, int) or units < 0:
            raise MeterError(f"units must be a non-negative int, got {units!r}")
        try:
            with closing(self._connect()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    row = conn.execute("SELECT provider, use, model, units, cost_micros FROM calls "
                                       "WHERE id = ? AND status = 'reserved'", (reservation_id,)).fetchone()
                    if row is None:
                        conn.execute("ROLLBACK")
                        return False
                    provider, use, model, reserved_units, reserved_cost = row
                    entry = self.prices.find(provider, use, model)
                    # the ceiling is what reserve PRICED: a zero-unit reservation was priced at one unit
                    final_units = (min(max(units, 1), max(reserved_units, 1)) if entry is not None
                                   else reserved_units)
                    cost = min(entry.cost(final_units), reserved_cost) if entry is not None else reserved_cost
                    conn.execute("UPDATE calls SET status = 'ok', units = ?, cost_micros = ? WHERE id = ?",
                                 (final_units, cost, reservation_id))
                    conn.execute("COMMIT")
                    return True
                except BaseException:
                    _rollback(conn)
                    raise
        except sqlite3.Error as exc:
            failed = type(exc).__name__
        raise MeterError(f"the voice ledger could not be used: {failed}")

    def release(self, reservation_id: int) -> bool:
        """The call failed before anything was served: the row stops counting and its cost reads zero. A call that
        reached the provider and failed AFTER it may have been charged; the caller settles that one instead."""
        return self._end(reservation_id, "UPDATE calls SET status = 'released', cost_micros = 0 "
                                         "WHERE id = ? AND status = 'reserved'")

    def _end(self, reservation_id: int, sql: str) -> bool:
        try:
            with closing(self._connect()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    changed = conn.execute(sql, (reservation_id,)).rowcount
                    conn.execute("COMMIT")
                    return changed == 1
                except BaseException:
                    _rollback(conn)
                    raise
        except sqlite3.Error as exc:
            failed = type(exc).__name__
        raise MeterError(f"the voice ledger could not be used: {failed}")

    # --- reads --------------------------------------------------------------------------------------------------------
    def spent(self) -> Spend:
        """The counted totals for the current local month and day (an estimate, dated by ``prices_as_of``)."""
        now = self._clock()
        try:
            with closing(self._connect()) as conn:
                month, day, m0, d0 = self._sums(conn, now)
            return Spend(month, day, m0, d0, self.prices.as_of)
        except sqlite3.Error as exc:
            failed = type(exc).__name__
        raise MeterError(f"the voice ledger could not be used: {failed}")

    def spent_if_present(self) -> Spend:
        """:meth:`spent` that creates nothing: with no ledger file yet the totals are zero and no file is made; an
        existing file is opened read-only (``mode=ro``), so a page's poll never writes the ledger. A file that is not
        a ledger, or a symlink, is a :class:`MeterError`."""
        now = self._clock()
        m0, _m1, d0, _d1 = self._periods(now)
        if os.path.islink(self.path):
            raise MeterError(f"voice ledger {self.path} is a symlink")
        if not self.path.exists():
            return Spend(0, 0, m0, d0, self.prices.as_of)
        try:
            uri = self.path.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=_BUSY_WAIT_S, isolation_level=None)) as conn:
                # the same format test the normal connection path runs (:meth:`_create`), but a missing row is a
                # refusal here: this path never writes one, and a foreign database with a compatible ``calls``
                # table must not read as a ledger
                row = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
                _need_format(row[0] if row is not None else None)
                month, day, m0, d0 = self._sums(conn, now)
            return Spend(month, day, m0, d0, self.prices.as_of)
        except sqlite3.Error as exc:
            failed = type(exc).__name__
        raise MeterError(f"the voice ledger could not be read: {failed}")

    def spent_by_provider_if_present(self) -> dict[str, dict[str, tuple[int, int]]]:
        """Counted calls per provider and period, read-only and creating nothing: ``{provider: {"day": (calls, micros),
        "month": (calls, micros)}}``. Same rows and periods as :meth:`spent_if_present` (reserved and ok, never released);
        a missing ledger is ``{}``. An estimate like every figure here. See :meth:`snapshot_if_present`."""
        return self.snapshot_if_present()[1]

    def snapshot_if_present(self) -> tuple[Spend, dict[str, dict[str, tuple[int, int]]]]:
        """The totals AND the per-provider rows from ONE clock read and ONE statement, so the two always agree: the totals
        are the sum of the rows. Read-only, creates nothing (a missing ledger is zero and ``{}``); a symlink or a file
        that is not a ledger is a :class:`MeterError`."""
        now = self._clock()
        m0, m1, d0, d1 = self._periods(now)
        if os.path.islink(self.path):
            raise MeterError(f"voice ledger {self.path} is a symlink")
        if not self.path.exists():
            return Spend(0, 0, m0, d0, self.prices.as_of), {}
        try:
            uri = self.path.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=_BUSY_WAIT_S, isolation_level=None)) as conn:
                row = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
                _need_format(row[0] if row is not None else None)
                rows = conn.execute(
                    "SELECT provider, COUNT(*), COALESCE(SUM(cost_micros), 0), "
                    "COALESCE(SUM(CASE WHEN at >= ? AND at < ? THEN 1 ELSE 0 END), 0), "
                    "COALESCE(SUM(CASE WHEN at >= ? AND at < ? THEN cost_micros ELSE 0 END), 0) "
                    f"FROM calls WHERE {_COUNTED} AND at >= ? AND at < ? GROUP BY provider",
                    (d0, d1, d0, d1, m0, m1)).fetchall()
            by = {r[0]: {"month": (r[1], r[2]), "day": (r[3], r[4])} for r in rows}
            month = sum(v["month"][1] for v in by.values())
            day = sum(v["day"][1] for v in by.values())
            return Spend(month, day, m0, d0, self.prices.as_of), by
        except sqlite3.Error as exc:
            failed = type(exc).__name__
        raise MeterError(f"the voice ledger could not be read: {failed}")

    def ledger(self, limit: int | None = None) -> list[LedgerRow]:
        """Rows newest first, for checking the estimate against a provider's dashboard."""
        sql = "SELECT id, at, provider, use, model, units, cost_micros, status FROM calls ORDER BY id DESC"
        args: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            args = (int(limit),)
        try:
            with closing(self._connect()) as conn:
                return [LedgerRow(*r) for r in conn.execute(sql, args)]
        except sqlite3.Error as exc:
            failed = type(exc).__name__
        raise MeterError(f"the voice ledger could not be used: {failed}")


def _need_format(found: object) -> None:
    """The ledger's format stamp is :data:`FORMAT`, else a :class:`MeterError` (the one test both the creating path and
    the read-only one run)."""
    if found != FORMAT:
        raise MeterError(f"voice ledger format {found!r} is not {FORMAT!r}")


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


# --- the fallback decision (pure) -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Route:
    """Where a spoken or dictated request goes. ``kind`` is ``premium`` (``provider`` names which), ``on_device`` (the
    page's own voice) or ``text_only`` (nothing is spoken or transcribed; the text stays text). ``reasons`` lists
    (provider, outcome) for every candidate passed over, in preference order, and ``reason`` is the first, which is the
    one the operator is told about because it is about the provider they chose."""
    kind: str
    provider: str | None
    reasons: tuple[tuple[str, str], ...]

    @property
    def reason(self) -> str:
        return self.reasons[0][1] if self.reasons else OK


def choose_route(candidates: Sequence[str], outcomes: Mapping[str, str], *, on_device_available: bool) -> Route:
    """Pick the first candidate whose outcome is :data:`OK` or not yet known (absent from ``outcomes``: not tried, so try
    it). Any other outcome string, whatever it names (a cap, a refused key, a 429, a timeout), passes that candidate
    over. With none left the route is the on-device voice if the page has one, else text only. Pure: no clock, no
    ledger, no I/O, so the cases are a table."""
    passed: list[tuple[str, str]] = []
    for name in candidates:
        outcome = outcomes.get(name, OK)
        if outcome == OK:
            return Route("premium", name, tuple(passed))
        passed.append((name, outcome))
    return Route("on_device" if on_device_available else "text_only", None, tuple(passed))
