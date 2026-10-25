"""S1 3c-9 session A: the CONTROL and keys service programs (`levain/slot/services/control.py`, `.../keys.py`).

Each program is its one serve call plus argv parsing (3c design §2.4): the AST pin below holds every call either makes
into `levain`. The service-entry tests run each program the way its unit does (`service_boot.main([name])`, the
program's argv then holding only its own name) and read what reached the serve call. The process test runs both
programs from this checkout in a temp store: CONTROL answers an operator's STATUS, keys attaches to CONTROL, and both
stop clean on SIGTERM."""
from __future__ import annotations

import ast
import os
import pwd
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain.broker import keys as K
from levain.slot import control as C
from levain.slot import layout, service_boot, units
from levain.slot.install_helper import operator_record_text
from levain.slot.services import control as SC
from levain.slot.services import keys as SK

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "levain" / "slot" / "services"
HOST = "darwin" if sys.platform == "darwin" else "linux"


def _levain_calls(path: Path) -> list[str]:
    """Every call in ``path`` whose callee is a name bound by a ``from levain... import`` (dotted as written)."""
    tree = ast.parse(path.read_text())
    bound = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
             and (n.module or "").startswith("levain") for a in n.names}
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        parts, f = [], n.func
        while isinstance(f, ast.Attribute):
            parts.append(f.attr)
            f = f.value
        if isinstance(f, ast.Name) and f.id in bound:
            out.append(".".join([f.id, *reversed(parts)]))
    return sorted(out)


def test_control_program_calls_nothing_but_serve():
    # the one act is C.serve; the rest builds its config (the record's parser, the layout's record path) or names a
    # refusal (a missing account, a missing record)
    assert _levain_calls(SERVICES / "control.py") == ["C.ServeConfig", "C.serve", "Refused", "Refused",
                                                      "Refused", "Refused", "operator_record_path", "parse_operator_record"]


def test_keys_program_calls_nothing_but_keys_main():
    # the one act is K.main; the two lookups are keys' own, named before it runs (EX_CONFIG, 3c-9 B ruling 2)
    assert _levain_calls(SERVICES / "keys.py") == ["K._account_uid", "K._gw_uids", "K.main"]


def test_built_services_lists_both_programs():
    have, pending = units.built_services(HOST, ROOT / "levain")
    names = {s.name for s in have}
    assert {"control", "keys"} <= names
    assert not [p for p in pending if "services/control.py" in p or "services/keys.py" in p]


#: runpy warns that the program module is already imported (this file imports it); the run is a fresh execution
_RERUN = pytest.mark.filterwarnings("ignore:'levain.slot.services:RuntimeWarning")


def _entry(monkeypatch, name: str) -> None:
    """``service-entry <name>`` as the unit runs it (run-dir check passed): the program runs as ``__main__``."""
    monkeypatch.setattr(sys, "argv", ["/usr/libexec/levain/service-entry", name])
    with pytest.raises(SystemExit) as e:
        service_boot.main([name], check=lambda h, n: (True, ""))
    assert e.value.code == 0


@_RERUN
def test_service_entry_runs_keys_on_the_install_paths(monkeypatch):
    seen = []
    monkeypatch.setattr(K, "main", lambda argv=None: seen.append(list(argv)) or 0)
    monkeypatch.setattr(K, "_gw_uids", lambda: frozenset({601}))     # an install with an egress slot
    monkeypatch.setattr(K, "_account_uid", lambda name: 602)
    _entry(monkeypatch, "keys")
    R, L = layout.RUN[HOST], layout.STORE[HOST]
    assert seen == [["--runtime-root", str(R), "--lib-root", str(L), "--control", str(R / "control" / "control.sock")]]


def test_keys_argv_flags_follow_the_install_paths():
    # keys' argparse keeps the last of a repeated flag, so a flag given here replaces the install's
    argv = SK.keys_argv(["keys", "--lib-root", "/x", "--test-single-uid"], host=HOST)
    assert argv[:6] == ["--runtime-root", str(layout.RUN[HOST]), "--lib-root", str(layout.STORE[HOST]),
                        "--control", str(layout.RUN[HOST] / "control" / "control.sock")]
    assert argv[6:] == ["--lib-root", "/x", "--test-single-uid"]


def test_keys_has_no_unmetered_path():
    # keys.main opens the ledger before it binds anything, and its parser has no flag that skips it
    src = (ROOT / "levain" / "broker" / "keys.py").read_text()
    main = src[src.index("def main(argv"):]
    assert "ledger.Ledger.open(" in main and main.index("ledger.Ledger.open(") < main.index("asyncio.run(_serve(")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main")
    flags = [c.args[0].value for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
             and c.func.attr == "add_argument" and c.args and isinstance(c.args[0], ast.Constant)]
    assert flags == ["--runtime-root", "--lib-root", "--control", "--test-single-uid"]


