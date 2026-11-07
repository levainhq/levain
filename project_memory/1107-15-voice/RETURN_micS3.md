# RETURN: cockpit mic S3 (routes + flag + gates)

Branch `seat/1107-20-micS3` (worktree `/Users/phillipclapham/Briefcase/.levain-1107-20-micS3`), not pushed. Sha: `git log -1`.

## Built
- `levain/cockpit/voice.py`: `STREAM_OPEN/CHUNK/CLOSE`, `STREAM_PATHS`, `STREAM_STATUS` (code -> HTTP status), `stream_refusal`, `stream_open/chunk/close`, `stream_offered`, and `"stream": true` on voice.json (`serve_voice`). Two new WORDS (`no_stream`, `stream_media_type`).
- `levain/web_server.py`: `_voice_stream_answer` (gates in transcribe's order: room host/broker/dir 404, surface token 403, owner 403, `premium_stt` off 404 `premium_stt_off` BEFORE length/body/meter/key; then content type, declared length, registry, opportunistic `reap()`, body read, route). `_voice_post` dispatches stream paths to it; do_POST carve-out and the reserved-route set include `STREAM_PATHS`. Server owns the registry (built lazily, `max_sessions=1`), a daemon reaper thread (`voice_reap_interval` 5 s) and `_close_voice_streams()` called first in `server_close` (stops the reaper, then `registry.shutdown()` settles every stream). Test seams: `voice_stream_factory`, `voice_stream_kwargs`, `voice_reap_interval`. Cockpit page (`/`) sends `Permissions-Policy: microphone=(self)`; CSP untouched.
- Open/close take a declared length of 0 (413 otherwise). Chunk: `application/octet-stream` (415), <= 64 KiB (413, never read), read under a 10 s deadline, bypasses `voice_gate`.

## Status mapping (pinned in a test)
bad_sequence 409 · bad_chunk 400 · too_fast 429 · ceiling/idle_timeout/closed 409 · unknown_session 404 (wrong owner identical) · too_many_sessions/meter_unavailable/settle_backlog/unpriced/stopping 503 · provider_failed 502 · month_cap/day_cap 402 · internal 500 · key_unavailable follows transcribe's key words (409 no_stt_key / 503 keychain/authority / 500 unexpected). A malformed `seq` is bad_sequence; a malformed or over-long `sid` is unknown_session.

## One deliberate narrowing of the contract
`stream: true` appears only when the FIRST provider row with `key == "set"` and `stt` in uses is a streaming provider (today deepgram). Reason: the page sends the first such row, offered order is inworld, openai, deepgram, and `stream/open` serves deepgram only, so a user holding keys for both would be sent to openai and fail. Better fix (S4/page follow-up): the page should send a provider the server names (e.g. `stream_provider`) instead of "first row". Open for any other provider answers 409 `no_stream`.

## Tests
`tests/test_cockpit_voice_stream_routes.py`: 25 passed (rig reused from test_cockpit_voice_routes; fake stream factory, real signed key, fake security). Each test docstring names the reverting line. Mutation-checked 7 reverts (opportunistic reap, server_close hook, Permissions-Policy arg, premium_stt gate, max_sessions, reaper start, revoke-before-open): each turned a test red. Covers off=>404 x3, auth/token on loopback/mesh/proxied, no-owner, no-room, cross-site, flag present/absent (no key, tts-only, premium off, openai first), revoke before open, key once to the factory, no secret in replies or logs (incl. factory raising with the key in its text), 1-session cap, seq rules, body caps/type/whole samples, voice_gate bypass, wrong-owner == unknown, close settles + collects, timer reap, request reap, server_close settles, header, status table.
Broader run (888 passed, 7 min): test_cockpit_voice_routes, test_voice_stream_session, test_voice_deepgram_stream, test_mic_page, test_voice_csp_media, test_voice_providers, test_voice_meter, test_voice_custody, test_shell_asset_versions, test_voice_room_hook, test_cockpit_k2a, test_http_guards, test_cockpit_w1b_server, and the new file.

## Known edges
- Open reaps inside the registry and every stream request reaps again; reap takes only try-locks, so a hung provider call never blocks it.
- The reaper thread starts on the first stream request, not at server start (nothing to reap before then).
- The chunk path reads its body before the session is looked up, so a wrong-owner/unknown-sid chunk still costs one <= 64 KiB read.
- `deepgram_stream` is imported at open time; a missing wsclient surfaces as a 500 `internal`, not a startup failure.

## Residue (not verified here)
Real Deepgram on a real key; a real iPhone (permission prompts, cadence, lock/call); the reaper timer under real load and a real slow provider; Permissions-Policy in a real Safari/Chrome (header presence only is tested).
