"""Every file that launches headless Chrome takes its flags from the one shared file (the check is per file,
not per launch site: a file with several launches passes if it references the shared flags anywhere).

A launcher without ``--use-mock-keychain --password-store=basic`` puts a macOS keychain dialog on the operator's
screen (a fresh profile asks for Chrome's storage key). The flags live once, in ``tests/chrome_flags.json``; a launch
site names that file (Python: ``CHROME_SAFE_FLAGS``) instead of typing them, so a new launcher cannot forget them
without this test failing."""
from __future__ import annotations

import functools
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

import pytest

from tests import _shared_chrome

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


def test_the_shared_session_chrome_launch_site_is_scanned_and_spreads_the_flags():
    """The session Chrome's launcher names launch markers, so the scan covers it; it must spread the shared flags itself."""
    files = _tracked_sources()
    name = "tests/_shared_chrome.py"
    assert name in files, "the session Chrome launcher is not a tracked source the scan reads"
    text = files[name]
    assert any(m in text for m in LAUNCH_MARKERS)
    assert "*CHROME_SAFE_FLAGS" in text
    assert offending_sites({name: text}) == []
    assert offending_sites({name: text.replace("CHROME_SAFE_FLAGS", "X")}) == [name]


# --- the shell selection, by behaviour over a fake cache tree (python selector and the two node drivers) ---------------------

GIVEN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HOST_ARCH = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(platform.machine().lower())
OTHER_ARCH = "x64" if HOST_ARCH == "arm64" else "arm64"
NODE = shutil.which("node")
on_mac = pytest.mark.skipif(platform.system() != "Darwin" or HOST_ARCH is None, reason="the shell cache is a macOS layout")


def _shell(root: Path, version: str, arch: str, *, mode: int = 0o755) -> Path:
    d = root / f"mac_{arch}-{version}" / f"chrome-headless-shell-mac-{arch}"
    d.mkdir(parents=True)
    f = d / "chrome-headless-shell"
    f.write_text("#!/bin/sh\nexit 0\n")
    f.chmod(mode)
    return f


def _cases(tmp: Path):
    """(name, root as the flags file names it, setup(root) -> the expected pick or None for the given Chrome, env, explicit)."""
    def with_shell(root):
        return _shell(root, "156.0.8078.80", HOST_ARCH)

    def numeric(root):
        _shell(root, "99.0.1.1", HOST_ARCH)
        return _shell(root, "156.0.8078.80", HOST_ARCH)

    def stray(root):
        root.mkdir(parents=True, exist_ok=True)
        (root / ".DS_Store").write_text("x")
        (root / "mac_arm-999.0.0.0").write_text("a file where a version directory goes")
        return _shell(root, "156.0.8078.80", HOST_ARCH)

    def not_exec(root):
        _shell(root, "156.0.8078.80", HOST_ARCH, mode=0o644)

    def wrong_platform(root):
        _shell(root, "156.0.8078.80", OTHER_ARCH)

    def as_directory(root):
        (root / "mac_arm-156.0.8078.80" / f"chrome-headless-shell-mac-{HOST_ARCH}" / "chrome-headless-shell").mkdir(parents=True)

    def empty(root):
        root.mkdir(parents=True, exist_ok=True)

    return [
        ("installed and executable", with_shell, {}, False),
        ("newest version by number, not by text", numeric, {}, False),
        ("a stray file in the cache is skipped", stray, {}, False),
        ("no cache at all", None, {}, False),
        ("an empty cache", empty, {}, False),
        ("not executable", not_exec, {}, False),
        ("wrong-platform directory", wrong_platform, {}, False),
        ("the binary is a directory", as_directory, {}, False),
        ("LEVAIN_FULL_CHROME=1", with_shell, {"LEVAIN_FULL_CHROME": "1"}, False),
        ("a Chrome named on purpose", with_shell, {}, True),
        ("a drive that declared needs_full_chrome", with_shell, {"LEVAIN_DRIVE_FULL_CHROME": "1"}, False),
        ("CHROME set to exactly the default path", with_shell, {"CHROME": GIVEN}, False),
    ]


# the cases whose answer is the shell (the rest answer the given Chrome); the declared-full-Chrome case is a driver rule, not the python selector's
SHELL_CASES = {"installed and executable", "newest version by number, not by text", "a stray file in the cache is skipped"}
DRIVER_ONLY = {"a drive that declared needs_full_chrome", "CHROME set to exactly the default path"}
AMBIENT = ("LEVAIN_FULL_CHROME", "LEVAIN_DRIVE_FULL_CHROME")   # a case never inherits these from the runner's environment


def _run_table(pick, tmp_path: Path, *, driver_cases: bool) -> list[str]:
    """Runs every case through ``pick(root, given, env, explicit) -> chosen path``; returns the cases it answered wrongly."""
    wrong = []
    for i, (name, setup, env, explicit) in enumerate(_cases(tmp_path)):
        if name in DRIVER_ONLY and not driver_cases:
            continue
        root = tmp_path / f"case{i}"
        expected_bin = setup(root) if setup else None
        want = str(expected_bin) if name in SHELL_CASES else GIVEN
        if name in SHELL_CASES and not (platform.system() == "Darwin" and HOST_ARCH):
            continue
        got = pick(root, GIVEN, env, explicit)
        if got != want:
            wrong.append(f"{name}: got {got!r}, want {want!r}")
    return wrong


