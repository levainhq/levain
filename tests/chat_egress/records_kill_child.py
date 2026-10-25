"""A test egress service for R15-10-A8's `kill -9` run (`test_records.py`): run by path, never imported by pytest.

argv: <dispatch dir> <launch id> <completed request id> <cut request id>. It writes the first request whole (attempt,
allow, outcome), then the second request's attempt and allow, prints `ALLOW-SENT <id>` once that allow is "sent", and
waits for an outcome that never comes: the parent kills it with SIGKILL there.
"""

from __future__ import annotations

import sys
import threading

from levain.chat_egress import records as rec
from levain.chat_egress import wire
from levain.chat_egress.lifecycle import Effect, Event, RequestLifecycle
from levain.coordinator.store import CoordStore

ATTEMPT = rec.Attempt(host="example.com", method="POST", path="/upload", names=(), setup_record_id=None,
                      attach=rec.Attach.NONE, deciding_step="card", open_network=False)


def main() -> None:
    directory, launch_id, done, cut = sys.argv[1:5]
    records = rec.EgressRecords(CoordStore(directory))

    def perform_for(request_id: str, obs=None):
        def perform(eff, step):
            if eff is Effect.WRITE_ATTEMPT:
                records.write_attempt(launch_id, request_id, step.verdict, ATTEMPT)
            elif eff is Effect.SEND_ALLOW:
                sys.stdout.write(f"ALLOW-SENT {request_id}\n" if request_id == cut else "")
                sys.stdout.flush()
            elif eff is Effect.WRITE_OUTCOME:
                records.write_outcome(launch_id, obs)
        return perform

    first = RequestLifecycle()
    first.fire(Event.ALLOW, perform_for(done))
    first.fire(Event.RESPONSE, perform_for(done, wire.parse_obs_line(wire.obs_response_line(done, 201, None, "0"))))
    second = RequestLifecycle()
    second.fire(Event.ALLOW, perform_for(cut))
    threading.Event().wait()          # the outcome never comes: SIGKILL lands here


if __name__ == "__main__":
    main()
