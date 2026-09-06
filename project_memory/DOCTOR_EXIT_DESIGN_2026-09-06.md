# `levain doctor` — what its exit code should MEAN

**Written 2026-09-06 by `0906+14 levain-seat`, at Phill's instruction, for Phill and `0906+0 main` to decide from.**

⛔ **THIS IS ANALYSIS AND OPTIONS. NOTHING HERE IS BUILT AND NOTHING HERE IS DECIDED.** Phill's
framing: *"figure out this design question together … and then either build something or get Levain
released."* The decision is his. This document exists so the decision is made against measurement
instead of against a sketch.

⛔ **NOTHING SHIPPED FROM THIS SESSION.** No tag, no build, no upload, no restructuring of
`[Unreleased]` into a cut. The release hold is untouched.

⚖ **THIS IS THE POST-L3 VERSION.** `complement,codex,glm-5.3` reviewed the first draft and returned
one HIGH and ten MED/LOW, with two independent lineages agreeing on four. **Seven of my own claims did
not survive** — including the conclusion I had already relayed to the fan-in. Every finding was
resolved against disk before being accepted; the full list and what changed is §8. **Read §8 before
quoting anything here as measured.**

---

## 0. THE QUESTION AS FILED (`spore-840`)

`levain doctor` exits 1 identically for *"something is broken"* and for *"a routine post-upgrade step
is pending."* An instrument that goes red on the expected path teaches its operator to discount it —
expensive for the one tool we ask operators to trust when something genuinely is wrong.

The measurement is already done and is **not re-derived here** (`[Unreleased]` at `6d715ab`): every
0.4.x passes its own `doctor` at exit 0, and every one gives the identical two FAILs — `hook
freshness`, `compat: levain` — and exit 1 under 0.4.4. Both checks are correct, neither is new,
nothing is broken, both remedies are named in the failure text.

`spore-840` sketched three answers: a distinct exit code · a `[STALE]` severity · change only what
the release notes promise. **None of those is what I would recommend, and the reason is in §2.**

---

## 1. ⚖ THE FRAMING IS WRONG — BUT NOT IN THE WAY I FIRST WROTE IT

> ⛔ **THIS SECTION WAS REWRITTEN AFTER L3.** Its first version made two errors that `glm-5.3` and
> `codex` filed INDEPENDENTLY, and both were right. Both are corrected below and the original wording
> is not preserved, because a decision document should carry the corrected argument, not a debate.
> What the errors were is recorded in §8.

The question assumes a severity scale: `OK < STALE < FAIL`. Doctor is in fact answering two questions:

- **Q1 — is this install FUNCTIONING?** (gate-shaped: is my activation layer actually running?)
- **Q2 — is this install AT THE COMPOSED SET?** (gauge-shaped: is maintenance pending?)

| fact | Q1 functioning? | Q2 at-set? |
|---|---|---|
| hooks are the previous release's copies | unverifiable — see below | NO |
| `compat: levain` — set composed against 0.4.3 | yes | NO |
| `activation scope` — user-level wiring, install scope | **NO — silently dark** | typically YES |
| ⭐ **hook command targets a DIFFERENT install** (`doctor.py`, the `settings <event> → <script>` FAIL, *"command does not target this install's hook script"*) | **NO** | **YES — artifacts fully current** | 
| store corrupt | NO | NO |

⚡ **THE FOURTH ROW IS THE ONE THAT MATTERS AND IT WAS MISSING FROM THE FIRST DRAFT.** `glm-5.3`
pointed out that my original four rows were all co-monotone — they admit the total order
`fresh < stale < dark < corrupt` — so **the table asserted non-collinearity and did not exhibit it.**
The incomparable pair does exist, it is on disk, and it is the foreign-wiring FAIL above: an install
whose artifacts are perfectly current and which is **not functioning at all**. Q1=no, Q2=yes. That
cannot be placed on the same axis as stale-hooks (Q1-unverifiable, Q2=no).

⛔ **AND CODEX KILLED THE CONCLUSION I DREW FROM IT, WHICH IS A SEPARATE POINT AND ALSO CORRECT.**
Even with genuinely cross-cutting predicates, **"not totally ordered" is not "cannot be
represented."** A per-check `[STALE]` badge labels each check on its own axis and imposes no
cross-check ordering; aggregation then needs only a precedence rule
(`hard failure > maintenance-only > success`). **Nobody filed a global severity scale.** So the
cross-cutting observation is a real fact about the domain and it is **NOT** a killer argument against
Option B. B's actual killers are in §5, and they hold on their own.

### ⛔ THE STATE IS **DRIFT**, NOT **UNKNOWN** — and I had this backwards

