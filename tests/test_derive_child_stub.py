"""The stub factory the derive child's tests start it with (``--factory test_derive_child_stub:factory``). Not a test module:
it holds no test, only the real Cockpit the child builds, with providers small enough to say what each test needs.

The panels and what each is for:
  fast     rows with a declared ``valid_until``: the panel whose child answer must equal the in-process derive of the same panel
  partial  a read that skipped rows and carries a note and a source instant (a status other than ok, deterministically)
  count    a row whose text is how many times THIS process has read it: a warm child counts up, a fresh one starts over
  hang     reads until the flag file ``hang.flag`` under ``$DERIVE_CHILD_STUB_DIR`` is gone: a derive that does not come back
  boom     a provider that raises (the engine turns it into an error snapshot; the child must live on)
  explode  a panel whose ``derive_panel`` itself raises (an exception out of the derive, which no provider can make)
  chatty   a provider that writes to stdout (``print`` and a raw write to fd 1) while it reads
  pid      the child's pid and the engine file it imported, as a row
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from levain.cockpit.engine import Cockpit, ProviderSpec, Read, RowIn

VALID_UNTIL = "2099-01-01T00:00:00+00:00"


def _row(n: int, text: str = "") -> RowIn:
    return RowIn(f"r{n}", text or f"row {n}", {"at": "2026-10-01T00:00:00+00:00"}, stored={"id": f"r{n}"})


def _spec(pid: str, read, **kw) -> ProviderSpec:
    base = dict(id=pid, kind="triage-list", title=pid, priority="feed", read=read, order="time.desc",
                facets=frozenset({"at"}), version_fields=("id",))
    base.update(kw)
    return ProviderSpec(**base)


_calls = {"n": 0}


class StubCockpit(Cockpit):
    def derive_panel(self, panel_id):
        if panel_id == "explode":
            raise ValueError("the stub's derive_panel blew up")
        return super().derive_panel(panel_id)


def _hang(ctx):
    flag = Path(os.environ["DERIVE_CHILD_STUB_DIR"]) / "hang.flag"
    while flag.exists():
        time.sleep(0.05)
    return Read(rows=(_row(1, "released"),))


def _count(ctx):
    _calls["n"] += 1
    return Read(rows=(_row(1, f"call {_calls['n']}"),))


def _boom(ctx):
    raise RuntimeError("the stub provider raised")


def _chatty(ctx):
    print("a stray print from a provider")
    os.write(1, b"a raw write to fd 1\n")
    sys.stdout.write("a stray write to sys.stdout\n")
    return Read(rows=(_row(1, "chatty"),))


def _pid(ctx):
    # the process, and the engine file it imported: the child must run the install its parent runs
    from levain.cockpit import engine
    return Read(rows=(_row(1, f"{os.getpid()} {engine.__file__}"),))


def factory() -> Cockpit:
    ck = StubCockpit()
    ck.register(_spec("fast", lambda ctx: Read(rows=(_row(1), _row(2), _row(3)), valid_until=VALID_UNTIL)))
    ck.register(_spec("partial", lambda ctx: Read(rows=(_row(1), _row(2)), skipped=((2, "unreadable"),), note="two skipped",
                                                   as_of="2026-10-01T00:00:00+00:00")))
    ck.register(_spec("count", _count))
    ck.register(_spec("hang", _hang, timeout_s=120.0))
    ck.register(_spec("boom", _boom))
    ck.register(_spec("explode", lambda ctx: Read(rows=())))
    ck.register(_spec("chatty", _chatty))
    ck.register(_spec("pid", _pid))
    return ck


def failing_factory() -> Cockpit:
    raise RuntimeError("the stub factory cannot build")
