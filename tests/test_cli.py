"""Tests for the levain CLI dispatch (levain/cli.py)."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from levain.cli import main


def test_init_web_threads_packs_to_run_init_web():
    # `--web --pack` is now supported: the browser interview composes the pack
    # too (the mutual-exclusivity guard is gone). _cmd_init must thread the
    # repeatable --pack values through to run_init_web as `packs`.
    with mock.patch("levain.init_server.run_init_web", return_value=0) as m:
        rc = main([
            "init", "--web",
            "--pack", "./pressable-domain",
            "--pack", "./pressable-solutions-role",
        ])
    assert rc == 0
    kwargs = m.call_args.kwargs
    assert kwargs["packs"] == [Path("./pressable-domain"), Path("./pressable-solutions-role")]


def test_init_web_without_pack_passes_no_packs():
    # Base-only web onboarding (no --pack) threads `packs=None` — unchanged behavior.
    with mock.patch("levain.init_server.run_init_web", return_value=0) as m:
        rc = main(["init", "--web"])
    assert rc == 0
    assert m.call_args.kwargs["packs"] is None


def test_init_web_with_bad_pack_fails_clean(capsys: pytest.CaptureFixture[str]):
    # A pack that does not compose (here: a nonexistent dir with no pack.toml) must
    # fail clean BEFORE the server binds — run_init_web validates the composition
    # up-front and returns nonzero rather than 500-ing the first request.
    rc = main(["init", "--web", "--pack", "/tmp/levain-nonexistent-pack-xyz"])
    assert rc == 1
    assert "pack composition failed" in capsys.readouterr().err


# --- K4a: `levain daemon install-seat` (the scheduled governed seat) ---------------------------

def _seat_argv(**over):
    a = {"path": None, "task": "t", "interval": 3600, "label": "com.x.seat",
         "model": None, "max_iterations": None, "dry_run": False}
    a.update(over)
    return a


def test_install_seat_refuses_a_non_entity(tmp_path, capsys: pytest.CaptureFixture[str]):
    """Without this the schedule turns a one-time usage error into a PERMANENT silent one: the
    unit installs happily and then fails identically every interval, forever, into a log.

    select_provider is mocked NOT for speed but for BLAST RADIUS: this test asserts a guard, and
    a test that asserts a guard must never be able to perform the guarded act when the guard is
    absent. Unmocked, a broken/removed validation lets this call reach the REAL launchd and
    install a live user agent into ~/Library/LaunchAgents pointing at a pytest tmp dir — which is
    exactly what happened during a mutation run of this very test, leaving a loaded unit that
    would wake hourly to fail. A test's failure mode must not be "mutates the developer's machine"."""
    (tmp_path / "notanentity").mkdir()
    with mock.patch("levain.daemon.select_provider") as sel:
        rc = main(["daemon", "install-seat", "--path", str(tmp_path / "notanentity"),
                   "--task", "review"])
    assert rc == 2
    assert "not an initialized Levain entity" in capsys.readouterr().err
    sel.return_value.install.assert_not_called()


def test_install_seat_validates_before_touching_the_service_manager(tmp_path):
    # the entity check must run BEFORE any provider call — a bad path must never reach launchctl.
    (tmp_path / "nope").mkdir()
    with mock.patch("levain.daemon.select_provider") as sel:
        rc = main(["daemon", "install-seat", "--path", str(tmp_path / "nope"), "--task", "t"])
    assert rc == 2
    sel.return_value.install.assert_not_called()


def test_install_seat_maps_zero_iterations_to_unbounded(tmp_path):
    # 0 is the operator's explicit "use the SDK's own limit"; omitting the flag must keep our
    # FINITE default. Collapsing the two would silently unbound every seat that didn't ask.
    from levain import daemon

    seen = {}
    real_build = daemon.build_seat_spec        # bind BEFORE patching, else fake_build recurses

    def fake_build(**kw):
        seen.update(kw)
        return real_build(**kw)

    with mock.patch("levain.session.require_openhands_entity", return_value=None), \
         mock.patch("levain.daemon.select_provider"), \
         mock.patch("levain.daemon.build_seat_spec", side_effect=fake_build):
        main(["daemon", "install-seat", "--path", str(tmp_path), "--task", "t",
              "--max-iterations", "0", "--dry-run"])
        assert seen["max_iterations"] is None
        seen.clear()
        main(["daemon", "install-seat", "--path", str(tmp_path), "--task", "t", "--dry-run"])
        assert seen["max_iterations"] == daemon.DEFAULT_SEAT_MAX_ITERATIONS