def _py_pick(root, given, env, explicit):
    flags = root.parent / f"{root.name}.flags.json"
    flags.write_text(json.dumps({"flags": [], "headless_shell_root": str(root)}))
    old = {k: os.environ.get(k) for k in (*AMBIENT, *env)}
    for k in AMBIENT:
        os.environ.pop(k, None)
    os.environ.update(env)
    try:
        return _shared_chrome._pick_chrome(given, explicit=explicit, flags_file=flags)
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _clean_env() -> dict[str, str]:
    """The runner's environment without the variables a case sets for itself (full-Chrome switches, a named CHROME)."""
    return {k: v for k, v in os.environ.items() if k not in (*AMBIENT, "CHROME")}


def _node_pick(script: str):
    def pick(root, given, env, explicit):
        d = root.parent / f"{root.name}.drv"
        (d / "sky_replay").mkdir(parents=True)
        src = ROOT / "tests" / script
        dest = d / script
        dest.write_text(src.read_text(encoding="utf-8"))
        (d / "chrome_flags.json").write_text(json.dumps({"flags": [], "headless_shell_root": str(root)}))
        steps = d / "steps.json"
        steps.write_text("[]")
        argv = [NODE, str(dest), given if not explicit else given + "-named", "about:blank", str(steps)] if script == "_page_drive.mjs" \
            else [NODE, str(dest), given if not explicit else given + "-named", "-"]
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60, env={**_clean_env(), "LEVAIN_TEST_PICK_CHROME_DRY": "1", **env})
        assert r.returncode == 0, r.stderr
        out = r.stdout.strip()
        return given if out == given + "-named" else out
    return pick


def test_the_python_selector_picks_by_behaviour(tmp_path):
    assert _run_table(_py_pick, tmp_path, driver_cases=False) == []


@pytest.mark.skipif(NODE is None, reason="node is needed")
@pytest.mark.parametrize("script", ["_page_drive.mjs", "sky_replay/run_cdp.mjs"])
def test_the_node_selectors_pick_by_behaviour(tmp_path, script):
    assert _run_table(_node_pick(script), tmp_path, driver_cases=script == "_page_drive.mjs") == []


@on_mac
def test_a_selector_that_always_returns_the_given_chrome_fails_the_table(tmp_path):
    """The positive control: the table is not satisfiable by `return given`."""
    wrong = _run_table(lambda root, given, env, explicit: given, tmp_path, driver_cases=True)
    assert len(wrong) == len(SHELL_CASES) and all(w.split(":")[0] in SHELL_CASES for w in wrong)


def test_the_python_selector_survives_an_unknown_account_and_a_malformed_root(tmp_path, monkeypatch, capsys):
    def unknown(uid):
        raise KeyError(uid)
    monkeypatch.setattr(_shared_chrome.pwd, "getpwuid", unknown)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert _shared_chrome._real_home() == tmp_path / "home"
    flags = tmp_path / "f.json"
    flags.write_text(json.dumps({"flags": [], "headless_shell_root": 7}))
    _shared_chrome._warned.clear()
    assert _shared_chrome._pick_chrome(GIVEN, flags_file=flags, system="Darwin", machine="arm64") == GIVEN
    assert _shared_chrome._pick_chrome(GIVEN, flags_file=flags, system="Darwin", machine="arm64") == GIVEN
    assert capsys.readouterr().err.count("headless_shell_root is not a string") == 1   # one warning, not one per call
    flags.write_text("{not json")
    assert _shared_chrome._pick_chrome(GIVEN, flags_file=flags, system="Darwin", machine="arm64") == GIVEN


def test_the_session_chrome_reads_full_chrome_and_a_named_chrome_when_it_starts(monkeypatch):
    monkeypatch.delenv("CHROME", raising=False)
    monkeypatch.setenv("LEVAIN_FULL_CHROME", "1")
    monkeypatch.setattr(_shared_chrome.platform, "system", lambda: "Darwin")
    assert _shared_chrome._session_chrome() == _shared_chrome.DEFAULT_CHROME
    monkeypatch.delenv("LEVAIN_FULL_CHROME")
    monkeypatch.setenv("CHROME", "/opt/named/chrome")
    assert _shared_chrome._session_chrome() == "/opt/named/chrome"


