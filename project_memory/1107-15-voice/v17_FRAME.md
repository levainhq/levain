# V1-7 frame: the card script and the notification read

Unfamiliar constructs researched before building (what was read, what it settled).

1. **The script.** NPCardVoice (flowConnect `Shared/NPCardVoice.swift`, deleted in 74f53b7 / 1105-13; read from `74f53b7^`) and
   its pins `tools/verify_np_voice.swift` (real row np-ab37059b). Order: headline verbatim (NPCard.headline: first `.!?`+space at
   char 20..200, else cut at a space near 160 over 200), then `"<key>, <text>"` per option, the rec's line `+ ", recommended"`,
   joined by `\n`, empty option text skipped. `card_format.js` `headline()` is the same rule (the iOS card's port), so card_voice.js
   reuses it; `levain/cockpit/card_script.py` is the Python twin; a node run in tests/test_card_voice.py compares the two.
2. **What a "card" is on the cockpit.** The Needs-you focus view (`openNeedsFocus` -> `questionCard`) of a filed question
   (`GET /cockpit/question/<n>.json`: `what`, `options[{key,label}]`, `rec`). A consent card has an effect block, not NPCard's
   options; it is not read in V1-7 (no NPCardVoice contract for it).
3. **The notification path.** `sw.js notificationclick` -> `sw_open.js` -> `location.assign(/?view=needs&item=<id>)` -> a FRESH page
   load -> `needsItemJump` -> `openNeedsFocus`. A fresh load has no gesture and the player is locked (`voice.js unlock()` needs a tap),
   so the design's "plays without a page tap, or degrades" resolves at runtime by `LevainVoice.isUnlocked()` or
   `navigator.userActivation.isActive`; otherwise "Tap to hear this card". Whether iOS ever grants activation here is the iPhone residue.
4. **The server.** The speak route's invariant is "the words are the record's". The card branch keeps it: `{card_id: "question:<n>",
   chunk, provider, voice, speed}` (exact key set), the words rebuilt from `QuestionStore.get(n)` by card_script, never from the body.
