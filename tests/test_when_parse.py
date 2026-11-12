"""Tests for ``levain.when_parse`` — the List's reminder time grammar (row 188, parser slice).

Every clock in this file is an argument: ``NOW`` and the sweeps below are fixed instants, and one test pins that the module
never reads the system clock, so no case here depends on the hour the suite runs at.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from levain import when_parse
from levain.when_parse import When, WhenRefused, is_remind, parse_remind, read_when_text

NOW = datetime(2026, 11, 10, 14, 0)          # a Tuesday
VECTORS = Path(__file__).parent / "vectors" / "list_intake_vectors.json"
_CANON = re.compile(r"^(daily \d\d:\d\d|weekly (MON|TUE|WED|THU|FRI|SAT|SUN)(,(MON|TUE|WED|THU|FRI|SAT|SUN))* \d\d:\d\d"
                    r"|monthly \d{1,2} \d\d:\d\d|every \d{1,3} days \d\d:\d\d)$")


def remind(line, now=NOW):
    return parse_remind(line, now)


def refused(line, now=NOW):
    with pytest.raises(WhenRefused) as ei:
        parse_remind(line, now)
    assert str(ei.value).strip()
    return str(ei.value)


# --- the reproduced failures (red on the intake grammar before this module) -------------------------------------------

def test_a_remind_line_reads_as_a_remind_not_a_sort():
    assert remind("remind me thu 3pm call") == {"kind": "remind", "when": "2026-11-12T15:00", "text": "call"}


def test_at_before_a_time_is_consumed_not_left_in_the_words():
    reading, words = read_when_text("thu at 3pm call dentist", NOW)
    assert (reading.iso, words) == ("2026-11-12T15:00", "call dentist")


def test_a_bare_hour_after_a_day_is_refused_not_read_as_a_word():
    with pytest.raises(WhenRefused, match="3pm"):
        read_when_text("thu 3 call dentist", NOW)
    refused("remind me thu 3 call dentist")


def test_tonight_reads_and_a_desk_drop_cannot_repeat():
    assert read_when_text("tonight call", NOW)[0].iso == "2026-11-10T20:00"
    with pytest.raises(WhenRefused, match="desk drop cannot repeat"):
        read_when_text("every sun 9pm bins", NOW)
    assert remind("remind every sun 9pm bins") == {"kind": "remind", "when": "2026-11-15T21:00", "text": "bins",
                                                    "recur": "weekly SUN 21:00"}


# --- the shape test and prose ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("line", ["remind me to call Jim", "remind Som about dinner", "remind", "remind me", "remind me,",
                                  "reminder thu 3pm x", "buy milk", "email Tim remind me thu 3pm", "", "   "])
def test_prose_and_near_misses_are_not_a_remind(line):
    assert is_remind(line) is False
    assert remind(line) is None


@pytest.mark.parametrize("line", ["remind me thu 3pm call", "Remind Me Thu 3pm call", "REMIND thu 3pm call",
                                  "remind me at 3pm call", "remind me noon call", "remind me +2h call", "remind me 11-20 x",
                                  "remind me tonight x", "remind me daily x", "remind me every mon x", "remind me weekly mon x",
                                  "remind me monthly 15 x", "remind me next thu x", "remind me today x", "remind me 3 x"])
def test_a_time_word_right_after_remind_makes_it_a_remind(line):
    assert is_remind(line) is True


def test_the_tier_shape_test_and_the_parse_agree_on_a_corpus():
    corpus = ["remind me thu 3pm call", "remind me to call Jim", "remind me next thu x", "remind me every week x",
              "remind me 3 call", "remind me tomorrow", "remind Som about x", "remind me at the office", "remind me at 5 x",
              "remind me every sun 9pm bins", "buy milk", "remind me zzz", "remind me today 9am x", "remind me 25:00 x"]
    for line in corpus:
        try:
            parsed = parse_remind(line, NOW)
        except WhenRefused:
            parsed = "refused"
        assert is_remind(line) == (parsed is not None), line


def test_at_without_a_time_is_words_not_a_remind():
    assert is_remind("remind me at the office") is False


# --- one-off readings (fixed clock: Tue 2026-11-10 14:00) ---------------------------------------------------------------

@pytest.mark.parametrize("body,when,words", [
    ("9am x", "2026-11-11T09:00", "x"),                  # a time already past today is tomorrow's
    ("3pm x", "2026-11-10T15:00", "x"),
    ("at 3pm x", "2026-11-10T15:00", "x"),
    ("15:30 x", "2026-11-10T15:30", "x"),
    ("5:30 pm x", "2026-11-10T17:30", "x"),
    ("noon x", "2026-11-11T12:00", "x"),
    ("midnight x", "2026-11-11T00:00", "x"),
    ("12am x", "2026-11-11T00:00", "x"),
    ("12pm x", "2026-11-11T12:00", "x"),
    ("tomorrow x", "2026-11-11T09:00", "x"),
    ("tomorrow at 7:15am x", "2026-11-11T07:15", "x"),
    ("tomorrow midnight x", "2026-11-11T00:00", "x"),
    ("today 5pm x", "2026-11-10T17:00", "x"),
    ("tonight x", "2026-11-10T20:00", "x"),
    ("tonight 9pm x", "2026-11-10T21:00", "x"),
    ("tonight 11:30pm x", "2026-11-10T23:30", "x"),
    ("thu x", "2026-11-12T09:00", "x"),
    ("Thursday at 3pm x", "2026-11-12T15:00", "x"),
    ("tue 5pm x", "2026-11-10T17:00", "x"),             # today counts while its time is ahead
    ("tue 9am x", "2026-11-17T09:00", "x"),             # and rolls a week when it is not
    ("sun noon x", "2026-11-15T12:00", "x"),
    ("11-20 x", "2026-11-20T09:00", "x"),
    ("11-20 7:30pm x", "2026-11-20T19:30", "x"),
    ("11-10 3pm x", "2026-11-10T15:00", "x"),
    ("11-09 x", "2027-11-09T09:00", "x"),                # a yearless date that has passed is the next year's
    ("2027-01-02 x", "2027-01-02T09:00", "x"),
    ("+90m x", "2026-11-10T15:30", "x"),
    ("+2h x", "2026-11-10T16:00", "x"),
    ("+3d x", "2026-11-13T14:00", "x"),
    ("thu 9am 2 tickets", "2026-11-12T09:00", "2 tickets"),   # a bare number after the time stays a word
    ("thu 45 widgets", "2026-11-12T09:00", "45 widgets"),     # too big to be an hour: a word
    ("tomorrow 2026 plan", "2026-11-11T09:00", "2026 plan"),
])
def test_one_off_forms(body, when, words):
    reading, got = read_when_text(body, NOW)
    assert (reading.iso, got, reading.recur) == (when, words, None)


def test_a_thursday_said_on_a_thursday_before_and_after_its_time():
    thu = datetime(2026, 11, 12, 10, 0)
    assert read_when_text("thu 3pm x", thu)[0].iso == "2026-11-12T15:00"
    assert read_when_text("thu 9am x", thu)[0].iso == "2026-11-19T09:00"
    assert read_when_text("thu 10:00 x", thu)[0].iso == "2026-11-19T10:00"      # the same minute is not ahead


def test_the_minute_before_midnight_and_the_next_day():
    late = datetime(2026, 11, 10, 23, 59)
    assert read_when_text("+1m x", late)[0].iso == "2026-11-11T00:00"
    assert read_when_text("tomorrow x", late)[0].iso == "2026-11-11T09:00"
    assert read_when_text("11:59pm x", late)[0].iso == "2026-11-11T23:59"
    with pytest.raises(WhenRefused, match="passed"):
        read_when_text("tonight x", late)


def test_the_year_seam_and_the_leap_day():
    dec = datetime(2026, 12, 28, 12, 0)
    assert read_when_text("01-03 x", dec)[0].iso == "2027-01-03T09:00"
    assert read_when_text("02-29 x", NOW)[0].iso == "2028-02-29T09:00"
    with pytest.raises(WhenRefused, match="not a calendar date"):
        read_when_text("2027-02-29 x", NOW)


def test_the_comma_a_phone_adds_is_not_part_of_a_word_that_names_the_time():
    assert read_when_text("Thu, 3pm, x", NOW)[0].iso == "2026-11-12T15:00"
    assert remind("remind me thu, 3pm, to call")["text"] == "call"


# --- today / tonight / past ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("body", ["today x", "today 9am x", "today 2:00pm x", "tonight 1pm x"])
def test_an_explicit_today_with_a_past_time_is_refused_not_rolled(body):
    with pytest.raises(WhenRefused, match="passed"):
        read_when_text(body, NOW)


def test_tonight_with_a_morning_time_is_refused():
    with pytest.raises(WhenRefused, match="evening"):
        read_when_text("tonight 8am x", NOW)


@pytest.mark.parametrize("body", ["2026-11-01 x", "+0m x", "2026-11-10 9am x"])
def test_a_moment_not_in_the_future_is_refused(body):
    with pytest.raises(WhenRefused, match="not in the future"):
        read_when_text(body, NOW)


# --- bare numbers and unreadable times -----------------------------------------------------------------------------------

@pytest.mark.parametrize("body", ["thu 3 x", "tomorrow 9 x", "at 3 x", "thu at 3 x", "3 x", "11-20 7 x", "tonight 9 x", "today 5 x"])
def test_a_bare_hour_in_a_time_slot_is_refused_with_the_fix(body):
    with pytest.raises(WhenRefused, match=r"am/pm"):
        read_when_text(body, NOW)


@pytest.mark.parametrize("body", ["tomorrow 25:00 x", "tomorrow 9:60 x", "tomorrow 13pm x", "tomorrow 0am x", "thu at 45 x", "blorp x",
                                  "?", "tmrw x", "next x", "11-31 x", ""])
def test_a_time_that_does_not_read_is_refused(body):
    with pytest.raises(WhenRefused):
        read_when_text(body, NOW)


def test_next_weekday_reads_two_ways_and_is_refused():
    with pytest.raises(WhenRefused, match="two ways"):
        read_when_text("next thu x", NOW)
    assert "two ways" in refused("remind me next thu x")


def test_a_bare_hour_after_a_repeat_day_is_refused_too():
    for line in ("remind me daily 3 pills", "remind me every sun 9 bins", "remind me every 3 days 8 x", "remind me every month 15 3 x"):
        assert "am/pm" in refused(line), line


# --- repeating phrases -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("line,when,recur,text", [
    ("remind me every day x", "2026-11-11T09:00", "daily 09:00", "x"),
    ("remind me every day at 6pm x", "2026-11-10T18:00", "daily 18:00", "x"),
    ("remind me daily 8:30am x", "2026-11-11T08:30", "daily 08:30", "x"),
    ("remind me every weekday 7am x", "2026-11-11T07:00", "weekly MON,TUE,WED,THU,FRI 07:00", "x"),
    ("remind me every sun 9pm x", "2026-11-15T21:00", "weekly SUN 21:00", "x"),
    ("remind me every Sunday x", "2026-11-15T09:00", "weekly SUN 09:00", "x"),
    ("remind me every mon,wed x", "2026-11-11T09:00", "weekly MON,WED 09:00", "x"),
    ("remind me every mon, wed and fri 8am x", "2026-11-11T08:00", "weekly MON,WED,FRI 08:00", "x"),
    ("remind me every fri and mon x", "2026-11-13T09:00", "weekly MON,FRI 09:00", "x"),
    ("remind me every mon and mon x", "2026-11-16T09:00", "weekly MON 09:00", "x"),
    ("remind me weekly tue 5pm x", "2026-11-10T17:00", "weekly TUE 17:00", "x"),
    ("remind me every tue 9am x", "2026-11-17T09:00", "weekly TUE 09:00", "x"),
    ("remind me every other thu 10am x", "2026-11-12T10:00", "every 14 days 10:00", "x"),
    ("remind me every 3 days 6pm x", "2026-11-10T18:00", "every 3 days 18:00", "x"),
    ("remind me every 3 days 9am x", "2026-11-13T09:00", "every 3 days 09:00", "x"),
    ("remind me every 1 day 6pm x", "2026-11-10T18:00", "every 1 days 18:00", "x"),
    ("remind me every 2 weeks 6pm x", "2026-11-10T18:00", "every 14 days 18:00", "x"),
    ("remind me every month on the 15th x", "2026-11-15T09:00", "monthly 15 09:00", "x"),
    ("remind me every month 15 8pm x", "2026-11-15T20:00", "monthly 15 20:00", "x"),
    ("remind me monthly 1st x", "2026-12-01T09:00", "monthly 1 09:00", "x"),
    ("remind me monthly 10th 5pm x", "2026-11-10T17:00", "monthly 10 17:00", "x"),
    ("remind me monthly 31 x", "2026-11-30T09:00", "monthly 31 09:00", "x"),
    ("remind me every sun x to the store", "2026-11-15T09:00", "weekly SUN 09:00", "x to the store"),
    ("remind me every sun 9pm to bins", "2026-11-15T21:00", "weekly SUN 21:00", "bins"),
])
def test_repeating_phrases_compile_to_a_canon_rule_and_its_first_occurrence(line, when, recur, text):
    got = remind(line)
    assert got == {"kind": "remind", "when": when, "text": text, "recur": recur}
    assert _CANON.match(got["recur"])


def test_the_first_occurrence_is_strictly_after_now():
    at = datetime(2026, 11, 10, 14, 0)
    assert remind("remind me daily 2pm x", at)["when"] == "2026-11-11T14:00"
    assert remind("remind me every tue 2pm x", at)["when"] == "2026-11-17T14:00"
    assert remind("remind me monthly 10 2pm x", at)["when"] == "2026-12-10T14:00"
    assert remind("remind me every 3 days 2pm x", at)["when"] == "2026-11-13T14:00"


def test_monthly_past_a_months_end_rings_on_its_last_day():
    assert remind("remind me monthly 31 x", datetime(2027, 2, 3, 9, 0))["when"] == "2027-02-28T09:00"
    assert remind("remind me monthly 31 x", datetime(2027, 4, 3, 9, 0))["when"] == "2027-04-30T09:00"
    assert remind("remind me monthly 30 x", datetime(2028, 2, 3, 9, 0))["when"] == "2028-02-29T09:00"
    assert remind("remind me monthly 15 x", datetime(2026, 12, 20, 9, 0))["when"] == "2027-01-15T09:00"


@pytest.mark.parametrize("now", [datetime(2026, 3, 7, 12, 0), datetime(2026, 10, 31, 12, 0)])
def test_a_daylight_saving_week_keeps_the_wall_clock_hour(now):
    # the occurrence is a calendar date plus a wall-clock time, never 24 h added to an instant
    assert remind("remind me daily 9am x", now)["when"].endswith("T09:00")
    nxt = now + timedelta(days=1, hours=21)                 # the morning after the change day
    assert remind("remind me daily 9am x", nxt)["when"].endswith("T09:00")
    assert remind("remind me every 14 days 9am x", now)["when"].endswith("T09:00")


@pytest.mark.parametrize("line,word", [
    ("remind me every week x", "which day"),
    ("remind me every month x", "which day of the month"),
    ("remind me weekly x", "which day"),
    ("remind me monthly x", "which day of the month"),
    ("remind me monthly 32 x", "not a day of the month"),
    ("remind me monthly 0 x", "not a day of the month"),
    ("remind me every year x", "not a repeat"),
    ("remind me every weekend x", "not a repeat"),
    ("remind me every 2nd monday x", "nth weekday"),
    ("remind me every last friday x", "not a repeat"),
    ("remind me every 53 weeks x", "out of range"),
    ("remind me every 367 days x", "out of range"),
    ("remind me every 0 days x", "out of range"),
    ("remind me every 3", "every 3 what"),
    ("remind me every 3 fortnights x", "not a repeat"),
    ("remind me every other x", "one weekday"),
    ("remind me every other mon,wed x", "one weekday"),
    ("remind me every", "every what"),
    ("remind me daily", "no words"),
])
def test_what_the_store_cannot_hold_is_refused_with_a_reason(line, word):
    assert word in refused(line)


def test_every_limit_is_inclusive():
    assert remind("remind me every 366 days 6pm x")["recur"] == "every 366 days 18:00"
    assert remind("remind me every 52 weeks 6pm x")["recur"] == "every 364 days 18:00"


# --- the words -------------------------------------------------------------------------------------------------------------

def test_one_leading_to_is_dropped_and_nothing_else_is():
    assert remind("remind me thu 3pm to call Jim")["text"] == "call Jim"
    assert remind("remind me thu 3pm TO call Jim")["text"] == "call Jim"
    assert remind("remind me thu 3pm to to call")["text"] == "to call"
    assert remind("remind me thu 3pm tomorrow's list")["text"] == "tomorrow's list"
    assert remind("remind me thu 3pm Call   Jim,  NOW")["text"] == "Call   Jim,  NOW"
    assert remind("  remind   me   thu   3pm   call  ")["text"] == "call"


@pytest.mark.parametrize("line", ["remind me thu 3pm", "remind me thu 3pm to", "remind me tomorrow", "remind me +2h"])
def test_no_words_after_the_time_is_refused(line):
    assert "no words" in refused(line)


def test_trailing_time_words_are_not_parsed():
    assert remind("remind me to call the dentist thu 3pm") is None


# --- @-style readings through read_when_text, against the shipped intake vectors -------------------------------------

_VEC = json.loads(VECTORS.read_text(encoding="utf-8"))
_VNOW = datetime.fromisoformat(_VEC["now"])
# A vector whose reading this grammar deliberately changes: a bare "3" after a day reads as an hour and is refused (the
# design's reproduced misparse); the shipped vectors keep the old reading until the wiring slice re-vendors them.
_CHANGED_BY_DESIGN = {"@tomorrow 3 things to do"}


@pytest.mark.parametrize("case", [c for c in _VEC["cases"] if c["line"].startswith("@")], ids=lambda c: repr(c["line"]))
def test_the_at_vectors_read_the_same(case):
    body = case["line"][1:]
    if case["line"] in _CHANGED_BY_DESIGN:
        with pytest.raises(WhenRefused, match="am/pm"):
            read_when_text(body, _VNOW)
    elif case.get("refuse"):
        try:
            _, words = read_when_text(body, _VNOW)
        except WhenRefused:
            return
        assert words == "", "a refused @ line either fails to read or has no words"
    else:
        reading, words = read_when_text(body, _VNOW)
        assert {"kind": "drop", "when": reading.iso, "text": words} == case["expect"]


# --- structure ---------------------------------------------------------------------------------------------------------------

def test_the_module_never_reads_the_system_clock():
    src = Path(when_parse.__file__).read_text(encoding="utf-8")
    code = "\n".join(line.split("#")[0] for line in src.splitlines())
    assert not re.search(r"\.(now|today|utcnow|fromtimestamp)\(|\btime\.(time|monotonic)\b|\bimport time\b", code)


def test_an_aware_now_is_refused_loudly_and_seconds_are_ignored():
    from datetime import timezone
    with pytest.raises(TypeError):
        read_when_text("+1h x", datetime(2026, 11, 10, 14, 0, tzinfo=timezone.utc))
    n = datetime(2026, 11, 10, 14, 0, 45)
    assert read_when_text("+1m x", n)[0].iso == "2026-11-10T14:01"
    assert n.second == 45


def test_when_is_a_value():
    w = When(datetime(2026, 11, 10, 14, 0), "daily 09:00")
    assert w.iso == "2026-11-10T14:00" and w == When(datetime(2026, 11, 10, 14, 0), "daily 09:00")


def test_a_refusal_is_a_valueerror():
    assert issubclass(WhenRefused, ValueError)


_SWEEP_LINES = [
    "remind me 9am x", "remind me 11pm x", "remind me noon x", "remind me midnight x", "remind me tomorrow x",
    "remind me tomorrow 12am x", "remind me thu x", "remind me sun 11:59pm x", "remind me tonight x", "remind me tonight 11pm x",
    "remind me +1m x", "remind me +1h x", "remind me 12-31 x", "remind me 01-01 x", "remind me daily 12am x",
    "remind me daily 11:59pm x", "remind me every weekday x", "remind me every mon,thu 10pm x", "remind me every other sat x",
    "remind me every 2 days 3am x", "remind me every 30 days 12pm x", "remind me monthly 31 12am x",
    "remind me every month 29 11pm x", "remind me every sun 9pm x",
]


def test_every_line_reads_ahead_of_any_clock_at_any_hour():
    """The class that failed the land CI once: a case green at some hours of the day only. Each line is read against a
    clock at every hour and half hour of a week (crossing a month end and a leap February), and the result is always a
    minute strictly after ``now``, on a minute boundary, in a shape the agenda store reads."""
    starts = [datetime(2026, 11, 8, 0, 0), datetime(2028, 2, 25, 0, 0), datetime(2026, 12, 29, 0, 0)]
    for start in starts:
        for step in range(7 * 48):
            now = start + timedelta(minutes=30 * step)
            for line in _SWEEP_LINES:
                try:
                    got = parse_remind(line, now)
                except WhenRefused as exc:
                    assert "passed" in str(exc), (line, now, exc)      # only an explicit past tonight may refuse here
                    assert "tonight" in line
                    continue
                when = datetime.strptime(got["when"], "%Y-%m-%dT%H:%M")
                assert when > now, (line, now, got)
                assert when - now < timedelta(days=366 * 8), (line, now, got)
                if "recur" in got:
                    assert _CANON.match(got["recur"]), got


def test_the_reading_is_the_same_whichever_instant_inside_a_minute_asks():
    a = remind("remind me daily 2pm x", datetime(2026, 11, 10, 13, 59, 0))
    b = remind("remind me daily 2pm x", datetime(2026, 11, 10, 13, 59, 59, 999999))
    assert a == b


# --- agreement with flow's recurrence arithmetic (the canon rules are flow's; skipped where flow is not on the machine) ----

def _flow_module():
    path = os.environ.get("LEVAIN_FLOW_INTENTIONS")
    if not path or not Path(path).is_file():
        pytest.skip("LEVAIN_FLOW_INTENTIONS (flow's scripts/intentions.py) is not set")
    sys.path.insert(0, str(Path(path).parent))
    try:
        spec = importlib.util.spec_from_file_location("flow_intentions_for_when_parse", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(Path(path).parent))
    return mod


_PARITY_LINES = [
    "remind me daily x", "remind me every day 6pm x", "remind me every weekday 7am x", "remind me every sun 9pm x",
    "remind me every mon,wed,fri 8:30am x", "remind me every 3 days 6pm x", "remind me every 2 weeks 9am x",
    "remind me every month on the 15th x", "remind me monthly 31 x", "remind me monthly 29 8pm x",
]


def test_the_canon_and_the_first_occurrence_equal_flows():
    flow = _flow_module()
    starts = [datetime(2026, 11, 10, 14, 0), datetime(2027, 1, 31, 23, 30), datetime(2028, 2, 28, 9, 0),
              datetime(2026, 3, 8, 1, 59), datetime(2026, 11, 1, 1, 30), datetime(2026, 12, 31, 23, 59)]
    for now in starts:
        for hour in range(0, 24, 5):
            at = now.replace(hour=hour)
            for line in _PARITY_LINES:
                got = parse_remind(line, at)
                assert flow.parse_recur(got["recur"])["canon"] == got["recur"], got
                assert flow.next_occurrence(got["recur"], at) == datetime.strptime(got["when"], "%Y-%m-%dT%H:%M"), (line, at, got)


def test_every_other_weekday_keeps_the_fortnight_through_the_anchor():
    flow = _flow_module()
    got = parse_remind("remind me every other thu 10am x", NOW)
    first = datetime.strptime(got["when"], "%Y-%m-%dT%H:%M")
    assert first.weekday() == 3
    # the writer stores recur_anchor == the first minute; flow's anchored arithmetic then lands a fortnight on
    assert flow.next_occurrence(got["recur"], first, prev_next=got["when"]) == first + timedelta(days=14)
