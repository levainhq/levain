"""A host's session reader on the session route: the same token gate, a stem shape check, the reader's answer."""
from __future__ import annotations

import dataclasses
import json

from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_session_log import _get

UUID = "3f2a9c1e-7b4d-4e8a-9f10-2c3d4e5f6a7b"
BODY = {"agent": "claude_code", "workspace": "/zq/flow", "first_seen": "2026-10-21T20:00:00Z",
        "last_seen": "2026-10-21T20:05:00Z", "resume": None,
        "events": [{"at": "2026-10-21T20:01:00Z", "kind": "prompt", "text": "zq-sreader-prompt", "cut": 0}],
        "consent": [], "skipped": 0}


def test_a_host_reader_serves_its_own_ids_behind_the_token_and_never_sees_a_bad_stem(tmp_path):
    calls: list[str] = []

    def reader(stem, now):
        calls.append(stem)
        return BODY if stem == UUID else None

    _root, src, _ids = _install(tmp_path)          # install_root set too: the reader wins
    with _serve(dataclasses.replace(src, session_reader=reader), surface_tokens={"browser": "T"}) as (url, _h):
        route = f"{url}/cockpit/session/{UUID}.json"
        assert _get(route)[0] == 403 and _get(route, "wrong")[0] == 403 and calls == []
        status, blob = _get(route, "T")
        assert status == 200 and json.loads(blob) == BODY
        assert _get(f"{url}/cockpit/session/agent-unknown.json", "T")[0] == 404
        seen = len(calls)
        for bad in ("..%2F" + UUID, "a%2Fb", "a..b", ".hidden", "x" * 129):
            assert _get(f"{url}/cockpit/session/{bad}.json", "T")[0] == 404, bad
        assert calls[seen:] == []


def test_a_reader_that_raises_or_returns_no_events_list_is_a_failed_read(tmp_path):
    def reader(stem, now):
        if stem == "boom":
            raise RuntimeError("zq-reader-exploded")
        return {"agent": "x", "events": "not a list"}

    _root, src, _ids = _install(tmp_path)
    with _serve(dataclasses.replace(src, session_reader=reader), surface_tokens={"browser": "T"}) as (url, _h):
        for stem in ("boom", "noevents"):
            status, blob = _get(f"{url}/cockpit/session/{stem}.json", "T")
            assert status == 500 and b"zq-reader-exploded" not in blob and b"not a list" not in blob


class _Shifting(dict):
    """A reader-owned dict that changes after its first serialization (json reads a dict subclass via items())."""

    reads = 0

    def items(self):
        self.reads += 1
        if self.reads == 1:
            return super().items()
        return {**dict(super().items()), "events": [object()]}.items()


def test_a_reader_body_is_serialized_once_so_a_later_change_never_drops_the_connection(tmp_path):
    held: list[_Shifting] = []

    def reader(stem, now):
        held.append(_Shifting(BODY))
        return held[-1]

    _root, src, _ids = _install(tmp_path)
    with _serve(dataclasses.replace(src, session_reader=reader), surface_tokens={"browser": "T"}) as (url, _h):
        status, blob = _get(f"{url}/cockpit/session/{UUID}.json", "T")
    assert status == 200 and json.loads(blob) == BODY and held[-1].reads == 1
