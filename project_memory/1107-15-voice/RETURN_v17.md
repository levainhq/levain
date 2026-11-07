# RETURN v17: Voice V1-7, the card script and the notification read

Branch `seat/1107-20-v17` off c6d451b7 (the mic chain). Not pushed. Frame: `v17_FRAME.md`.

## Built
- `levain/templates/web/card_voice.js` (new): `script(what, options, rec)` (the NPCardVoice port, pinned from the real row np-ab37059b,
  read from flowConnect `74f53b7^:Shared/NPCardVoice.swift` and `tools/verify_np_voice.swift`), `speaker(id, body)` (the Play / Stop /
  "Tap to hear this card" control), `opened(id)`.
- `levain/cockpit/card_script.py` (new): the Python twin the premium route reads; `tests/test_card_voice.py` compares the two by a node run.
- `POST /cockpit/voice/speak` gains the exact key set `{card_id, chunk, provider, voice, speed}`; the words are rebuilt from the
  filed question (`QuestionStore.get(n)`), never from the body. Only `question:<n>` cards; a consent card is not read (no contract).
- The speaker is drawn on every question card's focus view; a card opened by a notification deep link (`/?view=needs&item=<id>`)
  with "Read a notification aloud when I open it" on calls `LevainVoice.play` when the page may play (player unlocked, or
  `navigator.userActivation.isActive`), else shows the large "Tap to hear this card"; setting off: Play; any other open: Play.

## Shared-file lines touched (micwire and others: merge notes)
- `levain/cockpit/voice.py`: import line 50 (`card_script`); `CARD_KEYS` constant (~90-92); three `WORDS` rows (`bad_card`, `no_card`,
  `card_nothing`, ~180-182); `_parse_speak` (`card =` line, the key check, the `if card:` branch before the reply_gseq check, ~521-540);
  `speak` signature + docstring + the `_card_chunks` branch (~553-570); `_card_chunks` new function after `_reply_chunks` (~611-629).
  Nothing in the stream-provider naming.
- `levain/web_server.py`: one `_ASSETS` row `/card_voice.js` after `/voice.js` (~1270-1271); the `cockpit_voice.speak(...)` call in
  `_voice_answer` gains `question_store=getattr(srv, "question_store", None)` (~3934-3935).
- `levain/templates/web/voice.js`: ONE statement, `fetchChunk`'s body (~274-275): `card_id` when `reply.card` is set, else the
  room keys exactly as before. (The design said V1-7 must not touch voice.js; the premium tier cannot name a card otherwise.)
- `levain/templates/web/dashboard_core.js`: `questionCard` (+3 lines after `drawQuestionBody`, ~6409); `needsItemJump`'s two open sites
  (+1 line each, ~9069 and ~9105). `dashboard.html`: one script tag after voice.js. `mic.js`: not touched.

## Tests (`tests/test_card_voice.py`, 9; all green) and the revert that turns each red
Each was reverted in turn and the named test went red (run by script this session):
- pinned script: delete `line += ", recommended"` in `card_script.script` -> red; same line in `card_voice.js` -> `same_script` red.
- notification read: delete the `opened(...)` call in `needsItemJump` -> red. Setting gate: `V.settings().notify === true` -> `true` -> red.
- locked page: `if (V.isUnlocked()) return true;` -> `return true;` -> red. Other-way opens: `takeOpened` `hit = true` -> red.
- route: `set(req) == CARD_KEYS` -> `>=` -> red; the `not chunks` refusal removed -> red; `CARD_ID` widened to `consent:` -> red.
Regression run of every test importing a touched module: see the last section.

## Residue (his, on the iPhone)
1. **Autoplay on iOS needs a user gesture.** A notification tap is a gesture to the OS, not to the page; the opened page is a fresh load,
   locked. I expect "Tap to hear this card" on the iPhone [inferred]; whether Safari ever grants a live user activation there is the unknown.
   The tests pin both outcomes with stubs. The premium path also never fetches audio on a locked page (a blocked play would be billed unheard).
2. What he hears (voice, timing, the silent switch) is not judged by any test. Premium voice needs a Signer and his
   np-9f8785c9 (a) setup; the card read uses Settings > Voice's tier like a room reply.
3. A consent card is not read; open-ended and answered cards read their ask (answered cards still show the speaker).

## Not run / known edges
- No real provider and no real audio. `userActivation` is stubbed in the browser tests.

## Regression run of the tests importing a touched module (2026-11-07 ~15:50)
`tests/test_card_voice.py` 9/9 green; `node tests/card_format_check.js` ALL PASS. The wider files (voice routes, stream routes,
voice_text, csp_media, room_hook, player_browser, player_real, mic_page, card_format, needs_item_link, band_title,
band_read_timeout, sign_card_format) were running in four parallel processes when this was written, slowly (browser tests): 47
dots, 0 failures so far, NOT finished. The seat that lands this branch must see them finish: they are the proof that the
voice.py key check, the voice.js body line and the dashboard_core.js hooks broke nothing.
