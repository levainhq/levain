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


def test_a_terminated_line_that_does_not_parse_fails_at_once(tmp_path) -> None:
    p = tmp_path / "ledger.jsonl"
    p.write_bytes(b'{"kind": "reserved"}\n{not json}\n')
    start = time.monotonic()
    with pytest.raises(json.JSONDecodeError):
        file_lines(str(p))
    assert time.monotonic() - start < 0.5


def test_a_last_line_that_never_finishes_fails_after_the_bounded_wait(tmp_path) -> None:
    p = tmp_path / "ledger.jsonl"
    p.write_bytes(b'{"kind": "reserved"}\n{"kind": "sett')
    with pytest.raises(json.JSONDecodeError):
        file_lines(str(p), wait_s=0.2)
