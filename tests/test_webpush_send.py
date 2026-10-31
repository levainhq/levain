"""Web push transport, fix round 2 (L3 r1 rows S1-S9): vapid cache, send budget, spawn, retire, errors, record."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from levain.webpush import ece, record, sender, vapid
from levain.webpush.store import PushStore
from tests._push_fixtures import owned_devices


@pytest.fixture(autouse=True)
def _owned_devices(monkeypatch):
    """Every device a test here subscribes under is a paired device of a journal beside the queue, as every production
    subscribe's is: a subscription is live only under a paired device its journal holds (``store.live_rows``)."""
    owned_devices(monkeypatch)

FCM = "https://fcm.googleapis.com/fcm/send/abc"


def _jwt_verifies(jwt: str, public_b64: str) -> bool:
    head, claims, sig = jwt.split(".")
    raw = ece.b64d(sig)
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ece.b64d(public_b64))
    try:
        pub.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                   f"{head}.{claims}".encode("ascii"), ec.ECDSA(hashes.SHA256()))
    except Exception:  # noqa: BLE001
        return False
    return True


def _parse(header: str) -> tuple[str, str]:
    t, k = header.removeprefix("vapid ").split(", ")
    return t.removeprefix("t="), k.removeprefix("k=")


def _ua_keys():
    ua = ec.generate_private_key(ec.SECP256R1())
    return ece.b64e(ece.public_bytes(ua)), ece.b64e(os.urandom(16))


def test_s1_a_rotate_between_key_read_and_token_cache_never_leaves_a_mismatched_token(tmp_path, monkeypatch):
    vapid.ensure_keys(tmp_path)
    real = vapid.ensure_keys

    def racing(queue_dir, *a, **kw):
        out = real(queue_dir, *a, **kw)
        if not kw.get("rotate") and not racing.done:
            racing.done = True
            real(queue_dir, rotate=True)   # the rotate lands right after the key was read
        return out

    racing.done = False
    monkeypatch.setattr(vapid, "ensure_keys", racing)
    vapid.authorization(tmp_path, FCM)
    monkeypatch.setattr(vapid, "ensure_keys", real)
    jwt, public = _parse(vapid.authorization(tmp_path, FCM)["Authorization"])
    assert public == real(tmp_path)[1] and _jwt_verifies(jwt, public)


def test_s1_a_cached_token_for_another_public_key_is_not_reused(tmp_path):
    vapid.ensure_keys(tmp_path)
    vapid.authorization(tmp_path, FCM)
    other = ece.b64e(ece.public_bytes(ec.generate_private_key(ec.SECP256R1())))
    with PushStore(tmp_path).write() as c:
        c.execute("UPDATE vapid_tokens SET jwt = ? || ? || jwt", (other, ":"))
    jwt, public = _parse(vapid.authorization(tmp_path, FCM)["Authorization"])
    assert public == vapid.ensure_keys(tmp_path)[1] and _jwt_verifies(jwt, public)


def test_s2_every_send_starts_at_once_and_an_unfinished_one_is_unknown_not_zero(tmp_path, monkeypatch):
    store = PushStore(tmp_path)
    for i in range(12):
        p, a = _ua_keys()
        store.add_subscription(f"https://fcm.googleapis.com/fcm/send/{i}", p, a, "d%d" % (i % 4))
    started, release = [], threading.Event()
    monkeypatch.setattr(sender, "WALL_BUDGET", 0.6)

    def slow(req):
        started.append(threading.current_thread())
        release.wait(5)
        return 201

    monkeypatch.setattr(sender, "_post", slow)
    t0 = time.monotonic()
    got = sender.send(tmp_path, kind="test", ref=None, title="t", body="b")
    took = time.monotonic() - t0
    assert len(got) == 12 and len(started) == 12 and took < 3
    assert all(s == sender.UNKNOWN for _, s in got) and sender.UNKNOWN != 0
    assert all(t.daemon for t in started)
    release.set()
    time.sleep(0.3)   # a late answer is never written back
    assert {d["status"] for d in store.record()[0]["deliveries"]} == {sender.UNKNOWN}


