"""3c-9 D1 (R4 §8's `test_doctor_key_limit_rows`, widened by D1): the invoice-ceiling rows of `levain doctor` run as the
operator, whose account cannot open keys' credentials, built from keys' OWN limit read (keys' refresher against a
fake OpenRouter, its STATUS, CONTROL's shape check), and the same verdict as the root read of the same key status."""
from __future__ import annotations

import asyncio
import os
import shutil
import time


from levain import doctor as D
from levain.broker import keys as K
from levain.slot import control as C
from tests.test_keys_status import (OR, PairRoutes, key_body, needs_non_root, or_cert, or_cred,  # noqa: F401
                                    with_fake_openrouter)


def _keys_report(or_cert, monkeypatch, lib, names, answers, *, stall=None, key_limit_s=1.0):
    """keys' STATUS after one refresher pass (relayed through CONTROL's shape check), how long the pass took, and the
    hosts keys resolved. `stall`: the credential whose connect never completes."""
    from levain.broker import upstream as U

    real_connect = U._connect
    order = sorted(names)

    async def connect(addr, port):
        if stall is not None and len(calls) == order.index(stall):
            calls.append(stall)
            await asyncio.sleep(3600)
        calls.append("x")
        return await real_connect(addr, port)
    calls: list = []
    monkeypatch.setattr(U, "_connect", connect)

    async def body(fake, picks):
        k = K.Keys(lib_root=lib, routes=PairRoutes([(OR, n) for n in names] + [("api.anthropic.com", "an1")]))
        k._in_loop()
        k.key_limit_s = key_limit_s
        t0 = time.monotonic()
        await k.limit_pass()
        took = time.monotonic() - t0
        return C._keys_status_answer(k.status()), took, list(picks)
    return asyncio.run(with_fake_openrouter(or_cert, monkeypatch, answers, body))


def _held_rows(lib, told):
    lib_keys = os.path.join(lib, "keys")
    os.chmod(lib_keys, 0)
    try:
        return {r.name: r for r in D._check_invoice_ceiling(D.Path(lib), None, keys_status=lambda: told)}
    finally:
        os.chmod(lib_keys, 0o700)


@needs_non_root
def test_doctor_key_limit_rows(or_cert, monkeypatch):
    names = ["or-lim", "or-none", "or-slow", "or-stall"]
    lib = _ADM_lib(names)
    answers = {"sk-or-lim-SECRET": (0, 200, key_body(12.5, 3.25)), "sk-or-none-SECRET": (0, 200, key_body(None, None)),
               "sk-or-slow-SECRET": (3, 200, key_body(1, 1)), "sk-or-stall-SECRET": (0, 200, key_body(1, 1))}
    try:
        told, took, picks = _keys_report(or_cert, monkeypatch, lib, names, answers, stall="or-stall")
        rows = _held_rows(lib, told)
    finally:
        shutil.rmtree(lib, ignore_errors=True)
    assert took < 2 * 1.0 + 1.0                      # the slow one and the stalled connect each bounded by key_limit_s
    assert picks == [OR] * len(names)                # the Anthropic route opened no connection
    for n in names:
        row = rows[f"invoice ceiling ({n})"]
        assert row.ok, row                           # every outcome a row, never a refusal
    assert rows["invoice ceiling (or-lim)"].detail.startswith("key limit $12.50 total, $3.25 remaining (as keys read "
                                                              "it less than a minute ago)")
    assert not rows["invoice ceiling (or-lim)"].warn
    assert rows["invoice ceiling (or-none)"].warn and "no credit limit is set" in rows["invoice ceiling (or-none)"].detail
    for n in ("or-slow", "or-stall"):
        r = rows[f"invoice ceiling ({n})"]
        assert r.warn and r.detail == "the key limit was not read: keys' read failed (OpenRouter did not answer within " \
                                      "keys' deadline)", r
    an = rows["invoice ceiling (an1)"]
    assert an.neutral and "workspace spend limit" in an.detail


