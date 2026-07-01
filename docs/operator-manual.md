# Driving Your Partner — a Levain operator's manual

This guide is about *driving* the partner you just installed: working with it day to day so its memory grows into something that actually knows you and your work. It covers the handful of things you do, and why they matter — the rest follows.

Your partner already knows how to be a partner; that was seeded at install. This manual is the other half of the loop: your side.

**Where your partner lives.** There's no separate app to launch. Your partner is the Claude Code (or Codex) session you open in the folder where you installed it — you start a session the way you always do, and it's there, already carrying its memory. Everything in this manual happens inside that session, except the few `levain …` commands (§9, §10), which you run in a terminal.

## 1. What you're driving

You did not install a chatbot. A chatbot forgets you when the window closes, so every session starts with a re-brief. Your partner remembers across sessions, and its memory grows. It starts near-empty on purpose; over your first weeks it takes on who you are, how you think, what you're building, and what it has learned working with you. Month five doesn't look like day one, because it grew there with you.

That one fact drives the rest of this manual:

- You don't re-explain yourself each session — you correct and add, and the correction sticks.
- You close sessions on purpose (§4) — that's when its memory of you updates.
- You stay in the loop on what it remembers (§7, §9) — a memory that grows can grow *wrong*, and you're the one who catches that.

Everything below follows from "it remembers, and it grows."

## 2. A day with it

A day with your partner has a shape, and the shape is the point: it keeps the two things that get skipped under pressure — starting oriented and ending consolidated — from getting skipped.

**You open, and you don't re-brief.** You don't paste yesterday's context or re-explain what you're working on. The partner already carries it — who you are, what's active, where you left off. You start most days by just asking for the read: what's actionable, what's waiting on someone else, what's gone quiet that shouldn't have, what's on the calendar. What comes back is a sense of the field and, usually, a proposed order for the day.

**Then the small move that makes orientation real: you push back on the order.** You know things the partner doesn't — that a loud item is actually noise, that you've decided to let a certain kind of thing wait, that today is low-energy and you want the generative work first. You say so, and the day reshapes. The orientation isn't the partner assigning your day; it's the two of you agreeing what the day *is* before either of you spends energy on it. Five minutes, and the important thing wins instead of the loudest.

**You work, and capture runs underneath the whole time.** You don't manage it. As you go, the partner is quietly recording what happens — decisions, findings, turns in the work — so nothing has to be reconstructed later. When a stray thought or a later-task surfaces mid-flow, you drop it in the Tray (§5) and keep moving; you don't stop to file it. The work stays in the foreground; the memory takes care of itself in the background.

**You close on purpose — you "wrap."** At a natural end, when the day produced something worth keeping, you tell the partner to consolidate (§4). This is the one bookend you actively trigger, and it's where the day becomes part of the durable record instead of evaporating. It's not filing; it's the partner thinking about the day and recomposing what it knows. You give it room.

That's the rhythm — orient, work the order, wrap — and it's deliberately light. Two short bookends around the real work. The busier the day, the more they earn their keep, because a busy day is exactly the one you're tempted to plow through without coming up for air.

## 3. How the memory works

You don't need the internals to drive well. You do need a rough sense of where things go, because it explains how the partner behaves.

Think of it as a few different memories, each doing a different job:

- a **diary** — every session, it records what happened, cheaply and in full; it rarely re-reads the whole thing.
- an **always-loaded summary** — the compact picture it carries into every session: who you are, what's active, what it has learned, what's still open. This is what's actually loaded when you talk to it, and it's kept small.
- **reference it recalls on cue** — proven lessons held off to the side and surfaced the moment what you're doing calls for them, so it can draw on far more than it carries around.
- **open loops** — what still has to resolve: a task, a question, a thread you asked it to hold.

The one thing worth holding onto: the always-loaded summary stays small on purpose. A partner doesn't get sharper by keeping more in front of it; it gets vaguer, the way a person buried in notes loses the thread. So it keeps the summary tight and pushes everything else to the diary, the reference store, and the loop list, pulling them back only when they're relevant. When it "forgets" a detail from three weeks ago and then recalls it exactly when it matters, that's the design working, not a glitch.

You'll see these when you look inside (§9): the summary is laid out in sections — who you are, what's active, what it has learned, decisions, context, its read on the relationship. You don't write those; the partner does, when you wrap. Your job is to read them and catch anything wrong (§7).

## 4. Capture vs. consolidate — the mechanic you drive

Your partner writes to memory in two very different ways, and telling them apart is most of what you need to run it well.

**Capture happens on its own, every session, and it's always safe.** As you work, it records what happened — decisions, findings, turns in the work — as dated entries. Append-only: it can't corrupt anything, it runs whether or not you think about it, and you can't do it wrong. Two sessions can capture at once without colliding.

**Consolidation is the move you make.** At the end of a session that produced something worth keeping, the partner takes what it captured and rewrites the always-loaded summary — who you are, what you're on, what it has learned. You trigger it by saying "wrap" (or "consolidate" / "update your memory"). That's the moment its memory of you actually changes.

