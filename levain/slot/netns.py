"""Linux network namespaces per slot (CONTRACT §9.1): ``levain-e<n>`` per egress slot and
``levain-v<n>`` per verifier pair, ``lo`` only and up.

Namespaces do not survive a reboot, so the same Steps serve ``setup`` and the reaper's boot pass
(§1.3): :func:`netns_steps` is the list both run. Each step skips a namespace already present, read
from ``ip netns list`` (the namespaces ``ip`` itself knows, not a path test), and ``start`` refuses
``netns-missing`` rather than run in the host's (§1.3).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

from levain.firing.hands import Step, _abs
from levain.slot.ids import SlotId

if TYPE_CHECKING:
    from levain.slot.removal import Live


def _ip() -> str:
    return _abs("ip")


def _netns_listed(name: str, *, want: bool) -> tuple[str, ...]:
    # ``ip netns list`` is read on its own first: a pipe would report only grep's status, so a failing
    # ``ip`` would read as "not listed". Its failure exits 2, which is neither answer, so the step it
    # guards is not skipped and runs, failing loudly rather than hiding a namespace.
    neg = "" if want else "! "
    script = (f'out=$({_ip()} netns list) || exit 2; '
              f'{neg}printf "%s\\n" "$out" | {_abs("cut")} -d" " -f1 | {_abs("grep")} -qxF -- "$1"')
    return ("/bin/sh", "-c", script, "sh", name)


def netns_present(name: str) -> tuple[str, ...]:
    """A ``skip_if`` that exits 0 when ``ip netns list`` names ``name`` (its lines read ``<name>`` or
    ``<name> (id: <k>)``)."""
    return _netns_listed(name, want=True)


def netns_absent(name: str) -> tuple[str, ...]:
    return _netns_listed(name, want=False)


def netns_steps(slots: Iterable[SlotId]) -> list[Step]:
    """Create each slot's namespace if absent, and bring its ``lo`` up (setup and the boot pass)."""
    ip = _ip()
    steps: list[Step] = []
    for s in sorted(set(slots)):
        steps += [
            Step(f"create the network namespace {s.netns} (loopback only)", (ip, "netns", "add", s.netns),
                 skip_if=netns_present(s.netns)),
            Step(f"bring up lo in {s.netns}", (ip, "-n", s.netns, "link", "set", "lo", "up")),
        ]
    return steps


def netns_undo_steps(slots: Iterable[SlotId]) -> list[Step]:
    """Delete each slot's namespace if present. Run after every slot process is stopped."""
    ip = _ip()
    return [Step(f"delete the network namespace {s.netns}", (ip, "netns", "delete", s.netns),
                 skip_if=netns_absent(s.netns))
            for s in sorted(set(slots))]


__all__ = ["netns_absent", "netns_present", "netns_steps", "netns_undo_steps"]


def netns_state(name: str, live: "Live") -> str:
    """OURS: ``ip netns list`` names it (a namespace carries no owner; CONTRACT §9.1 reserves the names). A failing
    ``ip netns list`` raises: never read as absent."""
    from levain.slot.removal import ABSENT, OURS

    out = live.read((_ip(), "netns", "list"))
    return OURS if any(ln.split(" ", 1)[0] == name for ln in out.splitlines()) else ABSENT


def remove_netns_steps(name: str, *, live: "Live") -> list[Step]:
    """Row 33 R1b, ``netns/1`` (R8): ``ip netns delete`` the RECORDED namespace."""
    from levain.slot.removal import chain, point, remove_step

    ip = _ip()
    return [remove_step(f"delete the network namespace {name}", ("netns/1", name), live, lambda: netns_state(name, live),
                        chain(lambda: live.act((ip, "netns", "delete", name)), point(live, "netns/1:after-delete")),
                        tools=(ip,))]