def test_install_seat_dry_run_changes_nothing(tmp_path, capsys: pytest.CaptureFixture[str]):
    with mock.patch("levain.session.require_openhands_entity", return_value=None), \
         mock.patch("levain.daemon.select_provider") as sel:
        rc = main(["daemon", "install-seat", "--path", str(tmp_path), "--task", "t", "--dry-run"])
    assert rc == 0
    sel.return_value.install.assert_not_called()      # dry-run must never install
    sel.return_value.would_install.assert_called_once()
    assert "NOTHING is changed" in capsys.readouterr().out


def test_install_seat_rejects_an_incoherent_schedule(tmp_path, capsys: pytest.CaptureFixture[str]):
    with mock.patch("levain.session.require_openhands_entity", return_value=None), \
         mock.patch("levain.daemon.select_provider"):
        rc = main(["daemon", "install-seat", "--path", str(tmp_path), "--task", "t",
                   "--interval", "0"])
    assert rc == 2
    assert "positive number of seconds" in capsys.readouterr().err


def test_install_seat_names_BOTH_log_streams_as_the_fan_in_surface(tmp_path, capsys):
    """launchd sends stdout and stderr to SEPARATE files, and the gated-halt report — "HELD AT
    THE EFFERENT GATE" plus the pending-action list — goes to STDERR. Naming only the stdout path
    sends the operator to a file showing the entity's ACTIVITY but never the fact that it was
    STOPPED, which breaks the exact fan-in this command claims to provide. (codex L3 HIGH.)"""
    with mock.patch("levain.session.require_openhands_entity", return_value=None), \
         mock.patch("levain.daemon.select_provider") as sel:
        sel.return_value.install.return_value = "installed"
        rc = main(["daemon", "install-seat", "--path", str(tmp_path), "--task", "t"])
    out = capsys.readouterr().out
    assert rc == 0
    # BOTH streams named...
    assert ".log" in out and ".err" in out
    # ...and the stderr one identified as where the DECISIONS land, not just listed.
    err_line = next(ln for ln in out.splitlines() if ".err" in ln)
    assert "DECISION" in err_line.upper() and "fan-in" in err_line
    assert "GOVERNED, NOT AUTONOMOUS" in out