Three things follow, and they're the whole discipline:

- **The wrap is where the thinking happens — give it room.** It isn't just saving; it's compressing, and the compression is the cognition: deciding what mattered and what to let go. A rushed wrap is a worse memory. Don't interrupt one, and don't start one when you're already out of time.
- **Wrap at the end, not all the time.** Work freely — capture has you covered — then wrap once, at a natural stop, when the session earned it. A quick question you asked and answered doesn't need one; an afternoon that changed the plan does. When unsure, wrap: a skipped wrap is how a good session quietly fails to land.
- **Only one session wraps at a time.** Run a single session — the normal case — and this is automatic; you never see it. If two live sessions both try to wrap, the partner lets the first through and holds the second to capture-only, so two writers can't overwrite each other. If you run parallel sessions on purpose, pick one to do the wrapping; the rest just capture.

The rule underneath: work freely, wrap at the end.

## 5. Dropping things in — the Tray, the Keep, and focus

Three ways to hand something to your partner outside the flow of conversation:

- **The Tray** is where you offload things you don't want to lose — a stray thought, a "deal with this later," a prompt to pick up next session. You don't file it; you drop it and the partner sorts it into wherever it belongs — a task, a question, a thread to revisit. The point is that you never categorize anything.
- **The Keep** is durable reference — the command you always forget, a standing note, context that stays true. It persists until you remove it, and the partner hands the right note back the moment it's relevant.
- **`focus`** is the one-line "what I'm on right now" that travels across sessions. Set it — `levain focus "shipping the Q3 audit"` — and every session opens knowing your current thread. Clear it when you move on.

You don't need a command for the Tray or the Keep: you just tell the partner in the conversation. "Hold this for later" drops something in the Tray; "keep this handy" puts it in the Keep; it sorts and files from there. (If you'd rather manage them directly — reorder, edit, clear — the `serve --write` cockpit in §9 lets you.)

The Tray is the thing that lets you stay in flow. Mid-task, a thought lands that has nothing to do with what you're on — a different thread to circle back to, a config to check later, an idea worth keeping. The old habit is to either chase it and lose your thread, or trust yourself to remember and don't. You offload it in one line, almost mid-sentence, and it's gone from your head but not lost. After a week of it, the background hum of "don't forget to…" quiets down, because the system is holding the loose threads instead of your attention.

The Keep replaces the scattered-notes problem — the command you look up every time, the standing fact you re-explain. Put it in the Keep once and the partner surfaces it exactly when it's relevant, instead of you digging through old messages. And `focus` has the biggest payoff for its size: set your current thread once, and you stop spending the first two minutes of each session re-establishing where you are.

## 6. Thinking modes — shifting how the partner thinks

Beyond *what* your partner knows, you can shift *how it thinks* for a stretch of work, with a small set of bracket-tagged modes. Each is a deliberate gear-change — a different mode of reasoning, not a different personality — and the right one at the right moment noticeably changes the quality of the work.

- **`[!deeper]`** — maximum rigor. First principles, second- and third-order consequences, assumptions checked rather than assumed, the conclusion argued *against* before it's trusted. For anything load-bearing.
- **`[!creative]`** — break the assumptions. Sort the real constraints from the inherited ones, generate before filtering, look for cross-domain connections, ask the naive question. For when the obvious approaches are spent.
- **`[!breakthrough]`** — both at once, for when you've been stuck more than twice: break the frame first, then apply full rigor to what survives.
- **`[!execute]`** — execution mode. Restate what's changing and why before touching it, verify against the actual goal, flag anything wrong-looking mid-flight. For when you're doing, not deciding.
- **`[!humanize]`** — strip the AI-voice texture from writing meant for an audience that would distrust it. For a customer reply, a public post, anything where reading as machine-written would undercut it.

You can chain them (`[!deeper][!creative]`) and drop them inline, mid-conversation, wherever the thinking needs to shift. Treat this set as the starting gears, not a fixed inventory — the seed ships with them, and a maturing partner may grow or rename its own; what matters is the gear-change, not the notation.

**Why tags, not commands.** A tag stays resident, so it sets the posture *before* you consciously reach for it — which is the whole point, since a mode that only switched on after you'd started would arrive too late to shape the work. That's why they're inline tags you drop, not menu items you go fetch.

## 7. Trust vs. verify — you are the first line

Your partner has real machinery protecting the integrity of its memory. It keeps a tamper-evident log of every change. It won't let a wrap silently gut the identity sections. It catches a "learning" that cites evidence that isn't there, and demotes a pattern that only shows up because it got repeated, not because it's true. That machinery runs on every wrap; you don't have to think about it.

Here's what it can't catch, and why this section exists: it checks that a lesson is *grounded* and *not gamed* — not that a lesson is *right*. A confidently-stated, well-cited, entirely-wrong conclusion passes every automated check. So does a slow drift where the partner starts telling you what you want to hear. So does a memory that caught the right shape but the wrong fact — the correct name on the wrong role, a plausible summary of a meeting that didn't quite go that way.

