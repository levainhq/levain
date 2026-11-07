# RETURN z2b build: the seat's GitHub read door (option (a), Phill np-672fa792)

Branch seat/1107-20-z2b, committed, NOT pushed.

## Built
- `levain/seat_gh.py`: host-side executor. `gh_read` (GET only; paths = `/repos/{levainhq/levain|phillipclapham/flow}` + `contents|pulls|issues|actions(runs,jobs,workflows,artifacts only)|commits|branches`; query-key allowlist; https to api.github.com only, plain http only to loopback for tests; redirects refused; body scrubbed and capped), `git_fetch` (host git into `<home>/gh_mirror/<owner>/<name>.git`, git env rebuilt from scratch, fresh empty HOME, per-URL credential helper answering only github.com + the allowed repo's `.git` path, `http.followRedirects=false`), token store `<home>/seat_gh/<owner>` 0600 in 0700 (loosened modes, links, wrong owner = refused, never repaired), fixed-words refusals, no fallback to keychain/hosts.yml/GH_TOKEN/ssh agent, a log line per call in `push/seat_gh.jsonl`.
- `levain/seat_gh_mcp.py`: stdio MCP server `levain_gh`, tools `gh_read`, `git_fetch`; schemas have no method/host/url/token field, extra arguments refused.
- `levain/seat_profile.py`: `--mcp-config` carries exactly `levain_gh` (`-EP -B -m levain.seat_gh_mcp --home <levain home>`); `permissions.allow` names its two tools; `<home>/seat_gh` added to sandbox `denyRead` and the `Read(...)` deny beside the key directory; a root overlapping it is refused. Canary profile (no levain_home) uses `<canary prefix>/levain-home`.
- `levain/seat_canary.py`: P0 expects exactly `levain_gh` (connected); P1b fails unless the loaded servers are exactly `["levain_gh"]`.
- `levain/cli.py`: `levain seat gh-token set|clear|status` (token from STDIN only; no token argument exists; getpass on a tty). `set` GETs `/repos/<owner's repo>` from the host, takes expiry from GitHub's `github-authentication-token-expiration` header else `--expires`, stores only fingerprint (sha256[:12]) + expiry beside the token record.
- `docs/seat-profile.md`: new "Reading GitHub" section, table rows updated. Deviation from the frame: `gh_read` uses `http.client` in-process, not a `gh` child (no gh dependency, no gh config surface). No `levain seat gh-mcp` subcommand: the repo's convention for Levain MCP servers is `python -P -m <module>` (band_mcp), so I followed it.

## Tests
`tests/test_seat_gh.py` (109), plus updated/added cases in test_seat_profile, test_seat_canary, test_docs_seat_profile. Fake local HTTP server for gh_read, local bare repo (`file://`) for git_fetch, no real token, no sandbox profile run. Full run of every test importing seat_profile/seat_launch/canary code: 435 passed across test_seat_gh, _profile, _canary, _canary_probe_seat, _docs_seat_profile, _doctor_seat_profile, _seat_launch, _seat_lease, _seat_push, _seat_record.
Mutation check (34 single-line mutants of seat_gh.py, each run against test_seat_gh.py): 33 red at first. Survivors: "fetch out scrub" (fixed: new test with a branch named like the token; now red) and "store symlink S_ISREG check" (equivalent: a link fails the 0o077 test (link mode 755) and O_NOFOLLOW anyway; defence in depth, left). Covered refusals: other repo, non-GET, other host, redirect, path traversal/percent/alphabet, actions secrets/variables/runners, token in argv/env/log/return, absent, expired, loose store modes, scrub (body, git stderr, git output), helper host/protocol/repo/action, set_token check/shape.

## UNVERIFIED (the Z2 residue; needs the real run)
1. A real fine-grained token against api.github.com and github.com (the credential helper over real https; `http.followRedirects=false` against a real redirect).
2. The seat actually loads `levain_gh` on Claude Code 2.1.319: init event shows it connected, `mcp__levain_gh__*` tool names, and that `permissions.allow` suffices in headless acceptEdits (no prompt). The live canary (P0/P1b) has not been run.
3. That the sandboxed seat can `git fetch` from `<home>/gh_mirror/...` (read access to the mirror path, `file` transport in the seat's git) and cannot read `<home>/seat_gh` (denyRead measured live).
4. `github-authentication-token-expiration` header present on a fine-grained PAT response (else `--expires` is required; the command says so).
5. Whether `levainhq` is an org distinct from `phillipclapham` (assumed two owners).

## Phill's commands (two fine-grained, read-only tokens: Contents/Pull requests/Issues/Actions/Metadata read, ONE repository each, resource owner as named)
Mint each at github.com/settings/personal-access-tokens; for levainhq choose resource owner `levainhq`, repo `levain`; for the other, owner `phillipclapham`, repo `flow`. Then, token pasted at the hidden prompt (or piped on STDIN):

    levain seat gh-token set --owner levainhq --expires YYYY-MM-DD
    levain seat gh-token set --owner phillipclapham --expires YYYY-MM-DD

(`--expires` is used only if GitHub's reply carries no expiry; the date is the one on the mint page.) Check: `levain seat gh-token status`. Revoke: `levain seat gh-token clear --owner <owner>`.
