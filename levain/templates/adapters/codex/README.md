# Levain — Codex adapter

The Codex adapter places the methodology-core seed into Codex's context-file
format and wires the two things a Levain entity needs to run: the
**activation hooks** (the posture mechanism, via SessionStart +
UserPromptSubmit) and the **anneal-memory MCP server** (the entity's
memory). Codex exposes both hook events the activation mechanism needs with
`hookSpecificOutput.additionalContext` injection — Stop is *not* wired
because Codex 0.133's `stop.command.output` schema does not accept
`hookSpecificOutput` (Layer D folds into the two wired hooks instead, same
shape as the Claude Code adapter; see *Known characteristics* below).

The adapter is **project-scoped on the install side and global on the
harness-config side**: the install lives inside one directory (the operator's
partnership working directory) and the hooks plus MCP server are wired
relative to it — but Codex's hooks config (`~/.codex/hooks.json`) and MCP
server registration (`~/.codex/config.toml`) are global. At v1, that means
**one Levain Codex install per machine**; multi-install Codex support is a
post-v1 item (see Known characteristics).

**Supported platforms:** macOS, Linux, and WSL. Native Windows is not
supported in v1.

> **This README documents the adapter and a manual install path for
> dogfooding it.** The supported end-to-end install — running an interview to
> fill the seed templates, initializing the store, loading the continuity
> scaffold, resolving the placeholder tokens below — is `levain init` (build
> step 4). Until that ships, the manual sequence here stands up a working
> install.

## Install layout

```
<partnership-directory>/          <- the Levain install; the operator's working dir
├── AGENTS.md                     <- from AGENTS.md.template; instructs explicit seed reads
├── .levain/
│   └── memory.db                 <- the anneal-memory store (created by `init`)
├── seed/                         <- the methodology-core (Levain layer 3)
│   ├── origin.md                 <- filled by onboarding
│   ├── partnership.md
│   ├── world.md                  <- filled by onboarding
│   ├── memory.md
│   ├── continuity.md             <- the entity's starting continuity scaffold
│   └── README.md
└── activation/
    ├── posture.md                <- operator-editable; the primacy posture string
    ├── recency_directives.md     <- operator-editable; the recency directive set
    └── hooks/
        ├── _levain_hook.py        <- shared helpers
        ├── session_start.py       <- SessionStart hook (Layer A + Layer D start-catch)
        └── user_prompt_submit.py  <- UserPromptSubmit hook (Layer B + Layer D nudge)

~/.codex/
├── hooks.json                    <- from hooks.json.template; wires the hooks globally
└── config.toml                   <- gains the [mcp_servers.anneal_memory] block from mcp.template.toml
```

## Template placeholders

Three templates carry placeholder tokens that resolve to environment-specific
values. `levain init` (build step 4) resolves them; for a manual install,
substitute them by hand per the steps below.

| Token | In | Resolves to |
|-------|----|-------------|
| `{{INSTALL_DIR}}` | `hooks.json.template`, `mcp.template.toml` | absolute path of the partnership directory |
| `{{PYTHON}}` | `hooks.json.template` | the working Python 3 interpreter (`python3`, or its absolute path) |
| `{{ANNEAL_MEMORY}}` | `mcp.template.toml` | the `anneal-memory` executable (`anneal-memory`, or its absolute path) |

`{{PYTHON}}` and `{{ANNEAL_MEMORY}}` are environment-dependent on purpose:
a bare command name resolves against `PATH`, and Codex CLI launched from a
GUI app or `cron` inherits a *minimal* `PATH` that often omits `pip --user`
script directories. Resolving them to absolute paths at install time removes
that failure mode.

## What the adapter wires

### 1. Activation hooks — the posture mechanism

See `../../activation_spec.md` for the full design.

