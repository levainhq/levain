# Levain — Claude Code adapter

The Claude Code adapter places the methodology-core seed into Claude Code's
context-file format and wires the two things a Levain entity needs to run:
the **activation hooks** (the posture mechanism) and the **anneal-memory MCP
server** (the entity's memory). It is a **Tier 1** adapter — Claude Code
exposes both hook events the activation mechanism needs (`SessionStart`,
`UserPromptSubmit`).

The adapter is **project-scoped**: everything lives inside one directory — the
operator's partnership working directory — and the hooks, the MCP server, and
the memory store are all wired relative to it. It never touches the operator's
global Claude Code configuration, and a second Levain install on the same
machine is fully independent of the first.

**Supported platforms:** macOS, Linux, and WSL. Native Windows is not supported
in v1 — the hook commands use POSIX shell form. (A Windows template is a
post-v1 item.)

> **This README documents the adapter and a manual install path for dogfooding
> it.** The supported end-to-end install — running an interview to fill the
> seed templates, initializing the store, loading the continuity scaffold,
> resolving the placeholder tokens below — is `levain init` (build step 4).
> Until that ships, the manual sequence here stands up a working install.

## Install layout

```
<partnership-directory>/          <- the Levain install; $CLAUDE_PROJECT_DIR
├── CLAUDE.md                     <- from CLAUDE.md.template; @-imports the seed
├── .mcp.json                     <- from mcp.template.json; registers anneal_memory
├── .claude/
│   └── settings.json             <- from settings.template.json; wires the hooks
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
```

## Template placeholders

Three templates carry placeholder tokens that resolve to environment-specific
values. `levain init` (build step 4) resolves them; for a manual install,
substitute them by hand per the steps below.

| Token | In | Resolves to |
|-------|----|-------------|
| `{{INSTALL_DIR}}` | `mcp.template.json` | absolute path of the partnership directory |
| `{{PYTHON}}` | `settings.template.json` | the working Python 3 interpreter (`python3`, or its absolute path) |
| `{{ANNEAL_MEMORY}}` | `mcp.template.json` | the `anneal-memory` executable (`anneal-memory`, or its absolute path) |

`{{PYTHON}}` and `{{ANNEAL_MEMORY}}` are environment-dependent on purpose:
a bare command name resolves against `PATH`, and Claude Code launched from a
GUI app (VS Code / JetBrains / desktop) inherits a *minimal* `PATH` that often
omits `pip --user` script directories. Resolving them to absolute paths at
install time removes that failure mode.

## What the adapter wires

### 1. Activation hooks — the posture mechanism

The shipped activation content lives in `../../activation/` (`posture.md`,
`recency_directives.md`, `hooks/`). The layers this adapter wires are
described below.

- **`session_start.py`** → `SessionStart` (Layer A). Injects, at primacy
  position: the posture string from `activation/posture.md`, and the current
  date/time. Fires on `startup`/`resume`/`clear`/`compact` — `compact` matters:
  a context compaction rebuilds the window and the primacy posture must be
  re-injected. On a fresh session (`startup`/`clear`) it also runs the **Layer D
  start-catch** — if the previous session left episodes unwrapped, it flags it.
- **`user_prompt_submit.py`** → `UserPromptSubmit` (Layer B). Injects, at
  recency position before each prompt: one directive selected at random from
  `activation/recency_directives.md`. Once episodes-since-last-wrap pass a
  threshold it also appends the **Layer D ambient nudge**.

Layer D (keeping sessions wrapped) is **folded into these two hooks** — there
is no separate `Stop`/`SessionEnd` hook. A `Stop` hook injects context only
after the model has already stopped; a `SessionEnd` hook is side-effect-only
and does not fire on a hard terminal kill — so neither is the right tool.

### 2. Memory — the anneal-memory MCP server

`seed/memory.md` instructs the entity to operate its memory through an MCP
server named **`anneal_memory`**. `.mcp.json` registers it. The server name is
load-bearing — `seed/memory.md` and the `anneal://continuity` resource resolve
only if the server is registered under exactly that name. `mcp.template.json`
ships the correct registration; do not rename the server.

The store is pinned to **`.levain/memory.db` inside the install** — see
*Store scoping* below.

## Prerequisites

- **`anneal-memory`** installed (`pip install anneal-memory`). Both the MCP
  server and the hooks' Layer D wrap-check invoke it. The shipped Levain package
  pins a compatible anneal-memory range (see `pyproject.toml`); `levain doctor`
  verifies the installed version against Levain's known-good set.
- **Python 3** available. The hook commands invoke it (`{{PYTHON}}`). If no
  working interpreter is wired, the hooks silently do not run and the posture
  mechanism is off — verify with `python3 --version` first. On a fresh macOS
  without the Xcode command-line tools, `python3` may be absent.

## Install (manual — `levain init` automates this at build step 4)

1. **Install anneal-memory** and confirm it runs: `anneal-memory --help`.
   Note its absolute path (`command -v anneal-memory`) and your Python 3 path
   (`command -v python3`).
2. **Place `seed/`, `activation/`, and `CLAUDE.md`** (from `CLAUDE.md.template`)
   in the partnership directory.
3. **Create `.mcp.json`** at the directory root from `mcp.template.json`:
   replace `{{INSTALL_DIR}}` with the **absolute path** of the partnership
   directory and `{{ANNEAL_MEMORY}}` with the anneal-memory path from step 1.
4. **Create `.claude/settings.json`** from `settings.template.json`: replace
   `{{PYTHON}}` with the Python 3 path from step 1. If the directory already
   has a `.claude/settings.json`, do not overwrite it — merge: append the
   `SessionStart` and `UserPromptSubmit` entries into the existing `hooks`
   object's arrays for those events (each event's value is an *array* of
   matcher groups; appending a group is safe, replacing the array is not).
