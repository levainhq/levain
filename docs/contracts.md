# levain shared contracts: `levain-contracts/1`

The wire and cryptographic substrate every levain log speaks: Merkle math, signed notes, checkpoints,
cosignatures, witness submissions, origins, key fingerprints, canonical JSON leaf framing, the two frozen leaf types
and SSHSIG for device lines. Code: `levain/contracts/` (stdlib and `cryptography>=48` only; nothing there imports
`levain.team` or any other levain module). Vectors: `tests/golden/contracts/frozen/`. Design: levain
`project_memory/1012-s1/S1_DESIGN.md` (rev 10).

**Status: built, NOT yet frozen.** The freeze tag `levain-contracts/1` is cut only after a seat other than the
builder reads this document against the design's §1 (T36). Until that tag exists, nothing below is a promise.

"Frozen" means: the bytes are pinned by `tests/golden/contracts/frozen/`, a change to them fails CI, and a change needs
a new version tag, never an edit. Every expected byte string in a vector is hand-built from the spec text or by a few
lines of independent arithmetic in the generator beside it; a generator imports only the standard library and
`cryptography`, never `levain` (`tests/test_contracts_generators.py` checks every `.py` under
`tests/golden/contracts/` on its AST).

## How a vector is classified (§1.0)

Every vector records `expect.levain` (levain's verdict), `expect.upstream` (`{implementation, version, verdict}` of a
named upstream with no levain policy around it, or `n/a`) and `authority` (`spec` with the quoted clause,
`published_table`, `byte_derivation` with the rule, or `profile_rule` with its basis: a quoted `spec_permission` or a
`local_bound` rationale). `authority` decides `expect.levain`; `expect.upstream` is an observation. Its
classification `kind` is one of:

- `spec`: levain and the upstream agree with the spec.
- `levain_profile`: the spec permits what levain refuses; the upstream accepts. The vector names the spec line.
- `upstream_lax`: the spec says the input MUST be refused; the upstream accepts it.
- `no_upstream`: no upstream implementation exists (F6, F8, F9, F10, and levain-only checks elsewhere).

There is no hand-written list of differences: the list at the end of this document is generated from the vector
files (`python -m tests._contracts_vectors`) and `tests/test_contracts_docs.py` fails when it drifts.