@pytest.fixture(autouse=True)
def _non_root(monkeypatch):
    # the behaviour tests run as an unprivileged uid wherever the suite runs; the root refusal and the process test
    # (a real child, `skipif` root) say otherwise for themselves
    monkeypatch.setattr(os, "geteuid", lambda: 1000)


_UIDS = {"_levain_keys": 701, "_levain_qbroker": 702, "_levain_dispatch": 703}


def _accounts(monkeypatch, uids=_UIDS):
    real = pwd.getpwnam

    def getpwnam(name):
        if name in uids:
            return SimpleNamespace(pw_uid=uids[name])
        if name.startswith("_levain"):
            raise KeyError(name)
        return real(name)
    monkeypatch.setattr(pwd, "getpwnam", getpwnam)


def _record(tmp_path: Path, uid: int = 501) -> Path:
    p = tmp_path / "operator"
    p.write_text(operator_record_text("phill", uid))
    return p


@_RERUN
def test_service_entry_runs_control_on_the_install_config(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(C, "serve", lambda config: seen.append(config) or 0)
    monkeypatch.setattr(layout, "operator_record_path", lambda host: _record(tmp_path))
    _accounts(monkeypatch)
    _entry(monkeypatch, "control")
    L, R = layout.STORE[HOST], layout.RUN[HOST]
    assert seen == [C.ServeConfig(
        store_dir=L / "sessions", socket_path=R / "control" / "control.sock", operator_uid=501,
        keys_admin_path=R / "keys" / "admin.sock", keys_uid=701, qbroker_uid=702, routes_dir=L / "routes",
        dispatch_uid=703, ceiling_path=None, ceiling_uid=0, lineage_dir=L / "lineage", host=HOST)]


@pytest.mark.parametrize("missing", ["_levain_keys", "_levain_qbroker", "_levain_dispatch"])
def test_control_refuses_a_missing_account(monkeypatch, tmp_path, capsys, missing):
    monkeypatch.setattr(C, "serve", lambda config: pytest.fail("served"))
    _accounts(monkeypatch, {k: v for k, v in _UIDS.items() if k != missing})
    assert SC.main(["control", "--operator-record", str(_record(tmp_path))]) == SC.EX_CONFIG
    assert f"no-account: {missing}" in capsys.readouterr().err


def test_control_refuses_without_an_operator_record(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(C, "serve", lambda config: pytest.fail("served"))
    _accounts(monkeypatch)
    assert SC.main(["--operator-record", str(tmp_path / "absent")]) == SC.EX_CONFIG
    assert "no-operator" in capsys.readouterr().err
    (tmp_path / "bad").write_text("{}")
    assert SC.main(["--operator-record", str(tmp_path / "bad")]) == SC.EX_CONFIG
    assert "operator-record-malformed" in capsys.readouterr().err


def test_control_refuses_an_unreadable_operator_record(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(C, "serve", lambda config: pytest.fail("served"))
    _accounts(monkeypatch)
    (tmp_path / "adir").mkdir()
    assert SC.main(["--operator-record", str(tmp_path / "adir")]) == SC.EX_CONFIG
    assert "operator-record-unreadable" in capsys.readouterr().err
    locked = tmp_path / "locked"
    locked.write_text(operator_record_text("phill", 501))
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):      # a uid that bypasses file modes cannot make this record unreadable
            pytest.skip("mode 0 is readable by this uid")
        assert SC.main(["--operator-record", str(locked)]) == SC.EX_CONFIG
        assert "operator-record-unreadable" in capsys.readouterr().err
    finally:
        locked.chmod(0o600)


@pytest.mark.parametrize("exc", [ValueError("x"), KeyError("uid"), TypeError("x")])
def test_control_refuses_a_record_the_parser_cannot_convert(monkeypatch, tmp_path, capsys, exc):
    monkeypatch.setattr(C, "serve", lambda config: pytest.fail("served"))
    _accounts(monkeypatch)

    def parse(data):
        raise exc
    monkeypatch.setattr(SC, "parse_operator_record", parse)
    assert SC.main(["--operator-record", str(_record(tmp_path))]) == SC.EX_CONFIG
    assert "operator-record-malformed" in capsys.readouterr().err


def test_control_refuses_a_non_integer_uid(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(C, "serve", lambda config: pytest.fail("served"))
    _accounts(monkeypatch)
    bad = tmp_path / "baduid"
    bad.write_text('{"format":"levain-operator/1","name":"phill","uid":"five"}')
    assert SC.main(["--operator-record", str(bad)]) == SC.EX_CONFIG
    assert "operator-record-malformed" in capsys.readouterr().err


def test_control_refuses_a_relative_path(monkeypatch):
    monkeypatch.setattr(C, "serve", lambda config: pytest.fail("served"))
    with pytest.raises(SystemExit) as e:
        SC.main(["--socket", "control.sock", "--operator-uid", "1", "--keys-uid", "2", "--qbroker-uid", "3",
                 "--dispatch-uid", "4"])
    assert e.value.code == 2


def test_control_program_refuses_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(C, "serve", lambda config: pytest.fail("served"))
    assert SC.main([]) == SC.EX_USAGE


def test_control_refused_start_exits_tempfail(monkeypatch):
    from levain.slot import Refused

    def serve(config):
        raise Refused("control-running", "held")
    monkeypatch.setattr(C, "serve", serve)
    args = ["--operator-uid", "1", "--keys-uid", "2", "--qbroker-uid", "3", "--dispatch-uid", "4"]
    assert SC.main(args) == SC.EX_REFUSED


# --- both programs as processes -------------------------------------------------------------------------------------

FAKE = 2_000_000_000
STOP_S = 30


class _Proc:
    def __init__(self, module: str, args: list[str]) -> None:
        self.lines: list[str] = []
        self.p = subprocess.Popen([sys.executable, "-m", module, *args], env={**os.environ, "PYTHONPATH": str(ROOT)},
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True)
        self._t = threading.Thread(target=self._pump, daemon=True)
        self._t.start()

    def _pump(self) -> None:
        assert self.p.stdout is not None
        for x in self.p.stdout:
            self.lines.append(x.rstrip())

    def stop(self) -> int:
        """SIGTERM, then SIGKILL after ``STOP_S``; always reaped, so the caller's assertion never leaves a child."""
        if self.p.poll() is None:
            self.p.send_signal(signal.SIGTERM)
        try:
            rc = self.p.wait(STOP_S)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()
            rc = -signal.SIGKILL
            self.lines.append(f"killed after {STOP_S}s without exiting on SIGTERM")
        self._t.join(5)
        return rc


def _connect(sock: Path, s: float = 20.0) -> socket.socket:
    """A connected client, retried until ``s``: the socket file can exist a beat before the server accepts."""
    end = time.monotonic() + s
    while True:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(10)
        try:
            c.connect(str(sock))
            return c
        except OSError:
            c.close()
            if time.monotonic() >= end:
                raise
            time.sleep(0.05)


def _until(pred, s: float = 20.0) -> bool:
    end = time.monotonic() + s
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def store():
    # a short base: the socket paths under it stay inside sun_path (104 bytes on macOS)
    base = Path(tempfile.mkdtemp(prefix="lv9a"))
    for d in ("L/sessions", "L/routes/dispatch", "L/routes/verifiers", "L/lineage", "L/keys", "L/policy", "R/keys",
              "R/control"):
        (base / d).mkdir(parents=True, mode=0o700)
    yield base
    shutil.rmtree(base, ignore_errors=True)


def _control_args(base: Path, operator: int, keys_uid: int) -> list[str]:
    L, R = base / "L", base / "R"
    return ["control", "--store-dir", str(L / "sessions"), "--socket", str(R / "control" / "control.sock"),
            "--keys-admin", str(R / "keys" / "admin.sock"), "--routes-dir", str(L / "routes"),
            "--lineage-dir", str(L / "lineage"), "--ceiling-uid", str(os.getuid()),
            "--operator-uid", str(operator), "--keys-uid", str(keys_uid), "--qbroker-uid", str(FAKE + 1),
            "--dispatch-uid", str(FAKE + 2)]


@pytest.mark.skipif(os.geteuid() == 0, reason="both programs refuse root")
def test_programs_run_as_processes(store):
    sock = store / "R" / "control" / "control.sock"
    # CONTROL alone, this uid its operator: STATUS answered, then a clean stop
    ctl = _Proc("levain.slot.services.control", _control_args(store, os.getuid(), FAKE + 3))
    reply, rc = None, None
    try:
        c = _connect(sock)
        try:
            c.sendall(C.encode_frame({"v": 1, "op": "STATUS", "id": 1}))
            reply = C.read_frame(c)
        finally:
            c.close()
    finally:
        rc = ctl.stop()
    assert reply == {"id": 1, "ok": True, "records": []}, ctl.lines
    assert rc == 0, ctl.lines
    # CONTROL with this uid as keys' peer, and keys (single-uid test form) attached to it; keys' ledger opened
    sock.unlink(missing_ok=True)
    ctl = _Proc("levain.slot.services.control", _control_args(store, FAKE + 4, os.getuid()))
    keys = None
    attached = False
    try:
        _connect(sock).close()
        keys = _Proc("levain.slot.services.keys", ["keys", "--runtime-root", str(store / "R"),
                                                   "--lib-root", str(store / "L"), "--control", str(sock),
                                                   "--test-single-uid"])
        attached = _until(lambda: any("gw-keys: attached" in x for x in keys.lines))
    finally:
        keys_rc = keys.stop() if keys is not None else None
        ctl_rc = ctl.stop()
    assert attached, keys.lines if keys else ctl.lines
    assert (store / "L" / "keys" / "ledger.jsonl").exists()
    assert keys_rc == 0, keys.lines
    assert "levain gw-keys: stopped" in keys.lines
    assert ctl_rc == 0, ctl.lines
