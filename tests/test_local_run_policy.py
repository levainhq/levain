"""tests/local_run_policy.py: the local-run policy (a bare full run is refused off CI, workers are capped, background QoS)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import local_run_policy as p

ROOT = Path(__file__).resolve().parent.parent
NO_ENV = {}
CI_ENV = {"GITHUB_ACTIONS": "true"}


def _config(file_or_dir=(), keyword="", markexpr="", collectonly=False, lf=False, maxprocesses=0, worker=False, info=None, cwd=ROOT):
    cfg = SimpleNamespace(
        known_args_namespace=SimpleNamespace(
            file_or_dir=list(file_or_dir), keyword=keyword, markexpr=markexpr, collectonly=collectonly, lf=lf, **({info: True} if info else {})),
        option=SimpleNamespace(maxprocesses=maxprocesses),
        invocation_params=SimpleNamespace(dir=cwd),
        rootpath=ROOT,
    )
    if worker:
        cfg.workerinput = {}
    return cfg


def test_a_bare_full_run_is_refused_and_a_targeted_one_is_not():
    assert p.refuse_bare_full(_config(), NO_ENV) == p.REFUSAL
    assert "scripts/ci_full.py" in p.REFUSAL and "LEVAIN_LOCAL_FULL=1" in p.REFUSAL
    for narrowed in (_config(["tests/test_x.py"]), _config(["tests/test_x.py::test_y"]), _config(keyword="foo"),
                     _config(markexpr="invariant"), _config(collectonly=True)):
        assert p.refuse_bare_full(narrowed, NO_ENV) is None


def test_the_whole_tests_directory_is_a_full_run_and_an_info_flag_is_not():
    for whole in (["tests"], ["tests/"], ["."], [str(ROOT)], [str(ROOT / "tests")], ["tests", "tests/test_x.py"]):
        assert p.refuse_bare_full(_config(whole), NO_ENV) == p.REFUSAL, whole
    assert p.refuse_bare_full(_config(["."], cwd=ROOT / "tests"), NO_ENV) == p.REFUSAL   # `.` from inside tests/
    assert p.refuse_bare_full(_config(["tests/ci"]), NO_ENV) is None                     # a directory below tests/ narrows
    for flag in ("help", "version", "markers", "showfixtures"):
        assert p.refuse_bare_full(_config(info=flag), NO_ENV) is None, flag


def test_lf_narrows_only_with_failures_to_rerun_and_a_tests_subdirectory_narrows(tmp_path):
    cfg = _config(lf=True)
    cfg.rootpath = tmp_path
    (tmp_path / "tests").mkdir()
    assert p.refuse_bare_full(cfg, NO_ENV) == p.REFUSAL                      # empty cache: --lf runs everything
    cache = tmp_path / ".pytest_cache" / "v" / "cache"
    cache.mkdir(parents=True)
    (cache / "lastfailed").write_text('{"tests/test_x.py::test_y": true}')
    assert p.refuse_bare_full(cfg, NO_ENV) is None
    cfg.known_args_namespace.cacheclear = True                                 # --lf --cache-clear empties the cache first
    assert p.refuse_bare_full(cfg, NO_ENV) == p.REFUSAL
    cfg.known_args_namespace.cacheclear = False
    (cache / "lastfailed").write_bytes(b"\xff\xfe")                            # unreadable cache state is not a narrowing
    assert p.refuse_bare_full(cfg, NO_ENV) == p.REFUSAL
    (cache / "lastfailed").write_text("garbage {")                              # not JSON: pytest reads it as empty too
    assert p.refuse_bare_full(cfg, NO_ENV) == p.REFUSAL
    (cache / "lastfailed").write_text("{}")
    assert p.refuse_bare_full(cfg, NO_ENV) == p.REFUSAL
    assert p.refuse_bare_full(_config(cwd=ROOT / "tests" / "ci"), NO_ENV) is None   # started inside a tests/ subdirectory
    assert p.refuse_bare_full(_config(cwd=ROOT / "tests"), NO_ENV) == p.REFUSAL


def test_local_full_one_overrides_the_refusal_and_nothing_else_does():
    assert p.refuse_bare_full(_config(), {"LEVAIN_LOCAL_FULL": "1"}) is None
    assert p.refuse_bare_full(_config(), {"LEVAIN_LOCAL_FULL": "yes"}) == p.REFUSAL
    assert p.refuse_bare_full(_config(worker=True), NO_ENV) is None


def test_ci_environment_bypasses_the_refusal_the_cap_and_the_qos():
    assert p.on_ci(CI_ENV) and p.on_ci({"CI": "true"}) and not p.on_ci(NO_ENV)
    assert not p.on_ci({"CI": "false"}) and not p.on_ci({"CI": "0"}) and not p.on_ci({"GITHUB_ACTIONS": "false"})
    assert p.refuse_bare_full(_config(), CI_ENV) is None
    cfg = _config(maxprocesses=64)
    p.cap_workers(cfg, CI_ENV)
    assert cfg.option.maxprocesses == 64
    assert p.background_qos(CI_ENV, "darwin") is False


def test_workers_are_capped_off_ci_and_a_lower_value_stays():
    for given, want in ((0, p.LOCAL_WORKERS_MAX), (64, p.LOCAL_WORKERS_MAX), (2, 2)):
        cfg = _config(maxprocesses=given)
        p.cap_workers(cfg, NO_ENV)
        assert cfg.option.maxprocesses == want
    cfg = _config(maxprocesses=0)
    p.cap_workers(cfg, {"LEVAIN_LOCAL_WORKERS_MAX": "6"})
    assert cfg.option.maxprocesses == 6


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin background QoS")
def test_background_qos_is_set_and_inherited_by_a_child():
    # run in a child of its own so this test process keeps its priority
    code = (
        "import ctypes, subprocess, sys\n"
        "from tests import local_run_policy as p\n"
        "assert p.background_qos({}, 'darwin') is True\n"
        "libc = ctypes.CDLL(None)\n"
        "mine = libc.getpriority(4, 0)\n"
        "child = subprocess.run([sys.executable, '-c', 'import ctypes; print(ctypes.CDLL(None).getpriority(4, 0))'],\n"
        "                       capture_output=True, text=True).stdout.strip()\n"
        "print(mine, child)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(p.__file__).rsplit("/tests/", 1)[0])
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["1", "1"]
