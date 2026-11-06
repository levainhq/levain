# Security policy

Levain is a security product: it decides what an AI agent may do with your authority, and it signs the record of who
approved what. A flaw in how it does that is worth a private report.

## Supported versions

The latest release on PyPI ([pypi.org/project/levain](https://pypi.org/project/levain/), or ). Fixes land on  and ship in the next release; older releases are not patched. `levain --version` prints what you run, and [the changelog](CHANGELOG.md) names what each release changed.

## Reporting a vulnerability

Please report privately, not in a public issue or discussion. The one channel is GitHub private vulnerability reporting:
<https://github.com/levainhq/levain/security/advisories/new> (or the **Report a vulnerability** button on the
repository's Security tab).

A report that lets us reproduce the problem fastest names the Levain version (`levain --version`), the platform, the
steps, and what the attacker needs to already have (a paired phone, a local account, nothing).

We intend to acknowledge a report, keep you told as we work on it, and credit you in the release notes if you want that.
That is an intent, not a promise of a date.

## What is in scope

A flaw in any of these, reachable by someone who is not already entitled to do the thing:

- **The consent queue and standing grants:** an agent action that runs without the signed answer or grant its policy
  requires, a grant that covers more than it was signed for, or a record that does not show who approved what.
- **Signing:** a signed answer or grant that verifies when it should not, or one that fails to verify when it should.
  The signature namespaces are listed in [`docs/contracts.md`](docs/contracts.md) §F11.
- **The cockpit and pairing:** a way to use the cockpit, or a paired device, without the pairing or passkey step.
- **The sandbox and confinement:** a way out of the OS sandbox or the confinement floor that the documentation says holds.
- **Memory governance:** a path into long-term memory that the governed write path was meant to refuse.

## What is out of scope

- Anything the README already states as a boundary: see *Boundaries, kept honest* in the [README](README.md).
  Levain confines no Claude Code or Codex session; it installs hooks and consent paths for them.
- A problem that needs you to have already turned the efferent gate off yourself (`efferent_gate: "ungated"`, which the
  README describes under *Boundaries, kept honest*).
- Vulnerabilities in Claude Code, Codex, `anneal-memory` or another dependency: report those to their owners.
- Findings that need a process already running as your own OS user, or physical access to an unlocked machine you are
  already signed in to.
