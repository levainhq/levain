"""The consent pings also go out as web push when a paired phone has pings on (P1 wiring)."""
from __future__ import annotations

import shutil
import time

from levain import webpush
from levain.consent import delivery
from levain.consent.adapters import claude_code
from levain.webpush import sender
from levain.webpush.store import DIR_NAME, PushStore
from tests._push_fixtures import phone, revoke

FCM = "https://fcm.googleapis.com/fcm/send/wire-test"


def _phone(qdir):
    phone(qdir, FCM)
    return PushStore(qdir)


def _spy(monkeypatch):
    calls = []
    monkeypatch.setattr(sender, "spawn", lambda qdir, **kw: calls.append(kw) or True)
    return calls


def test_no_phone_reads_nothing_and_creates_no_store(tmp_path, monkeypatch):
    calls = _spy(monkeypatch)
    assert webpush.live(tmp_path) is False
    assert claude_code.notify(claude_code.AdapterSettings(), "levain: x waits", cwd=tmp_path) is False
    assert calls == [] and not (tmp_path / DIR_NAME).exists()
    assert claude_code.ping_configured(claude_code.AdapterSettings(), tmp_path) is False


def test_a_phone_with_pings_on_gets_the_gate_ping_with_its_ref(tmp_path, monkeypatch):
    _phone(tmp_path)
    calls = _spy(monkeypatch)
    assert webpush.live(tmp_path) is True
    assert claude_code.ping_configured(claude_code.AdapterSettings(), tmp_path) is True
    assert claude_code.notify(claude_code.AdapterSettings(), "levain: claude waits on you: Bash (request r1).",
                              cwd=tmp_path, ref="request:r1") is True
    assert calls == [{"kind": "consent", "ref": "request:r1", "title": "levain",
                      "body": "claude waits on you: Bash (request r1).", "url": "/?view=needs", "command": None,
                      "push": True, "hold": None}]


def test_a_ruling_ping_reaches_the_phone_without_a_notify_command(tmp_path, monkeypatch):
    _phone(tmp_path)
    calls = _spy(monkeypatch)
    monkeypatch.setattr(claude_code, "load_settings", lambda qdir: claude_code.AdapterSettings())
    send = delivery.agent_notify(tmp_path, "claude_code", "r9")
    assert send is not None
    send("levain: ruling on gap 1 (ALLOW) waits: x.")
    assert calls and calls[0]["kind"] == "ruling" and calls[0]["ref"] == "request:r9"


def test_a_revoked_phone_is_not_live(tmp_path):
    store = _phone(tmp_path)
    revoke(store)
    assert webpush.live(tmp_path) is False


def _argv_settings(command):
    return claude_code.AdapterSettings(notify_argv=(command,))


def test_a_notify_command_only_install_still_gets_the_ping_recorded(tmp_path, monkeypatch):
    calls = _spy(monkeypatch)
    true = shutil.which("true")
    assert webpush.live(tmp_path) is False
    assert claude_code.notify(_argv_settings(true), "levain: claude waits on you: Bash (request r2).",
                              cwd=tmp_path, ref="request:r2") is True
    assert calls == [{"kind": "consent", "ref": "request:r2", "title": "levain",
                      "body": "claude waits on you: Bash (request r2).", "url": "/?view=needs", "command": True,
                      "push": False, "hold": None}]


def test_a_notify_command_that_cannot_start_is_recorded_as_not_started_and_the_ping_is_not_sent(tmp_path, monkeypatch):
    calls = _spy(monkeypatch)
    assert claude_code.notify(_argv_settings("bin/relative"), "levain: x", cwd=tmp_path) is False
    assert [c["command"] for c in calls] == [False]


def test_a_phone_and_a_command_share_one_ping_row(tmp_path, monkeypatch):
    _phone(tmp_path)
    calls = _spy(monkeypatch)
    assert claude_code.notify(_argv_settings(shutil.which("true")), "levain: x", cwd=tmp_path) is True
    assert len(calls) == 1 and calls[0]["command"] is True


def test_no_cwd_records_nothing(tmp_path, monkeypatch):
    calls = _spy(monkeypatch)
    assert claude_code.notify(_argv_settings(shutil.which("true")), "levain: x") is True
    assert calls == []


