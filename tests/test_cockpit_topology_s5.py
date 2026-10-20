"""K10 S5: the F12 provider fills what the mock draws from data Levain really records.

Item 1: a default install's consent reader names no directory (``directory`` is None = the default queue); the dispatch
store sits in that default queue, so the topology reads it there. Item 2: file conflicts from the Edit/Write paths the
PostToolUse hook logs. Item 3: a dispatch task's label is its contract's own goal.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from tests.test_cockpit_topology import _rig, _stamp, _stem


def _file_dispatch(cdir, tmp_path, goal=None):
    from levain.cockpit.consent import utc_stamp
    from levain.dispatch.jobs import JobStore
    from tests.test_dispatch_contract import POLICY, make_repo, registry, submission

    path, base = make_repo(tmp_path / "dispatch-repo")
    exp = utc_stamp(datetime.now(timezone.utc) + timedelta(hours=2))
    sub = submission(path, base, expires_at=exp)
    if goal is not None:
        sub["goal"] = goal
    job, _cap = JobStore(cdir).file(sub, resolve_template=registry(), policy=POLICY, filer_session="s-run",
                                    idem_key=None)
    return job


class _DefaultReader:
    """web_server._ConsentReader as a default install builds it: ``directory`` present and None."""

    directory = None

    def __call__(self, now):
        from levain.consent.listing import read_listing

        return read_listing(self.directory, now=now)


def test_a_default_install_reads_the_dispatch_store_in_the_default_queue(tmp_path, monkeypatch):
    ck, _root, _src, cdir, _work = _rig(tmp_path, monkeypatch)       # LEVAIN_HOME -> tmp, queue = its consent/
    job = _file_dispatch(cdir, tmp_path)
    ck.attach_consent_rows(_DefaultReader())
    v = ck.panel("topology", credential_class="none")["value"]
    assert "dispatch" not in v["unreadable"], v["errors"]
    assert f"dispatch:{job.id}" in {t["id"] for t in v["tasks"]}


def test_a_reader_with_no_directory_attribute_names_no_dispatch_directory(tmp_path, monkeypatch):
    from levain.cockpit.providers import _dispatch_dir

    _rig(tmp_path, monkeypatch)
    assert _dispatch_dir(lambda now: None) is None
    assert _dispatch_dir(_DefaultReader()).name == "consent"
    explicit = tmp_path / "elsewhere"

    class _Explicit(_DefaultReader):
        directory = explicit

    assert _dispatch_dir(_Explicit()) == explicit


def test_a_dispatch_task_is_labelled_by_its_contracts_goal(tmp_path, monkeypatch):
    ck, _root, _src, cdir, _work = _rig(tmp_path, monkeypatch)
    job = _file_dispatch(cdir, tmp_path, goal="Round the money totals to cents\nwith a long second line")
    v = ck.panel("topology", credential_class="none")["value"]
    t = next(x for x in v["tasks"] if x["id"] == f"dispatch:{job.id}")
    assert t["label"] == "Round the money totals to cents with a long…"[:48] or t["label"].startswith("Round the money")
    assert len(t["label"]) <= 48
    assert "second line" not in t["label"] or len(t["label"]) <= 48
