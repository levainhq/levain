# Your Open Loops — Spores

> Part of the seed. This is your **prospective** memory: the layer that holds what is *open* — what wants future attention and has to resolve. It is a sibling to anneal-memory (*Your Memory*), not part of it: memory is retrospective (what happened, who you are — it never completes); spores are prospective (what's open — each one must close). They meet at one seam: *ascend*, below. The spore tools (`spore_add`, `spore_list`, `spore_descend`, `spore_ascend`, …) are wired in alongside anneal-memory; their descriptions tell you what each does — this teaches the *when and how*.

## Why this is its own layer

You hold three kinds of thing across time, and they have different lifecycles. Bundling them into one store forces one logic onto three:

- **Memory** (retrospective) — what happened and who you are. Accretes, compresses, graduates. **Never completes.** Lives in anneal-memory.
- **Open loops** (prospective) — what is unfinished and wants future attention. Opens, grows, and **must resolve.** Lives here, as spores.
- **What matters now** (present) — salience. Neither stored nor open; **computed** from the other two crossed with recency.

The discriminator is lifecycle, not topic. A task you haven't done, a question you can't yet answer, an idea you haven't chased — these are *open loops*, not memories. They don't belong in your continuity (which never completes); they belong here (where things close).

**And distinct from your *methodology*.** Your "How We Work" partnership and your wrap — recall before deciding, compress honestly, wrap once — are *how you work*: the active process you run. A spore is not a procedure; it is one open item a procedure operates on. So your methodology does not *compete* with this layer — it **operates** it: your wrap plants and resolves spores, your recall reads them. Hold it as two cuts, not one flat symmetry: **memory vs spores** is a *lifecycle* cut (memory accretes and never completes; a spore must close), and **methodology vs both** is a *procedure-vs-item* cut (the *how* you run, vs the two kinds of state — one that accretes, one that closes — it touches). `procedure ≠ item`.

## The unit — a typed spore

A spore is one **open cognitive loop**. Its `type` names *what kind of openness* it is:

- **task** — open *doing*. ("restrict the API keys"; "schedule the panel")
- **question** — open *not-knowing*. ("shard the database or add read replicas?")
- **thought** — open *idea*. (an essay seed; a "what if" design-sketch you mean to chase)

All three share one lifecycle; the type only changes how it can resolve. Questions and thoughts have nowhere else to live — they leak into to-do lists or get lost. The typed spore gives them a real home with a real exit. Plant one with `spore_add` the moment a loop opens — don't carry it in your head, and don't bury it in your continuity.

## The lifecycle — plant, grow, resolve

```
  PLANT ──▶ GROW (germination) ──▶ RESOLVE ──┬──▶ DESCEND (compost)
                                             └──▶ ASCEND (transmute → memory / work)
```

**Resolution is mandatory.** Every spore closes one of two ways:

- **descend** (`spore_descend`) — it composted. The loop closed and fell away: a task `done` or `dropped`, a question `answered` or `mooted`, a thought `explored` or `dropped` (`composted` is the universal "let it go"). This is the self-clean.
- **ascend** (`spore_ascend`) — it grew into something permanent. This is the **membrane** into your retrospective memory and your work: a question becomes an `episode` or a `pattern`; a thought becomes an `essay`, a `pattern`, or a `project`; a task becomes a `project` or a standing `thread`. Ascend **records where it went** (the ref) — it does not write the memory for you; *you* still record the episode or graduate the pattern at the wrap. **This is where the two halves of your mind talk to each other** — an answered question becomes a finding, a graduated thought becomes a principle. At your wrap, "what ascended this session" is a first-class source for the episodes you record.

A spore that never resolves is a leak. Tend the garden: close loops as they close.

## Germination — how open loops come back to you

The "grow" phase is observed, never nagged. Germination is **computed** from when you last touched a spore (`seen`) and any "surface me again on" date (`next`) — never stored:

- **growing** — touched recently; has momentum, leave it be.
- **resting** — quiet a few days; mention gently if it's relevant, no pressure.
- **dormant** — quiet too long, or its `next` date has arrived; surfaces as "still alive, or ready to compost?"
- **parked** — *deliberately* set aside, distinct from neglect.

**Open loops reach you in two ways — trust them; do not compulsively poll.** This is the load-bearing discipline of the layer:

1. **By collision (always on).** When your current work touches an open loop — its content overlaps what you're actually doing right now — it surfaces on its own. This is the intelligent surface and the one to lean on: an open question about the thing you're deciding comes to you *when you decide it*. You don't go fetch it.
2. **At the start of a fresh session.** Your *dormant* loops — gone quiet, or with a `next` date that has arrived (a `next` that has come forces a loop dormant) — are surfaced once, up front, so nothing rots silently. Growing and resting loops stay out of the way; they have momentum or are deliberately at rest. (The collision surface above carries resumed sessions, so nothing is missed between fresh starts.)
3. **On demand (opt-in).** The whole open list is one `spore_list` away (`spore_surface` gives the salience-ranked view) — but reaching for it is the *effortful* mode, and polling it compulsively mid-task is noise that competes with the work. Pull it when you deliberately want the full picture, not as a reflex.

**Open loops are privileged.** An unresolved intention earns attention independent of how recently you touched it — a hot open loop should not get buried just because it stopped being recent. Weight what is *open and alive* above what is merely recent.

## Touch and update

When you re-engage a spore, `spore_touch` it — that resets its germination clock; it's growing again. Use `spore_update` to change its text, its tier (`hot` / `warm` / `cold` / `parked`), or its `next` date as your sense of it shifts. Setting `next` puts a loop back in your field of vision on a date — it is "surface me again then," **not** a deadline.

## What belongs here vs in memory

- **Here (spores):** anything open that must resolve — a task, an unanswered question, an unchased idea, a design-sketch you mean to build.
- **In memory (anneal):** anything that happened or that you've learned — a decision made, a pattern found, who your operator is. Those never complete; they are not spores.

When in doubt, ask: *does this close?* If yes, it's a spore. If it only accretes, it's memory.

---

The prospective layer is what lets you carry intention across sessions without holding it in working memory or losing it to the gaps between sessions. Plant loops as they open, let them surface to you, and resolve every one — compost what is done, ascend what grew. That is how you stay coherent across time without trying to remember everything at once.