My first draft said the honest name for hook staleness is `unknown`, citing `manifest.py:139`. **Both
seats refuted it from the line I quoted.** `manifest.py:139` reads
`"unknown",  # a read failed — honesty floor: NOT "in sync"`. **A stale hook is not a failed read.**
The comparison SUCCEEDS and establishes a known fact: the bytes differ from the package's. levain's
own taxonomy already has the right name one line up — `manifest.py:137`,
`"drift",    # reality changed underneath the lock / schema mismatch` — and `drift` is **actionable**
(`manifest.py:150`, `_ACTIONABLE`).

⚡ **The correction strengthens the argument rather than weakening it.** `unknown` says *"cannot be
graded"*, which argues for leaving it red forever. **`drift` says "red, and there is an action" —
which argues FOR building the reconcile path (§2), not for a severity tier.**

⚠ And the first draft used two incompatible readings of the same fact three paragraphs apart — the
table scored stale hooks *"they ran fine yesterday"* (functioning=yes) and the prose then said *"I
cannot tell you whether your activation layer is correct"* (functioning=unknown), **each where it
suited the adjacent argument.** The table above now says `unverifiable` in both places, which is the
honest reading: the hooks demonstrably ran, and whether they carry the CURRENT activation semantics
is what nobody can say.

### ▶ WHAT SURVIVES ALL OF THAT, AND IT IS THE PART THE RECOMMENDATION USES

⛔ **`hook freshness` failing is CORRECT and must stay non-green — on the CARRIER argument, which is
measured and which neither seat disputed.** `spore-840`'s counter-argument is not a caveat to weigh
against softening; it is decisive. The docstring at `doctor.py:1320` proves it with a real case:
Alex's 2026-08-01 `install_root()` bug **silently killed the whole activation layer**, the fix shipped
in the package, and it *reached nobody who merely upgraded*. **A stale hook is exactly how a shipped
fix fails to arrive.**

**So the defect is not that doctor is lying. It is that the operator has no supported way to clear a
correctly-reported drift.** That is §2.

---

## 2. ⚡⚡ THE RELOCATION — THIS IS A MISSING UPGRADE PATH, NOT AN EXIT-CODE PROBLEM

`_check_hook_freshness`'s own docstring says it, and I read it rather than inferred it
(`levain/doctor.py:1320`):

> *"Levain copies artifacts into the operator's install at `init` and has NO upgrade path that
> refreshes them: `update.py` never re-installs, and `reconcile.py` only handles pack drift."*

**Doctor is red on the expected path because the expected path is INCOMPLETE.** `pip install -U
levain` upgrades the package and leaves the install carrying the previous release's artifacts.
Making doctor quieter would silence a correct gate to conceal a missing feature.

⚠ **PRECISION codex FORCED, because the file contradicted itself:** there IS a documented *procedure*
— §3 quotes `init --force` as *"step two of the documented upgrade procedure"*. **What does not exist
is a MECHANISM.** The docs tell the operator to run a second command; nothing in `pip install -U`
runs it, and nothing verifies they did. "No upgrade path" means no mechanism — the loose phrasing
read as a claim that the docs are silent, which they are not.

⛔ **AND THE TWO-FAIL COUNT IS A FLOOR, NOT A PROPERTY OF 0.4.4.** There is a **third** check in the
same family already shipped — `_check_carrier_freshness` (`levain/doctor.py:1248`), whose docstring
opens *"⚠ THE UPGRADE PATH DOES NOT RE-RENDER THIS FILE."* On the upgrade path its only trigger is a
seed file changing classification, and none did between 0.4.3 and 0.4.4 [that diff is record-sourced,
not measured by me]. **A release that moves a seed file eager → on-demand makes it three FAILs.**
⚠ Two qualifications from L3, both correct and both narrowing my original flat claim: it matches the
eager form in the carrier TEXT, so an operator hand-editing the carrier fires it with **no release
involved**; and it iterates the CURRENT `ON_DEMAND_SEED`, so an **on-demand → eager** move would NOT
fire it. The trigger is wider than "a release" in one direction and narrower than "any classification
change" in the other. `_check_hook_freshness`'s docstring names the pattern outright: *"A THIRD SURFACE IN
THE SAME CLASS, and the class is the finding."*

**Measured breadth of the family — CORRECTED AFTER L3, WHERE I HAD INFLATED IT 2.5×.**
`grep -n "levain init --force" levain/doctor.py` returns six lines: `:302 :414 :1291 :1359 :1496
:1584`. `:1359` is docstring narrative; five are live remedies. ⛔ **But five STRINGS are not five
CHECKS**, and I wrote "five checks" under the heading *Measured* — the exact register this document
exists to police. By function boundaries (`grep -n "^def"`): `:302` and `:414` are two branches of
**`_check_recorded_answers`** (`:239`); `:1496` and `:1584` are two branches of
**`_check_hook_freshness`** (`:1320`); `:1291` is **`_check_carrier_freshness`** (`:1248`).
**Five strings, THREE checks.**
⛔ **And `_check_recorded_answers` is not in this family at all** — a missing or mistyped interview
record goes red with no upgrade involved. **THE STALE-COPIED-ARTIFACT FAMILY IS TWO CHECKS: hooks and
carrier.** The argument survives on two; the inflated number is what would have been quoted.

