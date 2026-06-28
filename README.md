# Levain

**A portable cognitive-partnership memory + methodology kit. You ship the seed that grows a practice, not the practice.**

```
pip install levain
levain init
```

**Heads-up before you scroll** — Levain v1 boundaries (kept honest):

- **Harnesses:** Claude Code + Codex CLI at v1. One adapter per install (use two installs if you need both).
- **Python:** 3.12+ required. The package uses `importlib.resources.as_file()` on a directory resource, which gained support in 3.12. v0.1.0 supported 3.11+ but only for filesystem installs; v0.1.1 bumps the floor to 3.12 and adds zipapp / PyInstaller / `pip install --target` into zip support.
- **Web pane onboarding:** CLI interview at v1.0. Browser-based onboarding lands in v1.1.
- **Codex hook reliability:** Codex 0.132/0.133 has a known platform-level hook-trust gap that no harness consumer can work around. `levain doctor` + `levain verify-hooks` surface the wiring; whether your Codex install actually fires the hooks is up to Codex itself ([upstream tracking issue](https://github.com/openai/codex/issues/17532)).
- **Memory substrate:** anneal-memory (0.7.x) is the layer-1 dependency; pinned in `pyproject.toml`. It now carries the **crystallized-pattern tier** (on-demand graduated memory) — see *Dependencies* below.

---

## What this is

If you've been working with an AI partner long enough to feel the session-amnesia problem — every conversation starts over, every insight has to be re-explained, the *quality* of the partnership keeps resetting — Levain is the kit you'd otherwise build for yourself.

It packages four things that work together:

1. **A Complementary Learning Systems memory substrate** via the `anneal-memory` library — four layers (episodic + continuity + Hebbian associations + limbic) plus two sibling stores: prospective **spores** (open loops that self-clean) and an on-demand **crystallized** pattern tier (proven, stable wisdom held *out* of always-loaded context and recalled on cue, so the working set stays bounded while earned wisdom keeps growing). Your partner's hippocampus, working memory, lateral connections, and long-term semantic store.
2. **A methodology-core seed** — a small set of files defining who the entity is, how partnership works, how memory accrues, who its operator is.
3. **An activation mechanism** — primacy-position posture + recency directives + session-boundary wrap discipline, wired through harness hooks.
4. **A scripted onboarding interview** — `levain init` walks you through filling the seed templates so the entity is uniquely yours from session one.

The kit installs into a Claude Code or Codex CLI workspace. Both harnesses are tier-1 supported at v1.

## Why a seed, not a finished methodology

The load-bearing claim under the name *levain* (the sourdough starter you feed):

**A grown cognitive-partnership methodology cannot be shipped as a methodology.** Hand someone the artifact and they get a fossil — text describing practices that don't accrete in their substrate. Practice-encoded knowledge transfers through use, not specification.

So Levain ships the *engine that grows* a methodology: substrate + graduation mechanism + wrap discipline + reflection loop + minimal starting posture. Your own methodology accretes from your sessions. Which is why it sticks, and why it's yours.

A separate operator running Levain will not end up with this operator's continuity — different texture, different graduated patterns, different shape. That's the point.

## Proof

See [`examples/accrual/growth_timeline.md`](examples/accrual/growth_timeline.md) — one continuity file rendered at four snapshots over five months. It started at 96 lines / 6 sections, locked into 9 sections around week 12, and has held that shape since while density grew inside it.

Your week 1 will look like the first snapshot. That's the right starting place — not the last snapshot, which is what this operator's partnership grew into. The trajectory is the proof; the endpoint is not the target.

## Install

```
pip install levain
levain init
```

`levain init` runs the scripted interview, lays down the seed templates, registers the chosen adapter (Claude Code or Codex), and initializes the memory store at `.levain/memory.db`. One adapter per install at v1 — multi-adapter layering is a v1.1 candidate.

```
levain init --path /path/to/install [--adapter claude-code|codex] [--force]
levain doctor --path /path/to/install
levain verify-hooks --path /path/to/install
```

`levain doctor` is the loud in-environment health check — interpreter resolution, MCP server registration, store reachability, hook injection liveness.

`levain verify-hooks` actually invokes the activation hooks via stdin JSON and verifies they emit valid output. Closes the silent-skip class — particularly the Codex platform hook-reliability gap where the harness itself doesn't surface failures.

## Inspect your substrate

Your partner's memory normally only exists *inside* a session. These commands let you look at it from outside — memory health (is the Hebbian write-path live or silently dark?), the association graph, crystallized patterns, open loops, and your State / Active-Threads narrative. Read-only by default: they inspect, not change (the one exception is `levain serve --write`, the governed writable cockpit, noted below).

```
levain dashboard          # a terminal glance (add --json for the raw view)
levain serve              # a local web-app in your browser — localhost only
```

`levain serve` runs a tiny localhost web server (default `http://127.0.0.1:7420`) and opens your browser to a live dashboard of your own substrate. It binds loopback only, refuses non-loopback Hosts, ships its UI from the package (no CDN, no account, renders offline) — your memory stays on your machine. `Ctrl+C` to stop; `--port` / `--no-open` to adjust. Read-only by default; **`--write`** opts into the governed writable cockpit — operate your State / spores / Tray-Keep right from the browser, through the same governed seam `levain tui` uses (loopback-only, the localhost bind is the auth).

```
pip install 'levain[app]'   # optional: the in-host render
levain serve-app            # serve the same dashboard as an MCP App, inside the host
```

`levain serve-app` is the alternate surface for hosts that render MCP Apps (Claude desktop/web, etc.). It needs the optional `mcp` extra; `levain serve` needs nothing beyond the base install.

## Audience

Operator-class developers — the ~5% who already sense session-amnesia is a real problem and would build their own fix. The ceiling isn't a crack; it's the shape of the correct audience.

If you've already built your own substrate-management scripts, you'll recognize the pieces. If "continuity file" and "wrap protocol" don't land, this isn't your tool yet.

## What's in v1

- Methodology-core seed templates (harness-neutral, small, dense)
- Claude Code adapter (tier-1)
- Codex CLI adapter (tier-1)
- `levain init` / `levain doctor` / `levain verify-hooks` CLI
- Accrual demo showing the empirical growth trajectory

## What's not in v1

- The framework (heartbeat, control pane) — a native flow-shaped framework is parked at v2, held off substitution-drift by one invariant: the human is the fan-in.
- The web-pane onboarding — the CLI interview is the only front-end at v1.0; the web pane absorbs into v1.1.
- Multi-adapter layering — one adapter per install at v1; two installs if you need both Claude Code and Codex.

## Dependencies

Levain layers on [`anneal-memory`](https://github.com/phillipclapham/anneal-memory) — the memory library. Both ship to PyPI. Clean dependency direction: Levain depends on anneal-memory, never the reverse.

The division of labor is the point: **anneal-memory is the substrate; Levain is the harness that fires it.** anneal-memory deliberately can't reach into your session — a memory library that hooked every prompt would forfeit the framework-neutrality that lets it work everywhere. So raw anneal gives you the stores and *manual* recall (you query if you remember to — the dead-store failure mode discipline rots into). Levain wires the hooks that surface the right memory at the right moment automatically — discipline becomes an invariant. Today that fires for the **prospective** layer: an open loop (spore) germinates into context the moment your prompt collides with it. The same hook **will extend** to the **crystallized** pattern tier in v2 — per-turn recall of proven, stable wisdom so it stays effective without clogging always-loaded context. That extension is proven in the flow reference workspace and is Levain v2's opening slice; it is **not** wired into the v1 adapter yet (v1 fires spores, not crystals). This is why "anneal only *fully* works inside a complementary harness" is a feature line, not a caveat: the store is universal, the firing is the harness's job.

## License

Apache 2.0. See [`LICENSE`](LICENSE) for full text and [`NOTICE`](NOTICE) for attribution.

The patent grant matters: as the kit accrues operator-class contributions, downstream operators are protected against future contributor patent ambush. The "second sourdough surface" — the activation layer that the operator edits — is the design intent. Apache 2.0 protects that surface.

---

*Built inside the flow reference workspace; lives at levainhq.com.*
