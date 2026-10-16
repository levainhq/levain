"""Every file that launches headless Chrome takes its flags from the one shared file (the check is per file,
not per launch site: a file with several launches passes if it references the shared flags anywhere).

A launcher without ``--use-mock-keychain --password-store=basic`` puts a macOS keychain dialog on the operator's
screen (a fresh profile asks for Chrome's storage key). The flags live once, in ``tests/chrome_flags.json``; a launch
site names that file (Python: ``CHROME_SAFE_FLAGS``) instead of typing them, so a new launcher cannot forget them
without this test failing."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FLAGS_FILE = ROOT / "tests" / "chrome_flags.json"
LAUNCH_MARKERS = ("--headless", "--user-data-dir", "--remote-debugging-port", "--dump-dom")
SOURCE_SUFFIXES = {".py", ".mjs", ".js", ".sh", ".yml", ".yaml"}
# the file that defines the flags, and this guard (which names the markers to look for)
EXEMPT = {"tests/chrome_flags.json", "tests/_chrome_flags.py", "tests/test_chrome_launch_sites.py"}
REFERENCES = ("CHROME_SAFE_FLAGS", "chrome_flags.json")


def offending_sites(files: dict[str, str]) -> list[str]:
    """Paths whose text launches Chrome (names a launch marker) without taking the shared flags."""
    return sorted(p for p, text in files.items()
                  if p not in EXEMPT and any(m in text for m in LAUNCH_MARKERS) and not any(r in text for r in REFERENCES))


def _tracked_sources() -> dict[str, str]:
    names = subprocess.run(["git", "ls-files", "-z", "--", "tests", "levain", ".github", "scripts"], cwd=ROOT,
                           capture_output=True, text=True, check=True).stdout.split("\0")
    out = {}
    for n in filter(None, names):
        p = ROOT / n
        if p.suffix in SOURCE_SUFFIXES and p.is_file():
            out[n] = p.read_text(encoding="utf-8", errors="replace")
    return out


def test_the_shared_flags_hold_the_keychain_protection():
    flags = json.loads(FLAGS_FILE.read_text(encoding="utf-8"))["flags"]
    assert "--use-mock-keychain" in flags and "--password-store=basic" in flags and "--headless=new" in flags


def test_every_chrome_launch_site_takes_the_shared_flags():
    """Per file: each file that names a launch marker must also reference the shared flags."""
    files = _tracked_sources()
    sites = [p for p, t in files.items() if p not in EXEMPT and any(m in t for m in LAUNCH_MARKERS)]
    assert sites, "the scan found no launch site at all, so it is not looking where the launchers are"
    assert offending_sites(files) == []


def test_the_guard_flags_a_launch_site_that_types_its_own_flags():
    """The positive control: the same scan, over a site stripped of the shared-flags reference, fails."""
    good = 'run_driver([CHROME, *CHROME_SAFE_FLAGS, "--dump-dom", url])'
    stripped = 'run_driver([CHROME, "--headless=new", "--dump-dom", url])'
    mjs_stripped = "spawn(chrome, ['--headless=new', '--user-data-dir=x'])"
    assert offending_sites({"tests/a.py": good}) == []
    assert offending_sites({"tests/a.py": stripped, "tests/b.mjs": mjs_stripped}) == ["tests/a.py", "tests/b.mjs"]
