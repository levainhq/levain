# RETURN micE2E: the whole cockpit mic chain, run (only the provider faked)

Branch seat/1107-20-micE2E on c6d451b7. New file only: tests/test_cockpit_mic_chain_e2e.py (4 tests). No shared code touched.

## Raw run
```
tests/test_cockpit_mic_chain_e2e.py::test_the_happy_path_end_to_end PASSED
tests/test_cockpit_mic_chain_e2e.py::test_the_provider_drops_the_socket_mid_session_and_the_page_falls_back_to_the_recording PASSED
tests/test_cockpit_mic_chain_e2e.py::test_a_key_revoked_at_open_is_refused_before_any_socket PASSED
tests/test_cockpit_mic_chain_e2e.py::test_premium_stt_off_is_404_and_the_button_is_hidden PASSED
============================== 4 passed in 13.79s ==============================
```
Revert checks run for 3 of 4 (all went red as named): Authorization header dropped in deepgram_stream.py:195 (happy: `None == 'Token ...'`); `if not srv.premium_stt` -> `if False` at web_server.py:3959 (off test: open/close answer 413 not 404); `canWav` forced false in mic.js (drop test: no /voice/transcribe ever posted). The revoke test's revert (read_key after the factory) was NOT mutation-run.

## What ran for real
`make_server(premium_stt=True)` + signed custody key in a fake keychain + S3 routes + S1 registry/meter + S2 wsclient + S2b DeepgramStream (built with its injectable base_url/connect_opts at an in-process fake ws server on 127.0.0.1) + the shipped page (mic.js; the REAL mic_worklet.js class, loaded as a same-origin script and driven with 128-frame blocks of a 440 Hz tone at 48 kHz) in headless Chrome.

## Observed (happy path)
open 200 -> chunk seq 0,1,2 all 200 (8000+8000+tail) -> close 200. Provider saw `Authorization: Token <key>`, query encoding=linear16 sample_rate=16000 channels=1, 9500-9600 samples (28800 in at 48 kHz), peak in the 0.5 PCM16 band, ~528 zero crossings (it is the 440 Hz tone at 16 kHz), then `CloseStream`. Draft showed "live words" mid-session, "the final words" after Stop; label back to Mic; track stopped once, ctx closed once, one getUserMedia. Key absent from outerHTML, localStorage, every logged fetch header/answer. Meter: no `reserved` row. Shutdown: registry `_closed`, `_live()==0`, no unsettled, no pending, reaper dead, no voice thread left, fake provider saw EOF.
Failure paths: (1) provider closes 1011 after the first audio -> later chunk POSTs fail -> page POSTs the kept WAV to /voice/transcribe (200), draft = "words from the recording", the wavFilled note shown, provider's close text appears nowhere page-visible, mic released, no reserved row. (2) key revoked (real signed revoke_grant) between tap and read_key -> open refuses (409/403/503), factory never built, no socket, "could not start" sentence, button usable again. (3) premium_stt OFF -> Mic hidden, no getUserMedia, no /stream request from the page, and the three routes answer 404 premium_stt_off when poked directly.

## Defects in the chain
None found in shared code. Observations, not defects: a partial rides the NEXT chunk's answer (one chunk, ~250 ms, late: by design of poll-per-chunk); the page CSP forbids eval, so a rig must load the worklet as a script, not `new Function` (test-side only).

## Residue / UNVERIFIED
- A real iPhone: the permission prompt (Safari tab and Home Screen app), real AudioWorklet cadence, screen lock, a call, route change, real mic levels. getUserMedia, AudioContext and the worklet node are fakes; only the processor class is real.
- A real Deepgram key and endpoint: the real wss TLS/host-allowlist path (the test flag insecure_for_test + allowed_hosts=None bypasses it), the real message shapes (the adapter's own docstring marks CloseStream/Metadata timing UNVERIFIED), real latency and spend/meter rates.
- Provider drop was tested as a clean 1011 close, not a TCP reset or a stall.
- The fallback's provider HTTP is the voice routes' fake transport (a deepgram-shaped body), not Deepgram.
