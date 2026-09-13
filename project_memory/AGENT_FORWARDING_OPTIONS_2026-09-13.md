# Levain — SSH-agent forwarding: security options (2026-09-13)

**For Phill's decision.** Written by `0913+12 levain-seat` after `spore-755` (a confined entity reaching a
local `sshd` as root through the forwarded agent) surfaced three reproduced residuals. Phill asked for
web research + a first-principles read + an options doc before deciding, rather than reflexively
tightening. Revised once after a fan-in desk challenge (source corrections + options split). Companion:
`spore-1005` (the three residuals, reproduced + measured).

Ground truth that constrains everything below: **`0.4.6` already ships option A** — a `deny_localhost_outbound`
that blocks outbound-to-self in `ssh_mode="agent"` (commit `666d9b6`). That closes the *fresh-connection,
agent-mode, local-sshd* path. It does **not** close the class. This doc is about the class.

---

## First principles

- **Asset:** the crown jewels — `~/.anneal-memory` (operator identity/memory), sibling entity stores,
  operator credential files. The floor denies the entity's hands from reading these.
- **Adversary:** a **prompt-injected entity** running inside the seatbelt floor. Assume it will try
  anything its hands can reach, with any tool — not only a cooperating `ssh`.
- **Capability actually needed:** authenticated **git to specific remotes** (and rarely, ssh to a
  specific dev host). Narrow and enumerable.
- **What we grant instead:** the operator's **whole ssh-agent** — a *general signing oracle*. OpenSSH's
  manual is blunt: an attacker with socket access "can perform operations on the keys that enable them to
  authenticate using the identities loaded into the agent" (`man.openbsd.org/ssh_config.5`, ForwardAgent).
  It signs for **any** host that trusts the key, through **any** hop the floor allows.
- **Minimality gap:** the grant (sign as the operator, anywhere) is far wider than the need (push to N
  known repos). Every `spore-755` residual — raw-mode, relay/ProxyCommand, ControlMaster — is a symptom
  of that gap. A localhost network deny can only chase symptoms; the oracle is the root.

### What the ecosystem actually does — the credential MODEL, not a claimed default

⚠ Corrected after the desk fetched `code.claude.com/docs/en/sandboxing` directly; an earlier draft
overstated Claude Code's *defaults*. What the page actually establishes:

- Claude Code **pre-allows no domains by default** (egress is allowlist-gated through an outside proxy).
- There is **no built-in credential deny list** — sandboxed commands **inherit the parent environment by
  default, including any credentials**, and `~/.ssh` is protected **only when you list it** under
  `sandbox.credentials`. So the *safe* configuration is a MODEL the operator opts into, not a default.
- The Unix-domain-socket filter is a **Linux seccomp** feature and **optional**; `allowUnixSockets` is
  **opt-in and carries an explicit sandbox-bypass warning** (e.g. `docker.sock`).
- Token **masking** (`injectHosts`: a sentinel shown to the agent, the real token substituted by the
  proxy on egress) is the documented way to auth `gh`/git over HTTPS without exposing the token.
- **Localhost egress default is NOT addressed on that page**, and the "macOS Seatbelt denies sockets
  except the proxy port" detail is from `github.com/anthropic-experimental/sandbox-runtime`, **not** the
  Claude Code docs page — treat it as sandbox-runtime's model, not a universal default.

So the honest convergence is a **credential model**: keep the signing credential **outside** the entity's
reach, hand in only a **repo-scoped** credential (deploy key or fine-grained token), mask tokens at an
egress proxy, and make socket/loopback reach **opt-in**. GitHub's own guidance backs the scope point: a
**deploy key** grants **one repository** (`docs.github.com/.../managing-deploy-keys`); a fine-grained PAT
scopes to selected repos with minimal permissions. Nobody's *documented* model hands a general signing
oracle to an untrusted process.

---

## Options (usefulness ↔ exposure)

### Option A — *(shipped in 0.4.6)* localhost-outbound deny, agent-mode — AND it is more than a belt
- **Closes:** (i) the fresh-connection, agent-mode, local-sshd read by any of the host's own addresses;
  **(ii) INDEPENDENTLY, the local-service side channel** — an entity hitting `argushub:8420` or any local
  daemon that re-exposes jewel content. (ii) has nothing to do with the ssh oracle and **survives D/E/F**.
- **Breaks:** the entity's in-floor access to *local* services in agent mode (opt-out `allow_localhost_outbound`).
- **Residual:** raw-mode, relay/signing-oracle, ControlMaster (the oracle is untouched).
- **Effort:** done.
- **Verdict:** keep it — and note that the loopback deny is part of the *fix* for the local-service
  channel, not only interim scaffolding for the oracle problem.

