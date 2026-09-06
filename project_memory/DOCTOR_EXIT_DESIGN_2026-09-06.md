# `levain doctor` — what its exit code should MEAN

**Written 2026-09-06 by `0906+14 levain-seat`, at Phill's instruction, for Phill and `0906+0 main` to decide from.**

⛔ **THIS IS ANALYSIS AND OPTIONS. NOTHING HERE IS BUILT AND NOTHING HERE IS DECIDED.** Phill's
framing: *"figure out this design question together … and then either build something or get Levain
released."* The decision is his. This document exists so the decision is made against measurement
instead of against a sketch.

⛔ **NOTHING SHIPPED FROM THIS SESSION.** No tag, no build, no upload, no restructuring of
`[Unreleased]` into a cut. The release hold is untouched.

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

## 1. ⚖ THE FRAMING IS WRONG, AND CORRECTING IT DISSOLVES HALF THE PROBLEM

The question assumes a severity scale: `OK < STALE < FAIL`. That assumes the two questions doctor is
being asked are collinear. **They are not.** Doctor is answering two independent predicates:

- **Q1 — is this install FUNCTIONING?** (gate-shaped: is my activation layer actually running?)
- **Q2 — is this install AT THE COMPOSED SET?** (gauge-shaped: is maintenance pending?)

Run the failing checks through both:

| fact | Q1 functioning? | Q2 at-set? |
|---|---|---|
| hooks are the previous release's copies | **they ran fine yesterday** | NO |
| `compat: levain` — set composed against 0.4.3 | yes | NO |
| `activation scope` — user-level wiring, install scope | **NO — silently dark** | irrelevant |
| store corrupt | NO | NO |

The predicates **cross-cut**. A single ordered badge (`OK`/`STALE`/`FAIL`) imposes an ordering on two
independent axes — which is `the_measurement_is_of_the_wrong_object` committed while trying to fix an
instance of it.

⚡ **And the honest name for the hook-staleness state is not "stale-but-expected". It is UNKNOWN.**
Stale hooks mean *"I cannot tell you whether your activation layer is correct, because the code
running it is not the code I know."* That is not fine and it is not broken. levain has already ruled
on this state and given it a name — `manifest.py:139`, `"unknown"  # a read failed — honesty floor:
NOT "in sync"`.

⛔ **Under levain's own honesty floor, `hook freshness` failing is CORRECT and must stay non-green.**
`spore-840`'s counter-argument ("stale hooks mean the OLD activation layer is genuinely running") is
not a caveat to weigh against softening — it is decisive, and the docstring at `doctor.py:1320`
proves it with a real case: Alex's 2026-08-01 `install_root()` bug **silently killed the whole
activation layer**, the fix shipped in the package, and it *reached nobody who merely upgraded*. A
stale hook is exactly how a shipped fix fails to arrive.

**So the defect is not the severity. Doctor is telling the truth.**

---

## 2. ⚡⚡ THE RELOCATION — THIS IS A MISSING UPGRADE PATH, NOT AN EXIT-CODE PROBLEM

`_check_hook_freshness`'s own docstring says it, and I read it rather than inferred it
(`levain/doctor.py:1320`):

> *"Levain copies artifacts into the operator's install at `init` and has NO upgrade path that
> refreshes them: `update.py` never re-installs, and `reconcile.py` only handles pack drift."*

**Doctor is red on the expected path because the expected path is INCOMPLETE.** `pip install -U
levain` upgrades the package and leaves the install carrying the previous release's artifacts.
Making doctor quieter would silence a correct gate to conceal a missing feature.

⛔ **AND THE TWO-FAIL COUNT IS A FLOOR, NOT A PROPERTY OF 0.4.4.** There is a **third** check in the
same family already shipped — `_check_carrier_freshness` (`levain/doctor.py:1248`), whose docstring
opens *"⚠ THE UPGRADE PATH DOES NOT RE-RENDER THIS FILE."* It did not fire on 0.4.3 → 0.4.4 only
because no seed file changed classification between them. **A release that changes one will make it
three FAILs.** `_check_hook_freshness`'s docstring names the pattern outright: *"A THIRD SURFACE IN
THE SAME CLASS, and the class is the finding."*

**Measured breadth of the family:** `grep -n "levain init --force" levain/doctor.py` returns six
lines — `:302 :414 :1291 :1359 :1496 :1584`. `:1359` is narrative inside a docstring; **five are live
operator-facing remedies.** Five checks point at one command that the upgrade path does not run.

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

⚠ That is *correct policy* — hooks are machinery, the product's answer to Alex's need was a config
channel (`.levain/config.json {"scope": "global"}`) and it shipped. It is not a policy defect.
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

⛔ **`spore-751` DOES NOT GATE THIS DECISION.** The install-path pass (`spore-751` + `spore-775` +
`spore-724`) can be sequenced independently, in either order. That is a loosening of the constraint I
was handed, derived by measurement.

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
  — `:57` (badge), `:59` (hint), `:124` (`failed = [...]`). Production cost is small. **The test cost
  is the real one: 54 `.ok` references in `tests/test_doctor_content.py`.** 81 `CheckResult(` sites to
  audit for which severity each should carry.
- ⛔ **Killer:** it does not change the exit code. The operator's habit and every pipeline still see 1.
  It is a cosmetic answer to a contract problem.
