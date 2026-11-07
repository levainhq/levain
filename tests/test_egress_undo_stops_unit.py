"""The Linux undo stops the boundary's systemd unit before it deletes the unit file, file present or not.

1107-26 (Ubuntu 24.04 guest, tests/ci/isolation_e2e.sh): after the final ``--undo`` the unit stayed loaded and
active (exited) with no file on disk, because the unit is ``RemainAfterExit`` and ``disable`` alone stops nothing.
A re-run over that stranded state (file already gone) must stop it too, so the stop is not gated on the file.
"""
from levain.firing import hands

USER = "_levain_coyote_847f6b"


def _stop_and_rm():
    steps = hands.egress_undo_steps(USER)
    unit = hands.egress_unit_name(USER)
    stop = [s for s in steps if s.argv[1:] == ("stop", unit)]
    rm = [s for s in steps if s.argv[:1] == ("/bin/rm",) and str(hands.egress_unit_path(USER)) in s.argv]
    return steps, stop, rm


def test_undo_has_a_stop_not_gated_on_the_unit_file():
    steps, stop, rm = _stop_and_rm()
    assert len(stop) == 1 and rm
    assert stop[0].skip_if == ()          # runs whether or not the file exists
    assert stop[0].allow_fail             # an unknown (not loaded) unit is not a failure
    assert steps.index(stop[0]) < steps.index(rm[0])


def test_the_file_gated_disable_still_comes_before_the_file_goes():
    steps, _, rm = _stop_and_rm()
    unit = hands.egress_unit_name(USER)
    disable = next(s for s in steps if s.argv[1:2] == ("disable",) and s.argv[-1] == unit)
    assert steps.index(disable) < steps.index(rm[0])
