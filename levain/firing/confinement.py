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
    ``id_*`` (apparatus L2). ``ssh_mode="raw"`` is the fallback (allow raw ``~/.ssh`` read).
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
    operator threat model; named.
  - RESOURCE EXHAUSTION (fork bomb, disk fill) is a self-DoS on the operator's own Mac, not a
    confinement breach — seatbelt doesn't cap CPU/mem/disk. Cheap defense (``ulimit`` in the shell
    wrapper) is a later polish, not a floor concern.
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
  - The v1 floor is the Phill-RATIFIED set; it does NOT include other credential stores that
    default-allow leaves readable — ``~/.config/gh`` (a GitHub token = repo push), ``~/.aws/
    credentials``, ``~/.netrc``, ``~/.ssh/authorized_keys`` write. Those are ≥ the ssh key in impact;
    whether to fold them into the floor (vs. leaving them to ``deny_files`` wiring) is a Phill policy
    call (spore-311), deliberately NOT auto-decided here — adding them could break the entity's own
    legit gh/aws usage.

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

import os
import platform
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
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
    "ShellResult",
    "SandboxedShell",
    "ConfinementProvider",
    "SeatbeltProvider",
    "select_provider",
    "sandbox_exec_available",
]

# The macOS seatbelt driver. An ABSOLUTE path (never a PATH lookup — a confined child must resolve
# the sandbox binary deterministically, and this is the OS-shipped location).
SANDBOX_EXEC = "/usr/bin/sandbox-exec"

SshMode = Literal["agent", "raw"]

# Backpressure bounds so a runaway producer (``yes``, ``tail -f`` left after a timeout) can't grow the
# PARENT process's memory without limit (apparatus L3 complement #4). Normal commands never hit these.
_MAX_OUTPUT_CHARS = 8 * 1024 * 1024   # per-command returned output cap (then truncate + mark)
_MAX_QUEUE_LINES = 200_000            # reader-queue depth; oldest dropped past this (runaway only)


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
        (the fallback when agent-only proves too tight — a build-session settle).

    **Operator-declared crown jewels (the caller MUST pass — this generic, operator-neutral module
    deliberately does NOT guess where an operator's secrets live):**
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

    ssh_dir = (home / ".ssh").resolve() if ssh_mode == "agent" else None

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
    # Ancestor write-denies for BOTH the subtree jewels and the file jewels (both relocate the same
    # way). The ssh_dir is guarded as a subtree below, so its ancestors are covered too.
    all_jewels = list(deny_read_write) + list(deny_files_t)
    if ssh_dir is not None:
        all_jewels.append(ssh_dir)
    write_dirs = _write_deny_ancestors(all_jewels)

    return CrownJewelsPolicy(
        entity_dir=ed,
        workspace=ws,
        deny_read_write=deny_read_write,
        deny_files=deny_files_t,
        deny_write_dirs=write_dirs,
        ssh_dir=ssh_dir,
        ssh_mode=ssh_mode,
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
        """Terminate the shell + EVERY child it spawned, then release resources. Idempotent, never
        raises. Signals the process GROUP (SIGTERM → SIGKILL) so a timed-out/backgrounded child is
        reaped, not orphaned to init."""
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
    everywhere else a caller-declared jewel is final; this keeps ssh consistent)."""
    p = path.resolve()
    if p in policy.deny_files:
        return True
    return any(p.is_relative_to(sub) for sub in policy.deny_read_write)


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
