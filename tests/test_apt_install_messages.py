"""tests/ci/apt_install.sh names the cause it can tell apart and never blames the runner's network for the rest.

The script runs as a subprocess with stub `sudo`, `timeout`, `sed` and `apt-get` first on PATH inside a tmp dir: no real
apt, no network, and the sources-file rewrite the script performs on a runner is a stub no-op here.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "ci" / "apt_install.sh"

# `update` and `install` each exit with the status named by the environment and, for install, print the text named by
# it; the stub appends every verb to a call log so a test can see what retried.
APT_GET = """#!/usr/bin/env bash
verb=""
for a in "$@"; do
  case "$a" in update|install) verb="$a" ;; esac
done
echo "$verb" >> "$STUB_CALLS"
if [ "$verb" = update ]; then
  exit "${STUB_UPDATE_RC:-0}"
fi
if [ -n "${STUB_INSTALL_OUT:-}" ]; then
  printf '%s\\n' "$STUB_INSTALL_OUT"
fi
exit "${STUB_INSTALL_RC:-0}"
"""

SUDO = '#!/usr/bin/env bash\nexec "$@"\n'
# timeout is invoked as `timeout -k 10 300 <cmd>...`
TIMEOUT = '#!/usr/bin/env bash\nshift 3\nexec "$@"\n'
# the sources rewrite must never touch the machine running the test
SED = "#!/usr/bin/env bash\nexit 0\n"


@pytest.fixture
def run(tmp_path):
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in (("apt-get", APT_GET), ("sudo", SUDO), ("timeout", TIMEOUT), ("sed", SED)):
        p = stubs / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    calls = tmp_path / "calls.log"
    calls.write_text("")
    home = tmp_path / "home"
    home.mkdir()

    def go(*pkgs, update_rc=0, install_rc=0, install_out=""):
        env = {
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(home),
            "LEVAIN_HOME": str(home / ".levain"),
            "TMPDIR": str(tmp_path),
            "STUB_CALLS": str(calls),
            "STUB_UPDATE_RC": str(update_rc),
            "STUB_INSTALL_RC": str(install_rc),
            "STUB_INSTALL_OUT": install_out,
        }
        r = subprocess.run(
            ["bash", str(SCRIPT), *pkgs], env=env, capture_output=True, text=True, timeout=60, check=False
        )
        return r, calls.read_text().split()

    return go


def test_success_installs_once(run):
    r, calls = run("acl")
    assert r.returncode == 0
    assert calls == ["update", "install"]


def test_no_packages_is_a_usage_error(run):
    r, calls = run()
    assert r.returncode == 2
    assert calls == []


def test_install_failing_with_a_healthy_update_is_not_called_a_mirror_outage(run):
    r, calls = run("acl", install_rc=100, install_out="E: Unable to locate package acl")
    assert r.returncode == 1
    assert "not a mirror outage" in r.stderr
    assert calls == ["update", "install"], "no fallback retry on a healthy update"


def test_a_failed_update_alone_does_not_stop_the_step(run):
    r, calls = run("acl", update_rc=100)
    assert r.returncode == 0
    assert calls == ["update", "install"]


def test_unknown_package_names_the_package_not_the_network(run):
    r, calls = run(
        "no-such-pkg",
        update_rc=100,
        install_rc=100,
        install_out="E: Unable to locate package no-such-pkg",
    )
    assert r.returncode == 1
    assert calls == ["update", "install", "update", "install"], "the retry on the other mirror still happens"
    assert "package" in r.stderr and "no-such-pkg" in r.stderr
    assert "network" not in r.stderr
    assert "mirror" not in r.stderr.split("retrying on archive.ubuntu.com", 1)[1]


def test_name_resolution_failure_is_called_the_network(run):
    r, _ = run(
        "acl",
        update_rc=100,
        install_rc=100,
        install_out="Err:1 http://archive.ubuntu.com/ubuntu noble/main acl\n  Temporary failure resolving 'azure.archive.ubuntu.com'\nE: Failed to fetch",
    )
    assert r.returncode == 1
    tail = r.stderr.split("retrying on archive.ubuntu.com", 1)[1]
    assert "network" in tail
    assert "not at the code under test" in tail


def test_an_unclassifiable_failure_says_it_cannot_tell(run):
    r, _ = run(
        "acl",
        update_rc=100,
        install_rc=100,
        install_out="E: Sub-process /usr/bin/dpkg returned an error code (1)",
    )
    assert r.returncode == 1
    tail = r.stderr.split("retrying on archive.ubuntu.com", 1)[1]
    assert "network" not in tail
    assert "cannot tell" in tail


def test_an_install_that_hit_the_time_limit_says_so(run):
    r, _ = run("acl", update_rc=100, install_rc=124)
    assert r.returncode == 1
    tail = r.stderr.split("retrying on archive.ubuntu.com", 1)[1]
    assert "time limit" in tail
    assert "network" not in tail


def test_a_held_dpkg_lock_is_named(run):
    r, _ = run(
        "acl",
        update_rc=100,
        install_rc=100,
        install_out="E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 1234",
    )
    assert r.returncode == 1
    tail = r.stderr.split("retrying on archive.ubuntu.com", 1)[1]
    assert "lock" in tail
    assert "network" not in tail


def test_apt_output_still_reaches_the_log(run):
    r, _ = run("acl", update_rc=100, install_rc=100, install_out="E: Unable to locate package acl")
    assert "Unable to locate package acl" in (r.stdout + r.stderr)


def test_the_closing_line_does_not_claim_the_second_attempt_ran_on_the_other_mirror(run):
    """diogenes-20261112-021740: the sources edit may change nothing (a runner whose sources never named the Azure mirror; the stub
    sed here is exactly that), and the script only prints what the sources name afterwards, so the closing line says what was
    tried and observed, never that the retry ran on archive.ubuntu.com."""
    r, calls = run("acl", update_rc=100, install_rc=100, install_out="E: Unable to locate package acl")
    assert calls == ["update", "install", "update", "install"]
    tail = r.stderr.split("retrying on archive.ubuntu.com", 1)[1]
    assert "again on archive.ubuntu.com" not in tail
    assert "both failed" in tail and "sources edit" in tail
