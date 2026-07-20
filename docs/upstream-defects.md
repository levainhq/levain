# Upstream defects levain shields against

Levain sits on top of other people's runtimes. When one of them damages data on its way to
levain's memory store, levain cannot fix the cause — but it must never pretend the damage did
not happen. This file is the record of what those defects are, what levain does about them,
and, honestly, what it does **not** do.

A shield is not a cure. Everything here stays open until the upstream layer fixes it.

---

## UD-1 — mis-decoded text arrives at the capture boundary (open)

**Status:** open upstream · shielded in levain since 2026-07-20
**Shield:** `levain/firing/encoding.py`, invoked from `CaptureRequest.__post_init__`
**Suspected layer:** the streaming decode path in Ollama `/v1` → litellm → OpenHands SDK

### What was observed

During the first real dogfood of a sovereign entity (`levain run` on glm-5.2 via Ollama, an
entity that took a real levain bug to a pushed pull request), the entity's stored memory came
back with mangled characters. This was verified in the **bytes of `memory.db`**, not in a
terminal rendering — the corruption is in the store, not in how it was displayed:

- an em-dash (`—`, U+2014, UTF-8 `E2 80 94`) was stored as `b'\xc3\xa2'` — which is the UTF-8
  encoding of `â`, i.e. the FIRST of the em-dash's three bytes, reinterpreted as a single-byte
  character and re-encoded. The other two bytes did not survive as part of the character;
- other spans were **doubly** encoded (`Ã¢` where `â` had already been wrong);
- the damage was **intermittent** — 4 mangled characters against 49 correct ones, in 2 of 11
  episodes.

Intermittency is the diagnostic detail. A systematically wrong codec mangles every non-ASCII
character; mangling a few is the signature of a **multi-byte character split across two
streaming chunks** and decoded independently, so one chunk ends mid-character and each half is
separately coerced through a single-byte codec.

### Why levain cannot fix the cause

`levain run` hands an `LLM` object to the OpenHands SDK (`levain/run.py`) and reads back text
that is **already decoded** (`levain/firing/agent_reply.py::message_event_text`). Levain owns
zero bytes of the streaming decode path. There is no bad decode of ours to correct: by the
time the text is levain's, the original bytes are gone.

### What levain does instead

The shield fires at the capture boundary — the one place levain does own, and the last point
before text becomes memory:

- **repairs only provably double-encoded spans.** A span is repaired only if it *opens with
  one of four lead-byte renderings* (`Â Ã â ð` — the single-byte forms of the UTF-8 leads
  `0xC2 0xC3 0xE2 0xF0`) **and** re-encoding it through cp1252/latin-1 produces bytes that are
  *strictly* valid UTF-8 decoding to control-free text. Whole-span or nothing; no scoring, no
  "most plausible" reconstruction. The lead gate is load-bearing and was added after review:
  the byte-structural checks *alone* silently rewrote ordinary typography, because an accented
  capital is itself a valid UTF-8 lead byte and an em-dash is a valid continuation byte
  (`CAFÉ—hands down` → `CAFɗhands down`, in 413 of 420 such adjacencies);
- **leaves everything else byte-for-byte untouched, and flags it.** The `b'\xc3\xa2'` case
  above is *unrepairable by construction* — two of the three bytes are gone, and no amount of
  cleverness can recover which character it was. Guessing would write a *wrong* character into
  a memory store, which is worse than visible damage;
- **records a receipt.** Anything repaired or flagged is written into the episode's metadata
  under `levain_encoding`, and logged as a warning during the session. For a product whose
  claim is "a memory you can trust because you can see what is in it", a silent repair would
  be its own trust defect.

### What is still missing

**A minimal reproduction has not been isolated to a specific layer.** The chain has three
candidates (Ollama's `/v1` response streaming, litellm's chunk handling, the OpenHands SDK's
event assembly) and the fault has only been observed end-to-end, under load, intermittently.
Filing against the wrong project wastes a maintainer's time and gets closed, so no upstream
issue has been filed yet. The next step is a chunk-boundary harness that forces a split inside
a multi-byte character at each layer in turn.

**The shield's flag coverage is Latin-lead-biased.** Mojibake from a 3-byte character (CJK,
Hangul, Devanagari, Greek) renders with a `0xE0`–`0xEF` lead — `à` through `ï` — which are
ordinary accented lowercase letters. Flagging on those would fire on a large share of French
and Portuguese captures, so that damage currently passes through unflagged. Cyrillic (`0xD0`
lead → `Ð`) and everything Latin-1 *is* covered. Pinned as an explicit test rather than left
implicit: `test_KNOWN_BLIND_SPOT_cjk_mojibake_passes_through_unflagged`.

Until then, the honest statement is: **levain detects and records this damage; it does not
prevent it, it cannot recover what the upstream chain has already discarded, and its detection
is not uniform across scripts.**

### What levain guarantees on its own side

Levain's own read path does not commit the same error. `levain/turn_input.py` decodes operator
input at **line** granularity, and a newline is always a character boundary — so a multi-byte
character can never be split across two decodes in levain's code. Regression-tested
(`tests/test_turn_input.py::test_multibyte_character_split_across_read_chunks_survives`).