### Option B — extend the loopback deny to **both ssh_modes**
- **Closes:** the raw-mode fresh-connection sshd variant **and** the local-service side channel in raw
  mode too (that need exists in both modes regardless of what happens to the oracle — see A(ii)).
- **Breaks:** all in-floor loopback in raw mode as well (same accepted cost, wider).
- **Residual:** relay + ControlMaster still open (oracle symptoms).
- **Effort:** one line + tests (prototyped, held per Phill).
- **Verdict:** ⬆ upgraded from the first draft. Because A(ii) survives every credential option, **denying
  loopback in BOTH modes is part of the fix, not symptom-chasing** — the local-service channel is real in
  raw mode. Ship B regardless of which credential option wins. It just isn't *sufficient* alone.

### Option C — **destination-constrained agent** (OpenSSH 8.9 `ssh-add -h`)
- **Closes:** narrows the oracle to allowlisted destinations (`openssh.org/agent-restrict.html`).
- **Residual:** ⛔ constraints are enforced **only by a cooperating ssh** — a non-cooperating client on
  the same socket (`socat`, a hand-rolled tool) still abuses the oracle (OpenSSH says so explicitly). Our
  adversary controls the process, so this is a soft fence, not a wall.
- **Effort:** medium. **Verdict:** does not bind our threat model. Not the answer.

### Option D1 — **scoped key/token FILE inside the floor, no agent forwarding**
- **Closes:** the oracle. A read-write **deploy key**/scoped token authorizes exactly the entity's
  repo(s), is **not** in the laptop's `authorized_keys`, so `ssh localhost` fails → raw-mode, relay, and
  ControlMaster variants all die with it.
- **Breaks:** ssh-form git remotes move to the scoped key/HTTPS; non-git ssh the entity did (never a
  stated need). Multi-repo → multiple credentials.
- **Residual:** ⚠ the credential is a **file the entity holds, and the entity egresses to the network — so
  it is EXFILTRATABLE.** This CONTRADICTS the ecosystem's "credential outside the sandbox" model (that is
  why D2 exists). Blast radius if stolen = the repo(s) it scopes, revocable — see the write-capability
  residual below.
- **Effort:** medium.

### Option D2 — **scoped credential kept OUTSIDE the floor** (git credential helper / proxy-side injection)
- **Closes:** the oracle AND the exfiltration surface of D1 — the entity never holds the secret; a
  host-side credential helper or an egress proxy injects/masks it (Claude Code's `injectHosts` + token
  mask). This is the **actual ecosystem convergence** and a direct step toward E.
- **Breaks:** needs the helper/proxy plumbing (more than D1); HTTPS remotes.
- **Residual:** the injected credential still authorizes its scoped repos (see write-capability below);
  near-zero exfil surface.
- **Effort:** medium-high. **Verdict:** the right credential model. Prefer over D1 wherever the plumbing
  is affordable.

### Option E — **D2 + host-allowlisted egress proxy** (the full ecosystem-standard)
- **Closes:** D2's credential-scoping **plus** host-scoping + loopback/metadata pivots (deny-by-default
  egress). The `sandbox-runtime` model.
- **Breaks:** most build — a proxy process, allowlist config, TLS termination for masking.
- **Effort:** high. **Verdict:** the right *destination*, not a today change.

### Option F — **no remote credential in the floor at all; operator pushes**
- **Closes:** everything — no oracle, no key file, no injected token, **near-zero exfil surface**. The
  entity commits locally; the push is **surfaced to the operator**, who runs it outside the floor.
- **Breaks:** **usefulness** — no autonomous push; the operator is in the loop for every push. For an
  unattended seat this is a hard stop on the "open a PR nightly" workflow.
