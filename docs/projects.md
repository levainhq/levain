# Projects: tracked and untracked

Levain knows each repo in one of two modes. Both are supported. The mode decides where the repo's state lives. It does not change the repo's build or test commands, or its conventions files, as long as `retire` does not name them.

- **Untracked** is the default. The repo keeps its own state (its own files, issues or status page), and a seat reads those files.
- **Tracked** moves the repo's state onto Levain's work board. The repo keeps one file that says what it is for, `CHARTER.md`, on its main branch.

Every command here is a `levain board` or `levain charter` verb. `levain board <verb> --help` and `levain charter <verb> --help` print every flag.

## The two modes

| | untracked | tracked |
| --- | --- | --- |
| What the operator said | nothing, or "work in X" | "track X", said in conversation with the desk |
| INTENT (what the repo is for) | the operator's own files, if any | `CHARTER.md` at the repo root, on origin/main, checked by `levain charter check` |
| Conventions (how to build and test) | the repo's own (AGENTS.md, CLAUDE.md, CONTRIBUTING) | the same files. Tracking does not change them, as long as `retire` does not name them (see Known edges) |
| STATE (what is open, in flight, built, done) | the operator's own files | the board: items in Levain's store, with state derived on every read from git, seats and the band. No file holds it by hand |
| RECORD (decisions, handoffs, findings) | the operator's own | episodes in the operator's memory store, tagged `repo:<name>` and `item:<name>#<n>`. Artifacts (design pages, review transcripts) are files in a record directory outside the repo's git |
| The board | holds no items for it (once the store has a registry; see Known edges) | holds its items |
| A seat's context | the repo's own files | `levain board context <name>#<n>`, plus the conventions files |

An untracked repo is fully supported. The desk launches seats in it, and a seat reads the repo's own files. Levain draws no board column for it.

Three rules hold in every install:

- **The board holds items only for repos that are tracking or tracked.** Once a store has a registry, `levain board add` refuses any other repo, and says why. An `aborting` repo is refused too. Before a store's first registration that check is off; see [Known edges](#known-edges).
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

   The repo needs at least one commit. Without one, `register` refuses: nothing identifies the repository. The name X must match `[a-z0-9][a-z0-9._-]*`: lowercase only. `register Alpha` prints `refused: repo 'Alpha' must match [a-z0-9][a-z0-9._-]*`. X is a name, not the directory's name; the directory is what `--path` says.

2. **The desk reads X's existing memory, read-only.** That is `next.md` or `brief.md`, any project memory, status pages, TODO files, the README and the conventions files.