def test_install_seat_tells_the_truth_when_the_entity_is_UNGATED(tmp_path, capsys):
    """Never assert a governance property without checking it. An entity pinned
    efferent_gate:"ungated" runs efferent actions unattended with nothing halting them — claiming
    "every efferent action HALTS at the K3 gate" there is worse than silence, because it is the
    last thing the operator reads before they stop watching. (glm L3.)"""
    with mock.patch("levain.session.require_openhands_entity", return_value=None), \
         mock.patch("levain.daemon.select_provider") as sel, \
         mock.patch("levain.firing.confinement.load_confinement_config") as cfg:
        sel.return_value.install.return_value = "installed"
        cfg.return_value = mock.Mock(efferent_gate="ungated")
        rc = main(["daemon", "install-seat", "--path", str(tmp_path), "--task", "t"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "UNGATED SEAT" in out and "NOT GOVERNED" in out
    assert "GOVERNED, NOT AUTONOMOUS" not in out      # the false claim must be ABSENT


def test_install_seat_REFUSES_when_the_confinement_config_is_unreadable(tmp_path, capsys):
    """Two properties in one, and the second was a behaviour change at L3.

    (1) A config read failure must not fake EITHER posture — a false "governed" tells the operator
    to stop watching, a false "ungated" cries wolf. (2) It must also not INSTALL: the runtime loads
    the same config fail-closed, so the seat would refuse to start on every interval forever,
    turning one typo into recurring scheduled noise in a log nobody reads. Refuse at install, where
    a human is present to read the error (codex L3 LOW)."""
    with mock.patch("levain.session.require_openhands_entity", return_value=None), \
         mock.patch("levain.daemon.select_provider") as sel, \
         mock.patch("levain.firing.confinement.load_confinement_config",
                    side_effect=OSError("boom")):
        sel.return_value.install.return_value = "installed"
        rc = main(["daemon", "install-seat", "--path", str(tmp_path), "--task", "t"])
    cap = capsys.readouterr()
    assert rc == 2
    sel.return_value.install.assert_not_called()
    assert "cannot read this entity's confinement config" in cap.err
    assert "GOVERNED, NOT AUTONOMOUS" not in cap.out and "UNGATED SEAT" not in cap.out


def test_install_seat_does_not_swallow_a_programming_error_as_posture_unknown(tmp_path, capsys):
    """The posture handler was a bare `except Exception` and it caught a NameError from a
    mis-ordered reference in the same function, reporting it as "governance posture unknown" — a
    coding error wearing a config error's clothes, which would have made the next typo silently
    permanent. The handler is narrow now; an unexpected error must CRASH, not degrade."""
    with mock.patch("levain.session.require_openhands_entity", return_value=None), \
         mock.patch("levain.daemon.select_provider"), \
         mock.patch("levain.firing.confinement.load_confinement_config",
                    side_effect=TypeError("a bug, not a config problem")):
        with pytest.raises(TypeError):
            main(["daemon", "install-seat", "--path", str(tmp_path), "--task", "t"])


@pytest.mark.parametrize("bad", ["-1", "-5"])
def test_install_seat_rejects_a_negative_iteration_bound(tmp_path, capsys, bad: str):
    """A negative bound is a typo (`-1` for `1`) that serializes into the unit's argv and is only
    discovered as whatever the SDK does with it, once per interval, in a log nobody watches —
    silently defeating the finite default the time-bound rests on. Found INDEPENDENTLY by both L3
    lineages, which is why it is pinned rather than documented."""
    with mock.patch("levain.session.require_openhands_entity", return_value=None), \
         mock.patch("levain.daemon.select_provider"):
        rc = main(["daemon", "install-seat", "--path", str(tmp_path), "--task", "t",
                   "--max-iterations", bad])
    assert rc == 2
    assert "must be >= 0" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["-1", "-0.5"])
def test_install_seat_rejects_a_negative_wall_clock_bound(tmp_path, capsys, bad: str):
    """SAME GUARD, THE OTHER UNIT — `guard_scoped_by_symptom_misses_the_class`.

    The negative-bound hole was found on `--max-iterations`; the CLASS is "any numeric bound flag
    accepts nonsense and the nonsense becomes policy". Here it is worse than a bad step count:
    `TurnDeadline` treats a non-positive value as DISARMED, so `--max-seconds -1` would read to the
    operator as "bound it very tightly" and deliver "not bounded at all" — an unbounded seat wearing
    a bounded seat's command line."""
    with mock.patch("levain.session.require_openhands_entity", return_value=None), \
         mock.patch("levain.daemon.select_provider"):
        rc = main(["daemon", "install-seat", "--path", str(tmp_path), "--task", "t",
                   "--max-seconds", bad])
    assert rc == 2
    assert "must be >= 0" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["-1", "-0.5"])
def test_run_task_rejects_a_negative_wall_clock_bound(tmp_path, capsys, bad: str):
    """The same flag on the OTHER command. `levain run --task` is where a hand-driven operator meets
    it, and the seat is only one caller — validating at one door and not the other is the exact
    shape of the K2 defect where `init_server.py` never called the new validation."""
    rc = main(["run", str(tmp_path), "--task", "t", "--max-seconds", bad])
    assert rc == 2
    assert "must be >= 0" in capsys.readouterr().err


