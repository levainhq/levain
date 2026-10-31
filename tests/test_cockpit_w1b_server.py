"""W1b slice A (levain project_memory/1030-2-w1b/DESIGN.md r4, sections 2, 6, 7 A): the owner key opens nothing off the
box, the typed short code, the box-direct device list and revoke, and the test ping. Each test names the design row or
invariant it pins."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

from levain.cockpit.firejournal import PAIRED_PREFIX, CockpitDB
from levain.web_server import make_server
from levain.webpush.store import PushStore
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import PROXY, T, _call, _pair, _serve
from tests.test_cockpit_webpush import NS, _free_port, _sub, _signed
from levain.consent.sign import Signer
import threading

FC = "flowconnect-surface-token-0123456789abcdef"


@pytest.fixture
def src(tmp_path: Path) -> Any:
    return _install(tmp_path)[1]


@pytest.fixture
def rig(tmp_path: Path) -> tuple[Any, Path]:
    qdir = tmp_path / "signq"
    qdir.mkdir(mode=0o700)
    return _install(tmp_path)[1], qdir


@contextmanager
def _signed_fc(src: Any, qdir: Path) -> Iterator[tuple[int, Any]]:
    """The cockpit with a Signer, the owner key and a named program surface (``flowconnect``)."""
    port = _free_port()
    signer = Signer(qdir, (NS,), "localhost", lambda: {})
    signer.page_origin = f"http://localhost:{port}"
    httpd = make_server(src, host="127.0.0.1", port=port, surface_tokens={"browser": T, "flowconnect": FC},
                        install_class="reported", signer=signer)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield port, httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def _short(port: int, token: str = T, headers: "dict | None" = None, **body: Any) -> str:
    st, out, _ = _call(port, "POST", "/cockpit/pair/offer", {"short": True, **body}, token=token, headers=headers)
    assert st == 200 and set(out) == {"short", "short_ttl_seconds"}, out
    return out["short"]


def test_the_owner_key_is_refused_on_a_proxied_request_and_still_works_on_loopback(src) -> None:
    """Run 1 (L-1): the owner token on a proxied request to a writable server was 200; it is now the credential-less 403."""
    with _serve(src) as (port, _h):
        assert _call(port, "GET", "/cockpit/manifest.json", token=T)[0] == 200
        for method, path, body in (("GET", "/cockpit/manifest.json", None), ("GET", "/cockpit/pair/devices.json", None),
                                   ("POST", "/cockpit/pair/offer", {"label": "x"})):
            st, out, _ = _call(port, method, path, body, token=T, headers=PROXY)
            assert st == 403, (path, out)
        assert _call(port, "POST", "/cockpit/pair/offer", {"label": "x"}, token=T)[0] == 200


def test_every_token_403_names_what_would_help_from_the_same_predicate_as_the_manifest(src) -> None:
    """Invariant 1: the 403 body's ``auth`` and the manifest's ``owner_key`` agree, on loopback and proxied."""
    with _serve(src) as (port, _h):
        _d, tok = _pair(port)
        man = _call(port, "GET", "/cockpit/manifest.json", token=T)[1]
        assert man["transport"]["owner_key"] is True
        assert _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY)[1]["transport"]["owner_key"] is False
        for headers, want in ((None, "owner"), (PROXY, "pair")):
            # the manifest read is open on loopback with no credential (class "none", 200); only a proxied read is refused
            for method, path, body in (("GET", "/cockpit/manifest.json", None), ("GET", "/cockpit/pair/devices.json", None),
                                       ("POST", "/cockpit/pair/offer", {}), ("POST", "/cockpit/pair/revoke", {"id": "d-0"}),
                                       ("POST", "/cockpit/verb", {"verb": "x"})):
                if headers is None and path == "/cockpit/manifest.json":
                    continue
                st, out, _ = _call(port, method, path, body, headers=headers)
                assert (st, out.get("auth")) == (403, want), (path, headers, out)
                assert "token" in out["message"]


def test_a_short_code_with_no_label_pairs_a_device_called_paired_device(src) -> None:
    """Run 3 (a literal r3 build stored NULL and failed this insert)."""
    with _serve(src) as (port, _h):
        code = _short(port)
        assert len(code) == 9 and code[4] == "-"
        st, out, hdrs = _call(port, "POST", "/cockpit/pair/claim", {"short": code.lower().replace("-", " ")})
        assert st == 200 and hdrs["cache-control"] == "no-store" and set(out) == {"device_id", "token"}, out
        devs = _call(port, "GET", "/cockpit/pair/devices.json", token=out["token"], headers=PROXY)[1]["devices"]
        assert [(d["label"], d["paired_by"], d["subscribed"]) for d in devs] == [("paired device", "browser", False)]


def test_five_wrong_tries_kill_the_code_and_a_new_mint_works(src) -> None:
    with _serve(src) as (port, _h):
        code = _short(port)
        wrong = "0000-0000" if code != "0000-0000" else "1111-1111"
        for _ in range(5):
            st, out, _ = _call(port, "POST", "/cockpit/pair/claim", {"short": wrong})
            assert st == 403 and "mint a new one" in out["message"], out
        assert _call(port, "POST", "/cockpit/pair/claim", {"short": code})[0] == 403      # dead, though it is right
        again = _short(port)
        assert _call(port, "POST", "/cockpit/pair/claim", {"short": again})[0] == 200


def test_a_claim_with_no_live_short_code_takes_no_write_lock(tmp_path: Path) -> None:
    db = CockpitDB(tmp_path)
    now = datetime.now(timezone.utc)

    def no_write(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the journal's write lock was taken")

    db.write = no_write                                   # type: ignore[method-assign]
    assert db.redeem_short("0000-0000", now=now) is None
    assert db.redeem_short("not a code", now=now) is None


def test_a_used_and_an_expired_short_code_are_refused(src, tmp_path: Path) -> None:
    with _serve(src) as (port, httpd):
        used = _short(port)
        assert _call(port, "POST", "/cockpit/pair/claim", {"short": used})[0] == 200
        assert _call(port, "POST", "/cockpit/pair/claim", {"short": used})[0] == 403
        expired = _short(port)
        conn = sqlite3.connect(httpd.broker.data_root / "cockpit.db")
        conn.execute("UPDATE pair_short_offer SET expires = '2000-01-01T00:00:00Z'")
        conn.commit()
        conn.close()
        assert _call(port, "POST", "/cockpit/pair/claim", {"short": expired})[0] == 403


def test_the_two_codes_cannot_stand_in_for_each_other_and_the_body_names_exactly_one(src) -> None:
    """Invariant 4: a short code as {code} and a link code as {short} are refused, both or neither key is 400, and
    the short code still redeems afterwards."""
    with _serve(src) as (port, _h):
        short = _short(port)
        link = _call(port, "POST", "/cockpit/pair/offer", {}, token=T)[1]["code"]
        assert _call(port, "POST", "/cockpit/pair/claim", {"code": short})[0] == 403
        assert _call(port, "POST", "/cockpit/pair/claim", {"code": short.replace("-", "")})[0] == 403
        assert _call(port, "POST", "/cockpit/pair/claim", {"short": link})[0] == 403
        for body in ({}, {"code": link, "short": short}, {"short": short, "label": "x"}):
            assert _call(port, "POST", "/cockpit/pair/claim", body)[0] == 400, body
        assert _call(port, "POST", "/cockpit/pair/offer", {"short": "yes"}, token=T)[0] == 400
        assert _call(port, "POST", "/cockpit/pair/claim", {"short": short})[0] == 200
        assert _call(port, "POST", "/cockpit/pair/claim", {"code": link})[0] == 200


def test_a_link_mint_leaves_a_live_short_code_live(src) -> None:
    with _serve(src) as (port, _h):
        short = _short(port)
        st, out, _ = _call(port, "POST", "/cockpit/pair/offer", {"label": "laptop"}, token=T)
        assert st == 200 and "short" not in out and out["pair_path"].startswith("/pair#code=")
        assert _call(port, "POST", "/cockpit/pair/claim", {"short": short})[0] == 200


def test_revoking_a_device_expires_the_short_code_its_family_minted(src) -> None:
    with _serve(src) as (port, _h):
        dev, tok = _pair(port)
        code = _short(port, token=tok, headers=PROXY)
        assert _call(port, "POST", "/cockpit/pair/revoke", {"id": dev}, token=T)[0] == 200
        assert _call(port, "POST", "/cockpit/pair/claim", {"short": code})[0] == 403


def test_box_list_prints_no_hash_and_box_revoke_takes_that_family_only(src, capsys) -> None:
    from levain.cli import main

    with _serve(src) as (port, httpd):
        a, tok_a = _pair(port, label="ipad")
        b, _tok_b = _pair(port, label="phone")
        c = _call(port, "POST", "/cockpit/pair/claim", {"short": _short(port, token=tok_a, headers=PROXY)})[1]["device_id"]
        root = str(httpd.broker.data_root)
        assert main(["cockpit", "pair", "--data-root", root, "--list"]) == 0
        listing = capsys.readouterr().out
        assert all(d in listing for d in (a, b, c)) and "ipad" in listing and "token" not in listing
        assert tok_a not in listing and "hash" not in listing
        assert main(["cockpit", "pair", "--data-root", root, "--revoke", a]) == 0
        assert sorted(capsys.readouterr().out.split(": ", 1)[1].strip().split(", ")) == sorted([a, c])   # a and its child
        live = {d["id"] for d in _call(port, "GET", "/cockpit/pair/devices.json", token=_pair(port)[1])[1]["devices"]
                if d["revoked_at"] is None}
        assert b in live and a not in live and c not in live
        assert main(["cockpit", "pair", "--data-root", root, "--revoke", "d-0123456789ab"]) == 1
        assert main(["cockpit", "pair", "--data-root", root, "--list", "--short"]) == 2


def test_the_powers_table_holds_row_by_row_for_each_credential_class(rig, monkeypatch) -> None:
    """DESIGN section 2's table, as built: the owner key (loopback request), a named surface and a paired device. The
    subscribe row for a write credential is the ruled one-tap pair-on-the-spot, not ``device_required``."""
    from levain.webpush import sender

    monkeypatch.setattr(sender, "_post", lambda req: 201)
    src, qdir = rig
    store = PushStore(qdir)
    with _signed_fc(src, qdir) as (port, _h):
        ping = store.add_ping("sign", "np-x", "t", "b")
        victims = [_pair(port, label=f"v{i}")[0] for i in range(3)]
        _dev, paired_tok = _pair(port, label="me")
        classes = (("owner", T, None), ("surface", FC, PROXY), ("paired", paired_tok, PROXY))
        for i, (name, tok, hdrs) in enumerate(classes):
            def call(method, path, body=None):
                return _call(port, method, path, body, token=tok, headers=hdrs)
            assert call("GET", "/cockpit/manifest.json")[0] == 200, name                    # read
            assert call("POST", "/cockpit/pair/offer", {})[0] == 200, name                  # mint a link
            assert call("POST", "/cockpit/pair/offer", {"short": True})[0] == 200, name     # mint a short code
            st, out, _ = call("POST", "/cockpit/pair/revoke", {"id": victims[i]})           # revoke any device
            assert st == 200 and out["revoked"] == [victims[i]], (name, out)
            st, out, _ = call("POST", "/webpush/subscribe", _sub(10 + i))
            assert st == 200, (name, out)
            assert ("token" in out) == (name != "paired"), (name, out)                      # a write credential pairs on the spot
            st, out, _ = call("POST", "/webpush/test", {})
            assert (st, out.get("error")) == ((200, None) if name == "paired" else (403, "device_required")), (name, out)
            assert call("POST", "/webpush/unsubscribe", {"endpoint": _sub(10 + i)["endpoint"]})[0] == 200, name
            assert call("POST", "/webpush/opened", {"ping": ping})[0] == 200, name         # any existing ping id
        assert _call(port, "POST", "/cockpit/pair/revoke", {"id": "d-0123456789ab"}, token=FC)[0] == 404
        # a surface token is never revoked from the Devices panel: it is no paired device
        assert _call(port, "POST", "/cockpit/pair/revoke", {"id": "browser"}, token=T)[0] == 400


def test_a_test_ping_creates_deliveries_only_for_the_caller(rig, monkeypatch) -> None:
    """Invariant 3, under the paired token that one tap returns."""
    from levain.webpush import sender

    sent: list[str] = []
    monkeypatch.setattr(sender, "_post", lambda req: sent.append(req.full_url) or 201)
    src, qdir = rig
    with _signed(src, qdir) as (port, _h):
        st, one, _ = _call(port, "POST", "/webpush/subscribe", _sub(1), token=T)         # the one tap: pairs and subscribes
        assert st == 200 and one["token"]
        _d2, tok2 = _pair(port, label="second")
        assert _call(port, "POST", "/webpush/subscribe", _sub(2), token=tok2)[0] == 200
        st, out, _ = _call(port, "POST", "/webpush/test", {}, token=one["token"])
        assert st == 200 and [d["status"] for d in out["deliveries"]] == [201], out
        assert sent == [_sub(1)["endpoint"]]
        st, listing, _ = _call(port, "GET", "/cockpit/pair/devices.json", token=T)
        assert {d["id"]: d["subscribed"] for d in listing["devices"]} == {one["device_id"]: True, _d2: True}
        (ping,) = [p for p in PushStore(qdir).record(limit=10) if p["kind"] == "test"]
        assert ping["ref"] == PAIRED_PREFIX + one["device_id"]
        assert [d["subscription_id"] for d in ping["deliveries"]] == [one["id"]]


# --- L3 r1 fixes (project_memory/1030-47_L3_w1ba_r1.txt) ---------------------------------------------------------------

def test_box_short_mint_on_a_fresh_data_root_starts_the_journal(tmp_path: Path, capsys) -> None:
    """A (codex HIGH): an off-loopback cockpit with no journal yet has no credential but the box's mint."""
    from levain.cli import main

    root = tmp_path / "never-served" / "cockpit"
    assert main(["cockpit", "pair", "--data-root", str(root), "--list"]) == 1      # nothing to list: no journal made
    assert not (root / "cockpit.db").exists()
    assert main(["cockpit", "pair", "--data-root", str(root), "--short", "--print"]) == 0
    code = capsys.readouterr().out.strip()
    assert len(code) == 9 and code[4] == "-", code
    assert (root.stat().st_mode & 0o777) == 0o700
    got = CockpitDB(root).redeem_short(code, now=datetime.now(timezone.utc))
    assert got is not None and got[0].startswith("d-")
    # the first serve sets the class, as on any fresh journal
    assert CockpitDB(root, install_class="reported").set_install_class("reported") == "reported"


