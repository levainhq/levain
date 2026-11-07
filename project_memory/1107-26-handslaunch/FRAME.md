# FRAME: the hands chat launch seam for `prepare_launch(held=True)` (lane seat/1107-26-handslaunch)

Base origin/seat/1107-20-m2 67320847. Research only. **No code: there is no single seam to wire into.**

## Verdict
Nothing in the tree starts a Claude or Codex chat as the hands user. The launcher would have to be built from
scratch, so per the brief this lane stops at this frame. The L3 HIGH ("a revoked server stays usable after an
earlier manual `levain hands mcp prepare` until a launcher refreshes the files") stays open by design, and it can
only close in the slice that builds the launcher.

## Where a launch happens today (every vendor-spawn path, checked against disk)
| Path | What it spawns | uid | MCP config | Hands? |
|---|---|---|---|---|
| `levain/session.py` (`EntitySession`, ~L851-1075) | OpenHands SDK in-process; bash tool via `hands_prefix` | hands, **bash only** | none (no vendor CLI) | holds `hold_session_lock` (SHARED) + `hold_hands_session` (EXCLUSIVE, one session per entity) |
| `levain/coordinator/room_launch.py:780` -> `claude_session.room_argv` | `claude -p` stream-json (the room) | operator | its own `--strict-mcp-config --mcp-config <run_dir>/mcp.json` (the one door) | no; by ruling "Chat A: full mind, no hands, one door" (11-01) |
| `levain/coordinator/launch.py` `_harness()` | the operator's `claude` worker | operator, `child_env(seat="attended")` | operator's own config | no |
| `levain/coordinator/codex_launch.py` | attaches to the operator's Codex app-server socket | operator | operator's `~/.codex/config.toml` | no |
| `levain/chat_driver.py` `ChatEgressService.launch` / `ChatLaunch.attach_cli` | chat egress + a CLI leader for `claude-code`/`codex` | (not wired) | n/a | no production caller at all |
| `levain/host_guard.py` `CCHostProfile.argv()`, `seat_profile.py`, `slot/start.py`, `dream/executor.py` | canary / seat / slot / dream children | operator | strict, empty or fixed | no |
| `levain/firing/hands_login.py`, `hands_mcp_login.py` | `claude`/`codex` **login, status, logout, `claude mcp add/remove`** as hands | hands | n/a | the only hands vendor spawns; none is a chat |

`git grep -n "_launch_prefix\|claude_chat_argv\|codex_chat_argv" -- levain` confirms: the argv builders have no
caller; `_launch_prefix` is used only by login/status/logout/mcp verbs. `1103-27/MCP_HANDS_RESEARCH.md` L20-24 and
`1104-8/RETURN_hmcp.md` L41 ("M2 (hands-uid chat launch ... waits on RM-G)") say the same; RM-G landed as part of the
operator-uid room, not a hands chat.

## The seam, as the launcher slice must build it (contract, not code)
One function, the only path that execs a hands vendor chat, in this order:
1. `hold_hands_session(entity_dir)` (EXCLUSIVE, one hands session per entity) and `hold_session_lock(entity_dir)`
   (SHARED), exactly as `EntitySession` does. The exclusive one matters here: `held=True` skips prepare's own
   exclusive `hands_lock`, and the session lock alone is SHARED, so two launchers could otherwise each read the
   registry and commit in either order (a launcher that read before a revoke committing after one that read after
   it). The one-session lock serialises preparers.
2. `prepare_launch(hands, entity_dir, cockpit_root, held=True)`, **immediately before** the exec, with nothing in
   between that could let the files be rewritten (the hands account itself cannot run yet: no hands process is
   alive, `_stop_hands_user` has run).
3. Only on a normal return: `Popen(claude_chat_argv(hands, ...))` / `codex_chat_argv`.

**Refusal path (fail closed):** any `HandsMcpRefused` or `HandsMcpError` (and `hl.HandsLoginError`, `OSError`,
`SubprocessError`, which `cmd_prepare` already treats as failures) ends the launch with the message, releases both
locks, and starts no vendor. A partial prepare (`HandsMcpError` naming one replaced file) is also a no-launch; the
next launch rewrites both from the registry. Never fall back to "launch with the previous files": that is the
exact HIGH.

**Tests the launcher slice owes** (from a reproduced failure): register A and B, prepare, revoke A, launch -> the
launch files read at spawn time contain B and not A; prepare made to refuse (e.g. a foreign `[mcp_servers.x]` in
config.toml, or an unreadable authority log) -> the fake vendor spawn is never called and the locks are released;
mutation: move the prepare after the spawn, or swallow the exception -> red.

**What launch-time prepare still does not close (state it in the slice):** a revoke landing while a hands chat is
running does not reach that chat (the vendor read the file at start; a stdio server is already running). Closing
that needs either the session to end on revoke or the revoke path to stop the hands session. Also: the registry
revoke is written to the cockpit authority log without taking the entity's hands lock, so "a revocation lands before
the read or after the write" holds per launch only in the sense that each launch re-reads; the mid-session case is
the real residue.

## Open design question: should the hands Claude chat run `--strict-mcp-config`? (a note, not a block)
**Recommendation: yes, strict, and keep it strict.** `claude_chat_argv` already does so.

Why:
- Without strict, Claude also loads servers from the hands `~/.claude.json` (user and local scope) and from a
  project `.mcp.json` in the cwd. The cwd is the hands workspace, which the hands agent and any cloned repository
  write. So a planted `.mcp.json`, or a server the hands chat adds to its own `.claude.json` mid-session, would run
  at the next launch outside the registry: self-granting, and the same class as the open HIGH (a server live that
  the registry does not hold). Strict makes the registry, rendered at launch, the only source; that is the
  invariant "the registry governs hands MCP servers" made structural rather than checked.
- Codex has no strict flag, which is why M2 refuses any foreign `[mcp_servers.*]`; strict on Claude gives Claude
  the same registry-only property for free, and keeps the two vendors' rules alike.
- Every Levain-contained Claude child already runs strict (room, seat profile, slot, dream, host canary); the
  operator's own worker (`coordinator/launch.py` `_harness()`) does not, by design, since it is his config. A hands
  chat is a contained child, not his; without strict it would be the one contained exception.

The strongest case against: M3 signs a server in through `claude mcp add` as hands, which records it in
`.claude.json`; under strict that definition is ignored and the server comes from `levain-mcp.json` instead. If
Claude keys the stored OAuth token (`.credentials.json`) by something scope-dependent, a server declared via
`--mcp-config` might not find M3's token and would re-prompt or fail. That is M2 Residue 2 and needs the VM with a
real signed-in hands account. **If that measurement fails, fix M3's token placement (or sign in against the
launch-file definition), not the strict flag**: dropping strict to make sign-in work re-opens the self-grant path.

