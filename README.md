# Levain

**A portable cognitive-partnership memory + methodology kit. You ship the seed that grows a practice, not the practice.**

```
pip install levain
```

Levain gives your AI partner a memory that persists across sessions, and keeps that memory **yours**. It lives on your machine, in a store you own and can read, inspect, and edit from outside any session. Nothing reaches long-term memory except through a path you govern. Every governed change is recorded, and a failed read shows up as *unknown* instead of a false all-clear. It's a memory you can trust because you can see what's in it and how it got there.

Apache 2.0 · Python 3.12+ · Claude Code, Codex CLI, and OpenHands.

![The cockpit's Health and Cognition Trace panels — Hebbian links, graduation counts, episode counts, and a live per-wrap oscilloscope, all real numbers from the author's own long-running memory store.](https://raw.githubusercontent.com/levainhq/levain/main/docs/img/cockpit-health-trace.png)

*`levain serve` — memory health and a live consolidation trace, from the author's own long-running memory store. You don't have to trust the pitch above; this is what "you can see what's in it" actually looks like.*

---

## What this is

If you've worked with an AI partner long enough to feel the session-amnesia problem — every conversation starts cold, every insight gets re-explained, the *quality* of the partnership keeps resetting — Levain is the kit you'd otherwise build for yourself.

It packages four things that work together:

1. **A memory substrate you own.** Episodes from every session, a rolling summary your partner reads to orient each new conversation, an association graph that surfaces related memories on its own, and an affect layer that tracks what mattered. All in a local SQLite file. Two more stores sit on top: proven patterns graduate into a **crystallized** tier, kept out of the always-loaded context and recalled only when what you're doing calls for it; **open loops** surface the moment your prompt touches them and resolve out of your field of view once your partner marks them done.

2. **A methodology-core seed.** A small, dense set of files defining who your partner is, how the partnership works, how memory accrues, and who you are to it. Written so your partner lives *inside* the method instead of pointing at it.

3. **An activation layer.** Instructions that prime your session at open, sharpen each prompt turn, and shape how sessions close. Wired through your harness's hooks, so it happens on its own, not when you remember to ask.

4. **A scripted onboarding.** `levain init` walks you through filling the seed so your partner is uniquely yours from session one. Terminal, a browser form with `--web`, or a JSON answer file with `--answers` when nobody is at the keyboard.

## Three ways to run it

Levain isn't a tool you adopt in one place. It's a governed substrate you can apply wherever you already work, in any combination.

**Inside the harness you're already using.** Claude Code and Codex CLI both wire in via hooks — your partner stops starting cold, and its proven patterns and open loops fire on their own, per turn, without you asking.

**As its own sovereign partner.** `levain init --adapter openhands` scaffolds an entity that isn't wired into anything — its own identity, its own memory store, its own hands on your real repos, running on an open model you choose rather than a frontier vendor's hosted assistant (point it at a local Ollama model to also keep the compute itself on your machine). It talks to you as a REPL (`levain run`) or takes one task and exits (`levain run --task`), and it consolidates its own memory with its own mind (`levain wrap`).

**On a schedule, unattended.** `levain daemon install-seat` runs that same sovereign entity on a cadence — every hour, every day, whatever you set — while you're away. It's not autonomous in the sense that should worry you: every action the entity takes that reaches outside itself halts and reports back to you before it lands, because with nobody watching there's nobody to ask. See *Hand it a schedule* below for exactly what that means.

All three modes share one store discipline and one governance model. You're not choosing between three products.

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
levain init --adapter claude-code    # or --adapter codex, or --adapter openhands; prompts if omitted
levain init --web                    # fill the same interview in a browser form, localhost only
```

One adapter per install. To run both Claude Code and Codex, make two installs.

### Installing without a terminal in front of you

The interview can be answered from a file instead of typed — which is how you
provision more than one install, or any install that has to come up unattended.

```
levain init --answers-template > answers.json   # every question, blank, ready to fill
levain init --adapter openhands --answers answers.json --path ./seat-1
```

The answer file is a JSON object **keyed by slot name**, never an ordered list, so
you never have to know or match the order the interview happens to ask in:

```json
{ "OPERATOR_NAME": "Chris", "ENTITY_NAME": "Ada", "LOCATION": "Ohio" }
```

`--answers-template` writes the blank file to stdout and a guide to *what each
question is asking* to stderr — so the redirect above gives you valid JSON and you
still get to read the questions. Every slot must be present; `""` is a real answer
meaning "skip this," exactly as pressing enter does in the terminal — except for
your name and your partner's, which an entity is not allowed to be missing.

`--answers` requires `--adapter`, and it never prompts for anything. If a field is
missing, unknown, or has plainly landed in the wrong slot, it says so and installs
nothing — rather than writing a half-captured partner and reporting success.

## Composing a domain — AgentPacks

The base seed gives your partner a general methodology. A **pack** layers a domain on top of that at install time: your own doctrine, your own process library, your own vocabulary — composed over the same governed substrate rather than swapped in for it.

```
levain init --pack ./my-domain-pack --pack ./my-role-pack
```

A pack is just a directory: a `pack.toml` declaring a name and an order, plus a `seed/` folder of Markdown files. A new file in a pack *adds* to what the base install carries; a file with the same name as a base file *overrides* it, with the higher-`order` pack winning on a collision. Files that need the interview to fill them in are declared in `render`; everything else is copied byte-for-byte. `--pack` is repeatable, so a domain layer and a role layer can stack — the domain pack carries the doctrine and process, the role layer carries who's driving it and how they talk. Every file a pack adds gets wired into your partner's activation the same way the base seed does; nothing you compose in sits on disk unread. Pack chapters you ship alongside the seed also show up in `levain docs` (below), composed with the base operator manual instead of living in a separate doc nobody opens.

A domain pack's doctrine can also state where your partner should act on its own and where it has to stop and hand a decision to you — write that judgment as plain prose in the pack's seed, the same way you'd write anything else it should know. Today that's exactly what it is: prose your partner reads and is expected to follow, not something the harness enforces. The efferent gate Levain does enforce (below) — on a scheduled seat, on `run --task`, and on any REPL run you pin to `gated` — classifies by *tool kind*, bash vs. a file-editor read vs. write, not by your domain's judgment about what's safe to automate.

There's no packs marketplace here and no bundled example pack — packs are something you author for your own domain, the same way you'd author the interview answers that make an install yours. The mechanism is the product; what you put in it is yours to write.

## Run a sovereign entity

`levain init --adapter openhands` scaffolds something different from the hook-wired adapters above: an entity that is its *own* partner, not a layer inside a harness you're already running. Its own identity, its own memory store, never a hook into your Claude Code or Codex session, never a reach into `~/.anneal-memory` or any other store on your machine — that isolation is checked and enforced, not assumed.

```
pip install 'levain[openhands]'
levain init --adapter openhands --path ./ada
levain run ./ada                    # talk to it as a REPL, on an open model by default
levain run ./ada --task "..."       # or hand it one task, non-interactively, and read the exit code
levain wrap ./ada                   # consolidate what it learned into its own lasting memory
```

It runs against Ollama by default, through `--model`/`--base-url` — or any OpenAI-compatible or Anthropic-style endpoint you point it at instead. The out-of-the-box default, `glm-5.2:cloud`, is served through Ollama Cloud, so by default the episode content does leave your machine; point `--model` at a plain Ollama model name (no `:cloud`) to keep the whole conversation local. Either way it's an open model you chose, never a frontier vendor's hosted assistant. Give it hands and, on macOS, it works your real repos through a file editor and a sandboxed shell, both fenced to the same crown-jewels floor described below — `~/.anneal-memory`, sibling stores, and your SSH key material stay off-limits no matter what it's asked to do. On any other platform there's no confinement provider yet, so it gets the file editor only, never an unconfined shell — see *Boundaries*. `--no-tools` runs it as a pure conversational partner with no hands at all. `levain wrap` is what makes the entity's identity compound instead of just accumulating transcripts: it metabolizes its own raw episodes into its own six-section memory, composed on its own model by default, so the entity that answers you tomorrow has actually learned from today.

## Hand it a schedule — the governed seat

`levain daemon install-seat` takes the sovereign entity above and runs it unattended, on a cadence, while you're away.

```
levain daemon install-seat --path ./ada --task "check the queue and report" --interval 3600
```

This is the part that separates a governed seat from the always-on personal-agent runtimes that make headlines for the wrong reasons. **We never ask the model whether what it's about to do is safe.** The gate that halts an action reads the *tool*, not the model's opinion of itself — because a gate the entity can talk its way through is not a gate. By default, every action the seat takes that reaches outside itself — a shell command, a file write, anything efferent — halts before it executes: exit code 4 in the seat's log, the activity that led up to it right there for you to read. It's not a permission prompt, because an unattended seat has nobody to ask, and there's no queue holding the action for you to approve later — the halt ends that run, and the next scheduled turn starts fresh (and will halt again at the same point if the task still needs that action). Because bash is classified as efferent by *kind*, not by what the command actually does, a seat task that needs the shell at all will halt on it every run, even for something as harmless as `ls` — plan seat tasks around the file-editor hand where you can, or expect to be reading a lot of exit-4 logs. This default can be turned off per entity (`efferent_gate: "ungated"` in `.levain/confinement.json`), which is a real escape hatch and not a decision to make lightly for something that runs unattended.

The seat is wall-clock bounded (`--max-seconds`), so a stalled model endpoint can't silently strand it running forever behind a schedule that looks healthy; a bound this hits exits 5, distinctly from a genuine failure, so a supervisor watching the exit code knows the difference between "the environment stalled" and "something is actually broken." And it consolidates its own memory — after each scheduled turn it checks the backlog and consolidates once enough new episodes have accumulated (`--consolidate-every N` is an episode count, not a cadence; default: the wrap-nudge threshold) — but a consolidate run unattended may only *metabolize*, never *crystallize*: it can compose its working memory, but promoting anything into the crystallized, always-loaded tier is refused structurally unless a human runs the wrap themselves. An agent nobody is watching does not get to rewrite its own bedrock.

## Keep it in sync — `doctor` and `update`

What changed between releases — and anything that changes an existing deployment's behaviour — is in [`CHANGELOG.md`](https://github.com/levainhq/levain/blob/main/CHANGELOG.md). Read it before upgrading across a minor version.

Levain composes a stack across two version lines that `pip` alone can't keep aligned: the `anneal-memory` library (versioned separately on PyPI) and your methodology seed (versioned inside Levain). `pip` keeps the *library* compatible, but it's blind to *methodology* drift. A new memory feature can land as a contradiction with your older, hand-tuned instructions instead of a clean addition. That drift is what breaks a long-running install.

Levain ships a known-good version set and two commands to hold it:

```
levain doctor          # loud, in-environment health check
levain update          # reconcile the whole set in one fail-safe pass
```

**`levain doctor`** checks the things a silently-dead install hides: the interpreter resolves, `anneal-memory` is reachable, the store opens, the memory server `init` set up is still wired to your store, the hooks point at *this* install. It also reports whether your version set has drifted from the tested known-good — library version, store schema, unreviewed memory-migration proposals. It exits nonzero on failure, so it drops into a shell pipeline: `1` when something is wrong, `6` when every failure is a post-upgrade step you have not applied yet (the output names the command). A read that fails is reported as a failure, never quietly passed.

**`levain update`** brings the stack back to known-good in one ordered, reversible pass: bring `anneal-memory` to the tested version (the env-mutating step asks first; `--yes` to auto-confirm, `--no-pip` to skip it and print the exact command for your own package manager), re-run the partnership schema if the store drifted, surface the library's migration proposals for you to apply *under review* (it never edits your instruction files for you), and record the reconciled set. `--dry-run` shows the plan and changes nothing.

## Look at — and operate — your own memory

Your partner's memory normally only exists *inside* a session. These look at it from outside, and optionally let you steer it. All on your machine, no vendor host, no account.

```
levain dashboard          # a one-shot terminal glance (add --json for the raw view)
levain serve              # a live local view in your browser
levain tui                # a full-screen interactive terminal view
levain docs               # the operator manual, composed with any pack's own chapters
```

**`levain serve`** runs a tiny localhost web app (default `http://127.0.0.1:7420`) and opens your browser to a live view of your substrate: memory health, the association graph, crystallized patterns, open loops, and your State / Active-Threads narrative. It binds loopback only, refuses non-loopback hosts, and serves its own UI from the package (no CDN, renders offline). Read-only by default. Pass **`--write`** to edit your memory from the browser: your State, the lifecycle of your open loops, your inbox and reference notes. Every change goes through a governed path that records it, so the writable view doubles as an audit log of what you did. It stays loopback-only by construction: your seed and config are private, so there's no off-box write surface.

![The Operate zone — Open Loops, Tray, and Keep panels, each editable from the browser.](https://raw.githubusercontent.com/levainhq/levain/main/docs/img/cockpit-operate.png)

*Open Loops surface on their own when a prompt touches them; Tray is your inbox into the partnership; Keep is durable reference. Shown on a freshly-seeded demo install ("Ridge"), not a real one — an operator's actual Open Loops and Tray are exactly the kind of thing this README won't put on the internet.*

![The State, Active Threads, Patterns, Decisions, and Context panels from the same demo install.](https://raw.githubusercontent.com/levainhq/levain/main/docs/img/cockpit-state.png)

*The felt-memory side: what a wrap actually writes to your continuity file, rendered from outside the session. Same demo install as above.*

**`levain tui`** is the terminal-native peer of `serve`: read and steer without a browser or a port. `--read-only` drops to a pure inspection view.

**`levain focus`** sets the one line your sessions read to orient: *what you're working on right now*. It travels across sessions like the rest of your memory.

```
levain focus "shipping the v2 onboarding flow"   # set it
levain focus                                       # show it + how fresh it is
levain focus --clear                               # unset it
```

Two of those write targets are your own inbox into the partnership. Dump anything mid-stream (a thought, a handoff, something to pick up next time) and it surfaces at the start of your next session, then resolves. Keep durable reference you want your partner to recall when it's relevant. You dump freely; the kit sorts.

For hosts that render MCP Apps (and only those), `pip install 'levain[app]'` adds what `levain serve-app` needs to run: a read-only in-host view served over stdio. `levain serve` needs nothing beyond the base install.

## Keep it running (macOS)

```
levain daemon install       # start the local write window on login, survive a crash
levain daemon install-seat  # install a governed, scheduled entity — see "Hand it a schedule" above
levain daemon status        # is it actually installed and running?
levain daemon would-install  # dry-run: show what install would do, change nothing
levain daemon restart       # restart a running serve or seat, e.g. after new code
levain daemon uninstall
```

`levain daemon install` keeps the local writable cockpit (`levain serve --write`) available without an ad-hoc background process — it starts on login and restarts on crash. `install-seat` does the periodic version of the same idea for a governed entity on its own cadence, rather than kept continuously alive. Both are per-user, no admin or root (a launchd user agent on macOS today; Linux `systemd --user` and Windows Task Scheduler are planned), and both stay pointed at loopback, never off-box. `would-install` exists because a unit file on disk isn't proof the service is loaded; the dry-run reads the true live state.

## Audience

Operator-class developers: the people who already feel session-amnesia as a real problem and would build their own fix. If you've built your own substrate-management scripts, you'll recognize the pieces. If "continuity file" and "session-closing reflection" don't land yet, this probably isn't your tool yet.

## Boundaries, kept honest

- **Harnesses:** Claude Code and Codex CLI (hook-wired), plus `openhands` (hookless, scaffolds a sovereign entity). One adapter per install — separate installs if you need more than one.
- **Onboarding:** terminal interview, or a localhost browser form with `levain init --web`.
- **Always-on daemon and governed seats:** macOS today; Linux and Windows are planned.
- **Confined shell hands:** the sandboxed bash tool for a sovereign entity (`levain run`) is macOS only. On any other platform the entity gets the file-editor hand and no shell — it fails closed rather than granting an unconfined one.
- **The efferent gate is a default, not a lock.** `efferent_gate: "ungated"` in an entity's `.levain/confinement.json` turns the halt off entirely, including for a scheduled seat. `daemon install-seat` warns loudly at install time if you're about to install one that way; `doctor` doesn't re-check it on a seat that's already running.
- **One seat at a time:** a governed seat runs one entity on one schedule. Coordinating several seats as a fleet is not built yet — see *Where this is going* below.
- **Codex hook reliability:** recent Codex versions have a platform-level hook-trust gap no consumer can work around. `levain verify-hooks` (and `levain doctor --invoke`) invoke each hook with the JSON a harness would send and prove the scripts fire correctly; whether Codex itself invokes them at runtime is up to Codex.
- **No security absolutes.** The confinement floor is real and load-bearing, but it's a floor, not a guarantee — it denies a fixed set of crown-jewel paths and known escape routes, not "everything dangerous." Read the module docstrings in `levain/firing/confinement.py` if you're deciding whether to trust it with something that matters.

## What it's built on

Levain layers on [`anneal-memory`](https://pypi.org/project/anneal-memory/) (pinned `>=0.9.10,<0.10`). The division of labor is the whole idea: **anneal-memory is the substrate; Levain is the harness that fires it.** anneal-memory deliberately can't reach into your session. A memory library that hooked every prompt would forfeit the neutrality that lets it run under any harness. So on its own it gives you the stores and manual recall; Levain wires the hooks that surface the right memory automatically. Clean dependency direction, both on PyPI: Levain depends on anneal-memory, never the reverse.

## Build on it

Writing your own surface over a substrate? `levain.kernel` is the published seam: the data model, the terminal and web drivers, and the governed write and action dispatch. Import one namespace instead of reaching into internals. Register extra read-only panels or extra governed verbs through `make_server(...)` and they ride the same auth, confirm, and audit envelope as the built-in edits. It's a pure re-export; the governance lives in the substrate, not the surface.

### Working on levain itself — run this once per clone

```
bash scripts/install_hooks.sh
```

It points `core.hooksPath` at the tracked `scripts/hooks/` and installs a `pre-push` gate that runs
the release-stamp test before anything becomes public. `core.hooksPath` is local config git can't
ship on its own, so a fresh clone has no gate until someone runs that line — which is why it's
documented here rather than only in the script. The gate fails closed; the deliberate escape is
`git push --no-verify`.

## Where this is going (not shipped yet)

Everything above is installable today. This section is future tense on purpose — nothing here has an install command, because none of it is released.

- **The pack will carry your domain's judgment, and something will route on it.** Right now a pack's doctrine on when to act and when to ask is prose your partner reads — real, but not enforced (see *Composing a domain* above), and the efferent gate classifies by tool kind alone. The design calls for every install to carry a pack, and for that pack to declare its domain's automation threshold: which kinds of steps are *eligible* to run on their own and which always need a person. Eligibility isn't the same as being allowed to run unwatched, though — the design is explicit that a below-threshold step doesn't skip the gate on day one just because a pack says it's eligible. It has to earn that by running gated for a while and building a clean track record under a floor it can't buy its way past; only then does it graduate to executing on its own with a quiet receipt instead of a halt. A step above the threshold still gates the way every efferent action does today, and a step that starts below the threshold but turns into something above it mid-run pops back to the gate before it acts. It's still undecided which layer would own that routing — the gate Levain ships today, a separate always-on autonomic layer, or something in the pack itself — so read "the gate" above as "whatever ends up enforcing this," not a claim about today's `firing/gate.py`.
- **Linux for the always-on cockpit and governed seats.** The confinement floor already has a Linux design in progress (sandboxing via `bwrap`, lifecycle via `systemd --user`, mirroring what `daemon install`/`install-seat` do on macOS today), but it isn't merged or released. Windows autostart is a further-out planned target, not started.
- **Talking to a running entity from the cockpit.** Right now the alive Conversation an entity holds during `levain run` lives only in that process. The plan is a server-held session your cockpit (`levain serve`/`levain tui`) can open a `/turn` onto, so you could watch and steer a sovereign entity's conversation the way you already watch its memory.
- **A fleet of seats.** `daemon install-seat` runs one governed entity on one schedule. Coordinating several seats — an inter-entity bus, cross-seat aggregation — is a real target but a later one, and it inherits the same gate discipline: nothing about running more entities relaxes what any single one is allowed to do unattended.

None of this changes what ships today. Nothing gets looser just because a pack declares a step eligible — that eligibility still has to be earned, gated, under a floor — and until any of this ships, an efferent action's only way past the gate is the same all-or-nothing `ungated` override that exists now.

## License

Apache 2.0. See [`LICENSE`](https://github.com/levainhq/levain/blob/main/LICENSE) and [`NOTICE`](https://github.com/levainhq/levain/blob/main/NOTICE). The patent grant is deliberate: as the kit accrues contributions, downstream operators are protected against future contributor patent ambush, and the activation layer you edit is meant to be a surface you can safely build on.

---

*levainhq.com*
