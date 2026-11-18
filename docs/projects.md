# Projects: tracked and untracked

Levain knows each repo in one of two modes. Both are supported. The mode decides where the repo's state lives. It does not change the repo's build or test commands, or its conventions files.

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
   levain board register X --words "<his words>" --by <desk> --path <repo directory>
   ```

   The repo needs at least one commit. Without one, `register` refuses: nothing identifies the repository.

2. **The desk reads X's existing memory, read-only.** That is `next.md` or `brief.md`, any project memory, status pages, TODO files, the README and the conventions files.

3. **The desk drafts one review page.** It drafts the charter with `levain charter init X --stdout`, which prints the skeleton and writes nothing. The file reaches the repo only when step (c) lands it. The page holds the charter, the proposed items (a title, the operator's words verbatim where the old files carry them, a DONE line and a `done_kind` for each), what is not carried and why, which old files will be retired, and which conventions belong in the repo's conventions file rather than the charter.

4. **The operator reads it and answers in conversation.** Edits go back to step 3.

5. **The desk runs the ceremony in this order.** A keyed `add` repeats safely (see (b)), and `begin-track` derives its migration id when `--migration` is omitted, so a retry derives the same one.

   - **(a) Begin.** Writes the migration id and locks X to new work:

     ```
     levain board begin-track X --words "<his yes>" --by <desk> --manifest <file.json> [--owner <desk>] [--migration M]
     ```

     The manifest is JSON. `plan_items` names the item keys (the `<k>` in each `M:<k>`), and the charter's Plan lists the ids those keys produce. `contract_items` names the repos that hold stubs to land. `retire` (optional) names the old memory files to copy and stub, and `record_dir` (optional) names where the copies go. While X is `tracking`, a seat launched on X must carry the migration id, and `board add` for X must be keyed to it. `board repos` shows the step that is next.

   - **(b) Expand: the items.** Each item is added with its key and a hash of its fields:

     ```
     levain board add --repo X --title "<title>" --origin "<origin>" --by <desk> --key M:<k> --payload-hash <sha256>
     ```

     The same key with the same hash returns the existing id. The same key with a different hash is an error. Keys never expire. Each ceremony has its own migration id, and the key carries it, so a second ceremony's items get new ids. The items exist but are outside the board's scope until the switch.

   - **(c) Expand: the charter.** The item ids go into the charter's Plan. `CHARTER.md` lands on X's main through X's own landing path. `levain charter check --ref origin/main` reports no problems. `check` exits 0 in warn mode, so read what it reports, not its exit status. `track` runs the same check itself and refuses on any problem. It also refuses unless the Plan and the manifest name the same items. A stalled landing only means the switch has not happened, and X stays locked.

   - **(d) Switch.** This is the one record that moves the source of truth from the old files to the board:

     ```
     levain board track X --words "<his yes>" --by <desk>
     ```

     It refuses unless (b) and (c) hold. From here the board is the source, and the lock lifts.

   - **(e) Contract: the stubs.** Each file named in `retire` is copied to `<record dir>/pre-track-<date>/` with a sha256 line, then replaced at its old path by a stub. The stub's first line is the marker `<!-- levain: tracked stub -->`. Until a stub lands, `levain board repos` reports `stale: <path> still holds pre-track content`. No seat acts on the old content in that gap.

6. **The desk writes one decision episode** tagged `repo:X`, naming the charter, the item count and the record directory.

**Aborting.** Before the switch only. `levain board abandon-track X --words "<his words>" --by <desk>` makes X `aborting` and cuts its items. Run it again to finish the cuts. `levain board abandon-done X --by <desk>` returns X to untracked once every item is cut and any compensation, such as a reverted charter, has landed. After the switch there is no abort. Leaving tracked mode is `levain board untrack X --words "<his words>" --by <desk>`, which refuses while any item is not done, cut or parked.

**The desk's own door.** A desk seat's brief opens the board verbs except `move`, `pause`, `resume` and `ack-torn`, and the charter verbs `init` and `check`. Those four board verbs run from a terminal.

## Charter

`levain charter init NAME` writes `CHARTER.md` at the repository root, with the name, the Export and the Plan filled in. Every other section is a `<fill: ...>` placeholder that `check` reports until it is replaced. `init` never overwrites a file. With `--stdout` it prints the skeleton and writes nothing.

`levain charter check` reads the charter, from origin/main by default. In this release it only warns: it prints what a valid charter needs and exits 0 either way.

## A seat on a tracked item

A worker launched for a tracked item is told to:

- run `levain board context NAME#N` first, which is the one context contract;
- put `Board-Item: NAME#N` on every commit;
- put `Board-Closes: NAME#N` on the commit that meets the item's DONE line;
- put `Board-Seat: <durable key>` on every closing commit;
- write decisions and handoffs as episodes tagged `repo:NAME` and `item:NAME#N`.

