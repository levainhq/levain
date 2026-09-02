# Changelog

All notable changes to Levain. Format is loosely [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses [SemVer](https://semver.org/spec/v2.0.0.html).

> **This file starts at 0.4.2.** Earlier releases were documented in commit messages only — which is itself one of the defects this release closes: an operator upgrading through 0.4.x had no surface that told them what changed underneath their install. Entries for 0.4.0 and 0.4.1 are backfilled below because they carry a behaviour change adopters needed to know about and were never told.

## [Unreleased]

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

### Upgrading

- **`pip install -U levain` will also upgrade `anneal-memory` to >= 0.9.8.** This is a real API contract, not a lockstep bump: the new `[wrap blocked]` advice names the `wrap_cancel` MCP tool, which does not exist before anneal 0.9.8. Against 0.9.7 the hook would name a tool the agent cannot reach — precisely the defect this pair of releases exists to fix.
- **If you wired Levain's hooks globally and your activation layer has been quiet, this is why.** Add `{"scope": "global"}` to `.levain/config.json`, then run `levain doctor` — it now tells you which way it is set.
- **If you are carrying a local patch to `in_install_session()`, drop it.** `levain update` overwrites the hook templates, which is what kept eating it; the supported opt-in survives updates.

## [0.4.1] — 2026-08-02

**`doctor` is no longer permanently red for Codex operators.** `_check_hook_freshness` compared every install's hooks against the Claude Code template tree, so every Codex install failed on a false "stale hooks" report. It shipped in 0.4.0 — the release built to carry this same reporter's previous fixes.

## [0.4.0] — 2026-08-01

The governed seat, the efferent gate, and three field-reported fixes.

> ⚠ **UNDOCUMENTED AT THE TIME, AND IT CHANGED DEPLOYMENTS SILENTLY.** This release deleted the hook's `$CLAUDE_PROJECT_DIR` read. That read's containment check accepted **any ancestor of the hook file** as the install root, which could resolve the whole activation layer under the wrong root — so an install with globally-wired hooks may have been passing the session gate *by accident*. Tightening `install_root()` to derive from the hook file's own location was correct, but for anyone in that position it turned activation off, with no note in any release material saying so and `doctor` still reporting healthy. **0.4.2 is the release that gives that operator a supported way to get the behaviour back** (`scope: "global"`), and a `doctor` check that says which way the gate is set.
