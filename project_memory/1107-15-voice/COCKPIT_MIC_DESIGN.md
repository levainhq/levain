# Cockpit mic: getUserMedia + server STT with streaming partials (Phill 346 a)

Research lane, 2026-11-07, read-only on code. Web pages were NOT fetched this session: every external claim below is from
my knowledge of the docs at the URLs given and is marked UNVERIFIED until a slice fetches it. Repo facts are read from disk.

## What the repo has today (read)
- `levain/cockpit/voice.py`: `transcribe()` = one POST of a finished recording (`UPLOAD_MAX` 4 MiB, `BODY_DEADLINE_S` 20,
  `UPLOAD_BYTES_PER_SECOND` 4000 as the meter floor, MIME in `providers.MIME_ALLOW`: webm, mp4, mpeg, wav). Goes through
  `_call()`: revoke-reach check, `meter.reserve(provider,"stt",model,units)`, `custody.read_key`, adapter call with
  `secret.reveal()` inline, `settle`/`release` in `finally`. Fixed-words refusals only.
- `web_server.py` ~3800: the transcribe route is 404 `premium_stt_off` unless `srv.premium_stt` (hidden `levain serve --premium-stt`).
  `voice_gate` = BoundedSemaphore(`VOICE_INFLIGHT`=2) bounds voice POSTs.
