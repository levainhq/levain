"""Child driver for ``tests/test_slot_routes_trust_replace.py``: one ``routes-trust --replace`` against a scratch lib
root, KILLED (SIGKILL, nothing of the process's own cleanup runs) at one point of the replace.

``python -m tests._routes_replace_kill <L> <request file> <point>``; ``point`` is ``rename:<n>`` (before the replace's
n-th rename under ``L``), ``mkdir:<n>`` (before its n-th mkdir under ``L``) or ``written`` (after the new pair is
written and read back). The kill is the crash the intent marker is for; the parent then inspects what is left."""
from __future__ import annotations

import io
import os
import signal
import sys
from pathlib import Path

from levain.slot import install_helper as IH, runtime_helper as RH


def main() -> None:
    L, req, point = Path(sys.argv[1]), Path(sys.argv[2]).read_bytes(), sys.argv[3]
    kind, _, n = point.partition(":")
    count = {"rename": 0, "mkdir": 0}

    def kill() -> None:
        os.kill(os.getpid(), signal.SIGKILL)

    def counted(name, real):
        def act(src, *a, **kw):
            if str(src).startswith(str(L)):
                count[name] += 1
                if kind == name and count[name] == int(n):
                    kill()
            return real(src, *a, **kw)
        return act

    os.rename = counted("rename", os.rename)
    os.mkdir = counted("mkdir", os.mkdir)
    if kind == "written":
        real = IH._write_trust_pair

        def written(*a, **kw):
            real(*a, **kw)
            kill()
        IH._write_trust_pair = written

    class Lock:
        def __call__(self, host=None):
            class Cm:
                def __enter__(self):
                    return None

                def __exit__(self, *a):
                    return None
            return Cm()

    IH.run_routes_trust(os.getuid(), "darwin", read=io.BytesIO(req).read, lock=Lock(), replace=True,
                        store_seams=RH.Seams(host="darwin", store=L, trusted=lambda st: True))
    print("finished without the kill", flush=True)


if __name__ == "__main__":
    main()
