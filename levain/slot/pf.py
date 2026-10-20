"""The macOS pf anchor ``levain`` (CONTRACT §8.3), as text plus the Steps that load and unload it.

tcp/udp only; ICMP is the profile's (§8.1). An egress slot may open tcp to its own front port and
nothing else; a verifier pair (driver and child) may open nothing. :func:`anchor_rules` is the ONE
function that renders the rules, so a rule the R-M1 run may add (an own-loopback allowance) has one
place to go.

The anchor only filters once the main ruleset references it, so setup also hooks ``/etc/pf.conf``
(two exact lines, added idempotently) and reloads it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from levain.firing.hands import Step, _ensure_line
from levain.slot import Refused
from levain.slot.ids import SlotId

ANCHOR = "levain"
ANCHOR_FILE = Path("/etc/pf.anchors/levain")
PF_CONF = Path("/etc/pf.conf")
PFCTL = "/sbin/pfctl"

#: The front port of egress slot n (CONTRACT §2.2, V9).
GWPORT_BASE = 47100

#: The two lines setup keeps in the main ruleset: the filter anchor, and where its rules load from.
PF_CONF_HOOK = (f'anchor "{ANCHOR}"', f'load anchor "{ANCHOR}" from "{ANCHOR_FILE}"')


def gwport(slot: SlotId) -> int:
    if slot.slot_class != "egress":
        raise Refused("bad-token", f"{slot.tag} has no front port")
    return GWPORT_BASE + slot.index


def anchor_rules(slots: Iterable[SlotId]) -> list[str]:
    """The anchor's rules, egress slots first, then verifier pairs, each in index order (§8.3)."""
    slots = sorted(set(slots))
    rules: list[str] = []
    for s in (x for x in slots if x.slot_class == "egress"):
        user = s.primary
        rules.append(f"pass out quick proto tcp from any to 127.0.0.1 port {gwport(s)} user {user}")
        rules.append(f"block out quick proto {{tcp udp}} user {user}")
    for s in (x for x in slots if x.slot_class == "none"):
        for user in s.accounts:   # the driver AND the child, which runs executor-written steps
            rules.append(f"block out quick proto {{tcp udp}} user {user}")
    return rules


def anchor_text(slots: Iterable[SlotId]) -> str:
    return ("# Levain session slots (CONTRACT §8.3). Written by the install helper; removed by its undo verb.\n"
            + "".join(f"{r}\n" for r in anchor_rules(slots)))


def load_steps(slots: Iterable[SlotId]) -> list[Step]:
    """Write the anchor file (checked with ``pfctl -n``), hook it into the main ruleset, reload the
    main ruleset (checked first), load the anchor's rules, and enable pf. Idempotent: setup and
    replenish run the same steps with the slots then configured."""
    slots = tuple(slots)
    steps = [
        Step(f"write the pf anchor {ANCHOR} (checked with pfctl -n)",
             write=(ANCHOR_FILE, anchor_text(slots), 0o644), validate=(PFCTL, "-n", "-a", ANCHOR, "-f")),
    ]
    steps += [Step(f"keep `{line}` in {PF_CONF}", call=lambda ln=line: _ensure_line(PF_CONF, ln, present=True))
              for line in PF_CONF_HOOK]
    steps += [
        Step(f"check {PF_CONF} parses", (PFCTL, "-n", "-f", str(PF_CONF))),
        Step(f"reload {PF_CONF}, which now references the anchor", (PFCTL, "-f", str(PF_CONF))),
        Step(f"load the anchor {ANCHOR}'s rules", (PFCTL, "-a", ANCHOR, "-f", str(ANCHOR_FILE))),
        Step("enable pf (a reference only; it stays enabled for whoever else enabled it)", (PFCTL, "-E")),
    ]
    return steps


def unload_steps() -> list[Step]:
    """Flush the anchor, take the hook out of the main ruleset, reload it, remove the anchor file.
    pf itself is left enabled: another user of pf may rely on it."""
    steps = [Step(f"flush the pf anchor {ANCHOR}", (PFCTL, "-a", ANCHOR, "-F", "all"))]
    steps += [Step(f"take `{line}` out of {PF_CONF}", call=lambda ln=line: _ensure_line(PF_CONF, ln, present=False))
              for line in reversed(PF_CONF_HOOK)]
    steps += [
        Step(f"check {PF_CONF} parses", (PFCTL, "-n", "-f", str(PF_CONF))),
        Step(f"reload {PF_CONF}", (PFCTL, "-f", str(PF_CONF))),
        Step(f"remove {ANCHOR_FILE}", ("/bin/rm", "-f", str(ANCHOR_FILE))),
    ]
    return steps


__all__ = ["ANCHOR", "ANCHOR_FILE", "GWPORT_BASE", "PF_CONF", "PF_CONF_HOOK", "anchor_rules", "anchor_text",
           "gwport", "load_steps", "unload_steps"]
