# quotepath L3 r2 (input see report): research before any round 3 (uncommitted)

Round 2 not clean; findings cluster on three constructs.

## A. The hook's fail-closed boundary (codex HIGH 1, complement MED 1 + LOW 3/4, codex MED 6)
Every round has found a new exception path outside the per-call try (r0: team.toml; r1: state.json, discover;
r2: _interval's RecursionError, an unresolved symlink path). The per-path boundary is the construct.
- Kubernetes admission webhooks: one `failurePolicy: Fail` set at the CALLER decides what any webhook error means,
  https://kubernetes.io/docs/reference/access-authn-authz/extensible-admission-controllers/#failure-policy
- OPA Gatekeeper "failing closed" is the same policy, applied at the boundary rather than inside the policy code,
  https://open-policy-agent.github.io/gatekeeper/website/docs/failing-closed/
- Claude Code hooks: a hook that errors is non-blocking unless it emits a deny,
  https://docs.claude.com/en/docs/claude-code/hooks
=> DELETE the per-path handling: main() is the one boundary. For pretooluse, ANY exception, anywhere, is a deny
   when the realpath of the target (or the payload cwd) has a ledger root above it; otherwise silent.

## B. Memory bounds (codex HIGH 2, complement MED 2)
- git cat-file --batch is a stream; size via --batch-check, https://git-scm.com/docs/git-cat-file
- the bound must count every tree reference, not unique blobs (one blob can sit at many paths).
=> a BOUND, not a guard: byte-only git() output for plumbing, the limits summed per leaf, and a leaf-count cap.

## C. Two trusted records judged at different times (codex HIGH 3, MED 4)
pins.json and refs/levain/accepted are separate records advanced by separate transactions; a read and a fetch
interleave (codex HIGH 3), and repin + read + sync depend on order (codex MED 4).
- TUF keeps the client's trusted metadata as one set, updated in one verified step, never partially,
  https://theupdateframework.github.io/specification/latest/#detailed-client-workflow
- git itself: one ref transaction updates several refs atomically, https://git-scm.com/docs/git-update-ref (--stdin,
  start/prepare/commit)
=> DELETE the second record: the accepted remote sha lives INSIDE pins.json, and every advance of either (a local
   read's acceptance, a remote acceptance) is one read-judge-write under pins.lock. While a quarantine ref exists,
   reads do not advance pins (the quarantine must be resolved first); repin clears pins and is followed by
   accept-on-next-sync of the quarantined tip under the same lock.
