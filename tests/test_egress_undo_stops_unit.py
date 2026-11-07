"""The Linux undo stops the boundary's systemd unit before it deletes the unit file.

1107-26 (Ubuntu 24.04 guest, tests/ci/isolation_e2e.sh): after the final ``--undo`` the unit stayed loaded and
active (exited) with no file on disk, because the unit is ``RemainAfterExit`` and ``disable`` alone stops nothing.
"""
from levain.firing import hands


def test_undo_disable_stops_the_unit_before_the_file_goes():
    steps = hands.egress_undo_steps("_levain_coyote_847f6b")
    unit = hands.egress_unit_name("_levain_coyote_847f6b")
    names = [s.why for s in steps]
    disable = next(s for s in steps if s.argv[1:2] == ("disable",) and s.argv[-1] == unit)
    assert "--now" in disable.argv
    remove = next(s for s in steps if s.argv[:1] == ("/bin/rm",) and str(hands.egress_unit_path("_levain_coyote_847f6b")) in s.argv)
    assert steps.index(disable) < steps.index(remove), names
