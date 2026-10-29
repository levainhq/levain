"""An idle install (keys and CONTROL running, no session yet): CONTROL answers ``keys-unavailable`` to KEYS-STATUS
because it never opens the admin connection for an operator's read, so doctor's keys-held rows get no report. Their
words must say that keys holds the credential and the read comes once a session has started, never read as a
permission failure, and never a healthy verdict (an absent number is not health)."""
from __future__ import annotations

import os

import pytest

from levain import doctor
from tests.test_doctor_3c9_rows import _base, _cred, _fetch_returning, _openrouter_route, _rows, _store

needs_non_root = pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 directory")
PERMISSION_WORDS = ("your account", "cannot open", "cannot read", "permission")


def _held_store(tmp_path):
    store = _store(tmp_path)
    _base(store, [_openrouter_route()])
    _cred(store, "orkey", "openrouter.ai")
    (store / "keys" / "ledger.jsonl").write_bytes(b"x")
    return store


def _idle_rows(store):
    """The rows as an operator gets them with no session: the asker's None (CONTROL answered keys-unavailable)."""
    os.chmod(store / "keys", 0)
    try:
        return _rows(store, fetch=_fetch_returning(200, {}, []), keys_status=lambda: None)
    finally:
        os.chmod(store / "keys", 0o700)


def _no_permission_words(detail):
    low = detail.lower()
    return [w for w in PERMISSION_WORDS if w in low]


@needs_non_root
def test_idle_limit_row_says_keys_holds_it_and_the_read_comes_with_a_session(tmp_path):
    r = _idle_rows(_held_store(tmp_path))["invoice ceiling (orkey)"]
    assert _no_permission_words(r.detail) == []
    assert "keys holds orkey" in r.detail and "once a session has started" in r.detail
    assert r.warn and "$" not in r.detail      # not a healthy verdict, and no number keys did not give


@needs_non_root
def test_idle_ledger_row_says_keys_holds_it_and_the_read_comes_with_a_session(tmp_path):
    r = _idle_rows(_held_store(tmp_path))["spend ledger"]
    assert _no_permission_words(r.detail) == []
    assert r.detail.startswith("held by keys") and "once a session has started" in r.detail
    assert r.neutral and not any(ch.isdigit() for ch in r.detail)


@needs_non_root
def test_idle_dispatch_row_says_keys_holds_it_and_the_read_comes_with_a_session(tmp_path):
    store = _held_store(tmp_path)
    (store / "keys" / "routemap.head").write_bytes(b"x")
    r = _idle_rows(store)["invoice ceiling (dispatch routes)"]
    # this row's branch proves a direct read was refused AND that keys gave none: it says both, and no more
    assert "this account's own read of the route maps was refused" in r.detail and "keys gave none" in r.detail
    assert "not answering" not in r.detail and "no session" not in r.detail
    assert r.detail.startswith("held by keys") and "once a session has started" in r.detail
    assert r.neutral
