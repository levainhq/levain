# Embedding the cockpit engine

`levain.cockpit.Cockpit` is the read half of the shared cockpit: providers register panels, and the
engine turns their results into the manifest and panel payloads. This page states the two
guarantees an embedder (a server, a TUI, a test harness) builds on. The engine's module docstring
(`levain/cockpit/engine.py`) carries the same contract beside the code.

## Providers are trusted code

Providers are operator-registered, in-process code, trusted not to be adversarial. Every provider
call runs in a worker thread under its panel's timeout, so a provider that hangs or raises costs
that panel an error answer, never the caller. Provider output objects are not deep-copied: a
provider that returns a hostile object (a container whose methods block, a hostile `__hash__`) is
outside the model. Anything that lets an untrusted party register a provider (a team server, a
plugin) has to close that first.

## Interrupts: safety always, liveness never

Python delivers `KeyboardInterrupt` only to the main thread, and between any two bytecodes, so no
library code can make a critical section fully interrupt-safe.

- **Safety, under any interrupt:** two provider reads of the same panel never run at once. A read's
  worker invokes the provider only after the engine has started it cleanly; an interrupt before
  that cancels the read without calling the provider.
- **Liveness after an interrupt is not promised.** Once a `KeyboardInterrupt` has escaped a read,
  that `Cockpit` object may keep answering errors for some panels. **Rebuild the Cockpit** (call
  `stop()` and construct a new one) instead of reusing it.

Levain's own hosts already behave this way: `levain serve` reads on request and refresher threads,
which never receive the signal, and its main thread closes the server on Ctrl-C; `levain tui
--manifest` stops the Cockpit and exits. [judged by the K1 cockpit seat, 2026-10-10, against
`levain/web_server.py` and the R1 branch's `levain/tui.py`.]

A host that needs a Cockpit to stay usable across Ctrl-C has to keep the interrupt out of the
engine's reads: install its own SIGINT handler that records the signal and acts on it between
reads, which is the approach Trio takes for its own internals. Background:

- Python, "Signals and threads": https://docs.python.org/3/library/signal.html#signals-and-threads
- Nathaniel J. Smith, "Control-C handling in Python and Trio": https://vorpus.org/blog/control-c-handling-in-python-and-trio/
