"""The macOS pf rules for the session slots (CONTRACT §8.3, §23, §24), as text plus the Steps that load
and unload them, and the per-slot lo0 aliases (§24).

tcp/udp only; ICMP is the profile's (§8.1). An egress slot may open tcp to its own front port and to
its own lo0 alias, and nothing else; its alias is closed to every other uid. A verifier pair (driver
and child) may open nothing. Rules name users by NUMERIC uid (§23). :func:`anchor_rules` is the ONE
function that renders the rules; :func:`alias_steps` is the ONE list of alias Steps, shared by setup,
replenish and the ``com.levain.pf`` boot one-shot (aliases do not survive a reboot).

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
from levain.slot.ids import SlotId, in_own_range, loopback_alias

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
    """The anchor's rules, egress slots first, then verifier pairs, each in index order (§8.3, §24).
    ``ids``: account name -> uid, for every account of ``slots``.

    The egress block is §24's measured rule set, in its measured order: every rule is ``quick``, so pf
    takes the FIRST match and a reordering changes what passes. Grouped by rule, each group in slot
    order:

    1. inbound guards, decided on the SYN (inbound ``user`` = the LISTENER's owner): a slot's listener
       is reached only on its own alias; never over inet6; and a listener on a slot's alias that is not
       the slot's own is refused (a squatter);
    2. ``pass in`` on lo0 to the slot's alias for its own uid, ``keep state``: the inbound half needs a
       state of its own, or the listener's SYN-ACK leg is dropped by the lo0 block in 4 (§24);
    3. outbound (``user`` = the sending socket): the slot passes to its front port and to its own
       alias, ``keep state``, and is blocked for everything else;
    4. every other uid is blocked to every slot alias on lo0.
    """
    slots = sorted(set(slots))
    egress = [(s, _uid(ids, s.primary), loopback_alias(s)) for s in slots if s.slot_class == "egress"]
    rules: list[str] = []
    if egress:
        uids = " ".join(str(u) for _, u, _ in egress)
        aliases = " ".join(a for _, _, a in egress)
        rules += [f"block in quick proto tcp from any to ! {a} user {u} flags S/SA" for _, u, a in egress]
        rules.append(f"block in quick inet6 proto tcp from any to any user {{ {uids} }} flags S/SA")
        rules += [f"block in quick on lo0 proto tcp from any to {a} user != {u} flags S/SA" for _, u, a in egress]
        rules += [f"pass in quick on lo0 proto tcp from any to {a} user {u} keep state" for _, u, a in egress]
        rules += [f"pass out quick on lo0 proto tcp from any to 127.0.0.1 port {gwport(s)} user {u} keep state"
                  for s, u, _ in egress]
        rules += [f"pass out quick on lo0 proto tcp from any to {a} user {u} keep state" for _, u, a in egress]
        rules += [f"block out quick proto {{ tcp udp }} user {u}" for _, u, _ in egress]
        rules.append(f"block out quick on lo0 proto {{ tcp udp }} from any to {{ {aliases} }}")
    for s in (x for x in slots if x.slot_class == "none"):
        for name in s.accounts:   # the driver AND the child, which runs executor-written steps
            rules.append(f"block out quick proto {{tcp udp}} user {_uid(ids, name)}")
    return rules


def anchor_text(slots: Iterable[SlotId], ids: Mapping[str, int]) -> str:
    return ("# Levain session slots (CONTRACT §8.3, §24). Written by the install helper; removed by its undo verb.\n"
            + "".join(f"{r}\n" for r in anchor_rules(slots, ids)))


# --- pf's enable reference --------------------------------------------------------------------------------

Run = Callable[..., "subprocess.CompletedProcess[str]"]


#: What pfctl prints when the reference a token names is already gone. "pf not enabled" is pfctl's text
#: for pf holding no reference at all, the state a reboot leaves when nothing else enabled pf; that -X
#: prints it there is reasoned, not run. A token pf forgot while something else keeps pf enabled prints
#: some other text and so is KEPT: a release that may not have happened is never recorded as done.
PF_TOKEN_GONE = ("pf not enabled",)


def _token_gone(r: "subprocess.CompletedProcess[str]") -> bool:
    text = f"{r.stderr or ''}\n{r.stdout or ''}".lower()
    return any(t in text for t in PF_TOKEN_GONE)


def _read_token(token_path: Path) -> tuple[str | None, str]:
    """(the stored token, or None; a note). A file that holds no token names nothing pf could release,
    so it is removed rather than left to wedge every later undo."""
    try:
        token = token_path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return None, ""
    except UnicodeDecodeError:
        token = ""
    if re.fullmatch(r"[0-9]{1,20}", token):
        return token, ""
    token_path.unlink(missing_ok=True)
    return None, f"{token_path} held no pf token; removed it (nothing to release)"


def _release(token: str, run: Run) -> tuple[bool, str]:
    r = run([PFCTL, "-X", token], capture_output=True, text=True)
    if r.returncode == 0:
        return True, ""
    if _token_gone(r):
        return True, f"pf no longer held token {token} ({(r.stderr or '').strip()})"
    return False, f"pfctl -X {token} failed, token kept: {(r.stderr or r.stdout or '').strip()}"


def _write_token(token_path: Path, token: str) -> None:
    """Root 0600, replaced in one rename after the bytes are on disk, so a crash leaves the old token
    or the new one, never a torn file."""
    tmp = token_path.with_name(f".{token_path.name}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, f"{token}\n".encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, token_path)
    dfd = os.open(token_path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def release_enable_reference(token_path: Path = PF_TOKEN, run: Run = subprocess.run) -> tuple[bool, str]:
    """Release the reference whose token :func:`take_enable_reference` stored, then remove the token
    file. No file, or a file holding no token, = nothing to release. The file goes only once pf
    confirms the reference is gone (released now, or already dropped); any other failure keeps it."""
    token, note = _read_token(token_path)
    if token is None:
        return True, note
    ok, why = _release(token, run)
    if ok:
        token_path.unlink(missing_ok=True)
    return ok, why


def take_enable_reference(token_path: Path = PF_TOKEN, run: Run = subprocess.run) -> tuple[bool, str]:
    """``pfctl -E`` and store its token, readable by root alone (§23). The new reference is taken BEFORE
    an earlier run's is released, so pf never sits with no Levain reference between the two. If the old
    one cannot be released, the new one is given back and the old token stays recorded: a re-run holds
    one reference, never two, and never one with no record."""
    old, note = _read_token(token_path)
    r = run([PFCTL, "-E"], capture_output=True, text=True)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "pfctl -E failed").strip()
    m = _TOKEN_RE.search(f"{r.stdout}\n{r.stderr}")
    if m is None:
        return False, "pfctl -E printed no token"
    new = m.group(1)
    if old is not None:
        ok, why = _release(old, run)
        if not ok:
            back, back_why = _release(new, run)
            return False, why + ("" if back else f"; and the new reference {new} could not be given back: {back_why}")
    _write_token(token_path, new)
    return True, note


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


#: Read by the alias ``skip_if`` s; ``/sbin/ifconfig lo0`` prints one ``inet <addr> netmask <mask>``
#: line per IPv4 address on lo0.
IFCONFIG = "/sbin/ifconfig"
_AWK = "/usr/bin/awk"


def _alias_test(alias: str, *, present: bool, ifconfig: str) -> tuple[str, ...]:
    # awk compares the address FIELD for equality (no regex, so a dot is not a wildcard and
    # 127.0.1.2 never matches 127.0.1.21); exit 0 = the alias is (present) / is not (absent) on lo0.
    want = "f" if present else "!f"
    script = f'$1 == "inet" && $2 == a {{ f = 1 }} END {{ exit !({want}) }}'
    return ("/bin/sh", "-c", f'{ifconfig} lo0 | {_AWK} -v a="$1" \'{script}\'', "sh", alias)


def alias_present(alias: str, ifconfig: str = IFCONFIG) -> tuple[str, ...]:
    """A ``skip_if`` that exits 0 when ``ifconfig lo0`` lists ``alias`` as an inet address."""
    return _alias_test(alias, present=True, ifconfig=ifconfig)


def alias_absent(alias: str, ifconfig: str = IFCONFIG) -> tuple[str, ...]:
    return _alias_test(alias, present=False, ifconfig=ifconfig)


def alias_steps(slots: Iterable[SlotId]) -> list[Step]:
    """Add each egress slot's lo0 alias (§24, V27) unless lo0 already has it; verifier pairs get none.
    The one list setup, replenish and the ``com.levain.pf`` boot one-shot run."""
    return [Step(f"add the lo0 alias {loopback_alias(s)} for {s.tag}",
                 (IFCONFIG, "lo0", "alias", loopback_alias(s), "netmask", "255.255.255.255"),
                 skip_if=alias_present(loopback_alias(s)))
            for s in sorted(set(slots)) if s.slot_class == "egress"]


def alias_undo_steps(slots: Iterable[SlotId]) -> list[Step]:
    """Remove each egress slot's lo0 alias if lo0 still has it. Run after every slot process is stopped."""
    return [Step(f"remove the lo0 alias {loopback_alias(s)} of {s.tag}", (IFCONFIG, "lo0", "-alias", loopback_alias(s)),
                 skip_if=alias_absent(loopback_alias(s)))
            for s in sorted(set(slots)) if s.slot_class == "egress"]


def unload_steps() -> list[Step]:
    """Flush the sub-anchor, release Levain's enable reference, remove both files."""
    return [
        Step(f"flush the pf anchor {ANCHOR}", (PFCTL, "-a", ANCHOR, "-F", "all")),
        Step(f"release pf's enable reference (the token in {PF_TOKEN})", call=lambda: release_enable_reference()),
        Step(f"remove {ANCHOR_FILE}", ("/bin/rm", "-f", str(ANCHOR_FILE))),
        Step(f"remove {PF_TOKEN}", ("/bin/rm", "-f", str(PF_TOKEN))),
    ]


__all__ = ["ANCHOR", "ANCHOR_FILE", "GWPORT_BASE", "IFCONFIG", "PFCTL", "PF_TOKEN", "PF_TOKEN_GONE", "alias_absent", "alias_present",
           "alias_steps", "alias_undo_steps", "anchor_rules", "anchor_text", "gwport", "load_steps",
           "release_enable_reference", "take_enable_reference", "unload_steps"]