def test_the_store_records_whether_the_command_started_and_an_old_store_gains_the_column(tmp_path):
    store = PushStore(tmp_path)
    a = store.add_ping("consent", None, "levain", "a", True)
    b = store.add_ping("consent", None, "levain", "b", False)
    c = store.add_ping("consent", None, "levain", "c")
    got = {r["id"]: r["command"] for r in store.record()}
    assert got == {a: 1, b: 0, c: None}
    import sqlite3
    db = tmp_path / DIR_NAME / "webpush.db"
    conn = sqlite3.connect(db)
    conn.execute("DROP TRIGGER pings_frozen")
    conn.execute("DROP TRIGGER pings_kept")
    conn.execute("ALTER TABLE pings DROP COLUMN command")
    conn.commit()
    conn.close()
    assert [r["command"] for r in PushStore(tmp_path).record()] == [None, None, None]


def test_a_real_notify_command_ping_lands_in_the_record_through_the_detached_child(tmp_path):
    """No spy: the real gate path starts the command and the real child process writes the row (the RUN of item 0)."""
    marker = tmp_path / "ran"
    script = tmp_path / "ping.sh"
    script.write_text(f"#!/bin/sh\necho \"$1\" > {marker}\n")
    script.chmod(0o755)
    qdir = tmp_path / "q"
    qdir.mkdir()
    assert claude_code.notify(_argv_settings(str(script)), "levain: claude waits on you: Bash (request r7).",
                              cwd=qdir, ref="request:r7") is True
    deadline = time.monotonic() + 20
    rows = []
    while time.monotonic() < deadline and not (rows and marker.exists() and marker.stat().st_size):
        time.sleep(0.2)
        db = qdir / DIR_NAME / "webpush.db"
        rows = PushStore(qdir).record() if db.exists() else []
    assert [(r["kind"], r["ref"], r["body"], r["command"], r["deliveries"]) for r in rows] == [
        ("consent", "request:r7", "claude waits on you: Bash (request r7).", 1, [])]
    assert marker.exists() and marker.read_text().strip() == "levain: claude waits on you: Bash (request r7)."


def _cli_record(qdir, capsys):
    import argparse

    from levain.webpush import cli

    assert cli._record(argparse.Namespace(queue_dir=qdir, json=False, limit=10)) == 0
    return capsys.readouterr().out


def test_the_record_says_whether_the_notify_command_started(tmp_path, capsys):
    store = PushStore(tmp_path)
    store.add_ping("consent", None, "levain", "a", True)
    store.add_ping("reping", None, "levain", "b", False)
    store.add_ping("test", None, "levain", "c")
    lines = _cli_record(tmp_path, capsys).splitlines()
    out = "\n".join(lines)
    assert out.count("notify command started") == 1 and out.count("notify command did not start") == 1
    assert [ln for ln in lines if " test " in ln and "notify command" in ln] == []


def test_the_cockpit_panel_says_whether_the_notify_command_started(tmp_path, monkeypatch):
    from levain.cockpit import providers
    from levain.consent import queue as cqueue

    PushStore(tmp_path).add_ping("consent", "request:r1", "levain", "a", True)
    monkeypatch.setattr(cqueue, "default_queue_dir", lambda: tmp_path)
    got = providers._notifications()(None)
    assert [r.facets["legacy_meta"] for r in got.rows if "notify command started" in r.facets["legacy_meta"]]


def test_the_child_refuses_a_command_that_is_not_a_bool(tmp_path, monkeypatch, capsys):
    import io

    from levain.webpush import __main__ as child

    class In:
        buffer = io.BytesIO(b'{"title":"t","body":"b","ref":null,"url":"/","command":"yes"}')

    monkeypatch.setattr(child.sys, "stdin", In)
    assert child.main(["send", "--queue-dir", str(tmp_path), "--kind", "consent"]) == 2
    assert not (tmp_path / DIR_NAME).exists()


def test_a_command_only_ping_pushes_to_no_phone_even_if_a_subscription_appears_before_the_child_reads(tmp_path):
    store = _phone(tmp_path)    # the caller saw no live phone (push=False), yet a subscription is there when the child reads
    assert sender.send(tmp_path, kind="consent", ref=None, title="levain", body="b", command=True, push=False) == []
    assert [r["command"] for r in store.record()] == [1] and store.record()[0]["deliveries"] == []


def test_a_revoked_device_whose_subscription_was_left_unretired_is_not_sent_to(tmp_path):
    store = _phone(tmp_path)
    revoke(store)
    assert store.live_subscriptions() == []
