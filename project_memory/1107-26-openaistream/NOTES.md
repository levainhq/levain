# OpenAI live dictation: events and price (read 2026-11-07)

Event model, https://developers.openai.com/api/docs/guides/realtime-transcription : with turn_detection null the client commits;
`input_audio_buffer.committed` carries the `item_id`; `conversation.item.input_audio_transcription.delta` and `.completed` carry
that `item_id` (completed also `transcript`); `.failed` carries it too. Completion order across items is NOT guaranteed; the
docs' example records the id from `committed` and ignores deltas and completions whose id does not match. `gpt-live-transcribe`
is the recommended realtime transcription model (no server_vad / semantic_vad; no detected-language output).

Price, https://developers.openai.com/api/docs/pricing : gpt-live-transcribe $0.017 / minute (gpt-realtime-whisper the same);
gpt-4o-mini-transcribe $0.003 / minute; gpt-4o-transcribe $0.006 / minute. So the codex finding stands (about 5.7x).

Fix: `providers.STREAM_STT_MODELS` is the one table the adapter, the stream-open route and the price check read; the meter has a
`gpt-live-transcribe` row at 17_000 micros per 60 s. `_take` ignores events for ids not seen in `committed`.
