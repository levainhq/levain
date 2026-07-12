"""levain.firing.confinement — the OS-sandbox confinement floor for a sovereign entity's HANDS.

STEP (spore-303, rescoped 2026-07-10 → spore-311): the sovereign ``levain run`` entity becomes a
complete CC/Codex REPLACEMENT — a stateful networked shell that works on the operator's REAL repos,
not a workspace jail. The file-editor slice (step 6) confined the entity's file hands IN-PROCESS;
bash cannot be confined that way (a persistent host shell's ``cd ~`` / absolute paths escape any
Python check), so the shell needs an OS sandbox. This module is that sandbox — SLICE 1: the
confinement CORE (the policy + the profile + the persistent sandboxed shell), behind a provider seam.

**The polarity FLIPS (Phill-ratified 2026-07-10).** The step-6 file jail was ``(deny default) →
allow the workspace`` — a JAIL. A CC replacement inverts it: ``(allow default) → DENY the crown
jewels``. The OS sandbox stops being a jail and becomes a **structural FLOOR** that keeps the
(less-trusted, open-model) entity out of the sovereignty crown jewels no matter what it is told to
do, while it otherwise works like CC on real repos. This inversion also DISSOLVES the hardest part
of the old design — a default-DENY profile blocked ``cat``/``python`` from loading their own libs, so
it needed an empirically-hunted "system allow-set"; a default-ALLOW profile needs NO allow-set at all
(every tool loads its libs freely), so the profile is JUST the denylist.

**The crown-jewels denylist (structural, never — the Phill-ratified starting set):**
  - flow's store ``~/.anneal-memory/`` (the identity moat, in file terms);
  - credential files (declared by the caller — this module does NOT guess where secrets live);
  - sibling entities' ``<other>/.levain/`` stores (one entity can't read another's memory);
  - the ``~/.ssh`` key material (``ssh_mode="agent"``): the entity may USE keys via the agent socket
    to authenticate, but may not READ raw key material to exfil it — so ALL of ``~/.ssh`` is read+
    write-denied EXCEPT ``known_hosts`` (r+w, so ssh can record new host keys) and ``config`` (r).
    This is location-based, not name-based: it catches ``deploy_key`` / per-host keys, not just
    ``id_*`` (apparatus L2). ``ssh_mode="raw"`` is the fallback (allow raw ``~/.ssh`` read) — a live
    agent-mode round-trip 2026-07-12 (real ``ssh -T git@github.com`` auth + ``git ls-remote``, raw key
    read denied) confirmed agent-mode is tight AND functional, so raw is genuinely only a fallback.
  - the ssh persistence/exec vectors ``~/.ssh/{authorized_keys,authorized_keys2,config,rc}`` WRITE —
    denied in BOTH ssh_modes (slice 3, spore-322): a planted key (sshd honours it), a ``ProxyCommand``/
    ``Match exec`` in ``config`` (the operator's next ssh runs it), or an ``rc`` hook (sshd runs it on
    login) is code the operator/sshd runs LATER — a persistent backdoor, zero legit entity use. Rendered
    write-ONLY (raw-mode ~/.ssh reads still work), AND ~/.ssh's dir anchor — the LEXICAL path, so even a
    PRE-EXISTING ~/.ssh symlink can't be re-pointed — is pinned in ``deny_write_dirs`` so it can't be
    RELOCATED to dodge the literal (apparatus L3 codex, TWO rounds — a raw-mode ``mv ~/.ssh`` /
    symlink-anchor bypass, VERIFIED LIVE). The standard cred stores (``~/.config/gh`` / ``~/.aws/
    credentials`` / ``~/.netrc`` / ``~/.git-credentials``) are an OPT-IN (``deny_standard_creds``,
    default OFF — denying their read would break the entity's own gh/aws/curl use).
  - **ancestor-write-deny (apparatus L2 CRITICAL):** each crown jewel is pinned to an ABSOLUTE path,
    so under ``(allow default)`` the entity could ``mv`` a non-denied ANCESTOR directory of a jewel
    to relocate it out from under its deny string, then read it. So every ancestor dir of every jewel
    is ``file-write*``-denied (which blocks renaming THAT dir but still allows normal file creation
    INSIDE it — a write-check on the child path). Verified live 2026-07-11.

**Mechanism — macOS ``sandbox-exec`` (seatbelt).** Apple-deprecated since ~2017 but still ENFORCING
on Darwin 25.5 / macOS 26.5 (verified 2026-07-10, re-verified live here): a ``(allow default)``
profile that denies a subpath refuses a read of it (``Operation not permitted``) while real tools run
and — because the profile fences by PATH at the syscall level, not by cwd — a long-lived shell whose
cwd WANDERS (``cd ~``) still cannot read a denied path. That is exactly why an in-process fence fails
for a shell but an OS sandbox succeeds. A seatbelt profile applies to the process AND all its
descendants (the ``git`` / ``python`` / ``ssh`` the shell spawns), so the floor is inherited, not
per-command.

**The provider seam (``canonical_object_model_plus_replaceable_surfaces``, mirroring
``levain.daemon.DaemonProvider``).** ONE OS-agnostic :class:`CrownJewelsPolicy` behind a
:class:`ConfinementProvider` interface (``render_profile`` PURE → the platform's sandbox text;
``spawn_shell`` I/O → a persistent confined shell). macOS (:class:`SeatbeltProvider`) ships first;
Linux (``bwrap --ro-bind`` exclusions) and a container backend slot in as PURE ADDITIONS against this
contract — and the macOS denylist IS their requirements spec (macOS-first is the de-risk pass for all
three).

**Honest limits (welded in, not discovered — apparatus L2-verified, from the scope doc + review):**
  - ``sandbox-exec`` is Apple-DEPRECATED. Works on Darwin 25.5 (proven), Chrome still ships on it,
    but Apple could pull it — acceptable for a first slice; the provider seam is the hedge.
  - A PRE-POPULATED HARDLINK whose inode is a crown-jewel file, dropped by an untrusted party, could
    read through (seatbelt matches the path STRING; a hardlink has no path to the crown jewel). The
    entity itself cannot create one (it can't reference the out-of-tree target). Out of the single-
    operator threat model; named. (Slice 3 broadened this limit's surface to the WRITE direction too: a
    third-party pre-planted hardlink to a write-only vector at a writable path lets the entity write
    through — same out-of-model class.)
  - CUSTOM ``AuthorizedKeysFile`` (apparatus L3 complement): the ssh write-floor covers sshd's DEFAULT
    key files (``~/.ssh/authorized_keys`` + ``authorized_keys2``). An operator whose ``sshd_config``
    sets a non-default ``AuthorizedKeysFile`` (or an ``AuthorizedKeysCommand``) has a persistence path
    the floor does not name — pin it via ``deny_files``. Narrow (needs Remote Login on AND a customized
    sshd), but named rather than silently uncovered.
  - SEATBELT ``link()``/``rename()`` SOURCE-CHECK COUPLING (apparatus L3 complement, verified live): the
    write-floor's integrity rests on seatbelt applying ``file-write*`` to the SOURCE path of a hardlink/
    rename (so a write-denied file can't be relocated OUT), which held on Darwin 25.5 but is undocumented
    Apple behaviour on a deprecated tool. The ``ConfinementProvider`` seam is the hedge; the eventual
    Linux ``bwrap`` / container backend MUST re-prove "cannot hardlink/rename a write-denied file out" as
    a first-class provider-contract test — a ``--ro-bind`` on one file does not obviously give it.
  - REPLACEABLE LEXICAL HOME-CHAIN ANCESTOR (apparatus L3 codex round-3, the generalized class): the
    slice-3 fix pins the lexical ``~/.ssh`` anchor, but the pin is ``home.resolve() / ".ssh"`` — so if
    ``HOME`` ITSELF (or a lexical ancestor of it) is a USER-WRITABLE symlink (e.g. ``HOME=/tmp/linkhome``
    where ``linkhome -> /tmp/realhome``), the entity could ``rm /tmp/linkhome; ln -s /tmp/evilhome
    /tmp/linkhome`` to re-point the whole home, dodging every RESOLVED jewel deny. Out of the normal
    single-operator model (a real ``$HOME`` is ``/Users/<u>`` under root-owned ``/Users``, non-
    replaceable by the entity), so NOT fixed here — but named. This is the general "resolved jewel with
    a replaceable lexical ancestor" pattern; the real fix (if it ever matters) is to pin every writable
    symlink COMPONENT of every protected path's lexical chain, a cross-jewel pass, not an ssh one-off.
  - RESOURCE EXHAUSTION (fork bomb, disk fill) is a self-DoS on the operator's own Mac, not a
    confinement breach — seatbelt doesn't cap CPU/mem/disk. Cheap defense (``ulimit`` in the shell
    wrapper) is a later polish, not a floor concern.
  - DAEMONIZED SURVIVOR (apparatus L3 codex): a child that ``setsid``/``nohup``/double-forks into a NEW
    session escapes the shell's process-group teardown (:meth:`SandboxedShell.close`) and outlives the
    run — the same behavior a normal shell / CC / Codex has. It stays SANDBOX-CONFINED (crown jewels
    remain off-limits), so it is not a confinement breach, but it is unattended code with network +
    broad non-jewel authority. Human-in-the-loop bounds it now; the threshold membrane (network gating)
    is the unattended-operation answer.
  - NETWORK EXFIL of non-crown-jewel data: default-allow network + broad read means anything not
    crown-jeweled is exfiltratable — the SAME risk profile as CC. Mitigation is human-in-the-loop now
    + the threshold membrane (spore-295) gating network ops before unattended operation later.
  - IPC / LOCALHOST SIDE CHANNELS (apparatus L2, confirmed): default-allow leaves mach + loopback
    open, so the entity can drive an EXISTING unsandboxed daemon (``pbcopy``/``pasteboardd``) or hit a
    LOCAL service that re-exposes crown-jewel content (the argushub store on ``:8420``, the continuity
    digest pushed to Supabase) — the file-deny doesn't cover the socket. Spawning a NON-descendant
    unsandboxed helper via ``launchd`` is plausible (L2 could not confirm it under a non-GUI shell).
    Same class as network-exfil: human-in-the-loop now, the threshold membrane later.
  - CROWN-JEWEL DIRECTORY NAMES leak: ``ls ~`` lists ``.anneal-memory`` as a NAME (the parent's
    metadata is allowed; the subtree's CONTENTS + the jewel dir's own stat are denied). Names are
    already public; informational.
  - FLOOR-EXPANSION (spore-322, Phill-ratified 2026-07-12): ``~/.ssh/authorized_keys`` WRITE is now in
    the UNIVERSAL floor (:data:`_SSH_AUTHORIZED_KEYS`, both ssh_modes — a persistence backdoor with zero
    legit use, so it is folded unconditionally). The other default-allow-readable standard cred stores
    — ``~/.config/gh`` (a GitHub token = repo push/admin), ``~/.aws/credentials``, ``~/.netrc`` — are ≥
    the ssh key in impact, but denying their READ breaks the entity's own gh/aws/curl use, so they are
    an explicit OPT-IN (``deny_standard_creds`` in confinement.json, default OFF; :data:`_STANDARD_CRED_
    SUBTREES` / :data:`_STANDARD_CRED_FILES`) rather than always-on — operational-fit over purity. A
    caller can still pin any of them ad-hoc via ``deny_files`` / ``extra_deny_read_write``.

**Gating (v1 REALITY, load-bearing honesty).** This floor protects the crown jewels and NOTHING else.
With default-allow and no threshold membrane (a SPEC, not code — spore-295) and no permission prompts
(Phill: people bypass those IRL), a confabulating open model can still ``rm -rf`` a real repo,
``git push --force``, or ``curl | bash`` — none of which the floor stops. Therefore v1 = **structural
floor + you-in-the-loop** (YOLO-mode CC, crown jewels structurally protected). The membrane is the
precondition for UNATTENDED operation, sequenced after — NOT a v1 claim.

Pure stdlib (``os`` / ``platform`` / ``subprocess`` / ``tempfile`` / ``pathlib`` / ``threading``) —
importing this pulls NO anneal and NO openhands, so the confinement core is unit-testable in complete
isolation (the same dependency-isolated-leaf discipline as ``levain.firing.isolation`` and
``levain.daemon``). The OpenHands ``LevainBashTool`` that consumes this is a SEPARATE slice, and lives
with the file-editor tool under ``levain.firing.openhands`` (which is where the ``openhands`` import
is allowed to land).
"""
from __future__ import annotations

