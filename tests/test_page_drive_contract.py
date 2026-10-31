"""The page driver's contract for failure and retry: a thrown eval fails the drive unless the step may error (a navigating eval
never fails it), an act-until step repeats its action with its assertion and only while the condition is false, and one
environment scale stretches every deadline."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests import _page_drive_run
from tests.test__waits import SCALE_TABLE
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
    steps = [{"eval": "setTimeout(() => { window.__late = 1; }, 3000); 1"}, {"until": "window.__late", "ms": 2500}]
    monkeypatch.delenv("LEVAIN_TEST_WAIT_SCALE", raising=False)
    r = _drive(tmp_path, steps)
    assert r.returncode == 1 and "until never came true within 2500ms" in r.stderr, (r.returncode, r.stderr)
    monkeypatch.setenv("LEVAIN_TEST_WAIT_SCALE", "2")
    r = _drive(tmp_path, steps, timeout=90)                              # the scaled page-load gate alone can take 30 s
    assert r.returncode == 0, r.stderr
    monkeypatch.setenv("LEVAIN_TEST_WAIT_SCALE", "0.1")                  # below 1 counts as 1
    r = _drive(tmp_path, steps, timeout=90)
    assert r.returncode == 1 and "within 2500ms" in r.stderr, (r.returncode, r.stderr)


@needs_browser
@pytest.mark.parametrize("raw,want", SCALE_TABLE)
def test_the_driver_parses_the_wait_scale_as_the_python_side_does(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str, want: float) -> None:
    monkeypatch.setenv("LEVAIN_TEST_WAIT_SCALE", raw)
    r = _drive(tmp_path, [{"until": "false", "ms": 100}], timeout=90)   # fails at once; the message carries the scaled deadline
    assert r.returncode == 1 and f"within {round(100 * want)}ms" in r.stderr, (raw, r.returncode, r.stderr)


@needs_browser
def test_an_eval_that_navigates_passes_and_any_other_eval_error_still_fails(tmp_path: Path) -> None:
    r = _drive(tmp_path, [{"eval": "location.href = 'data:text/html,<p>x</p>'; 1"}, {"eval": "2"}])
    assert r.returncode == 0, r.stderr
    values = json.loads(r.stdout)["values"]
    assert values[0] in (1, {"navigated": True}) and values[-1] == 2, values
    # an eval left waiting on a promise while the page reloads is the shape Chrome answers with a destroyed-context error
    r = _drive(tmp_path, [{"eval": "setTimeout(() => location.reload(), 50); new Promise(() => {})"}, {"eval": "3"}])
    assert r.returncode == 0, r.stderr
    values = json.loads(r.stdout)["values"]
    assert values[0] == {"navigated": True} and values[-1] == 3, values
    r = _drive(tmp_path, [{"eval": "null.x; 1"}])
    assert r.returncode == 1 and "eval failed" in r.stderr and "TypeError" in r.stderr, (r.returncode, r.stderr)


@needs_browser
def test_act_until_does_not_run_its_action_again_once_the_condition_came_true_between_polls(tmp_path: Path) -> None:
    # the first run makes the condition true 150 ms later; the retry tick (every 200) must see that before it acts again
    do = "window.__n = (window.__n || 0) + 1; if (window.__n === 1) setTimeout(() => { window.__ok = 1; }, 150); 1"
    r = _drive(tmp_path, [{"do": do, "until": "window.__ok", "ms": 10000, "every": 200}, {"eval": "new Promise(r => setTimeout(() => r(window.__n), 600))"}])
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["values"][-1] == 1, r.stdout