5. **Initialize the memory store:**
   `anneal-memory --db <absolute-install-path>/.levain/memory.db init`
6. **Make the hooks executable:** `chmod +x activation/hooks/*.py` (optional —
   the wired command invokes the interpreter explicitly — but good hygiene).
7. **Start a Claude Code session** in the partnership directory.

`levain init` (build step 4) automates steps 2–6, resolves the placeholder
tokens, fills `world.md`/`origin.md` from an interview, and loads
`seed/continuity.md` into the store. In this manual path the entity starts
from an empty continuity — the expected near-empty start anyway
(`seed/memory.md`: "early on it will be near-empty").

## First-run approvals

Claude Code shows three one-time approval dialogs the first time the install is
opened — all are expected security prompts:

- **Hooks** — `.claude/settings.json` wires the activation hooks; approve it or
  the posture mechanism is off.
- **`@`-imports** — `CLAUDE.md` imports the seed files; approve it or the seed
  will not load.
- **MCP server** — `.mcp.json` registers `anneal_memory`; approve it or the
  entity has no memory. (Reset later, if needed, with
  `claude mcp reset-project-choices`.)

## Verify the install

Hook-injected context is **not visible in the Claude Code UI** — and every
failure mode here is silent. Verify the *actual* runtime, not a proxy:

- **Activation:** start a session and ask the entity *"what posture
  instructions and what date were injected into your context this session?"* —
  it should quote the posture string and today's date back.
- **Memory:** from inside that session, ask the entity to record an episode and
  then recall it. If the `anneal_memory` MCP server failed to register, the
  entity will report its memory tools are unavailable — test this in-session,
  not via `anneal-memory` in a terminal (a terminal has a different `PATH` than
  a GUI-launched Claude Code, so a terminal check can pass while the real
  MCP spawn fails).

## Store scoping

The anneal-memory store is pinned to **`.levain/memory.db` inside the
install** — by the hooks (`_levain_hook.store_path()`, install-relative) and by
`.mcp.json` (an absolute path to the same file). This is deliberate:
anneal-memory's *default* store is a single machine-global file
(`~/.anneal-memory/memory.db`); if two Levain installs both used the default
they would share one episode store and one continuity — **two entities'
memories, and identities, silently merged**. Install-relative scoping is
structural.

**Moving the install** breaks this: the hooks self-locate and follow the move,
but `.mcp.json` holds an *absolute* store path that goes stale. After moving
the directory, re-run `levain init` (or hand-edit `.mcp.json`'s path) — or the
MCP server and the hooks will read different stores.

## Known characteristics

- **`user_prompt_submit.py` runs `anneal-memory status` once per prompt** to
  read the unwrapped-episode count for the Layer D nudge. anneal-memory's store
  is WAL-mode, so this read does not block on the MCP server's writes — the
  cost is process-spawn overhead, fast on a normal store. The per-prompt path
  uses a tight 2s query timeout so a hung anneal-memory cannot stall a turn;
  it is fail-open (no count → no nudge).
- **`LEVAIN_HOOK_SUPPRESS=1`** — if you later build tooling that itself
  launches Claude Code from *inside* the install (a consultation runner, a
  batch job), set this in that tooling's environment so the activation
  directives do not leak into contexts that must stay independent. A typical
  operator never needs to set it.
- **The `CLAUDE.md` `@`-import list is curated.** It imports four seed files;
  `seed/continuity.md` is intentionally *not* imported — it is the entity's
  living memory and loads through the anneal-memory server, not as a static
  context file. If you add a seed file, add it to `CLAUDE.md`. The seed files
  must not `@`-import *each other* — they cross-reference by section title,
  which is what keeps them placement-agnostic.

## Harness portability

The hook scripts are nearly harness-neutral. For a non-Claude-Code adapter
(e.g. Codex, build step 3), expect **three** swap points, not one:

1. **`_levain_hook.emit()`** — formats output as Claude Code hook JSON
   (`hookSpecificOutput` + `hookEventName`). The primary seam; swap it for the
   target harness's hook-output format.
2. **The `CLAUDE_PROJECT_DIR` env var** read in `install_root()` — Claude
   Code-specific; honor the target harness's project-dir variable, or rely on
   the `__file__` fallback.
3. **The `source` vocabulary** (`startup`/`resume`/`clear`/`compact`) parsed in
   `session_start.py` — Claude Code's `SessionStart` payload values; re-verify
   the target harness's equivalents.
