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
- (Fixed in L3 r1: a chunk's sid, owner and seq are checked before its body is read; reads are bounded by `voice_chunk_gate`.)
- `deepgram_stream` is imported at open time; a missing wsclient surfaces as a 500 `internal`, not a startup failure.

## Residue (not verified here)
Real Deepgram on a real key; a real iPhone (permission prompts, cadence, lock/call); the reaper timer under real load and a real slow provider; Permissions-Policy in a real Safari/Chrome (header presence only is tested).

## L3 r1 fix round (4 lineages: complement, codex, deepseek, gemini@flash-low)
Every finding resolved against disk first. Tests: `tests/test_cockpit_voice_stream_routes.py` (+7) and `tests/test_voice_stream_session.py` (+2); 94 passed across the two files; broader run (19 voice/web_server/mic/stream files incl. test_web_server, test_cockpit_voice_routes, test_mic_page, test_cockpit_stream) 1113 passed in 10.5 min. Mutation-checked M1-M7 below: each revert turned its test red.

| # | Finding | Verdict | Fix / evidence | Test (revert that turns it red) |
|---|---|---|---|---|
| 1 | codex 1, complement 1: first stream request after server_close builds a registry + reaper nothing stops | CONFIRMED | `voice_streams_closed` set under `voice_stream_lock` in `_close_voice_streams`; `_voice_stream_registry` returns None after it; the route answers 503 `stopping` | `test_no_registry_or_reaper_is_built_after_server_close` (M1: drop the closed check) |
| 2 | codex 2: shutdown does not wait for an open inside `registry.open()` holding a `reserved` row | CONFIRMED | `StreamRegistry.shutdown` waits (Condition, bounded `open_wait`, default `OPEN_WAIT_S` 10 s) for opens in progress on OTHER threads; `_build` now gives its slot back LAST, after closing the stream and ending the reservation | `test_shutdown_waits_for_an_open_in_another_thread_to_end_its_reservation` (M6: no wait; M7: slot given back first), `test_shutdown_wait_is_bounded` |
| 3 | codex 5: registry published before `thread.start()`; close joins an unstarted thread and skips `shutdown()` | CONFIRMED (red test also hit `cannot join thread before it is started` in teardown) | start before publish; `_stop_voice_registry` joins only a live thread and always reaches `shutdown()` | `test_a_reaper_that_cannot_start_publishes_no_registry` (M2: start after publish) |
| 4 | codex 3, complement 5: singleton registry pins the first meter | CONFIRMED (latent: needs a store-directory rebind) | registry keyed on `meter.path`; a different path builds a new registry and stops + shuts the old one (its streams settle on their own ledger) outside the lock | `test_a_meter_rebind_rebuilds_the_registry_over_the_new_ledger` (M3: drop the path compare) |
| 5 | codex 4: chunk bodies bypass every gate; ownership checked after the body is read | CONFIRMED (authenticated client only) | `cockpit_voice.stream_chunk_precheck` (query, seq, sid+owner) before any read, connection closed on refusal; `voice_chunk_gate` (`STREAM_CHUNK_INFLIGHT`=2, non-blocking, 503 `voice_busy`) around read+feed. mic.js sends chunks serially, so 2 never binds a real page | `test_an_unknown_sid_chunk_is_refused_before_its_body_is_read` (M4), `test_chunk_body_reads_are_bounded_by_their_own_gate` (M5) |
| 6 | deepseek 1: `stream_offered(rows)` UnboundLocalError on an unreadable meter | REFUTED | `rows = []` is bound before the meter `try` in `serve_voice` | pinned: `test_voice_json_with_an_unreadable_meter_and_premium_stt_on_is_200` |
| 7 | deepseek 2: `_voice_post` releases `voice_gate` unconditionally for stream paths | REFUTED | `_voice_post` touches no gate; acquire/release are both inside `_voice_answer`, which stream paths never enter | existing `test_chunks_do_not_take_a_voice_slot` |
| 8 | deepseek 3: missing Content-Type on a chunk is 500 | REFUTED | `base_type(None)` returns `""` -> 415 | pinned: `test_a_chunk_with_no_content_type_is_415` |
| 9 | deepseek 5: meter rebuilt per request | REFUTED | `_voice_meter` returns the held Meter unless the path changed; building one creates no file | - |
| 10 | deepseek 4: reaper join times out, `shutdown()` races `reap()` | REFUTED | registry state is under `_lock`; `_end_if_expired` takes a try-lock and `_end` is idempotent on `_closed`; `_retry_unsettled` detaches its batch atomically, so no double settle | - |
| 11 | deepseek 6: `parse_qsl` mangles `+` in sids | REFUTED | sids are `secrets.token_urlsafe` (no `+`); mic.js percent-encodes | - |
| 12 | gemini 1: missing-`seq` KeyError | REFUTED | `_stream_query` returns None unless the keys are exactly `{sid, seq}` | - |
| 13 | gemini 2: `voice_custody` NameError | REFUTED | `web_server.py:122` imports `custody as voice_custody` | - |
| 14 | gemini 3: per-request `reap()` destroys active sessions | REFUTED | reap ends only sessions past idle (15 s) / age limits, re-checked under each session's try-lock; such a chunk would be refused by `feed` anyway | - |
| 15 | gemini 4: off-by-one on the chunk cap / length on open+close | REFUTED | `clen > MAX` (exactly 64 KiB passes, pinned in `test_chunk_body_rules_size_type_and_whole_samples`); `_declared_length` is the shared framing for every POST | - |
| 16 | gemini 5: `stream_close` hides a non-str close | REFUTED | `StreamSession.close` always returns `self._final` (str) | - |
| 17 | gemini 6: `last_read` not set on a revoke-reach refusal | REFUTED | matches `transcribe`/`_call`, which do not set it either (reach is not a key-read state) | - |
| 18 | complement 3: shutdown hangs on a stuck provider call | REFUTED as a hang (known edge) | the Deepgram stream's socket idle timeout is `TICK_S` 1 s and its send lock waits at most 2 ticks, so `_end_now` waits seconds, not forever | - |
| 19 | complement 4: Permissions-Policy comment overstates | CONFIRMED (comment only) | comment now says a same-origin frame still can | - |
| 20 | complement 2: revoke enforced only at open | KNOWN EDGE (not fixed) | a revoke mid-session lets chunks flow until idle (15 s) or the age limit (~135 s); the next open is refused | - |

Note: fix 2 touches `levain/voice/stream_session.py` (micS1's file); a merge with another branch editing `_build`'s finally or `shutdown` will conflict there.
