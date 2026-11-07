# RETURN cfgsign2 (board row 113, F2 built)

No trust-model question stopped this build. One calibration settled from the code: the frame said "T3-class floor" for the
phone approval, but a passkey can only be class `display-phone` (authority.py: "a synced passkey never signs T3"), which
covers T2. The approval floor is therefore T2 (same as a thread envelope and a signed review clear); the T3 authority over
configuration stays the laptop key's, unchanged. The phone is an added gate, so this weakens nothing.

Built: `levain/dispatch/phone_config.py`; `config request` + `config accept-approved` in `levain/dispatch/cli.py`; namespace
`levain-dispatch-config-approve` (registry, golden, fixed message grammar in `LEVAIN_SIGN_REQUESTS`);
`tests/test_dispatch_config_phone.py` (21 tests, mutation-checked).

Flow: `config request <kind> <body>` -> item bytes kept in `<dispatch-dir>/config-pending/<sha>.item`, request filed (1 h) and
pushed -> phone passkey signs -> `config accept-approved <rid> --signing-key <laptop key>` verifies the approval (levain's own
namespace + grammar, signed before expiry, record over the message, active passkey from the SAME authority log the store
verifies against), re-checks fields/head, shows the full diff, laptop key signs (no y/N), unchanged `ControlStore.accept`.

Known edges: the phone reads levain's one-line change/summary + the item hash, NOT the full diff (the diff prints at the
laptop before the touch). The T2 class check is unreachable by a passkey today (belt only; untestable, survived mutation).

Residue only a real phone + Touch ID can discharge:
1. A real passkey assertion from his iPhone on a real `config request` card (push arrival, the sheet rendering the 7-line
   message, one tap) -- tests use a software P-256 passkey and a recorded push.
2. The laptop key signing WITHOUT a terminal question: `ssh_signer` on his real Secure Enclave key must raise the Touch ID
   sheet from a non-tty run of `accept-approved`; tests use a file key. UNVERIFIED whether the sheet needs a tty.
3. Real queue discovery: `--sign-queue-dir`/`LEVAIN_SIGN_QUEUE_DIR` against his live cockpit queue and origin.
4. The live store untouched: no real dispatch store, key or request was used or filed.
