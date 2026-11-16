# Host grants: letting one session reach one more host

A governed session that runs in an egress slot reaches the network only through its gateway front, and the front
admits only the hosts on the session's own list. A host grant is how you add one host to that list for one running
session, with a tap, instead of editing the operator list with `routes-egress` and waiting.

This page says what the code does now. The phone's list-and-revoke surface is a separate piece of work, described at
the end in words about intent, not behaviour.

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
`unattended`, and its only way to a new host is `routes-egress` from the laptop. The agent sees the same
`X-Levain-Refused: host-not-allowed` for every one of these; the reason is in the front's log.

**Seats are not covered.** A seat's egress is its own lease (`levain seat egress`, see
[seat-profile.md](seat-profile.md)). Host grants are for slot sessions and never change a seat's route.

## Setting it up

Grants are signed by a key you enrol, and root keeps a grant store of its own. Both steps are yours to run once, and
neither is part of `setup` alone:

```
sudo <install-helper> enrol-authority-signer --principal YOUR_NAME --public-key '<type> <base64>' --egress-host
```

- `--egress-host` is what names the host-grant signing purpose for every key in that command and creates root's
  grant store. Without it the store does not exist, and listing a session's grants refuses as
  `egress-grant-not-ready`.
- **Run it again after you upgrade** to a build that has host grants: signers enrolled before it do not carry the new
  purpose, and a re-run with the flag adds it.
- **Run `setup` or `repair` as well**, so the per-slot grant directory exists. Merged is not running: a build that
  has the code but not these two steps admits nothing.

## What one revoke does, and what it does not claim

Revoking is root's `egress-grant-revoke <session> <grant id | all>`. It needs **no signature**, because it can only
narrow: it removes grants and never adds one.

- Revoking an id the session's file holds removes it. Revoking `all` removes every grant the file holds.
- An id the file does not hold (it expired, was pruned, or was already revoked) is not removed. Root writes it to a
  revocation ledger instead, so a signed grant that is still on its way to root cannot land afterwards. The answer
  says `recorded`, not `removed`, and means "that grant was already gone".
- `Revoke all` never touches that ledger.
- Revoke while a delivery is in flight can answer `session-busy`: tap again.
- **`Revoke all` revokes the grants that were in force.** A grant whose signed bytes were already on their way to
  root when you revoked can still land seconds later; it shows up as a new row, not as a failure.
- The answer proves root's file changed. It does not prove a tunnel closed. The sentence to rely on is this one:
  **Root revoked the grant. A healthy running front closes its granted tunnels on its next check, normally within
  about five seconds.** The front re-reads root's file on a fixed tick of five seconds, and ends a revoked grant's
  tunnels with `grant-revoked`, an expired one with `grant-expired`, and every granted tunnel with `grant-unchecked`
  if the check itself fails. A grant's expiry ends its tunnel on the tunnel's own clock check, not on the tick.
- Bytes already in a transport or kernel buffer when a tunnel ends may still be delivered.

**At teardown** a session's grants are removed; if that fails, they are removed at the slot's next render, and the
ended session's front is gone either way.

## From the laptop

There is no `levain` command for host grants yet. The raw root verbs, which an operator account may run, are:

```
sudo <runtime-helper> egress-grants-list <session>
sudo <runtime-helper> egress-grant-revoke <session> all
```

`egress-grants-list` answers the session's live grants (host, expiry, choice); an expired or revoked grant is simply
absent. A session id has the form `s-` and 16 hex digits; a grant id is `eh-` and 24 hex digits.

## The phone: planned, not built here

The phone cockpit's list-and-revoke surface (board item levain#66, slice S5) is separate work, and this page does not
describe it as running. What is decided about it:

- It is **planned** to be reachable only from the cockpit page itself and from devices paired from the cockpit page
  or from the laptop, each revoke logged by device name and rate-limited. A device paired through a relay is not
  planned to reach it.
- It is **planned** to call two root verbs only, a listing and a revoke, and the guarantee it aims at is exactly this
  and no wider: Untrusted HTTP fields can reach only these validated helper invocations.
- What a holder of that access can see is every live host grant: session ids, hosts and expiries. What they can do is
  end a session's grants. They can never add a grant, because adding one needs a signature root verifies.
