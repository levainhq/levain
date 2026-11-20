"""The doctor's ``TLS trust store`` row (levain#198): a python.org Python on macOS ships no CA bundle until its
``Install Certificates.command`` runs, so this Python's HTTPS to a provider fails CERTIFICATE_VERIFY_FAILED. The row
reads the store through an injectable probe, so no test needs a broken Python; the real probe is exercised in a fresh
interpreter."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from levain import doctor

_PY = sys.executable
_REPO = str(Path(__file__).resolve().parent.parent)
_SCRIPT = ("import sys; sys.path.insert(0, sys.argv[1]); from levain import doctor; "
           "print('row' if doctor._check_tls_trust(version=(3, 13), prefix='/x', platform='linux') else 'none')")


def _probe(x509_ca: int, capath: bool = False):
    return lambda: {"x509_ca": x509_ca, "capath_readable": capath}


def test_empty_store_and_no_default_paths_warns_naming_the_command():
    base = "/Library/Frameworks/Python.framework/Versions/3.13"
    rows = doctor._check_tls_trust(probe=_probe(0), version=(3, 13), prefix=base, platform="darwin")
    assert len(rows) == 1 and rows[0].name == "TLS trust store"
    assert rows[0].ok and rows[0].warn
    assert "/Applications/Python 3.13/Install Certificates.command" in (rows[0].hint or "")


def test_the_detail_speaks_of_this_python_not_the_broker():
    rows = doctor._check_tls_trust(probe=_probe(0), version=(3, 13), prefix="/x", platform="linux")
    assert "this Python (the one running levain)" in rows[0].detail
    assert "broker" not in rows[0].detail


def test_the_command_follows_the_running_version_not_a_hard_coded_one():
    base = "/Library/Frameworks/Python.framework/Versions/3.14"
    rows = doctor._check_tls_trust(probe=_probe(0), version=(3, 14), prefix=base, platform="darwin")
    assert "/Applications/Python 3.14/Install Certificates.command" in (rows[0].hint or "")
    assert "3.13" not in (rows[0].hint or "")


def test_a_python_org_prefix_decides_the_command():
    # the command is named only for a python.org framework build: any other Python gets words, not a path to run
    rows = doctor._check_tls_trust(probe=_probe(0), version=(3, 12), prefix="/opt/homebrew/opt/python@3.12",
                                   platform="darwin")
    assert rows[0].warn
    assert "Install Certificates.command" not in (rows[0].hint or "")


def test_loaded_certificates_leave_no_row():
    assert doctor._check_tls_trust(probe=_probe(140), version=(3, 13),
                                   prefix="/Library/Frameworks/Python.framework/Versions/3.13",
                                   platform="darwin") == []


def test_a_readable_capath_leaves_no_row():
    # capath certificates load lazily, so an empty x509_ca alongside a readable capath is not an empty store
    assert doctor._check_tls_trust(probe=_probe(0, capath=True), version=(3, 13),
                                   prefix="/Library/Frameworks/Python.framework/Versions/3.13",
                                   platform="darwin") == []


def test_a_probe_that_raises_is_a_warning_never_a_pass():
    def boom():
        raise OSError("no ssl")

    rows = doctor._check_tls_trust(probe=boom, version=(3, 13), prefix="/x", platform="darwin")
    assert len(rows) == 1 and rows[0].ok and rows[0].warn


def test_the_real_probe_answers_the_two_facts():
    got = doctor._tls_store_probe()
    assert set(got) == {"x509_ca", "capath_readable"}
    assert isinstance(got["x509_ca"], int)


class _Stub:
    def __init__(self, x509_ca):
        self._n = x509_ca

    def cert_store_stats(self):
        return {"x509_ca": self._n}


def _stub_ssl(monkeypatch, x509_ca, capath):
    import collections
    import ssl
    paths = collections.namedtuple("P", "capath")(capath)
    monkeypatch.setattr(ssl, "create_default_context", lambda: _Stub(x509_ca))
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: paths)


def test_a_capath_list_is_probed_segment_by_segment(tmp_path, monkeypatch):
    empty, full = tmp_path / "a", tmp_path / "b"
    empty.mkdir()
    full.mkdir()
    (full / "x.pem").write_text("x")
    _stub_ssl(monkeypatch, 0, os.pathsep.join([str(empty), str(full)]))
    assert doctor._tls_store_probe()["capath_readable"] is True
    _stub_ssl(monkeypatch, 0, os.pathsep.join([str(empty), str(tmp_path / "none")]))
    assert doctor._tls_store_probe()["capath_readable"] is False


def test_the_capath_scan_is_closed_and_skipped_when_certificates_loaded(tmp_path, monkeypatch):
    (tmp_path / "x.pem").write_text("x")
    closed = []
    real = os.scandir

    class _It:
        def __init__(self, path):
            self._it = real(path)

        def __enter__(self):
            return self._it

        def __exit__(self, *exc):
            closed.append(True)
            self._it.close()

    monkeypatch.setattr(os, "scandir", _It)
    _stub_ssl(monkeypatch, 0, str(tmp_path))
    assert doctor._tls_store_probe()["capath_readable"] is True and closed == [True]
    closed.clear()
    _stub_ssl(monkeypatch, 5, str(tmp_path))
    doctor._tls_store_probe()
    assert closed == []


def test_an_unreadable_capath_is_not_a_store(tmp_path, monkeypatch):
    def boom(path):
        raise PermissionError("denied")
    monkeypatch.setattr(os, "scandir", boom)
    _stub_ssl(monkeypatch, 0, str(tmp_path))
    assert doctor._tls_store_probe()["capath_readable"] is False


def _fresh(extra: dict) -> str:
    # the real probe in a fresh interpreter: the default context reads SSL_CERT_FILE / SSL_CERT_DIR when created
    env = {k: v for k, v in os.environ.items() if k not in ("SSL_CERT_FILE", "SSL_CERT_DIR")}
    env.update(extra)
    out = subprocess.run([_PY, "-I", "-c", _SCRIPT, _REPO], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().splitlines()[-1]


def test_an_empty_cafile_and_empty_capath_fire_the_row_in_a_fresh_interpreter(tmp_path):
    empty_file = tmp_path / "empty.pem"
    empty_file.write_text("")
    empty_dir = tmp_path / "empty_dir"
    empty_dir.mkdir()
    assert _fresh({"SSL_CERT_FILE": str(empty_file), "SSL_CERT_DIR": str(empty_dir)}) == "row"


def test_the_system_default_store_leaves_no_row_in_a_fresh_interpreter():
    count = subprocess.run([_PY, "-I", "-c", "import ssl; print(ssl.create_default_context().cert_store_stats()['x509_ca'])"],
                           capture_output=True, text=True, timeout=60)
    if count.returncode != 0 or int(count.stdout.strip() or 0) == 0:
        pytest.skip("that interpreter has no default CA store")
    assert _fresh({}) == "none"


def test_run_doctor_prints_the_trust_row_when_the_store_is_empty(tmp_path, capsys, monkeypatch):
    # deleting the row's core.extend line in _run_doctor turns this red
    monkeypatch.setattr(doctor, "_tls_store_probe", lambda: {"x509_ca": 0, "capath_readable": False})
    doctor.run_doctor(tmp_path)
    assert "TLS trust store: this Python" in capsys.readouterr().out


def test_the_readme_install_section_names_the_command():
    text = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    assert "Install Certificates.command" in text