## Built (same lane, second commit) and what remains
The Verdict above was the research commit; the launcher was then built in this lane.

**Built.** `levain/firing/hands_chat.py` `launch_hands_chat(entity_dir, vendor, cockpit_root, spawn=, emit=)`, the one
path that starts a hands vendor chat, exposed as **`levain hands launch claude|codex`** (named `launch` under `hands`
so it is not `levain chat`, the chat-network keys verb, nor the room; its help says "the hands-uid chat ... Not the
cockpit room"). Order: `hold_session_lock` (shared) then `hold_hands_session` (exclusive), the same order
`EntitySession` takes them -> `hands_live.sweep_hands_user` (refuses if anything of the hands user is left, so nothing
of that account can rewrite its own launch files between prepare and start) -> `prepare_launch(held=True)` -> the
vendor from `claude_chat_argv` (strict kept) / `codex_chat_argv`, cwd the hands workspace, through an injectable
`spawn(argv, cwd)` -> wait (Ctrl-C belongs to the vendor) -> sweep the hands user again -> both locks released in a
`finally`. Any `HandsMcpError` (incl. `HandsMcpRefused` and a partial prepare), `HandsLoginError`, `WsGitError`,
`OSError` (incl. a failed spawn), `SubprocessError` or a sweep that leaves processes -> `HandsChatRefused "...; no
chat was started"`, nothing spawned, locks released, no fallback to earlier files. **No vendor argument passes
through** (an added `--mcp-config` or Codex `-c mcp_servers...` would add a server outside the registry). Not wired
into the room or any other path. `hands_mcp_login.py`'s "not a launch yet" comment now names the launcher.

**Tests** (`tests/test_hands_chat_launch.py`, 11, fake vendor reading both files at spawn): (1) A and B registered,
manual prepare, A revoked: the stale files still hold A (the reproduced HIGH), the launch's vendor sees only B in both
files, for claude and codex; (2) foreign Codex server / unreadable log / partial prepare / stale hands process / spawn
OSError -> vendor never started, both locks free after; an open session refuses before any sweep or prepare; the verb
prints and exits 1; (3) events exactly sweep, prepare, spawn, sweep, each with both locks held. Mutations, each red then
restored: prepare moved after the spawn (7 red), prepare exception swallowed (4 red), one-session lock skipped (2 red),
session lock skipped (1 red), lock release skipped (10 red).

**Remains.**
- Mid-session revoke: a revoke landing while a hands chat runs does not reach it (the vendor read its file at start; a
  stdio server is already running). Needs the session to end on revoke, or the revoke path to stop the hands session.
- VM: `ps -o uid=` of the spawned child is the hands uid (the 1103-27 DONE line), and an interactive chat actually
  works through the sudo prefix with the terminal inherited (untested here: fakes only). Linux launches are refused
  up front since L3 r1 (below); Linux support is not built.
- M2 Residue 2 (OAuth token found under strict + `--mcp-config`): VM, real hands login.
- M4: doctor comparing the launch files with the registry (drift is invisible until the next launch or prepare).

## L3 round 1 fixes (3 lineages: complement, codex, deepseek; gemini produced nothing)
- **macOS only (codex HIGH, Linux).** On Linux `hands_prefix` runs `_HANDS_START`, which `os.execv`s the bare
  `claude`/`codex` with no PATH search from the hands workspace as cwd: a planted `<workspace>/codex` would run, or
  every launch fails ENOENT. Fixed by deletion: `host_refusal()` refuses any host that is not macOS ("the hands chat
  launch is macOS only in this slice") before a lock, the sweep or prepare.
- **Project layers (codex HIGH).** Codex loads `<workspace>/.codex/config.toml` (its `[mcp_servers]` included) when the
  hands home marks the workspace trusted, which prepare preserves. The launcher now refuses, after the sweep and the
  prepare and before the spawn, when the workspace holds a `.mcp.json`, a `.codex` link, or a `.codex/` entry named
  `config*` or ending `.toml`/`.json`, naming the file; nothing starts, both locks are released.
- **Signals and survivors (codex MED, complement MED).** SIGTERM/SIGHUP handlers are installed for the launch (refused
  outright if they cannot be, off the main thread): the first raises `SystemExit(128 + signal)`, the child is
  terminated (killed after 5 s), the hands account swept, both locks released, handlers restored, then re-raised;
  further signals and SIGINT are ignored during cleanup. Cleanup is `except BaseException` plus `finally`, and the
  sweep runs whenever the spawn was attempted (an interrupt inside the spawn included). Processes left after the chat
  make the command exit non-zero (the vendor's code, or 1 when that was 0) with the warning.

## Known edges
- **Concurrent revoke vs launch.** Authority writers take neither entity lock, so a revoke can land between prepare
  reading the registry and the spawn. That is equivalent to a revoke landing a moment after the launch, which a
  running chat never sees anyway (the mid-session revoke above); closing either needs the revoke path to reach the
  hands session.
- **`TERM=dumb`.** `confinement._hands_env` / `hands_prefix` run the vendor under `env -i ... TERM=dumb`, which may
  degrade or refuse the TUI. Belongs to the VM chat check; `confinement.py` is not edited in this slice.
- **Ancestor project layers.** The project-layer refusal checks the workspace itself only; if Codex walks from the cwd
  up to a project root above the workspace, a `.codex/config.toml` in an ancestor between them is not checked here.
  Unmeasured; the VM chat check can confirm where Codex stops.