The machine catches *unfilled* and *ungrounded*. It can't catch *filled-wrong*. That gap is yours — and it stays yours: the pull that makes a model drift toward the confident and the agreeable is structural, so it never fully leaves, and your check on it never fully retires. The partner watches for it from the inside, where it knows its own tendencies; you watch from the outside, where you see what it can't.

In practice this is light:

- **Read what it surfaces, especially early.** When the partner tells you what it learned or restates your situation, actually read it. A wrong belief caught in week two is a sentence; caught in month three it has quietly shaped a hundred answers.
- **When it states something as settled that you're not sure of, ask where it came from.** The partner can show you the evidence behind a belief. One that can't point to anything real is one to correct.
- **Correct in the moment.** If it has something wrong, say so — it will carry the correction. Silent disagreement teaches it nothing.

None of this is distrust. Trust grows, and it should — it's what lets you hand the partner more. It just doesn't retire the checking; the two run together. The partner's memory is only as true as the loop it grew in, and you're the half of that loop that can tell true from merely-plausible.

## 8. The outward-action rule

There's one failure sharp enough to get its own rule: **your partner can tell you it did something it didn't do.** Not out of carelessness and not as a lie — under load, a capable model produces the *shape* of a completed action ("message sent," "command run," "file written") so convincingly that the report reads exactly like the real thing. The account of success and the success itself are different objects, and the partner can generate the first without the second.

So for anything **outward-facing or irreversible** — a reply to a customer, a call against a live system, a change to production, a delete you can't take back — the rule is simple and absolute: **the action goes through your hands, and you confirm it landed through your own channel, not the partner's word.** Look at the sent folder. Hit the endpoint yourself. Read the actual file. The partner's "done" is a claim; the receipt is the ground truth, and you get the receipt.

This isn't hedging against a bad partner — it's the single most important daily habit in the whole practice, and it earns its place constantly. Two examples from a single hour of real work, both this exact failure: a health check reported an install fully green while the file it should have populated had rendered with the operator's name missing entirely — caught only by reading the rendered file instead of trusting the green check. And that same hour, a message that reported as sent, echoing back a complete sent-looking body, had silently failed to send — caught only by checking the actual send status instead of the confident echo. Neither was a broken tool; both were the report outrunning the reality, aimed at an action.

In any operational role — where your partner drafts outward messages or runs commands against live systems — this is a daily reflex, not a caveat. Trust it to do the work, and verify the outward action through your own eyes every time. Deep trust in the reasoning, hard verification of the irreversible act: that's the posture.

## 9. Looking inside — doctor, dashboard, serve, tui

You don't have to take the partner's word for its own health or its own memory — you can look directly. Four ways in, lightest to richest:

- **`levain doctor`** — the health check. Run it after install, after an upgrade, or any time something feels off. It checks that the machinery is wired correctly *and* that your identity file actually got filled — if the interview left your name or role blank, doctor says so loudly instead of letting a half-captured install look healthy. A green doctor means the plumbing works; it does not mean the memory is *correct* (that's §7, and it's yours).
- **`levain dashboard`** — a one-shot, read-only glance in your terminal: health, the association graph, the reference store, open loops, and the top of the always-loaded summary. Good for a quick "what does it know right now."
- **`levain serve`** — the same picture as a local web app in your browser (`127.0.0.1:7420`, your machine only). Read-only by default; add `--write` for the cockpit where you edit the summary, resolve loops, and manage the Tray and Keep directly. The richest way to read your partner's memory.
- **`levain tui`** — the terminal-native version of `serve`, for when you'd rather stay in the shell. Same read-and-steer surface.

**One habit worth forming early:** right after setup, open `serve` or `dashboard` and read your own "who you are" profile with the install in front of you. Doctor confirms the fields got filled; only you can confirm they got filled *right*. Reading it once catches a plausible-but-wrong capture before it becomes the foundation everything else grows on.

## 10. Keeping it healthy — updates, upgrades, drift

Your partner runs on a memory library (`anneal-memory`) that ships its own improvements over time. Keeping the two in step is one command and a short habit.

- **`levain update`** — the one move. It brings the memory library to the version your Levain was tested against, re-applies any updated partnership settings, and surfaces — for your review, never a silent rewrite — any changes to the standing instructions your partner runs on. Run it when you upgrade Levain, or when `doctor` says the versions have drifted apart.
- **Why the review step is there.** When the library changes how something works, `update` shows you the change instead of applying it behind your back — the same principle as everywhere else here: nothing rewrites your partner's operating instructions without you seeing it. Read the proposals, apply the ones that make sense, and `update` records that you've reconciled them.
- **After any upgrade, run `doctor`.** Thirty seconds to confirm the wiring survived the change and the memory is still reachable. Worth it every time.