The desk's brief teaches these five rules. The close check a tracked seat must pass is `levain/desk/seat_close.py`. Nothing in Levain calls it yet; see Known edges.

## Authorship and grades

Every record in the store and the registry carries three fields:

- `author`: `kind` (`operator` or `agent`) and `name`. The writer refuses a record without one. `--author-kind` defaults to `agent`, so an operator writing at a terminal says `--author-kind operator`. An agent's launch is named with `--launch-key`, which is refused for an operator.
- `grade`: how strongly the record was signed. Every record this tree writes is `asserted`.
- `prev`: the hash of the line before it, so the file is a hash chain.

| grade | what it means | how it is written |
| --- | --- | --- |
| `asserted` | A named author, unsigned. The writer cannot tell the operator's terminal from a seat running the same command, so `kind: operator` without a signature is still `asserted`. An agent's name is asserted too, not bound to a key. | Every write in this tree. |

Until a signed grade exists, `kind: operator` is a claim, and so is every author name. No verb refuses an unsigned decision yet. The signing slice adds the signed and relayed grades and that refusal; see Known edges.

**The chain.** Each line carries `prev`, the sha256 of the line before it, bytes as written. The first chained record commits to the unchained prefix that came before it, if there was one. An edit in the middle of a file breaks the chain at the next line. The board then reads the store as torn, and items read `unknown` until `levain board ack-torn --file <items.jsonl|repos.jsonl> --line N --why "<why>" --by <name>` acknowledges the line. A chain shows that lines were not changed in the middle. It does not show who wrote them: whoever can write the file can rewrite its tail and recompute every `prev`. Authorship comes from a signed grade.

## Known edges

- **The signing slice is not in this tree.** It adds `--sign` (a Touch ID signature at a terminal, with `--author-kind operator`), `--source <q-id>` (relaying one of his band answers), the `signed-terminal` and `relayed` grades, and the rule that the decision-class verbs (`decide`, `begin-track`, `track`, `abandon-track`, `untrack`) refuse an unsigned record. `move` joins that rule while its repo is tracking or tracked. Until the slice lands, every record is `asserted`.
- **`--source` will take native band ids only** (`q-` and 24 hex characters). A flow row (`np-…`) needs flow's reader first.
- **The phone route is not in this tree.** The signing slice's writer takes a phone signature (`signed-phone`). The cockpit door that signs with the passkey is a separate, later piece.
- **`retire` is not checked against the conventions files.** Keep AGENTS.md, CLAUDE.md and CONTRIBUTING out of it. The code does not refuse them.
- **Closing a tracked seat is not checked yet.** `levain/desk/seat_close.py` holds the check, and nothing in Levain calls it. Flow's seat close is the caller, and that side is not written.
- **Before a store's first registration, `add` takes items for any repo.** The registry is inactive until the store has a `repos.jsonl`, and the mode check starts with it.
- **Agent names are asserted, not key-bound.** Binding an agent to a key is 1.1.
- **The registry and the store belong to one install.** A second machine needs the same store.
- **The stub check reads only the paths named in `retire` at tracking.** A status file added later is not caught.
- **Git operations are outside the store's lock.** A `checkout`, `stash` or `merge` can replace a store file. Levain re-checks each file it opens by inode and refuses a replacement it cannot read past.