- `providers.py`: `DeepgramProvider.transcribe` (nova-3, prerecorded `/v1/listen`, `mip_opt_out=true`), `OpenAIProvider`
  (gpt-4o-mini-transcribe), `urllib_transport` (https, `ALLOWED_HOSTS`, no redirects). Stdlib only: no websocket client anywhere
  (`git grep websocket` hits only `levain/broker/*`, which is the credential broker's own handshake checker, not reusable).
- `web_server.py` is `ThreadingHTTPServer` + `BaseHTTP` handler: no websocket SERVER. No SSE route found (UNVERIFIED: I grepped
  `text/event-stream` in web_server.py only).
- `http_guards._CSP`: `default-src 'none'; script-src 'self'; connect-src 'self'; media-src blob: ...`. No `Permissions-Policy` header
  exists anywhere in levain/ or tests/ (grep), so the default allows `microphone` for self. Streaming to our own origin needs NO CSP change.
- `room_view.js` ~423-680: the composer already has a mic ("Dictate") built on `webkitSpeechRecognition`, shown ONLY if
  `processLocally === true` (on-device or absent); helpers `endDictation()`, `dbase`/`heard` state, foreground-only (`leave`), `note`
  line for fixed-word errors. The new mic reuses this slot and state machine; it replaces nothing when the premium switch is off.

## How established products do it (named, UNVERIFIED-fetched)
1. Deepgram live streaming: browser mic -> your server/ephemeral token -> `wss://api.deepgram.com/v1/listen` with raw `linear16` +
   `sample_rate`/`channels`, `interim_results=true`, `endpointing`, KeepAlive every <10 s, `CloseStream` to flush. Their browser
   example sends MediaRecorder `audio/webm;codecs=opus` chunks (timeslice 250 ms); Deepgram auto-detects containers on the socket.
   https://developers.deepgram.com/docs/live-streaming-audio · https://developers.deepgram.com/docs/getting-started-with-live-streaming-audio
2. OpenAI Realtime transcription session (WebSocket or WebRTC; `input_audio_buffer.append` base64 PCM16 24 kHz mono; deltas
   `conversation.item.input_audio_transcription.delta`). Same shape: PCM over a socket, partial deltas back.
   https://platform.openai.com/docs/guides/realtime-transcription (URL form UNVERIFIED; the repo cites developers.openai.com/api/docs/...)
3. Chunk-capture choices (MDN + WebKit): `MediaRecorder` timeslice gives container fragments (webm on Chrome, **audio/mp4 on Safari 14.1+**,
   never webm on iOS before 18.4, UNVERIFIED); only the FIRST fragment carries the header, so a mid-stream chunk is not decodable alone.
   `AudioWorklet` (Safari 14.5+) hands raw Float32 -> we convert to PCM16 16 kHz: every chunk is independent and every provider takes it.
   https://developer.mozilla.org/en-US/docs/Web/API/MediaRecorder · https://developer.mozilla.org/en-US/docs/Web/API/AudioWorklet
4. iOS PWA permission: WebKit bug 327237 (copied from flowConnect's 1030 VOICE.md): standalone Home Screen apps may re-ask the mic
   prompt each launch; also the mic stops when the page is backgrounded or the screen locks. UNVERIFIED by me; this is the iPhone residue.

## Decisions
- **Capture: AudioWorklet -> PCM16 mono 16 kHz** (resample in the worklet; ~32 KB/s). Chosen over MediaRecorder because mp4 fragments
  from Safari are not independently decodable and webm differs by browser; PCM also lets the stop-time fallback wrap the buffer as WAV
  (already in `MIME_ALLOW`) and use today's prerecorded `transcribe()`. Fallback if no AudioWorklet: ScriptProcessor (deprecated, UNVERIFIED on iOS 26).
- **Transport page -> server: a session of short POSTs, not a WebSocket and not one chunked POST.** Safari has no fetch request-body
  streaming (UNVERIFIED), and a stdlib websocket server is a new attack surface in a security-reviewed handler. Routes, all behind the same
  gates as transcribe (premium_stt, owner, token, CSRF/origin as today):
  `POST /cockpit/voice/stream/open?provider=` -> `{sid}`; `POST .../stream/chunk?sid=&seq=` (body = ~250-500 ms PCM, <=64 KiB) ->
  `{seq, partial, final}`; `POST .../stream/close?sid=` -> `{text}`. Each chunk response piggybacks the newest partial (latency =
  chunk interval + provider, expect 0.5-1 s UNVERIFIED), so no SSE/long-poll is needed. seq must be strictly +1 (gap or repeat = fixed-words refusal).
- **Server -> provider: one outbound wss per session, a small hand-written RFC 6455 client** in new `levain/voice/wsclient.py`
  (stdlib `ssl`+`socket`, host must be in `ALLOWED_HOSTS`, no redirects, masked frames, ping/pong, size caps). No new dependency
  (web_server's header says "no dependencies"). Deepgram first (`wss://api.deepgram.com/v1/listen?model=nova-3&encoding=linear16&sample_rate=16000&channels=1&interim_results=true&smart_format=true&mip_opt_out=true`). OpenAI Realtime = a later slice, same interface.
  Reader thread per session folds provider messages into `{partial, finals[]}` under a lock; the chunk route just reads it.
- **Key never reaches the page**: `custody.read_key` at open, `secret.reveal()` inline only into the handshake header (Deepgram
  `Authorization: Token`), then the name is deleted; the socket stays open without the key in any frame. Same rule as `_call` step 5.
- **Meter/caps**: reserve at open for a session ceiling (`STREAM_MAX_S`=120 s -> units 120, same unit as `upload_units`), settle at close with the
  seconds actually sent (bytes/32000, not the 4000 B/s floor); a row must never stay `reserved` (no reaper): close, idle timeout (15 s without
  a chunk), server shutdown and wss failure all settle in a `finally`. Today `Meter.settle(id)` takes no amount: UNVERIFIED whether it can
  settle a smaller figure; if not, slice 1 reserves the actual-rounded ceiling per 30 s window (reserve, settle, reserve next window).
- **Concurrency**: `VOICE_INFLIGHT`=2 is per request; a session is long-lived, so add a `stream_slots` semaphore (1 session at a time, he is one
  person) and let chunk POSTs bypass `voice_gate` (cheap, bounded body, session-owned). Max sessions, max sid age and per-sid byte cap server-side.
- **Gate**: reuse `srv.premium_stt`; off = 404 `premium_stt_off` on all three routes before any read. `voice.json` gains `"stream": true` only
  when on AND the provider lists `stt`. Product default stays OFF (Phill 11-05); this lane does not flip it.
- **CSP/Permissions-Policy**: connect-src 'self' already covers the POSTs; no `ws:` origin needed (page never opens a socket). Add an explicit
  `Permissions-Policy: microphone=(self)` response header (today absent = also allowed) so the intent is pinned and a test fails if it drops.
  No `media-src` change (capture is not playback).
- **UX**: the existing mic slot; tap = start, partial text fills the draft after `dbase` in a muted style, final replaces on `close`; foreground
  only (`visibilitychange`/`pagehide` => stop + close); fixed-words notes for denied/no-mic/provider-down; on any failure fall back to the
  on-device `webkitSpeechRecognition` control when present, else the keyboard dictation key (same message the page shows today).

## Slices (each one Sonnet lane; DONE line is the gate)
1. **S1 session core + meter** (owns `levain/cockpit/voice_stream.py` new, `tests/test_voice_stream_core.py` new; edits `levain/cockpit/voice.py`
   only to export WORDS/helpers). Session table, seq rules, caps, ceiling/idle/settle-in-finally, partial/final folding against a FAKE provider
   stream object. DONE: unit tests prove no `reserved` row survives close / idle / exception / shutdown, seq gap refused, key never in any logged/returned value (the VALUE-leak pattern of test_cockpit_voice_routes.py).
2. **S2 ws client + Deepgram stream adapter** (owns `levain/voice/wsclient.py`, `levain/voice/providers.py` stream class, `tests/test_voice_wsclient.py`).
   Tested against a local in-process fake wss/ws echo server on 127.0.0.1 (host allowlist test-injectable), covering masking, fragmented and ping frames,
   oversize, close, handshake-key mismatch. DONE: adapter yields interim + final from a recorded real Deepgram JSON transcript fixture (copy shapes from the docs; UNVERIFIED until a real key run).
3. **S3 routes + gates** (owns `levain/web_server.py` stream section only, `tests/test_cockpit_voice_stream_routes.py` new modelled on test_cockpit_voice_routes.py with `voice_transport` replaced by a fake stream factory).
   DONE: premium_stt off => 404 on all three, auth/owner/origin same as transcribe, body caps, `voice.json` `stream` flag, `Permissions-Policy` header test in test_voice_csp_media.py's style; leak scan clean.
4. **S4 page capture** (owns `levain/templates/web/voice_capture.js` new worklet+client, `levain/templates/web/room_view.js` mic slot hook, `tests/test_voice_stream_browser.py`).
   Worklet PCM16/16k, chunker, open/chunk/close client with serial in-order sends and abort. DONE: headless Chrome test with a fake `getUserMedia` (oscillator stream) against the fake-provider server shows partials in the draft, stop => final, denied/no-mic notes, hidden page stops.
5. **S5 fallback + OpenAI** (owns `providers.py` OpenAI Realtime stream class, the stop-time WAV fallback in voice_capture.js). DONE: with the ws path forced down, recording is sent as WAV to the existing `transcribe()` and text lands; OpenAI stream passes the S2-style fake-server test.
6. **S6 real-key + real-device pass (Phill's phone, a hands task, one ask)**: see residue.

## Residue (only a real iPhone / real key can show)
- Mic permission persistence in the installed Home Screen app (re-prompt each launch? bug 327237) and in a Safari tab; behaviour on `visibilitychange`, screen lock, an incoming call, AirPods route change.
- Whether AudioWorklet delivers steady 128-frame blocks at 48 kHz on iOS 26 under low power mode; resample quality; any audible clipping/echo (use `echoCancellation`, `noiseSuppression` constraints, UNVERIFIED).
- True partial latency over the tailnet/LAN + wss to Deepgram; chunk cadence that feels live (250 vs 500 ms).
- Real Deepgram nova-3 streaming and OpenAI Realtime protocol details (message shapes, KeepAlive, 5xx behaviour) against a live key; spend per minute vs the meter's price row.
- Desktop Chrome with the key in the real keychain via `custody.read_key`; the `--premium-stt` switch on a real `levain serve`.
- Does the 120 s ceiling and 1-session limit fit how he dictates; whether Safari "stays on" mic indicator after close (track.stop() leak check).