- **`session_start.py`** → `SessionStart` (Layer A). Injects, at primacy
  position: the posture string from `activation/posture.md`, and the current
  date/time. Fires on `startup` / `resume` / `clear` / `compact` (the full
  Codex 0.133 source vocabulary). On a genuinely fresh session
  (`source ∈ {"startup", "clear"}`) it also runs the **Layer D start-catch**
  — if the previous session left episodes unwrapped, it flags it. `resume`
  and `compact` are treated as ongoing work and skip the wrap-check.
- **`user_prompt_submit.py`** → `UserPromptSubmit` (Layer B). Injects, at
  recency position before each prompt: one directive selected at random
  from `activation/recency_directives.md`. Once episodes-since-last-wrap
  pass a threshold (default 12) it also appends the **Layer D ambient
  nudge**.

No Stop hook is wired. Codex 0.133's `stop.command.output` schema accepts
`continue` / `decision` / `reason` / `stopReason` / `suppressOutput` /
`systemMessage` — it does *not* accept `hookSpecificOutput.additionalContext`,
the shape the activation mechanism produces. The Claude Code adapter folds
Layer D entirely into SessionStart start-catch + UserPromptSubmit ambient
nudge for an architectural reason (Stop fires *after* the model stops, so
the injection target is structurally absent); the Codex adapter does the
same for a complementary reason (the output schema simply does not accept
the shape). Wrap discipline ultimately stays model-driven via the seed's
`memory.md` instructions, structurally unenforced by the harness on session
end.

### 2. Memory — the anneal-memory MCP server

`seed/memory.md` instructs the entity to operate its memory through an MCP
server named **`anneal_memory`**. The `[mcp_servers.anneal_memory]` block in
`~/.codex/config.toml` registers it. The server name is load-bearing —
`seed/memory.md` and the `anneal://continuity` resource resolve only if the
server is registered under exactly that name. `mcp.template.toml` ships the
correct registration; do not rename the server.

The store is pinned to **`.levain/memory.db` inside the install** — see
*Store scoping* below.

### 3. Hooks-config global wiring

`~/.codex/hooks.json` (the **sidecar** JSON file) registers the two hooks.
Codex's inline `[[hooks.X]]` TOML form in `~/.codex/config.toml` was
empirically observed to fail to parse in codex-cli 0.132.0 ("invalid type:
map, expected a sequence in `hooks`"). The JSON sidecar at
`~/.codex/hooks.json` works without any explicit feature flag.

## Prerequisites

- **`anneal-memory`** installed (`pip install anneal-memory`). Both the MCP
  server and the hooks' Layer D wrap-check invoke it. v1 is built against
  anneal-memory v0.3.x; the shipped Levain package will pin a compatible
  range.
- **Python 3** available. The hook commands invoke it (`{{PYTHON}}`). If no
  working interpreter is wired, the hooks silently do not run and the
  posture mechanism is off — verify with `python3 --version` first.
- **Codex CLI** installed (`codex --version`). v1 is tested against
  codex-cli 0.132.0+.

## Install (manual — `levain init` automates this at build step 4)

1. **Install anneal-memory** and confirm it runs: `anneal-memory --help`.
   Note its absolute path (`command -v anneal-memory`) and your Python 3
   path (`command -v python3`).
2. **Place `seed/`, `activation/`, and `AGENTS.md`** (from
   `AGENTS.md.template`) in the partnership directory.
3. **Wire the hooks globally:** copy `hooks.json.template` to
   `~/.codex/hooks.json`, replacing `{{PYTHON}}` with your Python 3 path
   from step 1 and `{{INSTALL_DIR}}` with the **absolute path** of the
   partnership directory. If `~/.codex/hooks.json` already exists, do not
   overwrite — merge: append the `SessionStart` / `UserPromptSubmit`
   entries into the existing `hooks` object's arrays (each event's value
   is an *array* of matcher groups; appending a group is safe, replacing
   the array is not). Both `{{PYTHON}}` and `{{INSTALL_DIR}}` placeholders
   are shell-quoted in the template, so paths containing spaces work
   correctly.
