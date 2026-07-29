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


def test_install_seat_does_not_guess_when_the_gate_posture_is_unreadable(tmp_path, capsys):
    """A config read failure must not fake EITHER answer: a false "governed" tells the operator
    to stop watching, a false "ungated" cries wolf."""
    with mock.patch("levain.session.require_openhands_entity", return_value=None), \
         mock.patch("levain.daemon.select_provider") as sel, \
         mock.patch("levain.firing.confinement.load_confinement_config",
                    side_effect=OSError("boom")):
        sel.return_value.install.return_value = "installed"
        rc = main(["daemon", "install-seat", "--path", str(tmp_path), "--task", "t"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "GATE POSTURE UNKNOWN" in out
    assert "GOVERNED, NOT AUTONOMOUS" not in out and "UNGATED SEAT" not in out


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
