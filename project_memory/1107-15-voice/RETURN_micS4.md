# RETURN: cockpit mic S4 (the page)

Branch `seat/1107-15-micS4` (worktree `/Users/phillipclapham/Briefcase/.levain-1107-15-micS4`), not pushed. Sha: see `git log -1`.

## Files
- `levain/templates/web/mic.js` (new): `LevainMic` = available() / probe() / start() / stop() / active() / onPartial / onFinal / onNote / onEnd.
- `levain/templates/web/mic_worklet.js` (new): the AudioWorklet processor `levain-mic-tap` (forwards 128-frame Float32 blocks; resample + chunk live in mic.js so the ScriptProcessor fallback shares one path).
- `levain/web_server.py`: two `_ASSETS` rows (`/mic.js`, `/mic_worklet.js`). `levain/templates/web/dashboard.html`: `<script src="/mic.js">` after voice.js (shell digests pick it up automatically).
- `levain/templates/web/room_view.js`: a "Mic" button (`.rv-mic.rv-mic-stream`) beside Dictate, `hidden` until `LevainMic.probe()` resolves true. Send, unmount and a second tap stop it. Existing Dictate path untouched.
- `tests/test_mic_page.py` (new, 7 tests).

## CONTRACT the server slice (S3) must meet
Gate flag: `GET /cockpit/voice.json` must carry top-level `"stream": true` (only when premium_stt is on AND some provider lists `stt` in `uses` with `key: "set"`). The page shows the button only if `stream === true` AND a provider row has `key == "set"` and `"stt"` in `uses`; the first such provider's name is sent as `provider`. Absent/false = no button (default).

All POSTs carry header `X-Levain-Write-Token` (localStorage `levain_write_token`), same-origin, no cookies assumed.
1. `POST /cockpit/voice/stream/open?provider=<name>`, NO body. -> 200 `{"sid": "<string, 1..128 chars>"}`. Any non-2xx = open failed.
2. `POST /cockpit/voice/stream/chunk?sid=<sid>&seq=<n>`, body = raw PCM16 little-endian mono 16 kHz, `Content-Type: application/octet-stream`, normally exactly 8000 bytes (250 ms); the last chunk on stop may be shorter (even length). `seq` starts at 0 and is +1 per chunk, sent strictly serially (the next only after the previous answered). -> 200 `{"seq": n, "partial": "<string>", "final": "<string>"}` where `final` = ALL text committed so far and `partial` = the uncommitted tail; the page shows `final + " " + partial`. Non-2xx = the page stops, says "Streaming dictation stopped; the words so far are in the box.", and sends a best-effort close.
3. `POST /cockpit/voice/stream/close?sid=<sid>`, no body. -> 200 `{"text": "<string>"}` = the session's full final text; it replaces the live text in the draft. Close is also sent on stop(), a hidden page / pagehide, and the 120 s page-side ceiling (MAX_MS).
The page never sends any text. CSP unchanged (same-origin fetch; the worklet loads from `/mic_worklet.js`).

## Fixed sentences
- Open failed (permission refused, no mic, server open non-2xx, context creation failed): "Streaming dictation could not start; the Dictate control and the keyboard dictation key still work."
- Mid-stream failure: "Streaming dictation stopped; the words so far are in the box."

## Tests
`python3 -m pytest tests/test_mic_page.py` -> 7 passed (assets served and page tag; ordered seq 0,1,2 with 8000/8000/~3200-byte PCM and token header, partial drawn as value text with `<b>` kept literal, stop sends close, releases track and closes the context; hidden page stops; no button when the flag is absent; refused permission shows the sentence and opens nothing; server-refused open releases mic; ScriptProcessor fallback chunks identically). Mutation spot check: removing the visibilitychange listener and track.stop() turned 3 of them red. Also run: tests/test_voice_player_browser.py, test_voice_room_hook.py, test_shell_asset_versions.py (53 passed together with test_mic_page).

## Residue (a real iPhone only)
Permission prompts in a Safari tab and the Home Screen app (re-prompt per launch?), real AudioWorklet cadence and sample rate on iOS, screen lock / incoming call / route change behaviour, whether the mic indicator clears after stop, true latency, and whether the 120 s ceiling fits. The fakes here prove the logic, not the device.
