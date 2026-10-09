"""levain.firing.confinement — the OS-sandbox confinement floor for a sovereign entity's HANDS.

STEP (spore-303, rescoped 2026-07-10 → spore-311): the sovereign ``levain run`` entity becomes a
complete CC/Codex REPLACEMENT — a stateful networked shell that works on the operator's REAL repos,
not a workspace jail. The file-editor slice (step 6) confined the entity's file hands IN-PROCESS;
bash cannot be confined that way (a persistent host shell's ``cd ~`` / absolute paths escape any
Python check), so the shell needs an OS sandbox. This module is that sandbox — SLICE 1: the
confinement CORE (the policy + the profile + the stateful sandboxed shell), behind a provider seam.

**The polarity FLIPS (Phill-ratified 2026-07-10).** The step-6 file jail was ``(deny default) →
allow the workspace`` — a JAIL. A CC replacement inverts it: ``(allow default) → DENY the crown
jewels``. The OS sandbox stops being a jail and becomes a **structural FLOOR** that keeps the
(less-trusted, open-model) entity out of the sovereignty crown jewels no matter what it is told to
do, while it otherwise works like CC on real repos (one stated exception, Linux only: inside bash
the entries directly in ``$HOME`` are read-only; see step (0) of :func:`_bwrap_plan_impl`). This
inversion also DISSOLVES the hardest part
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
    credentials`` / ``~/.netrc`` / ``~/.git-credentials``) are folded in by ``build_policy``'s
    ``deny_standard_creds`` switch; :func:`levain.firing.drive.resolve_cred_floor` sets it, denying them in
    every drive except the interactive REPL unless the entity declares ``deny_standard_creds: false``).
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
``spawn_shell`` I/O → a stateful confined shell). macOS (:class:`SeatbeltProvider`) shipped first;
Linux (:class:`BwrapProvider`, K4c) shipped second against the same contract; a container backend
remains a PURE ADDITION — and the macOS denylist IS their requirements spec (macOS-first was the
de-risk pass, and it held: the contract needed ONE addition, ``available()``, and no reshaping).
⚠ The Linux mechanism is NOT "``--ro-bind`` exclusions" as this line predicted before it was built.
A ``--ro-bind`` denies WRITES while leaving READS open, so it is right for the write-only vectors and
WRONG for a crown-jewel subtree; those are ``--tmpfs`` + ``--remount-ro``. The full measured mapping
is tabulated above :func:`_bwrap_argv`.

**Honest limits (welded in, not discovered — apparatus L2-verified, from the scope doc + review):**
  - ``sandbox-exec`` is Apple-DEPRECATED. Works on Darwin 25.5 (proven), Chrome still ships on it,
    but Apple could pull it — acceptable for a first slice; the provider seam is the hedge.
  - A PRE-POPULATED HARDLINK whose inode is a crown-jewel file reads (or, for a write-only vector,
    writes) through, because both floors match paths. The entity itself cannot create one (``ln`` of a
    jewel is refused). bash is refused at spawn when any jewel has more than one name,
    wherever the names are (:func:`_refuse_multiply_linked_jewels`), and the file editor refuses any
    path that is another name for a jewel (:func:`linked_jewel_reason`). Still open: a link a host
    process creates while a shell is live (checked at spawn only), and copies, which are other files.
  - CUSTOM ``AuthorizedKeysFile`` (apparatus L3 complement): the ssh write-floor covers sshd's DEFAULT
    key files (``~/.ssh/authorized_keys`` + ``authorized_keys2``). An operator whose ``sshd_config``
    sets a non-default ``AuthorizedKeysFile`` (or an ``AuthorizedKeysCommand``) has a persistence path
    the floor does not name — pin it via ``deny_files``. Narrow (needs Remote Login on AND a customized
    sshd), but named rather than silently uncovered.
  - SEATBELT ``link()``/``rename()`` SOURCE-CHECK COUPLING (apparatus L3 complement, verified live): the
    write-floor's integrity rests on seatbelt applying ``file-write*`` to the SOURCE path of a hardlink/
    rename (so a write-denied file can't be relocated OUT), which held on Darwin 25.5 but is undocumented
    Apple behaviour on a deprecated tool. The ``ConfinementProvider`` seam is the hedge. ✅ **RE-PROVEN
    FOR LINUX (K4c, 2026-09-03) — this requirement is MET**, by a mechanism that does not rest on a
    vendor quirk: under :class:`BwrapProvider`, moving a write-denied file out returns EBUSY (it is a
    mountpoint) and hardlinking it out returns EXDEV (the bind is a separate mount device); ``rm`` of
    the file and ``rm -rf`` of its parent both return EBUSY too. Measured on Ubuntu 24.04 / bubblewrap
    0.9.0 and covered by a provider-contract test. The original doubt — "a ``--ro-bind`` on one file
    does not obviously give it" — was well placed; it turns out to give MORE than seatbelt does, for a
    different reason (mount identity, not a source-path predicate).
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
    run — the same behavior a normal shell / CC / Codex has (macOS; under bwrap each command's bash is
    pid 1 of its own pid namespace, and the kernel ends everything in it when bash exits, measured by
    ``test_linux_a_setsid_child_ends_with_its_command``). It stays SANDBOX-CONFINED (crown jewels
    remain off-limits), so it is not a confinement breach, but it is unattended code with network +
    broad non-jewel authority. ⚠ **The K3 efferent gate does NOT bound this one** — it halts actions
    BEFORE they run, and a setsid survivor is already running. Since K4a ships scheduled seats, the
    old "human-in-the-loop bounds it now" is FALSE for an unattended seat: there is no human. The
    per-domain network policy (``spore-417``) remains the real answer.
  - NETWORK EXFIL of non-crown-jewel data: default-allow network + broad read means anything not
    crown-jeweled is exfiltratable — the SAME risk profile as CC. Mitigation is now the K3 gate for
    a GATED drive (bash is always efferent, so an exfil attempt halts before it runs) plus
    human-in-the-loop where a human is present — but NEITHER covers an ``efferent_gate: "ungated"``
    seat or a setsid survivor. Per-domain network gating is ``spore-417`` (NOT spore-295, which
    shipped as the gate and has composted).
  - IPC / LOCALHOST SIDE CHANNELS (apparatus L2, confirmed): default-allow leaves mach + loopback
    open, so the entity can drive an EXISTING unsandboxed daemon (``pbcopy``/``pasteboardd``) or hit a
    LOCAL service that re-exposes crown-jewel content (the argushub store on ``:8420``, the continuity
    digest pushed to Supabase) — the file-deny doesn't cover the socket. Spawning a NON-descendant
    unsandboxed helper via ``launchd`` was MEASURED REFUSED for ``launchctl submit`` and
    ``launchctl bootstrap gui/<uid>`` from a real SeatbeltProvider shell (Diogenes 2026-10-01;
    submit re-run by the levain seat the same day: rc=1 and no job ran inside, rc=0 and the job ran
    in the outside control); ``launchctl print`` still answers, so launchd is reachable but will not
    create jobs for a sandboxed caller. Other routes to an unsandboxed process (Apple Events to an
    app, say) were not tried.
    Same class as network-exfil: gated-drive halt + human-in-the-loop where present, ``spore-417``
    for the per-domain policy. ⚠ An unattended seat has no human in this class either.
    ⛔ sshd ON THIS HOST IS THE SHARPEST INSTANCE OF THIS CLASS, AND ``ssh_mode="agent"`` MAKES IT
    WORSE, NOT TIGHTER (spore-755, REPRODUCED end to end 2026-09-13 on 0.4.5 and HEAD). Agent-mode
    forwards ``SSH_AUTH_SOCK`` by design; so with Remote Login on and a key the agent holds in
    ``authorized_keys``, ``ssh localhost cat <jewel>`` has sshd — unsandboxed, root — read a file the
    floor denies, authorised by the very socket agent-mode forwards. The forwarded agent socket is
    the SOLE carrier (disable it and the attack fails "Permission denied (publickey)"; the key FILE
    stays floor-denied). This is spore-725's "a floor that forwards a credential to an unsandboxed
    root reader is decorative for that file" applied one step over, and it reaches the host by ANY of
    its own addresses (loopback AND the LAN IP — seatbelt's ``localhost`` keyword covers every
    local-interface address), not loopback only. CLOSED for the FRESH-CONNECTION case in BOTH
    ssh_modes by ``deny_localhost_outbound`` (renders ``(deny network-outbound (remote ip
    "localhost:*"))``), DEFAULT-ON at ``ConversationBinding.create`` (Phill 2026-09-13, 0.4.6 = A+B),
    operator opt-out ``allow_localhost_outbound``. Both modes because raw mode has the same
    fresh-connection exposure (codex L3 HIGH#1, REPRODUCED): the entity reads its OWN key but the
    OTHER jewels stay floor-denied and a local sshd reads THEM as root just the same. This deny also
    closes the local-service side channel (argushub:8420 etc.) in both modes, independent of ssh.
    ⚠ RESIDUAL for 0.5 — raw mode still lets the entity READ the operator's ~/.ssh key material
    directly (worse than the agent socket, which never yields the key). The loopback deny stops that
    key reaching a LOCAL sshd, but not a remote one; the 0.5 redesign (default F / D2, and retiring or
    redefining raw mode as scoped-credential-only) is the real fix. See spore-1005.
    ⛔ RESIDUAL, NOT CLOSED — THE FORWARDED AGENT IS A SIGNING ORACLE (codex L3 HIGH#2, and this is
    the deepest limit). It signs for ANY sshd that authorises the agent's key, reached through ANY hop
    the floor allows. A REMOTE relay defeats the localhost deny entirely: ``ssh -o ProxyCommand='ssh
    relay nc %h %p' <this-host>`` reaches this host's own sshd from the OUTSIDE, so the only connection
    the sandbox sees is to the (allowed) relay. A Tailscale peer is exactly such a hop — so a Tailscale
    address is NOT "covered" (an earlier note wrongly said it was). This is not closeable by a network
    deny; the real closures are agent MEDIATION (destination/host-key constraints) or not forwarding
    the agent. ⚠ On THIS machine the precondition is currently absent — ~/.ssh/authorized_keys does
    not authorise the agent's key (MEASURED 2026-09-13) — but that is a fact about this Mac, not the
    class. Tracked in spore-1005.
    ⚠ RESIDUAL, NOT CLOSED — ssh CONNECTION MULTIPLEXING (spore-755 follow-up, REPRODUCED
    2026-09-13). If a ssh ControlMaster/mux socket to LOCALHOST already exists (operator runs
    ``ControlMaster auto``/``yes`` and has a live master to this host) and the entity can NAME the
    ControlPath (it may read ``~/.ssh/config`` in agent mode, or guess a default/tmp path), then
    ``ssh -S <ctlpath> localhost cat <jewel>`` rides the EXISTING authenticated master: an AF_UNIX
    connect the IP deny above cannot see, and the master is already authenticated so disabling the
    agent does not help. Placing the control socket under the (denied) ``~/.ssh`` does NOT save it —
    the entity cannot ``ls`` the socket but CAN ``connect()`` to it (connect ≠ read, the docker-socket
    class). The only BLANKET floor-side block is denying all AF_UNIX outbound, which was MEASURED to
    break ``getaddrinfo``/HTTPS/git-https/pip (macOS resolves through an mDNSResponder unix socket;
    exempting that socket's literal did NOT restore resolution), so it is not the default. The
    practical precondition is narrow — ssh multiplexing to LOCALHOST specifically is rare. Scoped
    closure (a resolver-preserving deny, or forcing ``ControlPath=none`` in the entity's own ssh
    invocation) is a follow-up; see the handoff spore.
  - CROWN-JEWEL DIRECTORY NAMES leak: ``ls ~`` lists ``.anneal-memory`` as a NAME (the parent's
    metadata is allowed; the subtree's CONTENTS + the jewel dir's own stat are denied). Names are
    already public; informational.
  - FLOOR-EXPANSION (spore-322, Phill-ratified 2026-07-12): ``~/.ssh/authorized_keys`` WRITE is now in
    the UNIVERSAL floor (:data:`_SSH_AUTHORIZED_KEYS`, both ssh_modes — a persistence backdoor with zero
    legit use, so it is folded unconditionally). The other default-allow-readable standard cred stores
    — :data:`_STANDARD_CRED_SUBTREES` + :data:`_STANDARD_CRED_FILES` (a GitHub token = repo push/admin) — are ≥
    the ssh key in impact. They are not universal: ``build_policy`` folds them in on its
    ``deny_standard_creds`` switch, which :func:`levain.firing.drive.resolve_cred_floor` turns on in every
    drive except the interactive REPL; an entity that needs gh/aws there declares
    ``deny_standard_creds: false`` in confinement.json (operational-fit over purity). A
    caller can still pin any of them ad-hoc via ``deny_files`` / ``extra_deny_read_write``.

**Gating (v1 REALITY, load-bearing honesty — REWRITTEN 2026-07-29 for the post-K3 world).** This
floor protects the crown jewels and NOTHING else. With default-allow and no permission prompts
(Phill: people bypass those IRL), a confabulating open model can still ``rm -rf`` a real repo,
``git push --force``, or ``curl | bash`` — **none of which THE FLOOR stops.** That sentence is about
the floor, and about the floor it is still exactly true.

What covers the rest is a SEPARATE mechanism that did not exist when this paragraph was written: the
**efferent gate** (:mod:`levain.firing.gate`, shipped K3), which HALTS every efferent action — and
bash is ALWAYS efferent, because a shell command's effect is not knowable by reading it — whenever no
human is present. The gate binds to the ABSENCE of the human, so the three examples above resolve
differently per drive:

  - **REPL** (``levain run``, ``human_present=True``) → UNGATED. The examples are live, and the
    operator watching tool activity stream past IS the fan-in. "you-in-the-loop" is literal here.
  - **Headless** (``--task``, or a scheduled seat) → GATED: the turn halts BEFORE the action runs,
    reports what was held, and exits ``EXIT_GATED`` (4). Nothing is executed.
  - **``efferent_gate: "ungated"``** in ``confinement.json`` disarms it in BOTH cases — an explicit
    operator opt-out, and the one configuration in which an UNATTENDED entity really can do all
    three. Any surface claiming an entity is governed must RESOLVE that setting, never assume it.

⚠ **UNATTENDED OPERATION IS NOW A v1 CLAIM** (K4a — ``levain daemon install-seat`` runs a governed
seat on a schedule). This paragraph used to say the opposite, and said it correctly while the gate
was still a spec. What remains ABSENT is not the gate but the per-domain threshold POLICY
(``spore-417``): the stakes/context escalators, capability recalibration, and any GRADUATION to
unattended-efferent firing. **Nothing graduates; everything efferent gates.** So v1 = **structural
floor + the efferent gate + you-in-the-loop wherever you are present.**

Pure stdlib (``os`` / ``platform`` / ``subprocess`` / ``tempfile`` / ``pathlib`` / ``threading``) —
importing this pulls NO anneal and NO openhands, so the confinement core is unit-testable in complete
isolation (the same dependency-isolated-leaf discipline as ``levain.firing.isolation`` and
``levain.daemon``). The OpenHands ``LevainBashTool`` that consumes this is a SEPARATE slice, and lives
with the file-editor tool under ``levain.firing.openhands`` (which is where the ``openhands`` import
is allowed to land).
"""
from __future__ import annotations

import atexit
import codecs
import errno
import inspect
import json
import logging
import os
import platform
import re
import select
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
import weakref
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Literal, NoReturn

# The efferent gate's accepted settings, imported rather than restated: the config loader and the
# gate must agree on the vocabulary by construction, not by two lists staying in sync. Both modules
# are stdlib-only leaves, so this adds no dependency weight.
from levain.firing.gate import GATE_SETTINGS, GateSetting

__all__ = [
    "SANDBOX_EXEC",
    "BWRAP",
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
    "BwrapProvider",
    "select_provider",
    "sandbox_exec_available",
    "bwrap_available",
    "bwrap_netns_available",
    "resolve_localhost_deny",
    "confinement_supported",
    "ConfinementDiagnosis",
    "diagnose_confinement",
]

# The macOS seatbelt driver. An ABSOLUTE path (never a PATH lookup — a confined child must resolve
# the sandbox binary deterministically, and this is the OS-shipped location).
_log = logging.getLogger("levain.firing.confinement")

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# The Linux sandbox driver. ABSOLUTE for the same reason as ``SANDBOX_EXEC`` — a confined child must
# resolve the sandbox binary deterministically, never through a PATH the entity could influence.
# ⚠ LIMIT, NAMED: this is the Debian/Ubuntu/Fedora location. A distro that ships ``bwrap`` elsewhere
# (Nix, some source builds) reads as "no confinement available" and fails CLOSED — the entity loses
# bash and is told why. That is the correct direction to be wrong in; a PATH lookup is not.
BWRAP = "/usr/bin/bwrap"

SshMode = Literal["agent", "raw"]

# Backpressure bounds so a runaway producer (``yes``, ``tail -f`` left after a timeout) can't grow the
# PARENT process's memory without limit (apparatus L3 complement #4). Normal commands never hit these.
_MAX_OUTPUT_CHARS = 8 * 1024 * 1024   # per-command returned output cap (then truncate + mark)

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

# The entity's OWN store files under ``<entity>/.levain/``, WRITE-denied to its confined hands (read
# stays allowed). anneal writes these IN THE HOST PROCESS (capture/wrap), never through the sandboxed
# shell, so the deny fences only the entity's own hands — not the legitimate composer. Load-bearing
# because spore-359 folds ``memory.continuity.md`` into the always-loaded system suffix and per-turn
# recall reads ``memory.crystal.json``; ``memory.db`` is the episodic source the wrap consolidates FROM.
# A raw self-write to any of them is a poison-the-next-session vector that bypasses the GOVERNED wrap
# path. (Said "human-gated" until K4a [6]: a seat may now wrap UNATTENDED — bounded, and structurally
# refused from crystallizing — so what this deny protects is that the write went through anneal's
# VALIDATED composer at all, which was always the real property. Who invoked it is a separate axis.)
# The SQLite sidecars (``-wal``/``-shm``/``-journal``) are included because anneal opens the store in
# WAL mode (``PRAGMA journal_mode=WAL``): in WAL mode the logical DB is ``memory.db`` PLUS ``memory.db-
# wal`` — committed frames live in the WAL, so denying only the main file leaves the episodic source
# corruptible/poisonable through the sidecar (L3 codex, verified live: the sidecars were writable). The
# host writer is unaffected (it is not under the sandbox); a sandboxed ``sqlite3`` inspection that needs
# ``-shm`` write is the accepted tradeoff — the entity has no legit reason to write its own store.
_OWN_MEMORY_FILENAMES = (
    "memory.continuity.md", "memory.crystal.json", "memory.db",
    "memory.db-wal", "memory.db-shm", "memory.db-journal",
)

# Standard, tool-CANONICAL credential locations. Folding these into the floor is an OPT-IN
# (``deny_standard_creds`` in ``confinement.json``) at THIS layer; the drive layer
# (levain.firing.drive.resolve_cred_floor) turns it on unless a human drives the session. Two reasons:
# (1) unlike an app secret (``.env.flow``), these ARE structurally knowable — gh / aws / curl look
# here BY DEFINITION, so naming them is not the FALSE-SECURITY guessing the module otherwise refuses;
# (2) denying their READ breaks the entity's OWN legitimate ``gh`` / ``aws`` / netrc-``curl`` use, so the
# switch stays per-entity: an entity that needs those tools outside the REPL declares
# ``deny_standard_creds: false``. They are >= the ssh key in impact (a gh token = repo push/admin; aws
# creds = infra/$), which is why the drive layer denies them by default wherever no human watches. ``~/.config/gh`` is a DIR (the token
# lives in ``hosts.yml``) → a denied subtree; ``credentials`` / ``.netrc`` are files.
# The Keychain's mach services (spore-1245). SecurityServer + securityd.xpc were MEASURED to carry the
# reads (denying them refused `security -w` and the osxkeychain helper); systemkeychain is Apple's
# separate System-keychain endpoint (codex L3), denied by name, not exercised.
_KEYCHAIN_MACH_SERVICES = (
    "com.apple.SecurityServer",
    "com.apple.securityd.xpc",
    "com.apple.securityd.systemkeychain",
)


def cred_floor_label(system: str | None = None) -> str:
    """What the standard cred floor covers on this OS, for banners: the macOS Keychain is folded in
    by the Seatbelt profile (``CrownJewelsPolicy.deny_keychain``); Linux has no counterpart."""
    import platform as _platform

    # Derived from the two tuples the floor enforces, so the banner cannot name fewer stores than
    # the floor denies (it once omitted both git credential files).
    base = " · ".join((*_STANDARD_CRED_SUBTREES, *_STANDARD_CRED_FILES))
    return base + (" · the Keychain" if (system or _platform.system()) == "Darwin" else "")


_STANDARD_CRED_SUBTREES = (
    "~/.config/gh",                 # gh OAuth token (hosts.yml) → repo push/admin
    # Cloud CLI credential caches (lane P2 item 4). Each holds a live token the CLI writes itself:
    "~/.aws/sso/cache",             # IAM Identity Center tokens (botocore _SSO_TOKEN_CACHE_DIR)
    "~/.aws/cli/cache",             # assumed-role temporary credentials (aws-cli assumerole CACHE_DIR);
                                    # not ~/.aws/cli itself, which holds `alias`
    "~/.aws/login/cache",           # `aws login` tokens (or $AWS_LOGIN_CACHE_DIRECTORY, below)
    "~/.aws/boto/cache",            # botocore JSONFileCache (assume-role / SSO credentials)
    "~/.config/gcloud",             # gcloud's credential databases and ADC (or $CLOUDSDK_CONFIG)
    "~/.azure",                     # az's MSAL token cache and service principals (or $AZURE_CONFIG_DIR)
)
# Credential directories that are a tool's WHOLE home. On Linux an absent one is created (0700,
# recorded in the placeholder ledger and removed at close if still empty) and masked like every
# other absent jewel directory, so the shell can neither plant a config there nor read what an
# operator's `gcloud auth login` writes there mid-session (L2 r1). One whose parent this user cannot
# write is skipped: the shell cannot create it either. macOS denies them whether or not they exist.
_PRESENT_ONLY_CRED_DIRS = ("~/.config/gcloud", "~/.azure")
# The tools' own credential-location overrides (L1 r1), read from the environment when the policy
# is built: (variable, "dir" | "file", the path(s) its value names). Each is denied IN ADDITION to
# the default location, which can still hold what was written before the override was set.
_CRED_OVERRIDES: tuple[tuple[str, str, str], ...] = (
    ("AWS_LOGIN_CACHE_DIRECTORY", "dir", "{}"),
    ("CLOUDSDK_CONFIG", "dir", "{}"),
    ("AZURE_CONFIG_DIR", "dir", "{}"),
    ("GH_CONFIG_DIR", "dir", "{}"),
    ("XDG_CONFIG_HOME", "dir", "{}/gh"),
    ("XDG_CONFIG_HOME", "file", "{}/git/credentials"),
    ("AWS_SHARED_CREDENTIALS_FILE", "file", "{}"),
    ("NETRC", "file", "{}"),
    ("NPM_CONFIG_USERCONFIG", "file", "{}"),
    ("DOCKER_CONFIG", "file", "{}/config.json"),
    ("KUBECONFIG", "file", "{list}"),       # a colon-separated list: every entry
)
_CRED_DIR_ENV = tuple(v for v, kind, _ in _CRED_OVERRIDES if kind == "dir")


def _git_store_files(config: Path) -> list[Path]:
    """The files a ``credential.helper = store --file <path>`` in git config ``config`` names."""
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        m = re.match(r"\s*helper\s*=\s*(.*)$", line)
        if not m:
            continue
        value = m.group(1).strip()
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        try:
            words = shlex.split(value)
        except ValueError:
            continue
        if not words or words[0] != "store":
            continue
        for k, w in enumerate(words):
            if w.startswith("--file="):
                out.append(Path(os.path.expanduser(w[len("--file="):])))
            elif w == "--file" and k + 1 < len(words):
                out.append(Path(os.path.expanduser(words[k + 1])))
    return out


def _cred_overrides(environ=None) -> tuple[list[Path], list[Path]]:
    """``(dirs, files)`` the credential-location overrides in ``environ`` name: absolute paths only (a
    relative value would be resolved against whatever directory levain was started in). Includes the
    store files a ``GIT_CONFIG_GLOBAL`` config names."""
    env = os.environ if environ is None else environ
    dirs: list[Path] = []
    files: list[Path] = []
    for var, kind, form in _CRED_OVERRIDES:
        value = env.get(var, "")
        values = value.split(os.pathsep) if form == "{list}" else [value]
        for v in values:
            v = os.path.expanduser(v)
            if not v or not os.path.isabs(v):
                continue
            path = Path(v if form == "{list}" else form.format(v))
            (dirs if kind == "dir" else files).append(path)
    git_global = os.path.expanduser(env.get("GIT_CONFIG_GLOBAL", ""))
    if git_global and os.path.isabs(git_global):
        files += [f for f in _git_store_files(Path(git_global)) if f.is_absolute()]
    return list(_dedup_paths(dirs)), list(_dedup_paths(files))


def _cred_dir_sources() -> list[Path]:
    """The credential directories that are a tool's whole home, lexical: the defaults and every
    override (:data:`_CRED_DIR_ENV`)."""
    home = Path.home()
    return list(_dedup_paths([home / d[2:] for d in _PRESENT_ONLY_CRED_DIRS] + _cred_overrides()[0]))


_STANDARD_CRED_FILES = (
    "~/.aws/credentials",           # aws access key/secret. NOT ~/.aws/config — it holds region /
                                    # profile / SSO the entity legitimately needs; an operator whose
                                    # ~/.aws/config carries a secret pins it ad-hoc via deny_files.
    "~/.netrc",                     # netrc PAT / http creds (git-https, curl)
    "~/.git-credentials",           # git HTTPS PAT store (same class as ~/.netrc — apparatus L1)
    "~/.config/git/credentials",    # XDG-path git credential store
    "~/.pypirc",                    # PyPI upload password/token: publish rights (supply-chain grade)
    "~/.npmrc",                     # npm registry _authToken / _auth: publish rights
    "~/.docker/config.json",        # registry `auths` when no credsStore helper holds them
    "~/.kube/config",               # cluster credentials (client certs, bearer tokens, exec plugins)
)

# The tool directories that hold a standard cred file. On Linux each is mounted READ-ONLY with its
# existing subdirectories bound back read-write (step (2a) of the bwrap plan, the construct the
# entity's own .levain store uses), so an absent cred file in one needs no mountpoint on the host
# and a link in one cannot be swapped from inside. A file planted at an absent cred path is a real
# vector: kubeconfig ``users[].exec`` and docker ``credHelpers`` run a command when the operator
# next uses the tool. Cost: inside a confined session the entity cannot create or rewrite a top-
# level file in these directories (``aws configure set``, ``kubectl config use-context``,
# ``git config --global`` for an XDG git user). macOS denies a path string whether it exists or
# not, so it needs none of this. $HOME itself is such a view on Linux (step (0) of the bwrap plan),
# so an absent $HOME-level cred file needs no placeholder either.
_CRED_TOOL_DIRS = ("~/.kube", "~/.docker", "~/.aws", "~/.config/git")


def floor_roots(specs) -> list[Path]:
    """THE way a list of jewel roots enters the floor, as data: each ``~/rel`` or absolute path at
    its three spellings (:func:`_spellings`). The standard cred stores, the cloud directories, the
    credential overrides and the ledger go through it; a new list (browser profiles) is one more
    tuple passed here, and the plan's absent-path handling (a tool directory read-only, an absent
    directory created and ledgered, a $HOME-level file absent from the $HOME view, a link refused
    where it could be swapped) applies to it unchanged."""
    return [p for spec in specs for p in _spellings(spec)]


def _secret_files() -> list[Path]:
    try:
        from levain.launch import secret_files
    except ImportError as exc:   # fail closed: a key file the floor cannot learn of is not left open
        raise ConfinementError(f"could not learn which secret files to deny ({exc})") from exc
    return secret_files()


def _spellings(spec: str | Path) -> list[Path]:
    """A jewel path at the three spellings the ssh vectors have used since 2026-08-21: the raw
    ``Path.home()`` form (the true lexical path), resolved HOME plus the unresolved rest, and the
    fully resolved target. ``spec`` is ``~/rel`` or an absolute path (then the middle spelling is the
    resolved parent plus the unresolved final component). Duplicates collapse, so in the ordinary
    no-symlink case this is one path.

    Why all three: Seatbelt canonicalises a path for data operations, but ``unlink``/``rename``/``ln``
    act on the LINK and are matched lexically, so under a resolved-only deny ``rm -f <link>`` succeeds
    and the recreated file is an object nothing names (the ssh finding, see ``build_policy`` (3)). On
    Linux the lexical spelling is what lets the bwrap plan see that a jewel is a link at all.
    Use it for every list of home-relative jewels (ssh, the standard cred stores, browser profiles),
    so the next list cannot repeat the resolved-only miss."""
    s = str(spec)
    if s == "~" or s.startswith("~/"):
        raw = Path.home() / s[2:]
        candidates = [lambda: Path.home().resolve() / s[2:], raw.resolve]
    else:
        raw = Path(s)
        candidates = [lambda: raw.parent.resolve() / raw.name, raw.resolve]
    out = [raw]
    for spell in candidates:
        # A symlink loop must not crash the policy build (L1 r1). The raw spelling stays denied (on
        # macOS a literal deny of a path no lookup can finish), and the Linux plan, which resolves
        # every root again, refuses bash with the reason.
        try:
            out.append(spell())
        except (OSError, RuntimeError):
            pass
    return list(_dedup_paths(out))

# CONTAINER / VM DAEMON SOCKETS — folded into the UNIVERSAL floor, default ON (spore-725).
#
# ⛔ A REACHABLE CONTAINER DAEMON IS A TOTAL BYPASS OF EVERYTHING ELSE IN THIS MODULE, and it
# was CONFIRMED END-TO-END on the shipping build (2026-09-03, re-reproduced independently
# 2026-09-04 at 1e44342): with the floor denying a jewel by path, one command returns it —
#     docker run --rm -v <jewel>:/x:ro alpine cat /x
# The daemon runs as root and was NEVER IN OUR SANDBOX, so no path deny, no mount trick and no
# namespace can hide anything from it. It does not escalate and needs no exotic technique: the
# entity simply asks a more privileged process to read the file for it.
#
# ⚠ WHY THIS IS AN ENUMERATION WHEN THE REST OF THE FLOOR REFUSES TO GUESS. The ~/.ssh floor is
# LOCATION-based precisely because name-based guessing is false security. These paths are the
# same KIND of knowable location as the standard cred stores (:data:`_STANDARD_CRED_FILES`) —
# a client looks here BY DEFINITION or it cannot find its daemon — but the list is still
# necessarily incomplete: a custom $DOCKER_HOST, a k8s/containerd/lima/OrbStack socket, or a
# TCP endpoint are all outside it. THEREFORE THE BANNER NAMES WHICH SOCKETS ARE COVERED AND
# NEVER CLAIMS CONTAINERS ARE FENCED. An unqualified claim here would be worse than the hole.
#
# ⛔⛔ AND THE INCOMPLETENESS IS CURRENTLY **NON-REMEDIABLE BY THE OPERATOR** — SAY SO, BECAUSE
# THE OBVIOUS REMEDY IS MEASURED-INERT (glm-5.3 L3, 2026-09-04). This module's documented answer
# for every OTHER un-enumerated jewel is "pin it via ``deny_files``" — it says exactly that for a
# custom ``AuthorizedKeysFile``. For a SOCKET that advice silently fails: ``deny_files`` renders
# only ``(deny file-read* file-write* (literal ...))``, and this very field's comment records the
# measurement that such a rule DOES NOT BLOCK ``connect()``. So an operator running OrbStack, a
# non-default colima profile (``colima start -p work``), or any socket outside the eight would
# pin it, receive the rename- and ancestor-denies, and get **no connect protection at all while
# believing it closed** — a false closure, which is worse than a known gap.
# ⛔ AND THE ENUMERATION IS A **SPAWN-TIME SNAPSHOT** — WAS A BUILD-TIME ONE, WHICH WAS THE
# codex L3 HIGH OF 2026-09-04 (CONFIRMED BY EXECUTION, FIXED HERE — spore-768). ``deny_sockets``
# stores each path RESOLVED. If a listed path is ABSENT at resolution it resolves to itself; if
# something later creates it as a symlink to an UNLISTED target, ``connect()`` canonicalises to that
# target — absent from the set — and a lexical ``network-outbound`` literal is MEASURED INERT.
# Measured end to end: a listed-but-absent path, later symlinked to an unlisted socket, was reachable
# THROUGH THE LISTED NAME.
# ▶ ``refresh_socket_denies`` now re-runs the resolution AT EVERY SHELL SPAWN (the seam is
# ``ConfinementProvider.spawn_shell``, made a template method so a provider cannot skip it), UNIONING
# the fresh targets into the build-time set — union, so a re-resolution can only ADD a deny, never
# drop one. That closes the build→spawn window, which is the long one: the policy is built at
# tool-creation time and the shell spawns lazily, then RE-spawns after every ``exit``.
# ⚠ **IT IS A SNAPSHOT, NOT COVERAGE, AND THE RESIDUAL IS IRREDUCIBLE HERE.** A seatbelt profile is
# fixed when the process starts, so a symlink created at a listed path AFTER the shell spawns is
# still not covered — and no rule in this profile language would cover it, because seatbelt
# CANONICALISES for ``network-outbound``: a deny can name a TARGET, never a NAME. There is no
# expressible "whatever this path points at, at connect time". A spawn-time snapshot is the
# strongest statement available, and it is stated as a snapshot for that reason.
# ⚖ WHY THAT RESIDUAL IS TOLERABLE, measured rather than argued:
#   · THE ENTITY CANNOT ARRANGE IT. Creating the symlink at a listed path is refused — "Operation not
#     permitted" — because arm (ii) write-denies every listed socket at BOTH spellings. It needs an
#     EXTERNAL actor (an operator, an installer) acting mid-session.
#   · AND THE UNLISTED SOCKET WAS ALREADY REACHABLE UNDER ITS OWN NAME, symlink or not — verified in
#     the same run. So the symlink adds no reachability that the unlisted socket did not already have,
#     and which the banner already declares uncovered. Marginal exposure was zero; the COST WAS THE
#     CLAIM, an operator reading a listed socket as covered when under that condition it was not.
# ▶ There is deliberately NO operator-declared socket list yet: ``ConfinementConfig`` carries only
# the all-or-nothing ``allow_container_sockets``. Adding a ``deny_sockets`` config key (resolved
# into ``sockets_l`` AND both spellings into ``deny_write_files_l``, so operator sockets get the
# identical three arms) is the right fix and is FILED FORWARD, not done inside a release freeze.
# Until it lands, the honesty floor is this paragraph plus the banner line.
_CONTAINER_DAEMON_SOCKETS = (
    "/var/run/docker.sock",                 # the canonical Docker/OCI endpoint (often a symlink)
    "~/.docker/run/docker.sock",            # Docker Desktop for Mac — what /var/run resolves TO
    "/run/docker.sock",                     # systemd-era Linux spelling of the same
    "~/.colima/default/docker.sock",        # colima, default profile
    "/run/podman/podman.sock",              # podman, rootful
    "~/.local/share/containers/podman/machine/podman.sock",   # podman machine (rootless)
    "/var/run/containerd/containerd.sock",  # containerd directly — below docker, same authority
    "/var/run/crio/crio.sock",              # CRI-O
    # ROOTLESS LINUX (K4c). These are the sockets an ENTITY CAN START ITSELF — both are systemd
    # --user socket units, so `systemctl --user start podman.socket` is one command from inside the
    # sandbox — which makes them the sharpest Linux instances, not the rare ones. An unset
    # $XDG_RUNTIME_DIR (every macOS host) drops the entry instead of denying a literal "$XDG..." path.
    "$XDG_RUNTIME_DIR/podman/podman.sock",  # podman, rootless
    "$XDG_RUNTIME_DIR/docker.sock",         # Docker, rootless
    "~/.docker/desktop/docker.sock",        # Docker Desktop for Linux
)


def _runtime_dirs() -> list[str]:
    """Every user runtime dir a rootless socket may live in. ``$XDG_RUNTIME_DIR`` when it is set and
    absolute, and on Linux ALSO the systemd default ``/run/user/<uid>``: a ``systemctl --user``
    socket lives there whatever the launching shell's variable says (a nix or container shell can
    point it elsewhere), and under cron or ``su`` the variable is missing entirely while the socket
    is not (L1, L2 and complement L3 r1, 2026-09-30). None of it off Linux unless the variable is set."""
    out: list[str] = []
    value = os.environ.get("XDG_RUNTIME_DIR", "")
    if value and os.path.isabs(value):
        out.append(value)
    if platform.system() == "Linux":
        default = f"/run/user/{os.getuid()}"
        if default not in out:
            out.append(default)
    return out


def _expand_socket_source(spec: str) -> list[Path]:
    """One roster entry as the paths it names; empty when it names a runtime dir this host does not
    have — a path still containing ``$`` would deny a file nobody can create, and read as cover."""
    if spec.startswith("$XDG_RUNTIME_DIR"):
        return [Path(r + spec[len("$XDG_RUNTIME_DIR"):]) for r in _runtime_dirs()]
    expanded = os.path.expandvars(spec)
    if "$" in expanded:
        return []
    return [Path(expanded).expanduser()]


class ConfinementError(RuntimeError):
    """The confinement could not be established or is unavailable on this platform. FAIL-CLOSED:
    a caller that cannot build the floor must refuse to grant bash hands, never fall through to an
    unconfined host shell (``structural_invariants_beat_discipline``)."""


class FloorRefreshError(ConfinementError):
    """The floor's evolving denies could not be re-derived (a trust file naming a store that cannot be
    hidden, an unreadable trust path). Distinct from a shell that failed to start, so a caller can
    record it as a refusal of the whole floor without replaying the derivation (codex L3 r5)."""


# --- the OS-agnostic policy (the canonical object) -------------------------------------------

@dataclass(frozen=True)
class CrownJewelsPolicy:
    """OS-agnostic description of the confinement FLOOR: default-allow, DENY the crown jewels.

    A :class:`ConfinementProvider` renders this into the platform's native sandbox profile. Build it
    with :func:`build_policy`, never by hand — the crown-jewels assembly (flow store + creds + sibling
    stores + ssh keys) is shared across OSes and is the load-bearing security surface.

    Paths are stored ABSOLUTE, and MOSTLY resolved (symlink-followed) so the rendered denies match
    what the kernel sees. ⚠ THE EXCEPTIONS ARE DELIBERATE (it once read "Every path is stored
    RESOLVED", which the 2026-08-21 change falsified and codex L3 caught): each ssh vector in
    ``deny_write_files``, and each standard cred store in ``deny_files`` / ``deny_read_write``, is
    stored at up to THREE spellings (:func:`_spellings`) — raw ``Path.home()``, resolved-HOME, and
    fully resolved — because resolving alone LOSES the lexical path, which link operations match
    and which sshd honours through ``realpath()``. ``deny_write_dirs`` carries the lexical ancestors
    too. See ``build_policy`` (3). ``workspace`` is the shell's starting cwd + a definitely-writable root; it is NOT a
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
    # relocated to dodge the literal — the codex raw-mode bypass. ⚠ EACH VECTOR APPEARS TWICE WHEN A
    # SYMLINK MAKES ITS LEXICAL AND RESOLVED PATHS DIFFER (build_policy (3)) — the dir pin does NOT
    # cover a symlinked FILE, since writes INSIDE ~/.ssh stay allowed by design. Distinct from deny_files (read+write);
    # the in-process file-editor twin denies them outright (read included, stricter-on-ssh).
    config_file: Path | None = None     # <entity>/.levain/confinement.json — the config that DEFINES
    # the floor is itself floor-protected (read+write denied), so the entity can't rewrite its own jail
    # (apparatus L1 — confinement.json lives in the entity-writable .levain/; a self-authored
    # ssh_mode="raw" / emptied deny_files would weaken the NEXT session's operator-declared floor).
    deny_sockets: tuple[Path, ...] = ()  # container/VM DAEMON sockets, RESOLVED (spore-725).
    # ⛔ A SEPARATE FIELD FROM ``deny_files`` ON PURPOSE, AND THE REASON IS THE WHOLE FINDING:
    # the two need DIFFERENT RENDERING VERBS. A seatbelt ``file-read* file-write*`` deny does
    # NOT block ``connect()`` to a unix socket — MEASURED 2026-09-04: with both docker socket
    # spellings in ``deny_files``, the profile emitted the literals, read clean in review, and
    # the attack still returned the jewel. Blocking the connect needs ``network-outbound``.
    # Folding sockets into ``deny_files`` would put them in a field whose renderer cannot deny
    # them — a guard that cannot see its own subject, in the one module where that is fatal.
    #
    # ✅ CASE AND UNICODE NORMALISATION ARE **NOT** A HOLE HERE — MEASURED 2026-09-04, because a
    # reviewer reasonably raised it: this module built ``_canon``/``_ci_within`` precisely
    # because ``Path.resolve()`` folds NEITHER, so a case- or NFD-variant path reaches the same
    # file while comparing unequal — and nothing casefolds these SBPL literals. On a
    # case-insensitive APFS volume (verified as the precondition), against a live socket with
    # only the exact resolved literal denied: ``TEST.sock`` REFUSED, ``Test.Sock`` REFUSED, an
    # NFD spelling of an NFC-bound socket REFUSED (the NFD path confirmed present on disk), and
    # the host-side connect still succeeded throughout, so the refusals are the sandbox's doing
    # and not a dead socket. **Seatbelt folds case and normalisation for network-outbound.**
    # ⛔ DO NOT ADD A REGEX BELT FOR THIS. It was proposed and is a measured no-op; a regex here
    # would be a second, weaker matcher over the one deny in this profile where a miss is total.
    #
    # ⚠ RESOLVED ONLY, and this is the opposite of ``deny_write_files`` two lines up. Seatbelt
    # CANONICALISES the path for network-outbound (as it does for read/write data ops), so a
    # resolved-only deny covers every symlinked spelling — MEASURED: denying only the LEXICAL
    # /var/run/docker.sock blocks NOTHING, not even a connect naming that exact path; denying
    # only the resolved ~/.docker/run/docker.sock refuses BOTH. A lexical socket literal is not
    # redundant-but-harmless, it is INERT, and a maintainer who adds one may believe it is the
    # entry doing the work.
    #
    # ⛔⛔ THE NETWORK DENY ALONE IS DEFEATED BY ``mv`` — the socket is ALSO write-denied (both
    # spellings, via ``deny_write_files``) and its ancestors write-denied (via ``all_jewels``).
    # MEASURED: under a network-only deny, ``mv sock moved.sock`` then connecting to the new
    # path CONNECTS, because after the rename the canonical path is one nothing names and the
    # daemon keeps serving the same listening inode. Isolated further: the socket LITERAL
    # write-deny is what stops that (a parent-DIR write-deny does NOT — renames inside a dir
    # stay legal by design), and the ancestor deny is separately required, because with only
    # the literal denied, renaming the PARENT DIR relocates the socket and connects. This is
    # the ``_write_deny_ancestors`` rename-relocation class (apparatus L2) applying to sockets.
    # ⚠ ALL THREE ARMS ARE LOAD-BEARING; each was falsified on its own before being kept.
    socket_spellings: tuple[Path, ...] = ()  # BOTH spellings of every denied socket — MESSAGE
    # CLASSIFICATION ONLY, never enforcement. `crown_jewel_reason` uses it to tell a socket from
    # an ssh vector without calling `.resolve()` inside the predicate, where a resolution error
    # would escape the fail-closed guard. Enforcement remains `deny_sockets` (resolved, arm i)
    # plus `deny_write_files` (both spellings, arms ii/iii).
    own_memory_files: tuple[Path, ...] = ()   # <entity>/.levain/memory.{continuity.md,crystal.json,db}
    # — the entity's OWN consolidated memory, crystal store, and episodic store: WRITE-denied (read
    # stays allowed on the seatbelt hand → the entity can `cat` its own memory), because spore-359 folds
    # memory.continuity.md into the always-loaded system_message_suffix and per-turn recall reads
    # memory.crystal.json — so a raw file-write by the entity's OWN confined hands would poison the
    # highest-trust surface on a FUTURE session, BYPASSING the GOVERNED `levain wrap` (said
    # "human-gated" until K4a [6] — a seat may now wrap unattended; the invariant is that ONLY the
    # validated composer writes these, never that a human ran it). Only the
    # HOST-process capture/wrap (anneal in-process, NOT the sandboxed shell) may compose them; write-
    # denying the sandboxed hands makes "only wrap composes the always-loaded memory" a STRUCTURAL
    # invariant, not a docstring (L2 domain review, 2026-07-17). Distinct from config_file (read+write
    # denied — the entity may not even READ its jail definition); here read is intentionally allowed.
    # ⛔ APPENDED LAST, AND THE POSITION IS LOAD-BEARING (codex L3 #4, 2026-09-04). This field was
    # first inserted between `socket_spellings` and `own_memory_files`, which silently shifted every
    # later field's POSITIONAL index in an exported, non-kw-only dataclass. Code built against the
    # previous signature that passed `own_memory_files` positionally would have landed the entity's
    # own memory paths in `socket_sources` — so the memory files would be treated as socket sources
    # (resolved and network-denied) while losing their write-deny, which is the poison-the-
    # always-loaded-memory vector `own_memory_files` exists to close. A new field on a public
    # dataclass goes at the END unless the whole class is made keyword-only in a deliberate break.
    socket_sources: tuple[Path, ...] = ()  # the LEXICAL (expanduser'd, UN-resolved) socket paths
    # that DEFINE the socket floor — the authority `deny_sockets` is derived FROM, kept so the
    # derivation can be RE-RUN. Empty when `allow_container_sockets=True` (opt-out) — which is why
    # this field exists rather than re-reading `_CONTAINER_DAEMON_SOCKETS` at spawn: the opt-out
    # must survive into the refresh, and inferring it from an empty `deny_sockets` would be a proxy.
    #
    # ⛔ WHY A SEPARATE FIELD AT ALL — THE TOCTOU (codex L3 HIGH, 2026-09-04; spore-768).
    # `deny_sockets` is RESOLVED, and this policy is built at TOOL-CREATION time while the confined
    # shell is spawned LAZILY afterwards (`SandboxedBashExecutor._ensure_shell`) and RE-spawned
    # after every `exit`/reset. A path resolved when the shell does not yet exist can stop being
    # true before the shell that the resolution fences ever starts: a listed socket ABSENT at build
    # resolves to ITSELF, and a later symlink at that path makes seatbelt canonicalise `connect()`
    # to a target absent from `deny_sockets` — REPRODUCED end to end 2026-09-04, the connect
    # THROUGH THE LISTED NAME returned the payload.
    # ⚠ THE ORDINARY "operator starts Docker after the policy was built" CASE IS **NOT** AN
    # INSTANCE, and a first draft of this comment said it was. `/var/run/docker.sock` resolves to
    # `~/.docker/run/docker.sock` (or `~/.colima/default/docker.sock`), and BOTH are themselves
    # entries in `_CONTAINER_DAEMON_SOCKETS` — the enumeration lists the canonical name AND what it
    # resolves to, so the union already covers that. Checked against the list rather than assumed.
    # The refresh is for a listed name pointing at a target the enumeration does NOT hold.
    # `refresh_socket_denies` re-runs the derivation from THIS field at spawn. See it for what the
    # refresh does and does not buy — the residual window is stated there, not papered over.
    # NOTE: network is default-ALLOWED (a CC replacement hits the network); there is deliberately NO
    # `allow_network` knob — an unwired boolean would be false security (the exact claim>enforcement
    # gap this module refuses). Network POLICY is a slice-3 / threshold-membrane concern. The ONE
    # targeted network deny is `deny_localhost_outbound` below — WIRED (it renders and enforces),
    # not an `allow_network`-style promise, and off by default so the connect-to-self side-channel
    # is closed only where a drive-layer caller opts in.
    deny_keychain: bool = False   # macOS: deny the Keychain services (spore-1245), set from the SAME
    # resolved switch as the standard cred stores (``deny_standard_creds``: denied by default in every
    # drive except the interactive REPL, per-entity overridable), so it is never on while a human drives
    # at the REPL unless the operator asks. Measured 2026-10-01 from inside the confined shell, before this flag:
    # ``security find-generic-password -w`` and ``git credential-osxkeychain get`` both read secrets.
    # No Linux counterpart is rendered: no Secret Service existed on any host available to test.
    deny_localhost_outbound: bool = False  # deny outbound connect() to THIS host — the spore-755
    # class, for a FRESH connection. If this host runs sshd (Remote Login) and it authorises a key
    # the entity can use, ``ssh localhost cat <jewel>`` has sshd — root, OUTSIDE the sandbox — read a
    # file the floor denies. REPRODUCED end to end 2026-09-13 on 0.4.5 and HEAD. In ``ssh_mode="agent"``
    # the carrier is the forwarded ``SSH_AUTH_SOCK`` (proven sole carrier: disable it and the attack
    # fails "Permission denied (publickey)"). This flag is mode-AGNOSTIC — it renders the deny
    # whenever True — and its CALL SITE (`ConversationBinding.create`) sets it in BOTH ssh_modes (Phill
    # 2026-09-13, 0.4.6 = A+B). codex L3 HIGH#1 REPRODUCED a raw-mode variant (the entity reads its own
    # key, but the OTHER jewels — ~/.anneal-memory, sibling stores, deny_files — stay floor-denied and
    # a local sshd reads THEM as root just the same), so both modes get the deny.
    # Same class as the container-daemon socket (spore-725): a floor
    # that denies the FILE but lets an unsandboxed root reader be reached is decorative for that file.
    # Renders ``(deny network-outbound (remote ip "localhost:*"))``.
    # ⚠ MEASURED SEMANTICS, not the name's face value: seatbelt's ``localhost`` keyword matches
    # EVERY address bound to a LOCAL INTERFACE on this host — 127.0.0.1, ::1, AND the machine's own
    # LAN address (MEASURED 2026-09-13 against a throwaway sshd bound on all interfaces) — while
    # leaving REMOTE hosts reachable (github.com:443 still connected). So this closes the FRESH-
    # connection self-sshd path by ANY of the host's own addresses, not loopback only.
    # Per-port / per-IP granularity is IMPOSSIBLE here: ``(remote ip "127.0.0.1:22")`` is a hard
    # sandbox error ("host must be * or localhost"), so the ruled ":22-only" deny is unbuildable and
    # the real choice is all-local-outbound or nothing. COST when opted in: the entity loses ALL
    # in-floor connect() to local services it might legitimately use (a dev server it started,
    # argushub on :8420) — MEASURED denied. Does NOT touch remote network.
    # ⛔ WHAT THIS DOES NOT CLOSE (codex L3 HIGH#2, and it is NOT closeable by a localhost deny): the
    # forwarded agent is a SIGNING ORACLE. It signs for ANY sshd that authorises the agent's key,
    # reached through ANY hop the floor allows — a REMOTE relay/ProxyCommand (`ssh -o
    # ProxyCommand='ssh relay nc %h %p' <this-host>`) reaches this host's own sshd from the outside,
    # so the connection the sandbox sees is to the (allowed) relay, not to a local address. A Tailscale
    # 100.x peer is exactly such a hop. So a Tailscale address is NOT "covered" — an earlier comment
    # wrongly claimed it was. The honest closure is agent MEDIATION (destination/host-key constraints)
    # or not forwarding the agent, not this deny. Also not covered: an off-interface address (NAT
    # hairpin to the router's external IP). And ssh CONNECTION MULTIPLEXING — a live localhost
    # ControlMaster socket the entity can name (AF_UNIX, unseen by this IP deny). Both tracked in
    # spore-1005 / the honest-limits block above.
    sqlite_sidecars: tuple[Path, ...] = ()  # the ``-wal``/``-shm``/``-journal`` paths of every
    # non-directory jewel, a subset of ``deny_files``. A WAL-mode SQLite store keeps committed frames
    # that have not been checkpointed in ``<db>-wal``, so denying only the main file left them
    # readable and writable (Diogenes MEDIUM 2026-10-02, run on both providers). Seatbelt denies
    # them by name whether or not they exist. On Linux a mount cannot cover a sidecar created after
    # spawn, so a jewel that is a SQLite database refuses bash there instead
    # (:func:`_refuse_plantable_sqlite_jewels`); bwrap mounts only the sidecars present at spawn.
    trust_spellings: tuple[Path, ...] = ()  # the derive-trust file's write-denied spellings
    # (spore-1308) — MESSAGE CLASSIFICATION ONLY, like `socket_spellings`; enforcement is
    # `deny_write_files`. ⚠ NEW FIELDS GO AT THE END: one inserted earlier shifts every later field
    # for a positional caller (codex, L3 2026-10-02, reproduced; repeated by spore-1308's first cut,
    # L1 2026-10-03). tests/test_floor_project_memory.py freezes the order.
    ro_tool_dirs: tuple[Path, ...] = ()  # the standard cred files' tool directories
    # (:data:`_CRED_TOOL_DIRS`, raw ``Path.home()`` spelling), set with ``deny_standard_creds``. Linux
    # only: the bwrap plan mounts each read-only and binds its existing subdirectories back read-write,
    # creating an absent one at 0700 first. Seatbelt needs no counterpart (it denies absent paths).
    cred_dir_sources: tuple[Path, ...] = ()  # the credential directories that are a tool's whole home
    # (:func:`_cred_dir_sources`: ~/.config/gcloud, ~/.azure, and every directory override), lexical.
    # Their spellings are in ``deny_read_write``; this tells the Linux plan which absent roots may be
    # skipped when this user cannot create them. Empty unless ``deny_standard_creds``.
    trusted_roots: tuple[tuple[Path, Path], ...] = ()  # (as spelled, real path) for the workspace,
    # the entity dir and $HOME, resolved ONCE here. A symlink above one of them is the operator's,
    # not the entity's: the file editor's walk (levain.firing.openhands.tools) maps a path under one
    # to its real spelling and then follows no symlink below it (head ruling 2026-10-07).


def _trusted_roots(entity_dir, workspace) -> tuple[tuple[Path, Path], ...]:
    """``(as spelled, real)`` for the workspace, the entity dir and $HOME, longest first."""
    ed = Path(os.path.abspath(os.path.expanduser(str(entity_dir))))
    ws = (Path(os.path.abspath(os.path.expanduser(str(workspace)))) if workspace is not None
          else ed / "workspace")
    out: dict[Path, Path] = {}
    for lex in (ws, ed, Path(os.path.abspath(os.path.expanduser("~")))):
        real = lex.resolve()
        out[lex] = real
        out[real] = real
    return tuple(sorted(out.items(), key=lambda kv: len(str(kv[0])), reverse=True))


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


PROJECT_MEMORY_HOME = ".anneal-projects"
# The autonomic engine's store DIRECTORY (``levain.autonomic.db.default_store_dir``): its binding registry,
# run journal, SQLite sidecars and effect leases, denied read and write as ONE subtree under the levain
# home (``$LEVAIN_HOME``, else ``~/.levain``). The database's sidecars live inside it, so bash is not refused.
AUTONOMIC_STORE_DIR = "autonomic"
DERIVE_TRUST_ENV = "ANNEAL_MEMORY_DERIVE_TRUST"


def _project_memory_jewels(home: Path) -> tuple[list[Path], list[Path], list[Path]]:
    """The operator's PROJECT memory: ``(subtrees, trust_spellings, store_links)`` (spore-1308, ruled
    by Phill 2026-10-03, option A).

    ``~/.anneal-projects`` holds per-project anneal stores and, by flow's convention, the derive-trust
    file that binds re-derive labels to repo roots. It is operator memory exactly as ``~/.anneal-memory``
    is, and a project store's continuity may be loaded into an operator session's prompt (flow did so
    for every levain seat when this was ruled), so a confined write there is an injection path. A run
    on 2026-10-03 showed a confined seatbelt shell could read and write both. It is denied read+write.

    ``$ANNEAL_MEMORY_DERIVE_TRUST``, if set in THIS process when the floor is built, names the trust
    file anneal reads. Its spellings are write-denied: the path as given, its real parent with the
    final component unresolved (a symlink's own location, which is what ``rm`` and ``rename`` act on),
    and the fully resolved target. Its parent directory is write-denied as a literal by the ancestor
    pin every jewel gets, so it cannot be renamed. ``build_policy`` drops a spelling whose own
    location is inside a store subtree (see the comment there for what it does not check). A value
    that cannot be resolved refuses the floor.
    NOT covered, by design of this cut: a project home moved away from ``~/.anneal-projects`` (its
    stores are wherever the trust file's entries point); a symlink hop in the env path beyond its
    last directory (the replaceable-intermediate-symlink class this module documents for ssh); and
    a value set only in another process's environment, which is how flow sets it, so there the
    default subtree is what protects. A relative value resolves against this process's cwd.

    ``store_links``: when ``~/.anneal-memory`` or ``~/.anneal-projects`` is itself a symlink, the link
    (resolved HOME, final component unresolved) is returned so it is pinned like ``~/.ssh``; the
    subtree deny lands on the target, and without the pin the link can be removed and a planted tree
    put in its place (L2, run 2026-10-03). On Linux a pinned symlink in a writable directory refuses
    bash (fail-closed)."""
    subtrees: list[Path] = [(home / PROJECT_MEMORY_HOME).resolve()]
    home_r = home.resolve()
    store_links = [home_r / n for n in (".anneal-memory", PROJECT_MEMORY_HOME)
                   if (home_r / n).is_symlink()]
    spellings: list[Path] = []
    raw = os.environ.get(DERIVE_TRUST_ENV, "")
    if raw:
        try:
            lexical = Path(raw).expanduser()
            if not lexical.is_absolute():
                lexical = Path.cwd() / lexical
            for sp in (lexical, lexical.parent.resolve() / lexical.name, lexical.resolve()):
                if sp not in spellings:
                    spellings.append(sp)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ConfinementError(
                f"${DERIVE_TRUST_ENV}={raw!r} cannot be resolved ({exc}) — refusing to build the "
                "floor (fail-closed). Unset it or point it at the trust file."
            ) from exc
    return subtrees, spellings, store_links


def _anneal_trusted_dbs(path: Path) -> list[str]:
    """The ``stores[].db`` strings anneal itself would trust in the trust file at ``path``, or ``[]``.

    Mirrors anneal's loader (``anneal_memory.rederive._load_trust``, read path): no symlink, a regular
    file owned by this user and not writable by others, in a directory with the same properties, with
    a ``stores`` list whose entries carry string ``db`` and ``root``. A file anneal would reject loads
    no store, so it protects nothing and must not widen the floor either (codex L3 2026-10-03: an
    untrusted file could otherwise hide a system directory). There is no size cap, because anneal has
    none and a skipped valid file would leave its stores open. Checks run in anneal's order: owner and
    mode first (anneal rejects, so nothing to protect), then type. A directory loads nothing in anneal
    either. Any other non-regular file (a FIFO, a device) REFUSES the floor: anneal does not check the
    type and could read stores from it, and this cannot read it without blocking (codex L3 r2, r3)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_NOCTTY", 0))
    except OSError:
        return []
    try:
        st = os.fstat(fd)
        if st.st_uid != os.geteuid() or st.st_mode & 0o022:
            return []
        if stat.S_ISDIR(st.st_mode):
            return []
        if not stat.S_ISREG(st.st_mode):
            raise ConfinementError(
                f"the anneal trust path {path} is not a regular file, so the stores it may name cannot "
                "be read — refusing to build the floor (fail-closed). Replace it with a regular file."
            )
        pst = os.stat(os.path.dirname(os.path.abspath(path)))
        if pst.st_uid != os.geteuid() or pst.st_mode & 0o022:
            return []
        chunks = []
        while chunk := os.read(fd, 1 << 16):
            chunks.append(chunk)
        data = json.loads(b"".join(chunks).decode("utf-8"))
    except ConfinementError:
        raise   # a RuntimeError subclass: the refusal above must not be read as "unreadable"
    except (OSError, ValueError, RuntimeError):   # RuntimeError: RecursionError on deep nesting
        return []
    finally:
        os.close(fd)
    stores = data.get("stores") if isinstance(data, dict) else None
    if not isinstance(stores, list):
        return []
    return [s["db"] for s in stores
            if isinstance(s, dict) and isinstance(s.get("db"), str) and isinstance(s.get("root"), str)]


def _trust_listed_stores(home: Path, entity_dir: Path, workspace: Path) -> list[Path]:
    """Directories of the project stores the operator's anneal trust files name, to deny read+write
    (spore-1308 follow-on, ruled by Phill 2026-10-03: "yes for trust file thing").

    A store lives wherever its trust file's ``stores[].db`` points, so naming one directory does not
    cover a moved project home. Read at each policy build and each spawn (:func:`refresh_socket_denies`):
    ``$ANNEAL_MEMORY_DERIVE_TRUST`` if set in this process, anneal's default
    ``~/.anneal-memory/derive-trust.json`` and flow's ``~/.anneal-projects/derive-trust.json``, each
    through :func:`_anneal_trusted_dbs`. A missing or rejected file adds nothing, and the
    ``~/.anneal-projects`` subtree still applies.

    Both the db's real directory and, for a symlinked db, its lexical directory are denied, whether or
    not they exist: an absent one could be created by the entity and filled with a store anneal already
    trusts (codex L3 r2), and "this user cannot create it" is not something a same-uid shell is bound by
    (it can chmod its own directories, or rename them away: L3 r4), so it is never a reason to skip.
    On Linux an absent one becomes bwrap's mountpoint; one bwrap could not create refuses bash there
    with the path named, so a stale entry is pruned rather than silently skipped. More than a bounded
    number of distinct listed store directories refuses the floor. The entity's own
    canonical store (``<entity>/.levain/memory.db``) is skipped, since ``own_memory_files`` governs it.
    ⛔ A store whose directory cannot be denied as a whole REFUSES THE FLOOR (ConfinementError): the
    filesystem root, a top-level or temp directory, a directory that is or holds ``$HOME``, the entity or
    the workspace, or one inside the entity or workspace. anneal writes more than twenty names beside a
    db (continuity, crystal, spores, temp files, backups, locks), new ones by rename, so a partial deny
    of those files would be a guard to be bypassed one name at a time; the store needs a directory of its
    own. Containment is decided by file identity, so a case- or link-variant spelling cannot dodge it.
    NOT covered: a trust file this process cannot locate (a flow home moved by
    ``$FLOW_PROJECT_MEMORY_HOME`` without ``$ANNEAL_MEMORY_DERIVE_TRUST`` exported to levain), and a
    store added after a shell spawned, until the next spawn."""
    candidates = [home / ".anneal-memory" / "derive-trust.json",
                  home / PROJECT_MEMORY_HOME / "derive-trust.json"]
    raw = os.environ.get(DERIVE_TRUST_ENV, "")
    if raw:
        try:
            candidates.insert(0, Path(raw).expanduser())
        except (OSError, RuntimeError, ValueError) as exc:
            raise ConfinementError(
                f"${DERIVE_TRUST_ENV}={raw!r} cannot be resolved ({exc}) — refusing to build the "
                "floor (fail-closed)."
            ) from exc
    canonical = (entity_dir / ".levain" / "memory.db").resolve()
    listed = 0
    shared = {Path("/tmp").resolve(), Path("/var/tmp").resolve(), Path(tempfile.gettempdir()).resolve()}
    out: list[Path] = []
    seen: set[Path] = set()
    for trust in candidates:
        for db in _anneal_trusted_dbs(trust):
            try:
                lexical = Path(db).expanduser()
                if not lexical.is_absolute():
                    continue
                if lexical.resolve() == canonical:
                    continue
                dirs = {lexical.parent.resolve(), lexical.resolve().parent}
            except (OSError, RuntimeError, ValueError):
                continue
            for d in sorted(dirs):
                if d in seen:
                    continue
                listed += 1
                if listed > _MAX_LISTED_STORES:
                    raise ConfinementError(
                        f"the anneal trust files list more than {_MAX_LISTED_STORES} store "
                        "directories this user could reach — refusing to build the floor "
                        "(fail-closed). Prune the trust files."
                    )
                unsafe = (str(d) == d.anchor or len(d.parts) <= 2 or d in shared
                          or any(_inside_by_identity(d, p) for p in (home, entity_dir, workspace))
                          or _inside_by_identity(entity_dir, d) or _inside_by_identity(workspace, d))
                if unsafe:
                    raise ConfinementError(
                        f"the anneal store {db} (listed in {trust}) sits in {d}, which cannot be "
                        "hidden from the entity without hiding its own home, entity or workspace. "
                        "Refusing to build the floor (fail-closed). Move the store into a directory "
                        "of its own and update the trust file."
                    )
                seen.add(d)
                out.append(d)
    return out


_MAX_LISTED_STORES = 256


def _mountpoint_creatable(d: Path) -> bool:
    """Whether bwrap could create ``d`` as a mountpoint: its nearest existing ancestor is a directory
    this process can write and search. Used ONLY to refuse with a clear message (a wrong answer there
    fails closed), never to skip a deny: a same-uid shell can change what this returns."""
    effective = os.access in os.supports_effective_ids
    for a in d.parents:
        try:
            if a.exists():
                return a.is_dir() and os.access(a, os.W_OK | os.X_OK, effective_ids=effective)
        except OSError:
            return False
    return False


def _inside_by_identity(container: Path, p: Path, *, include_self: bool = True) -> bool:
    """True if ``p`` is ``container`` or lies under it, by file identity: each existing ancestor of
    ``p`` (``p`` itself too, unless ``include_self`` is False, which tests strictly inside) is compared
    with ``container`` via ``samefile``. A path that does not exist falls back to resolved-path
    comparison."""
    chain = [p, *p.parents] if include_self else list(p.parents)
    try:
        cont_exists = container.exists()
    except OSError:
        cont_exists = False
    for a in chain:
        try:
            if cont_exists and a.exists():
                if os.path.samefile(a, container):
                    return True
            elif a.resolve() == container.resolve():
                return True
        except (OSError, RuntimeError):
            continue
    return False


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
    # The DEFAULT stays permissive on purpose, and it is NOT the security decision (glm L3
    # raised it as fail-open). This function is the MECHANISM — build a floor from explicit
    # inputs. The POLICY (what an undeclared entity should get) lives one layer up in
    # `levain.firing.drive.resolve_cred_floor`, which is drive-aware; teaching this default
    # to be opinionated would put the same policy in two places that can disagree, and it
    # would reverse a deliberate operational-fit decision (denying these reads breaks the
    # entity's own gh/aws/curl use) at the wrong altitude. The real risk glm named — a FUTURE
    # PRODUCTION caller that forgets — is guarded structurally instead, by a source-level
    # test asserting every production call site passes this explicitly.
    deny_standard_creds: bool = False,
    # DEFAULT-ON, unlike ``deny_standard_creds`` above, and the asymmetry is deliberate.
    # That one is opt-IN because denying those reads breaks the entity's own gh/aws/curl
    # hands — a real operational cost against a real capability. Denying a container-daemon
    # socket costs the entity NOTHING it can legitimately do inside a floor: reaching the
    # daemon IS the bypass, since the daemon is unsandboxed and root. A floor with a live
    # socket in it is not a weaker floor, it is a decorative one. The opt-OUT exists for the
    # operator who genuinely runs containers from the entity and accepts that.
    allow_container_sockets: bool = False,
    deny_localhost_outbound: bool = False,
) -> CrownJewelsPolicy:
    """Assemble the crown-jewels floor for the entity at ``entity_dir``.

    **Universal floor (always denied — the structurally-knowable crown jewels):**
      - the operator-laptop memory store ``~/.anneal-memory/`` (subtree) — the identity moat in file
        terms (mirrors :func:`levain.firing.isolation.flow_store_dir`); a sovereign entity must never
        read the operator's own memory;
      - sibling entities' ``<other>/.levain/`` stores (subtrees) — one entity can't read another's
        memory;
      - the autonomic engine's store directory ``<levain home>/autonomic`` (``$LEVAIN_HOME``, else
        ``~/.levain``; subtree, read and write) — the binding registry and run journal that decide
        what fires, so an entity can neither read nor rewrite its own authority (:data:`AUTONOMIC_STORE_DIR`);
      - ⛔ ALL of ``~/.ssh`` (read+write-denied when ``ssh_mode="agent"``, except ``known_hosts``
        r+w and ``config`` r) — LOCATION-BASED, NOT NAME-BASED. See this module's docstring, which
        is the one home for this fact and carries its provenance (apparatus L2). This bullet used
        to say ``~/.ssh/id_*``, describing the name-based design the module explicitly REPLACED —
        and it contradicted the very next bullet, four lines below, which says the whole subtree is
        already covered in agent mode. Both were in one docstring and only one was true. The
        direction was SAFE (the emitter is STRICTER than the doc, denying the whole subtree), which
        is why it survived fifteen nights of review; the cost is that a caller reading it believes
        ``deploy_key`` and per-host keys stay readable under agent mode, understating the floor and
        making the two modes look closer than they are — and raw mode is where the real vector
        lives;
      - ``~/.ssh/authorized_keys`` (+ ``authorized_keys2``) WRITE — denied in BOTH ssh_modes: planting
        a key is a persistent SSH backdoor with zero legit entity use (in agent-mode the whole ~/.ssh
        subtree already covers it; in raw-mode this is the sole guard). Surgical (literal, not ancestor-
        expanded), so raw-mode keeps ~/.ssh otherwise writable;
      - levain's ledger of the Linux floor's session placeholders, ``~/.levain-runtime/floor/``
        (subtree): it decides what levain deletes from the operator's home.

    **``deny_standard_creds=True``:** fold the standard tool-canonical cred stores
    (:data:`_STANDARD_CRED_SUBTREES` + :data:`_STANDARD_CRED_FILES`) into the floor, each at its three
    spellings, and name their tool directories (:data:`_CRED_TOOL_DIRS`) for the Linux plan. These
    are knowable locations (not the false-security guessing the module refuses). This parameter's own default is
    False; the product default comes from :func:`levain.firing.drive.resolve_cred_floor`, which passes
    True in every drive except the interactive REPL unless the entity declares ``false``. Wired from ``confinement.json`` via :meth:`levain.firing.binding.ConversationBinding.create`.

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
    store and SIBLING stores are the structural crown jewels.

    **What the floor confines (its definition, head ruling 2026-10-07).** The floor confines the
    ENTITY'S SHELL and the entity's file editor. The operator's own unconfined processes (their
    terminal, their tools, a session they started with the cred floor off) are trusted: running as
    the operator's user, outside any sandbox, they already hold every crown jewel directly, so
    nothing the floor does is meant to stand between them and it. This is why the Linux plan binds
    host paths BY NAME: :func:`_bwrap_plan` classifies each entry it carries into a view ($HOME, a
    tool directory, the entity's store) and emits a bind of that name, which bwrap resolves when it
    starts. Between the two only an unconfined process of the same user can swap the source, since
    inside any confined shell those names sit in read-only views and cannot be renamed, and that
    process gains nothing by the swap that it does not already have. Under a separate hands user,
    bwrap runs as that user, so a source reached through a swapped link is still access-checked as
    the hands user."""
    # ⛔ TYPE-CHECK THE OPT-OUT AT THIS BOUNDARY — IT FAILS **OPEN**, WHICH IS THE ONE
    # DIRECTION A SECURITY FLAG MUST NOT FAIL (codex L3, 2026-09-04; MEASURED HERE).
    # Passing the STRING "false" for this flag — exactly what an env-var-backed or
    # hand-rolled caller does — is TRUTHY, and it
    # removed ALL THREE ARMS: `deny_sockets` went 7 -> 0 and the socket write-denies 10 -> 0.
    # **A value meaning "no" read as "yes" and silently deleted the floor.**
    # The JSON loader already validates the type, so no shipping caller is affected today —
    # which is precisely why this is worth pinning: this is the PUBLIC MECHANISM boundary,
    # and the next caller is the one that gets it wrong. `isinstance(True, int)` is also True,
    # so a JSON number would slip a plain int check; require a real bool.
    # ⚠ THE ILLUSTRATIVE CALL FORM WAS DELIBERATELY REMOVED FROM THIS COMMENT. Writing the
    # example out in literal call syntax made
    # `test_every_production_caller_of_build_policy_declares_the_cred_floor` — a SOURCE-LEVEL
    # scanner — count this PROSE as a production call site and fail. Two guards doing their
    # jobs, colliding on a comment ABOUT a call rather than a call. Do not re-add the example.
    if not isinstance(allow_container_sockets, bool):
        raise ConfinementError(
            f"allow_container_sockets must be a bool, got "
            f"{type(allow_container_sockets).__name__} ({allow_container_sockets!r}) — "
            f"fail-closed. A truthy non-bool would DISABLE the container-socket floor "
            f"entirely (spore-725)."
        )
    # Same boundary guard for the spore-755 opt-*in* deny. Unlike the flag above this one fails
    # CLOSED (a truthy non-bool turns the deny ON, over-restrictive, not a bypass) — but passing the
    # STRING "false" would still flip a caller's intended OFF to ON and silently break localhost, so
    # the public boundary is held to the same bool contract (glm L3, 2026-09-13). `isinstance(True,
    # int)` is True, so require a real bool, not a plain int check.
    if not isinstance(deny_localhost_outbound, bool):
        raise ConfinementError(
            f"deny_localhost_outbound must be a bool, got "
            f"{type(deny_localhost_outbound).__name__} ({deny_localhost_outbound!r}) — fail-closed."
        )
    ed = Path(entity_dir).expanduser().resolve()
    ws = (Path(workspace).expanduser().resolve() if workspace is not None
          else (ed / "workspace").resolve())
    home = Path.home()

    subtrees: list[Path] = [(home / ".anneal-memory").resolve()]
    subtrees.append((Path(os.environ.get("LEVAIN_HOME") or home / ".levain").expanduser() / AUTONOMIC_STORE_DIR).resolve())
    project_subtrees, trust_spellings, store_links = _project_memory_jewels(home)
    subtrees.extend(project_subtrees)
    listed_dirs = _trust_listed_stores(home, ed, ws)
    subtrees.extend(listed_dirs)
    subtrees.extend(_sibling_entity_stores(ed))
    for extra in extra_deny_read_write:
        subtrees.append(Path(extra).expanduser().resolve())
    files: list[Path] = [Path(f).expanduser().resolve() for f in deny_files]

    # The standard tool-canonical cred stores, folded in when the caller passes deny_standard_creds
    # (resolved by levain.firing.drive.resolve_cred_floor: True except at the interactive REPL, unless
    # the entity declares ``deny_standard_creds: false`` in confinement.json). They flow through the SAME subtree/file machinery (read+write deny + ancestor
    # write-deny), so no separate rendering path is needed.
    # Each at all three spellings (:func:`_spellings`): resolved-only left a stow-style
    # ``~/.kube/config -> ~/dotfiles/kube/config`` link out of the floor, so ``rm`` + recreate of the
    # link planted a kubeconfig the operator's next kubectl would run (codex L3, 2026-10-07).
    tool_dirs: list[Path] = []
    cred_dir_sources: list[Path] = []
    if deny_standard_creds:
        cred_dir_sources = _cred_dir_sources()
        subtrees.extend(floor_roots([*_STANDARD_CRED_SUBTREES, *cred_dir_sources]))
        files.extend(floor_roots([*_STANDARD_CRED_FILES, *_cred_overrides()[1]]))
        tool_dirs = [home / d[2:] for d in _CRED_TOOL_DIRS]
    # The model API key file levain was given (`--api-key-file`): denied both ways, so the key the
    # flag keeps off the command line is not left readable to the entity instead (L2 r1).
    files.extend(floor_roots(_secret_files()))
    # The ledger of the session-scoped Linux placeholders (:func:`_ledger_enter`) decides what levain
    # deletes from the operator's home, so the entity may neither read nor write it.
    # At all three spellings, like every other jewel (L1 r1): a link at ~/.levain-runtime must not
    # leave the ledger reachable by a name the floor does not hold.
    subtrees.extend(_spellings("~/.levain-runtime/floor"))

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
    #   (3) EACH VECTOR IS DENIED AT BOTH ITS LEXICAL AND ITS RESOLVED PATH (2026-08-21). ``.resolve()``
    #       alone gives CONTENT protection (the real write target) but LOSES the lexical path, and a
    #       PRE-EXISTING ``~/.ssh/authorized_keys -> ~/evil/authorized_keys`` symlink is therefore denied
    #       only at ~/evil — while the entity reaches the LEXICAL ~/.ssh/authorized_keys, which sshd
    #       honours via ``realpath()``.
    #       ⛔⛔ "WRITES TO" WAS THE WRONG VERB AND IT WAS A REFUTED MECHANISM (Diogenes HIGH
    #       2026-08-22, measured live with sandbox-exec; re-measured independently 2026-08-23).
    #       A DIRECT DATA WRITE AT THE LEXICAL PATH IS DENIED: seatbelt CANONICALISES a requested
    #       path before matching ``(literal ...)``, so a profile denying only the RESOLVED path
    #       already refuses ``echo >> <lexical>`` with "Operation not permitted". The converse
    #       minimal profile — denying only the LEXICAL path — lets that same append through at
    #       exit 0, which is what makes this canonicalisation rather than coincidence.
    #       ▶ THE REAL VECTOR IS UNLINK-THEN-RECREATE: ``unlink``/``rename``/``ln`` act on the LINK
    #       and ARE matched lexically, so ``rm -f <lexical>`` succeeds under a resolved-only deny
    #       and the recreated file is an object nothing names. Denying both spellings closes that
    #       too, so the code below is unchanged and correct — only its stated reason moved.
    #       ⚠ DO NOT REASON FROM THE OLD MODEL. "seatbelt matches writes lexically" is FALSE and
    #       was generalised into a design rule here; a guard built on it would be built backwards.
    #       The (2) dir-anchor pin does NOT cover this: it blocks renaming
    #       ~/.ssh while deliberately still allowing "file creation/reads INSIDE it", which is exactly
    #       the write the attack uses. ⚠ THIS IS THE SAME lexical-vs-resolved ARGUMENT ALREADY MADE
    #       TWICE IN THIS FILE — for the ~/.ssh dir anchor below ("resolved jewel vs lexical anchor are
    #       different objects") and for the confinement config's fail-closed symlink check. It was
    #       applied to the DIRECTORY and to the CONFIG and not to the FILES between them.
    #       ⛔ DENY BOTH RATHER THAN REASONING ABOUT SEATBELT'S CANONICALISATION: whether the sandbox
    #       matches ``(literal ...)`` lexically or after resolving is a platform detail we would then
    #       depend on being right about. Every entry makes the deny correct under either semantics.
    #       ``_dedup`` collapses them to one entry in the ordinary no-symlink case.
    #       ⚠ IT IS NOT FREE, AND THE FIRST DRAFT OF THIS COMMENT CLAIMED IT WAS ("breaks no real
    #       setup") — corrected by codex L3. A dotfile manager (stow/chezmoi/yadm) that REPLACES or
    #       RE-LINKS ~/.ssh/config now fails inside raw mode. Reading it still works and ssh still
    #       works; only rewriting the link does not. That is a real workflow cost, accepted
    #       deliberately: an entity re-pointing ~/.ssh/config IS the ProxyCommand vector.
    #   ⛔⛔ KNOWN-OPEN, NOT CLOSED BY THIS FIX (codex L3 2026-08-21, each REAL and each reproduced
    #       by reasoning from disk — do not read this block as "ssh is now safe in raw mode"):
    #       (a) REPLACEABLE INTERMEDIATE SYMLINK. With ~/.ssh/authorized_keys -> ../bridge/authorized_
    #           keys and ~/bridge -> ~/good, the policy pins the lexical entry and ~/good — but NOT
    #           ~/bridge, because ``_write_deny_ancestors`` only walks RESOLVED ancestors. The entity
    #           re-points ~/bridge and writes a file neither denied endpoint names. TOCTOU by design:
    #           it never touches a denied path.
    #       (b) REPLACEABLE / SYMLINKED HOME. Same shape one level up; already admitted at the top of
    #           this file. ⛔ THE THIRD SPELLING ABOVE DOES NOT NARROW IT EITHER — this line
    #           used to say it did, and measurement refutes that. The RAW ``Path.home() / .ssh``
    #           entry differs from the other two ONLY when an ANCESTOR component is a symlink,
    #           and seatbelt CANONICALISES ancestor components for every operation, not just
    #           data access — so the entry fires for NO operation on that hand. Live on Darwin
    #           25.5 with /tmp -> /private/tmp: a profile whose only rule names the
    #           un-canonicalised spelling allows append, `rm -f` AND `mv` (exit 0); the control
    #           naming the canonical spelling denies all three. It is equally dead on the
    #           in-process hand, where ``crown_jewel_reason`` resolves its argument first, so a
    #           lexical entry can never be what matches.
    #           ▶ THE LINE IS KEPT AS DEFENCE-IN-DEPTH AGAINST A CANONICALISATION CHANGE — this
    #           file already warns that seatbelt is Apple-deprecated and its behaviour
    #           undocumented — NOT as a mitigation of (b). Deleting a belt from a security floor
    #           to win a tidiness argument is the wrong direction of error; claiming it mitigates
    #           a live vector is the wrong direction of belief.
    #       (c) TRANSITIVE INPUTS. An existing ``Include config.d/*`` in a protected config, or an
    #           ``rc`` that sources a writable helper, is an exec path we do not deny — the anchor
    #           deliberately permits child creation inside ~/.ssh. Denying the top-level files does
    #           not protect what those files PULL IN.
    #       (d) DYNAMIC DIVERGENCE BETWEEN THE HANDS. ``crown_jewel_reason`` resolves the requested
    #           path FIRST, so a symlink retargeted AFTER policy construction leaves seatbelt matching
    #           the static lexical literal while the in-process twin resolves to a new, unlisted
    #           target. Denying more endpoints does not fix resolve-first.
    #       (e) HARDLINKS — a pre-existing outside hardlink to a vector: refused at spawn and by the
    #           file editor (see the honest limits above).
    deny_write_files_l: list[Path] = []
    for n in _SSH_WRITE_DENIED:
        # THREE SPELLINGS, and the first one is easy to miss (apparatus L3 codex, 2026-08-21): RAW
        # ``Path.home()`` (the true lexical path), resolved HOME + un-deref'd .ssh, and the real
        # content target through symlinks. One helper now builds them for every jewel list.
        deny_write_files_l.extend(_spellings(f"~/.ssh/{n}"))
    # The relocated derive-trust file (spore-1308): write-only, both spellings, for the same reason as
    # the ssh vectors above. Writing it rebinds a re-derive label to a root the writer picked.
    # A trust spelling whose own location (final component unresolved) is already denied both ways
    # is dropped: inside any read+write subtree (operator stores, siblings, extras, credential
    # subtrees), inside ~/.ssh in agent mode, or equal to a deny_files entry. Listing it again would
    # only add a redundant write-only mount. A spelling that is a link INTO a hidden subtree stays,
    # and bwrap step (5) binds /dev/null at its target rather than re-exposing it.
    covered = list(subtrees) + ([ssh_dir] if ssh_dir is not None else [])
    kept: list[Path] = []
    for sp in trust_spellings:
        try:
            loc = sp.parent.resolve() / sp.name
        except (OSError, RuntimeError, ValueError) as exc:
            raise ConfinementError(
                f"${DERIVE_TRUST_ENV} spelling {sp} cannot be resolved ({exc}) — refusing to build "
                "the floor (fail-closed)."
            ) from exc
        if loc in files or any(loc == t or loc.is_relative_to(t) for t in covered):
            continue
        kept.append(sp)
    trust_spellings = kept
    deny_write_files_l.extend(trust_spellings)

    # De-dup while preserving order (a sibling could coincide with an extra).
    # ⚠ THE SOUNDNESS PREMISE HERE USED TO READ "resolved paths compare exactly, so a simple
    # seen-set is sound" — AND THE FOUR LINES DIRECTLY ABOVE IT FALSIFY IT (Diogenes levain-LOW
    # 2026-08-22). Two of the three spellings appended above are LEXICAL and are deliberately NOT
    # resolved; that is the entire point of the fix. So the premise names a property only one third
    # of the input has.
    # ▶ THE SEEN-SET IS STILL SOUND, for a reason that does not depend on resolution: this dedups
    # by ``Path`` VALUE EQUALITY, which is exact for any pair of paths whatever their provenance.
    # It collapses the ordinary no-symlink case where the three spellings coincide, and correctly
    # KEEPS them apart when they diverge — which is the behaviour the deny list needs. The old
    # sentence reached the right conclusion through an argument about the wrong property.
    def _dedup(items: list[Path]) -> tuple[Path, ...]:
        seen: set[Path] = set()
        out: list[Path] = []
        for p in items:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return tuple(out)

    # CONTAINER-DAEMON SOCKETS (spore-725) — THREE ARMS, EACH MEASURED SEPARATELY 2026-09-04
    # against a real unix-socket server under a real ``sandbox-exec`` profile, and each kept
    # only after the profile WITHOUT it was shown to lose:
    #   (i)   CONNECT — ``deny_sockets`` renders ``(deny network-outbound (literal <resolved>))``.
    #         RESOLVED spelling only: seatbelt canonicalises for network-outbound, so the
    #         resolved entry covers every symlinked path, and a LEXICAL-only entry blocks
    #         nothing at all (measured on /var/run/docker.sock -> ~/.docker/run/docker.sock).
    #   (ii)  RENAME/UNLINK OF THE SOCKET — the socket goes into ``deny_write_files_l`` at BOTH
    #         spellings, because link operations ARE matched lexically (the same asymmetry this
    #         function already documents for the ssh vectors). Without it: ``mv sock moved`` and
    #         then connecting to ``moved`` SUCCEEDS — the canonical path is now one nothing
    #         names, and the daemon keeps serving the same listening inode.
    #   (iii) RELOCATION OF AN ANCESTOR — falls out free, because these entries land in
    #         ``all_jewels`` below and ``_write_deny_ancestors`` write-denies every parent dir.
    #         Without it: renaming the socket's PARENT DIR relocates it and connects. Measured
    #         that a parent-dir write-deny does NOT substitute for (ii) — renames INSIDE a dir
    #         stay legal by design — nor (ii) for (iii). Neither arm is redundant.
    # ⚠ The host/daemon is OUTSIDE the sandbox and is unaffected by all three; verified by a
    # host-side connect succeeding against the same socket while the entity's was refused.
    socket_sources_l: list[Path] = []
    if not allow_container_sockets:
        socket_sources_l = [p for s in _CONTAINER_DAEMON_SOCKETS for p in _expand_socket_source(s)]
    socket_sources_t = _dedup(socket_sources_l)
    # ⛔ ONE RESOLUTION FEEDS ALL THREE ARMS (codex L3 #5 + glm, 2026-09-04). This block used to
    # resolve the sources THREE times — once for the connect deny, once inline for the write deny,
    # once inside the `socket_spellings` comprehension — so a concurrent retarget could give the
    # three arms three different snapshots, and the "one place this derivation lives" claim in
    # `_socket_arms` was false while it was written. `_socket_arms` is now that one place, and
    # `refresh_socket_denies` re-runs THE SAME function at spawn so build and spawn cannot drift.
    _arms = _socket_arms(socket_sources_t)
    deny_sockets_t = _arms.targets
    deny_write_files_l.extend(_arms.write_spellings)
    socket_spellings_t = _arms.spellings

    # A FILE-shaped jewel may be a SQLite store (argushub's ~/.anneal-memory is one, in WAL mode), and
    # its sidecars hold data the main-file deny does not cover. Same class as
    # ``_OWN_MEMORY_FILENAMES`` for the entity's own store (Diogenes MEDIUM 2026-10-02). Named for
    # every non-directory jewel rather than sniffed by header: an absent sidecar costs one literal
    # on macOS and nothing on Linux. The paths are the RESOLVED ones, which is what SQLite names its
    # sidecars after.
    sidecars: list[Path] = []
    known_dirs = {*listed_dirs, *_spellings("~/.levain-runtime/floor")}
    for jewel in _dedup(subtrees + files):
        if jewel in known_dirs and not jewel.is_file():
            continue   # a store directory, possibly absent: it has no sidecars beside it (glm L3 r3)
        # Not a directory, rather than is a file: a jewel absent when the policy is built can be
        # created as a SQLite store before the shell starts (codex, L3 2026-10-02).
        if not jewel.is_dir():
            sidecars.extend(jewel.with_name(jewel.name + s) for s in ("-wal", "-shm", "-journal"))
    sqlite_sidecars_t = _dedup(sidecars)
    files.extend(sqlite_sidecars_t)

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
    # spore-359 safety (L2 domain review, 2026-07-17): WRITE-deny the entity's OWN store files so its
    # confined hands cannot compose the always-loaded memory — only the host-process wrap/capture may.
    # LEXICAL paths (the path the hands would write / anneal writes), so a CREATE of a not-yet-existing
    # file (pre-first-wrap pre-planting) is denied too. Fail-CLOSED on a symlinked store file (same
    # reasoning as the config above: a symlink lets `rm`+recreate at the lexical path dodge a resolved-
    # target deny, and .levain is entity-writable) — anneal writes these as plain files, so a symlink
    # is anomalous. ``.levain`` is already an ancestor-write-dir via ``config_file`` below; these ride
    # the same anchor pin.
    own_memory_files_l: list[Path] = []
    for _name in _OWN_MEMORY_FILENAMES:
        _lexical = ed / ".levain" / _name
        if _lexical.is_symlink():  # catches broken symlinks too (is_symlink is True regardless of target)
            raise ConfinementError(
                f"the entity's own memory file {_lexical} is a symlink — refusing to grant confined "
                "hands. A symlinked store file could be unlinked and rewritten to poison the next "
                "session's always-loaded memory; keep it a plain file inside .levain/."
            )
        own_memory_files_l.append(_lexical)
    own_memory_files_t = _dedup(own_memory_files_l)

    # Ancestor write-denies for the subtree jewels, file jewels, the write-only ssh files, the entity's
    # own store files, AND the config file (all relocate the same way). Including ``deny_write_files_t``
    # here write-denies ~/.ssh (the persistence-vector dir anchor) so it can't be renamed/replaced to
    # relocate the protected literal — the raw-mode bypass codex found. The ssh_dir is guarded as a
    # subtree below (agent mode), so its ancestors are covered too.
    # ⛔ THE CLAUSE THAT STOOD HERE — "in raw mode this is the only thing that pins ~/.ssh's anchor" —
    # WAS FALSE, and :469 in this same file already said so ("the files are added to ``all_jewels``
    # below, so ~/.ssh (their ancestor DIR) is write-denied"). Two comments in one file, opposite
    # answers. See the block below `ssh_anchor` for the measurement; pinned by
    # test_the_ssh_anchor_is_redundant_because_the_ancestor_walk_already_names_it.
    all_jewels = (
        list(deny_read_write) + list(deny_files_t) + list(deny_write_files_t)
        + list(own_memory_files_t) + [config_file]
    )
    if ssh_dir is not None:
        all_jewels.append(ssh_dir)
    # Pin the LEXICAL ~/.ssh anchor too (apparatus L3 codex re-verify HIGH — "resolved jewel vs lexical
    # anchor are different objects").
    # ⚠ THE SENTENCE BELOW DESCRIBED A ``deny_write_files_l`` THAT STOPPED EXISTING TWENTY LINES
    # ABOVE IT, IN THE SAME COMMIT (Diogenes levain-MEDIUM 2026-08-22). Since 7211bbf that list is
    # built from THREE spellings, not one: raw lexical, resolved-HOME lexical, and resolved. So
    # ".resolve() only" is no longer true of it, and the paragraph is kept below with its original
    # argument intact ONLY because that argument is about the DIRECTORY and still holds.
    # ⛔⛔ THE 2026-08-23 REBUTTAL THAT STOOD HERE WAS ITSELF FALSE, AND A WRONG REBUTTAL IS WORSE
    # THAN A WRONG FINDING — it discharges the next reader's obligation to look again. It said the
    # 2026-08-22 redundancy finding "does not hold" because "the three spellings deny lexical FILES
    # inside ~/.ssh and never the lexical DIRECTORY, so ``rm ~/.ssh; ln -s ~/evil ~/.ssh`` … is named
    # by this anchor and by nothing else."
    # ▶ MEASURED, NOT ARGUED (2026-09-03): ``_write_deny_ancestors`` walks ``jewel.parents`` and does
    # NOT resolve, so the DIRECTORY is produced by the FILES' own ancestors. Both lexical spellings
    # are in ``all_jewels`` by construction thirty lines up — ``Path.home() / ".ssh" / n`` and
    # ``home.resolve() / ".ssh" / n``, the first two :func:`_spellings` — so their parents are
    # exactly ``home / ".ssh"`` AND ``home.resolve() / ".ssh"``. The second of those IS
    # ``ssh_anchor``. It is therefore already denied whether or not HOME is itself a symlink, which
    # is the one case the rebuttal's "and by nothing else" needed in order to be true.
    # ⚠ THE LINE IS KEPT ANYWAY, and deliberately: it is harmless, ``_dedup`` collapses it, and
    # deleting a belt from a security floor to win a tidiness argument is the wrong direction of
    # error. What was actually wrong here was the JUSTIFICATION, not the code.
    # ▶ AND THE CLAIM IS NOW EXECUTED RATHER THAN ASSERTED — if a future edit teaches
    # ``_write_deny_ancestors`` to resolve, this anchor stops being redundant and becomes
    # load-bearing again, which is precisely the day you need to be told:
    # ``test_the_ssh_anchor_is_redundant_because_the_ancestor_walk_already_names_it``.
    # Historical form of the argument, still correct for the directory:
    # ``deny_write_files_l`` uses ``.resolve()`` for CONTENT protection
    # (it must deny the real write TARGET, even through a symlink) — but that FOLLOWS a pre-existing
    # ``~/.ssh -> ~/realssh`` symlink, so ancestor-deny pins ~/realssh, leaving lexical ~/.ssh replaceable
    # (``rm ~/.ssh; ln -s ~/evil ~/.ssh`` → the planted key at ~/evil dodges the resolved-target deny).
    # Deny the lexical dir path (resolved HOME + un-deref'd ``.ssh``) so ``rm``/``mv``/replace of ~/.ssh
    # ITSELF is blocked; child reads/writes still resolve through it, so raw mode is not re-jailed.
    ssh_anchor = home.resolve() / ".ssh"
    write_dirs = _dedup(list(_write_deny_ancestors(all_jewels)) + [ssh_anchor] + store_links)

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
        own_memory_files=own_memory_files_t,
        deny_sockets=deny_sockets_t,
        socket_spellings=socket_spellings_t,
        trust_spellings=_dedup(list(trust_spellings)),
        socket_sources=socket_sources_t,
        deny_localhost_outbound=deny_localhost_outbound,
        deny_keychain=deny_standard_creds,
        sqlite_sidecars=sqlite_sidecars_t,
        ro_tool_dirs=tuple(tool_dirs),
        cred_dir_sources=tuple(cred_dir_sources),
        trusted_roots=_trusted_roots(entity_dir, workspace),
    )


@dataclass(frozen=True)
class _SocketArms:
    """The THREE arms of the socket floor, all derived from ONE resolution pass.

    ⛔ ONE RESOLUTION, NOT THREE — codex L3 #5 + glm, 2026-09-04, and it is a correctness fix and
    not tidiness. The first version of this fix resolved the sources in three separate places
    (``_resolve_socket_targets`` for the connect arm, an inline ``lex.resolve()`` for the write arm,
    a third inside the ``socket_spellings`` comprehension), so under a CONCURRENT retarget the three
    arms could derive from three different snapshots — a connect-deny naming one target while the
    write-deny that protects it names another. It also made the docstring claim "the one place this
    derivation lives" FALSE while it was written."""

    targets: tuple[Path, ...]        # arm (i): resolved, for the network-outbound connect deny
    write_spellings: tuple[Path, ...]  # arms (ii)/(iii): BOTH spellings, for the write deny
    spellings: tuple[Path, ...]      # message classification in `crown_jewel_reason`


def _socket_arms(sources: tuple[Path, ...]) -> _SocketArms:
    """Derive all three socket arms from ``sources`` in a SINGLE resolution pass.

    FAIL-CLOSED: any resolution error raises :class:`ConfinementError` rather than escaping as a raw
    ``OSError``/``RuntimeError``. The caller is ultimately ``spawn_shell``, whose contract is to
    refuse rather than hand back a shell whose socket denies may name the wrong target, so the
    conversion happens HERE — at the derivation — instead of at each call site, where the next call
    site added is the one that forgets.

    ⚠ The lexical spelling is kept ALONGSIDE the resolved one because the arms match differently:
    the connect arm is resolved-only (seatbelt canonicalises for ``network-outbound``), while the
    link operations arms (ii)/(iii) are matched LEXICALLY. That asymmetry is measured and is
    documented at the ``deny_sockets`` / ``deny_write_files`` fields."""
    targets: list[Path] = []
    write_spellings: list[Path] = []
    spellings: list[Path] = []
    for lex in sources:
        try:
            resolved = lex.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise ConfinementError(
                f"could not resolve the container-socket path {lex} ({exc}) — refusing to build "
                "the confinement floor rather than emit a socket deny that may name the wrong "
                "target (fail-closed)."
            ) from exc
        targets.append(resolved)
        write_spellings.append(lex)
        write_spellings.append(resolved)
        spellings.append(lex)
        spellings.append(resolved)
    return _SocketArms(
        targets=_dedup_paths(targets),
        write_spellings=_dedup_paths(write_spellings),
        spellings=_dedup_paths(spellings),
    )


def refresh_socket_denies(policy: CrownJewelsPolicy) -> CrownJewelsPolicy:
    """Re-derive ALL THREE socket arms from ``policy.socket_sources`` as the filesystem is NOW, and
    return a policy whose socket denies are the build-time sets UNION the freshly-derived ones.

    Called by :meth:`ConfinementProvider.spawn_shell` immediately before the profile is rendered, and
    by the tool executor before each (re)spawn so the union PERSISTS. ``spore-768`` / codex L3 HIGH
    2026-09-04, corrected by a second codex L3 the same day.

    ⛔ **ALL THREE ARMS, AND THE FIRST VERSION OF THIS FUNCTION REFRESHED ONLY THE CONNECT ARM.**
    That was wrong, and it was wrong in the way this module has already measured twice: a
    ``network-outbound`` deny ALONE is defeated by ``mv``. Confirmed by execution before this
    correction — after a refresh that touched only ``deny_sockets``, the freshly-resolved target was
    in the connect deny, absent from ``deny_write_files``, its parent absent from
    ``deny_write_dirs``, and ``crown_jewel_reason`` returned ``None`` for it, so BOTH hands would let
    the entity rename it and connect to the new name. The comment justifying the omission reasoned
    about the arms on the LISTED path and never about the arms on the NEW TARGET.
    ⚡ The class is this repo's own dominant one: the arm that is visible in a deny list was
    implemented, the two that make it hold were not, and prose was written to explain the gap.

    ⛔ **UNION, NEVER REPLACE — the fail-closed property.** A refresh that REPLACED a set would hand
    an attacker a DELETION primitive: point a listed socket at a decoy immediately before spawn and
    the real target drops off the deny list. Union makes every socket set monotonic, so a
    re-resolution can only ever ADD — which is also why this needs no "did it change?" branch to get
    wrong, and why it is safe to run unconditionally on every spawn.
    ⚠ **AND MONOTONICITY IS ONLY REAL IF THE RESULT IS KEPT** (codex L3 #2). The first version
    returned a refreshed policy that the caller rendered and discarded, so the next respawn started
    from the build-time set again and a target denied at spawn 1 was reachable after spawn 2 —
    "can only ADD" was true within one spawn and false across the respawns the comment explicitly
    relied on. :class:`SandboxedBashExecutor` now assigns the result back to its cached policy.

    ⚠ **WHAT THIS DOES NOT BUY: THE WINDOW IS NARROWED TO SPAWN→CONNECT, NOT CLOSED.** A seatbelt
    profile is fixed when the process starts, so a symlink created at a listed socket path AFTER the
    shell spawns is still uncovered. That residual is IRREDUCIBLE with seatbelt rather than merely
    unfixed: seatbelt canonicalises the path for ``network-outbound``, so a deny can only ever name a
    TARGET, never a NAME — there is no expressible rule for "whatever this path points at, at connect
    time". A ``(subpath ...)`` belt fails for the same reason, and this module already measured a
    regex belt dead. **A spawn-time snapshot is the strongest statement this profile language can
    make**, which is why the honesty surfaces say snapshot and not covered.
    ⚡ The residual is tolerable for a measured reason, not an argued one: the ENTITY cannot create
    that symlink (arm (ii) write-denies every listed socket at both spellings), so an EXTERNAL actor
    must do it mid-session, and an unlisted socket it could point at was already reachable under its
    own name — which the banner declares. Marginal exposure was zero even before this fix; what was
    false was the CLAIM that a listed socket is covered.

    ▶ SIDE EFFECT WORTH NAMING: when a listed name does point somewhere unenumerated, the union gives
    that target the full three-arm treatment — so the refresh closes the unlisted socket a listed
    name reaches. It does NOT close unlisted sockets generally; that is ``spore-754`` (an
    operator-declared socket list) and is deliberately a separate change."""
    # Trust-listed project stores are re-derived at the same seam, as a union for the same reason
    # (spore-1308 follow-on, codex L3 2026-10-03): a store anneal starts trusting after the binding
    # was built is covered from the next spawn on. Raises ConfinementError on an unsafe store.
    listed = _trust_listed_stores(Path.home(), policy.entity_dir, policy.workspace)
    new_dirs = list(_dedup_paths([d for d in listed if d not in policy.deny_read_write]))
    if new_dirs:
        policy = replace(
            policy,
            deny_read_write=_dedup_paths(list(policy.deny_read_write) + new_dirs),
            deny_write_dirs=_dedup_paths(list(policy.deny_write_dirs)
                                         + list(_write_deny_ancestors(new_dirs))),
        )
    if not policy.socket_sources:
        return policy  # opt-out (`allow_container_sockets=True`) or nothing enumerated

    arms = _socket_arms(policy.socket_sources)
    deny_sockets = _dedup_paths(list(policy.deny_sockets) + list(arms.targets))
    write_files = _dedup_paths(list(policy.deny_write_files) + list(arms.write_spellings))
    spellings = _dedup_paths(list(policy.socket_spellings) + list(arms.spellings))
    # Arm (iii): the fresh target's ANCESTORS, or renaming its parent dir relocates it out from
    # under the literal deny — measured on the built-in sockets, and it applies identically to a
    # target the refresh just learned about.
    write_dirs = _dedup_paths(
        list(policy.deny_write_dirs) + list(_write_deny_ancestors(list(arms.write_spellings)))
    )

    if (deny_sockets == policy.deny_sockets
            and write_files == policy.deny_write_files
            and spellings == policy.socket_spellings
            and write_dirs == policy.deny_write_dirs):
        return policy
    return replace(
        policy,
        deny_sockets=deny_sockets,
        deny_write_files=write_files,
        socket_spellings=spellings,
        deny_write_dirs=write_dirs,
    )


def _dedup_paths(items: list[Path]) -> tuple[Path, ...]:
    """Order-preserving de-duplication. The module-level twin of ``build_policy``'s local ``_dedup``,
    which is a closure and cannot be reached from here. Order is preserved because the rendered
    profile is compared verbatim in tests and a set would make it nondeterministic."""
    seen: set[Path] = set()
    out: list[Path] = []
    for p in items:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return tuple(out)


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
    ``.anneal-memory-backup``. Over-matching here is FAIL-CLOSED (refuse a variant of a crown jewel).

    ⚠ **"WRONGLY REJECTS NOTHING" IS A macOS CLAIM AND IT DOES NOT PORT — measured on argushub
    (ext4) 2026-09-03, and predicted before any Linux code existed.** On a case-INSENSITIVE volume
    (APFS/Windows) a case variant of a jewel IS the same on-disk file, so folding case rejects only
    paths the kernel would deny anyway and the sentence held. On a case-SENSITIVE volume (ext4)
    ``~/.Anneal-Memory`` and ``~/.anneal-memory`` are two genuinely different directories, yet
    :func:`_ci_within` returns True for the pair — so a legitimate path CAN be denied on account of a
    jewel it merely resembles. The BEHAVIOUR is deliberately unchanged: on a pure denylist,
    over-denying is the safe direction, and folding case is what closes the variant bypass that
    motivated this predicate. Only the CLAIM is qualified. Do NOT "fix" this by making the compare
    platform-conditional — that would reopen the bypass on exactly the volumes where it bites."""
    p = _canon(str(path))
    r = _canon(str(root))
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


# Paths that are views of a PROCESS rather than files (lane P2 item 2c). The file editor runs inside
# levain's own process, so on Linux ``/proc/self/mem`` is levain's memory (a process may always read
# itself), with the carried environment and the model API key in it, and ``/proc/<pid>/environ`` is
# any same-user process's exec-time environment. ``/dev/fd/N`` and ``/dev/std*`` are levain's own open
# files on both OSes (an open SQLite store, say). The editor has no reason to touch any of them.
_PROCESS_VIEW_ROOTS = (Path("/proc"), Path("/dev/fd"))
_PROCESS_VIEW_FILES = (Path("/dev/stdin"), Path("/dev/stdout"), Path("/dev/stderr"))


def _process_view_reason(given: Path | str, resolved: Path) -> str | None:
    """Why ``given`` (as the caller spelled it, and as it resolves) is a process view, or None. The
    given spelling matters on Linux: ``/proc/self/fd/N`` resolves to wherever fd N points, which is
    the file it exposes and may be an ordinary path."""
    try:
        spelled = Path(os.path.abspath(os.path.expanduser(str(given))))
    except (ValueError, OSError, RuntimeError):
        spelled = resolved
    roots = list(_PROCESS_VIEW_ROOTS)
    if platform.system() == "Linux":
        # Any other procfs on the host is the same view (a chroot's or a container's /proc): the
        # editor runs in levain's own mount namespace, where step (8) of the bash plan does not reach.
        try:
            roots += _extra_procfs_mounts()
        except OSError as exc:
            return f"{given}: the mount table could not be read to rule out a process view ({exc}) — refused"
    for q in (spelled, resolved):
        if any(_ci_within(q, r) for r in roots) or any(_canon(str(q)) == _canon(str(f))
                                                       for f in _PROCESS_VIEW_FILES):
            return (f"{given} is a view of a running process (/proc, /dev/fd, /dev/std*), which "
                    "would expose levain's own memory, open files and environment — refused")
    return None


def _procfs_devices() -> set[int]:
    devs: set[int] = set()
    for mp in [Path("/proc"), *_extra_procfs_mounts()]:
        try:
            devs.add(os.stat(mp).st_dev)
        except OSError:
            pass
    return devs


def opened_file_path(fd: int) -> str:
    """The path of the file ``fd`` has open, as the kernel names it: ``/proc/self/fd/<fd>`` on Linux
    (a process may always read its own), ``F_GETPATH`` on macOS. Raises OSError when there is none
    (a pipe, a socket) or it cannot be read."""
    if platform.system() == "Linux":
        path = os.readlink(f"/proc/self/fd/{fd}")
        if not path.startswith("/"):   # "pipe:[123]", "socket:[456]", "anon_inode:...": no file
            raise OSError(f"fd {fd} is not a file ({path})")
        return path
    import fcntl

    getpath = getattr(fcntl, "F_GETPATH", None)
    if getpath is None:
        raise OSError("this platform cannot name an open file")
    raw = fcntl.fcntl(fd, getpath, bytes(1024))
    return os.fsdecode(raw.split(b"\0", 1)[0])


def opened_file_reason(policy: CrownJewelsPolicy, fd: int) -> str | None:
    """Why the file ALREADY OPEN on ``fd`` must not be read or written through the file editor, or
    None. The editor's path check runs before the editor opens the path, so a link the shell flips in
    between (to ``/proc/<pid>/environ``, or to any jewel) passed it (L2 r1). This judges the object
    actually opened, by its device (any procfs) and by the name the kernel gives it, before a byte
    is read. Fail-closed: a file whose name cannot be learnt is refused."""
    try:
        st = os.fstat(fd)
        if platform.system() == "Linux" and st.st_dev in _procfs_devices():
            return "the opened file is in a /proc filesystem, a view of a running process"
        if stat.S_ISDIR(st.st_mode):
            return None
        path = opened_file_path(fd)
    except OSError as exc:
        return f"the opened file could not be identified ({exc}) — refused (fail-closed)"
    reason = crown_jewel_reason(policy, path)
    if reason is not None:
        return reason
    # The opened object's own identity, never a second stat of its name: the shell can unlink or
    # replace that name between the open and this check (codex, L3 r2), and then the name leads to
    # another file. The jewel walk runs when the object has other names or its name no longer leads
    # to it; a single-name file still at its name is judged by the name, as above.
    try:
        named = os.stat(path)
        moved = (named.st_dev, named.st_ino) != (st.st_dev, st.st_ino)
    except (OSError, ValueError):
        moved = True
    if st.st_nlink < 2 and not moved:
        return None
    try:
        jewels = _jewel_inodes(policy)
    except ConfinementError as exc:
        return f"the opened file could not be checked against the crown jewels: {exc}"
    hit = jewels.get((st.st_dev, st.st_ino))
    if hit is not None:
        return f"the opened file is another name for the crown jewel {hit[1]}, which the floor denies"
    return None


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
    jewel, and the file editor never renames an existing file or directory: its only rename is
    ``insert`` moving its own temp file onto the target, which walks the target's parents by directory
    fd and refuses a symlink among them (``levain.firing.openhands.tools._floored_move``). Also denies
    the entity's OWN memory STORE files (``own_memory_files`` — continuity/crystal/db + sidecars): the
    file editor has no legit reason to touch the store (the wrap composes it in-process), so it is denied OUTRIGHT
    here (read included), while the seatbelt hand still allows bash to READ it — spore-359, so a hand-
    write can't poison the always-loaded memory the wrap alone composes. The rest of ``<entity>/.levain/``
    is NOT a jewel (the entity's memory is its own to read); the firing's ``assert_entity_isolated``
    moat, not this predicate, is what keeps recall/capture off flow's store."""
    try:
        p = Path(path).expanduser().resolve()
    # ⛔ RuntimeError IS IN THIS CLAUSE FOR THE SAME REASON IT IS IN `doctor.py`'s, AND THIS
    # FUNCTION IS THE ONE WHERE IT MATTERS MOST. `Path("~someuser/...").expanduser()` raises
    # RuntimeError — not OSError, not ValueError — for a user with no passwd entry. MEASURED:
    # `Path("~nosuchuser42/.ssh/authorized_keys").expanduser()` -> `RuntimeError: Could not
    # determine home directory.` The `path` here is supplied BY THE ENTITY, so a confabulating
    # model emitting a `~user` spelling reached a function whose docstring promises FAIL-CLOSED
    # and got neither a denial nor an allow — it got an exception out of the security predicate.
    # ⚠ THE SAME RELEASE ALREADY FIXED THIS CLASS IN `doctor.py` AND DID NOT REACH HERE. Its
    # CHANGELOG entry even records the fix reintroducing an earlier defect "by a new door" — and
    # the door it did not check was the in-process crown-jewels twin. `guard_scoped_by_symptom_
    # misses_the_class`, with the class named in the same release that missed it.
    # (glm-5.2 repo-read seat, run 8de86ccec2bd439f — a review recorded as "produced nothing"
    # whose row was in verdicts.jsonl the whole time. Every claim re-verified here by execution.)
    except (ValueError, OSError, RuntimeError) as exc:
        return f"path {path!r} could not be resolved ({exc}) — refused (fail-closed)"
    if (why := _process_view_reason(path, p)) is not None:
        return why
    for sub in policy.deny_read_write:
        if _ci_within(p, sub):
            return f"{p} is under the crown-jewel store {sub}"
    for f in policy.sqlite_sidecars:
        if _ci_within(p, f):
            return f"{p} is a SQLite sidecar (-wal/-shm/-journal) of a crown-jewel file"
    for f in policy.deny_files:
        if _ci_within(p, f):
            return f"{p} is a crown-jewel credential file"
    if policy.config_file is not None and _ci_within(p, policy.config_file):
        return f"{p} is the confinement config (the entity cannot rewrite its own floor)"
    if policy.ssh_dir is not None and _ci_within(p, policy.ssh_dir):
        return f"{p} is under ~/.ssh key material ({policy.ssh_dir})"
    # Container sockets are write-denied through the SAME ``deny_write_files`` list as the ssh
    # vectors (build_policy arm (ii)), so the loop below must say WHICH it matched or it tells an
    # operator that ``/var/run/docker.sock`` is "an ssh persistence/exec vector
    # (authorized_keys/config/rc)" — a refusal reason that is simply false, in the surface they
    # read to understand their own floor. The DENIAL is correct either way; only the stated
    # reason would have been wrong.
    # ⚠ This hand has no connect() primitive, so it enforces the RENAME/UNLINK arm only. The
    # connect arm is the seatbelt profile's ``network-outbound`` rule and has NO in-process twin.
    # ⛔ AND THE REASON THAT USED TO BE GIVEN FOR ACCEPTING THE GAP WAS A FALSE UNIVERSAL
    # (Diogenes 2026-09-05). It read: "one policy, two enforcers" holds for every OTHER field on
    # this policy and does NOT hold for this one. It does not hold for `deny_write_dirs` either —
    # MEASURED by calling this predicate against every entry: **under `ssh_mode="agent"` exactly ONE
    # entry matches** — always `~/.ssh`, via the `ssh_dir` arm above — and every other entry is
    # enforced by the seatbelt hand alone. **Under `"raw"` NOTHING matches**, because `build_policy`
    # sets `ssh_dir=None` there and the arm is guarded on it, so the seatbelt hand enforces the
    # whole list alone.
    # ⚠ THE COUNT IS DELIBERATELY ABSENT (Diogenes LOW, 2026-09-06). This used to read "**30
    # entries, exactly 1 matched** ... the other 29", and the total is not a property of the policy —
    # it is a property of WHERE THE ENTITY DIRECTORY SITS, because `deny_write_dirs` holds the
    # entity's ANCESTOR directories. It moves with path DEPTH (measured 26..33 across four
    # locations) and the `deny_standard_creds=True` cells shift it again.
    # ⚡⚡ AND THE STRONGEST EVIDENCE THAT IT DOES NOT BELONG HERE IS THAT THREE CAREFUL
    # MEASUREMENTS PRODUCED THREE DIFFERENT ANSWERS AGAINST IDENTICAL CODE: 28, then 30, then a
    # four-location sweep of 26/30/33/33, then an independent re-run of that same sweep giving
    # 26/30/30/33. Nobody was wrong and nothing changed — each harness placed its probe directory
    # at a different depth. A number that three correct measurements disagree about is a number
    # describing the MEASURER.
    # ⭐ **The invariant is what survives all of it: AT MOST ONE entry matches, and when one does it
    # is always `~/.ssh` via the `ssh_dir` arm.** That is the whole of what this argument needs, and
    # it is re-runnable by anyone from any directory.
    # ⛔ "IN EVERY CELL" STOOD HERE AND WAS FALSE IN HALF THE CONFIGURATION SPACE (Diogenes DRIFT,
    # 2026-09-07, re-measured here). `SshMode = Literal["agent", "raw"]`, and sweeping the full
    # product of ssh_mode x deny_standard_creds x allow_container_sockets returns ONE match in every
    # `"agent"` cell and ZERO in every `"raw"` one — raw is half the space, not an exotic corner.
    # ⚠ THE CODE IS RIGHT AND ONLY THE SENTENCE OVERSTATED: `ssh_dir` is None in raw mode by
    # design, so there is no key-confinement target for the arm to name. But it overstated toward
    # the REASSURING reading — a maintainer would have concluded this predicate always covers
    # `~/.ssh`, then used it as a "does the file-editor hand refuse this?" check and got a blanket
    # no-reason in a mode the comment said was impossible. In the file that defines the security
    # floor.
    # ⚠ Stated by DIMENSION and not by cell count on purpose, and the first draft of this very
    # sentence said "the eight-cell sweep ... the four agent cells" before catching itself — the
    # same self-vouching cardinality being deleted at the `deny_write_files` renderer below, in the
    # paragraph written to delete it. The class is easier to name than to avoid.
    # ⚡ The zero cells make the argument below STRONGER, not weaker. So the connect arm is not
    # the lone exception; partial coverage is the NORM across this object, and the honest
    # statement of the gap is that the two hands enforce DIFFERENT
    # SUBSETS by construction — the file editor has no connect and no rename-of-a-directory
    # primitive, so it can only ever cover what its own primitives reach.
    # ⚠ A single-instance claim is what makes a gap look exceptional; naming it as the norm is
    # what stops the next reader treating an uncovered field as an anomaly worth "fixing" here.
    sockets = set(policy.socket_spellings) or set(policy.deny_sockets)
    for wf in policy.deny_write_files:
        if _ci_within(p, wf):
            # WHICH KIND of write-denied file is this? Decided by MEMBERSHIP in the socket set,
            # never by filename. A `wf.name.endswith(".sock")` test stood here for one commit and
            # was replaced on review: this module's whole ssh design exists because NAME-BASED
            # reasoning about security-relevant paths is the thing it refuses (`~/.ssh` is denied
            # by LOCATION for exactly that reason), and a socket that is not spelled `.sock` —
            # `podman.socket`, a systemd-activated name, an operator's own path — would have been
            # reported to them as an "ssh persistence/exec vector".
            # ⚠ `deny_sockets` is RESOLVED-ONLY while `deny_write_files` carries both spellings, so
            # the lexical entry must be resolved before the membership test or it never matches.
            if wf in sockets:
                return (f"{p} is a container/VM daemon socket — reaching an unsandboxed root "
                        f"daemon bypasses the whole floor (spore-725)")
            if wf in policy.trust_spellings:
                return (f"{p} is the derive-trust file — writing it would rebind which repo roots "
                        f"the operator's project memory re-derives against (spore-1308)")
            return f"{p} is a write-protected ssh persistence/exec vector (authorized_keys/config/rc)"
    for mf in policy.own_memory_files:
        # The file editor renames nothing but its own temp file, and has no legit reason to touch the
        # entity's own store
        # (its memory is composed by the host-process wrap, never edited by hand) → denied OUTRIGHT
        # here (read included), same fail-closed stance as the ssh write-files. The SEATBELT hand still
        # allows the READ (bash may `cat` it); denying both hands the WRITE is what closes the poison-
        # the-always-loaded-memory vector (spore-359 / L2 review).
        if _ci_within(p, mf):
            return f"{p} is the entity's own memory store — only `levain wrap` composes it, not the hands"
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
    deny_standard_creds: bool | None = None
    # TRI-STATE (K4a, 2026-07-29): fold _STANDARD_CRED_SUBTREES + _STANDARD_CRED_FILES into the floor?
    #   True  → deny ALWAYS (an explicit operator pin; the drive mode does not soften it)
    #   False → allow ALWAYS (an explicit operator OPT-IN that deliberately survives an unattended
    #           seat — a seat whose job is "open a PR nightly" genuinely needs gh)
    #   None  → ABSENT: derive from the DRIVE MODE (allow only interactive, deny otherwise)
    # It was a plain `bool = False` until an unattended seat existed to distinguish, at which point
    # ABSENT and EXPLICIT-FALSE being indistinguishable made the new default either un-overridable
    # or defeated by every config already on disk. The reason this is not the `efferent_gate: "auto"`
    # string enum two fields down — which solves the identical problem — is that a bool→string change
    # invalidates every config already carrying true/false, and this loader is FAIL-CLOSED, so that
    # is not a graceful degrade: it bricks `levain run`. Resolution + the full argument for why the
    # floor is drive-dependent AT ALL (the rest of it deliberately is not) →
    # `levain.firing.drive.resolve_cred_floor`.
    allow_container_sockets: bool = False
    # spore-725. Default FALSE = the container/VM daemon sockets are DENIED (the floor's normal
    # state). Set true ONLY if this entity genuinely needs to drive containers and the operator
    # accepts that doing so voids the rest of the floor on that machine: the daemon is root and
    # unsandboxed, so anything it will mount, it will read. ⚠ NOT a tri-state like
    # ``deny_standard_creds`` above, deliberately — that one is tri-state because the right
    # answer DIFFERS BY DRIVE MODE (an unattended seat should lose gh; an interactive one should
    # not). A live socket is a total bypass in EVERY drive mode, including the interactive one,
    # so there is no mode for the absent value to derive a different answer from, and a
    # tri-state would only add a third way to spell the same two outcomes.
    efferent_gate: GateSetting = "auto"
    # The K3 EFFERENT GATE (spore-295). "auto" (default) derives the mode from whether a human is
    # driving — ungated at the REPL (the operator watching the stream IS the fan-in), gated for
    # `--task` and any unattended seat. "gated" / "ungated" pin it explicitly. It lives HERE, beside
    # the crown-jewels floor, because the two answer one operator question — what may this entity do
    # to the world — and splitting that answer across two files is how half of it stops being read.
    allow_localhost_outbound: bool = False
    # spore-755. The OPT-OUT of the connect-to-self deny, and the exact sibling of
    # ``allow_container_sockets`` above (allow_* = opt out of a default deny; absent means False =
    # the deny stays on). Default FALSE means the floor denies outbound connect() to THIS host in
    # BOTH ssh_modes (Phill 2026-09-13, 0.4.6 = A+B), closing the FRESH-CONNECTION local-sshd bypass
    # (REPRODUCED 2026-09-13; loopback-loss ACCEPTED). Set true ONLY if this entity genuinely needs a LOCAL
    # service (a dev server it started, argushub on :8420) and the operator accepts that a local sshd
    # can then be asked to read a floor-denied file. NOT a tri-state like ``deny_standard_creds`` —
    # the answer does not vary by DRIVE mode.
    # ⛔ APPENDED LAST, AND THE POSITION IS LOAD-BEARING (codex L3 HIGH#3, 2026-09-13). This field was
    # first inserted BEFORE ``efferent_gate``, which silently shifted every later field's POSITIONAL
    # index in this exported, non-kw-only dataclass: ``ConfinementConfig((), (), "agent", None, False,
    # "gated")`` would then land "gated" in ``allow_localhost_outbound`` (truthy → the connect-to-self
    # deny SILENTLY OFF) and leave ``efferent_gate="auto"`` (an intended-gated run turned auto). This
    # is the EXACT ``own_memory_files`` class from the sibling dataclass; a new field on a public
    # dataclass goes at the END unless the whole class is made keyword-only in a deliberate break.
    # ``test_confinement_config_field_order_is_append_only`` pins it.
    hands_user: str | None = None
    hands_uid: int | None = None
    hands_workspace: Path | None = None
    hands_egress_ports: tuple[int, ...] = ()
    # M2: the dedicated unprivileged user `sudo levain setup-isolation` created for this entity's
    # bash, its numeric id, and the workspace it created for it outside the operator's home; removed
    # by its `--undo`. All three or none: absent means not set up. Appended last, per the rule above.


_CONFINEMENT_CONFIG_NAME = "confinement.json"


def load_confinement_config(entity_dir: Path | str, *, bound_hands: bool = True) -> ConfinementConfig:
    """Load ``<entity>/.levain/confinement.json`` if present, else the default (universal floor only).

    Schema (all fields optional)::

        {"deny_files": ["~/Documents/flow/.env.flow"],
         "deny_subtrees": ["~/some/secrets"],
         "ssh_mode": "agent",
         "deny_standard_creds": false,
         "allow_container_sockets": false,
         "allow_localhost_outbound": false,
         "efferent_gate": "auto"}

    ``~`` is expanded in every path. Unknown keys are IGNORED (forward-compat). A MISSING file returns
    the default (empty declarations, ``ssh_mode="agent"``, ``efferent_gate="auto"``) — the universal
    floor still protects the structurally-knowable crown jewels. A PRESENT-but-MALFORMED file (bad JSON,
    wrong types, an invalid ``ssh_mode`` or ``efferent_gate``) raises :class:`ConfinementError` —
    FAIL-CLOSED: a broken crown-jewels declaration must not silently drop the operator's secrets and hand
    the entity a floor with holes; the caller refuses to grant confined hands and surfaces the error, so
    the operator fixes the config. A mistyped ``efferent_gate`` is held to the SAME standard for the same
    reason — ``"gate"`` silently falling back to the default is a governance declaration the operator
    believes they made and did not."""
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

    # ABSENT is its own state — do NOT default it to False here (K4a). The sentinel must survive the
    # loader so `drive.resolve_cred_floor` can tell "the operator never declared this" (derive from
    # the drive mode) from "the operator explicitly wants creds reachable" (honour it, even for an
    # unattended seat). Collapsing them here is exactly what made the unattended default impossible
    # to express in the first place.
    deny_standard_creds = data.get("deny_standard_creds", None)
    # ``isinstance(True, int)`` is True, so guard against a JSON number sneaking in as a bool — require
    # a real bool (fail-closed: an ambiguous cred-floor declaration must not silently mis-parse).
    # ``None`` is legal ONLY as absence; a literal JSON `null` is rejected rather than silently read
    # as "derive", because an explicit null is an operator TYPING something and meaning it, and we
    # cannot tell which of the three they meant.
    if deny_standard_creds is not None and not isinstance(deny_standard_creds, bool):
        raise ConfinementError(
            f"{base}: deny_standard_creds must be true or false, got "
            f"{deny_standard_creds!r} — fail-closed."
        )
    if "deny_standard_creds" in data and data["deny_standard_creds"] is None:
        raise ConfinementError(
            f"{base}: deny_standard_creds is present but null. Omit the key entirely to let the "
            f"drive mode decide (allowed only at the interactive REPL, denied in every other drive), "
            f"or set it to true/false to pin it (false lets the entity read them) — fail-closed."
        )

    efferent_gate = data.get("efferent_gate", "auto")
    if efferent_gate not in GATE_SETTINGS:
        raise ConfinementError(
            f"{base}: efferent_gate must be one of {', '.join(GATE_SETTINGS)}, got "
            f"{efferent_gate!r} — fail-closed."
        )

    # spore-725. A plain bool, and ABSENT means FALSE (deny) — the opposite treatment from
    # ``deny_standard_creds`` above, whose absence is a live sentinel. Absence here can safely
    # collapse to the default precisely BECAUSE the default is the safe one: an operator who
    # never heard of this key gets the socket denied. ⚠ A literal null is still REFUSED rather
    # than read as absence — same reason as its sibling: a null is an operator typing something
    # and meaning it, and on a security floor we do not guess which thing.
    allow_container_sockets = data.get("allow_container_sockets", False)
    if not isinstance(allow_container_sockets, bool):
        raise ConfinementError(
            f"{base}: allow_container_sockets must be true or false, got "
            f"{allow_container_sockets!r} — fail-closed. Omit the key to keep container/VM "
            f"daemon sockets DENIED (the default)."
        )

    # spore-755. Same absent-means-False/deny treatment as allow_container_sockets — a null is still
    # REFUSED, not read as absence, on the same fail-closed grounds.
    allow_localhost_outbound = data.get("allow_localhost_outbound", False)
    if not isinstance(allow_localhost_outbound, bool):
        raise ConfinementError(
            f"{base}: allow_localhost_outbound must be true or false, got "
            f"{allow_localhost_outbound!r} — fail-closed. Omit the key to keep the connect-to-self "
            f"deny ON in both ssh_modes (the default)."
        )

    # M2. Absent means not set up. Each value must be one setup-isolation could have written, and
    # the three come together: anything else (a typo, another account, a hand-edited path) is
    # refused, not tried.
    hands_user = data.get("hands_user")
    hands_uid = data.get("hands_uid")
    hands_workspace_raw = data.get("hands_workspace")
    hands_workspace: Path | None = None
    present = [k for k in ("hands_user", "hands_uid", "hands_workspace") if k in data]
    if present:
        from levain.firing.hands import HANDS_USER_RE, WORKSPACE_ROOT, hands_user_name

        redo = (" Run `sudo levain setup-isolation --undo` and set it up again — fail-closed.")
        if len(present) != 3:
            raise ConfinementError(
                f"{base}: hands_user, hands_uid and hands_workspace come together; found only "
                f"{', '.join(present)}.{redo}"
            )
        if not isinstance(hands_user, str) or not HANDS_USER_RE.match(hands_user):
            raise ConfinementError(
                f"{base}: hands_user must be the name `levain setup-isolation` recorded, got "
                f"{hands_user!r}.{redo}"
            )
        # bound_hands=False only for setup-isolation itself, so a moved entity can still be undone.
        if bound_hands and hands_user != hands_user_name(entity_dir):
            # The name is derived from this entity's path. A record copied from another entity would
            # share its hands user while locking a different hands.lock (levain.firing.ws_git).
            raise ConfinementError(
                f"{base}: hands_user {hands_user} was set up for another entity directory (this one's "
                f"would be {hands_user_name(entity_dir)}).{redo}")
        if isinstance(hands_uid, bool) or not isinstance(hands_uid, int) or hands_uid <= 0:
            raise ConfinementError(f"{base}: hands_uid must be a positive integer, got {hands_uid!r}.{redo}")
        # Exactly the path setup creates for THIS user: undo (as root) re-groups and re-modes that
        # tree, so a looser check would let a hand-edited path aim it at another directory.
        expected = {str(r / hands_user / "workspace") for r in WORKSPACE_ROOT.values()}
        home = str(Path.home()) + os.sep
        if (
            not isinstance(hands_workspace_raw, str)
            or hands_workspace_raw not in expected
            or (hands_workspace_raw + os.sep).startswith(home)
        ):
            raise ConfinementError(
                f"{base}: hands_workspace must be the path setup-isolation created for {hands_user} "
                f"({' or '.join(sorted(expected))}), got {hands_workspace_raw!r}.{redo}"
            )
        hands_workspace = Path(hands_workspace_raw)
    # The loopback TCP ports the hands user's network boundary allows (Linux, P-1 (a)); setup writes
    # them with the three above and reads them back on a repair run.
    hands_egress_ports: tuple[int, ...] = ()
    if "hands_egress_ports" in data:
        from levain.firing.hands import HandsSetupError, check_egress_ports

        if not present:
            raise ConfinementError(f"{base}: hands_egress_ports belongs to a hands setup, and there is none — "
                                   "fail-closed. Remove the key.")
        try:
            hands_egress_ports = check_egress_ports(data["hands_egress_ports"])
        except HandsSetupError as exc:
            raise ConfinementError(f"{base}: {exc} — fail-closed.") from None

    return ConfinementConfig(
        deny_files=_paths("deny_files"),
        deny_subtrees=_paths("deny_subtrees"),
        ssh_mode=ssh_mode,
        deny_standard_creds=deny_standard_creds,
        allow_container_sockets=allow_container_sockets,
        allow_localhost_outbound=allow_localhost_outbound,
        efferent_gate=efferent_gate,
        hands_user=hands_user,
        hands_uid=hands_uid,
        hands_workspace=hands_workspace,
        hands_egress_ports=hands_egress_ports,
    )


# --- the sandboxed shell (the I/O primitive) -------------------------------------------------

@dataclass(frozen=True)
class ShellResult:
    """One command's result from a :class:`SandboxedShell`. ``output`` merges stdout+stderr (a
    terminal shows both interleaved). ``exit_code`` is the status levain's ``waitpid`` returned for the
    process it spawned; ``signal`` is set instead when a signal ended it. Under bwrap that process is
    bwrap, which waits on bash and exits with bash's status, a signal death of bash as 128 + its
    number, so that arrives in ``exit_code``; a signal levain sends (``interrupt()``) ends bwrap
    itself, the kernel then kills bash (``--die-with-parent``), and it arrives in ``signal``.
    ``timed_out`` is True when the
    command did not finish within the deadline and levain killed its process group (``exit_code`` and
    ``signal`` are then ``None``)."""

    output: str
    exit_code: int | None
    timed_out: bool = False
    signal: int | None = None


# The fixed program each command's bash runs (``bash -c _RUNNER bash``). Everything it is given arrives
# on stdin, a socket levain holds the other end of, as NUL-terminated DATA: the directory to start in,
# the shell's home directory (its workspace, used when the first is gone; the process itself starts in
# ``/``), ``o<OLDPWD>`` or ``-``, the exported variables as ``NAME=value`` fields ended by an empty field, then
# the command. Each is applied with ``builtin cd`` / ``builtin export`` / an assignment; nothing levain
# carries is ever sourced or evaluated, only the command itself. A ``__levain_*`` name is never
# exported (levain never sends one either), so nothing carried can steer the runner's own variables.
# The socket is then moved to fd ``_STATE_FD`` (87: far from the low fds scripts use for lock files)
# and stdin becomes /dev/null.
# The EXIT trap writes back on that fd, and only while it is still a socket (a command that put a
# file there gets nothing written into it): ``P<pwd -P>``, ``O<OLDPWD>`` when set, ``E<NAME>=<value>``
# per exported variable, and ``Z``, each NUL-terminated. levain parses that as data (see
# ``SandboxedShell._carry``) and drops it when the command timed out or waitpid reports a signal. The
# command (or a background job of it) can write to the fd too; what it writes can only become its own
# next directory and environment, which ``cd`` and ``export`` already give it.
# Nothing here reports completion or a status: levain learns both from waitpid.
# bash 3.2 (macOS /bin/bash): with errexit on and an EXIT trap set, a shell that exits through the
# errexit path exits 0 and ``$?`` in the trap is already 0 (an unbound variable under ``-u``, ``${x?}``,
# a syntax error inside ``eval``; bash 5.2 reports them correctly, so the fix applies to bash < 4
# only). The trap turns that case into exit 1 when ``$?`` is 0, ``-e`` is on and the command neither
# ran to its end (``__levain_done``) nor called ``exit``. A call of ``exit`` however spelled (``\exit``,
# ``'exit'``, ``$e``, ``X=1 exit``) reaches the ``exit`` function below, which marks it
# (``__levain_x``); ``builtin exit`` and ``command exit`` skip functions, so ``BASH_COMMAND`` (read
# before any command in the trap: ``[[`` and other commands there overwrite it; assignments do not) is
# matched for those, quotes and backslashes removed.
# A signal never needs the correction: measured on bash 3.2, a shell a signal ends still dies of that
# signal when its EXIT trap calls ``exit 1``, and levain discards what the trap wrote. So no signal is
# trapped (a trapped INT would end the command even when its foreground program handled Ctrl-C), except
# by bash as pid 1 of a pid namespace (bwrap ``--as-pid-1``): pid 1 ignores a signal it has no handler
# for, so there HUP/INT/QUIT/TERM are trapped to skip the write-back (``__levain_g``) and exit 128+n.
# Bash 3.2 is the floor: no mapfile, no ${x@Q}, no associative arrays, no {fd} redirections.
_STATE_FD = 87
_RUNNER = r"""IFS= builtin read -r -d '' __levain_w
IFS= builtin read -r -d '' __levain_h
IFS= builtin read -r -d '' __levain_o
__levain_k=' PWD OLDPWD SHLVL _ '
while IFS= builtin read -r -d '' __levain_e && [[ -n $__levain_e ]]; do
  case $__levain_e in __levain_*) continue ;; esac
  __levain_k="$__levain_k${__levain_e%%=*} "
  builtin export -- "$__levain_e"
done
IFS= builtin read -r -d '' __levain_c
exec 87<&0 </dev/null
for __levain_n in $(builtin compgen -e); do
  case $__levain_k in *" $__levain_n "*) ;; *) builtin unset -v -- "$__levain_n" 2>/dev/null ;; esac
done
builtin cd -- "$__levain_w" 2>/dev/null || {
  builtin cd -- "$__levain_h" 2>/dev/null || {
    builtin printf 'levain: cannot enter %s nor the workspace %s; the command did not run\n' "$__levain_w" "$__levain_h" >&2
    builtin exit 126
  }
  builtin printf 'levain: %s is gone; this command starts in %s\n' "$__levain_w" "$PWD" >&2
}
case $__levain_o in o*) OLDPWD=${__levain_o#o} ;; *) builtin unset -v OLDPWD ;; esac
builtin unset -v __levain_w __levain_h __levain_o __levain_k __levain_e __levain_n \
  __levain_done __levain_g __levain_x __levain_xs __levain_b __levain_m __levain_s __levain_f
exit() {
  __levain_xs=$?
  __levain_x=1
  if [[ $# == 0 ]]; then builtin exit "$__levain_xs"; fi
  builtin exit "$@"
}
__levain_exit() {
  __levain_s=$?
  __levain_f=$-
  __levain_m=$BASH_COMMAND
  { builtin set +euxv; } 2>/dev/null
  builtin trap '' PIPE
  IFS=$' \t\n'
  if [[ -n ${__levain_g-} ]]; then builtin return; fi
  __levain_b=
  if [[ $__levain_s == 0 && -z ${__levain_done-} && -z ${__levain_x-} && $__levain_f == *e* && ${BASH_VERSINFO[0]} -lt 4 ]]; then
    __levain_m=${__levain_m//[\\\'\"]/}
    case $__levain_m in
      exit|exit[[:space:]]*|builtin[[:space:]]exit|builtin[[:space:]]exit[[:space:]]*) ;;
      builtin[[:space:]]--[[:space:]]exit|builtin[[:space:]]--[[:space:]]exit[[:space:]]*) ;;
      command[[:space:]]exit|command[[:space:]]exit[[:space:]]*) ;;
      command[[:space:]]--[[:space:]]exit|command[[:space:]]--[[:space:]]exit[[:space:]]*) ;;
      *) __levain_b=1 ;;
    esac
  fi
  if [[ -S /dev/fd/87 ]]; then
    {
      builtin printf 'P%s\0' "$(builtin pwd -P 2>/dev/null)"
      if [[ -n ${OLDPWD+x} ]]; then builtin printf 'O%s\0' "$OLDPWD"; fi
      for __levain_n in $(builtin compgen -e); do
        builtin printf 'E%s=%s\0' "$__levain_n" "${!__levain_n}"
      done
      builtin printf 'Z\0'
    } >&87 2>/dev/null
  fi
  if [[ -n $__levain_b ]]; then builtin exit 1; fi
}
builtin trap __levain_exit EXIT
if [[ $$ == 1 ]]; then
  builtin trap '__levain_g=1; builtin trap - HUP; builtin kill -HUP $$; builtin exit 129' HUP
  builtin trap '__levain_g=1; builtin trap - INT; builtin kill -INT $$; builtin exit 130' INT
  builtin trap '__levain_g=1; builtin trap - QUIT; builtin kill -QUIT $$; builtin exit 131' QUIT
  builtin trap '__levain_g=1; builtin trap - TERM; builtin kill -TERM $$; builtin exit 143' TERM
fi
builtin eval "$__levain_c"
__levain_done=$?
builtin exit "$__levain_done"
"""

# The PATH a hands-user bash starts with: system and package-manager directories only, never a
# directory under the operator's home (which the hands user cannot enter anyway).
HANDS_PATH = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
SUDO = "/usr/bin/sudo"


@dataclass(frozen=True)
class HandsIdentity:
    """The separate, unprivileged OS user an entity's bash runs as (``levain setup-isolation``,
    :mod:`levain.firing.hands`): its name, uid, home and workspace."""

    user: str
    uid: int
    home: str
    workspace: Path
    #: Linux: the loopback TCP ports the hands user's network boundary lets out (setup's
    #: ``--egress-port``), which the launch probes and relays. Used on Linux only.
    egress_ports: tuple[int, ...] = ()


def hands_for(cfg: "ConfinementConfig", mode: str, *, system: str | None = None) -> HandsIdentity | None:
    """The hands user bash runs as for a session in drive ``mode``, or None (bash runs as the
    operator). The ONE place this is decided, so the binding, the banner and doctor agree.

    - No hands user set up: None.
    - The interactive REPL: None, by design (design §8 i, D4): a human reads every turn there.
    - macOS or Linux, headless or unattended: the hands user. On Linux bwrap runs AS it in an
      allowlisted view that leaves the operator's home out (:func:`_hands_bwrap_argv`). A setup whose account is gone raises (fail-closed): never a silent
      fall-back to the operator.
    - Any other OS: None."""
    if cfg.hands_user is None or cfg.hands_uid is None or cfg.hands_workspace is None:
        return None
    if mode == "interactive":
        return None
    if (system or platform.system()) not in ("Darwin", "Linux"):
        return None
    import pwd

    redo = ("Set it up again: sudo levain setup-isolation --undo, then sudo levain setup-isolation.")
    try:
        entry = pwd.getpwnam(cfg.hands_user)
    except KeyError:
        raise ConfinementError(
            f"the entity's hands user {cfg.hands_user} does not exist — refusing to run bash as you "
            f"instead (fail-closed). {redo}"
        ) from None
    # By uid as well as by name: an account deleted and re-created under the same name is another
    # account, and the workspace's ownership and ACLs were made for the recorded uid (S2 L1b LOW-3).
    if entry.pw_uid != cfg.hands_uid:
        raise ConfinementError(
            f"the entity's hands user {cfg.hands_user} now has uid {entry.pw_uid}, not the uid "
            f"{cfg.hands_uid} setup recorded — refusing to run bash as it (fail-closed). {redo}"
        )
    return HandsIdentity(cfg.hands_user, cfg.hands_uid, entry.pw_dir, cfg.hands_workspace,
                         tuple(cfg.hands_egress_ports))


def _hands_env(hands: HandsIdentity) -> dict[str, str]:
    """The whole environment a hands-user bash starts with (``env -i`` drops everything else, sudo's
    own variables included)."""
    return {"HOME": hands.home, "PATH": HANDS_PATH, "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "TERM": "dumb", "USER": hands.user, "LOGNAME": hands.user}


#: Linux: run first by every hands process, as the hands user, between sudo and what it starts. It
#: joins a NEW, empty session keyring, then execs its argv. The operator's session keyring otherwise
#: crosses sudo (no pam_keyinit in Ubuntu's sudo stack) and bwrap (a user namespace does not detach
#: it): RUN in a VM 2026-10-09, `keyctl print` inside a hands bash printed a key the operator had
#: added to @s. ``python3 -I -S`` (no site, no environment); the keyctl syscall number is the
#: architecture's, and an architecture not listed refuses rather than run with the operator's keyring.
_HANDS_KEYRING_JOIN = r"""import ctypes, os, sys
nr = {"x86_64": 250, "aarch64": 219, "riscv64": 219}.get(os.uname().machine)
if nr is None:
    sys.stderr.write("levain: no keyctl syscall number known for %s; refusing to start with the "
                     "operator's session keyring\n" % os.uname().machine)
    os._exit(126)
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
if libc.syscall(ctypes.c_long(nr), ctypes.c_long(1), ctypes.c_void_p(None)) < 0:
    sys.stderr.write("levain: could not join a new session keyring: %s\n" % os.strerror(ctypes.get_errno()))
    os._exit(126)
os.execv(sys.argv[1], sys.argv[1:])
"""


def hands_prefix(hands: HandsIdentity, *, system: str | None = None) -> list[str]:
    """``sudo -n -u <hands> /usr/bin/env -i <env>``: what goes in front of the sandbox driver. sudo is
    OUTSIDE the sandbox: the shipped profile refuses to exec a setuid binary (measured in the M1 VM
    run), so the profile applies to the hands process, which is the point. ``-n``: never prompt.
    On Linux the keyring join (:data:`_HANDS_KEYRING_JOIN`) follows ``env -i``."""
    argv = [SUDO, "-n", "-u", hands.user, "/usr/bin/env", "-i",
            *(f"{k}={v}" for k, v in _hands_env(hands).items())]
    if (system or platform.system()) == "Linux":
        argv += [HANDS_PYTHON, "-I", "-S", "-c", _HANDS_KEYRING_JOIN]
    return argv


def _require_hands_python() -> None:
    """Linux: every hands process starts through ``python3`` (the keyring join); refuse by name
    without it."""
    if not os.access(HANDS_PYTHON, os.X_OK):
        raise ConfinementError(
            f"{HANDS_PYTHON} is missing: every process run as the entity's own user on Linux starts "
            "through it, to leave your session keyring behind. Install python3 — refusing to run it as "
            "the entity's own user, or as you instead (fail-closed)."
        )


def _require_hands_sudo(hands: HandsIdentity) -> None:
    """Fail closed when the operator can no longer run commands as the hands user (the sudoers rule
    removed, the account retired): bash never falls back to the operator."""
    from levain.launch import child_env

    try:
        r = subprocess.run([SUDO, "-n", "-u", hands.user, "/usr/bin/true"], capture_output=True,
                           stdin=subprocess.DEVNULL, cwd="/", env=child_env(), timeout=30)
        ok, said = r.returncode == 0, r.stderr.decode("utf-8", "replace").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        ok, said = False, str(exc)
    if not ok:
        raise ConfinementError(
            f"bash runs as the entity's own user {hands.user} here, but sudo refused to start it "
            f"({said or 'no reason given'}) — refusing to run bash as you instead (fail-closed). "
            "Set it up again: sudo levain setup-isolation --undo, then sudo levain setup-isolation."
        )


def _hands_signal(hands: HandsIdentity, pgid: int, sig: int) -> bool:
    """Signal a hands-user process group. levain (the operator) may not signal another uid's
    processes, so the signal is sent AS the hands user (the same sudoers rule allows it). False when
    sudo refused or stalled, or kill found nothing to signal."""
    from levain.launch import child_env

    try:
        r = subprocess.run([SUDO, "-n", "-u", hands.user, "/bin/kill", f"-{int(sig)}", "--", f"-{int(pgid)}"],
                           capture_output=True, stdin=subprocess.DEVNULL, cwd="/", env=child_env(), timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def sweep_hands_user(user: str, *, uid: int | None, timeout: float = 5.0) -> str | None:
    """Stop every process of the hands user ``user`` (SIGKILL to all it may signal, sent as it), and
    verify none of the entity's is left. For a session's start and end: a command can leave its
    process group (``setsid``), so killing the groups levain started does not reach everything the
    entity left running. Safe only while one session owns the user
    (:func:`levain.firing.ws_git.hold_hands_session`). None when none is left, else what is.

    The check is :func:`levain.firing.ws_git.entity_session_live`, the one ws-git uses: on macOS
    launchd starts (and restarts) per-user system agents for a uid that ran Apple code, and those
    are not the entity's (S2 L3 r3, codex HIGH). Root and levain's own account are refused. ``uid`` is
    the hands user's id as setup recorded it: no name lookup runs here, since one can block in NSS
    with no bound (S2 L3 r5, codex MED); None (not known) is refused."""
    from levain.firing.ws_git import WsGitError, entity_session_live
    from levain.launch import child_env

    deadline = time.monotonic() + timeout
    if uid is None:
        return f"the id of {user} is not known, so its processes cannot be checked"
    if uid in (0, os.getuid()):
        return f"refusing to stop every process of {user} (uid {uid}): it is root or levain's own account"
    said: str | None = None
    while True:
        # Every step, the check's pgrep and ps included, is bounded by what is left of `timeout`, with
        # no floor, so a stalled one cannot stretch the sweep (S2d codex MED, r3, r4).
        left = deadline - time.monotonic()
        if left > 0:
            try:
                subprocess.run([SUDO, "-n", "-u", user, "/bin/kill", "-9", "--", "-1"], capture_output=True,
                               stdin=subprocess.DEVNULL, cwd="/", env=child_env(), timeout=left)
            except (OSError, subprocess.TimeoutExpired):
                pass
        left = deadline - time.monotonic()
        if left <= 0:   # what the last check found, or that none could run (S2 L3 r5)
            return said or (f"the stop of {user}'s processes ran out of its {timeout:g} s before it "
                            "could check that none is left")
        try:
            if not entity_session_live(uid, timeout=left):
                return None
            said = f"processes of {user} are still running"
        except WsGitError as exc:
            said = str(exc)
        left = deadline - time.monotonic()
        if left <= 0:
            return said
        time.sleep(min(0.2, left))


# Names never carried from one command to the next, whatever the entity set them to: each makes a
# shell (or the dynamic loader) run code or reparse input at startup, or is bash's own bookkeeping.
_NEVER_CARRIED = frozenset({
    "BASH_ENV", "ENV", "PROMPT_COMMAND", "SHELLOPTS", "BASHOPTS", "PS4", "IFS", "CDPATH",
    "GLOBIGNORE", "EXECIGNORE", "POSIXLY_CORRECT", "TMOUT", "PWD", "OLDPWD", "SHLVL", "_",
})
# ``BASH_`` covers BASH_FUNC_* (an exported function), BASH_ENV, BASH_XTRACEFD, BASH_LOADABLES_PATH;
# ``__levain_`` the runner's own variables (the runner refuses them too).
_NEVER_CARRIED_PREFIXES = ("BASH_", "LD_", "DYLD_", "__levain_")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# The most levain reads back from one command's state channel.
_MAX_CARRY_BYTES = 4 * 1024 * 1024
# Finished commands whose output pipe a background job still holds, kept for their later output. Past
# this many, the oldest pipe's read end is closed (its writer then gets EPIPE), so background jobs
# cannot use up levain's file descriptors.
_MAX_LATE = 32

# After a command's bash exits, how long its output pipe may stay open before the result is returned:
# a background job (``server &``) holds the pipe, and its later output is reported with the next run.
_DRAIN_GRACE = 0.2
# A killed group's members get this long after SIGTERM before SIGKILL.
_KILL_GRACE = 1.0
# How long a reap waits for a signal still being sent to the leader's group, longer than a hands
# shell's signal (`_hands_signal`, two sudo runs) takes at its slowest. Past it the leader is left
# unreaped and its group kept.
_REAP_WAIT = 30.0
# The start probe's deadline.
_START_TIMEOUT = 20.0


class _Output:
    """One command's merged stdout+stderr, read by its own thread until every writer has closed the
    pipe, or until :meth:`abandon`. Bounded: past ``_MAX_OUTPUT_CHARS`` it keeps a single truncation
    note and drops the rest."""

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._lock = threading.Lock()
        self._parts: list[str] = []
        self._kept = 0
        self._truncated = False
        self._abandoned = threading.Event()
        self.eof = threading.Event()
        threading.Thread(target=self._pump, daemon=True).start()

    def _add(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            if self._kept < _MAX_OUTPUT_CHARS:
                room = _MAX_OUTPUT_CHARS - self._kept
                self._parts.append(text[:room])
                self._kept += min(len(text), room)
                if len(text) <= room:
                    return
            if not self._truncated:
                self._parts.append(f"\n[output truncated at {_MAX_OUTPUT_CHARS} chars]\n")
                self._truncated = True

    def _pump(self) -> None:
        # poll() with a timeout, not a bare read: the fd is closed only by this thread, so closing it
        # from another thread can never race a read on a reused fd number.
        poller = select.poll()
        poller.register(self._fd, select.POLLIN)
        try:
            while not self._abandoned.is_set():
                try:
                    if not poller.poll(250):
                        continue
                    chunk = os.read(self._fd, 65536)
                except InterruptedError:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                self._add(self._decoder.decode(chunk))
            self._add(self._decoder.decode(b"", final=True))
        finally:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self.eof.set()

    def abandon(self) -> None:
        """Stop reading and close the read end (within the poll interval); a writer then gets EPIPE."""
        self._abandoned.set()

    def take(self) -> str:
        """Everything read since the last ``take`` (the bound counts the whole command's output)."""
        with self._lock:
            text, self._parts = "".join(self._parts), []
        return text


class _Carry:
    """The state channel of one command: levain's end of the socket that is bash's stdin. Reads what
    the runner's EXIT trap writes back (see ``_RUNNER``) until EOF, :meth:`stop`, or the bound, and
    keeps the LAST complete frame: the trap writes after everything the command itself wrote. (A
    background job still holding the state fd can write later; that, too, is only data.)"""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._stopping = threading.Event()
        self.done = threading.Event()
        threading.Thread(target=self._pump, daemon=True).start()

    def _take(self, chunk: bytes) -> bool:
        """Keep ``chunk``; False once over the bound (then nothing carries)."""
        with self._lock:
            if len(self._buf) + len(chunk) > _MAX_CARRY_BYTES:
                self._buf = bytearray()
                return False
            self._buf += chunk
            return True

    def _pump(self) -> None:
        # poll() with a timeout, never a blocking recv: stop() only sets a flag, and this thread then
        # reads whatever is already queued (non-blocking) before it closes. A shutdown() from another
        # thread would wake a blocking recv, but on macOS it also discards the unread queue, the
        # trap's frame included, when a background job keeps the socket open (S2 L2b L5).
        poller = select.poll()
        poller.register(self._sock.fileno(), select.POLLIN)
        try:
            while not self._stopping.is_set():
                try:
                    if not poller.poll(50):
                        continue
                    chunk = self._sock.recv(65536)
                except InterruptedError:
                    continue
                except OSError:
                    return
                if not chunk or not self._take(chunk):
                    return
            while True:
                try:
                    chunk = self._sock.recv(65536, socket.MSG_DONTWAIT)
                except (BlockingIOError, InterruptedError):
                    return
                except OSError:
                    return
                if not chunk or not self._take(chunk):
                    return
        finally:
            try:
                self._sock.close()
            except OSError:
                pass
            self.done.set()

    @property
    def frame(self) -> bytes | None:
        """The last ``...\\0Z\\0``-ended frame read, without its ``Z`` field, or None."""
        with self._lock:
            buf = bytes(self._buf)
        end = buf.rfind(b"\0Z\0")
        if end < 0:
            return b"" if buf.startswith(b"Z\0") else None
        start = buf.rfind(b"\0Z\0", 0, end)
        return buf[start + 3 if start >= 0 else 0: end + 1]

    def send(self, data: bytes) -> None:
        """Write the runner's input from a thread (a driver that never reads stdin must not block
        ``run()``), then half-close, so a read of the state fd by the command gets EOF."""
        sock = self._sock

        def feed() -> None:
            try:
                sock.sendall(data)
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        threading.Thread(target=feed, daemon=True).start()

    def stop(self) -> None:
        """Stop reading: the reader takes what is already queued, then closes the socket (within its
        poll interval). Everything the trap wrote before bash exited is queued by then. The write side
        is shut, which ends a feed still blocked on a driver that never read its input."""
        self._stopping.set()
        try:
            self._sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _never_carried(name: str) -> bool:
    return (not _ENV_NAME.fullmatch(name) or name in _NEVER_CARRIED
            or name.startswith(_NEVER_CARRIED_PREFIXES))


class SandboxedShell:
    """A ``/bin/bash`` under the platform sandbox, one bash process PER COMMAND.

    :meth:`run` spawns ``<driver argv> -c <runner> bash`` in a new session, writes its input to its
    stdin and waits for it. **Completion is levain's ``waitpid`` on that process and the status is its
    waitpid status** (spore-1385, Phill's ruling): nothing the shell prints is parsed for either, so
    code the entity runs cannot end a command early or forge its status. The model this replaced ran
    one bash reading commands from a pipe and took completion and ``$?`` from a line bash printed; bash
    read that pipe one byte at a time on fd 255, so a builtin in a command could read the status line
    ahead of bash and print one of its own (reproduced: ``tests/test_shell_oob_status.py``).

    **What carries from one command to the next, by design: the working directory, ``OLDPWD`` (so
    ``cd -`` works) and the exported variables.** levain holds them in memory, hands them to the next
    command's bash as data on its stdin, and reads them back from the command's EXIT trap over the same
    socket (see ``_RUNNER``). Nothing carried is ever sourced or evaluated, and there is no state file:
    a file another process could write would run code in this shell. Never carried: names that make a
    shell or the loader run code at startup (``_NEVER_CARRIED``; ``BASH_*``, ``LD_*``, ``DYLD_*``), and
    a variable the entity introduced whose name the launch allowlist refuses by rule (credential-shaped,
    or one of levain's own), see :meth:`_carry`. **Nothing else carries:** unexported variables,
    functions, aliases, traps, ``set``/``shopt`` options (``set -e`` included), ``umask``, ``ulimit``,
    the directory stack, ``$?``, ``$!`` and the job table all start fresh in each command, the way they
    do in a new terminal. A command that timed out, or that waitpid reports as ended by a signal,
    carries nothing (levain discards what the runner wrote back); the next command then starts from
    the last one that ended by itself. A command that replaces the EXIT trap
    (``trap ... EXIT``, ``exec prog``) writes nothing back either, nor one that puts something other
    than a socket on the state fd (``_STATE_FD``).

    The sandbox profile fences by PATH at the syscall level, so a ``cd`` into ``$HOME`` still cannot
    read a denied crown jewel.

    - **Output**: one pipe per command, stdout and stderr merged, bounded at ``_MAX_OUTPUT_CHARS``.
      After waitpid the pipe gets ``_DRAIN_GRACE`` to close; a background job that keeps it open
      (``server &``) has its later output returned, labelled, with the next result. At most
      ``_MAX_LATE`` such pipes are kept; past that the oldest is closed and its writer gets EPIPE.
    - **Timeout**: the command's whole process group is killed (SIGTERM, then SIGKILL) and reaped, and
      the shell stays usable.
    - **Background jobs** outlive the command that started them where the sandbox allows it (macOS).
      Under bwrap the command's bash is pid 1 of its pid namespace, so the namespace, and every
      background job in it, ends with the command.
    - **close()** kills every process group this shell started and waits for them to empty.
    - :meth:`run` is single-caller (a concurrent call fails fast); ``close()`` and ``interrupt()`` are
      safe from another thread while a ``run()`` is in flight. A command containing a NUL byte is
      refused (bash could not receive it whole).
    - No PTY: truly interactive programs (``vim``, a password prompt) see /dev/null on stdin.
    - A child that leaves the command's session or process group (``setsid``, ``set -m``, a double
      fork into a new session) escapes the group kill on macOS, as from any shell; it stays under the
      sandbox profile it inherited."""

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
        # The env bash STARTS with: levain's, never one the entity shaped (startup-execution names
        # stripped, so nothing runs before the runner). The entity's exports arrive as data.
        self._env = {k: v for k, v in env.items() if not _never_carried(k)}
        self._default_timeout = default_timeout
        self._leader: _Leader | None = None   # the command running now, if any
        # ⛔ THE POLICY THIS SHELL WAS ACTUALLY CONFINED BY — set by the provider seam, so the
        # caller can cache the EXACT set that got rendered (codex L3 #1, 2026-09-04). Without it the
        # executor refreshed once and `spawn_shell` refreshed AGAIN, discarding the second result:
        # if the source resolved to A in the executor and to B in the provider (an external
        # retarget in between), the shell was confined by A+B while the executor cached only A, so
        # B vanished at the next respawn. The union was NOT monotonic across respawns — the exact
        # property the refresh exists to provide, defeated by refreshing twice and keeping the
        # earlier answer. ⚠ My own comment claimed the double call "costs a resolution and changes
        # nothing"; that is true only if the filesystem is identical at both instants, which is
        # precisely what a TOCTOU fix may not assume.
        self.effective_policy: CrownJewelsPolicy | None = None
        # The carried state (see the class docstring), applied to each command and replaced by what
        # a command that ended by itself writes back.
        self._carried_cwd = str(cwd)
        self._carried_oldpwd: str | None = None
        self._carried_env: dict[str, str] = dict(self._env)
        self._started = False
        self._closed = False
        self._probe_carried = False          # did the last command's state frame come back
        self._run_lock = threading.Lock()    # serialize run(); fail-fast on concurrent misuse
        self._lock = threading.Lock()        # guards _groups / _late across run() and close()
        # Process groups this shell started that may still have members (a command's own group while
        # it runs, a finished command's background jobs after), pgid -> that command's leader, held
        # unreaped until the group is empty so the number cannot be reused (see `_Leader`).
        self._groups: dict[int, _Leader] = {}
        # Finished commands whose pipe a background job still holds: their later output.
        self._late: list[_Output] = []

    # -- lifecycle -----------------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        """True once :meth:`close` ran. A command never closes the shell: ``exit N`` ends that
        command's bash with status N and the next command starts from the carried state."""
        return self._closed

    # Whether each command runs in a pid namespace of its own, whose init gone means its group is
    # empty (:meth:`_group_emptied`). Required on Linux.
    _own_pid_namespace = False

    def start(self) -> "SandboxedShell":
        """Prove the driver runs bash, with a probe command that must print a random token and exit 0
        (a dead or misconfigured sandbox driver fails the spawn here, never as a live-looking shell).
        Returns self (chainable)."""
        if self._started:
            return self
        if self._closed:
            raise ConfinementError("shell is closed")
        self._started = True
        try:
            token = f"__LEVAIN_READY_{os.urandom(8).hex()}__"
            r = self._execute(f"printf '%s\\n' '{token}'", _START_TIMEOUT)
            if not (r.timed_out or r.exit_code != 0 or token not in r.output) and not self._probe_carried:
                # bash ran, but its state frame never came back: something between levain and bash
                # replaced the socket on its stdin (a sudoers `log_input` makes it a pipe), and nothing
                # would carry from one command to the next, silently (S2 L2b L3).
                raise ConfinementError(
                    "the shell's state channel did not answer the start probe (is the socket on bash's "
                    "stdin replaced, e.g. by a sudo log_input setting?) — refusing a shell whose "
                    "directory and exports would silently not carry."
                )
            if r.timed_out or r.exit_code != 0 or token not in r.output:
                driver = os.path.basename(self._argv[0]) if self._argv else "the sandbox driver"
                said = " | ".join(x.strip() for x in r.output.splitlines()[-5:] if x.strip())
                how = "timed out" if r.timed_out else (
                    f"was killed by signal {r.signal}" if r.signal else f"exited {r.exit_code}"
                )
                raise ConfinementError(
                    f"the shell's start probe {how} — {driver} / bash did not run"
                    + (f": {said}" if said else ".")
                )
        except BaseException as exc:  # noqa: BLE001 — cleanup must survive Ctrl-C/SystemExit too
            self.close()
            if not isinstance(exc, Exception):
                raise
            reason = exc if isinstance(exc, ConfinementError) else (
                f"could not spawn the sandboxed shell ({exc}); argv={self._argv[:2]}…"
            )
            raise ConfinementError(str(reason)) from exc
        return self

    # -- the carried state ---------------------------------------------------------------------

    def _input(self, command: str) -> bytes:
        """The runner's stdin: the carried state as NUL-terminated data, then the command."""
        enc = lambda s: s.encode("utf-8", "surrogateescape")   # noqa: E731
        parts = [enc(self._carried_cwd), enc(str(self._cwd)), b"-" if self._carried_oldpwd is None
                 else b"o" + enc(self._carried_oldpwd)]
        parts += [enc(f"{k}={v}") for k, v in self._carried_env.items()]
        parts += [b"", enc(command)]
        return b"\0".join(parts) + b"\0"

    def _carry(self, frame: bytes) -> None:
        """Adopt what a command's EXIT trap wrote back, as data: ``P<cwd>``, ``O<OLDPWD>``,
        ``E<NAME>=<value>`` fields. A malformed frame carries nothing (the previous state stays).
        Kept: every name levain started bash with, and any other name except ``_NEVER_CARRIED`` and
        the ones :func:`levain.launch.refused_by_rule` refuses (credential-shaped, levain's own)."""
        from levain.launch import refused_by_rule

        fields = [f.decode("utf-8", "surrogateescape") for f in frame.split(b"\0")[:-1]]
        if not fields or not fields[0].startswith("P"):
            return
        cwd, oldpwd, env = fields[0][1:], None, {}
        for f in fields[1:]:
            if f.startswith("O") and oldpwd is None:
                oldpwd = f[1:]
            elif f.startswith("E") and "=" in f:
                name, value = f[1:].split("=", 1)
                if _never_carried(name):
                    continue
                if name not in self._env and refused_by_rule(name):
                    continue
                env[name] = value
            else:
                return
        if cwd:
            self._carried_cwd = cwd
        self._carried_oldpwd = oldpwd
        self._carried_env = env

    # -- one command ---------------------------------------------------------------------------

    def _spawn_argv(self) -> tuple[list[str], tuple[int, ...]]:
        """The argv for one command's driver, and fds it inherits (closed here after the spawn)."""
        return [*self._argv, "-c", _RUNNER, "bash"], ()

    def _leader_made(self, leader: _Leader) -> None:
        """Hook: a command's driver process exists and is watched; nothing of it has run yet."""

    def _after_spawn(self, pgid: int) -> None:
        """Hook: a command's process group exists and its bash is waiting for its input (the bwrap
        shell confirms it is in its cgroup leaf). Raising here refuses the command before it can run."""

    def _after_command(self, pgid: int) -> None:
        """Hook: a command's bash has been reaped and its result is about to be returned."""

    def _spawn(self) -> tuple[_Leader, _Output, _Carry]:
        # The argv first: it may raise (the bwrap shell refuses there, and records the claim), and
        # nothing below exists yet to leak (S2 L2b L6).
        argv, pass_fds = self._spawn_argv()
        made: list[int] = []
        try:
            if platform.system() == "Linux" and not self._own_pid_namespace:
                raise ConfinementError(
                    "on Linux a shell must run each command in a pid namespace of its own (bwrap), the "
                    "only way levain can tell that a command left nothing running — refusing (fail-closed)."
                )
            rd, wr = os.pipe()
            made += (rd, wr)
            ours, theirs = socket.socketpair()
        except BaseException:
            for fd in (*made, *pass_fds):
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise
        proc: subprocess.Popen[bytes] | None = None
        try:
            proc = subprocess.Popen(
                argv,
                stdin=theirs.fileno(),
                stdout=wr,
                stderr=wr,                  # merged, as a terminal shows them
                # `/`, which every user can enter: the runner then changes to the carried directory,
                # so the spawning process never needs access to it (a hands-user workspace).
                cwd="/",
                env=self._env,
                # A NEW session, so this command's process group (pgid == its pid) can be signalled
                # as a whole: its children, and a timed-out command's, would otherwise be orphaned.
                start_new_session=True,
                # WELD (apparatus L2 HIGH): no INHERITED fd may bypass the profile (seatbelt checks
                # open(), not read() of an already-open fd). CALLER CONTRACT: no crown-jewel fd may be
                # open in this process at spawn time.
                close_fds=True,
                pass_fds=pass_fds,
            )
            # Watched from here on, never reaped by Popen until its group is empty (see `_Leader`).
            leader = _Leader(proc)
            self._leader_made(leader)
        except BaseException:
            os.close(rd)
            ours.close()
            if proc is not None:   # the watch could not be set up; no input was sent, nothing ran
                self._signal(proc.pid, signal.SIGKILL)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            raise
        finally:
            os.close(wr)
            theirs.close()
            for fd in pass_fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
        out = _Output(rd)
        carry = _Carry(ours)
        with self._lock:
            # Registered under the lock, against a close() that ran meanwhile (S2 L3 r1, codex HIGH):
            # close() sets `_closed` before it takes its snapshot of the groups, so either it sees
            # this group and kills it, or this sees `_closed` and kills it here.
            closed = self._closed
            if not closed:
                self._groups[proc.pid] = leader
        if closed:
            if not self._kill_group(proc.pid, leader):
                with self._lock:
                    self._groups[proc.pid] = leader   # kept for close() to report, never dropped
                self._note_leftovers()
            out.abandon()
            carry.stop()
            raise ConfinementError("shell is closed")
        self._leader = leader
        return leader, out, carry

    def _kill_group(self, pgid: int, leader: _Leader) -> bool:
        """SIGTERM the group, give it ``_KILL_GRACE``, SIGKILL what is left, and wait for the group
        to empty; then reap its leader. False when it did not empty, when the SIGKILL was not
        delivered, or when the reap had to wait too long: the leader stays unreaped (the number stays
        this group's) and the caller must not report it killed."""
        self._signal_group(pgid, leader, signal.SIGTERM)
        leader.wait(_KILL_GRACE)
        delivered = True
        if not self._group_emptied(pgid, leader, 0.2):
            delivered = self._signal_group(pgid, leader, signal.SIGKILL)
        leader.wait(5.0)
        # A SIGKILL that may not have landed leaves the group held, whatever it looks like (r5).
        return (delivered and leader.exited and self._group_emptied(pgid, leader, 5.0)
                and self._reap(pgid, leader))

    def _signal_group(self, pgid: int, leader: _Leader, sig: int) -> bool:
        """Signal a group only while its leader is unreaped: the leader is held unreaped for the send
        (:meth:`_Leader.hold_for_signal`), so a number another thread has just freed is never
        signalled (S2d codex HIGH). No lock is held across the send, which for a hands shell is sudo,
        so a slow one never holds up a Ctrl-C or the shell's bookkeeping (S2 L3 r3, r4). True when
        the signal was delivered, or there was nothing left to deliver it to (see :meth:`_signal`)."""
        if not leader.hold_for_signal():
            return True   # reaped: its group was emptied and is not this shell's any more
        try:
            return self._signal(pgid, sig)
        finally:
            leader.signal_sent()

    def _reap(self, pgid: int, leader: _Leader) -> bool:
        """Reap a leader whose group is empty, then forget the group. A signal between the two finds
        the leader reaped and is not sent. False, the group kept, when a signal to it is still being
        sent after ``_REAP_WAIT``."""
        if not leader.reap(timeout=_REAP_WAIT):
            return False
        with self._lock:
            if self._groups.get(pgid) is leader:
                del self._groups[pgid]
        return True

    @staticmethod
    def _signal(pgid: int, sig: int) -> bool:
        """Send ``sig`` to group ``pgid``. True when it was delivered or the group is gone (ESRCH)."""
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        return True

    def _group_emptied(self, pgid: int, leader: _Leader, timeout: float) -> bool:
        """Whether group ``pgid`` has no live member (polled up to ``timeout``): THE authority every
        reap of a leader rests on. Here, macOS's: a process table read in one call
        (:func:`_group_gone`). Linux has no such call and /proc is read one entry at a time, which a
        member that keeps forking can evade (S2 L3 r3, r4, r5), so a Linux shell must run each command
        in a cgroup leaf of its own and answer from that (:class:`_BwrapShell`); any other shell is
        refused there at its spawn."""
        if platform.system() != "Darwin":
            return False
        return _group_gone(pgid, timeout=timeout)

    def _prune_groups(self) -> None:
        """Reap the leader of each group that has emptied (:meth:`_group_emptied`), and forget the
        group. Only then may its number be reused, and it is no longer signalled."""
        with self._lock:
            groups = list(self._groups.items())
        # Decided outside the lock: `_group_emptied` may run pgrep (S2 L3 r3).
        for pgid, leader in groups:
            if leader.reaped or (leader.wait(0) and self._group_emptied(pgid, leader, 0.0)):
                self._reap(pgid, leader)
            elif leader.exited:
                leader.release_watch()

    def _keep_late(self, out: _Output) -> None:
        with self._lock:
            self._late.append(out)
            while len(self._late) > _MAX_LATE:
                self._late.pop(0).abandon()

    def _late_output(self) -> str:
        with self._lock:
            late, still = self._late, []
            text = []
            for out in late:
                t = out.take()
                if t:
                    text.append(t)
                if not out.eof.is_set():
                    still.append(out)
            self._late = still
        joined = "".join(text)
        if not joined:
            return ""
        if not joined.endswith("\n"):
            joined += "\n"
        return f"[output from background jobs of earlier commands]\n{joined}[end of background output]\n"

    def _execute(self, command: str, deadline_s: float) -> ShellResult:
        data = self._input(command)
        leader, out, carry = self._spawn()
        pgid = leader.pid
        timed_out = False
        try:
            # bash waits on its stdin until the input arrives, so a refusal here stops the command
            # before any of it runs.
            self._after_spawn(pgid)
            carry.send(data)
            if not leader.wait(deadline_s):
                timed_out = True
                if not self._kill_group(pgid, leader):
                    # The signals did not land (sudo refused or stalled, say): the command may still
                    # be running, so it is not reported as killed, and the shell is closed (S2 L1b
                    # MED-2, L2b L4).
                    out.abandon()
                    carry.stop()
                    self.close()
                    raise ConfinementError(
                        "the command timed out and levain could not stop it: its process group is "
                        "still running. The shell was closed; check for the leftover processes "
                        f"(process group {pgid})."
                    )
        except BaseException:
            self._kill_group(pgid, leader)
            carry.stop()
            if not out.eof.wait(_DRAIN_GRACE):
                out.abandon()   # a survivor holding the pipe must not keep the reader (S2 L3 r1)
            raise
        finally:
            self._leader = None
        until = time.monotonic() + _DRAIN_GRACE
        carry.done.wait(_DRAIN_GRACE)
        out.eof.wait(max(0.0, until - time.monotonic()))
        carry.stop()
        # The reader takes what is queued and closes within its poll interval; the frame is read
        # only after that, or a background job holding the state fd would leave it half-read
        # (S2 L3 r1, codex LOW).
        carry.done.wait(1.0)
        self._after_command(pgid)
        text = out.take()
        if not out.eof.is_set():
            self._keep_late(out)
        if timed_out:
            return ShellResult(output=text, exit_code=None, timed_out=True)
        rc = leader.status
        if rc is None:
            # The leader exited before its watch was set up (its driver failed before the input was
            # sent), so its status comes from reaping it: the group is stopped and emptied first.
            if not self._kill_group(pgid, leader):
                self.close()
                raise ConfinementError(
                    "the shell's driver exited at once and levain could not stop what it left "
                    f"running (process group {pgid}). The shell was closed."
                )
            rc = leader.status
        self._prune_groups()
        if rc is not None and rc < 0:
            return ShellResult(output=text, exit_code=None, signal=-rc)
        frame = carry.frame
        self._probe_carried = frame is not None
        if frame is not None:
            self._carry(frame)
        return ShellResult(output=text, exit_code=rc)

    def run(self, command: str, *, timeout: float | None = None) -> ShellResult:
        """Run ``command`` in a fresh bash that starts from the carried state; return its output, and
        the exit status levain's waitpid reported (or the signal that ended it, or a timeout)."""
        # Single-caller: a concurrent call would race the carried state and the late-output buffer.
        # close() / interrupt() deliberately do NOT take this lock.
        if not self._run_lock.acquire(blocking=False):
            raise ConfinementError(
                "SandboxedShell.run() is single-caller; a command is already running on this shell."
            )
        try:
            if self._closed or not self._started:
                raise ConfinementError(
                    "shell is not running (call start() first, and not after close())"
                )
            if "\0" in command:
                raise ConfinementError(
                    "refusing a command that contains a NUL byte: bash cannot receive it whole."
                )
            self._prune_groups()
            late = self._late_output()
            r = self._execute(command, self._default_timeout if timeout is None else timeout)
            if late:
                r = replace(r, output=late + r.output)
            return r
        finally:
            self._run_lock.release()

    def interrupt(self) -> None:
        """Best-effort SIGINT to the running command's process GROUP (Ctrl-C it and its children).
        On Linux every signal levain sends a command kills it (:meth:`_BwrapShell._signal`), so there
        Ctrl-C ends the command, as bwrap's parent-death kill did before. Never raises."""
        leader = self._leader
        # An exited leader is unreaped or gone; neither is Ctrl-C'd.
        if leader is not None and not leader.exited:
            self._signal_group(leader.pid, leader, signal.SIGINT)

    def close(self) -> None:
        """Kill every process group this shell started (SIGTERM, then SIGKILL), wait for each to
        empty, and reap its leader. A group that does not empty is KEPT, leader unreaped, and named
        by :attr:`unemptied_groups`; calling close() again retries it. Never raises."""
        self._closed = True
        # Reap what already emptied: those are not signalled.
        self._prune_groups()
        with self._lock:
            groups = list(self._groups.items())
            late, self._late = self._late, []
        for out in late:
            out.abandon()
        # Each leader is still unreaped, so each number is still this shell's group (see `_Leader`).
        for pgid, leader in groups:
            self._signal_group(pgid, leader, signal.SIGTERM)
        deadline = time.monotonic() + _KILL_GRACE
        undelivered: set[int] = set()
        for pgid, leader in groups:
            if not self._group_emptied(pgid, leader, max(0.0, deadline - time.monotonic())):
                if not self._signal_group(pgid, leader, signal.SIGKILL):
                    undelivered.add(pgid)   # kept, whatever it looks like (r5)
        for pgid, leader in groups:
            leader.wait(5.0)
            emptied = leader.reaped or (pgid not in undelivered and leader.exited
                                        and self._group_emptied(pgid, leader, 5.0))
            if not (emptied and self._reap(pgid, leader)):
                # close() never raises; a group it could not empty is said, not hidden, and the
                # session keeps the workspace lock while it lives (S2 L3 r2, codex HIGH).
                _log.warning("levain: the shell's process group %d is still running after close()", pgid)
        self._note_leftovers()

    @property
    def unemptied_groups(self) -> tuple[int, ...]:
        """After :meth:`close`: the process groups it could not empty (their leaders still held)."""
        with self._lock:
            return tuple(sorted(self._groups)) if self._closed else ()

    @property
    def hands_user(self) -> str | None:
        """The account this shell's commands run as, when it is not levain's own."""
        return None

    def _note_leftovers(self) -> None:
        with _UNEMPTIED_LOCK:
            if self.unemptied_groups:
                _UNEMPTIED_SHELLS.add(self)
            else:
                _UNEMPTIED_SHELLS.discard(self)

    def __enter__(self) -> "SandboxedShell":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()


# Closed shells holding a group they could not empty, kept (strongly: each holds its groups' unreaped
# leaders) until a retry empties them. A session asks before it lets go of its workspace lock.
_UNEMPTIED_SHELLS: "set[SandboxedShell]" = set()
_UNEMPTIED_LOCK = threading.Lock()


def unemptied_shell_groups(hands_user: str) -> list[int]:
    """Process groups of closed shells running as ``hands_user`` that are still not empty, after one
    more close() of each (which kills and reaps what it can now)."""
    with _UNEMPTIED_LOCK:
        shells = [s for s in _UNEMPTIED_SHELLS if s.hands_user == hands_user]
    left: list[int] = []
    for s in shells:
        s.close()
        left += s.unemptied_groups
    return left


# --- the provider seam (mirrors levain.daemon.DaemonProvider) --------------------------------

class ConfinementProvider(ABC):
    """One thin provider per OS. ``render_profile`` is PURE (no I/O) so the generated sandbox text is
    fully testable without touching the system; ``spawn_shell`` shells out to the platform sandbox
    driver. macOS (:class:`SeatbeltProvider`) shipped first and Linux (:class:`BwrapProvider`, K4c)
    shipped against this contract with two additions, ``available()`` and
    ``enforces_localhost_deny`` — which is the seam doing its job. A container backend remains a pure addition. The macOS crown-jewels denylist
    is the requirements spec for every provider; the Linux one was re-derived against it row by row
    and measured, never ported."""

    #: Whether this provider can deny an outbound connect to THIS host (``deny_localhost_outbound``,
    #: spore-755). A provider that cannot must refuse to spawn while that deny is requested, never
    #: run without it; the operator's opt-out is ``allow_localhost_outbound`` in confinement.json.
    enforces_localhost_deny: bool = False
    #: Whether this provider enforces that deny by removing IP networking from bash (bwrap's
    #: ``--unshare-net``), which the banner must say, since pip/git/curl then fail inside bash.
    #: Pathname unix sockets still reach the host: see ``OFFLINE_RESIDUAL``.
    localhost_deny_removes_network: bool = False

    def localhost_deny_ready(self) -> bool:
        """Whether the deny can actually be applied on THIS host right now (a provider whose
        mechanism needs a host capability beyond :meth:`available` overrides this)."""
        return self.enforces_localhost_deny


    @abstractmethod
    def available(self) -> bool:
        """True iff THIS provider's sandbox driver is present + executable ON THIS HOST right now.

        Part of the CONTRACT rather than a module-level function, because the driver differs per OS
        (``sandbox-exec`` / ``bwrap`` / a container runtime) while the QUESTION does not. The previous
        shape asked the polymorphic :func:`select_provider` which provider applies and then checked
        the macOS driver unconditionally — one requirement with two enforcement models, which is the
        exact trap ``spore-418`` names for the confinement policy itself, arriving in the GATE. That
        shape had two faces the moment a second provider existed: on Linux a working ``bwrap`` floor
        would never be offered (the macOS driver is absent), and on a Mac asked about Linux it would
        answer True from a host that cannot know. Neither is a platform branch away — the check has
        to travel WITH the provider."""

    @abstractmethod
    def render_profile(self, policy: CrownJewelsPolicy) -> str:
        """Render ``policy`` into the platform's native sandbox profile text (no I/O)."""

    def spawn_shell(
        self,
        policy: CrownJewelsPolicy,
        *,
        env: dict[str, str] | None = None,
        default_timeout: float = 120.0,
        hands: HandsIdentity | None = None,
    ) -> SandboxedShell:
        """Start a stateful shell confined by ``policy``. The returned :class:`SandboxedShell` is
        already ``start()``\\ ed and carries the policy it was confined by in ``effective_policy``.
        With ``hands`` (see :func:`hands_for`), bash runs as that user; a provider that cannot do
        that refuses, never runs bash as the operator instead.

        ⛔⛔ **THE CALLER REFRESHES THE SOCKET FLOOR, NOT THIS METHOD — AND A METACLASS GUARD THAT
        TRIED TO ENFORCE THE OPPOSITE WAS DELETED AFTER FIVE VERSIONS AND SEVEN BYPASSES**
        (Phill ruled 2026-09-04, on codex L3 round 7). The history is the argument, so it is kept:

        | v | mechanism | defeated by |
        |---|-----------|-------------|
        | 1 | ``cls.__dict__``      | a mixin shadowing ``spawn_shell`` |
        | 2 | the module global     | ``importlib.reload`` |
        | 3 | ``parents[0]``        | *over*-refused valid multiple inheritance (broke import) |
        | 4 | ``cls.__mro__[1:]``   | a mixin shadowing it AGAIN |
        | 5 | an inherited marker   | marker poisoning · a derived metaclass · multiple marked roots |

        …plus post-definition assignment, which no version ever closed. ⚡ **The defeat surface is
        UNBOUNDED: Python does not support making a class hierarchy tamper-proof against its own
        subclasses, so there is no closed set of ways to shadow an attribute and every version was
        defeated in a NEW way.** Seven cases were enumerated into a table and an outside lineage
        immediately found three more. Enumeration cannot terminate here.
        ⚠ And the guard bought almost nothing even when it worked: an adversarial provider never
        needed to override this method — it could return anything at all from ``_spawn_shell_impl``,
        or not inherit from this class. **It blocked one spelling of a thing with many spellings.**

        ▶ **WHAT ACTUALLY ENFORCES THE INVARIANT NOW: the refresh happens in BOTH places that own
        it** — upstream in :meth:`SandboxedBashExecutor._ensure_shell`, and again HERE in this
        template method before ``_spawn_shell_impl`` runs. Double-refreshing is safe (monotonic
        union — see the comment below), and the second one is what covers a consumer that calls
        ``spawn_shell`` directly instead of going through ``_ensure_shell``. A provider that
        implements the documented ``_spawn_shell_impl`` seam never receives an unrefreshed policy.

        ⛔ **WHAT IS NOT ENFORCED, STATED PLAINLY BECAUSE A FALSE VERSION OF THIS STOOD HERE:** a
        subclass overriding ``spawn_shell`` ITSELF, rather than the ``_spawn_shell_impl`` hook,
        skips the refresh. That is the residual knowingly accepted when the metaclass was deleted
        (the defeat surface is unbounded; enumeration cannot terminate). It is an ACCIDENTAL-override
        risk and not only an adversarial one, because ``spawn_shell`` is the public non-underscore
        name that looks like the thing to override.

        ⛔ The K4c branch, written before this seam existed, had ``BwrapProvider`` (and its own
        ``SeatbeltProvider``) override ``spawn_shell`` DIRECTLY, and a docstring then claimed it
        inherited the refresh anyway. It did not; that was spore-768 waiting to return on Linux. The
        2026-09-30 port moved ``BwrapProvider`` onto ``_spawn_shell_impl``, and
        ``test_no_shipped_provider_overrides_spawn_shell`` now asserts it for every provider.
        ⚖ This is not a reversal of "make the refresh structural" — it is a stronger form of it. The
        metaclass tried to make it impossible to SKIP a step; moving it upstream makes the step not
        exist at this layer at all. Same move as the conversation-floor key: **stop balancing,
        dissolve.**"""
        # ⛔⛔ THE REFRESH IS BACK HERE, AND DELETING IT WAS MY ERROR — complement MED + glm-5.2 MED,
        # CONVERGENT (2026-09-04). glm's sentence is the correction and it is exactly right:
        # **"the metaclass was correctly deleted (unbounded defeat surface), but the template-method
        # refresh it was protecting was deleted alongside it. ONLY THE METACLASS NEEDED TO GO."**
        # ⚡ I conflated two separate things — a GUARD that tried to make the refresh unskippable
        # (impossible in Python, seven bypasses across five versions, correctly deleted) and the
        # REFRESH ITSELF sitting at the layer that owns it (structural, cheap, correct). Removing
        # the refresh left the invariant enforced at exactly ONE call site, so any second consumer —
        # `BwrapProvider` on the held branch, a future debug or preview harness, a test — that calls
        # `spawn_shell(policy)` without refreshing first renders the BUILD-TIME `deny_sockets`. That
        # is the spore-768 regression this whole release exists to close, reintroduced by the fix
        # for the guard that was protecting against it.
        # ⚠ AND DOUBLE-REFRESHING IS SAFE NOW, which it was not in round 4: the caller keeps the
        # policy the SHELL reports (`effective_policy`), not its own earlier snapshot, and
        # `refresh_socket_denies` is a monotonic union — so a second resolution can only ever widen
        # what the first produced. Round 4's lost-update came from keeping the EARLIER answer.
        try:
            refreshed = refresh_socket_denies(policy)
        except Exception as exc:
            raise FloorRefreshError(str(exc)) from exc
        _refuse_multiply_linked_jewels(refreshed)
        # `hands` is passed only when set, so a provider written before it existed still works for
        # every operator-uid shell, and refuses a hands one. Decided from the signature, never from a
        # TypeError, which a hands-capable provider can raise for any other reason (S2 L1b LOW-5).
        if hands is not None and not _accepts_hands(self._spawn_shell_impl):
            raise ConfinementError(
                f"{type(self).__name__} cannot run bash as the entity's own user — refusing to run "
                "it as you instead (fail-closed)."
            )
        shell = self._spawn_shell_impl(
            refreshed, env=env, default_timeout=default_timeout,
            **({"hands": hands} if hands is not None else {}),
        )
        # ⛔ Reject a non-shell AT THE SOURCE (codex L3, 2026-09-04): tolerating a falsy sentinel
        # only MOVED the crash to the caller's `.run`, as an AttributeError that `__call__` does not
        # convert into an in-band refusal.
        if not isinstance(shell, SandboxedShell):
            raise ConfinementError(
                f"{type(self).__name__}._spawn_shell_impl returned {type(shell).__name__}, not a "
                "SandboxedShell — refusing to hand back bash hands without a verified confined "
                "shell (fail-closed)."
            )
        shell.effective_policy = refreshed
        return shell

    @abstractmethod
    def _spawn_shell_impl(
        self,
        policy: CrownJewelsPolicy,
        *,
        env: dict[str, str] | None = None,
        default_timeout: float = 120.0,
        hands: HandsIdentity | None = None,
    ) -> SandboxedShell:
        """Platform half of :meth:`spawn_shell`. ``policy`` arrives with its socket connect arm
        ALREADY re-resolved for this spawn — render it as given; do not re-derive it here."""

    def hands_file(self, policy: CrownJewelsPolicy, hands: HandsIdentity, op: str, path: str,
                   data: bytes = b"", *, timeout: float = 60.0) -> bytes:
        """Run one file operation AS the hands user, under this provider's floor, inside the hands
        workspace: the file editor's reads and writes for an entity whose bash runs as that user (the
        editor never touches a file with the operator's rights there). ``op`` is ``read`` (returns
        the file's bytes), ``stat``, ``list`` (see :data:`_HANDS_FILE_HELPER` for their output) or
        ``write`` (``data`` replaces the file). Raises :class:`FileNotFoundError` for a missing path,
        :class:`HandsFileRefused` with the reason for a refusal, :class:`OSError` when the helper
        could not run; a provider that cannot run as another user raises :class:`ConfinementError`."""
        raise ConfinementError(
            f"{type(self).__name__} cannot use files as the entity's own user — refusing to use them as "
            "you instead (fail-closed)."
        )

    def hands_write(self, policy: CrownJewelsPolicy, hands: HandsIdentity, path: str, data: bytes,
                    *, timeout: float = 120.0) -> None:
        """Write ``data`` to ``path`` as the hands user (:meth:`hands_file` ``write``)."""
        self.hands_file(policy, hands, "write", path, data, timeout=timeout)


def _accepts_hands(fn: Any) -> bool:
    """Whether ``fn`` takes a ``hands`` keyword (named, or through ``**kwargs``)."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "hands" and p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)
               or p.kind is p.VAR_KEYWORD for p in params)


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
    config``) for the bash hand while the file editor still denies it — a two-enforcer split.
    ⚠ Not made exact with step (5)'s compare: a rebind lands inside the case-sensitive ssh tmpfs, so a
    mask on a case variant there is another dentry and would not cover it (L1, 2026-10-03). Cost: a
    deny of ``~/.ssh/Known_Hosts`` on a case-sensitive volume also drops the distinct
    ``known_hosts`` rebind."""
    p = path.resolve()
    if any(_ci_within(p, f) for f in policy.deny_files):
        return True
    return any(_ci_within(p, sub) for sub in policy.deny_read_write)


class SeatbeltProvider(ConfinementProvider):
    """macOS ``sandbox-exec`` (seatbelt / SBPL) provider.

    Renders a ``(version 1)(allow default)`` profile that DENIES the crown jewels — the polarity flip:
    the sandbox is a FLOOR, not a jail. Deny both read AND write on the subtrees (the entity can
    neither exfil nor corrupt flow's memory / a sibling's memory); deny the credential FILES; and for
    ``ssh_mode="agent"`` deny read+write on ALL of ``~/.ssh`` (see the module docstring — location-
    based, not ``id_*``) while re-allowing ``known_hosts`` (r+w) and ``config`` (r) so agent-auth
    still works. ⚠ ``config`` is read-re-allowed and its WRITE stays denied in BOTH modes, which
    the previous wording omitted."""

    enforces_localhost_deny = True  # ``(deny network-outbound (remote ip "localhost:*"))``


    def available(self) -> bool:
        """The seatbelt driver is ``/usr/bin/sandbox-exec``. Delegates to the long-standing module
        function so there is ONE definition of "is the macOS driver usable", not two."""
        return sandbox_exec_available()

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

        if policy.deny_sockets:
            lines.append(";; CONTAINER/VM DAEMON SOCKETS — the ONE deny in this profile that is not a")
            lines.append(";; file rule, and it has to be. A `file-read* file-write*` deny does NOT")
            lines.append(";; block connect() to a unix socket: MEASURED 2026-09-04 with both docker")
            lines.append(";; socket spellings in deny_files, the exploit still returned the jewel.")
            lines.append(";; Reaching an unsandboxed root daemon defeats every other rule here, so")
            lines.append(";; without this line the rest of the profile is decorative wherever a")
            lines.append(";; container runtime is installed. Paths are RESOLVED — seatbelt")
            lines.append(";; canonicalises for network-outbound, and a lexical entry is INERT.")
            lines.append(";; The rename/unlink half lives in the write-deny rules below.")
            lines.append("(deny network-outbound")
            for p_ in policy.deny_sockets:
                lines.append(f'    (literal "{_sbpl_string(str(p_))}")')
            lines.append(")")
            lines.append("")

        if policy.deny_localhost_outbound:
            lines.append(";; CONNECT-TO-SELF (spore-755) — deny outbound to THIS host so a local")
            lines.append(";; sshd (Remote Login) can't be asked, as unsandboxed root, to read a file")
            lines.append(";; the floor denies via the agent socket agent-mode forwards. Same class as")
            lines.append(";; the daemon-socket deny above: forwarding a credential to an unsandboxed")
            lines.append(";; root reader makes a file-deny decorative. ``localhost`` is a seatbelt")
            lines.append(";; KEYWORD matching every LOCAL-INTERFACE address (127.x, ::1, the LAN IP —")
            lines.append(";; MEASURED 2026-09-13), so this covers the host's own addresses, not")
            lines.append(";; loopback only; remote hosts stay reachable. ``:*`` = every port —")
            lines.append(";; seatbelt rejects a per-port loopback literal, so all-or-nothing is the")
            lines.append(";; only granularity it offers.")
            lines.append('(deny network-outbound (remote ip "localhost:*"))')
            lines.append("")

        if policy.deny_keychain:
            lines.append(";; THE KEYCHAIN (spore-1245) — on with the standard cred floor (any non-interactive drive by")
            lines.append(";; default). Without it, `security find-generic-password -w` and every")
            lines.append(";; credential helper (`git credential-osxkeychain`, gh's stored token) read")
            lines.append(";; the operator's secrets from inside this sandbox (measured 2026-10-01).")
            lines.append(";; Server-authenticated HTTPS and git over the forwarded ssh agent are")
            lines.append(";; unaffected (measured). The systemkeychain endpoint is denied by name;")
            lines.append(";; it was not exercised (no test item can be put in System.keychain unprivileged).")
            for svc in _KEYCHAIN_MACH_SERVICES:
                lines.append(f'(deny mach-lookup (global-name "{svc}"))')
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
            # ⛔ TWO CLASSES SHARE THIS LIST, SO IT RENDERS AS TWO BLOCKS WITH TWO ACCURATE
            # HEADERS (Diogenes 2026-09-05). One "ssh persistence/exec vectors (authorized_keys*,
            # config, rc)" header used to stand over the WHOLE list while TEN OF ITS LITERALS were
            # container/VM daemon sockets — MEASURED BY RENDERING A REAL PROFILE, not by reading.
            # ⚠ THIS SAID "10 of the 20 literals" AND NO CONFIGURATION PRODUCES 20 (Diogenes LOW,
            # 2026-09-06; re-measured independently 2026-09-06 and again 2026-09-07). Swept the
            # full product of `build_policy`'s ssh_mode x deny_standard_creds x
            # allow_container_sockets: `len(deny_write_files)` is 14 wherever
            # allow_container_sockets is False and 4 wherever it is True. The rendered split is
            # 4 ssh + 10 socket = 14. The RATIO is
            # what this sentence needs and the TOTAL is what rots, so the total is gone: a reader
            # checking the new two-block render against "20" finds 14 and has to conclude six
            # literals were dropped from a security floor. The false figure manufactured a phantom
            # gap in the very thing the sentence was written to reassure about. `crown_jewel_reason` had already been fixed to say WHICH kind it
            # matched; this renderer was the other surface saying it and kept the false sentence.
            # ⛔ AND THE SWEEP IS NOW NAMED BY ITS DIMENSIONS, NOT BY A CELL COUNT (Diogenes
            # DRIFT, 2026-09-07). The paragraph above said "all 16 cells" and "the eight ... cells".
            # All three dimensions are binary, so the product is 8 and each half is 4 — wrong by
            # exactly a factor of two, in the one number nobody re-derives, because a cardinality
            # describing the author's own sweep reads as provenance rather than as a claim. Every
            # figure a reader would check was exact; only the figure vouching for how hard the
            # author looked was wrong. Dimensions are readable off the signature and cannot disagree
            # with it; a cardinality must be recomputed by hand the moment a dimension gains a
            # value. This block's own thesis, applied to the block.
            # ⚠ AND THIS IS THE MORE AUTHORITATIVE SURFACE: the in-process message is transient,
            # the .sbpl is written to a file and is what the kernel enforces. An operator auditing
            # why `docker.sock` is write-denied read that it is an ssh persistence vector.
            # ⚠ SPLIT BY MEMBERSHIP IN THE SOCKET SET, NEVER BY FILENAME — mirroring
            # `crown_jewel_reason` exactly. A `.sock` endswith test stood there for one commit and
            # was replaced on review: `podman.socket`, a systemd-activated name or an operator's
            # own path would be misfiled, and name-based reasoning about security-relevant paths
            # is the thing this module refuses.
            _sockets = set(policy.socket_spellings) or set(policy.deny_sockets)
            _ssh_vectors = [f for f in policy.deny_write_files if f not in _sockets]
            _socket_files = [f for f in policy.deny_write_files if f in _sockets]
            if _ssh_vectors:
                lines.append(";; ssh persistence/exec vectors (authorized_keys*, config, rc) — ALWAYS write-")
                lines.append(";; denied (both ssh_modes): a planted key / ProxyCommand config / rc is code the")
                lines.append(";; operator or sshd runs later — a persistent backdoor, zero legit entity use.")
                lines.append("(deny file-write*")
                for f in _ssh_vectors:
                    lines.append(f'    (literal "{_sbpl_string(str(f))}")')
                lines.append(")")
                lines.append("")
            if _socket_files:
                lines.append(";; container/VM daemon sockets — write-denied so the socket FILE cannot be")
                lines.append(";; renamed or unlinked and re-pointed at an unlisted target. Reaching an")
                lines.append(";; unsandboxed root daemon bypasses the whole floor (spore-725). The CONNECT")
                lines.append(";; half is the network-outbound rule above; this is the rename/unlink arm.")
                lines.append("(deny file-write*")
                for f in _socket_files:
                    lines.append(f'    (literal "{_sbpl_string(str(f))}")')
                lines.append(")")
                lines.append("")

        if policy.own_memory_files:
            # The entity's OWN store (memory.continuity.md / crystal.json / db) — WRITE-only denied so a
            # confined-hands write can't poison the always-loaded memory that spore-359 injects, bypassing
            # the GOVERNED wrap ("human-gated" until K4a [6]; a seat may now wrap unattended, so the
            # property is the VALIDATED composer, not human presence). READ stays allowed (bash may
            # `cat` its own memory). anneal composes
            # these in the HOST process (not this sandbox), so the wrap/capture writer is untouched. LAST
            # deny block so last-match-wins keeps it denied; nothing re-allows these.
            lines.append(";; the entity's OWN memory store (continuity/crystal/episodic) — WRITE-denied:")
            lines.append(";; only the host-process `levain wrap`/capture composes it, never the hands, so")
            lines.append(";; a self-write can't poison the next session's always-loaded frame (spore-359).")
            lines.append("(deny file-write*")
            for p in policy.own_memory_files:
                lines.append(f'    (literal "{_sbpl_string(str(p))}")')
            lines.append(")")
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    def _spawn_shell_impl(
        self,
        policy: CrownJewelsPolicy,
        *,
        env: dict[str, str] | None = None,
        default_timeout: float = 120.0,
        hands: HandsIdentity | None = None,
    ) -> SandboxedShell:
        if not sandbox_exec_available():
            raise ConfinementError(
                f"{SANDBOX_EXEC} is not available — cannot establish the macOS confinement floor. "
                "Refusing to grant bash hands without the sandbox (fail-closed)."
            )
        profile_text = self.render_profile(policy)
        _refuse_kernel_mask_rules(profile_text)
        if hands is not None:
            # The floor is rendered for the hands workspace (the binding fenced it there), and the
            # profile is applied to the hands process: sudo -> env -i -> sandbox driver -> bash.
            _require_hands_sudo(hands)
            hands_shell = _HandsSeatbeltShell(
                hands=hands,
                argv=[*hands_prefix(hands), SANDBOX_EXEC, "-p", profile_text,
                      "/bin/bash", "--noprofile", "--norc"],
                cwd=hands.workspace,
                env=_hands_env(hands),
                default_timeout=default_timeout,
            )
            return hands_shell.start()
        # The profile goes on argv as TEXT (`-p`), never as a file. Each command is its own driver
        # process, so a profile file would be read again at every command, and the file (owned by this
        # uid, in its temp dir, outside every deny) could be rewritten by the entity between two
        # commands to drop every deny (S2 L2 C1). The text lives only in this process's memory.
        argv = [
            SANDBOX_EXEC, "-p", profile_text,
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
        )
        return shell.start()

    def hands_file(self, policy: CrownJewelsPolicy, hands: HandsIdentity, op: str, path: str,
                   data: bytes = b"", *, timeout: float = 60.0) -> bytes:
        return _seatbelt_hands_file(self, policy, hands, op, path, data, timeout)


class HandsFileRefused(PermissionError):
    """The hands file helper refused an operation (outside the workspace, a symlink, not a regular
    file, too large, permission denied); the message is its reason."""


# The largest file the editor reads through the hands helper.
_HANDS_READ_LIMIT = 64 * 1024 * 1024
# The most entries the editor lists in one directory (the helper reads no further than one past it).
_HANDS_LIST_MAX = 100_000
HANDS_ZSH = "/bin/zsh"

# The file editor's hands for an entity whose bash runs as its own user: ONE fixed zsh program, run as
# that user (sudo -n -u, env -i, no terminal) under the same sandbox profile as its bash, so every read
# and write is made with that user's rights and the floor's, never the operator's. zsh because it
# ships with every macOS and its zsh/system module opens with O_NOFOLLOW and O_NONBLOCK, which no
# stock shell utility can. Its input is data only: the operation, the workspace, the path, the size
# limit and the workspace's identity are arguments, a written file's content is stdin; nothing given
# to it is evaluated.
# Confinement (S2 L3 r1, codex HIGH + glm MED): the workspace must not be a symlink and must still be
# the directory levain recorded (device:inode, $5), and the helper ENTERS it. The path, lexically
# inside the workspace, is then walked one directory at a time with cd, refusing a symlink component
# and checking after every step that the physical working directory (getcwd) is still the workspace
# or under it, so a component swapped for a link between the check and the cd is caught. Everything
# after the walk is relative to the directory the helper is IN, which a later rename cannot move it
# out of. The last component is never followed: a symlink there is refused, or, for a write,
# replaced. That bounds where the editor looks; what it may touch is the kernel's to say, by the
# hands user's rights and the profile.
#   read   the regular file's bytes on stdout, at most limit+1 of them (levain refuses more); a FIFO,
#          device or directory is refused, not opened for long (O_NONBLOCK)
#   stat   lstat as "mode ino dev nlink uid gid size atime mtime ctime"
#   list   the directory's entries, then each non-hidden real subdirectory's non-hidden entries, as
#          NUL-terminated pairs "<l|d|f|o>" "<name or sub/name>", at most limit+1 bytes
#   write  stdin into a new file beside the target (O_CREAT|O_EXCL|O_NOFOLLOW), given the target's
#          permissions (0644 when new), then renamed over it: atomic, and a link or FIFO at the
#          target is replaced, never written through
# Exit 0 done, 2 no such path, 3 refused (the reason on stderr); anything else is a failure. /etc/zshenv,
# root's own file, is the one startup file `zsh -f` still reads. The descriptors are fixed numbers
# (5, 6): zsh 5.9 can report a wrong number for ``sysopen -u var`` after a redirected command (seen on
# macOS: the variable said 13, the file was open on 11).
_HANDS_FILE_HELPER = r"""emulate -R zsh
zmodload zsh/system zsh/stat 2>/dev/null || { print -ru2 -- 'zsh modules are unavailable'; exit 70 }
refuse() { print -rnu2 -- "$1"; exit 3 }
op=$1 ws=$2 p=$3 lim=$4 wsid=$5 cap=$6
[[ $ws == /* && $p == /* ]] || refuse "not an absolute path: $p"
[[ -L $ws ]] && refuse "the workspace $ws is a symlink"
zstat -L -H st -- $ws 2>/dev/null || refuse "the workspace $ws is gone"
[[ "$st[device]:$st[inode]" == $wsid ]] || refuse "the workspace $ws is not the directory levain recorded"
builtin cd -q -- $ws 2>/dev/null || refuse "cannot enter the workspace $ws"
zstat -L -H st -- . 2>/dev/null && [[ "$st[device]:$st[inode]" == $wsid ]] || refuse "the workspace $ws changed while it was entered"
wsr=$(builtin pwd -P)
inside() { local here; here=$(builtin pwd -P); [[ $here == $wsr || $here == $wsr/* ]] }
absent() { [[ -e $1 || -L $1 ]] || exit 2 }
enter() {
  [[ -L ./$1 ]] && refuse "$3 runs through a symlink; the editor does not follow links for an entity with its own user"
  [[ -d ./$1 ]] || { absent ./$1; refuse "$3: a component is not a directory" }
  builtin cd -q -- ./$1 2>/dev/null || refuse "cannot enter a directory of $3: permission denied"
  inside || refuse "$3 left the workspace while it was walked"
}
p=${p:a}
wsa=${ws:a}
if [[ $p == $wsa ]]; then
  parts=()
elif [[ $p == $wsa/* ]]; then
  parts=(${(s:/:)${p#$wsa/}})
else
  refuse "$3 is outside the workspace $ws"
fi
n=
if (( $#parts )); then
  n=$parts[-1]
  for c in $parts[1,-2]; do enter $c x $3; done
fi
kind() {
  if [[ -L $1 ]]; then REPLY=l; elif [[ -d $1 ]]; then REPLY=d; elif [[ -f $1 ]]; then REPLY=f; else REPLY=o; fi
}
case $op in
  stat)
    t=${n:-.}
    zstat -L -H st -- ./$t 2>/dev/null || { absent ./$t; refuse "cannot stat $3: permission denied" }
    print -rn -- "$st[mode] $st[inode] $st[device] $st[nlink] $st[uid] $st[gid] $st[size] $st[atime] $st[mtime] $st[ctime]"
    ;;
  read)
    [[ -n $n ]] || refuse "$3 is a directory"
    if ! sysopen -r -o nofollow,nonblock -u 5 -- ./$n 2>/dev/null; then
      absent ./$n
      [[ -L ./$n ]] && refuse "$3 is a symlink; the editor does not follow links for an entity with its own user"
      refuse "cannot open $3: permission denied"
    fi
    zstat -f 5 -H st 2>/dev/null || refuse "cannot stat $3"
    (( (st[mode] & 8#170000) == 8#100000 )) || refuse "$3 is not a regular file"
    (( st[size] <= lim )) || refuse "$3 is larger than $lim bytes"
    /usr/bin/head -c $(( lim + 1 )) <&5
    ;;
  list)
    [[ -n $n ]] && enter $n x $3
    [[ -r . && -x . ]] || refuse "cannot list $3: permission denied"
    setopt null_glob glob_dots
    # (Y): the glob stops reading the directory after cap+1 matches, so a huge one costs no more.
    es=( ./*(Y$(( cap + 1 ))) )
    (( $#es > cap )) && refuse "$3 has more than $cap entries; not listed"
    {
      for e in ${(o)es}; do
        kind $e
        print -rn -- "$REPLY"$'\0'"${e:t}"$'\0'
        if [[ $REPLY == d && ${e:t} != .* ]]; then
          (
            builtin cd -q -- $e 2>/dev/null && inside || exit 0
            cs=( ./[^.]*(Y$(( cap + 1 ))) )
            (( $#cs > cap )) && refuse "$3/${e:t} has more than $cap entries; not listed"
            for c in ${(o)cs}; do
              kind $c
              print -rn -- "$REPLY"$'\0'"${e:t}/${c:t}"$'\0'
            done
          ) || exit $?
        fi
      done
    } | /usr/bin/head -c $(( lim + 1 ))
    if (( pipestatus[1] == 3 )); then exit 3; fi
    ;;
  write)
    [[ -n $n ]] || refuse "$3 is a directory"
    [[ -d ./$n && ! -L ./$n ]] && refuse "$3 is a directory"
    mode=644
    if [[ -f ./$n && ! -L ./$n ]] && zstat -L -H st -- ./$n 2>/dev/null; then
      mode=$(( [##8] st[mode] & 8#7777 ))
    fi
    t=./.$n.levain-$$-$RANDOM
    sysopen -w -o create,excl,nofollow -m 600 -u 6 -- $t 2>/dev/null || refuse "cannot write in the directory of $3: permission denied"
    if ! /bin/cat >&6; then exec 6>&-; /bin/rm -f -- $t; refuse "writing $3 failed"; fi
    exec 6>&-
    if ! /bin/chmod $mode $t || ! /bin/mv -f -- $t ./$n; then /bin/rm -f -- $t; refuse "could not replace $3"; fi
    ;;
  *) refuse "unknown operation $op" ;;
esac
"""
_HANDS_FILE_OPS = ("read", "stat", "list", "write")


def _hands_file_argv(driver: list[str], hands: HandsIdentity, op: str, path: str, ws_id: str) -> list[str]:
    """The helper as the hands user under ``driver``, the platform's sandbox argv (the Seatbelt driver
    with its profile, or a hands launch's bwrap argv): the same program and arguments on both."""
    return [*hands_prefix(hands), *driver,
            HANDS_ZSH, "-f", "-c", _HANDS_FILE_HELPER, "zsh", op, str(hands.workspace), path,
            str(_HANDS_READ_LIMIT), ws_id, str(_HANDS_LIST_MAX)]


def _workspace_id(hands: HandsIdentity) -> str:
    """``device:inode`` of the hands workspace as levain sees it now; refuses a symlink there."""
    st = os.lstat(hands.workspace)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise HandsFileRefused(f"the hands workspace {hands.workspace} is not a directory (a symlink?)")
    return f"{st.st_dev}:{st.st_ino}"


def _stop_hands_group(hands: HandsIdentity, proc: subprocess.Popen[bytes]) -> bool:
    """SIGKILL a hands helper's whole session group: its members as the hands user, sudo (the leader,
    the operator's real uid) as levain. True once the group is gone and sudo reaped."""
    _hands_signal(hands, proc.pid, signal.SIGKILL)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.communicate(timeout=5)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    return _group_gone(proc.pid, timeout=5.0) and proc.poll() is not None


def _run_hands_helper(argv: list[str], hands: HandsIdentity, data: bytes,
                      timeout: float) -> subprocess.CompletedProcess[bytes]:
    """Run the helper in a new session (no controlling terminal) and, on a timeout or any error,
    kill its WHOLE group, not only sudo: ``subprocess.run`` kills just the child it started (S2 L3 r1,
    codex + glm)."""
    from levain.launch import child_env

    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd="/", env=child_env(), start_new_session=True)
    try:
        out, err = proc.communicate(data, timeout=timeout)
    except subprocess.TimeoutExpired:
        gone = _stop_hands_group(hands, proc)
        raise OSError(f"the editor's file operation as {hands.user} timed out"
                      + ("" if gone else f"; its process group {proc.pid} could not be stopped")) from None
    except BaseException:
        _stop_hands_group(hands, proc)
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _hands_file_op(hands: HandsIdentity, op: str, path: str, data: bytes, timeout: float,
                   driver: Callable[[], list[str]]) -> bytes:
    """One editor operation through the helper, on every platform: ``driver()`` gives the sandbox argv
    the helper runs under (asked after the operation and the workspace are checked), and the helper's
    exit status becomes the result or the exception :meth:`ConfinementProvider.hands_file` documents."""
    if op not in _HANDS_FILE_OPS:
        raise ValueError(f"unknown hands file operation {op!r}")
    try:
        ws_id = _workspace_id(hands)
    except FileNotFoundError:
        raise HandsFileRefused(f"the hands workspace {hands.workspace} is gone") from None
    argv = _hands_file_argv(driver(), hands, op, path, ws_id)
    # stdin is the content for a write and empty otherwise.
    r = _run_hands_helper(argv, hands, data if op == "write" else b"", timeout)
    said = r.stderr.decode("utf-8", "replace").strip()
    if r.returncode == 0:
        if op in ("read", "list") and len(r.stdout) > _HANDS_READ_LIMIT:
            raise HandsFileRefused(f"{path}: more than {_HANDS_READ_LIMIT} bytes; not read")
        return r.stdout
    if r.returncode == 2:
        raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)
    if r.returncode == 3:
        raise HandsFileRefused(said or f"{hands.user} may not {op} {path}")
    raise OSError(f"{hands.user} could not {op} {path} (status {r.returncode}): "
                  + (said.splitlines()[-1] if said else "no reason given"))


def _seatbelt_hands_file(provider: "SeatbeltProvider", policy: CrownJewelsPolicy, hands: HandsIdentity,
                         op: str, path: str, data: bytes, timeout: float) -> bytes:
    def driver() -> list[str]:
        text = provider.render_profile(policy)
        _refuse_kernel_mask_rules(text)
        return [SANDBOX_EXEC, "-p", text]

    return _hands_file_op(hands, op, path, data, timeout, driver)


def _bwrap_hands_file(policy: CrownJewelsPolicy, hands: HandsIdentity, op: str, path: str, data: bytes,
                      timeout: float) -> bytes:
    """The editor on Linux: the same helper under the bwrap argv a hands shell runs under, from the
    same planner and transform. The launch probes and the listener sweep are not run for it: the helper
    is a fixed program that opens files and never a socket, and a bwrap that cannot build the
    namespaces fails the operation (fail-closed)."""
    if not os.access(HANDS_ZSH, os.X_OK):
        raise ConfinementError(
            f"{HANDS_ZSH} is missing: install zsh (the editor's hands use it) — refusing to use the "
            "entity's files as you instead (fail-closed)."
        )
    _require_hands_python()

    def driver() -> list[str]:
        argv, _ = _bwrap_plan(policy)
        argv = _hands_bwrap_argv(argv, hands)
        missing = _hands_argv_unmade(argv)
        if missing is not None:
            # A shell creates such a placeholder under its ledger claim and removes it once no shell
            # needs it. The editor takes no claim, so it refuses rather than let bwrap create the
            # path on the host and leave it behind.
            raise ConfinementError(
                f"the floor mounts over {missing}, which does not exist; only a running shell creates it "
                "— refusing to use the entity's files (fail-closed). Run a command in the shell, then retry."
            )
        return argv

    return _hands_file_op(hands, op, path, data, timeout, driver)


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
    """A :class:`SandboxedShell` that re-runs the kernel-mask refusal, before every command, on the
    profile text it is about to hand the driver's ``-p``. Kept private — callers get a
    :class:`SandboxedShell` from :meth:`SeatbeltProvider.spawn_shell`."""

    def _spawn_argv(self) -> tuple[list[str], tuple[int, ...]]:
        argv, fds = super()._spawn_argv()
        # The exact argv element the driver will read.
        _refuse_kernel_mask_rules(argv[argv.index(SANDBOX_EXEC) + 2])
        return argv, fds


class _HandsSeatbeltShell(_SeatbeltShell):
    """A seatbelt shell whose bash runs as the entity's hands user (``sudo -n -u``, outside the
    sandbox driver). levain cannot signal another uid's processes, so every signal (timeout, close,
    interrupt) is sent as the hands user; checking that a group is empty still works from levain
    (``killpg(pgid, 0)`` answers EPERM while a member lives)."""

    def __init__(self, *, hands: HandsIdentity, argv: list[str], cwd: Path, env: dict[str, str],
                 default_timeout: float = 120.0) -> None:
        super().__init__(argv=argv, cwd=cwd, env=env, default_timeout=default_timeout)
        self.hands = hands

    @property
    def hands_user(self) -> str | None:
        return self.hands.user

    def _signal(self, pgid: int, sig: int) -> bool:   # type: ignore[override]
        # The group's members run as the hands user, except sudo, the group leader, whose REAL uid is
        # the operator's: only levain can signal it, and only the hands user the rest. A command that
        # stops itself (`kill -STOP $$`) stops sudo too, so after the signal both get SIGCONT, or a
        # stopped sudo would outlive the timeout, close() and levain (S2 L2b M2).
        delivered = _hands_signal(self.hands, pgid, sig)
        if sig != signal.SIGCONT:
            _hands_signal(self.hands, pgid, signal.SIGCONT)
        gone = False
        for s_ in (sig, signal.SIGCONT):
            try:
                os.killpg(pgid, s_)
            except ProcessLookupError:
                gone = True
            except OSError:
                pass
        # Three answers (S2 L3 r6): delivered (the hands user's kill said so), confirmed gone, or
        # unconfirmed. A kill that found nothing left exits non-zero like a refused or stalled sudo,
        # so "gone" is confirmed apart from it: ESRCH, or, since ESRCH cannot come while the leader is
        # held unreaped, macOS's one-call process table showing no live member (`_group_live`).
        return delivered or gone or not _group_live(pgid)


# --- Linux: bwrap (mount-namespace) provider -------------------------------------------------
#
# ⚠ THIS IS A RE-DERIVATION, NOT A PORT, AND EVERY ROW BELOW WAS MEASURED ON A LINUX KERNEL
# RATHER THAN REASONED FROM THE macOS PROFILE OR THE bwrap MAN PAGE.
#
# WHERE, EXACTLY, BECAUSE "MEASURED ON LINUX" WOULD LET A READER ASSUME THE WRONG HOST. The
# measurements were taken in a disposable Ubuntu 24.04.4 / bubblewrap 0.9.0 container (aarch64) on
# 2026-09-03, driving the real provider. They were NOT taken on argushub — argushub is Ubuntu 24.04.4
# / bubblewrap 0.9.0 too, but on x86_64, and it CANNOT RUN bwrap at all (see `bwrap_available`), which
# is what sent the proof to a container in the first place. Same distro, same bwrap, different arch.
# Later the same day (0e42e18's commit) the live floor ran on argushub hardware after its AppArmor
# profile was loaded. The 2026-09-30 port changed this argv (steps 1, 3b and 6, deferred remounts)
# and was first re-run only in a container (tests/linux/Dockerfile, kernel 6.12), which relaxes
# seccomp to permit user namespaces at all. On 2026-10-01 the `linux_live` tests ran on argushub
# again (the 0.5.0 commits f4283d3, 0e09df5 and a3bfeb7 report those runs). They are the real-host
# check for this argv: point them at a capable host after any change to it.
# seatbelt is a PATH PREDICATE evaluated per syscall; bwrap is a MOUNT NAMESPACE established once
# at spawn. The polarity is also inverted — crown-jewels is deliberately default-ALLOW-with-denies
# (the Slice-1 flip) while bwrap's idiom is default-deny-with-binds — so the base is ``--bind / /``
# and each jewel is then OVER-MOUNTED. ``confinement.py``'s module docstring states the standing
# requirement this had to satisfy: the Linux backend MUST re-prove "cannot hardlink/rename a
# write-denied file out". It does, and by a stronger mechanism than macOS (see ``_bwrap_argv``).
#
# THE MEASURED EQUIVALENCE TABLE (policy field -> bwrap op -> observed behaviour):
#
#   deny_read_write (subtrees)   --tmpfs P --remount-ro P   read ENOENT, write EROFS
#   deny_files / config_file     --ro-bind /dev/null P      read EACCES, write EACCES
#   deny_write_dirs (ancestors)  --bind P P (self-bind)     rename EBUSY, writes INSIDE still OK
#   deny_write_files             --ro-bind P P (self-bind)  read OK, write EROFS
#   own_memory_files             --ro-bind P P (self-bind)  read OK, write EROFS
#   ssh_dir (agent mode)         --tmpfs, then bind back    key material ENOENT; known_hosts r+w
#                                known_hosts (rw) + config  reaches the REAL host file; config
#                                (ro) INTO the tmpfs        read OK / write EROFS
#   deny_sockets, daemon dir     --tmpfs P --remount-ro P   connect ENOENT, holds across a daemon
#                                                           restart (measured 2026-09-30)
#   deny_sockets, shared dir     --ro-bind /dev/null S      connect ECONNREFUSED until the daemon
#                                                           recreates S (measured 2026-09-30)
#
# WHY THE ANCESTOR SELF-BIND IS NOT DROPPABLE HERE, WHICH IS THE FINDING THAT NEARLY WENT THE OTHER
# WAY. Inside ONE sandbox, renaming a jewel's ancestor does not expose the jewel — the over-mount is
# attached to the dentry and travels with the rename. That makes it LOOK like bwrap has no need for
# macOS's ancestor-write-deny. It does. The rename PERSISTS TO THE HOST, so the NEXT sandbox builds
# its over-mount at the ORIGINAL path, ``bwrap`` silently CREATES that missing path, and the tmpfs
# lands on a decoy while the real jewel sits at the new location fully readable. Measured: it printed
# the jewel. The relocation bypass (apparatus L2 CRITICAL) survives the port intact, and bwrap's
# path-autocreation makes it worse than on macOS by making the useless mount look protective.
# ``--bind P P`` is the exact analogue of macOS's ``(deny file-write* (literal <dir>))``: it makes the
# dir a MOUNTPOINT, so renaming it returns EBUSY while ``open(dir/child, O_CREAT)`` still succeeds —
# both halves measured.
#
# WHY ``--tmpfs`` IS PAIRED WITH ``--remount-ro`` AND NEVER USED BARE. A bare tmpfs over-mount hides
# the jewel but lets writes into it SILENTLY SUCCEED and vanish — the entity is told its write
# worked when nothing was written (``absence_of_signal_rendered_as_health`` arriving at the
# confinement layer). ``--remount-ro`` keeps the hiding and makes the refusal honest (EROFS).
#
# ⛔ ``--ro-bind-try`` / ``--bind-try`` ARE BANNED FOR JEWELS. On a missing source they start the
# sandbox with NOTHING protecting that path — a fail-OPEN in a security-surface generator, which is
# the shape of the defect the whole module is written against. Use the explicit forms and let a
# genuinely missing source raise.


def _shadowed_by(path: Path, roots: tuple[Path, ...]) -> bool:
    """True if ``path`` already sits under a subtree this policy over-mounts with a tmpfs.

    Load-bearing, not an optimization, and it SELECTS THE FORM rather than skipping the path. A
    SELF-bind (``--ro-bind P P``) of such a path would RE-EXPOSE it through the very over-mount that
    hid it: in ``ssh_mode="agent"`` every ssh persistence vector lives inside the tmpfs'd ``~/.ssh``,
    so self-binding ``authorized_keys`` there would restore READ access to material agent mode exists
    to deny. A ``/dev/null`` bind exposes nothing and is therefore the correct form here — see the
    write-only block in :func:`_bwrap_argv` for why it is applied rather than skipping outright.
    Matched with :func:`_ci_within` for consistency with the rest of the module (and see that
    function on why the fold is macOS-shaped). ⚠ The fold stays even though step (5)'s mask compare is
    exact: masks-last covers a self-bind with a later /dev/null mask, but a tmpfs is not a mask, and
    a tmpfs is case-sensitive even on a casefolded host, so an exact match here would let a case
    variant self-bind back into the subtree it hides (L1, 2026-10-03). Cost: on a case-sensitive
    volume a write-only file under a directory differing only in case from a root is hidden."""
    return any(_ci_within(path, r) for r in roots)


_SQLITE_MAGIC = b"SQLite format 3\x00"


def _sqlite_state(p: Path) -> str:
    """``"sqlite"``, ``"other"``, ``"unreadable"`` or ``"absent"`` for a jewel path. Checks the type
    with ``lstat`` BEFORE opening and opens non-blocking without following a link, so a FIFO or a
    device named as a jewel is classified instead of hanging the host process in ``open``
    (complement + codex, L3 2026-10-02). Reads 16 bytes."""
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unreadable"
    if not stat.S_ISREG(st.st_mode):
        return "other"
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return "unreadable"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):   # replaced between lstat and open
            return "other"
        return "sqlite" if os.read(fd, 16) == _SQLITE_MAGIC else "other"
    except OSError:
        return "unreadable"
    finally:
        os.close(fd)


def _foreign_runtime_dirs() -> list[str]:
    """The user runtime dirs (:func:`_runtime_dirs`) that are real directories owned by ANOTHER user,
    at both their given and resolved spellings: under ``su`` the inherited ``$XDG_RUNTIME_DIR`` is
    the first user's. Used only to explain a refusal, never to skip a check. Ownership is read from
    ``lstat`` of the directory itself, so a symlink spelled like a runtime dir is never listed."""
    out: list[str] = []
    for rd in _runtime_dirs():
        try:
            st = os.lstat(rd)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode) and st.st_uid != os.geteuid():
            out += [rd, os.path.realpath(rd)]
    return out


def _jewel_inodes(policy: CrownJewelsPolicy) -> dict[tuple[int, int], tuple[int, str]]:
    """For every crown-jewel file or socket: ``(st_dev, st_ino) -> (st_nlink, one path to it)``.

    The jewels are the named ones (followed through symlinks) plus every entry under the hidden
    subtrees and the ssh dir (walked without following symlinks), and on Linux the session bus and
    the systemd manager socket bwrap step (7) masks. An absent jewel is skipped: there is no file to
    have other names.

    ⛔ A jewel this user cannot stat or list raises :class:`ConfinementError` ("could not check"),
    whoever owns the directory in the way: a chmod on its own directory would otherwise switch the
    check off while the other name stays reachable (L2, RUN: ``chmod 0600 ~/.ssh`` plus a link to
    ``authorized_keys``). That includes a socket in another user's runtime dir: an earlier version
    skipped those, and each way of deciding which to skip was defeated by a name it did not look at,
    including a link the socket's owner made at a public path (codex + glm, the nlink L3 round). It
    includes a symlink loop on a jewel path too (``os.stat`` raises ELOOP). The refusal names the
    inherited ``$XDG_RUNTIME_DIR`` when that is the cause (:func:`_foreign_runtime_dirs`)."""
    seen: dict[tuple[int, int], tuple[int, str]] = {}

    def unverifiable(path: str, exc: OSError) -> NoReturn:
        hint = ""
        if any(rd.strip(os.sep) and (path == rd or path.startswith(rd.rstrip(os.sep) + os.sep))
               for rd in _foreign_runtime_dirs()):
            hint = (" It is under another user's runtime directory, usually an XDG_RUNTIME_DIR "
                    "inherited through su: run `unset XDG_RUNTIME_DIR` and start again.")
        raise ConfinementError(
            f"could not check {path} for other names ({exc.strerror or exc}). The floor denies paths, "
            "so an unchecked crown jewel could be reachable through a hardlink. Refusing this hand "
            "(fail-closed). Make it readable to this user, or remove it from the floor." + hint
        ) from exc

    def note(path: str, follow: bool) -> None:
        try:
            st = os.stat(path) if follow else os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            return   # absent (or a dangling link): no file to have other names
        except OSError as exc:
            unverifiable(path, exc)
        if stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
            return
        seen.setdefault((st.st_dev, st.st_ino),
                        (st.st_nlink, os.path.realpath(path) if follow else path))

    sockets: list[Path] = [*policy.deny_sockets, *policy.socket_spellings]
    if platform.system() == "Linux":
        sockets += [b for rd in _runtime_dirs()
                    for b in (Path(rd) / "bus", Path(rd) / "systemd" / "private")]
    named = [*policy.deny_files, *policy.deny_write_files, *policy.own_memory_files,
             *policy.sqlite_sidecars, *sockets]
    if policy.config_file is not None:
        named.append(policy.config_file)
    for f in named:
        note(str(f), follow=True)

    def walk_error(exc: OSError) -> None:
        if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
            return
        unverifiable(str(exc.filename or "a crown-jewel directory"), exc)

    roots = sorted({*policy.deny_read_write, *([policy.ssh_dir] if policy.ssh_dir else [])},
                   key=lambda p: str(p))
    walked: list[Path] = []
    for root in roots:
        if any(root == w or root.is_relative_to(w) for w in walked):
            continue
        walked.append(root)
        if not os.path.isdir(root):
            note(str(root), follow=True)   # a subtree root that is a file (argushub's ~/.anneal-memory)
            continue
        for dirpath, _dirs, files in os.walk(root, onerror=walk_error):   # symlinked dirs not followed
            real_dir = os.path.realpath(dirpath)
            for name in files:
                note(os.path.join(real_dir, name), follow=False)
    return seen


def _refuse_multiply_linked_jewels(policy: CrownJewelsPolicy) -> None:
    """Refuse bash when any crown-jewel file or socket has more than one name, wherever the names are.

    The floor denies PATHS. A hardlink is the same file under another path, so a link planted
    before the shell starts, at a path the floor does not deny, bypasses it both ways. REPRODUCED
    2026-10-03 on macOS seatbelt and Linux bwrap at e8f403d: a link to a ``deny_files`` token printed
    it, and writing through a link to ``authorized_keys`` added a line to the host's real file, while
    both direct paths were refused; on Linux a link to a user-owned daemon socket reached the daemon
    (L2, RUN). levain cannot find the other names, so every jewel :func:`_jewel_inodes` finds must
    have ``st_nlink == 1``. An earlier version counted the names it could see and graded them by
    access; four review rounds each found a way that count misjudged, so the rule is now strict: a
    second name anywhere refuses, including one inside a hidden subtree.
    Checked at spawn only: a link a host process makes while a shell is live is not watched (Phill's
    ruling, 2026-10-03). The file editor checks every path it touches instead
    (:func:`linked_jewel_reason`)."""
    for links, path in _jewel_inodes(policy).values():
        if links > 1:
            raise ConfinementError(
                f"{path} is a crown jewel with {links} names on disk. The floor denies paths, so "
                "another name would let the shell read or write it. Refusing to grant bash hands "
                "(fail-closed). Find the other names with "
                f"`find <volume> -samefile {shlex.quote(path)}` and remove the extra links."
            )


def linked_jewel_reason(policy: CrownJewelsPolicy, path: str | Path) -> str | None:
    """Why ``path`` must be refused as ANOTHER NAME for a crown jewel, or None.

    :func:`crown_jewel_reason` matches paths, so a hardlink to a jewel at an undenied path passed it:
    RUN 2026-10-03 (L1), the file editor's ``view`` of the link returned the denied token while bash
    was refused. The editor checks the exact path it is about to touch: a file with more than one
    name whose identity is a jewel's is refused, whatever the spelling (the jewel's own spellings
    are refused earlier by :func:`crown_jewel_reason`). A path to a file with ONE name never reaches
    the jewel walk, so a bind alias of a single-name jewel is outside this check, as it is for bash.
    Not covered: a host process that swaps a link into place between this check and the editor's
    open (codex L3 r1; the same host-process class as a link made while a shell is live). The jewel
    walk runs only when the target has more than one name.

    The path is normalised the way the stock editor normalises it (``Path``), so the file checked
    is the file it opens: a raw ``<link>/`` or ``<link>/.`` stats as ENOTDIR, which read as absent,
    while the editor's ``Path`` drops the suffix and opened the link (codex, the nlink L3 round)."""
    path = str(Path(os.path.expanduser(str(path))))
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None   # absent (a create), unreadable or a NUL byte: the editor's own error stands
    if stat.S_ISDIR(st.st_mode) or st.st_nlink < 2:
        return None
    try:
        jewels = _jewel_inodes(policy)
    except ConfinementError as exc:
        return f"{path} has more than one name, and the crown-jewel check failed: {exc}"
    hit = jewels.get((st.st_dev, st.st_ino))
    if hit is None:
        return None
    return (f"{path} is another name (a hardlink) for the crown jewel {hit[1]}, "
            "which the floor denies")


def _refuse_plantable_sqlite_jewels(policy: CrownJewelsPolicy) -> None:
    """Refuse bash on Linux when a crown jewel is a SQLite database.

    REPRODUCED 2026-10-02, both in the Linux container:
    · PLANT (L1): with the store closed, SQLite has deleted its ``-wal``, and nothing a mount can do
      stops the confined shell CREATING ``<db>-wal`` in a writable directory. The shell copied a
      valid WAL there and the host's next open replayed it: ``host reads: [('good',),
      ('PLANTED',)]``. A ``-journal`` is replayed the same way in rollback mode.
    · LATE SIDECAR (codex, L3): for a database in a directory this user CANNOT write, a root
      service that writes after the shell starts creates a readable ``-wal`` no mount covers; the
      shell read the new row (``grep`` rc 0) while the main file stayed denied.
    So no directory makes a SQLite jewel safe on Linux. macOS denies the sidecar names themselves,
    which covers both (the same plant: ``Operation not permitted``).
    An unreadable regular file in a directory this user can write is refused too: it may be a
    database a more privileged process uses, and the shell could plant beside it (codex, L3).
    ⚖ Before 0e09df5 bwrap aborted on a file-shaped root, so such a host had no bash; that commit
    made bash start and opened this path. Refusing restores fail-closed. Ruled by Phill 2026-10-02
    for the writable-directory case, with no opt-out; the late-sidecar run widened it. A store that
    is a DIRECTORY is hidden by a read-only tmpfs and is unaffected."""
    candidates = [p for p in policy.deny_read_write if not p.is_dir()] + list(policy.deny_files)
    for jewel in candidates:
        state = _sqlite_state(jewel)
        if state == "sqlite":
            raise ConfinementError(
                f"{jewel} is a SQLite database. On Linux the floor cannot cover the -wal or "
                "-journal file SQLite creates beside it after the shell starts, so the shell could "
                "read recent rows from it, or plant one that SQLite replays into the database. "
                "Refusing to grant bash hands (fail-closed). Keep the store in a directory (as "
                "~/.anneal-memory/ normally is) to use bash."
            )
        if state == "unreadable" and os.access(jewel.parent, os.W_OK):
            raise ConfinementError(
                f"{jewel} cannot be read by this user, and it sits in a directory this user can "
                "write. If it is a database another process uses, the shell could plant a file "
                "beside it that the database replays. Refusing to grant bash hands (fail-closed)."
            )


def _reachable(p: Path) -> bool:
    """False when this user cannot even stat ``p`` (EACCES: another user's runtime dir). Such a path
    is out of the entity's reach too, so there is nothing to mask — and probing it must not raise
    and cost the shell (measured 2026-09-30: XDG_RUNTIME_DIR naming another uid's dir)."""
    try:
        p.exists()
        return True
    except PermissionError:
        return False


def _host_spelling(p: Path) -> Path:
    """``p`` with its parent resolved. A mount destination is spelled this way: bwrap resolves a
    destination inside its new root, where an absolute link in a parent (``~/.config`` pointing
    elsewhere) does not lead where it leads on the host, so a mount spelled through one cannot land."""
    return p.parent.resolve() / p.name


def _home_read_only_root() -> Path | None:
    """The real ``$HOME`` the bwrap plan binds read-only (step (0) of :func:`_bwrap_plan_impl`), or
    None when it binds none ($HOME unresolvable, the filesystem root, or not a directory)."""
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        return None
    if home == Path(home.anchor) or not home.is_dir():
        return None
    return home


def _bwrap_file_target(f: Path, frozen: tuple[Path, ...] = ()) -> Path:
    """Where a mount for the protected FILE ``f`` must land. A mount cannot land on a symlink (bwrap
    aborts; measured 2026-09-30 with a stow-style ~/.ssh/config). A link this user can replace is
    REFUSED, because masking its target leaves the link free to be swapped for a planted file; one
    they cannot replace, or one in a directory of ``frozen`` (mounted read-only by the plan, so the
    link cannot be replaced from inside), is masked at its real path."""
    if not f.is_symlink():
        return _host_spelling(f)
    _refuse_replaceable_link(f, frozen)
    return _link_target(f)


def _link_target(f: Path) -> Path:
    """The real path of the link ``f``, refusing one that leads nowhere. ``resolve()`` alone returns
    a loop's own path on Python 3.13 (it raised on 3.12), and a mask aimed there, or at a dangling
    link's absent target, would make bwrap abort or create the target on the host."""
    try:
        return f.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ConfinementError(
            f"{f} is a symlink that leads nowhere (dangling, or a loop). The Linux floor masks a "
            "link at its target, and this one has none. Refusing to grant bash hands (fail-closed). "
            "Remove or fix the link."
        ) from None


def _refuse_replaceable_link(f: Path, frozen: tuple[Path, ...] = ()) -> None:
    """Refuse bash when the jewel spelling ``f`` is a symlink in a directory this user can write,
    unless that directory is one of ``frozen``: the plan mounts it read-only, so inside bash the link
    can be neither removed nor replaced (``$HOME`` itself, step (0); a store or tool directory)."""
    if f.parent.resolve() in frozen:
        return
    if os.access(f.parent, os.W_OK):
        raise ConfinementError(
            f"{f} is a symlink in a directory this user can write. The Linux floor protects files "
            "with mounts, and a mount cannot cover a symlink, so the link could be replaced by a "
            "planted file. Refusing to grant bash hands (fail-closed). Replace the symlink with the "
            "real file to use bash (for a standard credential store, setting "
            "\"deny_standard_creds\": false in .levain/confinement.json also does it). A link "
            "directly in a home directory bash sees read-only (the usual case) is accepted."
        )


_MASK_OPS = ("--bind", "--ro-bind", "--bind-try", "--ro-bind-try")
# Every bwrap op that can put content on a path. One of these after a mask could cover it.
_CONTENT_OPS = _MASK_OPS + ("--dev-bind", "--dev-bind-try", "--bind-fd", "--ro-bind-fd",
                            "--bind-data", "--ro-bind-data", "--file", "--overlay",
                            "--tmp-overlay", "--ro-overlay")


def _refuse_bind_after_mask(argv: list[str]) -> None:
    """Refuse a plan in which an op that puts content on a path (``_CONTENT_OPS``) follows a
    ``/dev/null`` mask. A later mount on the same dentry wins, so such an op could put a masked file
    back (see the masks-last rule in :func:`_bwrap_plan_impl`). Checked on the finished argv, so it
    binds any step added later, not only step (5). The scan reads one token at a time; a plan path is
    absolute, so it never equals an op name."""
    first_mask = None
    for i in range(len(argv) - 1):
        op = argv[i]
        if op not in _CONTENT_OPS:
            continue
        src = argv[i + 1]
        if op in _MASK_OPS and src == "/dev/null":
            if first_mask is None:
                first_mask = i
        elif first_mask is not None:
            raise ConfinementError(
                f"internal: the floor's plan binds {src} after the /dev/null mask on "
                f"{argv[first_mask + 2]}, which could re-expose a masked file. Refusing to grant "
                "bash hands (fail-closed)."
            )


def _unescape_mountinfo(field: str) -> str:
    """mountinfo octal-escapes space, tab, newline and backslash as ``\\ooo`` (proc_pid_mountinfo(5))."""
    import re
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def _extra_procfs_mounts(mountinfo: str = "/proc/self/mountinfo") -> list[Path]:
    """Every ``proc`` filesystem mounted outside ``/proc`` on this host, from ``mountinfo``. Empty
    where the file does not exist (macOS). An unreadable or malformed table raises OSError, which
    :func:`_bwrap_plan` turns into a refusal: a procfs it could not look for is not known absent."""
    try:
        text = Path(mountinfo).read_text(encoding="utf-8", errors="surrogateescape")
    except FileNotFoundError:
        return []
    out: list[Path] = []
    for line in text.splitlines():
        pre, sep, post = line.partition(" - ")
        fields = pre.split(" ")
        if not sep or len(fields) < 5 or not post:
            raise OSError(f"unreadable mount table line in {mountinfo}: {line!r}")
        if post.split(" ")[0] != "proc":
            continue
        mp = Path(_unescape_mountinfo(fields[4]))
        if mp == Path("/proc") or mp.is_relative_to("/proc"):
            continue
        out.append(mp)
    return list(_dedup_paths(out))


def _bwrap_argv(policy: CrownJewelsPolicy) -> list[str]:
    """The bwrap argv alone — :func:`_bwrap_plan` without the directories to create first."""
    return _bwrap_plan(policy)[0]


def _bwrap_plan(policy: CrownJewelsPolicy) -> tuple[list[str], list[str]]:
    """:func:`_bwrap_plan_impl`, with a filesystem error while inspecting the host (an unlistable
    store, an entry vanishing mid-walk) turned into the fail-closed :class:`ConfinementError` every
    caller handles — not a raw ``OSError`` that crashes the tool call (codex, L3 r1)."""
    try:
        return _bwrap_plan_impl(policy)
    except ConfinementError:
        # A deliberate refusal already names its cause. ConfinementError is a RuntimeError, so
        # without this it was re-wrapped as "could not inspect the host", a false reason.
        raise
    except (OSError, RuntimeError) as exc:   # RuntimeError: a symlink loop in Path.resolve()
        raise ConfinementError(
            f"could not inspect the host to build the Linux floor ({exc}) — refusing to grant bash "
            "hands (fail-closed)."
        ) from exc


def _bwrap_plan_impl(policy: CrownJewelsPolicy) -> tuple[list[str], list[str]]:
    """THE single source of the bwrap invocation, as ``(argv, dirs_to_create_first)``. :meth:`BwrapProvider.render_profile` renders this
    list as text and :meth:`BwrapProvider.spawn_shell` executes it — deliberately ONE computation
    with two views, never two builders that can drift (the ``two_things_that_should_be_one`` class
    fired on this repo the day before this was written).

    Pure: no I/O EXCEPT ``Path.exists`` probes, which decide between the self-bind and the
    ``/dev/null`` form for a write-denied file, and a 16-byte header read of file-shaped jewels
    (:func:`_refuse_plantable_sqlite_jewels`). Reads, never a mutation."""
    _refuse_plantable_sqlite_jewels(policy)
    argv: list[str] = [
        BWRAP,
        # The polarity flip, in one flag: the whole filesystem is present and writable, exactly as a
        # CC/Codex replacement needs, and the floor is then subtracted from it.
        "--bind", "/", "/",
        # MEASURED AS REQUIRED, not added defensively: with `--bind / /` alone, /proc is MISSING and
        # /dev/null is unopenable, which breaks ordinary tooling and (worse) silently breaks shell
        # redirections a test might be relying on.
        "--proc", "/proc",
        "--dev", "/dev",
        # The sandbox dies with the levain process that owns it. Complements — never replaces —
        # SandboxedShell.close()'s process-group teardown.
        "--die-with-parent",
        # A new PID namespace (lane P2 item 2c), so the ``--proc /proc`` above shows only the
        # sandbox's own processes: "A /proc filesystem shows (in the /proc/pid directories) only
        # processes visible in the PID namespace of the process that performed the mount"
        # (pid_namespaces(7)). Without it, bash read ``/proc/<pid>/environ`` and ``cmdline`` of levain
        # and of every other process of this user, which is where their secrets live. Costs: bash
        # cannot see or signal host processes (``ps``, ``kill`` of a server started outside). bash is
        # that namespace's pid 1 (``--as-pid-1``, added in `spawn_shell`) and runs in a session of its
        # own (``--new-session``), so it is NOT in the process group ``_signal_group`` signals: a
        # signal there reaches bwrap, and bash dies when bwrap does (``--die-with-parent`` above).
        # When bash, pid 1, exits, the kernel kills the rest of the namespace (pid_namespaces(7)).
        "--unshare-pid",
        # A cgroup namespace rooted at the command's own leaf: without it bash, which runs as the
        # operator, could move itself into any other cgroup the operator owns and out of the leaf levain
        # kills (RUN on argushub 2026-10-09: ESCAPED without, refused with; 1009-13_S2/DESIGN.md P4).
        "--unshare-cgroup",
        # cgroupfs read-only inside: `cgroup.kill` and `cgroup.freeze` check only file permission, not
        # the cgroup namespace, so bash (the operator's uid) could otherwise stop or freeze any cgroup
        # the operator owns, another command's leaf or the operator's own services included.
        "--ro-bind", "/sys/fs/cgroup", "/sys/fs/cgroup",
    ]
    if policy.deny_localhost_outbound:
        # spore-755 on Linux (option B, see LINUX_LOCALHOST_REFUSAL's comment): a new, empty network
        # namespace. Closes connects back to this host over TCP AND abstract unix sockets (both
        # measured), at the cost of IP networking inside bash. Not closed: pathname unix sockets,
        # named in ``OFFLINE_RESIDUAL``.
        argv.append("--unshare-net")

    # Steps (2)-(6) build the BODY first, because step (1) has to know which directories the body
    # will make bwrap CREATE on the host. Read-only remounts are collected and applied LAST: a
    # later mount whose mountpoint lies inside a jewel tmpfs has to be created while that tmpfs is
    # still writable, or bwrap aborts.
    head, argv = argv, []
    remount_ro: list[str] = []
    # ⛔ EVERY ``/dev/null`` FILE MASK IS EMITTED AFTER EVERY HOST-SOURCED BIND, and
    # :func:`_refuse_bind_after_mask` refuses a plan that breaks that order. A later mount on the same
    # dentry wins, so a self-bind emitted after a mask puts the host file back on top of it whenever
    # the two spellings name one dentry (a case variant on a casefolded volume is the reasoned case;
    # any spelling a string compare does not match is the general one). RUN on argushub 2026-10-03,
    # bwrap 0.9.0, with the aliases `dir//D`, `dir/./D` and `dir/x/../D` for a masked `dir/D`: mask
    # then self-bind printed the file, self-bind then mask refused it. Ordering closes that whenever
    # the two spellings reach the same mount and dentry, so step (5) compares spellings exactly. It
    # does not cover a hardlink or a second bind of a parent directory: those are other dentries, which
    # no mask ever covered, in either order (RUN, L2 2026-10-03). The parked samefile branch put an
    # identity check between the plan and bwrap instead; five review rounds each found a different
    # way past it.
    masks: list[str] = []
    # $HOME as step (0) binds it read-only. A link directly in it cannot be removed or replaced from
    # inside bash, so it is masked at its target instead of refused (desk ruling 2026-10-07, after
    # the CI live job showed $HOME's top level read-only). Deeper links keep the refusal.
    home_ro = _home_read_only_root()
    frozen_home: tuple[Path, ...] = (home_ro,) if home_ro is not None else ()

    # (2a) THE ENTITY'S OWN STORE DIR (``.levain``) IS MOUNTED READ-ONLY, and every existing ordinary
    # entry in it is bound back read-write. The confined shell can then CREATE nothing at the top of
    # its own store, which is the only mount-namespace way to stop a PLANT of a file that does not
    # exist yet (a first memory.continuity.md, a SQLite sidecar, a confinement.json) without
    # leaving a stub on the host. The stub was the defect, measured 2026-09-30: K4c gave each absent
    # file a ``--ro-bind /dev/null`` mountpoint, and bwrap left it on the host as a 0-byte 0444 file
    # that the HOST's own readers then failed on — anneal cannot open a 0444 memory.db ("attempt to
    # write a readonly database"), and an absent confinement.json (what ``init`` produces) came back
    # as empty JSON that the next session's ``load_confinement_config`` refuses.
    # ⛔ IT COMES BEFORE the jewel tmpfs of step (2): a bind takes its source from the real host
    # tree, so emitted after a tmpfs at or under .levain it re-exposed that jewel (complement +
    # codex, L3 r1). Emitted before them, any deny inside .levain lands on top of it. It comes AFTER
    # the tool-directory views below: an entity inside a tool directory (~/.kube/project) sits under
    # a child the view binds back read-write, and that bind emitted later covered this read-only
    # one (codex, L3 r3).
    # ⚖ STRICTER THAN macOS, STATED: on macOS the confined shell may create new files in .levain
    # other than the denied literals and edit the ones it may write; here it may do neither for a
    # top-level file. Its subdirectories stay writable.
    protected = set(policy.own_memory_files)
    if policy.config_file is not None:
        protected.add(policy.config_file)
    ro_store_dirs: list[Path] = []
    store_argv: list[str] = []
    for d in sorted({p.parent for p in protected}, key=lambda p: str(p)):
        if d.is_symlink() or not d.is_dir():
            # `levain run` refuses an entity with no .levain/ before it gets here; anything else
            # reaching this has no store to protect, and must not be handed a shell that would
            # create one (with stubs) on the host.
            raise ConfinementError(
                f"the entity store {d} is missing or a symlink — refusing to grant bash hands "
                "(fail-closed)."
            )
        store_argv += ["--ro-bind", str(d), str(d)]
        ro_store_dirs.append(d)
        # Only DIRECTORIES come back read-write. A per-FILE bind pins the file's inode, so a host
        # write by rename (anneal and levain write that way) would leave the shell reading and
        # writing an orphan (complement, L3 r1); a top-level file therefore stays read-only here.
        for child in sorted(d.iterdir(), key=lambda p: p.name):
            if child in protected or child.is_symlink() or not child.is_dir():
                continue
            # `-try` is banned for JEWELS (a missing source must not start an unprotected sandbox).
            # This is the opposite case: an ordinary subdirectory the host removed between planning
            # and spawn should cost that subdirectory, not the whole shell. It stays pinned by inode
            # for the shell's life, so a host-side replacement of it is not seen (complement, r2).
            store_argv += ["--bind-try", str(child), str(child)]
    entity_store_dirs = list(ro_store_dirs)

    def _nearest_existing(q: Path) -> Path:
        q = q.parent.resolve() / q.name
        while not os.path.lexists(q) and q != q.parent:
            q = q.parent
        return q

    # The standard cred files' TOOL directories (lane P2, items 5 and 6) get a VIEW, not the host
    # directory: a read-only tmpfs holding the real directory's existing entries except the cred
    # names. Subdirectories come back read-write, files read-only, links as the same links. A cred
    # file absent at spawn then needs no mountpoint, so no 0444 stub lands on the host, and it stays
    # absent inside bash even when the operator's tool creates it on the host mid-session: the host
    # directory is not what bash sees (desk ruling 2026-10-07; a read-only bind of the host directory
    # showed such a file). bash cannot create one either, the tmpfs being read-only. A
    # cred file that is a link is masked at its target (step 4); any other link is recreated as the
    # same link, which cannot be replaced from inside. The cost: an entry the
    # host adds or replaces at the top of a tool directory after spawn is not seen (a per-file bind
    # pins the inode). An absent tool directory is created first (0700, by the provider: step (1)
    # puts it in ``create_first``) and recorded in the placeholder ledger, which removes it at close
    # if it is still empty. The ops are bwrap(1)'s --tmpfs, --bind-try, --ro-bind-try, --symlink and
    # --remount-ro. A mountpoint inside the view is made in the tmpfs, except under a subdirectory
    # bound back from the host (a "window", ~/.aws/sso): there it is on the host, so it is prepared,
    # ledgered and watched like any other (:func:`_mount_plan_paths`; codex, L3 r3).
    # The names a view (a tool directory, or $HOME in step (0)) leaves out. A denied socket is one:
    # absent from the view, it stays unreachable across a daemon restart too.
    secret_names = {_host_spelling(p) for p in (*policy.deny_files, *policy.deny_read_write,
                                               *policy.deny_write_files, *policy.sqlite_sidecars,
                                               *policy.deny_sockets)}
    tool_views: list[Path] = []
    tool_windows: list[Path] = []
    for t in sorted(policy.ro_tool_dirs, key=lambda p: str(p)):
        real = t.parent.resolve() / t.name
        if real.is_symlink():
            _refuse_replaceable_link(real, (*frozen_home, *entity_store_dirs))
            real = _link_target(real)
        if os.path.lexists(real) and not real.is_dir():
            continue   # a file where the tool's directory should be: nothing can live under it
        if real in ro_store_dirs:
            continue
        if not os.path.lexists(real) and _nearest_existing(real) in frozen_home:
            continue   # absent, and not creatable in step (0)'s $HOME view: nothing to show or hide
        argv += ["--tmpfs", str(real)]
        remount_ro.append(str(real))
        ro_store_dirs.append(real)
        tool_views.append(real)
        if not real.is_dir():
            continue
        for child in sorted(real.iterdir(), key=lambda p: p.name):
            if child in secret_names:
                continue
            if child.is_symlink():
                argv += ["--symlink", os.readlink(child), str(child)]
            elif child.is_dir():
                argv += ["--bind-try", str(child), str(child)]
                tool_windows.append(child)
            else:
                argv += ["--ro-bind-try", str(child), str(child)]
    argv += store_argv
    # $HOME is a read-only view too (step (0)): an absent path directly in it needs no mount.
    ro_store_dirs += list(frozen_home)

    view_roots = {*tool_views, *frozen_home}

    def _left_out_of_a_view(f: Path) -> bool:
        # A credential name directly in a view ($HOME, a tool directory) is not carried into it, so
        # inside bash it is ABSENT, not an empty file: no mask (head ruling 2026-10-07). A link there
        # still has its target masked, through the target's own spelling.
        h = _host_spelling(f)
        return h.parent in view_roots and h in secret_names and not f.is_symlink()

    def _absent_in_ro_store(f: Path, dirs: list[Path] | None = None) -> bool:
        # Nothing to hide and nothing the shell can create: no mount, so no host stub. Decided by
        # the NEAREST EXISTING ancestor, not the parent: an absent `.levain/vault` (or a path
        # deeper under an absent dir) cannot be created from inside a read-only store dir, and
        # mounting it made bwrap mkdir inside that read-only mount, so bash never started
        # (Diogenes LOW 2026-10-02, run in the Linux container). An existing subdirectory is bound
        # back read-write in (2a), so a path under one is still mounted. ⚠ SPAWN-TIME, like step
        # (6): in the entity's store a path the HOST creates after spawn is visible through the
        # read-only bind (a tool directory is a tmpfs view, where it is not).
        # A read-only tool directory may not exist yet (the provider creates it just before
        # bwrap), so the walk stops at one whether or not it exists. Compared at the real parent,
        # because the read-only mounts are spelled that way.
        dirs = ro_store_dirs if dirs is None else dirs
        if f.exists():
            return False
        anc = f.parent.resolve()
        while anc not in dirs and not anc.exists() and anc != anc.parent:
            anc = anc.parent
        return anc in dirs

    # (2) CROWN-JEWEL SUBTREES — hidden AND honest about refusing writes.
    # Parent-first, and a root already inside another is dropped: a parent tmpfs emitted after its
    # child hides the child's mountpoint, and the deferred remount of the child then aborts bwrap
    # (codex, L3 r1, with deny_subtrees given child-first).
    # ⛔ EXACT containment, never `_ci_within`: that matcher over-matches on purpose and is safe only
    # where a match REFUSES; here a match DROPS a deny, so /srv/Secret would have swallowed a
    # distinct /srv/secret on a case-sensitive filesystem (complement + codex, L3 r2). Its only
    # error direction now is keeping a root, which at worst aborts bwrap (fail-closed).
    tmpfs_roots: list[Path] = []
    file_roots: list[Path] = []   # subtree roots that are files: denied both ways, like step (4)
    # Every destination steps (2) and (4) mask with /dev/null, recorded AS EMITTED. Step (5) checks
    # its self-bind targets against these, never against a second resolve of the policy paths.
    masked_both: list[str] = []
    nested_in_ssh: list[Path] = []
    ssh_dir = policy.ssh_dir
    cred_dir_spellings = set(floor_roots(policy.cred_dir_sources))
    for sub in sorted(policy.deny_read_write, key=lambda p: str(p)):
        # A subtree root spelled lexically (:func:`_spellings`) may be a link, and bwrap refuses to
        # mount on one (it resolves the path and aborts; measured 2026-09-30). Its resolved spelling is
        # in the policy too and gets the tmpfs; the link itself is safe only where it cannot be
        # replaced from inside, so a link this user can write the directory of refuses bash, as a
        # cred FILE link does, unless that directory is $HOME itself (read-only in bash, step 0). Any other root is mounted at its real parent.
        real = sub.parent.resolve() / sub.name
        if real.is_symlink():
            _refuse_replaceable_link(real, (*frozen_home, *ro_store_dirs))
            # Skipped only because its target is mounted under its own spelling, which is checked
            # here rather than assumed (L2 r1): a list that added a link root alone is refused.
            if real.resolve() not in policy.deny_read_write:
                raise ConfinementError(
                    f"{real} is a symlink to {real.resolve()}, which the floor does not name, so the "
                    "Linux plan cannot cover it. Refusing to grant bash hands (fail-closed)."
                )
            continue
        sub = real
        if any(sub == r or sub.is_relative_to(r) for r in tmpfs_roots):
            continue
        # A root STRICTLY inside the ssh dir cannot be mounted here: step (3)'s ssh tmpfs, emitted
        # later, would hide it and its deferred remount would then abort bwrap (Diogenes MEDIUM
        # 2026-10-01, reproduced on argushub). It is mounted AFTER that tmpfs instead, keeping its
        # remount, so a write under it fails EROFS rather than evaporating in the bare ssh tmpfs
        # (L3 2026-10-01, all three lineages).
        if ssh_dir is not None and sub != ssh_dir and sub.is_relative_to(ssh_dir):
            nested_in_ssh.append(sub)   # mounted after step (3)'s ssh tmpfs, see there
            continue
        # An absent root under a TOOL directory is mounted all the same, inside that directory's
        # tmpfs view, so nothing is created on the host: what the operator's tool writes there
        # mid-session must stay unreadable (the aws caches, research §4; L2 r1), and in the view it
        # is not even present. Only the entity's own store dirs skip absent roots.
        absent_in_store = _absent_in_ro_store(sub, [*entity_store_dirs, *frozen_home])
        if not sub.exists() and not absent_in_store and not _mountpoint_creatable(sub):
            if sub in cred_dir_spellings:
                continue   # a tool's home this user cannot create: the shell cannot create it either
            raise ConfinementError(
                f"{sub} is a crown-jewel directory that does not exist, and bwrap cannot create it to "
                "cover it (its nearest existing parent is not writable). If an anneal trust file lists "
                "a store there that no longer exists, remove that entry. Refusing to grant bash hands "
                "(fail-closed)."
            )
        if absent_in_store:
            continue
        if sub.exists() and not sub.is_dir():
            # A subtree root that is a FILE (argushub's ~/.anneal-memory is a SQLite file, measured
            # 2026-10-01): a tmpfs cannot be mounted over it and bwrap aborts before bash starts.
            # Deny it the way step (4) denies a file, which refuses both read and write.
            masks.append(str(sub))
            masked_both.append(str(sub))
            file_roots.append(sub)
            continue
        tmpfs_roots.append(sub)
        argv += ["--tmpfs", str(sub)]
        remount_ro.append(str(sub))

    # (3) ssh KEY MATERIAL (agent mode). tmpfs the directory, then bind the two files ssh actually
    # needs back INTO it — the mount-namespace analogue of SBPL's last-match-wins re-allow. The
    # known_hosts bind is READ-WRITE and reaches the REAL host file (measured: a write inside the
    # sandbox appeared in the host's known_hosts), so ssh can still record new host keys; config is
    # read-only, matching macOS where its WRITE stays denied in BOTH modes. NEVER re-allow a path the
    # CALLER explicitly denied — same rule, same reason as the seatbelt provider.
    rebound: list[Path] = []
    if policy.ssh_dir is not None:
        ssh = policy.ssh_dir
        argv += ["--tmpfs", str(ssh)]
        tmpfs_roots.append(ssh)
        known_hosts = ssh / "known_hosts"
        config = ssh / "config"
        if known_hosts.exists() and not _caller_denies(known_hosts, policy):
            argv += ["--bind", str(known_hosts), str(known_hosts)]
            rebound.append(known_hosts)
        if config.exists() and not _caller_denies(config, policy):
            argv += ["--ro-bind", str(config), str(config)]
            rebound.append(config)
        for sub in nested_in_ssh:
            if sub.exists() and not sub.is_dir():
                masks.append(str(sub))
                masked_both.append(str(sub))
                file_roots.append(sub)
            else:
                argv += ["--tmpfs", str(sub)]
                remount_ro.append(str(sub))
                tmpfs_roots.append(sub)

    # (4) READ+WRITE-DENIED FILES — credential files and the confinement config that defines the
    # floor. `--ro-bind /dev/null` denies BOTH directions (EACCES on read and on write), which is the
    # closest analogue of macOS's `(deny file-read* file-write* (literal ...))`. Measured to hold with
    # and without `--dev`, so it does not depend on the device tree above.
    roots = tuple(tmpfs_roots)
    deny_both = list(policy.deny_files)
    if policy.config_file is not None:
        deny_both.append(policy.config_file)
    for f in deny_both:
        # ⛔ NO `continue` HERE — THE SKIP'S JUSTIFICATION WAS FALSE FOR THE FORM ACTUALLY IN USE
        # (Diogenes MEDIUM, 2026-09-04). This read `if _shadowed_by(f, roots): continue  # already
        # hidden by a tmpfs; binding it back would re-expose it`. The bind below is
        # `--ro-bind /dev/null <path>`, which binds **/dev/null** and re-exposes NOTHING — which is
        # exactly why step (5) uses that same form under identical shadowing, under a comment
        # explaining that the honesty is restored per-file on the paths that matter.
        #
        # WHAT THE SKIP COST, and it hit the STRICTER deny class while the weaker one stayed
        # protected: the ssh tmpfs is the ONE tmpfs not paired with `--remount-ro`, because
        # `known_hosts` must stay writable. So a caller-pinned `deny_files` path landing under
        # ~/.ssh in agent mode got NO mount at all, and a WRITE to it silently SUCCEEDED into the
        # ephemeral tmpfs and evaporated — the dishonest-refusal shape this module refuses three
        # paragraphs earlier, and a divergence from macOS, where `SeatbeltProvider` emits an
        # unconditional `(deny file-read* file-write*` for every `deny_files` entry.
        #
        # ⚠ IT IS REACHABLE BY THIS MODULE'S OWN INSTRUCTIONS: the docstring's custom
        # `AuthorizedKeysFile` limit tells the operator to pin it via `deny_files`, and a
        # non-default `AuthorizedKeysFile` normally lives under ~/.ssh.
        #
        # ⚡ AND `_shadowed_by`'s OWN DOCSTRING ALREADY DESCRIBED THE CORRECT BEHAVIOUR — it says it
        # "SELECTS THE FORM rather than skipping the path" and points at the write-only block "for
        # why it is applied rather than skipping outright". It described step (5) while step (4),
        # its other call site, did the opposite. The helper was right; one caller was not.
        if _absent_in_ro_store(f) or _left_out_of_a_view(f):
            continue
        if f in policy.sqlite_sidecars and not f.exists():
            # A sidecar absent at spawn is not mounted: its mountpoint would be a 0444 stub that
            # breaks the host's SQLite writer. A jewel that is a SQLite database never gets here
            # (refused at the top of this plan), so these are the names beside a jewel that is not
            # one, where no SQLite will create them.
            continue
        # Under a jewel tmpfs the mount lands in the ephemeral tmpfs, where a host symlink at that
        # path is not visible; only a path on the host tree needs the symlink check (glm, L3 b).
        if _shadowed_by(f, roots):
            dest = str(f)
        else:
            # A link in a read-only store or tool dir, or directly in $HOME: masked at its target.
            dest = str(_bwrap_file_target(f, (*frozen_home, *ro_store_dirs)))
        masks.append(dest)
        masked_both.append(dest)

    # (5) WRITE-ONLY-DENIED FILES — the ssh persistence/exec vectors and the entity's OWN memory
    # store. Read stays allowed (raw-mode ~/.ssh reads work; the entity may `cat` its own memory);
    # write returns EROFS. `rm` of one returns EBUSY, and so does `rm -rf` of its parent directory —
    # both measured, which is what makes the vector floor hold without the ancestor pin doing it.
    #
    # ⚠ THE MISSING-FILE CASE MUTATES THE HOST, AND IT IS DELIBERATE. It is not the only place: any
    # mount whose target is missing makes bwrap create it (steps 1, 2, 4, 6 too), and step (2a)
    # exists because doing it inside the entity's own store broke that store.
    # A mount needs a mountpoint. macOS denies a path STRING, so it covers a file that does not exist
    # YET — which is the entire point of the ssh vector floor (`spore-322`): the attack is PLANTING an
    # authorized_keys that was never there. bwrap can only over-mount something that exists, so a
    # missing vector gets `--ro-bind /dev/null`, and bwrap CREATES the mountpoint — leaving a 0-byte
    # `-r--r--r--` regular file on the host after the sandbox exits (measured). Blocking the plant is
    # worth an empty file: sshd reads an empty authorized_keys as NO KEYS, an empty config/rc as no
    # directives. The alternative — skip it — is `--ro-bind-try` by another name and lets the plant
    # through, which a control run confirmed it does.
    # The entity's own store files never reach the missing-file branch: an absent one is covered by
    # the read-only store dir in step (2a), because the stub this branch would leave broke the
    # host's own store when it was tried there (measured 2026-09-30). The same holds for a cred
    # file in a tool directory (step 2a); a $HOME-level cred file's stub from step (4) lasts only
    # for the session (:func:`_ledger_enter`), as does every stub this step creates: the provider
    # records whatever it creates in the placeholder ledger, which removes it once no session needs it.
    sockets = set(policy.socket_spellings) | set(policy.deny_sockets)
    # Targets steps (2) and (4) already mask, as those steps EMITTED them (codex, L3 r2: a second
    # resolve of the policy paths can observe a symlink retargeted after step (4) ran). EXACT
    # strings: a self-bind of a spelling this misses is emitted before the mask, so the mask still
    # lands on top of it (the masks-last rule above). The case-folded compare this replaced also hid
    # a distinct, write-only file on a case-sensitive volume (0.5.4 known open issue).
    denied_both_targets = set(masked_both)
    for f in tuple(policy.deny_write_files) + tuple(policy.own_memory_files):
        if _absent_in_ro_store(f) or _left_out_of_a_view(f):
            continue
        if f in deny_both or f in file_roots:
            # Step (4) (or step (2), for a subtree root that is a file) already denies it BOTH ways. A self-bind here would take its source from the
            # real host file and stack it on top, making a read-denied file readable again (L2
            # review, 2026-09-30); macOS denies such a file both ways unconditionally.
            continue
        if f in sockets:
            # Step (6) owns every socket path. A self-bind here would not stop a connect, and the
            # missing-file branch below would try to CREATE a socket path under a root-owned /run.
            continue
        if f in rebound:   # exact: a case-folded match here would SKIP a protection (codex, L3 b)
            # known_hosts / config were deliberately bound BACK for ssh to work. known_hosts is
            # read-write by design; config is a ``--ro-bind``, which already refuses writes honestly
            # (EROFS). Over-mounting either here would undo the re-allow.
            continue
        if _shadowed_by(f, roots):
            # Inside a tmpfs the content is already gone, but a WRITE would SILENTLY SUCCEED and
            # evaporate — measured live: planting ~/.ssh/authorized_keys under agent mode returned
            # rc=0 while the host file stayed absent. The attack fails; the REPORT lies, which is the
            # `absence_of_signal_rendered_as_health` shape this module refuses elsewhere (it is why
            # `--remount-ro` is paired onto every jewel tmpfs). The ssh tmpfs CANNOT be remounted
            # read-only because known_hosts has to stay writable, so the honesty is restored per-file
            # on exactly the paths that matter: the vectors are a fixed, enumerable list.
            # ⚡ AND THE MOUNTPOINT LANDS INSIDE THE TMPFS, so unlike the missing-file case below this
            # costs NO host mutation at all — bwrap creates it in the ephemeral filesystem.
            masks.append(str(f))
        elif f.is_symlink() and not f.exists():
            # A dangling link: masking its target would make bwrap create a stub wherever the link
            # points (complement, L3 r2). Refuse it, with the reason.
            raise ConfinementError(
                f"{f} is a dangling symlink — the Linux floor cannot protect it without creating "
                "its target. Refusing to grant bash hands (fail-closed). Remove or fix the link."
            )
        elif f.exists():
            target = _bwrap_file_target(f, frozen_home)
            if _shadowed_by(target, roots) or str(target) in denied_both_targets:
                # The spelling is outside every hidden subtree but its TARGET is inside one: a
                # self-bind takes its source from the host tree, so it would put the hidden file
                # back, readable, inside the tmpfs that hid it, and no mask is there to land on top.
                # RUN on argushub 2026-10-03 (a trust link in a non-writable directory pointing into
                # a sibling entity's store): the confined shell read the sibling's file. /dev/null
                # denies both ways instead. The second test (the target is itself a step (2)/(4)
                # mask) only saves a redundant mount: that mask is emitted after any self-bind.
                masks.append(str(target))
            else:
                argv += ["--ro-bind", str(target), str(target)]
        else:
            masks.append(str(_host_spelling(f)))

    # (6) CONTAINER-DAEMON SOCKETS (spore-725) — THE CONNECT ARM, MEASURED 2026-09-30 on a Linux
    # kernel (6.12, bubblewrap 0.9.0, in Docker with the namespace restrictions relaxed):
    #   ``--ro-bind S S``           connect SUCCEEDS. A read-only mount does not stop connect(),
    #                               which is the same fact as ``-v docker.sock:...:ro`` still
    #                               granting the daemon. It was K4c's only socket arm as ported.
    #   ``--ro-bind /dev/null S``   connect REFUSED (ECONNREFUSED) — until the daemon RECREATES
    #                               its socket: the host-side unlink detaches the mount and the
    #                               sandbox then reaches the new socket. Measured.
    #   ``--tmpfs P --remount-ro P``  over the socket's own DIRECTORY: ENOENT, and it HOLDS across
    #                               a daemon restart, because the new socket lands in the host dir
    #                               the sandbox cannot see. Measured.
    # So the directory form is used wherever the socket lives in a directory the daemon owns, and
    # the file form only where the parent is shared by everything (/run, $XDG_RUNTIME_DIR) and
    # hiding it would break the host. Paths are the RESOLVED targets, which is what connect() uses.
    # A daemon dir that is absent but creatable by this user (its parent exists and is writable)
    # is still mounted — bwrap creates it empty — because the user-level daemons
    # (``systemctl --user start podman.socket``) can be STARTED FROM INSIDE THE SANDBOX, and a
    # dir that is not hidden at spawn would then show the socket. An absent dir under a parent the
    # user cannot write belongs to a root daemon this user cannot start, and is skipped.
    # ⚠ RESIDUAL, STATED: a socket in a SHARED dir (rootful /run/docker.sock, rootless
    # $XDG_RUNTIME_DIR/docker.sock) is covered only while its inode lives. A restart re-exposes it
    # for the rest of that shell, and a rootless daemon absent at spawn can be started afterwards.
    # Both run through the user's service manager, which is outside this namespace.
    # That manager is itself the sharper bypass, and step (7) hides it: the entity cannot ask it to
    # start or restart anything.
    # Compared RESOLVED, because the socket targets are: a symlinked runtime dir or HOME compared
    # lexically would get the directory form, a read-only tmpfs over the whole runtime dir (L1 + L2).
    shared = {Path(p).resolve() for p in ("/", "/run", "/var/run", "/tmp", "/var/tmp")}
    shared.add(Path.home().resolve())
    for runtime in _runtime_dirs():
        shared.add(Path(runtime).resolve())
    hidden_dirs: list[Path] = []
    for s in sorted(policy.deny_sockets, key=lambda p: str(p)):
        parent = s.parent
        if not _reachable(parent):
            continue   # another user's runtime dir: this user (and so the entity) cannot reach it
        if parent.resolve() in shared:
            if s.exists():
                masks.append(str(s))
            continue
        if parent in hidden_dirs:
            continue
        if parent.is_dir() or (parent.parent.is_dir() and os.access(parent.parent, os.W_OK)):
            argv += ["--tmpfs", str(parent)]
            remount_ro.append(str(parent))
            hidden_dirs.append(parent)

    # (7) THE USER'S SERVICE MANAGER AND SESSION BUS (spore-1244), MEASURED 2026-09-30 in a
    # systemd-as-PID-1 container with a real `systemd --user` (systemd 255, linger on), driving the
    # real provider: with neither hidden, `systemd-run --user --pipe --wait cat <jewel>` from INSIDE
    # the confined shell printed the jewel — the manager runs the command outside this namespace, a
    # whole-floor bypass. With `$XDG_RUNTIME_DIR/systemd` hidden and `$XDG_RUNTIME_DIR/bus` masked,
    # both `systemd-run --user` and `systemctl --user` get "Failed to connect to bus". The same
    # class as the container sockets above (an unsandboxed daemon's socket), so it is denied by
    # default the same way. The cost: the shell has no `systemctl --user` and no session D-Bus.
    # ⚠ A bus started AFTER the shell, or a D-Bus on an abstract socket (older distros), is not
    # covered — the manager that could start one is unreachable from inside, so the entity cannot.
    for r in _runtime_dirs():
        rd = Path(r)
        if not _reachable(rd):
            continue
        sysd = rd / "systemd"
        if _reachable(sysd) and (sysd.is_dir() or (rd.is_dir() and os.access(rd, os.W_OK))):
            argv += ["--tmpfs", str(sysd)]
            remount_ro.append(str(sysd))
        bus = rd / "bus"
        if _reachable(bus) and bus.exists():
            masks.append(str(bus))

    # (8) ANY OTHER procfs MOUNT. ``--bind / /`` is recursive, so a procfs mounted elsewhere on the
    # host (a container runtime's, a chroot's) would still show the host's processes after
    # ``--unshare-pid``. Each is hidden by a read-only tmpfs. Read from this process's own
    # /proc/self/mountinfo, which is the host mount namespace bwrap copies.
    for mp in _extra_procfs_mounts():
        if not _reachable(mp) or _shadowed_by(mp, tuple(tmpfs_roots)):
            continue
        if not mp.is_dir():
            masks.append(str(mp))   # a bind of one procfs FILE: a tmpfs cannot cover a file (L2 r1)
            continue
        argv += ["--tmpfs", str(mp)]
        remount_ro.append(str(mp))

    # The masks close the body (the masks-last rule above). They are appended BEFORE step (1), which
    # pins every directory the body makes bwrap create, so a mask's absent parent is pinned too
    # ("test_bwrap_creates_and_pins_an_absent_ssh_dir_before_planting_anything_in_it" fails if not).
    # One mask per destination: a link and its target are both in the policy (:func:`_spellings`)
    # and can name the same file.
    for m in dict.fromkeys(masks):
        argv += ["--ro-bind", "/dev/null", m]

    # (1) ANCESTOR DIRS, EMITTED FIRST. Parent-before-child is a HARD ordering requirement (bwrap
    # applies ops in sequence and a child mount must land inside an already-pinned parent); sorting
    # path strings is parent-first, because a parent is a prefix of its child.
    # ``--bind P P`` makes each ancestor a MOUNTPOINT, so renaming it is EBUSY — the relocation
    # finding below. Three cases, all measured or reviewed on 2026-09-30:
    #   · PRESENT: pinned at its RESOLVED path. A mount cannot land on a symlink, so /var/run (a
    #     symlink to /run) aborted bwrap until it was resolved.
    #   · A SYMLINK THIS USER CAN REPLACE (its parent is writable, and not $HOME, which step (0)
    #     makes read-only inside bash): REFUSED. Pinning the target leaves
    #     the link itself free to be swapped for a real directory holding a planted file, and macOS
    #     covers that link by name while a mount cannot. Fail closed, as bwrap itself used to.
    #   · ABSENT: pinned only if the body will CREATE something under it — then it is created here
    #     first (by the provider on the host, 0700, just before bwrap runs: bwrap resolves a bind's
    #     SOURCE before it applies ``--dir``, so it cannot pin a directory it creates itself —
    #     measured; recorded in the placeholder ledger, which removes it at close if it is still
    #     empty) and pinned, because an unpinned directory that bwrap creates on the
    #     host can be renamed away and replaced with a planted one (L1 + L2 review: raw mode with no
    #     ~/.ssh, plant ``authorized_keys``). An absent ancestor nothing will be created under holds
    #     nothing and is skipped — a self-bind of a missing source aborts bwrap, which is how the
    #     socket roster's absent daemon dirs (/run/containerd) once cost every Linux host its bash.
    created: list[str] = []
    for k, op in enumerate(argv):
        if op in ("--bind", "--ro-bind"):
            created.append(argv[k + 2])
        elif op in ("--tmpfs", "--dir"):
            created.append(argv[k + 1])
    pins: list[tuple[str, bool]] = []   # (path, needs creating)
    for d in sorted(policy.deny_write_dirs, key=lambda p: str(p)):
        if d.is_symlink():
            # A link directly in $HOME is frozen by step (0)'s read-only bind: pin its target.
            if os.access(d.parent, os.W_OK) and d.parent.resolve() not in frozen_home:
                raise ConfinementError(
                    f"{d} is a symlink in a directory this user can write. The Linux floor pins a "
                    "jewel's parent directories with mounts, and a mount cannot pin a symlink, so "
                    "the link could be swapped for a planted directory. Refusing to grant bash "
                    "hands (fail-closed). Replace the symlink with the real directory to use bash."
                )
            entry = (str(_link_target(d)), False)
        elif d.is_dir():
            entry = (str(d.resolve()), False)
        elif any(c == str(_host_spelling(d)) or c.startswith(str(_host_spelling(d)) + "/")
                 for c in created):
            entry = (str(_host_spelling(d)), True)
        else:
            continue
        e = Path(entry[0])
        if (any(e != v and e.is_relative_to(v) for v in tool_views)
                and not any(e != w and e.is_relative_to(w) for w in tool_windows)):
            # In a tool directory's view and not below a window into the host: the view is read-only
            # and each window is itself a mountpoint, so nothing here can be renamed, and a pin would
            # only create a directory on the host for nothing. Below a window it is pinned as usual
            # (complement, L3 r3).
            continue
        if entry[0] not in [p for p, _ in pins]:
            pins.append(entry)
    # (0) $HOME's OWN ENTRIES ARE READ-ONLY (desk ruling 2026-10-07, option (c)), AS A VIEW (head
    # ruling, same day, (b)). $HOME is a read-only tmpfs holding its existing entries except the
    # credential names: each subdirectory bound back read-write, each file read-only, each link as
    # the same link. Inside bash nothing can be created, removed, renamed or swapped directly in
    # $HOME, while everything below a subdirectory (the workspace, a repo, ~/.cache) is as writable
    # as before; every link at the top level (a dotfile manager's ~/.config, ~/.netrc) is
    # unswappable from inside. And a credential file at the top level (~/.netrc, ~/.npmrc, ...) that
    # the operator's tool creates on the host mid-session lands in the host directory, which bash
    # never sees, not even in a command already running; an absent one needs no placeholder. It goes
    # after the pins of $HOME's own ancestors and BEFORE every deeper mount: a later tmpfs on $HOME
    # would hide the mounts beneath it, and the deeper pins and the body land on top of it. $HOME's
    # own pin is dropped: the tmpfs is itself a mountpoint, so $HOME still cannot be renamed.
    # Costs: a tool that creates a NEW top-level file or directory in $HOME (a first ~/.npm, say)
    # fails inside bash; a top-level file the host replaces after spawn is not seen (a per-file bind
    # pins the inode).
    home_ops: list[str] = []
    home_real = home_ro
    if home_real is not None and policy.workspace.resolve() == home_real:
        raise ConfinementError(
            f"the workspace is {home_real} itself, whose own entries are read-only inside bash on "
            "Linux, so the entity could not write at its workspace root. Refusing to grant bash "
            "hands (fail-closed). Use a subdirectory of your home as the workspace."
        )
    if home_real is not None:
        home_ops = ["--tmpfs", str(home_real)]
        for child in sorted(home_real.iterdir(), key=lambda p: p.name):
            if child in secret_names:
                continue
            if child.is_symlink():
                home_ops += ["--symlink", os.readlink(child), str(child)]
            elif child.is_dir():
                # Not `-try`: a subdirectory removed between the plan and the spawn aborts bwrap (a
                # refusal; the next spawn plans again) rather than starting with it silently missing.
                home_ops += ["--bind", str(child), str(child)]
            else:
                home_ops += ["--ro-bind-try", str(child), str(child)]
    outer: list[str] = []
    ancestors: list[str] = []
    create_first: list[str] = []
    for path, create in sorted(pins):
        if create:
            create_first.append(path)
        if home_ops and Path(path) == home_real:
            continue
        if home_ops and home_real.is_relative_to(path):
            outer += ["--bind", path, path]
        else:
            ancestors += ["--bind", path, path]

    out = head + outer + home_ops + ancestors + argv
    for r in remount_ro:
        out += ["--remount-ro", r]
    if home_ops:
        out += ["--remount-ro", str(home_real)]
    _refuse_bind_after_mask(out)
    return out, create_first


def _bwrap_runs_without_a_pid_namespace() -> bool:
    """True when bwrap starts WITHOUT ``--unshare-pid`` and fails WITH it, which pins a refusal on the
    PID namespace rather than on user namespaces in general. Both probes run here, so the answer
    never rests on another check's result."""
    def runs(*extra: str) -> bool:
        try:
            proc = subprocess.run(
                [BWRAP, "--bind", "/", "/", "--proc", "/proc", "--dev", "/dev", *extra, "/bin/true"],
                capture_output=True, timeout=10, env=_probe_env(),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    return runs() and not runs("--unshare-pid")


def _probe_env() -> dict[str, str]:
    from levain.launch import child_env   # stdlib-only, so this module stays a dependency leaf

    return child_env()


# --- one cgroup v2 leaf per command (S2, Phill 2026-10-09: "yes sorry, that is correct: A'") --------
# Every process of a Linux command lives in ONE cgroup v2 leaf levain names before the command runs: a
# transient scope of the operator's own systemd user manager (``systemd-run --user --scope``), which
# systemd delegates to the operator. kill = write ``1`` to its ``cgroup.kill``; gone = its
# ``cgroup.events`` says ``populated 0``, or the leaf is absent (the kernel refuses to remove a populated
# cgroup, and systemd removes an empty scope). No pid is needed for either. Why a user scope and not a
# unit setup installs: a process may be moved between cgroups only by a writer of the COMMON
# ANCESTOR's ``cgroup.procs``, which for the operator's session and any delegated unit is root's (RUN on
# argushub 2026-10-09, project_memory/1009-13_S2/DESIGN.md P1); the user manager does the move for us.
SYSTEMD_RUN = "/usr/bin/systemd-run"
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_LEVAIN_SLICE = "levain.slice"
_CGROUP_KILL_KERNEL = (5, 14)   # cgroup.kill first shipped in 5.14
_LINGER_REMEDY = ("run `sudo levain setup-isolation` (it enables linger for you, so your systemd user "
                  "manager runs without a login session), or `sudo loginctl enable-linger $USER`")


def _user_manager_rel(uid: int) -> str:
    return f"user.slice/user-{uid}.slice/user@{uid}.service"


def _leaf_rel(uid: int, unit: str) -> str:
    """The leaf of transient scope ``unit``, relative to the cgroup root (layout RUN on systemd 255)."""
    return f"{_user_manager_rel(uid)}/{_LEVAIN_SLICE}/{unit}.scope"


# A leaf's unit names the levain process that made it (pid namespace, pid and kernel start time), so a
# sweep can tell a crashed levain's leaf from a live one's without any other record. A pid names a
# process only inside its own pid namespace, so leaves are made, and swept, only from the host's initial
# one (PROC_PID_INIT_INO, include/linux/proc_ns.h); a levain in a nested namespace is refused by name,
# since its leaves would outlive it with no sweep able to judge them (L3 r10, r11). The namespace stays
# in the name so the sweep judges only names it can read, whatever made them; a name without it is
# never judged (L3 r12).
# Bounded, canonical fields (no leading zero): a tag is read back from the ledger, an unbounded digit run
# would not convert (L3 r14), and a padded number would not compare equal to /proc's (L3 r18).
_N = r"(?:0|[1-9][0-9]{0,19})"
_LEAF_UNIT = re.compile(rf"levain-({_N})-({_N})-({_N})-[0-9a-f]{{12}}-{_N}\.scope")
_INIT_PIDNS = "4026531836"
_PID_MAX_LIMIT = 4194304   # PID_MAX_LIMIT on 64-bit Linux (include/linux/threads.h); pids stay below it


def _leaf_unit(token: str, n: int) -> str | None:
    """The unit name of this levain's leaf number ``n``; None when this levain must not make one (not
    in the initial pid namespace) or /proc cannot say who it is."""
    started = _proc_start_time(os.getpid())
    if started is None or _pidns() != _INIT_PIDNS:
        return None
    return f"levain-{_INIT_PIDNS}-{os.getpid()}-{started}-{token}-{n}"


def _leaf_orphaned(rel: str, uid: int) -> bool:
    """Whether leaf ``rel`` of ``uid`` may be killed as a crashed levain's: a levain leaf name from the
    initial pid namespace, read from that namespace, whose maker (its pid and start time) is gone. The
    crash sweep and the claim path both ask only this, so they cannot disagree about a leaf (L3 r13, r14)."""
    head = f"{_user_manager_rel(uid)}/{_LEVAIN_SLICE}/"
    m = _LEAF_UNIT.fullmatch(rel[len(head):]) if rel.startswith(head) else None
    if m is None or m.group(1) != _INIT_PIDNS or _pidns() != _INIT_PIDNS:
        return False
    # Gone means the kernel says so: no such process, a zombie, or a later start time. A read that fails
    # any other way, or a stat line too short to read, is not knowing, and an unknown maker keeps its
    # leaf (L3 r15: an EMFILE here killed a live levain's command).
    pid = int(m.group(2))
    if not 1 <= pid < _PID_MAX_LIMIT:
        return False   # no levain has that pid: not a name to judge (L3 r17: kill(0) is our own group)
    try:
        fields = _stat_fields(pid)
    except (FileNotFoundError, ProcessLookupError):
        try:
            os.kill(pid, 0)   # a /proc that hides processes (hidepid) also says ENOENT (L3 r16)
        except ProcessLookupError:
            return True
        except OSError:
            pass
        return False
    except OSError:
        return False
    if len(fields) < 20:
        return False
    return fields[0] in (b"Z", b"X", b"x") or fields[19] != m.group(3).encode()


def _stat_fields(pid: int) -> list[bytes]:
    """The fields of ``/proc/<pid>/stat`` after the command name, as bytes: a process sets its own
    name, and one that does not decode must not stop a sweep (L3 r16)."""
    return Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[-1].split()


def sweep_dead_leaves() -> list[str]:
    """Kill every leaf whose levain is gone (its pid absent, or now another process's), and return the
    ones still populated afterwards. A crashed levain's sandbox can outlive it: bwrap's PDEATHSIG misses a
    namespace init cloned before it is armed (bubblewrap #633, #700; reproduced by the S2 proving run)."""
    uid = os.getuid()
    base = _CGROUP_ROOT / _user_manager_rel(uid) / _LEVAIN_SLICE
    try:
        names = os.listdir(base)
    except OSError:
        return []
    left: list[str] = []
    killed: list[str] = []
    for name in names:
        rel = f"{_user_manager_rel(uid)}/{_LEVAIN_SLICE}/{name}"
        if not _leaf_orphaned(rel, uid):
            continue
        (killed if _leaf_kill(rel) else left).append(rel)
    # Every leaf is killed first, then all are awaited against ONE deadline: the sweep runs at every
    # CLI start, so a wait per leaf would add up (L3 r10).
    deadline = time.monotonic() + 5.0
    for rel in killed:
        if not _leaf_gone(rel, timeout=max(0.0, deadline - time.monotonic())):
            left.append(rel)
    return left


def _cgroup_problem(*, release: str | None = None, osrelease: str | None = None, mounts: str | None = None,
                    container: bool | None = None, pidns: str | None = None, uid: int | None = None,
                    root: Path = _CGROUP_ROOT) -> tuple[str, str | None] | None:
    """Why a Linux command cannot get a cgroup leaf here, as (reason, remedy), or None. Each refusal is
    by name; there is no fallback that runs a command without its leaf. The inputs default to this
    host's; they are parameters so each refusal is testable off Linux."""
    if osrelease is None:
        try:
            osrelease = Path("/proc/sys/kernel/osrelease").read_text()
        except OSError:
            osrelease = ""
    if "microsoft" in osrelease.lower() or "wsl" in osrelease.lower():
        return ("this is WSL2, which levain's Linux floor does not support yet (its cgroup setup is "
                "not measured)", "run levain on a Linux host")
    if container is None:
        container = any(os.path.exists(f) for f in ("/.dockerenv", "/run/.containerenv",
                                                     "/run/systemd/container"))
    if container:
        return ("this is a container, which levain's Linux floor does not support yet (cgroup "
                "delegation into it is not measured)", "run levain on the host")
    pidns = _pidns() if pidns is None else pidns
    if pidns == "-":
        return ("levain cannot read its pid namespace from /proc/self/ns/pid, so a crashed levain's "
                "command scopes could not be told from a live one's", "run levain with /proc mounted")
    if pidns != _INIT_PIDNS:
        return ("levain is running in a nested pid namespace, where a crashed levain's command scopes "
                "could not be told from a live one's", "run levain in the host's pid namespace")
    m = re.match(r"(\d+)\.(\d+)", release if release is not None else os.uname().release)
    if not m or (int(m.group(1)), int(m.group(2))) < _CGROUP_KILL_KERNEL:
        return (f"Linux {release if release is not None else os.uname().release} has no cgroup.kill "
                "(it needs 5.14 or later), which is how levain stops every process of a command",
                "upgrade the kernel")
    if mounts is None:
        try:
            mounts = Path("/proc/self/mounts").read_text()
        except OSError:
            mounts = ""
    fields = [ln.split() for ln in mounts.splitlines()]
    v2 = [f for f in fields if len(f) > 3 and f[1] == str(root) and f[2] == "cgroup2"]
    legacy = any(len(f) > 2 and f[2] == "cgroup" for f in fields)
    if not v2 or legacy:
        return (f"cgroups here are not the unified v2 hierarchy at {root} (v1 or hybrid), which "
                "levain needs to account for every process of a command",
                "boot with the unified cgroup hierarchy (`systemd.unified_cgroup_hierarchy=1`)")
    # Without `nsdelegate` a cgroup namespace is not a delegation boundary, and a command could move
    # itself out of its leaf despite `--unshare-cgroup` (kernel cgroup-v2.rst, "nsdelegate").
    if "nsdelegate" not in v2[-1][3].split(","):
        return (f"cgroup2 at {root} is mounted without `nsdelegate`, so a command could leave its "
                "cgroup", "mount cgroup2 with `nsdelegate` (systemd does this by default)")
    if not (os.path.isfile(SYSTEMD_RUN) and os.access(SYSTEMD_RUN, os.X_OK)):
        return (f"{SYSTEMD_RUN} is missing: levain puts each command in a systemd user scope",
                "use a systemd host")
    uid = os.getuid() if uid is None else uid
    mgr = root / _user_manager_rel(uid)
    try:
        st = os.lstat(mgr)
        ok = stat.S_ISDIR(st.st_mode) and st.st_uid == uid
    except OSError:
        ok = False
    if not ok:
        return (f"your systemd user manager is not running ({mgr} is absent or not yours)", _LINGER_REMEDY)
    if root == _CGROUP_ROOT and not os.path.exists(f"/run/user/{uid}/bus"):
        return (f"your systemd user manager has no bus at /run/user/{uid}/bus yet, which "
                "`systemd-run --user` needs", _LINGER_REMEDY)
    return None


def _leaf_chain_problem() -> str | None:
    """Run the launch chain every command uses, minus the floor's mounts: a transient user scope
    around ``bwrap --unshare-pid --unshare-cgroup`` with cgroupfs read-only. None when it ran, else what
    it said. Static checks cannot see an LSM, a seccomp profile or a user manager that refuses (L3 r9)."""
    unit = _leaf_unit(os.urandom(6).hex(), 0)
    if unit is None:
        return "levain cannot read its own start time from /proc, or is not in the host's pid namespace"
    rel = _leaf_rel(os.getuid(), unit)
    argv = ["/usr/bin/env", f"XDG_RUNTIME_DIR=/run/user/{os.getuid()}", SYSTEMD_RUN, "--user", "--scope",
            "--quiet", "--collect", f"--slice={_LEVAIN_SLICE}", f"--unit={unit}", "--",
            BWRAP, "--die-with-parent", "--bind", "/", "/", "--proc", "/proc", "--dev", "/dev",
            "--unshare-pid", "--unshare-cgroup", "--ro-bind", "/sys/fs/cgroup", "/sys/fs/cgroup",
            "/bin/true"]
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, env=_probe_env())
    except OSError as exc:
        return str(exc)
    try:
        _, err = proc.communicate(timeout=20)
    except subprocess.SubprocessError:
        # Killing systemd-run alone would leave a stalled bwrap in its scope, owned by this live levain,
        # so no sweep would ever reap it (L3 r10): kill the scope too and wait for it to empty.
        proc.kill()
        _leaf_kill(rel)
        try:
            proc.communicate(timeout=5)
        except subprocess.SubprocessError:
            pass
        if _leaf_gone(rel, timeout=5.0):
            return "the launch probe did not finish within 20 s"
        return f"the launch probe did not finish within 20 s, and its scope {rel} could not be emptied"
    if proc.returncode == 0:
        return None
    said = err.decode("utf-8", "replace").strip().splitlines()
    return said[-1] if said else f"exit {proc.returncode}"


def _leaf_populated(rel: str) -> bool | None:
    """True or False from the leaf's ``cgroup.events``; None when the leaf is absent (removed)."""
    try:
        text = (_CGROUP_ROOT / rel / "cgroup.events").read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ENODEV:   # removed while open
            return None
        return True   # cannot tell: not gone
    for line in text.splitlines():
        if line.startswith("populated "):
            return line.split()[1] != "0"
    return True


def _leaf_kill(rel: str) -> bool:
    """SIGKILL every process in the leaf and its descendants (``cgroup.kill``). True when written, or
    the leaf is absent; False when the write failed."""
    try:
        (_CGROUP_ROOT / rel / "cgroup.kill").write_text("1")
    except FileNotFoundError:
        return not (_CGROUP_ROOT / rel).exists()   # the leaf is gone; a leaf with no cgroup.kill is not
    except OSError as exc:
        return exc.errno == errno.ENODEV
    return True


def _leaf_gone(rel: str, *, timeout: float) -> bool:
    """True once the leaf has no live process (polled up to ``timeout``)."""
    deadline = time.monotonic() + timeout
    while _leaf_populated(rel) is True:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def bwrap_available() -> bool:
    """True iff ``bwrap`` is present AND CAN ACTUALLY ESTABLISH A NAMESPACE ON THIS HOST RIGHT NOW.

    ⛔ THIS EXECUTES bwrap. THAT IS THE POINT, AND A FILE-EXISTENCE CHECK IS NOT A SUBSTITUTE — the
    check that spore-418 specified (``bwrap`` present + ``unprivileged_userns_clone``) REPORTS GREEN
    ON A HOST WHERE bwrap CANNOT RUN. Measured on argushub 2026-09-03: ``/usr/bin/bwrap`` present,
    ``kernel.unprivileged_userns_clone = 1``, and every invocation fails with ``setting up uid map:
    Permission denied``, because Ubuntu 23.10+ gates unprivileged user namespaces behind a DIFFERENT
    knob — ``kernel.apparmor_restrict_unprivileged_userns`` — that the specified probe never reads.
    Two proxies both green, the real target red: the honest probe is to run the thing.

    Deliberately NOT cached. It is one ``bwrap --bind / / /bin/true`` (single-digit milliseconds), and
    a cached answer would survive an operator loading an AppArmor profile or flipping the sysctl —
    reporting "no confinement" for the life of the process on a box that has just become capable."""
    if not (os.path.isfile(BWRAP) and os.access(BWRAP, os.X_OK)):
        return False
    try:
        proc = subprocess.run(
            [BWRAP, "--bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "--unshare-pid",
             "/bin/true"],
            capture_output=True,
            timeout=10,
            env=_probe_env(),
        )
    except (OSError, subprocess.SubprocessError):
        # Cannot even attempt it -> not available. FAIL CLOSED; never let an exception here read as
        # "probably fine".
        return False
    return proc.returncode == 0


def bwrap_netns_available() -> bool:
    """True iff bwrap can ALSO make a new network namespace here — the localhost deny's argv.

    :func:`bwrap_available` runs without ``--unshare-net``, so it says nothing about a host where a
    user namespace works and a network namespace does not (a kernel without ``CONFIG_NET_NS``,
    gVisor, a loopback setup bwrap cannot complete). There, every bash spawn under the deny dies at
    startup, and doctor and the banner must not have promised bash (complement + codex, L3 on
    option B). Same shape and the same fail-closed rule as :func:`bwrap_available`: run it."""
    if not (os.path.isfile(BWRAP) and os.access(BWRAP, os.X_OK)):
        return False
    try:
        proc = subprocess.run(
            [BWRAP, "--unshare-net", "--bind", "/", "/", "--proc", "/proc", "--dev", "/dev",
             "--unshare-pid", "/bin/true"],
            capture_output=True,
            timeout=10,
            env=_probe_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


# ⚖ THE LINUX POSTURE, DECIDED 2026-10-01 (option B, under Phill's go): bwrap has no
# per-destination connect deny, so the deny renders as ``--unshare-net`` and bash runs with no IP
# network (pathname unix sockets remain: ``OFFLINE_RESIDUAL``). Measured on argushub the same day: under ``--unshare-net`` a 127.0.0.1 listener is refused
# (curl rc 7), an abstract-socket D-Bus is refused (busctl rc 1), the internet is gone (rc 6) and bash
# itself works; with no flag all three are reachable. The entity's own model calls are made by the
# levain process, outside bwrap. ``allow_localhost_outbound: true`` gives bash the network back, with
# the spore-755 exposure. Until 2026-10-01 the deny refused bash outright on Linux (option C).
#: The refusal for a provider that CANNOT enforce the deny (see :func:`resolve_localhost_deny`).
LINUX_LOCALHOST_REFUSAL = (
    "this platform's sandbox cannot block connections back to this host, and this entity's floor "
    "requires that (a local sshd reached through the forwarded ssh agent reads crown-jewel files as "
    "an unsandboxed user — spore-755). The file editor still works. To accept that exposure and get "
    "bash, set \"allow_localhost_outbound\": true in .levain/confinement.json."
)

#: The refusal when the provider enforces the deny by ``--unshare-net`` but the probe for it failed
#: (see :func:`bwrap_netns_available`; a timeout or a transient bwrap failure lands here too).
NETNS_REFUSAL = (
    "the `bwrap --unshare-net` probe failed on this host, and this entity's floor blocks "
    "connections back to this host by removing bash's network. The file editor still works. "
    "To accept that exposure and get bash, set \"allow_localhost_outbound\": true in "
    ".levain/confinement.json."
)

#: What bash can still reach when the deny runs as ``--unshare-net``. One string, printed by the
#: banner and doctor, so the residual is named everywhere the posture is (codex, L3 on option B
#: twice; the ControlMaster class is spore-1005).
OFFLINE_RESIDUAL = (
    "bash keeps only its own isolated loopback. NOT blocked: a unix socket at a file path the floor "
    "does not deny (an ssh ControlMaster or a proxy socket in /tmp, D-Bus, X11), which can reach "
    "this host's services and so bypass the block (spore-1005); and inside a VM, AF_VSOCK to the "
    "hypervisor"
)


def resolve_localhost_deny(allow_localhost_outbound: bool) -> tuple[bool, str | None, bool]:
    """``(bash_ok, refusal, offline)`` for a host that can otherwise sandbox bash.

    ONE computation for ``levain run`` (session) and ``levain doctor``, which used to compute this
    separately and disagree when :func:`select_provider` raised (glm, L3 on option B). Fails closed:
    anything undeterminable drops bash, never promises it."""
    if allow_localhost_outbound:
        return True, None, False
    try:
        provider = select_provider()
        if not provider.enforces_localhost_deny:
            return False, LINUX_LOCALHOST_REFUSAL, False
        if not provider.localhost_deny_ready():
            return False, NETNS_REFUSAL, False
        return True, None, bool(provider.localhost_deny_removes_network)
    except Exception as exc:  # noqa: BLE001 — undetermined == no bash (the honesty floor)
        return False, (f"the localhost deny could not be checked on this host ({exc}). To accept "
                       "that exposure and get bash, set \"allow_localhost_outbound\": true in "
                       ".levain/confinement.json."), False


_UNREACHABLE = (-1, -1, -1)   # lstat refused (EACCES/EPERM): watched for becoming reachable


_STARTUP_EXEC_VARS = frozenset({"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "PS4", "PROMPT_COMMAND"})


def _identity(p: Path) -> tuple[int, int, int] | None:
    """``(st_dev, st_ino, file type)`` of ``p`` without following a link, None if it is absent, or
    ``_UNREACHABLE`` if this user cannot even stat it (another user's runtime directory: the plan
    leaves such a path alone, so it must not cost the shell either; L3 r4). Any other ``lstat``
    error propagates (the caller refuses)."""
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return None
    except PermissionError:
        # Unreachable only when the directory blocking the way belongs to ANOTHER user: one this user
        # owns, a same-uid shell can chmod open, read through and close again (complement L3 r5).
        for a in p.parents:
            try:
                ast = os.lstat(a)
            except PermissionError:
                continue
            if ast.st_uid == os.geteuid():
                raise
            return _UNREACHABLE
        raise
    return (st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode))


def _describe(ident: tuple[int, int, int] | None) -> str:
    if ident is None:
        return "absent"
    if ident == _UNREACHABLE:
        return "unreachable"
    kind = {stat.S_IFREG: "file", stat.S_IFDIR: "dir", stat.S_IFLNK: "symlink"}.get(ident[2], "other")
    return f"{kind} inode {ident[1]}"


def _mount_plan_paths(
    argv: list[str], policy: CrownJewelsPolicy
) -> tuple[dict[str, str | None], list[str]]:
    """What the bwrap plan covers on the host: ``(mounted, unmounted)``.

    ``mounted`` maps each host path the plan mounts over to the mountpoint the provider may create if
    it is absent: ``"dir"`` for a tmpfs, ``"file"`` for a ``/dev/null`` bind, or None for a bind it must
    never create (a self-bind of something that exists, a ``--*-try`` bind the plan lets bwrap skip,
    an ancestor pin the provider makes separately) (L3 r4). ``unmounted`` is
    every jewel path the policy names that the plan does NOT mount (an absent jewel under a read-only
    store, a SQLite sidecar absent at spawn): nothing covers those, so they are watched for appearing.
    The first op on a path decides its kind, so a path both self-bound and masked records None (the
    self-bind comes first; its source exists, so nothing is created). A path strictly inside a tmpfs
    root is left out of both, by EXACT containment: the tmpfs hides the
    host tree there, and a case-folded match would drop a distinct path on a case-sensitive filesystem
    (L3 2026-10-03). Unless it is strictly inside a host directory self-bound into that tmpfs after it
    (a tool view's window): below one the host tree is back (L3 r3)."""
    mounted: dict[str, str | None] = {}
    tmpfs: list[Path] = []
    windows: list[Path] = []   # host directories bound back inside a tmpfs: host-backed again below
    i = 0
    while i < len(argv):
        op = argv[i]
        if op == "--tmpfs":
            mounted.setdefault(argv[i + 1], "dir")
            tmpfs.append(Path(argv[i + 1]))
            i += 2
        elif op in ("--bind", "--ro-bind", "--bind-try", "--ro-bind-try"):
            src, dst = argv[i + 1], argv[i + 2]
            if dst != "/":
                kind = "file" if src == "/dev/null" else None   # /dev/null always exists (codex L3 r5)
                mounted.setdefault(dst, kind)
                pd = Path(dst)
                if (src == dst and any(pd != t and pd.is_relative_to(t) for t in tmpfs)
                        and os.path.isdir(dst)):
                    windows.append(pd)
            i += 3
        elif op in ("--proc", "--dev", "--remount-ro", "--dir"):
            i += 2
        elif op == "--symlink":   # a link made in a tmpfs view (a tool directory): nothing on the host
            i += 3
        else:
            i += 1

    def hidden(q: str) -> bool:
        # Strictly inside a tmpfs, and not strictly inside a host directory bound back into it (a
        # tool view's ~/.aws/sso: what lies below it is the host's again; codex, L3 r3).
        # The NEAREST covering mount decides, so a tmpfs or mask nested inside a window is hidden
        # again (L1 r3: ~/.aws/sso/cache's contents under the ~/.aws/sso window).
        pq = Path(q)
        covers = [(len(r.parts), 1) for r in tmpfs if pq != r and pq.is_relative_to(r)]
        covers += [(len(w.parts), 0) for w in windows if pq != w and pq.is_relative_to(w)]
        return bool(covers) and max(covers)[1] == 1

    mounted = {q: k for q, k in mounted.items() if not hidden(q)}
    unmounted = [q for q in _named_jewel_paths(policy) if q not in mounted and not hidden(q)]
    return mounted, unmounted


def _named_jewel_paths(policy: CrownJewelsPolicy) -> list[str]:
    """Every jewel path the policy names, spelled as the plan spells it (real parent, final component
    unresolved). EVERY deny_read_write root, not only file-shaped ones: an absent root created as a
    directory would otherwise be neither mounted nor watched (codex L3 r4)."""
    named = [*policy.deny_files, *policy.deny_write_files, *policy.own_memory_files,
             *policy.sqlite_sidecars, *policy.deny_read_write]
    if policy.config_file is not None:
        named.append(policy.config_file)

    def spelled(p: Path) -> str:
        try:
            return str(p.parent.resolve() / p.name)
        except (OSError, RuntimeError):
            return str(p)

    return list(dict.fromkeys(spelled(Path(p)) for p in named))


def _prepare_mountpoints(mounted: dict[str, str | None],
                         created: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    """Create every absent host mountpoint before bwrap runs, so the manifest can be recorded before
    the start and nothing is adopted afterwards. bwrap would create the same things (an empty 0444
    file for a ``/dev/null`` bind, a directory for a tmpfs); creating them here first means the object
    it mounts over is the one recorded. An exclusive create that loses a race keeps whatever is there,
    which is still recorded before bwrap mounts over it. Returns every object THIS call created,
    parents included, as ``(path, "file" | "dir")``: the ledger owns exactly those (its own
    successful ``mkdir`` or ``O_EXCL`` create, never a path merely seen absent), and removes them
    once no session needs them. Each is appended to ``created`` (the caller's list, when given) as it
    is made, so a failure part-way still leaves the caller holding what was already created."""
    if created is None:
        created = []

    def mkdir(a: Path) -> None:
        try:
            a.mkdir(mode=0o700)   # each level 0700: mkdir(parents=True) gives the umask (glm L3 r5)
        except FileExistsError:
            return                # someone else's (or a racing spawn's): not levain's to remove
        created.append((str(a), "dir"))

    for q, kind in mounted.items():
        p = Path(q)
        if kind is None or os.path.lexists(p):
            continue
        # bwrap would create missing parents too; without them a jewel under an absent directory
        # (~/.config/gh/hosts.yml on a host with no ~/.config/gh) refused bash (complement L3 r4).
        for a in [a for a in reversed(p.parents) if not os.path.lexists(a)]:
            mkdir(a)
        if kind == "dir":
            mkdir(p)
        else:
            try:
                fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                             0o444)
            except FileExistsError:
                continue
            try:
                os.fchmod(fd, 0o444)   # exactly 0444 whatever the umask: the ledger checks the mode
            except OSError:
                # Not the 0444 file the ledger knows how to remove (under umask 077 it is 0400), so it
                # is removed now, while this call still holds it and knows it is the one it made.
                try:
                    st, lst = os.fstat(fd), os.lstat(p)
                    if (st.st_dev, st.st_ino) == (lst.st_dev, lst.st_ino):
                        os.unlink(p)
                finally:
                    os.close(fd)
                raise
            os.close(fd)
            created.append((q, "file"))
    return created


# --- the session-scoped placeholder ledger (Linux) -----------------------------------------------
#
# A mount needs a mountpoint, so an absent jewel under a writable directory (a cred directory under
# an existing ~/.config, say) is masked over a placeholder the provider creates. Skipping it would let
# the shell PLANT the file, so the placeholder stays, but only while a session needs it: before this
# ledger such placeholders were left on the host for good, and ``npm login`` / ``docker login`` then
# failed on a 0444 file. (A $HOME-level cred file needs none since $HOME became a view, step (0).)
# ⛔ Never unlinked while a session might rely on it. Since Linux 3.18 a host-side unlink of a
# file that is a mountpoint in ANOTHER mount namespace succeeds and lazily DETACHES that mount
# (torvalds/linux 8ed936b, "vfs: Lazily remove mounts on unlinked files and directories"), which
# would let a live shell create the file. So each object carries the claims of the shells that mask
# it, and is removed only when none is left: at close, at levain exit, and by the sweep that runs
# before every Linux spawn, at levain's launch and in ``levain doctor`` (a crash or SIGKILL leaves
# claims whose process is gone).
# ⛔ Removed only while it is still the object created: same device and inode, and for a file still
# empty and 0444, for a directory still empty. An operator's ``npm login`` that replaced it by rename
# leaves a different inode, which this keeps (and the replacement closed the live shell, through the
# manifest check in :class:`_BwrapShell`).
# The ledger directory is a crown jewel (``build_policy``), so the entity can neither read it nor
# forge a claim. Directories the provider creates to pin them (an absent ~/.kube, say) are recorded
# the same way.

_LEDGER_NAME = "placeholders.json"


def _ledger_dir() -> Path:
    return Path.home() / ".levain-runtime" / "floor"


def _ledger_dir_problem(create: bool) -> str | None:
    """Why the ledger directory cannot be trusted, or None. It must be a real directory (no link at
    ``~/.levain-runtime`` or at ``floor``), owned by this user, and ``floor`` must be 0700: a link there
    could point the ledger at a file someone else wrote, and the ledger decides what levain deletes.
    With ``create``, absent levels are made 0700 first."""
    d = _ledger_dir()
    for level in (d.parent, d):
        if create and not os.path.lexists(level):
            try:
                level.mkdir(mode=0o700)
            except FileExistsError:
                pass
            except OSError as exc:
                return f"cannot create {level} ({exc})"
        try:
            st = os.lstat(level)
        except FileNotFoundError:
            return f"{level} does not exist"
        except OSError as exc:
            return f"cannot inspect {level} ({exc})"
        if not stat.S_ISDIR(st.st_mode):
            return f"{level} is not a directory (a link or a file)"
        if st.st_uid != os.geteuid():
            return f"{level} is owned by another user"
    if stat.S_IMODE(os.lstat(d).st_mode) != 0o700:
        return f"{d} is not mode 0700"
    return None


def _proc_start_time(pid: int) -> str | None:
    """The kernel's start time of ``pid`` (Linux, field 22 of /proc/<pid>/stat), or None elsewhere or
    when unreadable. Part of a claim, so a reused pid does not keep a dead session's claim alive."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return data.rsplit(")", 1)[-1].split()[19]


def _pidns() -> str:
    try:
        return str(os.stat("/proc/self/ns/pid").st_ino)
    except OSError:
        return "-"


def _new_claim() -> str:
    """``pid:starttime:pidns:token``. A claim made in another pid namespace (a container sharing this
    home) cannot be judged from here, so :func:`_claim_alive` keeps it."""
    pid = os.getpid()
    return f"{pid}:{_proc_start_time(pid) or '-'}:{_pidns()}:{os.urandom(6).hex()}"


def _claim_alive(claim: str) -> bool:
    parts = claim.split(":")
    try:
        pid = int(parts[0])
    except ValueError:
        return False
    if pid <= 0:
        return False
    if len(parts) >= 4 and parts[2] != _pidns():
        return True   # made in another pid namespace: not ours to judge, so kept
    try:
        os.kill(pid, 0)
        owner = True
    except ProcessLookupError:
        owner = False
    except PermissionError:
        # Another user's process holds the pid. Its start time, where /proc shows it, tells levain
        # itself from a reuse (L3 r10); unreadable or unrecorded, the claim is kept.
        now = _proc_start_time(pid)
        if now is None or len(parts) < 4 or parts[1] == "-" or now == parts[1]:
            return True
        owner = False
    except OSError:
        owner = False
    if owner and len(parts) >= 4 and parts[1] != "-":
        now = _proc_start_time(pid)
        owner = now is None or now == parts[1]   # a later start: the pid was reused
    if owner:
        return True
    # levain is gone (dead, or its pid now another process's), but its sandbox may not be: bwrap's
    # PDEATHSIG does not reach a namespace init cloned before it is armed (bubblewrap #633, #700). The
    # tag says what the claim covers, and each kind is judged on its own (L3 r9: a reused pid dropped
    # the claim without looking at the leaf).
    tag = parts[4] if len(parts) >= 5 else ""
    if tag.startswith("c"):
        # The leaf of the command it covers, tagged before that command was spawned: kill it, and keep
        # the claim while it is populated (the next sweep looks again; no wait under the ledger lock).
        rel = tag[1:]
        if not _leaf_orphaned(rel, os.getuid()):
            return True   # not ours to judge, or its maker is alive: kept
        _leaf_kill(rel)
        return not _leaf_gone(rel, timeout=0.5)
    if tag.startswith("b"):
        # The previous build's tag: the command's bash, pid 1 of its namespace, by pid and kernel start
        # time; a zombie pid 1 means its namespace is already empty.
        pid_s, _, start = tag[1:].partition("@")
        try:
            data = Path(f"/proc/{int(pid_s)}/stat").read_text()
        except (ValueError, OSError):
            return False
        fields = data.rsplit(")", 1)[-1].split()
        if len(fields) < 20 or fields[0] in ("Z", "X", "x"):
            return False
        return start in ("", "-") or fields[19] == start
    if tag.startswith("g"):
        # An earlier build's tag names the command's process group. killpg(0) is levain's own group and
        # 1 is init's: neither is a command's, so neither keeps a claim.
        try:
            if int(tag[1:]) <= 1:
                return False
            os.killpg(int(tag[1:]), 0)
            return True
        except (ValueError, ProcessLookupError):
            return False
        except PermissionError:
            return True
    return False


def _object_unchanged(entry: dict) -> bool:
    try:
        st = os.lstat(entry["path"])
    except OSError:
        return False
    if (st.st_dev, st.st_ino) != (entry.get("dev"), entry.get("ino")):
        return False
    if entry.get("kind") == "dir":
        if not stat.S_ISDIR(st.st_mode):
            return False
        try:
            return not os.listdir(entry["path"])
        except OSError:
            return False
    return stat.S_ISREG(st.st_mode) and st.st_size == 0 and stat.S_IMODE(st.st_mode) == 0o444


def _identity_matches(entry: dict) -> bool:
    try:
        st = os.lstat(entry["path"])
    except OSError:
        return False
    return (st.st_dev, st.st_ino) == (entry.get("dev"), entry.get("ino"))


class _LedgerTxn:
    """One transaction on the ledger, under its exclusive lock: everything a spawn does to the objects
    it relies on (sweep, plan, create, record, claim) happens inside one, so another session's release
    or sweep cannot remove an object between this spawn deciding to use it and claiming it (L1 r1).
    On exit, every entry no live claim holds is dropped, and its object removed when it is still the
    one levain made. ``ok`` is False when the ledger cannot be trusted or read; then nothing is
    recorded and nothing removed, which leaves objects where they are rather than guessing."""

    def __init__(self) -> None:
        self.ok = False
        self.problem: str | None = None
        self.entries: list[dict] = []
        self.removed: list[str] = []
        self.rollback: set[str] = set()   # objects to remove at once if this commit fails
        self._lock = None

    def __enter__(self) -> "_LedgerTxn":
        import fcntl

        self.problem = _ledger_dir_problem(create=True)
        if self.problem is not None:
            return self
        d = _ledger_dir()
        try:
            self._lock = open(d / (_LEDGER_NAME + ".lock"), "a")
            fcntl.flock(self._lock, fcntl.LOCK_EX)
            try:
                data = json.loads((d / _LEDGER_NAME).read_text(encoding="utf-8"))
                entries = data["entries"]
            except FileNotFoundError:
                entries = []
            if not isinstance(entries, list):
                raise ValueError("entries is not a list")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.problem = f"the ledger {d / _LEDGER_NAME} cannot be read ({exc})"
            return self
        self.entries = [e for e in entries if isinstance(e, dict) and isinstance(e.get("path"), str)]
        for e in self.entries:
            e["claims"] = [c for c in e.get("claims", []) if isinstance(c, str)]
        self.ok = True
        return self

    def sweep(self) -> None:
        """Drop the claims of processes that no longer exist."""
        for e in self.entries:
            e["claims"] = [c for c in e["claims"] if _claim_alive(c)]

    def record(self, created: list[tuple[str, str]]) -> None:
        """Record objects levain itself just created (its own successful ``mkdir`` or ``O_EXCL``
        create, never "absent when looked at"), with their identity, unclaimed."""
        for path, kind in created:
            try:
                st = os.lstat(path)
            except OSError:
                continue
            self.entries = [e for e in self.entries if e["path"] != path]
            self.entries.append({"path": path, "kind": kind, "dev": st.st_dev, "ino": st.st_ino,
                                 "claims": []})

    def claim(self, paths: set[str], claim: str) -> None:
        """Add ``claim`` to every entry at one of ``paths`` whose object is still the one recorded."""
        for e in self.entries:
            if e["path"] in paths and _identity_matches(e):
                e["claims"].append(claim)

    def retag(self, old: str, new: str) -> None:
        for e in self.entries:
            e["claims"] = [new if c == old else c for c in e["claims"]]

    def drop(self, claim: str) -> None:
        for e in self.entries:
            e["claims"] = [c for c in e["claims"] if c != claim]

    def __exit__(self, *exc: object) -> None:
        try:
            if not self.ok:
                return
            kept: list[dict] = []
            # Children before parents, so a created ~/.config/git goes before a created ~/.config.
            for e in sorted(self.entries, key=lambda e: len(e["path"]), reverse=True):
                if e["claims"]:
                    kept.append(e)
                    continue
                if _object_unchanged(e):
                    try:
                        if e.get("kind") == "dir":
                            os.rmdir(e["path"])
                        else:
                            os.unlink(e["path"])
                        self.removed.append(e["path"])
                    except OSError:
                        kept.append(e)   # still there: try again next time
                elif e.get("kind") == "dir" and _identity_matches(e):
                    # Still levain's directory, only not empty yet (another session's object inside
                    # it, say): kept, so it goes once that is gone (L3 r1).
                    kept.append(e)
                # else: it is no longer the object levain made (the operator's now); forget it
            path = _ledger_dir() / _LEDGER_NAME
            tmp = path.with_name(path.name + ".tmp")
            try:
                tmp.write_text(json.dumps({"entries": sorted(kept, key=lambda e: e["path"])}, indent=1),
                               encoding="utf-8")
                os.replace(tmp, path)
            except OSError as exc:
                self.problem = f"the ledger {path} cannot be written ({exc})"
                # What this transaction's spawn made is in no ledger on disk and no shell will use
                # it: removed now, still under the lock, if it is still the object made (L3 r3).
                for e in sorted(self.entries, key=lambda e: len(e["path"]), reverse=True):
                    if e["path"] in self.rollback and _object_unchanged(e):
                        try:
                            os.rmdir(e["path"]) if e.get("kind") == "dir" else os.unlink(e["path"])
                        except OSError:
                            pass
        finally:
            if self._lock is not None:
                self._lock.close()   # releases the flock


def _ledger_enter(created: list[tuple[str, str]], mounted: set[str], claim: str) -> None:
    """Record ``created`` and claim every entry at a path in ``mounted`` (one transaction)."""
    with _LedgerTxn() as txn:
        if txn.ok:
            txn.record(created)
            txn.claim(mounted, claim)


def _ledger_release(claim: str) -> list[str]:
    """Drop ``claim`` and remove what no session holds any more. Called only once the claiming
    shell's namespace is gone (:meth:`_BwrapShell.close`)."""
    with _LedgerTxn() as txn:
        if txn.ok:
            txn.drop(claim)
    return txn.removed


def sweep_floor_placeholders() -> list[str]:
    """Drop the claims of processes that no longer exist and remove the objects nobody holds. Run
    inside every Linux spawn's own transaction, at launch (:mod:`levain.launch`) and by
    ``levain doctor``. Returns the paths removed. A crashed levain's cgroup leaves are killed first, so
    the claims that name them are judged on leaves that are emptying (L3 r9)."""
    if platform.system() == "Linux":
        sweep_dead_leaves()
    if not (_ledger_dir() / _LEDGER_NAME).exists():
        return []
    with _LedgerTxn() as txn:
        if txn.ok:
            txn.sweep()
    return txn.removed


def ledger_problem() -> str | None:
    """Why the placeholder ledger cannot be used, or None (for ``levain doctor``: a ledger levain
    cannot read or trust leaves every placeholder on disk, so it must be said, not swallowed)."""
    d = _ledger_dir()
    if not os.path.lexists(d) and not os.path.lexists(d.parent):
        return None   # never used on this host
    with _LedgerTxn() as txn:
        pass
    return txn.problem


def live_floor_placeholders() -> list[Path]:
    """The placeholder FILES a live session holds on this host right now (for the banner)."""
    try:
        entries = json.loads((_ledger_dir() / _LEDGER_NAME).read_text(encoding="utf-8"))["entries"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    return [Path(e["path"]) for e in entries
            if isinstance(e, dict) and e.get("kind") == "file"
            and any(isinstance(c, str) and _claim_alive(c) for c in e.get("claims", []))]


_LIVE_BWRAP_SHELLS: "weakref.WeakSet[_BwrapShell]" = weakref.WeakSet()


@atexit.register
def _close_live_shells() -> None:
    """At levain's exit, close every Linux shell still open, so each releases its placeholders the
    normal way, after its namespace is gone. Releasing a claim while its shell lived would let the
    release unlink a placeholder still mounted in that shell, which detaches the mount."""
    for shell in list(_LIVE_BWRAP_SHELLS):
        try:
            shell.close()
        except Exception:  # noqa: BLE001 — exit must go on
            pass


def _group_live(pgid: int) -> bool:
    """Whether process group ``pgid`` has a member that is not a zombie. The shell keeps each group's
    leader unreaped while its group lives (:class:`_Leader`), so a zombie must not count.

    macOS only: elsewhere a group that ``killpg`` still finds is called live, since Linux has no one-call
    process table to tell its members from a zombie leader (see :meth:`SandboxedShell._group_emptied`)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass   # a zombie (macOS answers EPERM for one, measured), or a member of another uid
    else:
        if platform.system() == "Darwin":
            return True   # macOS signals no zombie, so a success is a live member (measured)
    if platform.system() != "Darwin":
        return True
    # macOS (and other BSDs): pgrep lists no zombie (measured), and lists every user's processes.
    from levain.launch import child_env

    try:
        r = subprocess.run(["/usr/bin/pgrep", "-g", str(int(pgid))], capture_output=True,
                           stdin=subprocess.DEVNULL, cwd="/", env=child_env(), timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return True   # cannot tell: not gone
    return r.returncode != 1


def _group_gone(pgid: int, *, timeout: float) -> bool:
    """True once no live process is left in process group ``pgid`` (polled until ``timeout``)."""
    deadline = time.monotonic() + timeout
    while True:
        if not _group_live(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


_NOTE_EXITSTATUS = 0x04000000   # <sys/event.h>: NOTE_EXIT's data carries the wait status


class _Leader:
    """A command's driver process, the leader of its process group, watched WITHOUT being reaped.

    A pid is not reused until its process is reaped, and a process group's id is its leader's pid
    (POSIX.1-2024 Base Definitions 4.17), so while the leader stays an unreaped zombie no new process
    group can take its number and a signal to it can reach only this command's processes. The shell
    reaps the leader only once its group has no live member (S2 L3 r2, codex HIGH: a background job's
    group was kept by number after its leader was reaped, and could be recycled under a later kill).

    The exit status is read without reaping: ``waitid(WNOWAIT)`` where Python has it (Linux; CPython
    leaves it out on macOS), else a kqueue ``NOTE_EXIT | NOTE_EXITSTATUS`` registered right after the
    spawn. A leader that exited before that registration (its driver failed before the command's input
    was sent) has its status read when it is reaped."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self.proc = proc
        self.pid = proc.pid
        self.status: int | None = None   # Popen.returncode-style, once seen exited
        self.exited = False
        self.reaped = False
        self._reap_lock = threading.Lock()
        # Signals being sent to the group right now (outside the lock: a hands signal is sudo). The
        # leader is not reaped while any is out, so the number they name stays this group's.
        self._signalling = 0
        self._signals_done = threading.Condition(self._reap_lock)
        self._kq: Any = None
        self._pidfd: int | None = None
        if hasattr(os, "waitid"):
            try:
                self._pidfd = os.pidfd_open(self.pid)   # type: ignore[attr-defined]
            except (AttributeError, OSError):
                self._pidfd = None
            return
        kq = select.kqueue()
        ev = select.kevent(self.pid, filter=select.KQ_FILTER_PROC, flags=select.KQ_EV_ADD,
                           fflags=select.KQ_NOTE_EXIT | _NOTE_EXITSTATUS)
        try:
            got = kq.control([ev], 1, 0)
        except OSError as exc:
            kq.close()
            if exc.errno != errno.ESRCH:
                raise
            self.exited = True   # ESRCH: it exited before the watch was set
            return
        errs = [e.data for e in got if e.flags & select.KQ_EV_ERROR]
        if errs:
            kq.close()
            if any(d != errno.ESRCH for d in errs):
                # Only ESRCH means "already exited"; anything else is a watch that could not be set,
                # and a running command must not be read as a finished one (S2 L3 r3).
                raise OSError(errs[0], f"cannot watch the shell's process: {os.strerror(errs[0])}")
            self.exited = True
            return
        self._kq = kq
        self._take(got)

    def _take(self, events: list[Any]) -> None:
        for e in events:
            if e.filter == select.KQ_FILTER_PROC and e.fflags & select.KQ_NOTE_EXIT:
                # The status first: a waiter on another thread that sees `exited` reads it (r3).
                self.status = os.waitstatus_to_exitcode(e.data)
                self.exited = True

    def wait(self, timeout: float) -> bool:
        """True once the leader has exited (it stays unreaped), polled up to ``timeout`` seconds.

        Safe from two threads at once (run() and a close() from another thread): the kqueue reports
        the exit to one of them, so each blocks in short slices and rechecks what the other saw, and
        a watch the other thread released by reaping the leader ends the wait instead of raising."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self.exited:
                return True
            left = max(0.0, deadline - time.monotonic())
            step = min(left, 0.05)
            kq, pidfd = self._kq, self._pidfd
            try:
                if kq is not None:
                    self._take(kq.control(None, 1, step))
                elif hasattr(os, "waitid"):
                    r = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)  # type: ignore[attr-defined]
                    if r is not None and r.si_pid == self.pid:
                        self.status = (r.si_status if r.si_code == os.CLD_EXITED  # type: ignore[attr-defined]
                                       else -r.si_status)
                        self.exited = True
                    elif step > 0:
                        if pidfd is not None:
                            select.select([pidfd], [], [], step)
                        else:
                            time.sleep(step)
                elif step > 0:
                    time.sleep(step)   # the watch is gone: another thread reaped the leader
            except (ChildProcessError, OSError, ValueError):
                # Another thread's reap() may be between the kernel's reap and its flags: it holds
                # the reap lock across both, so taking it once settles which this was.
                with self._reap_lock:
                    pass
                if not (self.reaped or self.exited):
                    raise
            if self.exited:
                return True
            if left <= 0:
                return False

    def hold_for_signal(self) -> bool:
        """Keep the leader unreaped while the caller signals its group, outside the reap lock; False
        when it is already reaped (its number may be another group's now: send nothing). Every True
        is followed by :meth:`signal_sent`."""
        with self._reap_lock:
            if self.reaped:
                return False
            self._signalling += 1
            return True

    def signal_sent(self) -> None:
        with self._reap_lock:
            self._signalling -= 1
            self._signals_done.notify_all()

    def reap(self, timeout: float | None = None) -> bool:
        """Reap the leader (only once its group has no live member) and free the watch. Waits for any
        signal still being sent to the group (S2 L3 r4), up to ``timeout``: False, still unreaped,
        when one is still out then (r5)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._reap_lock:
            while self._signalling:
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    return False
                self._signals_done.wait(left)
            if self.reaped:
                return True
            self.proc.wait()
            if self.status is None:
                self.status = self.proc.returncode
            self.exited = True
            self.reaped = True
            self._release()
            return True

    def release_watch(self) -> None:
        """Free the watch of a leader seen exited and kept unreaped (its group still held): its exit is
        known, so the watch is not needed again, and a held leader would otherwise keep an fd."""
        with self._reap_lock:
            if self.exited:
                self._release()

    def _release(self) -> None:
        if self._kq is not None:
            self._kq.close()
            self._kq = None
        if self._pidfd is not None:
            try:
                os.close(self._pidfd)
            except OSError:
                pass
            self._pidfd = None


# --- the Linux hands launch (S2-linux) ------------------------------------------------------------
# bwrap runs AS the hands user, in an ALLOWLISTED view (Phill's ruling (A), 2026-10-09): the sandbox
# root is bwrap's own empty tmpfs, and only the trees bash needs are mounted into it. Read-only: /usr
# (and the merged-/usr links /bin, /sbin, /lib*), /etc and /opt. Writable: the hands workspace and the
# hands home. Fresh and empty: /tmp, /var/tmp, /run, and bwrap's own /dev and /proc. The host's root is
# never bound, so the operator's home, the entity directory, /srv, /mnt, /media, /var/lib, daemon
# sockets under /run and container volumes are not in the view at all: no list of what to hide and no
# matching of socket names. Of the floor's plan, only ops whose target lies in a host tree of the view
# are kept (a mask over a jewel in /etc, say); everything else it hides is already absent. The network
# namespace is always new (abstract unix sockets, the host's loopback), the IPC namespace is new, and
# the user namespace is new with ``--disable-userns``, so no nested one can be made inside (ENOSPC).
# Linux hands is therefore stricter than macOS hands, which stays default-allow under Seatbelt.

#: Host trees in a hands view, read-only (absent ones are skipped).
_HANDS_VIEW_RO = ("/usr", "/etc", "/opt")
#: The merged-/usr links: a symlink here becomes the same symlink in the view, a real directory (an
#: unmerged host) is bound read-only.
_HANDS_VIEW_LINKS = ("/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
#: Fresh, empty and writable in a hands view.
_HANDS_VIEW_TMP = ("/tmp", "/var/tmp", "/run")
#: Ops of the floor's plan and the number of arguments each takes. An op not named here is refused by
#: the transform, so a new op in the plan cannot pass through it unexamined.
_BWRAP_OP_ARITY = {
    "--bind": 2, "--ro-bind": 2, "--bind-try": 2, "--ro-bind-try": 2, "--symlink": 2,
    "--tmpfs": 1, "--remount-ro": 1, "--dir": 1, "--proc": 1, "--dev": 1,
    "--die-with-parent": 0, "--unshare-pid": 0, "--unshare-cgroup": 0, "--unshare-net": 0,
}


_HANDS_VIEW_FLAGS = ("--unshare-user", "--disable-userns", "--unshare-ipc", "--unshare-net", "--unshare-pid",
                     "--unshare-cgroup", "--die-with-parent")


def _hands_view_trees(hands: HandsIdentity) -> tuple[list[str], list[str]]:
    """``(read_only, writable)``: the host trees a hands view binds, read-only ones that exist here and
    the hands workspace and home."""
    ro = [t for t in _HANDS_VIEW_RO if os.path.isdir(t) and not os.path.islink(t)]
    return ro, [str(hands.workspace), os.path.realpath(hands.home)]


def _in_hands_view(path: str | Path, hands: HandsIdentity) -> bool:
    """Whether ``path`` lies in a host tree of the hands view, so bash run as the hands user can see it."""
    ro, rw = _hands_view_trees(hands)
    p = Path(path)
    return any(p == Path(t) or p.is_relative_to(t) for t in (*ro, *rw))


def _hands_bwrap_argv(argv: list[str], hands: HandsIdentity) -> list[str]:
    """The floor's bwrap ``argv`` (``_bwrap_plan``'s, without the command) turned into a hands launch's
    allowlisted view: the view's own mounts first, then every op of the floor's plan whose target lies
    in a host tree of the view, in the plan's order; every other op is dropped. Reads only the host's
    link layout and which view trees exist."""
    if not argv or argv[0] != BWRAP:
        raise ConfinementError("internal: the hands launch was given a plan that is not bwrap's — refusing "
                               "(fail-closed).")
    ro, rw = _hands_view_trees(hands)
    trees = [Path(t) for t in (*ro, *rw)]
    out = [argv[0], *_HANDS_VIEW_FLAGS]
    for t in ro:
        out += ["--ro-bind", t, t]
    for link in _HANDS_VIEW_LINKS:
        if os.path.islink(link):
            out += ["--symlink", os.readlink(link), link]
        elif os.path.isdir(link):
            out += ["--ro-bind", link, link]
    out += ["--proc", "/proc", "--dev", "/dev"]
    for t in _HANDS_VIEW_TMP:
        out += ["--tmpfs", t]
    for t in rw:
        out += ["--bind", t, t]
    i = 1
    while i < len(argv):
        op = argv[i]
        n = _BWRAP_OP_ARITY.get(op)
        if n is None or i + n >= len(argv):
            raise ConfinementError(f"internal: the floor's plan has {op!r}, which the hands launch does not "
                                   "know how to place — refusing (fail-closed).")
        args = argv[i + 1:i + 1 + n]
        i += 1 + n
        if n and any(Path(args[-1]) == t or Path(args[-1]).is_relative_to(t) for t in trees):
            out += [op, *args]
    return out


def _hands_ns_problem(hands: HandsIdentity, timeout: float = 30.0) -> str | None:
    """Can bwrap build a hands launch's namespaces AS the hands user here? None when it can, else what
    it said. Ubuntu 23.10+ with ``kernel.apparmor_restrict_unprivileged_userns=1`` refuses it until the
    bwrap-userns-restrict profile is installed (RUN 2026-10-09)."""
    from levain.launch import child_env

    argv = [*hands_prefix(hands), BWRAP, "--unshare-user", "--disable-userns", "--unshare-ipc", "--unshare-net",
            "--unshare-pid", "--die-with-parent", "--bind", "/", "/", "--proc", "/proc", "--dev", "/dev",
            "/bin/true"]
    try:
        r = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL, cwd="/", env=child_env(),
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"bwrap could not be run as {hands.user} ({exc})"
    if r.returncode == 0:
        return None
    said = r.stderr.decode("utf-8", "replace").strip().splitlines()
    return (f"bwrap cannot build the sandbox's namespaces as {hands.user} ({said[-1] if said else 'no reason given'}). "
            "On Ubuntu 23.10+ install the bwrap-userns-restrict AppArmor profile: sudo install -m 0644 "
            "/usr/share/apparmor/extra-profiles/bwrap-userns-restrict /etc/apparmor.d/ && sudo apparmor_parser "
            "-r /etc/apparmor.d/bwrap-userns-restrict (from the apparmor-profiles package); bubblewrap 0.8 or "
            "later is needed for --disable-userns")


def _hands_argv_unmade(argv: list[str]) -> str | None:
    """The first mount target of a hands bwrap ``argv`` that is not on the host, or None. bwrap would
    create such a target itself, on the host, unless it lies in a tmpfs mounted earlier in the argv
    (then it is made in that view only); a ``-try`` bind with no source is skipped by bwrap."""
    views: list[Path] = []
    i = 1
    while i < len(argv):
        op = argv[i]
        n = _BWRAP_OP_ARITY.get(op, 0)
        args = argv[i + 1:i + 1 + n]
        i += 1 + n
        if not n or op in ("--symlink", "--dir"):
            continue
        dst = Path(args[-1])
        if op in ("--bind-try", "--ro-bind-try") and not os.path.lexists(args[0]):
            continue
        if not any(dst.is_relative_to(v) and dst != v for v in views) and not os.path.lexists(dst):
            return str(dst)
        if op == "--tmpfs":
            views.append(dst)
    return None


# --- the proxy relays (criterion 1) ---------------------------------------------------------------
# A hands launch's network namespace has no route to the host's loopback, where the entity's proxy
# listens. Two relays carry exactly the recorded egress ports across, and add no authority: the host
# side runs AS the hands user in the host's network namespace, so its TCP connects to 127.0.0.1:<port>
# are the hands user's and the nftables table still decides which ports get out; the namespace side
# runs inside each command's sandbox. They meet on pathname unix sockets in a fresh 0700 directory
# under the hands user's own home, which the hands view binds read-write. ONE fixed program for both, run with
# ``python3 -I -S`` (stdlib only, no site, no environment): every input is argv data, nothing is
# evaluated, and it connects to nothing but 127.0.0.1:<port> or <dir>/<port>.sock.
#   out <dir> <port>...   bind <dir>/<port>.sock (0600) for each port, relay each connection to
#                         127.0.0.1:<port>; print "ready" once every socket listens; on SIGTERM remove
#                         the sockets and the directory (levain, another user, cannot)
#   in <dir> <port>... -- <argv>
#                         listen on 127.0.0.1:<port> for each port, fork the relay to <dir>/<port>.sock,
#                         then exec <argv> (the command's bash) in its own place: bash stays pid 1 of
#                         the command's pid namespace and the relay, its child, dies with it. Every
#                         socket listens before bash exists, so a connection is queued, never refused,
#                         and the relay is in no job table of bash (``wait`` does not wait for it).
HANDS_PYTHON = "/usr/bin/python3"
_HANDS_RELAY_PREFIX = ".levain-relay-"
_HANDS_RELAY_READY_TIMEOUT = 10.0
_HANDS_RELAY = r"""import os, signal, socket, sys, threading, time
def pump(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        return
    try:
        b.shutdown(socket.SHUT_WR)
    except OSError:
        pass
def pair(c, connect):
    try:
        t = connect()
    except OSError:
        c.close()
        return
    th = threading.Thread(target=pump, args=(c, t), daemon=True)
    th.start()
    pump(t, c)
    th.join()
    c.close()
    t.close()
def serve(ls, connect):
    while True:
        try:
            c = ls.accept()[0]
        except OSError:
            time.sleep(0.05)
            continue
        threading.Thread(target=pair, args=(c, connect), daemon=True).start()
def tcp(port):
    return lambda: socket.create_connection(("127.0.0.1", port))
def unix(path):
    def connect():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(path)
        except OSError:
            s.close()
            raise
        return s
    return connect
def ports(args):
    for a in args:
        if not (a.isascii() and a.isdigit() and 0 < int(a) < 65536):
            raise ValueError("not a port: %r" % a)
    return [int(a) for a in args]
def run(listeners):
    for s, connect in listeners:
        threading.Thread(target=serve, args=(s, connect), daemon=True).start()
    while True:
        signal.pause()
mode, d = sys.argv[1], sys.argv[2]
if mode == "out":
    made = []
    mine = []
    def tidy():
        for p in made:
            try:
                os.unlink(p)
            except OSError:
                pass
        if mine:
            try:
                os.rmdir(d)
            except OSError:
                pass
    def stop(*_):
        tidy()
        os._exit(0)
    signal.signal(signal.SIGTERM, stop)
    os.umask(0o077)
    try:
        listeners = []
        want = ports(sys.argv[3:])
        os.mkdir(d, 0o700)
        mine.append(d)
        for port in want:
            p = os.path.join(d, "%d.sock" % port)
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.bind(p)
            made.append(p)
            os.chmod(p, 0o600)
            s.listen(64)
            listeners.append((s, tcp(port)))
    except Exception as e:
        sys.stderr.write("levain relay: %s\n" % e)
        sys.stderr.flush()
        tidy()
        os._exit(1)
    sys.stdout.write("ready\n")
    sys.stdout.flush()
    n = os.open(os.devnull, os.O_RDWR)
    os.dup2(n, 1)
    os.dup2(n, 2)
    run(listeners)
elif mode == "in":
    i = sys.argv.index("--")
    try:
        listeners = []
        for port in ports(sys.argv[3:i]):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            s.listen(64)
            listeners.append((s, unix(os.path.join(d, "%d.sock" % port))))
    except Exception as e:
        sys.stderr.write("levain relay: cannot listen inside the sandbox: %s\n" % e)
        sys.stderr.flush()
        os._exit(126)
    if os.fork() == 0:
        n = os.open(os.devnull, os.O_RDWR)
        for f in (0, 1, 2):
            os.dup2(n, f)
        run(listeners)
    for s, _ in listeners:
        s.close()
    os.execv(sys.argv[i + 1], sys.argv[i + 1:])
else:
    sys.stderr.write("levain relay: unknown mode %r\n" % mode)
    os._exit(2)
"""


def _hands_relay_dir(hands: HandsIdentity) -> str:
    """A fresh directory name for one shell's relay sockets, under the hands user's home."""
    return os.path.join(os.path.realpath(hands.home), _HANDS_RELAY_PREFIX + os.urandom(8).hex())


def _hands_relay_in(hands: HandsIdentity, sockdir: str) -> list[str]:
    """What goes in front of each command's bash in a hands launch with egress ports."""
    return [HANDS_PYTHON, "-I", "-S", "-c", _HANDS_RELAY, "in", sockdir,
            *(str(p) for p in hands.egress_ports), "--"]


class _HandsRelay:
    """The host side of one hands shell's relays: a process group led by sudo, the relay in it running
    as the hands user. :meth:`stop` is idempotent."""

    def __init__(self, hands: HandsIdentity, proc: subprocess.Popen[bytes], sockdir: str) -> None:
        self.hands, self.proc, self.sockdir = hands, proc, sockdir
        self._gone: bool | None = None

    def stop(self) -> bool:
        """SIGTERM as the hands user first, so the relay removes its sockets and directory; then the
        whole group killed and sudo reaped. True once nothing of it is left."""
        if self._gone is not None:
            return self._gone
        _hands_signal(self.hands, self.proc.pid, signal.SIGTERM)
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        self._gone = _stop_hands_group(self.hands, self.proc)
        return self._gone


def _start_hands_relay(policy: CrownJewelsPolicy, hands: HandsIdentity, sockdir: str) -> _HandsRelay:
    """Start the host side of the relays and wait (bounded) for its ready line; refuse by name when it
    does not come. Nothing of a refused start is left running."""
    from levain.launch import child_env

    argv = [*hands_prefix(hands), HANDS_PYTHON, "-I", "-S", "-c", _HANDS_RELAY, "out", sockdir,
            *(str(p) for p in hands.egress_ports)]
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd="/", env=child_env(), start_new_session=True)
    relay = _HandsRelay(hands, proc, sockdir)
    try:
        got = b""
        deadline = time.monotonic() + _HANDS_RELAY_READY_TIMEOUT
        fd = proc.stdout.fileno()   # type: ignore[union-attr]
        while b"\n" not in got:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                break
            chunk = os.read(fd, 64)
            if not chunk:
                break
            got += chunk
        if got == b"ready\n":
            for f in (proc.stdout, proc.stderr):
                f.close()   # type: ignore[union-attr]
            proc.stdout = proc.stderr = None
            return relay
        said = ""
        if proc.poll() is not None:
            said = proc.stderr.read().decode("utf-8", "replace").strip()   # type: ignore[union-attr]
        raise ConfinementError(
            f"the proxy relay for the entity's egress ports {list(hands.egress_ports)}, run as {hands.user}, "
            f"did not start ({said.splitlines()[-1] if said else f'no ready line within {_HANDS_RELAY_READY_TIMEOUT:g} s'})"
            " — refusing to run bash as the entity's own user, or as you instead (fail-closed)."
        )
    except BaseException:
        relay.stop()
        raise


# --- pathname listeners (criterion 2) -------------------------------------------------------------
# A unix socket or FIFO the hands user may write is a deputy the network namespace does not cut. The
# view holds no host tree but /usr, /etc and /opt read-only (and the hands user's own), so those are
# what is walked.
#: The walk, as the hands user inside its own view: every socket and FIFO it may write in the view's
#: read-only host trees. bash cannot reach any other host file, and the view's writable trees are the
#: hands user's own.
_HANDS_FIND = "/usr/bin/find"
_HANDS_WALK_TIMEOUT = 120.0


def _hands_listener_problem(hands: HandsIdentity) -> str | None:
    """A unix socket or FIFO in the read-only host trees of a hands view that the hands user may write,
    named with the remedy; None when there is none. Asked of the kernel as the hands user inside the
    view with none of the floor's masks (they only hide; so this sees at least what bash will), by
    ``find -writable``, which is access(2): modes, ACLs and every ancestor's search bit."""
    from levain.launch import child_env

    ro, _ = _hands_view_trees(hands)
    if not ro:
        return None
    argv = [*hands_prefix(hands), *_hands_bwrap_argv([BWRAP], hands), _HANDS_FIND, *ro, "(", "-type", "s", "-o", "-type", "p", ")",
            "-writable", "-print0"]
    try:
        r = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL, cwd="/", env=child_env(),
                           timeout=_HANDS_WALK_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"levain could not walk, as {hands.user}, the host files its bash would see ({exc})"
    found = [x.decode("utf-8", "surrogateescape") for x in r.stdout.split(b"\0") if x]
    said = [x for x in r.stderr.decode("utf-8", "replace").splitlines() if x.strip()]
    # find exits 1 when a directory cannot be listed and walks the rest. Only that is let through: a
    # directory the hands user cannot list is one it cannot look up names in by listing, and anything
    # else (bwrap or find failing to start) refuses.
    unlisted = [x for x in said if x.startswith(f"{_HANDS_FIND}: ") and x.endswith(": Permission denied")]
    if r.returncode != 0 and (not said or len(unlisted) != len(said)):
        return (f"levain could not walk, as {hands.user}, the host files its bash would see "
                f"({said[-1] if said else f'status {r.returncode}'})")
    if not found:
        return None
    more = f" and {len(found) - 3} more" if len(found) > 3 else ""
    return (f"{hands.user} may write to the socket or FIFO{'s' if len(found) > 1 else ''} "
            f"{', '.join(found[:3])}{more}, which bash run as it sees: tighten its mode so the entity's "
            "user cannot write it")


def _hands_launch_problem(hands: HandsIdentity) -> str | None:
    """What stops a Linux hands launch, checked before any of its processes starts (Phill's ruling (A):
    probe at every hands launch). None when nothing does."""
    from levain.firing.hands import HandsSetupError, egress_boundary_problem, hands_net_group, net_group_problem

    try:
        net_group = hands_net_group(hands.user)
    except HandsSetupError as exc:
        return str(exc)
    problem = net_group_problem(hands.user)
    if problem is not None:
        return f"the entity's network boundary is open: {problem}"
    problem = egress_boundary_problem(hands.user, hands.egress_ports, net_group=net_group)
    if problem is not None:
        return f"the entity's network boundary does not hold: {problem}"
    return _hands_ns_problem(hands)


class _BwrapShell(SandboxedShell):
    """A :class:`SandboxedShell` that checks, before every command, that the disk still matches the
    mounts it was started with (spore-1312, rec C, ruled by Phill 2026-10-03, widened by its L3).

    Two checks, one rule each:
      - the spawn-time SQLite jewel check (:func:`_refuse_plantable_sqlite_jewels`) again, because an
        empty jewel can be initialised as a database IN PLACE, keeping its inode (RUN on Linux at
        3838801: the next command read a row out of the host's ``-wal``);
      - every path in the mount manifest still has the identity it had when
        the mounts were made, which covers every way the host can put something new where a mount
        was: an atomic rewrite, an unlink while a connection keeps a ``-wal``, a swap for a link, a
        sidecar that was absent (so unmounted) and appeared. Each of those was RUN or reviewed on
        2026-10-03 before this replaced the per-case checks.
    If either fires, the shell is closed (its process group killed) and the command refused; the next
    spawn mounts what is there now, or refuses at the spawn check. A filesystem error while checking
    refuses the same way. A host-side rewrite of a protected file therefore closes the live shell,
    including a ``levain wrap`` rewriting the entity's own continuity, or a daemon recreating its socket:
    that rewrite detached the mount, so the shell could no longer be trusted with it.
    The manifest is recorded once, before bwrap runs, after the provider has created every absent
    host mountpoint itself (:func:`_prepare_mountpoints`), so nothing is adopted after the start. It
    covers every mounted path and every jewel the plan leaves unmounted (:func:`_mount_plan_paths`).
    NOT covered: a command already running when something changes keeps its access for as long as it
    runs; so does a command whose path changes between this check and its start; a store with no
    recognisable header (SQLCipher) is never classified as a database. Each command is its own bwrap
    whose bash is pid 1 of its pid namespace, so nothing a command starts (a background job, a
    ``setsid`` child) outlives it to hold access past the next check."""

    def __init__(
        self,
        *,
        policy: CrownJewelsPolicy,
        manifest: dict[str, tuple[int, int, int] | None],
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        default_timeout: float = 120.0,
    ) -> None:
        super().__init__(argv=argv, cwd=cwd, env=env, default_timeout=default_timeout)
        self._jewel_policy = policy
        self._manifest = dict(manifest)   # recorded before the start; never updated
        self._ledger_claim: str | None = None   # this shell's claim on the placeholder ledger
        self._claim_base: str | None = None     # the claim before a command's bash is added to it
        # Each command's cgroup leaf by its process group (see `_leaf_rel`), bound to the leader OBJECT
        # so a later group that reuses the number is never judged by an earlier one's leaf; deleted
        # when that leader is reaped. The leaf, not a pid, answers "may anything of it still run".
        self._leaves: dict[int, tuple[_Leader, str]] = {}
        self._pending_leaf: str | None = None   # named for the spawn in progress, before its leader exists
        # EVERY leaf named for a spawn, from the moment it is named until it is confirmed empty with its
        # driver exited: leaf -> its driver, None while none exists yet. Nothing in between is untracked,
        # so the claim is never released, nor retagged away, while any of them may hold a sandbox.
        self._named: dict[str, _Leader | None] = {}
        self._unit_token = os.urandom(6).hex()
        self._units = 0
        self._relay: _HandsRelay | None = None   # a hands launch's host-side proxy relay, if any

    # Each command runs in a pid namespace of its own (``--unshare-pid --as-pid-1``), inside its leaf.
    _own_pid_namespace = True

    def _group_emptied(self, pgid: int, leader: _Leader, timeout: float) -> bool:
        """Empty once the command's driver (the leader) has exited and its leaf has no live process.
        Only THIS leader's leaf counts."""
        deadline = time.monotonic() + timeout
        if not leader.wait(timeout):
            return False
        rec = self._leaves.get(pgid)
        if rec is None or rec[0] is not leader:
            return False
        return _leaf_gone(rec[1], timeout=max(0.0, deadline - time.monotonic()))

    def _signal(self, pgid: int, sig: int) -> bool:   # type: ignore[override]
        """Every signal levain sends a Linux command stops it: SIGKILL to the driver (levain's own
        unreaped child, so the number is still it), then ``cgroup.kill`` on its leaf. The driver first:
        until ``systemd-run`` has moved it into the leaf it is outside it, and dead it cannot exec the
        sandbox afterwards. True when the leaf kill was written or the leaf is absent."""
        rec = self._leaves.get(pgid)
        leaf = rec[1] if rec is not None else self._pending_leaf
        try:
            os.kill(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            return False
        return leaf is None or _leaf_kill(leaf)

    def _kill_group(self, pgid: int, leader: _Leader) -> bool:
        """Kill the command (driver and leaf), wait for the leaf to empty, then reap the driver. False,
        the group kept, when the kill was not written or the leaf did not empty in time."""
        delivered = self._signal_group(pgid, leader, signal.SIGKILL)
        leader.wait(5.0)
        return (delivered and leader.exited and self._group_emptied(pgid, leader, 5.0)
                and self._reap(pgid, leader))

    def _reap(self, pgid: int, leader: _Leader) -> bool:
        if not super()._reap(pgid, leader):
            return False
        with self._lock:
            rec = self._leaves.get(pgid)
            if rec is not None and rec[0] is leader:
                del self._leaves[pgid]
                self._named.pop(rec[1], None)   # reaped only once its leaf was confirmed empty
        return True

    def _settled(self) -> bool:
        """Every named leaf is empty and its driver has exited (a leaf with no driver yet is not)."""
        with self._lock:
            named = list(self._named.items())
        for leaf, leader in named:
            if leader is None or not leader.wait(0) or not _leaf_gone(leaf, timeout=5.0):
                return False
            with self._lock:
                if self._named.get(leaf) is leader:
                    del self._named[leaf]
        return True

    def _spawn(self) -> tuple[_Leader, _Output, _Carry]:
        try:
            return super()._spawn()
        except BaseException:
            # A spawn that failed before its driver was recorded (Popen raised, or the watch could not
            # be set up and the driver was killed): kill whatever reached its leaf, and stop tracking
            # it only once it is empty.
            with self._lock:
                leaf, self._pending_leaf = self._pending_leaf, None
            if leaf is not None and self._named.get(leaf, False) is None:
                _leaf_kill(leaf)
                if _leaf_gone(leaf, timeout=5.0):
                    with self._lock:
                        if self._named.get(leaf, False) is None:
                            del self._named[leaf]
            raise

    def _leader_made(self, leader: _Leader) -> None:
        with self._lock:
            leaf, self._pending_leaf = self._pending_leaf, None
            if leaf is not None:
                self._leaves[leader.pid] = (leader, leaf)
                self._named[leaf] = leader

    def close(self) -> None:
        # The claim is released only once every leaf is EMPTY, never while any process of a sandbox
        # lives: a host-side unlink of a placeholder still mounted in a live namespace detaches that
        # mount (Linux 3.18+, 8ed936b), and the shell could then plant the file (L1 + L2 r1). A leaf
        # that does not empty keeps the claim; once this levain is gone, the next Linux spawn's
        # `sweep_dead_leaves` kills the leaf and the ledger sweep then drops the claim. Released in
        # `finally`, whatever the base teardown did.
        try:
            super().close()
        finally:
            if self._relay is not None:
                self._relay.stop()   # after the commands: nothing inside is left to use it
            claim, self._ledger_claim = self._ledger_claim, None
            _LIVE_BWRAP_SHELLS.discard(self)
            if claim is not None and not self.unemptied_groups and self._settled():
                _ledger_release(claim)

    def _spawn_argv(self) -> tuple[list[str], tuple[int, ...]]:
        # One command at a time on the claim: it names one leaf, so an earlier one still populated
        # refuses this command (a retag would stop the claim covering it).
        if not self._settled():
            raise ConfinementError(
                "an earlier command's sandbox could not be stopped — refusing to run the command "
                "(fail-closed): the shell's claim must keep covering it, so close this shell."
            )
        problem = _cgroup_problem()
        if problem is not None:
            raise ConfinementError(f"{problem[0]} — refusing to run the command (fail-closed)."
                                   + (f" To fix: {problem[1]}." if problem[1] else ""))
        argv, pass_fds = super()._spawn_argv()
        unit = _leaf_unit(self._unit_token, self._units + 1)
        if unit is None:
            # The unit name carries it, so a crash sweep can tell this levain's leaves from a live one's.
            raise ConfinementError("levain cannot read its own start time from /proc, or is not in the "
                                   "host's pid namespace — refusing to run the command (fail-closed): a "
                                   "crash could not be told from a live levain.")
        self._units += 1
        leaf = _leaf_rel(os.getuid(), unit)
        # The claim names the leaf BEFORE anything is spawned in it: after a levain crash at any later
        # point the sweep finds the leaf, kills it, and keeps the claim until it is empty.
        claim = self._ledger_claim
        if claim is not None and self._claim_base is not None:
            tagged = f"{self._claim_base}:c{leaf}"
            with _LedgerTxn() as txn:
                if txn.ok:
                    txn.retag(claim, tagged)
            if not txn.ok or txn.problem is not None:
                raise ConfinementError(
                    f"{txn.problem} — refusing to run the command (fail-closed): the shell's claim on "
                    "the floor's files could not be recorded."
                )
            self._ledger_claim = tagged
        with self._lock:
            self._pending_leaf = leaf
            self._named[leaf] = None
        # systemd-run finds the user manager through XDG_RUNTIME_DIR; bash does not get it unless the
        # caller gave it, so a second env drops it again after the move.
        scope = [SYSTEMD_RUN, "--user", "--scope", "--quiet", "--collect", f"--slice={_LEVAIN_SLICE}",
                 f"--unit={unit}", "--"]
        if "XDG_RUNTIME_DIR" not in self._env:
            scope = ["/usr/bin/env", f"XDG_RUNTIME_DIR=/run/user/{os.getuid()}", *scope,
                     "/usr/bin/env", "-u", "XDG_RUNTIME_DIR"]
        return scope + argv, pass_fds

    def _after_spawn(self, pgid: int) -> None:
        # bash is blocked reading its input, so nothing of the command has run yet. Before it may run,
        # its driver must be IN its leaf: systemd-run moves itself there and then execs bwrap, so
        # everything the command ever starts is a descendant of a member.
        with self._lock:
            rec = self._leaves.get(pgid)
        if rec is None:
            raise ConfinementError(
                "the command's process group is no longer tracked by this shell — refusing to run the "
                "command (fail-closed)."
            )
        leader, leaf = rec
        want = f"0::/{leaf}"
        unit = leaf.rsplit("/", 1)[-1]
        deadline = time.monotonic() + _START_TIMEOUT
        while True:
            try:
                now = Path(f"/proc/{pgid}/cgroup").read_text().strip()
            except OSError:
                now = ""
            if now == want:
                return
            if now.startswith("0::/") and now.endswith(f"/{unit}"):
                # Its own scope (the unit name is unique) under a layout `_leaf_rel` did not predict:
                # kill and wait on the path it is really in, then refuse.
                seen = now[len("0::/"):]
                with self._lock:
                    self._leaves[pgid] = (leader, seen)
                    self._named.pop(leaf, None)
                    self._named[seen] = leader
                raise ConfinementError(
                    f"the command's cgroup is {seen}, not {leaf} as levain expects for this systemd — "
                    "refusing to run the command (fail-closed)."
                )
            if leader.wait(0) or time.monotonic() >= deadline:
                raise ConfinementError(
                    "the command's sandbox did not start in its cgroup (systemd-run --user failed or "
                    "stalled; `levain doctor` checks it) — refusing to run the command (fail-closed)."
                )
            time.sleep(0.01)

    def _after_command(self, pgid: int) -> None:
        # The command's bash was pid 1 of its namespace, so the kernel is killing everything left in
        # it; wait for the leaf to empty, so the next command's leaf is the only one the claim names.
        with self._lock:
            rec = self._leaves.get(pgid)
        if rec is not None:
            _leaf_gone(rec[1], timeout=5.0)

    def _recheck(self) -> None:
        _refuse_plantable_sqlite_jewels(self._jewel_policy)
        for q, was in self._manifest.items():
            now = _identity(Path(q))
            if now != was:
                raise ConfinementError(
                    f"{q} changed since the shell started ({_describe(was)} -> {_describe(now)}); "
                    "the floor no longer covers what is on disk there."
                )

    def run(self, command: str, *, timeout: float | None = None) -> ShellResult:
        if self.closed:
            return super().run(command, timeout=timeout)   # the base refusal names the real reason
        try:
            self._recheck()
        except (OSError, RuntimeError) as exc:   # RuntimeError includes ConfinementError
            # A filesystem error while re-inspecting is a refusal too, as it is at spawn
            # (_bwrap_plan): a raw OSError would crash the tool call and leave the shell alive.
            self.close()
            raise ConfinementError(
                f"a crown jewel changed since this shell started, or could not be re-checked, so "
                f"the shell was closed and the command was not run. {exc}"
            ) from exc
        return super().run(command, timeout=timeout)


class BwrapProvider(ConfinementProvider):
    """Linux ``bubblewrap`` (mount-namespace) provider — the K4c counterpart to
    :class:`SeatbeltProvider`.

    Renders ``--bind / /`` (the default-ALLOW polarity) and then OVER-MOUNTS each crown jewel. See
    the module-level comment above :func:`_bwrap_argv` for the measured equivalence table, and that
    function for why each op was chosen over its plausible alternative.

    WHERE THIS IS STRONGER THAN THE macOS FLOOR, stated because the module docstring names the macOS
    version's weak point explicitly. The seatbelt write-floor's integrity rests on Apple applying
    ``file-write*`` to the SOURCE path of a hardlink/rename — undocumented behaviour on a deprecated
    tool. Here, moving a write-denied file out returns EBUSY (it is a mountpoint) and hardlinking it
    out returns EXDEV (the bind is a separate mount device). Both are ordinary, documented mount
    semantics rather than a vendor quirk, and both were measured.

    WHERE IT IS DIFFERENT IN A WAY A READER MUST KNOW. macOS answers a denied read with EPERM; a
    tmpfs over-mount answers with ENOENT — the jewel does not appear to exist rather than appearing
    forbidden. Confidentiality is equal (arguably better: existence is not confirmed), but an error
    message a human reads will say "No such file or directory", and anything matching on EPERM to
    detect a denial will not fire.

    ``--unshare-pid`` IS NOW BUILT (lane P2 item 2c, 2026-10-07), for a different reason than the one
    this paragraph once weighed it for: without it the sandbox's procfs showed every process of this
    user, so bash could read ``/proc/<pid>/environ`` of levain and the rest. pid_namespaces(7): the
    new procfs shows only the namespace's processes (asserted live by
    ``test_linux_live_bash_sees_no_host_process``, which the CI workflow's live job runs), and "If
    the "init" process of a PID namespace terminates, the kernel terminates all of the processes in
    the namespace via a SIGKILL signal". Each command now runs as its own bwrap with bash as that
    pid 1 (``--as-pid-1``), so a ``setsid`` child ends with its command
    (``test_linux_a_setsid_child_ends_with_its_command`` measures it)."""

    #: Enforced by ``--unshare-net`` (no IP network in bash), not by a per-destination rule; what it
    #: does not close is ``OFFLINE_RESIDUAL``.
    enforces_localhost_deny = True
    localhost_deny_removes_network = True

    def available(self) -> bool:
        return _cgroup_problem() is None and bwrap_available() and _leaf_chain_problem() is None

    def localhost_deny_ready(self) -> bool:
        return bwrap_netns_available()

    def _prepare(self, policy: CrownJewelsPolicy, made: list[tuple[str, str]],
                 hands: HandsIdentity | None = None, relay_dir: str | None = None):
        """Plan the floor and put on the host what it needs, inside the caller's ledger transaction:
        ``(argv, create_first, mounted, unmounted, manifest)``. Every object it creates is appended to
        ``made`` as it goes, so a refusal part-way still hands the ledger everything made."""
        # The named jewels' state BEFORE the plan reads the disk: a path that changes between the plan
        # and the manifest (an absent root created as a directory, say) would be recorded in its new
        # state, unmounted and never "changed" again, so the spawn refuses instead (codex L3 r4).
        try:
            before_plan = {q: _identity(Path(q)) for q in _named_jewel_paths(policy)}
        except (OSError, RuntimeError) as exc:
            raise ConfinementError(
                f"could not inspect the floor's jewels ({exc}) — refusing to grant bash hands "
                "(fail-closed)."
            ) from exc
        # The workspace exists BEFORE the plan reads $HOME: one directly in $HOME made later would
        # be missing from the step (0) view (codex, closing pass). Not a jail — reach is
        # default-allowed; Popen needs it to exist.
        policy.workspace.mkdir(parents=True, exist_ok=True)
        argv, create_first = _bwrap_plan(policy)
        mounted, unmounted = _mount_plan_paths(argv, policy)
        if hands is not None:
            # The bookkeeping (placeholders, manifest, the per-command recheck) stays the operator
            # floor's, computed above from its plan; only what bwrap executes changes.
            argv = _hands_bwrap_argv(argv, hands)
        # `-p` (privileged mode): bash neither imports exported functions nor reads SHELLOPTS,
        # BASHOPTS, ENV or BASH_ENV, so nothing from the env runs before the first recheck (codex L3 r6).
        # `--as-pid-1`: bash itself is pid 1 of the namespace and the bwrap process levain waits on
        # is its parent, so the status levain reads is bash's own waitpid status. Without it bwrap's
        # reaper is pid 1 and passes bash's status on through an eventfd the reaper holds; the reaper
        # runs as bash's user and stays dumpable (bubblewrap.c: do_init, monitor_child), so code in the
        # sandbox that could take that fd from it (ptrace, or pidfd_getfd where Yama allows) could
        # write a status of its choosing (spore-1385). With bash as pid 1 there is no such channel.
        # `--new-session`: bash calls setsid(), so it and everything it starts are in a process group
        # of their own, apart from the bwrap process levain waits on. Without it a `kill 0` inside the
        # sandbox reached bwrap too (a group signal crosses pid namespaces), so a command could stop or
        # kill the process that reports its status (S2 L2 M5).
        # A hands launch with egress ports starts each command's bash through the namespace side of the
        # proxy relays, which execs it (see `_HANDS_RELAY`): bash is still pid 1.
        relay_in = _hands_relay_in(hands, relay_dir) if hands is not None and relay_dir is not None else []
        argv = argv + ["--new-session", "--as-pid-1", *relay_in, "/bin/bash", "--noprofile", "--norc", "-p"]
        # Directories the floor must PIN but that do not exist yet (see step (1) in `_bwrap_plan`).
        # Created in parent-first order, 0700, by this process: bwrap cannot pin what it creates.
        # Each one this process made is levain's, recorded in the placeholder ledger and removed once
        # no session needs it; one that appeared meanwhile is someone else's and is left alone.
        for d in create_first:
            try:
                Path(d).mkdir(mode=0o700)
                made.append((d, "dir"))
            except FileExistsError:
                pass
            except OSError as exc:
                raise ConfinementError(
                    f"could not create {d} to pin it before sandboxing ({exc}) — refusing to grant "
                    "bash hands (fail-closed)."
                ) from exc
        try:
            _prepare_mountpoints(mounted, made)
            manifest = {q: _identity(Path(q)) for q in [*mounted, *unmounted]}
        except (OSError, RuntimeError) as exc:
            raise ConfinementError(
                f"could not prepare or record the floor's mountpoints ({exc}) — refusing to grant "
                "bash hands (fail-closed)."
            ) from exc
        moved = [q for q in unmounted if q not in before_plan or manifest.get(q) != before_plan[q]]
        if moved:
            raise ConfinementError(
                f"{moved[0]} changed while the floor was being planned, so the plan may not cover it "
                "— refusing to grant bash hands (fail-closed). Try again."
            )
        if hands is not None:
            # Only what the hands view shows is watched: a jewel outside it (the operator editing
            # ~/.ssh/config, say) is absent from bash's view and must not close the shell.
            manifest = {q: v for q, v in manifest.items() if _in_hands_view(q, hands)}
        return argv, create_first, mounted, unmounted, manifest

    def render_profile(self, policy: CrownJewelsPolicy) -> str:
        """The bwrap invocation as shell-quoted text.

        bwrap has no profile FILE — the policy IS the argv — so "render the platform's native
        sandbox profile text" is honoured by rendering the exact command. Quoted with
        :func:`shlex.join` so the rendered form is both diffable in a test and pasteable into a
        terminal to reproduce a floor by hand, which is how an equivalence claim gets re-checked
        later by someone who does not trust this docstring."""
        argv, create_first = _bwrap_plan(policy)
        mkdir = f"mkdir -m 700 {shlex.join(create_first)} && " if create_first else ""
        return mkdir + shlex.join(argv) + "\n"

    def hands_file(self, policy: CrownJewelsPolicy, hands: HandsIdentity, op: str, path: str,
                   data: bytes = b"", *, timeout: float = 60.0) -> bytes:
        return _bwrap_hands_file(policy, hands, op, path, data, timeout)

    def _spawn_shell_impl(
        self,
        policy: CrownJewelsPolicy,
        *,
        env: dict[str, str] | None = None,
        default_timeout: float = 120.0,
        hands: HandsIdentity | None = None,
    ) -> SandboxedShell:
        relay_dir: str | None = None
        if hands is not None:
            _require_hands_python()
            _require_hands_sudo(hands)
            problem = _hands_launch_problem(hands) or _hands_listener_problem(hands)
            if problem is not None:
                raise ConfinementError(f"{problem} — refusing to run bash as the entity's own user, or as "
                                       "you instead (fail-closed).")
            if hands.egress_ports:
                relay_dir = _hands_relay_dir(hands)
        if not bwrap_available():
            d = diagnose_confinement("Linux")
            raise ConfinementError(
                f"{BWRAP} cannot establish the floor's namespaces on this host — refusing to grant "
                f"bash hands without a confinement floor (fail-closed). {d.reason}."
                + (f" To fix: {d.remedy}." if d.remedy else "")
                + " The usual cause on Ubuntu 23.10+ is `kernel.apparmor_restrict_unprivileged_userns=1`; "
                "`bwrap` being installed and `kernel.unprivileged_userns_clone=1` can both be true on "
                "a host where this still fails, which is why it is probed by running bwrap."
            )
        # Started once per shell, after the sweep above (its sockets are not swept) and before the start
        # probe; from the shell's construction on, the shell owns it and its close stops it.
        relay = _start_hands_relay(policy, hands, relay_dir) if hands is not None and relay_dir else None
        try:
            return self._spawn_bwrap(policy, env, default_timeout, hands, relay)
        except BaseException:
            if relay is not None:
                relay.stop()
            raise

    def _spawn_bwrap(self, policy: CrownJewelsPolicy, env: dict[str, str] | None, default_timeout: float,
                     hands: HandsIdentity | None, relay: _HandsRelay | None) -> SandboxedShell:
        # A crashed levain's leaves first, so the ledger sweep below finds their claims empty.
        sweep_dead_leaves()
        # ONE ledger transaction from the sweep to the claim (L1 r1): another session's release or sweep
        # cannot remove an object this plan relies on between the plan and the claim. The sweep runs
        # FIRST, so a crashed session's objects are either gone before the plan looks or claimed by it.
        claim = _new_claim()
        made: list[tuple[str, str]] = []
        with _LedgerTxn() as txn:
            if not txn.ok:
                # Without a ledger this shell's claims cannot reach the disk, and another session
                # that can read it later would remove what this shell's masks stand on (codex, L3 r3).
                raise ConfinementError(
                    f"{txn.problem} — refusing to grant bash hands (fail-closed): the floor's "
                    "placeholder ledger is needed to keep its files in place for this shell."
                )
            txn.sweep()
            try:
                argv, create_first, mounted, unmounted, manifest = self._prepare(
                    policy, made, hands, relay.sockdir if relay is not None else None)
            finally:
                # Recorded even on a refusal: unclaimed, it is removed as the transaction ends.
                txn.record(made)
                txn.rollback = {p for p, _ in made}
            # Everything this spawn made is claimed too, parents included (an absent ~/.config
            # made for ~/.config/gh): unclaimed and non-empty, it would otherwise be forgotten.
            txn.claim({*mounted, *create_first, *(p for p, _ in made)}, claim)
        if txn.problem is not None:
            # The claim never reached the disk, so another session's close could remove a placeholder
            # this shell's mask would stand on, and the shell could then plant it (codex, L3 r2).
            raise ConfinementError(
                f"{txn.problem} — refusing to grant bash hands (fail-closed): without its claim on "
                "disk, another session could remove a file this shell's floor relies on."
            )
        # Startup-execution controls stripped as well as ignored by `-p`: bash would source, import or
        # expand these before the first per-command check, so a jewel that appeared after the manifest
        # could be read before anything looked (codex L3 r5, r6).
        shell_env = {
            k: v for k, v in (env if env is not None else _default_shell_env()).items()
            if k not in _STARTUP_EXEC_VARS and not k.startswith("BASH_FUNC_")
        }
        if hands is not None:
            # sudo OUTSIDE bwrap, as on macOS; bash starts from the hands user's environment alone.
            argv = [*hands_prefix(hands), *argv]
            shell_env = _hands_env(hands)
        shell = _BwrapShell(
            # The per-command SQLite check, like the manifest, covers only jewels in the hands view.
            policy=policy if hands is None else replace(
                policy,
                deny_read_write=tuple(p for p in policy.deny_read_write if _in_hands_view(p, hands)),
                deny_files=tuple(p for p in policy.deny_files if _in_hands_view(p, hands))),
            manifest=manifest,
            argv=argv,
            cwd=policy.workspace,
            env=shell_env,
            default_timeout=default_timeout,
        )
        shell._ledger_claim = claim
        shell._claim_base = claim
        shell._relay = relay
        _LIVE_BWRAP_SHELLS.add(shell)
        try:
            # Each command, the start probe included, tags the claim with its process group.
            shell.start()
            shell._recheck()   # whatever changed during the start closes it before any command
        except ConfinementError:   # a RuntimeError subclass: the recheck's own refusal, unchanged
            shell.close()
            raise
        except (OSError, RuntimeError) as exc:
            # A filesystem error in the recheck is a refusal like every other spawn-time inspection,
            # not a crash past the caller's ConfinementError handler (codex + complement L3 r6).
            shell.close()
            raise ConfinementError(
                f"could not re-check the floor's jewels after the shell started ({exc}) — refusing "
                "to grant bash hands (fail-closed)."
            ) from exc
        except BaseException:
            shell.close()   # never orphan a started shell (glm L3 r2)
            raise
        return shell


def _refuse_kernel_mask_rules(profile_text: str) -> None:
    """Refuse, before any exec, a Seatbelt profile that would make XNU build a syscall mask.

    A malformed one kernel-panicked a host on 2026-10-07 (a hand-written experiment, not levain's
    profile). levain calls ``sandbox-exec`` by absolute path, so no PATH shim on the host sees its
    profiles; this check is levain's own, at its only exec point, on every machine. The token set
    and the computed-operation forms live in :mod:`levain.firing.seatbelt_guard`.
    """
    from levain.firing.seatbelt_guard import scan

    hits = scan(profile_text)
    if hits:
        raise ConfinementError(
            "refusing to start the macOS confinement floor: the Seatbelt profile contains "
            f"{', '.join(hits)}, which makes the kernel build a syscall mask (a malformed one "
            "panicked a Mac on 2026-10-07). levain never emits these; this profile was altered."
        )


def sandbox_exec_available() -> bool:
    """True iff the macOS seatbelt driver is present + executable. The honesty floor: a caller must
    check this and REFUSE to grant bash hands if False, rather than fall through to an unconfined
    host shell."""
    return os.path.isfile(SANDBOX_EXEC) and os.access(SANDBOX_EXEC, os.X_OK)


def confinement_supported(system: str | None = None) -> bool:
    """True iff an OS confinement floor can actually be established RIGHT NOW — a provider exists
    for the OS AND *that provider's* sandbox driver is present + executable on this host.

    The honest gate for granting bash hands: ``levain run`` calls this to decide whether to offer the
    bash tool at all, rather than wire a tool whose first command would fail-closed. With no provider
    for the OS, or with the provider's driver missing, returns False — the entity gets its file-editor
    hand but no bash, and the banner says so (honesty floor). NEVER grants an unconfined shell as a
    fallback.

    ⚠ THE DRIVER CHECK BELONGS TO THE PROVIDER (:meth:`ConfinementProvider.available`), NOT TO THIS
    FUNCTION. It used to select polymorphically and then call ``sandbox_exec_available()``
    unconditionally, which was correct only while exactly one provider existed and would have failed
    in BOTH directions the moment a second one landed (see that method's docstring). Adding a
    platform branch here would have been the same defect one layer up.

    ⚠ ``system`` IS A SELECTION SEAM, NOT A REMOTE QUERY. The availability probe reads THIS host's
    filesystem, so ``confinement_supported("Linux")`` from a Mac asks "would the Linux provider find
    its driver *here*", never "is that other box confined". Callers gating real hands must pass
    ``None`` (the running OS); the parameter exists so provider selection is testable."""
    try:
        provider = select_provider(system)
    except ConfinementError:
        return False
    return provider.available()


def select_provider(system: str | None = None) -> ConfinementProvider:
    """The confinement provider for ``system`` (default: the running OS).

    macOS → :class:`SeatbeltProvider`. Linux → :class:`BwrapProvider` (K4c). Anything else raises
    :class:`ConfinementError` naming the seam — a container backend and a Windows-native provider are
    still PURE ADDITIONS here — and until one is built a caller on those platforms must fail-closed,
    never grant an unconfined shell.

    ⚠ SELECTING A PROVIDER IS NOT THE SAME AS HAVING A FLOOR. This answers "which provider governs
    this OS", never "can it run here" — :func:`confinement_supported` is the gate that asks the
    provider itself, and on Linux the answer is genuinely often False on a host where bwrap IS
    installed (see :func:`bwrap_available`)."""
    system = system or platform.system()
    if system == "Darwin":
        return SeatbeltProvider()
    if system == "Linux":
        return BwrapProvider()
    raise ConfinementError(
        f"OS confinement is not yet implemented for {system!r} — macOS (sandbox-exec) and Linux "
        "(bwrap) ship today. The ConfinementProvider seam is here; a container / Windows-native "
        "provider slots in as a pure addition. Refusing to grant bash hands without a confinement "
        "floor (fail-closed)."
    )


@dataclass(frozen=True)
class ConfinementDiagnosis:
    """Why this host does or does not have an OS confinement floor, in operator-facing terms.

    ⚡ ONE SOURCE FOR THE HOST'S ANSWER, WHICH TWO SURFACES NEED. The ``levain run`` banner and
    ``levain doctor`` both have to answer "will this entity get bash, and if not what do I do about
    it". The one ENTITY-side reason (``LINUX_LOCALHOST_REFUSAL``) is a shared constant both print.
    Computing that twice is the ``two_things_that_should_be_one_computed_by_two_pieces_of_code``
    class, and the drift would land in the two places an operator looks when something is wrong."""

    supported: bool
    provider: str | None   # human name of the provider for this OS, None if there is none
    reason: str            # one line: why there is (or is not) a floor here
    remedy: str | None     # operator-actionable fix, or None when there is nothing to do

    def operator_note(self) -> str:
        """A single line for the run banner. The remedy is folded in because the banner is often
        the ONLY thing an operator reads before concluding the tool is broken."""
        if self.supported:
            return self.reason
        return self.reason if self.remedy is None else f"{self.reason} — {self.remedy}"


def _apparmor_restricts_userns() -> bool:
    """True if this kernel's AppArmor policy is the thing blocking unprivileged user namespaces.

    ⛔ DIAGNOSTIC ONLY. THIS MUST NEVER BECOME THE CAPABILITY GATE, and the distinction is the
    whole lesson of K4c: ``spore-418`` specified exactly this shape of check (read a sysctl) and it
    reported GREEN on a host where bwrap could not run, because it named the OTHER sysctl —
    ``unprivileged_userns_clone``, the older Debian-lineage knob, which still reads 1 on Ubuntu
    while ``apparmor_restrict_unprivileged_userns`` is what actually decides. Capability is decided
    by RUNNING bwrap (:func:`bwrap_available`); this only explains a failure after the fact, so
    being wrong here costs a worse error message and never a wrong floor."""
    try:
        return Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns").read_text().strip() == "1"
    except OSError:
        return False


_APPARMOR_REMEDY = (
    "install Ubuntu's own bwrap profile: `sudo apt install apparmor-profiles && sudo install -m "
    "0644 /usr/share/apparmor/extra-profiles/bwrap-userns-restrict /etc/apparmor.d/ && sudo "
    "apparmor_parser -r /etc/apparmor.d/bwrap-userns-restrict` (keeps the host-wide restriction on; "
    "reverse with `apparmor_parser -R`). It stacks the sandboxed process under a child profile with "
    "NO capabilities: a child may still create a plain user namespace but cannot map root into it, so "
    "the entity's bash cannot run a nested bwrap, rootless docker/podman, flatpak, or a browser "
    "sandbox. Measured, with a control — not read off the profile."
)


def diagnose_confinement(system: str | None = None) -> ConfinementDiagnosis:
    """Answer "does this host have an OS confinement floor, and if not what do I do" ONCE.

    ⚠ THE THREE OUTCOMES ARE NOT TWO, and collapsing them is what made the pre-K4c message wrong:
    a floor can be absent because the OS has NO PROVIDER (nothing the operator can do), or because
    a provider exists and this HOST will not let it run (one command away). Telling an Ubuntu
    operator "no OS sandbox on this platform" when Levain fully supports their platform sends them
    looking for a port that already shipped."""
    system = system or platform.system()
    try:
        provider = select_provider(system)
    except ConfinementError:
        return ConfinementDiagnosis(
            supported=False, provider=None,
            reason=f"no OS confinement provider for {system} — macOS and Linux ship today",
            remedy=None,
        )

    if isinstance(provider, SeatbeltProvider):
        if provider.available():
            return ConfinementDiagnosis(True, "sandbox-exec (macOS seatbelt)",
                                        "macOS seatbelt floor active", None)
        return ConfinementDiagnosis(
            False, "sandbox-exec (macOS seatbelt)",
            f"{SANDBOX_EXEC} is missing or not executable on this Mac", None,
        )

    # Linux. THREE distinguishable states, and an operator needs a different sentence for each.
    if not (os.path.isfile(BWRAP) and os.access(BWRAP, os.X_OK)):
        return ConfinementDiagnosis(
            False, "bwrap (Linux mount namespace)",
            f"bubblewrap is not installed at {BWRAP}",
            "install it (`sudo apt install bubblewrap`, `sudo dnf install bubblewrap`, "
            "`sudo pacman -S bubblewrap`)",
        )
    problem = _cgroup_problem()
    if problem is not None:
        return ConfinementDiagnosis(False, "bwrap (Linux mount namespace)", problem[0], problem[1])
    if bwrap_available():
        # This is everything `BwrapProvider.available()` checks; asking it again would run the
        # launch probe a second time (L3 r10).
        said = _leaf_chain_problem()
        if said is not None:
            return ConfinementDiagnosis(
                False, "bwrap (Linux mount namespace)",
                f"a command cannot start in its own cgroup scope here ({said})", _LINGER_REMEDY)
        return ConfinementDiagnosis(True, "bwrap (Linux mount namespace)",
                                    "Linux bwrap floor active", None)
    if _bwrap_runs_without_a_pid_namespace():
        # The floor needs `--unshare-pid` (so bash cannot read other processes' environments), and
        # a new PID namespace needs a fresh /proc mount, which the kernel refuses where /proc has
        # masked or covered paths: the default inside Docker and podman (L2 r1, from bubblewrap.c).
        # Never dropped to make bash start: fail closed, with the cause and the fix.
        return ConfinementDiagnosis(
            False, "bwrap (Linux mount namespace)",
            "bwrap runs here, but cannot give bash its own PID namespace: a fresh /proc cannot be "
            "mounted (usually because this is a container whose /proc has masked paths)",
            "run levain on the host, or start the container with an unmasked /proc (Docker and "
            "podman: `--security-opt systempaths=unconfined`)",
        )
    if _apparmor_restricts_userns():
        return ConfinementDiagnosis(
            False, "bwrap (Linux mount namespace)",
            "bwrap is installed but this kernel's AppArmor policy denies unprivileged user "
            "namespaces (Ubuntu 23.10+; fixed by default in 25.04+)",
            _APPARMOR_REMEDY,
        )
    return ConfinementDiagnosis(
        False, "bwrap (Linux mount namespace)",
        "bwrap is installed but cannot create a user namespace on this kernel",
        "check `sysctl kernel.unprivileged_userns_clone user.max_user_namespaces` and any "
        "container/seccomp policy confining this process",
    )