4. **Register the MCP server:** append the contents of `mcp.template.toml`
   to `~/.codex/config.toml`, replacing `{{ANNEAL_MEMORY}}` with the
   anneal-memory path from step 1 and `{{INSTALL_DIR}}` with the absolute
   install path. **Idempotency note:** if `[mcp_servers.anneal_memory]`
   already exists in `~/.codex/config.toml` (e.g. from a prior install
   attempt), replace its body in place — do *not* append a duplicate
   section header, which produces a TOML parse error on Codex startup.
5. **Initialize the memory store:**
   `anneal-memory --db <absolute-install-path>/.levain/memory.db init`
6. **Make the hooks executable:** `chmod +x activation/hooks/*.py`
   (optional — the wired command invokes the interpreter explicitly — but
   good hygiene).
7. **Start a Codex session** in the partnership directory (`cd
   <partnership-directory> && codex`).

`levain init` (build step 4) automates steps 2–6, resolves the placeholder
tokens, fills `world.md` / `origin.md` from an interview, and loads
`seed/continuity.md` into the store. In this manual path the entity starts
from an empty continuity — the expected near-empty start anyway
(`seed/memory.md`: "early on it will be near-empty").

## First-run approvals

Codex shows trust / approval prompts on first run:

- **Project trust** — the partnership directory may need `trust_level =
  "trusted"` set in `~/.codex/config.toml` (Codex prompts the first time
  you `cd` in and run `codex`).
- **Hook trust (LOAD-BEARING)** — Codex maintains per-content-hash trust
  for hook scripts. The first interactive `codex` invocation that
  encounters configured hooks prompts the operator to trust each one.
  **Hooks will not fire until trust is established.** Once established,
  trust persists for that exact script content; editing the hook script
  silently invalidates trust until re-approved. This is a Codex platform
  behavior, not a Levain-specific gate. Implications:
    1. **Run interactive `codex` once before any `codex exec` invocation**
       to establish trust — `codex exec` does not surface trust prompts and
       will silently skip untrusted hooks.
    2. **Re-trust after edits.** Any edit to `posture.md` /
       `recency_directives.md` is *content-only* and does not affect hook
       trust (the hook scripts themselves are unchanged). But editing
       `_levain_hook.py` / `session_start.py` / `user_prompt_submit.py`
       invalidates trust until re-approved interactively.
    3. **`--dangerously-bypass-hook-trust`** is documented to bypass the
       trust check for one invocation, but empirical observation
       (Argus deployment, codex-cli 0.132.0) shows it does **not
       reliably restore firing under `codex exec`** in all cases. Best
       defense is interactive trust establishment.
- **MCP server** — first session may prompt to approve the `anneal_memory`
  server registration; approve it or the entity has no memory.

## Verify the install

Hook-injected context is **not visible in the Codex UI** — and every failure
mode here is silent. Verify the *actual* runtime, not a proxy:

- **Activation:** start an **interactive** `codex` session (`cd
  <partnership-directory> && codex`) and ask the entity *"what posture
  instructions and what date were injected into your context this
  session?"* — it should quote the posture string and today's date back.
  Interactive mode is the reliable path because (a) it surfaces hook trust
  prompts that `codex exec` does not, and (b) Argus empirical observation
  (codex-cli 0.132.0) is that interactive-session hooks fire reliably
  while `codex exec` is intermittent. If activation works interactively,
  the templates are wired correctly; subsequent `codex exec` reliability
  is a Codex platform reliability question, not an adapter issue.