⚖ **Every option that treats the symptom — severity, exit codes, release notes — leaves this family
growing and re-teaches the discounting at each release.**

---

## 3. ⛔⛔ THE FINDING I DID NOT EXPECT: THE REMEDY DOCTOR PRINTS IS KNOWN-HARMFUL

Measured in the shipped hook's own comment (`levain/templates/activation/hooks/_levain_hook.py`,
the Activation-scope block):

> *"He was carrying a local patch to this file, which `levain init --force` replaced on each upgrade
> — that command rewrites the whole activation/ tree and is step two of the documented upgrade
> procedure, **so following our own instructions is what ate it.**"*

The one external operator levain has already lost work to the remedy doctor currently prints. And the
backup does not cover it:

- `levain/install.py:1597` — `_OPERATOR_EDITABLE = ("posture.md", "recency_directives.md")`
- `levain/install.py:1712` — `for name in _OPERATOR_EDITABLE:`

**The pre-`rmtree` backup covers exactly two markdown files. A patched hook script is deleted with no
backup and no warning.**

⛔⛔ **AND IT IS NOT ONE OPERATOR ONCE — LEVAIN'S OWN SHIPPED CODE SAYS SO.** Added after
`0906+8 fanin` verified this section independently and found evidence I had missed; **coordinates
re-derived by me with `grep -n`, not adopted:**
`levain/templates/activation/hooks/_levain_hook.py:316` — *"an operator carrying local edits to their
installed hooks (Alex De Groodt was…)"* · **`:319` — *"Removing it is a breaking change to a file
operators demonstrably patch."***

⚡ **So the shipped hook DOCUMENTS that operators demonstrably patch this file, and reasons about
backward compatibility on that basis — while the pre-`rmtree` backup covers two markdown files that
are not it.** The product knows the behaviour exists, protects the wrong artifacts, and prints a
remedy whose reassurance enumerates only what it does protect.

⚠ That is *correct policy* on the narrow question — hooks are machinery, and the product's answer to
Alex's need was a config channel (`.levain/config.json {"scope": "global"}`) which shipped. It is not
a policy defect.
**It is a NOTICE defect, and it is `a_true_statement_standing_where_a_thing_should_be` in a shipped
operator-facing string.** `_check_hook_freshness`'s printed hint says:

> *"(your store, seed answers and activation markdown edits are kept)"*

Every clause is true. It enumerates what is kept, and the reader supplies *"…so nothing of mine is
lost."* What is not kept, and not named, is a patched hook.

▶ **This is separable from the design decision and can be authorised on its own** — see Option F.

---

## 4. ⚖ THE `spore-751` DEPENDENCY: I ASKED, AND THE ANSWER IS THAT IT DOES **NOT** GATE THIS

I routed a question through the fan-in asking whether `spore-751`'s PATH-resolution defect makes
doctor's version reading unreliable as an input to a severity decision. **The relayed answer was yes.
I re-derived it and the correction is mine to make: it is yes for the ANNEAL axes and NO for the axis
that actually goes red on upgrade.** [MEASURED HERE]

- `levain/manifest.py:58` — `from levain import __version__`
- `levain/manifest.py:354` — `levain=__version__` inside `discover_installed_set`
- `levain/manifest.py:255` — `levain=__version__` inside `declared_set`
- `levain/manifest.py:251` — *"``levain`` is the SOURCE ``__version__`` (not ``importlib.metadata``…)"*

**The `levain` axis is an in-process constant. It never touches `PATH`.** `shutil.which("anneal-memory")`
lives at `levain/doctor.py:811` (and `:689`) and feeds the **anneal** axes only.

⭐ **So the two checks that go red on a correct upgrade read from the most trustworthy sources in the
whole doctor: local filesystem content hashes, and an in-process version constant.** The reading
`spore-751` corrupts is the anneal one — and `ahead` is *already* advisory-green at
`levain/doctor.py:833` (`advisory = {"pending", "ahead"}`).

⛔⛔ **AND HERE IS WHERE I OVERREACHED, CAUGHT BY CODEX AT L3 AND CONFIRMED ON DISK. THE CORRECTION
IS THE MOST IMPORTANT THING IN THIS SECTION.**

I proved independence for the **READ** path and then stated it as a conclusion about **THE DECISION**.
Those are not the same claim, and D is a **WRITE**.

[MEASURED HERE, after codex filed it]:
- `levain/install.py:333` — `anneal_path = shutil.which("anneal-memory") or "anneal-memory"`
- `levain/install.py:1790` — `_substitute_hook_placeholders(new_tree / "hooks", {"{{ANNEAL_MEMORY}}": anneal_path})`
- `levain/install.py:1482` and `:1538` — the same value substituted into the MCP config.

