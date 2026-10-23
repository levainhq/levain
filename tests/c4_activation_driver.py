"""CONTROL's ROUTE-SUBMIT killed mid-activation, for S1 3c-4's fault gates (`tests/test_routemaps.py`).

`python tests/c4_activation_driver.py <routes_dir> <sessions_dir> <request.json> <point>` builds a `Control` over the
route store `<routes_dir>` exactly as `control.serve` does, answers the one ROUTE-SUBMIT in `<request.json>` from the
`_levain_dispatch` peer through `Control.handle`, and SIGKILLs itself at the activation's fault point `<point>` (one of
`levain.slot.routemaps.FAULT_POINTS`). A point never reached prints the answer and exits 0."""
from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from levain.slot import control as C       # noqa: E402
from levain.slot import routemaps          # noqa: E402

OP_UID = 501
DISPATCH_UID = 7001


def main(argv: list) -> int:
    routes_dir, sessions_dir, request, point = argv
    if point not in routemaps.FAULT_POINTS:
        print(f"unknown fault point {point}", file=sys.stderr)
        return 2

    def fault(at: str) -> None:
        if at == point:
            os.kill(os.getpid(), signal.SIGKILL)

    routes = routemaps.ControlRoutes(Path(routes_dir), fault=fault)
    control = C.Control(C.StateStore(Path(sessions_dir)), OP_UID, dispatch_uid=DISPATCH_UID, routes=routes)
    control.start()
    with open(request) as f:
        req = json.load(f)
    print(json.dumps(control.handle(C.Conn(DISPATCH_UID), req)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