- **Seed load (separate from activation):** activation tests the hook
  injection path; this tests the AGENTS.md instruction-following path,
  which is the C layer (the four seed files becoming the entity's
  identity). Ask the entity to quote a specific line from `world.md` (e.g.
  the operator's name or a stable role detail). If the entity can quote
  it accurately, AGENTS.md was followed and the seed loaded. If not,
  AGENTS.md was skipped — debug whether AGENTS.md exists in the install
  root, whether the seed files exist where AGENTS.md says they do, and
  whether the entity's first-prompt pressure overrode the read-first-
  before-doing-any-work instruction. The activation-quote check above
  tests the hook path independently; this seed-quote check tests the
  identity-load path independently; both passing is the full A + C
  verification.
- **Memory:** from inside that session, ask the entity to record an episode
  and then recall it. If the `anneal_memory` MCP server failed to register,
  the entity will report its memory tools are unavailable — test this
  in-session, not via `anneal-memory` in a terminal (a terminal has a
  different `PATH` than a GUI-launched Codex, so a terminal check can pass
  while the real MCP spawn fails).
- **Manual hook smoke test (substrate-independent):** to verify a hook's
  *wiring is correct* without depending on Codex to invoke it, run each
  hook script standalone from inside the install:
  ```
  cd <install-dir>
  echo '{"source":"startup"}' | python3 activation/hooks/session_start.py
  echo '{}' | python3 activation/hooks/user_prompt_submit.py
  ```
  Each should emit a single line of `hookSpecificOutput` JSON. Empty output
  means the `should_fire()` install-scoping gate is failing (cwd not inside
  install root) or the activation file (`posture.md` /
  `recency_directives.md`) couldn't be read. This validates the *script
  half* of the contract independently of whether Codex invokes them. A
  future `levain doctor` / `levain verify hooks` command will automate this
  with expected-side-effect checks; until then, the standalone invocation
  is the canonical verification path.
- **Stranger-install end-to-end verification (recommended for new
  deployments):** the canonical install-verification recipe — proven
  against a vanilla Codex install on a separate host — uses `CODEX_HOME`
  isolation so the verification runs against a clean `.codex/` state
  without intercepting from the operator's main Codex install:
  ```
  # On the destination host (operator's machine OR a separate verification host).
  # Use ~/levain-verify-install — operator-private, mode 700 — not /tmp,
  # which is world-readable on most Unix systems and would leak the
  # auth.json tokens copied below.
  VERIFY=~/levain-verify-install
  mkdir -p "$VERIFY" && chmod 700 "$VERIFY"

  rsync -avz --exclude='.codex/state*' --exclude='.codex/logs*' \
    --exclude='.codex/sessions' --exclude='.codex/.tmp' \
    <levain-install-source>/ "$VERIFY"/

  # Patch absolute paths for destination machine (portable across
  # macOS BSD sed and GNU sed by using Python instead — BSD sed's
  # `-i` syntax differs from GNU's, so `sed -i 's|...|...|'` is not
  # portable):
  python3 -c "
  import sys, pathlib
  src_anneal, dst_anneal, src_root, dst_root = sys.argv[1:5]
  for p in (pathlib.Path(dst_root) / '.codex' / 'config.toml',
            pathlib.Path(dst_root) / '.codex' / 'hooks.json'):
      t = p.read_text()
      t = t.replace(src_anneal, dst_anneal).replace(src_root, dst_root)
      p.write_text(t)
  " <source-anneal-path> <destination-anneal-path> <source-install-root> "$VERIFY"

  # Replace auth tokens (Codex auth.json is machine-bound), mode 600:
  install -m 600 ~/.codex/auth.json "$VERIFY"/.codex/auth.json

  # Run isolated from operator's main Codex install:
  cd "$VERIFY"
  CODEX_HOME="$VERIFY"/.codex codex exec \
    --dangerously-bypass-approvals-and-sandbox \
    --dangerously-bypass-hook-trust \
    --skip-git-repo-check \
    "<verification prompt — e.g. ask the entity to identify itself, quote a line from world.md (seed-load check), record + recall an episode, and quote any injected posture/recency text>"
  ```
  `CODEX_HOME` isolation matters — without it, the operator's main Codex
  install state intercepts and the verification reads/writes the wrong
  store. Verified working on codex-cli 0.132.0 against `argushub`.

## Store scoping

The anneal-memory store is pinned to **`.levain/memory.db` inside the
install** — by `mcp.template.toml` (an absolute path) and by the hooks
(`_levain_hook.store_path()`, install-relative). This is deliberate:
anneal-memory's *default* store is a single machine-global file
(`~/.anneal-memory/memory.db`); if two Levain installs both used the
default they would share one episode store and one continuity — **two
entities' memories, and identities, silently merged**. Install-relative
scoping is structural.

**Moving the install** breaks this: the hooks self-locate and follow the
move, but `~/.codex/config.toml`'s MCP server registration holds an
*absolute* store path that goes stale, and `~/.codex/hooks.json` holds an
absolute `{{INSTALL_DIR}}` in the hook command. After moving the directory,
re-run `levain init` (or hand-edit both files) — or the MCP server and the
hooks will read different stores, and the hooks may not fire at all.

## Known characteristics

- **Codex hook reliability under `codex exec` is a Codex platform issue,
  not adapter-shaped.** Three independent test contexts confirm this on
  codex-cli 0.132.0: (a) laptop zero-trust install (hooks never fired),
  (b) argushub stranger zero-trust install (hooks never fired despite
  identical adapter file shape and `--dangerously-bypass-hook-trust`),
  (c) argushub Argus-production trust-established install (hooks fired
  intermittently — SessionStart confirmed firing via `state.json` mtime
  updates 3x in one day, then failed on subsequent invocations with no
  config change). The `--dangerously-bypass-hook-trust` flag is documented
  to bypass trust but empirically does not reliably restore firing under
  non-interactive `codex exec`. Related upstream: [GH issue
  #17532](https://github.com/openai/codex/issues/17532) (`codex_hooks do
  not fire in interactive sessions when configured via repo-local
  .codex/config.toml` — adjacent scope, same class of silent-skip bug).
  Layer A (posture) and Layer C (seed reading via AGENTS.md instruction)
  are reliable; MCP wiring is reliable; Layer B + Layer D ship
  file-correct but operators design around the platform unreliability.
  Implications for operators:
    1. **Autonomous workloads** (scheduled tasks, batch runs) that invoke
       `codex exec` cannot rely on Layer B recency injection or Layer D
       ambient nudge / start-catch firing reliably. The anneal-memory
       wrap obligation is structurally documented in `seed/memory.md` and
       enforced model-side; hook reinforcement is best-effort, not a
       guarantee.
    2. **Interactive sessions** are the canonical surface for the
       activation mechanism. The architecture continues to work — primacy
       loading via AGENTS.md instruction-following is reliable, MCP
       record/recall is reliable, and interactive hooks fire reliably
       once trusted — but operators running pure-exec workflows should
       design around the limitation rather than expect hook coverage.
    3. **Trust hash invalidation on hook edits.** Editing the hook
       *scripts* (`_levain_hook.py` / `session_start.py` /
       `user_prompt_submit.py`) invalidates trust silently; re-trust
       requires interactive `codex` invocation. Editing the *activation
       files* (`posture.md` / `recency_directives.md`) does not affect
       trust — those are read by the hook scripts at runtime, the script
       content is unchanged.
- **Codex Stop output schema does not accept `hookSpecificOutput`** —
  surfaced by L3 cross-substrate review (codex agent reading Codex 0.133's
  embedded schema). `stop.command.output` accepts `continue` / `decision`
  / `reason` / `stopReason` / `suppressOutput` / `systemMessage` only.
  This means: even if Codex platform hook reliability is fixed upstream,
  a Stop hook emitting `hookSpecificOutput.additionalContext` would be
  parsed as invalid output, not injected as context. The adapter therefore
  excludes Stop entirely — Layer D folds into SessionStart start-catch
  (`source ∈ {"startup", "clear"}`) + UserPromptSubmit ambient nudge
  (episodes-since-wrap ≥ threshold), same shape as the Claude Code
  adapter. Verifying whether `decision: block` + `reason` or
  `systemMessage` would route the wrap reminder back into the model's
  context is a v1.1 investigation; requires interactive Codex with stable
  hook reliability to test.
- **One Levain Codex install per machine at v1.** `~/.codex/hooks.json` and
  the MCP server registration are global. Multi-install Codex support
  requires either per-project Codex hook scoping (if Codex adds that) or a
  Levain-side install-selector — both are post-v1 items.
- **Codex hook inline-TOML form fails in v0.132.0.** The JSON sidecar at
  `~/.codex/hooks.json` is the working shape. If a future Codex version
  fixes inline TOML parsing, the adapter can move the wiring into
  `config.toml` directly.
- **Hooks are sandboxed against `/tmp` writes.** Hook scripts can run
  subprocesses (the Layer D `episodes_since_wrap` query works) but cannot
  write directly to `/tmp/*.log` from inside the hook process. Use
  `$HOME/...` paths for any diagnostic logging the operator adds.
- **`user_prompt_submit.py` runs `anneal-memory status` once per prompt** to
  read the unwrapped-episode count for the Layer D nudge. anneal-memory's
  store is WAL-mode, so this read does not block on the MCP server's
  writes — the cost is process-spawn overhead, fast on a normal store. The
  per-prompt path uses a tight 2s query timeout so a hung anneal-memory
  cannot stall a turn; it is fail-open (no count → no nudge).
- **`LEVAIN_HOOK_SUPPRESS=1`** — if you later build tooling that itself
  launches Codex from *inside* the install (a consultation runner, a batch
  job), set this in that tooling's environment so the activation directives
  do not leak into contexts that must stay independent. A typical operator
  never needs to set it.
- **The `AGENTS.md` is curated.** It tells the entity to read the four seed
  files in order. `seed/continuity.md` is intentionally *not* in that list —
  it is the entity's living memory and loads through the anneal-memory
  server, not as a static context file. If you add a seed file, add it to
  `AGENTS.md`. The seed files must not reference each other by `@`-import
  syntax (Codex does not support it); they cross-reference by section
  title, which is what keeps them placement-agnostic.
- **Hooks fire for every Codex session on the machine.** Because
  `~/.codex/hooks.json` is global, the hooks run any time `codex` is
  invoked anywhere. The `in_install_session()` gate in `_levain_hook.py`
  scopes them: hooks fire only when Codex's cwd is inside the install, and
  no-op silently otherwise. Without this gate, unrelated Codex sessions
  would receive Levain's posture and recency directives.

## Harness portability

The hook scripts are nearly substrate-neutral. For a non-Codex adapter,
expect **three** swap points:

1. **`_levain_hook.emit()`** — formats output as hook JSON
   (`hookSpecificOutput` + `hookEventName` + `additionalContext`). Codex
   and Claude Code use the same shape for SessionStart and
   UserPromptSubmit; another harness with a different shape swaps this
   function. Per-event output schemas matter — Codex 0.133's `Stop` does
   not accept this shape (see *Known characteristics*), so verify each
   target event's schema before assuming `emit()` ports cleanly.
2. **The SessionStart `source` vocabulary** parsed in `session_start.py` —
   Codex 0.133 emits `startup` / `resume` / `clear` / `compact`
   (matching Claude Code); the matcher covers all four. Re-verify the
   target harness's equivalents.
3. **Hook events wired** — Codex excludes Stop due to output schema
   constraint; Claude Code uses SessionStart + UserPromptSubmit by the
   same architectural logic (Stop fires after the model stops). A
   different harness may wire different events; the wiring is in
   `hooks.json.template` (per Codex) or `settings.template.json` (per
   Claude Code).

The Codex-vs-Claude-Code divergence (no `CODEX_PROJECT_DIR` env var) is
already absorbed: `install_root()` relies only on `__file__` parents
resolution.
