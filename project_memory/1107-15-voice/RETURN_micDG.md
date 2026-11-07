# RETURN micDG (cockpit mic S2b: Deepgram live-stream adapter)

Branch seat/1107-15-micDG (on origin/seat/1107-15-micws). Not pushed, not merged.

## Head must merge first
S1 (`seat/1107-15-micS1`, owns `levain/voice/stream_session.py`). The adapter imports
`ProviderStream, StreamNotStarted, StreamUpdate` from it at module top. This branch does not contain S1; the test file
installs a stand-in `levain.voice.stream_session` in sys.modules only when the real one is absent, so after the S1 merge the
real module is used with no edit. Also needs the wsclient branch (already this branch's base).

## API (levain/voice/deepgram_stream.py)
- `factory(secret) -> DeepgramStream` (what S3 passes to `StreamRegistry.open`).
- `DeepgramStream(secret, *, base_url, connect_opts, keepalive_after, close_drain)`: `send_audio(pcm)`, `poll() -> StreamUpdate(partial, final, failed)`, `close() -> str` (idempotent).
- Connect failure: `StreamNotStarted` when wsclient refused before any byte (url/header/connect), else `DeepgramStreamError("connect")` (S1 settles one unit).
- `DeepgramStreamError(code)` with fixed `WORDS`; `stream.reason` holds a fixed code once failed.
- Query (pinned by test): `model=nova-3&encoding=linear16&sample_rate=16000&channels=1&interim_results=true&endpointing=300&smart_format=true&mip_opt_out=true`; header `Authorization: Token <key>`; host pinned to `providers.ALLOWED_HOSTS`.
- One reader thread, bounded queue (256), KeepAlive after 4 s idle (max 6 in a row), CloseStream on close then wait up to 3 s for Metadata/close, unfinalised partial kept in the returned text.

## Tests
tests/test_voice_deepgram_stream.py: 20 passed (13.5 s). Fake server reuses `Server` from tests/test_voice_wsclient.py. Covers: query/header, interim->final assembly, is_final handling, close drains last final, partial kept at deadline, provider error close, 6 malformed shapes, binary + Error message, early normal close, keepalive bound, overflow, connect refusals (not started), 401 after connect, key absent from attributes/repr/exception frames (traceback f_locals walk).

## Sources (fetched 2026-11-07)
- https://developers.deepgram.com/docs/lower-level-websockets
- https://developers.deepgram.com/reference/speech-to-text/listen-streaming.md

## UNVERIFIED
- `Token <key>` header value form (pages give no format; matches repo's prerecorded adapter).
- Literal JSON of KeepAlive/CloseStream/Finalize (pages give schema type enum only; `{"type": "<Name>"}` derived).
- Metadata and UtteranceEnd example JSON (required fields only on the page); that Metadata always arrives after CloseStream; the close code after CloseStream (1000/1005 accepted).
- Name of the error message type (treated as any `type` containing "error"); close codes on fatal errors.
- `endpointing=300` value (default 10 ms per the page); real latency; behavior against a live key.
