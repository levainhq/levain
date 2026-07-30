"""K4a [6] — the SEAT SURFACE: what the unit file declares, and what the CLI refuses.

The seat's self-consolidate is the single most governance-relevant thing a Levain unit does — it is
the entity rewriting its own memory with nobody present. So it must be visible in the argv (an
auditor reading the plist can answer "does this modify its own identity while I sleep?") and the
flags around it must refuse contradictions rather than silently resolve them.
"""
from __future__ import annotations

import json
from pathlib import Path

from levain import daemon
from levain.cli import _print_bound_cadence_notes, _seat_consolidate_line, main


def _entity(tmp_path: Path) -> Path:
    d = tmp_path / "ent"
    (d / ".levain").mkdir(parents=True)
    (d / ".levain" / "config.json").write_text(json.dumps({"adapter": "openhands"}))
    return d


# ======================================================================================
# the argv — the auditable surface
# ======================================================================================

def test_the_seat_argv_declares_that_it_consolidates(tmp_path):
    spec = daemon.build_seat_spec(entity_path=tmp_path / "e", task="t", interval=3600)
    assert "--consolidate" in spec.argv
    # And it declares the BOUND on that consolidate, for the same reason `--max-seconds` is
    # emitted: "is this seat's memory-write bounded" must be answerable from the unit file.
    i = spec.argv.index("--consolidate-max-seconds")
    assert spec.argv[i + 1] == f"{daemon.DEFAULT_SEAT_CONSOLIDATE_MAX_SECONDS:g}"


def test_a_non_consolidating_seat_declares_nothing_rather_than_a_false_flag(tmp_path):
    spec = daemon.build_seat_spec(
        entity_path=tmp_path / "e", task="t", interval=3600, consolidate=False,
    )
    assert not [a for a in spec.argv if a.startswith("--consolidate")]


def test_the_threshold_reaches_the_argv_when_set(tmp_path):
    spec = daemon.build_seat_spec(
        entity_path=tmp_path / "e", task="t", interval=3600, consolidate_every=7,
    )
    i = spec.argv.index("--consolidate-every")
    assert spec.argv[i + 1] == "7"


def test_an_unbounded_consolidate_emits_no_bound_flag_rather_than_a_zero(tmp_path):
    """`--consolidate-max-seconds 0` would round-trip through the CLI as "explicitly unbounded",
    but emitting the literal 0 into the argv invites a reader to parse it as a bound of zero. The
    absence is the honest encoding, matching how `--max-seconds` handles the same case."""
    spec = daemon.build_seat_spec(
        entity_path=tmp_path / "e", task="t", interval=3600, consolidate_max_seconds=None,
    )
    assert "--consolidate-max-seconds" not in spec.argv
    assert "--consolidate" in spec.argv


def test_consolidate_flags_follow_the_unattended_declaration(tmp_path):
    """Both facts live in one argv and describe one posture: `--unattended` says the crystallization
    bound applies, `--consolidate` says a memory write happens at all. Neither is inferable from the
    other, so an auditor needs both."""
    spec = daemon.build_seat_spec(entity_path=tmp_path / "e", task="t", interval=3600)
    assert "--unattended" in spec.argv and "--consolidate" in spec.argv


# ======================================================================================
# the CLI refusals
# ======================================================================================

def test_run_refuses_a_consolidate_tuning_flag_without_consolidate(tmp_path, capsys):
    rc = main(["run", str(tmp_path), "--task", "x", "--consolidate-every", "5"])
    assert rc == 2
    assert "require --consolidate" in capsys.readouterr().err


def test_run_refuses_a_consolidate_bound_without_consolidate(tmp_path, capsys):
    rc = main(["run", str(tmp_path), "--task", "x", "--consolidate-max-seconds", "60"])
    assert rc == 2
    assert "require --consolidate" in capsys.readouterr().err


