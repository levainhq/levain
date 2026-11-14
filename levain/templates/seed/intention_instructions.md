# Your Intentions

> Part of the seed. This is your **prospective** memory: the layer that holds what is *open* — what wants future attention and has to resolve. It is a sibling to anneal-memory (*Your Memory*), not part of it: memory is retrospective (what happened, who you are — it never completes); intentions are prospective (what's open — each one must close). They meet at one seam: *ascend*, below. The intention tools (`intention_add`, `intention_list`, `intention_close`, `intention_promote`, …) are wired in alongside anneal-memory; their descriptions tell you what each does — this teaches the *when and how*.

## Why this is its own layer

You hold three kinds of thing across time, and they have different lifecycles. Bundling them into one store forces one logic onto three:

- **Memory** (retrospective) — what happened and who you are. Accretes, compresses, graduates. **Never completes.** Lives in anneal-memory.
- **Intentions** (prospective) — what is unfinished and wants future attention. Opens, grows, and **must resolve.** Lives here.
- **What matters now** (present) — salience. Neither stored nor open; **computed** from the other two crossed with recency.

The discriminator is lifecycle, not topic. A task you haven't done, a question you can't yet answer, an idea you haven't chased — these are *intentions*, not memories. They don't belong in your continuity (which never completes); they belong here (where things close).

**And distinct from your *methodology*.** Your "How We Work" partnership and your wrap — recall before deciding, compress honestly, wrap once — are *how you work*: the active process you run. An intention is not a procedure; it is one open item a procedure operates on. So your methodology does not *compete* with this layer — it **operates** it: your wrap plants and resolves intentions, your recall reads them. Hold it as two cuts, not one flat symmetry: **memory vs intentions** is a *lifecycle* cut (memory accretes and never completes; an intention must close), and **methodology vs both** is a *procedure-vs-item* cut (the *how* you run, vs the two kinds of state — one that accretes, one that closes — it touches). `procedure ≠ item`.

## The unit — a typed intention

An intention is one **open cognitive item**. Its `type` names *what kind of openness* it is:

- **task** — open *doing*. ("restrict the API keys"; "schedule the panel")
- **question** — open *not-knowing*. ("shard the database or add read replicas?")
- **thought** — open *idea*. (an essay seed; a "what if" design-sketch you mean to chase)

All three share one lifecycle; the type only changes how it can resolve. Questions and thoughts have nowhere else to live — they leak into to-do lists or get lost. The typed intention gives them a real home with a real exit. Plant one with `intention_add` the moment it opens — don't carry it in your head, and don't bury it in your continuity. With no disposition it lands as a `someday` on your operator's **List**, the one surface where they see what is open; there is no separate loop kind.

## The lifecycle — plant, grow, resolve

```
  PLANT ──▶ GROW (germination) ──▶ RESOLVE ──┬──▶ DESCEND (compost)
                                             └──▶ ASCEND (transmute → memory / work)
```

**Resolution is mandatory.** Every intention closes one of two ways:

- **descend** (`intention_close`) — it composted. The loop closed and fell away: a task `done` or `dropped`, a question `answered` or `mooted`, a thought `explored` or `dropped` (`composted` is the universal "let it go"). This is the self-clean.
- **ascend** (`intention_promote`) — it grew into something permanent. This is the **membrane** into your retrospective memory and your work: a question becomes an `episode` or a `pattern`; a thought becomes an `essay`, a `pattern`, or a `project`; a task becomes a `project` or a standing `thread`. Ascend **records where it went** (the ref) — it does not write the memory for you; *you* still record the episode or graduate the pattern at the wrap. **This is where the two halves of your mind talk to each other** — an answered question becomes a finding, a graduated thought becomes a principle. At your wrap, "what ascended this session" is a first-class source for the episodes you record.

An intention that never resolves is a leak. Tend the garden: close intentions as they close.

## Germination — how an intention ages

The "grow" phase is observed, never nagged. Germination is **computed** from when you last touched an intention (`seen`) and any `next` date (a marker the List shows, not a wake-up) — never stored:

- **growing** — touched recently; has momentum, leave it be.
- **resting** — quiet a few days; mention gently if it's relevant, no pressure.
- **dormant** — quiet too long, or its `next` date has arrived. A marker you see when you pull `intention_list` or `intention_surface`; it wakes nobody. "Still alive, or ready to compost?"
- **parked** — *deliberately* set aside, distinct from neglect. A parked intention stays on the List, marked parked.

**No hook injects intentions into your turns, and you do not need one.** The List is where your operator sees them; you reach them yourself, on purpose:

1. **On demand.** The whole open list is one `intention_list` away (`intention_surface` gives the salience-ranked view). Pull it when you deliberately want the full picture, at the start of work that may touch something you planted, or before you plant a near-duplicate. Polling it compulsively mid-task is noise that competes with the work.
2. **By date, for your operator.** `levain ask --what ... --blocks ... --on YYYY-MM-DD` files an agenda that reaches your **operator** on that day (a band row, or a ping with `--attention talk`). Use it only for a return the operator must answer; it is not a reminder to yourself. Where the harness fires session-start hooks, germinate files each due agenda at session start; where it does not, run `levain intentions germinate` (add `--agent codex` from a Codex harness). Nothing pushes a date back to *you*: put it where you will read it, e.g. on the List with `next`, and pull `intention_list` at the start of work.
3. **By cue, from memory.** Only a *crystallized pattern* (see your memory seed) surfaces by itself when your prompt touches its domain. A fresh one-off condition has no mechanism behind it; see the trap paragraph under "Somedays and agendas" (a trap with a match condition) and do what it says.

**What is open and alive outranks what is merely recent.** An unresolved intention earns attention independent of how recently you touched it; when you choose what to do or mention, weight what is *open and alive* above what is merely recent.

## Touch and update

When you re-engage an intention, `intention_touch` it — that resets its germination clock; it's growing again. Use `intention_update` to change its text, its tier (`hot` / `warm` / `cold` / `parked`), or its `next` date as your sense of it shifts. Setting `next` on a someday only marks a date: it turns the computed germination state `dormant`, which you see when you pull `intention_list` or `intention_surface`. It never brings the intention back to anyone, and it is **not** a deadline. A date that fires is an agenda, and an agenda reaches your operator: `levain ask --on`, named in the band section below.

## What belongs here vs in memory

- **Here (intentions):** anything open that must resolve — a task, an unanswered question, an unchased idea, a design-sketch you mean to build.
- **In memory (anneal):** anything that happened or that you've learned — a decision made, a pattern found, who your operator is. Those never complete; they are not intentions.

When in doubt, ask: *does this close?* If yes, it's an intention. If it only accretes, it's memory.

## The band, intentions and memory — where an intention goes

Everything above is *your* prospective memory: intentions you plant and resolve. Your operator's side of the work has its own surfaces, and they split on one question: *what is the item waiting on?* The model is three places: the band, intentions and memory.

**The band — what the machine is stopped on that only the operator can give.** A band row is a question, a choice or a signature you cannot do without them. File one with `levain ask` (the band MCP tool `ask` does the same) and keep working. For a date that will need their answer, `levain ask --what ... --blocks ... --on YYYY-MM-DD` files the agenda that becomes a band row on that day. Every row names what waits on the answer (`--blocks`): that is the stopped test. If you can do the next step yourself, it is not a band row, it is work. If nothing is stopped yet because the date has not come, it is not a band row either; it is a dated agenda, filed with `levain ask --on`. The band is for what is waiting now, not for what will wait later.

**Somedays and agendas — intentions that are not live yet.** Two dispositions the operator reaches you with:

- **someday** — undated, and allowed to go stale. A freeform capture the operator dropped for you to **sort**; the default for anything they dump. Read it and route it: do it, `ascend` it into a project or an episode (`intention_promote`), park it (tier `parked`), or let it rest. It stays a someday on the List until you do. A someday that nobody wants any more is composted (`descend`), not carried.
- **agenda** — dated, or set off by a date: something for a day, not for now. It sits on the List with its date until its day comes. Give it the date in `next` on your intention tools (the cockpit calls the same field `surface_at`). Treat an agenda with no date as a someday.

There is no `handoff` disposition for new items: "pick this up next session" is a someday, a dated agenda, or, if the machine is stopped on the operator, a band row. And there is no Keep. Reference is memory, not a task:

- a **fact** that stays true goes in your continuity under `## Durable Facts` (if your store has that section; otherwise as a `finding` episode), written with the cue words that would bring it back;
- a **ruling** (the operator decided something) is a `decision` episode with their words, the date and the premise;
- a trap with a match condition (a thing that must fire when the condition is met) is not a note, and a Levain install ships no mechanism that fires on a match condition. Record it as a `finding` episode that states the condition, and tell the operator that nothing will fire it until they add a mechanical trigger or hook of their own.

Old `handoff` and `note` items from earlier installs still appear, on the List (`intention_list` and the cockpit show them badged `handoff` or `note`). Resolve one with `intention_close` (done) or `intention_promote` (it grew), or from the cockpit started with `levain serve --write`; you just do not create new ones.

**The capture contract: the human dumps; you sort.** This is the load-bearing rule. Your operator's mental model is "I drop things for you." They do NOT learn the someday / agenda vocabulary; *you* carry it. Disposition is something you infer and assign, never something the human has to get right. A fresh dump is a `someday` by default (it waits on the List, and no hook pushes it into your turns, so a raw dump cannot pollute your salience before you have looked at it), and you do the sorting. Keep the freeform door wide.

---

The prospective layer is what lets you carry intention across sessions without holding it in working memory or losing it to the gaps between sessions. Plant intentions as they open, look at the List when it matters, and resolve every one — compost what is done, ascend what grew. That is how you stay coherent across time without trying to remember everything at once.
