# RETURN cfgsign (lane 1107-20)
Built: nothing but the frame. STOPPED per brief: a design question only Phill can settle.
Finding: the consent sheet signs only with phone passkeys; `ControlStore._root_problem` refuses a passkey trust root by design and the pin is a
laptop key with no rotation. A phone-sheet signature over a config item cannot pass `ControlStore.accept`. See CFGSIGN_FRAME.md for F1/F2/F3.
Recommend F2 (phone passkey approves a fixed-grammar request in a new levain-own namespace; laptop Touch ID key signs; existing accept writes).
Tests: none written/run (no code). Residue: a real phone sign and the real request are his. UNVERIFIED: that F2's laptop touch can be non-terminal
(depends on ssh_signer/Secure Enclave key path; not exercised). Desk command to file the real request: none yet (verb not built).