import json
import os
import platform
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal

__all__ = [
    "SANDBOX_EXEC",
    "ConfinementError",
    "SshMode",
    "CrownJewelsPolicy",
    "build_policy",
    "crown_jewel_reason",
    "ConfinementConfig",
    "load_confinement_config",
    "ShellResult",
    "SandboxedShell",
    "ConfinementProvider",
    "SeatbeltProvider",
    "select_provider",
    "sandbox_exec_available",
    "confinement_supported",
]

# The macOS seatbelt driver. An ABSOLUTE path (never a PATH lookup — a confined child must resolve
# the sandbox binary deterministically, and this is the OS-shipped location).
SANDBOX_EXEC = "/usr/bin/sandbox-exec"

SshMode = Literal["agent", "raw"]

# Backpressure bounds so a runaway producer (``yes``, ``tail -f`` left after a timeout) can't grow the
# PARENT process's memory without limit (apparatus L3 complement #4). Normal commands never hit these.
_MAX_OUTPUT_CHARS = 8 * 1024 * 1024   # per-command returned output cap (then truncate + mark)
_MAX_QUEUE_LINES = 200_000            # reader-queue depth; oldest dropped past this (runaway only)

# The ssh files WRITE-denied in EVERY ssh_mode — the persistence/exec-vector floor (slice 3, Phill-
# ratified 2026-07-12; expanded from authorized_keys-only after apparatus L1 caught the config/rc gap +
# the banner overclaim). Writing ANY of these grants code the operator or sshd runs LATER, with ZERO
# legit entity use — a strictly worse vector than reading a key (a read exfils; a write grants standing
# execution):
#   - ``authorized_keys`` / ``authorized_keys2`` — sshd honours a planted INBOUND key (standing access);
#   - ``config`` — the operator's OUTBOUND ssh runs ``ProxyCommand`` / ``LocalCommand`` / ``Match exec``
#     from it, so a written ssh config is RCE on the operator's NEXT ``ssh``/``git`` (the strongest one);
#   - ``rc`` — sshd runs ``~/.ssh/rc`` on INBOUND login.
# In ``ssh_mode="agent"`` the whole ``~/.ssh`` subtree is already denied (``config`` there is re-allowed
# READ-only, so its WRITE stays denied — consistent); in ``ssh_mode="raw"`` (raw ``~/.ssh`` reads
# allowed) these literal write-denies are the SOLE guard on the exec vectors — the raw-mode gap a live
# round-trip confirmed (a bare ``authorized_keys`` append succeeded). NOT exhaustive of ALL persistence
# (a ``~/.zshrc`` plant is equally possible in BOTH modes — the human-in-the-loop / non-crown-jewel-write
# limit documented in "Gating"); these are the ssh-specific zero-legit vectors, closed because the
# module's thesis is no claim>enforcement gap. SURGICAL (literal, not ancestor-expanded — see build_policy).
_SSH_WRITE_DENIED = ("authorized_keys", "authorized_keys2", "config", "rc")

# Standard, tool-CANONICAL credential locations. Folding these into the floor is an OPT-IN
# (``deny_standard_creds`` in ``confinement.json``, default OFF), never automatic, for two reasons:
# (1) unlike an app secret (``.env.flow``), these ARE structurally knowable — gh / aws / curl look
# here BY DEFINITION, so naming them is not the FALSE-SECURITY guessing the module otherwise refuses;
# (2) denying their READ breaks the entity's OWN legitimate ``gh`` / ``aws`` / netrc-``curl`` use, so a
# CC-replacement's hands must not lose them by default (operational-fit). They are >= the ssh key in
# impact under default-allow (a gh token = repo push/admin; aws creds = infra/$), so the operator who
# does NOT need those tools flips ONE config line to fold them in. ``~/.config/gh`` is a DIR (the token
# lives in ``hosts.yml``) → a denied subtree; ``credentials`` / ``.netrc`` are files.
_STANDARD_CRED_SUBTREES = ("~/.config/gh",)   # gh OAuth token (hosts.yml) → repo push/admin
_STANDARD_CRED_FILES = (
    "~/.aws/credentials",           # aws access key/secret. NOT ~/.aws/config — it holds region /
                                    # profile / SSO the entity legitimately needs; an operator whose
                                    # ~/.aws/config carries a secret pins it ad-hoc via deny_files.
    "~/.netrc",                     # netrc PAT / http creds (git-https, curl)
    "~/.git-credentials",           # git HTTPS PAT store (same class as ~/.netrc — apparatus L1)
    "~/.config/git/credentials",    # XDG-path git credential store
)


class ConfinementError(RuntimeError):
    """The confinement could not be established or is unavailable on this platform. FAIL-CLOSED:
    a caller that cannot build the floor must refuse to grant bash hands, never fall through to an
    unconfined host shell (``structural_invariants_beat_discipline``)."""


# --- the OS-agnostic policy (the canonical object) -------------------------------------------

@dataclass(frozen=True)
class CrownJewelsPolicy:
    """OS-agnostic description of the confinement FLOOR: default-allow, DENY the crown jewels.

    A :class:`ConfinementProvider` renders this into the platform's native sandbox profile. Build it
    with :func:`build_policy`, never by hand — the crown-jewels assembly (flow store + creds + sibling
    stores + ssh keys) is shared across OSes and is the load-bearing security surface.

    Every path is stored RESOLVED (symlink-followed, absolute) so the rendered denies match what the
    kernel sees. ``workspace`` is the shell's starting cwd + a definitely-writable root; it is NOT a
    jail (the entity may read/write broadly under default-allow) — it is just where a fresh entity's
    work lands by convention."""

    entity_dir: Path
    workspace: Path
    deny_read_write: tuple[Path, ...]   # crown-jewel SUBTREES (flow store, sibling .levain/ stores)
    deny_files: tuple[Path, ...]        # crown-jewel FILES (credential files) — literal, not subtree
    deny_write_dirs: tuple[Path, ...]   # ancestor DIRS write-denied → block rename-relocation (L2)
    ssh_dir: Path | None                # ~/.ssh to key-confine (ssh_mode="agent"); None = "raw" mode
    ssh_mode: SshMode = "agent"
    deny_write_files: tuple[Path, ...] = ()   # WRITE-only denied ssh FILES (authorized_keys*, config,
    # rc — the persistence/exec vectors) applied in BOTH ssh_modes: they block PLANTING a key / a
    # ProxyCommand config / an rc hook (code the operator or sshd runs later). Rendered as write-only
    # literals (read stays allowed on the seatbelt hand → raw-mode ~/.ssh reads work), AND their ancestor
    # ~/.ssh is pinned in deny_write_dirs (build_policy adds them to all_jewels) so the dir can't be
    # relocated to dodge the literal — the codex raw-mode bypass. Distinct from deny_files (read+write);
    # the in-process file-editor twin denies them outright (read included, stricter-on-ssh).
    config_file: Path | None = None     # <entity>/.levain/confinement.json — the config that DEFINES
    # the floor is itself floor-protected (read+write denied), so the entity can't rewrite its own jail
    # (apparatus L1 — confinement.json lives in the entity-writable .levain/; a self-authored
    # ssh_mode="raw" / emptied deny_files would weaken the NEXT session's operator-declared floor).
    # NOTE: network is default-ALLOWED (a CC replacement hits the network); there is deliberately NO
    # `allow_network` knob — an unwired boolean would be false security (the exact claim>enforcement
    # gap this module refuses). Network POLICY is a slice-3 / threshold-membrane concern.


