"""``file_lines`` reads a ledger a writer thread may be appending to: a last line without its newline is a line still being
written (CI: ``test_client_chosen_refusal_names_past_the_window_cap_fold_into_the_bare_reason`` died with a
``JSONDecodeError`` on exactly such a read), so the read waits a bounded time for it; a line that IS terminated and does not
parse is corruption and fails at once."""
from __future__ import annotations

import json
import threading
import time

import pytest

from tests.ledger_support import file_lines


def test_a_last_line_still_being_appended_is_waited_for(tmp_path) -> None:
    p = tmp_path / "ledger.jsonl"
    p.write_bytes(b'{"kind": "reserved", "seq": 1}\n{"kind": "sett')

    def finish() -> None:
        time.sleep(0.15)
        with open(p, "ab") as f:
            f.write(b'led", "seq": 2}\n')

    t = threading.Thread(target=finish)
    t.start()
    try:
        got = file_lines(str(p))
    finally:
        t.join()
    assert [o["kind"] for o in got] == ["reserved", "settled"]


def test_a_terminated_line_that_does_not_parse_fails_at_once(tmp_path, monkeypatch) -> None:
    p = tmp_path / "ledger.jsonl"
    p.write_bytes(b'{"kind": "reserved"}\n{not json}\n')

    def no_wait(_s: float) -> None:
        raise AssertionError("the retry path was entered for a terminated line")

    monkeypatch.setattr("tests.ledger_support.time.sleep", no_wait)
    with pytest.raises(json.JSONDecodeError):
        file_lines(str(p))


def test_a_last_line_cut_inside_a_multibyte_character_is_waited_for(tmp_path) -> None:
    p = tmp_path / "ledger.jsonl"
    whole = '{"kind": "noted", "who": "José"}\n'.encode("utf-8")
    cut = whole.index(b"\xc3") + 1                      # the first byte of the two-byte é is written, the second is not yet
    p.write_bytes(b'{"kind": "reserved"}\n' + whole[:cut])

    def finish() -> None:
        time.sleep(0.15)
        with open(p, "ab") as f:
            f.write(whole[cut:])

    t = threading.Thread(target=finish)
    t.start()
    try:
        got = file_lines(str(p))
    finally:
        t.join()
    assert [o["kind"] for o in got] == ["reserved", "noted"] and got[1]["who"] == "José"


def test_a_last_line_that_never_finishes_fails_after_the_bounded_wait(tmp_path) -> None:
    p = tmp_path / "ledger.jsonl"
    p.write_bytes(b'{"kind": "reserved"}\n{"kind": "sett')
    with pytest.raises(json.JSONDecodeError):
        file_lines(str(p), wait_s=0.2)