@needs_non_root
def test_doctor_keys_row_keeps_the_last_good_read_and_names_the_latest_failure(tmp_path):
    entry = {"credential": "or1", "age_s": 600, "limit_microdollars": 20_000_000, "remaining_microdollars": 0,
             "reset": "monthly", "byok": False, "failed": "http-5xx"}
    lib = _ADM_lib([])
    try:
        told = {"ledger_bytes": None, "limits": [entry], "limits_truncated": 0, "routes": [[OR, "or1"]],
                "routes_problem": None, "routes_truncated": 0}
        r = _held_rows(lib, told)["invoice ceiling (or1)"]
        not_yet = _held_rows(lib, {**told, "limits": [{**entry, "age_s": None, "limit_microdollars": None,
                                                        "remaining_microdollars": None, "failed": None}]})
        cut = _held_rows(lib, {**told, "limits": [], "limits_truncated": 1})
        silent = _held_rows(lib, {**told, "limits": []})
    finally:
        shutil.rmtree(lib, ignore_errors=True)
    assert r.warn and r.detail == ("key limit $20.00, $0.00 remaining; the key is exhausted: OpenRouter will refuse its "
                                   "calls; cap $20.00 per month, not a total: spend can pass it across periods; BYOK "
                                   "usage is not counted against this limit (as keys read it 10 min ago; its latest "
                                   "read failed: OpenRouter failed the request (a 5xx status))")
    assert "has not read this key's limit yet" in not_yet["invoice ceiling (or1)"].detail
    assert "$" not in not_yet["invoice ceiling (or1)"].detail       # never a number keys did not read
    assert cut["invoice ceiling (or1)"].detail.endswith("keys' answer had no room for it")
    d = silent["invoice ceiling (or1)"]
    assert d.warn and "keys answered" in d.detail and "or1" in d.detail      # keys answered: never "gave none"
    for row in (d, cut["invoice ceiling (or1)"]):
        assert "gave none" not in row.detail and "not answering" not in row.detail and "no session" not in row.detail


def test_doctor_root_read_and_keys_report_give_one_verdict(tmp_path):
    """The shared interpretation: the root read of a key status and keys' report of the same numbers say the same
    thing, keys' age words aside."""
    cases = [(12.5, 3.25, None, True), (None, None, None, True), (20, 0, "weekly", False), (5, 5, None, None)]
    for i, (lim, rem, reset, byok) in enumerate(cases):
        store = tmp_path / f"s{i}"
        (store / "keys" / "cred").mkdir(parents=True)
        or_cred(str(store), "or1")
        body = key_body(lim, rem, reset, byok)
        root = D._openrouter_limit_row(store, "or1", "n", lambda url, h, t: (200, body), 5.0)
        micro = lambda v: None if v is None else round(v * 1_000_000)   # noqa: E731
        told = {"limits": [{"credential": "or1", "age_s": 30, "limit_microdollars": micro(lim),
                            "remaining_microdollars": micro(rem), "reset": reset, "byok": byok, "failed": None}],
                "limits_truncated": 0}
        keys = D._keys_limit_row("or1", "n", ("held", "hint"), lambda: told)
        assert keys.detail == root.detail + " (as keys read it less than a minute ago)", (root, keys)
        assert (keys.warn, keys.hint) == (root.warn, root.hint)


def test_doctor_root_read_validates_as_keys_does(tmp_path):
    """L3 r1 E3 + C1: the root read takes a key status through keys' own validation, so a value keys calls
    `bad-value` is not read on either path (no dollar figure, a warning), and a negative remaining is exhausted on
    both."""
    from types import SimpleNamespace
    bad = [key_body(-1, 1), key_body(5, 5, None, "false"), key_body(5, 5, None, 0), key_body(1e305, 1),
           key_body(5, 5, "Daily!"), key_body(5, -1e10), key_body(5, True)]
    good = [key_body(5, -0.5), key_body(5, -0.5, "monthly", False)]
    for i, body in enumerate(bad + good):
        store = tmp_path / f"s{i}"
        (store / "keys" / "cred").mkdir(parents=True)
        or_cred(str(store), "or1")
        root = D._openrouter_limit_row(store, "or1", "n", lambda url, h, t: (200, body), 5.0)
        got = K._limit_values(SimpleNamespace(status=200, body=body))
        if body in bad:
            assert got == "bad-value", body
            assert root.ok and root.warn and "$" not in root.detail and "not read" in root.detail, (body, root)
            continue
        lim, rem, reset, byok = got
        told = {"limits": [{"credential": "or1", "age_s": 30, "limit_microdollars": lim,
                            "remaining_microdollars": rem, "reset": reset, "byok": byok, "failed": None}],
                "limits_truncated": 0}
        keys = D._keys_limit_row("or1", "n", ("held", "hint"), lambda: told)
        assert root.warn and "the key is exhausted" in root.detail and "-$0.50 remaining" in root.detail, root
        assert keys.detail == root.detail + " (as keys read it less than a minute ago)", (root, keys)
        assert (keys.warn, keys.hint) == (root.warn, root.hint)


def _ADM_lib(names) -> str:
    import tempfile
    d = tempfile.mkdtemp(prefix="kl", dir="/tmp")
    os.makedirs(os.path.join(d, "routes"))
    os.makedirs(os.path.join(d, "keys", "cred"))
    for n in names:
        or_cred(d, n)
    return d


def test_doctor_words_every_keys_limit_failure():
    assert set(D._LIMIT_FAILURE_WORDS) == set(K.LIMIT_FAILURES)
    assert len(set(D._LIMIT_FAILURE_WORDS.values())) == len(K.LIMIT_FAILURES)   # each token its own words
