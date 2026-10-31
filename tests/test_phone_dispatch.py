"""The phone port's dispatch routes (``/dispatch/<job>/...`` and the ``dispatch_*`` listing keys): the same token,
JSON, size and origin gates as a consent answer, over the ONE cockpit the sign requests use. The token opens the
route; only a passkey assertion the job store verifies signs anything."""
from __future__ import annotations

import shutil
from typing import Any

import pytest

from tests.test_dispatch_phone import RP, file_job, phone_for, refused, repo, world  # noqa: F401, F811  (fixtures)

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")






















# --- fix round 5 -----------------------------------------------------------------------------------------------------





# --- 1029-5: the phone's dispatch challenge names the version its card showed (as the cockpit sheet's does) ------------

def _amended(httpd: Any, job: Any, monkeypatch: Any, digest: str = "d" * 64) -> None:
    """The job read as amended (its contract's digest another) from here on; the version is over the record."""
    phone = httpd.dispatch
    real = phone.record

    def record(j: Any, now: str, signers: "list[str]", note: str) -> "dict[str, Any]":
        rec = real(j, now, signers, note)
        if j.id == job.id:
            rec["digest"] = digest
            rec["sha256"] = phone.version(rec)      # the listed version is over the record, as record() makes it
        return rec

    monkeypatch.setattr(phone, "record", record)


def _no_step(httpd: Any, monkeypatch: Any) -> "list[Any]":
    calls: "list[Any]" = []
    real = httpd.dispatch.step
    monkeypatch.setattr(httpd.dispatch, "step", lambda *a: calls.append(a) or real(*a))
    return calls












def test_the_phone_and_the_cockpit_name_one_job_by_one_version(world) -> None:
    """The phone's listed sha256 is the very version the cockpit sheet binds (one function, not two)."""
    from levain.dispatch.phone import DispatchPhone
    from levain.web_server import DispatchSheet

    rec = {"id": "dj-x", "digest": "a" * 64, "state": "filed", "lease_end": "t", "authorise": {"run": 1, "onto": "main", "tier": "T2"}}
    assert DispatchSheet.version(rec) == DispatchPhone.version(rec)