def test_run_task_normalizes_an_explicit_zero_bound_to_unbounded(tmp_path):
    """`0` is the operator's explicit "no wall-clock bound", and it must reach `run_task` as `None`.

    One representation of "no bound" past the boundary, because `TurnDeadline` already treats both as
    disarmed — and two spellings of one state is how a later `if max_seconds:` check starts
    disagreeing with an `if max_seconds is not None:` check."""
    seen: dict[str, object] = {}

    def capture(**kwargs):
        seen.update(kwargs)
        return 0

    with mock.patch("levain.run.run_task", side_effect=capture):
        rc = main(["run", str(tmp_path), "--task", "t", "--max-seconds", "0"])
    assert rc == 0
    assert seen["max_seconds"] is None


def test_run_task_passes_the_wall_clock_bound_through(tmp_path):
    """The CONTROL for the test above: a real bound must arrive intact, or normalizing zero would be
    indistinguishable from dropping the flag entirely."""
    seen: dict[str, object] = {}

    def capture(**kwargs):
        seen.update(kwargs)
        return 0

    with mock.patch("levain.run.run_task", side_effect=capture):
        rc = main(["run", str(tmp_path), "--task", "t", "--max-seconds", "42.5"])
    assert rc == 0
    assert seen["max_seconds"] == 42.5


# --- drift-lock: CLI defaults must equal the daemon module's constants ------------------------

def test_cli_daemon_defaults_match_the_daemon_module():
    """The daemon CLI defaults are LITERALS (the daemon module is imported lazily inside the
    command funcs so `levain --help` stays cheap). This pins those literals to the module
    constants, so a divergence fails the suite instead of silently shipping two different
    defaults — the drift this file's five hardcoded labels are otherwise open to."""
    from levain import daemon

    seen: dict[str, object] = {}

    def capture(args):
        seen.update(vars(args))
        return 0

    with mock.patch("levain.cli._cmd_daemon_install_seat", side_effect=capture):
        main(["daemon", "install-seat", "--task", "t"])
    assert seen["label"] == daemon.DEFAULT_SEAT_LABEL
    assert seen["interval"] == daemon.DEFAULT_SEAT_INTERVAL
    # `--max-seconds` / `--max-iterations` default to None AT THE PARSER and resolve to the module
    # constants inside the command, so what is drift-locked here is the sentinel, not the value —
    # a literal default would be a second copy of the number.
    assert seen["max_seconds"] is None
    assert seen["max_iterations"] is None

    seen.clear()
    with mock.patch("levain.cli._cmd_daemon_install", side_effect=capture):
        main(["daemon", "install"])
    assert seen["label"] == daemon.DEFAULT_LABEL


def test_run_refuses_unattended_without_task(tmp_path, capsys: pytest.CaptureFixture[str]):
    """`--unattended` is a security-relevant DECLARATION (no human in the loop → the standard cred
    stores join the floor). A REPL is definitionally attended, so honouring it there is impossible
    — and dropping it silently would leave the operator believing a posture they did not get."""
    rc = main(["run", str(tmp_path), "--unattended"])
    assert rc == 2
    assert "--unattended requires --task" in capsys.readouterr().err


@pytest.mark.parametrize("flag", [
    ["--consolidate"],
    ["--consolidate-every", "3"],
    ["--consolidate-max-seconds", "5"],
])
def test_run_refuses_consolidate_flags_without_task(flag, tmp_path, capsys):
    """The consolidate refusal lived INSIDE `if task is not None`, so on the REPL path these were
    SILENTLY IGNORED and the call fell straight through to run_entity — while its `--unattended`
    sibling directly above is deliberately OUTSIDE for exactly this reason.

    `--consolidate` is the flag that PERMITS THE ENTITY TO REWRITE ITS OWN MEMORY (daemon.py
    calls it "the single most governance-relevant fact about the unit"), so an operator believing
    they enabled or bounded it when they did not is the precise failure class this keystone
    exists to make impossible. The pre-existing rule was written for the two TUNING flags and
    missed the one that actually carries the authority.
    (Diogenes 2026-07-30 — guard_scoped_by_symptom_misses_the_class.)"""
    rc = main(["run", str(tmp_path), *flag])
    assert rc == 2, f"{flag} without --task must be REFUSED, not ignored"
    assert "require --task" in capsys.readouterr().err