- **Residual:** essentially none on the credential axis (the operator's judgment is the gate).
- **Effort:** low-medium (surface the pending push through the Tray / a review step).
- **Verdict:** the **minimal grant**. Right when autonomy isn't required; too restrictive for an
  unattended seat. Include so Phill sees the whole spectrum.

### ⛔ The write-capability residual (applies to C/D1/D2/E — understated in the first draft)
A push credential is **write-capable**, and a prompt-injected entity can push **malicious code to `main`,
CI workflow files, or release tags** — a **supply-chain** vector, not a contained one. "One repo, bounded"
undersold this. Mitigations, in increasing safety:
- push only to a dedicated **entity branch** + **branch protection** on `main`;
- a **fine-grained token WITHOUT workflow permission** (can't rewrite `.github/workflows`);
- a **read-only** key with the operator performing the actual push (collapses toward F).
⚠ AND: levain's **efferent gate** (`gate.py:105`, "git push is efferent and routine") gates **COMMANDS**.
A hand-rolled client that uses a key file or a socket directly **never hits the gate** — so the gate is
not a backstop for a credential the entity physically holds (D1). D2/F keep the secret out of the entity's
hands, which is why they dominate.

---

## Migration (D/E/F are breaking for existing agent-mode operators)

Agent forwarding is the **current default** (`ssh_mode="agent"`), and external operators rely on it (e.g.
the operator in the 0.4.4/0.4.5 thread who git-pushes over the forwarded agent). Removing or replacing it
is a **breaking change**, so it does not belong in a 0.4.x patch:
- **0.4.x:** ship A (done) and B (loopback deny both modes) — non-breaking hardening; document the oracle
  residuals honestly (done).
- **0.5.0:** introduce the scoped-credential model (D2 preferred, D1 as the low-plumbing fallback), make
  the operator's-agent forwarding **opt-in / deprecated**, and ⚠ **decide raw mode explicitly** (see below).
  Provide a migration note: how to mint a deploy key / fine-grained token per entity, and the
  credential-helper/proxy wiring for D2.
- **0.5+/E:** the egress proxy.

### ⚠ Raw mode is the biggest gap under any credential option
`ssh_mode="raw"` lets the entity **read the operator's actual `~/.ssh` private keys directly** — which is
**worse** than the forwarded agent (the agent never yields key material; raw mode yields the key itself).
"Stop forwarding `SSH_AUTH_SOCK`" does nothing about it. So the credential redesign MUST also settle raw
mode: **retire `ssh_mode="raw"`**, or **redefine it as "the scoped credential only"** (never the operator's
`~/.ssh`). Leaving raw mode as-is while fixing agent mode would move the whole problem one door over.

---

## Recommendation

1. **Keep A** (shipped) and **ship B** (loopback deny in both modes) in 0.4.x — the local-service side
   channel is real in both modes and survives every credential option, so this is fix, not scaffolding.
2. **Direction = D2** (scoped credential kept outside the floor) for 0.5, with **D1** as the fallback only
   where the helper/proxy plumbing isn't yet affordable, accepting D1's exfil residual. Either closes the
   whole `spore-755` oracle class at the root.
3. **Settle raw mode in the same change** — retire it or redefine it as scoped-credential-only.
4. **Constrain the write capability** — entity branch + branch protection, token without workflow perm.
5. **Long-term = E** (egress proxy). **Offer F** where autonomy isn't needed (minimal grant, operator pushes).
6. **NOT C** — constraints don't bind a non-cooperating client, which is our adversary.

**One-sentence frame for Phill:** we currently hand a prompt-injectable process the operator's
identity-everywhere; the fix is not a better fence around the oracle, it is replacing the oracle with a
credential that opens exactly one door — and keeping even that credential out of the entity's hands.

---

### Sources (fetched 2026-09-13; ⚠ = corrected/unverified per the desk challenge)
- Claude Code sandboxing: `code.claude.com/docs/en/sandboxing` (⚠ re-read by the desk: pre-allows no
  domains; NO built-in credential deny list, env inherited incl. credentials, `~/.ssh` protected only when
  listed; `allowUnixSockets` opt-in + Linux-seccomp, with a bypass warning; localhost default NOT
  addressed), `code.claude.com/docs/en/sandbox-environments`, `anthropic.com/engineering/claude-code-sandboxing`
- `github.com/anthropic-experimental/sandbox-runtime` — deny-by-default egress proxy, macOS Seatbelt
  denies sockets except the proxy port, resolved-address check blocks loopback/link-local. (This is
  sandbox-runtime's model, NOT the CC docs page's stated default.)
- Codex CLI: `learn.chatgpt.com/docs/sandboxing`, `learn.chatgpt.com/docs/permissions` (network OFF by
  default in workspace-write). ⚠ Codex git-auth + localhost specifics: unconfirmed.
- OpenHands: `docs.openhands.dev/.../sandboxes/overview`, `.../integrations-settings` (token env vars).
- Devin: `docs.devin.ai/onboard-devin/environment` (encrypted secrets as `$VARS`, "cannot discover").
  Aider: `aider.chat/docs/install/docker.html`. ⚠ Cursor: unverified (docs redirected).
- ssh-agent as signing oracle: `man.openbsd.org/ssh_config.5` (ForwardAgent), `man.openbsd.org/ssh-agent.1`,
  `openssh.org/agent-restrict.html`, `man.openbsd.org/ssh-add.1` (`-c`, `-h`), `openssh.org/txt/release-8.9`.
  ⚠ `AddKeysToAgent` predates 8.9 — do not attribute it to the destination-constraint work.
- git credential scoping: `git-scm.com/docs/gitcredentials`, `docs.github.com/.../managing-deploy-keys`,
  `docs.github.com/.../managing-your-personal-access-tokens`.