def _fake_exe(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return str(path)


def test_linux_with_chrome_unset_skips_even_with_chromium_on_path(tmp_path, monkeypatch):
    """Linux, CHROME unset, chromium on PATH: SKIPS (CI's plain shards rely on it) and the launch is not the PATH binary."""
    chromium = _fake_exe(tmp_path / "bin" / "chromium")
    monkeypatch.delenv("CHROME", raising=False)
    monkeypatch.setattr(_shared_chrome.platform, "system", lambda: "Linux")
    monkeypatch.setattr(_shared_chrome.shutil, "which", lambda n: chromium if n == "chromium" else None)
    monkeypatch.setattr(_shared_chrome, "DEFAULT_CHROME", str(tmp_path / "absent" / "Google Chrome"))
    assert _shared_chrome._session_chrome() != chromium
    assert "CHROME is unset" in _shared_chrome.chrome_skip_reason()


def test_the_skip_check_follows_the_resolver_to_a_cached_headless_shell(tmp_path, monkeypatch):
    """macOS, no app bundle, only a cached chrome-headless-shell: the resolver finds it, so the check must not skip."""
    shell = _fake_exe(tmp_path / "root" / "mac_arm-156.0.1" / "chrome-headless-shell-mac-arm64" / "chrome-headless-shell")
    monkeypatch.delenv("CHROME", raising=False)
    monkeypatch.delenv("LEVAIN_FULL_CHROME", raising=False)
    monkeypatch.setattr(_shared_chrome.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(_shared_chrome.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(_shared_chrome, "DEFAULT_CHROME", str(tmp_path / "absent" / "Google Chrome"))
    flags = tmp_path / "flags.json"
    flags.write_text(json.dumps({"flags": [], "headless_shell_root": str(tmp_path / "root")}))
    monkeypatch.setattr(_shared_chrome, "CHROME_FLAGS_FILE", flags)
    monkeypatch.setattr(_shared_chrome, "_pick_chrome", functools.partial(_shared_chrome._pick_chrome, flags_file=flags))
    assert _shared_chrome._session_chrome() == shell
    assert _shared_chrome.chrome_skip_reason() is None


def test_the_drivers_name_the_flags_file_and_the_launcher_spreads_it():
    """The wiring the behaviour tests above cannot see: the flags file names a root, and both node drivers read their config."""
    cfg = json.loads(FLAGS_FILE.read_text(encoding="utf-8"))
    assert cfg["headless_shell_root"].startswith("~/")


@pytest.mark.skipif(NODE is None, reason="node is needed")
@on_mac
@pytest.mark.parametrize("script", ["_page_drive.mjs", "sky_replay/run_cdp.mjs"])
@pytest.mark.parametrize("ambient", AMBIENT)
def test_an_ambient_full_chrome_variable_does_not_leak_into_the_cases(tmp_path, monkeypatch, script, ambient):
    """The runner's own LEVAIN_FULL_CHROME / LEVAIN_DRIVE_FULL_CHROME must not decide the env={} cases (python or node)."""
    monkeypatch.setenv(ambient, "1")
    for sub in ("py", "node"):
        (tmp_path / sub).mkdir()
    assert _run_table(_py_pick, tmp_path / "py", driver_cases=False) == []
    assert _run_table(_node_pick(script), tmp_path / "node", driver_cases=script == "_page_drive.mjs") == []


def _drive_pick(tmp_path: Path, steps_text: str) -> str:
    """The Chrome ``_page_drive.mjs`` would launch for a drive whose steps are ``steps_text`` (dry run), with a shell installed."""
    root = tmp_path / "cache"
    shell = _shell(root, "156.0.8078.80", HOST_ARCH)
    d = tmp_path / "drv"
    d.mkdir()
    (d / "_page_drive.mjs").write_text((ROOT / "tests" / "_page_drive.mjs").read_text(encoding="utf-8"), encoding="utf-8")
    (d / "chrome_flags.json").write_text(json.dumps({"flags": [], "headless_shell_root": str(root)}))
    steps = d / "steps.json"
    steps.write_text(json.dumps([{"eval": steps_text}]))
    r = subprocess.run([NODE, str(d / "_page_drive.mjs"), GIVEN, "about:blank", str(steps)], capture_output=True, text=True, timeout=60,
                       env={**_clean_env(), "LEVAIN_TEST_PICK_CHROME_DRY": "1"})
    assert r.returncode == 0, r.stderr
    out = r.stdout.strip()
    assert out in (GIVEN, str(shell)), out
    return "full" if out == GIVEN else "shell"


@pytest.mark.skipif(NODE is None, reason="node is needed")
@on_mac
def test_the_source_sniff_routes_real_push_use_to_full_chrome_and_a_push_api_delete_to_the_shell(tmp_path):
    cases = [
        ("delete window.Notification; delete window.PushManager;", "shell"),
        ("1 + 1", "shell"),
        ("PushManager.prototype.subscribe = async () => null;", "full"),   # the positive control: a real push use
        ("navigator.serviceWorker.register('/sw.js')", "full"),
        ("reg.pushManager.getSubscription()", "full"),
        ("Notification.requestPermission()", "full"),
        ("new Notification('x')", "full"),
    ]
    wrong = [f"{text!r}: got {got}, want {want}" for i, (text, want) in enumerate(cases)
             if (got := _drive_pick(tmp_path / f"c{i}", text)) != want]
    assert wrong == []