⚡ **THE RENDER PATH TAKES A `shutil.which()` RESULT AND BAKES IT INTO EVERY HOOK SCRIPT ON DISK.**
That is `spore-751`'s exact mechanism, on the write side. **Automating the re-render (either D1 or
D2) automates baking one machine's PATH lookup into the operator's hooks** — and on an operator whose
`~/.local/bin/anneal-memory` is stale, it writes the stale one in and records nothing.

⚖ **SO THE HONEST SPLIT, AND IT IS NARROWER THAN WHAT I FIRST WROTE AND RELAYED:**
- **`spore-751` does NOT gate the DIAGNOSIS.** The two checks that go red on upgrade read from an
  in-process constant and local file content. That measurement stands unchallenged.
- ⛔ **`spore-751` DOES bear on BUILDING D**, because D writes hooks through the very substitution
  that resolves anneal by `PATH`. **Do not call them independent because the read-side axis is.**

▶ **CONSEQUENCE FOR §7's SEQUENCING QUESTION: it is no longer a free preference.** If D is built, it
should either follow the install-path pass or explicitly adopt whatever single anneal authority that
pass decides — otherwise D ships the split-brain into every operator's hooks on a schedule.

⚠ **I RELAYED THE OVERREACHED FORM TO THE FAN-IN AND IT WAS ACCEPTED. It has been corrected.** This
is the third hop of the same class inside one session — measure the reachable half, conclude about
the whole — which is precisely the class §1 is about. **Knowing the class did not stop me producing
it, and only an outside seat caught it.**

⚠ **What the fan-in raised that still stands, filed not resolved:** anneal has no `last_writer_version`
field, so doctor's anneal reading says *which binary `PATH` found*, never *which binary writes the
store* — two states with opposite severities, indistinguishable. That is a real defect in the ANNEAL
axis's severity and it belongs to the install-path pass, not to this question. [RELAYED, unverified
by me — the anneal seat was stopped at ~11:00. Re-derive before building on it.]

---

## 5. THE OPTIONS, WITH COSTS

### A — Ship nothing; the `[Unreleased]` CHANGELOG correction is the whole answer
- **Cost:** the trigger recurs at **every** release. Each one re-teaches every operator that red is
  normal, and the release notes carry a standing apology.
- **Right about:** preserves the honesty floor exactly; zero risk; already written.
- **Verdict:** the correct **floor**, already shipped. Not sufficient alone — but it is a real option
  if the answer is "not now."

### B — A `[STALE]` third badge beside `[OK]` / `[FAIL]`
- **Cost, measured:** `CheckResult.ok: bool` is `levain/doctor.py:51`; its consumers are exactly three
  — `:57` (badge), `:59` (hint), `:124` (`failed = [...]`). Production cost is small.
  ⚠ **AUDIT SURFACE, NOT CHURN — corrected after L3, where I had overstated it.** 54 `.ok` references
  in `tests/test_doctor_content.py` and 81 `CheckResult(` sites are what a tri-state would have to be
  *checked against*; with a default mapping from the existing boolean, **most stay valid** and the
  real cost is output compatibility plus mixed-state aggregation. I counted the audit surface and
  called it the cost.
- ⛔ **Killer:** it does not change the exit code. The operator's habit and every pipeline still see 1.
  It is a cosmetic answer to a contract problem.
- ⛔ **AND THE "SECOND KILLER" I WROTE HERE IS WITHDRAWN.** It said a badge imposes an ordering on
  cross-cutting predicates. **codex is right that it does not:** a per-check badge labels each check
  on its own axis, and aggregation needs only a precedence rule
  (`hard failure > maintenance-only > success`). **"Not totally ordered" is not "cannot be
  represented", and nobody filed a global severity scale.** The cross-cutting fact in §1 is real and
  is not an argument against B.
- **Verdict:** recommend against — on the exit-code point and on §1's carrier argument, which stand.
  ⚠ **Not on the ordering argument, which was mine and was wrong.**

### C — A distinct exit code for the maintenance-pending class
- **Precedent is strong and in-repo:** `levain/session.py:107–165` already defines
  `EXIT_OK=0 · EXIT_NO_REPLY=1 · EXIT_USAGE=2 · EXIT_TURN_FAILED=3 · EXIT_GATED=4 · EXIT_TIMEOUT=5 ·
  EXIT_INTERRUPTED=130`, framed as *"the process-level contract for every non-interactive driver."*
  `EXIT_GATED`'s docstring argues this exact move: *"Its own code, and that is the point."*
- **Cost:** doctor currently returns raw `0`/`1` (`levain/doctor.py:154`) and has no `--json` and no
  `--quiet` — the exit code is its only machine channel (CLI surface is `--path` and `--invoke` only,
  `levain/cli.py:156–183`). A new code lands in a namespace claiming to be CLI-wide, so it should
  extend that vocabulary rather than open a second one.
