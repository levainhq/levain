"""The doctor's ``TLS trust store`` row (levain#198): a python.org Python on macOS ships no CA bundle until its
``Install Certificates.command`` runs, so the broker's HTTPS to a provider fails CERTIFICATE_VERIFY_FAILED. The row
reads the store through an injectable probe, so no test needs a broken Python."""
from __future__ import annotations

from pathlib import Path

from levain import doctor


def _probe(x509_ca: int, cafile: bool = False, capath: bool = False):
    return lambda: {"x509_ca": x509_ca, "cafile_readable": cafile, "capath_readable": capath}


def test_empty_store_and_no_default_paths_fails_naming_the_command():
    base = "/Library/Frameworks/Python.framework/Versions/3.13"
    rows = doctor._check_tls_trust(probe=_probe(0), version=(3, 13), prefix=base, platform="darwin")
    assert len(rows) == 1 and rows[0].name == "TLS trust store"
    assert not rows[0].ok
    assert "/Applications/Python 3.13/Install Certificates.command" in (rows[0].hint or "")


def test_the_command_follows_the_running_version_not_a_hard_coded_one():
    base = "/Library/Frameworks/Python.framework/Versions/3.14"
    rows = doctor._check_tls_trust(probe=_probe(0), version=(3, 14), prefix=base, platform="darwin")
    assert "/Applications/Python 3.14/Install Certificates.command" in (rows[0].hint or "")
    assert "3.13" not in (rows[0].hint or "")


def test_a_python_org_prefix_decides_the_command():
    # the command is named only for a python.org framework build: any other Python gets words, not a path to run
    rows = doctor._check_tls_trust(probe=_probe(0), version=(3, 12), prefix="/opt/homebrew/opt/python@3.12",
                                   platform="darwin")
    assert not rows[0].ok
    assert "Install Certificates.command" not in (rows[0].hint or "")


def test_loaded_certificates_leave_no_row():
    assert doctor._check_tls_trust(probe=_probe(140), version=(3, 13),
                                   prefix="/Library/Frameworks/Python.framework/Versions/3.13",
                                   platform="darwin") == []


def test_a_readable_default_cafile_or_capath_leaves_no_row():
    # capath certificates load lazily, so an empty x509_ca alongside a readable path is not an empty store
    for kw in ({"cafile": True}, {"capath": True}):
        assert doctor._check_tls_trust(probe=_probe(0, **kw), version=(3, 13),
                                       prefix="/Library/Frameworks/Python.framework/Versions/3.13",
                                       platform="darwin") == []


def test_a_probe_that_raises_is_a_warning_never_a_pass():
    def boom():
        raise OSError("no ssl")

    rows = doctor._check_tls_trust(probe=boom, version=(3, 13), prefix="/x", platform="darwin")
    assert len(rows) == 1 and rows[0].ok and rows[0].warn


def test_the_real_probe_answers_the_three_facts():
    got = doctor._tls_store_probe()
    assert set(got) == {"x509_ca", "cafile_readable", "capath_readable"}
    assert isinstance(got["x509_ca"], int)


def test_the_readme_install_section_names_the_command():
    text = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    assert "Install Certificates.command" in text
