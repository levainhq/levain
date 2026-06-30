# Levain

**A portable cognitive-partnership memory + methodology kit. You ship the seed that grows a practice, not the practice.**

```
pip install levain
```

Levain gives your AI partner a memory that persists across sessions, and keeps that memory **yours**. It lives on your machine, in a store you own and can read, inspect, and edit from outside any session. Nothing reaches long-term memory except through a path you govern. Every governed change is recorded, and a failed read shows up as *unknown* instead of a false all-clear. It's a memory you can trust because you can see what's in it and how it got there.

Apache 2.0 · Python 3.12+ · Claude Code and Codex CLI.

---

## What this is

If you've worked with an AI partner long enough to feel the session-amnesia problem — every conversation starts cold, every insight gets re-explained, the *quality* of the partnership keeps resetting — Levain is the kit you'd otherwise build for yourself.

It packages four things that work together:

1. **A memory substrate you own.** Episodes from every session, a rolling summary your partner reads to orient each new conversation, an association graph that surfaces related memories on its own, and an affect layer that tracks what mattered. All in a local SQLite file. Two more stores sit on top: proven patterns graduate into a **crystallized** tier, kept out of the always-loaded context and recalled only when what you're doing calls for it; **open loops** surface the moment your prompt touches them and close themselves when they're done.

2. **A methodology-core seed.** A small, dense set of files defining who your partner is, how the partnership works, how memory accrues, and who you are to it. Written so your partner lives *inside* the method instead of pointing at it.

3. **An activation layer.** Instructions that prime your session at open, sharpen each prompt turn, and shape how sessions close. Wired through your harness's hooks, so it happens on its own, not when you remember to ask.

4. **A scripted onboarding.** `levain init` walks you through filling the seed so your partner is uniquely yours from session one. Terminal, or a browser form with `--web`.

The kit runs under Claude Code or Codex CLI. Both are first-class.

## Why a seed, not a finished methodology

The claim under the name *levain*, the sourdough starter you feed:

**A grown cognitive-partnership methodology can't be shipped as a methodology.** Hand someone the finished artifact and they get a fossil. Text describing practices that never take root in their substrate. Practice-encoded knowledge transfers through use, not specification.

So Levain ships the *engine that grows* a methodology: the substrate, a graduation mechanism, a discipline for how sessions close, a reflection loop, a minimal starting posture. Your own methodology accretes from your own sessions. Which is why it sticks, and why it's yours. Run Levain and you will *not* end up with this operator's memory. Different texture, different graduated patterns, different shape.

## The proof

See [`examples/accrual/growth_timeline.md`](https://github.com/levainhq/levain/blob/main/examples/accrual/growth_timeline.md) — one continuity file rendered at four points across five calendar months. It started at 96 lines and 6 sections, hit one architectural cliff between week 4 and week 12, locked into a 9-section shape, and has held that shape since while density grew inside it (96 → 549 lines, +3 sections total).

Your week 1 will look like the first snapshot. That's the right starting place, not the last snapshot, which is what *this* partnership grew into. The trajectory is the proof; the endpoint is not the target.

## What fires on its own

This is why Levain exists instead of just installing a memory library. Raw memory libraries quietly rot, because episodes pile up but you only recall what you happen to remember to query. A library can't fix that without hooking your session, and hooking every prompt would cost it the neutrality that lets it run under any harness. So that's the harness's job, and it's what Levain wires:

- **Open loops surface on collision.** A relevant open loop drops into context the moment your prompt touches it.
- **Crystallized patterns recall per turn.** Your proven, stable wisdom is held out of the always-loaded context and surfaced when what you're doing calls for it. Useful without clogging the window.

The store is universal; the *firing* is the harness's job. (Under Codex there's a platform caveat on whether hooks fire at all — see *Boundaries, kept honest*. `levain verify-hooks` proves your wiring is correct regardless.)

## Set it up

```
pip install levain
levain init --path ./my-partner
```

This creates `./my-partner`, runs the interview, lays down the seed, registers the adapter you choose, initializes the store at `.levain/memory.db`, and records the exact version set it composed. Then open that directory with Claude Code or Codex and start working.

```
levain init                          # install into the current directory (must be empty)
levain init --force                  # install into an existing workspace (backs up anything it touches)
levain init --adapter claude-code    # or --adapter codex; prompts if omitted
levain init --web                    # fill the same interview in a browser form, localhost only
```

One adapter per install. To run both Claude Code and Codex, make two installs.

## Keep it in sync — `doctor` and `update`

Levain composes a stack across two version lines that `pip` alone can't keep aligned: the `anneal-memory` library (versioned separately on PyPI) and your methodology seed (versioned inside Levain). `pip` keeps the *library* compatible, but it's blind to *methodology* drift. A new memory feature can land as a contradiction with your older, hand-tuned instructions instead of a clean addition. That drift is what breaks a long-running install.

Levain ships a known-good version set and two commands to hold it:

```
levain doctor          # loud, in-environment health check
levain update          # reconcile the whole set in one fail-safe pass
```

