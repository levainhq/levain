"""The macOS pf rules for the session slots (CONTRACT §8.3, §23, §24), as text plus the Steps that load
and unload them, and the per-slot lo0 aliases (§24).

tcp/udp only; ICMP is the profile's (§8.1). An egress slot may open tcp to its own front port and to
its own lo0 alias, and nothing else; its alias is closed to every other uid. A verifier pair (driver
and child) may open nothing. Rules name users by NUMERIC uid (§23). :func:`anchor_rules` is the ONE
function that renders the rules; :func:`alias_steps` is the ONE list of alias Steps, shared by setup,
replenish and the ``com.levain.pf`` boot one-shot (:func:`boot_steps`: a reboot leaves pf disabled, the
anchor empty and no alias on lo0).

The rules load into the sub-anchor ``com.apple/levain``, which Apple's stock ``/etc/pf.conf`` already
evaluates through ``anchor "com.apple/*"`` (§23). Levain never edits ``/etc/pf.conf`` and never reloads
it: ``pfctl -f /etc/pf.conf`` replaces the main ruleset other services may have changed. Apple's own
``com.apple.pfctl`` job runs that reload at boot, in no order with the one-shot; a reload left the rules
loaded in ``com.apple/levain`` in place [measured in r3c10-vm 10-26, lane pfboot raw m1].
pf's enable reference is taken with ``pfctl -E`` and its token kept in :data:`PF_TOKEN`, so undo
releases exactly that reference (``pfctl -X``) and pf stays enabled for whoever else enabled it.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Mapping

from levain.firing.hands import _RETIRED_IDS as _HANDS_RETIRED_IDS, Step
from levain.launch import child_env
from levain.slot import Refused, write_all
from levain.slot.ids import RETIRED_IDS_PATH, SlotId, in_own_range, loopback_alias

if TYPE_CHECKING:
    from levain.slot.removal import Live

ANCHOR = "com.apple/levain"
ANCHOR_FILE = Path("/etc/pf.anchors/levain")
PF_TOKEN = Path("/var/db/levain/pf.token")
PFCTL = "/sbin/pfctl"
#: The optimizer flag every load and check of the anchor carries (see :func:`load_steps`).
PF_OPTIMIZE = ("-o", "none")

#: The front port of egress slot n (CONTRACT §2.2, V9).
GWPORT_BASE = 47100

_TOKEN_RE = re.compile(r"Token\s*:\s*([0-9]{1,20})")
#: The first line of every rules file :func:`anchor_text` writes starts with this (every version since S1:
#: ``git log -S`` on it in this file); :func:`anchor_state` reads the file as Levain's only then.
ANCHOR_HEADER = "# Levain session slots"
#: What :func:`_write_token` writes, exactly one line of it (the unstripped bytes): :func:`enable_state` reads a
#: recorded token path as Levain's only when it holds this.
_TOKEN_BYTES = re.compile(rb"[0-9]{1,20}\n?")
#: Levain's own one-id-per-line files inside ``pf-enable/1``'s bound: a single id there is a token's bytes, so these
#: paths are never a token (L3 r2 complement LOW 1).
_NOT_TOKENS = frozenset(RETIRED_IDS_PATH.values()) | frozenset(_HANDS_RETIRED_IDS.values())


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
    return (f"{ANCHOR_HEADER} (CONTRACT §8.3, §24). Written by the install helper; removed by its undo verb.\n"
            + "".join(f"{r}\n" for r in anchor_rules(slots, ids)))


# --- pf's enable reference --------------------------------------------------------------------------------

Run = Callable[..., "subprocess.CompletedProcess[str]"]


#: What pfctl -X prints (exit 1) when the reference a token names is already gone, both measured (1c VM
#: row C21): "pf not enabled" with pf holding no reference at all, the state a reboot leaves when nothing
#: else enabled pf; "pf: token invalid" for a token pf no longer knows while pf stays enabled. Any other
#: text KEEPS the token: a release that may not have happened is never recorded as done.
PF_TOKEN_GONE = ("pf not enabled", "pf: token invalid")
#: How :func:`take_enable_reference` starts a failure that leaves a Levain reference pf holds and no token names: pf
#: stays enabled, and a retry would only add another such reference.
ENABLE_UNCERTAIN = "pf enable state uncertain"


def _token_gone(r: "subprocess.CompletedProcess[str]") -> bool:
    text = f"{r.stderr or ''}\n{r.stdout or ''}".lower()
    return any(t in text for t in PF_TOKEN_GONE)


def _read_token(token_path: Path) -> tuple[bool, str | None, str]:
    """(whether the path could be read, the stored token or None, a note). A file that holds no token
    names nothing pf could release, so it is removed rather than left to wedge every later undo. A path
    that cannot be read (a directory, say) is reported unreadable and left as it is: nothing is
    unlinked that might not be the token file."""
    try:
        token = token_path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return True, None, ""
    except UnicodeDecodeError:
        token = ""
    except OSError as e:
        return False, None, f"cannot read the pf token {token_path}: {e.strerror or e}"
    if re.fullmatch(r"[0-9]{1,20}", token):
        return True, token, ""
    token_path.unlink(missing_ok=True)
    return True, None, f"{token_path} held no pf token; removed it (nothing to release)"


def _release(token: str, run: Run) -> tuple[bool, bool, str]:
    """(released or gone, gone: pf no longer held it, so releasing it took nothing pf held, a note)."""
    r = run([PFCTL, "-X", token], capture_output=True, text=True, env=child_env())
    if r.returncode == 0:
        return True, False, ""
    if _token_gone(r):
        return True, True, f"pf no longer held token {token} ({(r.stderr or '').strip()})"
    return False, False, f"pfctl -X {token} failed, token kept: {(r.stderr or r.stdout or '').strip()}"


def _write_token(token_path: Path, token: str) -> None:
    """Root 0600, replaced in one rename after the bytes are on disk, so a crash leaves the old token
    or the new one, never a torn file."""
    tmp = token_path.with_name(f".{token_path.name}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        write_all(fd, f"{token}\n".encode("ascii"))
        os.fsync(fd)
    except OSError:
        # The old token stays; a torn new one is never renamed over it.
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    os.close(fd)
    os.replace(tmp, token_path)
    dfd = os.open(token_path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def release_enable_reference(token_path: Path = PF_TOKEN, run: Run = subprocess.run, *,
                             after_release: Callable[[], None] = lambda: None) -> tuple[bool, str]:
    """Release the reference whose token :func:`take_enable_reference` stored, then remove the token
    file. No file, or a file holding no token, = nothing to release. The file goes only once pf
    confirms the reference is gone (released now, or already dropped); any other failure keeps it. A
    token path that cannot be read is a failure, and nothing is removed. ``after_release``: the crash
    table's ``pf-enable/1:after-release`` point (row 33 R1b), between the release and the unlink."""
    readable, token, note = _read_token(token_path)
    if not readable:
        return False, note
    if token is None:
        return True, note
    ok, _, why = _release(token, run)
    if ok:
        after_release()
        token_path.unlink(missing_ok=True)
    return ok, why


