# RETURN micS5wav: stop-time WAV fallback for the cockpit mic (S5b)

Branch seat/1107-20-micS5wav (on S3). Edited: levain/templates/web/mic.js, tests/test_mic_page.py. No route, no asset registration, no room_view change.

## What it does
- Every PCM chunk is also kept in memory (`s.pcm`), bounded by `LevainMic.limits.wavMax` = 4 MiB - 44 (UPLOAD_MAX less the header); oldest chunks drop past it and the note says so.
- The kept PCM is wrapped as a 16 kHz mono PCM16 WAV and POSTed once to the existing `/cockpit/voice/transcribe?provider=<stream provider>` (audio/wav, write token) when: a chunk fails or times out (mid-session or during stop), the open fails/times out after >= 8000 bytes were captured, or the close cannot confirm the final text. Text arrives through onFinal with one fixed note (wavFilled / wavTrimmed). `closeFailed()` is cleared on a good recovery so Send is not blocked.
- A failed or empty transcribe falls back to the old sentences/closeFailed behaviour. `deadlines.wav` = 30 s. While the WAV is in flight `finish()` is held (`wavBusy`) so a Mic tap or visibility change cannot cancel it; one attempt per session.

## Tests (tests/test_mic_page.py, fake gained a transcribe route; default answers 500 so the older tests keep their meaning)
8 new, each names the line whose revert turns it red. Full mic page + room hook suites: 43 passed.

## Known edges / residue
- An open refused instantly (no audio yet) still ends at once with the "could not start" sentence: the mic is not kept recording in a WAV-only mode. Staying on to record for a stop-time WAV after an open failure would be a behaviour change to the S4 contract; not done.
- Dictation whose stream flag is off (button hidden) has no WAV-only path.
- The transcribe route answers premium_stt_off unless the server runs with premium STT on; then the fallback fails to the old sentence.
- Real iPhone behaviour (upload size over cellular, Safari ArrayBuffer POST) is untested here.
