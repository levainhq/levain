# Levain Seed — Methodology-Core

The seed is what a new install lays down to bootstrap a cognitive-partnership entity. It is layer 3 of Levain (the methodology-core) plus the layer-2 continuity scaffold.

Five files:

| File | Kind | How it is filled |
|------|------|------------------|
| `world.md` | operator template | onboarding interview fills the `{{SLOTS}}` |
| `origin.md` | entity template | onboarding fills `{{ENTITY_NAME}}`, `{{SUBSTRATE}}`, `{{JOB}}`, `{{OPERATOR_NAME}}` |
| `partnership.md` | static core | ships as-is — universal, identical for every entity |
| `memory.md` | static core | ships as-is — universal memory-operation guide |
| `continuity.md` | starter scaffold | ships near-empty and verbatim — its `{{ENTITY_NAME}}` title and body are the entity's to fill and grow from its first wrap (NOT onboarding-filled) |

`world.md` + `origin.md` are operator/entity-specific (filled at onboarding). `partnership.md` + `memory.md` are the universal core (never filled). `continuity.md` is the entity's own accreting memory (shipped empty by design).

**No proper nouns appear in the five always-loaded entity files.** `world.md`, `origin.md`, `partnership.md`, `memory.md`, `continuity.md` are pure second-person instruction — the entity is *inside* the method, it never points *at* it. One necessary exception: `partnership.md` and `memory.md` name `anneal-memory`, the memory system the entity operates — that is naming a tool the entity uses, not a provenance breadcrumb. Naming the kit itself (Levain, and its provenance) lives one layer out — in this README and the kit docs, never in the always-loaded seed.

Seed files cross-reference each other by **section title** (*How We Work*, *Who Your Operator Is*, *Your Memory*), never by filename — titles survive both separate-file placement and concatenation into one harness context file (see `../activation_spec.md`, adapter invariant).

A harness adapter places these files in a specific harness's context-file format and wires the activation hooks — see `../activation_spec.md` for the activation contract and `../next.md` for the build plan.

---

*Extracted from `flow/seed/` on May 17, 2026 (Levain v1, Step 1). Source `flow/seed/` remains the live fleet workshop and is unmodified.*
