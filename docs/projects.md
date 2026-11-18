# Projects: tracked and untracked

Levain knows each repo in one of two modes. Both are supported. The mode decides where the repo's state lives. It does not change the repo's build or test commands, or its conventions files, as long as `retire` does not name them.

- **Untracked** is the default. The repo keeps its own state (its own files, issues or status page), and a seat reads those files.
- **Tracked** moves the repo's state onto Levain's work board. The repo keeps one file that says what it is for, `CHARTER.md`, on its main branch.

Every command here is a `levain board` or `levain charter` verb. `levain board <verb> --help` and `levain charter <verb> --help` print every flag. For `trail`, the flags are on `levain board trail export --help` and `levain board trail verify --help`.

## The two modes

| | untracked | tracked |
| --- | --- | --- |
| What the operator said | nothing, or "work in X" | "track X", said in conversation with the desk |
| INTENT (what the repo is for) | the operator's own files, if any | `CHARTER.md` at the repo root, on origin/main, checked by `levain charter check` |
| Conventions (how to build and test) | the repo's own (AGENTS.md, CLAUDE.md, CONTRIBUTING) | the same files. Tracking does not change them, as long as `retire` does not name them (see Known edges) |
| STATE (what is open, in flight, built, done) | the operator's own files | the board: items in Levain's store, with state derived on every read from git, seats and the band. No file holds it by hand |
| RECORD (decisions, handoffs, findings) | the operator's own | episodes in the operator's memory store, tagged `repo:<name>` and `item:<name>#<n>`. Artifacts (design pages, review transcripts) are files in a record directory outside the repo's git |
| The board | holds no live items for it (once the store has a registry; see Known edges). An abandoned tracking leaves its cut items behind | holds its items |
| A seat's context | the repo's own files | `levain board context <name>#<n>`, plus the conventions files |

An untracked repo is fully supported. The desk launches seats in it, and a seat reads the repo's own files. Levain draws no board column for it.

Three rules hold in every install:

- **The board takes new items only for repos that are tracking or tracked.** Once a store has a registry, `levain board add` refuses any other repo, and says why. An `aborting` repo is refused too. A repo whose tracking was abandoned keeps the items that were cut; they stay in the store (see Known edges). Before a store's first registration that check is off; see [Known edges](#known-edges).
- **Levain writes no state.** Stored records are registrations, items, decisions and mode changes. State is computed when you read it.
- **A repo is in play only by a `register` record.** A directory scan can find a repo; finding it does not put it in play.

## Untracked: what Levain recommends

An operator with no method of their own can keep the three things apart inside the repo:

- one `CHARTER.md` for INTENT, which changes slowly and which any contributor or tool can read;
- one status file for STATE, which only one writer touches;
- one append-only log for RECORD.

Tracking then starts from a conversation: the desk reads those files, drafts the charter and the items, and the operator says yes.

## Tracking a repo

Tracking is a conversation first and a ceremony second. Before the yes, two things can be written, and neither tracks the repo: the store setup in step 1 (on a fresh install only), and the registration, which puts the repo in play. Everything else waits for the yes.

1. **The operator says "track X".** If this install has no item store yet, the desk runs `levain board use <dir>` first. Then it puts X in play if X is not already there:

   ```
   levain board register X --words "<the operator's words>" --by <desk> --path <repo directory>
   ```

   The repo needs at least one commit. Without one, `register` refuses: nothing identifies the repository. `register` and `charter init` take one name rule, `[a-z0-9][a-z0-9._-]*`: lowercase letters, digits, `.`, `_` and `-`, starting with a letter or digit. `register Alpha` prints `refused: repo 'Alpha' must match [a-z0-9][a-z0-9._-]*`, and `charter init` refuses a name outside the rule in the same terms: `levain charter init: the name 'Alpha' must be lowercase letters, digits, '.', '_' or '-', starting with a letter or digit (the name `levain board register` takes, and the prefix of the repo's item ids).` A name `register` accepts is a name the charter and its Plan lines accept, so a repo registered as `v2.app` or `2fa` can reach `track`. X is a name, not the directory's name; the directory is what `--path` says.

2. **The desk reads X's existing memory, read-only.** That is `next.md` or `brief.md`, any project memory, status pages, TODO files, the README and the conventions files.