def take_enable_reference(token_path: Path = PF_TOKEN, run: Run = subprocess.run) -> tuple[bool, str]:
    """``pfctl -E`` and store its token, readable by root alone (§23). The new reference is taken BEFORE
    an earlier run's is released, so pf never sits with no Levain reference between the two. If the old
    one cannot be released, the new one is given back and the old token stays recorded, so a re-run that
    completes holds one recorded reference. A token write that fails with no old reference released gives the new one
    back too, so the boot one-shot's launchd retries never pile up references no token names. A recorded token pf
    answers gone (a reboot dropped it) released nothing pf held, so it counts as no old reference here.

    Once a LIVE old reference is released, the new one is NEVER given back: it may be all that holds pf on under running
    sessions (L3 verify r2 codex HIGH). A token write that fails then keeps it held, unrecorded, and answers
    :data:`ENABLE_UNCERTAIN`, as does a give-back that fails (the reference stays, named by no token). That answer is
    not retried (:func:`levain.slot.services.pf.main` exits 0 on it), since each retry would take one more reference no
    token names. The reference left unrecorded outlives an undo, which keeps pf enabled: the fail-closed side (known
    edge), and so is a crash between ``-E`` and the token write, or a ``-E`` whose output names no token (L3 r1
    codex MED 2: unparseable ``-E`` output needs a pfctl format change; it fails as an ordinary, retried step). A token
    path that cannot be read is refused before ``-E``."""
    readable, old, note = _read_token(token_path)
    if not readable:
        return False, note
    r = run([PFCTL, "-E"], capture_output=True, text=True, env=child_env())
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "pfctl -E failed").strip()
    m = _TOKEN_RE.search(f"{r.stdout}\n{r.stderr}")
    if m is None:
        return False, "pfctl -E printed no token"
    new = m.group(1)
    if old is not None:
        ok, gone, why = _release(old, run)
        if not ok:
            back, _, back_why = _release(new, run)
            if back:
                return False, why
            return False, f"{ENABLE_UNCERTAIN}: {why}; and the new reference {new} could not be given back: {back_why}"
        if gone:
            old = None      # it held nothing (a reboot dropped it): no live reference was released (L3 verify r3 P5)
    try:
        _write_token(token_path, new)
    except OSError as e:
        failed = f"cannot record the pf token {new} in {token_path}: {e.strerror or e}"
        if old is not None:
            return False, f"{ENABLE_UNCERTAIN}: {failed}; reference {new} kept (the old one {old} is released)"
        back, _, back_why = _release(new, run)
        if back:
            return False, f"{failed}; reference {new} given back"
        return False, f"{ENABLE_UNCERTAIN}: {failed}; and the new reference {new} could not be given back: {back_why}"
    return True, note


