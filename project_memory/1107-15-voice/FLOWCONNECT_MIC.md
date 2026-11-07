# flowConnect mic button: what it uses, and can the cockpit have it

Researched 2026-11-07, read-only. Nothing in flowConnect was edited.

## What it uses (code)
- Not keyboard dictation. `flowConnect/Services/VoiceController.swift` runs the iOS 26 **SpeechAnalyzer + SpeechTranscriber** pipeline: `SpeechTranscriber(` with `locale: Locale.current`, `reportingOptions: [.volatileResults]`, then `SpeechAnalyzer(modules: [transcriber])`, `SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith:`, fed by `AsyncStream<AnalyzerInput>`. Partial text arrives via `for try await result in transcriber.results`. A comment in the file calls it "Free: SpeechAnalyzer live transcription".
- Audio capture: one `AVAudioEngine` input tap (`AudioManager`), converted to the analyzer format; permission via `AVAudioApplication.requestRecordPermission`.
- Permission for speech: `SFSpeechRecognizer.requestAuthorization` is called ONLY for the authorization prompt; `SFSpeechRecognizer` itself never recognizes anything (confirmed by grep: no recognizer or `requiresOnDeviceRecognition` use).
- The setting: `isPremiumSTT` reads UserDefaults `"premiumSTT"`. OFF = the SpeechAnalyzer path above. ON = records a wav and POSTs it to `\(serverURL)/stt` (`sendToWhisper`, `x-openai-key`).
- Info.plist / project.yml: `NSMicrophoneUsageDescription`, `NSSpeechRecognitionUsageDescription`. `IPHONEOS_DEPLOYMENT_TARGET = 26.0` in `flowConnect.xcodeproj/project.pbxproj`, so the iOS 26 API is unconditional, no fallback needed.
- Gap (grep: no `AssetInventory`, `installedLocales`, `assetInstallationRequest` anywhere in *.swift): the app never checks or downloads the on-device language model. See risk below.

## (a) Will it keep working in flowConnect? YES, with one hardening item
- `SpeechAnalyzer`, `SpeechTranscriber` and `AssetInventory` are all "introduced iOS 26.0" and not marked deprecated in Apple's symbol data. `SFSpeechRecognizer` (iOS 10+) is also not marked deprecated; Apple positions the new API as the modern path, the old one stays. The app already targets 26, so nothing breaks on 26/27. (I read the DocC JSON; "not deprecated" is absence of a flag, not an Apple statement of future intent. UNVERIFIED for iOS 27 release notes.)
- On-device: the SpeechTranscriber model runs locally once the locale model is installed. Models are per locale and downloaded through `AssetInventory`; Apple's sample flow is `AssetInventory.assetInstallationRequest(supporting:)` then `downloadAndInstall()`. Per-locale coverage list: UNVERIFIED (check `SpeechTranscriber.supportedLocales` on device).
- Risk: the code uses `Locale.current` and never installs the model. It works for Phill today (model likely already present from system dictation or earlier use), but a fresh device, a changed region, or a locale without a model could fail or stall at cold start (the code has a 10 s cold-start watchdog). Cheap hardening: before `analyzer.start`, check `SpeechTranscriber.installedLocales`, and run `assetInstallationRequest`/`downloadAndInstall` if missing, with a visible state. Not a current breakage, a latent one. UNVERIFIED that this ever fired for him.
- Permissions/entitlements: only the two usage strings above; no special entitlement.

## (b) Can the cockpit adopt it or an equivalent? Not the same thing; three options
1. **Web Speech API (`webkitSpeechRecognition`)**: exists in iOS Safari since 14.5, "powered by the same speech engine as Siri", requires Siri enabled (WebKit blog). Per Firtman it is Apple's cloud service, so audio goes to Apple (the exact data handling: UNVERIFIED). MDN has `processLocally`/`SpeechRecognition.install()` but I could not confirm any Safari support for them; assume NOT on-device. **Standalone PWA (Home Screen): does not work.** WebKit bug 225298: "not available in SafariViewController and web apps added to Home Screen for now" (2021); bug closed RESOLVED LATER, last touched 2025-03-01, no fix. Apple forums thread 748048 reports the same. So for the installed iPhone cockpit: dead. In plain Safari tab and desktop Chrome it works, but Chrome also sends audio to Google servers (MDN note). Not recommended as the primary path.
2. **getUserMedia + MediaRecorder -> server STT (what the cockpit already has: Deepgram/OpenAI premium)**: works in standalone PWA foreground (MediaRecorder audio/mp4, Safari 14.1+; per the existing 1030 VOICE.md capability table). Audio leaves the device by design. This is the working path today; the cost is latency (record-then-send) and a mic prompt that the open WebKit bug 327237 says may re-ask on every launch (UNVERIFIED by me, copied from the 1030 research).
3. **In-browser on-device (Whisper WASM / WebGPU, e.g. Transformers.js)**: possible. whisper-tiny is roughly 15 to 40 MB depending on quantization (blog figures, conflicting), downloaded once and cached. WebGPU in iOS Safari is reported available since Safari 26 (a vendor blog; Transformers.js docs still say behind a flag, so CONFLICTING). CPU/WASM fallback is slower. No measured iPhone latency found: UNVERIFIED. Honest cost: a model download, cache eviction risk in a PWA, battery and heat, accuracy below Apple's SpeechTranscriber and Deepgram, and a non-streaming feel unless chunked. A build-and-measure experiment, not a drop-in.

Verdict (b): the flowConnect mic button has NO web equivalent. Nothing native (SpeechAnalyzer) is reachable from a PWA. The closest practical match is option 2 with streaming; option 1 fails in the installed PWA.

## Recommendation
- flowConnect: leave the mic alone; add the `AssetInventory` check as a small hardening slice.
- Cockpit: use getUserMedia + the existing server STT. If Phill wants "free and instant", prototype streaming chunks to Deepgram (partials while speaking) before spending on Whisper WASM. Treat `webkitSpeechRecognition` only as a desktop-Chrome/Safari-tab bonus with feature detection, never the installed-PWA path. Test the PWA on his real iPhone (mic permission persistence, backgrounding).

## Sources
- https://developer.apple.com/documentation/speech/speechtranscriber (symbol data: iOS 26.0, via /tutorials/data/documentation/speech/speechtranscriber.json)
- https://developer.apple.com/documentation/speech/speechanalyzer , https://developer.apple.com/documentation/speech/assetinventory (iOS 26.0)
- https://developer.apple.com/documentation/speech/sfspeechrecognizer (iOS 10.0, no deprecation flag)
- https://developer.apple.com/documentation/speech/bringing-advanced-speech-to-text-capabilities-to-your-app (page body would not render; AssetInventory flow from general knowledge, UNVERIFIED against the page)
- https://webkit.org/blog/11648/new-webkit-features-in-safari-14-1/
- https://bugs.webkit.org/show_bug.cgi?id=225298
- https://developer.apple.com/forums/thread/748048
- https://firt.dev/ios-14.5/
- https://developer.mozilla.org/en-US/docs/Web/API/SpeechRecognition (compat table did not render; Safari/processLocally support UNVERIFIED)
- https://huggingface.co/docs/transformers.js/en/guides/webgpu
- https://blog.logrocket.com/how-to-build-a-real-time-voice-ai-agent-in-the-browser
- Local: /Users/phillipclapham/Briefcase/flowConnect/project_memory/1030-46-long-work/VOICE.md (capability table, bugs 327237 and others)
