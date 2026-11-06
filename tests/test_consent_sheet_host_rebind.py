"""The consent sheet under a host that rebinds the consent reader AFTER make_server (Phill 11-06, live on his phone: a
band row's Approve link and its push opened ``/sign/consent/<id>?choice=allow`` and got 404 ``not found``).

Flow's bridge serves the kernel's make_server with a Signer over its signed queue while the cockpit's consent reader is
the default queue (another directory), then points the reader at the Signer's queue (its ``SignQueueReader``, a reader
whose ``directory`` is the Signer's) and attaches the grants' revoke itself. The sheet's same-queue condition was read
once, inside make_server, while the queues still differed, so the consent sheet was never built and every consent sheet
route answered 404 for the server's whole life. The condition is now read at each request against the reader the server
holds then: a host that points the reader at the Signer's queue gets the sheet, and a reader over any other queue still
gets no consent route (a button on one queue's card never answers a request from another queue)."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.test_cockpit_consent_sheet import _file, _q, _signer
from tests.test_cockpit_sign_routes import _call, _serving
from tests.test_grant_phone import RP, world  # noqa: F401  (world is a fixture)

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


class _RebindReader:
    """Flow's ``SignQueueReader`` (scripts/bridge.py), reproduced: rows from the serve's own reader, ``directory`` named."""

    def __init__(self, inner: Any, directory: Path) -> None:
        self._inner, self.directory = inner, directory

    def __call__(self, now: Any) -> Any:
        return self._inner(now)


def _flow_wiring(httpd: Any, directory: Path) -> None:
    """What flow's ``show_grants`` does after make_server returned: the reader rebound, the grants' revoke attached."""
    from levain.consent.grant_phone import GrantPhone
    from levain.consent.sign import sign_root

    signer = httpd.signer
    httpd.consent_reader = _RebindReader(httpd.consent_reader, directory.resolve())
    httpd.grant_phone = GrantPhone(directory, signer.rp_id, signer.page_origin, signer.keys,
                                   lambda: sign_root(directory))


@pytest.fixture
def elsewhere(world, tmp_path, monkeypatch):
    """The default consent queue is NOT the Signer's (flow: ~/.levain/consent beside ~/.levain-sign/signq)."""
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "levain-home"))
    return world


def test_a_reader_rebound_to_the_signers_queue_after_make_server_serves_the_sheet(elsewhere, tmp_path) -> None:
    rid = _file(elsewhere)          # filed in the Signer's queue, as consent_trial's hook files it
    with _serving(tmp_path / "inst", signer=_signer(elsewhere), extra_hosts=[RP]) as (url, httpd):
        # before the host's wiring: the reader names another queue, so no consent route exists
        assert _call(url, f"/sign/consent/{_q(rid)}", token=None)[0] == 404
        _flow_wiring(httpd, elsewhere.qdir)
        status, page = _call(url, f"/sign/consent/{_q(rid)}?choice=allow", token=None)
        assert status == 200 and b"/sign.js" in page, (status, page)
        status, got = _call(url, f"/cockpit/consent/{_q(rid)}/sheet.json")
        assert status == 200 and got["request"]["id"] == rid and got["request"]["state"] == "pending", got
        assert [o["key"] for o in got["request"]["options"]] == ["allow", "deny"], got


def test_a_reader_rebound_to_another_queue_still_serves_no_consent_route(elsewhere, tmp_path) -> None:
    rid = _file(elsewhere)
    other = tmp_path / "other-queue"
    other.mkdir()
    with _serving(tmp_path / "inst", signer=_signer(elsewhere), extra_hosts=[RP]) as (url, httpd):
        _flow_wiring(httpd, other)
        assert _call(url, f"/sign/consent/{_q(rid)}", token=None)[0] == 404
        assert _call(url, f"/cockpit/consent/{_q(rid)}/sheet.json")[0] == 404
        assert _call(url, f"/cockpit/consent/{_q(rid)}/challenge",
                     {"decision": "deny", "signer_fp": elsewhere.phone.fp})[0] == 404


def test_a_reader_moved_off_the_signers_queue_takes_the_sheet_with_it(world, tmp_path, monkeypatch) -> None:
    """The condition holds at each request, not once: a sheet built at start goes when the cards leave its queue."""
    monkeypatch.setenv("LEVAIN_HOME", str(world.qdir.parent))      # the default queue IS the Signer's at start
    rid = _file(world)
    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (url, httpd):
        assert _call(url, f"/cockpit/consent/{_q(rid)}/sheet.json")[0] == 200
        other = tmp_path / "other-queue"
        other.mkdir()
        httpd.consent_reader = _RebindReader(httpd.consent_reader, other)
        assert _call(url, f"/cockpit/consent/{_q(rid)}/sheet.json")[0] == 404
        assert _call(url, f"/sign/consent/{_q(rid)}", token=None)[0] == 404
