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


def test_an_ineligible_origin_removes_the_stored_one(tmp_path):
    co.write_origin(tmp_path, "https://box.ts.net")
    assert co.write_origin(tmp_path, "http://localhost:1") is None
    assert co.read_origin(tmp_path) is None
    co.write_origin(tmp_path, "https://box.ts.net")
    assert co.write_origin(tmp_path, None) is None and not (tmp_path / co.FILE_NAME).exists()


def test_a_failed_write_leaves_no_old_value(tmp_path, monkeypatch):
    co.write_origin(tmp_path, "https://box.ts.net")

    def boom(*a, **k):
        raise OSError("disk")
    monkeypatch.setattr(co.os, "fsync", boom)
    assert co.write_origin(tmp_path, "https://new.ts.net") is None
    assert co.read_origin(tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_withdraw_removes_only_its_own_value(tmp_path):
    co.write_origin(tmp_path, "https://box.ts.net")
    co.withdraw_origin(tmp_path, "https://other.ts.net")
    assert co.read_origin(tmp_path) == "https://box.ts.net"
    co.withdraw_origin(tmp_path, "https://box.ts.net")
    assert co.read_origin(tmp_path) is None
    co.withdraw_origin(tmp_path, "https://box.ts.net")           # missing: no error


def test_a_missing_queue_dir_is_created_private(tmp_path):
    q = tmp_path / "x" / "queue"
    assert co.write_origin(q, "https://box.ts.net") is not None
    assert stat.S_IMODE(q.stat().st_mode) == 0o700 and co.read_origin(q) == "https://box.ts.net"


def test_a_fifo_at_the_path_reads_none_and_builds_no_link(tmp_path):
    import os
    from levain.band_sources import _sign_link

    os.mkfifo(tmp_path / co.FILE_NAME)
    assert co.read_origin(tmp_path) is None
    assert _sign_link(tmp_path, "np-aaa-0000", "a" * 16) is None


@pytest.mark.parametrize("rid,version", [(42, "a" * 16), ("np-aaa-0000", None), ("np-aaa-0000", 7),
                                         ("../../x\n", "a" * 16), (None, None), (["x"], {})])
def test_a_weird_request_id_or_version_gives_no_link_and_no_exception(tmp_path, rid, version):
    from levain.band_sources import _sign_link

    co.write_origin(tmp_path, "https://box.ts.net")
    link = _sign_link(tmp_path, rid, version)
    assert link is None or link.startswith("https://box.ts.net/sign/")
    assert _sign_link(tmp_path, 42, "a" * 16) is None