def test_install_seat_refuses_tuning_flags_that_contradict_no_consolidate(tmp_path, capsys):
    ent = _entity(tmp_path)
    rc = main([
        "daemon", "install-seat", "--path", str(ent), "--task", "x",
        "--no-consolidate", "--consolidate-every", "5",
    ])
    assert rc == 2
    assert "contradict --no-consolidate" in capsys.readouterr().err


def test_install_seat_refuses_a_zero_threshold(tmp_path, capsys):
    """A threshold of 0 would make every interval "due" against an empty store — a model call per
    interval to metabolize nothing, forever, in a log nobody watches."""
    ent = _entity(tmp_path)
    rc = main([
        "daemon", "install-seat", "--path", str(ent), "--task", "x", "--consolidate-every", "0",
    ])
    assert rc == 2
    assert ">= 1" in capsys.readouterr().err


def test_wrap_refuses_a_nonfinite_bound(tmp_path, capsys):
    """The shared validator reaches `wrap` too. `nan` is the nasty one: it defeats every comparison
    guard, so nothing arms while the surface still prints a bound."""
    rc = main(["wrap", str(tmp_path), "--max-seconds", "nan"])
    assert rc == 2
    assert "finite" in capsys.readouterr().err


# ======================================================================================
# the banner — it must answer the question it raises
# ======================================================================================

def test_the_consolidate_line_answers_for_both_states(tmp_path):
    on = _seat_consolidate_line(True, 7, 900.0)
    assert "every 7 episodes" in on and "900s" in on
    assert "may NEVER crystallize" in on
    off = _seat_consolidate_line(False, None, None)
    # Printed rather than omitted: an absent line reads as "no such feature", not "it is off".
    # ⚠ This assertion was originally `A and B or C`, which is `(A and B) or C` — and B named a
    # phrase the line does not contain, so the whole thing passed on C alone while proving nothing
    # about "OFF" being reported at all. Corrected to assert the two facts SEPARATELY. The claim was
    # fixed rather than the test loosened; same discipline as ⑥'s `nan`-instead-of-`inf` weak test.
    assert "OFF" in off
    assert "degrades" in off, "an off consolidate must say what it COSTS, not merely that it is off"


def test_the_cadence_warning_is_computed_against_the_SUM_of_both_bounds(capsys):
    """THE DEFECT THIS PREVENTS: checking only the turn bound prints a reassuring
    "1800s < 3600s, all good" for a seat whose real worst-case occupancy is 2700s. What holds the
    launchd label is the PROCESS — turn then consolidate — so the sum is the number that matters."""
    _print_bound_cadence_notes(
        1800.0, 2400, consolidate=True, consolidate_max_seconds=900.0,
    )
    out = capsys.readouterr().out
    assert "2700" in out
    assert "SKIP intervals" in out


def test_no_cadence_warning_when_the_sum_fits(capsys):
    _print_bound_cadence_notes(600.0, 3600, consolidate=True, consolidate_max_seconds=900.0)
    assert "SKIP intervals" not in capsys.readouterr().out


def test_a_bounded_turn_with_an_unbounded_consolidate_is_called_out(capsys):
    """The turn bound alone reads as safe, so the asymmetry has to be named — otherwise the seat
    looks bounded while owning an unbounded tail that can hang it exactly the same way."""
    _print_bound_cadence_notes(600.0, 3600, consolidate=True, consolidate_max_seconds=None)
    out = capsys.readouterr().out
    assert "TURN is bounded but the CONSOLIDATE is not" in out


def test_a_non_consolidating_seat_is_judged_on_the_turn_bound_alone(capsys):
    """Regression guard on the sum arithmetic: with the phase off there is no tail to add, and
    counting a consolidate bound that will never run would produce a false warning."""
    _print_bound_cadence_notes(600.0, 3600, consolidate=False, consolidate_max_seconds=900.0)
    out = capsys.readouterr().out
    assert "SKIP intervals" not in out
    assert "CONSOLIDATE is not" not in out