def _write_deny_ancestors(jewels: list[Path]) -> tuple[Path, ...]:
    """Every ancestor DIRECTORY of every crown jewel (up to, but excluding, the filesystem root).

    Closes the rename-relocation bypass (apparatus L2 CRITICAL): a jewel is pinned to an absolute
    path, so under ``(allow default)`` the entity could ``mv`` a non-denied ANCESTOR of a jewel to
    move it out from under its deny, then read it. Write-denying each ancestor dir blocks renaming
    THAT dir (verified live) while STILL allowing normal file creation inside it (a write-deny on the
    literal dir path does not deny ``open(dir/child, O_CREAT)`` — a write-check on the child path).

    The root ``/`` is excluded (denying it is pointless; renaming ``/`` is impossible). Including
    high dirs like ``/Users`` / ``$HOME`` is harmless — they can't be renamed anyway (root-owned
    parent), and file creation inside them still works. Returned sorted for a deterministic profile."""
    out: set[Path] = set()
    for jewel in jewels:
        for anc in jewel.parents:
            if str(anc) == anc.anchor:  # skip the filesystem root itself
                continue
            out.add(anc)
    return tuple(sorted(out))


def _sibling_entity_stores(entity_dir: Path) -> tuple[Path, ...]:
    """The ``.levain/`` stores of SIBLING entities under the same parent — crown jewels this entity
    must never read (one sovereign mind can't reach another's memory).

    Enumerated at build time from ``<parent>/*/.levain`` EXCLUDING this entity's own store. Best-
    effort + structural: it denies the siblings that EXIST when the shell starts; a sibling created
    mid-session is not retroactively denied (a known v1 limit — the single-operator threat model does
    not include an adversary spinning up entities during a run; named, not papered over). An
    unreadable parent (permissions / not-a-dir) yields no siblings rather than raising — the fixed
    crown jewels (flow store, creds) are the non-negotiable floor; sibling isolation is additive."""
    own = (entity_dir / ".levain").resolve()
    parent = entity_dir.parent
    out: list[Path] = []
    try:
        children = sorted(parent.iterdir())
    except OSError:
        return ()
    for child in children:
        store = child / ".levain"
        try:
            if not store.is_dir():
                continue
            resolved = store.resolve()
        except OSError:
            continue
        if resolved != own:
            out.append(resolved)
    return tuple(out)


def build_policy(
    entity_dir: Path | str,
    *,
    workspace: Path | str | None = None,
    ssh_mode: SshMode = "agent",
    deny_files: tuple[Path | str, ...] = (),
    extra_deny_read_write: tuple[Path | str, ...] = (),
    deny_standard_creds: bool = False,
) -> CrownJewelsPolicy:
    """Assemble the crown-jewels floor for the entity at ``entity_dir``.

    **Universal floor (always denied — the structurally-knowable crown jewels):**
      - the operator-laptop memory store ``~/.anneal-memory/`` (subtree) — the identity moat in file
        terms (mirrors :func:`levain.firing.isolation.flow_store_dir`); a sovereign entity must never
        read the operator's own memory;
      - sibling entities' ``<other>/.levain/`` stores (subtrees) — one entity can't read another's
        memory;
      - ``~/.ssh/id_*`` private keys (read-denied when ``ssh_mode="agent"``): the entity authenticates
        via the agent socket but can't READ raw key material to exfil it. ``ssh_mode="raw"`` omits this
        (the fallback — a live round-trip 2026-07-12 confirmed ``ssh_mode="agent"`` is tight AND
        functional, so raw is genuinely only a fallback);
      - ``~/.ssh/authorized_keys`` (+ ``authorized_keys2``) WRITE — denied in BOTH ssh_modes: planting
        a key is a persistent SSH backdoor with zero legit entity use (in agent-mode the whole ~/.ssh
        subtree already covers it; in raw-mode this is the sole guard). Surgical (literal, not ancestor-
        expanded), so raw-mode keeps ~/.ssh otherwise writable.

    **Operator OPT-IN (``deny_standard_creds=True``):** fold the standard tool-canonical cred stores
    (``~/.config/gh`` subtree, ``~/.aws/credentials``, ``~/.netrc``) into the floor. These are knowable
    locations (not the false-security guessing the module refuses), but denying their READ breaks the
    entity's own gh/aws/curl use, so it is OFF by default — the operator enables it when the entity
    does not need those tools. Wired from ``confinement.json`` via :func:`policy_for_conv_state`.

    **Operator-declared crown jewels (the caller MUST pass — this generic, operator-neutral module
    deliberately does NOT guess where an operator's app-specific secrets live):**
      - ``deny_files`` — credential FILES (e.g. flow's ``~/Documents/flow/.env.flow``). Denied by
        ``literal`` so a same-named file elsewhere is unaffected. NOTE: guessing a filename like
        ``~/.env.flow`` is worse than useless — it is FALSE SECURITY (it "protects" a path the secret
        isn't at while missing the real one; L4-live 2026-07-11 caught exactly this), so the module
        refuses to guess. The ``levain run`` wiring (or a ``.levain/`` confinement config) supplies the
        operator's real cred files here.
      - ``extra_deny_read_write`` — additional crown-jewel SUBTREES (a secrets dir, another store).

    Every crown jewel's ancestor dirs are additionally write-denied (:func:`_write_deny_ancestors`) to
    close the rename-relocation bypass (apparatus L2). NOTE the entity's OWN ``<entity>/.levain/``
    store is NOT denied — the entity's memory is its own to read/write. Only the operator's memory
    store and SIBLING stores are the structural crown jewels."""
    ed = Path(entity_dir).expanduser().resolve()
    ws = (Path(workspace).expanduser().resolve() if workspace is not None
          else (ed / "workspace").resolve())
    home = Path.home()

    subtrees: list[Path] = [(home / ".anneal-memory").resolve()]
    subtrees.extend(_sibling_entity_stores(ed))
    for extra in extra_deny_read_write:
        subtrees.append(Path(extra).expanduser().resolve())

    files: list[Path] = [Path(f).expanduser().resolve() for f in deny_files]

    # OPT-IN (default OFF): fold the standard tool-canonical cred stores into the floor. Knowable
    # locations, not a guess — but denying their READ breaks the entity's own gh/aws/curl hands, so the
    # operator enables this only when the entity does not need those tools (``deny_standard_creds`` in
    # confinement.json). They flow through the SAME subtree/file machinery (read+write deny + ancestor
    # write-deny), so no separate rendering path is needed.
    if deny_standard_creds:
        subtrees.extend(Path(s).expanduser().resolve() for s in _STANDARD_CRED_SUBTREES)
        files.extend(Path(f).expanduser().resolve() for f in _STANDARD_CRED_FILES)

    ssh_dir = (home / ".ssh").resolve() if ssh_mode == "agent" else None

    # The ssh persistence/exec vectors (~/.ssh/authorized_keys*, config, rc — ``_SSH_WRITE_DENIED``) are
    # WRITE-denied in EVERY ssh_mode. TWO-PART enforcement (part 2 added after apparatus L3 codex, non-
    # replaceable, found a real raw-mode bypass in the literal-only first cut):
    #   (1) the FILES render as write-ONLY denies — read stays allowed on the seatbelt hand so raw-mode
    #       ~/.ssh reads still work (the in-process file-editor twin denies them outright); AND
    #   (2) the files are added to ``all_jewels`` below, so ~/.ssh (their ancestor DIR) is write-denied.
    #       WITHOUT (2), raw-mode leaves ~/.ssh renameable: ``mv ~/.ssh ~/.ssh.bak; ln -s ~/evil ~/.ssh``
    #       relocates the directory ANCHOR, so a key planted at ~/evil/authorized_keys is NOT the denied
    #       literal yet sshd's ``realpath()`` honours it (codex; VERIFIED LIVE — the bypass planted an
    #       attacker key). Write-denying the ~/.ssh dir LITERAL blocks renaming/replacing it while STILL
    #       allowing file creation/reads INSIDE it (a child-path write-check), so raw-mode is NOT re-jailed.
    ssh_home = home / ".ssh"
    deny_write_files_l = [(ssh_home / n).resolve() for n in _SSH_WRITE_DENIED]

    # De-dup while preserving order (a sibling could coincide with an extra); resolved paths compare
    # exactly, so a simple seen-set is sound.
    def _dedup(items: list[Path]) -> tuple[Path, ...]:
        seen: set[Path] = set()
        out: list[Path] = []
        for p in items:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return tuple(out)

    deny_read_write = _dedup(subtrees)
    deny_files_t = _dedup(files)
    deny_write_files_t = _dedup(deny_write_files_l)
    # The confinement config that DEFINES the floor is itself floor-protected (read+write denied): the
    # entity can't rewrite its own jail. It lives in the entity-writable ``.levain/`` (deliberately NOT
    # a crown jewel — the entity's memory is its own), so without this a self-authored ``ssh_mode="raw"``
    # / emptied ``deny_files`` would weaken the NEXT session's operator-declared floor (apparatus L1).
    # ``load_confinement_config`` runs in the HOST process (not the sandbox), so denying the entity's
    # HANDS from touching it does not stop the host from reading it to BUILD this policy.
    #
    # FAIL-CLOSED on a SYMLINKED config (apparatus L3 codex HIGH): if ``confinement.json`` (or its
    # ``.levain`` parent) is a symlink, ``.resolve()`` would store the symlink's TARGET as the denied
    # path — leaving the LEXICAL ``.levain/confinement.json`` (the path ``load_confinement_config`` reads)
    # unprotected, so the entity could ``rm`` + recreate it (``.levain`` is writable) to weaken the next
    # session. Requiring a plain file (lexical == resolved) makes the read+write+unlink deny cover the
    # exact path load reads, and keeps the file-editor's resolve-then-check in agreement.
    config_lexical = ed / ".levain" / _CONFINEMENT_CONFIG_NAME
    config_file = config_lexical.resolve()
    if config_file != config_lexical:
        raise ConfinementError(
            f"the confinement config {config_lexical} is a symlink (resolves to {config_file}) — "
            "refusing to grant confined hands. A symlinked config could be unlinked and rewritten to "
            "weaken the next session's floor; make it a plain file inside .levain/."
        )
    # Ancestor write-denies for the subtree jewels, file jewels, the write-only ssh files, AND the
    # config file (all relocate the same way). Including ``deny_write_files_t`` here write-denies ~/.ssh
    # (the persistence-vector dir anchor) so it can't be renamed/replaced to relocate the protected
    # literal — the raw-mode bypass codex found. The ssh_dir is guarded as a subtree below (agent mode),
    # so its ancestors are covered too; in raw mode this is the only thing that pins ~/.ssh's anchor.
    all_jewels = list(deny_read_write) + list(deny_files_t) + list(deny_write_files_t) + [config_file]
    if ssh_dir is not None:
        all_jewels.append(ssh_dir)
    # Pin the LEXICAL ~/.ssh anchor too (apparatus L3 codex re-verify HIGH — "resolved jewel vs lexical
    # anchor are different objects"). ``deny_write_files_l`` uses ``.resolve()`` for CONTENT protection
    # (it must deny the real write TARGET, even through a symlink) — but that FOLLOWS a pre-existing
    # ``~/.ssh -> ~/realssh`` symlink, so ancestor-deny pins ~/realssh, leaving lexical ~/.ssh replaceable
    # (``rm ~/.ssh; ln -s ~/evil ~/.ssh`` → the planted key at ~/evil dodges the resolved-target deny).
    # Deny the lexical dir path (resolved HOME + un-deref'd ``.ssh``) so ``rm``/``mv``/replace of ~/.ssh
    # ITSELF is blocked; child reads/writes still resolve through it, so raw mode is not re-jailed.
    ssh_anchor = home.resolve() / ".ssh"
    write_dirs = _dedup(list(_write_deny_ancestors(all_jewels)) + [ssh_anchor])

    return CrownJewelsPolicy(
        entity_dir=ed,
        workspace=ws,
        deny_read_write=deny_read_write,
        deny_files=deny_files_t,
        deny_write_dirs=write_dirs,
        ssh_dir=ssh_dir,
        ssh_mode=ssh_mode,
        config_file=config_file,
        deny_write_files=deny_write_files_t,
    )