def test_s3_spawn_uses_dash_P_and_a_cwd_that_is_not_the_queue_dir_and_the_child_can_import_levain(tmp_path, monkeypatch):
    seen = {}
    real_run = subprocess.run

    class P:
        def __init__(self, argv, **kw):
            seen.update(argv=argv, kw=kw)
            self.stdin = type("S", (), {"write": lambda s, d: None, "close": lambda s: None})()

    monkeypatch.setattr(sender.subprocess, "Popen", P)
    assert sender.spawn(tmp_path, kind="consent", ref="r", title="t", body="b")
    assert "-P" in seen["argv"] and seen["argv"].index("-P") < seen["argv"].index("-m")
    assert seen["kw"]["cwd"] != str(tmp_path)
    monkeypatch.undo()
    run = real_run([sys.executable, "-P", "-c", "import levain.webpush"], cwd=seen["kw"]["cwd"],
                         env=seen["kw"]["env"], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


def test_s4_a_subscription_that_cannot_be_prepared_is_retired(tmp_path, monkeypatch):
    store = PushStore(tmp_path)
    p, a = _ua_keys()
    bad = store.add_subscription(FCM, "not-a-key", a, "d")
    good = store.add_subscription(FCM + "2", p, a, "d")
    monkeypatch.setattr(sender, "_post", lambda req: 201)
    got = dict(sender.send(tmp_path, kind="test", ref=None, title="t", body="b"))
    assert got[bad] == 0 and got[good] == 201
    assert [r["id"] for r in store.live_subscriptions()] == [good]


def test_s4_a_store_fault_on_the_vapid_token_keeps_the_subscription(tmp_path, monkeypatch):
    store = PushStore(tmp_path)
    p, a = _ua_keys()
    sid = store.add_subscription(FCM, p, a, "d")

    def busy(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sender.vapid, "authorization", busy)
    assert dict(sender.send(tmp_path, kind="test", ref=None, title="t", body="b"))[sid] == 0
    assert [r["id"] for r in store.live_subscriptions()] == [sid]


def test_s5_a_send_child_exception_lands_in_a_bounded_log_and_exits_nonzero(tmp_path, monkeypatch):
    from levain.webpush import __main__ as child

    def boom(*a, **kw):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(sender, "send", boom)
    monkeypatch.setattr(sys, "stdin", type("I", (), {"buffer": __import__("io").BytesIO(
        json.dumps({"title": "t", "body": "b", "ref": None, "url": "/"}).encode())})())
    log = tmp_path / "webpush" / "send-errors.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(f"old {i} {'x' * 300}\n" for i in range(300)))      # past the size bound
    assert child.main(["send", "--queue-dir", str(tmp_path), "--kind", "test"]) != 0
    lines = log.read_text().splitlines()
    assert len(lines) == 200 and "store exploded" in lines[-1]


def test_s7_ping_record_cli_carries_the_outcome(tmp_path, capsys):
    from levain.cli import main

    store = PushStore(tmp_path)
    store.add_ping("test", None, "t", "b")
    assert main(["ping", "record", "--queue-dir", str(tmp_path)]) == 0
    assert "nothing to follow up" in capsys.readouterr().out
    assert main(["ping", "record", "--json", "--queue-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)[0]["outcome"]["state"] == "none"


def test_s8_the_outcome_pretends_no_via(tmp_path):
    assert "via" not in record.outcome_of(tmp_path, "nothing")
    assert "via" not in record.outcome_of(tmp_path, "question:999")


def test_s9_a_ping_without_an_outcome_does_not_kill_the_panel(tmp_path, monkeypatch):
    from levain.cockpit import providers
    from levain.cockpit.results import Read

    PushStore(tmp_path).add_ping("test", None, "t", "b")
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path.parent / "home"))
    from levain.consent import queue as cq

    monkeypatch.setattr(cq, "default_queue_dir", lambda: tmp_path)
    ping = PushStore(tmp_path).record()[0]
    monkeypatch.setattr(record, "record", lambda q, rows: [ping])   # no "outcome" key
    got = providers._notifications()(None)
    assert isinstance(got, Read) and len(got.rows) == 1


def test_spawn_keeps_the_inherited_pythonpath_and_hands_the_child_an_absolute_queue_dir(tmp_path, monkeypatch):
    seen = {}

    class P:
        def __init__(self, argv, **kw):
            seen.update(argv=argv, kw=kw)
            self.stdin = type("S", (), {"write": lambda s, d: None, "close": lambda s: None})()

    monkeypatch.setenv("PYTHONPATH", "/kept/one")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sender.site, "getsitepackages", lambda: [])
    monkeypatch.setattr(sender.subprocess, "Popen", P)
    assert sender.spawn("q", kind="consent", ref="r", title="t", body="b")
    parts = seen["kw"]["env"]["PYTHONPATH"].split(os.pathsep)
    assert parts[0] == str(__import__("pathlib").Path(sender.__file__).resolve().parents[2]) and "/kept/one" in parts
    qd = seen["argv"][seen["argv"].index("--queue-dir") + 1]
    assert qd == str((tmp_path / "q").resolve())


