# RETURN cfgsign2 (board row 113, F2 built; L3 round 1 + round 2 fixed)

## The build (a3f6ba50)

No trust-model question stopped this build. One calibration settled from the code: the frame said "T3-class floor" for the
phone approval, but a passkey can only be class `display-phone` (authority.py: "a synced passkey never signs T3"), which
covers T2. The approval floor is therefore T2 (same as a thread envelope and a signed review clear); the T3 authority over
configuration stays the laptop key's, unchanged. The phone is an added gate, so this weakens nothing.

Built: `levain/dispatch/phone_config.py`; `config request` + `config accept-approved` in `levain/dispatch/cli.py`; namespace
`levain-dispatch-config-approve` (registry, golden, fixed message grammar in `LEVAIN_SIGN_REQUESTS`);
`tests/test_dispatch_config_phone.py`.

Flow: `config request <kind> <body>` -> item bytes kept in `<dispatch-dir>/config-pending/<sha>.item`, request filed (1 h)
over a message that signs store, kind, generation, prev, item-sha256, expires and `rp` (the queue cockpit's site at
filing), pushed -> a passkey OF THAT SITE signs on the phone -> `config accept-approved <rid> --signing-key <laptop key>`
verifies the approval (levain's own namespace + grammar, signed before the signed deadline, record over the message, the
record's passkey of the signed site AND active on that site in the SAME authority log the store verifies against, class
covers T2), locks the kept item, re-checks fields/head, shows the full item, laptop key signs (no y/N), judges the approval
AGAIN after the touch, then the unchanged `ControlStore.accept`.

## L3 round 1 (46079908)

Point-of-use re-verification after the touch (revoked passkey / expiry during the touch discards the signature); signed
`expires` line (request-file edits cannot extend it); exclusive claim before any signer; bounded/unfollowed result reads;
unique O_EXCL temporaries + fsync; filing failure removes the kept item; age pruning; agent refusal on every config verb.

## L3 round 2 (verify round cfg_v1, 4 lineages)

1. codex: passkey SITE binding lost (the RP was derived from the record's own key, tautological; any active passkey of
   ANY site in this log approved). FIXED: `rp:` is a signed line of the message (grammar in `namespaces.py`), taken from
   the queue's published origin at filing (`phone_config.queue_rp`; no origin = nothing filed). `read_approval` requires
   `key_application(record public_key) == rp` and looks the key up active on that rp only. A cockpit stopping between
   tap and touch no longer matters: the site travels in the signed message.
2. codex MED: concurrent same-bytes filers; one's cleanup removed the other's item. FIXED BY DELETION: a kept item is
   published with `os.link` (never over another file); an existing one is refused (`FileExistsError`), never replaced,
   refreshed or shared. Each item has one owner, so cleanup removes only its own. Also closes codex's companion MED (a
   claim locking a replaced inode): the inode never changes while kept.
3. codex LOW: directory fsync failure after publish left an orphan. FIXED: publish + sync in one try; a failure after
   the link removes the item and the temporary.
4. complement N1: identical re-request left a stale mtime under a live approval. CLOSED BY (2), not by `os.utime`: an
   identical re-request is refused, so every kept item's mtime is its one filing time and its approval's signed deadline
   (filing + 1 h) is always inside the prune age (2 h). Note: identical bytes are unreachable from the CLI anyway (the
   item's `issued_at` carries microseconds).
5. gemini `_sign_fault_not_done` undefined: REFUTED (defined in cli.py); a one-line test pins it.

Tests: `tests/test_dispatch_config_phone.py` 41 (6 new in round 2). Mutation-checked: tautological site, replace over a
kept item, no item cleanup after publish, rp not from the queue -- each caught by exactly its test.

## Known edges (closed under the reachability line)

- codex LOW: the request file is linked into the queue, then the queue's directory fsync fails -> `SignRefused` -> the
  kept item is removed while a signable request exists. Fail-closed: approving it gives "not kept here; file a new
  request". Not a data loss, not reachable by anyone untrusted.
- Terminal kept items (denied/expired/abandoned) leave only by age pruning on a later request (bounded scan 4096).
- State/record/signature are separate reads: a concurrent replacement fails closed with a confusing refusal.
- Stale store raises `InputRefused` (exit 2, same as `ConfigRefused`'s public exit).

## Residue only a real phone + Touch ID can discharge

1. A real passkey assertion from his iPhone on a real `config request` card (push arrival, the sheet rendering the
   message with its `rp` line, one tap) -- tests use a software P-256 passkey and a recorded push.
2. The laptop key signing WITHOUT a terminal question: `ssh_signer` on his real Secure Enclave key must raise the Touch ID
   sheet from a non-tty run of `accept-approved`; tests use a file key. UNVERIFIED whether the sheet needs a tty.
3. Real queue discovery: `--sign-queue-dir`/`LEVAIN_SIGN_QUEUE_DIR` against his live cockpit queue and origin (a
   `config request` now needs the cockpit running to file).
4. The live store untouched: no real dispatch store, key or request was used or filed.
