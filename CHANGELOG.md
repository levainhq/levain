# Changelog

All notable changes to Levain. Format is loosely [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses [SemVer](https://semver.org/spec/v2.0.0.html).

> **This file starts at 0.4.2.** Earlier releases were documented in commit messages only — which is itself one of the defects this release closes: an operator upgrading through 0.4.x had no surface that told them what changed underneath their install. Entries for 0.4.0 and 0.4.1 are backfilled below because they carry a behaviour change adopters needed to know about and were never told.

## [Unreleased]

Stamped `0.4.4.dev0`. **The tree past a release tag no longer claims the released version** — see *Versioning* at the foot of this file.

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

Eight tests failed on every run, on any machine without the optional `mcp` and `openhands` extras, because two import sites lacked the `importorskip` guard used at a dozen others. A permanent red is indistinguishable from a real regression, which made "the suite is green" unusable as evidence. Same passing count, zero failures, and the eight now skip and say why.

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
