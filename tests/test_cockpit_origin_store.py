"""levain.consent.cockpit_origin: the stored cockpit origin is written whole at 0600 and only a valid https one."""
from __future__ import annotations

import stat

import pytest

from levain.consent import cockpit_origin as co


def test_a_valid_origin_round_trips_at_0600_and_nothing_stored_reads_none(tmp_path):
    assert co.read_origin(tmp_path) is None
    path = co.write_origin(tmp_path, "https://Box.ts.net:8443")
    assert path is not None and co.read_origin(tmp_path) == "https://box.ts.net:8443"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == [co.FILE_NAME]            # no temp file left


@pytest.mark.parametrize("bad", [None, 5, "", "http://box.ts.net", "https://box.ts.net/", "https://box.ts.net/x",
                                 "https://u@box.ts.net", "https://box.ts.net\n", "https://" + "a" * 5000,
                                 "http://localhost:8080", "https://1.2.3.4"])
def test_a_bad_origin_is_not_written(tmp_path, bad):
    assert co.write_origin(tmp_path, bad) is None
    assert list(tmp_path.iterdir()) == []
