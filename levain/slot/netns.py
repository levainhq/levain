"""Linux network namespaces per slot (CONTRACT §9.1): ``levain-e<n>`` per egress slot and
``levain-v<n>`` per verifier pair, ``lo`` only and up.

Namespaces do not survive a reboot, so the same Steps serve ``setup`` and the reaper's boot pass
(§1.3): :func:`netns_steps` is the list both run. Each step skips a namespace already present, and
``start`` refuses ``netns-missing`` rather than run in the host's (§1.3).
"""
from __future__ import annotations

from typing import Iterable

from levain.firing.hands import Step, _abs
from levain.slot.ids import SlotId

NETNS_DIR = "/run/netns"   # where `ip netns add` mounts a named namespace


def _ip() -> str:
    return _abs("ip")


def netns_steps(slots: Iterable[SlotId]) -> list[Step]:
    """Create each slot's namespace if absent, and bring its ``lo`` up (setup and the boot pass)."""
    ip, test = _ip(), _abs("test")
    steps: list[Step] = []
    for s in sorted(set(slots)):
        steps += [
            Step(f"create the network namespace {s.netns} (loopback only)", (ip, "netns", "add", s.netns),
                 skip_if=(test, "-e", f"{NETNS_DIR}/{s.netns}")),
            Step(f"bring up lo in {s.netns}", (ip, "-n", s.netns, "link", "set", "lo", "up")),
        ]
    return steps


def netns_undo_steps(slots: Iterable[SlotId]) -> list[Step]:
    """Delete each slot's namespace if present. Run after every slot process is stopped."""
    ip, test = _ip(), _abs("test")
    return [Step(f"delete the network namespace {s.netns}", (ip, "netns", "delete", s.netns),
                 skip_if=(test, "!", "-e", f"{NETNS_DIR}/{s.netns}"))
            for s in sorted(set(slots))]


__all__ = ["NETNS_DIR", "netns_steps", "netns_undo_steps"]
