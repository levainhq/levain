# RETURN z2b (seat 1107-20-z2b): STOPPED AFTER THE FRAME, nothing built

Frame: `project_memory/1107-15-voice/Z2B_FRAME.md` (researched: gh GH_TOKEN, git credential helpers, GitHub App installation tokens,
fine-grained PAT; URLs inside).
- Built: nothing (the brief says stop when the frame needs a ruling). Tests: none run. No code touched.
- Why stopped: the seat must be able to CALL a host-side executor. Only an MCP server (or a TLS-rewriting proxy) is model-callable
  and outside the sandbox. That breaks the profile's "no MCP loaded" invariant and canary P0/P1b. Recommendation: one Levain-owned
  stdio MCP `levain_gh`, verbs `gh_read` (GET, two-repo path allowlist) and `git_fetch` (host git into the Levain mirror, seat then
  fetches the local mirror; network stays empty).
- Rulings for the desk/Phill: (1) MCP (a) vs proxy (b); (2) TWO tokens: a fine-grained PAT has one resource owner, so
  levainhq/levain and phillipclapham/flow cannot share one (unless levainhq is not an org: UNVERIFIED); (3) stdio MCP allowed in the seat.
- Token slot (to file Phill's hands row once ruled): directory `<levain home>/seat_gh/` (0700), one 0600 file per owner, set by
  `levain seat gh-token set --owner levainhq|phillipclapham`, token on STDIN, not argv. Command does not exist yet. Mint: read-only,
  Contents/Metadata/PRs/Issues/Actions, 90 days, one per owner. I did NOT file the needs_phill row (nothing to put in the slot yet).
- UNVERIFIED: levainhq owner type; Claude Code 2.1.319 accepting a Levain `--mcp-config` server alongside the seat's strict flags;
  proxy route entirely; any real VM run (Z2's).
