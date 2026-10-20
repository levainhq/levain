"""The short pairing code: eight characters the operator types once into the installed phone app, for the iPhone whose
Home Screen app never receives the manifest's long code. These pin its shape, its ten-minute single-use life, that only
the newest one redeems, the sliding lockout that makes a typeable code safe to take from a caller with no credential,
and the route, CLI and page that carry it."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import levain
from levain.webpush import devices
from levain.webpush.store import PushStore

T0 = datetime(2026, 10, 19, 22, 0, 0, tzinfo=timezone.utc)
_TOK = "run-token-for-the-shortcode-test"
_JS = Path(levain.__file__).parent / "templates" / "web" / "phone_app.js"


@pytest.fixture
def conn(tmp_path: Path):
    c = PushStore(tmp_path).connection()
    try:
        yield c
    finally:
        c.close()


def _wrong(code: str) -> str:
    """A well-shaped code that is not ``code``."""
    first = devices.SHORT_ALPHABET[(devices.SHORT_ALPHABET.index(code[0]) + 1) % len(devices.SHORT_ALPHABET)]
    return first + code[1:]


def test_normalize_folds_case_hyphens_spaces_and_look_alikes():
    assert devices.normalize_short("abcd-efgh") == "ABCDEFGH"
    assert devices.normalize_short("  AB CD\tEF-GH \n") == "ABCDEFGH"
    assert devices.normalize_short("abcd–efgh") == "ABCDEFGH"            # a keyboard's en dash for the hyphen
    assert devices.normalize_short("OIL0-1234") == "01101234"                  # O reads 0, I and L read 1
    assert devices.normalize_short("oil0-1234") == "01101234"
    for bad in ("", "   ", "-", "ABCD-EFG", "ABCD-EFGHJ", "ABCD-EFGU", "ABCD-EF!H", "ABCD-EFGÉ", "ABCD-ＥFGH",
                "x" * 65, None, 12345678, b"ABCDEFGH"):
        assert devices.normalize_short(bad) is None, bad


def test_a_minted_code_is_eight_crockford_characters_shown_with_one_hyphen(conn):
    for _ in range(30):
        shown = devices.mint_short_pairing(conn, "phone", T0)
        assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}", shown), shown
        assert devices.normalize_short(shown) == shown.replace("-", "")
    # only the hash is stored
    rows = conn.execute("SELECT code_hash FROM short_pairings").fetchall()
    assert rows and all(len(r[0]) == 64 and shown.replace("-", "") not in r[0] for r in rows)


def test_a_short_code_lives_ten_minutes_to_the_second(conn):
    code = devices.mint_short_pairing(conn, "phone", T0)
    assert devices.redeem_short(conn, code, T0 + timedelta(minutes=10)) is None                   # dead at 10:00
    code = devices.mint_short_pairing(conn, "phone", T0)
    got = devices.redeem_short(conn, code, T0 + timedelta(minutes=9, seconds=59))              # alive at 9:59
    assert got is not None and got[0].startswith("d-")
    assert devices.verify(conn, got[1], T0) == got[0]


def test_a_new_code_kills_every_open_one_and_each_code_works_once(conn):
    older = devices.mint_short_pairing(conn, "a", T0)
    newer = devices.mint_short_pairing(conn, "b", T0 + timedelta(seconds=5))
    assert devices.redeem_short(conn, older, T0 + timedelta(seconds=6)) is None
    dev, token = devices.redeem_short(conn, newer.lower(), T0 + timedelta(seconds=7))    # typed in lower case
    assert devices.redeem_short(conn, newer, T0 + timedelta(seconds=8)) is None          # once
    assert [d["label"] for d in devices.list_devices(conn) if d["id"] == dev] == ["b"]
    # a long code minted beside it is untouched by the short one's life
    long_code = devices.mint_pairing(conn, "long", T0)
    devices.mint_short_pairing(conn, "c", T0)
    assert devices.redeem(conn, long_code, T0) is not None


def test_unknown_spent_and_expired_codes_are_one_answer_and_each_is_a_failure(conn):
    spent = devices.mint_short_pairing(conn, "s", T0)
    assert devices.redeem_short(conn, spent, T0) is not None
    expired = devices.mint_short_pairing(conn, "e", T0)
    late = T0 + devices.SHORT_PAIRING_TTL
    answers = [devices.redeem_short(conn, c, late) for c in (_wrong(spent), spent, expired)]
    assert answers == [None, None, None]
    assert conn.execute("SELECT COUNT(*) FROM redeem_failures").fetchone()[0] == 3
    # input that cannot be a code is refused without a lookup, and is no failure
    for bad in ("", "ABC", "x" * 200, None):
        assert devices.redeem_short(conn, bad, late) is None
    assert conn.execute("SELECT COUNT(*) FROM redeem_failures").fetchone()[0] == 3


def test_five_wrong_codes_lock_out_a_right_one_until_the_window_slides(conn):
    # the failures fall one minute before the code's own mint, so the window slides open while the code still lives
    # (a mint clears the failures, so none is made inside this test after the five)
    code = devices.mint_short_pairing(conn, "phone", T0)
    wrong = _wrong(code)
    early = T0 - timedelta(minutes=1)
    for i in range(devices.SHORT_MAX_FAILURES):
        assert devices.redeem_short(conn, wrong, early + timedelta(seconds=i)) is None
    # locked: the right code is refused, the refusal is not another failure, and the wait runs to the first failure's
    # age-out (early + 10 min = T0 + 9 min)
    at = T0 + timedelta(seconds=30)
    assert devices.redeem_short(conn, code, at) == (devices.LOCKED, 510)
    assert devices.redeem_short(conn, wrong, at) == (devices.LOCKED, 510)
    assert conn.execute("SELECT COUNT(*) FROM redeem_failures").fetchone()[0] == devices.SHORT_MAX_FAILURES
    assert devices.redeem_short(conn, code, T0 + timedelta(minutes=8, seconds=59)) == (devices.LOCKED, 1)
    # the first failure ages out at T0 + 9:00, which opens the lock; the code itself lives until T0 + 10:00
    nine = T0 + timedelta(minutes=9)
    got = devices.redeem_short(conn, code, nine)
    assert got is not None and got[0].startswith("d-")
    # a success leaves the failures: four are still in the window, so one more wrong code shuts the lock again
    assert devices.redeem_short(conn, wrong, nine) is None
    assert devices.redeem_short(conn, wrong, nine)[0] == devices.LOCKED


def test_a_mint_clears_the_lock_and_the_new_code_redeems_at_once(conn):
    code = devices.mint_short_pairing(conn, "phone", T0)
    wrong = _wrong(code)
    for i in range(devices.SHORT_MAX_FAILURES):
        assert devices.redeem_short(conn, wrong, T0 + timedelta(seconds=i)) is None
    at = T0 + timedelta(seconds=30)
    assert devices.redeem_short(conn, code, at)[0] == devices.LOCKED
    # the operator, holding the run token or the laptop, mints a fresh code: it redeems at once, lock or no lock
    fresh = devices.mint_short_pairing(conn, "phone", at)
    assert conn.execute("SELECT COUNT(*) FROM redeem_failures").fetchone()[0] == 0
    got = devices.redeem_short(conn, fresh, at)
    assert got is not None and got[0].startswith("d-")
    # and nothing a caller without a credential does clears them: a redeem's success leaves them, a refused one adds
    again = devices.mint_short_pairing(conn, "phone", at)
    for i in range(devices.SHORT_MAX_FAILURES - 1):
        assert devices.redeem_short(conn, _wrong(again), at) is None
    assert devices.redeem_short(conn, again, at) is not None
    assert conn.execute("SELECT COUNT(*) FROM redeem_failures").fetchone()[0] == devices.SHORT_MAX_FAILURES - 1
    long_code = devices.mint_pairing(conn, "long", at)                        # the long code's mint is not a short mint
    assert devices.redeem(conn, long_code, at) is not None
    assert conn.execute("SELECT COUNT(*) FROM redeem_failures").fetchone()[0] == devices.SHORT_MAX_FAILURES - 1


def test_a_pasted_code_with_invisible_characters_still_reads(conn):
    # what a copy from Messages or Notes can carry: zero-width space, left-to-right mark, BOM, isolates, soft hyphen,
    # and a stray control character
    for ch in ("​", "‎", "﻿", "⁦", "⁩", "­", "‍", "\x07"):
        for typed in (ch + "ABCD-EFGH", "ABCD-EFGH" + ch, "ABCD" + ch + "-EFGH", "AB" + ch + "CD" + ch + "EFGH",
                      "⁦ABCD-EFGH⁩"):
            assert devices.normalize_short(typed) == "ABCDEFGH", repr(typed)
    # the cap is on the input as it arrived: 64 invisible characters around a code is still too long
    assert devices.normalize_short("​" * 57 + "ABCDEFGH") is None
    assert devices.normalize_short("​" * 56 + "ABCDEFGH") == "ABCDEFGH"
    # and a code that is wrong is still not a code once they are gone
    assert devices.normalize_short("​ABCD-EFG") is None
    # end to end: the pasted copy of a live code redeems
    code = devices.mint_short_pairing(conn, "phone", T0)
    assert devices.redeem_short(conn, "⁦" + code + "​⁩", T0) is not None


def test_two_racing_mints_leave_one_redeemable_code(tmp_path: Path):
    """Probabilistic: without the one transaction both inserts can land after both deletes. A few rounds with many
    threads make that likely; the assertion is exact (one live code) every round."""
    PushStore(tmp_path).connection().close()
    for _ in range(6):
        start = threading.Barrier(6)
        codes: list = []

        def go() -> None:
            c = PushStore(tmp_path).connection()
            try:
                start.wait()
                codes.append(devices.mint_short_pairing(c, "phone", T0))
            finally:
                c.close()

        threads = [threading.Thread(target=go) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert len(codes) == 6
        c = PushStore(tmp_path).connection()
        try:
            held = {r[0] for r in c.execute("SELECT code_hash FROM short_pairings WHERE used_at IS NULL")}
            assert len(held) == 1, len(held)
            live = [code for code in codes if devices._hash(devices.normalize_short(code)) in held]
            assert len(live) == 1, (codes, live)
            # the one live code redeems; every other one is dead
            assert devices.redeem_short(c, live[0], T0) is not None
            assert c.execute("SELECT COUNT(*) FROM short_pairings WHERE used_at IS NULL").fetchone()[0] == 0
        finally:
            c.close()


def test_two_racing_redeems_of_one_code_make_one_device(tmp_path: Path):
    for _ in range(5):
        code = _mint(tmp_path)
        start = threading.Barrier(2)
        got: list = []

        def go() -> None:
            c = PushStore(tmp_path).connection()
            try:
                start.wait()
                got.append(devices.redeem_short(c, code, T0))
            finally:
                c.close()

        threads = [threading.Thread(target=go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert sorted(g is None for g in got) == [False, True], got
    c = PushStore(tmp_path).connection()
    try:
        assert len(devices.list_devices(c)) == 5
    finally:
        c.close()


def _mint(qdir: Path) -> str:
    c = PushStore(qdir).connection()
    try:
        c.execute("DELETE FROM redeem_failures")    # each race's loser is a failure; keep the lock out of the loop
        c.commit()
        return devices.mint_short_pairing(c, "phone", T0)
    finally:
        c.close()


def test_an_existing_store_gains_the_short_code_tables(tmp_path: Path):
    c = PushStore(tmp_path).connection()
    try:
        dev, token = devices.redeem(c, devices.mint_pairing(c, "old", T0), T0)
        with c:
            c.execute("DROP TABLE short_pairings")
            c.execute("DROP TABLE redeem_failures")
    finally:
        c.close()
    raw = sqlite3.connect(tmp_path / "webpush" / "webpush.db")
    try:
        names = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        raw.close()
    assert "short_pairings" not in names and "devices" in names          # the store as it was before this change
    c = PushStore(tmp_path).connection()
    try:
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"short_pairings", "redeem_failures"} <= names
        assert devices.verify(c, token, T0) == dev                             # nothing it held is lost
        assert c.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()[0] == "levain-push-store/1"
        assert devices.redeem_short(c, devices.mint_short_pairing(c, "new", T0), T0) is not None
    finally:
        c.close()


def test_levain_ping_pair_prints_a_code_that_redeems(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    from levain.cli import main

    qdir = tmp_path / "q"
    qdir.mkdir(mode=0o700)
    assert main(["ping", "pair", "--label", "my phone", "--queue-dir", str(qdir)]) in (0, None)
    out = capsys.readouterr().out
    assert "10 minutes" in out and "Pairing code box" in out
    code = re.search(r"pairing code: ([0-9A-Z]{4}-[0-9A-Z]{4})", out).group(1)
    c = PushStore(qdir).connection()
    try:
        dev, _token = devices.redeem_short(c, code, datetime.now(timezone.utc))
        assert [d["label"] for d in devices.list_devices(c) if d["id"] == dev] == ["my phone"]
    finally:
        c.close()


@contextmanager
def _serving(qdir: Path):
    from levain.phone_server import make_phone_server

    httpd = make_phone_server(qdir, "localhost", port=0, token=_TOK)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _post(url: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    from levain.phone_server import PHONE_TOKEN_HEADER

    headers = {"Content-Type": "application/json", **({PHONE_TOKEN_HEADER: token} if token else {})}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_the_route_answers_200_403_and_429_through_the_phone_server(tmp_path: Path):
    qdir = tmp_path / "q"
    qdir.mkdir(mode=0o700)
    with _serving(qdir) as url:
        status, minted = _post(url + "/webpush/pair", {"label": "iPhone"}, _TOK)
        assert status == 200 and set(minted) == {"code", "short", "short_ttl_seconds"}
        assert minted["short_ttl_seconds"] == 600 and re.fullmatch(r"[0-9A-Z]{4}-[0-9A-Z]{4}", minted["short"])
        assert _post(url + "/webpush/pair/redeem", {"code": minted["code"]})[0] == 200    # the long code still works
        status, dev = _post(url + "/webpush/pair/redeem", {"short": minted["short"].lower().replace("-", " ")})
        assert status == 200 and dev["device_id"].startswith("d-") and dev["token"]
        assert _post(url + "/webpush/pair", {"label": "x"}, dev["token"])[0] == 403       # a device mints nothing

        short = _post(url + "/webpush/pair", {"label": "iPhone"}, _TOK)[1]["short"]
        wrong = _wrong(short.replace("-", ""))
        for _ in range(devices.SHORT_MAX_FAILURES):
            status, body = _post(url + "/webpush/pair/redeem", {"short": wrong})
            assert (status, body) == (403, {"error": "bad_code", "message": "the pairing code is unknown, spent or expired"})
        status, body = _post(url + "/webpush/pair/redeem", {"short": short})
        assert status == 429 and body["error"] == "locked" and 590 <= body["retry_after"] <= 600
        assert body["message"] == "too many wrong codes; try again in 10 min"
        assert set(body) == {"error", "message", "retry_after"}


def test_a_locked_route_opens_only_for_a_run_token_mint(tmp_path: Path):
    qdir = tmp_path / "q"
    qdir.mkdir(mode=0o700)
    with _serving(qdir) as url:
        dev = _post(url + "/webpush/pair/redeem", {"short": _post(url + "/webpush/pair", {}, _TOK)[1]["short"]})[1]
        minted = _post(url + "/webpush/pair", {}, _TOK)[1]
        for _ in range(devices.SHORT_MAX_FAILURES):
            assert _post(url + "/webpush/pair/redeem", {"short": _wrong(minted["short"].replace("-", ""))})[0] == 403
        assert _post(url + "/webpush/pair/redeem", {"short": minted["short"]})[0] == 429
        # nothing a caller without the run token can send opens it: the long code's redeem, a device asking to mint,
        # no credential asking to mint
        assert _post(url + "/webpush/pair/redeem", {"code": minted["code"]})[0] == 200
        assert _post(url + "/webpush/pair", {}, dev["token"])[0] == 403
        assert _post(url + "/webpush/pair", {})[0] == 403
        assert _post(url + "/webpush/pair/redeem", {"short": minted["short"]})[0] == 429
        # the operator's fresh code redeems at once
        fresh = _post(url + "/webpush/pair", {}, _TOK)[1]["short"]
        status, body = _post(url + "/webpush/pair/redeem", {"short": fresh})
        assert status == 200 and body["token"]


def test_the_page_offers_the_typed_code_in_the_installed_app():
    js = _JS.read_text(encoding="utf-8")
    for words in ("This app is not paired yet: type the pairing code from the laptop or from Safari.",
                  "That code did not match, or it expired (only the newest code works). Get a new one: run levain ping "
                  "pair on the laptop, or in Safari tap Install on this phone.",
                  "Too many wrong codes. Try again in ", " min, then get a new code.",
                  "Type this code in the box the app shows.", "New code", "Pairing code"):
        assert words in js, words
    for attr in ('id: "pair-code"', 'autocapitalize: "characters"', 'autocorrect: "off"', 'autocomplete: "off"',
                 'spellcheck: "false"', 'inputmode: "text"', "JSON.stringify({ short: text })"):
        assert attr in js, attr
    # the long-code and run-token boot paths are still there, and the redeem of a typed code sends no token header
    assert "JSON.stringify({ code: pair })" in js and "phone_token=" in js
    redeem = js[js.index("function redeemShort"):js.index("function submitPair")]
    assert "X-Levain-Phone-Token" not in redeem and "location.reload()" in js
    assert "innerHTML" not in js


def test_the_page_copy_never_sends_the_operator_back_to_a_browser_link():
    """String guards only: the behaviour behind each (the revoked box, one mint per tap, the dead run token's switch,
    a 403 that is not the credential one) is proven by driving the page in a browser, not here."""
    js = _JS.read_text(encoding="utf-8")
    for gone in ("Open the link the laptop printed to install.", "open the link the laptop printed instead",
                 "then open the install link from the laptop again", "pair it again from the laptop",
                 'r.body.error === "forbidden"'):
        assert gone not in js, gone
    for kept in ("This phone was unpaired on the laptop: type a new pairing code in the box above to pair it again.",
                 "This browser will not keep the pairing, and the code is spent. Get a new code and try again in a ",
                 "and type it here.", "this page's (token|credential) is needed", "if (installBusy) return;"):
        assert kept in js, kept
    # the server's two credential-needed sentences are the ones the page keys on
    from levain.phone_server import PHONE_TOKEN_HEADER

    assert re.match(r"this page's (token|credential) is needed", f"this page's token is needed, sent as {PHONE_TOKEN_HEADER}")
    assert re.match(r"this page's (token|credential) is needed", "this page's credential is needed")


def test_the_box_takes_a_pasted_code_with_invisible_characters_in_front_of_it():
    """The server strips format characters up to its own raw-input cap, so the box must not cut the paste first."""
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "levain" / "templates" / "web" / "phone_app.js").read_text()
    box = js[js.index('id: "pair-code"'):][:200]
    assert 'maxlength: "64"' in box and 'maxlength: "12"' not in box


def test_the_page_drops_a_subscription_made_under_another_installs_key():
    # the page's resync compares the browser subscription's key to the laptop's before handing it over; the run that
    # proves it on a real browser is in levain-memory 1019-app (a stale-key subscription reads "off")
    js = _JS.read_text(encoding="utf-8")
    for words in ("function keyMismatch(s)", "s.options.applicationServerKey", 'api("GET", "/webpush/key")',
                  "a.length !== b.length || a.some((v, i) => v !== b[i])",
                  "were made for a different Levain install, so they were switched off",
                  "could not be switched off here: tap Turn off, then turn on", 'pingNote = subState === "off"'):
        assert words in js, words
    assert "keyMismatch(s).then((stale) =>" in js