def test_the_consolidate_tuning_refusal_still_fires_with_a_task(tmp_path, capsys):
    """Guard the other direction: hoisting a task-level refusal must not shadow the ORIGINAL
    rule, which makes a different claim — the tuning flags require `--consolidate` itself."""
    rc = main(["run", str(tmp_path), "--task", "hi", "--consolidate-max-seconds", "5"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "require --consolidate" in err
    assert "require --task" not in err, "with --task present, the task-level rule must not fire"


def test_install_seat_reports_the_RESOLVED_cred_floor(tmp_path, capsys):
    """The banner used to carry one RESOLVED line (the gate) directly above one STATIC line (the
    floor), answering the same operator question — and that asymmetry was the bug that let a
    ratified cred decision be true in the plan and absent from the run."""
    with mock.patch("levain.session.require_openhands_entity", return_value=None), \
         mock.patch("levain.daemon.select_provider") as sel, \
         mock.patch("levain.firing.confinement.load_confinement_config") as cfg:
        sel.return_value.install.return_value = "installed"
        cfg.return_value = mock.Mock(efferent_gate="auto", deny_standard_creds=None)
        rc = main(["daemon", "install-seat", "--path", str(tmp_path), "--task", "t"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "cred floor" in out and "DENIED" in out
    assert "the default for an unattended seat" in out


def test_install_seat_warns_LOUDLY_when_creds_are_opted_back_in(tmp_path, capsys):
    """An explicit false is legal and must keep working — but a seat that can read the operator's
    gh/aws credentials unattended is exactly what the default exists to prevent, so the override
    has to be VISIBLE at install rather than discoverable only by reading confinement.json."""
    with mock.patch("levain.session.require_openhands_entity", return_value=None), \
         mock.patch("levain.daemon.select_provider") as sel, \
         mock.patch("levain.firing.confinement.load_confinement_config") as cfg:
        sel.return_value.install.return_value = "installed"
        cfg.return_value = mock.Mock(efferent_gate="auto", deny_standard_creds=False)
        rc = main(["daemon", "install-seat", "--path", str(tmp_path), "--task", "t"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "READABLE by this" in out and "OVERRIDDEN" in out


# ---------- the bound/cadence notes must appear on BOTH seat surfaces (K4a ⑥) ----------

def _seat_dry_run(tmp_path, *extra: str):
    with mock.patch("levain.session.require_openhands_entity", return_value=None), \
         mock.patch("levain.daemon.select_provider") as sel, \
         mock.patch("levain.firing.confinement.load_confinement_config") as cfg:
        cfg.return_value = mock.Mock(efferent_gate="auto", deny_standard_creds=None)
        rc = main(["daemon", "install-seat", "--path", str(tmp_path), "--task", "t",
                   "--dry-run", *extra])
    return rc, sel


def test_dry_run_WARNS_about_an_unbounded_seat(tmp_path, capsys):
    """`--dry-run` is the surface whose whole purpose is *inspect before you commit*, so it is the
    one place this warning matters MOST — and the first version of this code printed it only after
    `provider.install()`, leaving the dry-run silently showing "wall-clock UNBOUNDED" while the real
    install warned loudly about the identical config.

    Two surfaces disagreeing about one fact, with the SILENT one being what a cautious operator
    reaches for. Found by running the dry-run and reading it, not by review."""
    rc, _ = _seat_dry_run(tmp_path, "--max-seconds", "0")
    out = capsys.readouterr().out
    assert rc == 0
    assert "NO WALL-CLOCK BOUND" in out
    assert "STOP RUNNING PERMANENTLY" in out
    assert "wall-clock UNBOUNDED" in out


def test_dry_run_NOTES_when_the_bound_outlives_the_cadence(tmp_path, capsys):
    """Not an error and not refused — "poll as often as possible, one at a time" is legitimate. But
    launchd coalesces per label, so the operator asked for one cadence and will get another, and a
    schedule that quietly means something else is how the original defect hid."""
    rc, _ = _seat_dry_run(tmp_path, "--interval", "600")
    out = capsys.readouterr().out
    assert rc == 0
    assert "coalesces per label" in out
    assert "SKIP intervals" in out


def test_dry_run_is_QUIET_when_the_bound_fits_the_cadence(tmp_path, capsys):
    """THE CONTROL. The default configuration must produce neither note, or the warnings become
    noise an operator learns to scroll past — which is how a real one gets missed."""
    rc, _ = _seat_dry_run(tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "NO WALL-CLOCK BOUND" not in out
    assert "SKIP intervals" not in out
    assert "1800s wall-clock" in out, "it must still REPORT the resolved bound"


def test_the_install_path_prints_the_same_notes_as_the_dry_run(tmp_path, capsys):
    """ONE function, TWO call sites — pinned, because the defect was precisely that the two surfaces
    carried different amounts of truth about the same config."""
    with mock.patch("levain.session.require_openhands_entity", return_value=None), \
         mock.patch("levain.daemon.select_provider") as sel, \
         mock.patch("levain.firing.confinement.load_confinement_config") as cfg:
        sel.return_value.install.return_value = "installed"
        cfg.return_value = mock.Mock(efferent_gate="auto", deny_standard_creds=None)
        rc = main(["daemon", "install-seat", "--path", str(tmp_path), "--task", "t",
                   "--max-seconds", "0"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "NO WALL-CLOCK BOUND" in out
    assert "wall-clock UNBOUNDED" in out


# ---------- non-finite bounds are refused at BOTH doors (codex L3, HIGH) ----------

@pytest.mark.parametrize("bad", ["nan", "inf", "NaN", "Infinity"])
def test_run_task_refuses_a_non_finite_wall_clock_bound(tmp_path, capsys, bad: str):
    """`argparse type=float` happily parses these, and `nan` then slips through EVERY comparison
    guard — `nan < 0` is False so a `>= 0` validator passes it, and `nan > 0` is False so the deadline
    arms NOTHING. The result is an UNBOUNDED run whose own banner prints "nans wall-clock".

    That is the worst failure shape this feature can have: not a crash, a run that looks time-bounded
    to the operator and is not. `inf` fails the other way — it reaches `signal.setitimer`, which
    raises OverflowError."""
    rc = main(["run", str(tmp_path), "--task", "t", "--max-seconds", bad])
    assert rc == 2
    assert "finite" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["nan", "inf"])
def test_install_seat_refuses_a_non_finite_wall_clock_bound(tmp_path, capsys, bad: str):
    """The SAME guard at the other door — and worse here, because a `nan` serializes into the PLIST as
    `--max-seconds nan`, so the seat reads as bounded to anyone auditing the unit file. The K2 defect
    was precisely an invariant only its strongest caller enforced."""
    with mock.patch("levain.session.require_openhands_entity", return_value=None), \
         mock.patch("levain.daemon.select_provider"):
        rc = main(["daemon", "install-seat", "--path", str(tmp_path), "--task", "t",
                   "--max-seconds", bad])
    assert rc == 2
    assert "finite" in capsys.readouterr().err


@pytest.mark.parametrize("cmd", [
    ["run", "ENT", "--task", "t", "--max-seconds", "-inf"],
    ["daemon", "install-seat", "--path", "ENT", "--task", "t", "--max-seconds", "-inf"],
])
def test_a_leading_dash_bound_is_refused_by_the_PARSER_before_our_guard(tmp_path, cmd):
    """`-inf` never reaches the non-finite guard, and that is worth pinning rather than assuming.

    argparse treats a value beginning with `-` as an OPTION, so `--max-seconds -inf` fails as
    "expected one argument" and exits 2 via SystemExit — a refusal, just an earlier and cruder one
    than our own message. Pinned in the exact shape it actually happens, because the tempting "fix"
    for the ugly error text is to teach the parser to accept negative-looking values, which would
    walk `-inf` straight past the guard the test above exists to enforce. Both paths must stay
    closed; only one of them is ours."""
    argv = [str(tmp_path) if a == "ENT" else a for a in cmd]
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2


def test_cli_adapter_choices_match_KNOWN_ADAPTERS():
    """⚠ THE PIN. `--adapter`'s argparse `choices=` is a hand-written DUPLICATE of
    `install.KNOWN_ADAPTERS`, and that duplication is deliberate — see the comment at the
    call site. Deriving it would import `levain.install` during parser construction,
    which runs on EVERY invocation including `--help`; measured, that import is 18.5 ms
    against a 20-30 ms `--help`, so deriving would roughly double the cost of the exact
    path cli.py's "lazy imports keep `levain --help` fast" docstring exists to protect.

    So the duplicate stays and this test is the invariant instead — paying the import at
    TEST time, where it is free. `structural_invariants_beat_discipline`: the copy is
    fine, an UNPINNED copy is not.

    What this catches: adding a fourth adapter to KNOWN_ADAPTERS without updating the
    CLI. `install.py:290` would accept it, but argparse would reject it first, with an
    error naming the wrong authority — the drift is invisible until an operator hits it.
    """
    import argparse
    from unittest import mock

    from levain.install import KNOWN_ADAPTERS

    captured: dict[str, object] = {}
    real_add_argument = argparse.ArgumentParser.add_argument

    def spy(self, *args, **kwargs):
        if args and args[0] == "--adapter" and "choices" in kwargs:
            captured["choices"] = list(kwargs["choices"])
        return real_add_argument(self, *args, **kwargs)

    with mock.patch.object(argparse.ArgumentParser, "add_argument", spy):
        with pytest.raises(SystemExit):
            main(["init", "--help"])

    assert "choices" in captured, "could not observe --adapter's choices"
    assert captured["choices"] == list(KNOWN_ADAPTERS), (
        f"cli.py --adapter choices {captured['choices']} have drifted from "
        f"install.KNOWN_ADAPTERS {list(KNOWN_ADAPTERS)} — update the CLI list, or "
        "delete choices= and let install.py:290 be the only gate"
    )


class TestMaxSecondsErrorNamesTheFlagYouTyped:
    """The shared bound validator used to name `--max-seconds` for every caller, including the
    ones validating `--consolidate-max-seconds`.

    ⛔ An operator who typed a bad `--consolidate-max-seconds` was told
    `levain run: --max-seconds must be >= 0` — a flag they never set — and sent to fix the wrong
    option. Being ONE validator is right (a second copy is a second place to forget the two edge
    cases an L3 lineage each found); saying the same thing about every caller was not.

    ⚠ There were TWO call sites, not one: `levain run` and `daemon install-seat` both route the
    consolidate bound through it. The grep found the sibling after the first was fixed — the
    sibling class, again.
    """

    def _stderr_of(self, **kw):
        import contextlib, io
        from levain.cli import _reject_bad_max_seconds
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert _reject_bad_max_seconds(-1.0, **kw) is True
        return err.getvalue()

    def test_default_still_names_max_seconds(self):
        assert "--max-seconds" in self._stderr_of(command="run")

    def test_consolidate_bound_names_the_consolidate_flag(self):
        out = self._stderr_of(command="run", flag="--consolidate-max-seconds")
        assert "--consolidate-max-seconds" in out
        assert "levain run: --max-seconds" not in out, "still naming the flag the operator did not type"

    def test_every_consolidate_call_site_passes_the_flag(self):
        """Source-level, because a future caller is exactly how this recurs."""
        import re
        from pathlib import Path
        import levain
        src = (Path(levain.__file__).resolve().parent / "cli.py").read_text(encoding="utf-8")
        offenders = [
            c.strip()[:80]
            for c in re.findall(r"_reject_bad_max_seconds\(\s*(?!value)(.*?)\)\s*:", src, re.S)
            if "consolidate" in c and "flag=" not in c
        ]
        assert not offenders, (
            "a consolidate bound is validated without naming its own flag, so the error will "
            f"say --max-seconds: {offenders}"
        )
