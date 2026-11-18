# Projects: tracked and untracked

Levain knows each repo in one of two modes. Both are supported. The mode decides where the repo's state lives. It does not change how the repo is built, tested or contributed to.

- **Untracked** is the default. The repo keeps its own state (its own files, issues or status page), and a seat reads those files.
- **Tracked** moves the repo's state onto Levain's work board. The repo keeps one file that says what it is for, `CHARTER.md`, on its main branch.

Every command here is a `levain board` or `levain charter` verb. `levain board <verb> --help` and `levain charter <verb> --help` print every flag.

## The two modes

| | untracked | tracked |
| --- | --- | --- |
| What the operator said | nothing, or "work in X" | "track X", said in conversation with the desk |
| INTENT (what the repo is for) | the operator's own files, if any | `CHARTER.md` at the repo root, on origin/main, checked by `levain charter check` |
| Conventions (how to build and test) | the repo's own (AGENTS.md, CLAUDE.md, CONTRIBUTING) | the same files, unchanged. Tracking does not touch them |
| STATE (what is open, in flight, built, done) | the operator's own files | the board: items in Levain's store, with state derived on every read from git, seats and the band. No file holds it by hand |
| RECORD (decisions, handoffs, findings) | the operator's own | episodes in the operator's memory store, tagged `repo:<name>` and `item:<name>#<n>`. Artifacts (design pages, review transcripts) are files in a record directory outside the repo's git |
| The board | holds no items for it | holds its items |
| A seat's context | the repo's own files | `levain board context <name>#<n>`, plus the conventions files |

An untracked repo is fully supported. The desk launches seats in it, and a seat reads the repo's own files. Levain holds no items for it and draws no board column for it.

Three rules hold in every install:

