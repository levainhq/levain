"""Linux network namespaces per slot (CONTRACT §9.1): ``levain-e<n>`` per egress slot and
``levain-v<n>`` per verifier pair, ``lo`` only and up.

Namespaces do not survive a reboot, so the same Steps serve ``setup`` and the reaper's boot pass
(§1.3): :func:`netns_steps` is the list both run. Each step skips a namespace already present, read
from ``ip netns list`` (the namespaces ``ip`` itself knows, not a path test), and ``start`` refuses
``netns-missing`` rather than run in the host's (§1.3).
"""
from __future__ import annotations

from typing import Iterable

from levain.firing.hands import Step, _abs
from levain.slot.ids import SlotId


def _ip() -> str:
    return _abs("ip")


def netns_present(name: str) -> tuple[str, ...]:
    """A ``skip_if`` that exits 0 when ``ip netns list`` names ``name`` (its lines read ``<name>`` or
    ``<name> (id: <k>)``)."""
    return ("/bin/sh", "-c", f'{_ip()} netns list | {_abs("cut")} -d" " -f1 | {_abs("grep")} -qxF -- "$1"',
            "sh", name)


def netns_absent(name: str) -> tuple[str, ...]:
    return ("/bin/sh", "-c", f'! {{ {_ip()} netns list | {_abs("cut")} -d" " -f1 | {_abs("grep")} -qxF -- "$1"; }}',
            "sh", name)


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