- ⛔ **Second killer:** it imposes an ordering on two independent predicates (§1).
- **Verdict:** recommend against.

### C — A distinct exit code for the maintenance-pending class
- **Precedent is strong and in-repo:** `levain/session.py:107–165` already defines
  `EXIT_OK=0 · EXIT_NO_REPLY=1 · EXIT_USAGE=2 · EXIT_TURN_FAILED=3 · EXIT_GATED=4 · EXIT_TIMEOUT=5 ·
  EXIT_INTERRUPTED=130`, framed as *"the process-level contract for every non-interactive driver."*
  `EXIT_GATED`'s docstring argues this exact move: *"Its own code, and that is the point."*
- **Cost:** doctor currently returns raw `0`/`1` (`levain/doctor.py:154`) and has no `--json` and no
  `--quiet` — the exit code is its only machine channel (CLI surface is `--path` and `--invoke` only,
  `levain/cli.py:156–183`). A new code lands in a namespace claiming to be CLI-wide, so it should
  extend that vocabulary rather than open a second one.
- ⚠ **The honest problem, and it surprised me:** existing pipelines see *nonzero* either way and
  behave exactly as today, so **it buys nothing for anyone who has already written one.** And the harm
  `spore-840` names is **human** — *"teaches its operator to discount it."* A human does not read `$?`.
  They read the red.
- **Verdict:** cheap, safe, low value. Viable as a rider on D; weak on its own.

### D — ⭐ CLOSE THE UPGRADE PATH so the expected path reaches green
The option the measurement actually points at, and the only one that shrinks the family in §2.

- **D1 — a `levain upgrade` command.** One step that re-renders the install and reconciles the set.
  The documented path becomes `pip install -U levain && levain upgrade`; doctor goes green after it;
  a red doctor means what it says again.
  - *Cost:* a new CLI verb (surface growth on a product whose discipline is subtraction), and it must
    carry `init --force`'s fail-loud backup contract. Docs, operator manual, README, and the shipped
    adapter READMEs all move together.
- **D2 — fold the hook re-render into `levain update`, scoped to `activation/hooks/` only.**
  Cheaper: no new verb. The machinery/operator-owned split is enforced by **directory** — `hooks/` is
  machinery, `activation/*.md` is operator-owned — so the shipped promise in
  `levain/templates/docs/operator-manual.md:139` (*"It does not rewrite your partner's activation
  files"*) survives intact, because `posture.md` and `recency_directives.md` are not under `hooks/`.
  - ⛔ *Cost, and it is real:* it makes `update` mutate the install tree. Today `update` is the SAFE
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
1. Doctor is not lying. §1 — under levain's own honesty floor the red is correct, and the counter-
   argument in `spore-840` is decisive rather than a caveat.
2. The problem is a missing feature, not a misgrading. §2 — and the family it belongs to has a third
   member already shipped and latent.
3. A severity tier builds an ordering on cross-cutting predicates and does not move the exit code
   anyway. A new exit code is invisible to every pipeline that already exists.
4. After D, the two FAILs stop appearing on the expected path, so there is nothing left to re-grade —
   **the honesty floor is preserved by making the install honest, not by making the instrument quiet.**

⛔ **What I would NOT do under any option: soften, downgrade, or exempt `hook freshness`.** It is the
carrier for shipped fixes reaching operators; it exists because one did not.

**If the answer is "not now":** A alone is coherent and costs nothing. `spore-840` stays open, and the
`[Unreleased]` note keeps operators correctly informed in the meantime.

---

## 7. ⚖ WHAT NEEDS PHILL

1. **D or not-D**, and if D, **D1 (new verb) or D2 (fold into `update`)**. This is a product-surface
   decision, not a code one.
2. **Does D cover the carrier as well as the hooks**, or hooks only?
3. **Option F on its own** — authorise the hint correction independently of everything above?
4. **Sequencing against the install-path pass** (`spore-751` + `spore-775` + `spore-724`). §4 shows
   they are **independent**, so this is a preference, not a constraint. ⭐ The outgoing levain seat
   recommended designing the install-path trio *together*; D touches `install.py`'s render chain, so
   there is a real argument for doing D inside that pass rather than beside it.

⛔ **The release hold is a separate question and is not affected by any of this.** `0906+0 main` has
read all three of Alex's messages: no bug reports. The condition Phill set is answered.

---

## 8. PROVENANCE

**[MEASURED HERE, 2026-09-06, every coordinate from `grep -n` — none hand-counted from a `sed`/`awk`
window]:** `doctor.py:51 :57 :59 :124 :154 :689 :811 :833 :855 :1248 :1320 :1591` ·
`manifest.py:58 :135 :137 :139 :150 :251 :255 :354 :389` · `install.py:1597 :1712` ·
`session.py:107–165` · `cli.py:156–183` · `packs.py:340` · `install.py:388` · `doctor.py:1372` · the `init --force` remedy sites `:302 :414 :1291 :1359
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

**[RELAYED, NOT VERIFIED BY ME]:** anneal's missing `last_writer_version` and the 0.9.9 /
0.9.10.dev0 skew, from `0906+8 fanin` citing `0906+6 anneal-memory-seat`'s close report. That seat is
stopped. **Do not treat it as load-bearing without re-deriving it.** Nothing in §5 or §6 depends on it.
