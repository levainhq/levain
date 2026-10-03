# Changelog

All notable changes to Levain. Format is loosely [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses [SemVer](https://semver.org/spec/v2.0.0.html).

> **This file starts at 0.4.2.** Earlier releases were documented in commit messages only — which is itself one of the defects this release closes: an operator upgrading through 0.4.x had no surface that told them what changed underneath their install. Entries for 0.4.0 and 0.4.1 are backfilled below because they carry a behaviour change adopters needed to know about and were never told.

## [Unreleased]

### Added

- **`levain serve --chat <entity>`: chat with an entity through the local web server.** Pass `--chat` once per OpenHands entity. The server holds each conversation in memory and drives its turns as jobs: `POST /chat/open {"entity": <dir name>}` starts a session, `POST /chat/turn {"session_id", "message"}` sends a message, and `GET /chat/job.json?id=` reports the job. A running job's `activity` list grows as the entity uses its tools, and the finished job carries the turn's result (reply, tool activity, error, and `exit_code` as `levain run --task` reports it). `GET /chat.json` lists the entities, and the sessions by entity and state; it never lists a session id, so an id is held only by the client that opened the session. `/chat/approve`, `/chat/reject` and `/chat/close` complete the set. `--model`, `--base-url`, `--api-key` and `--max-iterations` work as they do for `levain run` (`--max-iterations` must be at least 1). There is no chat page yet; this is the API only.
  - **The server builds every agent itself.** A request names an entity the operator passed on the command line, plus message text. It cannot supply an agent, a tool spec, a model or a drive mode; such fields in a request body are ignored. The crown-jewels floor travels in the hands' tool spec, so a client-supplied spec would be the client writing its own floor.
  - **Every chat route needs the chat token.** `levain serve --chat` makes a new token each time it starts and prints it once; send it as `X-Levain-Chat-Token`. It lives only in the server's memory and your terminal. Without it a chat route answers 403. The rest of the server stays token-free on loopback: the token is for the routes that make an entity act, because a container reaching your loopback through Docker's `host.docker.internal`, a sandboxed app or another account on the machine can reach loopback without being able to edit your files.
  - **Sessions are `headless`, so the efferent gate is armed** for any entity whose `efferent_gate` is `auto` (the default). The token shows a caller holds it, not that a person is watching, so the server does not claim a human is present. A turn that proposes an efferent action (any shell command, any file write) halts; `/chat/approve` runs the held actions, `/chat/reject` refuses them with a reason the entity sees, and a new message is refused until one of the two happens. The credential floor is the same as for an interactive session.
  - **An entity whose `confinement.json` sets `allow_localhost_outbound` is refused**, at startup and again from the floor of the session once it opens. Its shell could call the chat routes itself and approve its own held actions. Use `levain run` for it.
  - **Several entities, one process.** Each session's hands enforce its own entity's floor, and each turn is captured into its own entity's store. A session whose turn errors is marked `broken` and its shell is released; it takes no further turns.
  - **A turn stops at a wall-clock limit**, `--turn-seconds` (default 1800). At the deadline the job reports `deadline_hit: true` and the turn is asked to stop. It stops at its next step boundary, so a model call or shell command already running finishes first; it is reported `timed_out` (exit code 5), nothing from it is captured, and its session is closed. Tool calls the model asked for in the same step that had not started when the stop arrived are skipped, and are not listed in the turn's activity. A model endpoint that stalls in the middle of a call can hold a session past the deadline for as long as the model client's own timeout and retries allow. A session being closed reads `closing`, and counts toward the session limit until its shell is released. A `--turn-seconds` longer than a thread can wait (`threading.TIMEOUT_MAX`) is refused at startup.
  - **Loopback-only, nothing persisted.** A server with `--chat` refuses a non-loopback bind, because its chat routes have no off-box auth. The routes use the same Host check, cross-site refusal and JSON-only body rule as the write routes. A restart ends every conversation; resuming one after a restart is not offered.
  - At most four live sessions per server. When a session fails to start, the server keeps the error text, drops the exception and collects it, so tools built before the failure are released before the failure is reported.

### Fixed

- **A refused action is no longer listed as work.** After an efferent action was rejected at the gate, the turn's tool activity (the REPL's summary, `--task` output, a chat job's `tool_activity`) still showed the refused command, on the same screen as the refusal.

- **Tool activity now shows the shell command an entity runs.** A bash action used to appear as `⚙ terminal: TerminalAction` in the REPL's stream, in `--task` output and in a turn's `tool_activity`, so the person watching never saw the command. It now shows the command's first line, cut at 160 characters.

- **The crown-jewels floor now denies the operator's project memory, `~/.anneal-projects`, on both macOS and Linux**, reading and writing, the way it already denied `~/.anneal-memory`. That directory holds per-project anneal stores and the derive-trust file that says which repo roots a store re-derives its claims against. A confined shell could read and write both (run on macOS in all three drive modes): it could rebind a trust label, or write into a project store whose memory is later loaded into an operator session. An entity can no longer read project memory either; none needed to.
  - If `$ANNEAL_MEMORY_DERIVE_TRUST` is set in levain's own environment when the floor is built, that file is write-denied at the path given, at the link's own location when the path runs through a symlinked directory, and at its target; its directory cannot be renamed. A relative value is resolved against levain's working directory, which may not be the one the reader uses. The variable is read from levain's environment only: a value set just for another program (flow sets it only for its project-memory command) protects nothing here, and the default location is what is covered.
  - **Not covered:** a project home moved away from `~/.anneal-projects`, whose stores can be anywhere the trust file points; and a path to the trust file that runs through more than one symlinked directory, whose middle links can be swapped (the same open limit as for `~/.ssh` in a symlinked home).
  - If `~/.anneal-memory` or `~/.anneal-projects` is itself a symlink, the link is now pinned, so it can no longer be removed and replaced with a planted directory. **On Linux this refuses bash while either store is a symlink**; replace the link with the real directory.
  - On Linux, a host with no `~/.anneal-projects` gets an empty one the first time a confined shell starts, because bwrap creates the directory it mounts over; it stays after the shell exits. A trust file named by the variable but missing at that moment is left as an empty read-only file, which anneal then refuses to trust until it is removed.
- **`levain serve` now sends its security headers on every response**, including the ones Python's HTTP server generates itself (an unsupported method such as OPTIONS or PUT, or a malformed request). Those used to go out with no Content-Security-Policy, `X-Content-Type-Options` or `X-Frame-Options`; `levain init --web` and `levain docs` already sent them.
- **`levain init --web` now holds the install lock while it refreshes the pack docs**, as `levain init` in the terminal already did. The refresh clears and recopies `.levain/docs/`; it used to run after the install had released the lock, so a `levain init` or `levain update` started at that moment could interleave its own copy. A busy install is still refused before anything is written (409, not reported as a partial install).

### Changed

- **The three local web servers share one set of request guards** (`levain.http_guards.GuardedHandler`): the security headers, the DNS-rebinding Host check, the cross-site read refusal, and the write checks (origin, JSON-only body, Content-Length, the 413 for an oversize body). Each server used to carry its own copy, and the copies had drifted (the fix above). No route or refusal changes.

## [0.5.2] — 2026-10-02

### Changed

- **Each conversation's crown-jewels floor, drive mode and entity are now one value created when the session opens, and handed to every part that enforces them.** Before this, `$LEVAIN_DRIVE_MODE` and `$LEVAIN_ENTITY_DIR` were the only channel the floor and the memory firings had. With two `EntitySession`s in one process (which no CLI path reaches yet; the server-held conversation will), we measured three crossovers, on macOS and on Linux:
  - A second session's drive mode set the first session's file-editor and bash floor. The first session's banner said one thing while its tools enforced another.
  - A second session that failed to open still moved the first session's floor.
  - Once the process named a second entity, the first session's recall, re-anchor and capture all used the second entity's store.
  The new `levain.firing.binding.ConversationBinding` holds the entity, the drive mode and the resolved floor. It reaches both hands as data in their tool spec, and the condenser carries the entity, so a fork keeps all of it and nothing is looked up by conversation. Two entities can be open in one process, and each reads and writes only its own store. `EntitySession.open` builds the hands and reads their floor back before it returns, so a session's floor cannot change after it opens. (A library caller that builds a conversation itself gets its hands at the first turn, from the tool spec as it stands then.)
- **Both hands are now one tool spec, `levain_hands`,** built by `build_entity_tools(binding, with_bash=...)`. **Breaking for library callers:** `build_entity_tools` now requires the binding, and the registry names `levain_file_editor` and `levain_bash` are gone. The tool names the model sees (`file_editor`, `terminal`) are unchanged.
- **A process that has opened an entity refuses every default-kind (`"anneal"`) store operation** (`$LEVAIN_ENTITY_PROCESS`). A stray bare `vagus_run` or `wrap_nudge` there used to be redirected to the entity; it now refuses loudly, so it can never reach the operator's `~/.anneal-memory`. Levain's own capture always names its entity and is unaffected.
- **`confinement.json` is read once at session start.** The banner and the floor come from the same read (it was read twice; listed under 0.5.0's known open issues).
- **Hands built for one entity cannot be put on another entity's agent**: `build_entity_agent` refuses them.

### Removed

- **`$LEVAIN_DRIVE_MODE` and its "one process hosts one drive mode" refusal** (`bind_drive_mode`, `current_drive_mode`, `DriveModeConflict`). Once any unattended session had opened, the process could never open an interactive one. An interactive session now opens beside an unattended one, each with its own floor.
- **`$LEVAIN_ENTITY_DIR`, `bind_entity` and the "one process hosts one entity" refusal.** `guard_entity` checks an entity without touching process state. An isolated firing or seed built with no entity now has no store, instead of reading the variable.

### Fixed

- **A session that failed to start after its conversation was built left that conversation open** until the process exited. It is now closed.
- **Forking a conversation whose hands had been built crashed** (`cannot pickle '_thread.lock' object`). A fork now gets its own hands, fenced by the same floor.
- **Installing `levain[openhands]` and `levain[app]` in two steps broke one of them.** `openhands-sdk` 1.26.0 accepts any `fastmcp>=3`, and fastmcp 4 requires mcp 2, while the MCP-App server needs mcp 1. So `[openhands]` alone pulled mcp 2.x. Adding `[app]` afterwards then broke the OpenHands import, and installing in the other order broke `levain serve-app`. Neither pip nor uv reported a problem. The `openhands` extra now also requires `mcp>=1.27,<2`, which keeps fastmcp on 3.x. Installing both extras in one command already worked and still does. The cost: `levain[openhands]` can no longer share an environment with a package that needs mcp 2 or fastmcp 4.
- **`levain serve-app` with mcp 2.x installed told you to install `levain[app]`**, which you already had. It now names the mcp version it found and what repairs it: re-installing both extras when OpenHands is installed (pinning mcp alone would break it), pinning mcp when nothing else needs mcp 2, and a separate environment when another package's fastmcp 4 does.
- **A session that failed to start no longer leaves a file-editor history directory behind**, whichever step failed. A session that closed normally already removed it.

## [0.5.1] — 2026-10-02

### Security

- **A SQLite crown jewel's sidecar files were open to the confined shell.** When a denied store is a file rather than a directory (an `~/.anneal-memory` that is a SQLite database, as on some hosts), only the main file was denied. In WAL mode, recent committed rows live in `<db>-wal` until a checkpoint, so the shell could read them and write `<db>-shm`.
  - **macOS:** the floor now also denies `-wal`, `-shm` and `-journal` beside every file-shaped jewel, by name, whether or not they exist yet.
  - **Linux:** worse in 0.5.0, and not fixable with mounts. A mount can only cover a file that exists, and SQLite creates and deletes its sidecars as it runs. With the store closed, the shell could create `<db>-wal` itself, and the host's next open replayed it into the database, so the entity could rewrite the store. A `-wal` the host creates after the shell starts (for a store in any directory, including one only a more privileged service writes) was readable. So `levain run` now **refuses bash on Linux when a crown jewel is a SQLite database**, and when a jewel cannot be read and sits in a directory you can write. The file editor still works. There is no opt-out.
  - **If your host is affected** (bash is refused with "is a SQLite database"): move the store into a directory, as `~/.anneal-memory/` normally is. Before 0.5.0 such a host had no bash at all.

### Known open issues

- **A crown-jewel path that does not exist when the shell starts, and later becomes a SQLite database, is not refused on Linux.** The check runs at spawn. Credential files and the default store (`~/.anneal-memory/`, created as a directory) are not affected.
- **On Linux, a jewel that is an empty (or not yet initialised) file when the shell starts is not refused**, and neither is one this user cannot read in a directory it cannot write. Either can become, or be, a database whose sidecars a later writer creates where the shell can see them.
- **An encrypted SQLite store (SQLCipher) has no recognisable header**, so the Linux refusal does not see it. A planted sidecar would need the store's key to be replayed.

### Fixed

- **On Linux, a deny path inside `.levain` that did not exist yet stopped bash from starting.** bwrap tried to create it inside the read-only store mount. Such a path cannot be created from inside the shell either, so it is no longer mounted.
- **`levain doctor` passed a malformed `confinement.json` when the host diagnosis itself raised.** The diagnosis ran first, and a probe error was reported as "not determinable" before the entity's config was read, so doctor stayed green for an entity `levain run` refuses. The config is now read first, so it fails the check on every host.
- **On Linux, reinstalling a seat during a turn warned "PREVIOUS definition" even when nothing changed.** The default seat runs the `levain` console script, and the kernel records a script's process as `<interpreter> <script> <args>`, so the exact comparison with the seat's command never matched. The check now compares the end of the command line, which still catches a changed task.
- **A failed periodic-to-resident reinstall on Linux left the service enabled at login.** The rollback restored the timer but did not disable the resident service that the failed install had enabled, so after the next login the service ran beside its own timer. The rollback now disables it.
- **The README told non-macOS users there was no sandboxed shell.** It was wrong from 0.5.0, which ships the Linux (`bwrap`) shell. The PyPI 0.5.0 page carries the old sentence; this release replaces it.

## [0.5.0] — 2026-10-01

**Minor, not patch: Linux becomes a supported platform** (a bubblewrap confinement floor and `systemd --user` seats). Install-truth work lands alongside it: `levain update` now refreshes the activation tree and adapter files after an upgrade.

### Known open issues in this release

Found in review, not fixed in 0.5.0, each tracked for a later release:

- **A pack `order` change alone can switch which pack's activation hook is installed, without review.** When two packs ship the same `activation/hooks/<file>`, changing only one pack's `order` in its `pack.toml` makes `levain update` install the other pack's hook. A changed hook *file* is held for review; a changed *winner* is not.
- **A pack source edited and then reverted while `levain update` runs can slip its edited hook in.** It needs a concurrent writer to the pack directory during the update.
- **Codex's machine-global files are guarded by a per-install lock.** `~/.codex/hooks.json` and `config.toml` are shared by every install on the machine, so two installs updating at once can interleave on them.
- **On Linux, the localhost block does not cover unix sockets at a file path, or AF_VSOCK.** Bash runs with no IP network, but a socket at a path the floor does not deny (an ssh ControlMaster or proxy socket in `/tmp`, D-Bus, X11) can still reach this host's services and bypass the block, and inside a VM so can AF_VSOCK. `levain run` and `levain doctor` print this. The ssh ControlMaster case is the same on macOS (named since 0.4.6).
- **`levain run` reads `.levain/confinement.json` twice at startup.** The banner is rendered from one read and the floor is built from the second, so editing that file at the very moment an entity starts can make the banner describe a different floor from the one in force. The entity cannot write the file; only an operator edit at startup can cause this.

### Changed — on macOS, an unattended seat can no longer read the Keychain

From inside the confined shell, `security find-generic-password -w` and Keychain-backed credential helpers (`git credential-osxkeychain`, the token `gh` keeps in the Keychain) used to read your secrets, in every drive mode. The macOS floor now denies direct access to the Keychain services whenever the standard credential floor is on: by default for an **unattended** seat, not while you drive `levain run` yourself. It is the same switch as `~/.config/gh` · `~/.aws/credentials` · `~/.netrc`, so `"deny_standard_creds": false` in `.levain/confinement.json` gives a specific seat its Keychain back, and `true` denies it while you drive too. Server-authenticated HTTPS and git over the forwarded ssh agent are unaffected. What breaks for a seat with the floor on: HTTPS pushes with a Keychain-stored credential, `gh`, `docker-credential-osxkeychain`, Python `keyring`, code signing, and client certificates held in the Keychain; such a seat needs the opt-out or the ssh agent. **What this does not cover:** credential stores that are not the Keychain (a running `git credential-cache` daemon, for one, answers over its own socket), and asking an unsandboxed app that is already running to read the Keychain for the entity (the same open class as the localhost and IPC limits in the confinement docs). Linux has no counterpart in this release.

### Changed — `levain doctor` sends a stale hook or carrier to `levain update`, not `init --force`

Since `update` refreshes the activation tree and adapter files (below), doctor's remedy for a hook or `CLAUDE.md` / `AGENTS.md` that predates the installed levain is `levain update --path <install>`, which keeps your edits. It used to say these fixes "do not arrive via `levain update`" and sent you to `init --force`, which re-runs the interview. `init --force` stays the remedy for an edited hook you want back to the package's version, and for an unreadable install receipt.

### Fixed — install truth

- **A pack `order` change alone now moves the installed seed file.** Previously the old winner's copy stayed installed and every later `levain update` reported clean. The lock now records which layer won each seed filename (`wins` on each pack's provenance; a lock without it is read as before), and `update` installs the new winner, backing up an edited copy first. A rendered seed whose win moved is flagged for re-onboard.
- **Two `levain init --force` runs on one install can no longer interleave.** One of them used to crash mid-swap or leave the activation receipt describing a tree that was not installed. `init` and `update` now take a per-install lock (`.levain/install.lock`), and a second run is refused at once with nothing written. A filesystem that does not support `flock` at all proceeds unguarded, with a note; any other lock error (including `ENOLCK`, e.g. NFS with lockd down) refuses with the real error.

### Added — install truth

- **`levain update` now refreshes the activation tree and the adapter files after an upgrade**: the hooks, `posture.md`, `recency_directives.md`, `CLAUDE.md` / `AGENTS.md`, `.claude/settings.json`, `.mcp.json`, and for codex the `hooks.json` / `config.toml` block that belong to this install. `pip install -U levain` no longer needs `levain init --force` after it.
  - Each file is decided on its own. A file you edited is kept.
  - If the new release changed that file too, its new version goes to `.levain/pending/` and the file is listed once for you to merge (`update` exits 1 that run).
  - On an install made before Levain recorded what it wrote, only hook scripts, and JSON files that differ solely where Levain substitutes your interpreter path, are replaced, each with a backup. Everything else is staged.
  - Codex's machine-global files are never repointed from another install.
- **`levain adopt-answers [--dry-run]`.** Reads your interview answers back out of hand-edited `world.md` / `origin.md` (and any pack's rendered seeds) and records them as `.levain/answers.json`, so a re-render from the record keeps your edits instead of reverting them.
  - All or nothing: a seed that does not read back to an answer set that renders it again is refused, with the first line that departs, and nothing is written.
  - Also refused: a drifted pack, or a field your record never answered.


### Added — Linux support: a confinement floor and a scheduled seat (K4c)

Levain's sovereign entity now runs on Linux, not only macOS. Two pieces, and the second is the one that makes an unattended seat possible at all:

- **Confinement** — a `bwrap` (bubblewrap) mount-namespace floor enforcing the same crown-jewels policy as the macOS seatbelt floor. It is a re-derivation, not a port: seatbelt is a path predicate, `bwrap` is a mount namespace with the opposite default, and every mapping was measured on a Linux kernel rather than read off a man page. Where the two differ, Linux is stronger — a write-denied file cannot be renamed out (`EBUSY`, it is a mountpoint) or hardlinked out (`EXDEV`, separate mount device), which rests on ordinary mount semantics instead of the undocumented Apple behaviour the macOS floor depends on.
- **Lifecycle** — a `systemd --user` provider (service, plus a separate `.timer` for a scheduled seat). `install` also requests `loginctl enable-linger`, without which a user unit stops when your last session ends and a headless box's seat would quietly die at logout.

**⚠ On Ubuntu 23.10 through 24.10, including 24.04 LTS, expect Levain to report no bash hands until you act.** Those releases restrict unprivileged user namespaces through AppArmor, and `bwrap` needs one. Ubuntu 25.04+ ships the fix by default; Debian, Fedora, Arch and RHEL-family distros are unaffected. Levain fails closed and says so — the entity still gets its file-editor hand, which is fully cross-platform, but no shell.

**The fix is Ubuntu's own profile, not one you write.** `levain doctor` prints the three commands, and they are in the README under *Linux*. It keeps the host-wide restriction on and is reversible. ⚠ One consequence worth knowing before you apply it: the profile stacks the sandboxed process under a child profile with no capabilities, so the entity's bash can no longer run a nested `bwrap`, rootless docker/podman, flatpak, or a browser sandbox.

**Do not infer capability from the usual two checks.** `bwrap` being installed and `kernel.unprivileged_userns_clone=1` can BOTH be true on a host where every invocation still fails — that pair reads green on a machine we measured as entirely unable to run it. Levain therefore probes by *executing* `bwrap`, and you should too.

### Fixed — the run banner told Linux operators their platform was unsupported

`levain run` printed *"bash dropped: no OS sandbox on this platform"* whenever the bash hand was unavailable. That was true while macOS was the only supported OS. It is false now in the most common Linux case — a **supported** platform whose kernel refuses to start the sandbox, which is one command away from working — and it is the sentence an operator reads before concluding Levain has no Linux support.

The banner now names the real reason and the fix, and `levain doctor` gained a **confinement floor** check reporting which floor is active or why there is none. A missing hand is reported, never failed: an entity with only its file-editor hand is a working, supported configuration, and turning a healthy install red for a missing optional hand is how an operator learns to ignore `doctor`. Both surfaces read one shared diagnosis.

### Fixed — `confinement_supported()` could not have reported a second platform correctly

It selected a provider polymorphically and then checked the macOS driver unconditionally. With one provider that was invisible; with two it fails in both directions — a working Linux floor would never be offered, and asked about Linux from a Mac it would answer yes from a host that cannot know. The driver check now travels with the provider.

### Fixed, before release — the Linux floor as first written let containers through, broke the entity's own memory, and could not start on most hosts

Found by running it on a Linux kernel while bringing K4c onto this release line, and by review:

- **A container daemon socket was reachable from inside the Linux sandbox.** The socket was mounted read-only, and a read-only mount does not stop a `connect()`: the daemon answered. A socket in a directory its daemon owns is now hidden by hiding that directory, which held when the daemon restarted and recreated its socket. A socket in a directory everything shares (`/run/docker.sock`, rootless Docker's `$XDG_RUNTIME_DIR/docker.sock`) can only be masked as a file: it is covered while it exists when the shell starts, and a daemon that starts or restarts after that is reachable until the next shell. The banner says so on Linux.
- **The rootless sockets were not on the list.** Rootless podman, rootless Docker and Docker Desktop for Linux are on it now. Rootless podman's directory is hidden even before podman first runs, so starting it later from inside the sandbox does not expose it. If `$XDG_RUNTIME_DIR` is unset (cron, `su`), Linux falls back to `/run/user/<uid>` instead of dropping these entries.
- **The entity's own memory broke after its first bash command.** For each store file that did not exist yet, the sandbox left a read-only empty file on the host. The next write to the episodic store then failed ("attempt to write a readonly database"), and an entity with no `confinement.json`, which is what `init` produces, refused to start next time because the file was now empty JSON. The entity's `.levain` directory is now mounted read-only for the confined shell, with its existing subdirectories writable, so the shell can create nothing new there and nothing is left behind. On macOS the shell may still create and edit other files at the top of `.levain`; on Linux it may not.
- **Bash failed to start on any Linux host without containerd, CRI-O and colima.** Every daemon directory on the socket list was pinned as a jewel's parent, and the sandbox cannot pin a directory that does not exist or a symlink such as `/var/run`. A missing parent is now skipped unless the sandbox will create something inside it, in which case it is created first and pinned, so it cannot be renamed away and replaced. A symlink is pinned at its real path, except one the user could replace (a symlinked `~/.ssh`), which gets no bash on Linux.
- **A file denied both ways could become readable.** A credential the operator pinned under `~/.ssh` in raw mode was masked and then re-exposed by a later read-only mount of the real file.
- **`levain doctor` reported bash for a config `levain run` refuses.** A malformed `confinement.json` now FAILS the confinement floor check, because `levain run` refuses to start that entity. This is the one way the check fails; a missing hand is still reported, never failed.
- **The user's service manager was a way out of the Linux sandbox.** Measured in a container with a real `systemd --user`: from inside the confined shell, `systemd-run --user` ran a command outside it that printed a crown-jewel file. The shell can no longer reach the user's systemd manager or session D-Bus (measured: both are refused), so it has no `systemctl --user` and no desktop D-Bus. There is no opt-out yet.
- **Scheduled seats on Linux (`systemd --user`).** A task containing `%`, `${...}` or a backslash was rewritten by systemd before it reached the seat (measured: `%n` became the unit name); arguments are now escaped and arrive exactly. Reinstalling a running unit now applies the new definition instead of leaving the old one running. Switching a cockpit to a seat stops the cockpit service. A seat's status now shows a failing last turn. systemd older than 240 is refused before anything is written. The cadence is start to start, as on macOS; the old description said otherwise.
- **A daemon label is now a plain name on both platforms.** Letters, digits, `.`, `_` and `-` only. A label such as `../x` used to name a file outside the unit directory for `install` and `uninstall`.
- **The sandbox creates missing mount targets on the host** (an empty directory or file where a protected path did not exist). That has always been true of the Linux floor; it is now stated, and kept out of the entity's own store.

### Changed — on Linux, bash runs without IP network while the entity's floor blocks connections back to this host

Since 0.4.6 the floor stops the entity from connecting to this machine's own services (the `sshd` route in 0.4.6's security note). On macOS the sandbox blocks just that destination. On Linux, `bwrap` cannot block a single destination, so the block runs bash in its own empty network namespace: connections back to this host fail, over TCP and over abstract unix sockets (both measured), and so does all other network inside bash (`pip`, `git fetch`, `curl`). Bash keeps its own isolated loopback. Not blocked: a unix socket at a file path the floor does not deny (an ssh ControlMaster or a proxy socket in `/tmp`, D-Bus, X11), which can reach this host's services and so bypass the block (spore-1005), and, inside a VM, AF_VSOCK to the hypervisor. On a host where `bwrap` runs but cannot create a network namespace, bash is dropped rather than offered and left to fail. The entity's model calls are made by `levain` outside the sandbox and are unaffected. `levain run` and `levain doctor` both say so. To give bash the network back and accept that exposure, set `"allow_localhost_outbound": true` in the entity's `.levain/confinement.json`.

## [0.4.8] — 2026-09-30

### Fixed — `levain init` in the terminal no longer writes an empty identity

`levain init` answered with bare Enter throughout used to exit 0 and write an empty operator and entity name into `seed/origin.md`, which `levain doctor` then failed. The terminal interview was the one onboarding path that skipped the identity rule; the `--answers` file and the web form already refused it. The interview now re-asks a blank operator or entity name, drops a blank one restored from an old checkpoint so it is asked again, and after three blanks in a row stops with an error rather than looping (a closed pipe reads as endless blanks). An identity question inside an optional section can no longer be skipped into a blank. The re-ask of a refused blank no longer prints a "(revising)" header, which belongs to going back with `:back`.

### Fixed — `levain update` no longer deletes a seed file that another pack still ships, or one you edited

When a pack's override of a base seed file was dropped, `update` could delete the file outright, or leave it reading as up to date, so the base version never came back. Reconcile now works out which layer owns each seed file: a dropped override restores the layer it hid, an unreadable base layer means nothing is deleted (it is reported instead), a stray Finder `.DS_Store` in a pack's `seed/` no longer makes the whole stack unreadable, a file moved from one pack to another in one update is judged once, and an undecodable `pack.toml` is treated as an unreadable layer rather than crashing `update`. A copy you edited is kept, backed up and reported, never overwritten silently. Each restore or removal prints one line saying what it did.

**Known limit, unchanged from 0.4.7:** a change to a pack's `order` alone, with no override dropped, is not reconciled. The `pack.toml` comment documents it as a v1 limitation.

### Changed — `levain doctor` exits 1, not 6, on a corrupt or empty activation receipt

`doctor`'s hook-freshness check used to read a receipt it could not parse the same way it reads an install that predates the receipt entirely: both landed on exit 6, "an upgrade step is pending." **A receipt that exists but is unreadable is not a pre-receipt install** — `_write_activation_receipt` is the sole writer and runs only after the tree is in place, a failed write reads as absent (not corrupt), and a failed swap rolls the prior receipt back — so a legitimate install cannot produce a corrupt or empty one. Reading it as "no receipt yet" could downgrade a tampered hook (an appended `os.system(...)`, say) from broken (exit 1) to routine-pending (exit 6). A corrupt or empty receipt now exits 1, naming the receipt's path and why doctor could not read it, with the same `init --force` remedy. An absent receipt (a genuinely pre-receipt install) is unchanged: still exit 6.

`levain init --force`'s own post-swap notice had the identical gap: absent, corrupt and empty receipts all printed the byte-identical "no install receipt covers it" line for the retained backup tree. Corrupt and empty now say so explicitly — "its install receipt was unreadable (corrupt/empty) … a damaged receipt is not a pre-receipt install" — while absent keeps its original text.

*(This entry was filed under `[Unreleased]` when 0.4.8 was cut. The change is in the 0.4.8 code, so it belongs here.)*

## [0.4.7] — 2026-09-14

### Changed — Levain now requires anneal-memory 0.9.10 or later

The anneal floor moves from `>=0.9.8` to `>=0.9.10`. Per anneal's own 0.9.10 changelog, the published 0.9.9 could report a destroyed audit history as healthy: once the sealed audit file its manifest names and the active file were deleted, `verify()` returned `valid=True` with no entries, and `anneal-memory audit` could leave out a whole sealed week while still reporting the trail trusted. Levain calls no new anneal API; the floor exists so a Levain install cannot resolve to that anneal. `pip install -U levain` brings anneal along.

### Changed — `levain init` backs up `~/.codex/config.toml` whenever it would change the memory-server block at all

The Codex adapter rewrites the global `[mcp_servers.anneal_memory]` block. It used to copy `config.toml` aside only when it judged that something you had customised was about to be lost, and that judgement missed cases: a wrapper `command` that kept Levain's arguments, or a comment you had written inside the block. Now any change to that block's text writes `config.toml.bak.<timestamp>` first, and the notice says what kind of change it was:

- upgrading an install made before this release: "keeps its store and now starts the memory server as …", plus where the copy is;
- a comment or formatting inside the block: "keeps its settings, but comments or formatting inside that block were not kept", plus where the copy is;
- anything else that differs: the existing "customisation is gone" warning, plus where the copy is.

Re-running `init` against a block Levain wrote, byte for byte, still does nothing and says nothing.

### Changed — `levain doctor` explains the anneal-version mismatch on installs set up by an older Levain

Levain releases before 0.4.7 recorded the version of whichever `anneal-memory` came first on `PATH`, which was not always the anneal Levain itself runs. After upgrading such an install, `doctor`'s `compat: anneal-lock` line could call that mismatch "an out-of-band upgrade". For an install last composed by a Levain older than 0.4.7 it now says the recorded version most likely belonged to a different `anneal-memory` on `PATH`, and points at `levain init --force` or `levain update`, either of which records the anneal Levain actually imports. The check and `doctor`'s exit code are unchanged.

### Changed — the memory server is now the anneal installed alongside Levain, not the first one on `PATH`

`levain init` used to look up `anneal-memory` with a `PATH` search and write whatever it found into the MCP registration and the hooks. If a second anneal sat earlier on `PATH` (say an old `~/.local/bin/anneal-memory`), that one served your memory, while `levain doctor` and `levain update` checked a version chosen by the same lookup. Levain itself imported a different anneal again.

- The MCP registration (`.mcp.json` for Claude Code, `[mcp_servers.anneal_memory]` in `~/.codex/config.toml` for Codex) now runs `<Levain's Python> -P -m anneal_memory`. `-P` keeps the directory the client was started in off Python's import path, so an `anneal_memory/` source checkout there cannot stand in for the installed package.
- The hooks, `doctor`, `update` and `init --web` use the `anneal-memory` script that belongs to the anneal package Levain's own Python imports. They find it through that package's install record, not through `PATH`. If that script cannot be run, they fall back to `<Levain's Python> -P -m anneal_memory`. The hooks no longer try a bare `anneal-memory` from `PATH` at all.

**Existing installs keep their old registration until you re-run `levain init --force`.** Nothing breaks if you don't; you keep whichever anneal the old lookup picked. `levain doctor` now reports such a registration as a pending post-upgrade step (below).

### Changed — `levain doctor` exits 6 when the only failures are post-upgrade steps not yet applied

`doctor` used to exit 1 both when something was broken and when a routine post-upgrade step was still pending, so the expected upgrade path looked like a broken install. It now exits:

- `0` — every check passed;
- `1` — at least one failure that is not a pending upgrade step (and whenever `--invoke`'s live-fire check fails);
- `6` — every failure is a post-upgrade step not yet applied: hooks or the adapter carrier that predate the installed levain, the version set last composed by an older levain (`levain update` pending), or a memory-server registration that does not run Levain's own interpreter (`levain init --force` pending).

The failures are still printed as `FAIL`, because they are real: until you apply the named remedy, the install keeps running the activation layer from the release it was last set up with.

⚠ **If a script branches on specific codes** (`case $? in 1) ...`), exit 6 no longer matches the `1` branch. Scripts that only test for nonzero (`levain doctor || alert`) behave as before.

### Changed — `levain init --force` keeps your whole previous `activation/` tree, and deletes an old one only when it can prove you never edited it

`init --force` used to look at each file under `activation/` before replacing the tree, and copy the ones it judged to be yours into `.levain/backups/activation/<timestamp>/`. Anything that judgement could not handle was lost: an edit saved in the moment between the check and the replacement, a symlink, a directory it could not read.

- **The previous tree is moved whole** to `.levain/backups/activation/tree-<timestamp>/`. Every file, symlink and directory you had is in it, exactly as it was.
- **`init` now records what it installed** in `.levain/activation-manifest.json`: for each file under `activation/`, the sha256 of the bytes as installed and of the package file they came from. A copy travels beside each kept tree as `tree-<timestamp>.receipt.json`.
- **An old tree is deleted only if every file in it matches that record**, with nothing added and no symlinks: then it holds nothing of yours. The newest three such trees are kept. Any tree that differs, has extra files or symlinks, or has no readable record (every tree kept from an install made before this release) is **never deleted**, and `init` names it.
- The "Operator-edited … preserved at" lines are worked out from that record, so they now also catch an edit made only to the `_INSTALL_ANNEAL_BIN` line of a hook. Where there is no record, `init` says it cannot tell.
- If `.levain/backups/activation/` cannot take the move, the previous tree is kept beside `activation/` as `.levain-activation-prev-<timestamp>`, `init` tells you so, and levain never removes it. A reinstall is never blocked by this.
- Backup directories written by earlier releases (named with a bare timestamp) are never removed.
- This supersedes 0.4.5's stated exception for symlinks under `activation/`: they are now kept as symlinks.
- If `activation/` itself is a symlink, the link is kept beside it as `.levain-activation-prev-<timestamp>` (where it still resolves) and its target is left untouched; `init` says so.

## [0.4.6] — 2026-09-13

### Security — a local `sshd` could be used to read a confined entity's crown jewels via the forwarded agent (agent mode) or a readable key (raw mode)

⚠ **This affects 0.4.5 and every earlier release with the seatbelt floor.** If you run a confined
entity in the default `ssh_mode="agent"` **and** macOS **Remote Login** is on (System Settings →
General → Sharing → Remote Login) **and** a key your ssh-agent holds is in your
`~/.ssh/authorized_keys`, then a confined entity could run `ssh localhost cat <a crown-jewel file>`
and get the file back — even though the floor denies reading that file directly. `sshd` runs as
root, was never inside the sandbox, and was authorised by the very agent socket that agent-mode
forwards by design. This is the same total-bypass class as reaching a container daemon socket (fixed
in 0.4.x): a floor that denies a *file* but forwards a *credential* to an unsandboxed root reader is
decorative for that file. **Reproduced end to end**; the forwarded agent socket is the sole carrier.

**Fixed:** in **both** ssh modes the floor now denies outbound network connections to *this host* —
`(deny network-outbound (remote ip "localhost:*"))`. macOS seatbelt's `localhost` covers **every
address bound to a local interface** (loopback `127.0.0.1`/`::1` *and* the machine's own LAN
address), so the entity can no longer reach a `sshd` this machine runs by any of its own addresses,
while **remote** hosts stay reachable — ssh/git to real remotes over the forwarded agent is
unaffected. (It also blocks the entity reaching a local service that re-exposes jewels, e.g. a
loopback API, independent of ssh.)

- **Cost, and it is real:** a confined entity in agent mode can no longer reach a **local service**
  either — a dev server it started, a database, an `argushub` on `127.0.0.1:8420`. If an entity
  genuinely needs that, set `"allow_localhost_outbound": true` in its `.levain/confinement.json` and
  accept that the `sshd` vector is reopened for that entity.
- **Scope:** the deny applies in **both** ssh modes. The reason it is not agent-only: in
  `ssh_mode="raw"` the entity can read its *own* key directly, but the **other** crown jewels
  (`~/.anneal-memory`, sibling stores, your declared secrets) stay floor-denied, and a local `sshd`
  authenticated with that key reads *them* as root just the same (reproduced) — so raw mode needs the
  deny too. ⚠ A separate raw-mode exposure remains for a later release: raw mode still lets the entity
  *read the operator's `~/.ssh` private keys directly*. The loopback deny stops those keys reaching a
  **local** `sshd`, not a remote one; retiring or redefining raw mode as scoped-credential-only is
  part of the credential redesign below.
- **Not covered — ssh connection multiplexing (a documented residual, not closed).** This deny
  stops a *fresh* connection to a local `sshd`. It does **not** stop reuse of an **existing** ssh
  `ControlMaster`/mux socket to localhost: if you run `ControlMaster auto`/`yes` and have a live
  master to this host, a confined entity that can name the `ControlPath` can ride that already
  authenticated master over its unix socket — which the IP deny cannot see, and which needs no
  agent. Denying *all* outbound unix-socket connects would close it but was measured to break
  `getaddrinfo`/HTTPS/`git`/`pip` (macOS resolves DNS through an mDNSResponder unix socket), so it is
  not the default. The precondition is narrow (ssh multiplexing *to localhost* specifically is
  unusual); a scoped fix is tracked for a later release. If this matters to you now, set
  `ControlMaster no` for the confined entity or don't keep localhost masters alive.
- **Not covered — the forwarded agent is a signing oracle (relay/`ProxyCommand`).** A forwarded
  agent signs for *any* `sshd` that authorises its key, reached through *any* hop the floor allows.
  An entity can go `ssh -o ProxyCommand='ssh <relay> nc %h %p' <this-host>` — the sandbox sees only
  the allowed connection to the relay, which connects back to this host's `sshd`. So a **Tailscale**
  peer (or any reachable relay) defeats the localhost deny; a Tailscale address is **not** covered by
  it (an earlier draft wrongly claimed it was). This is not closeable by a network deny — it needs
  agent *mediation* or not forwarding the agent, part of the agent-forwarding review above.
- **Not covered — off-interface addresses:** a `sshd` reachable only through an address **not** bound
  to a local interface (e.g. a NAT hairpin to your router's external IP).
- **Why not just deny port 22:** macOS seatbelt has no per-port or per-IP loopback filter
  (`(remote ip "127.0.0.1:22")` is rejected outright), so denying all outbound-to-self is the only
  enforcement it offers.

### Fixed — `init --adapter codex` widened the permissions on your global Codex config, and `levain update` narrowed them on `.levain/config.json`

⚠ **This affects 0.4.5, which is published.** If you ran `levain init --adapter codex` under 0.4.5
against an existing `~/.codex/config.toml` that already had an `[mcp_servers.anneal_memory]` block,
its permissions were reset to your shell's default — **whether or not anything was repointed**, and
whether or not you saw any output about it. Run:

```
ls -l ~/.codex/config.toml
```

If that is more open than you set it, restore it with `chmod 600 ~/.codex/config.toml` (or whatever
you had). Upgrading does not repair a file already changed; it stops it happening again.

⚠ **How open it became depends on your `umask`, so there is no single number to look for.** Under
the common `022` it is `-rw-r--r--`, readable by every account on the machine. **Under `002` it is
`-rw-rw-r--` — group-WRITABLE**, which for a file that tells every Codex session on the machine
which memory store to read is worse than it sounds. Under `077` nothing changed.

⛔ **The earlier wording of this entry said "if it repointed Codex's memory", and that was wrong.**
Only the *backup* is conditional on a repoint; the write happens on every run. Re-running `init
--adapter codex` against the store already registered — the ordinary re-install — widened the file
silently, with no backup and nothing printed. That is the case least likely to have been noticed,
and the first version of this advisory excluded exactly those people.

0.4.5 made that write atomic, correctly — a partial write could otherwise truncate your whole
global Codex config. But writing a new file and moving it into place replaces the file itself, so
the permissions came from the process default rather than from the file being replaced. The
previous version wrote through the existing file and so kept them without trying.

The file sits beside `~/.codex/auth.json` and is machine-wide, so an operator who set `0600` on it
meant it. The **mode** of the file being replaced is now carried across, and nothing else is — the
modification time still moves, because the file genuinely did change.

### Fixed — a Codex block Levain could not read was replaced silently, with no backup

The warning and the backup that guard Codex's machine-wide `anneal_memory` registration only fired
when Levain could read **both** the old store path and the new one out of the block. If your block
was not the shape Levain writes — you moved the store into a wrapper `command`, or restructured
`args` so it no longer carries `--db` — Levain could not read the old store, took no backup, said
nothing, and replaced your block anyway.

That is backwards: a block Levain cannot read is the one most likely to have been edited by hand,
and the least safe to overwrite without a copy. Replacing a block Levain does not recognise now
copies the file aside first and says so, naming where the copy is.

Re-running against the store already registered still does nothing and still says nothing — that
case takes nothing away, and turning it into backup spam would train you to ignore the notice that
matters.

### Fixed — repointing Codex replaced a symlinked `config.toml` instead of writing through it

If `~/.codex/config.toml` is a symlink into a dotfiles repo — stow, chezmoi, or a hand-rolled
setup — 0.4.5 replaced **the symlink** with a regular file and left the real file holding the old
registration. Levain then correctly printed that Codex now points at the new store, and the
operator's next re-stow silently put the old one back. A registration that undoes itself later,
having been accurately announced at the time.

The write now lands on the file the symlink points at, and the symlink survives.

⚠ **Known-open: a hardlinked `config.toml` still loses its link.** The atomic replace breaks it
(the other name keeps the old contents), and there is no atomic-rename form that preserves a
hardlink — the alternative is writing through the existing inode, which is the torn-write risk the
atomic write exists to remove. If you hardlink your Codex config, check the other name after
running `init --adapter codex`.

⚠ **Stated limit: the mode is restored, ACLs and extended attributes are not.** Replacing a file
drops them and there is no macOS API in this path that puts them back. If you restricted
`~/.codex/config.toml` with `chmod +a` rather than with a mode bit, that restriction is still lost
when Levain repoints Codex. Use a mode bit if you need it to survive.

⚡ The backup taken moments earlier in the same operation had always preserved the mode. So a single
run left `config.toml.bak.<timestamp>` at `0600` and the live `config.toml` beside it at `0644` —
the copy made to protect the file was better protected than the file.

**The same defect was live in the opposite direction and is fixed too.** Every atomic write in
Levain replaces the file rather than writing through it, so every one of them can move the mode.
`.levain/config.json` — the file your entity name and brand settings live in — is written through a
helper whose temporary file is always created `0600`, so an operator who had opened it up to `0644`
or `0640` (to let a second account or a service read it) had it closed back to `0600` by the next
`levain update`, surfacing later as an unrelated-looking permission error at the reader. That helper
now carries the existing mode across as well. A file being created for the first time still gets
`0600`, which is the safer default when there is no operator intent to preserve.

## [0.4.5] — 2026-09-06

**`levain init --force` is the command our own upgrade instructions tell you to run, and in 0.4.4 it deleted operator edits to the activation tree without a backup and without a word.** It copied aside exactly two files — `posture.md` and `recency_directives.md` — while replacing everything else, so a patched hook script was destroyed on the documented upgrade path. That happened to a real operator.

This release widens what survives that command, and corrects the operator-facing messages that described a system slightly better-behaved than the one we shipped.

### Changed — `init --force` now preserves everything under `activation/` it cannot rebuild, not two filenames

Anything at `activation/` whose bytes this install cannot reproduce — because you edited it, or because no layer provides it any more — is copied to `.levain/backups/activation/<timestamp>/`, keeping its relative path, **before** the tree is replaced. Hooks included. Files you added yourself included.

Membership is decided by whether the bytes are reproducible, never by filename, so this does not need updating when the tree gains a file.

⚠ **It preserves; it does not always announce.** Where a difference is one `init` itself wrote — the `_INSTALL_ANNEAL_BIN` line, which is re-resolved on every run — the copy is still made but no "operator-edited" notice is printed, because an edit of your own confined to that same line is indistinguishable from our substitution. If that copy cannot be made, `init` says so and names the file rather than continuing silently. **Where the difference is one we cannot explain, `init` refuses rather than overwrite it.**

⛔ **Stated exception: symlinks are not handled.** A symlink to a directory inside `activation/` is never examined and is removed with the tree; a symlink to a file is compared by its target's contents and replaced by a regular file. Neither is backed up. If you have symlinked anything under `activation/`, copy it aside yourself before upgrading.

### Fixed — `doctor` told you what `init --force` keeps, and left out what it does not

Two checks — `hook freshness` and the carrier check (`CLAUDE.md freshness` on a Claude Code install, `AGENTS.md freshness` on Codex) — ended their advice with a list of what survives the re-render. Every item was true, and the list read as complete: it named your store, your seed answers and your activation markdown edits, and did not mention that the whole `activation/` tree is rewritten or that the interview runs again.

Both now say the interview re-runs, say the tree is rewritten, and name where edits are copied. **The "seed answers are kept" clause is gone because it was false** — `init --force` starts the interview with no answers pre-filled and clears the saved checkpoint, so working through it accepts new answers over your recorded ones.

### Fixed — a hook you had not touched could be reported as operator-edited

`init` resolves the `anneal-memory` binary on the `PATH` fresh on every run and writes the result into the hook. Running `init --force` from a shell that resolves it differently — a virtualenv versus a plain login shell is enough — changed those bytes with no involvement from you, and the new backup then reported the hook as an operator edit.

It also recurred: each run wrote its own resolution in, so alternating between two shells re-triggered it indefinitely, and on an install whose backup directory was not writable it turned a routine re-install into a refusal.

Hook scripts are now compared with that one substituted line normalised out, so only differences we did not write are reported.

### Fixed — `init --adapter codex` repointed Codex's machine-wide memory without saying so

The `[mcp_servers.anneal_memory]` block in `~/.codex/config.toml` is global by design: it is what every Codex session on the machine reads, not just the install you ran `init` from. Replacing it was silent and left no copy, so running `init` against a second install — or a scratch directory — moved every Codex session onto that install's store with nothing printed.

`init` now names the store it is leaving and the store it is adopting, says the registration is machine-wide, and copies `config.toml` aside first. It still performs the repoint, because pointing Codex at a different install is a legitimate thing to want and is the documented way to undo this. Re-running against the store already registered stays silent.

The config file is now written atomically and the notice is printed only once the write has landed.

### Known issue — an install path containing `\`, `"` or a newline produces an unparseable Codex config

Not new in this release and not introduced by it. `init --adapter codex` substitutes the install path into `config.toml` without escaping it for TOML, so those characters produce a file that is **not valid TOML** — a backslash is an escape introducer inside a basic string and a quote ends it early. What Codex does with a config it cannot parse is its business and we have not measured it; what we can say is that levain wrote the file wrong. All three characters are legal in POSIX paths and all three are rare. Avoid them in an install path until this is fixed.

### Correction to 0.4.4's upgrade note — a normal upgrade leaves `doctor` red, and 0.4.4 did not say so

⛔ **`pip install -U levain` to 0.4.4 leaves `levain doctor` at exit 1 with two failures, for every operator, on the correct upgrade path.** Measured against the published wheels — 0.4.3 and 0.4.4 installed side by side in clean virtualenvs, upgraded the way an operator upgrades:

```
0.4.3   All checks passed.                                                    exit 0
0.4.4   [FAIL] hook freshness: installed hook script(s) differ from the package
        [FAIL] compat: levain: levain upgraded 0.4.3 -> 0.4.4 since the set was last composed
                                                                              exit 1
```

**Both failures are correct, and neither check is new.** 0.4.3 carries both and passes them (`hook scripts match the package`, `levain 0.4.3 (matches last composed)`). They turn red *because the version moved* — the hook scripts on disk were rendered by the previous release's templates, and the compatibility set was composed against the previous release. **Nothing is broken.** The remedies are the two commands the failures already name:

- `levain init --force --path <install>` — re-renders the activation tree. ⚠ **It re-runs the interview**, and anything you have edited under `activation/` is copied to `.levain/backups/activation/<timestamp>/` first (see *Changed* below; in 0.4.4 that backup covered only `posture.md` and `recency_directives.md`). Your store is untouched;
- `levain update` — reconciles the anneal + schema + migration set to the new known-good.

⚠ **What 0.4.4's own `Upgrading` note got wrong**, and it is why this correction exists: it says to run `doctor` after upgrading and then warns about exactly ONE possible new failure — `activation scope` — which only affects operators whose hooks are wired at the user level. It presents `levain init --force` as what you need *additionally*, for three named features, rather than as a required step. **An operator who follows it exactly is prepared for one specific failure and receives two different ones.** That section is left as published; this entry is the correction.

⚠ **Measured from every 0.4.x release, not inferred.** Each of 0.4.0, 0.4.1, 0.4.2 and 0.4.3 passes its own `doctor` at exit 0, and each one gives the identical two failures and exit 1 under 0.4.4 — the `compat` message naming the version it came from in each case. So this is the whole 0.4.x upgrade surface, measured rather than generalised from one path.

▶ **The design question underneath this is open and deliberately not answered here:** `doctor` exits 1 identically for *"something is broken"* and for *"a routine post-upgrade step is pending."* An instrument that goes red on the expected path teaches its operator to discount it, which is expensive for the one tool we ask operators to trust when something genuinely is wrong.

### Fixed — a rejected confined shell could leak if logging the rejection failed

A shell that fails the confinement check is torn down before the refusal is raised. That teardown was reached only by falling off the end of the handler that logs a failed `close()` — and a logging call is not guaranteed not to raise, since a custom handler whose `emit` raises propagates straight out of it. A broken logging handler therefore skipped the teardown entirely, leaking the subprocess, its process group, the FIFO directory and the reader thread on every rejected spawn, **and** replaced the confinement refusal with the logging error. The teardown now runs from a `finally`, so no logging failure can strand it.

## [0.4.4] — 2026-09-06

**The crown-jewels confinement floor stops reporting coverage it does not have.** Three ways a path or a socket could sit outside the floor while the tooling said it was fenced: a `~user` path that threw its way past the check, a container daemon socket that defeated the floor entirely, and a listed socket resolved before the shell it fences existed. The `levain run` banner now names which sockets are covered **and which are not** — the deny is an enumeration, and an enumeration is always incomplete.

Alongside it: `doctor` stops reporting green on two of the three ways to write user-level wiring, both local servers stop mishandling two `Content-Length` cases, and the test suite has zero permanent failures for the first time.

### Fixed — a `~user` path could throw its way past the crown-jewels check

`crown_jewel_reason` — the in-process guard that decides whether the file-editor hand may touch a
path — caught `ValueError` and `OSError` from resolving that path, but not `RuntimeError`. Python
raises exactly `RuntimeError` from `expanduser()` for a `~someuser` spelling with no passwd entry.
The path comes from the entity, so a model emitting `~unknownuser/...` reached a function documented
as fail-closed and got **neither a refusal nor an allow** — it got an exception out of the security
predicate.

The same gap sat in `declared_resources`, whose docstring says *"Never raises."* and whose own
comment explains why that matters: a raise there surfaces a raw error event and **skips the
executor**, so the floored refusal never fires at all.

Both now catch it and refuse the path. This is the same defect the `doctor` fix in this release was
written for; it reached `doctor` and not the security twin beside it.

### Fixed — a container daemon socket defeated the whole confinement floor

If a container runtime was installed, a confined entity could read any crown jewel with one
command — `docker run --rm -v <jewel>:/x:ro alpine cat /x`. The daemon runs as root and was never
inside our sandbox, so no path deny and no mount could hide anything from it: on such a machine
every other rule in the floor was decorative. No privilege escalation and no exotic technique were
involved; the entity simply asked a more privileged process to read the file on its behalf.

The known container/VM daemon sockets — docker, podman, containerd, CRI-O — are now part of the
universal floor, denied by default. Opt out with `"allow_container_sockets": true` in
`.levain/confinement.json` if your entity genuinely needs to drive containers and you accept that
doing so voids the rest of the floor on that machine.

**The obvious fix does not work, which is why this took three rules rather than one.** A seatbelt
`file-read* file-write*` deny does not block `connect()` to a unix socket — with the socket paths in
the credential denylist the profile emits correct-looking rules and the exploit still returns the
jewel. Blocking the connect needs a `network-outbound` rule, and that rule alone is then defeated by
renaming the socket, and *that* is defeated by relocating its parent directory. All three are
enforced, and each was measured failing on its own before it was kept.

### Fixed — a listed socket could be reported as covered while it was not

The socket denies above are stored **resolved** — the profile can only name where a path *points*,
never the path itself, because the sandbox canonicalises the target before matching. Those denies
were computed when the policy was built, and the confined shell starts later (and starts again after
every `exit`). A listed socket that did not exist yet resolved to itself, so if something created it
as a symlink to an unlisted target in between, a connect through the **listed name** landed on a
target nothing denied.

The resolution now re-runs every time a shell starts, and a freshly-resolved target gets the **same
three rules** as a socket named in the list — connect, rename, and relocation — not just the connect
one. The fresh entries are **added** to the existing set rather than replacing it, and the widened
set carries forward into the next shell, so the denylist can only ever grow across a session.
Coverage is now a spawn-time snapshot instead of a build-time one, and `levain run` says so in those
words.

Two related consistency fixes came out of the same reviews. The re-resolution happens **once** per
shell start and the shell reports back the exact rule set it was started with, so nothing a shell
enforced can be lost when the next one starts. And both of the entity's hands — the shell and the
file editor — now read **one** floor object rather than each holding its own copy, so a rule added
for one is enforced by the other.

The first version of this fix updated only the connect rule, which the three rules above already
explain is not enough on its own; the next re-resolved twice and kept the earlier answer. The guard
meant to stop a sandbox backend from skipping the re-resolution was rewritten five times and
sidestepped seven ways, and was ultimately **deleted rather than rewritten a sixth time** — the ways
to shadow a method in Python are not a closed set, so enumerating them cannot terminate. What
replaces it is structural: the re-resolution now happens in the base method itself, so a backend
implementing the documented seam cannot receive stale rules. Each of these was caught before release
and the tests pin them. (The single review count for this whole section is stated at its end.)

**What this was, honestly: a wrong claim rather than a new hole.** A confined entity cannot create
that symlink — the socket is write-denied at both spellings, which is one of the three rules above —
so it takes something outside the sandbox to arrange, and the unlisted socket it would point at was
already reachable under its own name, which the banner already told you. What it cost was your
ability to trust the list: a socket that was named as covered was not. That is fixed.

**The residual is stated rather than papered over.** A sandbox profile is fixed when the process
starts, so a symlink created at a listed path *after* a shell is already running is still not
covered, and no rule available here would cover it. A spawn-time snapshot is the strongest statement
this sandbox language can make, which is why the wording says snapshot and not "covered".

**Both of the entity's hands read one set of rules, per conversation.** The shell and the file
editor previously each held their own copy, so a rule the shell learned at startup was invisible to
the editor. They now share one set for the length of a conversation, and a later conversation never
inherits an earlier one's — which also means a run that should be more restricted than the last one
actually is.

**Nine rounds of review went into this section, and every one of them found something in the
previous round's fix — a rate that did not bend once.** That number is the count of the review
rounds recorded in this repository's own commit trail for the change, not an estimate; earlier
drafts of these notes said "three" in one place and "five" in another for the same span, and both
understated it. Round nine ran on 2026-09-05 against an outside model
lineage and still returned findings; one was a real resource leak on a rejected-shell path and is
fixed here, and the others did not hold against the code as it now stands. One of the
rounds found the real shape of the shared-rules problem: the rules were cached per *entity*, which
made "both hands see the same rules" and "the rules are current" pull against each other, so fixing
either one broke the other. Caching per *conversation* removes the conflict rather than balancing
it, because a conversation has one set of rules by definition. Everything found was caught before
release.

**The re-resolution now happens before the sandbox backend is asked to start a shell**, rather than
inside it. An earlier attempt guarded the backend against skipping that step; the guard was removed
because it could be worked around in a growing number of ways and never covered them all, while
doing the work upstream means a backend is simply handed rules that are already current. There is no
step left to skip. Nothing here is operator-visible — it changes which layer is responsible, not what
the floor denies.

**The banner names which sockets are covered, and which are not.** The deny is an enumeration and an
enumeration is always incomplete, so `levain run` says so rather than claiming containers are
fenced: a custom `$DOCKER_HOST`, a TCP daemon endpoint, or any runtime whose socket is not in the
list remains reachable. If that describes your setup, the floor does not cover it.

⚠ **macOS only in this release.** The Linux confinement floor is not in 0.4.4, so there is nothing
here for it to apply to yet; the same three arms land with Linux support.

### Fixed — `levain doctor` carried a dead import, and the release did not pass lint

`_sha256_file` had been imported by the hook-freshness check since the comparison it served was
replaced by placeholder-aware normalisation, along with a comment explaining why it was imported
rather than reimplemented. A dead import keeps its justifying comment looking live. Both are gone,
and `ruff` passes again.

### Fixed — `doctor` reported green on two of the three ways to write user-level wiring

The dark-install detector compared each hook command token as a **literal path**, with no variable expansion and no `~` handling. So `~/lev/activation/hooks/session_start.py` and `$HOME/lev/...` — the second of which Levain's own `settings.template.json` spells inside double quotes — resolved against the current directory, never matched the install, and the scan came back empty. `levain doctor` then reported the reassuring install-scoped PASS on the exact configuration the check exists to FAIL.

A miss in `_hook_command_targets` produces a wiring FAIL, which is loud. A miss here produced silence — the same `absence_of_signal_rendered_as_health` that this whole run of releases is about, inside the instrument built to end it.

⚠ **The first cut of that fix reintroduced a defect closed earlier in this same release.** `Path("~someuser/...").expanduser()` raises `RuntimeError` for a user with no passwd entry, and `RuntimeError` is neither `OSError` nor `ValueError` — so expanding tokens added a fresh way for a *foreign* `~/.claude/settings.json` to abort the entire doctor run. That is the `[null]`-entry crash from 0.4.2 arriving by a new door. Caught before commit by asking whether the condition the guard covers would disable the guard.

### Fixed — the upgrade note named the wrong command

The 0.4.3 notes said `levain update` overwrites the hook templates. It does not: `update` writes nothing under `activation/` at all, and its pack phase only ever writes under `seed/`. What replaces the activation tree is **`levain init --force`** — which is step two of the documented upgrade procedure, so following our own instructions is what kept eating the local patch. Corrected at all four sites carrying the claim, including `docs/operator-manual.md`, which said `update` "re-applies any updated partnership settings".

### Fixed — the Codex adapter README promised a failure Codex operators cannot get

It told Codex operators `doctor` FAILS when hooks are wired at the user level under install scope. For a Codex install both halves of that condition are the **default**, and `_user_level_wiring` deliberately never reads `~/.codex/hooks.json` — a test pins the never-FAIL. A promise of a red that never comes reads as a green. The bullet now says `doctor` REPORTS the gate for Codex and names where the answer actually appears.

### Fixed — the seed still scaffolded the graduation cap the library removed

`seed/continuity.md` taught "graduates to 2x, then 3x" while `seed/memory.md` teaches the uncapped ladder. The 2026-08-31 uncapping landed at one of the two sites, so a fresh entity was still scaffolded with the cap anneal dropped in 0.9.7. The scaffold now states the direction and points at `memory.md` rather than carrying a second copy of the rule.

### Fixed — `doctor` reported a pack-owned hook as permanently stale, and the remedy could not clear it

A pack hook containing an install-time placeholder (`{{ANNEAL_MEMORY}}`) was compared as RAW BYTES against the manifest's PRE-substitution source hash, so it read stale forever. Worse, the fix `doctor` itself prints — `levain init --force` — **cannot** clear it, because re-rendering substitutes the placeholder again. The base-adapter branch never had this problem because it normalises both sides; the pack branch skipped that step and now does the same, against the pack source. A pack whose source directory has moved is reported as *not comparable* rather than as a stale hook — that is the pack-drift surface's business, not this check's.

### Fixed — a corrupt `.levain/manifest.json` silently turned pack-hook checking OFF

`doctor` read the pack lock through a wrapper that returns "no packs" for ABSENT and for CORRUPT alike, so a truncated or partially-synced manifest dropped every pack-owned hook out of the comparison **while still reporting "hook scripts match the package"** — over a tampered file, if there was one. It now refuses and names the cause: an unreadable lock is not a clean bill of health. A genuinely absent lock is still the ordinary pack-less install and stays green.

### Fixed — the local servers mishandled two Content-Length cases

Both `levain serve` and the onboarding server:

- **A header like `Content-Length: ²` crashed past the guard.** The check was `str.isdigit()`, which is TRUE for characters `int()` refuses, and `int()` was the very next statement — so a deliberate `411` became an unhandled exception. Now ASCII-digits-only, which is also what RFC 7230 specifies. Non-ASCII digits are refused with a `411`.
- **An oversize POST whose Content-Length OVERSTATED the body got no response at all** (`levain serve` only). The drain stranded on the socket timeout, and the resulting error was indistinguishable from a benign browser keep-alive reset, so it was swallowed: no `413`, no traceback, no log entry. The connection is now always closed on an oversize refusal, and the `413` is sent whether or not the client sent the bytes it promised.

### Fixed — `--consolidate-max-seconds` errors named `--max-seconds`

`levain run` and `levain daemon install-seat` validate the consolidate bound through the same checker as `--max-seconds`, which hardcoded that flag in its messages — so a bad `--consolidate-max-seconds` value produced `levain run: --max-seconds must be >= 0`, sending the operator to fix an option they had not set.

### Changed — a lease-expired job now reads `failed` instead of disappearing

**Behaviour change, and it is operator-visible.** A `pending`/`running` job whose lease expired (a crashed worker's orphan) was DROPPED on the next store write, so polling it returned `unknown` — the status this API reserves for "never seen it, re-propose". Four places in the code and the `/job` route's own docs said such a job reads `failed`.

The composition is what made it matter: the result TTL is matched to the 24-hour idempotency window **deliberately**, so a replayed propose can still poll its handle. Non-terminal records reaped at the 10-minute lease broke that — a same-key retry replayed a handle saying `pending` while the store answered `unknown` for that job id, which is the dead-handle case the TTL matching exists to prevent, and it made an expensive job look re-runnable.

Such a record is now transitioned to `failed` / `"interrupted"` — exactly what restart recovery already does to the same records — and then ages out under the result TTL like any other finished job.

### Fixed — the crown-jewels floor's own record

No behaviour change; the enforcement was correct throughout and is stricter than these descriptions implied.

- `build_policy`'s public docstring described the superseded **name-based** ssh design (`~/.ssh/id_*`) and contradicted its own next bullet. The floor is **location-based** — all of `~/.ssh`, which catches `deploy_key` and per-host keys — and the module docstring is now the single place that states it.
- A comment claimed the raw `Path.home()` ssh spelling *narrows* the symlinked-HOME vector. Measured: it is unreachable on both enforcing hands. The entry is kept as defence-in-depth against a platform canonicalisation change; the claim that it mitigates a live vector is gone.
- The seed's `continuity.md` scaffold taught the capped graduation ladder at a **third** site missed by the earlier fix, and `crystallization.py` carried a fourth.

### Fixed — the suite has zero permanent failures for the first time

Nine tests failed on every run, on any machine without the optional `mcp` and `openhands` extras, because three sites lacked the guard used at a dozen others — seven in an unguarded `build_app` test class, two at `openhands` import sites. A permanent red is indistinguishable from a real regression, which made "the suite is green" unusable as evidence. A base install now runs the suite to zero failures, and the nine skip and say why.

### Changed — the tree between releases is stamped `.devN`

**A release tag names a tree, and the commit after it no longer claims the same version.** For one window in 0.4.3 the tag and the commit after it both read `version = "0.4.3"` while that commit had rewritten the `[wrap blocked]` message the shipped hook emits — so `0.4.3` named two different trees, and what a developer cloned was not what an adopter installed, with nothing in the tree saying so. **The release commit is now the last one on its number**; the next commit bumps both stamps (`pyproject.toml` and `levain/__init__.py`) to the next `.dev0`. Asserted by the test suite rather than left to a release checklist — the checklist is what missed it. See *Versioning* at the foot of this file.

### Changed — `[wrap blocked]` no longer names a shell command or a false count

- The episode count is **gone from the message**, not rephrased. `episodes_since_wrap` counts from the last *completed* wrap, so it included episodes frozen inside the open snapshot: measured at 3 reported while 1 was actually waiting. An honest figure needs more qualification than the line can carry.
- Its discriminator was `anneal-memory wrap-status`, a **shell** command — which was the reporter's own finding one layer up, since an agent with no shell was his entire report. It now points at `status`, reachable over MCP.
- Split by caller: a `SessionStart` hook fires before this session ever called `prepare_wrap`, so advice to "compress what prepare_wrap returned" named an artifact it does not have.
- `episodes_since_wrap()`'s docstring no longer justifies itself as "the count-only view for callers that do not give advice" — it has zero in-repo callers and is a compatibility shim for operators carrying local hook edits. It now says so. *(This retracts the rationale given for it under 0.4.2 below; that entry stands as an accurate record of what 0.4.2 shipped.)*
- `verify.py`'s timeout hint and the Codex adapter README both named `episodes_since_wrap` as the per-prompt subprocess site, which is now one delegation away.

### Fixed — tests

- The activation-scope tests read `LEVAIN_SCOPE` and `CLAUDE_CONFIG_DIR` from the **real environment**, so their verdict depended on the reviewer's shell. `LEVAIN_SCOPE=global` — the value `doctor`'s own hint tells operators to set — turned the dark-config regression guard RED, and `CLAUDE_CONFIG_DIR` made four user-wiring guards pass **vacuously**, silently retiring the two-installs-on-one-machine discrimination. One autouse fixture in `conftest.py`; the suite is now identical under all three environments.
- The source-checkout skip guarded one of two sibling tests, eight lines below the comment explaining why it must exist. Hoisted into the shared helper.

### Upgrading

⛔ **`pip install -U levain` DOES NOT REFRESH YOUR INSTALL'S ACTIVATION FILES OR SEED.** It never has. The templates ship inside the wheel, but `levain init` COPIED them into your install when you created it, and no upgrade path rewrites those copies — `levain update` reconciles the memory library and surfaces changes for review, it does not apply them. This is the single most-repeated cause of "I upgraded and nothing changed" in this project's history.

**What you get from `pip install -U levain` alone:**

- every `doctor` fix above — the user-level wiring detector, the pack-hook staleness fix, the corrupt-lock refusal;
- both Content-Length fixes in `levain serve` and the onboarding server;
- the `--consolidate-max-seconds` message fix;
- the job-store lease behaviour change.

**What additionally requires `levain init --force`** (it replaces the activation tree, backing up your edits to `posture.md` and `recency_directives.md` first, and preserves your anneal-memory store):

- the reworded `[wrap blocked]` hook message;
- the seed's uncapped graduation ladder — **relevant if your entity was scaffolded before this release**, since it was being taught a ladder that stopped at 3x while the library had removed the ceiling;
- the corrected Codex adapter README and operator manual.

⚠ **Run `levain doctor` after upgrading.** If you wired Levain's hooks at the user level with a `~` or `$HOME` path, this release is the first one that can SEE that — so a `doctor` run that passed before may now correctly FAIL with `activation scope`. That is not a regression: it means your activation layer has been silently off in those sessions and `doctor` previously could not tell you.

## [0.4.3] — 2026-09-02

**`doctor` no longer fails a correct install rooted at `$HOME`.** A one-line fix to a defect that shipped in 0.4.2, found by review within the hour.

### Fixed — the install's own settings file was read as user-level wiring

For an install rooted at `$HOME`, `<install>/.claude/settings.json` and `~/.claude/settings.json` are **the same file** — the one `levain init` wrote. `_check_activation_scope` read it as a deliberate operator choice, reported the activation layer dark, and made `levain doctor` exit nonzero on an install that was working perfectly.

⚠ **This is the third time `doctor` has gone red for a whole class of operators**, and all three are one mistake: confusing the file the *installer* wrote with a signal of operator *intent*.

- **0.4.0** — `_check_hook_freshness` compared every install against the Claude Code tree, so every Codex install failed on a false "stale hooks" report.
- **0.4.2's first draft** — the new scope check treated `~/.codex/hooks.json` as user-level wiring, when Codex has no per-project hooks file and `levain init` writes that path itself. Caught before release by running `doctor` against a real install of each adapter.
- **This one** — the Claude Code equivalent, which that adapter-by-adapter check did not reach because it needs an install at a specific *location* rather than a specific adapter.

The function's own docstring already named the discriminator (*"`levain init` writes `<install>/.claude/settings.json`, INSIDE the install"*); the code simply never tested that identity. It does now, and a genuinely dark configuration still fails.

### Fixed — the 0.4.2 hook fix had no test at any of its four call sites

Not a shipped defect — the code was correct — but the reason it could stop being correct without anyone noticing. The suite covered `wrap_state()` and `format_wrap_blocked()` as units and nothing covered the **routing** between them, which is the entire finding. Replacing the call sites with `state = (hook.episodes_since_wrap(), False)` reintroduces the original bug completely and leaves the full suite green.

That is the same structural failure that let the bug ship in the first place: the function under the activation layer having no test that runs it. Four parameterised tests now drive each hook's `main()` on **both** adapter trees and assert what the entity is actually told.

## [0.4.2] — 2026-09-02

**Four field-reported defects from one adopter, all in the activation layer.** Reported by [Alex De Groodt](https://github.com/Hurleveur) on 2026-08-04 against 0.4.1. No API removals. Default behaviour is unchanged for every install that does not opt in.

Requires **anneal-memory >= 0.9.8** (up from 0.9.7) — see *Upgrading* below.

### Added — activation scope: an explicit global opt-in

The activation hooks fire only for sessions whose working directory is inside the Levain install. That default is deliberate and is **not changed here**: it is what stops an unrelated session in another codebase inheriting this partnership's posture, recency directives and memory surfaces.

What was missing was any way to *say* you wanted the other behaviour. An operator who moves Levain into a global tool and wires the hooks at user level works in project directories all day, and every one of those sessions sat outside the install — so posture injection, recency directives, spore surfacing, crystallized-pattern recall and the wrap nudge were **all silently off**, while `levain doctor` reported the install healthy.

Two channels, both new:

- `.levain/config.json` → `{"scope": "global"}` — durable, survives `levain update`, and visible to `doctor`.
- `LEVAIN_SCOPE=global` — a per-session override that wins over the config.

**Fail-closed on the contamination axis:** anything that is not exactly `global` (absent, misspelled, wrong type, malformed JSON, unreadable file) resolves to install scope. Silently staying scoped is the status quo; silently going global leaks a partnership's posture into someone else's workspace, so an unreadable config must never be the thing that opens the gate. `LEVAIN_HOOK_SUPPRESS=1` still wins over both.

Applied to **both** adapter copies of the hook — Claude Code and Codex — and the shared surface is pinned byte-identical by test, because the two copies have drifted before.

### Fixed — the wrap nudge told you to run the one call that could not work

`episodes_since_wrap()` fetched anneal's full `status --json` and **discarded `wrap_in_progress`**. With a wrap left open by an earlier session, `prepare_wrap` can only raise — and Layer D plus the `[wrap check]` line went on advising "run prepare_wrap" every single prompt while episodes piled up behind it. The reporter sat at 29, then 31 episodes against a threshold of 12, for three days.

This was a **routing defect, not a missing feature**: `dashboard.py` reads that field at three sites and `tui.py` reads it too. Levain already knew. The one consumer that fires on every prompt was the one dropping it.

- New `wrap_state()` returns `(episodes_since_wrap, wrap_in_progress)` from a **single** `status --json` call — the nudge runs inside a tight per-prompt timeout budget, so re-fetching a field already in hand would double the subprocess cost of every prompt.
- All four advice sites (`session_start` and `user_prompt_submit`, on both adapters) now branch on it and emit `[wrap blocked]` instead, which names both ways out: finish the wrap with `save_continuity`, or abandon it with the `wrap_cancel` MCP tool / `anneal-memory wrap-cancel`.
- `episodes_since_wrap()` is retained as the count-only view for callers that do not give advice.
- A missing or non-bool `wrap_in_progress` degrades to `False` rather than failing the whole read, so an older anneal still gets the ordinary nudge.

### Fixed — `doctor` never mentioned the activation gate

A green `doctor` was compatible with the entire activation layer being off, because every static check reads **files** — hooks present, wired to this install, python resolvable, store open — while what actually decides whether a hook emits anything is a **runtime gate** doctor did not report on.

New `activation scope` check:

- reports the configured scope, and where hooks fire, in every run;
- **FAILS** on the one combination that is genuinely dark: install-scoped, while this install's hooks are wired from `~/.claude/settings.json` — which for a Claude Code install is something the operator did deliberately, since `levain init` wires `<install>/.claude/settings.json` instead;
- **never fails a Codex install for its global hooks.** Codex has no per-project hooks file, so `levain init --adapter codex` writes `~/.codex/hooks.json` itself; treating that as operator intent would fail every Codex install in existence. Codex installs get a fuller report instead, naming the condition explicitly: the hooks run in every Codex session and stay silent outside the install;
- treats user-level wiring as correct once global scope is opted into.

Deliberately narrow and purely additive. It does not touch `_check_hook_freshness` or anything else the pending doctor redesign owns.

> The first draft of this check **did** fail every Codex install — the same shape as the 0.4.0 defect below, in the release built for the same reporter. The suite was green for it both times. What caught it was running `levain doctor` against a real install of each adapter before tagging.

### Fixed — the README misstated the anneal-memory pin

It claimed `>=0.9.6` while the package declared `>=0.9.7`. Corrected, and now **asserted by test** against `pyproject.toml` and `KNOWN_GOOD_ANNEAL` — the same class of defect as everything else in this release (a description that disagrees with correct code), so the fix is an assertion rather than a corrected number.

### Fixed — defects found by review INSIDE this release

Every item below is a bug in code written earlier in this same release, caught by the review mesh before it shipped:

- **The hooks gained a new way to go silently dark.** A pack may layer its own `_levain_hook.py` over the base activation tree, so a 0.4.2 entry point can end up composed against a pre-0.4.2 helper with no `wrap_state`. The AttributeError was swallowed by the structural fail-open catch and every hook emitted **nothing**. The entry points now feature-detect and degrade to the old count-only nudge. Shipping a fresh silent-dark path in the release whose whole purpose is ending one is not a trade worth making for a shorter call.
- **`[wrap blocked]` told the agent cancelling was free.** It said "Nothing is lost either way" while recommending `wrap_cancel` — but anneal runs one server process per client session against a shared store, so the wrap may belong to a **live** sibling mid-compression, and cancelling discards its work. The line now says whose wrap it may be, tells the agent to check `anneal-memory wrap-status` first, and says plainly what cancelling destroys.
- **`doctor` asserted an activation scope it could not verify.** `LEVAIN_SCOPE` was ignored on the grounds that doctor's environment is not the session's. That holds for a per-invocation override and breaks the moment it is exported from a shell profile — misreporting in both directions, including a hard FAIL on an install that was working fine. Doctor now names the variable and the ambiguity instead of asserting past it, and no longer fails an install whose gate the environment has opened.
- **`doctor` crashed on a malformed foreign config.** `{"hooks":{"SessionStart":[null]}}` raised an uncaught `AttributeError` that aborted the whole run — breaking the function's own documented promise never to hard-fail on someone else's file.
- **User-level wiring detection missed real cases:** it inspected only the first command found (so a foreign hook registered ahead of Levain's hid it), ignored `CLAUDE_CONFIG_DIR`, and treated `${CLAUDE_PROJECT_DIR}` wiring as pointing at this install when at user level it resolves to whatever project is open.
- **A stale `AGENTS.md`** left behind by an adapter switch produced a Codex diagnostic on a Claude Code install; the check now uses the shared `effective_adapter` classifier.

### Also in this release — work that accumulated since 0.4.1

⚠ **0.4.2 is not only the four items above, and saying otherwise would be the same defect they describe.** The `v0.4.1` tag is old; a month of work sat on `main` behind a deliberate decision not to cut a release for it (recorded at the time in *"Record what is fixed, what is open, and why there is no 0.4.2"*). All of it ships here:

- **`doctor` hook-freshness, closed on both axes** — the check now works in relative-path space and iterates the union of keyspaces, and asks the manifest which hooks a pack owns. This was the fourth recurrence of one bug shape; a fixture that had been missing three times now exists.
- **SSH vectors denied at every spelling**, not just the resolved one (`levain/firing/confinement.py`).
- **Five findings closed** from the spore-604 bugfix session.
- **The anneal floor moved to `>=0.9.7`** (AM-LEVELCAP) before this release moved it again to `>=0.9.8`, and compat fixtures now derive the version instead of hard-coding it.
- **The seed no longer teaches crystallize-OUT** while the level ladder is capped.
- Routine routing of the nightly reviewer's findings into project memory (documentation only; no shipped code).

*(This section was added after publication. The `0.4.2` sdist on PyPI carries the release notes without it — the omission was in the notes, never in the code.)*

### Upgrading

- **`pip install -U levain` will also upgrade `anneal-memory` to >= 0.9.8.** This is a real API contract, not a lockstep bump: the new `[wrap blocked]` advice names the `wrap_cancel` MCP tool, which does not exist before anneal 0.9.8. Against 0.9.7 the hook would name a tool the agent cannot reach — precisely the defect this pair of releases exists to fix.
- **If you wired Levain's hooks globally and your activation layer has been quiet, this is why.** Add `{"scope": "global"}` to `.levain/config.json`, then run `levain doctor` — it now tells you which way it is set.
- **If you are carrying a local patch to `in_install_session()`, drop it.** What kept eating it is `levain init --force` — step two of the documented upgrade procedure (`pip install -U levain`, then `levain init --force`), which replaces the whole `activation/` tree. `levain update` was never the culprit: it writes nothing under `activation/` at all. The supported opt-in lives in `.levain/config.json`, which neither command touches.

## [0.4.1] — 2026-08-02

**`doctor` is no longer permanently red for Codex operators.** `_check_hook_freshness` compared every install's hooks against the Claude Code template tree, so every Codex install failed on a false "stale hooks" report. It shipped in 0.4.0 — the release built to carry this same reporter's previous fixes.

## [0.4.0] — 2026-08-01

The governed seat, the efferent gate, and three field-reported fixes.

> ⚠ **UNDOCUMENTED AT THE TIME, AND IT CHANGED DEPLOYMENTS SILENTLY.** This release deleted the hook's `$CLAUDE_PROJECT_DIR` read. That read's containment check accepted **any ancestor of the hook file** as the install root, which could resolve the whole activation layer under the wrong root — so an install with globally-wired hooks may have been passing the session gate *by accident*. Tightening `install_root()` to derive from the hook file's own location was correct, but for anyone in that position it turned activation off, with no note in any release material saying so and `doctor` still reporting healthy. **0.4.2 is the release that gives that operator a supported way to get the behaviour back** (`scope: "global"`), and a `doctor` check that says which way the gate is set.

---

## Versioning — why the tree is stamped `.devN` between releases

A release tag names a tree. The commit *after* it does not, and for one window in 0.4.3 both read `version = "0.4.3"` while the second had rewritten a shipped hook message — so "0.4.3" named two different trees, and what a developer cloned was not what an adopter installed, with nothing in the tree saying so.

**The rule: the release commit is the last one on that number.** The next commit bumps both stamps (`pyproject.toml` and `levain/__init__.py`) to the next `.dev0`, and its changes go under `## [Unreleased]`. Cutting a release drops the suffix in the same commit that tags it. This is asserted by `tests/test_manifest.py::TestReleaseStampIsNotAPublishedVersion`, not left to the release checklist — the checklist is what missed it.