def test_two_failing_children_each_keep_their_log_line(tmp_path):
    from levain.webpush import __main__ as child

    (tmp_path / "webpush").mkdir()

    def fail(n):
        for i in range(50):
            child._log_error(tmp_path, RuntimeError(f"child {n} line {i}"))

    threads = [threading.Thread(target=fail, args=(n,)) for n in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = (tmp_path / "webpush" / "send-errors.log").read_text().splitlines()
    assert len(lines) == 100 and all(any(f"child {n} line {i}" in ln for ln in lines) for n in (1, 2) for i in range(50))


def test_an_unknown_or_unanswered_delivery_reads_as_words_in_the_record_and_the_panel(tmp_path, monkeypatch, capsys):
    from levain.cli import main
    from levain.cockpit import providers
    from levain.consent import queue as cq

    store = PushStore(tmp_path)
    p, a = _ua_keys()
    late = store.add_subscription(FCM, p, a, "d")
    mute = store.add_subscription(FCM + "2", p, a, "d")
    ping = store.add_ping("test", None, "t", "b")
    store.add_delivery(ping, late, sender.UNKNOWN)
    store.add_delivery(ping, mute, 0)
    assert main(["ping", "record", "--queue-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert f"sub {late}: no answer in time" in out and f"sub {mute}: no answer]" in out
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path.parent / "home"))
    monkeypatch.setattr(cq, "default_queue_dir", lambda: tmp_path)
    sent = providers._notifications()(None).rows[0].stored["sent"]
    assert sent == "no answer in time, no answer"


class _Refusal(sender.urllib.error.HTTPError):
    def __init__(self, code, body):
        super().__init__("https://web.push.apple.com/x", code, "refused", {}, None)
        self._body = body

    def read(self, n=-1):
        return self._body[:n] if n >= 0 else self._body


def _refused_by(monkeypatch, code, body):
    def boom(req, timeout=None):
        raise _Refusal(code, body)

    monkeypatch.setattr(sender._OPENER, "open", boom)
    return sender._post(object())


def test_a_refusal_that_names_the_key_retires_the_subscription(tmp_path, monkeypatch):
    # Apple's own reply for a subscription made under another install's VAPID key (measured on the iPhone, 10-19)
    got = _refused_by(monkeypatch, 400, b'{"reason":"VapidPkHashMismatch"}')
    assert got == 400 and isinstance(got, sender._Dead)
    store = PushStore(tmp_path)
    p, a = _ua_keys()
    sid = store.add_subscription(FCM, p, a, "d")
    monkeypatch.setattr(sender, "_post", lambda req: sender._Dead(400))
    assert dict(sender.send(tmp_path, kind="test", ref=None, title="t", body="b"))[sid] == 400
    assert store.live_subscriptions() == []


def test_an_ordinary_400_or_an_unreadable_body_keeps_the_subscription(tmp_path, monkeypatch):
    for body in (b'{"reason":"PayloadTooLarge"}', b""):
        got = _refused_by(monkeypatch, 400, body)
        assert got == 400 and not isinstance(got, sender._Dead)
    store = PushStore(tmp_path)
    p, a = _ua_keys()
    sid = store.add_subscription(FCM, p, a, "d")
    monkeypatch.setattr(sender, "_post", lambda req: 400)
    sender.send(tmp_path, kind="test", ref=None, title="t", body="b")
    assert [r["id"] for r in store.live_subscriptions()] == [sid]
