"""The phone app (P2 state line, P3 board, pairing): what the phone may do through the cockpit's own verb kernel, how a
phone pairs, and what the phone port's seam lets each credential reach. Built from the 10-19 RUNs on a scratch install
(levain project_memory/1019-app/PHONE_APP_RUN.md, run_devices.py), where each of these paths was first executed."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.phone_app import PhoneApp, PhoneAppError


@pytest.fixture
def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """A real `levain init --adapter claude-code --consent-hook on` under a scratch HOME, with two Tray items and one
    loop; returns (install root, consent queue dir)."""
    from anneal_memory.spores import SporeStore

    from levain.cli import main

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in ("LEVAIN_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        monkeypatch.delenv(var, raising=False)
    assert main(["init", "--answers-template"]) in (0, None)
    answers = json.loads(capsys.readouterr().out)
    answers.update(OPERATOR_NAME="Test", ENTITY_NAME="test")
    (tmp_path / "answers.json").write_text(json.dumps(answers))
    inst = tmp_path / "inst"
    assert main(["init", "--path", str(inst), "--adapter", "claude-code", "--answers", str(tmp_path / "answers.json"),
                 "--consent-hook", "on", "--force"]) in (0, None)
    store = SporeStore(inst / ".levain" / "memory.spores.json")
    store.add(type="task", text="tray one", disposition="seed")
    store.add(type="task", text="tray two", disposition="seed")
    store.add(type="task", text="a loop")
    from levain import install as install_mod

    return inst, install_mod._consent_queue_dir()       # conftest points init's queue at a per-test directory


def _panel(board: dict, pid: str) -> dict:
    return next(p for p in board["panels"] if p["id"] == pid)


def test_phone_sets_the_state_line_and_works_the_tray_only_as_the_kernel_allows(install):
    inst, qdir = install
    app = PhoneApp(qdir)
    state = app.state()
    act = state["actions"][0]
    assert (act["verb"], act["gesture"]) == ("operator_state", "direct")
    app.fire({"verb": "operator_state", "panel_id": "state", "params": {"text": "on the phone"},
              "panel_version": act["target"]["panel_version"]})
    ctx = json.loads((inst / ".levain" / "context.json").read_text())
    assert (ctx["state"], ctx["state_source"]) == ("on the phone", "phone")

    tray = _panel(app.board(), "tray")
    first, second = tray["rows"][:2]
    gestures = {a["verb"]: a["gesture"] for a in first["actions"]}
    assert gestures["spore_descend"] == gestures["spore_surface_at"] == "direct"
    assert gestures["spore_update"] == "laptop-only"
    app.fire({"verb": "spore_surface_at", "panel_id": "tray", "row_id": first["id"], "row_version": first["version"],
              "params": {"surface_at": (datetime.now().date() + timedelta(days=2)).isoformat()}})
    kind = next(a for a in second["actions"] if a["verb"] == "spore_descend")["options"]["spore_kind"][0]
    app.fire({"verb": "spore_descend", "panel_id": "tray", "row_id": second["id"], "row_version": second["version"],
              "params": {"spore_kind": kind}, "confirm": True})
    assert _panel(app.board(), "tray")["rows"] == []        # one held to its date, one resolved

    # capture stays on the laptop (b1); edits and other panels are not on the phone at all
    for req in ({"verb": "spore_seed", "panel_id": "tray", "params": {"text": "x"}},
                {"verb": "spore_update", "panel_id": "tray", "params": {}},
                {"verb": "spore_descend", "panel_id": "keep", "params": {}}):
        with pytest.raises(PhoneAppError) as exc:
            app.fire(req)
        assert exc.value.status == 403






def _raise_on_the_laptop(inst: Path, tmp_path: Path, verb: str) -> Path:
    """A laptop-presence key enrolled in the install's cockpit journal and a signed raise of ``verb`` to T2, anchored as
    the broker anchors its own appends; returns the cockpit data root."""
    import sqlite3
    import subprocess

    import levain.authority as auth
    from levain.authority import ReplayCache

    key = tmp_path / "laptop"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "laptop", "-f", str(key)], check=True,
                   capture_output=True, timeout=30)
    public = " ".join((tmp_path / "laptop.pub").read_text().split()[:2])
    fp = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(key)], check=True, capture_output=True,
                        text=True).stdout.split()[1]

    def sign(message: bytes) -> str:
        return subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(key), "-n", auth.NAMESPACE], input=message,
                              capture_output=True, check=True, timeout=30).stdout.decode()

    root = inst / ".levain" / "cockpit"
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    try:
        auth.create_schema(conn)
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        for op in ({"op": "enrol", "fingerprint": fp, "public_key": public, "class": "laptop-presence",
                    "custody": "passphrase"}, {"op": "raise", "verb": verb, "tier": "T2"}):
            auth.sign_and_append(conn, op, signer_fp=fp, sign=sign, clock=lambda: datetime.now(timezone.utc),
                                 cache=ReplayCache(), anchor=auth.anchor_path(root))
    finally:
        conn.close()
    return root


def test_a_verb_the_laptop_raised_is_laptop_only_on_the_phone(install, tmp_path: Path):
    inst, qdir = install
    _raise_on_the_laptop(inst, tmp_path, "operator_state")
    app = PhoneApp(qdir)
    act = app.state()["actions"][0]
    assert (act["verb"], act["gesture"]) == ("operator_state", "laptop-only")
    with pytest.raises(PhoneAppError) as exc:
        app.fire({"verb": "operator_state", "panel_id": "state", "params": {"text": "raised"},
                  "panel_version": act["target"]["panel_version"]})
    assert exc.value.status == 403
    ctx = inst / ".levain" / "context.json"
    assert not ctx.exists() or json.loads(ctx.read_text()).get("state") != "raised"


def test_a_governed_install_whose_log_cannot_be_read_takes_no_phone_writes(install, tmp_path: Path):
    from levain.cockpit.install import marker_path

    inst, qdir = install
    root = _raise_on_the_laptop(inst, tmp_path, "spore_note")
    markers = tmp_path / "markers"
    markers.mkdir()
    marker_path(root, markers).write_text("{}")
    (root / "cockpit.db").write_bytes(b"not a database")
    app = PhoneApp(qdir, marker_directory=markers)
    act = app.state()["actions"][0]                     # reads still answer, every verb laptop-only
    assert act["gesture"] == "laptop-only"
    with pytest.raises(PhoneAppError) as exc:
        app.fire({"verb": "operator_state", "panel_id": "state", "params": {"text": "x"},
                  "panel_version": act["target"]["panel_version"]})
    assert (exc.value.status, exc.value.error) == (503, "authority_unreadable")


def test_a_governed_install_with_no_authority_log_yet_takes_no_phone_writes(install, tmp_path: Path):
    from levain.cockpit.install import marker_path

    inst, qdir = install
    root = inst / ".levain" / "cockpit"
    root.mkdir(parents=True, exist_ok=True)
    markers = tmp_path / "markers"
    markers.mkdir()
    marker_path(root, markers).write_text("{}")
    assert not (root / "cockpit.db").exists()
    app = PhoneApp(qdir, marker_directory=markers)
    act = app.state()["actions"][0]                     # reads unchanged
    with pytest.raises(PhoneAppError) as exc:
        app.fire({"verb": "operator_state", "panel_id": "state", "params": {"text": "x"},
                  "panel_version": act["target"]["panel_version"]})
    assert (exc.value.status, exc.value.error) == (503, "authority_absent")
    assert "open the cockpit once on the laptop" in str(exc.value)
    ctx = inst / ".levain" / "context.json"
    assert not ctx.exists() or json.loads(ctx.read_text()).get("state") != "x"
