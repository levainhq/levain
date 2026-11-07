# OpenAI Realtime transcription: what was verified (read 2026-11-07)

Pages read: https://developers.openai.com/api/docs/guides/realtime-transcription (platform.openai.com 301s there),
https://developers.openai.com/api/docs/guides/realtime-conversations, and the first 100k chars of
https://developers.openai.com/api/reference/resources/realtime/server-events. /guides/realtime-websocket returned 404.
Page content came through WebFetch's summarizer, not raw HTML.

## Verified (stated on a page)
- URL `wss://api.openai.com/v1/realtime?intent=transcription`, no `model` param.
- Setup is `session.update` with `session.type "transcription"`; `session.audio.input.format {type:"audio/pcm", rate:24000}`;
  `session.audio.input.transcription.model "gpt-live-transcribe"`; `session.audio.input.turn_detection null` (the model supports
  neither server_vad nor semantic_vad). Optional transcription fields prompt, keywords, languages (plural), delay.
- Audio: 24 kHz PCM16 mono, base64 in `audio`; the Go example sends 9,600-byte (200 ms) chunks.
- Client events `input_audio_buffer.append {type, audio}` and `input_audio_buffer.commit {type}` (commit at the end of a turn).
- Server events `conversation.item.input_audio_transcription.delta {item_id, content_index, delta}` ("Hello,", incremental),
  `...completed {item_id, content_index, transcript}`, `input_audio_buffer.committed {item_id}`, `...transcription.failed {item_id}`.
  Completions of different turns may arrive out of order: match by item_id.
- `error` event: `{event_id, type:"error", error:{type, code?, message, param?, event_id?}}` (reference page, with example).
- With VAD off on a conversation session, you "manually send input_audio_buffer.commit".

## Could NOT verify
- Auth header: no page fetched shows it. `Authorization: Bearer <key>` is the standard OpenAI form; no `OpenAI-Beta` header is
  sent (the GA transcription page does not name one). Wrong guess = a 401 at handshake (counts as started, one unit).
- That the model id `gpt-live-transcribe` is available to Phill's key; that a `session.update` right after connect (before
  `session.created`) is accepted.
- Whether deltas stream before a commit when turn_detection is null (the adapter assumes transcription follows each commit).
- The minimum commit size: 100 ms is from the older Realtime docs, not these pages; the adapter pads a short tail to 100 ms.
- Whether `delta` is incremental (assumed: yes, per the "Hello," example) or cumulative.
- Idle timeout / keepalive rules, session max length, close codes, rate limits; the JSON of session.created/updated.
- The reference page's remaining 135k chars (events section) were not read.