def _canon(path: str) -> str:
    """Canonicalize a path string for comparison on a case- AND Unicode-normalization-insensitive
    volume (macOS APFS / Windows). ``Path.resolve()`` folds NEITHER, so ``~/.Anneal-Memory`` and an
    NFD/NFC variant of a path both point at the SAME on-disk file the kernel denies, yet compare
    UNEQUAL to the stored (canonical-case) jewel under a plain ``==``/``is_relative_to``."""
    return unicodedata.normalize("NFC", path).casefold()


def _ci_within(path: Path, root: Path) -> bool:
    """True iff ``path`` is ``root`` or lives under it, matched case- AND normalization-insensitively.

    A case-sensitive compare would let a case-variant of a crown jewel (``~/.Anneal-Memory``) or a
    Unicode-normalization variant slip past this IN-PROCESS check while the kernel (and the bash-side
    seatbelt) treat it as the SAME file — the exact ``claim > enforcement`` gap the sibling
    ``isolation._is_within_ci`` guards for the store forbidden-zone (apparatus L2 HIGH, and a
    regression: the step-6 ``assert_path_within_workspace`` predecessor used the CI compare and this
    predicate initially dropped it). Separator-anchored so it never false-matches a sibling like
    ``.anneal-memory-backup``. Over-matching here is FAIL-CLOSED (refuse a variant of a crown jewel) —
    ``crown_jewel_reason`` is a pure denylist, so there is no legitimate path this wrongly rejects."""
    p = _canon(str(path))
    r = _canon(str(root))
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def crown_jewel_reason(policy: CrownJewelsPolicy, path: Path | str) -> str | None:
    """Return a human reason if ``path`` is a crown jewel the floor denies, else ``None``.

    This is the IN-PROCESS twin of the seatbelt profile (:meth:`SeatbeltProvider.render_profile`),
    for a hand that is NOT under ``sandbox-exec`` — the relaxed file editor, which runs as ordinary
    in-process Python and so cannot ride the OS sandbox that fences bash. ONE policy, TWO enforcers:
    bash rides the rendered profile, the file editor calls this — so both hands enforce the SAME
    denylist and there is no ``claim > enforcement`` gap between them (``structural_invariants_beat_
    discipline``). FAIL-CLOSED: an unresolvable path (embedded NUL, un-stat-able) is treated as denied
    rather than silently allowed. Matching is case- AND normalization-insensitive (:func:`_ci_within`)
    so a macOS case-variant of a jewel can't slip past this while the kernel treats it as the same file.

    It denies: the crown-jewel SUBTREES (flow store + sibling ``.levain/`` stores + operator-declared
    extra subtrees + the opt-in standard cred stores), the crown-jewel credential FILES, the confinement
    CONFIG file (the entity can't rewrite its own floor), the ``deny_write_files`` (``~/.ssh/authorized_
    keys*`` — denied to the file editor OUTRIGHT in BOTH ssh_modes, read included: the file editor has
    no legit reason to touch ssh files at all, so this is fail-closed and consistent with the stricter-
    on-ssh stance; the seatbelt hand allows the READ in raw-mode, and denying both hands the WRITE is
    what matters), and — when ``ssh_mode="agent"`` — the WHOLE ``~/.ssh`` subtree. NOTE the ssh
    deny is stricter than the seatbelt's (which re-allows ``known_hosts`` r+w + ``config`` r so the
    SHELL's ssh can record host keys): the file editor has no legitimate need to touch ssh files (that
    is bash's job via ``ssh``), so denying all of ``~/.ssh`` here is fail-closed and avoids a read/write-
    polarity subtlety. Ancestor write-dirs are NOT checked — they exist to block ``mv``-relocation of a
    jewel, and the file editor has no rename primitive (its commands are ``view``/``create``/
    ``str_replace``/``insert``/``undo_edit``). The entity's OWN ``<entity>/.levain/`` store is
    deliberately NOT a crown jewel (its memory is its own); the firing's ``assert_entity_isolated``
    moat, not this predicate, is what keeps recall/capture off flow's store."""
    try:
        p = Path(path).expanduser().resolve()
    except (ValueError, OSError) as exc:
        return f"path {path!r} could not be resolved ({exc}) — refused (fail-closed)"
    for sub in policy.deny_read_write:
        if _ci_within(p, sub):
            return f"{p} is under the crown-jewel store {sub}"
    for f in policy.deny_files:
        if _ci_within(p, f):
            return f"{p} is a crown-jewel credential file"
    if policy.config_file is not None and _ci_within(p, policy.config_file):
        return f"{p} is the confinement config (the entity cannot rewrite its own floor)"
    if policy.ssh_dir is not None and _ci_within(p, policy.ssh_dir):
        return f"{p} is under ~/.ssh key material ({policy.ssh_dir})"
    for wf in policy.deny_write_files:
        if _ci_within(p, wf):
            return f"{p} is a write-protected ssh persistence/exec vector (authorized_keys/config/rc)"
    return None


# --- the operator-declared confinement config (optional, per-entity) -------------------------

