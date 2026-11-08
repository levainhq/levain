# RETURN_micwire

Branch seat/1107-20-micwire (base c6d451b7). Not pushed.

## What changed
- `levain/cockpit/voice.py`: `STREAM_PROVIDERS = ("deepgram", "openai")`. New `stream_provider(rows)`: the first row with key set, `stt` in uses AND a name in STREAM_PROVIDERS, else None. `stream_offered` now wraps it. `GET /cockpit/voice.json` adds a top-level `"stream_provider": "<name>"` beside `"stream": true`; both are absent when premium_stt is off or no row qualifies. The page-side "first keyed row" mismatch is closed: openai (listed first) now qualifies, and an adapter-less keyed row never hides a qualifying later one.
- `levain/web_server.py` (STREAM_OPEN): factory table `{"deepgram": deepgram_stream.factory, "openai": openai_stream.factory}` picked by the `provider` query. Custody (`voice_custody.read_key`), revoke-reach check, registry reserve/settle and meter are the unchanged shared `stream_open` path, so OpenAI goes through exactly what Deepgram does. A provider outside STREAM_PROVIDERS is refused 409 `no_stream` before any key read. `srv.voice_stream_factory` (test override) still stands in for every provider. premium_stt gate untouched.
- `levain/templates/web/mic.js` probe(): uses `j.stream_provider` when a non-empty string (this name is also what the WAV fallback transcribes with); otherwise the old first-keyed-stt-row rule.

## Tests (revert lines named in each docstring)
- `tests/test_cockpit_voice_stream_routes.py`: replaced the old "first keyed provider has no stream" test with: both-keys case names openai and the key value is in no response byte (red: `stream_provider` body line dropped / last-row pick / openai removed from STREAM_PROVIDERS); openai tts-only + deepgram names deepgram (red: `"stt" in uses` ignored); pure `stream_provider` skips an adapter-less keyed row, None when none qualifies (red: the `in STREAM_PROVIDERS` clause); with STREAM_PROVIDERS narrowed (every dictation-capable provider has an adapter now, so no real provider is adapter-less) an openai open is 409 no_stream and the flag drops (also applied to the older no-stream open test); each provider is built by its own factory with no test factory set, key reaches only that factory and is not in the response (red: the `factories.get(query_provider, ...)` line replaced by a single factory).
- `tests/test_mic_page.py` (browser test, needs a browser): the fake voice.json now lists a keyed openai stt row FIRST but names `stream_provider: deepgram`; the existing open/WAV assertions (`?provider=deepgram`) turn red if the `j.stream_provider` branch in mic.js is removed.

## Known edges
- If STREAM_PROVIDERS gains a name with no entry in the web_server `factories` table, the open is refused (StreamNotStarted -> the registry's refusal), never routed to another provider's adapter; the test pins the two current names only.
- No real OpenAI call was made (unchanged from S5oa's residue).

## Run
653 passed: test_cockpit_voice_stream_routes, test_cockpit_voice_routes, test_voice_{stream_session,openai_stream,deepgram_stream,providers,meter,custody,csp_media}, test_cockpit_grant_voice_row; test_mic_page 30 passed.
