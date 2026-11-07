# CFGSIGN frame (1107-20 cfgsign lane): a consent-sheet sign request for a dispatch config item

STATUS: STOPPED AFTER THE FRAME. One trust-model question only Phill can settle (below). Nothing was coded.

## What exists (read from source at origin/main)

- **Config item** (`levain/dispatch/control.py`): canonical JSON `{format, store_id, generation, prev, issued_at, kind, body}`;
  the operator signs those exact bytes (`item_bytes`) as an SSHSIG in namespace `levain-dispatch-config`. `ControlStore.accept(item, sig)`
  re-verifies against the PINNED trust root (`meta.trust_root`, a fingerprint set once at `init`, no rotation), checks store id, generation
  = top+1 and `prev` = head digest, judges the body (`registry.body_refusal`), writes in one transaction. Stale generation, replay, wrong
  store are already refused by `_judge`.
- **The root rule** (`_root_problem`): the pinned key must be enrolled, active, class covering T3, custody a stated presence custody.
  **`PASSKEY` custody is refused by name: "the trust root is a passkey; configuration is signed on the laptop."** Pin is set once.
- **The consent sheet** (`levain/consent/sign.py`, `levain/band_sources.py`): signs ONLY with a phone WebAuthn passkey (user-verified;
  assertion -> SSHSIG via `consent.passkey.to_sshsig`). Generic requests cannot use a namespace levain verifies; levain's own requests
  go through `file_levain_request` in a namespace listed in `contracts.namespaces.LEVAIN_SIGN_REQUESTS` with a fixed message grammar
  (precedents: review-clear, terminal-answer countersign).
- **Budget/envelope path** (`coordinator/budget.py file_sign`, `phone.py`): the SIGN record is a `replies` row; the envelope is signed by a
  PASSKEY (T4 floor) and `EnvelopeStore.accept_envelope` accepts passkey signers. Envelopes accept passkeys; config does not.
- **CLI** (`dispatch/cli.py`): `config init|sign|accept|show`. `sign` signs + accepts behind a terminal y/N with a LAPTOP key; `accept`
  takes an item signed elsewhere (also terminal-gated, agent-refused).

## The conflict

The consent sheet can only produce a PASSKEY signature. The config store's trust root can by design never be a passkey, and the pin is the
operator's existing laptop key (no rotation). So a phone-sheet signature over a config item **cannot verify through the existing accept path**
without changing one of: (a) the root rule/pin, or (b) who actually signs.

## Frames (each needs Phill's choice; none is a code-only decision)

- **F1 passkey becomes an allowed config root.** Relax `_root_problem` for PASSKEY (T3-covering passkey), add a way to pin a second/new root
  (no rotation exists: needs the authority-anchored rotation the module doc already names as planned, or a new store = loses chain).
  Cost: a synced passkey signs the dispatch provider policy; control.py explicitly excludes this today. Biggest change, widest blast radius.
- **F2 phone approves, laptop key signs (countersign pattern, like terminal answers).** Request is filed in a new levain-own namespace
  (e.g. `levain-dispatch-config-approve`, message = fixed grammar: kind, generation, prev, body hash + plain-words summary). Phone passkey signs
  the APPROVAL. A new verb (`config accept-approved`) checks the approval signature (passkey, T3-class floor, hash matches the exact item
  bytes, generation still next) and then the LAPTOP key signs the item with a Touch ID touch (no y/N typing, no terminal prompt).
  Trust root and `control.py` unchanged; phone is a gate, laptop key still the root. Residue: the laptop touch still happens at the laptop.
- **F3 as F2 but the approval itself is the authority for an unattended laptop sign** (key held with presence off). Weakens the "presence"
  custody premise of the root; not recommended.

Recommendation: **F2**. It keeps `control.py`'s root rule intact and uses the existing LEVAIN_SIGN_REQUESTS precedent. But "his passkey/Touch ID
on the sheet" (np-1d481284 a) reads as the phone signature BEING the config signature, which is F1. That is his model call.

## Question for Phill (one)

np-1d481284 (a) says the provider policy is signed "on the consent sheet". The sheet signs with a phone passkey; config roots refuse passkeys by
design. Choose: **F1** (allow a passkey config root; needs a root-rotation/new-pin step) or **F2** (phone passkey approves, the laptop Touch ID
key signs and the existing verified accept path writes it). 

## What was NOT done
No code, no request filed, nothing signed or accepted, no real key or live app touched. Body validated earlier by the prior lane (hash c2626d2b...).
