# Host grants: letting one session reach one more host

A governed session that runs in an egress slot reaches the network only through its gateway front, and the front
admits only the hosts on the session's own list. A host grant is how you add one host to that list for one running
session, with a tap, instead of editing the operator list with `routes-egress` and waiting.

This page describes the whole stack: root's verbs and store (levain#66 S2), the gateway front that enforces a grant
(S3), the card that files one for a tap (S4) and the cockpit's list and revoke (S5). Each section names the part it
describes.

## What a grant is

- A grant names **one host, on port 443**, for **one session in one slot**. It does not name an account, a path or a
  repository.
- **An allowlist names hosts, not accounts.** A host that accepts uploads (a code host, a paste site, a file store)
  can receive a copy of anything the session can read, into an account you did not intend. A grant to such a host
  allows exactly that. Levain does not prevent it.
- A grant lasts until the end of the session or a chosen time, whichever is first: the choice `s` ("this session")
  is capped at 8 hours from issue, the choice `d` ("24 hours") at 24 hours from issue and still applies to this
  session only.
- Root keeps at most 16 grants per session file. A grant to a host that is already routed, or to a name that is never
  granted (`localhost`, `*.local`, `*.internal`, `metadata.*` and the other names that are never the public internet),
  is refused. The front's address check still runs on a granted host: a name that resolves to a private or
  loopback address is refused.

**Which sessions qualify.** Only an `interactive` or `login` session on an egress-class slot. A dispatch or planner
session has no person at the keyboard to tap, so a host it asks for that is not on its list is refused as
`unattended`, and its only way to a new host is `routes-egress` from the laptop. Two different words name one
refusal. On the wire the agent sees `X-Levain-Refused: host-not-allowed`, the same header for a host that is off the
list for any reason. In the front's log the same line carries `refused: host-not-allowed` and a `detail`: `unattended`
for these sessions, `off-list` for an interactive or login session whose host has no grant. The reason is in the
log, not on the wire.

## How a grant is issued

A grant is issued by a tap on the phone's passkey, filed as a card by the session's supervisor (S4). The steps:

1. While an interactive or login session runs on an egress slot, the supervisor asks root's `egress-refusals` which
   hosts the front refused as off-list for this session. The host is taken from root's log of those refusals; nothing
   in the signed message says the host was refused.
2. For a refused host it files one card in the signing queue and sends a push notice where a phone is live. One host card is open in a
   queue at a time. The card names the host and the slot and offers the two choices, `s` and `d`.
3. You tap it with the passkey enrolled for host grants (see Setting it up). The supervisor hands the signed answer to
   root's `egress-grant`, which verifies the signature, the session, the slot and the time, and publishes the grant.
4. A card nobody taps expires on its own, and tapping it afterwards does nothing.

No card is filed when no cockpit is running for the signing queue. If root's `egress-grants-list` is refused when the
session starts (for example `egress-grant-not-ready`), the supervisor says once that host cards are off for this
session and the session goes on.

**Seats are not covered.** A seat's egress is its own lease (`levain seat egress`, see
[seat-profile.md](seat-profile.md)). Host grants are for slot sessions and never change a seat's route.

## Setting it up

Grants are signed by a key you enrol, and root keeps a grant store of its own. Two steps are required, and the
second is not the first:

1. **Enrol the signer with `--egress-host`**, once:

```
sudo <install-helper> enrol-authority-signer --principal YOUR_NAME --public-key '<type> <base64>' --egress-host
```

2. **Run `setup` or `repair`**, so the per-slot grant directory exists. `setup` and `repair` create it; only step 1 is
   outside them.

- `--egress-host` is what names the host-grant signing purpose for every key in that command and creates root's
  grant store. Without it the store does not exist, and listing a session's grants refuses as
  `egress-grant-not-ready`. A phone passkey gets the purpose only when it is in that command as `--passkey`.
- **Run step 1 again after you upgrade** to a build that has host grants: signers enrolled before it do not carry the
  new purpose, and a re-run with the flag adds it. The re-run must repeat what the first one had: the same
  `--principal`, the same `--public-key`, `--confirm` if the first had it, and every `--passkey`. A line whose
  principal differs, or whose namespaces the new command would not keep, is refused `authority-signer-changed`, and a
  key left out of the command keeps its old line without the new purpose.