- **The board holds items only for repos that are tracking or tracked.** Once a store has a registry, `levain board add` refuses any other repo, with the reason. An `aborting` repo is refused too. The exception is a store with no registry yet, covered under [Known edges](#known-edges).
- **Levain writes no state.** Stored records are registrations, items, decisions and mode changes. State is computed when you read it.
- **A repo is in play only by a `register` record.** A directory scan can find a repo, and finding it does not put it in play.

## Untracked: what Levain recommends

An operator with no method of their own can keep the three things apart inside the repo:

- one `CHARTER.md` for INTENT, which changes slowly and which any contributor or tool can read;
- one status file for STATE, which only one writer touches;
- one append-only log for RECORD.

Tracking then becomes a short conversation. The desk reads those files, drafts the charter and the items, and the operator says yes.

## Tracking a repo

Tracking is a conversation first and a ceremony second. Nothing is written until the operator says yes.

1. **The operator says "track X".** The desk puts X in play if it is not already there:

   ```
   levain board register X --words "<his words>" --by <desk> --path <repo directory>
   ```

   The repo needs at least one commit. Without one, `register` refuses: nothing identifies the repository.

2. **The desk reads X's existing memory, read-only.** That is `next.md` or `brief.md`, any project memory, status pages, TODO files, the README and the conventions files.

3. **The desk drafts one review page.** It holds the charter (`levain charter init X`, filled from the old memory), the proposed items (a title, the operator's words verbatim where the old files carry them, a DONE line and a `done_kind` for each), what is not carried and why, which old paths will be retired, and which conventions belong in the repo's conventions file rather than the charter.

4. **The operator reads it and answers in conversation.** Edits go back to step 3.

5. **The desk runs the ceremony in this order.** Each step repeats safely.

   - **(a) Begin.** Writes the migration id and locks X to new work:

     ```
     levain board begin-track X --words "<his yes>" --by <desk> --manifest <file.json> [--owner <desk>] [--migration M]
     ```

     The manifest is JSON: `plan_items` (the item keys), `contract_items` (repos), and optionally `retire` and `record_dir`. While X is `tracking`, a seat launched on X must carry the migration id, and `board add` for X must be keyed to it. `board repos` shows the step that is next.

   - **(b) Expand: the items.** Each item is added with its key and a hash of its fields:

     ```
     levain board add --repo X --title "<title>" --origin "<origin>" --by <desk> --key M:<k> --payload-hash <sha256>
     ```

     The same key with the same hash returns the existing id. The same key with a different hash is an error. Keys never expire, so a second ceremony on X cannot receive the first one's items. The items exist but are outside the board's scope until the switch.

   - **(c) Expand: the charter.** The item ids go into the charter's Plan. `CHARTER.md` lands on X's main through X's own landing path, and `levain charter check --ref origin/main` passes. A stalled landing only means the switch has not happened, and X stays locked.

   - **(d) Switch.** This is the one record that moves the source of truth from the old files to the board:

     ```
     levain board track X --words "<his yes>" --by <desk>
     ```

     It refuses unless (b) and (c) hold. From here the board is the source, and the lock lifts.

   - **(e) Contract: the stubs.** Each old file is copied to `<record dir>/pre-track-<date>/` with a sha256 line, then replaced at its old path by a stub. The stub's first line is the marker `<!-- levain: tracked stub -->`. Until a stub lands, `levain board repos` reports `stale: <path> still holds pre-track content`. No seat acts on the old content in that gap.

6. **The desk writes one decision episode** tagged `repo:X`, naming the charter, the item count and the record directory.

**Aborting.** Before the switch only. `levain board abandon-track X --words "<his words>" --by <desk>` makes X `aborting` and cuts its items. Run it again to finish the cuts. `levain board abandon-done X --by <desk>` returns X to untracked once every item is cut and any compensation, such as a reverted charter, has landed. After the switch there is no abort. Leaving tracked mode is `levain board untrack X --words "<his words>" --by <desk>`, which refuses while any item is not done, cut or parked.

**The desk's own door.** A desk seat's brief opens thirteen board verbs (`state` through `untrack`) and two charter verbs (`charter init`, `charter check`). `move`, `pause`, `resume` and `ack-torn` are not opened to a desk seat; they run from a terminal.

## Charter

`levain charter init NAME` writes `CHARTER.md` at the repository root, with the name, the Export and the Plan filled in. Every other section is a `<fill: ...>` placeholder that `check` reports until it is replaced. `init` never overwrites a file.

`levain charter check` reads the charter, from origin/main by default. In this release it only warns: it prints what a valid charter needs and exits 0 either way.

## A seat on a tracked item

A worker launched for a tracked item is told to:

- run `levain board context NAME#N` first, which is the one context contract;
- put `Board-Item: NAME#N` on every commit;
- put `Board-Closes: NAME#N` on the commit that meets the item's DONE line;
- put `Board-Seat: <durable key>` on every closing commit;
- write decisions and handoffs as episodes tagged `repo:NAME` and `item:NAME#N`.

The desk's brief teaches these five rules. Levain supplies the check a tracked seat's close must pass (`levain/desk/seat_close.py`); the launcher that closes the seat runs it.

## Authorship and grades

Every record in the store and in the registry carries three fields:

- `author`: `kind` (`operator` or `agent`) and `name`. The writer refuses a record without one. `--author-kind` defaults to `agent`, so an operator writing at a terminal says `--author-kind operator`. An agent's launch is named with `--launch-key`, which is refused for an operator.
- `grade`: how strongly the record was signed. See the table below.
- `prev`: the hash of the line before it, so the file is a hash chain.

| grade | what it means | how it is written |
| --- | --- | --- |
| `asserted` | A named author, unsigned. The writer cannot tell the operator's terminal from a seat running the same command, so `kind: operator` without a signature is still `asserted`. An agent's name is asserted too, not bound to a key. | Every write without `--sign` or `--source`. This is the default. |
| `relayed` | His answer to a band row, relayed by the desk. The record cites the row, and its words are a verbatim excerpt of the answer. | `--source <q-id>`. Only native band question ids (`q-` and 24 hex characters) are accepted in this slice. |
| `signed-terminal` | Signed by the operator's Touch ID key at a terminal. | `--sign`, with `--author-kind operator`. It needs a fresh terminal key and the authority log that enrols it. |
| `signed-phone` | Signed by the operator's passkey. | Not reachable from a command yet. The writer accepts a phone signature, and the cockpit door that mints it is not built in this slice. |

Only a signed grade makes `kind: operator` a verified fact. Anything else is a claim, and the record says so.

**The chain.** Each line carries `prev`, the sha256 of the line before it, bytes as written. The first chained record commits to the unchained prefix that came before it, if there was one. An edit in the middle of a file breaks the chain at the next line. The board then reads the store as torn, and items read `unknown` until `levain board ack-torn --file <items.jsonl|repos.jsonl> --line N --why "<why>" --by <name>` acknowledges the line. A chain shows that lines were not changed in the middle. It does not show who wrote them: whoever can write the file can rewrite its tail and recompute every `prev`. Authorship comes from a signed grade.

**Which verbs need a signed or relayed record.** A decision needs a signed or relayed record, and an unsigned one is refused. The decision-class verbs are `decide`, `begin-track`, `track`, `abandon-track` and `untrack`. `move` is decision-class too, when the repo is tracking or tracked. Every other write (`add`, `amend`, `register`, `pause`, `resume`, `ack-torn`, `abandon-done`) accepts `asserted`. The refusal names both ways to satisfy the rule: `--sign`, or `--source <q-id>`.

## Known edges

- **Before a store's first `register`, `add` takes items for any repo.** The registry is inactive until the store has a `repos.jsonl`, and the mode check starts with it.
- **`--source` takes native band ids only.** A row from flow (`np-…`) needs flow's reader, which lands with the flow-side work. Until then, a decision-class write for a flow row needs `--sign` at a terminal.
- **The passkey route has no door yet.** The writer accepts a phone signature, but the cockpit door that signs with the passkey is not in this slice. `signed-phone` cannot be written from a command yet.
- **`move` is decision-class only while the repo is tracking or tracked.**
- **Agent names are asserted, not key-bound.** Binding a named agent to a key is 1.1.
- **The registry and the store belong to one install.** A second machine needs the same store.
- **The stub check reads only the paths named at tracking.** A status file added later is not caught.
- **Git operations are outside the store's lock.** A `checkout`, `stash` or `merge` can replace a store file. Levain re-checks each file it opens by inode and refuses a replacement it cannot read past.