def test_box_mint_is_refused_while_a_server_holds_the_lock_on_a_fresh_root(tmp_path: Path, capsys) -> None:
    import fcntl
    import os

    from levain.cli import main

    root = tmp_path / "cockpit"
    root.mkdir(mode=0o700)
    fd = os.open(root / "cockpit.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert main(["cockpit", "pair", "--data-root", str(root), "--short", "--print"]) == 1
        assert "try again" in capsys.readouterr().err
        assert not (root / "cockpit.db").exists()
    finally:
        os.close(fd)


def test_a_claim_racing_a_new_mint_adds_no_miss_to_the_new_code(tmp_path: Path) -> None:
    """B (codex MED): the claim read code A, a mint replaced it with B before the claim's write lock; the try was
    against A, so B keeps all its tries, and A is dead."""
    root = tmp_path / "cockpit"
    root.mkdir(mode=0o700)
    now = datetime.now(timezone.utc)
    db, other = CockpitDB(root), CockpitDB(root)
    a = db.mint_short_offer(label=None, minted_by="box", now=now)
    minted: list[str] = []
    real_write = db.write

    @contextmanager
    def mint_then_write() -> Iterator[Any]:
        minted.append(other.mint_short_offer(label=None, minted_by="box", now=now))
        with real_write() as conn:
            yield conn

    db.write = mint_then_write                                   # type: ignore[method-assign]
    for typed in ("0000-0000", a):                               # a wrong try, then A itself (right when it was read)
        assert db.redeem_short(typed, now=now) is None
    db.write = real_write                                        # type: ignore[method-assign]
    with other.read() as conn:
        assert conn.execute("SELECT misses FROM pair_short_offer").fetchone()[0] == 0
    assert db.redeem_short(minted[-1], now=now) is not None


def test_the_owner_key_proxied_is_no_credential_even_while_the_broker_is_on_standby(src) -> None:
    """C (codex MED): with the ledger locked elsewhere a paired lookup answers 503; the owner key off the box must
    never reach it, so it is the plain no-credential 403."""
    import fcntl
    import os

    with _serve(src) as (port, httpd):
        root = Path(httpd.broker.data_root)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(root / "cockpit.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for method, path, body in (("GET", "/cockpit/manifest.json", None), ("POST", "/cockpit/pair/offer", {})):
                st, out, _ = _call(port, method, path, body, token=T, headers=PROXY)
                assert (st, out.get("auth")) == (403, "pair"), (path, st, out)
        finally:
            os.close(fd)


def test_every_separately_gated_read_names_what_would_help_and_keeps_its_headers(rig) -> None:
    """D (codex MED): the reads gated past _route answer the same 403 as every other gate, Vary and no-store kept."""
    src, qdir = rig
    with _signed(src, qdir) as (port, _h):
        # the grant listing shares the sign listing's gate (_sign_read_admitted) and is unrouted on this server
        for path in ("/webpush/key", "/webpush/record", "/cockpit/sign.json", "/cockpit/consent/x.json", "/cockpit/question/1.json", "/cockpit/note/1.json",
                     "/cockpit/band.json", "/cockpit/threads.json", "/cockpit/board.json", "/cockpit/session/x.json"):
            st, out, hdrs = _call(port, "GET", path)
            assert (st, out.get("auth")) == (403, "owner"), (path, st, out)
            assert "token" in out["message"] and hdrs.get("cache-control") == "private, no-store", (path, hdrs)
            assert "X-Levain-Write-Token" in hdrs.get("vary", ""), (path, hdrs)
            st, out, _ = _call(port, "GET", path, token=T, headers=PROXY)
            assert (st, out.get("auth")) == (403, "pair"), (path, st, out)


def test_a_loopback_server_with_no_owner_key_offers_none(src) -> None:
    """E (codex MED): only a named surface configured, so there is no owner key to ask for."""
    with _serve(src, surface_tokens={"flowconnect": FC}) as (port, _h):
        assert _call(port, "GET", "/cockpit/manifest.json", token=FC)[1]["transport"]["owner_key"] is False
        st, out, _ = _call(port, "POST", "/cockpit/pair/offer", {})
        assert (st, out.get("auth")) == (403, "pair"), out
    with _serve(src) as (port, _h):
        assert _call(port, "GET", "/cockpit/manifest.json", token=T)[1]["transport"]["owner_key"] is True


def test_ping_test_with_a_device_says_so_and_refuses_a_malformed_id(rig, capsys) -> None:
    """G (complement LOW)."""
    from levain.cli import main

    _src, qdir = rig
    for bad in ("d-typo", "paired-d-0123456789ab", "D-0123456789AB"):
        assert main(["ping", "test", "--queue-dir", str(qdir), "--device", bad]) == 2, bad
        assert "not a device id" in capsys.readouterr().out
    assert main(["ping", "test", "--queue-dir", str(qdir), "--device", "d-0123456789ab"]) == 1
    assert capsys.readouterr().out.strip() == "no live subscription for that device"


def test_a_short_code_minted_by_a_device_then_revoked_cannot_redeem_for_the_minters_revoke(tmp_path: Path) -> None:
    """H3 (deepseek): ``minted_by`` is ``paired-d-...``; the redeem's own revoked-minter check reads past the prefix,
    so the code dies even where the revoke's expiry UPDATE did not reach it."""
    root = tmp_path / "cockpit"
    root.mkdir(mode=0o700)
    now = datetime.now(timezone.utc)
    db = CockpitDB(root)
    dev = db.redeem_short(db.mint_short_offer(label="ipad", minted_by="box", now=now), now=now)[0]
    code = db.mint_short_offer(label=None, minted_by=PAIRED_PREFIX + dev, now=now)
    db.revoke_devices([dev], by="box", now=now)
    with db.write() as conn:      # undo the revoke's expiry, so only the redeem's check stands between
        conn.execute("UPDATE pair_short_offer SET expires = ?", ("2999-01-01T00:00:00Z",))
    assert db.redeem_short(code, now=now) is None
    with db.read() as conn:
        assert tuple(conn.execute("SELECT misses, used_at FROM pair_short_offer").fetchone()) == (0, None)


def test_a_loopback_server_reached_through_a_proxy_header_refuses_the_owner_key(src) -> None:
    """I (deepseek LOW): a loopback bind is no exemption; an X-Forwarded-For makes the request proxied."""
    with _serve(src) as (port, _h):
        st, out, _ = _call(port, "POST", "/cockpit/pair/offer", {"label": "x"}, token=T, headers=PROXY)
        assert (st, out.get("auth")) == (403, "pair"), out


def test_a_refused_short_claim_says_guessed_out_apart_from_expired(src) -> None:
    """The ruled known edge (5 wrong tries kill the code) stands; its words now tell a guesser from a timeout."""
    with _serve(src) as (port, httpd):
        code = _short(port)
        wrong = "0000-0000" if code != "0000-0000" else "1111-1111"
        for _ in range(4):
            assert "check it and try again" in _call(port, "POST", "/cockpit/pair/claim", {"short": wrong})[1]["message"]
        assert "5 wrong tries were made" in _call(port, "POST", "/cockpit/pair/claim", {"short": wrong})[1]["message"]
        assert "5 wrong tries were made" in _call(port, "POST", "/cockpit/pair/claim", {"short": code})[1]["message"]
        expired = _short(port)
        conn = sqlite3.connect(httpd.broker.data_root / "cockpit.db")
        conn.execute("UPDATE pair_short_offer SET expires = '2000-01-01T00:00:00Z'")
        conn.commit()
        conn.close()
        assert "has expired" in _call(port, "POST", "/cockpit/pair/claim", {"short": expired})[1]["message"]
