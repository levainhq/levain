"""The page driver's contract for failure and retry: a thrown eval fails the drive unless the step may error, an act-until step
repeats its action with its assertion, and one environment scale stretches every deadline."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests import _page_drive_run
from tests.test_cockpit_flip_parity import CHROME, DRIVE, NODE, needs_browser


def _drive(tmp_path: Path, steps: list, timeout: float = 60):
    f = tmp_path / "steps.json"
    f.write_text(json.dumps(steps))
    return _page_drive_run.run([NODE, str(DRIVE), CHROME, "about:blank", str(f)], timeout=timeout)


@needs_browser
def test_an_eval_that_throws_fails_the_drive_and_names_the_step_and_the_error(tmp_path: Path) -> None:
    r = _drive(tmp_path, [{"eval": "1"}, {"eval": "(() => { throw new Error('boom-in-the-page'); })()"}])
    assert r.returncode == 1 and "step 1" in r.stderr and "boom-in-the-page" in r.stderr, (r.returncode, r.stderr)
    assert (tmp_path / "timeout-step-1.png").stat().st_size > 0 and (tmp_path / "timeout-step-1.txt").exists(), r.stderr
    r = _drive(tmp_path, [{"eval": "document.querySelector('#nope').click(); 1"}])      # a missing node: the common lost-event shape
    assert r.returncode == 1 and "step 0" in r.stderr and "eval failed" in r.stderr, (r.returncode, r.stderr)


@needs_browser
def test_may_error_collects_the_error_in_place_of_a_value(tmp_path: Path) -> None:
    r = _drive(tmp_path, [{"eval": "(() => { throw new Error('expected-throw'); })()", "may_error": True}, {"eval": "2"}])
    assert r.returncode == 0, r.stderr
    values = json.loads(r.stdout)["values"]
    assert "expected-throw" in values[0]["error"] and values[1] == 2, values


@needs_browser
def test_act_until_runs_its_action_again_while_the_condition_is_false(tmp_path: Path) -> None:
    # the action only takes effect on its second run, as a click does that a repaint ate the first time
    do = "window.__n = (window.__n || 0) + 1; if (window.__n >= 2) window.__ok = 1; 1"
    r = _drive(tmp_path, [{"do": do, "until": "window.__ok", "ms": 10000, "every": 300}, {"eval": "window.__n"}])
    assert r.returncode == 0, r.stderr
    values = json.loads(r.stdout)["values"]
    assert values[0] is True or values[0] == 1, values
    assert values[1] >= 2, values


@needs_browser
def test_act_until_posts_form_and_a_failure_names_the_last_error_of_the_action(tmp_path: Path) -> None:
    r = _drive(tmp_path, [{"do": "document.querySelector('#nope').click()", "posts": 1, "ms": 1200, "every": 300}])
    assert r.returncode == 1 and "step 0" in r.stderr and "posts never reached 1" in r.stderr, r.stderr
    assert "last error from the action" in r.stderr, r.stderr
    assert (tmp_path / "timeout-step-0.png").exists(), r.stderr
    r = _drive(tmp_path, [{"do": "window.__x = 1", "until": "false", "ms": 600, "every": 200, "may_timeout": True}])
    assert r.returncode == 0, r.stderr                                  # the opt-out is the same as `until`'s
    assert json.loads(r.stdout)["values"] == [{"timeout": "false"}]


@needs_browser
def test_the_wait_scale_multiplies_a_deadline_and_never_shortens_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    steps = [{"eval": "setTimeout(() => { window.__late = 1; }, 1500); 1"}, {"until": "window.__late", "ms": 1000}]
    monkeypatch.delenv("LEVAIN_TEST_WAIT_SCALE", raising=False)
    r = _drive(tmp_path, steps)
    assert r.returncode == 1 and "until never came true within 1000ms" in r.stderr, (r.returncode, r.stderr)
    monkeypatch.setenv("LEVAIN_TEST_WAIT_SCALE", "2")
    r = _drive(tmp_path, steps)
    assert r.returncode == 0, r.stderr
    monkeypatch.setenv("LEVAIN_TEST_WAIT_SCALE", "0.1")                  # below 1 counts as 1
    r = _drive(tmp_path, steps)
    assert r.returncode == 1 and "within 1000ms" in r.stderr, (r.returncode, r.stderr)
