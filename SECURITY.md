# Security policy

> **DRAFT, NOT YET LANDABLE.** The reporting channel below is the maintainer's choice and is not chosen yet. Do not
> merge this file while the line marked `SLOT` is still a slot: a security policy that points at a channel nobody reads
> is worse than none.

Levain is a security product: it decides what an AI agent may do with your authority, and it signs the record of who
approved what. A flaw in how it does that is worth a private report.

## Supported versions

The latest released minor version. Fixes land on `main` and ship in the next release; older minors are not patched.
`levain --version` prints what you run, and [the changelog](CHANGELOG.md) names what each release changed.

## Reporting a vulnerability

Please report privately, not in a public issue or discussion.

- **GitHub private vulnerability reporting:** <https://github.com/levainhq/levain/security/advisories/new>
  (only works once the repository has the setting turned on; this is a repository setting for the maintainer to switch).
- **SLOT, the maintainer's choice: `[a reporting mailbox, or "GitHub advisories only"]`**

A report that lets us reproduce the problem fastest names the Levain version (`levain --version`), the platform, the
steps, and what the attacker needs to already have (a paired phone, a local account, nothing).

We intend to acknowledge a report, keep you told as we work on it, and credit you in the release notes if you want that.
That is an intent, not a promise of a date.

## What is in scope

A flaw in any of these, reachable by someone who is not already entitled to do the thing:

- **The consent queue and standing grants:** an agent action that runs without the signed answer or grant its policy
  requires, a grant that covers more than it was signed for, or a record that does not show who approved what.
- **Signing:** a signed answer or grant that verifies when it should not, or one that fails to verify when it should.
  The frozen formats are described in [`docs/contracts.md`](docs/contracts.md).
- **The cockpit and pairing:** a way to use the cockpit, or a paired device, without the pairing or passkey step.
- **The sandbox and confinement:** a way out of the OS sandbox or the confinement floor that the documentation says holds.
- **Memory governance:** a path into long-term memory that the governed write path was meant to refuse.

## What is out of scope

- Anything the README already states as a boundary: see *Boundaries, kept honest* in the [README](README.md).
  Levain confines no Claude Code or Codex session; it installs hooks and consent paths for them.
- A problem that needs you to have already disabled a gate yourself (the README names the ones you can turn off).
- Vulnerabilities in Claude Code, Codex, `anneal-memory` or another dependency: report those to their owners.
- Findings that need physical access to an unlocked machine you are already signed in to.
