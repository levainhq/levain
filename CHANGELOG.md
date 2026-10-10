# Changelog

All notable changes to Levain. Format is loosely [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses [SemVer](https://semver.org/spec/v2.0.0.html).

> **This file starts at 0.4.2.** Earlier releases were documented in commit messages only — which is itself one of the defects this release closes: an operator upgrading through 0.4.x had no surface that told them what changed underneath their install. Entries for 0.4.0 and 0.4.1 are backfilled below because they carry a behaviour change adopters needed to know about and were never told.

## [0.7.0] — 2026-10-10

The entity's own hands: on macOS and Linux, `levain setup-isolation` runs its bash as a separate user, each command in a fresh shell whose exit status comes from the operating system, and on Linux behind a network boundary of its own. Also the governed action engine (`levain.autonomic`, library only), the operator state line and the starter jar.

### Added

- **On Linux, the entity's user has a network boundary of its own.** `levain setup-isolation` adds an nftables rule on the entity's user id that refuses every IP connection it opens, except to the loopback ports you name with `--egress-port` (where its model proxy listens) and the network git that `levain ws-git` starts (`push`, `fetch`, `pull` and `ls-remote`, which run with a group nothing else of the entity's gets). The rule loads at boot and again whenever `nftables.service` starts, restarts or reloads, and `levain doctor` checks it by trying a connection as the entity's user. An administrator who rewrites the firewall (`nft flush ruleset`, firewalld, ufw) lifts it until it is loaded again; `sudo levain setup-isolation --path <entity>` puts it back. Setup refuses, by name, on a Linux host without nftables, where the kernel refuses nftables, or without systemd running as init. On macOS the entity's user has no such boundary yet.
- **`levain state`: a freeform state line beside the focus.** One line in your own words (`levain state "wiped, keep it light"`) that every Claude Code and Codex session reads at start, shown with its age in their session-start hooks, the TUI and the web masthead (an OpenHands session does not read it yet). It is shown as you wrote it, except that runs of whitespace, line breaks included, become single spaces. It is never parsed, scored or used to limit what the partner does. It expires after 8 hours (a state whose age cannot be established is dropped too), holds at most 500 characters, and `--clear` removes it. It lives in `.levain/context.json`; a web edit is a governed write of kind `operator_state`.
- **The starter jar on the dashboard masthead.** An SVG jar whose level is today's episode count against the entity's own typical day (the median of up to 14 prior days; the dashed mark is a typical day, full is twice that). A bubble rises for each episode that arrived since the last render, up to five at a time. It reads counts only, from the entity's own store; with no store or too little history the jar is drawn empty and says why. `prefers-reduced-motion` keeps the level still and drops the bubbles.
- **On Linux, bash under the sandbox no longer has your session keyring.** Every sandboxed command, yours and the entity's, starts in a new, empty session keyring, so it cannot read keys you hold there. The cost: a credential kept in the session keyring (a krb5 `KEYRING:` credential cache, a token added with `keyctl`) does not work inside Levain's bash; file-based credentials are unaffected. Each command also ends when levain does, even if levain is killed.
- **`levain setup-isolation`: the entity's bash can run as its own user.** On macOS any process can read the command line and environment of every other process running as the same user, and no sandbox rule changes that (measured 2026-10-07). After `sudo levain setup-isolation`, on macOS, headless chat turns, `--task` runs and scheduled seats execute bash as a dedicated user with no password and no login shell, so the kernel refuses those reads; files only you can read stay out of its reach too. The file editor reads and writes as that user as well, and only inside the entity's workspace; it does not follow a symlink there. The entity gets its own workspace outside your home and its own ssh key; your ssh-agent is not passed to it. On Linux the same sessions run bash as that user too, in an allowlisted view: `/usr`, `/etc` and `/opt` read-only, its workspace and home, fresh `/tmp`, `/var/tmp` and `/run`; your home, `/srv`, `/var/lib`, other disks and daemon sockets are absent, and it shares no IPC objects or session keyring with you. A crown jewel with a second name on disk (a hardlink) refuses such a session as it refuses your own bash, even when the jewel itself is outside that view: levain cannot find where the other name is, and it may be inside the view. Linux is therefore stricter than macOS, where the entity's bash still sees what its user may read. Before each command, levain refuses, naming the path, if the entity's user could write a socket, read or write a FIFO, or search an unlistable directory in `/usr`, `/etc` or `/opt` (something root creates there during a command, or a mount added mid-command, is not seen: that stays the administrator's to hold); Linux needs `python3`, and `zsh` for the file editor. The interactive REPL is unchanged (`levain run` and `levain doctor` say which user bash runs as). If sudo can no longer start that user, bash is refused rather than run as you. One such session of an entity runs at a time (a second is refused), and when it ends every process of the entity's user is stopped, including one a command moved out of its process group; if something cannot be stopped, the session says so and `ws-git` stays locked until levain exits. A session also stops anything of that user an earlier levain left running before it starts, and refuses to start if it cannot. `--undo` removes everything it created. `levain doctor` warns while an entity is not set up.
- **`levain ws-git`, `levain ws-put` and `levain ws-adopt`.** The entity's workspace belongs to the entity's user, and you change it through the entity. `ws-git` runs git there as that user, with hooks and similar settings off, and waits while the entity is running. `ws-put` copies a file in as data. `ws-adopt` imports a repository of yours, every branch, and leaves yours where it is.

- **`levain.autonomic`: the action manifest is frozen once a gate holds it.** `EfferentGate` freezes the `ActionManifest` it is built with; `register` then raises `ManifestFrozen` and the declarations are read-only. A change to them, a tightening included, means building a new manifest and a new gate (today: the next process).
- **`levain.autonomic`: the governed action engine for work that runs with no session open.** Folded in from the vagus project's efferent half (vagus is retiring as a separate package; this is the second piece of that fold, after `levain.firing` in July). It holds bindings (a standing grant to act when an event matches), the risk and trust postures with floors that no trust level can buy down, the confirm-and-timeout queue, chains, graduation and the kill check. It ships as a library only: no command, server route or default config reaches it yet, so an install behaves exactly as before. It imports nothing outside itself and the standard library.
- **The `levain.autonomic` binding registry and run journal live in one SQLite database, inside one store directory.** `BindingStore(dir)` and `RunJournal(dir)` share `dir/autonomic.db` (WAL journaling, `synchronous=FULL`, `fullfsync` on macOS; the database's sidecars and the journal's effect leases stay inside the directory, which is created private). The confinement floor denies that directory, read and write, at its conventional location `<levain home>/autonomic` (`AUTONOMIC_STORE_DIR`), as it denies `~/.anneal-memory`; it is a directory, so bash is not refused on its account. A change to standing authority and everything it implies commit in one transaction: a pause, revoke, expire, tighten, supersede or remove together with the fence that stops runs admitted before it; an admission together with a one-shot's claim. The store carries a format marker and refuses a database with another, or a database that holds tables and no marker (it is not taken over); a store deleted under a running object is refused rather than started empty. The registry derives every record's identity instead of trusting it: each row's key must be its record's `binding_id` and the seal recomputed from the record's own sealed core; a record that is not JSON, whose core does not parse, or that seals to another id makes the registry corrupt (reads return no bindings, every write refuses), and `BindingStore.integrity()` and `binding_liveness`'s `registry_corrupt` say why, so a corrupt registry is not mistaken for an empty one. Because the key is the content's own address, a revoked record keeps colliding with its grant even if its stored id is rewritten. A record whose unsealed bookkeeping does not parse keeps its identity and is skipped. **Migration direction: a registry kept in the earlier JSON file format (this package's object format or the vagus package's list) enters once, through `BindingStore.migrate_json(path)`**: it refuses a file that does not read clean (bad JSON, a duplicate key at any depth, two entries for one id, an entry whose identity does not derive) and a store that has been used (it holds or has held a registry, or any run state), both checked before anything is written; then it writes a backup beside the file, imports every record in one transaction, reads them back and compares, and records the source in the store's `meta`; the file is not read again. `add` is create-only (`False`, nothing written, for an existing id; a re-add carrying tightenings raises). `add` and `replace_atomic` write only a record that reads back.
- **`replace_atomic` states what it expects to find.** It now takes the old binding as the caller read it, not its id. Under the lock the stored old grant must exist, be live (fireable by the same check the fire view uses) and equal what the caller read, bookkeeping included; otherwise nothing is written. A revoked, expired, paused or barred grant is no longer superseded into a live one (all three reviewers of the previous round flagged it), and a grant paused, tightened or fired since the caller's read is not superseded on stale data. The optional `precondition` stays as an extra check. It returns a `ReplaceResult`, truthy when written, whose `reason` names the check that refused it.
- **A trajectory `bound` that is present must be a predicate.** `{"bound": null}` (or any non-predicate value) used to be accepted and treated as no bound, which left the prediction monitor off for a grant that appeared to ask for it. It is now refused when a binding is stored or tightened, and if one reaches the fire path anyway the monitor reads it as diverged and stops the fire. Leaving the key out is still how a trajectory says it is descriptive only. When a binding's guards carry several trajectories, every declared bound must hold; a descriptive trajectory earlier in the list no longer hides a bound added after it.
- **`levain.autonomic.journal`: a durable run journal, wired into the fire path.** (New in this release; an earlier file-based design of it never shipped, so there is nothing to migrate from.) Every binding fire is a journaled run, with no other mode: the gate and the binding store take one `RunJournal` (one store directory), and the gate refuses a binding fire without a run, as `FireDispatcher` and `ChainExecutor` refuse a gate without a journal. A run's id is a content address over the binding and the exact event, so delivering the same event again resumes the same run, and each link of a chain is one effect. An effect runs at most once, also across processes delivering the same event at once: a done effect replays its recorded result (no second send, no second graduation count, and a receipt that never landed is written then, from the provenance recorded with the effect, not the new delivery's); an effect whose process died, or whose executor raised or returned something that is not a result, is poisoned and never retried, only listed by `poisoned()`; whether an effect is still in progress is read from a lock its owner holds across the call, not from a process id; a replay whose bytes differ from the recorded effect cancels the run; a store that cannot be read runs nothing. **The journal is the only durable home of a journaled decision.** A confirm-class proposal records its sealed pending AS the run's hold, one row that also carries, for a link of a chain, the state that resumes the chain; `EfferentGate.open_pendings()` and `get_pending()` read open decisions from the open holds; `resolve` is one write-once decision (a rejection's cancel of the run commits with it), so of any number of resolvers exactly one decision counts and the rest change nothing and write no receipt. Re-delivering an event while its decision is open finds the same hold and pending, so nothing is asked twice. A refusal that is not a decision (an approval without an enrolled key's signature, an approval nobody may give unattended, a run not yet admitted, a registry that cannot be read or no longer grants the run) is checked before anything is written, records nothing and leaves the decision open; an approval whose effect cannot run yet is reported as held (`approved_not_yet_run`), never as refused, runs once on the run's next delivery or resolve, and is listed by `RunJournal.approved_unrun()`; a receipt names whoever made the approval that fired; `resolve` accepts an approval from anyone but a person only for an expired cooling-off pending whose action is on the auto-fire allowlist. A link of a chain resolves only through its chain (`ChainExecutor.resume`, which reads the chain state from the hold; `ChainExecutor.sweep_timeouts` applies the silence default to expired chain links); a plain `resolve` refuses it. While a decision is open on a binding, no undecided effect of that binding runs, in that run or any other (a dispatch reports it as held, and delivering the event again after the decision resumes it). A decision whose run has been cancelled or fenced is no longer open: it can never fire, so it is not listed and holds nothing back, though a reply to it still resolves, as a stop. A kill or refusal whose cancel of the run cannot be written is reported as held, with no receipt, never as a terminal stop. Pausing, revoking, expiring, tightening, superseding or removing a binding fences its admitted runs, which stop at their next effect even if the grant is live again by then; the fence commits with the change, through any handle on the store, and every effect reads it in its own transaction. Every effect also reads the registry in that transaction and does not run if the registry does not read clean or its binding is absent or no longer fireable (a claimed one-shot counts as live for the run it was claimed for). `BindingStore.admit` checks the binding is fireable, admits the run at its current generation and claims a one-shot (with the run it was claimed for, so only that run can finish it, and a later revoke fences it) in one transaction. `binding_liveness` reports open decisions per binding, poisoned effects and approved effects not yet run (a cancelled or fenced run's approval is not listed: it will not run). `ConfirmDecision` refuses a non-bool `approved`. A `FireDispatcher` refuses a store and gate that do not share one journal and a chain executor on a different gate. A binding's pending is sealed with the risk floor it was proposed at (derived from the binding's sealed tools, where its risk comes from) and with the hold it is the decision of, so two runs proposing identical content at one clock reading get two pending ids, and a pending in a hold it does not name is rejected; a resolve re-validates against the highest of that floor, the manifest's current floor for the action name (if the manifest declares it) and the floor of the binding's tools now, re-derived from the sealed binding by the binding's risk resolver, which the gate takes as `binding_risk` (`FireDispatcher` and `ChainExecutor` refuse a gate without one, and derive a binding's risk from it when it fires: they take no risk resolver of their own, so a proposal and its resolve read the same one); a risk that cannot be re-derived refuses the decision, and so does a journaled pending sealed without a floor; a registry that cannot be read at that moment refuses nothing: the decision stays open and silence decides nothing until it reads. When the floor has risen, a reply must meet the raised rung (a signature over the raised rung; no unattended approval) or the decision stays open, silence takes the raised rung's default (a rung above cooling-off drops), and an approval given before the rise ends its run with a receipt if its effect has not started (checked in the cancel's own transaction), whether it is resumed or its event is delivered again. The journal records the rung each approval met, so an approval given at the raised rung still runs after a stop before its effect. A new proposal is made at the same raised rung. Manual (human-authority) pendings keep the pending store, which now writes durably, refuses to rewrite a file it cannot read, and resolves a duplicated id once; a binding's pending found there is refused and never fires. An event source must deliver at least once and give distinct events distinct content (an id): two deliveries with identical content are one run. Library only, like the rest of the package: no command or default config wires it yet; `levain.autonomic.db.default_store_dir()` names the conventional location, `<levain home>/autonomic`.
- **`levain.autonomic`: a person's approval is a signature, verified again where it is used; the risk fence is the risk inputs themselves; and no binding effect runs until an executor can prove confinement.** A `ConfirmDecision` from a person that approves (any decider but `on-loop`, the silence default; an absent or unknown `by` counts as a person) is authority only when it carries an SSH signature (`signer`, `signature`; namespace `levain-confirm`) by a key named in the gate's allowed-signers file (`EfferentGate(confirm_signers=...)`) over the challenge `EfferentGate.confirm_challenge(pending_id)` gives: the store's identity (a random id the store keeps in its `meta`, made when it is first opened by this version), the pending's id and its hold's id, a hash of its whole record, the risk fence and the rung the approval must meet. Without one, or with a signature by another key, in another store, for another decision, under another fence or at a lower rung, it is refused and the decision stays open (a manual pending stays in its store). `by` is a label, not authority, and `typed_proof` is gone: the signature is the elevated rung's re-authentication. A denial needs no signature. A gate with no allowed-signers file accepts no person's approval. **The signed hold:** a journaled approval is stored with its signer, signature, the exact challenge it signed and that challenge's fence (`RunJournal.decide` refuses a person's approval without them: `unsigned_approval`), and `RunJournal.effect` asks again at the effect's admission whether the decision recorded on its hold is authority NOW (`authorize`, which the gate supplies): the rung the effect needs is derived again from the current risk inputs and the decision must have met it (a rung raised by policy alone, with the inputs unchanged, counts); a person's approval must verify over the challenge rebuilt for the fence current then; a decision recorded as the silence default (`on-loop`) must still be one the gate may give unattended. The check runs outside the journal's write transaction (a verifier can take seconds; writers, pause and revoke among them, do not wait for it) and counts only if the decision and the fence are unchanged when the admission commits, else it is made again. An approval refused there (one with no stored signature, written by an earlier version of this store, which gains the columns in place, or any way but a verified resolve; one signed under another fence or below the rung needed now; an `on-loop` decision nobody may give unattended) does not run: its hold REOPENS, undecided with its signature cleared, and the resolve reports it pending again (`journal:reopened:<why>`) at the rung needed now, never approved-not-yet-run. An approval whose check cannot run (the verifier or its files unavailable, the registry unreadable) is held with the approval intact (`journal:held:unavailable:<why>`). An effect runs only under the decision its resolver acted on (`RunJournal.effect(expect=...)`, from `decision_key`): one reopened and decided again in between with any other decision is held. The verdict is used only if the risk inputs it derived the rung from are the ones read at admission. Authority is evaluated when the admission checks it: an enrolment removed from the allowed-signers file while an admission is checking does not stop that admission (enrolment is a file, not state the journal's transaction can read; a signed record of enrolments is to replace it). A risk that changes after a person signed therefore asks them again, at the rung the new inputs give, instead of ending the run. **The fence:** the risk-catalog revision (`RunJournal.revise_risk`, `risk_revision`, `ActionRequest.risk_revision`) is deleted. An effect's fence is a digest of the risk inputs its rung was decided from (the manifest's entry for the action, and the binding's risk for the link as the gate's `binding_risk` resolver derives it), and `RunJournal.effect(fence=..., fence_now=...)` reads those inputs again inside the admission's transaction: a different digest is STALE (`journal:stale`; decided again, nothing recorded), inputs that cannot be read are UNCLASSIFIED (`journal:unclassified:...`, held, not a crash). A change made any way is fenced; a change to another tool is not a reason to stop this effect. **Confinement:** the self-declared `confined` attribute is gone. No executor can prove today that it runs a binding's effect through the confinement floor as the entity's separate hands user, so every binding effect is refused up front (`executor_not_confined`): the fire path refuses before it admits a run or claims a one-shot, the gate refuses any journaled request before the journal is touched (no hold opens, no proposal is pushed), and a resolve refuses an approval before writing it (a rejection is still written). Binding effects stay refused until the M2 hands wiring exists; then the one function that says so (`levain.autonomic.gate._executor_is_confined`) changes, and nothing in configuration can change it before. The executor checked is the object called (captured once per call). **The verifier:** `ssh-keygen` runs from an absolute path (`/usr/bin/ssh-keygen`, or `EfferentGate(ssh_keygen=...)`), never found through `PATH`, only if it is a regular file owned by root and writable by neither group nor others, with a fixed environment (nothing copied from the process); `verify_signature` never raises, and `verify_signature_status` tells a refusal (`not_verified`) from a check that could not run (`unavailable`). **The allowed-signers file is a trust anchor like the binary:** it and every directory above it must be root-owned and writable by nobody else, or no signature verifies against it, so enrolling a key is an act of root. Symlinks are resolved and the resolved path is checked and used; a file or directory the verifying process's uid may write through an ACL is refused (and so is a process whose real and effective uids differ, which that check cannot answer for), and an allowed-signers file it cannot read makes a check that cannot run (the approval stays, held), not a refusal. A signed approval whose risk cannot be re-derived ends with a receipt once the journal has stopped its run for good (fenced or cancelled), and otherwise stays as it is. A manual pending's signature is verified against the record actually claimed from the store, never a separate lookup (`integrity:unverified_signature`). What the check cannot see, and the operator must hold: that an enrolled key cannot leave its hardware, and whom root lets enrol a key.

### Changed

- **Each bash command runs in a fresh shell; the directory and exported variables carry, nothing else does.** The working directory, `OLDPWD` (so `cd -` works) and exported variables carry from one command to the next, as data levain holds; functions, aliases, `set`/`shopt` options, traps, `umask`, `ulimit` and unexported variables start fresh in each command, as in a new terminal. Names that make a shell or the loader run code at startup (`BASH_ENV`, `ENV`, `PROMPT_COMMAND`, `SHELLOPTS`, `BASH_FUNC_*`, `LD_*`, `DYLD_*` and similar) never carry, and neither does a credential-shaped variable the entity exported. `exit N` now ends only that command, and a command that times out is killed with every process in its session while the shell stays usable (on macOS, a process that starts its own session, with `setsid` or a double fork, is outside that kill, as from any shell; it keeps the sandbox it inherited, and when bash runs as the entity's own user it is stopped when the session ends).

- **Chat and `levain run --task` no longer read your standard credential stores by default.** `~/.config/gh`, `~/.aws/credentials`, `~/.netrc`, `~/.git-credentials`, `~/.config/git/credentials` and, on macOS, the Keychain are now denied in every drive except the interactive `levain run` REPL, as they already were for a scheduled seat. Before, a `headless` session (a cockpit chat turn, or `--task` without `--unattended`) could read them, and reading a file is not an action the gate holds. An entity that needs `gh` or `aws` there sets `"deny_standard_creds": false` in `.levain/confinement.json`; an explicit `true` or `false` is unchanged. Measured 2026-10-07 on macOS under the real sandbox profile: a headless session's shell got `~/.config/gh/hosts.yml` before this change and is denied after it.
- **Four more token files are standard credential stores:** `~/.pypirc` and `~/.npmrc` (registry publish tokens), `~/.docker/config.json` (registry auths) and `~/.kube/config` (cluster credentials). Each held a live token, or can, and was readable by a confined entity in every drive; measured 2026-10-07 under the real profile for the first three. They follow the same default and the same `deny_standard_creds` switch as the stores above.

- **The cloud CLIs' credential caches are standard credential stores.** `~/.aws/sso/cache`, `~/.aws/cli/cache`, `~/.aws/login/cache` and `~/.aws/boto/cache` (SSO tokens and temporary role credentials), `~/.config/gcloud` (gcloud's credential databases and application-default credentials) and `~/.azure` (az's token cache and service principals) now follow the same default and the same `deny_standard_creds` switch, as do the directories `$AWS_LOGIN_CACHE_DIRECTORY`, `$CLOUDSDK_CONFIG` and `$AZURE_CONFIG_DIR` name. `~/.aws/config` and `~/.aws/cli/alias` stay readable. macOS denies them whether or not they exist. On Linux an absent one is created empty (0700) for the session, masked, and removed afterwards if it is still empty, so the entity can neither plant a config there nor read what a `gcloud auth login` or `aws sso login` you run during the session writes there; one in a place your user cannot create it is skipped, since the entity cannot create it either. The floor also follows each tool's own override of where it keeps credentials, denying the default location as well: `AWS_SHARED_CREDENTIALS_FILE`, `KUBECONFIG` (every entry), `DOCKER_CONFIG`, `GH_CONFIG_DIR`, `NETRC`, `NPM_CONFIG_USERCONFIG`, `XDG_CONFIG_HOME` (git's credential store and gh), and a `credential.helper = store --file` in the config `GIT_CONFIG_GLOBAL` names. The file given to `--api-key-file` is denied to the entity too.
- **A credential store that is a symlink is denied at the link as well as its target.** The floor added each standard store resolved only, so with a dotfile manager's `~/.kube/config -> ~/dotfiles/kube/config` the link was in no rule: on macOS the entity could remove it and create a planted `~/.kube/config` (a kubeconfig `exec` entry runs a command the next time you use `kubectl`). Each store is now denied at its literal path, at the path under your resolved home and at its target, the three spellings the `~/.ssh` files already had, and the directory holding the link is protected against rename. On Linux a symlink directly in your home (`~/.netrc`, `~/.kube`, `~/.config`, or `~/.anneal-memory` / `~/.anneal-projects`, which refused bash before) is accepted and what it points at is protected, because inside bash your home's own entries are read-only and the link cannot be replaced; a symlink deeper down that you could replace (`~/.config/gh` as a link inside a real `~/.config`) refuses bash with the reason, because a mount cannot cover a link. A link inside one of the four tool directories below is fine.
- **On Linux, an absent credential file no longer leaves a read-only empty file in your home for good.** bwrap masks a file by mounting over it, so an absent cred file got a 0444 placeholder that stayed after the session and then broke `npm login` or `docker login`. Inside bash, each of the four tool directories (`~/.kube`, `~/.docker`, `~/.aws`, `~/.config/git`) is now a read-only copy of its own listing without the credential files: existing subdirectories are still writable and other files are read-only, so an absent credential file there needs no placeholder, cannot be planted, and stays unreadable when your own `kubectl` or `aws` creates it during the session. A file added or replaced at the top of one of those directories during a session is not seen inside bash until the next one; an absent tool directory is created (0700) for the session and removed afterwards if it is still empty. Inside bash the entity cannot create or rewrite a top-level file in those directories (`aws configure set`, `kubectl config use-context`, `git config --global` with an XDG git config). Your home folder is the same kind of view inside bash (its own entries read-only, every subdirectory as writable as before, the credential files left out), so an absent `~/.netrc`, `~/.npmrc`, `~/.pypirc` or `~/.git-credentials` needs no placeholder either, and one you create during a session (a `docker login`, an `npm login`) is never readable inside bash, not even by a command already running. Where the floor still has to create something to mount over (a credential directory under an existing `~/.config`, say), levain records it in `~/.levain-runtime/floor/` (denied to the entity) and removes it once the last session using it has fully ended, if it is still what levain made; levain removes only what its own create made, never a file or directory that was merely absent when it looked. A crash's leftovers are removed at the next bash start and by `levain doctor`, which also names the ones a running session holds.

- **On Linux, every process of a bash command runs in a cgroup of its own, and stopping the command stops all of it.** Each command runs in a transient scope of your systemd user manager (`systemd-run --user --scope`, under `levain.slice`); levain kills it with `cgroup.kill` and knows it is gone when the cgroup holds no process, with no process id involved. A command can no longer outlive a levain that was killed while starting it (bwrap's parent-death signal misses that window, bubblewrap #633 and #700), and it cannot move itself into another of your cgroups (bwrap `--unshare-cgroup`). **Requires** Linux 5.14 or later, the unified cgroup v2 hierarchy, and a running systemd user manager: on a server or under cron, run `sudo levain setup-isolation` again (it now enables linger) or `sudo loginctl enable-linger $USER`. WSL2, containers, older kernels and v1 or hybrid cgroups are refused by name, and `levain doctor` says which.

- **The entity can no longer read other processes' environments through `/proc`.** On Linux, bash now runs in its own PID namespace (bwrap `--unshare-pid`), so its `/proc` lists only the sandbox's processes; before, it could read `/proc/<pid>/environ` and `cmdline` of levain and of every other process of your user, where exported tokens live. Any other `proc` filesystem mounted on the host is hidden too. Cost: inside bash, `ps`, `pgrep` and `kill` see only the sandbox's own processes. The file editor (which runs inside levain itself) refuses any path under `/proc` (or any other procfs on the host), `/dev/fd` and `/dev/stdin|stdout|stderr`, by the path as given and as it resolves, on both platforms: those reached levain's own memory and open files. It also judges each file it opens by the object actually opened, before reading or truncating it, so a link the shell swaps between the check and the open is refused rather than followed. Where bwrap cannot give bash its own PID namespace (typically inside a container whose `/proc` has masked paths), bash is refused and `levain doctor` names the cause and the fix. On Linux, levain marks itself not dumpable at startup (as ssh-agent does), so other processes of your user cannot read its memory, environment or open files through `/proc` or attach to it; attaching `py-spy` or `gdb` to levain now needs root, and levain writes no core dumps.

- **Tokens exported in the shell that starts levain are no longer readable from levain's process listing.** The kernel reports a process's environment as it was when the program started (`ps -E` on macOS, `/proc/<pid>/environ` on Linux), and changing it afterwards does not change that report, so a `GH_TOKEN` or `AWS_SECRET_ACCESS_KEY` exported in your shell was readable by every process of your user, the entity's sandbox included, for the whole session. levain now restarts itself once at launch (same process id) with only a fixed list of non-secret variables (`PATH`, `HOME`, locale, terminal, `XDG_*`, `LEVAIN_*` and similar) in that report, and hands everything else to the new image through a private file descriptor; it is back in levain's environment in memory, so model providers, proxies and CA bundles work as before. Measured 2026-10-07 on macOS: `ps -E` of a levain-style process launched with a token showed it, and after the restart did not. Every program levain starts (git, pip, anneal, your editor, service managers, the browser it opens) is now given the same list plus only the variables that program needs, for example proxies for a `git push`; `tests/test_launch.py` fails on any call in levain's own code that passes none. Programs that libraries inside levain start themselves are not covered by that test. A variable in levain's own namespaces (`LEVAIN_*`, `ANNEAL_*`, `VAGUS_*`) whose name looks like a credential (`..._TOKEN`, `..._KEY`, `..._SECRET` and the like) is carried rather than listed. If levain cannot rebuild its command line to restart itself (an unusual interpreter invocation), it refuses to start and says how to start it, rather than run with the launch environment visible. On macOS the hand-over file is created in `~/.levain-runtime/floor/carry` (denied to every entity) and removed before anything is written to it. The console script now starts at `levain.launch:main`; reinstall (`pip install -e .` or upgrade) for an existing `levain` command to pick this up.
- **`--api-key-file PATH` and `LEVAIN_API_KEY` keep the model API key off the command line** for `levain run`, `levain wrap` and `levain serve --chat`. The file is refused if other users can read or write it. `--api-key VALUE` still works: levain takes it out of its command line at launch and prints a one-line warning, because until then the value was visible to other processes (on Linux, to every user).

- **The file editor no longer follows a link it acts through, and answers about a protected path as if it were missing.** Its `insert` moved a temp file onto the target with a plain move, its directory view listed with a plain directory listing, and its existence, type, size and binary checks ran by name, all in levain's own process and outside the sandbox, so a link the entity repointed between levain's check and the act could write onto, list, or reveal the existence and size of a protected file. Each now walks the path one directory at a time without following links below the workspace, the entity directory and your home (links above those, such as macOS's `/tmp`, are yours and are resolved once), judges the directory it holds, and acts from there; a protected path reads exactly like a missing one. Cost: the editor refuses a path that runs through a symlink inside those roots, and a path outside them that runs through any symlink.
- **On Linux the entity cannot create or remove files directly in `$HOME`.** Inside bash, your home directory's own entries are read-only: nothing can be created, removed, renamed or replaced at its top level, so no new dotfile can be planted there and no link there (a dotfile manager's `~/.config`, say) can be swapped. Everything inside its subdirectories (your repos, the entity's workspace, `~/.cache`) stays writable as before. A tool that creates a new top-level file or directory in `$HOME` the first time it runs fails inside bash. If you remove or rename a top-level directory of your home while a session runs, the entity's shell closes and the next one starts on the new layout.

### Fixed

- **`levain init` writes files that parse, whatever the install path holds.** The install path, the interpreter path and the `anneal-memory` path were pasted raw into string literals. A path with a `\` or `"` produced a codex `config.toml` that codex could not read (which disables every MCP server it lists, not only levain's), a codex `hooks.json` and a claude-code `.mcp.json` that were invalid JSON, and with a `"` could add keys of its own; `init` still exited 0. The same paste put the `anneal-memory` path into every activation hook's Python source, so a `"` there, or an ordinary Windows path such as `C:\Users\...`, left the hooks unable to compile. Each slot is now escaped for the string it sits in (and, in a hook's command, for the shell's double quotes as well, in a form `levain doctor`, `verify-hooks` and `levain update` read back as the same path); a path that is not valid UTF-8 is refused by name; and levain parses the whole codex `config.toml` it is about to write, and leaves it, and `hooks.json`, as they were if its change is what would stop the file parsing. A `config.toml` that did not parse before levain touched it is written as before, with a warning. Reproduced and closed with a real `levain init`: `codex mcp list` reads such a path exactly, and both codex hooks fire from it.
- **A command can no longer forge its own completion or exit status.** The confined shell read its commands from a pipe and learned "done, with status N" from a line bash printed, so a command could read ahead in that pipe and print a status of its choosing (reproduced). Each command is now its own bash, and levain takes completion and the status from the operating system (waitpid), never from output. The old named command pipe, whose path another process of the same user could open, is gone with it, and no state file or profile file is left where another process could rewrite it: on macOS the sandbox profile is passed as text, never read from a file again between commands.
- **levain refuses to start its macOS floor with a profile that would make the kernel build a syscall mask.** A malformed syscall-mask rule in a hand-written Seatbelt profile kernel-panicked a Mac on 2026-10-07. levain never renders such a rule (a test pins it), and its one launch point now also scans the profile it is about to hand to the sandbox and refuses, before anything runs, if it finds one or a form that could compute one. A host-side PATH guard cannot see levain's launches, because levain calls the sandbox by absolute path.
- **The launch banner names every credential store the floor covers.** Its "READABLE" warning named three of the five stores (it left out both git credential files); it now reads the floor's own list.

### Known open issues

- **On Linux, bash under the sandbox can still look names up through the host's DNS resolver.** Bash has no IP network there, but unless it runs as the entity's own user the system D-Bus socket stays reachable, and through it `systemd-resolved` resolves whatever name bash asks for, on the network; the name asked for can carry data out. Measured 2026-10-10 on an Ubuntu host with this release: `resolvectl query` of a fresh name, run in that bash, was answered from the network. Bash run as the entity's user after `levain setup-isolation` gets its own empty `/run`, so these sockets are not in its view; there the resolver's D-Bus, varlink and nscd routes were closed when last measured (2026-10-09, on the build before this release). This closes before Levain 2.0.
- **On Linux, a placeholder can outlive a levain that was killed.** The empty files and directories levain creates to mask absent credential paths are released by levain itself, when a session closes or levain exits, not by the kernel. If levain is killed, they stay until the next levain launch or `levain doctor` removes them.
- **If levain cannot save its record of those placeholders (a full disk, say) as a session ends, the record can name files already removed.** The placeholders that session freed are removed before the record is saved, so the record, and the launch banner that reads it, keep naming them until a later session drops them. A session that cannot save the record as it starts is refused, as before.

## [0.6.9] — 2026-10-05

A memory fix for the reply classifier's size bound.

### Fixed

- **The 200 kB size check no longer copies a reply it will not check.** It compared the reply's UTF-8 size by encoding the whole reply first, so a very large reply cost a transient copy of itself (a 300-million-character reply: 0.03 s and about 286 MiB, measured 2026-10-05). A reply with more characters than the bound is now refused before anything is encoded; UTF-8 spends at least one byte per character, so it is over the bound in bytes too.

## [0.6.8] — 2026-10-05

The chat panel says when a model's tool call could not be read, and opens unlocked.

### Changed

- **A reply that is a model's raw tool-call syntax is no longer shown as the entity's reply.** When an open model's tool call fails to parse before it reaches levain, its markup arrives as reply text and that call does not run (glm-5.2:cloud did this on 6 of 10 runs of one file-creation prompt on 0.6.7). The panel and the REPL now show "The model tried to call a tool, but its call couldn't be read, so nothing ran. Ask again, or switch models." When other actions ran earlier in the same turn (for example an action you approved), it says instead that the last call did not run and the listed actions did. The text follows, collapsed under "What the model sent" in the panel (under levain's name, not the entity's), and escaped the way the consent box escapes a held call (printable ASCII as itself, every other character as `\u{XXXX}`). The rule: tool-call markup outside a code region is treated as a leak, including inside headings, lists and quotes; replies over 200 kB are not checked. The markup is GLM argument tags (`</arg_key><arg_value>`) or a `<tool_call>` wrapper that opens a call (JSON, GLM tags or Qwen3-Coder's `<function=...>`), and a wrapper and its JSON split across paragraphs count as one. Code regions (fenced and indented code blocks and code spans) are decided by a CommonMark parser, so markup written in code is an answer, as is markup written with an escape or an entity. A reply that is entirely function-call JSON (bare, or the whole of one fenced block) naming only the session's own tools is a leak too. A model has no reason to write tool-call markup in prose, and a reply flagged when it was quoting the markup on purpose still shows the reply's text beneath the notice. The call is never repaired or run. `levain run --task` prints the notice and the text on stderr instead of the reply on stdout; with `--quiet` the stdout payload is still the text as it arrived. The exit code is unchanged (0). The chat API's turn result carries `unreadable_call: true` for such a reply.
- **`levain serve --chat` opens the cockpit unlocked on macOS.** The browser opens with the chat token in the URL fragment (`#chat_token=...`), which a browser never sends to a server, so the token reaches no access log and no Referer. The page keeps it in that tab's `sessionStorage` and removes it from the address bar at once; a reload stays unlocked, and a server restart asks again. The link is printed on every platform (`open the cockpit, unlocked: ...`). Where the browser would be launched with the URL on its command line (Linux `xdg-open`, a browser binary, or `$BROWSER` set), or when macOS's osascript launcher fails (an SSH session, for example), levain opens the plain URL instead, because other OS users can read a command line, and they are among the callers the token keeps out: open the printed link, or paste the printed token. The per-launch token itself is unchanged.
- **New dependency: `markdown-it-py>=4,<5`**, the CommonMark parser that tells the classifier above which parts of a reply are code. An install with the `openhands` extra (what runs a chat turn) already had it, through that extra's own dependencies.
- **One click starts a session.** With one entity registered, the panel shows a "Start session" button for it; the picker appears only when there are several.
- Known limits: the browser's history keeps the link as first opened, with its fragment, and a duplicated or restored tab keeps the tab's `sessionStorage`; either works only while the same server runs. Leaked calls in other formats (MiniMax's `<minimax:tool_call>`, Llama's `<|python_tag|>`, Mistral's `[TOOL_CALLS]`, DeepSeek and Kimi tool tokens, a GLM call with no arguments, or a fragment cut off before any argument tag) are still shown as a reply. The leaked text is still captured in the entity's memory as that turn's reply. A leak in a reply over 200 kB is shown as the reply, because such replies are not checked. A `<tool_call>` written before a code block and JSON written after it are read as one call across the code, so that reply is flagged. Markup inside a link's URL or title, or inside a link reference definition, is not read. The size check encodes the whole reply first: on a 300-million-character reply that took 0.03 s and about 286 MiB of transient memory (measured 2026-10-05).

## [0.6.7] — 2026-10-05

Chat panel polish from the first click-through.

### Changed

- **Enter sends a chat message; Shift+Enter starts a new line.** Only in the message box, and not while an input method is composing a character. No key approves or rejects a held action: the decision buttons are clicks, and Enter in the reject reason only starts a new line.
- **The reject reason is a one-line field that grows as you type.** It rendered as a tall box with the placeholder centred vertically.
- **"Check what happened" (was "Re-read the session") shows what the lost request did.** After an unclear outcome, `GET /chat/session.json` now returns `last_job`, the session's most recent job that started (its kind, status, activity and result), and the panel shows which actions ran and the entity's reply, but only when that job is the request it lost track of (the same job, or one that started after the last job the page saw start). If nothing has started since, it says the request has not run (and that a delayed request could still arrive); any other record is never shown as the outcome. A consent box still comes only from the session's current held set, with the "may already have run" warning. When the token is no longer accepted, the panel says "The server restarted, so that session has ended. Your last request may or may not have run"; when the server no longer has the session, it says so (a restart, or a session that ended and was cleaned up). Both name the entity's workspace to check: a restart ends every chat session, because they live in the server's memory.
- A message is sent only by a real activation of Send (a click, or Enter or Space on the focused button) or a real Enter in the message box, never while the box is disabled or a decision is in flight; a scripted form submit sends nothing. No key approves anything by itself.
- **Approve asks once more, in place.** Approve opens a confirm row, "Run the N held actions?  [Cancel] [Run them]". Cancel takes Approve's place in the row and has the focus, so a held key lands on Cancel and a double click never reaches Run them; reaching Run them takes Tab or an arrow key, then Enter, Space or a click. Tab and the arrow keys stay inside the row while it is open, and Esc or Cancel closes it. Mouse and keyboard both work. Reject stays one step, and it comes before Approve, so Tab from the reject reason reaches Reject first.
- A "Check what happened" answers for one lost request: it is removed as soon as you send another message or decide a held action, and a check that finds the request never started does not offer another.
- Known limit: after the check reports that a lost request has not run, a request that was only delayed on the way and starts later can be read as the outcome of a later request that is also lost.

## [0.6.6] — 2026-10-05

The cockpit chat panel, with a consent box that asks you to approve the tool call itself and binds the approval to it. BREAKING for chat API clients: approve needs the current decision id.

### Added

- **A chat panel in the cockpit for `levain serve --chat`.** Enter the token the server printed (held in the page only, never stored), pick an entity, open a session, send a turn, and see the reply and tool activity.
- **What a held action asks you to approve is the tool call itself.** For each held action, the consent box (and the REPL's held-action line) shows the tool name and the call's arguments as the OpenHands runtime stored them: the mapping it builds the action from, not a rendering of the built object. The two keys the model may add to any call and the runtime drops before building (its own `security_risk` rating and its `summary`) are not shown, because they are the model's account of itself; `summary` stays shown when the tool takes it as an input (an MCP tool whose schema declares it). A one-line short form parsed from the same text is shown beside it, never instead of it. Every member of the held batch is listed, inert ones (`finish`, `think`) included, because approving runs them all. Printable ASCII is shown as itself and every other character (tab, CR, every non-ASCII letter, emoji and look-alike such as a Cyrillic `а`) as `\u{XXXX}`, with a backslash shown as `\\`, so two different calls never look the same. A call that cannot be read is marked "not shown in full" and can only be rejected (409 `undecidable` from the server; the REPL rejects too).
- **An approval binds to the bytes it was shown.** When a turn halts, the server records a SHA-256 over the ordered event id, tool name and full stored arguments of every held action. At approve it reads that again from the live conversation and refuses with 409 `stale_decision` if anything differs or cannot be read; nothing runs, the decision id is spent, and the hold can then only be rejected (`GET /chat/session.json` still returns its `pending` set, without a `decision_id`). The digest itself is never sent to a client.
- **After an unclear outcome the panel never offers a decision on its own.** A turn or decision request that does not come back as a 202 with a job, lost contact with a running job, or a job the server no longer holds all read as "the outcome of the last turn/decision is unknown; the previous request may already have run", with compose blocked and one "Re-read the session" button. Only a real click on it reads the session, and the warning stays attached to whatever that read shows: a fresh consent box if the session is still held, or an idle session ready for the next message. An error response is treated the same way, the server's own `409 stale_decision` included, because nothing the page receives proves who wrote it (a proxy can answer with a JSON error after forwarding the request). A token refusal after the page sent a change (open, turn, decision, close) or while it follows one also lets go of the session and says the outcome is unknown, never just "not accepted". `GET /chat/session.json?id=<session_id>` returns the current `decision_id` and the `pending` set for a gated session.
- Known limits: an action read back from a persisted conversation event file is trusted to match its stored call, so anyone who can edit the operator's conversation files is outside what the consent box defends; the box defends against the model's arguments, not against installed tool code (a tool runs its own code with whatever arguments it is given). Non-ASCII text is shown escaped and backslashes are doubled (the price of an unambiguous display). A reloaded page cannot resume a gated session, because the session id lives only in the page (nothing runs, and the hold stays for the API or the REPL). Any token holder with a session id may reject it. The open and close requests still read an error as a definite refusal (an open that did run leaves a session the page does not show; it ends with the server), and a decision response that arrives after its session was closed in the page is not discarded (it can write an error line into the next session on that page; nothing is approved by it).

### Changed

- **BREAKING (chat API): `POST /chat/approve` requires the current decision id in `expect`.** There is no approve-by-session-id-alone path, for the panel or any API caller: a missing `expect` is refused with 400 `decision_id_required` (and does not spend the hold), and a stale or wrong one is 409 `stale_decision`. Read the id from the halting turn's result (`decision_id`) or from `GET /chat/session.json?id=<session_id>` while the session is gated. `POST /chat/reject` still accepts a missing `expect` (rejecting runs nothing); one that names an id must match. Clients that approved by session id alone (the 0.5.4 contract) must be updated.

## [0.6.5] — 2026-10-05

A security fix for the team layer (a member could make the owner's consolidate write outside the ledger worktree; 0.6.0 to 0.6.4 are affected) and three team fixes from the nightly review.

### Security

- **A team member could make the owner's `levain team consolidate` write outside the ledger worktree (0.6.0 to 0.6.4; upgrade to fix).** The ledger branch is written by every member, and the owner's consolidate wrote `PROJECT.md` to its path in the worktree, following a symlink. A member who pushed `PROJECT.md` as a symlink therefore had the canon written over any file the owner can write (reproduced on the 0.6.4 wheel: a file outside the worktree was overwritten). The canon carries members' ruling words, so a target such as a shell startup file could turn a ruling into a command. `PROJECT.md` and `team.toml` are now written as a new regular file renamed into place, so a link there is replaced, never followed, and `team.toml` is never read through a link. Run record: flow `projects/levain/reference/team_canon_symlink_run_1005/`.

### Fixed

- **A teammate's ruling can no longer draw fake `[team]` lines into your session.** The `SessionStart` context printed each ruling's words, paths and owner exactly as recorded, so a line break in a member's ruling started a new line that read as the hook's own (`[team] ...`) in every teammate's agent context. Every line the hooks, `levain team status` and `PROJECT.md` assemble is now folded onto one line as a whole, whatever field or file name fed it (words, paths, owner, pack, author, the edited file's own name, warnings and git errors), so a line break in any of them stays inside its line. Found by the nightly review, reproduced, and pinned by a test that fails on 0.6.3.
- **`team.toml` refuses member handles that would share a ledger folder.** A handle's ledger folder drops a trailing `-` or `.`, so `ana` and `ana-` (or `ana.`) both wrote `ledger/ana/`, and adding the second member made every line the first had written read as the second's: the first member's rulings stopped being enforced, and one member could sign as the other. Handles are now compared by their folder name ignoring case (which also covers the existing case-only check), and a team whose `team.toml` holds such a pair is refused with both names. A `team.toml` holding such a pair at the tip is treated like any unusable tip: the newest valid version is used, with a warning that now also reaches the session-start context, or the hooks print a visible `ledger unavailable` line. A project name that spans lines is folded onto one line when read (older files keep loading) and refused when written. Known limits: a member renamed from `ben` to `ben-` (no pair at any one time) still shares `ledger/ben/` across the rename, and an email written as `<a@x.com>` is a different roster entry from `a@x.com` though git records both the same way.
- The `levain wrap` exit-code documentation no longer says exit 1 leaves the identity unchanged; on three save paths it is not established, and the printed message says what the store shows.

## [0.6.4] — 2026-10-05

The team view and its cockpit back-link, and `levain doctor`'s continuity headroom line.

### Added

- **`levain team view`: a read-only localhost page over the team ledger.** Four panes, one per question a lead asks: what waits on me (questions and tensions you own), where agents were stopped (acknowledgement counts per path, never per person), what is held by nobody, and what is in force (with an In Force filter and a path filter). It serves GET routes only, has no write path, binds loopback only and rides the cockpit's stylesheet and guards. `--port` (default 7450), `--cockpit-url` for the nav link, `--recheck-days`, `--ack-flag`.
- **The cockpit links to running team views.** Each running view publishes one entry under `<levain home>/team-views/` and holds an exclusive file lock on it for its whole life; the cockpit's Team control lists the entries it cannot lock (a live view holds them) and links to them. Nothing is probed over the network, and the cockpit never deletes an entry; a newly started view removes entries no view holds. A process forked from a view without exec closes its copy of the lock at once. Served only by a loopback-bound cockpit to a loopback peer. The cockpit lists at most 8 views, judged in entry-name order within a one-second budget, and says "team list may be incomplete" whenever it stopped early, could not read the directory in full, or could not judge an entry; a registry fault answers 500 and the page keeps the list it had. Known limits: the registry needs a local filesystem for the levain home (NFS and some FUSE mounts emulate the lock; there the view prints that it is not registered and keeps serving); a view that is alive but wedged is still listed, and the browser shows the hang; past 8 live views, which ones show is decided by their random entry names, not by project or age; a registry directory that can never be read (a symlink, another user's directory) shows the same "may be incomplete" note as a transient failure; a temp file left by a killed view stays in the directory (it is never listed); a thread that forks while another thread is inside `register()` can give the child a copy of the lock before the at-fork hook knows about it.
- **`levain doctor` reports each entity's continuity headroom against anneal's hard maximum.** The new `continuity headroom` line reads the entity's continuity through its store (`Store.load_continuity`, strict UTF-8, as the next wrap will) and the schema persisted there, and says how many characters are used, the bound, and how many are left (flagging under 10%). It fails when the file is over the maximum (a wrap's save is refused unless it recomposes the continuity below it), when the file cannot be read or measured, and when the path exists but is not a regular file (it is never opened). It says nothing when there is no continuity file yet. Known limits: the regular-file check and the read are two steps, so a file swapped for a FIFO between them (a type swap by a concurrent writer; wraps rename regular files into place) would block the run, because closing it means re-implementing anneal's read; and a store with completed wraps whose continuity file has gone missing is not reported here (a verified `.tmp` recovery file can hold the memory, so "missing" needs anneal's recovery check, which this line does not run). The count is anneal's own: the file's characters minus the durable section (`anneal_memory.durable.section_chars`) against `anneal_memory.schema.hard_max_chars`; nothing is re-implemented here. It needs anneal-memory 0.9.33 or later, which levain already requires.

## [0.6.3] — 2026-10-04

The framed team export, the other half of anneal-memory's `team-import` contract v2 (0.9.38, with the 0.9.39 fix).

### Changed

- **`levain team export --jsonl` now emits the framed stream** (anneal's contract v2): the header `{"anneal_team_stream":2}`, then one envelope `{"frame": <ledger file>, "n": <line number in that file>, "line": <the ledger line, as a JSON string>}` per verified entry, one unbroken run per ledger file, in file order. `anneal-memory team-import -` now requires exactly one root and one author per file, so a second root planted in one file is refused (before, anything after a first valid root in a stream was only judged by its chain). The envelope is built with `json.dumps`, so ledger text can never become structure. The SessionStart import into the member's anneal store uses the same stream. `--in-force` is unchanged and unframed (it is a reading convenience that cannot be chain-verified). **Anyone piping the old unframed export by hand keeps working**: anneal 0.9.38 still reads unframed input, with a stderr note that a second root in one file is not detected. A ledger file whose path does not fit anneal's label rule (a member can push any `*.jsonl` name under `ledger/`) is exported under an opaque `unnamed-<index>-<sha256>` label (unique by construction) rather than failing the export or the SessionStart import. Opaque labels are not stable across exports (the index is the file's position in the sorted list, so adding a ledger file that sorts earlier renumbers them); anneal's contract uses the label for grouping and report text only. Known limit, not new in 0.6.3: the ledger is rebuilt from `git log -p`, and a ledger file whose name git must quote (a space, a non-ASCII character) is not read at all, so its entries are neither enforced nor exported. The internal `levain.team.export.export_lines` is now `export_stream` (it returns the framed stream). Framing checks structure, not authenticity: the stream is still one trust unit the exporter vouches for.
- **Levain now requires anneal-memory 0.9.39 or later** (`anneal-memory>=0.9.39,<0.10`), and 0.9.39 is the version it is tested against: 0.9.38 is the first release that reads framed input, and 0.9.39 fixes a library-only regression in it (an endless blank-line iterable looped in `import_ledger`; the CLI path was not exposed). No migration-manifest entry and no schema changed, so existing installs' seeds are unchanged.

## [0.6.2] — 2026-10-04

0.6.1 was tagged and never published: CI on its tagged commit failed on Linux (a project's own commit hook ran during the replay), so what follows is 0.6.1's content plus the fix for that. No schema, id or exit-code change; the anneal floor stays at 0.9.36.

Hardening of the team layer's sync replay, from the known-open list of 0.6.0. No schema, id or exit-code change; the anneal floor stays at 0.9.36.

### Fixed

- **Team sync replay (the 0.6.0 known-open list).** When `levain team sync` has to replay local entries after a `team.toml`/`PROJECT.md` conflict:
  - git's rerere is switched off for the replay (`rerere.enabled=false`, `rerere.autoupdate=false`), so a recorded resolution can no longer turn a local entry into an apparently empty pick that was then skipped as "already upstream".
  - A pick is skipped as "already upstream" only when git itself stopped on it (exit 1, `CHERRY_PICK_HEAD` naming that very commit, nothing unmerged, nothing staged). A cherry-pick that fails for another reason (a stale `index.lock`, another pick already in progress) now stops the sync with git's own message and leaves the branch where it was, instead of failing in `cherry-pick --skip` with the wrong cause or dropping an entry.
  - Every git command levain runs on the ledger runs with the project's own git hooks disabled (`core.hooksPath=/dev/null`, set once in the one place git is called). Git on Linux runs a repository's commit, checkout and reference-transaction hooks on exactly these operations: a failing `prepare-commit-msg` made `levain team sync` fail on a conflict replay, and a hook that rewrites the index could make a real entry look like an empty pick. Found by CI on the 0.6.1 tag, not by review, and the first fix (the flag on two commands) was found incomplete by all three review lineages. The replay's rebase also no longer tries to GPG-sign (cherry-pick and levain's own commits already did not).
  - Local commits are replayed in topological order (`--topo-order`), so skewed commit clocks cannot reorder them.
  - If the replay was published but the worktree could not be moved back onto the branch, the discard warning is still printed (it used to be lost with the error), the checkout is retried forced, and only if that fails too does the error name the `git checkout -f` to run.
  - A failure while re-attaching the worktree after a failed replay no longer replaces the error that caused the replay to fail; it is reported as a warning that the worktree was left detached and that the next command re-attaches it.
  The rerere skip itself (a copied `.git` plus rerere autoupdate) was reasoned from review and not reproduced; the flags are pinned by a test that reads the git command lines. The wrong-cause, stale-marker, re-attach and post-publish cases are pinned by tests that inject the failing git result and fail on 0.6.0; the "already upstream" skip is pinned by a real git run. Known limits: if both the checkout and the forced re-attach fail, the error names the first failure only; a conflict in the replay is reported as "git could not run: <git's last line>".

## [0.6.0] — 2026-10-04

Team context: one project's decisions shared across a team of engineers, with git as the wire and a Claude Code hook that puts a recorded decision in front of the agent at the edit it governs.

### Added

- **`levain team`** (`init`, `join`, `record`, `retire`, `sync`, `status`, `verify`, `consolidate`, `export`, `install`, `doctor`, `member add`, `pack-sync`). An orphan `levain-ledger` branch in the project's repository holds `team.toml`, one append-only SHA-256-chained JSON-lines file per author per clone (`ledger/<author>/<device>.jsonl`, so two clones never write one file), and the owner-generated `PROJECT.md`. Every read and write goes through a private, locked `git worktree` of that branch under `<git common dir>/levain-team/`. Entry schema v1: `v id ts author agent session type kind mode paths owner words summary reason recheck supersedes refs pack rule_id prev hash`; types `decision constraint finding question tension ack retire`; a ruling needs an owner and the decider's own words. Refused at write: missing fields, unknown supersede targets, text that looks like a secret, field sizes above the limits anneal-memory's import enforces.
- **A Claude Code `PreToolUse` hook** (Edit|Write|MultiEdit|NotebookEdit) and a `SessionStart` line, wired by `levain team init/join/install` into `.claude/settings.local.json` of every working tree of the clone (git-excluded; it names your interpreter). Someone else's ruling denies the first edit per session with the full record as the reason, and the retry is allowed and recorded as an `ack`; `block` mode always denies; a `tension` on the path always denies; practices, findings, questions and your own rulings are context only. The hook never emits `allow` (that would bypass the user's own permission prompt). Every failure is fail-open with one visible `[team] ledger unavailable: ...` line. `LEVAIN_TEAM_SESSIONSTART_RULINGS=off` drops the per-ruling lines from the session-start context so enforcement rests on the edit-time hook.
- **Authority.** Only an entry's own author, or the `team.toml` owner, may supersede or retire it, and replacing a ruling takes a ruling (or a retire) carrying the decider's words; links that break these rules are reported and ignored, whether written through the CLI or pushed by hand. Only the owner creates the ledger, consolidates the canon, seeds pack rules or changes membership.
- **The ledger is read from git history, not the working tree**: every line ever added under `ledger/` on the `levain-ledger` ref, in the order it was added, each attributed to the author email of the commit that added it. Removing or rewriting a line changes nothing and is reported; a line added to a file that belongs to someone else (the owner, for pack files) is not enforced and is reported. Reads take no lock and always see a completed commit. If `team.toml` at the tip does not parse, the newest version that does is used, with a warning. `levain team verify` also reports `team.toml` and `PROJECT.md` changes not made by the owner of the version before them.
- **`judgment.toml` in a pack**: `[pack] name/version` plus `[[rule]] id paths kind owner words reason recheck mode`. `levain team pack-sync` (and `init --pack`) seeds the rules as entries authored `pack:<name>`; an upgrade supersedes changed rules and retires dropped ones; a rule the team retired is not resurrected by re-syncing unchanged text.
- **`levain team export --jsonl`**: every verified entry, per file in file order, for anneal-memory's team import (`--in-force` for the current view). `tests/test_team_golden.py` pins the canonical-JSON and hash bytes.
- **Optional anneal import.** At session start, if the installed anneal-memory ships `anneal_memory.team` and a store is configured (`--anneal-db`, or `<repo>/.levain/memory.db`), the ledger is imported once per ledger tree with `--link-authority <owner>`; otherwise it is skipped and `levain team doctor` says which. `LEVAIN_TEAM_ANNEAL_IMPORT=off` disables it.

- **A forged ledger line is one reported problem, never a failed read.** Lines are parsed through one bounded entry point: nesting past 32, lines past 256K characters, stray closers, CPython's integer-digit limit and lone surrogates are each refused for that line alone, its neighbours verify exactly as if it were absent, and the PreToolUse hook therefore cannot be stopped by one forged line on the `levain-ledger` branch. (A line nested ~200,000 brackets deep otherwise raises RecursionError out of every read, and has run for minutes in `json.loads` on Windows.) The misfiled-entry filter is linear in a hostile file. Review: three lineages over three rounds, each round's pre-registration in the fan-in log.

### Changed

- **Levain now requires anneal-memory 0.9.36 or later** (`anneal-memory>=0.9.36,<0.10`), and 0.9.36 is the version it is tested against: it is the first published release carrying `anneal-memory team-import`, the other end of `levain team export --jsonl`. No migration-manifest entry and no schema changed, so existing installs' seeds are unchanged.

### Known limits

- Identity is the author email on each commit, mapped through `team.toml`. Anyone can set it, so levain detects and reports; it does not authenticate. A teammate who sets their git email to the owner's can act as the owner. Authentication is the git host's job (branch protection, required signed commits); levain does not check signatures.
- Any member can record a `tension` on a broad path, which stops every edit there until its author or the owner retires it. That is deliberate (a stop is cheap to lift and expensive to miss) and visible in `levain team status`.
- `levain team init` must be run by the owner named in `--owner`. A clone whose `.git` was copied from another machine relinks its ledger worktree to itself, and two clones sharing a device id refuse to rebase rather than drop an entry: run `levain team join --new-device` in the copy.
- Bash writes are not intercepted. Two rulings that disagree are not detected; a `tension` entry is how a team marks one. A nested repository or submodule is governed only by its own ledger.
- Freshness is a fetch at session start and at most every `fetch_interval` (default 300 s) from the hook.
- POSIX only (`fcntl`). Claude Code started with `--bare` loads no settings hooks, so the team layer is absent there.
- `levain doctor` does not yet check the team layer; `levain team doctor` does.
- Needs git 2.31 or later (`rev-parse --path-format`).
- The ledger branch is kept linear (levain only rebases and cherry-picks). A merge commit pushed to it by hand is reported, and lines that exist only in a merge resolution are not read.
- If two clones that share a device id have BOTH committed entries, `levain team join --new-device` does not move the copy's already-committed entries to its new file: reset the copy's ledger to the remote (`git -C .git/levain-team/worktree reset --hard origin/levain-ledger`) and record them again.
- Two `pack-sync` runs from two different owner clones at the same time can leave two versions of one pack rule in force until the next `pack-sync` (one run at a time per clone is enforced).
- If a member's email changes in `team.toml`, lines they added under the old email stop being enforced until the change is reverted or the entries are recorded again.
- When a sync must replay local commits after a `team.toml`/`PROJECT.md` conflict: in a clone whose `.git` was copied from another (shared device id) AND that has `rerere.enabled` with `rerere.autoupdate`, a local entry can be skipped as "already upstream". Run `levain team join --new-device` in a copied clone, and do not enable rerere autoupdate for the ledger worktree.
- In that same replay, commits whose clocks were skewed can replay out of order, and a cherry-pick that fails for an unrelated reason (a stale `index.lock`) is reported with the wrong cause. The sync stops and nothing is lost; `levain team doctor` and the worktree at `.git/levain-team/worktree` show the state.
- A member with push access who recommits another member's line under their own name makes that line count as theirs, so it is not enforced, and reports it. Part of the trust boundary above.

## [0.5.12] — 2026-10-04

Levain is now tested against, and requires, anneal-memory 0.9.33, which refuses to save a continuity above a hard maximum.

### Changed

- **Levain now requires anneal-memory 0.9.33 or later** (`anneal-memory>=0.9.33,<0.10`), and 0.9.33 is the version it is tested against. 0.9.33 adds a hard maximum on the saved continuity (`anneal_memory.schema.hard_max_chars`, 1.25 times the schema's target, the Durable Facts section excluded): a save above it is refused, the refusal is written to anneal's audit trail, and the wrap stays open. It adds no migration entry, so the seed guidance is unchanged.
- **`levain wrap` names a hard-maximum refusal for what it is.** It already cancelled its own wrap and kept the rejected draft on any refused save; it used to explain every refusal as a dropped section, a mis-cited episode or a too-small memory. A refusal for size now says the memory is longer than the hard maximum, beside anneal's own message naming what to cut. In a Claude Code install the entity wraps over MCP: the refusal is the tool result it reads, and an open wrap left behind is surfaced at the next session start.

## [0.5.11] — 2026-10-04

Levain is now tested against, and requires, anneal-memory 0.9.32, so `levain doctor` and `levain update` no longer report a fresh install's anneal as ahead of the known-good version.

### Changed

- **Levain now requires anneal-memory 0.9.32 or later** (`anneal-memory>=0.9.32,<0.10`), and 0.9.32 is the version it is tested against. 0.9.32 was released while 0.5.10 shipped, so a fresh 0.5.10 install resolved it and `levain doctor` reported "anneal-memory 0.9.32 is AHEAD of this levain release's known-good 0.9.31 — untested together". 0.9.32 makes anneal's multi-statement reads one snapshot; it adds no migration entry, schema or API, so the seed guidance is unchanged.
- The auto-memory mirror recognises anneal's supersession refusal by the phrase `shares too little`, a single literal in anneal's source.

## [0.5.10] — 2026-10-04

The seed now gives Claude Code's own auto-memory a job (the operator-facing layer) instead of displacing it, and a Claude Code install mirrors every native memory write into the entity's anneal store.

### Changed

- **`seed/memory.md` now hands the harness's built-in memory the operator-facing layer.** It used to call that memory "a scratchpad for harness-operational notes only ... subordinate", and in Claude Code runs with auto-memory on, a Levain entity wrote no native memory files at all. The new wording keeps anneal-memory authoritative for the work history, decisions, the self-model and anything that must be portable or governed, and tells the entity to save the operator's preferences, shorthand, working rules and corrections in the harness's memory, alongside operational notes. A rule that shapes a work decision still gets its own anneal episode. The tie-break is unchanged: on the self-model, anneal-memory wins. An unedited `seed/memory.md` is refreshed by `levain update`; an edited one is kept and the new version staged under `.levain/pending/`.
- **The trade, stated plainly:** what the entity writes to native memory lives in Claude Code's store (`~/.claude/projects/<folder>/memory/`), which is not sovereign, not portable and not under anneal-memory's immune system. The mirror below is what brings it back under anneal.

### Added

- **The auto-memory mirror (Claude Code adapter), ON by default.** A new `activation/hooks/automemory_mirror.py`, wired as a `PostToolUse` hook on `Write|Edit` and swept at every session start, copies each change to this install's Claude Code auto-memory notes into this install's anneal store, one way. A new note becomes an episode carrying its text; an edit records an episode that supersedes the current one for that note (anneal's own `--supersedes`, so recall hides the old one); a deletion records a retraction that supersedes it; a note recreated after a deletion supersedes the retraction. Notes edited by hand or through Bash (`rm`, `sed -i`) are picked up at the next sweep, not at once. It never writes continuity and never writes back to native memory; the wrap decides what graduates. The hook does its work in a detached process and always exits 0; on a write under Claude Code's projects folder it may first run one `git rev-parse` (capped at 2 s).
  - **What is superseded is read from the store**, not from the mirror's own state file: a write supersedes every episode the store currently shows for that note. anneal accepts a second supersession of an episode that was already superseded, so an id remembered locally could otherwise leave an old note current.
  - **No backfill.** The first sweep that sees a memory folder records the hash of every note already in it and writes no episode: on a new install, on an existing one upgraded with `levain update`, and for an `autoMemoryDirectory` set later. A folder that drops out of view (a different `CLAUDE_CONFIG_DIR`, a removed setting) keeps its record, so it is not backfilled when it comes back. A note written after the session started is mirrored even when the session-start sweep runs after it.
  - **Turn it off** with `"automemory_mirror": false` in `.levain/config.json` (durable), or `LEVAIN_AUTOMEMORY_MIRROR=off` (per session; it wins over the config).
  - **What a mirrored episode looks like:** source `automemory-mirror`, tags `automemory`, `mirror` and `amem-path-<16 hex>` (one per note). A note whose frontmatter `type` is `user` or `feedback` is type `decision`, tagged `operator-rule`, with content starting `OPERATOR RULE (auto-memory mirror): memory/<file>.md`; any other note is type `context`, starting `AUTO-MEMORY NOTE (auto-memory mirror): memory/<file>.md`. An edit reads `..., REVISED (auto-memory mirror)` and a deletion `RETRACTED OPERATOR RULE` / `RETRACTED AUTO-MEMORY NOTE`. `anneal-memory --db .levain/memory.db episodes --source automemory-mirror` lists the current ones (add `--include-superseded` for the history).
  - **Which folders:** Claude Code's per-project folder for the install directory, for the git work tree it sits in and for the repository of a linked worktree (Claude Code keys auto-memory by repository, so an install inside a repository shares that repository's notes), or `$CLAUDE_CODE_PROJECT_DIR_NAME`; plus the effective `autoMemoryDirectory` when it is set in the install's `.claude/settings.local.json` or `.claude/settings.json` (an absolute or `~/` path, as Claude Code requires). A user-scope `autoMemoryDirectory` is not mirrored: one folder shared by every project would pull other projects' notes into this entity. An `autoMemoryDirectory` given by `claude --settings` or by managed (policy) settings cannot be seen by a hook and is not mirrored. With `scope: "global"`, sessions started outside the install write to other projects' folders, which are not mirrored. `MEMORY.md` (the index) is never mirrored.
  - **If the state file is lost** while the store already holds mirror episodes, the mirror stops (re-baselining then would leave an edited or deleted note current until its next edit) and writes `.levain/automemory_mirror.lost`; `levain doctor` reports it. Recovery is a person's: restore `.levain/automemory_mirror.json` from a backup (an older copy is safe) and delete the marker, or turn the mirror off. Deleting only the marker stops it again.
  - **If the very first sweep cannot read the anneal store**, it waits (it cannot tell a new install from lost state) and records when; once the store is readable, notes written since then are mirrored, not baselined. `levain doctor` reports the wait.
  - **A memory folder that is missing or cannot be listed** (an unmounted volume, a removed directory) is skipped, and nothing in it is retracted; `levain doctor` reports it when it holds mirrored notes.
- `levain doctor` reports the mirror: wired, off, start-sweep only (advisory: run `levain update`), stopped, or a last sweep that could not read the store, left notes unmirrored or could not list a memory folder.

### Known open issues

- **`levain doctor` reports the mirror wired** when a `PostToolUse` command names `automemory_mirror.py`, without checking that its matcher covers `Write`/`Edit` or that it passes `hook`; only a hand-edited `.claude/settings.json` can differ from the rendered one.
- **The first-sweep wait records one time for all folders.** A folder that cannot be listed when the store recovers is baselined, not mirrored, when it returns; a folder configured during the wait has notes written after the wait began mirrored.
- **A sweep that waits more than 60 seconds for another sweep's lock gives up**, and the note it was started for is mirrored at the next session start or memory write, not at once.

## [0.5.9] — 2026-10-04

Levain is now tested against, and requires, anneal-memory 0.9.31, so `levain doctor` and `levain update` no longer report a fresh install's anneal as ahead of the known-good version.

### Changed

- **Levain now requires anneal-memory 0.9.31 or later** (`anneal-memory>=0.9.31,<0.10`), and 0.9.31 is the version it is tested against. 0.9.31 was already what a fresh install resolved to, so `levain doctor` and `levain update` reported it as "ahead of this release's known-good, untested together"; they no longer do. Levain keeps its known-good anneal version and its floor as one number, so they move together; the one 0.9.31 behaviour Levain's own text leans on is `anneal-memory wrap-status`, which `levain wrap`'s refusal sends you to and which reads one transaction from 0.9.31 (0.9.30's could show a wrap that had been replaced with the older wrap's bound token; it is an advisory display). anneal 0.9.30 and 0.9.31 added no migration entry and changed no schema, and their API additions are additive and unused by the seed, so the seed needed no change.

### Added

- A test that fails when the pyproject floor and the known-good anneal version differ, when the anneal installed for the test is older than the known-good one, or when that anneal has a migration entry newer than the version the seed is reconciled to (the next anneal that adds one), so a release is not cut with an entry nobody has reviewed against the seed.

## [0.5.8] — 2026-10-04

`levain wrap` stops claiming things the store does not show: after a stop, a failed save, or a failure while showing the result, its messages now say only what was established (and name the command that settles the rest), `--reset` clears a wrap whose episode list anneal cannot read, and the symlink-loop warning fires on Python 3.13 as on 3.12.

### Fixed

- **`levain wrap --reset` could not clear a wrap whose episode list anneal could not read** (new in 0.5.7). Every wrap `levain wrap` opens is token-bound, so when the snapshot is unreadable `--reset` and the unattended discard now name the store's bound token and clear the wrap by that compare-and-swap, which still leaves any wrap that replaced it alone. (A wrap another program opened with a caller-supplied token is bound too, and is cleared the same way when it passes the same lock and age check.) A wrap with no token to name is refused as before, with the commands for that entity's store.
- **The wall-clock stop report claimed things the store did not show** (since 0.4.x). It said the wrap was "CANCELLED cleanly" even when the cancel had failed (the store locked by another process, so the wrap stays open), and also after a stop that landed when no wrap of that run was open. It now follows what the cancel found: cancelled (the clean wording), failed (the wrap may still be open, and what clears it), or no open wrap of this run (neither claim, pointing at the dry run and the continuity file).
- **After a failed save with nothing left in progress, `levain wrap` said the memory COMMITTED and not to re-run, even when another program had cleared the wrap and nothing was saved** (since 0.4.x). It now claims COMMITTED only with positive evidence (the last completed wrap id moved and anneal's own "preserved at" recovery text is in the error), NOT saved when its own wrap is still in progress (a refused compose) or when the id did not move and the episodes are still unconsolidated, and otherwise (including when the store cannot be read) says the store does not show whether this memory was recorded and to check with `levain wrap --dry-run`.
- **A seat's second-phase consolidate that exited non-zero was reported as leaving the memory UNCHANGED**, including exit 1 after a save that had committed (since 0.4.x). The line now points at the consolidate's own diagnostic for what was established about the memory.
- **An error raised while showing the result after the memory was saved was reported as "could not read the store" and exit 2** (since 0.4.x). It now says the memory was saved, that there is nothing to re-run, and exits 0.
- **On Python 3.13 a symlink loop at `.levain/memory.continuity.md` booted the entity seed-only without the warning Python 3.12 gives.** `Path.resolve()` stopped raising on a loop in 3.13, so the guard's warning never fired. The warning now fires on every version; the boot is the same seed-only boot.

### Changed

- The symlink-loop test is no longer skipped on Python 3.13, where CI already ran the suite.

### Known open issues

- **A peer that completes Levain's wrap and immediately opens its own, between the compose and the save, can make a failed save report "NOT saved"** (since 0.4.x). Both programs would have to act inside the same few milliseconds; the message names the wrap as refused when its memory was in fact recorded by the peer, and `levain wrap --dry-run` settles it.
- **A stop that lands before the main cleanup begins can leave the store open and the wrap lock held in that process** (since 0.4.x): while the store is being opened or the wrap lock is taken. A second stop landing while `levain wrap` is closing the store can skip releasing the lock the same way. A command-line run exits, so this matters only to code that runs `levain wrap` inside a long-lived process.
- **The hard-stop report (the backstop that terminates the process outright) always says nothing was written and that the wrap is left in progress** (since 0.4.x). It cannot inspect the store from there, so it does not know whether a wrap was open or whether the save had committed; `levain wrap --dry-run` settles it.
- **A second stop that lands while `levain wrap` is cancelling can leave the wrap open** (since 0.4.x). A Ctrl-C followed by the wall-clock stop while the cancel is running strands the wrap until the next run's discard or `--reset`.
- **`--reset` and the unattended discard cannot tell a dead wrap from another program's live one** beyond Levain's lock and the wrap's age (since 0.4.x). They clear the wrap they read by its token; whether that wrap is dead is the operator's judgement for `--reset`.
- **On a Linux host with a rootful container daemon whose directory only root can read (for example `/run/podman`), the floor refuses to give the entity bash.** That is the hardlink check refusing a crown jewel it cannot stat, by design; making the directory readable to the user is the stated remedy. Whether such a host should refuse by default is undecided.

## [0.5.7] — 2026-10-04

`levain wrap` no longer cancels a wrap that is not its own: it chooses its wrap's token itself, gives it to anneal, and cancels by it on every exit. The Claude Code install no longer tells its entity that `levain wrap` recomposes its memory. Levain now requires anneal-memory 0.9.30.

**Upgrade Levain together with anneal-memory.** Levain 0.5.7 needs anneal-memory 0.9.30 (`prepare_wrap(wrap_token=...)`), and the pip floor pulls it. `levain update` refreshes an install's `CLAUDE.md` and memory seed to the corrected wording (an edited copy is kept and the new one staged under `.levain/pending/`).

### Fixed

- **`levain wrap` now checks the schema the wrap froze, not only the one the store had before the wrap started** (listed as known open in 0.5.6). The partnership check runs again once anneal has started the wrap, on the schema anneal froze for it, and the composing model's instructions are built from that same schema. A store moved off the partnership schema before anneal read it for the wrap is refused (exit 2) and the wrap is cancelled; nothing is saved. This narrows the window, it does not close it: see Known open issues.
- **An interrupt during `levain wrap` before anneal returned the wrap's token could cancel another program's wrap** (known open since 0.4.x, listed in 0.5.6). `levain wrap` now chooses the wrap's token itself and hands it to anneal, so every cancel (Ctrl-C, the wall-clock stop, a store error) names the wrap Levain started, and anneal clears it only if it still carries that token. Discarding a prior run's stuck wrap (the unattended seat's self-heal, and `--reset`) names the token it read in the same way, so a wrap another program opened in between is left alone; one that went idle in between counts as discarded. This needs anneal-memory 0.9.30, which Levain now requires.
- **A store error after the wrap had started left that wrap open** (found in review of the change above). `levain wrap` treated every store error as happening before anything was written, so an error after anneal had started the wrap returned without cancelling it, and the next wrap found one in progress. It now cancels its own wrap first.
- **Cancelling a failed wrap could clear another program's wrap on the same store.** After a failure, `--dry-run`, an empty compose, or an unexpected status from anneal, `levain wrap` either read the wrap's token and then cleared whatever wrap was open, or cleared it without reading the token at all, so a wrap another program started in between was the one cleared. The token is now compared and the wrap cleared in one step by anneal, a wrap that is not ours is left alone, and an unexpected status cancels by Levain's own token, which clears a wrap Levain opened and never another program's.
- **A cancel that failed was reported as done.** When the store could not be written (for example, locked by another program) or its wrap state could not be confirmed (no token, or a damaged in-progress record), `levain wrap` still said the wrap had been cancelled, and `--dry-run` exited 0 with its wrap left open. It now says the wrap could not be cancelled and how to discard it, and `--dry-run` exits 1.
- **The Claude Code install no longer tells its entity that `levain wrap` recomposes its memory.** `levain wrap` refuses a Claude Code install ("not a clean OpenHands entity"); a Claude Code entity writes `.levain/memory.continuity.md` itself through the MCP `prepare_wrap`, then `save_continuity` sequence. The generated `CLAUDE.md` and the adapter README named the wrong mechanism, as did the 0.5.5 entry below (errata: that entry's "recomposes" is the entity's own wrap, not the `levain wrap` command). **Existing installs:** `levain update` refreshes the corrected `CLAUDE.md` and seed (an edited copy is kept and the new one staged under `.levain/pending/`).
- **The memory seed no longer points the entity at an "Association Context" section.** anneal names it `## Episode Associations` and usually leaves it out of the wrap package, because a wrap's own links are among episodes that have left the next window.

### Added

- A GitHub Actions workflow (`.github/workflows/test.yml`) runs the test suite on Ubuntu for Python 3.12 and 3.13 on pushes to `main` and `seat/**` branches, tags and pull requests.

### Known open issues

- **`levain wrap --reset` no longer clears a wrap whose metadata cannot be read, unless anneal calls that state partial** (new in 0.5.7). It used to clear whatever wrap was open; without a token to name, a forced clear would also take any wrap another program opened in its place. `anneal-memory --db <the entity's .levain/memory.db> wrap-status` shows the wrap, and `wrap-cancel --partial` or `wrap-cancel --wrap-token <its token>` on the same `--db` clears a dead one; Levain's message spells those commands out with the entity's store.
- **A second stop that lands while `levain wrap` is cancelling can leave the wrap open** (since 0.4.x). A Ctrl-C followed by the wall-clock stop while the cancel is running strands the wrap until the next run's discard or `--reset`.
- **A failed cancel is still reported as a clean one on the wall-clock stop** (since 0.4.x). If the store is locked by another process when `levain wrap` cancels after a timeout, the wrap stays open and the report still says it was cancelled; the next run's discard (or `--reset`) clears it.
- **If another program clears Levain's wrap while it composes, the failure message says the memory was committed** (since 0.4.x). Nothing was saved and the episodes are still unwrapped: re-run.
- **`--reset` and the unattended discard cannot tell a dead wrap from another program's live one** beyond Levain's lock and the wrap's age (since 0.4.x). They clear the wrap they read by its token; whether that wrap is dead is the operator's judgement for `--reset`.
- **An error raised after the memory was saved is reported as a store read failure** (since 0.4.x). If showing the result fails after the save committed, `levain wrap` says it could not read the store and exits 2, though the memory was saved; a re-run then finds nothing to consolidate.

## [0.5.6] — 2026-10-04

`levain wrap` keeps working for existing entities under anneal-memory 0.9.28, the composing model is asked for the sections the store actually has (including anneal 0.9.28's optional `## Durable Facts`), and Levain now requires anneal-memory 0.9.28.

**Upgrade Levain together with anneal-memory.** Levain 0.5.5 refuses `levain wrap` for every existing entity once anneal-memory 0.9.28 is installed ("this entity's store is not on the 6-section partnership schema", exit 2). Install Levain 0.5.6 with anneal-memory 0.9.28, not anneal-memory alone. Do not run the `set-schema` command that message prints just to get past it: on a partnership store it only opts the store into the new optional section.

### Known open issues

Found in review, not fixed in 0.5.6:

- **An interrupt during `levain wrap` before anneal returns the wrap's token can cancel another program's wrap** (since 0.4.x). On Ctrl-C or the wall-clock stop in that interval, `levain wrap` clears whatever wrap is open on the store, on the grounds that its own lock proves the wrap is its own. That lock keeps other `levain` processes out, not other programs using the same store (the `anneal-memory` CLI, an MCP client), so a wrap one of them started in that moment is the one cleared. The same applies to a wall-clock stop that lands while `levain wrap` is handling a store error: the wrap can be left open.
- **`levain wrap` checks the store's schema before the wrap starts, not the schema the wrap freezes** (since 0.4.x). Another process that changes the store's schema in between (an `anneal-memory set-schema` run by hand, or by another tool on the same store) is not caught, and the wrap runs under the new schema. Levain's own wrap lock does not cover other programs.
- **The dashboard matches section headings by exact case**, so a continuity written with `## durable facts` (which anneal accepts) is not shown there. Its content is unaffected.
- **A file saved while `levain update` is replacing it can be overwritten** (since 0.5.0 for hooks, `CLAUDE.md` / `AGENTS.md`, settings and `.mcp.json`; seed files join them in this release). `levain update` decides from the file as it read it and then replaces it; an editor that saves in between loses that save, and a copy replaced without a record keeps a backup taken at replace time, not decision time. Levain's install lock keeps other `levain` processes out, not editors. Close the entity's files before running `levain update`.
- **`levain doctor` can report "upgrade pending" for a Claude Code install whose living memory does load.** You see it when you have put a code block, or an HTML comment left open on its line, above the `@.levain/memory.continuity.md` line in `CLAUDE.md`. Doctor only counts that import when nothing above it could hide it from Claude Code, so it cannot confirm it, and `levain update` will not change your edited file. Move the import line above the code block or comment and the warning clears.
- **In that same layout, doctor's context-size figure leaves out the living memory**, so the total it prints is smaller than what Claude Code loads by the size of `.levain/memory.continuity.md`. Moving the import line fixes this too.
- **`levain doctor` can count the living-memory import on a line Claude Code may not read as its own.** Doctor splits `CLAUDE.md` into lines the way Python does, which also breaks at a vertical tab, a form feed, a bare carriage return and some Unicode line and space characters; Claude Code's handling of those has not been measured. A `CLAUDE.md` that levain wrote, or that you edited in an ordinary editor, contains none of them. If doctor says the memory loads and the entity does not see it, check that line for stray control characters.
- **A hardlink made after bash starts, or between the file editor's check and its open, and a copy of a crown jewel, are not caught by the hardlink check** (both platforms). The check counts a jewel's names when a shell starts and each time the editor touches a file; a copy is a different file with one name.
- **A pack `order` change alone can switch which pack's activation hook is installed, without review** (since 0.5.0). When two packs ship the same `activation/hooks/<file>`, changing only one pack's `order` makes `levain update` install the other pack's hook. A changed hook *file* is held for review; a changed *winner* is not.
- **A pack source edited and then reverted while `levain update` runs can slip its edited hook in** (since 0.5.0). It needs a concurrent writer to the pack directory during the update.
- **Codex's machine-global files are guarded by a per-install lock** (since 0.5.0). `~/.codex/hooks.json` and `config.toml` are shared by every install on the machine, so two installs updating at once can interleave on them.
- **On Linux, the localhost block does not cover unix sockets at a file path, or AF_VSOCK** (since 0.5.0). A socket at a path the floor does not deny (an ssh ControlMaster or proxy socket in `/tmp`, X11) can still reach this host's services, and inside a VM so can AF_VSOCK. The user's systemd manager and session D-Bus are refused. The ssh ControlMaster case is the same on macOS.
- **On Linux, the shell is checked before each command, not during one.** A command already running when a protected file changes, or one started in the background earlier, keeps its access while it runs; so does a command whose path changes between the check and its start; a `setsid` child survives the shell being closed. A host process that links a denied file to a path the shell can read while the shell is live is the same case: it is not watched between checks (a denied file under a hidden directory is not rechecked at all, by design, since the operator's own store changes constantly).
- **An encrypted SQLite store (SQLCipher) has no recognisable header** (since 0.5.1), so the Linux SQLite checks do not see it. A planted sidecar would need the store's key to be replayed.
- **A trust file levain cannot find is not covered**: a flow project home moved with `FLOW_PROJECT_MEMORY_HOME`, unless `$ANNEAL_MEMORY_DERIVE_TRUST` is also set for levain; and a path to the trust file through more than one symlinked directory.
- **When a trust-file refresh refuses the floor mid-session, a command the bash hand is starting at that moment can still run** until the shell finishes closing. Every later command is refused.
- **`spawn_shell(env=...)` on Linux passes that environment to `bwrap` as well as to bash**, so a loader variable such as `LD_PRELOAD` in it runs before the sandbox exists. levain's own callers pass no environment; this affects only a program that calls the API with its own.
- **The README does not mention that on Linux bash is refused when a crown jewel is a SQLite database** (since 0.5.1; the CHANGELOG entry for 0.5.1 explains it).
- **An `EntitySession` takes one driver at a time.** Driving one session from two threads at once (a turn while a rejection is being confirmed, or two turns) is not supported and can run an action nobody approved. `levain run` and `levain serve --chat` each drive a session from one place at a time (chat refuses a second request while one is running).
- **`levain serve --chat`: a tool error the model recovered from, in a turn the wall-clock stop lands on, discards that turn** (reported `timed_out`, not captured, session closed). It fails closed. A session whose open hangs keeps its slot until the server restarts.

Not run before release: a real model writing a new durable fact under `levain wrap`'s prompt (no free compose model was available; the prompt text, the guard and the save path are tested).

### Fixed

- **`levain wrap` accepts a store persisted before an optional section existed.** It compared the store's sections to anneal's current list exactly, so anneal 0.9.28's optional `## Durable Facts` made every older partnership store fail the check. It now compares the required sections only; a store missing a required one is still refused.
- **`levain wrap` asks the composing model for the sections the store actually has.** Its instructions listed six fixed headings and said all six must appear, which outranked anneal's own wrap guidance, so a store with `## Durable Facts` never had a new durable fact written to it. The list is now read from the store's schema, and an optional section is marked as optional.

### Changed

- **Levain now requires anneal-memory 0.9.28 or later** (`anneal-memory>=0.9.28,<0.10`). anneal-memory 0.9.28 adds an optional `## Durable Facts` section for facts that would change a future answer (an allergy, a commitment, a fact a later action depends on), one line each with cue words, kept until you drop one. The seed's memory guidance names the section and its line form and leaves the rules to anneal's wrap guidance, and the dashboard shows the section. A NEW entity gets the section. An EXISTING one keeps its schema until you opt it in with `anneal-memory --db .levain/memory.db set-schema partnership`; `levain update` does not do that for you, because changing a store's schema is the operator's decision.

## [0.5.5] — 2026-10-03

Claude Code installs now load the entity's living memory at the start of every session, `levain update` brings an existing install's seed files up to date, a hardlink to a crown jewel no longer gets past the floor, and Levain now requires anneal-memory 0.9.26.

### Known open issues

Found in review, not fixed in 0.5.5:

- **A file saved while `levain update` is replacing it can be overwritten** (since 0.5.0 for hooks, `CLAUDE.md` / `AGENTS.md`, settings and `.mcp.json`; seed files join them in this release). `levain update` decides from the file as it read it and then replaces it; an editor that saves in between loses that save, and a copy replaced without a record keeps a backup taken at replace time, not decision time. Levain's install lock keeps other `levain` processes out, not editors. Close the entity's files before running `levain update`.
- **`levain doctor` can report "upgrade pending" for a Claude Code install whose living memory does load.** You see it when you have put a code block, or an HTML comment left open on its line, above the `@.levain/memory.continuity.md` line in `CLAUDE.md`. Doctor only counts that import when nothing above it could hide it from Claude Code, so it cannot confirm it, and `levain update` will not change your edited file. Move the import line above the code block or comment and the warning clears.
- **In that same layout, doctor's context-size figure leaves out the living memory**, so the total it prints is smaller than what Claude Code loads by the size of `.levain/memory.continuity.md`. Moving the import line fixes this too.
- **`levain doctor` can count the living-memory import on a line Claude Code may not read as its own.** Doctor splits `CLAUDE.md` into lines the way Python does, which also breaks at a vertical tab, a form feed, a bare carriage return and some Unicode line and space characters; Claude Code's handling of those has not been measured. A `CLAUDE.md` that levain wrote, or that you edited in an ordinary editor, contains none of them. If doctor says the memory loads and the entity does not see it, check that line for stray control characters.
- **A hardlink made after bash starts, or between the file editor's check and its open, and a copy of a crown jewel, are not caught by the hardlink check** (both platforms). The check counts a jewel's names when a shell starts and each time the editor touches a file; a copy is a different file with one name.
- **A pack `order` change alone can switch which pack's activation hook is installed, without review** (since 0.5.0). When two packs ship the same `activation/hooks/<file>`, changing only one pack's `order` makes `levain update` install the other pack's hook. A changed hook *file* is held for review; a changed *winner* is not.
- **A pack source edited and then reverted while `levain update` runs can slip its edited hook in** (since 0.5.0). It needs a concurrent writer to the pack directory during the update.
- **Codex's machine-global files are guarded by a per-install lock** (since 0.5.0). `~/.codex/hooks.json` and `config.toml` are shared by every install on the machine, so two installs updating at once can interleave on them.
- **On Linux, the localhost block does not cover unix sockets at a file path, or AF_VSOCK** (since 0.5.0). A socket at a path the floor does not deny (an ssh ControlMaster or proxy socket in `/tmp`, X11) can still reach this host's services, and inside a VM so can AF_VSOCK. The user's systemd manager and session D-Bus are refused. The ssh ControlMaster case is the same on macOS.
- **On Linux, the shell is checked before each command, not during one.** A command already running when a protected file changes, or one started in the background earlier, keeps its access while it runs; so does a command whose path changes between the check and its start; a `setsid` child survives the shell being closed. A host process that links a denied file to a path the shell can read while the shell is live is the same case: it is not watched between checks (a denied file under a hidden directory is not rechecked at all, by design, since the operator's own store changes constantly).
- **An encrypted SQLite store (SQLCipher) has no recognisable header** (since 0.5.1), so the Linux SQLite checks do not see it. A planted sidecar would need the store's key to be replayed.
- **A trust file levain cannot find is not covered**: a flow project home moved with `FLOW_PROJECT_MEMORY_HOME`, unless `$ANNEAL_MEMORY_DERIVE_TRUST` is also set for levain; and a path to the trust file through more than one symlinked directory.
- **When a trust-file refresh refuses the floor mid-session, a command the bash hand is starting at that moment can still run** until the shell finishes closing. Every later command is refused.
- **`spawn_shell(env=...)` on Linux passes that environment to `bwrap` as well as to bash**, so a loader variable such as `LD_PRELOAD` in it runs before the sandbox exists. levain's own callers pass no environment; this affects only a program that calls the API with its own.
- **The README does not mention that on Linux bash is refused when a crown jewel is a SQLite database** (since 0.5.1; the CHANGELOG entry for 0.5.1 explains it).
- **An `EntitySession` takes one driver at a time.** Driving one session from two threads at once (a turn while a rejection is being confirmed, or two turns) is not supported and can run an action nobody approved. `levain run` and `levain serve --chat` each drive a session from one place at a time (chat refuses a second request while one is running).
- **`levain serve --chat`: a tool error the model recovered from, in a turn the wall-clock stop lands on, discards that turn** (reported `timed_out`, not captured, session closed). It fails closed. A session whose open hangs keeps its slot until the server restarts.

### Changed

- **Levain now requires anneal-memory 0.9.26 or later** (`anneal-memory>=0.9.26,<0.10`), so upgrading Levain also upgrades anneal-memory. anneal-memory 0.9.26 stopped using Hebbian links in recall: a pattern now surfaces through the episodes its evidence cites. Links still form at each wrap and can be inspected until they decay. The seed's memory guidance says so now. It no longer claims that co-citing keeps recall alive, or asks you to co-cite on every wrap. Citing every episode that genuinely supports a pattern is enough. Before this, a Levain install on anneal-memory 0.9.26 finished `levain update` with "anneal is AHEAD of this levain release's known-good" and one pending migration proposal (AM-HOP-RETIRED); a fresh install now reconciles cleanly. The README's description of the association graph is corrected to match.

### Fixed

- **A Claude Code install now loads its living memory at the start of every session.** The generated `CLAUDE.md` imports `.levain/memory.continuity.md`, the continuity the entity's own wrap recomposes (not `levain wrap`, which serves OpenHands entities only). Before, the session-start hook injected posture, the date, identity, focus, a wrap check and due open loops but not the continuity, which loaded only if the entity chose to read it: in a run of 8 simulated days it did so on 4 or 5, and it missed the day that carried the operator's stated intent in 2 of 3 runs. Measured on this release: a fresh install's first turn, with every tool disabled, answers a fact held only in the continuity file; without the import line it cannot. The continuity's size is the target anneal composes it to (a target, not an enforced limit) for the partnership schema (25,500 characters in anneal-memory 0.9.26) and now counts toward the `doctor` context-surface figure. **Existing installs:** `levain doctor` reports the missing import as a pending upgrade, and `levain update` adds it if `CLAUDE.md` is still the copy levain wrote (an edited one gets the new version staged under `.levain/pending/`). Until the first wrap the file does not exist; Claude Code skips the import and the session starts normally. Codex installs are unchanged (`AGENTS.md` has no import mechanism; the entity still reads `anneal://continuity`).
- **`levain update` now brings the seed's methodology files up to date on an existing install.** Before, it refreshed hooks, settings and `CLAUDE.md` but never `seed/partnership.md`, `seed/memory.md` or `seed/spore_instructions.md`, so an install kept its first release's copies; an install made by 0.5.4 and updated with this release kept 0.5.4's `memory.md`. Each of those files is now decided like the other files `levain update` refreshes: a copy you have not edited is replaced, and an edited one is kept, with this release's version put in `.levain/pending/seed/` and listed once. Installs made before this release have no record of what Levain wrote there, so a copy is treated as unedited only when its bytes equal what an earlier release shipped; the replaced copy is kept in `.levain/backups/seed/`. This covers Claude Code, Codex and OpenHands installs. `world.md` and `origin.md` (written from your interview answers) and files a pack supplies are not touched by this.
- **`levain update --ack` could mark the anneal migration proposals done against files the same run had just changed.** The ack was recorded before the pack reconcile and the adapter refresh ran, so the proposals you reviewed described files that one of those steps then replaced or held for review (a seed you had edited held with its new text at `<file>.new`, or an unedited seed or `CLAUDE.md` rewritten). The ack is now recorded last, and only on a run that changed and held nothing: otherwise update says what changed, leaves the marker where it was, and you check the proposals against the files as they are now and run `levain update --ack` again. After a Levain upgrade that usually means running it twice. A state that needs review on every run (a recorded pack whose source is gone, an unreadable pack lock) blocks the ack until it is fixed. The proposals are still shown as before.
- **A reply could be shown with broken characters while the stored episode had them repaired** (`levain run`, `--task` and `levain serve --chat`). The model chain sometimes delivers UTF-8 text decoded twice (on 0.5.4 through chat, `café —` arrived as `cafÃ©` plus `â` and two invisible control characters). Capture already repaired the provable cases; the reply shown to the person now goes through the same repair, so the characters you read match the stored episode. "Provable" is about the bytes, not intent: text that quotes mojibake on purpose (code or notes about encodings) is shown repaired, just as it is stored, and the shown reply does not mark the repair (the episode's `levain_encoding` receipt does). Text that does not match the repair's narrow pattern is left as it arrived.
- **On a case-sensitive Linux filesystem, a write-protected file whose name differs only in letter case from a denied file in the same directory is readable again** (a known open issue in 0.5.4). The floor had compared those names ignoring case, so it hid the second file as if it were the first. It now compares them exactly, which is safe because of the change below. Not changed (both fail closed): on a case-sensitive filesystem, a write-protected file under a directory whose name differs only in case from a hidden directory is still hidden, and a denied `~/.ssh/Known_Hosts` still hides a distinct `known_hosts` in ssh agent mode.
- **On Linux, every file the floor denies is now masked after every file it binds, and the plan refuses to start bash if that order is ever broken.** When two binds land on the same file, the later one wins. A read-only bind emitted after a mask could therefore put the denied file back, whenever the two paths named the same file through a spelling the floor did not compare equal (a letter-case variant on a case-insensitive volume, for example). Masking last closes that for any two spellings of the same file entry. It does not cover a hardlink, which is another file entry; see the next item. Visible side effect: a denied file inside a directory the floor hides (such as the systemd runtime directory) now shows there as an empty device entry instead of not existing; its content is still denied, and the host no longer gets an empty placeholder file for it.
- **Both platforms: a hardlink to a crown jewel no longer gets past the floor** (the 0.5.4 known open issue about hardlinks). The floor denies paths, so a hardlink planted earlier at a path it does not deny let an entity read a denied file or write a protected one: writing through a link to `~/.ssh/authorized_keys` added a line to the real file (reproduced on macOS and Linux), the file editor's `view` of a link returned a denied token, and on Linux a link to a user-owned daemon socket reached the daemon.
  - **bash:** when a shell starts, levain checks every jewel (the named jewel files and sockets, everything under the hidden directories, `~/.ssh` in agent mode, and on Linux the session bus and systemd manager sockets) and refuses bash, naming the file, when any of them has more than one name on disk, wherever the other names are. Remove the extra links (`find <volume> -samefile <file>`) to use bash again. A jewel levain cannot check (a directory made unreadable to you, or another user's directory in the way) also refuses bash. That includes container sockets in another user's runtime directory: on Linux, `su` without a fresh login keeps the first user's `XDG_RUNTIME_DIR`, and bash is refused with a message to run `unset XDG_RUNTIME_DIR`. levain cannot see what other names such a socket has.
  - **file editor:** a path that is another name for a jewel is refused, whatever its spelling (a bind alias of a file with a single name is outside this check, as it is for bash). The editor checks the path each time it is asked to touch a file.
  - **Known behaviour:** two names that are both inside the floor refuse bash too. An operator who puts a tree with internal hardlinks inside the floor (a local `git clone`, a uv or pnpm store) gets bash refused until the extra links are removed.
  - Not covered: a link another process creates after the check (while a shell is starting or live, or between the editor's check and its open of the file), and copies (a backup or a copy-on-write clone is a different file, with only one name).
  - Cost: each shell start reads the hidden directories (under 0.1 s for about 1,100 files, measured on macOS).

## [0.5.4] — 2026-10-03

Adds `levain serve --chat`, a chat with an entity through the local web server (K1 part 2), and fixes a refusal at the efferent gate that could still run the refused action.

### Known open issues

Found in review, not fixed in 0.5.4:

- **A pack `order` change alone can switch which pack's activation hook is installed, without review** (since 0.5.0). When two packs ship the same `activation/hooks/<file>`, changing only one pack's `order` makes `levain update` install the other pack's hook. A changed hook *file* is held for review; a changed *winner* is not.
- **A pack source edited and then reverted while `levain update` runs can slip its edited hook in** (since 0.5.0). It needs a concurrent writer to the pack directory during the update.
- **Codex's machine-global files are guarded by a per-install lock** (since 0.5.0). `~/.codex/hooks.json` and `config.toml` are shared by every install on the machine, so two installs updating at once can interleave on them.
- **On Linux, the localhost block does not cover unix sockets at a file path, or AF_VSOCK** (since 0.5.0). A socket at a path the floor does not deny (an ssh ControlMaster or proxy socket in `/tmp`, X11) can still reach this host's services, and inside a VM so can AF_VSOCK. The user's systemd manager and session D-Bus are refused. The ssh ControlMaster case is the same on macOS.
- **On Linux, the shell is checked before each command, not during one.** A command already running when a protected file changes, or one started in the background earlier, keeps its access while it runs; so does a command whose path changes between the check and its start; a `setsid` child survives the shell being closed. A host process that links a denied file to a path the shell can read while the shell is live is the same case: it is not watched between checks (a denied file under a hidden directory is not rechecked at all, by design, since the operator's own store changes constantly).
- **An encrypted SQLite store (SQLCipher) has no recognisable header** (since 0.5.1), so the Linux SQLite checks do not see it. A planted sidecar would need the store's key to be replayed.
- **A trust file levain cannot find is not covered**: a flow project home moved with `FLOW_PROJECT_MEMORY_HOME`, unless `$ANNEAL_MEMORY_DERIVE_TRUST` is also set for levain; and a path to the trust file through more than one symlinked directory.
- **When a trust-file refresh refuses the floor mid-session, a command the bash hand is starting at that moment can still run** until the shell finishes closing. Every later command is refused.
- **On a case-sensitive Linux filesystem, a write-protected file whose name differs only in letter case from a denied file in the same directory is hidden from the shell** (it should stay readable). This fails closed: the shell loses read access to that one file.
- **`spawn_shell(env=...)` on Linux passes that environment to `bwrap` as well as to bash**, so a loader variable such as `LD_PRELOAD` in it runs before the sandbox exists. levain's own callers pass no environment; this affects only a program that calls the API with its own.
- **The README does not mention that on Linux bash is refused when a crown jewel is a SQLite database** (since 0.5.1; the CHANGELOG entry for 0.5.1 explains it).
- **A hardlink to a denied file, at a path the floor does not deny, reads that file** (both platforms). The floor denies paths, not files, so the content is readable through the other name. The planned fix matches by file identity; it is not in 0.5.4.
- **An `EntitySession` takes one driver at a time.** Driving one session from two threads at once (a turn while a rejection is being confirmed, or two turns) is not supported and can run an action nobody approved. `levain run` and `levain serve --chat` each drive a session from one place at a time (chat refuses a second request while one is running).
- **`levain serve --chat`: a tool error the model recovered from, in a turn the wall-clock stop lands on, discards that turn** (reported `timed_out`, not captured, session closed). It fails closed. A session whose open hangs keeps its slot until the server restarts.

### Added

- **`levain serve --chat <entity>`: chat with an entity through the local web server.** Pass `--chat` once per OpenHands entity. The server holds each conversation in memory and drives its turns as jobs: `POST /chat/open {"entity": <dir name>}` starts a session, `POST /chat/turn {"session_id", "message"}` sends a message, and `GET /chat/job.json?id=` reports the job. A running job's `activity` list grows as the entity uses its tools, and the finished job carries the turn's result (reply, tool activity, error, and `exit_code` as `levain run --task` reports it). `GET /chat.json` lists the entities, and the sessions by entity and state; it never lists a session id, so an id is held only by the client that opened the session. `/chat/approve`, `/chat/reject` and `/chat/close` complete the set. `--model`, `--base-url`, `--api-key` and `--max-iterations` work as they do for `levain run` (`--max-iterations` must be at least 1). There is no chat page yet; this is the API only.
  - **The server builds every agent itself.** A request names an entity the operator passed on the command line, plus message text. It cannot supply an agent, a tool spec, a model or a drive mode; such fields in a request body are ignored. The crown-jewels floor travels in the hands' tool spec, so a client-supplied spec would be the client writing its own floor.
  - **Every chat route needs the chat token.** `levain serve --chat` makes a new token each time it starts and prints it once; send it as `X-Levain-Chat-Token`. It lives only in the server's memory and your terminal. Without it a chat route answers 403. The rest of the server stays token-free on loopback: the token is for the routes that make an entity act, because a container reaching your loopback through Docker's `host.docker.internal`, a sandboxed app or another account on the machine can reach loopback without being able to edit your files.
  - **Sessions are `headless`, so the efferent gate is armed** for any entity whose `efferent_gate` is `auto` (the default). The token shows a caller holds it, not that a person is watching, so the server does not claim a human is present. A turn that proposes an efferent action (any shell command, any file write) halts; `/chat/approve` runs the held actions, `/chat/reject` refuses them with a reason the entity sees, and a new message is refused until one of the two happens. The credential floor is the same as for an interactive session.
  - **An entity whose `confinement.json` sets `allow_localhost_outbound` or `allow_container_sockets` is refused**, at startup and again from the floor of the session once it opens. Its shell could call the chat routes itself, or from a container it starts on the host network, and approve its own held actions. Use `levain run` for it.
  - **Several entities, one process.** Each session's hands enforce its own entity's floor, and each turn is captured into its own entity's store. A session whose turn errors is marked `broken` and its shell is released; it takes no further turns.
  - **A turn stops at a wall-clock limit**, `--turn-seconds` (default 1800). At the deadline the job reports `deadline_hit: true` and the turn is asked to stop. The stop takes effect at the turn's next step boundary, so a model call or shell command already running finishes first. If that step finishes the turn and every tool call the turn made ran to an outcome, the turn is returned normally (captured, `timed_out: false`) and keeps its session. Otherwise, including when the stop cancelled any of the turn's tool calls or one of them errored, the turn is reported `timed_out` (exit code 5), nothing from it is captured, and its session is closed (marked `broken`, its shell released). Capture is not covered by the limit: a deadline that passes while a finished turn is being captured does not stop it, and a capture that hangs holds the job past the deadline. Tool calls the model asked for in the same step that had not started when the stop arrived are skipped, and are not listed in the turn's activity. A model endpoint that stalls in the middle of a call can hold a session past the deadline for as long as the model client's own timeout and retries allow. A session being closed reads `closing`, and counts toward the session limit until its shell is released. A `--turn-seconds` longer than a thread can wait (`threading.TIMEOUT_MAX`) is refused at startup.
    - `deadline_hit` stays set once the deadline passes, even when the turn completes; `timed_out` is what says whether the stop ended the turn.
  - **Loopback-only, nothing persisted.** A server with `--chat` refuses a non-loopback bind, because its chat routes have no off-box auth. The routes use the same Host check, cross-site refusal and JSON-only body rule as the write routes. A restart ends every conversation; resuming one after a restart is not offered.
  - At most four live sessions per server. When a session fails to start, the server keeps the error text, drops the exception and collects it, so tools built before the failure are released before the failure is reported.

### Fixed

- **A refused action is no longer listed as work.** After an efferent action was rejected at the gate, the turn's tool activity (the REPL's summary, `--task` output, a chat job's `tool_activity`) still showed the refused command, on the same screen as the refusal.

- **Tool activity now shows the shell command an entity runs.** A bash action used to appear as `⚙ terminal: TerminalAction` in the REPL's stream, in `--task` output and in a turn's `tool_activity`, so the person watching never saw the command. It now shows the command's first line, cut at 160 characters.

- **Rejecting a held action could still run it if the rejection was only half recorded.** `levain run`'s reject checked only that the conversation no longer read as halted. OpenHands clears the halt before it records the rejection, so a failure between the two (or a status that could not be read) left the action unanswered, and the next step executed it. The refusal now has to be confirmed: the status must read as not halted and every held action must have its rejection recorded, or the turn ends with the actions not run and the session refuses every further turn until it is restarted. Found in review; it needs an internal OpenHands failure, which an entity cannot cause.

## [0.5.3] — 2026-10-03

A patch release of crown-jewels floor fixes. On Linux, a credential replaced by a rename while a confined shell was live could be read by that shell in every release since 0.5.0; the shell now closes when anything under its floor changes. Both platforms now deny the operator's project memory and the stores a derive-trust file names.

### Known open issues

Found in review, not fixed in 0.5.3:

- **A pack `order` change alone can switch which pack's activation hook is installed, without review** (since 0.5.0). When two packs ship the same `activation/hooks/<file>`, changing only one pack's `order` makes `levain update` install the other pack's hook. A changed hook *file* is held for review; a changed *winner* is not.
- **A pack source edited and then reverted while `levain update` runs can slip its edited hook in** (since 0.5.0). It needs a concurrent writer to the pack directory during the update.
- **Codex's machine-global files are guarded by a per-install lock** (since 0.5.0). `~/.codex/hooks.json` and `config.toml` are shared by every install on the machine, so two installs updating at once can interleave on them.
- **On Linux, the localhost block does not cover unix sockets at a file path, or AF_VSOCK** (since 0.5.0). A socket at a path the floor does not deny (an ssh ControlMaster or proxy socket in `/tmp`, X11) can still reach this host's services, and inside a VM so can AF_VSOCK. The user's systemd manager and session D-Bus are refused. The ssh ControlMaster case is the same on macOS.
- **On Linux, the shell is checked before each command, not during one.** A command already running when a protected file changes, or one started in the background earlier, keeps its access while it runs; so does a command whose path changes between the check and its start; a `setsid` child survives the shell being closed.
- **An encrypted SQLite store (SQLCipher) has no recognisable header** (since 0.5.1), so the Linux SQLite checks do not see it. A planted sidecar would need the store's key to be replayed.
- **A trust file levain cannot find is not covered**: a flow project home moved with `FLOW_PROJECT_MEMORY_HOME`, unless `$ANNEAL_MEMORY_DERIVE_TRUST` is also set for levain; and a path to the trust file through more than one symlinked directory.
- **When a trust-file refresh refuses the floor mid-session, a command the bash hand is starting at that moment can still run** until the shell finishes closing. Every later command is refused.
- **On a case-sensitive Linux filesystem, a write-protected file whose name differs only in letter case from a denied file in the same directory is hidden from the shell** (it should stay readable). This fails closed: the shell loses read access to that one file.
- **`spawn_shell(env=...)` on Linux passes that environment to `bwrap` as well as to bash**, so a loader variable such as `LD_PRELOAD` in it runs before the sandbox exists. levain's own callers pass no environment; this affects only a program that calls the API with its own.
- **The README does not mention that on Linux bash is refused when a crown jewel is a SQLite database** (since 0.5.1; the CHANGELOG entry for 0.5.1 explains it).


### Fixed

- **On Linux, a write-protected path that is a symlink into a hidden crown-jewel directory no longer exposes the file it points at.** The floor protected such a path by mounting its target read-only over itself, and that mount brought the hidden file back into view. Run on Linux: a derive-trust symlink in a directory the user cannot write, pointing into another entity's store, let the confined shell read that entity's file. The target is now covered with an empty file instead.
- **The crown-jewels floor now denies the operator's project memory, `~/.anneal-projects`, on both macOS and Linux**, reading and writing, the way it already denied `~/.anneal-memory`. That directory holds per-project anneal stores and the derive-trust file that says which repo roots a store re-derives its claims against. A confined shell could read and write both (run on macOS in all three drive modes): it could rebind a trust label, or write into a project store whose memory is later loaded into an operator session. An entity can no longer read project memory either; none needed to.
  - If `$ANNEAL_MEMORY_DERIVE_TRUST` is set in levain's own environment when the floor is built, that file is write-denied at the path given, at the link's own location when the path runs through a symlinked directory, and at its target; its directory cannot be renamed. A relative value is resolved against levain's working directory, which may not be the one the reader uses. The variable is read from levain's environment only: a value set just for another program (flow sets it only for its project-memory command) protects nothing here, and the default location is what is covered.
  - **Project stores named in a trust file are denied wherever they are.** levain reads the trust files anneal itself would trust (the same symlink, owner and permission rules, entries with both `db` and `root`): `$ANNEAL_MEMORY_DERIVE_TRUST` if set in levain's own environment, anneal's default `~/.anneal-memory/derive-trust.json` and flow's `~/.anneal-projects/derive-trust.json`. It denies the directory of each listed store, reading and writing, both when the floor is built and again at every shell start, so a store trusted mid-session is covered from the next shell on. A listed store's directory is denied even before it exists (on Linux bwrap creates it as it mounts). On Linux, one bwrap cannot create, such as a stale entry on an unmounted or root-owned path, refuses bash with the path named, so you remove the entry; it is never skipped, because a confined shell running as you could change its permissions. More than 256 distinct store directories refuses to start the session. A trust path that is a FIFO or a device (which anneal would read) refuses too; a directory there loads nothing, as in anneal. If a refresh fails mid-session, for any reason including a passing read error, both the shell and the file editor refuse for the rest of that conversation, and a running shell is closed at once. A missing, unreadable or rejected trust file adds nothing, and the `~/.anneal-projects` deny still holds.
  - **A listed store that cannot be hidden as a whole directory now refuses to start the session**: one that sits in `/`, a top-level or temporary directory, a directory that is or contains `$HOME`, the entity or its workspace, or a directory inside the entity or workspace (the entity's own `.levain/memory.db` excepted). anneal writes many files beside a database, so denying only some of them would leave the rest open; move such a store into a directory of its own.
  - **Not covered:** a trust file levain cannot find, which is the case for a flow project home moved with `FLOW_PROJECT_MEMORY_HOME` unless `$ANNEAL_MEMORY_DERIVE_TRUST` is also set for levain; and a path to the trust file that runs through more than one symlinked directory, whose middle links can be swapped (the same open limit as for `~/.ssh` in a symlinked home).
  - If `~/.anneal-memory` or `~/.anneal-projects` is itself a symlink, the link is now pinned, so it can no longer be removed and replaced with a planted directory. **On Linux this refuses bash while either store is a symlink**; replace the link with the real directory.
  - On Linux, a host with no `~/.anneal-projects` gets an empty one the first time a confined shell starts, because bwrap creates the directory it mounts over; it stays after the shell exits. A trust file named by the variable but missing at that moment is left as an empty read-only file, which anneal then refuses to trust until it is removed.
- **On Linux, the confined shell now closes before its next command if anything on disk changed under its floor since it started.** A Linux mount attaches to the file or directory at a path when the shell starts, not to the path, so when the host later put something new there, the mount stayed on the old one and the new one was exposed. Run on Linux: a denied credential replaced by a rename (the usual way credentials are rotated) was readable by the same live shell, **in every release since 0.5.0**; a store initialised as a database after the shell started had its `-wal` read; so did a database unlinked while its connection kept the `-wal`. Before each command levain now checks that every path the floor mounted still has the identity it had at the start (and that no SQLite sidecar has appeared beside a jewel), plus the 0.5.1 SQLite check; if not, the shell is closed (its process group is killed), the command is refused with the reason, and the next command starts a fresh shell over what is there now. A host rewriting a protected file mid-session, including `levain wrap` rewriting the entity's own memory or a daemon recreating its socket, therefore restarts the shell. **Still open:** a command that is already running when the change happens, or one started in the background earlier, keeps its access for as long as it runs, and so does a command whose path changes between the check and its start; a `setsid` child survives the kill; an encrypted store (SQLCipher) has no header to recognise. A jewel path that does not exist at spawn is left as an empty read-only file, so a program that later tries to create its database there fails ("attempt to write a readonly database") until that file is removed.
- **`levain serve` now sends its security headers on every response**, including the ones Python's HTTP server generates itself (an unsupported method such as OPTIONS or PUT, or a malformed request). Those used to go out with no Content-Security-Policy, `X-Content-Type-Options` or `X-Frame-Options`; `levain init --web` and `levain docs` already sent them.
- **`levain init --web` now holds the install lock while it refreshes the pack docs**, as `levain init` in the terminal already did. The refresh clears and recopies `.levain/docs/`; it used to run after the install had released the lock, so a `levain init` or `levain update` started at that moment could interleave its own copy. A busy install is still refused before anything is written (409, not reported as a partial install).

### Changed

- **The three local web servers share one set of request guards** (`levain.http_guards.GuardedHandler`): the security headers, the DNS-rebinding Host check, the cross-site read refusal, and the write checks (origin, JSON-only body, Content-Length, the 413 for an oversize body). Each server used to carry its own copy, and the copies had drifted (the fix above). No route or refusal changes.

## [0.5.2] — 2026-10-02

### Changed

- **Each conversation's crown-jewels floor, drive mode and entity are now one value created when the session opens, and handed to every part that enforces them.** Before this, `$LEVAIN_DRIVE_MODE` and `$LEVAIN_ENTITY_DIR` were the only channel the floor and the memory firings had. With two `EntitySession`s in one process (which no CLI path reaches yet; the server-held conversation will), we measured three crossovers, on macOS and on Linux:
  - A second session's drive mode set the first session's file-editor and bash floor. The first session's banner said one thing while its tools enforced another.
  - A second session that failed to open still moved the first session's floor.
  - Once the process named a second entity, the first session's recall, re-anchor and capture all used the second entity's store.
  The new `levain.firing.binding.ConversationBinding` holds the entity, the drive mode and the resolved floor. It reaches both hands as data in their tool spec, and the condenser carries the entity, so a fork keeps all of it and nothing is looked up by conversation. Two entities can be open in one process, and each reads and writes only its own store. `EntitySession.open` builds the hands and reads their floor back before it returns, so a session's floor cannot change after it opens. (A library caller that builds a conversation itself gets its hands at the first turn, from the tool spec as it stands then.)
- **Both hands are now one tool spec, `levain_hands`,** built by `build_entity_tools(binding, with_bash=...)`. **Breaking for library callers:** `build_entity_tools` now requires the binding, and the registry names `levain_file_editor` and `levain_bash` are gone. The tool names the model sees (`file_editor`, `terminal`) are unchanged.
- **A process that has opened an entity refuses every default-kind (`"anneal"`) store operation** (`$LEVAIN_ENTITY_PROCESS`). A stray bare `vagus_run` or `wrap_nudge` there used to be redirected to the entity; it now refuses loudly, so it can never reach the operator's `~/.anneal-memory`. Levain's own capture always names its entity and is unaffected.
- **`confinement.json` is read once at session start.** The banner and the floor come from the same read (it was read twice; listed under 0.5.0's known open issues).
- **Hands built for one entity cannot be put on another entity's agent**: `build_entity_agent` refuses them.

### Removed

- **`$LEVAIN_DRIVE_MODE` and its "one process hosts one drive mode" refusal** (`bind_drive_mode`, `current_drive_mode`, `DriveModeConflict`). Once any unattended session had opened, the process could never open an interactive one. An interactive session now opens beside an unattended one, each with its own floor.
- **`$LEVAIN_ENTITY_DIR`, `bind_entity` and the "one process hosts one entity" refusal.** `guard_entity` checks an entity without touching process state. An isolated firing or seed built with no entity now has no store, instead of reading the variable.

### Fixed

- **A session that failed to start after its conversation was built left that conversation open** until the process exited. It is now closed.
- **Forking a conversation whose hands had been built crashed** (`cannot pickle '_thread.lock' object`). A fork now gets its own hands, fenced by the same floor.
- **Installing `levain[openhands]` and `levain[app]` in two steps broke one of them.** `openhands-sdk` 1.26.0 accepts any `fastmcp>=3`, and fastmcp 4 requires mcp 2, while the MCP-App server needs mcp 1. So `[openhands]` alone pulled mcp 2.x. Adding `[app]` afterwards then broke the OpenHands import, and installing in the other order broke `levain serve-app`. Neither pip nor uv reported a problem. The `openhands` extra now also requires `mcp>=1.27,<2`, which keeps fastmcp on 3.x. Installing both extras in one command already worked and still does. The cost: `levain[openhands]` can no longer share an environment with a package that needs mcp 2 or fastmcp 4.
- **`levain serve-app` with mcp 2.x installed told you to install `levain[app]`**, which you already had. It now names the mcp version it found and what repairs it: re-installing both extras when OpenHands is installed (pinning mcp alone would break it), pinning mcp when nothing else needs mcp 2, and a separate environment when another package's fastmcp 4 does.
- **A session that failed to start no longer leaves a file-editor history directory behind**, whichever step failed. A session that closed normally already removed it.

## [0.5.1] — 2026-10-02

### Security

- **A SQLite crown jewel's sidecar files were open to the confined shell.** When a denied store is a file rather than a directory (an `~/.anneal-memory` that is a SQLite database, as on some hosts), only the main file was denied. In WAL mode, recent committed rows live in `<db>-wal` until a checkpoint, so the shell could read them and write `<db>-shm`.
  - **macOS:** the floor now also denies `-wal`, `-shm` and `-journal` beside every file-shaped jewel, by name, whether or not they exist yet.
  - **Linux:** worse in 0.5.0, and not fixable with mounts. A mount can only cover a file that exists, and SQLite creates and deletes its sidecars as it runs. With the store closed, the shell could create `<db>-wal` itself, and the host's next open replayed it into the database, so the entity could rewrite the store. A `-wal` the host creates after the shell starts (for a store in any directory, including one only a more privileged service writes) was readable. So `levain run` now **refuses bash on Linux when a crown jewel is a SQLite database**, and when a jewel cannot be read and sits in a directory you can write. The file editor still works. There is no opt-out.
  - **If your host is affected** (bash is refused with "is a SQLite database"): move the store into a directory, as `~/.anneal-memory/` normally is. Before 0.5.0 such a host had no bash at all.

### Known open issues

- **A crown-jewel path that does not exist when the shell starts, and later becomes a SQLite database, is not refused on Linux.** The check runs at spawn. Credential files and the default store (`~/.anneal-memory/`, created as a directory) are not affected.
- **On Linux, a jewel that is an empty (or not yet initialised) file when the shell starts is not refused**, and neither is one this user cannot read in a directory it cannot write. Either can become, or be, a database whose sidecars a later writer creates where the shell can see them.
- **An encrypted SQLite store (SQLCipher) has no recognisable header**, so the Linux refusal does not see it. A planted sidecar would need the store's key to be replayed.

### Fixed

- **On Linux, a deny path inside `.levain` that did not exist yet stopped bash from starting.** bwrap tried to create it inside the read-only store mount. Such a path cannot be created from inside the shell either, so it is no longer mounted.
- **`levain doctor` passed a malformed `confinement.json` when the host diagnosis itself raised.** The diagnosis ran first, and a probe error was reported as "not determinable" before the entity's config was read, so doctor stayed green for an entity `levain run` refuses. The config is now read first, so it fails the check on every host.
- **On Linux, reinstalling a seat during a turn warned "PREVIOUS definition" even when nothing changed.** The default seat runs the `levain` console script, and the kernel records a script's process as `<interpreter> <script> <args>`, so the exact comparison with the seat's command never matched. The check now compares the end of the command line, which still catches a changed task.
- **A failed periodic-to-resident reinstall on Linux left the service enabled at login.** The rollback restored the timer but did not disable the resident service that the failed install had enabled, so after the next login the service ran beside its own timer. The rollback now disables it.
- **The README told non-macOS users there was no sandboxed shell.** It was wrong from 0.5.0, which ships the Linux (`bwrap`) shell. The PyPI 0.5.0 page carries the old sentence; this release replaces it.

## [0.5.0] — 2026-10-01

**Minor, not patch: Linux becomes a supported platform** (a bubblewrap confinement floor and `systemd --user` seats). Install-truth work lands alongside it: `levain update` now refreshes the activation tree and adapter files after an upgrade.

### Known open issues in this release

Found in review, not fixed in 0.5.0, each tracked for a later release:

- **A pack `order` change alone can switch which pack's activation hook is installed, without review.** When two packs ship the same `activation/hooks/<file>`, changing only one pack's `order` in its `pack.toml` makes `levain update` install the other pack's hook. A changed hook *file* is held for review; a changed *winner* is not.
- **A pack source edited and then reverted while `levain update` runs can slip its edited hook in.** It needs a concurrent writer to the pack directory during the update.
- **Codex's machine-global files are guarded by a per-install lock.** `~/.codex/hooks.json` and `config.toml` are shared by every install on the machine, so two installs updating at once can interleave on them.
- **On Linux, the localhost block does not cover unix sockets at a file path, or AF_VSOCK.** Bash runs with no IP network, but a socket at a path the floor does not deny (an ssh ControlMaster or proxy socket in `/tmp`, D-Bus, X11) can still reach this host's services and bypass the block, and inside a VM so can AF_VSOCK. `levain run` and `levain doctor` print this. The ssh ControlMaster case is the same on macOS (named since 0.4.6).
- **`levain run` reads `.levain/confinement.json` twice at startup.** The banner is rendered from one read and the floor is built from the second, so editing that file at the very moment an entity starts can make the banner describe a different floor from the one in force. The entity cannot write the file; only an operator edit at startup can cause this.

### Changed — on macOS, an unattended seat can no longer read the Keychain

From inside the confined shell, `security find-generic-password -w` and Keychain-backed credential helpers (`git credential-osxkeychain`, the token `gh` keeps in the Keychain) used to read your secrets, in every drive mode. The macOS floor now denies direct access to the Keychain services whenever the standard credential floor is on: by default for an **unattended** seat, not while you drive `levain run` yourself. It is the same switch as `~/.config/gh` · `~/.aws/credentials` · `~/.netrc`, so `"deny_standard_creds": false` in `.levain/confinement.json` gives a specific seat its Keychain back, and `true` denies it while you drive too. Server-authenticated HTTPS and git over the forwarded ssh agent are unaffected. What breaks for a seat with the floor on: HTTPS pushes with a Keychain-stored credential, `gh`, `docker-credential-osxkeychain`, Python `keyring`, code signing, and client certificates held in the Keychain; such a seat needs the opt-out or the ssh agent. **What this does not cover:** credential stores that are not the Keychain (a running `git credential-cache` daemon, for one, answers over its own socket), and asking an unsandboxed app that is already running to read the Keychain for the entity (the same open class as the localhost and IPC limits in the confinement docs). Linux has no counterpart in this release.

### Changed — `levain doctor` sends a stale hook or carrier to `levain update`, not `init --force`

Since `update` refreshes the activation tree and adapter files (below), doctor's remedy for a hook or `CLAUDE.md` / `AGENTS.md` that predates the installed levain is `levain update --path <install>`, which keeps your edits. It used to say these fixes "do not arrive via `levain update`" and sent you to `init --force`, which re-runs the interview. `init --force` stays the remedy for an edited hook you want back to the package's version, and for an unreadable install receipt.

### Fixed — install truth

- **A pack `order` change alone now moves the installed seed file.** Previously the old winner's copy stayed installed and every later `levain update` reported clean. The lock now records which layer won each seed filename (`wins` on each pack's provenance; a lock without it is read as before), and `update` installs the new winner, backing up an edited copy first. A rendered seed whose win moved is flagged for re-onboard.
- **Two `levain init --force` runs on one install can no longer interleave.** One of them used to crash mid-swap or leave the activation receipt describing a tree that was not installed. `init` and `update` now take a per-install lock (`.levain/install.lock`), and a second run is refused at once with nothing written. A filesystem that does not support `flock` at all proceeds unguarded, with a note; any other lock error (including `ENOLCK`, e.g. NFS with lockd down) refuses with the real error.

### Added — install truth

- **`levain update` now refreshes the activation tree and the adapter files after an upgrade**: the hooks, `posture.md`, `recency_directives.md`, `CLAUDE.md` / `AGENTS.md`, `.claude/settings.json`, `.mcp.json`, and for codex the `hooks.json` / `config.toml` block that belong to this install. `pip install -U levain` no longer needs `levain init --force` after it.
  - Each file is decided on its own. A file you edited is kept.
  - If the new release changed that file too, its new version goes to `.levain/pending/` and the file is listed once for you to merge (`update` exits 1 that run).
  - On an install made before Levain recorded what it wrote, only hook scripts, and JSON files that differ solely where Levain substitutes your interpreter path, are replaced, each with a backup. Everything else is staged.
  - Codex's machine-global files are never repointed from another install.
- **`levain adopt-answers [--dry-run]`.** Reads your interview answers back out of hand-edited `world.md` / `origin.md` (and any pack's rendered seeds) and records them as `.levain/answers.json`, so a re-render from the record keeps your edits instead of reverting them.
  - All or nothing: a seed that does not read back to an answer set that renders it again is refused, with the first line that departs, and nothing is written.
  - Also refused: a drifted pack, or a field your record never answered.


### Added — Linux support: a confinement floor and a scheduled seat (K4c)

Levain's sovereign entity now runs on Linux, not only macOS. Two pieces, and the second is the one that makes an unattended seat possible at all:

- **Confinement** — a `bwrap` (bubblewrap) mount-namespace floor enforcing the same crown-jewels policy as the macOS seatbelt floor. It is a re-derivation, not a port: seatbelt is a path predicate, `bwrap` is a mount namespace with the opposite default, and every mapping was measured on a Linux kernel rather than read off a man page. Where the two differ, Linux is stronger — a write-denied file cannot be renamed out (`EBUSY`, it is a mountpoint) or hardlinked out (`EXDEV`, separate mount device), which rests on ordinary mount semantics instead of the undocumented Apple behaviour the macOS floor depends on.
- **Lifecycle** — a `systemd --user` provider (service, plus a separate `.timer` for a scheduled seat). `install` also requests `loginctl enable-linger`, without which a user unit stops when your last session ends and a headless box's seat would quietly die at logout.

**⚠ On Ubuntu 23.10 through 24.10, including 24.04 LTS, expect Levain to report no bash hands until you act.** Those releases restrict unprivileged user namespaces through AppArmor, and `bwrap` needs one. Ubuntu 25.04+ ships the fix by default; Debian, Fedora, Arch and RHEL-family distros are unaffected. Levain fails closed and says so — the entity still gets its file-editor hand, which is fully cross-platform, but no shell.

**The fix is Ubuntu's own profile, not one you write.** `levain doctor` prints the three commands, and they are in the README under *Linux*. It keeps the host-wide restriction on and is reversible. ⚠ One consequence worth knowing before you apply it: the profile stacks the sandboxed process under a child profile with no capabilities, so the entity's bash can no longer run a nested `bwrap`, rootless docker/podman, flatpak, or a browser sandbox.

**Do not infer capability from the usual two checks.** `bwrap` being installed and `kernel.unprivileged_userns_clone=1` can BOTH be true on a host where every invocation still fails — that pair reads green on a machine we measured as entirely unable to run it. Levain therefore probes by *executing* `bwrap`, and you should too.

### Fixed — the run banner told Linux operators their platform was unsupported

`levain run` printed *"bash dropped: no OS sandbox on this platform"* whenever the bash hand was unavailable. That was true while macOS was the only supported OS. It is false now in the most common Linux case — a **supported** platform whose kernel refuses to start the sandbox, which is one command away from working — and it is the sentence an operator reads before concluding Levain has no Linux support.

The banner now names the real reason and the fix, and `levain doctor` gained a **confinement floor** check reporting which floor is active or why there is none. A missing hand is reported, never failed: an entity with only its file-editor hand is a working, supported configuration, and turning a healthy install red for a missing optional hand is how an operator learns to ignore `doctor`. Both surfaces read one shared diagnosis.

### Fixed — `confinement_supported()` could not have reported a second platform correctly

It selected a provider polymorphically and then checked the macOS driver unconditionally. With one provider that was invisible; with two it fails in both directions — a working Linux floor would never be offered, and asked about Linux from a Mac it would answer yes from a host that cannot know. The driver check now travels with the provider.

### Fixed, before release — the Linux floor as first written let containers through, broke the entity's own memory, and could not start on most hosts

Found by running it on a Linux kernel while bringing K4c onto this release line, and by review:

- **A container daemon socket was reachable from inside the Linux sandbox.** The socket was mounted read-only, and a read-only mount does not stop a `connect()`: the daemon answered. A socket in a directory its daemon owns is now hidden by hiding that directory, which held when the daemon restarted and recreated its socket. A socket in a directory everything shares (`/run/docker.sock`, rootless Docker's `$XDG_RUNTIME_DIR/docker.sock`) can only be masked as a file: it is covered while it exists when the shell starts, and a daemon that starts or restarts after that is reachable until the next shell. The banner says so on Linux.
- **The rootless sockets were not on the list.** Rootless podman, rootless Docker and Docker Desktop for Linux are on it now. Rootless podman's directory is hidden even before podman first runs, so starting it later from inside the sandbox does not expose it. If `$XDG_RUNTIME_DIR` is unset (cron, `su`), Linux falls back to `/run/user/<uid>` instead of dropping these entries.
- **The entity's own memory broke after its first bash command.** For each store file that did not exist yet, the sandbox left a read-only empty file on the host. The next write to the episodic store then failed ("attempt to write a readonly database"), and an entity with no `confinement.json`, which is what `init` produces, refused to start next time because the file was now empty JSON. The entity's `.levain` directory is now mounted read-only for the confined shell, with its existing subdirectories writable, so the shell can create nothing new there and nothing is left behind. On macOS the shell may still create and edit other files at the top of `.levain`; on Linux it may not.
- **Bash failed to start on any Linux host without containerd, CRI-O and colima.** Every daemon directory on the socket list was pinned as a jewel's parent, and the sandbox cannot pin a directory that does not exist or a symlink such as `/var/run`. A missing parent is now skipped unless the sandbox will create something inside it, in which case it is created first and pinned, so it cannot be renamed away and replaced. A symlink is pinned at its real path, except one the user could replace (a symlinked `~/.ssh`), which gets no bash on Linux.
- **A file denied both ways could become readable.** A credential the operator pinned under `~/.ssh` in raw mode was masked and then re-exposed by a later read-only mount of the real file.
- **`levain doctor` reported bash for a config `levain run` refuses.** A malformed `confinement.json` now FAILS the confinement floor check, because `levain run` refuses to start that entity. This is the one way the check fails; a missing hand is still reported, never failed.
- **The user's service manager was a way out of the Linux sandbox.** Measured in a container with a real `systemd --user`: from inside the confined shell, `systemd-run --user` ran a command outside it that printed a crown-jewel file. The shell can no longer reach the user's systemd manager or session D-Bus (measured: both are refused), so it has no `systemctl --user` and no desktop D-Bus. There is no opt-out yet.
- **Scheduled seats on Linux (`systemd --user`).** A task containing `%`, `${...}` or a backslash was rewritten by systemd before it reached the seat (measured: `%n` became the unit name); arguments are now escaped and arrive exactly. Reinstalling a running unit now applies the new definition instead of leaving the old one running. Switching a cockpit to a seat stops the cockpit service. A seat's status now shows a failing last turn. systemd older than 240 is refused before anything is written. The cadence is start to start, as on macOS; the old description said otherwise.
- **A daemon label is now a plain name on both platforms.** Letters, digits, `.`, `_` and `-` only. A label such as `../x` used to name a file outside the unit directory for `install` and `uninstall`.
- **The sandbox creates missing mount targets on the host** (an empty directory or file where a protected path did not exist). That has always been true of the Linux floor; it is now stated, and kept out of the entity's own store.

### Changed — on Linux, bash runs without IP network while the entity's floor blocks connections back to this host

Since 0.4.6 the floor stops the entity from connecting to this machine's own services (the `sshd` route in 0.4.6's security note). On macOS the sandbox blocks just that destination. On Linux, `bwrap` cannot block a single destination, so the block runs bash in its own empty network namespace: connections back to this host fail, over TCP and over abstract unix sockets (both measured), and so does all other network inside bash (`pip`, `git fetch`, `curl`). Bash keeps its own isolated loopback. Not blocked: a unix socket at a file path the floor does not deny (an ssh ControlMaster or a proxy socket in `/tmp`, D-Bus, X11), which can reach this host's services and so bypass the block (spore-1005), and, inside a VM, AF_VSOCK to the hypervisor. On a host where `bwrap` runs but cannot create a network namespace, bash is dropped rather than offered and left to fail. The entity's model calls are made by `levain` outside the sandbox and are unaffected. `levain run` and `levain doctor` both say so. To give bash the network back and accept that exposure, set `"allow_localhost_outbound": true` in the entity's `.levain/confinement.json`.

## [0.4.8] — 2026-09-30

### Fixed — `levain init` in the terminal no longer writes an empty identity

`levain init` answered with bare Enter throughout used to exit 0 and write an empty operator and entity name into `seed/origin.md`, which `levain doctor` then failed. The terminal interview was the one onboarding path that skipped the identity rule; the `--answers` file and the web form already refused it. The interview now re-asks a blank operator or entity name, drops a blank one restored from an old checkpoint so it is asked again, and after three blanks in a row stops with an error rather than looping (a closed pipe reads as endless blanks). An identity question inside an optional section can no longer be skipped into a blank. The re-ask of a refused blank no longer prints a "(revising)" header, which belongs to going back with `:back`.

### Fixed — `levain update` no longer deletes a seed file that another pack still ships, or one you edited

When a pack's override of a base seed file was dropped, `update` could delete the file outright, or leave it reading as up to date, so the base version never came back. Reconcile now works out which layer owns each seed file: a dropped override restores the layer it hid, an unreadable base layer means nothing is deleted (it is reported instead), a stray Finder `.DS_Store` in a pack's `seed/` no longer makes the whole stack unreadable, a file moved from one pack to another in one update is judged once, and an undecodable `pack.toml` is treated as an unreadable layer rather than crashing `update`. A copy you edited is kept, backed up and reported, never overwritten silently. Each restore or removal prints one line saying what it did.

**Known limit, unchanged from 0.4.7:** a change to a pack's `order` alone, with no override dropped, is not reconciled. The `pack.toml` comment documents it as a v1 limitation.

### Changed — `levain doctor` exits 1, not 6, on a corrupt or empty activation receipt

`doctor`'s hook-freshness check used to read a receipt it could not parse the same way it reads an install that predates the receipt entirely: both landed on exit 6, "an upgrade step is pending." **A receipt that exists but is unreadable is not a pre-receipt install** — `_write_activation_receipt` is the sole writer and runs only after the tree is in place, a failed write reads as absent (not corrupt), and a failed swap rolls the prior receipt back — so a legitimate install cannot produce a corrupt or empty one. Reading it as "no receipt yet" could downgrade a tampered hook (an appended `os.system(...)`, say) from broken (exit 1) to routine-pending (exit 6). A corrupt or empty receipt now exits 1, naming the receipt's path and why doctor could not read it, with the same `init --force` remedy. An absent receipt (a genuinely pre-receipt install) is unchanged: still exit 6.

`levain init --force`'s own post-swap notice had the identical gap: absent, corrupt and empty receipts all printed the byte-identical "no install receipt covers it" line for the retained backup tree. Corrupt and empty now say so explicitly — "its install receipt was unreadable (corrupt/empty) … a damaged receipt is not a pre-receipt install" — while absent keeps its original text.

*(This entry was filed under `[Unreleased]` when 0.4.8 was cut. The change is in the 0.4.8 code, so it belongs here.)*

## [0.4.7] — 2026-09-14

### Changed — Levain now requires anneal-memory 0.9.10 or later

The anneal floor moves from `>=0.9.8` to `>=0.9.10`. Per anneal's own 0.9.10 changelog, the published 0.9.9 could report a destroyed audit history as healthy: once the sealed audit file its manifest names and the active file were deleted, `verify()` returned `valid=True` with no entries, and `anneal-memory audit` could leave out a whole sealed week while still reporting the trail trusted. Levain calls no new anneal API; the floor exists so a Levain install cannot resolve to that anneal. `pip install -U levain` brings anneal along.

### Changed — `levain init` backs up `~/.codex/config.toml` whenever it would change the memory-server block at all

The Codex adapter rewrites the global `[mcp_servers.anneal_memory]` block. It used to copy `config.toml` aside only when it judged that something you had customised was about to be lost, and that judgement missed cases: a wrapper `command` that kept Levain's arguments, or a comment you had written inside the block. Now any change to that block's text writes `config.toml.bak.<timestamp>` first, and the notice says what kind of change it was:

- upgrading an install made before this release: "keeps its store and now starts the memory server as …", plus where the copy is;
- a comment or formatting inside the block: "keeps its settings, but comments or formatting inside that block were not kept", plus where the copy is;
- anything else that differs: the existing "customisation is gone" warning, plus where the copy is.

Re-running `init` against a block Levain wrote, byte for byte, still does nothing and says nothing.

### Changed — `levain doctor` explains the anneal-version mismatch on installs set up by an older Levain

Levain releases before 0.4.7 recorded the version of whichever `anneal-memory` came first on `PATH`, which was not always the anneal Levain itself runs. After upgrading such an install, `doctor`'s `compat: anneal-lock` line could call that mismatch "an out-of-band upgrade". For an install last composed by a Levain older than 0.4.7 it now says the recorded version most likely belonged to a different `anneal-memory` on `PATH`, and points at `levain init --force` or `levain update`, either of which records the anneal Levain actually imports. The check and `doctor`'s exit code are unchanged.

### Changed — the memory server is now the anneal installed alongside Levain, not the first one on `PATH`

`levain init` used to look up `anneal-memory` with a `PATH` search and write whatever it found into the MCP registration and the hooks. If a second anneal sat earlier on `PATH` (say an old `~/.local/bin/anneal-memory`), that one served your memory, while `levain doctor` and `levain update` checked a version chosen by the same lookup. Levain itself imported a different anneal again.

- The MCP registration (`.mcp.json` for Claude Code, `[mcp_servers.anneal_memory]` in `~/.codex/config.toml` for Codex) now runs `<Levain's Python> -P -m anneal_memory`. `-P` keeps the directory the client was started in off Python's import path, so an `anneal_memory/` source checkout there cannot stand in for the installed package.
- The hooks, `doctor`, `update` and `init --web` use the `anneal-memory` script that belongs to the anneal package Levain's own Python imports. They find it through that package's install record, not through `PATH`. If that script cannot be run, they fall back to `<Levain's Python> -P -m anneal_memory`. The hooks no longer try a bare `anneal-memory` from `PATH` at all.

**Existing installs keep their old registration until you re-run `levain init --force`.** Nothing breaks if you don't; you keep whichever anneal the old lookup picked. `levain doctor` now reports such a registration as a pending post-upgrade step (below).

### Changed — `levain doctor` exits 6 when the only failures are post-upgrade steps not yet applied

`doctor` used to exit 1 both when something was broken and when a routine post-upgrade step was still pending, so the expected upgrade path looked like a broken install. It now exits:

- `0` — every check passed;
- `1` — at least one failure that is not a pending upgrade step (and whenever `--invoke`'s live-fire check fails);
- `6` — every failure is a post-upgrade step not yet applied: hooks or the adapter carrier that predate the installed levain, the version set last composed by an older levain (`levain update` pending), or a memory-server registration that does not run Levain's own interpreter (`levain init --force` pending).

The failures are still printed as `FAIL`, because they are real: until you apply the named remedy, the install keeps running the activation layer from the release it was last set up with.

⚠ **If a script branches on specific codes** (`case $? in 1) ...`), exit 6 no longer matches the `1` branch. Scripts that only test for nonzero (`levain doctor || alert`) behave as before.

### Changed — `levain init --force` keeps your whole previous `activation/` tree, and deletes an old one only when it can prove you never edited it

`init --force` used to look at each file under `activation/` before replacing the tree, and copy the ones it judged to be yours into `.levain/backups/activation/<timestamp>/`. Anything that judgement could not handle was lost: an edit saved in the moment between the check and the replacement, a symlink, a directory it could not read.

- **The previous tree is moved whole** to `.levain/backups/activation/tree-<timestamp>/`. Every file, symlink and directory you had is in it, exactly as it was.
- **`init` now records what it installed** in `.levain/activation-manifest.json`: for each file under `activation/`, the sha256 of the bytes as installed and of the package file they came from. A copy travels beside each kept tree as `tree-<timestamp>.receipt.json`.
- **An old tree is deleted only if every file in it matches that record**, with nothing added and no symlinks: then it holds nothing of yours. The newest three such trees are kept. Any tree that differs, has extra files or symlinks, or has no readable record (every tree kept from an install made before this release) is **never deleted**, and `init` names it.
- The "Operator-edited … preserved at" lines are worked out from that record, so they now also catch an edit made only to the `_INSTALL_ANNEAL_BIN` line of a hook. Where there is no record, `init` says it cannot tell.
- If `.levain/backups/activation/` cannot take the move, the previous tree is kept beside `activation/` as `.levain-activation-prev-<timestamp>`, `init` tells you so, and levain never removes it. A reinstall is never blocked by this.
- Backup directories written by earlier releases (named with a bare timestamp) are never removed.
- This supersedes 0.4.5's stated exception for symlinks under `activation/`: they are now kept as symlinks.
- If `activation/` itself is a symlink, the link is kept beside it as `.levain-activation-prev-<timestamp>` (where it still resolves) and its target is left untouched; `init` says so.

## [0.4.6] — 2026-09-13

### Security — a local `sshd` could be used to read a confined entity's crown jewels via the forwarded agent (agent mode) or a readable key (raw mode)

⚠ **This affects 0.4.5 and every earlier release with the seatbelt floor.** If you run a confined
entity in the default `ssh_mode="agent"` **and** macOS **Remote Login** is on (System Settings →
General → Sharing → Remote Login) **and** a key your ssh-agent holds is in your
`~/.ssh/authorized_keys`, then a confined entity could run `ssh localhost cat <a crown-jewel file>`
and get the file back — even though the floor denies reading that file directly. `sshd` runs as
root, was never inside the sandbox, and was authorised by the very agent socket that agent-mode
forwards by design. This is the same total-bypass class as reaching a container daemon socket (fixed
in 0.4.x): a floor that denies a *file* but forwards a *credential* to an unsandboxed root reader is
decorative for that file. **Reproduced end to end**; the forwarded agent socket is the sole carrier.

**Fixed:** in **both** ssh modes the floor now denies outbound network connections to *this host* —
`(deny network-outbound (remote ip "localhost:*"))`. macOS seatbelt's `localhost` covers **every
address bound to a local interface** (loopback `127.0.0.1`/`::1` *and* the machine's own LAN
address), so the entity can no longer reach a `sshd` this machine runs by any of its own addresses,
while **remote** hosts stay reachable — ssh/git to real remotes over the forwarded agent is
unaffected. (It also blocks the entity reaching a local service that re-exposes jewels, e.g. a
loopback API, independent of ssh.)

- **Cost, and it is real:** a confined entity in agent mode can no longer reach a **local service**
  either — a dev server it started, a database, an `argushub` on `127.0.0.1:8420`. If an entity
  genuinely needs that, set `"allow_localhost_outbound": true` in its `.levain/confinement.json` and
  accept that the `sshd` vector is reopened for that entity.
- **Scope:** the deny applies in **both** ssh modes. The reason it is not agent-only: in
  `ssh_mode="raw"` the entity can read its *own* key directly, but the **other** crown jewels
  (`~/.anneal-memory`, sibling stores, your declared secrets) stay floor-denied, and a local `sshd`
  authenticated with that key reads *them* as root just the same (reproduced) — so raw mode needs the
  deny too. ⚠ A separate raw-mode exposure remains for a later release: raw mode still lets the entity
  *read the operator's `~/.ssh` private keys directly*. The loopback deny stops those keys reaching a
  **local** `sshd`, not a remote one; retiring or redefining raw mode as scoped-credential-only is
  part of the credential redesign below.
- **Not covered — ssh connection multiplexing (a documented residual, not closed).** This deny
  stops a *fresh* connection to a local `sshd`. It does **not** stop reuse of an **existing** ssh
  `ControlMaster`/mux socket to localhost: if you run `ControlMaster auto`/`yes` and have a live
  master to this host, a confined entity that can name the `ControlPath` can ride that already
  authenticated master over its unix socket — which the IP deny cannot see, and which needs no
  agent. Denying *all* outbound unix-socket connects would close it but was measured to break
  `getaddrinfo`/HTTPS/`git`/`pip` (macOS resolves DNS through an mDNSResponder unix socket), so it is
  not the default. The precondition is narrow (ssh multiplexing *to localhost* specifically is
  unusual); a scoped fix is tracked for a later release. If this matters to you now, set
  `ControlMaster no` for the confined entity or don't keep localhost masters alive.
- **Not covered — the forwarded agent is a signing oracle (relay/`ProxyCommand`).** A forwarded
  agent signs for *any* `sshd` that authorises its key, reached through *any* hop the floor allows.
  An entity can go `ssh -o ProxyCommand='ssh <relay> nc %h %p' <this-host>` — the sandbox sees only
  the allowed connection to the relay, which connects back to this host's `sshd`. So a **Tailscale**
  peer (or any reachable relay) defeats the localhost deny; a Tailscale address is **not** covered by
  it (an earlier draft wrongly claimed it was). This is not closeable by a network deny — it needs
  agent *mediation* or not forwarding the agent, part of the agent-forwarding review above.
- **Not covered — off-interface addresses:** a `sshd` reachable only through an address **not** bound
  to a local interface (e.g. a NAT hairpin to your router's external IP).
- **Why not just deny port 22:** macOS seatbelt has no per-port or per-IP loopback filter
  (`(remote ip "127.0.0.1:22")` is rejected outright), so denying all outbound-to-self is the only
  enforcement it offers.

### Fixed — `init --adapter codex` widened the permissions on your global Codex config, and `levain update` narrowed them on `.levain/config.json`

⚠ **This affects 0.4.5, which is published.** If you ran `levain init --adapter codex` under 0.4.5
against an existing `~/.codex/config.toml` that already had an `[mcp_servers.anneal_memory]` block,
its permissions were reset to your shell's default — **whether or not anything was repointed**, and
whether or not you saw any output about it. Run:

```
ls -l ~/.codex/config.toml
```

If that is more open than you set it, restore it with `chmod 600 ~/.codex/config.toml` (or whatever
you had). Upgrading does not repair a file already changed; it stops it happening again.

⚠ **How open it became depends on your `umask`, so there is no single number to look for.** Under
the common `022` it is `-rw-r--r--`, readable by every account on the machine. **Under `002` it is
`-rw-rw-r--` — group-WRITABLE**, which for a file that tells every Codex session on the machine
which memory store to read is worse than it sounds. Under `077` nothing changed.

⛔ **The earlier wording of this entry said "if it repointed Codex's memory", and that was wrong.**
Only the *backup* is conditional on a repoint; the write happens on every run. Re-running `init
--adapter codex` against the store already registered — the ordinary re-install — widened the file
silently, with no backup and nothing printed. That is the case least likely to have been noticed,
and the first version of this advisory excluded exactly those people.

0.4.5 made that write atomic, correctly — a partial write could otherwise truncate your whole
global Codex config. But writing a new file and moving it into place replaces the file itself, so
the permissions came from the process default rather than from the file being replaced. The
previous version wrote through the existing file and so kept them without trying.

The file sits beside `~/.codex/auth.json` and is machine-wide, so an operator who set `0600` on it
meant it. The **mode** of the file being replaced is now carried across, and nothing else is — the
modification time still moves, because the file genuinely did change.

### Fixed — a Codex block Levain could not read was replaced silently, with no backup

The warning and the backup that guard Codex's machine-wide `anneal_memory` registration only fired
when Levain could read **both** the old store path and the new one out of the block. If your block
was not the shape Levain writes — you moved the store into a wrapper `command`, or restructured
`args` so it no longer carries `--db` — Levain could not read the old store, took no backup, said
nothing, and replaced your block anyway.

That is backwards: a block Levain cannot read is the one most likely to have been edited by hand,
and the least safe to overwrite without a copy. Replacing a block Levain does not recognise now
copies the file aside first and says so, naming where the copy is.

Re-running against the store already registered still does nothing and still says nothing — that
case takes nothing away, and turning it into backup spam would train you to ignore the notice that
matters.

### Fixed — repointing Codex replaced a symlinked `config.toml` instead of writing through it

If `~/.codex/config.toml` is a symlink into a dotfiles repo — stow, chezmoi, or a hand-rolled
setup — 0.4.5 replaced **the symlink** with a regular file and left the real file holding the old
registration. Levain then correctly printed that Codex now points at the new store, and the
operator's next re-stow silently put the old one back. A registration that undoes itself later,
having been accurately announced at the time.

The write now lands on the file the symlink points at, and the symlink survives.

⚠ **Known-open: a hardlinked `config.toml` still loses its link.** The atomic replace breaks it
(the other name keeps the old contents), and there is no atomic-rename form that preserves a
hardlink — the alternative is writing through the existing inode, which is the torn-write risk the
atomic write exists to remove. If you hardlink your Codex config, check the other name after
running `init --adapter codex`.

⚠ **Stated limit: the mode is restored, ACLs and extended attributes are not.** Replacing a file
drops them and there is no macOS API in this path that puts them back. If you restricted
`~/.codex/config.toml` with `chmod +a` rather than with a mode bit, that restriction is still lost
when Levain repoints Codex. Use a mode bit if you need it to survive.

⚡ The backup taken moments earlier in the same operation had always preserved the mode. So a single
run left `config.toml.bak.<timestamp>` at `0600` and the live `config.toml` beside it at `0644` —
the copy made to protect the file was better protected than the file.

**The same defect was live in the opposite direction and is fixed too.** Every atomic write in
Levain replaces the file rather than writing through it, so every one of them can move the mode.
`.levain/config.json` — the file your entity name and brand settings live in — is written through a
helper whose temporary file is always created `0600`, so an operator who had opened it up to `0644`
or `0640` (to let a second account or a service read it) had it closed back to `0600` by the next
`levain update`, surfacing later as an unrelated-looking permission error at the reader. That helper
now carries the existing mode across as well. A file being created for the first time still gets
`0600`, which is the safer default when there is no operator intent to preserve.

## [0.4.5] — 2026-09-06

**`levain init --force` is the command our own upgrade instructions tell you to run, and in 0.4.4 it deleted operator edits to the activation tree without a backup and without a word.** It copied aside exactly two files — `posture.md` and `recency_directives.md` — while replacing everything else, so a patched hook script was destroyed on the documented upgrade path. That happened to a real operator.

This release widens what survives that command, and corrects the operator-facing messages that described a system slightly better-behaved than the one we shipped.

### Changed — `init --force` now preserves everything under `activation/` it cannot rebuild, not two filenames

Anything at `activation/` whose bytes this install cannot reproduce — because you edited it, or because no layer provides it any more — is copied to `.levain/backups/activation/<timestamp>/`, keeping its relative path, **before** the tree is replaced. Hooks included. Files you added yourself included.

Membership is decided by whether the bytes are reproducible, never by filename, so this does not need updating when the tree gains a file.

⚠ **It preserves; it does not always announce.** Where a difference is one `init` itself wrote — the `_INSTALL_ANNEAL_BIN` line, which is re-resolved on every run — the copy is still made but no "operator-edited" notice is printed, because an edit of your own confined to that same line is indistinguishable from our substitution. If that copy cannot be made, `init` says so and names the file rather than continuing silently. **Where the difference is one we cannot explain, `init` refuses rather than overwrite it.**

⛔ **Stated exception: symlinks are not handled.** A symlink to a directory inside `activation/` is never examined and is removed with the tree; a symlink to a file is compared by its target's contents and replaced by a regular file. Neither is backed up. If you have symlinked anything under `activation/`, copy it aside yourself before upgrading.

### Fixed — `doctor` told you what `init --force` keeps, and left out what it does not

Two checks — `hook freshness` and the carrier check (`CLAUDE.md freshness` on a Claude Code install, `AGENTS.md freshness` on Codex) — ended their advice with a list of what survives the re-render. Every item was true, and the list read as complete: it named your store, your seed answers and your activation markdown edits, and did not mention that the whole `activation/` tree is rewritten or that the interview runs again.

Both now say the interview re-runs, say the tree is rewritten, and name where edits are copied. **The "seed answers are kept" clause is gone because it was false** — `init --force` starts the interview with no answers pre-filled and clears the saved checkpoint, so working through it accepts new answers over your recorded ones.

### Fixed — a hook you had not touched could be reported as operator-edited

`init` resolves the `anneal-memory` binary on the `PATH` fresh on every run and writes the result into the hook. Running `init --force` from a shell that resolves it differently — a virtualenv versus a plain login shell is enough — changed those bytes with no involvement from you, and the new backup then reported the hook as an operator edit.

It also recurred: each run wrote its own resolution in, so alternating between two shells re-triggered it indefinitely, and on an install whose backup directory was not writable it turned a routine re-install into a refusal.

Hook scripts are now compared with that one substituted line normalised out, so only differences we did not write are reported.

### Fixed — `init --adapter codex` repointed Codex's machine-wide memory without saying so

The `[mcp_servers.anneal_memory]` block in `~/.codex/config.toml` is global by design: it is what every Codex session on the machine reads, not just the install you ran `init` from. Replacing it was silent and left no copy, so running `init` against a second install — or a scratch directory — moved every Codex session onto that install's store with nothing printed.

`init` now names the store it is leaving and the store it is adopting, says the registration is machine-wide, and copies `config.toml` aside first. It still performs the repoint, because pointing Codex at a different install is a legitimate thing to want and is the documented way to undo this. Re-running against the store already registered stays silent.

The config file is now written atomically and the notice is printed only once the write has landed.

### Known issue — an install path containing `\`, `"` or a newline produces an unparseable Codex config

Not new in this release and not introduced by it. `init --adapter codex` substitutes the install path into `config.toml` without escaping it for TOML, so those characters produce a file that is **not valid TOML** — a backslash is an escape introducer inside a basic string and a quote ends it early. What Codex does with a config it cannot parse is its business and we have not measured it; what we can say is that levain wrote the file wrong. All three characters are legal in POSIX paths and all three are rare. Avoid them in an install path until this is fixed.

### Correction to 0.4.4's upgrade note — a normal upgrade leaves `doctor` red, and 0.4.4 did not say so

⛔ **`pip install -U levain` to 0.4.4 leaves `levain doctor` at exit 1 with two failures, for every operator, on the correct upgrade path.** Measured against the published wheels — 0.4.3 and 0.4.4 installed side by side in clean virtualenvs, upgraded the way an operator upgrades:

```
0.4.3   All checks passed.                                                    exit 0
0.4.4   [FAIL] hook freshness: installed hook script(s) differ from the package
        [FAIL] compat: levain: levain upgraded 0.4.3 -> 0.4.4 since the set was last composed
                                                                              exit 1
```

**Both failures are correct, and neither check is new.** 0.4.3 carries both and passes them (`hook scripts match the package`, `levain 0.4.3 (matches last composed)`). They turn red *because the version moved* — the hook scripts on disk were rendered by the previous release's templates, and the compatibility set was composed against the previous release. **Nothing is broken.** The remedies are the two commands the failures already name:

- `levain init --force --path <install>` — re-renders the activation tree. ⚠ **It re-runs the interview**, and anything you have edited under `activation/` is copied to `.levain/backups/activation/<timestamp>/` first (see *Changed* below; in 0.4.4 that backup covered only `posture.md` and `recency_directives.md`). Your store is untouched;
- `levain update` — reconciles the anneal + schema + migration set to the new known-good.

⚠ **What 0.4.4's own `Upgrading` note got wrong**, and it is why this correction exists: it says to run `doctor` after upgrading and then warns about exactly ONE possible new failure — `activation scope` — which only affects operators whose hooks are wired at the user level. It presents `levain init --force` as what you need *additionally*, for three named features, rather than as a required step. **An operator who follows it exactly is prepared for one specific failure and receives two different ones.** That section is left as published; this entry is the correction.

⚠ **Measured from every 0.4.x release, not inferred.** Each of 0.4.0, 0.4.1, 0.4.2 and 0.4.3 passes its own `doctor` at exit 0, and each one gives the identical two failures and exit 1 under 0.4.4 — the `compat` message naming the version it came from in each case. So this is the whole 0.4.x upgrade surface, measured rather than generalised from one path.

▶ **The design question underneath this is open and deliberately not answered here:** `doctor` exits 1 identically for *"something is broken"* and for *"a routine post-upgrade step is pending."* An instrument that goes red on the expected path teaches its operator to discount it, which is expensive for the one tool we ask operators to trust when something genuinely is wrong.

### Fixed — a rejected confined shell could leak if logging the rejection failed

A shell that fails the confinement check is torn down before the refusal is raised. That teardown was reached only by falling off the end of the handler that logs a failed `close()` — and a logging call is not guaranteed not to raise, since a custom handler whose `emit` raises propagates straight out of it. A broken logging handler therefore skipped the teardown entirely, leaking the subprocess, its process group, the FIFO directory and the reader thread on every rejected spawn, **and** replaced the confinement refusal with the logging error. The teardown now runs from a `finally`, so no logging failure can strand it.

## [0.4.4] — 2026-09-06

**The crown-jewels confinement floor stops reporting coverage it does not have.** Three ways a path or a socket could sit outside the floor while the tooling said it was fenced: a `~user` path that threw its way past the check, a container daemon socket that defeated the floor entirely, and a listed socket resolved before the shell it fences existed. The `levain run` banner now names which sockets are covered **and which are not** — the deny is an enumeration, and an enumeration is always incomplete.

Alongside it: `doctor` stops reporting green on two of the three ways to write user-level wiring, both local servers stop mishandling two `Content-Length` cases, and the test suite has zero permanent failures for the first time.

### Fixed — a `~user` path could throw its way past the crown-jewels check

`crown_jewel_reason` — the in-process guard that decides whether the file-editor hand may touch a
path — caught `ValueError` and `OSError` from resolving that path, but not `RuntimeError`. Python
raises exactly `RuntimeError` from `expanduser()` for a `~someuser` spelling with no passwd entry.
The path comes from the entity, so a model emitting `~unknownuser/...` reached a function documented
as fail-closed and got **neither a refusal nor an allow** — it got an exception out of the security
predicate.

The same gap sat in `declared_resources`, whose docstring says *"Never raises."* and whose own
comment explains why that matters: a raise there surfaces a raw error event and **skips the
executor**, so the floored refusal never fires at all.

Both now catch it and refuse the path. This is the same defect the `doctor` fix in this release was
written for; it reached `doctor` and not the security twin beside it.

### Fixed — a container daemon socket defeated the whole confinement floor

If a container runtime was installed, a confined entity could read any crown jewel with one
command — `docker run --rm -v <jewel>:/x:ro alpine cat /x`. The daemon runs as root and was never
inside our sandbox, so no path deny and no mount could hide anything from it: on such a machine
every other rule in the floor was decorative. No privilege escalation and no exotic technique were
involved; the entity simply asked a more privileged process to read the file on its behalf.

The known container/VM daemon sockets — docker, podman, containerd, CRI-O — are now part of the
universal floor, denied by default. Opt out with `"allow_container_sockets": true` in
`.levain/confinement.json` if your entity genuinely needs to drive containers and you accept that
doing so voids the rest of the floor on that machine.

**The obvious fix does not work, which is why this took three rules rather than one.** A seatbelt
`file-read* file-write*` deny does not block `connect()` to a unix socket — with the socket paths in
the credential denylist the profile emits correct-looking rules and the exploit still returns the
jewel. Blocking the connect needs a `network-outbound` rule, and that rule alone is then defeated by
renaming the socket, and *that* is defeated by relocating its parent directory. All three are
enforced, and each was measured failing on its own before it was kept.

### Fixed — a listed socket could be reported as covered while it was not

The socket denies above are stored **resolved** — the profile can only name where a path *points*,
never the path itself, because the sandbox canonicalises the target before matching. Those denies
were computed when the policy was built, and the confined shell starts later (and starts again after
every `exit`). A listed socket that did not exist yet resolved to itself, so if something created it
as a symlink to an unlisted target in between, a connect through the **listed name** landed on a
target nothing denied.

The resolution now re-runs every time a shell starts, and a freshly-resolved target gets the **same
three rules** as a socket named in the list — connect, rename, and relocation — not just the connect
one. The fresh entries are **added** to the existing set rather than replacing it, and the widened
set carries forward into the next shell, so the denylist can only ever grow across a session.
Coverage is now a spawn-time snapshot instead of a build-time one, and `levain run` says so in those
words.

Two related consistency fixes came out of the same reviews. The re-resolution happens **once** per
shell start and the shell reports back the exact rule set it was started with, so nothing a shell
enforced can be lost when the next one starts. And both of the entity's hands — the shell and the
file editor — now read **one** floor object rather than each holding its own copy, so a rule added
for one is enforced by the other.

The first version of this fix updated only the connect rule, which the three rules above already
explain is not enough on its own; the next re-resolved twice and kept the earlier answer. The guard
meant to stop a sandbox backend from skipping the re-resolution was rewritten five times and
sidestepped seven ways, and was ultimately **deleted rather than rewritten a sixth time** — the ways
to shadow a method in Python are not a closed set, so enumerating them cannot terminate. What
replaces it is structural: the re-resolution now happens in the base method itself, so a backend
implementing the documented seam cannot receive stale rules. Each of these was caught before release
and the tests pin them. (The single review count for this whole section is stated at its end.)

**What this was, honestly: a wrong claim rather than a new hole.** A confined entity cannot create
that symlink — the socket is write-denied at both spellings, which is one of the three rules above —
so it takes something outside the sandbox to arrange, and the unlisted socket it would point at was
already reachable under its own name, which the banner already told you. What it cost was your
ability to trust the list: a socket that was named as covered was not. That is fixed.

**The residual is stated rather than papered over.** A sandbox profile is fixed when the process
starts, so a symlink created at a listed path *after* a shell is already running is still not
covered, and no rule available here would cover it. A spawn-time snapshot is the strongest statement
this sandbox language can make, which is why the wording says snapshot and not "covered".

**Both of the entity's hands read one set of rules, per conversation.** The shell and the file
editor previously each held their own copy, so a rule the shell learned at startup was invisible to
the editor. They now share one set for the length of a conversation, and a later conversation never
inherits an earlier one's — which also means a run that should be more restricted than the last one
actually is.

**Nine rounds of review went into this section, and every one of them found something in the
previous round's fix — a rate that did not bend once.** That number is the count of the review
rounds recorded in this repository's own commit trail for the change, not an estimate; earlier
drafts of these notes said "three" in one place and "five" in another for the same span, and both
understated it. Round nine ran on 2026-09-05 against an outside model
lineage and still returned findings; one was a real resource leak on a rejected-shell path and is
fixed here, and the others did not hold against the code as it now stands. One of the
rounds found the real shape of the shared-rules problem: the rules were cached per *entity*, which
made "both hands see the same rules" and "the rules are current" pull against each other, so fixing
either one broke the other. Caching per *conversation* removes the conflict rather than balancing
it, because a conversation has one set of rules by definition. Everything found was caught before
release.

**The re-resolution now happens before the sandbox backend is asked to start a shell**, rather than
inside it. An earlier attempt guarded the backend against skipping that step; the guard was removed
because it could be worked around in a growing number of ways and never covered them all, while
doing the work upstream means a backend is simply handed rules that are already current. There is no
step left to skip. Nothing here is operator-visible — it changes which layer is responsible, not what
the floor denies.

**The banner names which sockets are covered, and which are not.** The deny is an enumeration and an
enumeration is always incomplete, so `levain run` says so rather than claiming containers are
fenced: a custom `$DOCKER_HOST`, a TCP daemon endpoint, or any runtime whose socket is not in the
list remains reachable. If that describes your setup, the floor does not cover it.

⚠ **macOS only in this release.** The Linux confinement floor is not in 0.4.4, so there is nothing
here for it to apply to yet; the same three arms land with Linux support.

### Fixed — `levain doctor` carried a dead import, and the release did not pass lint

`_sha256_file` had been imported by the hook-freshness check since the comparison it served was
replaced by placeholder-aware normalisation, along with a comment explaining why it was imported
rather than reimplemented. A dead import keeps its justifying comment looking live. Both are gone,
and `ruff` passes again.

### Fixed — `doctor` reported green on two of the three ways to write user-level wiring

The dark-install detector compared each hook command token as a **literal path**, with no variable expansion and no `~` handling. So `~/lev/activation/hooks/session_start.py` and `$HOME/lev/...` — the second of which Levain's own `settings.template.json` spells inside double quotes — resolved against the current directory, never matched the install, and the scan came back empty. `levain doctor` then reported the reassuring install-scoped PASS on the exact configuration the check exists to FAIL.

A miss in `_hook_command_targets` produces a wiring FAIL, which is loud. A miss here produced silence — the same `absence_of_signal_rendered_as_health` that this whole run of releases is about, inside the instrument built to end it.

⚠ **The first cut of that fix reintroduced a defect closed earlier in this same release.** `Path("~someuser/...").expanduser()` raises `RuntimeError` for a user with no passwd entry, and `RuntimeError` is neither `OSError` nor `ValueError` — so expanding tokens added a fresh way for a *foreign* `~/.claude/settings.json` to abort the entire doctor run. That is the `[null]`-entry crash from 0.4.2 arriving by a new door. Caught before commit by asking whether the condition the guard covers would disable the guard.

### Fixed — the upgrade note named the wrong command

The 0.4.3 notes said `levain update` overwrites the hook templates. It does not: `update` writes nothing under `activation/` at all, and its pack phase only ever writes under `seed/`. What replaces the activation tree is **`levain init --force`** — which is step two of the documented upgrade procedure, so following our own instructions is what kept eating the local patch. Corrected at all four sites carrying the claim, including `docs/operator-manual.md`, which said `update` "re-applies any updated partnership settings".

### Fixed — the Codex adapter README promised a failure Codex operators cannot get

It told Codex operators `doctor` FAILS when hooks are wired at the user level under install scope. For a Codex install both halves of that condition are the **default**, and `_user_level_wiring` deliberately never reads `~/.codex/hooks.json` — a test pins the never-FAIL. A promise of a red that never comes reads as a green. The bullet now says `doctor` REPORTS the gate for Codex and names where the answer actually appears.

### Fixed — the seed still scaffolded the graduation cap the library removed

`seed/continuity.md` taught "graduates to 2x, then 3x" while `seed/memory.md` teaches the uncapped ladder. The 2026-08-31 uncapping landed at one of the two sites, so a fresh entity was still scaffolded with the cap anneal dropped in 0.9.7. The scaffold now states the direction and points at `memory.md` rather than carrying a second copy of the rule.

### Fixed — `doctor` reported a pack-owned hook as permanently stale, and the remedy could not clear it

A pack hook containing an install-time placeholder (`{{ANNEAL_MEMORY}}`) was compared as RAW BYTES against the manifest's PRE-substitution source hash, so it read stale forever. Worse, the fix `doctor` itself prints — `levain init --force` — **cannot** clear it, because re-rendering substitutes the placeholder again. The base-adapter branch never had this problem because it normalises both sides; the pack branch skipped that step and now does the same, against the pack source. A pack whose source directory has moved is reported as *not comparable* rather than as a stale hook — that is the pack-drift surface's business, not this check's.

### Fixed — a corrupt `.levain/manifest.json` silently turned pack-hook checking OFF

`doctor` read the pack lock through a wrapper that returns "no packs" for ABSENT and for CORRUPT alike, so a truncated or partially-synced manifest dropped every pack-owned hook out of the comparison **while still reporting "hook scripts match the package"** — over a tampered file, if there was one. It now refuses and names the cause: an unreadable lock is not a clean bill of health. A genuinely absent lock is still the ordinary pack-less install and stays green.

### Fixed — the local servers mishandled two Content-Length cases

Both `levain serve` and the onboarding server:

- **A header like `Content-Length: ²` crashed past the guard.** The check was `str.isdigit()`, which is TRUE for characters `int()` refuses, and `int()` was the very next statement — so a deliberate `411` became an unhandled exception. Now ASCII-digits-only, which is also what RFC 7230 specifies. Non-ASCII digits are refused with a `411`.
- **An oversize POST whose Content-Length OVERSTATED the body got no response at all** (`levain serve` only). The drain stranded on the socket timeout, and the resulting error was indistinguishable from a benign browser keep-alive reset, so it was swallowed: no `413`, no traceback, no log entry. The connection is now always closed on an oversize refusal, and the `413` is sent whether or not the client sent the bytes it promised.

### Fixed — `--consolidate-max-seconds` errors named `--max-seconds`

`levain run` and `levain daemon install-seat` validate the consolidate bound through the same checker as `--max-seconds`, which hardcoded that flag in its messages — so a bad `--consolidate-max-seconds` value produced `levain run: --max-seconds must be >= 0`, sending the operator to fix an option they had not set.

### Changed — a lease-expired job now reads `failed` instead of disappearing

**Behaviour change, and it is operator-visible.** A `pending`/`running` job whose lease expired (a crashed worker's orphan) was DROPPED on the next store write, so polling it returned `unknown` — the status this API reserves for "never seen it, re-propose". Four places in the code and the `/job` route's own docs said such a job reads `failed`.

The composition is what made it matter: the result TTL is matched to the 24-hour idempotency window **deliberately**, so a replayed propose can still poll its handle. Non-terminal records reaped at the 10-minute lease broke that — a same-key retry replayed a handle saying `pending` while the store answered `unknown` for that job id, which is the dead-handle case the TTL matching exists to prevent, and it made an expensive job look re-runnable.

Such a record is now transitioned to `failed` / `"interrupted"` — exactly what restart recovery already does to the same records — and then ages out under the result TTL like any other finished job.

### Fixed — the crown-jewels floor's own record

No behaviour change; the enforcement was correct throughout and is stricter than these descriptions implied.

- `build_policy`'s public docstring described the superseded **name-based** ssh design (`~/.ssh/id_*`) and contradicted its own next bullet. The floor is **location-based** — all of `~/.ssh`, which catches `deploy_key` and per-host keys — and the module docstring is now the single place that states it.
- A comment claimed the raw `Path.home()` ssh spelling *narrows* the symlinked-HOME vector. Measured: it is unreachable on both enforcing hands. The entry is kept as defence-in-depth against a platform canonicalisation change; the claim that it mitigates a live vector is gone.
- The seed's `continuity.md` scaffold taught the capped graduation ladder at a **third** site missed by the earlier fix, and `crystallization.py` carried a fourth.

### Fixed — the suite has zero permanent failures for the first time

Nine tests failed on every run, on any machine without the optional `mcp` and `openhands` extras, because three sites lacked the guard used at a dozen others — seven in an unguarded `build_app` test class, two at `openhands` import sites. A permanent red is indistinguishable from a real regression, which made "the suite is green" unusable as evidence. A base install now runs the suite to zero failures, and the nine skip and say why.

### Changed — the tree between releases is stamped `.devN`

**A release tag names a tree, and the commit after it no longer claims the same version.** For one window in 0.4.3 the tag and the commit after it both read `version = "0.4.3"` while that commit had rewritten the `[wrap blocked]` message the shipped hook emits — so `0.4.3` named two different trees, and what a developer cloned was not what an adopter installed, with nothing in the tree saying so. **The release commit is now the last one on its number**; the next commit bumps both stamps (`pyproject.toml` and `levain/__init__.py`) to the next `.dev0`. Asserted by the test suite rather than left to a release checklist — the checklist is what missed it. See *Versioning* at the foot of this file.

### Changed — `[wrap blocked]` no longer names a shell command or a false count

- The episode count is **gone from the message**, not rephrased. `episodes_since_wrap` counts from the last *completed* wrap, so it included episodes frozen inside the open snapshot: measured at 3 reported while 1 was actually waiting. An honest figure needs more qualification than the line can carry.
- Its discriminator was `anneal-memory wrap-status`, a **shell** command — which was the reporter's own finding one layer up, since an agent with no shell was his entire report. It now points at `status`, reachable over MCP.
- Split by caller: a `SessionStart` hook fires before this session ever called `prepare_wrap`, so advice to "compress what prepare_wrap returned" named an artifact it does not have.
- `episodes_since_wrap()`'s docstring no longer justifies itself as "the count-only view for callers that do not give advice" — it has zero in-repo callers and is a compatibility shim for operators carrying local hook edits. It now says so. *(This retracts the rationale given for it under 0.4.2 below; that entry stands as an accurate record of what 0.4.2 shipped.)*
- `verify.py`'s timeout hint and the Codex adapter README both named `episodes_since_wrap` as the per-prompt subprocess site, which is now one delegation away.

### Fixed — tests

- The activation-scope tests read `LEVAIN_SCOPE` and `CLAUDE_CONFIG_DIR` from the **real environment**, so their verdict depended on the reviewer's shell. `LEVAIN_SCOPE=global` — the value `doctor`'s own hint tells operators to set — turned the dark-config regression guard RED, and `CLAUDE_CONFIG_DIR` made four user-wiring guards pass **vacuously**, silently retiring the two-installs-on-one-machine discrimination. One autouse fixture in `conftest.py`; the suite is now identical under all three environments.
- The source-checkout skip guarded one of two sibling tests, eight lines below the comment explaining why it must exist. Hoisted into the shared helper.

### Upgrading

⛔ **`pip install -U levain` DOES NOT REFRESH YOUR INSTALL'S ACTIVATION FILES OR SEED.** It never has. The templates ship inside the wheel, but `levain init` COPIED them into your install when you created it, and no upgrade path rewrites those copies — `levain update` reconciles the memory library and surfaces changes for review, it does not apply them. This is the single most-repeated cause of "I upgraded and nothing changed" in this project's history.

**What you get from `pip install -U levain` alone:**

- every `doctor` fix above — the user-level wiring detector, the pack-hook staleness fix, the corrupt-lock refusal;
- both Content-Length fixes in `levain serve` and the onboarding server;
- the `--consolidate-max-seconds` message fix;
- the job-store lease behaviour change.

**What additionally requires `levain init --force`** (it replaces the activation tree, backing up your edits to `posture.md` and `recency_directives.md` first, and preserves your anneal-memory store):

- the reworded `[wrap blocked]` hook message;
- the seed's uncapped graduation ladder — **relevant if your entity was scaffolded before this release**, since it was being taught a ladder that stopped at 3x while the library had removed the ceiling;
- the corrected Codex adapter README and operator manual.

⚠ **Run `levain doctor` after upgrading.** If you wired Levain's hooks at the user level with a `~` or `$HOME` path, this release is the first one that can SEE that — so a `doctor` run that passed before may now correctly FAIL with `activation scope`. That is not a regression: it means your activation layer has been silently off in those sessions and `doctor` previously could not tell you.

## [0.4.3] — 2026-09-02

**`doctor` no longer fails a correct install rooted at `$HOME`.** A one-line fix to a defect that shipped in 0.4.2, found by review within the hour.

### Fixed — the install's own settings file was read as user-level wiring

For an install rooted at `$HOME`, `<install>/.claude/settings.json` and `~/.claude/settings.json` are **the same file** — the one `levain init` wrote. `_check_activation_scope` read it as a deliberate operator choice, reported the activation layer dark, and made `levain doctor` exit nonzero on an install that was working perfectly.

⚠ **This is the third time `doctor` has gone red for a whole class of operators**, and all three are one mistake: confusing the file the *installer* wrote with a signal of operator *intent*.

- **0.4.0** — `_check_hook_freshness` compared every install against the Claude Code tree, so every Codex install failed on a false "stale hooks" report.
- **0.4.2's first draft** — the new scope check treated `~/.codex/hooks.json` as user-level wiring, when Codex has no per-project hooks file and `levain init` writes that path itself. Caught before release by running `doctor` against a real install of each adapter.
- **This one** — the Claude Code equivalent, which that adapter-by-adapter check did not reach because it needs an install at a specific *location* rather than a specific adapter.

The function's own docstring already named the discriminator (*"`levain init` writes `<install>/.claude/settings.json`, INSIDE the install"*); the code simply never tested that identity. It does now, and a genuinely dark configuration still fails.

### Fixed — the 0.4.2 hook fix had no test at any of its four call sites

Not a shipped defect — the code was correct — but the reason it could stop being correct without anyone noticing. The suite covered `wrap_state()` and `format_wrap_blocked()` as units and nothing covered the **routing** between them, which is the entire finding. Replacing the call sites with `state = (hook.episodes_since_wrap(), False)` reintroduces the original bug completely and leaves the full suite green.

That is the same structural failure that let the bug ship in the first place: the function under the activation layer having no test that runs it. Four parameterised tests now drive each hook's `main()` on **both** adapter trees and assert what the entity is actually told.

## [0.4.2] — 2026-09-02

**Four field-reported defects from one adopter, all in the activation layer.** Reported by [Alex De Groodt](https://github.com/Hurleveur) on 2026-08-04 against 0.4.1. No API removals. Default behaviour is unchanged for every install that does not opt in.

Requires **anneal-memory >= 0.9.8** (up from 0.9.7) — see *Upgrading* below.

### Added — activation scope: an explicit global opt-in

The activation hooks fire only for sessions whose working directory is inside the Levain install. That default is deliberate and is **not changed here**: it is what stops an unrelated session in another codebase inheriting this partnership's posture, recency directives and memory surfaces.

What was missing was any way to *say* you wanted the other behaviour. An operator who moves Levain into a global tool and wires the hooks at user level works in project directories all day, and every one of those sessions sat outside the install — so posture injection, recency directives, spore surfacing, crystallized-pattern recall and the wrap nudge were **all silently off**, while `levain doctor` reported the install healthy.

Two channels, both new:

- `.levain/config.json` → `{"scope": "global"}` — durable, survives `levain update`, and visible to `doctor`.
- `LEVAIN_SCOPE=global` — a per-session override that wins over the config.

**Fail-closed on the contamination axis:** anything that is not exactly `global` (absent, misspelled, wrong type, malformed JSON, unreadable file) resolves to install scope. Silently staying scoped is the status quo; silently going global leaks a partnership's posture into someone else's workspace, so an unreadable config must never be the thing that opens the gate. `LEVAIN_HOOK_SUPPRESS=1` still wins over both.

Applied to **both** adapter copies of the hook — Claude Code and Codex — and the shared surface is pinned byte-identical by test, because the two copies have drifted before.

### Fixed — the wrap nudge told you to run the one call that could not work

`episodes_since_wrap()` fetched anneal's full `status --json` and **discarded `wrap_in_progress`**. With a wrap left open by an earlier session, `prepare_wrap` can only raise — and Layer D plus the `[wrap check]` line went on advising "run prepare_wrap" every single prompt while episodes piled up behind it. The reporter sat at 29, then 31 episodes against a threshold of 12, for three days.

This was a **routing defect, not a missing feature**: `dashboard.py` reads that field at three sites and `tui.py` reads it too. Levain already knew. The one consumer that fires on every prompt was the one dropping it.

- New `wrap_state()` returns `(episodes_since_wrap, wrap_in_progress)` from a **single** `status --json` call — the nudge runs inside a tight per-prompt timeout budget, so re-fetching a field already in hand would double the subprocess cost of every prompt.
- All four advice sites (`session_start` and `user_prompt_submit`, on both adapters) now branch on it and emit `[wrap blocked]` instead, which names both ways out: finish the wrap with `save_continuity`, or abandon it with the `wrap_cancel` MCP tool / `anneal-memory wrap-cancel`.
- `episodes_since_wrap()` is retained as the count-only view for callers that do not give advice.
- A missing or non-bool `wrap_in_progress` degrades to `False` rather than failing the whole read, so an older anneal still gets the ordinary nudge.

### Fixed — `doctor` never mentioned the activation gate

A green `doctor` was compatible with the entire activation layer being off, because every static check reads **files** — hooks present, wired to this install, python resolvable, store open — while what actually decides whether a hook emits anything is a **runtime gate** doctor did not report on.

New `activation scope` check:

- reports the configured scope, and where hooks fire, in every run;
- **FAILS** on the one combination that is genuinely dark: install-scoped, while this install's hooks are wired from `~/.claude/settings.json` — which for a Claude Code install is something the operator did deliberately, since `levain init` wires `<install>/.claude/settings.json` instead;
- **never fails a Codex install for its global hooks.** Codex has no per-project hooks file, so `levain init --adapter codex` writes `~/.codex/hooks.json` itself; treating that as operator intent would fail every Codex install in existence. Codex installs get a fuller report instead, naming the condition explicitly: the hooks run in every Codex session and stay silent outside the install;
- treats user-level wiring as correct once global scope is opted into.

Deliberately narrow and purely additive. It does not touch `_check_hook_freshness` or anything else the pending doctor redesign owns.

> The first draft of this check **did** fail every Codex install — the same shape as the 0.4.0 defect below, in the release built for the same reporter. The suite was green for it both times. What caught it was running `levain doctor` against a real install of each adapter before tagging.

### Fixed — the README misstated the anneal-memory pin

It claimed `>=0.9.6` while the package declared `>=0.9.7`. Corrected, and now **asserted by test** against `pyproject.toml` and `KNOWN_GOOD_ANNEAL` — the same class of defect as everything else in this release (a description that disagrees with correct code), so the fix is an assertion rather than a corrected number.

### Fixed — defects found by review INSIDE this release

Every item below is a bug in code written earlier in this same release, caught by the review mesh before it shipped:

- **The hooks gained a new way to go silently dark.** A pack may layer its own `_levain_hook.py` over the base activation tree, so a 0.4.2 entry point can end up composed against a pre-0.4.2 helper with no `wrap_state`. The AttributeError was swallowed by the structural fail-open catch and every hook emitted **nothing**. The entry points now feature-detect and degrade to the old count-only nudge. Shipping a fresh silent-dark path in the release whose whole purpose is ending one is not a trade worth making for a shorter call.
- **`[wrap blocked]` told the agent cancelling was free.** It said "Nothing is lost either way" while recommending `wrap_cancel` — but anneal runs one server process per client session against a shared store, so the wrap may belong to a **live** sibling mid-compression, and cancelling discards its work. The line now says whose wrap it may be, tells the agent to check `anneal-memory wrap-status` first, and says plainly what cancelling destroys.
- **`doctor` asserted an activation scope it could not verify.** `LEVAIN_SCOPE` was ignored on the grounds that doctor's environment is not the session's. That holds for a per-invocation override and breaks the moment it is exported from a shell profile — misreporting in both directions, including a hard FAIL on an install that was working fine. Doctor now names the variable and the ambiguity instead of asserting past it, and no longer fails an install whose gate the environment has opened.
- **`doctor` crashed on a malformed foreign config.** `{"hooks":{"SessionStart":[null]}}` raised an uncaught `AttributeError` that aborted the whole run — breaking the function's own documented promise never to hard-fail on someone else's file.
- **User-level wiring detection missed real cases:** it inspected only the first command found (so a foreign hook registered ahead of Levain's hid it), ignored `CLAUDE_CONFIG_DIR`, and treated `${CLAUDE_PROJECT_DIR}` wiring as pointing at this install when at user level it resolves to whatever project is open.
- **A stale `AGENTS.md`** left behind by an adapter switch produced a Codex diagnostic on a Claude Code install; the check now uses the shared `effective_adapter` classifier.

### Also in this release — work that accumulated since 0.4.1

⚠ **0.4.2 is not only the four items above, and saying otherwise would be the same defect they describe.** The `v0.4.1` tag is old; a month of work sat on `main` behind a deliberate decision not to cut a release for it (recorded at the time in *"Record what is fixed, what is open, and why there is no 0.4.2"*). All of it ships here:

- **`doctor` hook-freshness, closed on both axes** — the check now works in relative-path space and iterates the union of keyspaces, and asks the manifest which hooks a pack owns. This was the fourth recurrence of one bug shape; a fixture that had been missing three times now exists.
- **SSH vectors denied at every spelling**, not just the resolved one (`levain/firing/confinement.py`).
- **Five findings closed** from the spore-604 bugfix session.
- **The anneal floor moved to `>=0.9.7`** (AM-LEVELCAP) before this release moved it again to `>=0.9.8`, and compat fixtures now derive the version instead of hard-coding it.
- **The seed no longer teaches crystallize-OUT** while the level ladder is capped.
- Routine routing of the nightly reviewer's findings into project memory (documentation only; no shipped code).

*(This section was added after publication. The `0.4.2` sdist on PyPI carries the release notes without it — the omission was in the notes, never in the code.)*

### Upgrading

- **`pip install -U levain` will also upgrade `anneal-memory` to >= 0.9.8.** This is a real API contract, not a lockstep bump: the new `[wrap blocked]` advice names the `wrap_cancel` MCP tool, which does not exist before anneal 0.9.8. Against 0.9.7 the hook would name a tool the agent cannot reach — precisely the defect this pair of releases exists to fix.
- **If you wired Levain's hooks globally and your activation layer has been quiet, this is why.** Add `{"scope": "global"}` to `.levain/config.json`, then run `levain doctor` — it now tells you which way it is set.
- **If you are carrying a local patch to `in_install_session()`, drop it.** What kept eating it is `levain init --force` — step two of the documented upgrade procedure (`pip install -U levain`, then `levain init --force`), which replaces the whole `activation/` tree. `levain update` was never the culprit: it writes nothing under `activation/` at all. The supported opt-in lives in `.levain/config.json`, which neither command touches.

## [0.4.1] — 2026-08-02

**`doctor` is no longer permanently red for Codex operators.** `_check_hook_freshness` compared every install's hooks against the Claude Code template tree, so every Codex install failed on a false "stale hooks" report. It shipped in 0.4.0 — the release built to carry this same reporter's previous fixes.

## [0.4.0] — 2026-08-01

The governed seat, the efferent gate, and three field-reported fixes.

> ⚠ **UNDOCUMENTED AT THE TIME, AND IT CHANGED DEPLOYMENTS SILENTLY.** This release deleted the hook's `$CLAUDE_PROJECT_DIR` read. That read's containment check accepted **any ancestor of the hook file** as the install root, which could resolve the whole activation layer under the wrong root — so an install with globally-wired hooks may have been passing the session gate *by accident*. Tightening `install_root()` to derive from the hook file's own location was correct, but for anyone in that position it turned activation off, with no note in any release material saying so and `doctor` still reporting healthy. **0.4.2 is the release that gives that operator a supported way to get the behaviour back** (`scope: "global"`), and a `doctor` check that says which way the gate is set.

---

## Versioning — why the tree is stamped `.devN` between releases

A release tag names a tree. The commit *after* it does not, and for one window in 0.4.3 both read `version = "0.4.3"` while the second had rewritten a shipped hook message — so "0.4.3" named two different trees, and what a developer cloned was not what an adopter installed, with nothing in the tree saying so.

**The rule: the release commit is the last one on that number.** The next commit bumps both stamps (`pyproject.toml` and `levain/__init__.py`) to the next `.dev0`, and its changes go under `## [Unreleased]`. Cutting a release drops the suffix in the same commit that tags it. This is asserted by `tests/test_manifest.py::TestReleaseStampIsNotAPublishedVersion`, not left to the release checklist — the checklist is what missed it.