# --- steps ---------------------------------------------------------------------------------------------------


def load_steps(slots: Iterable[SlotId], ids: Mapping[str, int]) -> list[Step]:
    """Write the sub-anchor's file (checked with ``pfctl -n``), load it into ``com.apple/levain``, and
    take pf's enable reference. Idempotent: setup and replenish run the same steps with the slots then
    configured. Never touches ``/etc/pf.conf``.

    Both the check and the load run with the optimizer OFF (:data:`PF_OPTIMIZE`): the default ``-o basic``
    folds the per-user rules into ``<__automatic…>`` tables (runs/s2-macos raw/M01c_pf_slots: 8 egress slots,
    2 tables), and :func:`levain.slot.wall.check_egress_ready` refuses a folded anchor, so every slot would be
    refused at start. With ``-o none`` the live anchor prints the rules as written (raw/W12: 0 tables, 8
    READY), and the rules' order, which §24 measured (every rule is ``quick``), is kept as written."""
    slots = tuple(slots)
    return [
        Step(f"write the pf rules {ANCHOR_FILE} (checked with pfctl -n)",
             write=(ANCHOR_FILE, anchor_text(slots, ids), 0o644),
             validate=(PFCTL, "-n", *PF_OPTIMIZE, "-a", ANCHOR, "-f")),
        Step(f"load the rules into the pf anchor {ANCHOR}", _LOAD_ARGV),
        *_enable_steps(),
    ]


#: The ONE argv that loads the rules file into the anchor (setup, replenish, repair and the boot one-shot).
_LOAD_ARGV = (PFCTL, *PF_OPTIMIZE, "-a", ANCHOR, "-f", str(ANCHOR_FILE))


def _enable_steps() -> list[Step]:
    return [
        Step(f"create {PF_TOKEN.parent}", ("/bin/mkdir", "-p", "-m", "755", str(PF_TOKEN.parent))),
        Step(f"take pf's enable reference, its token kept in {PF_TOKEN}", call=lambda: take_enable_reference()),
    ]


