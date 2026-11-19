"""An attached cockpit over the kernel's own panels: what the request layer builds from a decoded row is what it builds from the
provider's own snapshot. Every panel ``build_default_cockpit`` registers is derived once through the real engine, published
through a real ``SnapshotWriter`` and read back through a real ``SnapshotReader``; the head etag over the decoded snapshot is
compared with the head etag over the snapshot the provider made, which is the same provider read (no second read of a
source that moves). The clock is fixed, so nothing time-dependent differs between the two."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from levain.cockpit import snapshot_host as sh
from levain.cockpit.derive import DeriveLoop, DeriveResult, PanelPlan
from levain.cockpit.engine import ReadContext
from levain.cockpit.providers import build_default_cockpit
from levain.cockpit.snapshots import SnapshotReader, SnapshotWriter
from tests.test_cockpit_k2a import _install

NOW = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
CRED = {"class": "none", "device_class": None}


@pytest.fixture()
def attached(tmp_path: Path):
    """The default cockpit over a real install, attached, every non-serve-composed panel derived once on this thread."""
    _root, src, _ids = _install(tmp_path)
    ck = build_default_cockpit(src, clock=lambda: NOW)
    path = tmp_path / "snapshots.db"
    writer, reader = SnapshotWriter(path, now=NOW.timestamp()), SnapshotReader(path)
    ck.attach_snapshots(reader)
    derived = [pid for pid in ck.panel_ids if not ck.serve_composed(pid)]

    def derive_fn(pid: str) -> DeriveResult:
        snap = ck.derive_panel(pid)
        status, body = sh.encode_snap(snap)
        return DeriveResult(status, body, error=snap.error)

    loop = DeriveLoop(writer, {pid: PanelPlan(pid, on_token=True) for pid in derived}, derive_fn, clock=NOW.timestamp)
    assert sorted(loop.run_once(NOW.timestamp())) == sorted(derived)
    yield ck, derived
    reader.close()
    writer.close()


def test_the_head_over_a_decoded_snapshot_says_what_the_head_over_the_providers_does_for_every_built_in_panel(attached) -> None:
    """The head's etag over a decoded snapshot is built from the row's body hash (D3), so it is no longer the provider path's
    text-built one; every other field of the head is the same, and the manifest carries the head the decoded snapshot makes."""
    ck, derived = attached
    assert derived and "edits" not in derived
    auth = ck.authority()
    ctx = ReadContext(auth.now, authority=auth)
    manifest = ck.manifest(CRED)["panels"]
    new_fields = {"etag", "valid_until", "stale", "pending"}
    for pid in derived:
        spec, provider_snap = ck._specs[pid], ck._state[pid].snap       # the snapshot the derive's own read committed
        want = ck._head(spec, provider_snap, "none", ctx.today, auth, ctx.now)
        decoded = ck._snap_for(spec, ctx.child())
        got = ck._head(spec, decoded, "none", ctx.today, auth, ctx.now)
        assert {k: v for k, v in got.items() if k not in new_fields} == \
            {k: v for k, v in want.items() if k not in new_fields}, pid
        assert set(got) - set(want) == {"valid_until", "stale", "pending"}, pid
        assert manifest[pid]["etag"] == got["etag"] and manifest[pid]["etag"] != want["etag"], pid
        assert decoded.fresh_info is not None and decoded.fresh_info.stale is False, pid


def test_the_probe_over_the_built_ins_agrees_with_the_manifest_and_edits_still_reads_its_store(attached) -> None:
    ck, derived = attached
    etags, _version = ck.change_probe().snapshot()
    manifest = ck.manifest(CRED)["panels"]
    assert set(etags) == set(ck.panel_ids) and "edits" in etags
    assert all(etags[pid] == manifest[pid]["etag"] for pid in etags)


def test_every_built_in_panel_serves_from_its_row_with_the_derive_time_as_its_age(attached) -> None:
    ck, derived = attached
    for pid in derived:
        got = ck.panel(pid)
        assert got is not None and got["as_of"] == NOW.isoformat(), pid
