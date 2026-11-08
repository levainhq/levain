# RETURN micS5oa (cockpit mic S5a: OpenAI Realtime transcription stream adapter)

Branch seat/1107-20-micS5oa (on the S3 branch). Committed, not pushed. web_server.py and voice.py untouched (a later step wires the factory).

## Built
- `levain/voice/openai_stream.py`: `OpenAIStream` (send_audio / poll / close), `factory(secret)`, `OpenAIStreamError(code)` with fixed `WORDS`,
  `Resampler16to24`, `close_bound`. Modelled rule for rule on deepgram_stream.py (one reader thread, send lock, key revealed once
  into the `Authorization: Bearer` header only, fixed reason codes, close stops the reader before the socket).
- Host `api.openai.com` is already in `providers.ALLOWED_HOSTS`; no change needed there.
- Differences: `session.update` (transcription, audio/pcm 24 kHz, turn_detection null) sent right after connect; adapter commits
  every ~2 s of audio itself (no server VAD) and at close (tail zero-padded to 100 ms); items kept in commit order, matched by
  item_id, deltas -> partial of the newest pending item, completed -> final; no keepalive; close = final commit then wait up to 5 s
  for every commit to complete (else `incomplete`, text still returned). Linear 16->24 kHz resample, chunk-boundary safe.
- `tests/test_voice_openai_stream.py`: 27 tests, each docstring names the line whose revert turns it red (I did not run the
  reverts; the names are from reading the code). Deepgram tests still pass (60 passed together, twice).
- `project_memory/1107-15-voice/OPENAI_REALTIME_NOTES.md`: verified vs not.

## UNVERIFIED (nothing run against a live key)
- Auth header form (`Authorization: Bearer`, no `OpenAI-Beta`); no fetched page showed it. Wrong = 401 at handshake (started, one unit).
- Model id `gpt-live-transcribe` for this key; session.update accepted before session.created.
- Whether deltas stream without a commit or only after each commit; delta incremental vs cumulative.
- 100 ms minimum commit (older docs), idle timeout / session max, close codes, session.created/updated JSON.
- Commit cadence of 2 s splits words at boundaries; latency/quality untested.
- Resolution is count-based (completed events vs commits sent): an unexpected extra completion could end close() early.
- Docs read through WebFetch summaries; the server-events page's last 135k chars were not read.