3. **The desk drafts one review page.** It drafts the charter with `levain charter init X --stdout`, which prints the skeleton and writes nothing. The file reaches the repo only when step (c) lands it. The page holds the charter, the proposed items (a title, the operator's words verbatim where the old files carry them, a DONE line and a `done_kind` for each), what is not carried and why, which old files will be retired, and which conventions belong in the repo's conventions file rather than the charter.

4. **The operator reads it and answers in conversation.** Edits go back to step 3.

5. **The desk runs the ceremony in this order.** A keyed `add` repeats safely (see (b)), and `begin-track` derives its migration id when `--migration` is omitted, so a retry derives the same one. The ceremony's decision-class writes (`begin-track`, `track`, `decide`, and `abandon-track` on the abort path) each carry provenance: `--sign` or `--source <q-id>`, as [Signing and relaying](#signing-and-relaying-decision-class-writes) says. Without one the write is refused. The examples below carry `--source`, because the desk runs the ceremony and relays the operator's answered band row.

   - **(a) Begin.** Writes the migration id and locks X to new work. Decision-class, so it carries `--source` (or `--sign`). The band question the desk files for this yes must carry the sha256 of the manifest file, so the answer binds exactly that manifest:

     ```
     levain board begin-track X --words "<a verbatim excerpt of the operator's answer>" --by <desk> --manifest <file.json> --source <q-id> [--owner <desk>] [--migration M]
     ```

     The manifest is JSON. `plan_items` names the item keys (the `<k>` in each `M:<k>`), and the charter's Plan lists the ids those keys produce. `contract_items` names the repos that hold stubs to land. `retire` (optional) names the old memory files to copy and stub, each as a `{repo, path}` object (a bare path string is refused: `a retire entry is exactly {repo, path}`), and `record_dir` (optional) names where the copies go. A complete manifest:

     ```json
     {
       "plan_items": ["k1", "k2"],
       "contract_items": ["alpha"],
       "retire": [{"repo": "alpha", "path": "status.md"}],
       "record_dir": "/path/to/records"
     }
     ```

     `begin-track` prints the migration id: `tracking alpha (migration mf538fa9a1e)`. That id is the `M` in `--key M:<k>`; `board repos` shows it too, and a retry prints `already tracking alpha (migration ...)` with the same id. While X is `tracking`, a seat launched on X must carry the migration id, and `board add` for X must be keyed to it. `board repos` shows the step that is next.

   - **(b) Expand: the items.** Each item is added with its key and a hash of its fields:

     ```
     levain board add --repo X --title "<title>" --origin "<origin>" --by <desk> --key M:<k> --payload-hash <sha256>
     ```

     The same key with the same hash returns the existing id. The same key with a different hash is an error. Keys never expire. Each ceremony has its own migration id, and the key carries it, so a second ceremony's items get new ids. The items exist but are outside the board's scope until the switch.

   - **(c) Expand: the charter.** The item ids go into the charter's Plan: `levain charter init X --items <store>` writes it with the Plan prefilled from the items added in (b), and the desk edits the rest by hand. `CHARTER.md` lands on X's main through X's own landing path. `levain charter check --ref origin/main --repo-name X --items <store>` reports `X's CHARTER.md (origin/main) is a valid charter.` once it is. The charter's title must name the registered repo X. `check` takes the repo's name from the directory it runs in, so when the directory is named otherwise (a clone called `clone-dir` for repo `alpha`) the title reads `names 'alpha', not this repository's name 'clone-dir'` until `--repo-name X` is passed. `check` only reports and exits 0; `track` is the gate. It runs the same check itself and refuses on any problem. It also refuses unless the Plan lists exactly the ids of the manifest's items that were added and not cut.

     To cut an item: `levain board decide --id X#<n> --decision cut --words "<a verbatim excerpt of the operator's answer>" --by <desk> --source <q-id>` (it prints `decided X#<n>`; `decide` is decision-class, so `--sign` is the other way to carry it). A cut item must also leave the landed Plan: `charter init --items` leaves a cut item out of a Plan it writes, but a Plan already landed is edited by hand and landed again before `track` will pass. A stalled landing only means the switch has not happened, and X stays locked.

   - **(d) Switch.** This is the one record that moves the source of truth from the old files to the board. It is decision-class. A relayed `track` is held to the same manifest as (a): its band question carries the sha256 of the manifest that `begin-track` recorded.

     ```
     levain board track X --words "<a verbatim excerpt of the operator's answer>" --by <desk> --source <q-id>
     ```

     It refuses unless (b) and (c) hold. From here the board is the source, and the lock lifts. `add --key` does not check the key's `<k>` against the manifest; `track` does (see Known edges).

   - **(e) Contract: the stubs.** There is no verb for this step; the desk does it by hand. It copies each file named in `retire` to `<record dir>/pre-track-<date>/` with a sha256 line (a file such as `SHA256` holding `<hash>  status.md`; the name is the desk's choice and nothing checks it), then replaces the file at its old path with a stub, commits and pushes. The stub's first line must be the marker `<!-- levain: tracked stub -->`; the rest is free text (the run used `This file is retired; state lives on the Levain board (levain board state).`). The one thing checked is that marker, in the file in the repo's working tree, not on origin/main. Until a stub is in place, `levain board repos` reports `stale: <path> still holds pre-track content`. No seat acts on the old content in that gap.

6. **The desk writes one decision episode** tagged `repo:X`, naming the charter, the item count and the record directory.

**Aborting.** Before the switch only. `levain board abandon-track X --words "<a verbatim excerpt of the operator's answer>" --by <desk> --source <q-id>` makes X `aborting` and cuts its items. It is decision-class, so it carries `--source` or `--sign`. Run it again to finish the cuts. `levain board abandon-done X --by <desk>` returns X to untracked once every item is cut and any compensation, such as a reverted charter, has landed; it is not decision-class and takes no provenance. After the switch there is no abort. Leaving tracked mode is `levain board untrack X --words "<the operator's words>" --by <operator> --sign --author-kind operator`, which refuses while any item is not done, cut, parked or out of 1.0. `untrack` takes `--sign` only: it has no `--source`, so the desk cannot relay it. The operator runs it at the laptop.

**The desk's own door.** A desk seat's brief opens the board verbs except `move`, `pause`, `resume` and `ack-torn`, and the charter verbs `init` and `check`. Those four board verbs run from a terminal.

## Reading the board

`levain board repos` lists each repo's mode and the next step. `levain board state` derives every item's state when you read it, and `levain board context X#<n>` prints one item's context.

- The `as of` time in `board state`'s header is the time of the last fetch of origin, not of the read. `--now` does not move it; it moves when origin has changed.
- On a store with no land-waiter configured, items read `unknown` (`waiters: not supplied`) and `board state` exits 3. Pass `--no-land-waiter` to read the items without one: `levain board state --no-land-waiter`, and the same for `board context`.
- `board state` exits 3 while its audit is not empty, whatever the items say.

## Charter

`levain charter init NAME` writes `CHARTER.md` at the repository root, with the name and the Export filled in. Its Plan is prefilled with one line per live item when `--items` names a store; otherwise the Plan is a placeholder line. Every other section is a `<fill: ...>` placeholder that `check` reports until it is replaced. `init` never overwrites a file. With `--stdout` it prints the skeleton and writes nothing.

`levain charter check` reads the charter, from origin/main by default. In this release it only warns: it prints what a valid charter needs and exits 0 either way.

## A seat on a tracked item

A worker launched for a tracked item is told to:

- run `levain board context NAME#N` first, which is the one context contract;
- put `Board-Item: NAME#N` on every commit;
- put `Board-Closes: NAME#N` on the commit that meets the item's DONE line;
- put `Board-Seat: <durable key>` on every closing commit;
- write decisions and handoffs as episodes tagged `repo:NAME` and `item:NAME#N`.

The desk's brief teaches these five rules. The close check a tracked seat must pass is `levain/desk/seat_close.py`. Nothing in Levain calls it yet; see Known edges.

## Signing and relaying: decision-class writes

The writer holds one fixed policy, and it checks it before it signs or writes anything. Some writes are decision-class: a record of one is a ruling by the operator, so it must say where that authority came from. An asserted decision-class record is refused.

**Decision-class writes.** `decide`, `begin-track`, `track`, `abandon-track` and `untrack`. `move` joins them when the repo is tracking, tracked or aborting and the move changes its origin or root set, because the repo's evidence would then be read from another repository. The same repository relocated on disk, and any move of an untracked repo, stay asserted. Every other verb (`register`, `add`, `amend`, `pause`, `resume`, `ack-torn`, `abandon-done`) accepts an asserted record.

**Two ways to give provenance.**

1. **`--sign`: the operator at the laptop, Touch ID.** The operator runs the verb in a fresh terminal (not inside an agent session, and not a remote one), with `--author-kind operator`. The write needs a terminal key and the authority log that enrols it, and the record is verified against that log before it is written. It shows the Touch ID prompt; a refused or cancelled prompt writes nothing. No lock is held while the prompt is up, so if another write lands on the store meanwhile the signed write refuses (`the board changed while you signed: sign again`) and writes nothing; run it again. `--sign` is the only way to write `untrack` and `move`. This page documents it from the verb's help and the code: the prompt needs the operator's hand, so the run below does not include one. Example:

   ```
   levain board track alpha --words "yes, track alpha" --by phill --sign --author-kind operator
   ```

2. **`--source <q-id>`: the desk relays his answered band row.** The desk files the question in the band, the operator answers it, and the desk writes the record citing the row. `<q-id>` is a native question id, `q-` and 24 hex characters; a bare band number or a flow `np-` id is refused (see Known edges). `--words` must be a verbatim excerpt of the answer (one line, no control characters): his sentence travels, never a paraphrase. The row must exist and be answered. `begin-track` and `track` carry one more rule: the question's text must carry the sha256 of the manifest file, so a yes binds exactly that manifest. `--band-dir` names the band store when it is not this install's own. `--source` is taken by `add`, `amend`, `decide`, `begin-track`, `track` and `abandon-track`; `untrack` and `move` do not take it. A complete relayed ceremony, run on a scratch store (the desk filed the question `Begin tracking alpha? manifest sha256 <sha256 of manifest.json>` and the operator answered `yes, begin tracking alpha`):

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
levain board: refused: begin-track is a decision-class write: it needs --sign (the operator's Touch ID), or --source <q-id> (the band's question id, q-<24 hex>) relaying his answer; an asserted record is refused
```

`track`, `decide` and `abandon-track` refuse in the same words, each naming its own verb. `untrack` and `move` do not take `--source`, so their refusal names `--sign` only: `untrack is a decision-class write: it needs --sign (the operator's Touch ID); an asserted record is refused`.

A relayed write that does not hold is refused too, and nothing is written:

- words that are not in the answer: `--source q-e89a...: the words are not a verbatim excerpt of the answer: 'yes please track alpha' (his sentence travels, never a paraphrase; quote exactly what the answer says)`;
- a `begin-track` or `track` whose question does not carry the manifest's digest: `--source q-a164...: the question does not carry the manifest's sha256: the answer cannot bind this manifest`;
- a flow row: `--source np-1b3dd088: np- rows are flow's band: their reader arrives with T12; cite a q- row (a native question id), or --sign`.

## Authorship and grades

Every record in the store and the registry carries three fields:

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

## Known edges

- **A signed decision rests on the operator's hand.** `--sign` needs a Touch ID prompt in a fresh terminal, a terminal key and an authority log that enrols it. A desk seat cannot sign, so a desk relays with `--source`, and `untrack` and `move` (which take no `--source`) wait for the operator.
- **`--source` takes native band ids only** (`q-` and 24 hex characters). A flow row (`np-…`) is refused until flow's reader exists.
- **The phone route is not on this page's ceremony.** The writer takes a phone signature (`signed-phone`), but the cockpit door that signs with the passkey is a separate, later piece.
- **`retire` is not checked against the conventions files.** Keep AGENTS.md, CLAUDE.md and CONTRIBUTING out of it. The code does not refuse them.
- **Closing a tracked seat is not checked yet.** `levain/desk/seat_close.py` holds the check, and nothing in Levain calls it. Flow's seat close is the caller, and that side is not written.
- **Before a store's first registration, `add` takes items for any repo.** The registry is inactive until the store has a `repos.jsonl`, and the mode check starts with it.
- **An aborted repo that holds cut items keeps one audit line.** After `abandon-done`, `board state` carries `unknown: -: X: the repo is untracked, not tracked: not read` for as long as the repo has cut items.
- **`add --key` does not check the key against the manifest.** `board add --key M:k3` succeeds for a `k3` that is not in `plan_items` and gets an id; `track` is what refuses it, when the Plan and the manifest differ.
- **Agent names are asserted, not key-bound.** Binding an agent to a key is 1.1.
- **The registry and the store belong to one install.** A second machine needs the same store.
- **The stub check reads only the paths named in `retire` at tracking.** A status file added later is not caught.
- **Git operations are outside the store's lock.** A `checkout`, `stash` or `merge` can replace a store file. Levain re-checks each file it opens by inode and refuses a replacement it cannot read past.
