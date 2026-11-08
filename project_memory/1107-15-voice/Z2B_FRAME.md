# Z2b frame: a seat reads GitHub (gh + git fetch) without ever holding the token

Seat 1107-20-z2b, 11-07. Research first, then the frame. NOTHING BUILT: the frame needs a ruling (section 4).

## 1. The constraint, from the code
- A seat's Bash is sandboxed with an empty network allowlist (`docs/seat-profile.md`, "Network"). Z2a put the push credential on the
  HOST side: a Stop/SessionEnd command hook (outside the sandbox) runs `levain.consent.push.execute` from a Levain-owned bare mirror.
  The seat holds no credential and has no tool to ask for one.
- Any secret a sandboxed process can read, the agent can read (`env`, `cat`, a wrapper script). So the credential can only ever live on
  the far side of a boundary the model can call across but not see through. The existing boundaries: (a) command hooks (not
  model-callable), (b) MCP servers (run outside the sandbox, model-callable, fixed verbs), (c) a network proxy that rewrites headers.
- The seat profile today loads NO MCP (`--strict-mcp-config` with an empty list) and the canary asserts it (P0 "no MCP server is
  loaded", P1b "inert target").

## 2. How established tools do it (researched)
1. **gh via `GH_TOKEN`** (https://cli.github.com/manual/gh_help_environment): GH_TOKEN overrides stored credentials; `GH_CONFIG_DIR`
   relocates hosts.yml/config. Good for the HOST-side child process; fatal as the delivery to an agent (the env var is readable).
2. **git credential helpers** (https://git-scm.com/docs/gitcredentials): a helper answers `get` on stdin/stdout, per-URL sections
   (`credential.<URL>.helper`), `useHttpPath=true` passes the repo path so a helper can refuse other repos. Right shape for the fetch,
   run by the host's git, never by the seat.
3. **GitHub App installation tokens** (https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app):
   1-hour, mintable per repository set and permission subset. The best long-run answer ("short-lived by design"), but it needs an App,
   a private key held by Levain and an install on both owners: a new standing credential class. Not this slice (D8 (a) is "now").
4. **Fine-grained PAT** (https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens):
   repo-scoped, read permissions, up to 366 days.
   **FINDING: a fine-grained PAT has ONE resource owner.** `levainhq/levain` (org) and `phillipclapham/flow` (user) cannot share one
   token. D8 (a) / HANDOFF_58's "a READ-ONLY GH_TOKEN" is therefore TWO tokens, one per owner. (Not verified that levainhq is an org
   rather than a user; if it is the same owner as flow, one token suffices. Phill reads the owner field on the mint page.)

## 3. The chosen frame
**A host-side read executor, token in a host-only file, the seat calls verbs, never sees bytes of the credential.**
- Slot: one directory under the levain home, `seat_gh/`, 0700, holding one file per resource owner (`levainhq`, `phillipclapham`), 0600,
  written by a new `levain seat gh-token set --owner <o>` that reads the token from STDIN (never argv), checks it by GET /repos/<repo>
  from the host, records only a fingerprint (sha256 prefix) and expiry. The seat profile's `denyRead` and a Read rule name `seat_gh/`
  exactly as they name the launcher key directory today (one more member of that deny list).
- Executor (host side, a new `levain/seat_gh.py`, modelled on `seat_push.py`): two verbs only.
  - `gh_read`: `GET` only, path allowlist = `/repos/{levainhq/levain|phillipclapham/flow}/{contents,pulls,issues,actions,commits,branches}...`
    plus `/repos/{repo}` itself; every other path/repo/method/host refused with fixed words. It calls `gh api` (or urllib) in a child whose
    env is rebuilt from scratch: `GH_TOKEN=<token for the path's owner>`, `GH_CONFIG_DIR=<empty temp dir>`, `GH_PROMPT_DISABLED=1`, no
    keychain, no inherited `GIT_*`/`GH_*`. The token is never in argv; output is scrubbed of the token value before it is returned.
  - `git_fetch`: the host's git fetches `https://github.com/<allowed repo>.git` into the Levain-owned bare mirror (the Z2a mirror
    pattern: GIT_ALLOW_PROTOCOL=https, hooks off, `credential.helper=` reset then a per-URL helper that answers `get` from the token file
    with `useHttpPath=true`, refusing any other path). The seat then fetches from the LOCAL mirror path into its clone; the seat's
    network allowlist stays empty.
- Scope: fixed repo allowlist in code (the two repos), read-only by construction (GET; fetch), token itself read-only by Phill's mint.
- Absent/expired/unreadable token: one fixed refusal sentence, no fallback to the operator's keychain, hosts.yml or ssh agent.
- Revocation: delete the file (or `gh-token clear`); the next verb refuses. Residual: a 90-day token is not short-lived; the executor
  enforces its own recorded expiry and refuses past it.

## 4. Why I stopped (a ruling is needed)
The model must be able to CALL the executor. The only model-callable host-side channel is an MCP server (or a header-rewriting proxy).
Both change the seat profile's stated invariant "no MCP loaded" and the canary probes P0/P1b that assert it, and the profile doc's
"Network: None" row (proxy case). That is a scope change to the seat boundary, not a bolt-on. Options for the desk/Phill:
- **(a) RECOMMENDED: one Levain-owned stdio MCP server, `levain_gh`, two fixed verbs, loaded via the profile's own `--mcp-config`**
  (the seat still reads no MCP from its roots; canary P0/P1b change to "exactly the `levain_gh` server and no other"). Same family as
  D5/M4 (host verbs as MCP tools with declared fields). Smallest surface; no TLS interception; no network opened to the seat.
- (b) A Levain proxy that injects `Authorization` for api.github.com/github.com and MITMs TLS (the CH-D2 chat-egress CA/decider
  machinery is the closest existing piece, `levain/chat_egress/`). Lets plain `gh`/`git fetch` run unchanged inside the seat, but needs
  the seat to trust a CA and a non-empty allowlist: strictly larger and unmeasured on Claude Code 2.1.319's sandbox.
- (c) Keep the token readable in the seat env (D8 (a) literal). Rejected: this is exactly what the brief forbids.
Rulings to ask: (1) (a) vs (b); (2) two tokens (one per owner) rather than one; (3) a stdio MCP server is allowed in the seat profile.

## 5. Build plan once ruled (a)
`levain/seat_gh.py` (judge + executor + token store, `seat_push.py` style), `levain/seat_gh_mcp.py` (stdio server, two tools),
`seat_profile.py` (mcp_config + denyRead/Read-deny for `seat_gh/`), `cli.py` (`seat gh-token set|clear|status`), canary P0/P1b
updated, docs/seat-profile.md section, tests naming the revert line for: token never in log/state/argv/return; other-repo refusal;
non-GET refusal; absent-token fixed words with no fallback.