- ⛔ **RE-COSTED AFTER L3. MY FIRST TWO WORDS FOR C — "buys nothing" AND "safe" — WERE BOTH FALSE, AND
  BOTH SEATS SAID SO INDEPENDENTLY.** I had written that existing pipelines see nonzero either way and
  behave exactly as today. That holds only for **truthiness** consumers (`levain doctor && …`,
  `if levain doctor`, `|| alert`). It is false for **enumerating** ones:
  ```
  levain doctor; case $? in 0) : ;; 1) page_oncall ;; esac
  ```
  Today maintenance-pending exits 1 and pages. Under C it exits a new code, **matches no branch, and
  falls through silently** — so C (a) DOES buy something for an already-written pipeline: it
  de-alerts the expected path, which is the *pipeline* half of `spore-840`'s harm that I asserted was
  human-only; and (b) is **NOT "safe"** — a script whose `1)` branch runs a REPAIR now silently skips
  it.
- ▶ **The fair costing: behaviour-preserving for truthiness consumers · behaviour-CHANGING for
  enumerating ones, in the intended direction for alerting and the wrong direction for repair ·
  value-ADDING for any consumer updated to know the new code.** It is a compatibility migration, not
  a free rider.
- **Verdict:** still weak on its own — the human harm `spore-840` names is read off the red, not off
  `$?`. But **"cheap, safe, low value" was unfair to it**, and Phill should see it costed as a
  migration if he weighs it.

### D — ⭐ CLOSE THE UPGRADE PATH so the expected path reaches green
The option the measurement actually points at, and the only one that shrinks the family in §2.

- **D1 — a `levain upgrade` command.** One step that re-renders the install and reconciles the set.
  The documented path becomes `pip install -U levain && levain upgrade`.
  ⚠ **"Doctor goes green after it" is the GOAL, not a property the option confers** — it holds only if
  the command covers EVERY init-only artifact. Cover hooks and leave the carrier and the expected path
  is still red, one release later, for a different population. That is why the carrier question below
  is part of this option and not a follow-up.
  - *Cost:* a new CLI verb (surface growth on a product whose discipline is subtraction), and it must
    carry `init --force`'s fail-loud backup contract. Docs, operator manual, README, and the shipped
    adapter READMEs all move together.
