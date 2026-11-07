// levain mic_worklet.js — the AudioWorklet half of mic.js: hands each 128-frame block of the first input channel to the page,
// copied (the engine reuses its buffer). It decides nothing; the resampling and the chunking are in mic.js, one code path
// shared with the ScriptProcessor fallback.
class LevainMicTap extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch && ch.length) { const c = ch.slice(0); this.port.postMessage(c, [c.buffer]); }   // the one copy, then handed over
    return true;
  }
}
registerProcessor("levain-mic-tap", LevainMicTap);