@dataclass(frozen=True)
class ConfinementConfig:
    """The operator-declared half of the crown-jewels floor, read from
    ``<entity>/.levain/confinement.json``. The UNIVERSAL floor (flow store + sibling stores + ssh key
    material) is always applied by :func:`build_policy` regardless; this config only ADDS the
    operator's app-specific secrets, because this generic, operator-neutral module deliberately does
    NOT guess where an operator's credentials live (guessing a path is FALSE SECURITY — it "protects"
    a path the secret isn't at while missing the real one)."""

    deny_files: tuple[Path, ...] = ()      # credential FILES (literal): e.g. ~/Documents/flow/.env.flow
    deny_subtrees: tuple[Path, ...] = ()   # additional crown-jewel SUBTREES: a secrets dir, another store
    ssh_mode: SshMode = "agent"
    deny_standard_creds: bool = False      # OPT-IN: fold ~/.config/gh + ~/.aws/credentials + ~/.netrc
    # into the floor (default OFF — denying their READ breaks the entity's own gh/aws/curl use).


_CONFINEMENT_CONFIG_NAME = "confinement.json"


def load_confinement_config(entity_dir: Path | str) -> ConfinementConfig:
    """Load ``<entity>/.levain/confinement.json`` if present, else the default (universal floor only).

    Schema (all fields optional)::

        {"deny_files": ["~/Documents/flow/.env.flow"],
         "deny_subtrees": ["~/some/secrets"],
         "ssh_mode": "agent",
         "deny_standard_creds": false}

    ``~`` is expanded in every path. Unknown keys are IGNORED (forward-compat). A MISSING file returns
    the default (empty declarations, ``ssh_mode="agent"``) — the universal floor still protects the
    structurally-knowable crown jewels. A PRESENT-but-MALFORMED file (bad JSON, wrong types, an invalid
    ``ssh_mode``) raises :class:`ConfinementError` — FAIL-CLOSED: a broken crown-jewels declaration must
    not silently drop the operator's secrets and hand the entity a floor with holes; the caller refuses
    to grant confined hands and surfaces the error, so the operator fixes the config."""
    base = Path(entity_dir).expanduser() / ".levain" / _CONFINEMENT_CONFIG_NAME
    try:
        raw = base.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ConfinementConfig()
    except OSError as exc:
        raise ConfinementError(f"could not read {base} ({exc}) — fail-closed.") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfinementError(
            f"{base} is not valid JSON ({exc}) — fail-closed (refusing to grant hands on a broken "
            "crown-jewels declaration)."
        ) from exc
    if not isinstance(data, dict):
        raise ConfinementError(f"{base} must be a JSON object, got {type(data).__name__} — fail-closed.")

    def _paths(key: str) -> tuple[Path, ...]:
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfinementError(
                f"{base}: {key!r} must be a list of path strings — fail-closed."
            )
        return tuple(Path(v).expanduser() for v in value)

    ssh_mode = data.get("ssh_mode", "agent")
    if ssh_mode not in ("agent", "raw"):
        raise ConfinementError(
            f"{base}: ssh_mode must be \"agent\" or \"raw\", got {ssh_mode!r} — fail-closed."
        )

    deny_standard_creds = data.get("deny_standard_creds", False)
    # ``isinstance(True, int)`` is True, so guard against a JSON number sneaking in as a bool — require
    # a real bool (fail-closed: an ambiguous cred-floor declaration must not silently mis-parse).
    if not isinstance(deny_standard_creds, bool):
        raise ConfinementError(
            f"{base}: deny_standard_creds must be true or false, got "
            f"{deny_standard_creds!r} — fail-closed."
        )

    return ConfinementConfig(
        deny_files=_paths("deny_files"),
        deny_subtrees=_paths("deny_subtrees"),
        ssh_mode=ssh_mode,
        deny_standard_creds=deny_standard_creds,
    )


# --- the persistent sandboxed shell (the I/O primitive) --------------------------------------

@dataclass(frozen=True)
class ShellResult:
    """One command's result from a :class:`SandboxedShell`. ``output`` merges stdout+stderr (a
    terminal shows both interleaved); ``exit_code`` is the command's ``$?``. ``timed_out`` is True
    when the command did not complete within the deadline (``exit_code`` is then ``None``)."""

    output: str
    exit_code: int | None
    timed_out: bool = False