- **D2 — fold the hook re-render into `levain update`, scoped to `activation/hooks/` only.**
  No new verb. The machinery/operator-owned split is enforced by **directory** — `hooks/` is
  machinery, `activation/*.md` is operator-owned — so the shipped promise in
  `levain/templates/docs/operator-manual.md:139` (*"It does not rewrite your partner's activation
  files"*) survives intact, because `posture.md` and `recency_directives.md` are not under `hooks/`.
  - ⛔ *Cost, and it is real — but stated precisely, because the loose version overstates it:* `update`
    ALREADY writes files (`.levain/manifest.json`, and pack reconciles under `seed/`). **What it has
    never written is `activation/`** — verified, not quoted: `levain/update.py` contains ZERO
    references to `apply_init`, `_install_adapter`, `_copy_activation_tree` or any `levain.install`
    import, and `levain/reconcile.py:296–297` SURFACES an `activation/` change rather than applying
    it (`:375` — *"the adapter @import list is NOT regenerated"*). So D2 does not make a read-only
    command destructive; it extends a writing command across the one boundary it has always
    respected. That is a smaller step than "mutate the install tree" implies, and still a real one:
    today `update` is the SAFE
    command and `init --force` is the destructive one, and operators are told so. Automating the hook
    re-render makes §3's silent patch-destruction **automatic and less visible** — so D2 must ship a
    hook backup, which does not exist today (`_OPERATOR_EDITABLE` is two markdown files).
  - ⛔ *And it must not be built naively:* the tree an adapter renders from is
    `order_activation_roots(templates_root, _base_activation_root(...), pack_dirs)` — defined at
    `levain/packs.py:340`, called from `levain/install.py:388`, quoted from `levain/doctor.py:1372`.
    (It is NOT defined in `install.py`; a `grep` scoped there returns nothing and reads as a phantom.)
    The
    `_check_hook_freshness` docstring records that **taking half that chain shipped twice** — 0.4.0
    (wrong-tree for every codex install, permanent exit 1) and 0.4.1 (pack-owned hooks reported stale,
    `init --force` unable to clear it). Reuse the helper whole; do not re-derive it.
- ⛔ **D3 — AND THIS IS THE PREREQUISITE BOTH OF THEM NEED, WHICH I HAD WRONG UNTIL I MEASURED THE
  RENDER PATH. `levain/install.py:1640` `_copy_activation_tree` composes the WHOLE activation tree and
  swaps it in atomically (its own docstring: staging dir, atomic swap, fail-loud on a vanished source).
  It has NO subtree filter** — `_activation_excluded` (`levain/install.py:1600`) skips only
  `__pycache__` and `*.pyc`. Its two callers are `levain/install.py:1458` and `:1503`.
  - ⚖ **So "scoped to `hooks/` only" is not a narrower call to an existing function.** It needs either
    a new mode on a function whose atomic-swap and fail-loud contracts are load-bearing, **or** a
    second, narrower render path — and a SECOND COPY OF THE LAYERING RULE is precisely what shipped
    as a defect twice (`_check_hook_freshness`'s docstring, 0.4.0 and 0.4.1).
  - ⚡ **That inverts the comparison I wrote one bullet up: on the RENDER MECHANISM, the verb is the
    cheap part and the narrowing is the expensive part.** D1 can reuse the render path unchanged;
    D2 cannot.
  - ⛔ **BUT codex IS RIGHT THAT THIS COMPARES TWO DIFFERENT JOBS, AND THE COMPARISON IS NOT FAIR AS
    WRITTEN.** D2 as described updates hooks only. A real one-step `levain upgrade` (D1) must
    additionally recover adapter + pack composition, reuse the recorded answers, regenerate the
    carrier, preserve operator edits, reconcile the compatibility set, and define partial-failure
    behaviour. **"D1 reuses the render path unchanged" compares a full upgrade workflow to a subtree
    operation and silently drops D1's orchestration cost.** ▶ **Before choosing, compare them against
    the SAME required postconditions** — that list is the actual specification of D and neither
    option is small once it is written down.
  - ⛔ **But the binding constraint is neither, and it is the same for both: §3.** D1 is "run
    `init --force` for you automatically", which makes the known patch-destruction automatic. D2 hits
    it too the moment it writes `hooks/`. **The backup covers two markdown files
    (`install.py:1597`), and a hook is not one of them.**
  - ▶ **THEREFORE: WIDEN THE BACKUP BEFORE AUTOMATING ANY RE-RENDER.** Small, independently valuable,
    and it is the honest first step of D whichever shape D takes. It is also the only part of D that
    is worth doing even if D is declined.

- **Either D also needs a decision on the CARRIER** (`_check_carrier_freshness`, `doctor.py:1248`) —
  same family, currently latent. Covering hooks and leaving the carrier is
  `guard_scoped_by_symptom_misses_the_class`, which this file has already shipped twice.

### E — The composite (what I would recommend; see §6)
D + A, with **no** severity tier and **no** exit-code change. C optional as a rider.

### F — The independent small fix: correct the hint's reassurance
`_check_hook_freshness`'s printed hint enumerates what `init --force` keeps and omits that a patched
hook is deleted without backup (§3). A string change, no design commitment, authorisable on its own
whatever is decided about D.
- ⚠ Touches shipped operator-facing copy → `spore-435` fires: read
  `projects/levain/reference/launch_narrative.md` before writing it.

---

## 6. ▶ RECOMMENDATION (a recommendation, not a decision)

**E: close the upgrade path (D), keep the CHANGELOG correction as the transitional note (A), and
change NEITHER the exit code NOR the severity vocabulary.**

Because:
1. Doctor is not lying. §1 — the red is correct **on the carrier argument** (a stale hook is how a
   shipped fix fails to arrive, measured from the `install_root` case), and `spore-840`'s
   counter-argument is decisive rather than a caveat. ⚠ **Not on the "honesty floor / UNKNOWN"
   reasoning I first used — that was withdrawn at L3.** The state is `drift`, which is actionable,
   **and that argues FOR the reconcile path rather than for leaving it red.**
2. The problem is a missing mechanism, not a misgrading. §2 — the stale-artifact family is **two**
   checks today (hooks, carrier), the carrier member is shipped and latent, and **the family grows
   while the upgrade path stays absent.** ⚠ Two, not the five I first wrote.
3. A severity tier does not move the exit code, which is where the contract lives. ⚠ **Not because
   it "imposes an ordering" — that argument was mine and L3 withdrew it (§5-B).**
4. ⚠ **STATED CONDITIONALLY, because the flat version was true only by redefining its own terms.**
   After D the **DOCUMENTED** path reaches green. **The bare `pip install -U levain` stays red** —
   and that is the path §0's measurement was taken on and the one most operators run. What changes is
   that the red becomes **clearable by a supported command instead of permanent**. That is the
   honesty-preserving outcome, and it is a smaller and truer claim than "the FAILs stop appearing."
   **The honesty floor is preserved by making the install honest, not by making the instrument
   quiet.**

⛔ **What I would NOT do under any option: soften, downgrade, or exempt `hook freshness`.** It is the
carrier for shipped fixes reaching operators; it exists because one did not.

▶ **AND D HAS AN ORDER, WHICH THE MEASUREMENT SETTLED RATHER THAN PREFERENCE:**
**D3 first — widen the backup past two markdown files.** Automating a re-render on top of today's
backup scope automates §3's silent patch-destruction, and that is true of D1 and D2 equally. D3 is
small, it is the only part of D worth doing even if D is declined, and it is the difference between
"we shipped an upgrade path" and "we shipped a thing that eats operator patches faster."
Then the shape: **D1 reuses the atomic render unchanged; D2 must either modify it or duplicate the
layering rule that has already shipped as a defect twice.** If both are on the table, D1 is the
cheaper build and D2 is the tidier product surface — that trade is Phill's, not mine.

**If the answer is "not now":** A alone is coherent and costs nothing. `spore-840` stays open, and the
`[Unreleased]` note keeps operators correctly informed in the meantime. ⭐ **D3 and F still stand on
their own in that world** — both are small, neither commits to a design, and both address harm that is
happening to a real operator now.

---

## 6b. ⚖ THE STRONGEST CASE AGAINST §6, STATED PROPERLY BECAUSE I AM THE ONE ARGUING THE OTHER SIDE

A decision document that only argues its own recommendation is a closing argument, not an aid. This is
the position I would take if I wanted to beat §6, and **I think it is genuinely strong:**

> **§1 proves the urgency away.** If doctor is telling the truth — and §1 argues exactly that — then
> the red is *correct*, the operator is *correctly informed* by the `[Unreleased]` note, and nobody is
> currently harmed by the exit code. What remains is that the red is **annoying and pedagogically
> corrosive over many releases**. That is real, and it is not urgent.
>
> **Meanwhile §3 IS urgent and D is not what fixes it.** A patched hook is being deleted with no
> backup, against the one named live operator. **F and D3 fix that. Neither requires deciding the
> design question at all.**
>
> **And D is a substantial build on a repo one decision away from release.** `spore-840` was filed
> as *"written down, deliberately not built"* precisely so it would not become the thing that holds
> the cut. Choosing E makes the design question the release's dependency — which is the outcome the
> spore was written to avoid.

▶ **THE ORDER THAT POSITION IMPLIES: D3 → F → release → D as its own piece of work later.**

⚖ **I do not think it beats §6 on the merits, and I do think it beats §6 on SEQUENCING — and Phill's
own sentence ends with *"or get Levain released."*** My honest position: **§6 is the right answer to
the question asked; the case above is the right answer to the question of what to do THIS WEEK.**
They are not in conflict unless someone treats D as a release blocker, and **nothing here should.**

⛔ **What would change my mind about §6 itself** (as opposed to its timing): evidence that operators
DO read `$?` — i.e. that anyone has scripted `levain doctor` into a pipeline where a maintenance-
pending exit would misfire. That would make Option C load-bearing rather than cosmetic, and I have
no such evidence either way. **The README ships the pipeline promise (`README.md:113`), so the
capability is advertised; whether it is used is unmeasured, and Alex is the only person who could
answer it.**

---

## 7. ⚖ WHAT NEEDS PHILL

1. **D or not-D**, and if D, **D1 (new verb) or D2 (fold into `update`)**. This is a product-surface
   decision, not a code one. ⚠ Note the build costs run OPPOSITE to the surface costs (§5 D3): D2 is
   the tidier surface and the more expensive build.
   ⛔ **And D3 — widening the activation backup past two markdown files — is a YES-OR-NO of its own,
   worth taking even if D is declined.**
2. **Does D cover the carrier as well as the hooks**, or hooks only?
3. **Option F on its own** — authorise the hint correction independently of everything above?
4. ⛔ **Sequencing against the install-path pass** (`spore-751` + `spore-775` + `spore-724`) — **NO
   LONGER A FREE PREFERENCE.** §4's correction: D **writes hooks through the very `shutil.which()`
   substitution `spore-751` is about** (`install.py:333` → `:1790`). So D either follows that pass or
   explicitly adopts whatever single anneal authority it decides. ⭐ **Which is where the outgoing
   levain seat put it — design the trio together — and my first draft argued against that on a
   measurement covering only the read path.**
5. ⛔ **A CONCURRENCY COST OF D THAT NOBODY HAD NAMED** (codex-only, confirmed on disk):
   `levain/install.py:1808` `os.replace(dst, old_aside)` then `:1809` `os.replace(new_tree, dst)` —
   **two renames, not an atomic directory exchange.** Between them the activation path does not
   exist, so a harness starting a hook in that window loses activation for that session. Rollback
   exists (`:1812–1813`) and does not close the window. Today `init --force` is rare and operator-
   initiated; **D would exercise this on every upgrade.** Quiesce, retry, or an indirection design is
   part of D's scope, not a follow-up.

⚠ **[RELAYED, NOT VERIFIED BY ME — provenance-marked after L3 flagged that I had applied my own
discipline everywhere except the highest-stakes adjacent claim.]** The release hold is a separate
question. I am told by `0906+8 fanin`, citing `0906+0 main`, that all three of Alex's messages were
read and contain no bug reports. **I have not read them and this analysis does not depend on it —
whoever owns the hold should confirm it from the source rather than from this file.**

---

## 8. PROVENANCE

**[MEASURED HERE, 2026-09-06, every coordinate from `grep -n` — none hand-counted from a `sed`/`awk`
window]:** `doctor.py:51 :57 :59 :124 :154 :689 :811 :833 :855 :1248 :1320 :1591` ·
`manifest.py:58 :135 :137 :139 :150 :251 :255 :354 :389` · `install.py:1597 :1712` ·
`session.py:107–165` · `cli.py:156–183` · `packs.py:340` · `install.py:388 :333 :1482 :1538 :1790 :1808 :1809 :1812–1813` ·
`doctor.py:1372` · **`update.py` (grepped: ZERO references to `apply_init` / `_install_adapter` /
`_copy_activation_tree` / any `levain.install` import)** · **`reconcile.py:296–297 :375`** ·
the `init --force` remedy sites `:302 :414 :1291 :1359
:1496 :1584` · counts: 3 `.ok` consumers in `doctor.py`, 54 in `tests/test_doctor_content.py`,
81 `CheckResult(` sites. Tree state at writing: `HEAD 6d715ab`, clean, 0 ahead of upstream.

**[FROM THE RECORD, not re-measured — deliberately, per instruction]:** the 0.4.x upgrade
measurement in `CHANGELOG.md` `[Unreleased]` at `6d715ab`.

⚠ **L0 ON THIS DOCUMENT CAUGHT TWO OF ITS OWN CLAIMS, AND BOTH ARE TODAY'S DOMINANT CLASS.** Recorded
rather than silently fixed, because the mechanism is the lesson.
1. **Off by one.** I wrote `cli.py:155–183`. `awk 'NR>=155'` printed the parser line *first* — but line
   155 is BLANK, so the first line I *saw* was line 156. **A window's first VISIBLE line is not its
   first line.** A blank leading line is invisible in rendered output, and that is exactly how a
   hand-read offset lands on a plausible neighbour. Corrected against `grep -n`.
2. **A phantom that was not one.** I grepped `def order_activation_roots` in `install.py`, got nothing,
   and nearly filed the shipped docstring at `doctor.py:1372` as citing a dead symbol. **The address
   moved, the claim did not** — it is defined at `packs.py:340` and called from `install.py:388`. The
   rule held: re-grep the CLAIM, not the coordinate. The file names are written down above so the next
   reader does not repeat my failed grep.

⛔⛔ **L3 RAN AGAINST THIS DOCUMENT AND IT WAS NOT A FORMALITY — `complement,codex,glm-5.3`, ONE HIGH
AND TEN MED/LOW, WITH TWO INDEPENDENT LINEAGES AGREEING ON FOUR.** Every finding was resolved against
disk before being accepted. What it changed, so a reader knows which parts of this file were wrong
ninety minutes ago:
1. ⛔ **§4's independence conclusion covered only the READ path.** codex found that D WRITES through
   `shutil.which()` (`install.py:333` → `:1790`). Confirmed on disk. **The conclusion I relayed to the
   fan-in was too broad and has been walked back.**
2. ⛔ **§1's "the honest name is UNKNOWN" was a smuggle, refuted by both seats from the line I
   quoted** — `manifest.py:139` reads *"a read failed"*. The right name is `drift` (`:137`), which is
   ACTIONABLE, and the correction argues FOR D rather than against grading.
3. ⛔ **§1's table asserted non-collinearity without exhibiting it** (all four rows were co-monotone).
   The incomparable row was added from disk. And **the conclusion I drew from it — that a badge is
   therefore incoherent — is withdrawn**: per-check badges plus a precedence rule represent it fine.
4. ⛔ **§2's "five checks" was five STRINGS across THREE checks; the real family is TWO.** A 2.5×
   inflation under the heading *Measured*.
5. ⛔ **§5-C was mis-costed** ("buys nothing", "safe" — both false for `case $?` consumers).
6. ⛔ **§6.4 was true only by redefining "expected path"**; now stated conditionally.
7. ⛔ **§7's release-hold sentence carried no provenance marker**; now marked RELAYED.
8. ⭐ **codex-only, the class it is non-replaceable for:** the activation swap is two `os.replace`
   calls, not an atomic exchange — added to §7 as a cost of D.
⚠ **The target DRIFTED under both seats** — I kept editing while they reviewed, so their line numbers
are stale by design and their CLAIMS were re-grepped rather than their coordinates. That is my process
error and it is why the ledger below is the authority, not their citations.
⚖ **ROUND-2 DECISION, PRE-REGISTERED BEFORE ANY ROUND-2 OUTPUT WAS READ: NO ROUND 2.** Prose, nothing
ships from it, findings dense and mostly consensus, and the decision-maker is waiting on the corrected
version. Recorded here because deciding after reading is choosing the answer you already saw.

**[RELAYED, NOT VERIFIED BY ME]:** anneal's missing `last_writer_version` and the 0.9.9 /
0.9.10.dev0 skew, from `0906+8 fanin` citing `0906+6 anneal-memory-seat`'s close report. That seat is
stopped. **Do not treat it as load-bearing without re-deriving it.** Nothing in §5 or §6 depends on it.
