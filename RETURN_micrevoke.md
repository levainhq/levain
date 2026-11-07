# RETURN micrevoke: a revoke mid-dictation ends the stream session

Branch seat/1107-20-micrevoke (on d7b149f2). Not pushed.

## Built
- `levain/cockpit/voice.py`: `stream_chunk` (now takes keyword `data_root`, `revoke_queue`) re-asks the open path's revoke question every `STREAM_REVOKE_RECHECK_S` = 2.0 s of SESSION time (`_stream_revoked`): (1) the provider's voice key record is still in a fresh log replay and still lists `stt` (the real revoke: `levain voice key revoke` / the grant page remove it; the secret is not read), (2) `revoke_refusal` still passes. Either failing ends the session via `StreamSession.end_revoked()` (the registry's normal `_end`: provider stream closed, meter settled, no `reserved` row) and answers the chunk with the open path's own fixed words (`no_key`/`no_stt_key`, `authority_unreadable`, `revoke_unreachable`, `revoke_unchecked`) plus `"revoked": true`. A log that cannot be replayed mid-session also ends it (same fail-closed rule as open). The chunk's audio is never sent.
- `levain/voice/stream_session.py`: `revoke_check_due(interval)` (cadence clock, per session, restarts on each check) and `end_revoked()`. "revoked" is an end reason only (no WORDS entry: the status-table test pins WORDS minus key_unavailable).
- `levain/web_server.py`: passes `data_root` and the grant queue to `stream_chunk`.
- `levain/templates/web/mic.js`: `postJson` reads `revoked` off a non-ok body; the chunk-failure catch treats `revoked` as a FINAL stop: no WAV fallback, kept PCM dropped, capture released, one fixed sentence ("Premium voice was turned off, so the dictation stopped; the words so far are in the box."), session finished.

## Cost measured
One `_stream_revoked` (two fresh replays, the rig's signed log) = 3.1 ms (test prints it). Cadence 2.0 s + one 250 ms chunk puts a revoked session's end under ~2.3 s, inside the 3 s target; per-chunk would be ~12x more log reads for no gain.

## Tests (each docstring names its red line)
- tests/test_cockpit_voice_stream_routes.py: revoke between chunk 3 and 4 ends at the next check (409 no_key + revoked, 5 chunks sent, stream closed once, row settled `ok`, no `reserved`, key value in no response); unrevoked session streams 12 chunks through every check; log read on a cadence (2 checks in 16 chunks); revoke-path loss ends it in `revoke_unreachable` words; unreadable log ends it closed; check cost bound.
- tests/test_mic_page.py: `test_a_revoked_chunk_is_a_final_stop_with_the_fixed_words_and_never_a_wav` (existing fake fetch, new `chunkRevoked` switch).

## Mutation checks (all turned red, restored)
cadence block disabled (4 red) · `end_revoked()` call dropped (2 red) · `end_revoked` closing the stream but not settling (red, row not settled) · key-record test inverted (4 red) · `revoke_refusal` call dropped (1 red) · mic.js `err.revoked` branch off (red: WAV posted).

## Suites run
test_cockpit_voice_routes, test_cockpit_voice_stream_routes, test_mic_page, test_voice_custody, test_voice_deepgram_stream, test_voice_openai_stream, test_voice_player_browser, test_voice_stream_session : 407 passed.

## Known edge left
A revoke is seen within the cadence, not instantly: up to ~2.25 s of audio after it still reaches the provider (and is metered).
