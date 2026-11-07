# RETURN micS1 (cockpit mic slice S1, session core)

Branch `seat/1107-15-micS1` (from origin/main e82a43d5). Not pushed.

## Files
- NEW `levain/voice/stream_session.py`, NEW `tests/test_voice_stream_session.py`
- ADDED `Meter.settle_actual(reservation_id, units)` in `levain/voice/meter.py` (existing behaviour untouched). Meter.settle took no amount, so this is the minimal API: row -> ok, units = min(max(units,1), reserved), cost = price(units) capped at the reserved cost; unknown/ended id -> False. Tested in the new test file.

## API
- `StreamRegistry(meter, max_sessions=4, idle_timeout=15.0, clock=time.monotonic)`
  - `.open(owner, provider, model, *, data_root, read_key, factory) -> StreamSession`: slot check, `meter.reserve(provider,"stt",model,120)`, `read_key(provider,"stt",authority=custody.authority_for(data_root))` once, `factory(Secret)->ProviderStream` (factory must reveal inline), key deleted. Any failure releases reservation + slot, raises `StreamRefused`.
  - `.get(sid, owner)` (wrong owner == unknown == ended: `unknown_session`), `.reap()` (idle sessions closed+settled), `.shutdown()`, `len()`.
- `StreamSession`: `.feed(seq, pcm) -> StreamUpdate` (seq strictly 0,1,2..; chunk 1..64 KiB, even length; rate <= 2x wall clock + 4 s burst; 120 s ceiling), `.mailbox()`, `.close() -> final text` (idempotent), `.take_audio()` (bounded buffer for the stop-time WAV fallback, cleared at end), `.owned_by()`, `.closed`, `.streamed_bytes`, `.sid` (token_urlsafe(24)).
- `ProviderStream` protocol: `send_audio(pcm)`, `poll() -> StreamUpdate(partial, final, failed)`, `close() -> str`. `StreamFactory = Callable[[Secret], ProviderStream]`.
- `StreamRefused(code, key_code="")`, `.words`; fixed `WORDS` table. Codes: bad_sequence, bad_chunk, too_fast, ceiling, idle_timeout, closed, unknown_session, too_many_sessions, provider_failed, meter_unavailable, month_cap, day_cap, unpriced, key_unavailable (+ key_code from custody / "unexpected"), internal.
- Settle: `_end()` closes the provider stream, then in a `finally` settles `seconds_for(bytes)` (ceil bytes/32000) via `settle_actual`; zero audio -> `release` (costs nothing). Bytes are counted before the send, so a send that fails mid-chunk is still charged for it (may have been billed).

## Tests
`tests/test_voice_stream_session.py` + `tests/test_voice_meter.py`: 101 passed. Covers seq gap/dup/replay/non-int, bad chunk, rate limit, ceiling (exact 120 s), buffer bound, session cap + slot return, owner mismatch, id uniqueness, settle exact seconds on close / reap idle / idle at feed and mailbox / provider send failure / poll failure / close failure / shutdown, release when no audio, open failures (key, factory, cap reads no key, meter down), key not an attribute, caplog free of audio/text/key.

## Design notes / questions for S3
- Idle expiry is lazy: S3 should call `registry.reap()` from a timer or each request; sessions also self-detect at feed/mailbox.
- `voice.revoke_refusal` (the revoke-reach gate) is NOT in `open()`; S3 must run it before `open`, as `_call` does.
- Pricing uses the Deepgram `nova-3` pre-recorded stt row; a streaming rate would be a table edit.
- The mailbox's `final` is the provider's finalised-so-far text; `close()` returns the provider close's text.
