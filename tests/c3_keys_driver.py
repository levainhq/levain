"""keys in its launcher form for S1 3c-3's attachment tests (`tests/c3_rig.py`).

`python tests/c3_keys_driver.py --runtime-root R --lib-root L --control C --test-single-uid` is
`tests/b2_keys_driver.py` (the same argv, the same plain-TCP upstream seam on 127.0.0.1:$B2_UPSTREAM_PORT) with the
attachment's clocks scaled down from the environment, each set on every `Keys` right after it is made:
$C3_UNCLAIMED_S (`unclaimed_s`, V23 from FRONT-ARM), $C3_RECONCILE_S (`reconcile_s`) and $C3_ATTACH_ANSWER_S
(`attach_answer_s`). Anything unset keeps keys' own value."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from levain.broker import keys as K   # noqa: E402
from tests import b2_keys_driver      # noqa: E402,F401 - its import points the upstream seam at the fake

_CLOCKS = {"C3_UNCLAIMED_S": "unclaimed_s", "C3_RECONCILE_S": "reconcile_s", "C3_ATTACH_ANSWER_S": "attach_answer_s"}
_real_init = K.Keys.__init__


def _init(self, *a, **kw):
    _real_init(self, *a, **kw)
    for env, attr in _CLOCKS.items():
        if os.environ.get(env):
            setattr(self, attr, float(os.environ[env]))


K.Keys.__init__ = _init

if __name__ == "__main__":
    raise SystemExit(K.main(sys.argv[1:]))
