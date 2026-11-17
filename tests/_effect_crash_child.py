"""A child process for tests/test_effect_attempts.py: runs ONE effect whose executor writes a marker file and then waits,
so the parent can kill the process mid-effect (a real SIGKILL, no cleanup runs). argv: worktree, cockpit root, journal
path, order id, order sha256, verb (restart_service | send_ack), marker path, ISO now."""
from __future__ import annotations

import sys
import time
from datetime import datetime

worktree, root, journal_path, order_id, sha, verb, marker, now_iso = sys.argv[1:9]
sys.path.insert(0, worktree)

from pathlib import Path  # noqa: E402

from levain.desk import effects as fx  # noqa: E402

fx.NIGHT_ARMING_FACTS = {"nd7_ruled": "np-00000007", "nd8_done": "np-00000008", "nd9_read": "np-00000009"}  # this child tests the crash, not the gate
from levain.desk.effect_attempts import Journal  # noqa: E402
from tests import test_effect_verbs as tv  # noqa: E402

now = datetime.fromisoformat(now_iso)
from levain.cockpit.consent import utc_stamp  # noqa: E402

tv.ARRIVED[0] = utc_stamp(now)
raw = tv.raw_mail()


def stall(*_a) -> int:
    with open(marker, "w") as fh:                   # the effect has started: the parent kills us here
        fh.write("in flight")
    time.sleep(120)
    return 0


class Transport:
    sender = "thirdmind@levain.example"

    def send(self, message) -> None:
        stall()


effects = fx.Effects(
    data_root=Path(root), clock=lambda: now, runner=stall, transport=Transport(),
    events=lambda eid: tv.monitor_event() if eid == tv.EVR else tv.event_for(raw),
    raw_source=lambda ref: raw, drafter=lambda eid: fx.Draft(tv.GOOD_DRAFT, "stand_in"), journal=Journal(journal_path))
if verb == "restart_service":
    effects.restart_service(order_id, "taoserv", "apache2", event_id=tv.EVR, expect_sha256=sha)
else:
    effects.send_ack(order_id, tv.EVT, expect_sha256=sha)