**`levain doctor`** checks the things a silently-dead install hides: the interpreter resolves, `anneal-memory` is reachable, the store opens, the memory server `init` set up is still wired to your store, the hooks point at *this* install. It also reports whether your version set has drifted from the tested known-good — library version, store schema, unreviewed memory-migration proposals. It exits nonzero on failure, so it drops into a shell pipeline. A read that fails is reported as a failure, never quietly passed.

**`levain update`** brings the stack back to known-good in one ordered, reversible pass: bring `anneal-memory` to the tested version (the env-mutating step asks first; `--yes` to auto-confirm, `--no-pip` to skip it and print the exact command for your own package manager), re-run the partnership schema if the store drifted, surface the library's migration proposals for you to apply *under review* (it never edits your instruction files for you), and record the reconciled set. `--dry-run` shows the plan and changes nothing.

## Look at — and operate — your own memory

Your partner's memory normally only exists *inside* a session. These look at it from outside, and optionally let you steer it. All on your machine, no vendor host, no account.

```
levain dashboard          # a one-shot terminal glance (add --json for the raw view)
levain serve              # a live local view in your browser
levain tui                # a full-screen interactive terminal view
```

**`levain serve`** runs a tiny localhost web app (default `http://127.0.0.1:7420`) and opens your browser to a live view of your substrate: memory health, the association graph, crystallized patterns, open loops, and your State / Active-Threads narrative. It binds loopback only, refuses non-loopback hosts, and serves its own UI from the package (no CDN, renders offline). Read-only by default. Pass **`--write`** to edit your memory from the browser: your State, the lifecycle of your open loops, your inbox and reference notes. Every change goes through a governed path that records it, so the writable view doubles as an audit log of what you did. It stays loopback-only by construction: your seed and config are private, so there's no off-box write surface.

**`levain tui`** is the terminal-native peer of `serve`: read and steer without a browser or a port. `--read-only` drops to a pure inspection view.

**`levain focus`** sets the one line your sessions read to orient: *what you're working on right now*. It travels across sessions like the rest of your memory.

```
levain focus "shipping the v2 onboarding flow"   # set it
levain focus                                       # show it + how fresh it is
levain focus --clear                               # unset it
```

Two of those write targets are your own inbox into the partnership. Dump anything mid-stream (a thought, a handoff, something to pick up next time) and it surfaces at the start of your next session, then resolves. Keep durable reference you want your partner to recall when it's relevant. You dump freely; the kit sorts.

For hosts that render MCP Apps (and only those), `pip install 'levain[app]'` adds what `levain serve-app` needs to run: a read-only in-host view served over stdio. `levain serve` needs nothing beyond the base install.

## Keep it running on login (macOS)

```
levain daemon install       # start the local write window on login, survive a crash
levain daemon status        # is it actually installed and running?
levain daemon would-install  # dry-run: show what install would do, change nothing
levain daemon uninstall
```

`levain daemon` keeps the local writable view of your memory available without an ad-hoc background process. It starts on login and restarts on crash, per-user with no admin or root (a launchd user agent on macOS today; Linux `systemd --user` and Windows Task Scheduler are planned). It's always pointed at loopback, never off-box. `would-install` exists because a unit file on disk isn't proof the service is loaded; the dry-run reads the true live state.

## Audience

Operator-class developers: the people who already feel session-amnesia as a real problem and would build their own fix. If you've built your own substrate-management scripts, you'll recognize the pieces. If "continuity file" and "session-closing reflection" don't land yet, this probably isn't your tool yet.

## Boundaries, kept honest

- **Harnesses:** Claude Code and Codex CLI. One adapter per install — two installs if you need both.
- **Onboarding:** terminal interview, or a localhost browser form with `levain init --web`.
- **Always-on daemon:** macOS today; Linux and Windows are planned.
- **Codex hook reliability:** recent Codex versions have a platform-level hook-trust gap no consumer can work around. `levain verify-hooks` (and `levain doctor --invoke`) invoke each hook with the JSON a harness would send and prove the scripts fire correctly; whether Codex itself invokes them at runtime is up to Codex.

## What it's built on

Levain layers on [`anneal-memory`](https://pypi.org/project/anneal-memory/) (pinned `>=0.9.6,<0.10`). The division of labor is the whole idea: **anneal-memory is the substrate; Levain is the harness that fires it.** anneal-memory deliberately can't reach into your session. A memory library that hooked every prompt would forfeit the neutrality that lets it run under any harness. So on its own it gives you the stores and manual recall; Levain wires the hooks that surface the right memory automatically. Clean dependency direction, both on PyPI: Levain depends on anneal-memory, never the reverse.

## Build on it

Writing your own surface over a substrate? `levain.kernel` is the published seam: the data model, the terminal and web drivers, and the governed write and action dispatch. Import one namespace instead of reaching into internals. Register extra read-only panels or extra governed verbs through `make_server(...)` and they ride the same auth, confirm, and audit envelope as the built-in edits. It's a pure re-export; the governance lives in the substrate, not the surface.

## License

Apache 2.0. See [`LICENSE`](https://github.com/levainhq/levain/blob/main/LICENSE) and [`NOTICE`](https://github.com/levainhq/levain/blob/main/NOTICE). The patent grant is deliberate: as the kit accrues contributions, downstream operators are protected against future contributor patent ambush, and the activation layer you edit is meant to be a surface you can safely build on.

---

*levainhq.com*