class SandboxedShell:
    """A persistent, stateful ``/bin/bash`` running UNDER the platform sandbox.

    ONE bash process reads sequential commands from a PRIVATE FIFO command channel, so cwd / env /
    shell functions PERSIST across :meth:`run` calls — a real stateful shell, not per-command exec
    (Phill: real dev work + SSH sessions need state). The sandbox profile fences by PATH at the syscall
    level, so even after the shell ``cd``\\ s into ``$HOME`` it still cannot read a denied crown jewel
    (the exact property an in-process fence cannot give a shell).

    Non-interactive (``bash --noprofile --norc`` reading a FIFO as its script): no PS1, no job control,
    no prompt noise. The command channel is a FIFO bash OPENS ITSELF (close-on-exec → children can't
    inherit/read it) and children get ``/dev/null`` stdin (so ``ssh host cmd`` / ``cat`` / a REPL can't
    hijack the channel — apparatus L1+L3). Per-command completion + exit code are read via an
    UNGUESSABLE, SPLIT sentinel (``<h1><h2> <$?>``): the joined output matches, but a ``set -x`` /
    DEBUG-trap trace of the sentinel line shows the halves separated, so a trace can't spoof
    end-of-command. A reader thread drains stdout into a bounded queue so :meth:`run` can enforce a
    wall-clock timeout without blocking on a hung command.

    KNOWN v1 LIMITS (documented, not hidden):
      1. **Single-caller.** :meth:`run` is NOT re-entrant/concurrent (it shares the sentinel counter +
         the stdout queue); a concurrent call fails FAST with :class:`ConfinementError`. ``close()`` /
         ``interrupt()`` are the only methods safe from another thread while a ``run()`` is in flight.
      2. **A NON-TERMINATING timed-out command wedges the shell.** ``timed_out`` leaves the command
         RUNNING; the sentinel self-heal recovers only when that command EVENTUALLY ends (its late
         sentinel is drained by the next ``run()``). A command that never returns (``nc -l``, an
         infinite loop) leaves its sentinel outstanding forever → later ``run()``\\ s time out draining
         it. Recovery is :meth:`close` + a fresh :meth:`SeatbeltProvider.spawn_shell`; this core does
         not auto-kill (that policy is the tool's). ``interrupt()`` SIGINTs the group, which a
         non-interactive bash treats as fatal — so it effectively ends the shell too.
      3. **Truly interactive programs** (``vim``, an interactive password prompt) need a PTY this
         core does not provide — the entity is an LLM driving non-interactive dev commands + agent-auth
         SSH, so a PTY is a later slice if ever needed.
      4. **A command that leaves the parser mid-statement** (unterminated heredoc/quote, a trailing
         ``\\``) fuses with the sentinel line → the split-token turns most such cases into a TIMEOUT
         (detectable) rather than silently-wrong output; the caller resets on timeout.
      5. A runaway producer left after a timeout has its output BOUNDED (per-command cap + a bounded
         reader queue that drops oldest); the loss is only under a runaway that will be ``close()``\\ d."""

    def __init__(
        self,
        *,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        default_timeout: float = 120.0,
    ) -> None:
        self._argv = argv
        self._cwd = cwd
        self._env = env
        self._default_timeout = default_timeout
        # An unguessable per-shell sentinel so no command's own output can spoof end-of-command.
        # The end-of-command marker. SPLIT into two halves emitted as separate printf args ('%s%s'):
        # the joined OUTPUT equals `_sentinel` (matched), but bash's own trace of the sentinel line
        # (`set -x` / `set -v` / a DEBUG trap echoing $BASH_COMMAND) shows the two halves SEPARATED by
        # whitespace, so a trace can't spoof end-of-command (apparatus L3 consensus — verified live:
        # unsplit, `set -x` silently corrupted the protocol).
        sentinel = f"__LEVAIN_SENTINEL_{os.urandom(12).hex()}__"
        self._sentinel = sentinel
        self._sent_h1 = sentinel[: len(sentinel) // 2]
        self._sent_h2 = sentinel[len(sentinel) // 2 :]
        self._proc: subprocess.Popen[str] | None = None
        self._cmd_w: IO[str] | None = None   # the FIFO command channel write end (see start())
        self._fifo_dir: str | None = None    # the tempdir holding the command FIFO (cleaned on close)
        self._stdout_q: "queue.Queue[str | None]" = queue.Queue(maxsize=_MAX_QUEUE_LINES)
        self._reader: threading.Thread | None = None
        self._run_lock = threading.Lock()    # serialize run(); fail-fast on concurrent misuse
        self._closed = False
        # OUTSTANDING sentinels: incremented per command written, decremented per sentinel seen. A
        # timed-out command leaves its sentinel outstanding (it fires late, when the command finally
        # ends), so the NEXT run() must first DRAIN the stale sentinel(s) + their late output before
        # reading its own — otherwise it would return the stale command's result. This makes the shell
        # SELF-HEAL across a timeout instead of desyncing (the caller can keep using it).
        self._pending = 0

    # -- lifecycle -----------------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        """True once this shell has been closed — either explicitly (:meth:`close`) or because a
        command ended it (an ``exit`` gives EOF → :meth:`run` self-closes). A consumer that reuses one
        shell across commands checks this to RESPAWN a fresh shell after the entity ran ``exit``,
        rather than handing the next command a dead channel (which would raise ``ConfinementError``)."""
        return self._closed

    def start(self) -> "SandboxedShell":
        """Spawn the sandboxed bash and its stdout reader thread. Returns self (chainable)."""
        if self._proc is not None:
            return self
        # The command channel is a FIFO that BASH OPENS ITSELF as its script. Why a FIFO and not an
        # inherited pipe fd (``pass_fds`` + ``/dev/fd/N``): an inherited fd is visible to bash's
        # CHILDREN at a known number, so a command can ``os.read(3, …)`` and steal the command stream /
        # learn the sentinel (apparatus L3 codex HIGH, verified live). A fd bash OPENS is CLOSE-ON-EXEC,
        # so children never inherit it — the command channel is private from the commands. Children's
        # stdin is ``/dev/null`` (no stdin hijack — apparatus L1). The fifo lives in an unguessable
        # mkdtemp dir (a child would have to guess the path to interfere).
        fifo_dir = tempfile.mkdtemp(prefix="levain-cmd-")
        fifo = os.path.join(fifo_dir, "cmd")
        rendezvous = -1
        try:
            os.mkfifo(fifo)
            self._proc = subprocess.Popen(
                [*self._argv, fifo],
                stdin=subprocess.DEVNULL,   # children get /dev/null, NOT the command channel
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,   # merge stderr into stdout (terminal-like)
                cwd=str(self._cwd),
                env=self._env,
                text=True,
                encoding="utf-8",
                errors="replace",           # a tool emitting non-UTF-8 bytes (binary `git diff`, a
                                            # latin-1 tool) must NOT crash the reader thread with a
                                            # UnicodeDecodeError + brick the shell (apparatus L3 —
                                            # verified live: strict decode killed the reader).
                bufsize=1,                  # line-buffered
                # A NEW session/process group (pgid == the shell's pid), so close()/interrupt() can
                # signal the WHOLE tree: a persistent shell spawns children (git, python, a timed-out
                # `sleep`) that a bare terminate() of bash alone would ORPHAN — reparented to init,
                # still running (verified live: a timed-out `sleep` survived close()). Signaling the
                # group reaps them; it also makes interrupt() reach a running child.
                start_new_session=True,
                # WELD (apparatus L2 HIGH): no INHERITED fd may bypass the profile (seatbelt checks
                # open(), not read() of an already-open fd). CALLER CONTRACT: no crown-jewel fd may be
                # open in this process at spawn time. With the FIFO channel there is NO passed fd.
                close_fds=True,
            )
            # Drain bash's stdout from the start so the handshake below can observe its output (incl. a
            # `sandbox-exec` startup error).
            self._reader = threading.Thread(target=self._drain_stdout, daemon=True)
            self._reader.start()
            # FIFO open dance (apparatus L3 codex round-2). O_RDWR RENDEZVOUS opens without blocking and
            # provides a reader so (a) bash's own blocking O_RDONLY script-open unblocks and (b) the real
            # O_WRONLY command channel opens without blocking. Then:
            #   - HANDSHAKE proves bash actually opened + is reading — else sandbox-exec/bash died at
            #     startup and spawn must FAIL, not hand back a dead shell (codex R2 #2);
            #   - close the rendezvous so the parent is WRITER-ONLY → a dead bash gives EPIPE on write,
            #     not an unbounded block (O_RDWR left the parent a reader → a big write could wedge; codex R2 #3);
            #   - UNLINK the fifo so a child's ``open($0)`` (bash discloses the script PATH as ``$0``,
            #     so "unguessable mkdtemp path" was NOT enough) hits ENOENT — the channel is truly
            #     private from the commands (codex R2 #1). bash keeps reading via its open fd.
            rendezvous = os.open(fifo, os.O_RDWR)
            self._cmd_w = os.fdopen(os.open(fifo, os.O_WRONLY), "w")
            self._handshake()
            os.close(rendezvous)
            rendezvous = -1
            os.unlink(fifo)
        except BaseException as exc:  # noqa: BLE001 — cleanup must survive Ctrl-C/SystemExit too, else
            # a cancellation during _handshake() leaks the rendezvous fd + fifo dir (close() can't
            # reach them: rendezvous is a local, _fifo_dir is unset until success — apparatus L3 codex R3).
            if rendezvous >= 0:
                try:
                    os.close(rendezvous)
                except OSError:
                    pass
                rendezvous = -1
            self._teardown_failed_start(fifo_dir)
            if not isinstance(exc, Exception):
                raise  # a cancellation (KeyboardInterrupt / SystemExit) propagates UNCHANGED
            reason = exc if isinstance(exc, ConfinementError) else (
                f"could not spawn the sandboxed shell ({exc}); argv={self._argv[:2]}…"
            )
            raise ConfinementError(str(reason)) from exc
        self._fifo_dir = fifo_dir
        return self

    def _handshake(self, timeout: float = 10.0) -> None:
        """Prove bash opened the FIFO + is executing commands: send a token, wait for it on stdout.
        Raise :class:`ConfinementError` if the shell exited / never responded (a dead sandbox driver
        must fail spawn, not masquerade as a live shell — apparatus L3 codex round-2 #2)."""
        cmd_w = self._cmd_w
        assert cmd_w is not None
        token = f"__LEVAIN_READY_{os.urandom(8).hex()}__"
        try:
            cmd_w.write(f"printf '%s\\n' '{token}'\n")
            cmd_w.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise ConfinementError(f"shell exited before the startup handshake ({exc})") from exc
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self._stdout_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                raise ConfinementError(
                    "shell exited during startup — sandbox-exec / bash did not start."
                )
            if token in line:
                return
        raise ConfinementError("timed out waiting for the shell startup handshake.")

    def _teardown_failed_start(self, fifo_dir: str) -> None:
        """Best-effort cleanup for a start() that raised: close the write end, kill the group, remove
        the fifo dir. Leaves ``_proc``/``_cmd_w`` None + ``_fifo_dir`` unset so a later close() is a
        no-op (this path already cleaned up)."""
        if self._cmd_w is not None:
            try:
                self._cmd_w.close()
            except Exception:  # noqa: BLE001 — teardown must never raise
                pass
            self._cmd_w = None
        if self._proc is not None and self._proc.poll() is None:
            self._signal_group(signal.SIGKILL)
        self._proc = None
        shutil.rmtree(fifo_dir, ignore_errors=True)

    def _drain_stdout(self) -> None:
        """Read stdout line by line into the queue; enqueue ``None`` at EOF (shell exited).

        Uses ``readline()`` in a loop, NOT ``for line in stdout`` — the iterator protocol read-aheads
        into an ~8KB buffer and won't yield a line until that buffer fills or EOF, which DEADLOCKS a
        persistent shell whose output never fills a block and whose stdin stays open (no EOF). Verified
        live: the iterator form hangs, ``readline()`` streams per line."""
        # Capture proc/stdout ONCE at entry into locals (apparatus L3 complement #3): close() nulls
        # self._proc from another thread, so re-reading self._proc.stdout mid-loop could AttributeError.
        proc = self._proc
        if proc is None or proc.stdout is None:
            self._stdout_q.put(None)
            return
        stdout: IO[str] = proc.stdout

        def enqueue(item: str | None) -> None:
            # Non-blocking put that DROPS the oldest line when full, so a runaway producer can't grow
            # memory AND the reader never blocks (a blocking put(None) at EOF could hang forever if no
            # consumer is draining). Lossy only under a runaway that will be close()d anyway.
            try:
                self._stdout_q.put_nowait(item)
            except queue.Full:
                try:
                    self._stdout_q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._stdout_q.put_nowait(item)
                except queue.Full:
                    pass

        try:
            while True:
                line = stdout.readline()
                if line == "":  # EOF — the shell exited
                    break
                enqueue(line)
        finally:
            enqueue(None)

    def run(self, command: str, *, timeout: float | None = None) -> ShellResult:
        """Run ``command`` in the persistent shell; return its output + exit code.

        Writes the command, then a sentinel echo carrying ``$?``, and reads stdout until the sentinel
        line appears (or the deadline lapses → ``timed_out``). Because it is ONE long-lived bash, state
        set by a prior command (cwd, exported vars, functions) is visible here."""
        # run() is single-caller: it shares _pending + the stdout queue as ONE state machine, so
        # concurrent calls would corrupt output attribution + the sentinel counter. Fail FAST on
        # concurrent misuse (apparatus L3 consensus) rather than silently mis-attribute. close() /
        # interrupt() deliberately do NOT take this lock — they must work cross-thread while a run()
        # (possibly a long one) is in flight.
        if not self._run_lock.acquire(blocking=False):
            raise ConfinementError(
                "SandboxedShell.run() is single-caller; a command is already running on this shell."
            )
        try:
            # Local capture: close() may null self._cmd_w from another thread between this check and the
            # write; the local then fails with ValueError (write on a closed file) → clean refusal, not
            # an uncaught AttributeError (apparatus L3 consensus).
            cmd_w = self._cmd_w
            if self._closed or self._proc is None or cmd_w is None:
                raise ConfinementError(
                    "shell is not running (call start() first, and not after close())"
                )
            deadline_s = self._default_timeout if timeout is None else timeout

            # `command`, then the sentinel printf. The sentinel is SPLIT into two printf args ('%s%s')
            # whose OUTPUT joins to the token but whose SOURCE (what a `set -x` / DEBUG-trap trace
            # echoes) shows the halves separated — so a trace can't spoof end-of-command (apparatus L3).
            payload = (
                f"{command}\n"
                f"printf '%s%s %d\\n' '{self._sent_h1}' '{self._sent_h2}' \"$?\"\n"
            )
            try:
                cmd_w.write(payload)
                cmd_w.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise ConfinementError(
                    f"sandboxed shell command channel is closed ({exc})"
                ) from exc
            self._pending += 1  # this command's sentinel is now outstanding

            deadline = time.monotonic() + deadline_s
            collected: list[str] = []
            total = 0
            truncated = False
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # My sentinel stays outstanding (it fires when the command finally ends); the NEXT
                    # run() drains it. Collected so far is this (timed-out) command's partial output.
                    return ShellResult(output="".join(collected), exit_code=None, timed_out=True)
                try:
                    line = self._stdout_q.get(timeout=min(remaining, 0.5))
                except queue.Empty:
                    continue
                if line is None:  # EOF — the shell exited (e.g. the command ran `exit`)
                    self._pending = 0
                    self.close()  # reap + release (unlinks the profile for _SeatbeltShell)
                    return ShellResult(output="".join(collected), exit_code=None, timed_out=False)
                # Find the sentinel as a SUBSTRING, not a line-prefix: a command whose output has no
                # trailing newline (``head -c 60``, ``printf`` without ``\n``) concatenates the sentinel
                # onto its last output line, so a ``startswith`` check would miss it and the shell would
                # hang (verified live). The sentinel is an unguessable random token, so real command
                # output cannot spoof it.
                idx = line.find(self._sentinel)
                if idx != -1:
                    self._pending -= 1
                    if self._pending > 0:
                        # A STALE sentinel from a prior timed-out command — discard its (now-complete)
                        # output + keep reading for MY sentinel (self-heal the desync).
                        collected, total, truncated = [], 0, False
                        continue
                    if idx:
                        collected.append(line[:idx])  # output that shared the sentinel's line
                    tail = line[idx + len(self._sentinel):].strip()
                    try:
                        code = int(tail)
                    except ValueError:
                        code = None
                    return ShellResult(output="".join(collected), exit_code=code, timed_out=False)
                # Bound the per-command returned output (apparatus L3 complement #4): past the cap, stop
                # accumulating so a huge/runaway output can't grow the parent heap without limit.
                if total < _MAX_OUTPUT_CHARS:
                    collected.append(line)
                    total += len(line)
                elif not truncated:
                    collected.append(f"\n[output truncated at {_MAX_OUTPUT_CHARS} chars]\n")
                    truncated = True
        finally:
            self._run_lock.release()

    def _signal_group(self, sig: int) -> None:
        """Send ``sig`` to the shell's whole process GROUP (the shell + every child it spawned).
        Best-effort — a dead process / a platform without ``killpg`` is a no-op, never a raise."""
        proc = self._proc
        if proc is None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, OSError, AttributeError):
            pass

    def interrupt(self) -> None:
        """Best-effort SIGINT to the shell's process GROUP (Ctrl-C a hung command AND its children).
        Never raises. Signals the group, not just bash, because a running child (``sleep``, a build)
        is the process actually blocking — bash is asleep waiting on it."""
        if self._proc is None or self._proc.poll() is not None:
            return
        self._signal_group(signal.SIGINT)

    def close(self) -> None:
        """Terminate the shell + its process GROUP, then release resources. Idempotent, never raises.
        Signals the group (SIGTERM → SIGKILL) so a timed-out / backgrounded child in the shell's pgid
        is reaped, not orphaned to init.

        LIMIT (apparatus L3 codex, welded not hidden): a child that DAEMONIZES into a NEW session/pgid
        (``setsid`` / ``nohup`` / a double-fork) escapes ``killpg`` and SURVIVES close — the same
        behavior a normal shell (and CC/Codex) has. The survivor stays SANDBOX-CONFINED (the seatbelt
        is inherited, so crown jewels remain off-limits), but it is unattended code with network + broad
        non-jewel authority after the operator thinks the run ended. Reaping arbitrary setsid escapees
        is racy and out of scope here; the human-in-the-loop v1 posture + the crown-jewels floor bound
        it, and the threshold membrane (network gating) is the real answer for unattended operation."""
        self._closed = True
        proc = self._proc
        if self._cmd_w is not None:
            try:
                self._cmd_w.close()  # EOF on bash's script → it exits gracefully
            except Exception:  # noqa: BLE001 — already-closed / teardown must never raise
                pass
            self._cmd_w = None
        try:
            if proc is not None and proc.poll() is None:
                self._signal_group(signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._signal_group(signal.SIGKILL)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
        finally:
            self._proc = None
            if self._fifo_dir is not None:
                shutil.rmtree(self._fifo_dir, ignore_errors=True)  # always remove the command FIFO
                self._fifo_dir = None

    def __enter__(self) -> "SandboxedShell":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()


# --- the provider seam (mirrors levain.daemon.DaemonProvider) --------------------------------

class ConfinementProvider(ABC):
    """One thin provider per OS. ``render_profile`` is PURE (no I/O) so the generated sandbox text is
    fully testable without touching the system; ``spawn_shell`` shells out to the platform sandbox
    driver. The macOS provider ships first; ``bwrap`` (Linux) + a container backend are PURE ADDITIONS
    against this contract, and the macOS crown-jewels denylist is their requirements spec."""

    @abstractmethod
    def render_profile(self, policy: CrownJewelsPolicy) -> str:
        """Render ``policy`` into the platform's native sandbox profile text (no I/O)."""

    @abstractmethod
    def spawn_shell(
        self,
        policy: CrownJewelsPolicy,
        *,
        env: dict[str, str] | None = None,
        default_timeout: float = 120.0,
    ) -> SandboxedShell:
        """Start a persistent shell confined by ``policy`` (writes the profile, spawns the sandboxed
        bash). The returned :class:`SandboxedShell` is already ``start()``\\ ed."""


def _reject_control_chars(value: str) -> None:
    """A profile is a SECURITY-surface generator; a path containing a newline / control char could
    break the SBPL line and inject profile syntax (the sibling-enumeration path is where a weird dir
    name could flow in). Refuse it — FAIL CLOSED (apparatus L2). Not merely escaped, because SBPL's
    string-escape support for control chars is not something to bet the floor on."""
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ConfinementError(
            f"refusing to render a sandbox profile with a control character in a path ({value!r}) — "
            "fail-closed rather than risk profile-syntax injection."
        )


def _sbpl_string(value: str) -> str:
    r"""Escape a path for an SBPL double-quoted string literal (``"..."``). SBPL strings escape ``\``
    and ``"`` with a backslash. macOS resolved paths do not normally contain either, but escape
    defensively so a pathological path can never break out of the string and inject profile syntax.
    A control char (which SBPL escaping can't be trusted to neutralize) is refused outright."""
    _reject_control_chars(value)
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _sbpl_regex(value: str) -> str:
    r"""Escape a literal path PREFIX for use inside an SBPL ``(regex #"...")`` anchored match. Regex
    metacharacters in the path (``.`` in a dotfile, ``+``, ``(`` …) are escaped so the pattern matches
    the literal prefix, plus the SBPL-string escapes for ``\`` and ``"``."""
    _reject_control_chars(value)
    out: list[str] = []
    for ch in value:
        if ch in r".^$*+?()[]{}|\\/":
            out.append("\\" + ch)
        elif ch == '"':
            out.append('\\"')
        else:
            out.append(ch)
    return "".join(out)


def _caller_denies(path: Path, policy: CrownJewelsPolicy) -> bool:
    """True if the CALLER explicitly made ``path`` a crown jewel (in ``deny_files``, or under a
    ``deny_read_write`` subtree). Used so the ssh convenience re-allow of ``known_hosts`` / ``config``
    never SILENTLY overrides an operator's explicit deny of that same path (apparatus L3 consensus —
    everywhere else a caller-declared jewel is final; this keeps ssh consistent).

    Matched case- AND normalization-insensitively (:func:`_ci_within`), consistently with
    ``crown_jewel_reason`` (apparatus L3 codex MED): a case-sensitive compare here would let the ssh
    convenience-allow override an operator deny declared as a case/Unicode variant (e.g. ``~/.SSH/
    config``) for the bash hand while the file editor still denies it — a two-enforcer split."""
    p = path.resolve()
    if any(_ci_within(p, f) for f in policy.deny_files):
        return True
    return any(_ci_within(p, sub) for sub in policy.deny_read_write)


class SeatbeltProvider(ConfinementProvider):
    """macOS ``sandbox-exec`` (seatbelt / SBPL) provider.

    Renders a ``(version 1)(allow default)`` profile that DENIES the crown jewels — the polarity flip:
    the sandbox is a FLOOR, not a jail. Deny both read AND write on the subtrees (the entity can
    neither exfil nor corrupt flow's memory / a sibling's memory); deny the credential FILES; and for
    ``ssh_mode="agent"`` deny READS of ``~/.ssh/id_*`` (raw key material) while leaving the agent
    socket + ``known_hosts`` + ``config`` readable so agent-auth still works."""

    def render_profile(self, policy: CrownJewelsPolicy) -> str:
        lines: list[str] = [
            "(version 1)",
            "",
            ";; Levain sovereign-entity confinement — the crown-jewels FLOOR (spore-311).",
            ";; POLARITY: default-ALLOW so the entity works like CC/Codex on real repos, then",
            ";; structurally DENY the sovereignty crown jewels no matter what the model is told.",
            ";; A default-allow profile needs NO system allow-set (tools load their own libs freely).",
            "(allow default)",
            "",
        ]

        if policy.deny_read_write:
            lines.append(";; crown-jewel SUBTREES — flow store + sibling .levain/ stores (read+write)")
            lines.append("(deny file-read* file-write*")
            for p in policy.deny_read_write:
                lines.append(f'    (subpath "{_sbpl_string(str(p))}")')
            lines.append(")")
            lines.append("")

        if policy.deny_files:
            lines.append(";; crown-jewel FILES — credential files (literal, not a subtree)")
            lines.append("(deny file-read* file-write*")
            for p in policy.deny_files:
                lines.append(f'    (literal "{_sbpl_string(str(p))}")')
            lines.append(")")
            lines.append("")

        if policy.config_file is not None:
            lines.append(";; the confinement CONFIG that defines the floor is floor-protected (read+")
            lines.append(";; write) — the entity cannot rewrite its own jail. The host reads it un-")
            lines.append(";; sandboxed to BUILD this policy, so this only fences the entity's HANDS.")
            lines.append(
                f'(deny file-read* file-write* (literal "{_sbpl_string(str(policy.config_file))}"))'
            )
            lines.append("")

        if policy.deny_write_dirs:
            lines.append(";; ancestor DIRS write-denied → block mv-relocation of a crown jewel (L2).")
            lines.append(";; Denies renaming THESE dirs; still allows creating files INSIDE them.")
            lines.append("(deny file-write*")
            for p in policy.deny_write_dirs:
                lines.append(f'    (literal "{_sbpl_string(str(p))}")')
            lines.append(")")
            lines.append("")

        if policy.ssh_dir is not None:
            ssh = policy.ssh_dir
            known_hosts = ssh / "known_hosts"
            config = ssh / "config"
            lines.append(";; ssh KEY MATERIAL (ssh_mode=agent) — LOCATION-based, not name-based: deny")
            lines.append(";; ALL of ~/.ssh (catches deploy_key / per-host keys, not just id_*), so the")
            lines.append(";; entity authenticates via the agent SOCKET but can't read/plant raw keys.")
            lines.append(f'(deny file-read* file-write* (subpath "{_sbpl_string(str(ssh))}"))')
            # Re-allow the two files ssh actually needs — emitted AFTER the deny (SBPL is last-match-
            # wins, so these win): known_hosts r+w (ssh records new host keys) + config r. NEVER
            # re-allow a path the CALLER explicitly denied (else this convenience allow would silently
            # override an operator's crown-jewel deny — apparatus L3 consensus).
            if not _caller_denies(known_hosts, policy):
                lines.append(
                    f'(allow file-read* file-write* (literal "{_sbpl_string(str(known_hosts))}"))'
                )
            if not _caller_denies(config, policy):
                lines.append(f'(allow file-read* (literal "{_sbpl_string(str(config))}"))')
            lines.append("")

        if policy.deny_write_files:
            # LAST deny block (after the ssh block) so last-match-wins keeps it denied. WRITE-only (read
            # stays allowed → raw-mode ~/.ssh reads still work); the FILES are literal here, while their
            # dir anchor ~/.ssh is pinned against relocation by ``deny_write_dirs`` (build_policy adds
            # these to ``all_jewels`` — the codex raw-mode-bypass fix). In agent-mode the whole ~/.ssh
            # subtree already denies these (redundant-but-explicit); rendering in both modes makes the
            # persistence-vector floor ssh_mode-independent and self-documenting.
            lines.append(";; ssh persistence/exec vectors (authorized_keys*, config, rc) — ALWAYS write-")
            lines.append(";; denied (both ssh_modes): a planted key / ProxyCommand config / rc is code the")
            lines.append(";; operator or sshd runs later — a persistent backdoor, zero legit entity use.")
            lines.append("(deny file-write*")
            for p in policy.deny_write_files:
                lines.append(f'    (literal "{_sbpl_string(str(p))}")')
            lines.append(")")
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    def spawn_shell(
        self,
        policy: CrownJewelsPolicy,
        *,
        env: dict[str, str] | None = None,
        default_timeout: float = 120.0,
    ) -> SandboxedShell:
        if not sandbox_exec_available():
            raise ConfinementError(
                f"{SANDBOX_EXEC} is not available — cannot establish the macOS confinement floor. "
                "Refusing to grant bash hands without the sandbox (fail-closed)."
            )
        profile_text = self.render_profile(policy)
        # A temp profile file for the shell's lifetime — `sandbox-exec -f` reads it before the sandbox
        # applies, so it need not be inside the allow-set. The SandboxedShell owns unlink on close.
        fd, profile_path = tempfile.mkstemp(prefix="levain-seatbelt-", suffix=".sb")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(profile_text)
        except OSError as exc:
            os.unlink(profile_path)
            raise ConfinementError(f"could not write the seatbelt profile ({exc})") from exc

        argv = [
            SANDBOX_EXEC, "-f", profile_path,
            "/bin/bash", "--noprofile", "--norc",
        ]
        # A fresh entity's cwd is its workspace; ensure it exists (the shell's Popen(cwd=) needs a real
        # dir). Broad reach is default-allowed, so this is a convenience starting point, not a jail.
        policy.workspace.mkdir(parents=True, exist_ok=True)
        shell = _SeatbeltShell(
            argv=argv,
            cwd=policy.workspace,
            env=env if env is not None else _default_shell_env(),
            default_timeout=default_timeout,
            profile_path=Path(profile_path),
        )
        try:
            return shell.start()
        except Exception:
            # start() raised (a bad spawn) → the profile file would otherwise leak (close() is never
            # reached). shell.close() unlinks it even though _proc is None (apparatus L1 #4a).
            shell.close()
            raise


def _default_shell_env() -> dict[str, str]:
    """A minimal, sane env for the confined shell — PATH (so real tools resolve), HOME (so ``~``
    expands + git/ssh find their config), TERM=dumb (non-interactive, no escape-code noise). The
    caller (the tool slice) may pass a richer env; this is the safe default that does NOT forward the
    operator's loaded secrets (a cred FILE is denied by the profile, but a secret already in this
    process's env would otherwise be inherited — so start from a clean env, not ``os.environ``)."""
    base_path = os.environ.get("PATH", "")
    dirs = [d for d in base_path.split(os.pathsep) if d] or ["/usr/bin", "/bin"]
    # Guarantee the standard tool dirs are present even if this process's PATH is odd.
    for standard in ("/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        if standard not in dirs:
            dirs.append(standard)
    env = {
        "PATH": os.pathsep.join(dirs),
        "HOME": str(Path.home()),
        "TERM": "dumb",
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
    }
    # Forward the ssh-agent socket PATH (not a secret — a socket, not key material) so ``ssh_mode=
    # "agent"`` can actually authenticate: it denies raw key READS precisely to force agent auth, so
    # the agent socket must be reachable or agent-auth fails outright (apparatus L1 #5). The private
    # keys themselves stay unreadable via the profile.
    auth_sock = os.environ.get("SSH_AUTH_SOCK")
    if auth_sock:
        env["SSH_AUTH_SOCK"] = auth_sock
    return env


class _SeatbeltShell(SandboxedShell):
    """A :class:`SandboxedShell` that also unlinks its seatbelt profile file on close. Kept private —
    callers get a :class:`SandboxedShell` from :meth:`SeatbeltProvider.spawn_shell`."""

    def __init__(
        self,
        *,
        profile_path: Path,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        default_timeout: float = 120.0,
    ) -> None:
        # Explicit params forwarded to super (NOT **kwargs: object, which would erase the signature so
        # mypy couldn't catch a wrong/renamed kwarg — apparatus L1 #9).
        super().__init__(argv=argv, cwd=cwd, env=env, default_timeout=default_timeout)
        self._profile_path = profile_path

    def close(self) -> None:
        super().close()
        try:
            self._profile_path.unlink()
        except OSError:
            pass


def sandbox_exec_available() -> bool:
    """True iff the macOS seatbelt driver is present + executable. The honesty floor: a caller must
    check this and REFUSE to grant bash hands if False, rather than fall through to an unconfined
    host shell."""
    return os.path.isfile(SANDBOX_EXEC) and os.access(SANDBOX_EXEC, os.X_OK)


def confinement_supported(system: str | None = None) -> bool:
    """True iff an OS confinement floor can actually be established on this platform RIGHT NOW —
    a provider exists for the OS AND its sandbox driver is present + executable.

    The honest gate for granting bash hands: ``levain run`` calls this to decide whether to offer the
    bash tool at all (macOS with a working ``sandbox-exec``), rather than wire a tool whose first
    command would fail-closed. On any non-macOS platform (no provider yet) or a macOS box missing
    ``sandbox-exec``, returns False — the entity gets its file-editor hand but no bash, and the banner
    says so (honesty floor). NEVER grants an unconfined shell as a fallback."""
    try:
        select_provider(system)
    except ConfinementError:
        return False
    return sandbox_exec_available()


def select_provider(system: str | None = None) -> ConfinementProvider:
    """The confinement provider for ``system`` (default: the running OS).

    macOS → :class:`SeatbeltProvider`. Anything else raises :class:`ConfinementError` naming the seam
    — Linux ``bwrap`` + a container backend are PURE ADDITIONS here (the seam exists so "defer the
    others" is on-rails, not a rewrite), but until one is built, a non-macOS caller must fail-closed,
    never grant an unconfined shell."""
    system = system or platform.system()
    if system == "Darwin":
        return SeatbeltProvider()
    raise ConfinementError(
        f"OS confinement is not yet implemented for {system!r} — only macOS (sandbox-exec) ships in "
        "this slice. The ConfinementProvider seam is here; a bwrap (Linux) / container provider slots "
        "in as a pure addition. Refusing to grant bash hands without a confinement floor (fail-closed)."
    )
