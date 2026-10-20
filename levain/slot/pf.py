"""The macOS pf rules for the session slots (CONTRACT §8.3, §23), as text plus the Steps that load and
unload them.

tcp/udp only; ICMP is the profile's (§8.1). An egress slot may open tcp to its own front port and
nothing else; a verifier pair (driver and child) may open nothing. Rules name users by NUMERIC uid
(§23). :func:`anchor_rules` is the ONE function that renders the rules.

The rules load into the sub-anchor ``com.apple/levain``, which Apple's stock ``/etc/pf.conf`` already
evaluates through ``anchor "com.apple/*"`` (§23). Levain never edits ``/etc/pf.conf`` and never reloads
it: ``pfctl -f /etc/pf.conf`` flushes the main ruleset and every other service's dynamic anchors.
pf's enable reference is taken with ``pfctl -E`` and its token kept in :data:`PF_TOKEN`, so undo
releases exactly that reference (``pfctl -X``) and pf stays enabled for whoever else enabled it.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Iterable, Mapping

from levain.firing.hands import Step
from levain.slot import Refused
from levain.slot.ids import SlotId, in_own_range

ANCHOR = "com.apple/levain"
ANCHOR_FILE = Path("/etc/pf.anchors/levain")
PF_TOKEN = Path("/var/db/levain/pf.token")
PFCTL = "/sbin/pfctl"

#: The front port of egress slot n (CONTRACT §2.2, V9).
GWPORT_BASE = 47100

_TOKEN_RE = re.compile(r"Token\s*:\s*([0-9]{1,20})")


def gwport(slot: SlotId) -> int:
    if slot.slot_class != "egress":
        raise Refused("bad-token", f"{slot.tag} has no front port")
    return GWPORT_BASE + slot.index


def _uid(ids: Mapping[str, int], name: str) -> int:
    uid = ids.get(name)
    if not in_own_range("darwin", name, uid):
        raise Refused("bad-token", f"{name}: uid {uid!r} is not in its §1.1 range")
    assert isinstance(uid, int)
    return uid


def anchor_rules(slots: Iterable[SlotId], ids: Mapping[str, int]) -> list[str]:
    """The anchor's rules, egress slots first, then verifier pairs, each in index order (§8.3).
    ``ids``: account name -> uid, for every account of ``slots``."""
    slots = sorted(set(slots))
    rules: list[str] = []
    for s in (x for x in slots if x.slot_class == "egress"):
        uid = _uid(ids, s.primary)
        rules.append(f"pass out quick proto tcp from any to 127.0.0.1 port {gwport(s)} user {uid}")
        rules.append(f"block out quick proto {{tcp udp}} user {uid}")
    for s in (x for x in slots if x.slot_class == "none"):
        for name in s.accounts:   # the driver AND the child, which runs executor-written steps
            rules.append(f"block out quick proto {{tcp udp}} user {_uid(ids, name)}")
    return rules


def anchor_text(slots: Iterable[SlotId], ids: Mapping[str, int]) -> str:
    return ("# Levain session slots (CONTRACT §8.3). Written by the install helper; removed by its undo verb.\n"
            + "".join(f"{r}\n" for r in anchor_rules(slots, ids)))


# --- pf's enable reference --------------------------------------------------------------------------------

Run = Callable[..., "subprocess.CompletedProcess[str]"]


def release_enable_reference(token_path: Path = PF_TOKEN, run: Run = subprocess.run) -> tuple[bool, str]:
    """Release the reference whose token :func:`take_enable_reference` stored, then remove the token
    file. No file = nothing to release. A token pf no longer knows (a reboot dropped every reference)
    is not an error: the reference is gone either way."""
    try:
        token = token_path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return True, ""
    if not re.fullmatch(r"[0-9]{1,20}", token):
        return False, f"{token_path} does not hold a pf token"
    r = run([PFCTL, "-X", token], capture_output=True, text=True)
    token_path.unlink(missing_ok=True)
    return True, "" if r.returncode == 0 else f"pf no longer held token {token} ({(r.stderr or '').strip()})"


def take_enable_reference(token_path: Path = PF_TOKEN, run: Run = subprocess.run) -> tuple[bool, str]:
    """``pfctl -E`` and store its token, readable by root alone (§23). A token an earlier run stored is released first, so
    a re-run holds one reference, not two."""
    ok, why = release_enable_reference(token_path, run)
    if not ok:
        return ok, why
    r = run([PFCTL, "-E"], capture_output=True, text=True)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "pfctl -E failed").strip()
    m = _TOKEN_RE.search(f"{r.stdout}\n{r.stderr}")
    if m is None:
        return False, "pfctl -E printed no token"
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, f"{m.group(1)}\n".encode("ascii"))
    finally:
        os.close(fd)
    return True, ""


# --- steps ---------------------------------------------------------------------------------------------------


def load_steps(slots: Iterable[SlotId], ids: Mapping[str, int]) -> list[Step]:
    """Write the sub-anchor's file (checked with ``pfctl -n``), load it into ``com.apple/levain``, and
    take pf's enable reference. Idempotent: setup and replenish run the same steps with the slots then
    configured. Never touches ``/etc/pf.conf``."""
    slots = tuple(slots)
    return [
        Step(f"write the pf rules {ANCHOR_FILE} (checked with pfctl -n)",
             write=(ANCHOR_FILE, anchor_text(slots, ids), 0o644), validate=(PFCTL, "-n", "-a", ANCHOR, "-f")),
        Step(f"load the rules into the pf anchor {ANCHOR}", (PFCTL, "-a", ANCHOR, "-f", str(ANCHOR_FILE))),
        Step(f"create {PF_TOKEN.parent}", ("/bin/mkdir", "-p", "-m", "755", str(PF_TOKEN.parent))),
        Step(f"take pf's enable reference, its token kept in {PF_TOKEN}", call=lambda: take_enable_reference()),
    ]


def unload_steps() -> list[Step]:
    """Flush the sub-anchor, release Levain's enable reference, remove both files."""
    return [
        Step(f"flush the pf anchor {ANCHOR}", (PFCTL, "-a", ANCHOR, "-F", "all")),
        Step(f"release pf's enable reference (the token in {PF_TOKEN})", call=lambda: release_enable_reference()),
        Step(f"remove {ANCHOR_FILE}", ("/bin/rm", "-f", str(ANCHOR_FILE))),
        Step(f"remove {PF_TOKEN}", ("/bin/rm", "-f", str(PF_TOKEN))),
    ]


__all__ = ["ANCHOR", "ANCHOR_FILE", "GWPORT_BASE", "PFCTL", "PF_TOKEN", "anchor_rules", "anchor_text", "gwport",
           "load_steps", "release_enable_reference", "take_enable_reference", "unload_steps"]
