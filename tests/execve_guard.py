"""``os.execve`` owned by the test. levain calls ``os.execve`` directly, with no injectable seam (so the env scan in
test_launch.py sees every exec as a direct call), and a test reaches its fake through the module's ``os``:
``monkeypatch.setattr(wall.os, "execve", fake)``. Every test module whose tests run an exec path takes
:func:`refuse_real_execve` in an autouse fixture, so a test that forgot to patch can never exec over the pytest
process: the seams were also that safety net."""
from __future__ import annotations

import os

#: The real one, held at import (before any fixture patched it): a test whose forked child must really exec
#: through wall puts it back with ``monkeypatch.setattr(wall.os, "execve", REAL_EXECVE)``.
REAL_EXECVE = os.execve

#: The distinctive exit status of a guard hit in a forked child (see :func:`refuse_real_execve`). A test that forks
#: under the guard and reads the child's status treats this one as a failure.
GUARD_EXIT = 87

#: The fake kernel (``FakeOS``, ``StageFake``) the code under test last called through: their OsCalls mark it.
_active: list = []


def using(fake) -> None:
    _active[:] = [fake]


def pid_locked_real_execve(owner_pid: int):
    """The real ``os.execve`` for a test whose forked child must really exec, locked to the child: in ``owner_pid``
    (the pytest process) it raises AssertionError, so a regression that execs in the parent can never replace pytest."""
    def execve(path, argv, env):
        if os.getpid() == owner_pid:
            raise AssertionError("the real os.execve was called in the pytest process")
        return REAL_EXECVE(path, argv, env)
    return execve


def refuse_real_execve(monkeypatch, os_module, *, route: bool = False):
    """An autouse fixture's body (``yield from``): ``os_module.execve`` raises AssertionError until the test installs
    its own fake. With ``route``, a call goes to ``.execve`` of the fake kernel the code under test last called through
    (the stage reads its spec through it, exec_launch sets the spec fd inheritable through it, before either execs).
    In a process forked after the install the hit ends the child with ``os._exit(GUARD_EXIT)``.
    Teardown fails the test if the refusal was ever reached in the pytest process: stage_main catches Exception, so the raise alone could
    be swallowed into a STAGE_EXIT the test expected for another reason."""
    _active.clear()
    reached: list = []
    owner = os.getpid()

    def execve(path, argv, env):
        if route and _active:
            return _active[0].execve(path, argv, env)
        if os.getpid() != owner:
            # a forked child: ``reached`` is a copy the parent never sees, so leave by a status the parent can read
            os._exit(GUARD_EXIT)
        reached.append(path)
        raise AssertionError("os.execve not patched")
    monkeypatch.setattr(os_module, "execve", execve)
    yield
    _active.clear()
    assert reached == [], f"the real os.execve was reached unpatched: {reached}"
