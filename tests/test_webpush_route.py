"""The retired route (desk | devices): what is left of it. The route was folded into the one intent record (presence 1b):
`devices` is devices on, `desk` is follow. What stays is the pure `who` parse, the safe reader of an old route.json, and the
mapping of that file onto the record by the migration."""
from __future__ import annotations

import json

import pytest

from levain.webpush import intent, presence, route

NOW = 1_000_000.0


@pytest.fixture(autouse=True)
def _as_operator(monkeypatch):
    from levain.consent import band

    monkeypatch.setattr(band, "agent_process", lambda: None)


def _raw(tmp_path, text):
    d = tmp_path / "webpush"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    (d / route.FILE_NAME).write_text(text)


def _old(tmp_path, **rec):
    _raw(tmp_path, json.dumps({"route": "devices", "ts": NOW - 100, "until": NOW + 3600, "source": "operator", **rec}))


def test_nothing_writes_the_route_or_flips_the_record_on_its_own():
    """The one automatic writer of intent (auto_suggest, on every delivery) and the route setters are deleted."""
    for gone in ("auto_suggest", "projected_route", "set_route", "apply_route", "read_route", "_run_who"):
        assert not hasattr(route, gone), gone
    assert route.ROUTE_WORDS == ("desk", "devices")


def test_a_route_file_on_disk_decides_nothing_until_it_is_migrated(tmp_path):
    """An old route.json is read only as a projection: devices on until it ended, with the line saying it is not migrated."""
    _old(tmp_path)
    seen = presence.should_push(tmp_path, "ping", now=NOW, idle=lambda: 5.0, suggest=False)
    assert seen.push is True and intent.read(tmp_path, NOW)["status"] == "projection"
    assert intent.describe_line(tmp_path, now=NOW, idle=lambda: 5.0).endswith("Not migrated yet: levain ping migrate.")
    from levain.webpush import desk

    desk._write(tmp_path, desk.DESK_MESSAGE_NAME, {"at": NOW + 7190, "source": "test", "desk": ""})   # 1a (Phill 11-03): a typed desk message
    assert presence.should_push(tmp_path, "ping", now=NOW + 7200, idle=lambda: 5.0, suggest=False).push is False   # it has ended


WHO_SSH = "phill    ttys004  Nov  1 09:12 (203.0.113.7)"
WHO_MOSH = "phill    ttys005  Nov  1 09:15 (mac.example.net)"
WHO_LOCAL = "phill    ttys001  Nov  1 08:00"
WHO_CONSOLE = "phill    console  Nov  1 07:59"


@pytest.mark.parametrize("out,want", [(WHO_SSH, True), (WHO_MOSH, True), (WHO_LOCAL, False), (WHO_CONSOLE, False),
                                      (WHO_CONSOLE + "\n" + WHO_LOCAL + "\n" + WHO_SSH, True),
                                      ("phill    ttys002  Nov  1 08:00 (:0)", False), ("", False)])
def test_who_parser(out, want):
    assert route.remote_login_present(out) is want


@pytest.mark.parametrize("content", ["{", "[]", '{"route": "moon", "ts": 1}', '{"route": "desk"}', "x" * 2000])
def test_an_unreadable_old_route_file_is_refused_by_the_reader_and_dropped_by_the_migration(tmp_path, content):
    _raw(tmp_path, content)
    with pytest.raises(ValueError):
        route._load(tmp_path)
    got = intent.migrate(tmp_path, dry_run=True, now=NOW)
    assert any("route.json could not be read" in ln for ln in got["lines"]) and got["record"]["overlay"] is None


def test_a_symlinked_old_route_file_is_refused(tmp_path):
    d = tmp_path / "webpush"
    d.mkdir(mode=0o700)
    (d / route.FILE_NAME).symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError):
        route._load(tmp_path)


@pytest.mark.parametrize("text", [
    '{"route": "devices", "ts": 1, "until": %s, "source": "operator"}' % ("9" * 400),
    '{"route": "devices", "ts": %s, "until": null, "source": "operator"}' % ("9" * 400),
    '{"route": "desk", "ts": 1000000, "until": null, "source": "operator"}',
    '{"route": "desk", "ts": 1000000, "until": 2000000}',
    '{"route": "desk", "ts": 1000000, "until": 2000000, "source": "Operator"}',
    '{"route": "desk", "ts": 1000000, "until": 2000000, "source": "root"}',
    '{"route": "desk", "ts": 1000000, "until": 1e300, "source": "operator"}',
])
def test_a_malformed_old_record_is_refused_never_taken_as_an_operator_override(tmp_path, text):
    _raw(tmp_path, text)
    with pytest.raises(ValueError):
        route._load(tmp_path)


def test_the_migration_maps_each_old_route_kind_by_the_design(tmp_path):
    def run(name, **rec):
        q = tmp_path / name
        _old(q, **rec)
        return intent.migrate(q, dry_run=True, now=NOW)

    live = run("live")                                                      # an unexpired operator devices: one overlay
    assert live["record"]["overlay"]["devices"] == "on" and live["record"]["overlay"]["until"] == {"kind": "timer", "at": NOW + 3600}
    desk = run("desk", route="desk")                                        # operator desk meant 'measured': that is follow
    assert desk["record"]["overlay"] is None and any("follow" in ln for ln in desk["lines"])
    auto = run("auto", source="auto")                                       # no automatic flips of the record
    assert auto["record"]["overlay"] is None and any("automatic suggestion" in ln for ln in auto["lines"])
    ended = run("ended", until=NOW - 1)
    assert ended["record"]["overlay"] is None and any("had already ended" in ln for ln in ended["lines"])
    q = tmp_path / "withquiet"                                              # a quiet beats the route: the route is reported dropped
    _old(q)
    (q / "webpush" / presence.FILE_NAME).write_text(json.dumps({"word": "quiet", "ts": NOW - 50, "until": NOW + 600}))
    got = intent.migrate(q, dry_run=True, now=NOW)
    assert got["record"]["overlay"]["preset"] == "quiet" and any("route devices dropped" in ln for ln in got["lines"])


def test_loopback_hosts_are_not_remote_logins():
    for host in ("127.0.0.1", "::1", "localhost", "fe80::1%lo0", "::ffff:127.0.0.1", "LOCALHOST"):
        assert route.remote_login_present(f"phill ttys004 Nov 1 09:12 ({host})") is False, host
    assert route.remote_login_present("phill ttys004 Nov 1 09:12 (203.0.113.7)") is True


def test_host_classification_parses_addresses_and_never_prefix_matches():
    def line(h):
        return f"phill ttys004 Nov 1 09:12 ({h})"

    for local in ("127.0.0.2", "0:0:0:0:0:0:0:1", "::1", "[::1]", "fe80::1%en0", "localhost", "LOCALHOST", ":0",
                  "::ffff:127.0.0.1"):
        assert route.remote_login_present(line(local)) is False, local
    for remote in ("203.0.113.7", "127.evil.example", "localhost.evil.example", "fe80.example.com", "2001:db8::1"):
        assert route.remote_login_present(line(remote)) is True, remote