Cross-check: `tests/golden/contracts/xcheck/` (Go; CI job `contracts-xcheck`) runs every vector whose upstream is a Go
package through that package (`golang.org/x/mod v0.42.0` `sumdb/note`, `github.com/transparency-dev/formats v0.1.1`,
`github.com/transparency-dev/merkle v0.0.2`, `crypto/ed25519` and `strconv`/`encoding/base64` of `go1.27.2`;
F5's oracle is `github.com/transparency-dev/witness` `NewAddCheckpointHandler` in its own module), fails on an
unrecognised directory or one below its minimum count, and emits Go-generated vectors that Python verifies. For F10 it
also runs the F1-F3 layers inside every anchor levain accepts through upstream Go. `ssh-keygen` observations (F7,
F12) are checked by the Python tests, under the OpenSSH version the vectors recorded (`OpenSSH_10.3p1`).

## The frozen items

### §1.1 Grammar shared by F1-F5
- ASCII decimal `[0-9]` only, no leading zero except `0` (tlog-checkpoint: "no extra leading zeros"); base64 standard
  alphabet with padding, decoded canonically (re-encode and compare: "decoders MUST reject non-canonical encodings");
  integers `type(x) is int`, `0 ≤ x ≤ 2^63-1` in note text, `0 ≤ x ≤ 2^53-1` inside a JSON leaf (F8).
- Code: `levain/contracts/grammar.py`. Vectors: `frozen/grammar/`.

### F1. Merkle tree hashing and proofs
- Spec: RFC 6962 §2.1 (https://www.rfc-editor.org/rfc/rfc6962) and RFC 9162 §2.1
  (https://www.rfc-editor.org/rfc/rfc9162), SHA-256; the `m == 0` and `m == n` consistency cases from C2SP
  `tlog-witness@v1.1.0` (https://github.com/C2SP/C2SP/blob/tlog-witness/v1.1.0/tlog-witness.md).
- Code: `levain/contracts/merkle.py`. Vectors: `frozen/merkle/` (roots for sizes 0-8, 2^k-1, 2^k, 2^k+1 for k = 4,
  8, 10, and 1000 over `d_i = bytes([i % 256]) * (i+1)`; inclusion and consistency proofs; the RFC 6962 reference
  vectors from `transparency-dev/merkle` `testonly` at v0.0.2; one tampered variant per refusal).

### F2. Signed notes, and the Ed25519 profile
- Spec: C2SP `signed-note@v1.1.0` (https://github.com/C2SP/C2SP/blob/signed-note/v1.1.0/signed-note.md), Ed25519
  type `0x01` only. levain accepts at most 16 signature lines of at most 8 KiB each (UTF-8 bytes, newline excluded);
  a known key whose signature fails rejects the whole note (the spec's SHOULD, taken).
- Code: `levain/contracts/note.py`, `levain/contracts/ed25519.py`. Vectors: `frozen/note/`, `frozen/ed25519/`.
- **The pinned Ed25519 row.** levain's Ed25519 verdict is exactly the 12 verdicts the published speccheck table gives
  Go `crypto/ed25519`, OpenSSL 3.0, BoringSSL and PyCA, for speccheck vectors 0..11:

  | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 |
  |---|---|---|---|---|---|---|---|---|---|----|----|
  | V | V | V | V | X | X | X | X | X | X | X  | V  |

  Source: https://eprint.iacr.org/2020/1244 and https://github.com/novifinancial/ed25519-speccheck at commit
  `65519336fda78a3d016e947df6d82848aca0c9da` (`cases.json` sha256
  `08e47a36d9aead288664930505584f353fff113ab854f2800db1e4f5b3540450`). The row is the whole normative profile; no
  pre-check and no ZIP-215 sit in front of the library. `tests/test_contracts_ed25519.py` asserts levain reproduces
  it (CI goes red if a `cryptography` wheel changes it); xcheck runs Go's `ed25519.Verify` on the same 12 cases. Known
  edge: a `cryptography` built against LibreSSL or another backend is untested; the row test is the detector. Every
  Ed25519 verdict in levain (F2, F4, F12) goes through `ed25519.verify`.

### F3. Checkpoints
- Spec: C2SP `tlog-checkpoint@v1.1.0` (https://github.com/C2SP/C2SP/blob/tlog-checkpoint/v1.1.0/tlog-checkpoint.md).
  levain profile: zero extension lines; the log's key name equals the origin; the origin carries a frozen levain
  prefix (F6), and a team verifier requires `levain-team/<32 lowercase hex>` exactly. The spec MUST "no two signature
  lines with both the same key name and the same key id" is enforced.
- Code: `levain/contracts/checkpoint.py`. Vectors: `frozen/checkpoint/`.

### F4. Cosignatures
- Spec: C2SP `tlog-cosignature@v1.1.0` (https://github.com/C2SP/C2SP/blob/tlog-cosignature/v1.1.0/tlog-cosignature.md),
  Ed25519 `cosignature/v1` (type `0x04`). Frozen: verification to a record, `verify_cosignatures(checkpoint, trusted,
  now) -> [VerifiedCosignature(cosigner_name, key_id, timestamp)]`; a timestamp above `now + 600` is refused; `now` is
  the caller's. Counting records into a quorum is not frozen (P4).
- Code: `levain/contracts/checkpoint.py`. Vectors: `frozen/cosig/`.

### F5. Witness submission body
- Spec: C2SP `tlog-witness@v1.1.0` `add-checkpoint`
  (https://github.com/C2SP/C2SP/blob/tlog-witness/v1.1.0/tlog-witness.md). levain only produces it.
- Code: `checkpoint.add_checkpoint_body`. Vectors: `frozen/witness/` (oracle: the witness module's own handler).

### F6. Origins
- Grammar: ASCII `[a-z0-9][a-z0-9._-]*` segments joined by `/`, at most 255 bytes. Frozen prefixes `levain-team/`,
  `levain-fleet/`, `levain-receipt/`. Frozen production: `levain-team/<32 lowercase hex>`. Not frozen: the fleet (P13)
  and receipt (P12) productions. `fp_hex(blob)` = lowercase hex SHA-256 of an SSH public-key wire blob.
- Code: `levain/contracts/origin.py`. Vectors: `frozen/origin/`.

### F7. Key fingerprint
- `fp_of_blob(blob) = "SHA256:" + base64(SHA-256(blob))` without padding, for a plain OpenSSH public key of any type;
  equal to `ssh-keygen -lf`. Certificates are outside F7; the blob's embedded type must equal the declared one.
- Code: `levain/contracts/keys.py`; `levain/team/signing.py` `fingerprint()` keeps its allowlist and calls it.
  Known second computation: `levain/cockpit/keys.py` still fingerprints through `ssh-keygen` until it adopts
  `fp_of_blob` (another lane's file).
- Vectors: `frozen/fingerprint/` (one throwaway key per type, expected values from `ssh-keygen -lf` under
  `OpenSSH_10.3p1`; the `.pub` files sit beside them).

### F8. Canonical JSON, leaf framing and the leaf registry
- Canonical JSON is the byte form of `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`,
  UTF-8: keys by Unicode code point; `"` and `\` escaped; `\b \f \n \r \t` short forms; other U+0000-U+001F as
  lowercase `\u00xx`; everything else raw (a Go producer must disable HTML escaping). Parse refuses duplicate keys,
  lone surrogates, floats, NaN/Infinity, integers outside ±(2^53-1), nesting deeper than 32 (measured iteratively),
  a BOM and non-UTF-8. A log leaf's data `d` is accepted only when `canonical(parse(d)) == d` byte for byte.
- `chain_hash(prev, entry)` = lowercase hex SHA-256 of UTF-8(prev || canonical(entry without "hash")).
- **The registry** (`levain/contracts/leaves/__init__.py`): one global map `(leaf, v) -> (log kinds, field
  validator)`. Registered: `("line", 1)` on the team log (F9) and `("receipt_checkpoint", 1)` on the team log (F10).
  An unknown `(leaf, v)` is refused at ingest and reported UNVERIFIABLE at verify ("needs a newer levain"), never
  tampering. A rule change inside a type is a new `v`. Provisional shapes are never registered.
- Code: `levain/contracts/canon.py`. Vectors: `frozen/canon/` (the framing goldens run against an injected test
  registry).

### F9. The `line` leaf, the device message, the sidecar receipt and the team-log scope matrix
- Leaf `{"leaf":"line","v":1,"file":<str>,"entry":<the SEALED entry>,"device_fp":<F7>,"device_sig":<canonical base64
  of the raw SSHSIG blob>}`, exactly those keys, no `n`. `entry.hash` must equal `chain_hash(entry.prev, entry)`.
- **The frozen entry schema.** Every leaf's `entry` passes `validate_entry_v1` (`levain/contracts/leaves/entry_v1.py`),
  a copy of `levain/team/entry.py` `validate` at levain `31dea1f` whose future-`ts` branch reads `now` from its
  caller: the ingest time at ingest, `None` at verify (known edge: a far-future `ts` admitted by the trusted operator).
  Pinned: its sha256; its verdicts (accept, or refuse plus the refusing `raise` site and message) against the
  verdicts recorded in `frozen/entry/` at the freeze, which 31dea1f's own `validate` produced; 100% branch coverage
  of the copy by those vectors (`tests/_branch_coverage.py`, measured on CPython 3.12 and 3.13 in CI). A drift check
  against live `team.entry` prints "T schema changed: a new `(line, v)` is needed" and never fails. A later T schema
  change is a new `(line, v)`.
- **Device message**: `string(origin ASCII) || string(file UTF-8) || string(32 raw bytes of the chain hash)`, SSH
  strings (uint32 big-endian byte length); `file` is the exact JSON string, never normalised; the chain hash is 64
  lowercase hex. `device_sig` is verified in-process (F12) in namespace `levain-team-entry`.
- Ingest and verify refuse a repeated `(file, entry.hash)` (an operator fault at verify) and an `entry.id` already
  held by an ACCEPTED leaf (the first accepted wins; a refused leaf holds no id).
- **Scope matrix** `may_write_team_log(scope, entry, snapshot)`, the snapshot being the team log's verified prefix:
  `human` writes finding, question, tension, ack, retire; `member` writes finding, question, tension, with no
  non-empty `supersedes`; `receipt` writes no line. For every scope until P2: `decision`, `constraint` and any
  `kind: "ruling"` are refused. `supersedes` is default-deny: every target present in the snapshot exactly once and
  not a ruling. An unknown scope is refused at ingest and UNVERIFIABLE at verify. Known edge (V15): the matrix does
  not take `file`, so a writer can sign one entry under two file names with two signatures and a chaining `prev`.
- **Sidecar receipt** (TS §1.4) is a hint: `{v, type:"device", file, n, hash, handle, device, scope, device_sig,
  leaf, team_version, accepted}`; a client derives every field but `accepted` and refuses a receipt that disagrees.
  `device` is `fp_hex(key blob)[:16]`; `n`, `leaf` and `team_version` are integers.
- Code: `levain/contracts/leaves/team.py`, `leaves/entry_v1.py`. Vectors: `frozen/entry/`, `frozen/line/` (the
  hand-built message golden: origin `levain-team/` + 32 × `a`, file `ledger/dana/findings.jsonl`, chain hash
  `00…01`, lengths `0x0000002c`, `0x0000001a`, `0x00000020`; NFC vs NFD; a multibyte file; uppercase refused),
  `frozen/scope/` (every scope × entry type cell, the supersedes, ruling, uniqueness and idempotency cases).

### F10. `receipt_checkpoint` leaf
- `{"leaf":"receipt_checkpoint","v":1,"checkpoint":<signed note text>,"old_size":<int>,"consistency":[<base64 32-byte
  hashes>]}`, team log. The note verifies under the key the directory binds to the writing device (P1/P12, passed by
  the caller); `old_size` equals the same origin's previous accepted anchor's size (0 first); the proof verifies old
  -> new (F1); a fork (equal size, different root) and a smaller size are refused; a size above 2^53-1 is refused.
- Code: `levain/contracts/leaves/receipt.py`. Vectors: `frozen/receipt_checkpoint/`, signed under the PROVISIONAL
  receipt origins in `tests/golden/contracts/provisional/receipt_origin/` (P12; the checks do not depend on their
  form).

### F11. SSHSIG namespace registry
- Exact strings, never prefixes. Shipped: `git`, `levain-authority`, `levain-authority-reset`, `levain-confirm`,
  `levain-cockpit`. Reserved: `levain-team-entry` (F9), `levain-manifest`, `levain-consent`; the family
  `levain-team-<kind>` for P1's envelope kinds, where no kind may be `entry`.
- Code: `levain/contracts/namespaces.py`. Golden: `frozen/namespaces.json`. `tests/test_contracts_namespaces.py`
  walks the package's AST: every constant after `"-n"` in an `ssh-keygen -Y` argument list, every `namespace=`
  string, every module-level `*NAMESPACE*` string must be registered. Known edge: a namespace computed at run time
  escapes the test.

### F12. SSHSIG verification for `device_sig`, in-process
- Spec: OpenSSH `PROTOCOL.sshsig` (https://github.com/openssh/openssh-portable/blob/master/PROTOCOL.sshsig,
  `$OpenBSD: PROTOCOL.sshsig,v 1.5 2026/10/10$`). Raw blob, no armor; parsed to its last byte at the outer, key and
  signature levels. levain profile: namespace `levain-team-entry`, empty reserved, hash `sha512`, key `ssh-ed25519`
  whose F7 fingerprint equals `device_fp`, a 64-byte signature; `sha256`, a non-empty reserved field and other key
  types are `levain_profile` refusals. The signed data (§3) is verified through F2's `ed25519.verify`, never through
  `ssh-keygen` (its Ed25519 verdict moves between OpenSSH versions). P-256 device lines are P11, a new `(line, v)`.
- Code: `levain/contracts/sshsig.py`. Vectors: `frozen/sshsig/` (with `ecdsa-p256.sig`, made once by
  `ssh-keygen -Y sign` with a throwaway key). Test-time cross-check: `ssh-keygen -Y check-novalidate` accepts every
  armored valid control, and every `ssh-keygen -Y sign` blob verifies in levain. Measured: `OpenSSH_10.3p1` accepts
  the S+ℓ mutation that the pinned row refuses (`upstream_lax`).

### F13. Log signing keys are Ed25519
- Every note a levain log signs is signed by an Ed25519 key; an ssh-agent's response `string("ssh-ed25519") ||
  string(sig)` is unwrapped to the 64 bytes (`keys.unwrap_agent_signature`). `ecdsa-*` keys never sign C2SP notes.
  A note text containing an SSHSIG preamble is refused by F2's character rule.
- Vectors: within `frozen/note/`.

## Not frozen (provisional)

P1 the §1.4 signed envelope and every leaf that carries it · P2 `decision` / `constraint` lines and rulings · P3 taint
labels · P4 witness policy and the quorum evaluator · P5 the fleet bus message · P6 Shadow Receipt payloads · P7
operator-access leaves · P8 inclusion promises · P9 ML-DSA-44 cosignatures · P10 `tlog-proof` bundles · P11 the
Secure Enclave SSHSIG vector and P-256 device lines · P12 receipt identity · P13 fleet identity and the up-ledger. They
live only as fixtures under `tests/golden/contracts/provisional/<item>/` with a `STATUS` file; nothing in the package
builds or accepts them.

## Vendoring the vectors (anneal-memory)

The vectors are plain JSON and text with no levain import, so anneal-memory's member-mode import vendors them rather
than depending on levain: copy `tests/golden/contracts/frozen/` (at minimum `canon/`, `entry/`, `line/`, `scope/`)
from the commit the `levain-contracts/1` tag names, keep the files byte for byte, and run anneal's own canonical form,
`chain_hash` and entry-schema checks against them. The tag, not a branch, is the reference: a vector changes only under
a new tag. anneal reads `expect.levain` as the verdict and ignores `expect.upstream` (an observation).

## Vectors by kind

<!-- vectors-by-kind: generated by tests/_contracts_vectors.py render_by_kind(); tests/test_contracts_docs.py pins it -->

#### `spec`: 197 vectors

- `checkpoint` (13): `team-size-0`, `team-size-1`, `team-size-1048577`, `size-max-note-int`, `size-5000-digits-refused`, `size-negative-refused`, `size-plus-refused`, `size-space-refused`, `size-underscore-refused`, `size-fullwidth-refused`, `fleet-origin-at-prefix-verifier`, `same-name-different-id-accepted`, `different-name-same-id-accepted`
- `cosig` (10): `spec-example-input`, `three-cosignatures`, `timestamp-now-plus-600-accepted`, `trusted-cosigner-bad-signature-refused`, `unknown-cosigner-ignored`, `advance-consistent-accepted`, `advance-same-size-same-root-accepted`, `advance-same-size-different-root-refused`, `advance-smaller-refused`, `advance-inconsistent-refused`
- `ed25519` (12): `speccheck-0`, `speccheck-1`, `speccheck-2`, `speccheck-3`, `speccheck-4`, `speccheck-5`, `speccheck-6`, `speccheck-7`, `speccheck-8`, `speccheck-9`, `speccheck-10`, `speccheck-11`
- `fingerprint` (8): `fingerprint-ssh-ed25519`, `fingerprint-ecdsa-sha2-nistp256`, `fingerprint-ecdsa-sha2-nistp384`, `fingerprint-ecdsa-sha2-nistp521`, `fingerprint-sk-ssh-ed25519`, `fingerprint-sk-ecdsa-sha2-nistp256`, `declared-type-differs-from-blob-refused`, `non-canonical-base64-pad-bits-refused`
- `grammar` (19): `decimal-zero`, `decimal-one`, `decimal-max-note`, `decimal-plus-sign`, `decimal-leading-space`, `decimal-underscore`, `decimal-fullwidth-digits`, `decimal-negative`, `decimal-empty`, `decimal-trailing-newline`, `decimal-above-uint64`, `decimal-5000-digits`, `b64-one-byte`, `b64-three-bytes`, `b64-empty`, `b64-unpadded`, `b64-url-alphabet`, `b64-pad-in-middle`, `b64-space`
- `merkle` (108): `root-0`, `root-1`, `root-2`, `root-3`, `root-4`, `root-5`, `root-6`, `root-7`, `root-8`, `root-15`, `root-16`, `root-17`, `root-255`, `root-256`, `root-257`, `root-1023`, `root-1024`, `root-1025`, `root-1000`, `rfc6962-reference-root-0`, `rfc6962-reference-root-1`, `rfc6962-reference-root-2`, `rfc6962-reference-root-3`, `rfc6962-reference-root-4`, `rfc6962-reference-root-5`, `rfc6962-reference-root-6`, `rfc6962-reference-root-7`, `rfc6962-reference-root-8`, `inclusion-7-0`, `inclusion-7-1`, `inclusion-7-2`, `inclusion-7-3`, `inclusion-7-4`, `inclusion-7-5`, `inclusion-7-6`, `inclusion-8-0`, `inclusion-8-1`, `inclusion-8-2`, `inclusion-8-3`, `inclusion-8-4`, `inclusion-8-5`, `inclusion-8-6`, `inclusion-8-7`, `inclusion-1000-0`, `inclusion-1000-511`, `inclusion-1000-512`, `inclusion-1000-999`, `inclusion-refuse-index-eq-size`, `inclusion-refuse-index-above-size`, `inclusion-refuse-proof-short`, `inclusion-refuse-proof-long`, `inclusion-refuse-tampered-proof`, `inclusion-refuse-wrong-leaf`, `inclusion-refuse-leaf-hash-31-bytes`, `inclusion-refuse-proof-hash-31-bytes`, `consistency-1-1`, `consistency-1-2`, `consistency-2-2`, `consistency-1-3`, `consistency-2-3`, `consistency-3-3`, `consistency-1-4`, `consistency-2-4`, `consistency-3-4`, `consistency-4-4`, `consistency-1-5`, `consistency-2-5`, `consistency-3-5`, `consistency-4-5`, `consistency-5-5`, `consistency-1-6`, `consistency-2-6`, `consistency-3-6`, `consistency-4-6`, `consistency-5-6`, `consistency-6-6`, `consistency-1-7`, `consistency-2-7`, `consistency-3-7`, `consistency-4-7`, `consistency-5-7`, `consistency-6-7`, `consistency-7-7`, `consistency-1-8`, `consistency-2-8`, `consistency-3-8`, `consistency-4-8`, `consistency-5-8`, `consistency-6-8`, `consistency-7-8`, `consistency-8-8`, `consistency-1-1000`, `consistency-511-1000`, `consistency-512-1000`, `consistency-513-1000`, `consistency-999-1000`, `consistency-0-0`, `consistency-0-5`, `consistency-refuse-0-5-nonempty-proof`, `consistency-refuse-5-5-nonempty-proof`, `consistency-refuse-5-5-roots-differ`, `consistency-refuse-m-above-n`, `consistency-refuse-empty-proof`, `consistency-refuse-proof-short`, `consistency-refuse-proof-long`, `consistency-refuse-tampered-proof`, `consistency-refuse-wrong-old-root`, `consistency-refuse-wrong-new-root`
- `note` (16): `spec-example`, `three-signatures-one-unknown`, `name-only-match-ignored`, `id-only-match-ignored`, `sixteen-lines-pq-size`, `line-exactly-8-kib`, `cr-in-text-refused`, `nul-in-text-refused`, `missing-blank-line-refused`, `plus-in-key-name-refused`, `known-key-bad-signature-refused`, `no-known-key-refused`, `signature-line-without-dash-refused`, `invalid-utf8-refused`, `sshsig-preamble-text-refused`, `agent-roundtrip`
- `sshsig` (7): `good`, `wrong-namespace-refused`, `one-byte-tampered-refused`, `wrong-key-refused`, `trailing-byte-outer-blob-refused`, `trailing-byte-key-blob-refused`, `trailing-byte-signature-blob-refused`
- `witness` (4): `spec-example`, `old-size-0`, `old-equals-new`, `old-5-new-9`

#### `levain_profile`: 18 vectors

- `checkpoint` (10): `size-above-max-note-int`, `extension-line-refused`, `origin-not-key-name-refused`, `origin-no-levain-prefix-refused`, `origin-prefix-only-refused`, `team-verifier-31-hex-refused`, `team-verifier-33-hex-refused`, `team-verifier-uppercase-hex-refused`, `team-verifier-trailing-segment-refused`, `team-verifier-fleet-prefix-refused`
- `cosig` (2): `timestamp-now-plus-601-refused`, `type-04-signature-under-type-01-vkey-refused`
- `grammar` (1): `decimal-above-note-max`
- `note` (2): `seventeen-lines-refused`, `line-over-8-kib-refused`
- `sshsig` (3): `hash-sha256-refused`, `reserved-non-empty-refused`, `ecdsa-p256-key-refused`

#### `upstream_lax`: 15 vectors

- `checkpoint` (6): `size-leading-zero-refused`, `size-0-non-empty-root-refused`, `root-31-bytes-refused`, `root-non-canonical-base64-refused`, `duplicate-identical-line-refused`, `duplicate-name-and-id-different-bytes-refused`
- `grammar` (5): `decimal-leading-zero`, `decimal-double-zero`, `b64-nonzero-pad-bits`, `b64-embedded-newline`, `b64-embedded-cr`
- `merkle` (2): `consistency-refuse-0-5-old-root-not-empty`, `consistency-refuse-0-0-roots-not-empty`
- `note` (1): `non-canonical-base64-refused`
- `sshsig` (1): `s-plus-l-mutation-refused`

#### `no_upstream`: 304 vectors

- `canon` (49): `canonical-code-point-key-order`, `canonical-escape-set`, `canonical-emoji-value`, `canonical-integers`, `canonical-nesting-and-literals`, `leaf-canonical-accepted`, `leaf-canonical-accepted-at-verify`, `leaf-non-canonical-reordered-keys-refused`, `leaf-non-canonical-whitespace-refused`, `leaf-non-canonical-escaped-e-acute-refused`, `leaf-non-canonical-minus-zero-refused`, `leaf-non-canonical-bom-refused`, `leaf-non-canonical-trailing-newline-refused`, `leaf-canonical-e-acute-control`, `leaf-canonical-zero-control`, `leaf-parse-float-refused`, `leaf-parse-exponent-refused`, `leaf-parse-nan-refused`, `leaf-parse-infinity-refused`, `leaf-parse-duplicate-key-refused`, `leaf-parse-lone-surrogate-escape-refused`, `leaf-parse-surrogate-bytes-refused`, `leaf-parse-invalid-utf8-refused`, `leaf-parse-int-2-53-refused`, `leaf-parse-int-minus-2-53-refused`, `leaf-int-2-53-minus-1-accepted`, `leaf-depth-4-accepted`, `leaf-depth-32-accepted`, `leaf-depth-33-refused`, `leaf-depth-5000-refused`, `leaf-brackets-in-strings-are-not-depth`, `leaf-unknown-leaf-refused-at-ingest`, `leaf-unknown-leaf-unverifiable-at-verify`, `leaf-unknown-v-refused-at-ingest`, `leaf-unknown-v-unverifiable-at-verify`, `leaf-registered-leaf-wrong-log-kind-refused`, `leaf-registered-leaf-wrong-log-kind-refused-at-verify`, `leaf-receipt-checkpoint-on-team-accepted`, `leaf-frame-not-an-object-refused`, `leaf-frame-missing-v-refused`, `leaf-frame-v-true-refused`, `leaf-frame-v-string-refused`, `leaf-frame-v-negative-refused`, `leaf-frame-leaf-empty-refused`, `leaf-frame-leaf-not-string-refused`, `leaf-frame-empty-refused`, `chain-hash-team-golden-1`, `chain-hash-team-golden-2`, `chain-hash-ignores-hash-field`
- `entry` (89): `team-golden-entry-1`, `team-golden-entry-1-sealed`, `team-golden-entry-2`, `team-golden-entry-2-sealed`, `finding-minimal`, `finding-scanned-clean`, `secret-looking-text-unscanned`, `question-with-agent-session-owner`, `tension-practice-kind`, `decision-practice`, `constraint-ruling-with-mode`, `ruling-finding-mode-ask-once`, `ack-with-refs`, `retire-with-supersedes`, `retire-supersedes-a-a`, `paths-globs`, `path-six-starred-segments`, `known-finding-target`, `known-ruling-target-by-ruling`, `known-ruling-target-by-retire`, `known-empty-no-links`, `future-ts-within-a-day`, `boundary-owner-200`, `boundary-words-4000`, `boundary-reason-4000`, `boundary-summary-2000`, `boundary-recheck-1000`, `boundary-paths-100-items`, `boundary-supersedes-100-items`, `boundary-refs-100-items`, `boundary-path-300-chars`, `boundary-agent-128-chars`, `boundary-session-128-chars`, `boundary-author-handle-64-chars`, `r01-unknown-field`, `r02-schema-version`, `r03-agent-not-string`, `r04-missing-ts`, `r05-id-shape`, `r06-id-not-author-handle`, `r07-ts-shape`, `r08-ts-not-a-real-date`, `r09-ts-future`, `r10-agent-not-plain`, `r11-owner-201`, `r12-owner-not-printable`, `r13-type-unknown`, `r14-decision-without-kind`, `r15-kind-unknown`, `r16-mode-unknown`, `r17-mode-on-practice`, `r18-ruling-without-owner`, `r19-ruling-without-words`, `r20-supersedes-not-a-list`, `r21-paths-101-items`, `r22-path-301-chars`, `r23-seven-starred-segments`, `r24-segment-with-two-stars`, `r25-retire-without-supersedes`, `r26-ack-without-refs`, `r27-ack-with-supersedes`, `r28-path-absolute`, `r29-supersedes-itself`, `r30-unknown-link`, `r31-supersedes-an-ack`, `r32-supersedes-a-retire`, `r33-ruling-target-without-words`, `r34-ruling-target-by-a-practice`, `r35-secret`, `r02-schema-version-missing`, `r04-missing-id-empty`, `r10-session-not-plain`, `r11-recheck-1001`, `r18-ruling-owner-blank`, `r19-ruling-words-blank`, `r20-supersedes-an-object`, `r20-paths-holds-a-number`, `r20-refs-holds-a-blank`, `r21-refs-101-items`, `r28-path-backslash`, `r28-path-dotdot`, `r30-unknown-ref`, `r35-secret-in-summary`, `f9-member-finding-supersedes-empty-string`, `f9-member-finding-supersedes-object`, `f9-member-finding-id-not-author-prefixed`, `f9-member-finding-malformed-ts`, `now-none-skips-the-future-ts-branch`, `now-none-still-refuses-every-other-branch`
- `grammar` (12): `int-note-max`, `int-note-above-max`, `int-json-max`, `int-json-above-max`, `int-zero`, `int-negative-note`, `int-negative-json`, `int-true-note`, `int-true-json`, `int-false-note`, `int-float-note`, `int-string-note`
- `line` (43): `message-fixed-golden`, `message-file-nfc`, `message-file-nfd`, `message-file-multibyte`, `message-chain-hash-uppercase-refused`, `message-chain-hash-63-hex-refused`, `message-origin-not-team-production-refused`, `leaf-good-at-ingest`, `leaf-good-at-verify`, `leaf-nfd-file-good`, `leaf-signed-for-the-nfc-file-presented-as-nfd-refused`, `leaf-signed-for-another-origin-refused`, `leaf-signed-for-another-file-refused`, `leaf-device-fp-not-the-signing-key-refused`, `leaf-entry-edited-after-sealing-refused`, `leaf-entry-unsealed-refused`, `leaf-extra-key-n-refused`, `leaf-missing-device-fp-refused`, `leaf-file-not-a-string-refused`, `leaf-device-sig-armored-refused`, `leaf-device-sig-non-canonical-base64-refused`, `leaf-device-sig-trailing-byte-refused`, `leaf-leaf-bytes-not-canonical-refused`, `leaf-future-ts-refused-at-ingest`, `leaf-future-ts-accepted-at-verify-known-edge`, `leaf-unknown-scope-refused-at-ingest`, `leaf-unknown-scope-unverifiable-at-verify`, `leaf-line-v2-refused-at-ingest`, `leaf-line-v2-unverifiable-at-verify`, `leaf-receipt-scope-writes-no-line-refused`, `receipt-matches-the-derived-fields`, `receipt-team-version-disagrees-refused`, `receipt-team-version-as-string-refused`, `receipt-n-disagrees-refused`, `receipt-leaf-index-disagrees-refused`, `receipt-scope-disagrees-refused`, `receipt-handle-disagrees-refused`, `receipt-device-hint-disagrees-refused`, `receipt-hash-disagrees-refused`, `receipt-device-sig-disagrees-refused`, `receipt-extra-field-refused`, `receipt-accepted-malformed-refused`, `receipt-n-true-refused`
- `merkle` (3): `inclusion-refuse-index-true`, `inclusion-refuse-index-negative`, `consistency-refuse-m-true`
- `origin` (25): `grammar-simple`, `grammar-all-classes`, `grammar-255-bytes`, `grammar-256-bytes-refused`, `grammar-plus-refused`, `grammar-space-refused`, `grammar-uppercase-refused`, `grammar-ssh-fingerprint-refused`, `grammar-empty-refused`, `grammar-empty-segment-refused`, `grammar-trailing-slash-refused`, `grammar-leading-dot-refused`, `grammar-non-ascii-refused`, `kind-team`, `kind-fleet`, `kind-receipt`, `kind-other-prefix-refused`, `kind-no-prefix-refused`, `kind-bare-prefix-refused`, `team-production`, `team-31-hex-refused`, `team-33-hex-refused`, `team-uppercase-refused`, `team-trailing-segment-refused`, `team-non-hex-refused`
- `receipt_checkpoint` (27): `first-anchor`, `second-anchor-with-proof`, `second-anchor-with-proof-at-verify`, `equal-size-same-root-re-anchor-accepted`, `equal-size-different-root-refused`, `wrong-old-size-refused`, `first-anchor-old-size-not-zero-refused`, `old-size-wrong-with-the-valid-proof-refused`, `first-anchor-old-size-not-zero-empty-proof-refused`, `forked-second-anchor-refused`, `truncated-anchor-refused`, `a-refused-anchor-moves-nothing`, `two-origins-anchor-independently`, `size-2-53-minus-1-accepted`, `size-2-53-refused`, `note-signed-by-another-key-refused`, `checkpoint-extension-line-refused`, `old-zero-with-a-proof-refused`, `proof-one-hash-short-refused`, `proof-line-non-canonical-base64-refused`, `proof-line-31-bytes-refused`, `extra-key-refused`, `old-size-true-refused`, `old-size-2-53-refused`, `consistency-not-a-list-refused`, `v2-refused-at-ingest`, `v2-unverifiable-at-verify`
- `scope` (49): `may-write-human-finding`, `may-write-human-question`, `may-write-human-tension`, `may-write-human-ack`, `may-write-human-retire`, `may-write-human-decision`, `may-write-human-constraint`, `may-write-member-finding`, `may-write-member-question`, `may-write-member-tension`, `may-write-member-ack`, `may-write-member-retire`, `may-write-member-decision`, `may-write-member-constraint`, `may-write-receipt-finding`, `may-write-receipt-question`, `may-write-receipt-tension`, `may-write-receipt-ack`, `may-write-receipt-retire`, `may-write-receipt-decision`, `may-write-receipt-constraint`, `may-write-member-finding-supersedes-a-target-refused`, `may-write-member-finding-supersedes-empty-list-allowed`, `may-write-human-finding-kind-ruling-refused`, `may-write-member-finding-kind-ruling-refused`, `may-write-receipt-finding-kind-ruling-refused`, `may-write-human-retire-of-a-ruling-refused`, `may-write-human-retire-of-a-finding-allowed`, `may-write-human-retire-target-not-in-snapshot-refused`, `may-write-human-retire-one-allowed-one-ruling-refused`, `may-write-human-retire-a-a-allowed`, `may-write-human-finding-supersedes-a-finding-allowed`, `may-write-human-retire-target-twice-in-snapshot-refused`, `may-write-human-retire-empty-supersedes-refused`, `log-second-leaf-copying-an-accepted-id-refused-at-ingest`, `log-second-leaf-copying-an-accepted-id-refused-at-verify`, `log-a-refused-leaf-holds-no-id`, `log-repeated-file-hash-refused-at-ingest`, `log-repeated-file-hash-operator-fault-at-verify`, `log-member-finding-supersedes-empty-string-refused`, `log-member-finding-supersedes-object-refused`, `log-member-finding-id-not-prefixed-by-author-refused`, `log-member-finding-malformed-ts-refused`, `log-member-finding-carrying-supersedes-refused`, `log-member-finding-empty-supersedes-allowed`, `log-human-retire-of-an-accepted-finding-allowed`, `log-human-retire-dangling-target-refused`, `log-unknown-scope-refused-at-ingest`, `log-unknown-scope-unverifiable-at-verify`
- `sshsig` (2): `signed-data-fixed-message`, `embedded-key-not-device-fp-refused`
- `witness` (5): `old-size-0-with-proof-refused`, `old-above-new-refused`, `sixty-four-proof-lines-refused`, `size-0-non-empty-root-refused`, `proof-hash-31-bytes-refused`
<!-- /vectors-by-kind -->