def _is_rules_file(st: os.stat_result, head: bytes) -> str:
    """Why the file is not one :func:`anchor_text` wrote and root alone can change, or ``""``."""
    if not stat.S_ISREG(st.st_mode):
        return "is not a regular file"
    if st.st_uid != 0 or st.st_mode & 0o022:
        return f"is not root's file, writable by root alone ({st.st_uid}, {stat.S_IMODE(st.st_mode):04o})"
    if not head.startswith(ANCHOR_HEADER.encode()):
        return f"does not start with {ANCHOR_HEADER!r}"
    return ""


def load_recorded_anchor(path: Path = ANCHOR_FILE, run: Run = subprocess.run) -> tuple[bool, str]:
    """Load the rules file setup, replenish or repair last wrote, as it is, into :data:`ANCHOR` (the boot one-shot).
    It is never rewritten here. A file that is not Levain's (:func:`_is_rules_file`) is refused and nothing is loaded.
    The file's parent, ``/etc/pf.anchors``, is root's (root:wheel 0755, measured on the build Mac 10-26), so no
    other user can swap the file between the check and ``pfctl``'s own open. Known edges (L3 verify r2, not built: each
    needs root or a changed ``/etc/pf.anchors``): the check reads the open descriptor and ``pfctl`` reopens the path, so
    a parent made writable by another user (mode or ACL) could swap the file between them; and the header is read before
    the regular-file check, so a FIFO there fails as an ordinary, retried step rather than a clean refusal."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as e:
        return False, f"cannot open the pf rules {path}: {e.strerror or e}"
    try:
        why = _is_rules_file(os.fstat(fd), os.read(fd, len(ANCHOR_HEADER)))
    finally:
        os.close(fd)
    if why:
        return False, f"{path} {why}: nothing loaded"
    argv = _LOAD_ARGV if path == ANCHOR_FILE else (*_LOAD_ARGV[:-1], str(path))
    r = run(list(argv), capture_output=True, text=True, env=child_env())
    if r.returncode != 0:
        return False, f"pfctl could not load {path}: {(r.stderr or r.stdout or '').strip()}"
    return True, ""


def boot_steps(slots: Iterable[SlotId]) -> list[Step]:
    """What a reboot undoes, redone (the ``com.levain.pf`` one-shot), in this order: each egress slot's lo0 alias
    (:func:`alias_steps`), pf's enable reference taken again (a reboot drops every reference; the token recorded before
    it names one pf no longer knows, which :func:`take_enable_reference` releases as gone), and last the anchor loaded
    from its recorded rules file (:func:`load_recorded_anchor`). A start's readiness check
    (:func:`levain.slot.wall.macos_egress_ready`) reads all three; with the load last, a run that stops before it
    leaves the anchor empty and every egress start refused, never the rules and aliases in place with pf off (L3 r1
    codex HIGH)."""
    return [*alias_steps(slots),
            *_enable_steps(),
            Step(f"load the recorded rules {ANCHOR_FILE} into the pf anchor {ANCHOR}",
                 call=lambda: load_recorded_anchor())]


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


__all__ = ["ANCHOR", "ANCHOR_FILE", "GWPORT_BASE", "IFCONFIG", "PFCTL", "PF_OPTIMIZE", "PF_TOKEN", "PF_TOKEN_GONE", "alias_absent", "alias_present",
           "alias_steps", "anchor_rules", "anchor_text", "boot_steps", "gwport", "load_recorded_anchor", "load_steps",
           "release_enable_reference", "take_enable_reference"]


# --- row 33 R1b: removal by the RECORDED identity (§1.1 table rows 6-8) --------------------------------------------


def _lo0_has(addr: str, live: "Live") -> bool:
    """The three-state alias probe (DESIGN X4-M1): ``ifconfig lo0``'s own exit status is checked BEFORE parsing, so a
    failed read raises (the step fails) and only a zero exit that does not list ``addr`` reads as absent."""
    out = live.read((IFCONFIG, "lo0"))
    return any(ln.split()[:2] == ["inet", addr] for ln in out.splitlines())


def alias_state(addr: str, live: "Live") -> str:
    from levain.slot.removal import ABSENT, OURS

    return OURS if _lo0_has(addr, live) else ABSENT   # an address carries no owner: the bound is the ownership


def remove_alias_steps(addr: str, *, live: "Live") -> list[Step]:
    """``pf-alias/1`` (R8, before every ``pf-anchor/1``): ``ifconfig lo0 -alias <addr>``."""
    from levain.slot.removal import chain, point, remove_step

    return [remove_step(f"remove the lo0 alias {addr}", ("pf-alias/1", addr), live, lambda: alias_state(addr, live),
                        chain(lambda: live.act((IFCONFIG, "lo0", "-alias", addr)), point(live, "pf-alias/1:after-remove")),
                        tools=(IFCONFIG,))]


def anchor_state(anchor: str, file: str, live: "Live") -> str:
    """OURS: the rules file is a regular root file whose first line starts with :data:`ANCHOR_HEADER`, or it is gone
    and the anchor still holds rules. ABSENT: no file and ``pfctl -a <anchor> -sr`` prints no rule. NOT_OURS: any other
    file at the recorded path (nothing is flushed or removed)."""
    from levain.slot.removal import ABSENT, OURS, root_file

    state = root_file(live, Path(file), check=lambda b: b.split(b"\n", 1)[0].startswith(ANCHOR_HEADER.encode()))
    if state != ABSENT:
        return state
    return OURS if live.read((PFCTL, "-a", anchor, "-sr")).strip() else ABSENT


def remove_anchor_steps(anchor: str, file: str, *, live: "Live") -> list[Step]:
    """``pf-anchor/1`` (R8): flush the recorded anchor, then ``rm -f`` its rules file. Never pf's enable reference
    (that is ``pf-enable/1``'s): an anchor rename must not release the reference this run just took. A non-zero
    ``pfctl -F all`` fails the step (whether it exits 0 on an empty anchor is the crash table's to measure)."""
    from levain.slot.removal import at, chain, point, remove_step

    record = ("pf-anchor/1", (anchor, file))
    state = lambda: anchor_state(anchor, file, live)   # noqa: E731
    return [
        remove_step(f"flush the pf anchor {anchor}", record, live, state,
                    chain(lambda: live.act((PFCTL, "-a", anchor, "-F", "all")), point(live, "pf-anchor/1:after-flush")),
                    tools=(PFCTL,)),
        remove_step(f"remove {file}", record, live, state, lambda: live.act(("/bin/rm", "-f", at(live, Path(file)))),
                    tools=(PFCTL, "/bin/rm")),
    ]


def enable_state(token: str, live: "Live") -> str:
    """OURS: a regular root file holding a pf token (:data:`_TOKEN_BYTES`). Anything else at the recorded path is
    NOT_OURS and left: the bound admits every name under ``/var/db/levain``, the retired-ids file's included, and
    :func:`release_enable_reference` removes a file that holds no token. One of Levain's id files
    (:data:`_NOT_TOKENS`) is never a token, whatever it holds."""
    from levain.slot.removal import ABSENT, NOT_OURS, root_file

    state = root_file(live, Path(token), check=lambda b: _TOKEN_BYTES.fullmatch(b) is not None)
    return NOT_OURS if state != ABSENT and Path(token) in _NOT_TOKENS else state


def remove_enable_steps(token: str, *, live: "Live") -> list[Step]:
    """``pf-enable/1`` (R8, after every ``pf-anchor/1``): :func:`release_enable_reference` on the recorded token
    path, which removes the file only once pf confirms the reference gone (a file holding no token names nothing pf
    could release, and :func:`_read_token` removes it). ``pfctl -X`` runs bounded by ``live.timeout``."""
    from levain.slot.removal import remove_step

    def release() -> tuple[bool, str]:
        try:
            return release_enable_reference(live.at(Path(token)), run=live.timed,
                                            after_release=lambda: live.fault("pf-enable/1:after-release"))
        except subprocess.TimeoutExpired:
            return False, f"pfctl -X timed out after {live.timeout:g}s, token kept"

    return [remove_step(f"release pf's enable reference (the token in {token})", ("pf-enable/1", token), live,
                        lambda: enable_state(token, live), release, tools=(PFCTL,))]