3. **The desk drafts one review page.** It drafts the charter with `levain charter init X --stdout`, which prints the skeleton and writes nothing. It does not look at a `CHARTER.md` already in the repo, so it works there too. The file reaches the repo only when step (c) lands it. The page holds the charter, the proposed items (a title, the operator's words verbatim where the old files carry them, a DONE line and a `done_kind` for each), what is not carried and why, which old files will be retired, and which conventions belong in the repo's conventions file rather than the charter.

4. **The operator reads it and answers in conversation.** Edits go back to step 3.

5. **The desk runs the ceremony in this order.** A keyed `add` repeats safely (see (b)), and `begin-track` derives its migration id when `--migration` is omitted (from X's name, the manifest's bytes, `--words` and how many ceremonies X has had), so a retry with the same manifest and `--words` derives the same one. The ceremony's decision-class writes (`begin-track`, `track`, `decide`, and `abandon-track` on the abort path) each carry provenance: `--sign` or `--source <q-id>`, as [Signing and relaying](#signing-and-relaying-decision-class-writes) says. Without one the write is refused. The examples below carry `--source`, because the desk runs the ceremony and relays the operator's answered band row.

   - **(a) Begin.** Writes the migration id and locks X to new work. Decision-class, so it carries `--source` (or `--sign`). The band question the desk files for this yes must carry the sha256 of the manifest file, so the answer binds exactly that manifest:

     ```
     levain board begin-track X --words "<a verbatim excerpt of the operator's answer>" --by <desk> --manifest <file.json> --source <q-id> [--owner <desk>] [--migration M]
     ```

     The manifest is JSON. `plan_items` names the item keys (the `<k>` in each `M:<k>`), and the charter's Plan lists the ids those keys produce. `adopt_items` (optional) names X's EXISTING items, by id (`X#N`), that the ceremony takes in: they keep their id, records, state and evidence, and nothing is copied. `begin-track` refuses an entry that is not in the store, is another repo's, is cut, is parked or out of 1.0 (a parked item cannot be adopted: reopen it first), is already keyed `M:<k>` to this ceremony, or repeats, and it names every bad entry in the one refusal (`--manifest adopt_items: 2 bad entries: alpha#99 is not an item in the store; ...`). The yes binds the LIST of adopted ids, not the items' content: an adopted item is live work, and edits made to it during the ceremony stay. The charter's Plan must then be exactly the adopted items plus the keyed ones, less any cut. Items X already has that are in neither list stay on the board unadopted and out of the Plan; `levain board repos` names them (count and ids) while X is `tracking`, as information only. After `track`, `charter check --items` warns "in no plan group" for each of them: that is expected, not a defect. `contract_items` names the repos that hold stubs to land. Each of those repos gets a stubs item keyed `M:stubs:<repo>`, and `add` refuses that key while X is `tracking` ("add the ... item only after `levain board track`"), so the desk adds it after (d). `retire` (optional) names the old memory files to copy and stub, each as a `{repo, path}` object (a bare path string is refused: `a retire entry is exactly {repo, path}`; an entry whose repo is not registered is refused at `begin-track`, naming `levain board register`), and `record_dir` (optional) names where the copies go. A complete manifest:

     ```json
     {
       "plan_items": ["k1", "k2"],
       "contract_items": ["alpha"],
       "retire": [{"repo": "alpha", "path": "status.md"}],
       "record_dir": "/path/to/records"
     }
     ```

     `begin-track` prints the migration id: `tracking alpha (migration mf538fa9a1e)`. That id is the `M` in `--key M:<k>`; `board repos` shows it too, and a retry prints `already tracking alpha (migration ...)` with the same id, but only when the manifest's bytes, `--words`, `--owner` and the migration id (given or derived) all match the first call, and, on a relayed call, the same `--source`. Any other `begin-track` on a tracking repo is refused: `alpha is already tracking (migration M): finish it with `track` or `abandon-track``. While X is `tracking`, a seat launched on X must carry the migration id, and `board add` for X must be keyed to it. `board repos` shows the step that is next.

   - **(b) Expand: the items.** Items X already has are not added again: they enter the ceremony by id, through the manifest's `adopt_items` in (a), and keep their history. Only new items are added, each with its key and a hash of its fields:

     ```
     levain board add --repo X --title "<title>" --origin "<origin>" --by <desk> --key M:<k> --payload-hash <sha256>
     ```

     The same key with the same hash returns the existing id. The same key with a different hash is an error. Keys never expire. Each ceremony has its own migration id, and the key carries it, so a second ceremony's items get new ids. The items exist but are outside the board's scope until the switch.

   - **(c) Expand: the charter.** The item ids go into the charter's Plan: `levain charter init X --repo <X's directory> --items <store>` writes it with the Plan prefilled. While X is `tracking`, that is the manifest's adopted items plus the items added in (b) under the ceremony's keys, less any cut (the set `track` will check; an adopted item parked mid-ceremony stays in the Plan until it is cut); if the registry of a store that is found cannot be read, `init` refuses (exit 2) rather than guess the repo's mode; for a repo in any other mode, every item of X not cut, parked or out of 1.0. The desk edits the rest by hand. `CHARTER.md` lands on X's main through X's own landing path. `levain charter check --repo <X's directory> --ref origin/main --repo-name X --items <store>` reports `X's CHARTER.md (origin/main) is a valid charter.` once it is. The charter's title must name the registered repo X.

     **When X already has a `CHARTER.md`** (an untracked repo that kept the one this page recommends), `init` without `--stdout` refuses, exits 1 and writes nothing. A run on a scratch repo that held a `CHARTER.md`:

     ```
     $ levain charter init alpha --repo <alpha> --items <store>
     levain charter init: <alpha>/CHARTER.md already exists; it is never overwritten.
     $ levain charter init alpha --repo <alpha> --items <store> --stdout
     ## Plan
     - alpha#1
     - alpha#2
     ```

     (abridged to the Plan; the first command exited 1 and the second 0.) The desk has two ways. It can edit the existing file into the charter's shape, with the Plan lines `--stdout --items` printed, and check it with `levain charter check --worktree --repo <X's directory> --repo-name X --items <store>` before it lands. Or it can move the old file aside (name it in `retire` if it is state to retire), run `init` and land the new file. A cut item is left out of the Plan that `init` prints.

     Both verbs act on the git work tree they run in unless `--repo` names another (outside any work tree `init` exits 1 with `. is not inside a git work tree.`). When X is registered, `init`, and `check` with `--repo-name X`, refuse with exit 2 and touch nothing when they would write or read a repository other than the registered one: `levain charter init: alpha is registered at <registered path>, but this would write CHARTER.md in <here>. Pass --repo <registered path>.` (`check` says `... but this would read <here>. ...`). A worktree of the registered repository is the same repository and is fine. A clone is a different repository, so it is refused: `levain charter check --repo <clone> --ref HEAD --repo-name alpha` printed `levain charter check: alpha is registered at <alpha>, but this would read <clone>. Pass --repo <alpha>.` and exited 2. The guard stays open only when there is no store anywhere, or X is not registered. It also refuses with exit 2 when the registry, the store pointer or the registered checkout cannot be read, so it cannot tell. `check` is guarded only when `--repo-name` names X, and `check --file` is never guarded. `init --stdout` writes nothing and skips the wrong-repository guard; given `--repo`, it still needs that path to be a git work tree (`levain charter init alpha --stdout --repo <not a repo>` exited 1 with `... is not inside a git work tree.`). Without `--repo-name`, `check` takes the repo's name from the directory that holds the repository's `.git`, so when the directory is named otherwise (a clone called `clone-dir` for repo `alpha`) the title reads `names 'alpha', not this repository's name 'clone-dir'`. To check a clone's charter under X's name, check the registered checkout, or use `--file`.

     `check` only reports and exits 0 on the charter's content (the wrong-repository refusal above is the one exception); `track` is the gate. `check --items <store>` also warns about X's own items that the Plan does not name and that are not cut, parked or out of 1.0 (`alpha#2 is in the store and in no plan group`). It does not warn about another repo's items, and the Plan must not name them (`alpha#1 belongs to another repo; list only alpha#<n> ids in the Plan`). A warning is not a reason to add an id to the Plan: `track` runs the same check itself and refuses on any problem. It also refuses unless the Plan lists exactly the ids of the manifest's items that were added and not cut.

     To cut an item: `levain board decide --id X#<n> --decision cut --words "<a verbatim excerpt of the operator's answer>" --by <desk> --source <q-id>` (it prints `decided X#<n>`; `decide` is decision-class, so `--sign` is the other way to carry it). A cut item must also leave the landed Plan: `charter init --items` leaves a cut item out of a Plan it writes, but a Plan already landed is edited by hand and landed again before `track` will pass. A stalled landing only means the switch has not happened, and X stays locked.

   - **(d) Switch.** This is the one record that moves the source of truth from the old files to the board. It is decision-class. A relayed `track` is held to the same manifest as (a): its band question carries the sha256 of the manifest that `begin-track` recorded.

     ```
     levain board track X --words "<a verbatim excerpt of the operator's answer>" --by <desk> --source <q-id>
     ```

     It refuses unless (b) and (c) hold: the charter's Plan is exactly the adopted and keyed items, less any cut, both ways (`the manifest's items not in the charter's Plan: ...`, `the charter's Plan ids not in the manifest: ...`). The `track` record carries the adopted ids it moved in (`adopted`). An abort (`abandon-track`) cuts only the keyed items the ceremony added: it never cuts, amends or decides an adopted item, which predates the ceremony. Edits made to an adopted item during the ceremony (an amend, a decision) stay after `abandon-done`. From here the board is the source, and the lock lifts. `add --key` does not check the key's `<k>` against the manifest; `track` does (see Known edges).

   - **(e) Contract: the stubs.** There is no verb for this step; the desk does it by hand. It copies each file named in `retire` to `<record dir>/pre-track-<date>/` with a sha256 line (a file such as `SHA256` holding `<hash>  status.md`; the name is the desk's choice and nothing checks it), then replaces the file at its old path with a stub, and commits and pushes it when git tracks the file. A retired file outside git (a gitignored project-memory file) has nothing to commit: the stub is a working-tree stub, it stays in that one checkout, and the stub check reads it there, so no `contract_items` entry is needed for it. The stub's first line must be the marker `<!-- levain: tracked stub -->`; the rest is free text (the run used `This file is retired; state lives on the Levain board (levain board state).`). The one thing checked is that marker, in the file in the repo's working tree, not on origin/main. Until a stub is in place, `levain board repos` reports `stale: <path> still holds pre-track content`. A retired path that is deleted rather than stubbed is not reported as stale, so silence does not show that the stub landed. No seat acts on the old content in that gap.

6. **The desk writes one decision episode** tagged `repo:X`, naming the charter, the item count and the record directory.

**Aborting.** Before the switch only. `levain board abandon-track X --words "<a verbatim excerpt of the operator's answer>" --by <desk> --source <q-id>` makes X `aborting` and cuts its items. It is decision-class, so it carries `--source` or `--sign`. Run it again to finish the cuts. The same `--source` is accepted on the retry: the repo is already `aborting`, so the row is read and nothing new is recorded, and each cut still owed is written. A run on a scratch store: the first call printed `aborting alpha; cut 2 items: alpha#1, alpha#2`, and the same call again printed `aborting alpha; nothing to cut` and `the abandon-track <sha> (relayed) stands; --source <q-id> read, not recorded`, exit 0. Different `--words` on a retry are not recorded either, and the line says so. A retry that is relayed or unsigned is refused when the abandon that stands cannot be the cause of the cuts still owed; the message then names the signed re-run (`--sign`) that fixes it. `levain board abandon-done X --by <desk>` returns X to untracked once every item is cut and any compensation, such as a reverted charter, has landed; it is not decision-class and takes no provenance. After the switch there is no abort. Leaving tracked mode is `levain board untrack X --words "<the operator's words>" --by <operator> --sign --author-kind operator`, which refuses while any item is not done, cut, parked or out of 1.0. `untrack` takes `--sign` only: it has no `--source`, so the desk cannot relay it. The operator runs it at the laptop.

**The desk's own door.** A desk seat's brief opens the board verbs except `move`, `pause`, `resume` and `ack-torn`, and the charter verbs `init` and `check`. Those four board verbs run from a terminal.

## Reading the board

`levain board repos` lists each repo's mode and the next step. `levain board state` derives every item's state when you read it, and `levain board context X#<n>` prints one item's context.

- The `as of` time in `board state`'s header is the time of the last fetch of origin, not of the read. `--now` does not move it; it moves when origin has changed.
- On a store with no land-waiter configured, items that rest on the absence of a waiter read `unknown` (`waiters: not supplied`) and `board state` exits 3; an item holding a positive claim (a live seat, an open band row) or closed in git keeps its state. Pass `--no-land-waiter` to read the items without one: `levain board state --no-land-waiter`, and the same for `board context`. The flag declares only the waiters absent. The seats are read from Levain's launch records on their own: a launch carries the items it was started for when it was started with `--item`, and while an active Levain launch was started without it, items that rest on the absence of a seat still read `unknown` naming that launch (`launch <id> carries no board_items (it was started without --item)`), and `board state` still exits 3.
- `board state` exits 3 while its audit is not empty, whatever the items say.

## Charter

`levain charter init NAME` writes `CHARTER.md` at the repository root, with the name and the Export filled in. Its Plan is prefilled with one line per item of that name not decided cut, parked or out of 1.0 when `--items` names a store (for a repo in `tracking`, with its ceremony's adopted and keyed items instead); otherwise the Plan is a placeholder line. Every other section is a `<fill: ...>` placeholder that `check` reports until it is replaced; `check` reports it and still exits 0. `init` never overwrites a file. With `--stdout` it prints the skeleton and writes nothing.

`levain charter check` reads the charter, from origin/main by default. It only warns: it prints what a valid charter needs and exits 0 either way on the charter's content, so it is not a gate. It exits 2, and reads nothing, when the wrong-repository guard runs and refuses, which for `check` means a `--repo-name` that names a registered repo: `--repo` is a different repository from the registered one (a worktree of it is fine, a clone is not); the registry, the store pointer or the registered checkout cannot be read; or `--items` names something that is not a store, or a second store (`levain charter check --help` lists each case). Without `--repo-name`, those `--items` faults do not make it exit 2. An unusable command line exits 2 too. `--file` is never guarded. With `--json` the refusal is still one JSON document with the same keys (`valid` false, `why` `refused: ...`), and the message is also on stderr.

`init` exits 2, and writes nothing, in the same wrong-repository cases whenever it would write into a repository (a worktree of the registered repo is fine; `--stdout` is not guarded) and on an unusable command line. `init` exits 1 on a name outside the rule, on a path that is not a git work tree and on a `CHARTER.md` that already exists. The charter's own name and its item ids (`NAME#N`) are not scanned for a sha-shaped token or a date; prose still is.

## A seat on a tracked item

A worker launched for a tracked item is told to:

- run `levain board context NAME#N` first, which is the one context contract;
- put `Board-Item: NAME#N` on every commit;
- put `Board-Closes: NAME#N` on the commit that meets the item's DONE line;
- put `Board-Seat: <durable key>` on every closing commit;
- write decisions and handoffs as episodes tagged `repo:NAME` and `item:NAME#N`.

The desk's brief teaches these rules. The close check a tracked seat is meant to pass is `check_close` in `levain/desk/seat_close.py`; see Known edges.

## Signing and relaying: decision-class writes

The writer holds one fixed policy, and it checks it before it signs or writes anything. Some writes are decision-class: a record of one is a ruling by the operator, so it must say where that authority came from. An asserted decision-class record is refused.

**Decision-class writes.** `decide`, `begin-track`, `track`, `abandon-track` and `untrack`. `move` joins them when the repo is tracking, tracked or aborting and the move changes its origin or root set, because the repo's evidence would then be read from another repository. The same repository relocated on disk, and any move of an untracked repo, stay asserted. Every other verb (`register`, `add`, `amend`, `pause`, `resume`, `ack-torn`, `abandon-done`) accepts an asserted record.

**Two ways to give provenance.**

1. **`--sign`: the operator at the laptop, Touch ID.** The operator runs the verb in a fresh terminal (not inside an agent session, and not a remote one), with `--author-kind operator`. The write needs a terminal key and the authority log that enrols it, and the record is verified against that log before it is written. It shows the Touch ID prompt; a refused or cancelled prompt writes nothing. No lock is held while the prompt is up, so if another write lands on the store meanwhile the signed write refuses (`the board changed while you signed: sign again`) and writes nothing; run it again. `--sign` is the only way to write `untrack`, and the only way to write a `move` that is decision-class (see above). This page documents it from the verb's help and the code: the prompt needs the operator's hand, so the run below does not include one. Example:

   ```
   levain board track alpha --words "yes, track alpha" --by phill --sign --author-kind operator
   ```

2. **`--source <q-id>`: the desk relays the operator's answered band row.** The desk files the question in the band, the operator answers it, and the desk writes the record citing the row. `<q-id>` is a native question id, `q-` and 24 hex characters; a bare band number or a flow `np-` id is refused (see Known edges). `--words` must be a verbatim excerpt of the answer (one line, no control characters): the operator's sentence travels, never a paraphrase. The row must exist and be answered. `begin-track` and `track` carry one more rule: the question's text must carry the sha256 of the manifest file, so a yes binds exactly that manifest. `--band-dir` names the band store when it is not this install's own. `--source` is taken by `add`, `amend`, `decide`, `begin-track`, `track` and `abandon-track`; `untrack` and `move` do not take it, so a decision-class `move` can only be signed. A complete relayed ceremony, run on a scratch store (the desk filed the question `Begin tracking alpha? manifest sha256 <sha256 of manifest.json>` and the operator answered `yes, begin tracking alpha`):

   ```
   $ levain board begin-track alpha --words "yes, begin tracking alpha" --by "desk 1" --manifest manifest.json --source q-edaeeda02f7993f1ea3219ef --band-dir band
   tracking alpha (migration m93d593e51d)
   ```

   After the items are added and the charter has landed (the question for `track` carries the same manifest digest, and the operator answered `yes, track alpha`):

   ```
   $ levain board track alpha --words "yes, track alpha" --by "desk 1" --source q-e89accf2780146f356c0a6a9 --band-dir band
   tracked alpha (migration m93d593e51d)
   ```

**What an asserted decision write gets.** A refusal, and nothing is written:

```
$ levain board begin-track alpha --words "yes, begin tracking alpha" --by "desk 1" --manifest manifest.json
levain board: refused: begin-track is a decision-class write: it needs --sign (the operator's Touch ID), or --source <q-id> (the band's question id, q-<24 hex>) relaying the operator's answer; an asserted record is refused
```

`track`, `decide` and `abandon-track` refuse in the same words, each naming its own verb. `untrack` does not take `--source`, so its refusal names `--sign` only: `untrack is a decision-class write: it needs --sign (the operator's Touch ID); an asserted record is refused`.

A relayed write that does not hold is refused too, and nothing is written:

- words that are not in the answer: `--source q-e89a...: the words are not a verbatim excerpt of the answer: 'yes please track alpha' (the operator's sentence travels, never a paraphrase; quote exactly what the answer says)`;
- a `begin-track` or `track` whose question does not carry the manifest's digest: `--source q-a164...: the question does not carry the manifest's sha256: the answer cannot bind this manifest`;
- a flow row: `--source np-1b3dd088: np- rows are flow's band and are not read here: levain's relay reads this install's native q- question ids; cite a q- row or use --sign`.

## Authorship and grades

Every record the writer produces since the cutover to the chain carries three fields. A line from before it (the `legacy` row below) has no `prev` and is not the writer's own, so a missing `prev` there is not tampering:

- `author`: `kind` (`operator` or `agent`) and `name`. The writer refuses a record without one. `--author-kind` defaults to `agent`, so an operator writing at a terminal says `--author-kind operator`. An agent's launch is named with `--launch-key`, which is refused for an operator.
- `grade`: how strongly the record was signed. The writer sets it; a caller's record that carries a `grade` or a `sig` is refused.
- `prev`: the hash of the line before it, so the file is a hash chain.

| grade | what it means | how it is written |
| --- | --- | --- |
| `asserted` | A named author, unsigned. The writer cannot tell the operator's terminal from a seat running the same command, so `kind: operator` without a signature is still `asserted`. An agent's name is asserted too, not bound to a key. | A write with neither `--sign` nor `--source`. Only the verbs that are not decision-class accept it. |
| `signed-terminal` | The operator's Touch ID signature, checked against the authority log that enrols the key before the record is written. `author.kind` is `operator`. | `--sign` |
| `relayed` | The desk wrote it, citing a band answer. `author.kind` is `agent`, and the record carries `source`: the row's id, its producer, and the sha256 of the question and of the answer. The words are a verbatim excerpt of the answer. | `--source <q-id>` |

A signed record is a verified fact about who wrote it. A relayed record is the desk's act: it shows which answered row it relays and holds the digests, so the citation cannot later attach to a different answer. It does not carry a signature of its own. A reader sees the row's own grade beside it (a relayed record never claims more than the answer it quotes), and the signature on the answer itself is for the trail verifier to check.

**The chain.** Each line carries `prev`, the sha256 of the line before it, bytes as written. The first chained record commits to the unchained prefix that came before it, if there was one. An edit in the middle of a file breaks the chain at the next line. The board then reads the store as torn, and items read `unknown` until `levain board ack-torn --file <items.jsonl|repos.jsonl> --line N --why "<why>" --by <name>` acknowledges the line. A chain shows that lines were not changed in the middle. It does not show who wrote them: whoever can write the file can rewrite its tail and recompute every `prev`. Authorship comes from a signed grade.

## Who closed an item: `closed_by`

`levain board state --json` puts a `closed_by` on a `done` item whose close is git's: a `merged`, `document` or `run` item. A band close has no git author and carries none. `levain board context` prints the same verdict as a `closed by:` line. No close is ever refused for its author: every one gets a verdict, and the worst verdict is `unattributed`.

**The rule.** The line is the weakest claim the evidence supports. It names the strongest tier that holds, in this order:

| tier | the line reads | what it proves | what it does not |
| --- | --- | --- | --- |
| `signed` | `signed by an operator key <principal>` | The closing commit's SSH signature verifies, the board's own key rule accepts the key as a terminal (laptop-presence) signer, and the key was live in the operator's authority log and enrolled no later than the commit's date. | That a person acted. A key of the operator's signed. The commit's date is set by its signer, so the enrolment check is a sanity check and not a proof of when. |
| `trailer` | `agent <role> <launch>, asserted (trailer)` | A `Board-Seat: <durable key>` trailer maps to a launch record that claimed this item and ran in this item's repo. | Anything about who wrote the trailer: anyone can copy one. It is asserted, never proof. |
| `git` | `asserted by git author <name> <email>` | What the commit's author field says. | That the author is who they say. An unsigned commit of the operator's own lands here and closes. |
| `unattributed` | `unattributed: ...` and the reason | Nothing. It says no commit could be read. | Anything. It is reported, not refused. |

When a stronger tier is not credited, `note` says why: a key the rule refuses, a key enrolled after the commit's date, a key revoked (the log carries no revocation time, so a revoked key is never credited whatever the commit's date), a signature that does not verify, a trailer that maps to no launch, to a launch that claimed other items or none, or to one that ran in another repo. A commit with several different `Board-Seat` trailers reads none. A commit whose committer is not its author (a rebase, a squash merge) also names the committer: `, committed by <name> <email>`. No `note` means every check that was available ran.

**Where the close is read from.**

- A `merged` close carried by a commit's own `Board-Closes` trailer is read from that commit: the one the board state names, which is the latest by committer date, ties by sha.
- A close written as a note (`refs/notes/board`) is read from the commit on that ref that wrote the line, never from the commit it annotates: a note is not covered by the annotated commit's signature. When several note commits wrote the line, the latest addition counts. `commit` is then the note's commit and `close_commit` the annotated one. The note is read as it stands now.
- A `document` or `run` close is the file having been added to main, and nobody performs that. Its later removal does not undo it. The verdict is the tier of the first-parent commit on main that first added the path (for a directory, the first that added a file under it), and the line begins `first added <path>`, so it never reads as the closer. Later edits are not read.

**Two rules for the document and run path, and for the email.**

1. **The literal tracked path.** A document or run close credits the path exactly as the item's `done` line names it and as git tracks it. The path is worked out by its text alone (`..` is folded, nothing is looked up on disk) and git is run from the repo, never from inside the named path. So a symlink is not followed, and a submodule is not entered. A tracked link itself, or a submodule's own entry, credits the commit that added it. A file named through a tracked link (`link/f.txt`), or inside a submodule, reads `no commit on main added it`: git tracks the link, not what is under it. A path outside the work tree reads `outside version control`.
2. **An empty email is kept.** A git author whose email is empty is kept as present-but-empty, never dropped, and shows as `<>` (`"email": ""` in `--json`). A commit with no email does not read as a commit that never had one.

**Which repo is read.** The repo may be given as a relative path, and `board state` with no `--repo` on a store with no registry reads the repo in the current directory: the path and the repo are both made absolute from where it runs, so a document inside the repo is not read as outside it. The repo as given is tried before the toplevel git names, so a repo reached through a link to `.` reads as its whole work tree. A file added to main and later removed is still credited to the commit that added it.

A run on a scratch repo with `cwd` inside it and no `--repo` (`levain board state --json`, abridged): a `merged` item closed by a commit with an empty author email; a document that is a plain file; a document named through a tracked link `dirlink -> docs`; the link itself; a path outside the repo.

```
alpha#1 git  No Mail     asserted by git author                              email ""
alpha#2 git  Doc Person  first added docs/report.md, asserted by git author  email "x@x"
alpha#3 unattributed     the path is in the repo's work tree but no commit on main added it   (done: dirlink/report.md)
alpha#4 git  Link Person first added dirlink, asserted by git author         email "x@x"
alpha#5 unattributed     the path exists outside version control (outside the repo's work tree)
```

The same output came from `--repo .`. The link-to-`.` case and the added-then-removed case are from the code and its tests; they were not run here.

**A run on a scratch store.** Two items closed on a scratch repo by unsigned commits, and a third not yet closed (`levain board state --json`, abridged to `closed_by`):

```
levain#1 done {"tier": "git", "who": "t", "how": "asserted by git author", "commit": "51268ed9...", "email": "t@t"}
levain#2 done {"tier": "git", "who": "t", "how": "first added docs/r.md, asserted by git author", "commit": "959b63a1...", "email": "t@t"}
levain#3 ready null
```

`levain#1` is a `merged` item closed by a `Board-Closes` trailer; `levain#2` is a `document` item whose file was added by a commit with no closing trailer. Neither commit was signed and neither carried a `Board-Seat`, so both read `git`; `levain#2` says it is the commit that added the file. Only the `git` rows were run. The `signed` and `trailer` rows, the note path and the two rules above are from the code and its help.

**`--data-root` and `--desk-dir`.** `levain board state` and `levain board context` read the operator's keys from this install's own cockpit (the one the signed verbs verify against) and the launch records from this install's own desk store. `--data-root DIR` and `--desk-dir DIR` name others. Both are read-only. A default that is absent or unreadable reads "not checked" in `note`; it is never an error and never a silent `git` verdict presented as checked. If attribution itself fails, the board still reads and each git close says `the author could not be read (...)`.

**Known edges of close authorship:**

- **A `signed` verdict says a key signed, not that a person acted.** A key the operator enrolled can sign a commit an agent made.
- **A `trailer` verdict is a copied-trailer risk by design.** It is bound to a launch that claimed the item, not to a key.
- **A launch's repo is read from its working directory.** A seat launched from a directory outside the repo it works in, or from a worktree since removed, loses `trailer` and falls to `git`, with the reason in `note`.
- **A `document` or `run` verdict names the commit that added the file.** Not the commit that finished the work, and not whoever last edited it.

## The provenance trail: `levain board trail`

The store (`items.jsonl`) and the registry (`repos.jsonl`) are two append-only files, each a hash chain. The trail is both files read by someone who does not trust the machine that wrote them. `levain board trail verify` re-checks them; `levain board trail export` packs them into a bundle that can be checked somewhere else. The commands below were run on a scratch store from the code that carries them (`levain board trail --help`, `export --help` and `verify --help` are the reference).

**The rule.** Every line `verify` prints is the weakest claim the evidence supports. What it cannot check it says it did not check; what is malformed, missing or contradictory is a break. Material that claims a signature is never read as unsigned when the signature is missing or bad: that is a break.

**What `verify` checks**, per file:

- the chain (`prev` on every line, the cutover over a legacy prefix, a torn line and its `ack-torn`);
- every record through its file's own fold: a record the fold rejects is a break, whatever its chain and signature say;
- every signature, against the authority log of the machine running `verify` (and that a signed record names this store);
- the writer's policy: after the cutover, a decision-class record that is neither signed nor relayed, and a record graded `legacy` that appears after the cutover, are breaks (a `legacy` line before the cutover is not);
- every relayed citation: both digests, the words verbatim in the answer, the row's own grade. For `begin-track` and `track` it also checks the manifest digest the ceremony recorded.

**Verifying a store.** One verdict per record, then per file the anchor line, the tail line, the relayed line and the kept-head line. A run on a scratch store (items abridged):

```
$ levain board trail verify
authority: the log this install holds, replayed now (seq 2, head h)
items.jsonl (9 lines, head 326f6f82ea03...)
  items.jsonl:1 add alpha#1: legacy
  items.jsonl:3 add alpha#3: asserted by desk 24, unsigned
  items.jsonl:4 decide alpha#3: operator, signed with a ceremony
  items.jsonl:6 decide alpha#4: agent desk 24, relaying q-1b3dd0881b3dd0881b3dd088 (signed answer)
  items.jsonl:8 ack-torn: asserted by phill, unsigned
  note: line 7 torn, acknowledged by phill (asserted, line 8)
items.jsonl: anchored through line 4 (signed at 2026-11-17T12:00:00Z): every record up to it is committed by the operator's key
items.jsonl: 4 records after the last signed record (at 2026-11-17T12:00:00Z) are unanchored: anyone who can write this file could have rewritten them
items.jsonl: 1 relayed record(s) rest on a signed answer: the operator's words are anchored by the answer's own signature, which does not cover the chain
items.jsonl: no kept head given: a truncation of this file is not checked (`--head items=<hash>` from an earlier export)
repos.jsonl:4 begin-track beta: agent desk 24, relaying q-0000000a0000000a0000000a (unsigned answer)
repos.jsonl: 1 relayed record(s) rest on an answer no signature of a key you hold binds: nothing anchors the operator's words
levain board trail verify: no breaks
```

The sample's `items.jsonl:1 ... legacy` line is before the cutover, which is why `verify` still ends in `no breaks`. The words, as the code prints them:

- `legacy`: a line from before the chain began. `asserted by NAME, unsigned`: a named author with no signature. `operator, signed with a ceremony`: the operator's signature verified against your authority log.
- `agent NAME, relaying q-... (signed answer)` and `(unsigned answer)`: the desk relayed a band answer, and the answer's own signature does or does not bind the operator's words. A relayed record is never promoted past its answer.
- `anchored through line N`: a signed record at line N commits every record up to it, so none of them could have been rewritten without the operator's key. The tail line then counts the records after the last signed one as unanchored: anyone who can write the file could have rewritten them. The tail line is always printed.
- `NOT ANCHORED`: no signature stands as an anchor. It is printed when the only signatures that verify are under keys a bundle names (below), when no signed record verifies, when the file breaks at or before the last signed record, or when the signing key has since been revoked. A key revoked since is reported, never silently valid and never a break by itself. It reads differently by line: the `NOT ANCHORED` line says `its signer's key is revoked since (seq N)`, and a signed record's own line ends `, by a key revoked since (seq N)`. A close line says something else again (below). Only the bundle-keys case was run; the other three are from the code.
- A line that names who closed an item quotes the words of [Who closed an item: `closed_by`](#who-closed-an-item-closed_by); that section says what each tier proves and does not.
- `no kept head given`: the file can be cut at its end and still verify. Give `--head items=<hash>` (and `repos=<hash>`), the heads `export` printed. A head that is still on the chain reads `kept head ... is the current head: nothing was removed`. One that is not reads `TRUNCATION OR REWRITE: kept head ... is not on this chain (the file now ends at ...): records were removed or rewritten`, and that is a break.

**Exit codes.** `0`: no break. `3`: any break (a chain break, a record its fold rejects, a signature that does not verify, a relayed citation that does not hold, a head that is gone, a close the evidence refutes); `verify` then prints `levain board trail verify: N breaks` and a `BREAK` line per break. `2`: it could not run (the store cannot be read, a bad argument). The `2` case is from the help and the code; it was not run here.

**Who closed each item.** After the file lines, `verify` prints one line per closed item whose close is git's, in the words of [Who closed an item: `closed_by`](#who-closed-an-item-closed_by) and derived by the same function `levain board state` uses. A band close prints as the live board prints it. A live store has nothing recorded to compare with, so its close lines are never a break. A run on the scratch store with one `merged` item, one `document` item and one document no commit added:

```
$ levain board trail verify
...
close alpha#2: asserted by git author Plain Person <x@x>
close alpha#3: first added docs/report.md, asserted by git author Doc Person <x@x>
close alpha#4: unattributed: the path is in the repo's work tree but no commit on main added it
levain board trail verify: no breaks
```

`export` writes each close's `closed_by` into the bundle. `verify BUNDLE` does not take it on trust. It re-derives each close from the repo on the verifier's own install and judges the recorded close one predicate at a time: the commit, the signature, the trailer, the git author, the email, the committer and the `how` words. Each predicate holds, is refuted, or is undetermined. The same bundle, verified:

```
$ levain board trail verify out/trail-....json
...
close alpha#2: asserted by git author Plain Person <x@x> (re-derived from the repo; matches the bundle)
close alpha#3: first added docs/report.md, asserted by git author Doc Person <x@x> (re-derived from the repo; matches the bundle)
close alpha#4: unattributed: the path is in the repo's work tree but no commit on main added it (re-derived from the repo; matches the bundle)
levain board trail verify: no breaks
```

What a close line can say:

- `matches`: every predicate holds, and this install derives the tier the bundle records.
- `holds; this install also derives ...`: every predicate holds and this install derives a stronger tier. A weaker true claim needs no excuse.
- `BREAK close ...`: any one predicate is refuted. The line names the predicate and what the repo shows. One refuted predicate is a break, whatever else holds. A recorded close for an item the bundle's own records show open or closed by the band, or do not hold at all, is a break too, as is a recorded commit when the repo holds a different close for the item.
- `recorded in the bundle, not re-derived here: <why>`: a predicate is undetermined, and the reason is something this install lacks: no authority log, a key its log does not hold, no launch record here (launch records belong to one install), an unreadable object, or a repo that does not hold the commit. An undetermined predicate never hides a refuted one, and `verify` still exits `0`. Exit `0` there means nothing was shown wrong, not that the close was checked. When the repo itself is not reachable the line reads `(recorded in the bundle, not re-derived (repo not available))`.
- A close the repo shows that the bundle does not record is said, and is not a break. A bundle that records no closes prints this install's own derivation and says so.
- A signature that verifies under a key this install's log has revoked reads `signature by <principal> verifies; that key is revoked in this install's authority log (seq N)`. On a close line the wording never says "since". It is undetermined, so it exits `0`. Not run here; from the code.

Nothing in a bundle relaxes a check. A `degraded` flag it records is ignored, and its `note` is printed as the bundle's and is never compared. A recorded close the bundle says it could not read claims nothing, and `verify` prints what this install derives.

One altered close. The scratch bundle was edited so the author of `alpha#2` reads `Someone Else`:

```
BREAK close alpha#2: the bundle records [asserted by git author Someone Else <x@x>] and the repo derives [asserted by git author Plain Person <x@x>] (refuted: git author: the commit's git author is Plain Person)
close alpha#3: ... (re-derived from the repo; matches the bundle)
close alpha#4: ... (re-derived from the repo; matches the bundle)
levain board trail verify: 1 breaks
```

It exited `3`. A second edit, `alpha#2`'s tier changed to `signed`, also broke, with `refuted: email: a signed close carries no email; how: ...; signature: the commit carries no signature`. With the repo moved away, the same unedited bundle printed each close as `(recorded in the bundle, not re-derived (repo not available))` and exited `0`. The `signed`, `trailer` and revoked-key undetermined cases are from the code.

**A bundle.** `levain board trail export` writes one JSON file (format `levain-board-trail/1`) holding the records as written, the public keys the authority log holds, every band answer a relayed record cites, and the author verdict of each git close, and prints the two heads to keep:

```
$ levain board trail export --out out.json
bundle: out.json
items=<hash> repos=<hash>
```

The bundle carries the operator's words, so a destination inside a git work tree is refused, as is overwriting a file. With no `--out` it goes to `<levain home>/board-trail/`.

**A bundle's own keys are never the trust root.** `verify BUNDLE` replays the authority log of the verifier's own install (`--data-root` names another) and reads the bundle's keys as claims compared against it. On a machine that holds the authority log, a bundle gives the same verdicts as the live store, and the header says so: `authority: the log this install holds, replayed now (seq 2, head h); the bundle's 1 key(s) were compared to it`. A key the bundle names that the log does not hold the same way is a break. On a machine with no authority log of its own the same bundle reads:

```
authority: the keys the bundle names (seq 2, head h), NOT checked against an authority you hold: no authority log at this data root
  items.jsonl:4 decide alpha#3: signature valid under a key the bundle names; not checked against an authority you hold
items.jsonl: NOT ANCHORED: the signatures were checked only against keys the bundle itself names (no authority log of yours could be read (no authority log at this data root)): nothing here is committed by a key you hold
levain board trail verify: no breaks
```

Exit `0` there means nothing is broken, not that anything is anchored.

**Known edges of the trail:**

- **The tail is not anchored, and a bundle edit there passes.** A record after the last signed one can be rewritten and its chain recomputed. On the scratch bundle, changing a title in the last (unsigned) item and verifying the result gave `no breaks` and a different head. Only a kept head from an earlier export catches it.
- **A signature is checked against the authority log as it is now.** Whether it was valid at the record's own time is not checked.
- **An answer's signature binds the operator's words, not the chain.** An option pick is signed as `(letter) label`; the verdict then says the signature covers the pick and not the words the record quotes.
- **A revoked key is not told apart by time.** A signed close under a key this install's log has revoked reads as undetermined. An unsigned bundle carries no export time, so a promotion made after the revocation looks the same as an honest export made before it.
- **Asserted `move`s cost O(M squared).** Each asserted `move` is judged on the registry as it stood before it. Measured on commit 0c79ee3aa with 2,000 moves: about 28 seconds; not re-measured since. A registry that long is not a real store.

## Known edges

- **A signed decision rests on the operator's hand.** `--sign` needs a Touch ID prompt in a fresh terminal, a terminal key and an authority log that enrols it. A desk seat cannot sign, so a desk relays with `--source`, and `untrack`, and a `move` that changes a tracking, tracked or aborting repo's origin or root set (neither takes `--source`), wait for the operator.
- **`--source` takes native band ids only** (`q-` and 24 hex characters). A flow row (`np-…`) is refused: levain's relay reads this install's own q- ids, and an np- row belongs to flow's band and is not read here.
- **The phone route is not on this page's ceremony.** The writer takes a phone signature (`signed-phone`), but this page's ceremony uses only `--sign` (Touch ID) and `--source`.
- **`retire` is not checked against the conventions files.** Keep AGENTS.md, CLAUDE.md and CONTRIBUTING out of it. The code does not refuse them.
- **A tracked seat's close check is meant, not enforced.** `check_close` in `levain/desk/seat_close.py` is a pure function over facts its caller reads. It is the close check a seat is meant to pass. The board does not refuse a close on its own account: whether anything calls `check_close` is what `git grep -n 'check_close(' -- levain` shows, and with no caller a seat can close an item without the trailers.
- **Before a store's first registration, `add` takes items for any repo.** The registry is inactive until the store has a `repos.jsonl`, and the mode check starts with it.
- **An aborted repo that holds cut items keeps one audit line.** After `abandon-done`, `board state` carries `unknown: -: X: the repo is untracked, not tracked: not read` for as long as the repo has cut items.
- **`add --key` does not check the key against the manifest.** `board add --key M:k3` succeeds for a `k3` that is not in `plan_items` and gets an id; `track` is what refuses it, when the Plan and the manifest differ.
- **Agent names are asserted, not key-bound.** Nothing binds an agent's name to a key.
- **The registry and the store belong to one install.** A second machine needs the same store.
- **The stub check reads only the paths named in `retire` at tracking.** A status file added later is not caught.
- **Git operations are outside the store's lock.** A `checkout`, `stash` or `merge` can replace a store file. Levain re-checks each file it opens by inode and refuses a replacement it cannot read past.