- Merged is not running: a build that has the code but not steps 1 and 2 admits nothing.

## What one revoke does, and what it does not claim

Revoking is root's `egress-grant-revoke <session> <grant id | all>`. It needs **no signature**, because it can only
narrow: it removes grants and never adds one.

- Revoking an id the session's file holds removes it and answers `removed`. Revoking `all` removes what the file holds
  at that moment, and writes nothing to the ledger.
- The file can hold an expired grant: it is dropped when the next grant for the session is published, not when it
  expires. Revoking such an id answers `removed`, which is harmless, because an expired grant admits nothing.
- An id the file does not hold (never issued, already revoked, or dropped by a later rewrite) is not removed. Root
  writes it to a revocation ledger instead, so a signed grant that is still on its way to root cannot land afterwards.
  The answer says `recorded`, not `removed`.
- `Revoke all` never touches that ledger.
- Revoke while a delivery is in flight can answer `session-busy`: re-run the same command.
- A grant whose signed bytes were already on their way to root when you revoked can still land seconds later; it
  shows up as a new row, not as a failure.
- What each answer proves: `removed` proves root's session file changed. `recorded` proves root wrote the id to the
  ledger and that the session file did not hold it. Neither proves a tunnel closed. The sentence to rely on is this
  one: **Root revoked the grant. A healthy running front closes its granted tunnels on its next check, normally within
  about five seconds.** The front re-reads root's file on a fixed tick of five seconds, and ends a granted tunnel whose
  grant is no longer in it: `grant-revoked` for a revoked grant, `grant-expired` for one past its expiry, and every
  granted tunnel with `grant-unchecked` if the check itself fails. Expiry is enforced in both places: the tunnel also
  checks its own clock on every wait and every chunk, so it ends `grant-expired` at its expiry, not up to one tick
  later.
- Bytes already in a transport or kernel buffer when a tunnel ends may still be delivered.

**At teardown** a session's grants are removed; if that fails, they are removed at the slot's next render, and the
ended session's front is gone either way.

## From the laptop

There is no `levain` command for host grants. The raw root verbs are for the operator account that owns the session;
any other caller gets `no-session`, whether or not the session exists:

```
sudo <runtime-helper> egress-grants-list <session>
sudo <runtime-helper> egress-grant-revoke <session> all
sudo <runtime-helper> egress-grant-revoke <session> <grant id>
```

`egress-grants-list` answers `store` (root's grant-store id) and `entries`, each with `grant` (the `eh-` id to
revoke), `host`, `expires` and `choice`; an expired grant is absent, though a revoke of its id can still answer
`removed` (above). A session id has the form `s-` and 16 hex digits; a grant id is `eh-` and 24 hex digits.

## From the phone

The cockpit lists and revokes host grants (S5a is the route, S5b-page the block on the Decisions view). A cockpit
serves it only when it was started by `levain serve --write` with the root runtime helper on disk.

- **What it shows.** One call to root's `egress-grants-all` lists every live grant of your interactive or login egress
  sessions that are ACTIVE or paused (BRAKED), by slot: session id, host, expiry and choice. A slot root could not
  read is a note, never an empty list.
- **What it does.** Each grant has a Revoke and each session a Revoke all, both asking a second tap. It calls root's
  `egress-grant-revoke` and nothing else. A per-id revoke is sent only for an id the cockpit's own listing holds; any
  other answers "already gone" with no call to root.
- **Who may use it.** Only the cockpit page itself and devices paired from the cockpit page or from the laptop. A
  device paired through a relay, or another surface's token, is refused 403. Each revoke is logged with the device's
  name and limited to 10 a minute per device.
- **The guarantee it aims at is exactly this and no wider:** Untrusted HTTP fields can reach only these validated
  helper invocations. The cockpit runs as the operator, whose sudoers rule covers the whole helper, so this is a check
  on its input, not a confinement of the process.
- A holder of that access sees every live host grant. They can end a session's grants and can never add one, because
  adding one needs a signature root verifies.
