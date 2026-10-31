# Changelog

All notable changes to Levain. Format is loosely [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses [SemVer](https://semver.org/spec/v2.0.0.html).

> **This file starts at 0.4.2.** Earlier releases were documented in commit messages only — which is itself one of the defects this release closes: an operator upgrading through 0.4.x had no surface that told them what changed underneath their install. Entries for 0.4.0 and 0.4.1 are backfilled below because they carry a behaviour change adopters needed to know about and were never told.

## [Unreleased]

### Added
- **Groundwork: one sleep reconciler over both stores.** `awake.sync` now re-reads the coordinator's working threads AND the agents' live occurrences (`agent_occurrences` in state `admitted` or `launched`) under the one `awake.json` lock, whichever side calls it, and releases the holder only when both are empty; `levain doctor`'s host-sleep row names the source (coordinator, agent, or both) and the occurrence holding the host. Nothing starts an agent occurrence yet.
- **The coordinator's reply carries what changed in its store.** Ahead of the NEEDS YOU band, a coordinator session's
  reply block now has a work delta rendered by Levain (never model-written) from the coordinator store's records that carry
  its `gseq`: replies, launch events, lease events, ready moves and chat messages. At most 5 lines in `gseq` order, then
  `+N more: levain coord log --since GSEQ`; a store that cannot be read says `! not read` and moves nothing. The
  reprint check at Stop covers its lines (a block of work with an empty band is verified too, and the one retry names
  the work lines as well as the band), and the operator's cursor, an append-only `operator_views` table, advances only
  at that Stop, so an interrupted or unverified reply repeats the delta. A verified reply advances it past every record
  the block counted, the five shown AND the `+N more`: the block names `levain coord log --since GSEQ` for those, so a
  backlog is read with that command, not drained five lines per reply. A store from before the table gets it at its
  first verified reply, and a store replaced between the prompt and the Stop (another `store_id`) is left alone.
  Replies written before `replies` had a `gseq` have no place in the order and are counted in the header, not listed.
  New: `levain coord log --since GSEQ [--limit N]` (`N` 1..500), a read of the same records.
- **An OPEN question: `levain ask --open`.** A question filed with no options and no recommendation, answered only in the
  operator's own words (`levain question answer N --text`, the cockpit's write-in box, band shorthand
  `N: words`). `--open` takes no `--option`, no `--rec` and no `--would-push` (each refused beside it); the `ask` MCP tool takes `open: true`
  with only `what` and `blocks`; one option is still refused. An option key or a rec letter on an open question is
  refused, the band and `levain question list` show it as "open question: answer in your own words" with no rec,
  the ping carries no option rows, the coordinator can only escalate it (never answer it as a KNOW), and it is never
  held behind the coordinator's first read. The JSON of the band, the cockpit's question body and
  `levain question show --json` carry `open_ended`. The cockpit's page script is routed separately.

### Changed
- **The ping puts what you act on first.** A question ping is now the question, its options, the answer command, and
  only then the blocks, which take the room left and end "(full text on the card)" when cut. The room is measured in
  the bytes the phone push carries (emoji and accented text take more than one), and the question and the option
  labels are shortened when even they would not fit, so the options and the answer line always arrive. Before, a long
  blocks text (now up to 6,000 characters) could push the options and the answer line past what a notification keeps.
  The store and every card still hold the whole text. The `ask` MCP tool's
  schema now requires only `what` and `blocks` (options and rec are required unless `open` is true, checked in code).
- **`levain ask` takes a whole analysis.** A question's `--blocks` text now holds up to 6,000 characters (was 300) and an option
  label up to 600 (was 200). The caps are sized from the longest items a real caller filed (4,933 and 525 characters), and
  the cockpit and the band show a question's text whole. Still refused: a block or label with a line break,
  fewer than 2 or more than 6 options.
- **An open question says so on the cockpit.** A question filed with `--open` (no options, no rec) now reads
  "open question: answer in your own words" where the cockpit said a dangling "recommends ",
  "open question" in the Decisions row where it said "0 options", and its words box asks "your answer, in your
  own words". A question with options draws exactly as before.
### Changed
- **The Claude Code coordinator's own install now allows `Read`.** With your user layer left out, the `Read` rule in
  your own settings is gone too, so a read of a file outside the coordinator's workspace stopped at a prompt nobody
  answers. Its install's project settings now carry a bare `Read` allow, so it can read any file your own account can.
  Write, Edit, WebFetch and every shell command other than its own `coord`, `drops` and `thread` verbs still ask. Only
  the coordinator's own install gets the rule; a `levain init` for your sessions does not. The rule is the same with
  `--user-scope on`, where your own rules apply as well. An older coordinator install gets the rule the next time
  `levain coord agent claude` runs, or from `levain update --path <the coordinator directory>`: only the one entry is
  added to `permissions.allow` (made if missing), every other byte of the file kept, a hand-edited one included, and a
  second run changes nothing. Until then `levain coord status` says `coordinator install: upgrade pending` (and
  `install_upgrade_pending` in `--json`). A settings file that is missing or unreadable (not UTF-8 included), is not a JSON object, or whose
  `permissions` is not an object or `permissions.allow` not a list, is never written: `coord status` says `coordinator install: settings unreadable` with the reason (and
  `install_settings_problem` in `--json`), and the rule is yours to add by hand.
- **The Claude Code coordinator no longer loads your user-level Claude Code layer.** A coordinator session now starts
  with `--setting-sources project,local` and auto-memory off, so your `~/.claude` settings, hooks, `CLAUDE.md` and
  memory stay out of its context (measured on a cold turn: about 53K new cache tokens and $0.214 before, 9K and $0.038
  with the flag). Its own install's project and local settings, including the consent hook, still apply. To put your
  layer back, run `levain coord agent --user-scope on` (it keeps the agent, model and directory; `--user-scope off`
  returns to the default, and `levain coord agent claude --user-scope on` sets both at once). `levain coord status`
  and its `--json` show the choice (`user layer: off (project settings only)`). A launch keeps the choice it was asked
  under: changing it while a launch waits to start changes the next one. `--user-scope` alone needs a claude
  coordinator, and turning it on refuses, with the reason, when a `disableAllHooks` in your user settings would stop
  every launch. A store from before this change reads as off. With the layer off, a `disableAllHooks` that lives only in your user settings no longer refuses the coordinator
  launch, since the session never reads that file; `disableAllHooks` in the coordinator's own or an ancestor project
  still refuses. Covered by tests of the spawn argv and the setting.
- **The cockpit's owner key opens nothing off the box.** On a server bound to anything but loopback, and on a loopback
  server reached through a proxy (an extra host, a forwarded-for or Tailscale header), a request carrying the owner
  key is answered as if it carried no credential. Its 403 now says `"auth": "pair"` (a loopback request says
  `"auth": "owner"`), and the manifest's `transport` gains `owner_key`, so a page knows to ask for a pairing code
  rather than the key. `levain serve` no longer prints the owner key or puts it in the unlocked link on an
  off-loopback bind; it prints how to pair a device instead. Not run by hand against a real proxied cockpit.

### Removed
- **The phone port's run token and its device registry.** `levain consent phone` serves only the redirect page now and
  refuses without `--redirect-to`; `--token-file`, `--queue-dir` and `--sign-namespace` are gone from it (argparse
  refuses them), and `--link-file` holds `{page, pid}` with no token, so a script's statement is no longer signed on
  the phone port. The
  token-gated phone routes, `/webpush/pair`, `/webpush/pair/redeem`, the `?pair=` rewrite of `/app.webmanifest` and
  `levain ping pair`, `levain ping devices` and `levain ping revoke` are gone; pair a phone to the cockpit with
  `levain cockpit pair`. A push subscription is stored only under a paired cockpit device, a row under any other
  device is never sent to, and `POST /webpush/subscribe` with no device is 403. The service worker holds no credential: a
  tap writes the ping's id into its own `levain-sw-2` database and the page records the open; activating it deletes the
  old `levain-sw` database (the phone token's) and every `levain-` Cache Storage name it does not keep. The periodic
  ticker that released held pings from inside the phone server is retired with it: release is event-driven, run by the
  gate hook on every call and by the send child after each push, so a ping released by a quiet running out (or the desk
  going idle) waits for the next gate-hook call or push, and stays held while no agent calls. Covered by tests; not run
  on a real phone or a real push.

### Fixed
- **The cockpit's tokens-today figure no longer leaves out subagent lanes on a busy session.** It read only the first 64
  subagent transcripts by file name, so on a heavy session the later lanes were silently missing. It now reads every
  lane written since local midnight, newest first, and a lane it could not read in time (the read budget ran out) is
  counted and shown in the panel's "not read" line instead of dropped.
- **The morning card marks a presence word whose quote was not verified.** `_presence` dropped the `verified` flag the
  quote check puts on every word an agent sets, so the card showed an agent's word as if it were the operator's. The
  card's presence block now carries `verified` (true only when the quote was found in what he wrote; a record from before
  the check reads as not verified), and `levain coord morning` appends `(unverified)` after the quote, as `levain ping
  status` does. No setter: the block is unchanged.
- **The coordinator's verbs take their text without shell quoting.** Shadow 10-30: two of three real coordinators
  misfired on their own shell lines (flags kept in a variable that zsh does not split, then a `coord release` run in the
  same batch as the writes, which ended the lease under them). `drops note`, `coord escalate`, `coord answer`, `thread
  set` and `thread add` take `--text-file PATH` or `--stdin` (strict UTF-8, one trailing newline dropped, read under a
  byte cap, then the verb's own limit); `thread state` and `thread set` take `--cause-file PATH`; `drops split` takes
  the whole split as one JSON document (`--json-file PATH` or `--stdin`:
  `{"drop_id", "threads": [{"kind", "title", "about"?}], "attach": [{"kind", "thread"}]}`) through the same checks as
  `--thread` and `--attach`. Every old flag works as before. The coordinator brief shows these forms first, says never
  to keep flags in a shell variable, and says to release the lease only as the last step, as its own command. The JSON
  split refuses a key named twice at any level, a document nested too deeply, a `kind` that is not exactly one of the
  kinds, an empty split, and a wrong-typed `title`/`thread`/`about`, each by name; input files and stdin drop a UTF-8 BOM.
- **A coordinator's turn ends on a status line, and a drop that needs nothing gets a receipt.** The coordinator brief
  now carries an end-of-turn rule: the last line of its words is one `coordinator:` status line (what it did, how many
  items it escalated), never a question or "waits on you" (a coordinator that ended on an escalation sat in the agent
  view as "waiting for answers" for ~50 minutes); escalations go through `levain coord escalate` or the band's ask
  tool. New verb `levain drops note DROP_ID --why "one line" --by coordinator --token N` writes an append-only `noted`
  drop receipt (lease-fenced, refused on a split drop). A noted drop is not re-quoted in a brief, not counted as unseen
  (no replacement coordinator), not a "Drop not split" review item, and shows `[noted: why]` in `levain drops list` and
  `drops show`. The `levain coord watch` line for a silent holder now says its window is the service-beat window, not a
  bare "60 min".
- **A drop that lands while a coordinator sits done is acted on at the next watcher pass, not an hour later.** A
  coordinator that split its drop, released the lease and stayed listed (`claude --bg`, state `done`) kept the
  coordinator slot as `idle`, so drops filed after it sat unread until `LEASE_TTL + IDLE_GRACE`. `levain coord watch`
  (its serve thread included) now ends a `done` claude coordinator when an unsplit drop was delivered after its spawn
  claim (its brief quotes every drop before that), with cause `replaced while done`, never a death, stops its session
  and starts a fresh coordinator in the same pass, whose brief quotes the drop. The fresh launch is newer than the drop,
  so one drop replaces at most one coordinator. `levain coord wake` does the same at once for a done claude
  coordinator running without the lease (no drop needed), and otherwise says why and names `levain coord release`. A
  coordinator not `done` is untouched; a Codex coordinator and a drop with no watcher running still wait out the grace.
  A question filed with no live lease was never held, so it pings at filing (no ten-minute escalation applies to it).
  `levain coord status`, `drops list|show` and `thread list|show` no longer create `dispatch.db` or `awake.lock`, or
  add tables to a dispatch-only `dispatch.db`; they print `no coordinator store here` (`coord status --json` keeps its
  keys, with `"store": null`).
- **On macOS, a Codex session signed in with a ChatGPT subscription runs in a slot instead of failing with
  `account/read -32603`.** Codex syncs its managed preferences at start, and a slot account whose home was `/var/empty`
  could not. The slot accounts that run vendor CLIs now have a root-owned, empty home, `L/vendorhome`, made by setup,
  repair and replenish; the session also gets `SSL_CERT_FILE=/etc/ssl/cert.pem`, so Codex's TLS reaches chatgpt.com from
  inside the slot. `levain doctor` gains an `agent slot homes` row: an install from before this change reports upgrade
  pending (and exits that way) until `levain upgrade --no-pip` runs; a wrong owner, mode or a non-empty
  `Library/Preferences` reports broken. Run once in a macOS test VM with a real ChatGPT login (two turns answered); not
  run on Linux, where nothing changes.
- **A cockpit with no question store no longer draws the Needs you band, or a note that it could not be recorded.** An
  absent `questions.db` (no file, or an empty one) draws nothing in the band: no heading and no note, because nothing was
  ever filed. A store that has content but cannot be read, is locked or lacks its identity row keeps its "the band could
  not be recorded just now" line. The band also says it may be missing items when an authority source could not be read, when a second
  read of an item failed, or when a question row or an authority item's record cannot be decoded; such a read is never
  drawn as an absent store. The Board tab's Needs you column likewise reports nothing, instead of "could not be read",
  for a cockpit that hosts no question store, or whose store file is absent or empty; a store that exists but fails to
  read is still reported. Covered by tests,
  including a real-browser test for the absent and unreadable cases; not run by hand on a real machine.
- **`levain upgrade` run from a source tree with no installed `dist-info` refuses with "Could not tell how Levain was
  installed"** (its message says to upgrade it your usual way, then run `levain upgrade --no-pip`) instead of printing `internal error:
  PackageNotFoundError`. Covered by a test; not run by hand.
- **A Claude passthrough route without `platform.claude.com` is refused when the session's routes load, naming the
  host and the route.** The login refresh goes to that host through the route's tunnel; your own passthrough route
  replaces the shipped one, so one without it made every refresh fail. Levain never adds the host to your route; the
  refusal says to. Other routes (brokered, Codex) are not checked.

### Added
- **`levain serve` can serve the passkey signing pages.** `levain serve --write --sign-queue-dir DIR --sign-host NAME`
  hosts the cockpit's sign pages over the consent queue at `DIR`, for the https name `NAME` your reverse proxy serves
  on 443 (a `tailscale serve --https=443` name); that name is the one the passkey is made for and the one extra Host
  the server admits, and the page origin is `https://NAME`, the only origin the consent and dispatch sheets take a
  signature from. The queue and the name go together. Before anything is built, a cockpit without `--write` or with
  `--chat` refuses them with the server's own words, a name the browser's address bar would not show (uppercase, a
  trailing dot, a port) is refused in plain words and never rewritten, and `DIR` must be an existing readable
  directory, never created. The start-up text gains one line, `signing: <origin> (queue <dir>; consent and grant
  sheets on)`, or `off` with the reason when `DIR` is not the queue the cockpit's consent cards are read from;
  without the options nothing changes. Starting the signing pages publishes this cockpit's origin in the queue's
  `cockpit-origin.json` (replacing a value already there; a stop withdraws only its own) and settles orphaned sign
  attempts in the queue. Nothing is enrolled and no key or trust root is touched, and the phone reaches the pages
  with a paired device's token (the owner key opens nothing through a proxied name). Covered by tests, including a
  real request to a running `serve`; I did not run a passkey ceremony from a real phone through a real proxy.
- **Short pairing codes for the cockpit, and a test ping.** `POST /cockpit/pair/offer {"short": true}` mints a typed
  code (`XXXX-XXXX`, one use, 10 minutes, five wrong tries kill it, a newer short code replaces it) and
  `POST /cockpit/pair/claim {"short": "<typed>"}` spends it; the link form is unchanged and a code of one kind never
  redeems as the other. `levain cockpit pair --short` mints one on the box; `--list` shows the paired devices and
  `--revoke <id> [<id> ...]` revokes devices and the devices they paired, both reading the journal directly with no
  cockpit credential. `GET /cockpit/pair/devices.json` rows carry `subscribed`. `POST /webpush/test` sends a test
  ping to the calling paired device's subscriptions only, and `levain ping test --device d-...` does the same from the
  box. `levain cockpit pair --short` (or the link form) on a data root no server has started yet starts its cockpit
  journal first, so an off-loopback cockpit's first code needs no loopback visit. A refused short code says whether it
  was used up by wrong tries, expired, or already used. Covered by tests; not run against a real phone.
- **Every presence word an agent sets, replaces or is refused now leaves a row you can read.** `levain ping log` prints
  `<queue dir>/webpush/presence.log.jsonl` (who, which role, the word, the quote, the word it found, the outcome); an agent's
  write is not made when its row cannot be written, your own word always goes through. `levain coord morning` adds a
  `presence:` line when an agent set the standing word or was refused in the last 24 hours. What presence does is unchanged.
  Covered by tests; not run on a real machine.
- **An agent's `--said` quote is now checked against what you typed to it, and decides whether it may replace a word you set.**
  `levain ping away|remote|quiet|sleep --said "..."` from an agent session looks for the quote (at least 12 characters) in the
  last 20 turns you typed to that session in the past 6 hours; a message another session relayed, a notification or a tool
  result is not a turn you typed. A quote found there is recorded verified, with when you typed it; one not found still sets
  over no word or another agent's word and shows "(unverified)" in `levain ping status` and `levain ping log`, but it never
  replaces a word you set yourself, and a verified quote replaces your word only if you typed it after that word. The
  coordinator can name the drop that holds your words (`--drop DROP_ID`), since a drop reaches it in its brief file and not
  as a prompt. Reads Claude Code and Codex transcripts; checked against this machine's real ones, not on a released build.
- **An agent session can set your `away` or `remote` presence, only when it quotes your words.** `levain ping away`
  or `remote` run inside an agent session needs `--said "<what you told it>"` (the coordinator needs it for every
  word it may set); with no words, or with words over 300 characters, the command is refused and nothing changes. The record keeps the agent, its session and the quote, and `levain ping status` adds
  "set by <agent> <session> from your words: <quote>" (the coordinator's own session is named as such). A seat may set
  only `away` and `remote`; the coordinator may also set `quiet` and `sleep`. `back` and `loud` stay yours, and no
  agent session can replace a standing `loud`: while it stands, every word an agent tries to set is refused. A session that is detected but cannot be named refuses and sets nothing.
  Run from your own terminal (no agent session above it), any word works as before. Known edge: the role comes from
  the session id in the caller's environment under one user id, so a seat that sets the coordinator's session id is
  recorded as the coordinator and may set `quiet` or `sleep`; the record shows intent and does not stop a process that
  lies about its environment. Covered by tests; not run by hand on a real machine.
- **The cockpit has a Coordinator panel.** It shows the coordinator's wake state and the reason `levain coord
  status` gives for it, verbatim, then who holds the lease and until when, the coordinator setting, and the latest
  launches with their cause. The state is one of `held`, `launching`, `idle`, `unset`, `backoff`, `stopped` or `wake`.
  The panel's reads open the store read-only; its buttons (next entry) write. A lapse or a pending launch's expiry that is due but not yet written is applied
  to an in-memory copy, so the state row matches the CLI's, and the lease and launch rows say the write is not
  recorded. Covered by tests, including a real-browser test with one fixture store per wake state.
- **The Coordinator panel has Start, Stop and Hold.** Start asks for a coordinator now by the rules a drop runs (it
  waits out a backoff and starts nothing while the coordinator is stopped) and starts it at once; a Start
  that the coordinator's state refuses names that state and its reason, as the panel shows it. Stop releases the
  coordinator lease, voids a pending launch and ends a running one, and the serve's watcher stops that launch's
  session on its next pass. Hold takes the lease for 30 minutes: a pending launch is voided, a running launch ends
  the same way (the watcher stops its session on its next pass), and drops queue for you. A retry of the same tap
  repeats nothing, and a Hold whose lease cannot be read back says so instead of failing. The three buttons exist only
  on a serve that runs the watcher. An embedding server that runs its own watcher asks for them with
  `make_server(coordinator_verbs=True)` over a writable source.
  Covered by tests, including real-browser
  tests; not run by hand on a real machine, and a real session stopped from the panel has not been watched.
- **`levain serve --write` supervises the coordinator.** While the serve is up it runs the pass `levain coord watch`
  runs, every 30 seconds: it starts a pending launch nobody started, ends a launch whose session is gone, beats the
  live holder's lease and stops the session of a launch that is over. It reads the directory the Coordinator panel
  reads, and a directory with no coordinator store is not watched and not created. A pass and a hand-run `levain coord
  watch` take one lock, so they never stop the same thing twice. A pass that faults is one line in the serve log, and a
  pass whose lines repeat the previous pass's is not logged again, so a standing refusal such as "claude is not on PATH" is logged when it starts and when it changes. A launch finds `claude` on
  the PATH a login unit gives the serve. Covered by tests; a real serve under launchd stopping a real session has not
  been run by hand. Known edges: the serve's daemon unit does not carry `LEVAIN_HOME` (only `install-feeds` does), so
  every store under the levain home that a daemon-installed serve reads, the Coordinator panel's, its buttons' and the
  pass's included, is the default one, `~/.levain`; a hand-run `levain coord watch --once` exits 0 when the serve's
  pass held the lock.
- **A stopped coordinator puts one item on the Needs you band.** When three launches in a row have died, nothing
  launches until you look; the watcher files one band item per stop, with the panel's own reason, and pings at most
  once (when a notify command or a phone with pings is set up). Its one option, `seen`, only records that you saw it:
  the action is the Coordinator panel's Stop or Hold, and a Stop or Hold pressed there closes the item. Start is
  refused while the coordinator is stopped; a Start that goes through once the stop has ended some other way also
  closes a leftover item. An item you have answered is not filed again while the same stop stands, and a new run of
  deaths after a Stop, a Hold or a release from the command line files a new one. A coordinator whose setting names no harness Levain knows still files
  it, under Claude Code's name. Covered by tests; a real stopped coordinator on the band and the phone has not been run
  by hand. Known edges: a stop cleared from the command line leaves the item open until you answer it or press a panel
  verb, because no answer channel is Levain's own; a Stop, Hold or Start landing in the instant a pass is filing can
  still send one ping for a stop that has just ended; and a verb that lands before the filing commits leaves the item
  filed after the stop ended, open and without a ping, until you answer it or press a panel verb.
- **The cockpit's Board tab groups the ready queue under column headings, and the panel's ⤢ view draws the six columns.**
  The columns are backlog, ready, running, waiting, needs-you and done, each heading with its count. A downstream panel
  can declare the new `board` value kind with levain's `board.json` value; the kernel checks its shape only. That
  panel draws grouped on the page and as six columns in ⤢, carries a filter box and a count (the whole board's size, or
  "k of n" while a query is typed), and keeps its typed query across a refresh. The box narrows cards, never columns,
  and its query is shared by the panel and its ⤢ view, so ⤢ opens already filtered. A card draws its done line ("done
  when: ...") and `depends_on` ("after: ...") when it has them; a card with no journal record draws no journal line. If
  the board cannot be read, the open ⤢ view says so in one line instead of showing the last board. Covered by tests,
  including real-browser tests of the grouped tab; not run against a downstream's live board.
- **The needs-you band is on the cockpit's Decisions panel and the phone's Needs-you list.** Items carry band numbers; a
  tap on an option or a shorthand box answers by number. An answer made against a view that has since changed is
  refused, and the panel redraws and says why. The answer path accepts human channels only. On the phone, a followed
  sign link stays first and the band card follows it. Covered by tests (real Chrome for the pages); not run by hand.
- **The phone's dispatch card names the version it showed when you sign or cancel.** If the request changed after the
  card was drawn, the challenge is refused with a 409 "changed since it was shown" and the list is redrawn; any other
  409 on a challenge also shows "not signed" or "not cancelled" and redraws. Covered by tests; not run by hand.
- **BOUND, slice 1: a classifier and ledger columns for what a vendor's sandbox refused inside a call that ran.** The
  consent ledger gains `bound`, `bound_denials` and `bound_classes`, added in place with the ledger format unchanged.
  It informs and never authorises, and it stores a class and a count, never a URL, host, path or stderr line. It
  matches only strings measured in a real run; any other denial text reads `unknown`. Known edges: a Codex tool
  response is one string, so a command printing the phrase on stdout reads `touched` there; a Codex patch the sandbox
  rejected may fire no post event and so has no row. Covered by tests.
- **The shipped base map carries a `codex-subscription` passthrough route** (hosts `api.openai.com`, `auth.openai.com`,
  `chatgpt.com`), so `levain session codex --auth passthrough` no longer refuses `no-route:passthrough` for want of a route.
  This adds the route only; it does not claim Codex passthrough works inside a session.
- **`levain upgrade` (hidden, like `levain session`) upgrades Levain and its root side in one command.** It runs the
  installer that put Levain there (pip, `pip --user`, pipx or uv; it refuses and prints what to run when it cannot tell),
  restarts itself on the new code, stops with the clean-up commands printed if a session is stuck, then runs exactly
  `sudo <install-helper> repair --source <package>`, so the root helper's own checks (including who may write the
  source) all still apply. It asks for your password once per run, refuses to run as root or without a terminal, and
  `--dry-run` prints what it would run and changes nothing. It always runs the repair, even when the installer found
  nothing newer: the repair is safe to re-run, and it is the only check of the other root rows. So every run costs a
  password and a restart of Levain's services. The installer and sudo get only the environment they need (your
  `PIP_*`, `UV_*`, `PIPX_*`, proxy, CA, `PYTHONUSERBASE` and session-bus settings go to the installer), never your
  tokens or cloud keys. They run from `/`; a relative config path such as `PIP_CONFIG_FILE=./pip.conf` or
  `NETRC=./netrc` is made absolute from your directory first. The restart onto the new code ignores the current
  directory and gets none of your `PYTHON*` settings except `PYTHONUSERBASE` and `PYTHONNOUSERSITE`, so
  `PYTHONPATH=.` cannot bring a local `levain/` back in. If the helper gives no result, or reports success but sudo
  exits non-zero, it says the outcome is unknown and to run `levain upgrade --no-pip` again. The first hop from 0.11.26 is still two commands, because
  that release has no `upgrade`: `pip install -U 'levain[isolation]'`, then `levain upgrade`. Later hops are the one
  hidden command. It is not `levain update`, which refreshes one install directory's hooks. Covered by tests that
  drive every path through fakes (no real installer, sudo or root helper has been run by them).
- **`levain session claude|codex --auth passthrough` (no `--login`) is no longer refused at launch.** The launcher
  accepts it for both vendors; if the start step then refuses because the slot's stored login answers as a different
  account, the supervisor says which account (from Levain's own session record, never the session's terminal output)
  and that nothing was launched. For Codex it prints first that Codex has no server-checked identity, so Levain cannot
  tell which account the stored login belongs to (not a check). Run on a macOS VM for Claude only: a matching
  login started Claude logged in and was parked again at the end, and a baseline naming another account was refused with
  nothing launched and the parked login untouched. Not run in that first run: Codex, Linux, and a stored login older than about five and a
  half hours (the next entry replaces the check on a stored login).
- **An interactive Claude passthrough start refreshes the stored login once before it checks whose it is, so an old login no longer refuses and a swapped credential file is caught.** The check runs as the slot: it
  exchanges the stored refresh token at `platform.claude.com`, writes the rotated tokens back to the stored login
  atomically, then asks the profile with the new access token, so the account it names is the one the file's own refresh
  token belongs to. A dead login says to sign in again (`identity-expired`); an exchange that got no answer spends
  nothing and says to try again (`identity-unreachable`); nothing is launched on either. This replaces the earlier
  "reuse the login verified when the slot last parked" entry, whose whole-file pin is gone. Run on a macOS VM with real
  accounts, Claude only: two real starts on a not-yet-expired login (the refresh token rotated and was rewritten each
  time, no temp file left, the baseline unchanged); a file holding one account's access token beside another account's
  refresh token was refused `identity-changed` with nothing launched; a refresh token already spent was refused
  `identity-expired` from a real HTTP 400 `invalid_grant`. A login whose access token had really expired was started
  once (10-29 19:11 ET, same macOS VM, Claude only): Claude started logged in with no prompt, and across that start the
  stored login was rewritten with a rotated refresh token. That run did not separate Levain's exchange from
  Claude's own refresh. Not run: Codex, Linux. Known edges: a refresh that succeeds but whose
  exchange then stalls past 60 s keeps the old, now dead token (sign in again), and the start says `identity-unreachable`
  ("try again") for it, so the retry meets `identity-expired`; `identity-unreachable` is also said
  for an oversized credential, where a retry cannot succeed; a profile call that fails after a good refresh is named
  `identity-unparsable` instead of `identity-unreachable` (the new login is stored; starting again is the remedy);
  `levain upgrade` and routes-base still accept a passthrough route without the token host, and the next start refuses it.
- **A passthrough login asks whether to keep that account as the slot's baseline.** Once
  `levain session codex --auth passthrough --login` exits, Levain shows the account from its own session record (never
  the session's terminal output) and asks whether to keep it. Codex has no server-checked identity, so the baseline
  records it as "not a check". `levain session claude --auth passthrough --login` asks the account server which account
  the new login belongs to, as the session's own sandboxed account, and shows that account id when it asks. Anything
  still running as the session's account is ended first. If the check fails in any way, the login still completes and
  the failure is recorded by name; no baseline is offered. Run on a real macOS machine with two logins.
- **Signing a slot in again now works.** When a second `levain session ... --auth passthrough --login` on the same slot
  ends with your yes to its baseline question, the new login replaces the stored one. The old login is kept with that
  session's retained work (discard, restore and purge carry it), not deleted.
- **The cockpit's Build tab has a Threads section.** It shows the coordinator's threads and one thread's stream in the
  order it happened, with running tool calls, approvals shown as a line pointing you to Needs you, and your own lines
  marked unverified. It needs the page's write token and is read-only for now.
- **Pings come from the cockpit's own origin.** The cockpit serves web push itself (service worker, manifest,
  `/webpush/*`), behind its credential. The Devices panel turns pings on for the browser you are using with one tap
  on Turn on: a browser holding the cockpit's own key is paired on the spot, keeps its new device key, and shows up in
  the Devices list as "this browser (…)" with a Revoke; on a phone, add the cockpit to the Home Screen and pair it from
  there. Every subscription belongs to a paired device, and a revoked device gets no ping, whatever happened to its
  stored subscriptions: every send checks the device is still paired.
- **Sign links open the cockpit's sheet and carry no token.** `levain.consent.sign.deep_link` builds
  `<origin>/sign/<id>`. `levain consent phone --redirect-to <origin>` runs the old phone port as a redirect only: links
  you already hold open the cockpit's sign sheet, and the token in them is dropped, never sent on.
- **`levain consent canary --sandbox` checks the confinement.** It asks the agent, started through `levain session`,
  to run one fixed probe that tries five escapes (a write in your home, a host off the allow-list, your cockpit's port,
  a stand-in port the canary counts, a git hook in the repository the session was cloned from). The verdict comes from
  a results file the probe signs with a per-run secret over the exact command it was given, and from what Levain sees
  outside the session, never from the agent's screen: a retyped command reads as unclear, not as a pass. It fails on
  any escape and on an unclear run. The check reads the agent's work, and discards its scratch session afterwards,
  from the session the store records as cloned from the run's own commit, never from what the agent's screen says.
  `--cockpit-url` (with `--sandbox`) names the cockpit if it is not at the default address.
- **The cockpit signs in an overlay sheet.** A sign card opens `/sign/<id>` over the cockpit; the sheet shows the one
  request, freezes a written answer when you tap Sign and shows the exact line being signed, then the line that was
  signed. Before asking for a passkey it checks that a key enrolled for the cockpit may sign the request, and says what
  is needed when none may. If a reply is lost it reads the request twice, about a second and a half apart, and unlocks
  only if it is still open both times; closing mid-signature says it is not confirmed and checks first. Escape closes the
  sheet (not mid-composition), Tab cannot reach the cockpit behind it, and a cockpit that stops answering is given up on.
  The sheet is the only page the cockpit lets be framed, and only by its own origin.
- **The cockpit's `!N` badge breathes when a new decision waits on you.** It pulses for about half a minute, then sits
  still; it does not pulse at all when your system asks for reduced motion.
- **`levain binding policy create|renew|revoke|list` signs the graduation policies** that let a standing binding run
  below confirm (cooling-off or on-loop) without asking: the actions it may run at the risk Levain's manifest gives
  them, the lowest rung, and an expiry at most 30 days out. A policy covers only while its signing keys are listed in
  root's allowed-signers file and are not revoked; `create` prints what it hands over before anything is signed, and
  the command it prints names its store and cockpit by absolute path, so it signs the same store from any directory.
- **`levain doctor` says when nothing can graduate.** It reports root's authority-signers file when it is missing,
  untrusted or lists no key, and prints the one `sudo` line that enrols them (with every reason the helper may refuse).
  It reads that file the way the install helper does (size bound, whole-file UTF-8, no links), names every missing
  signer of a live graduation policy, and reports an unreadable cockpit authority log as a warning instead of an
  apparently healthy signers row. A damaged file or log never aborts it.
- **`levain daemon install-feeds` runs the review queue's feed pass on a schedule, with no model.** The pass is
  `levain coord review feeds`. Each run is bounded in whole seconds by the operating system, covering the whole command,
  and skips if another is still running; every clean run records its pass, so the cockpit shows when it last looked.
- **The cockpit's sign card no longer says nothing was signed after one read.** When an answer's reply is lost it stays
  frozen, reads the request twice, and comes back only if it is still open both times. Cancelling a passkey prompt on
  the card keeps the remembered passkey.
- **`levain doctor` reports the agent-session reaper, CONTROL and held sessions.** It says whether the reaper is
  passing, whether CONTROL answers, and any session stuck mid-end with the step that failed. It does not call a state
  healthy that it cannot establish now (a stale or contradicted pass record, an unanswered CONTROL); run as root it says
  CONTROL was not asked; an oversized or refused CONTROL answer reads as "could not list", not as a failure.
- **`levain binding graduate` says what is missing, and still exits 1.** It names the autonomic store (with its path;
  `--store-dir` takes the same store `policy create` signs) and that no executor can yet run a binding effect; a store
  or gate it cannot read is reported as a reason, not a traceback. `levain binding policy create --help` says that
  cooling-off behaves like confirm until an action is allowed to fire on its own.
- **The cockpit and the phone show the coordinator's review queue and morning card, read-only.** The cockpit shows them
  on the Morning and Board tabs: what needs a look, newest first, how long ago, and which items only you clear. The phone
  page has a Review tab with the morning card, the open items (what each is, what it says, how old it is) and the ones
  you cleared; it reads one page at a time. Neither appears until the coordinator has been used. Opening them never
  creates or changes the coordinator's store. They say when the review check last ran, or that it has not run yet or
  could not read the queue, and never "Nothing is waiting on you" in those cases; a section the morning card could not
  read says so instead of showing zero. A command too long for a cockpit row is cut with a visible "shortened" mark.
  On a phone other than an iPhone the page says "your screen lock" where it said Face ID.
- **Clear and reopen a coordinator review item from the cockpit or the phone.** The cockpit's Review queue and the
  phone's Review tab offer Clear on an open item and Reopen on a cleared one, as you. Only a paired device can do it
  (the shared page token cannot). The cockpit's queue also lists the ten most recently cleared items, under Cleared, so
  one can be reopened, and says when older cleared items exist and where to see them; its count is still the open
  items. A Clear or Reopen of an item that changed after you saw it (or in the same instant), or sent from a device
  revoked meanwhile, is refused and writes nothing, and neither ever creates a coordinator store. If your signed
  standing policy raises a review Clear or Reopen above its usual tier, or the authority log cannot be read, the
  cockpit and the phone refuse it and say how to clear it instead. When the coordinator store cannot be read or written
  for a moment the phone says "try again later". `levain coord review list` and the phone's queue show an item whose
  stored details are not in the expected shape, and `levain coord review clear` on a directory with no coordinator
  store says so and creates nothing.
- **An item marked "yours to clear" can be cleared with your passkey.** "Clear with passkey" on the cockpit or the
  phone asks you to sign the item's clear, and only a signature from a passkey enrolled here, over the item as it is
  now, clears it. One signature clears an item at most once: sent again, it is refused as out of date. The store
  records it as a signed clear; `levain coord review clear` on the laptop still clears it unsigned, recorded as manual,
  and is what the cockpit and the phone point to when your signed standing policy raises the passkey Clear above its
  usual tier (the phone refuses that before asking you to sign). On the phone, a second tap or a retry reuses the open
  request instead of filing another; with no passkey enrolled it says so and files nothing, and an item that does not
  exist, is already cleared or needs no signature is named as such instead of asking you to enrol a passkey. Where more
  than one passkey is enrolled, the picker stays on screen until you pick one or tap Cancel: a queue refresh already
  under way does not remove it, and Clear with passkey on another card leaves it as it is. Any store fault on a signed
  clear other than an already-used signature is reported as the store being unavailable.
- **`levain doctor` reports the brokered-spend install.** New rows: whether the dispatch route trust is pinned (and to
  which key; `trust.pub` is checked with `ssh-keygen` the way Levain verifies a route map, so a damaged key file fails),
  whether the keys service runs (an install with no egress slot says so instead of failing), the spend ledger's size,
  and, for each brokered credential, the provider-side limit that bounds your invoice, counting only the dispatch routes
  the keys service actually serves. OpenRouter's key limit and what remains of it are read when your account can open
  the credential, with when it resets and whether it leaves BYOK usage out or is used up; Anthropic's and OpenAI's are
  named as settings to make in the provider console. All provider reads together take at most ten seconds and never
  fail the check. It says so when `ssh-keygen` cannot run or no credential is set at all.
- **The cockpit remembers which passkey belongs to each enrolled key.** The first time a passkey signs from the cockpit,
  its credential id is recorded, so any browser (a new one, a re-paired one) asks for that passkey directly.
  `levain cockpit enroll --attested` records a new passkey's credential id and its provider (iCloud Keychain, Google
  Password Manager, and others), and `levain cockpit keys` shows them; rerunning it for a passkey already enrolled fills
  in what was never written and never replaces an id already held. These are display facts only: what a key may sign is
  still decided by its class alone.
- **Phone pushes follow one rule:** a ping is pushed unless you are muted, and then only when it is urgent (a request
  that is holding an agent), you are away, or your Mac has seen no input for 5 minutes. `levain ping quiet [30m]` and
  `levain ping sleep` mute: urgent pings still come through `quiet`, and `sleep` holds everything until you next touch
  the keyboard or mouse (or for an hour when this Mac cannot report its input). `levain ping loud [30m]` pushes
  everything for a while over any mute (`loud off` ends it); `away`, `remote` and `back` are optional overrides. A held
  ping is never lost: it is pushed when your Mac has been idle for 5 minutes, when a mute ends (one summary push, then
  the held pings), or when you say `loud`, `away`, `remote` or `back`, and it leaves the queue only once every phone
  still paired has taken it; a failed push is tried again a few minutes later. `levain ping status` says what the rule
  does now, why, and how many pings are held.
- **`levain doctor` run as your own account shows what the keys service reports,** instead of "held by keys": the
  spend ledger's size, the credentials of the routes keys serves (saying when keys' answer could not list every route,
  and never listing a credential keys does not serve), and each OpenRouter key's spending limit as keys last read it,
  with how long ago. keys reads those limits itself, when it starts and every five minutes; if its latest read failed
  the row says why, and a key past its limit shows as exhausted. Asking doctor never makes keys contact OpenRouter.
  When keys or CONTROL cannot be reached, or CONTROL has not yet connected to keys, the rows still say "held by keys".
  `sudo levain doctor`, which reads the key itself, checks OpenRouter's answer exactly as keys does.
- **Message a seat from its node.** Tapping a seat on the cockpit's agents graph opens its page: its status, what it is
  doing, and a box to message it, sent and signed like any other cockpit message. A seat the cockpit has no way to reach
  says so instead of showing nothing to type into, and a page that has turned read-only says nothing was sent (also
  when it turns read-only while the passkey prompt is open). The tap no longer jumps to the message card further down
  the page, and that card now sends to the desk, the cockpit's other readers or everyone: an untouched card asks you
  to choose a reader instead of sending to a default.
- **`sudo install-helper enrol-authority-signer`**: enrols the keys whose signatures can make a graduation policy
  count. One run lists the laptop key (`--public-key`, add `--confirm` to let it sign approvals too) and each phone
  passkey (`--passkey`, repeatable) in root's allowed-signers file, `/private/var/levain/policy/authority_signers`
  (`/var/levain/policy/authority_signers` on Linux), and prints each key's fingerprint to compare with
  `levain cockpit keys`. Keys are typed on the command line; root reads nothing the operator's account can write.
  A key the authority log would refuse to enrol is refused. `remove-authority-signer --key <fingerprint>` takes a key
  out again. Running either twice changes nothing. Both refuse to touch root's signer file unless it is root's alone
  (a file or folder another account can write is refused, its lines never re-written as root's), can be read whole,
  and is plain UTF-8; comments in the file are never taken for keys. Re-enrolling a key under another name, or with
  fewer permissions than its line has, is refused: remove it first. `undo` removes the file too, so a later setup for
  another person starts with no listed keys.
- **`levain dispatch config trust --replace` rotates root's route-trust pin** to a new dispatch store or a new laptop
  key. Stop the control and keys services first; it is refused while an open ticket or a live session still uses the
  old store, or while keys runs. The old store's route maps, verifier configuration and keys' head are moved to
  `L/routes/.replaced-<time>`, not deleted, and both restart at the new store; it says where the old store was set aside.
  If anything fails part way, the old pin and store are put back. A replace interrupted part way leaves a marker; until
  a `--replace` is run again, the control and keys services refuse to start, and that run first finishes the
  interrupted replace or puts the old store back. On an install where keys never ran, the replace also holds keys off.
- **The cockpit answers an agent's consent request with your passkey.** On the Needs-you card, Allow and Deny open the
  sign sheet with that answer picked, and an undecided request can be ruled from the decisions panel. The checks are the
  phone page's own. An answer a passkey cannot give here (allowing a T3 request) stays a terminal command, and a cockpit
  served on a port says where the request can be answered instead of asking for a passkey. A ruling is confirmed as soon
  as it is recorded; its delivery to the session continues on its own.
- **The consent sheet treats a request as changed when the words an option shows, or the exact line it signs, change.**
- **Slot sessions can fetch from a shipped list of hosts without a prompt.** Setup and repair add a shipped
  `egress-default` route to your route map (`github.com`, `api.github.com`, `pypi.org`, `files.pythonhosted.org`); every
  other host is still refused at the gateway, and the refusal now names how to allow it.
- **The install helper's `routes-egress` verb sets your own extra egress hosts** (JSON on stdin; an empty list removes
  them). They apply to sessions started afterwards. If your route map already has your own egress route for one of the
  `egress-default` hosts (for example `pypi.org`), your route keeps it and the shipped list leaves it off; setup, repair
  and `routes-base` say so. A map where two of your routes name the same host, a route that lists the same host twice, a
  subscription route that names one of those `egress-default` hosts, or your own egress route that names a host the
  shipped Claude route below also names (`claude.ai`, `platform.claude.com`) is refused when the map is merged, with
  the route names and the fix (edit `routes/base.json`), instead of every session failing at its gateway;
  `routes-egress` refuses to write over such a map.
- **The cockpit has a Calm switch.** On, the agents graph and the sky stop moving and the page's animations and transitions stop, and the
  ages on the page refresh every two minutes instead of faster, so an open cockpit tab costs the laptop far less; every
  number, card and answer stays. It is remembered on that device. On a desktop bar too narrow for the label it shows as an
  icon, filled while Calm is on; on a phone it sits at the right of the clock and date.
- **A Levain session can sign in to your own Claude subscription.** The base route map now includes `claude-subscription`,
  which lets a passthrough session reach `api.anthropic.com`, `claude.ai` and `platform.claude.com` and nothing else;
  telemetry, error-reporting and update hosts are not included. For now a passthrough session runs only as a sign-in:
  `levain session claude --repo PATH --auth passthrough --login`. `levain setup` and `repair` add the route to an
  existing install; if you already have your own passthrough route for Claude, the merge keeps yours, skips the shipped
  one, and says so each time it runs.
- **A slot's network front keeps a log.** Each egress slot's gateway writes its tunnels and refusals to `front.log` in
  the slot's gateway directory; the log is capped (one rotated copy), mode 0640, and never written through a link.
- **`levain binding pause|resume|abandon <id>`.** `pause` stops a binding from firing and stops runs it already started
  before their next action; `resume` makes a paused binding fireable again (it refuses a revoked or expired binding, and
  a confirm-class one with no kill in its sealed guard); `abandon` stops the runs a revoked or expired binding already
  started, and on an active or paused binding pauses it instead. Bindings do not fire yet in this build (no binding
  driver is wired), so these set the state the driver will read. Each opens the autonomic store as it is and prints one
  line (exit 1 with the reason when it cannot).
- **Graduation policies carry a guard.** `levain binding policy create` needs `--guard FILE`, a JSON list of entries that
  each give a reason the policy might be wrong, a kill (an event condition) and a drill (an example event the kill must
  not let through). Levain prints the guard back before signing and refuses a kill it could not evaluate, a drill the
  kill lets through, an entry missing any of the three, or a guard past the size limits. The guard is checked when the
  policy is signed, and, once the binding driver is wired, again just before a step it covers fires (see Groundwork). Every policy created now carries a guard and is written
  `levain-authority/3`, which an older
  levain stops at instead of reading. A graduation policy signed without a guard no longer covers anything (its bindings
  wait at CONFIRM again) and cannot be renewed: revoke it and create it again with `--guard`. The graduation card's
  command line includes `--guard <guard.json>`.

### Changed
- **The consent queue's store moves to format 5, one way.** The first consent hook call or `levain` command that opens
  it migrates it in place (from format 3 or 4), keeping every request, signed answer and receipt. An older levain then
  refuses that store: its hook makes no decision (the agent's own permission dialog asks), standing grants stop
  answering, and agent-config edits are denied. There is no way back to format 4, so upgrade every levain that shares
  the store (the hook, the cockpit, the CLI) together.
- **A coordinator store made by an earlier version is updated the first time a `levain` command that writes to it
  opens it:** the review history gains one column, and every entry is kept as it was.
- **`levain doctor` has an agent firewall probe row:** each agent slot's last recorded probe result, with its age and
  session. An open wall fails the row; an unknown or inconclusive result, or a closed one older than a day, warns; a
  slot never probed says so. Doctor reads the record and never probes.
- **A metered request with a `route` or a `models` fallback list (other than exactly its own model) is refused
  `model-not-allowed`.** A response that says another model ran is no longer priced as the requested model: the call is
  charged its whole hold, or the cost the provider reports when that is more, and the session stops as for an overrun.
  The check that a response came from the requested model applies to OpenRouter (openai-chat) routes only; an Anthropic
  response is settled at its reported usage, as before. On those routes a response naming the model as null, or naming
  two different models across a stream, counts as another model.
- **CONTROL's `STATUS` carries `budget_overrun` on a session stopped for an overrun:** the amount over, the number of
  calls, whether any was unproven, and `model_mismatch`. A session stopped because another model answered is shown even
  when it was charged no more than its hold.

- **A Claude Code session's settings skip WebFetch's hostname preflight, and claude.ai connectors are off in every
  session.** A session on an egress route is allowed WebFetch to that route's hosts without a prompt; the gateway still
  refuses every other host.
- **Codex sessions start with web search disabled on the command line,** so a repository's own Codex config cannot turn
  it back on.
- **A repo settings file that sets a proxy variable** (`HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`, `ALL_PROXY`, any case)
  **is refused at launch, by name.**
- **The gateway's log line for a refused connection names the host that was asked for,** when it is a valid host name or
  a plainly written address.

### Fixed
- **A login that ended without a yes on a slot that already holds a login no longer sticks.** The session used to end
  HELD and keep its slot leased for good. Now the stored login is left as it was, the new one is kept with the
  session's retained work instead, and the slot is released. A session already stuck this way is released by the
  reaper's next pass.
- **A brokered call the vendor refuses now records the vendor's error in the gateway log** (`upstream_error` in
  `gw.jsonl`: the error type, a short message and the request id, with any configured credential redacted), so a
  refusal such as a 400 says why instead of only its status.
- **Linux replenish restarts the keys service when it makes new gateway accounts, and does so after the pool is
  refilled,** so a keys failure cannot leave the pool short.
- **Two sessions making their first call at the same moment on a fresh install could both be refused with
  `database is locked`.** Opening a new store now spaces its retries at random, so the two openers no longer keep
  colliding.
- **A session's `budget_overrun` in CONTROL's `STATUS` goes away when the session is purged.**
- **On an OpenRouter route, a response from another model that reports a cost above the call's hold but no token
  counts is charged that cost** (it was charged only the hold).
- **The cockpit's top bar no longer runs over its tabs between 1041 and 1099 px wide** in a font wider than macOS's
  system font (Linux's default font, or Verdana): up to 1100 px the bar stacks, with the tabs on a row of their own, and
  Calm shows its icon from 1101 to 1280 px.
- **The levain home a consent hook protects comes from the environment the hook read when it started,** the same as
  everything else on its decision path. When the hook cannot open its queue, a write to the configured cockpit's
  authority store is still refused.
- **Chat: a read that crosses a turn boundary still answers or interrupts the control requests it carries,** so a
  pending request no longer leaves the CLI waiting while the read is refused. Read again, it does not re-send an
  interrupt or reply it already delivered; a later request that reuses an answered request id is answered.
- **Chat: a control request whose reply keeps failing is answered on every read, but the turn is interrupted for it
  only once.** An activation or re-enable waits for a read in progress, so that read's interrupt never reaches a turn
  started after it. A control reply the session does not hold still latches the launch off.
- **Chat: a Read whose path (or a launch directory) contains a NUL byte latches the launch off instead of erroring.**
- **`levain session adopt` works on a real macOS install again.** The confined importer died at startup, before it read
  anything, because its sandbox denied the system random device Python needs to start, so every adopt refused
  `importer-failed:result-malformed: 'no result line'`. The importer and verifier sandboxes now allow reading
  `/dev/urandom`.
- **`levain session adopt` no longer refuses `bad-root` on `/var/levain/export/operator`.** The export directories are now
  owned by root (group `levain-export` / `levain-dispatch-export`, mode 0750), as the store layout specifies. **Run the
  install helper's `repair --source <levain package>` once after upgrading** so an existing install's directories are
  re-owned.
- **Setup and repair on macOS now remove extended ACLs from Levain's store directories.** A directory that an older
  install gave to another account could keep an ACL grant (for example, to add files) after repair made it root's, and
  Levain's trust check would not see it.
- **macOS: after a reboot, Levain now restores pf on its own.** The `com.levain.pf` boot job turns pf on, loads Levain's
  firewall rules and puts back the loopback addresses the egress slots use. Before, `setup` and `repair` reported the job
  as pending and installed nothing, so every session refused to start until pf was set up again by hand. The job keeps
  its output in `/var/log/com.levain.pf.log`. If it cannot save its pf token it gives back the reference it took and
  launchd retries it until the token can be saved, so pf references no longer pile up; while the token's path stays
  unwritable, the job retries on launchd's throttle.
- **macOS: a session no longer starts while pf itself is disabled,** even when Levain's rules are loaded. The start is
  refused with `pf-disabled`.
- **A chat launch's network mode can only get stricter while it runs.** Turning the standing "open network" default on
  takes effect from the next chat launch; turning it off still applies at once to launches already running. A read of
  that setting that fails, is slow or answers too late gates only the request it was read for.
- **`pip` can install packages in chat again.** Recent `pip` versions check certificates through the macOS system
  verifier, which the chat sandbox cannot use, so every download failed. The chat environment now sets
  `PIP_USE_DEPRECATED=legacy-certs`, which makes `pip` use Levain's chat certificate. A `pip` older than that option (such
  as the 21.x that ships with Xcode's Python) refuses the setting; run it as `env -u PIP_USE_DEPRECATED pip ...`.
- **More sandbox settings that weaken Claude Code's sandbox are refused.** A repository's `.claude/settings.json` or
  managed settings that add Mach services (`sandbox.network.allowMachLookup`), allow Apple Events or nested user
  namespaces, re-allow reads (`sandbox.filesystem.allowRead`) or turn filesystem isolation off
  (`sandbox.filesystem.disabled`) are now refused by name, as `enableWeakerNetworkIsolation` already was. Allowing
  `com.apple.trustd.agent` let a sandboxed command make macOS fetch a web address of its choosing, outside Levain's proxy.
  Such a key holding the wrong type (for example `false` or `""` where a list belongs), or a list holding anything but
  strings, is refused by key name instead of launching.
- **A stuck cockpit read no longer wedges its panel until restart.** After the panel's time budget the cockpit starts a
  fresh read, holds at most two outstanding builds per source, and a panel waiting on the stuck read gets the fresh
  one's result. An idle store now costs the cockpit nothing (no time-derived etag, a cached view).
- **Turning on the system's reduced-motion setting no longer switches Ambient off for good.** The sky stays still while
  the setting is on, and the motion you chose comes back when you turn it off; Calm never writes the Ambient preference.
- **`levain binding resume` refuses a binding whose stored record is damaged** (a hand-corrupted counter) instead of
  making it live; pausing or revoking it still works. The pause, resume and abandon commands also read a store through a
  read-only connection first, so a database they refuse is left exactly as it was, and refuse a `/2` store missing a
  column.
- **`levain doctor`'s "agent front logs" row judges what a session start would.** It asks the runtime's own walk about
  each egress slot's front log directory (missing account, a `gw/` that is a file, a link, a wrong owner or mode fail
  the row; a genuinely missing directory also fails it, marked upgrade-pending with the repair line), names each slot
  under its own result, and no longer warns "could not be read" on a healthy install. A session start is refused for a
  missing front log directory too, and names the directory that is missing.
- **`levain binding policy create`'s read-back no longer overstates a guard.** A kill condition that hits an unexpected
  internal error, or an event number too large for a float, now reads as a kill Levain could not evaluate, so the
  guard is refused at signing instead of the check crashing.

### Groundwork (not reachable from a `levain` command yet, except where an entry says so)
- Groundwork for the session-start brief: the morning card takes an optional `now` (no wall-clock read, no band numbering when given), the band can be read without recording a view or numbering an item, and the ready board can be read on a connection the caller already holds. Every default is unchanged, and nothing in `levain` passes them yet, so there is no change you can see.
- Groundwork for named agents: a role registry. `levain agents role add <file>` registers a role (name, brief template with typed slots, ceiling, model, harness, executor) under a content hash and a version, and refuses a file with an unknown field, naming it; `levain agents role show <name>` prints it. Nothing runs a role yet, and no role is signed: authorisation comes through the agent link.
- Groundwork for chat approvals: the store now has tables for each approval request a chat session raises and everything that happens to it (asked, decided, answered, cancelled), kept in the same append-only style as the rest of the chat record. Nothing in `levain` writes to them yet, so there is no change you can see. A store from 0.11.25 or later is brought up to date the first time it is opened for writing, keeping every row it already had; opening one read-only changes nothing.
- **A contained-shell grant can answer a Levain chat launch's Bash calls.** It answers only a chat call carrying the
  proof of the launch it was granted for, never a Claude Code or Codex hook call; it is listed, counted and revocable
  like any standing grant, and an answer it gave stops being delivered once it is revoked. Nothing launches a chat
  session that uses it yet (CH-D2).
- **The autonomic store can keep every attempt of an effect (store format /3).** A retry carries the authority of the
  attempt before it (the same grant and cover); an attempt on a held link carries the hold's current decision, names
  who decided (a person or not) as that decision does, and fires no lower than the rung it decided. An attempt that
  never started is closed as refused, with its reason. The store refuses an unknown actor or posture and a run with no
  id. Nothing writes a /3 store yet.
- **A step a graduation policy covers is checked again just before it fires.** Once the binding driver is wired, the
  step about to run reads its cover afresh: if the cover changed since the step was decided, or cannot be read, the
  step is held and no receipt is written. The run is refused and ended when the policy does not sign this action at this risk, cover value
  and rung, when its guard is incomplete, or when the guard cannot be evaluated (no trigger event, or evaluating a
  condition fails with an error). A guard kill that trips, or reads as unknown, ends the run, and the cancel names the
  policy and the guard entry.
- **The graduation engine behind `levain binding graduate`.** Once the binding driver is wired, it takes the steps a
  policy covers, records each, and prints the one `levain binding policy create` line for a step none covers; the steps
  already taken stay printed when a later one fails, and a binding that changed during the run (revoked, paused,
  fired, or graduated by another run) is reported as `stale` with nothing written for it.
- **Chat egress has a composed test suite and a report.** The suite runs the real glue and decider over the sandbox
  runtime with real clients (curl, git, requests, pip, npm, uv) and sweeps every artifact of a run for leaked
  credentials. `levain.chat_egress.report` reads one launch's rows and the glue's event file into the wall total (exact
  after a clean end, marked unknown or crashed after a crash), per-reason counts, duplicate rows, and the path,
  credential-in-query and cap counts. No `levain` command prints it yet. The suite also covers the TLS path with a held
  key (the key reaches only its own host and no file or log; SNI, `Host` and request-smuggling forms reach the origin as
  nothing), `Expect: 100-continue`, pipelining behind a held card, a decider crash, and a client leaving while its card
  is held. A sandboxed client's SOCKS attempt is still refused, and now counted as a `socks` wall in the report.
- **Dispatch can follow the control plane's session events.** It reads them from a cursor it keeps across restarts
  (never moved past an event it could not finish; a new journal or a reset replays from the start), records a verifier's result from them and acknowledges it (`ARTIFACT-ACK`) only when dispatch
  holds that outcome, so the reaper can clear a finished session early. It recognises a repeated verifier refusal only by
  its ticket and outcome, stops on a run already holding another report or on a malformed or off-schema event before
  anything is written, reports a gap in the event stream with the count of events lost, and wakes a read waiting on its
  deadline when closed. Nothing runs it yet.
- **Before dispatch acknowledges a finished run's exported artifact, it takes it:** it copies the bundle into its own
  store under the bundle's digest, checks it against the digest the export names, syncs every directory, and records
  it. When the machine (not the artifact) fails to read the export, the follower stops by name and tries again on its
  next step; an artifact that can never be taken (missing bundle, wrong digest) is set aside after three tries, without
  an acknowledgement, so it no longer blocks later events. Nothing runs it yet.
- **Chat records a vendor's copy of your own message as an echo, not as an outside line.** When a worker's thread
  shows the words you just sent, in the same turn and within a minute, it no longer takes the turn away from you; any
  other line typed outside Levain still does. The timeline marks the echo as the vendor's copy.
- **Chat stores made by an earlier release are brought current when they are opened,** in one transaction that keeps
  every row, replaces any view, trigger or index whose text changed, and checks the file's integrity before committing.
  A store on a file Levain cannot write is read as it is and never changed.
- **The firewall self-probe** (`levain session` stays hidden): the root runtime helper has a `probe <session>` verb that
  connects to the vendor's own addresses as the slot user inside its wall and compares the result with the same
  connects made by root. A session launch runs it between preparing the slot and starting the agent, and starts only
  when the slot could not reach the vendor directly while root could; a probe that cannot tell (no address resolved,
  none reachable by root, a probe that did not answer, an answer not exactly the expected shape) is refused by name,
  never treated as safe. On macOS the probe runs again every minute while the session lives; a slot that reaches the
  vendor ends the session (its work kept), and so do two probes in a row that cannot answer, or five in a row that
  cannot tell (for example because the host is offline). Each probe's result is recorded per slot.
- **Chat checks that a worker's hooks are still live at every turn** and refuses messages while a check runs; a check
  that cannot run is retried, then turns chat off for that worker until you re-enable it. A stuck worker connection no
  longer freezes chat: a read or an interrupt that does not answer gives up after a bounded wait.
- **Chat turns a Claude worker off, until you re-enable it, when its stream shows a permission check Levain did not
  make:** a hook that did not run, an allow rule or a mode Levain did not set (even for a moment), a call accepted with no
  request to you (except Levain's own gate and reads inside the worker's folder), a control message Levain cannot answer,
  or lines the worker's stream lost while a tool call was open. When chat turns a worker off after it was live, a review
  item says which check did it. A control message Levain refuses is answered before chat records it, so a restart never
  leaves the worker waiting.
- **The Codex chat connection fails closed under load.** A request whose timeout passes before it is sent is not sent
  late, and a peer that accepts data slowly cannot stretch a send past its timeout; a send that could not start in time
  fails only that call. An oversized message, or a WebSocket frame that breaks RFC 6455 section 5 (reserved bits or
  opcodes, broken fragmentation), closes the connection at once instead of stalling it. Closing it always releases its
  file descriptors, even while a session callback is stuck.
- **Dispatch has a runner for its event follower** (`python -m levain.dispatch.follow_run`, with `--store-dir` defaulting to the dispatch store directory): it follows
  CONTROL's event stream, retries a CONTROL that is down with a growing wait, waits five minutes between retries of a
  stop no retry can fix, and stops cleanly on SIGTERM. No installed service runs it yet.
- **An isolated install runs a dispatch service as `_levain_dispatch`.** It takes a single-instance lock in its own
  store (`/var/levain/dispatch`), follows CONTROL's event stream into the dispatch job store, and restarts only when it
  crashes. A copy that gives up waiting for the lock exits 75 and is restarted; a clean return counts as a signal stop
  only when SIGTERM or SIGINT was received, and any other clean return is an error exit (70) and is restarted. A bad
  command line, a lock error, or a store directory that is not exactly mode 0700 exits 78, and so does a run as root;
  any other user but `_levain_dispatch` exits 77. On Linux, exits 77 and 78 are not restarted, and every Levain service
  unit stays down after a wrong-user exit (77) instead of restarting every few seconds. Its lockfile is created 0600
  under any umask. It launches no dispatch runs yet.
- Groundwork for the dream: `levain.dream.executor.consolidate_store` is the consolidate that `levain wrap` ran, lifted to take a store path, a compose mind and two hooks, and returning a typed result for every outcome instead of raising. `levain wrap` now calls it with one compose; its exit codes and its `--reset` remedies are unchanged, but some messages now name the store rather than the entity or the seat: "already running on this store" (was "this entity"), "This run would normally discard the orphan" (was "This seat"), "this store is not on / changed off the partnership schema" (was "this entity's store"), "check the log" (was "the seat's log"), and "the memory is unchanged" / "The identity is unchanged" (were "the entity's memory" / "The entity's identity"). The new core adds a re-compose loop (each draft checked first on a scratch copy of the store, up to three composes, every refusal fed back verbatim), a `claude -p` mind and an Ollama mind, a per-run dollar limit with a start rule, and `progress.json` and `usage.json`. The one command is `levain dream run --store-dir <copy> --mind claude|ollama`, for a COPY of a store; it refuses any store whose db, dir, text-tier files or lock resolve (symlinks followed) into `~/.anneal-memory`, refuses a non-positive `--max-seconds`, writes `progress.json` and `usage.json` only once it holds the store lock and never over a record that names a live compose child, and the Claude route refuses to compose until a price row for the model is added. Its messages name only commands that exist (anneal's `wrap-status` / `wrap-cancel --wrap-token` on the store, never `--reset`). Nothing runs it on a schedule. `--max-seconds` bounds the compose and its checks, not the final save, which runs after the bound is disarmed on purpose (the saving-phase journal is the recovery). The run works on the store's RESOLVED `memory.db` (a symlinked db no longer splits the lock and text files from the file SQLite writes); the occurrence dir is made absolute (a relative one names the same dir for the child's package, cwd and `--add-dir`), is refused inside `~/.anneal-memory` like the store, and a default one carries a random suffix so two runs in one second never share it. `--dry-run` needs no price row. A `claude` child that prints a success result and exits non-zero is not accepted (its cost is still counted). After a run that did not save and whose wrap was cancelled, `progress.json` names no wrap (`phase`, `wrap_token` and `composed_sha256` back to null).

### Groundwork (not reachable from a `levain` command yet, except where an entry says so)
- Groundwork for seam handoffs: the coordinator store gains an append-only `seam_handoffs` table and one lease-fenced writer for it, so a coordinator can leave a handoff (state, work in flight, next step, contingencies) at a rotation, an operator leave or a dream. A retry of the same request returns the stored handoff, and a late close behind a newer one is refused. Nothing calls it yet, so there is no change you can see. A store from before this is brought up to date the first time it is opened for writing, keeping every row it had.

## [0.11.26] — 2026-10-25

### Added
- **`levain chat setup` installs chat's one Node dependency** (`@anthropic-ai/sandbox-runtime` 0.0.81, pinned by
  integrity hash) into your Levain home, never globally, and records a hash of the whole installed tree. Chat needs
  Node 26 on your PATH and refuses any other Node version by name; the rest of Levain stays Python. `levain doctor`
  shows the Node and dependency versions, fails on a changed or damaged install, and reports an unreadable one as a
  problem instead of crashing. Run `levain chat setup` again after upgrading.

### Fixed
- **The cockpit no longer loses a script when a browser opens many connections at once.** Levain's web servers
  accepted only 5 waiting connections, so a page load through a proxy such as `tailscale serve` (one upstream
  connection per request) could have a script's connection reset at random: the board then said "read failed:
  undefined is not an object (evaluating 'window.LevainCockpitView.fromManifest')", or stayed on "connecting…". The
  cockpit, phone, init, docs and team view servers now queue up to 128 waiting connections.
- **Two sessions making their first call at the same moment no longer have one call refused with `database is
  locked`.** On a store not yet created (the queue broker's `limits.db` on a fresh install, or any other Levain store),
  opening it now waits for the other opener, up to the same 30 seconds as any other lock, including on the macOS
  system Python 3.9.

### Groundwork (not reachable from a `levain` command yet, except where an entry says so)
- **The queue broker's service program exists for an isolated install.** It serves every slot's queue socket, so slot
  sessions' tool-call limits are enforced, and picks up slots added later without a restart. No `levain` command
  installs or starts it yet.
- **Graduation coverage in the autonomic gate.** Below confirm, a binding's effect (every link of it), a resolve of a
  binding's held action and the silence default of a manual cooling-off action run only when a live graduation policy,
  signed by a key root lists, covers that declared action at the risk Levain's manifest gives it; otherwise they wait
  for your signed approval at confirm, and a policy revoked or lapsed after the decision stops the action at
  admission. A binding's last action always waits at the rung you ratified it at. Re-ratifying a binding to a looser
  rung below confirm needs the covering policy and is refused below the binding's risk floor. A clock or coverage read
  that fails while a held action is being decided leaves it waiting instead of dropping it. The autonomic store
  becomes format `levain-autonomic/2` (a `/1` store is upgraded in place when this version opens it; an older Levain
  refuses a `/2` store). No `levain` command fires bindings yet.
- **The chat network's egress launch.** Levain can start and stop a chat launch's egress proxy: the whole Node
  dependency tree is checked against what `levain chat setup` recorded before anything starts; upstream servers are
  trusted by the system roots only (an inherited `NODE_EXTRA_CA_CERTS` adds nothing) and the egress CA's key never
  touches disk; a launch under `open network` says so on its header ("open network: network writes are not gated"),
  taken from the launch's own network setting; a held key set up with `--basic-user` is placed as HTTP Basic; the
  SOCKS port cannot be held open by a slow client; a launch that fails to start, or a stop whose steps fail, leaves no
  sockets, pipes, threads or helper process behind; cleaning a launch's temporary files never follows a swapped-in
  symlink (Linux: an entry with no safe way to change it is left and reported). No chat launch uses it yet.
- **The gateway control plane keeps a journal of dispatch session events.** Every state change of a dispatch session,
  and its artifact once it is published, is written to disk before the change is acknowledged, and a reader can follow
  them as a stream from any point it last saw that the journal still keeps (a reader that fell behind pruning is told
  about the gap). A restart re-sends any event a crash left unwritten. One exception: a revocation goes through even
  when the journal cannot be written, so a broken journal never keeps a session live, and the change it replaced may
  never get its own event. A stream whose reader stops reading is closed after 30 seconds instead of holding a
  control-plane connection forever; a stream whose cursor is ahead of the journal is told to start again; a stream on a
  journal that can no longer be written ends with an error instead of waiting. If the journal file is lost or empty
  while its identity file survives, the control plane starts a new journal identity, so a reconnecting client resets
  and starts again from each session's current state rather than skipping events. Dispatch has no client for the
  stream yet, so no command follows it.
- **Dispatch can acknowledge a run's result (`ARTIFACT-ACK`).** The control plane accepts the acknowledgement only for
  the artifact or verifier report the session actually published, or, with no digest named, for a run that failed to
  produce one. Nothing in dispatch sends it yet.
- **The reaper moves a finished dispatch session's retained work to trash** once its result is acknowledged, or after
  `dispatch_retain_days` (default 2 days, at least 1). A dispatch session you restored yourself is never moved to trash
  automatically again, even when the restore lands while the reaper is deciding, and the reaper finishes a discard that
  was interrupted after the session reached trash.

## [0.11.25] — 2026-10-25

### Added
- **`levain limits set --slots`, `unset --slots` and `show --slots`: the limits agent-session slots run under.**
  `set --slots <agent|default> <kind> <value>` and `unset --slots <agent|default> <kind>` change tool calls, active hours
  or loop repeats; the command asks your password through `sudo` and the install helper's new `set-limits` verb, which
  replaces the root-owned `L/policy/limits` whole. A token limit, a fraction, an empty block or a value out of range is
  refused by name before anything is written. `show --slots` prints the limits in force for each agent and whether
  they come from the file or the defaults. If the file is unusable, `set --slots` refuses to edit it and names
  `--replace`, which writes a file holding only your change. `undo` removes `L/policy/limits`, so a later setup does
  not inherit an earlier operator's limits.
- **`levain doctor` has a `slot limits` row**: the limits each agent's slot sessions run under and where they come
  from. It fails when the file is unusable and warns when the queue broker is not running.
- **A sign link can carry the option you already tapped on your phone.** A link with `&v=<version>&choice=<key>`
  opens the phone page on that card with one "Sign (b) with Face ID" button and the exact line it signs, and the
  card's first lines name your pick; Face ID waits for your tap, and every other option still signs on its own tap.
  The pick holds only for the version of the item you tapped: if the item changed since, or the option is not on the
  card, the page says so at the top and shows no button. A link to a request that is no longer open says where it
  stands (already signed, denied or expired) on the first refresh, from the new `GET /sign/<id>/state`.
- **A choice sign request's option may now be up to 1000 characters** (was 200); a title, a requester and a write-in
  stay at 200. An older Levain refuses the longer options, so a filer sends them only to this version or later.

### Fixed
- **`levain doctor`'s hands check runs sudo by its absolute path, like every other place Levain runs sudo.** The
  path now has one home (`/usr/bin/sudo`), and when sudo is not there the check says so instead of reporting a
  workspace problem.
- **A Claude Code or Codex file edit inside the Levain home now asks for consent.** An edit to Levain's own state (for
  example the autonomic binding store) is agent config, matching what the confinement floor already denies. With
  `LEVAIN_HOME` set to a directory Levain cannot protect whole (`/tmp`, a top-level directory, or one that holds your
  home or project), only Levain's own state directories there count; a `LEVAIN_HOME` that cannot be resolved keeps the
  default Levain home protected instead of denying every call. Shell commands are still not parsed.

### Groundwork (not reachable from a `levain` command yet, except where an entry says so)
- **The authority log accepts a signed graduation policy and the graduation opt-in (`levain-authority/2`).** A policy
  expires only by renewal, and `levain consent grant revoke` and the phone's revoke remove a policy or the opt-in;
  before, both refused one with "no consent grant", though the authority log accepts the revoke.
- **The gateway control plane accepts signed verifier configuration (`CONFIG-SUBMIT`).** A verifier run's ticket is
  checked against the active verifier configuration when it is minted and again when its session starts, so a verifier
  whose entry was withdrawn or replaced no longer runs, and a session refuses to render when the installed driver
  template is not the one its signed entry names (`driver-template-digest-mismatch`). A verifier item must fit one
  message to the control plane, and `levain dispatch config` refuses a larger one up front. A session whose render
  fails part-way, including when the reply to its first state change was lost, is revoked over a fresh connection
  instead of staying half-rendered with its slot held.
- **The coverage check for signed graduation policies exists.** It decides whether a policy you signed covers an
  autonomic action (same action and risk, a trigger and family it names, unexpired, signed by keys root lists, for this
  store only); anything not covered runs at confirm. The autonomic store gains its `/2` format and the migration to it,
  and a store opened at `/1` refuses to read or write once another process has migrated it. Nothing calls the check
  and nothing migrates a store yet.
- **The queue broker enforces slot limits.** A slot session's calls are counted against `L/policy/limits` (defaults
  when there is no file). A session that reaches a limit stays stopped until that limit is raised or a new session
  starts; a different call or a pause does not free it. An unreadable or tampered file, a failing limits database, or
  a session that names a different agent than it first counted under stops the call, never falling back to looser
  limits. No installed service runs the queue broker for slot sessions yet.
- **Dispatch runs take their slot from a pool the isolation controller keeps in step with the machine's real slots.** A
  run ticket is refused (`no-free-slot`) when no free slot is left for it, one egress slot always stays yours, and `POOL`
  reports how many are free. A finished dispatch run gives its ticket back on its own once its slot is cleaned, even when
  the slot was reused before Levain looked. No `levain` command runs the controller yet.
- **A dispatch run's settings are checked against its run ticket before anything starts.** A run asking for other
  routes, another model, other verifier steps or another driver than its ticket names is refused, and so is a run whose
  ticket details never reached the machine.
- **A chat launch's network write has a card that lists what will be sent**: the method, the host and port, the names
  Levain removes, the key Levain adds, why it asked, the body's size and digest, every header and query name, and the
  path, last, so a long path cannot hide the rest. Values never appear; hidden or direction-changing characters, and
  text that looks like one of Levain's own markers or cut notes, show as visible markers. The card is decided only by
  your signed answer, never by a standing consent grant, and it shows what the request became, read from Levain's record
  of it: waiting, allowed, in flight, sent, denied, expired, ended, ambiguous or failed. No chat launch files one yet.
- **The chat egress decider answers the sandbox's network requests.** Each request is decided on its own thread: a body
  that never arrives is denied (408), a client that leaves mid-upload is recorded as ended, a write that fails partway
  cuts the connection so nothing misframed follows, and closing a launch never waits on a verdict stuck behind a
  sandbox that stopped reading. An audit id is used once per launch, and one launch keeps at most 131,072 of them
  (past that, new requests are refused with `audit_cap`); a request to a literal IPv6 address resolves; a consent card
  is withdrawn when its request is denied for any other reason. No chat launch starts the decider yet.

## [0.11.24] — 2026-10-25

### Added
- **A provider can report a task's spend as tokens from transcripts.** The topology contract accepts `cost.source: "transcript"`
  (tokens only, never a price) and the Work lens draws it as "N tokens from transcripts". Levain's own tasks still report no cost:
  none of them names the session that works it.
- **Per-device cockpit pairing (the server half).** A device that already holds a cockpit credential mints a one-time
  code (`POST /cockpit/pair/offer`, 128-bit, ten minutes, single use); a new device trades it for its OWN token at
  `POST /cockpit/pair/claim`, the one route that needs no credential. Each paired device is revocable alone
  (`POST /cockpit/pair/revoke`), and a revoke takes the devices it paired with it, voids their open codes, cancels
  their waiting requests and ends their open change streams. A paired token opens what the shared write token opens
  and nothing more: every passkey-signed act still needs the passkey. Only token hashes are stored, in `cockpit.db`.
  `levain cockpit pair` mints a code from the box itself (it refuses to print one into a pipe unless given
  `--print`), and `levain cockpit pair --revoke-all` ends every paired device.
- **The pairing page and the Devices panel (pairing P2).** The cockpit's Devices panel lists the paired devices, mints a
  pairing link and revokes a device alone. A new device opens the offering device's `/pair#code=...` link (it also opens
  from another site as a top-level navigation) and trades the code for its own key; "Pair it again" asks before it
  replaces a key this browser already holds. No QR code is drawn: the link is the offer.
- **`levain chat key setup`, `revoke` and `list` manage the keys Levain would hold for a chat launch.** A setup record
  names one entity, one host (a DNS name; no wildcard, IP literal or port) and the macOS keychain item (`--source` is its
  service, `--account` its account) whose value requests to that host on port 443 would carry, as a bearer token or, with
  `--basic-user`, as HTTP Basic. The value never passes through the command. Setup refuses unless exactly one keychain item
  matches, shows the record and asks `y/N` before signing it with an enrolled laptop-presence key (`--signing-key`; with no
  terminal it signs only with `--yes`), and needs `--expires` (a duration or a UTC time, at most 30 days). On a git fetch
  the key rides only a repo the record declares: each `--repo` plus, with `--workspace`, that repo's https remotes on the
  host, read once at setup and shown to you with any remote it left out. One live record per entity and host; `--replace`
  supersedes it. `revoke` takes the record id that `list` prints and accepts any enrolled key. Nothing yet reads these
  records: no chat launch attaches a key (see Groundwork).
- **`levain doctor` shows the agent-session isolation install.** An `agent sessions` row reads `not installed` (neutral)
  when the isolation store (`/private/var/levain` on macOS, `/var/levain` on Linux) is absent, and fails on a symbolic link
  where the store, its `slots` or its `policy` directory should be, or on a missing `slots` directory. An `agent slots` row
  counts the usable slots against the pool configuration ("N usable of M required", naming a short class, slots that are not
  usable, retired slots it did not count and entries it ignored), and fails when the pool configuration is missing or
  unreadable. An `agent store free space` row fails under 8 GiB, where new sessions are refused and running ones braked.
  Doctor only reads what your account can read; it leases and runs nothing.

### Changed
- **Claude Code 2.1.310 is a tested version for the consent hook.** Its hook canary passed (the hook fired on the
  sentinel write and the write was denied), so installing the hook for 2.1.310 no longer needs the untested-version
  override.
- **The seed memory files no longer count layers.** `memory.md` and `partnership.md` now say the agent's memory is never a log
  plus a memory file: a governed, versioned state machine (an append-only episode log, consolidated by one writer into
  continuity, an immune system that decides what may graduate, and a pattern tier recalled on cue).
- **Levain now requires anneal-memory 1.0.2.** anneal 1.0.2 forms no association links and ignores the affect
  input; the seed's wrap steps no longer say `prepare_wrap` returns association context, so a fresh install
  reads its 1.0.2 migration proposal as already reconciled instead of pending.


### Fixed
- **The phone server no longer writes its token into its log.** With `--link-file`, the token goes only into that
  0600 file, and the line naming the file is printed only after the file holds the link. Without `--link-file` the
  link is printed as before, for a person at a terminal.
- **A broker that is not running answers a paired device with 503, never a refusal that drops its token.** While the
  broker is starting, owned by another process or disabled, a request carrying a paired token gets `503
  broker_unavailable` with `Retry-After: 2`.

### Groundwork (not reachable from a `levain` command yet, except where an entry says so)
- **Per-run tickets exist in the isolation controller.** The controller can mint a ticket for a dispatch run, cancel one
  that is not yet bound to a session, and report an attempt's state. Minting the same attempt again with the same request
  returns the same ticket; with a different request it is refused. Every ticket is kept in one append-only journal that the
  controller rebuilds its counts from at start, and a journal it cannot read whole stops it from starting. A failed write
  stops ticket requests until the controller restarts. A ceiling file can cap the money reserved per run, the open tickets
  and the runs per day. A session token resolves with the facts of the ticket it is bound to. No `levain` command runs the
  controller yet.
- **Ticketed calls leave a lineage record.** The keys service writes one record for each upstream exchange of a ticketed
  session and sends them to the controller, which keeps them in an append-only file, checked and de-duplicated, with a
  record that fails its check set aside and reported back to keys. A full outbox makes keys refuse further ticketed calls.
  No `levain` command runs either service yet.
- **The parts of the chat launch's network filter exist; the filter itself does not run yet.** Built: the request
  parser, the per-request verdict (let it through, hold it for a card, or refuse it), the rule that carries a held key
  only to a declared repo on its host, the removal of credential headers, a request path recorded with its query values
  left out, the per-request attempt and outcome records, and the Node glue for the sandbox runtime. Not built: the
  process that answers the sandbox runtime's questions with those verdicts, the card, and the chat launch that would
  start them. `levain chat key` above is the one part you can run, and it only writes signed setup records.

## [0.11.23] — 2026-10-24

### Added
- **The cockpit's grant "revoke" button signs with your passkey.** On a cockpit served with a `signer=` over the same
  consent queue its grant cards come from, the button
  runs the phone page's own revoke ceremony: one passkey prompt, and the grant is revoked in the authority log. The
  server builds the link and the page checks the challenge is over that link before the prompt. The new routes,
  `GET /cockpit/grant/list.json` and `POST /cockpit/grant/<id>/revoke-challenge` and `.../revoke`, ride the sign
  routes' gate. Everywhere else the button still copies `levain consent grant revoke`, and the copy line stays under
  the signing button.
- **A grant card says why a passkey cannot revoke it.** When the page you are on cannot ask your passkey for a revoke
  (a passkey made on `localhost`, or a page address the passkey would refuse), the grant list names the reason and
  points to `levain consent grant revoke` instead of offering a button that fails. The phone page's Grants tab and the
  cockpit's grant card both do this.
- **The agents graph marks who is waiting on you.** The desk and any seat with an open decision carry a red mark and a
  "!N" count ("9+" past nine). The desk's mark is an engraved bezel that lights once when a decision arrives, with the
  count on a short callout beside it; a seat's mark is a red ring. A provider sets `needs_open` on the hub or a node,
  and may add `needs_first_card` (the id of the first decision's card): then the count is a button that opens that
  card, and tapping the node itself still opens or messages the seat. Every animation stops by itself (the bezel
  lights once, a seat's ring flashes a few times), and none runs under reduced motion.

### Changed
- **A passkey revoke signed on a port is accepted.** The authority log now takes a passkey's `revoke_grant` signed on
  `https://<the passkey's own name>:<any port>`, as well as on `https://<name>`. So a cockpit served on a port, such
  as `https://<name>:8443`, signs a grant revoke with the passkey instead of copying the command. Only the passkey's
  own name is accepted, and only on a revoke. Every other passkey signature in the log must still come from exactly
  `https://<name>`.
- **A compose card pre-selects only the readers its provider declares.** A provider that relied on 0.11.21's built-in
  default reader, seat tap or departed-seat recovery must declare the keys below. Without them, the card starts with
  nothing pressed, so a message goes to everyone. Tapping a seat on the agents graph presses that seat's chip only
  when the card declares `transient_prefix` and the seat is among its readers; otherwise it opens the agent page, as
  before. A departed reader's refusal is shown as the provider sent it. The departed-reader notice now reads "that
  reader is gone, choose again".


### Fixed
- **The cockpit message card's default reader and its departed-reader recovery are declared by the provider, not built
  into the page.** 0.11.21 pre-selected one named agent and recognised one refusal wording in the page's own script,
  and its entry described that agent as Levain's own. A compose `recipient` field now takes three optional keys,
  validated beside `enum` and refused on any other field type:
  - `default`: `enum` ids pressed at the start, spelled exactly as in `enum`. Empty or absent means nothing is pressed.
  - `transient_prefix`: the start of every id that can leave the roster between draw and send. The agents graph
    presses the chip whose id is `transient_prefix` followed by the seat's roster name.
  - `gone_match`: a case-insensitive substring of the refusal that says a chosen id has left. It is matched against
    the error code and the message together, as the page receives them. A stable error code is the safest thing to
    match.
  The departed-reader recovery needs both `transient_prefix` and `gone_match`, so a `gone_match` without a prefix is
  refused at registration. The graph tap needs only `transient_prefix`.

### Known edges
- A grant revoke is accepted from the passkey's name on any port, so another program on that machine serving the same
  name on another port (a second `tailscale serve` port) could ask the passkey for a revoke. You would see the Face ID
  prompt, and a revoke only removes a grant.
- One revoke waits per grant on each page server: a second challenge for the same grant (another tab) replaces the
  first, whose passkey answer is then refused, and you ask again.
- A metered call is priced by the request's `model` alone. A fallback list (`models`) or a `route` in the body is not
  read, and neither is the model the provider's response says ran. No route in this release reaches a vendor that
  honours them; the vendor routes that do will refuse both and settle from the model that ran.

## [0.11.22] — 2026-10-24

### Added
- **The cockpit can sign answers and messages with your passkey.** An embedder that files levain sign requests (the
  phone page's ceremony) can now have the cockpit page run the same ceremony. `make_server` takes `signer=` (a
  `levain.consent.sign.Signer` pinned to the page it is served on with `page_origin`), `extra_hosts=` (the bare https
  name a reverse proxy such as `tailscale serve` forwards to it) and `compose_signer=`. With a signer the cockpit serves
  `GET /cockpit/sign.json` and `POST /cockpit/sign/<id>/{challenge,answer,deny}`, each behind the write token, the
  Host allowlist and same-origin rules a write has. A question or note whose caller sets `sign_request` shows that
  request's own options as buttons; one tap asks your passkey and records a signature that `ssh-keygen -Y verify`
  accepts, the first answer winning when the phone and the cockpit answer one request. A cancelled passkey signs
  nothing and keeps what you typed. On any page other than the pinned one the card says where to sign and offers no
  other answer. A verb declared with `ActionVerb(sign=True)` is sent from its compose box only after your passkey signs
  its params: the page asks `POST /cockpit/sign/compose` (the `compose_signer` files the request; its `ValueError` is
  shown to you), runs the ceremony, then posts the verb with `sign_request`, which the verb's own effect must verify.
  Each draft carries one id (`client_txn`) for its whole life: the compose route names the same request for it while
  that request is open, and the send is keyed by it, so a lost reply, a retry or a reload files one request and sends
  once, at any tier: a signed verb is deduped by that key whatever its `idempotent` says. The kernel refuses a signed
  verb on every route, a dry run included, before its handler runs, unless its `sign_request` names a request a
  compose route filed for that verb and exactly those params and the Signer holds signed, and its `idempotency_key` is
  `sign-` plus that draft's id (a signed request is sent under one key only); and `ActionVerb(sign=True, job=True)` is
  refused. Which request each draft filed is kept in the Signer's sign directory (`sign/.compose.json`), so a restart
  or any number of other drafts never forgets it: a send that landed and lost its reply is answered with its first
  reply when it is sent again within the at-most-once window (24 hours), and runs once. A record goes when its request is denied, removed or expires unsigned,
  and at the latest 48 hours after its compose; a record file grown past what a read takes is pruned by the next
  compose once its requests have left. A `make_server` that fails part way through attaching to the caller's
  cockpit undoes its own attaches, keeping any panel registered meanwhile, and releases its pin on the Signer. `GET /cockpit/sign/<id>.json` reads one request whatever the listing's
  cap. Without a `compose_signer` a signed verb is not sent at all. A request through an `extra_hosts` name is treated as
  off the box: the cockpit's reads need the token there too. `levain.consent.sign.passkey_key_source` is the passkey
  key source the phone page uses, for an embedder building the Signer.
- **A passkey verifier can be pinned to an https page with a port.** `levain.consent.passkey` accepts an explicit
  `page_origin` of `https://<name>:<port>` for a name other than localhost; without one, the portless rule is
  unchanged for every existing verifier.

### Known edges
- A signed send refused outright for a reason other than its signature (a confirm, a stale row) drops its signed draft,
  so sending it again asks the passkey once more.
- Each tab gives a signed draft its own id, so the same words sent from two tabs file two requests.
- Once 1024 records are live on a queue (`COMPOSE_LIVE_MAX`), a new draft's compose is refused 429 `too_many_open`
  ("finish or deny open requests first") and files nothing, while a draft already recorded is still answered. Composes
  of new drafts running at once can each pass the check, so the count can pass 1024 by the composes in flight.
- Each compose prunes the queue's records before it files, and other drafts' composes wait for it; with many live
  records that wait grows.
- A proxy is recognised by the `extra_hosts` names and forwarding headers; a proxy that sends neither and names
  `127.0.0.1` reads as local.

## [0.11.21] — 2026-10-24

### Added
- **`levain session adopt SESSION --repo PATH` takes a finished session's work into your repo, after showing you what it
  is.** `levain session discard SESSION` and `levain session restore SESSION` move a retained session to trash and back.
  The `session` command stays out of `levain --help`, and a session still needs the root helpers, which have no public
  install path yet, so this reaches you through a build that has them. Levain checks the repo first: it must be a git
  root (not a subdirectory, linked worktree or submodule), use the same object format as the session's, not be shallow
  or partial, and carry the session's recorded base commit. A session that recorded no base is refused
  `base-unrecorded`. Levain then opens the session's export in a confined child and prints a preview: the new commits
  with author, committer and message, the changed paths from the base, config-surface files, symlinks and their
  targets, and names that collide by case or Unicode form. Everything printed is escaped. Then it asks what to take:
  `head`, `wip` (only the refs the export carries) or `none`. `--take head|wip|none` answers for a script; with no
  terminal and no `--take` it refuses `bad-argument` before doing anything. The take copies objects into your repo and
  moves the ref, never runs your repo's hooks, and never looks for a parent repo. After the preview Levain prints the
  private copy's directory and the one command that removes it. An adopt that fails before the preview removes the
  private copy; after it, Levain removes the bundle and index and leaves the directory for you to remove. `discard` and `restore`
  answer `session-busy` while the session is in use, `not-retained:trash` for a session already in trash and
  `not-trash:retained` for one not in trash; a discard or restore that stopped part-way is refused
  `discard-incomplete:<phase>` (`restore-incomplete:<phase>` for a restore) and finishes when you run it again. This
  replaces the 0.11.20 Groundwork line saying nothing calls the adopt stages. Tested on macOS.
- **Message an agent from the agents graph in the cockpit.** Tapping an agent's node presses its recipient chip in the
  message card, scrolls to the card and focuses the message box. `fanin`, the coordinating agent, is pre-selected, and
  un-pressing every chip sends to everyone. At phone width (720 px and narrower) the chips give way to a picker with the
  same choices. The pick survives the card's refresh and clears when a send lands; a send refused because the agent has
  gone says the agent is gone, asks you to choose again and redraws the chips. Known edges: an agent waiting on a card keeps its
  card-opening tap, so it is not messageable by tap; and an empty inbox card has no box to focus.
- **A metered route's holds survive a restart of the credential-holding process.** Each hold is written to the hold
  journal (`keys/ledger.jsonl`) and flushed to disk before the call it covers is sent. On the next start, every hold
  with no recorded outcome is booked as uncertain at its full amount, so a restart cannot make a session's cap look
  emptier than it is. If the journal cannot be opened, the credential-holding process refuses to start (exit 3). If the
  journal's writer fails or stalls while running, it admits and sends nothing from then on, and every unresolved hold
  stays charged at its full amount.

- **Metered routes hold each call's estimated cost against the route's per-session cap.** On a metered base route
  (a `levain-routes-base/2` map with a price table) the credential-holding process buffers the request body, checks
  the model against the route's price table, writes the output ceiling into the body and holds the call's estimated
  cost against the route's per-session cap before the request goes upstream; a call whose estimate does not fit the
  room left under the cap is refused `budget-exhausted` (402) and is never shrunk to fit. The body is forwarded as
  Levain's own re-serialization of the parsed JSON, not as the agent's bytes. A body that does not arrive within
  `BODY_READ_S` (in `levain/broker/keys.py`) is refused `body-timeout` (408). A websocket upgrade on a metered route
  is refused `body-unbounded:websocket` (403), because one exchange would carry many responses and no one usage would
  settle it. `levain doctor` prints the same words as a note after its checks (no badge, no effect on its exit code).

  Levain enforces an authorized-spend cap: a route's per-session cap on what Levain lets an agent spend. It is a budget, not a ceiling on your bill. Calls already running when the cap is reached can finish past it. To put a hard ceiling on what a provider can bill, set a spending limit on the API key itself.

  Sizing: a route's per-session cap should be at least the concurrent calls × their output ceiling × the model's output rate, or calls are refused 402 before they start.
- **Metered calls are charged what the provider reports, and a call that costs more than its hold stops its session.**
  When a call on a metered route completes, Levain reads the usage the provider reports in the response (input,
  output, cache writes, cache reads and per-call tools such as web search) and charges it at the route's price table.
  A call that reports no usage, or whose usage could not be read, is charged its whole hold. When a call costs more
  than was held for it, it is charged in full and its session is stopped: the session's later calls are refused
  `budget-overrun` (402), while calls already running finish and are charged from their own usage. The stop lasts
  until the session ends, or until the credential-holding process restarts: the stop is kept in the process's memory,
  not in the hold journal above, so holds persist across a restart and the stop does not. A streamed openai-chat
  request that does not say whether it wants usage is sent asking for it, so the provider adds one final chunk
  carrying the usage.

### Changed
- **Health, the graph panel and `levain wrap` stop treating Hebbian links as a sign of life.** Levain's anneal-memory
  requirement is unchanged (`>=1.0.0,<2`); everything below holds with anneal-memory 1.0.1 installed except where it
  says otherwise.
  - Health is now `live` (the store reads and at least one wrap committed) or `no wraps yet`. The date of the last wrap is
    shown as a fact. A fresh store no longer reads "DARK · 0 links". `write_path_live` stays in the health data for one
    more release (true when the status is live) so an older reader does not break.
  - The link count, strength and density figures leave the health panel. The graph panel keeps drawing the links already
    there and says `not formed` when the graph is empty. When anneal-memory 1.0.2 or later is installed and reports that
    it has stopped forming links, it also says `no new links since wrap N`; with 1.0.1 it says nothing extra.
  - Wrap rows show `+N links` only for older wraps that formed some.
  - `levain wrap` no longer prints an `associations:` line (it would read 0 on every wrap) and no longer sends an
    affective state. It prints `wrap: #N` when the installed anneal names the wrap it committed (anneal-memory 1.0.2 or
    later). `--affect-tag` and `--affect-intensity` still parse, do nothing, and print one line saying they are
    deprecated.
  - The seed memory files teach two layers (episodes and continuity) with the immune system, and drop the Hebbian and
    limbic sections. Run `levain update` to see the proposed changes to your copy.
  - Known edge: once anneal-memory 1.0.2 or later is in use, a process still on 1.0.1 can open a store that a newer
    process has touched and keeps forming links on it. Keep every install that shares a store (this one, other tools, the
    uv tool, hub venvs) on the same anneal-memory version.
- **The wheel ships the Unicode license** (`LICENSES/Unicode-3.0.txt`) with the confusables table the adopt preview uses.

### Removed
- **The keys service no longer relays websockets.** The relay added in 0.11.20 (see its Groundwork entry) is deleted:
  every brokered route the keys service serves is now metered, and a metered route refuses an upgrade
  (`body-unbounded:websocket`), so nothing could reach it. The front's own websocket path is unchanged.


### Fixed
- **The cockpit no longer breaks at half-screen widths.** At 864 to 1040 px the jar and live dot sat over the tabs; the
  weather card's foot stacked three items in one place; a strip segment shrank to 2 px; tile status rows and big-figure
  labels ran past the tile edge; and row verbs spilled over the meta line in a 341 to 520 px column. A status line that
  is cut now has the full text as its tooltip.
- **Drop from the phone survives a store change and a fast laptop clock.** A refusal because the phone's clock is ahead
  of the laptop's now retries with a fresh send time, and the refusal names the gap in minutes. A drop listing that
  arrives late no longer overwrites the store the page already adopted. A signed drop for another store on an item bound
  to a store is refused `other_store`. The envelope review now carries the page's store id, and the signing challenge
  binds to the store the review kept: a store replaced or gone between the review and Sign is refused `other_store`,
  and the challenge never creates a store.
- **The gateway log's `bytes_up` counts only the body bytes handed upstream.**
- **The keys service arms its shutdown triggers before it first listens**, so a SIGINT arriving the moment its socket
  appears takes the graceful path instead of ending the process with a signal exit.

### Groundwork (not reachable from a `levain` command yet, except where an entry says so)
- **The install helper's `repair --source PATH` is built.** It upgrades an existing install to a new generation, and it
  replaces 0.11.20's "not built yet". It checks that the `--source` package is writable by the operator alone and that
  the accounts its plan renders still exist (`account-missing`), stages and hashes a copy of the new code
  (`source-changed` if the source moved), places it, probes it, then execs into it so the new code finishes the work
  holding both install locks. A run that stopped after placing a generation resumes into it; if the staged copy no
  longer hashes the same it is removed and the run refuses `source-required:<id>`. No `levain` command runs it.
- **The keys service attaches to the isolation controller.** It holds one long-lived attachment and refuses every token
  while it is not attached. Losing the attachment fails closed on both sides: keys drops every front, and the
  controller revokes every egress session. A front that disappears, or never claims its registration within 10
  seconds of being armed, gets its session revoked. Keys reconciles its cached tokens with the controller once a
  minute, and a reconcile that goes unanswered three times in a row ends the attachment. A session stopped for
  spending past its hold is recorded by the controller for display and is not revoked. A failed read loop or reconcile
  ends the attachment instead of leaving it half-alive; a short-lived attachment backs off before the next try; and a
  spend-overrun notice is resent until acknowledged, or ends the attachment. The controller's service has a single
  entry point, `levain.slot.control.serve`. Nothing starts either process from a `levain` command yet.
- **Dispatch route maps.** The controller accepts a signed dispatch route map (`ROUTE-SUBMIT`), verifies it against the
  routes trust anchor, activates it as a numbered generation behind a durable head, and tells the keys service to
  reload it. Keys keeps its own durable head, refuses an older or rolled-back generation, and refuses every dispatch
  route until a valid reload. An equal generation and digest resubmitted converges instead of failing. A route id
  that is both a base route and a dispatch route is refused `keys-unavailable` (`route-collision`) when used, and so is
  any route while the dispatch ids cannot be read. A map is refused `routemap-signature`, `routemap-store`,
  `routemap-generation`, `routemap-invalid:<part>` or `routemap-verifier`. No session carries a dispatch pin yet, so
  every dispatch route is still refused.
- **The keys service's `--unmetered` flag is removed.** It was an internal flag on a process you do not start.

## [0.11.20] — 2026-10-23

This release carries the changes prepared as 0.11.17, 0.11.18 and 0.11.19, which were never published, and everything merged since.

### Security
- **A phone passkey enrolled at a custody other than `passkey` skipped the passkey checks (0.11.1 to 0.11.16; upgrade to fix).**
  Levain chose a key's checks by its stored custody, so such a passkey's signatures were taken without Face ID or the PIN,
  from any page origin, and, enrolled as a `laptop-presence` key at custody `fido`, at the laptop's rank. Levain now judges a key
  by its key material and refuses that enrolment. A key an older release enrolled this way is quarantined: the log still loads,
  the key signs nothing new, and `levain doctor` names the revoke and re-enrol commands.

### Added
- **Notes: an agent can send you a reply that needs no answer.** Through the band's tools an agent files a note: one
  line, with optional detail. It shows in the cockpit's Notes strip under Needs you and on the phone page below your
  questions, and you dismiss it with one tap. A note has no options and no recommendation, and dismissing it answers
  nothing, approves nothing and starts no agent. An agent that keeps filing is refused once too many notes are open
  (100 in total, and 25 per agent session when the session is known). The cockpit and phone show the oldest notes
  first and say how many they are not showing.

- **Standing grants can be revoked from the phone, and the cockpit page gives you the revoke line.** The phone page has a
  Grants tab. Each card shows the grant's tools, state, agent, workspace and expiry, what it answered, and its newest
  calls with refused ones first; a grant with a refused call leads the list. "revoke with passkey" shows the exact line
  that will be signed, and Face ID is asked only when you tap the sheet's own button. It reports the outcome in the
  server's words, and says "result unknown" when the answer never arrived after the signature was sent. The cockpit page
  has a matching card per grant beside the rule proposals, with a "revoke" button that copies the exact
  `levain consent grant revoke` line to run in a terminal: that page signs nothing.
- **`levain cockpit anchor reset --signing-key KEY` moves the authority log's anchor after a restore on purpose.** The
  anchor refuses a log that was rolled back; the refusal used to print this command, which did not exist. `KEY` is a
  `laptop-presence` key the log holds unrevoked (a private key file, or the `.pub` of a key an agent holds) and it may
  ask for a touch. The command signs the old anchor and the new head and keeps the signature in the anchor file. It
  refuses while a cockpit server holds the store, refuses an anchor that names another store (the refusal names the file
  to check), and leaves an anchor that already agrees with the log alone. Every key or grant that the rolled-back links
  held is gone from the log.
- **Drop to the coordinator from your phone.** The phone page has a Drop tab: type your words and send them, as
  `levain drop` does. The drop goes into an outbox on the phone first and is resent unchanged until the server answers,
  so a lost reply files it once; a refused drop stays marked, with "Send again" and "Discard". Under "with an envelope"
  you set where it may work, a branch to land onto, a stop time and a budget, review the server's own words for it, and
  sign it with your passkey. A land made from the phone is marked "inferred, not where you dropped from" in what you
  sign and in what `levain envelope` shows. A plain drop made from one Levain is never sent to another (a signed one has the known edge below): the page shows "from
  another Levain" with Discard only. A signed drop is never stored unsigned: a stale or widened one is refused, and the
  card offers "Sign again" or "Send as a plain drop". Only a paired device can use these routes; the run token cannot.
  An accepted drop wakes the coordinator, and the receipt says whether it started.
  - Known edges: a browser without Web Locks edits the outbox unlocked, so two tabs can overwrite each other's change;
    the page says "one tab at a time" there. If the laptop replaces its coordinator store after the page last read it
    and before you ask to sign an envelope, the signed drop goes to the new store instead of being refused.
- **The coordinator reads your agents' questions first.** `levain coord questions` lists the questions you have not
  read, each marked MAY ANSWER (asked by a worker Levain started under a live envelope, from inside a repo it names,
  with no option tied to a consent request) or ESCALATE. `levain coord answer N --option KEY --token N` (or `--text
  WORDS`) files the coordinator's answer as a KNOW in your review queue. The asking session gets it labelled as the
  coordinator's own reading, and the question stays open: your answer is the one that counts. `levain question answer`
  refuses a session Levain started as the coordinator. `levain coord escalate N --why WHY --token N` leaves a question
  to you. While a coordinator holds a live lease, `levain ask` holds its ping to you until the coordinator escalates the
  question or has not read it in 10 minutes.
  - Known edges: "asked from inside the envelope" is a label, not proof, because a question's directory and session are
    the asking process's own word. A KNOW reaches a live session at its next tool call only. An answer or revocation
    that lands between the coordinator's last re-read and its commit still files a KNOW; it grants nothing and waits in
    your queue.
- **The coordinator carries a morning card, an away switch and thread budgets.** `levain coord morning` prints what
  waits for you after a night away: whether away is on, what waits UNDECIDED, each thread's budget, what waits on you,
  and the open review items. `levain coord away DROP --token N` reads a drop as your word that you are away and turns
  away ON for both agents, so every SIGN waits for the card; only you can turn it off. `levain coord budget [THREAD]`
  shows each thread's budget against what its worker sessions used (tool calls, tokens and hours; dollars are not
  metered). Once a budget is crossed no new worker starts for that thread. A worker that holds several threads is
  refused, because no one budget could meter it. `levain envelope extend THREAD --budget K=N,...` writes the body that
  raises a thread's budget; keys you leave out keep the current value, a named value may not be below it, and at least
  one must be higher. You sign it with `levain envelope sign`; nothing else extends a budget.
- **A team's entries need a touch.** A new team refuses file keys: an entry counts only if its commit is signed by a
  FIDO key (`ssh-keygen -t ecdsa-sk` or `-t ed25519-sk`, a security key or Touch ID) with the user-presence flag set,
  which Levain checks itself. `levain team record` refuses a file key or an ssh-agent key before it writes anything.
  An entry that does not count is not in force, `levain team verify` reports it, and `levain team sync` holds it back
  with the entries that depend on it and publishes the rest; run in a terminal, `sync` names each held entry and asks
  one touch for it. The owner can allow file keys at the start (`levain team init --allow-file-keys`) or later with
  `levain team file-keys allow`, which needs a touch while the team refuses them; `levain team file-keys refuse` takes it
  back. A team made before this rule keeps working as it did, and `levain team record`, `status` and `doctor` say it has
  not chosen until its owner runs `levain team file-keys refuse` (recommended) or `allow`. An entry counted without
  presence is shown as `file key, no presence`.

### Changed
- **`levain doctor` and `levain cockpit keys` name a quarantined key.** `doctor` fails once per quarantined key, with its
  fingerprint, why, and the commands to run with this cockpit's `--data-root` filled in: for a phone key, revoke it and
  enrol a fresh passkey at custody `passkey`; for another class, enrol a replacement at its real custody first, then
  revoke. A revoked key stays revoked, so the same key cannot be enrolled again. If every `laptop-presence` key is
  quarantined, `doctor` fails and says no in-product recovery exists in this build. `levain cockpit keys` marks the row
  `QUARANTINED`. A quarantined key is not counted as a `laptop-presence` key that can sign.
- **`levain envelope echo --budget` must name every key the budget gauge measures** (`tool_calls`, `tokens`, `hours`). A
  key left out used to read as 0, and 0 stops the first worker. Name a key with `K=0` to mean none; `dollars_cents` may
  still be left out.

### Groundwork (not reachable from a `levain` command yet, except where an entry says so)
- **The install helper can set and remove a credential.** `set-credential <name>` takes the credential on stdin and
  checks it before the install lock is taken or anything is written; `remove-credential <name>` removes it. Each write
  runs in a child that has dropped to the keys service's own account (`_levain_keys`) before it touches the
  credential directory. The write is atomic, gets a fresh revision, and waits at most 10 seconds for the credential
  lock. If the file is in place but its directory entry cannot be synced, the write succeeds with a warning that a
  crash may undo it (a removal warns that the credential may come back). The command gives up after 30 seconds,
  stops the child and refuses `credential-child-timeout`. The keys service uses a new credential on its next exchange,
  and the token refresh below writes through its own one-thread writer.
- **The keys service refreshes an expiring sign-in token.** Before an exchange goes upstream, an access token that
  expires within 60 seconds waits for its refresh. An expired one is refused as `credential-unavailable` and is never
  sent. A 401 from the vendor starts a refresh. The request that got the 401 is answered `credential-unavailable`: the
  401 is not relayed and the request is never replayed. Concurrent requests for
  one credential share one refresh. A refresh that fails backs off from 30 seconds, doubling, to at most 15 minutes. The
  refresh writes through the credential writer above, and a credential the operator replaced during a refresh is kept.
- **The installed tree moves to versioned generations.** Setup installs levain's root-owned code as one content-addressed
  generation behind a single `current` link. It records the install's shape so a later repair can plan the upgrade of
  an existing install. The `repair` verb that will use this plan is not built yet.
- **The keys service carries a websocket.** A route that permits an upgrade now gets one. The keys service checks the
  client's handshake itself and asks the vendor to upgrade. It accepts the vendor's 101 only when it is HTTP/1.1 with the
  right accept value, no extensions, and a subprotocol the route can relay back. It then relays frames both ways under one
  idle clock. If a credential appears inside one frame's payload from the vendor, the frame is cut where the credential
  begins and the websocket is closed. This is a canary, not a wall: a credential split across fragmented frames is
  not caught. A masked, reserved-bit or reserved-opcode frame from the vendor, or one with an invalid length, also
  closes it. Nothing calls this path yet. The isolation layer that will route a client through it is still groundwork,
  and its design runs Codex with websockets off for 1.0.
- **The phone can file and sign cockpit writes, but no page offers it yet.** `POST /cockpit/verb` (the laptop's verb
  body plus the signing passkey's `fingerprint`), `GET /cockpit/pending/<id>` and `POST /cockpit/pending/<id>/signature`
  and `.../cancel` take a write for a short list of device verbs (spore edits). Every write is a pending that applies
  only once the passkey's signature verifies, through the same dispatch and broker as the laptop's. A paired device's
  credential is required; the phone page's signing hook is in place and nothing calls it. The phone's write waits as
  `standby` while the laptop's `levain serve --write` holds the write lock.
- **The coordinator's review queue exists.** It holds the three review modes, a KNOW filed with its reply in one
  transaction, a push record per SIGN or ANSWER, and feeds (no-split, dead seats, near-limit envelopes, end of work, the
  weekly digest). Only `levain coord morning` reads it, for what waits on you and the open items; nothing yet runs the feeds.
- **The install helper's `undo` is rebuilt on per-identity removal.** It removes each recorded item by its identity
  in a fixed order and retires no group id twice. It is not reachable from a `levain` subcommand.
- **Session adopt's later stages are built as library code.** The sealed export run (its image, run leaf, watchdog and
  one outcome classifier), the publish that exchanges the result tree in atomically, a root verification verb with a
  bounded `git fsck` output reader, and a tree reader. Nothing calls them from `levain adopt` yet.


### Fixed
- **The isolation install helper no longer refuses on a service that is still unloading.** This happened in `undo` right
  after `setup`, and in a re-run setup that stops the services of an earlier generation. After `launchctl bootout`, the
  install helper waits a bounded time for launchd to finish unloading the job before it checks the job is stopped.
  A job counts as absent only on launchd's "no such service" answer. Any other answer, or a `launchctl print`
  that hangs past its time limit, counts as "could not be checked", and the step refuses rather than carrying on with a root
  service possibly still loaded.
- **The remedies Levain prints for an uninitialised cockpit are complete commands.** The refusals that told you to run
  `levain cockpit ledger init`, the default-grant offer, and the "next:" line `levain cockpit key create` prints now name
  `--signing-key` and `--data-root` with the real paths, shell-quoted. Where the install has no cockpit journal yet, no
  complete command exists to print: `key create` says to start the cockpit (`levain serve --write --install-class
  reported`), open it once, and then run the init, and the default-grant offer names the init and its two flags,
  instead of printing a command that could only fail. The printed `levain consent` phone help now names
  `levain dispatch file --dispatch-dir`, which is the real flag.

## [0.11.16] — 2026-10-22


### Fixed
- **The cockpit's theme switch keeps every key the same size on a phone.** At phone width the selected light or dark
  key stood taller than the others, because the switch's track was shorter than its touch-sized keys. The track now
  holds them, and the selected key is marked by its colour and raised shading, not by its size. The desktop switch is
  unchanged.

### Groundwork for isolation (nothing here is reachable from a `levain` command yet)
- **The keys service gains its request path.** A brokered request is admitted, checked against the route's
  policy and the session's budget, and forwarded; a refused one is answered by name (`keys-unavailable`,
  `upstream-error`, `upstream-timeout`) instead of a closed connection, and a session's revoked token cannot come
  back from a resolve that was in flight when it was dropped. A route map with any entry that fails to parse refuses
  every route.
- **The keys service reaches the vendor.** It opens the vendor connection under one deadline that covers name
  resolution, the connect and TLS, and relays the vendor's status and the headers the route allows back to the
  session. A session that cancels its request before it is sent sends nothing to the vendor. An early 413, or a 401 on
  a static key, during an upload is passed back as soon as keys reads it; a 401 on a refreshing credential is answered
  `credential-unavailable`. A response with no body (HEAD, 204, 304) is held until it is complete, so a keys
  connection that ends early is answered `keys-unavailable` rather than looking finished. A malformed vendor response
  is logged as a fixed label, never its text, and a credential whose secret the redaction marker could contain or
  rebuild is refused when it is loaded.
- **Session adopt's importer takes in a session's objects** and checks them with `git fsck` before anything else
  happens.

## [0.11.15] — 2026-10-22

### Added
- **The cockpit's header is now a graph of your agents.** The header lens switches between **Agents graph**, **Event
  graph** and **Work**; the Agents graph is where a fresh browser opens, and your choice is kept per browser. The graph
  draws one hub, your agents grouped around it with a face and a state ring each, and moving streams for the ones that
  are working (none for an idle or unread one, and none if your system asks for reduced motion). A metrics strip counts
  sessions, events, running, waiting on you and tokens today (a figure with no data shows a dash, and its tooltip says
  why). Clicking a node opens that agent's page with its facts and the session transcript, newest entry first.
  - **Replay.** A strip under the graph scrubs back up to 14 hours: it redraws what was known at that minute, and what
    was not kept for the past shows as unknown, never as empty. Live updates pause while you look; Esc or Live returns.
  - **The Work lens** shows the live work in three columns (records, tasks, outcomes), loudest first: waiting, failed,
    stuck, running, open, done. **Lost if closed** is its own panel.
- **The cockpit page is rebuilt around four tabs.** Morning, Board, Build and Memory sit under a header with a
  light/dark switch that follows your system's setting. The Memory tab shows the neocortex as one card; Build
  has a large Sessions card and Results beside edits and loops; the Needs-you strip leads with open questions and
  clocked requests. Panels can be collapsed and reordered within a zone (saved in the browser; Reset layout beside the
  theme switch). A fresh install's empty panels say what fills them. The page also now reads your repos' recent commits,
  with a hostile `.git/config` neutralised, into the activity band.
- **Standing grants can be read and revoked from the cockpit and the phone (the routes; the buttons are not on the
  pages yet).** `GET /cockpit/consent/grants.json` (behind the write-token gate) lists every grant the consent hooks
  name, live, expired and inert, with what each answered and the exact line that revokes it. The phone page's
  `consent.json` lists the same grants, and `POST /grant/<id>/revoke-challenge` then `/grant/<id>/revoke` revoke one
  with a passkey. A grant that changed since the phone listed it is refused (`grant_changed`) before anything is signed.
- **`levain team draft`: the coordinator drafts ledger entries, you sign them.** `add`, `list`, `show`, `withdraw`
  and `promote`; only `promote` signs, under your key, after showing the whole draft. Two promotes of one draft sign one
  entry, and a ruling is signed only with the words you give (`--words` / `--words-file`), never the coordinator's.
- **`levain coord agent codex`: a Codex coordinator.** Drops can start a thread on your own Codex app-server daemon,
  named `levain-coordinator-<id>`, in a workspace Levain sets up (default `<levain home>/coordinator-codex`); the launch
  prints `codex resume --remote unix://<socket> <thread id>`. Levain never starts or stops the daemon: with none
  running, the launch waits and names `codex app-server daemon start`. `levain coord watch` ends a launch whose thread
  the daemon no longer has. Codex's consent hook must already be live (`levain init --adapter codex`), and you trust
  the workspace in Codex once.
- **`levain doctor` has a host-sleep row.** While a coordinator's work thread is working, Levain holds an idle-sleep
  assertion (`caffeinate -i`, or `systemd-inhibit --what=idle`) and the row says whether it is held. On macOS it says
  the hold does not stop the lid closing from sleeping the Mac. No power setting is written.
- **Dispatch files a job and its read set together.** `levain dispatch file` (and the MCP door) builds the contract's
  read set and exports its bundle before anything reaches the store, then publishes the job and the bundle together. A
  filing process that opened the store before a newer Levain migrated it is refused (`store-format`) instead of
  filing a job with no handoff. Nothing launches from the bundle yet. The job store moves to format 5 (see the upgrade
  note under Changed).
- **The consent hooks' version pin takes Claude Code 2.1.306 and 2.1.308.** `levain consent canary --agent claude_code`
  passed on 2.1.306 and on 2.1.308 (each time the hook fired, the agent reported the denial, the sentinel was not
  written). The pin is now Claude Code 2.1.300, 2.1.301, 2.1.306 and 2.1.308, and Codex 0.162.1.
- **Each consent request records the model the agent named.** Codex's hook payload names it; Claude Code's does not
  (2.1.306), so its requests read `unknown`. It is kept beside the request, never in what you sign. An older store
  gains it on its next open.
- **CI runs the cockpit's browser tests.** They had been skipped in CI, because it had no Chrome. A browser job now
  installs a pinned Chrome and fails on any skip.

### Changed
- **Upgrade note: the dispatch job store moves to format 5 the first time this version opens it, and Levain
  0.11.14 and earlier then refuse it. Do not downgrade after upgrading.** Your jobs are kept.
- **`levain team record`, `retire` and `draft` accept Windows line ends (CRLF) in prose fields.** They are read as
  ordinary line breaks. A lone carriage return is still refused.
- **The cockpit's dark theme lifts its panels and rules, and the Board's glance is separate cards again.** Light is
  unchanged.
- **The coordinator `levain coord agent` sets up runs `levain coord`, `levain drops` and `levain thread` without
  asking, and nothing else.** Those commands grant no authority (a signed envelope still does), and every other
  command asks you as it does in any install. Claude Code applies this only once you have trusted the directory. A
  live install you point it at with `--cwd` is used as is, with nothing pre-approved.
  - Those commands name the Python that set the install up (`<python> -P -m levain coord ...`), because a background
    session does not reliably have `levain` on its PATH.
  - Levain's own MCP servers (anneal_memory, levain_band, levain_dispatch) are approved in that install, so a
    coordinator starting in the background no longer waits on Claude Code's "new MCP servers found" dialog, which
    nobody can answer there. A server you add yourself still asks.


### Fixed
- **The phone page's top bar holds on a narrow phone.** On a narrow phone, or a wider font, the theme switch could
  wrap onto a row of its own. The bar now stays one row from 320 to 430 px wide, and a long white-label name is cut
  with an ellipsis, its full text in the tooltip.
- **The cockpit's phone-width tiles no longer clip.** The Ops tile's reading wraps onto the line under its name, and four
  tiles never leave a lone tile on a half-screen laptop.
- **The macOS persistence scan no longer refuses on a very deep tree.** The scan of the agent roots held absolute
  paths, so one entry deeper than the path limit refused the whole confinement floor. It now walks by directory
  descriptor. A re-read it cannot inspect refuses rather than reading as absent.

### Security
- **A team-ledger line could carry text a reader cannot see into every teammate's agent.** Levain now refuses hidden
  characters (TAG characters, bidi overrides, zero-width text) in a ruling's words, summary, reason, re-check and paths
  when you record or draft one. A line it reads without that check (one recorded before this release, or one a
  teammate wrote with another client) is now shown with each such character written as `⟨U+XXXX⟩` in the edit hook, session start, the canon and the
  team view.
- **A passkey may now sign one authority link: revoking a consent grant.** A revoke only takes authority away, so a
  stolen passkey can cost you prompts, never widen what an agent may do. Every other link (an enrolment, a grant, a
  policy change, a revoke of an egress grant) is refused for a passkey, and a passkey made on `localhost` signs no
  authority link: a phone page on localhost lists grants and says to revoke from the laptop.
- **The coordinator keeps to its door.** Inside the coordinator's workspace every `levain` command other than the
  coordinator's own verbs, `team draft add --by coordinator` and the team and draft read verbs is refused ("run it from your own
  terminal, outside it"). The workspace is read from a record Levain keeps outside every workspace
  (`<levain home>/consent/coordinator_workspaces.json`), so a coordinator that deletes or damages its own marker is still
  doored. This fences an honest coordinator; it is not a sandbox (see Known edges). `levain coord agent claude --cwd
  DIR` refuses a directory that is your own Levain install.

### Groundwork for isolation (nothing here is reachable from a `levain` command yet)
- **The isolation gateway and its session store are being built; the install step that would run them is not built
  yet.** No command starts any of it, and it needs the extra `levain[isolation]` (h11 >= 0.16.0). What has landed: the egress
  proxy (`CONNECT <host>:443` to an allow-listed host only, address validated once and connected to directly, TLS
  `ClientHello` must name the same host); the brokered front and the admission half of its keys service (who may
  connect); a reaper service that sweeps dead sessions and revokes the sessions
  whose holder is gone; render refusing to start a front a revoke has already ended; END (a session's close) walking a
  tree with a bounded number of descriptors and refusing one deeper than 1024 levels by name (`manifest-depth-cap`);
  session adopt's walker, and the discard, restore and purge verbs.
  - A session's base (its `HEAD` commit, branch and bundle hash) is recorded at render; a session rendered before this
    has none, and adopt will refuse it (`base-unrecorded`).

### Known edges
- A Claude Code coordinator runs with no OS sandbox, so its own shell can rewrite the coordinator record; the door
  fences an honest coordinator there, and the lease token still fences every write.
- A Codex coordinator runs under your own Codex approval and sandbox settings, so its first `levain coord claim` asks for
  your approval and waits until you open the thread. OpenAI marks Codex's app-server and its WebSocket transport
  experimental and not supported for production workloads; the Codex coordinator rests on it.
- A pre-rule team-ledger draft with CRLF in its summary still refuses at promote.
- `levain team` re-genesis lists the old ledger's in-force rulings without marking hidden characters (your terminal
  only; nothing reaches an agent).
- A coordinator started by a different Levain than the one that set up `~/.levain/coordinator` asks before each of
  its commands; run `levain update --path ~/.levain/coordinator` from the Levain that starts it.

## [0.11.14] — 2026-10-21

### Added
- **Choice sign requests.** A caller can file one card with 2-8 options (`levain-sign-choice/1`). You tap one on the phone and sign it with your passkey; the signature covers the message with your choice as its last line, so a verifier reads the decision from the signed bytes, checked with stock `ssh-keygen -Y verify`.
  - A card can carry the caller's recommendation (`recommended`, one of its option keys). The phone marks it and never
    selects it, and it is not signed: you still tap your choice.
  - A card can take your own words (`write_in: true`): type an answer and it signs as the choice line
    `(*): <your words>`, held to the same rules as an option's text (one line, 200 characters, nothing hidden). The
    words are frozen and the exact line is shown from the moment you tap until the signing ends. A verifier reads a
    write-in only when it asks for one (`parse_choice(..., write_in=True)`).
- **A link straight onto one card.** `#sign=<id>` on the phone page opens it on that request. `levain consent phone --link-file` writes the page and token for a caller building such a link; `--token-file` keeps the token across restarts, so links you already hold keep working.
- **Opt out of signed answers.** `levain consent signed-answers --set off|authority-only|all` (default authority-only) is read by a caller before it files: off files nothing; authority-only signs only answers that authorise an act.
- **`levain coord`: one coordinator at a time.** A drop that arrives while nobody holds the coordinator role asks for
  a coordinator session; drops after it wait for that one. `levain coord status` shows who holds the role and what the
  next drop would do, `levain coord take` holds it yourself, and `levain coord release` ends it. A session that lost
  the role (it went quiet past its time, or you took over) has its thread changes refused. Splitting or re-titling by
  hand now records you: `--by operator` is the default for `levain drops split` and `levain thread set`.
- **`levain coord agent`: choose the coordinator your drops start.** `levain coord agent claude` makes
  `~/.levain/coordinator` (or `--cwd DIR`) a Levain install for Claude Code when it is not one, records the choice, and
  prints the one line that trusts that directory in Claude Code, which you run once. `levain coord agent none` (and
  no choice at all) means drops only queue for you.
- **`levain coord launch`: start the coordinator a drop asked for.** It starts a Claude Code session in the background
  in the coordinator's directory, only when that install's consent hook is wired, and prints the `claude attach` line,
  or why it did not start. `levain drop` and `levain coord wake` now do this themselves when they are the ones that
  asked.
- **`levain coord watch`: keep the coordinator's sessions honest.** Leave it running (`--every S`, or `--once` for one
  pass): it starts any coordinator a drop from the phone or the cockpit asked for, ends a launch whose session is
  gone, keeps the role with a coordinator that is still working and wrote in the last hour, and stops a session whose
  launch is over (you took over, it lost the role, or it failed to start), and deletes that launch's brief, which
  quotes your drops. It touches only the sessions Levain started, in the directory it started them in.
- **Verifiers are signed, one per check, and Levain runs the checker itself.** Each verifier you sign names the checker
  Levain ships (`output_compare` is the first: it runs the template's steps and compares what each one printed to what you
  pinned) and the exact steps and inputs it checks. A report only counts if it comes from that checker, on those steps and
  inputs, so a changed checker or a changed step list is refused rather than read as a pass.

### Changed
- **Re-sign your verifiers after upgrading.** The old `verifiers` configuration item is no longer read; `levain dispatch
  config show` marks it SUPERSEDED once you have signed the new kind, and `levain doctor` warns while only the old item is signed.
  A template whose verifier id 0.11 accepted but this version does not (uppercase, `.`, `_`, `@`, `:`, `/`, `+`, or over 48
  characters) is refused by name until it is re-signed.
  - A control store holding the new `verifier` item cannot be read by Levain 0.11.x, so do not downgrade after signing one.
- **The dispatch job store moves to format 4** the first time this version opens it. Your jobs are kept. After that,
  Levain 0.11.13 refuses to open the store, so do not downgrade.


### Fixed
- **A job's capability no longer starts with `-`.** About one in 64 capabilities that 0.11.10 to 0.11.13 handed a
  planner began with a dash, and `--capability <it>` was refused as a missing value. New ones never do. A job filed
  before you upgrade is still reached with `--capability=<it>`, which the refusals and `--help` now show.

### Known edges
- A coordinator launch that fails (an untrusted directory, a session that never appears) counts against the
  coordinator: after three in a row Levain stops starting them until you run `levain coord take` or
  `levain coord release`.
- A drop from the phone or the cockpit asks for a coordinator without starting it: with no `levain coord watch`
  running, run `levain coord launch` within 5 minutes, or the request lapses (a lapse nobody tried to start does not
  count as one of those three).
- A coordinator that finished its turn still holds the role until its lease runs out: drops sent meanwhile wait up to
  an hour for the next coordinator. A new drop is not handed into a session that is already running.

## [0.11.13] — 2026-10-20

### Security
- **A Codex patch could write a different file from the one Levain checked (0.11.0 to 0.11.12; upgrade to fix).**
  Levain read the file names in a Codex `apply_patch` differently from Codex itself when a header line held an unusual
  whitespace or line-break character: a tab, a carriage return inside the line, a form feed, U+2028 and a few others.
  Codex then wrote to a file whose name Levain never saw, so a standing consent grant on one file could cover a write to
  another, and with a tab, to a file outside the workspace (reproduced against Codex 0.162.1's own patch tool: it deleted
  a file one directory up). Levain now reads those headers the way Codex does, and a patch whose header holds any such
  character comes to you as an ordinary consent request. Reading a very long header line also no longer stalls the hook.

### Added
- **`levain drop`: leave a message for your coordinator and go.** Your words are kept exactly as you typed them and
  authorise nothing. Each drop gets a drop receipt: one when it arrives, and one naming the threads it became (one
  intent per thread: work, ask, note or relay). Typed inside a git checkout, a drop records the repo and branch you
  were on. `levain drops list|show` reads them back; `levain thread list|show` shows the threads, and `merge`,
  `detach`, `add` and `state` fix a split or close a thread.
- **`levain envelope`: sign what a piece of work may do, once.** An envelope names where the work may happen (repos
  and paths), which lasting steps it may take (land onto a branch, push, publish), a budget and a stop time, and you
  sign it with an enrolled key. `levain envelope echo` writes one for threads you name and shows it in plain words;
  `levain envelope sign` shows it again in full, asks y/N, and signs it. `levain drop --envelope SPEC "words"` signs one
  as you send the drop. You can narrow or kill an envelope at any time without signing (`levain envelope kill`,
  `levain envelope narrow`); only a wider one needs a new signature.
- **Dispatch keeps one record per verified run.** When a verifier finishes a run, Levain checks its report against what
  that run was set up to check and keeps one record of the result. Landing a job reads only these records. A verifier that
  could not run at all (it timed out, or its report was missing or damaged) fails the job with its own cause,
  `verifier_refused`, so it never reads as the verifier saying no.
  - **The dispatch job store moves to format 3** the first time this version opens it. Your jobs are kept. After that,
    Levain 0.11.12 refuses to open the store, so do not downgrade.

### Known edges
- The coordinator session that reads drops and splits them comes later; until then a thread is named with
  `levain drops split`. The phone drop sheet comes later too.
- Nothing acts on an envelope yet: until Levain's separate-user setup is in place, every step that needs your say
  still asks you each time, and `levain envelope show` says so. Signing from the phone comes later.
- The verifier sessions that write these records come with the slot work; until then no run produces one.

## [0.11.12] — 2026-10-20

### Added
- **Adoption rules now say exactly what they check.** An adoption rule names the repository it lands in and a short
  list of typed checks: which branches it may land on, plus at least one limit (changed lines, bytes, files, or the
  paths it may touch; optionally no new files, or a maximum age). Free-text criteria are refused.
  - **Upgrading from 0.11.10:** a rule you signed with free-text criteria no longer applies. It never landed anything
    (0.11.10 ran no adoption), and it lands nothing now. `levain dispatch config show` and `levain doctor` name the
    rule and the fix: sign a new `adoption_rules` item with `repo` and typed criteria
    (`levain dispatch config sign adoption_rules <body.json>`).
  - **The dispatch job store moves to format 2** the first time this version opens it. Your jobs are kept. After that,
    Levain 0.11.10 refuses to open the store, so do not downgrade.
  - `levain doctor` has a new row, "dispatch adoption rules".


### Fixed
- Setting your state line, from the cockpit, the web page, the terminal view, the phone app or
  `levain state`, now also saves it once, word for word, as a memory episode tagged `felt,state`, so
  your next consolidate can read how you said things were. Before, only the live line changed and
  your memory never saw it. Setting the same words again while the line is still live, or clearing it, saves nothing new.
  If the memory store is missing or busy, the line is still set: `levain state` prints a note and the
  web response says `episode: failed`, and setting the same words again delivers the episode.
- That saved copy outlives the line. The line itself disappears after 12 hours or on
  `levain state --clear`; the episode stays in memory until you delete it. To delete it, tombstone
  it from the Recent episodes panel of the writable cockpit (`levain web --write`) or the terminal view.

### Known edges
- Adoption itself has no command yet (the `authorise` and `land` verbs and the phone card come in F11-s2).

## [0.11.11] — 2026-10-20

### Added
- **Export with your organisation's names.** `levain consent receipts export --format agentboundary --names FILE` maps
  Levain's own names (the actor, the approver's key, the policy, the capability, the working directory) to the aliases
  your verifier expects, before each receipt is hashed and chained. It renames; it never changes what happened. A file
  that sets a decision, a status, an actor type, a time, or what the run was (the agent's framework or model, the tool's
  name, the environment, any version but the policy's) is refused and nothing is exported. An authority name maps one to
  one, a permission-dialog approval is never relabelled, and every renamed field is marked `synthesized`. The names file
  is part of each receipt's id, so a renamed export never reuses an unrenamed receipt's id. Against the AgentBoundary
  v0.2-alpha conformance scenarios, Levain's receipts pass 26 of 54 as Levain writes them and 41 of 54 with a names file.

## [0.11.10] — 2026-10-20

### Added
- **Context meter in the Sessions view.** Each Claude Code and Codex session shows how much context it holds now ("context
  142K"; Codex "context 80K of 258K"), and says when it passes 200K (a fixed warn line, not the model's window). Read from
  the session's own transcript; a display, it decides nothing.
- **The phone page and the cockpit show what you are signing.** Each request on the phone consent page and in the
  cockpit's Decisions view now shows its input digest and request digest (short, the full value one tap away): the same
  two values `levain consent answer` prints before it signs. A question that leads to a push shows them too, since one tap
  there allows the push.
- **A hint beside a shell command.** For a shell request, a short "hint (a few patterns only; informs, never authorises)"
  says what the command looks like: piping a download into a shell, `$()`, `sudo`, `rm -r`, a force-push, writes outside the
  working directory and similar. When nothing matches it says "no pattern matched: read the command". A hint never changes
  what is signed or allowed.
- Invisible and direction-changing characters in an agent's text (the command, its path, a tool or agent name) are shown as
  visible markers such as ⟨U+202E⟩ on both surfaces.
- **Every answer you sign now also signs the queue's history.** The consent queue already wrote one receipt per step
  (filed, answered, expired, ruled), kept append-only by the database. Levain now reads those receipts as a Merkle log
  (RFC 6962, computed from the rows, nothing new stored), and every answer and late ruling you sign carries that log's
  head: how many receipts there were and their root hash. The queue applies your answer only if the receipts it covers
  are still exactly what you signed over. So a later rewrite of the history, even by something that removed the
  database's guards, shows up under your own key, not a signature Levain made for itself. `levain consent receipts
  verify` recomputes the log, re-checks every signed answer, and names any request whose receipts changed under it (exit
  1), and refuses a store it cannot read as the current log (a queue from before the upgrade that no levain command has
  opened yet, an empty file) with "UNREADABLE", exit 1; it lists answers from before this release as "signed before
  chaining" and an answer by a key you revoked since as "signer revoked since", neither a failure. `levain consent
  answer` and the phone page sign the queue's own record, so they carry the head with no change on your side. Answer and
  ruling formats move to `levain-consent-answer/2` and `levain-consent-ruling/2`; an old-format answer is refused when
  applied (answer the request again), and one already applied before the upgrade still delivers.
- **Budgets per session, on by default.** Levain counts what each Claude Code and Codex session spends, its subagents
  included: tool calls, active hours, fresh tokens, cache-read tokens, and how often one call repeats (a loop). Every
  call is counted, read-only ones too. When a session passes a limit, its next call waits and you get one ping:
  **Extend** (that limit doubles and the call goes on) or **End**. End, or no answer before the hold runs out, stops
  the session (Claude Code's turn ends; Codex has every later call refused) until you extend it, from the phone or with
  `levain limits extend <session>`, which also resumes it. Nothing is ever cut off silently, and the agent is told a
  budget you set stopped it, not a fault. The defaults sit well above a heavy day's sessions: 3,000 calls, 12 active
  hours, 15M fresh tokens, 600M cache-read tokens, one call 8 times in any 10. Change them with
  `levain limits set <limit> <value>` (per agent with `--agent`); see the counts with `levain limits show`; `levain
  doctor` shows the limits in force. Dollars are not enforced (a price table goes stale and a subscription is not billed
  per token).
- **`levain publish`: Levain publishes a package for the agent, only on your yes.** `levain publish pypi <files>` and
  `levain publish npm <tarball>` show you the exact package, version, files (their digests), target index and, for npm,
  the dist-tag and access; on your signed yes Levain uploads exactly those files to exactly that index itself. The name
  and version are read from inside the files, never from what the agent typed, and a file changed after asking changes
  nothing (Levain uploads its own copy). Settings that could send the upload elsewhere (environment variables,
  `.npmrc`, `publishConfig`) are overridden. PyPI credentials come from `TWINE_USERNAME` / `TWINE_PASSWORD`, or the
  `~/.pypirc` section for that exact index; with none, it refuses before asking you.
- **Web push to your phone (`levain ping`).** Levain can now ping your phone itself, without a notify command of your own:
  Chrome on Android from the phone page as it is, and an iPhone (iOS 16.4 or later) once the phone page is added to the Home
  Screen (the page shows the two taps). Pings go out for a gated call that waits on you, a re-ping, a ruling that waits, a
  question an agent asked, and a push request. `levain ping test` sends a test to every phone; `levain ping devices` lists
  paired phones and `levain ping revoke` removes one. A ping only opens the phone page: every answer still needs your passkey.
  Your `notify_argv` command keeps working beside it.
- **Pairing a phone by typed code.** On an iPhone the Home Screen app keeps its own storage, so a link opened in Safari does not
  reach it. Tap "Install on this phone" in Safari and the page shows an 8-character code (good for 10 minutes, one use, only the
  newest works); type it into the "Pairing code" box in the installed app. `levain ping pair` prints a fresh code from the
  laptop. Five wrong tries in 10 minutes lock every short-code pairing for up to 10 minutes, or until a new code is made.
- **The phone app's state line and board.** From the phone you can set your state line (the same one the cockpit, `levain
  state` and the TUI set; agents see it on their next prompt), and work your Tray: resolve and snooze an item. Open loops and
  Keep are there to read. The phone does only what the cockpit's own tiers allow without a signature; anything more says
  "on the laptop".
- **The notification record.** Every ping Levain sent, where it went (each phone, and whether your `notify_argv` command
  started), whether you opened it, and what came of it (answered, expired, ruled late, still waiting), read from the queue
  when you look. On the phone, in the cockpit and with `levain ping record`.
- **Export the consent receipts for an AgentBoundary verifier.** `levain consent receipts export --format agentboundary
  [--out FILE]` writes the queue's receipts as AgentBoundary v0.2-alpha receipts: a JSON array in UTF-8, one receipt per
  request state, chained, each field marked observed, inferred or synthesized. A request less than 24 hours old is held
  back until it settles, so a receipt never changes after it was exported. `--out` never overwrites a file.
- **A refused answer is kept as evidence.** When an answer verifies under the key it names but is refused (the key is
  not enrolled, revoked, compromised, of a class that does not cover the request, or the answer came after the hold
  ended), the queue now keeps it, a few per request, instead of only refusing it. An exported receipt for that request
  carries it as the evidence of the refusal, never as an approval. A key that is not enrolled, or one revoked as
  compromised, is never named the operator.
- **`levain dispatch`: a planner hands work to cheaper models, and you sign it first.** A planning agent (Claude Code or
  Codex) writes a task contract. The contract names a template you set up, the files the work may read and write, a
  budget and an expiry. Filing it creates a job and does nothing else. Before anything runs, you read the job on your
  own laptop and sign its digest with your enrolled key.
  - `levain dispatch file <contract.json>`: the planner files a contract. It gets the job id back, plus a capability
    (shown once) that lets that planner session recommend or cancel its own job later. Filing gives the planner no
    authority.
  - `levain dispatch show <job>` / `list`: the job as you will sign it. You see the goal in full, the read and write
    lists, the template, the models, the verifier, the budget, the expiry and every state change with its cause.
  - `levain dispatch sign <job>`: you sign the job's digest after reading the whole view and answering y/N on your
    terminal. An agent's process is refused.
  - `levain dispatch cancel <job>`: the planner can cancel its own job until you authorise a result. You can cancel any
    live job with a signature.
  - `levain dispatch recommend <job>`: the planner marks a verified result it likes. This is advisory only.
  - On your phone: the Levain page shows each filed job as a card (the goal in full, the files, the template, where the
    read files are sent, the budget and the digest), and you can sign it or cancel it with your passkey (Face ID). A
    passkey that is not enrolled for dispatch signing is never offered.
  - An MCP server (`levain_dispatch`), registered in the workspace `.mcp.json` by `levain init` / `levain update`, gives
    a Claude Code planner the same file, show, list, recommend and cancel tools. It has no sign tool. Claude Code asks
    you before each call (a headless run is refused). `levain doctor` reports whether it is registered.
- **`levain dispatch config`: you decide what may run.** Templates, verifiers, the provider policy (which provider may
  read which paths), the route map and the adoption rules are signed configuration. Each change is one item, signed by
  one pinned laptop key, in one hash chain.
  - `levain dispatch config init --trust-root <fingerprint>`: create the store and pin your laptop key. Only an
    enrolled, active laptop key held with presence can be pinned. A phone key or a passkey cannot.
  - `levain dispatch config sign <kind> <body.json> --signing-key <key>`: turn a body file into the next item. A body
    the rules refuse is refused before your key is touched. You see the kind, the generation, the store, the previous
    head and a diff against what is active now, then answer y/N.
  - `levain dispatch config accept <item> <signature>`: accept an item signed on another machine.
  - `levain dispatch config show`: what is configured now.

  A contract can only be filed against a template you signed. With nothing configured, every filing is refused.
- **`levain init` offers one default grant per agent.** When your authority log is set up and a laptop-presence key is
  enrolled, init shows one standing grant (Claude Code: workspace edits and WebFetch; Codex: apply_patch; 14 days) and
  asks [y/N], default No. A yes runs the `levain consent grant create` line it printed, with your key's usual passphrase
  or Touch ID. With no terminal, it prints that line and signs nothing; `--default-grants no` skips it.

### Changed
- **Claude Code sessions started in a subfolder are gated too.** Claude Code reads a project's `.claude/settings.json`
  only from the folder a session starts in, so a session started in a subfolder of a levain workspace ran none of
  levain's hooks: no consent queue, no gate, no memory hooks. When the workspace is its own git repository's root,
  levain now keeps its hooks in `.claude/settings.local.json` (which Claude Code also reads from the repository root),
  every command naming its script by absolute path, and keeps that file out of git. `levain update` moves an existing
  install over (your own entries in the local file stay, and a malformed local file stops the update with nothing
  written), and `levain doctor` says where the hooks are, warns about a levain hook left in the wrong file, and warns
  when Claude Code is too old to read the local file from the repository root.
- **Agent config and hooks are refused when nothing can approve the edit.** Until now, with no enrolled key that could
  approve an edit of agent config or hooks, Claude Code and Codex in acceptEdits, bypass, auto or dontAsk mode (Codex
  with approvals off) could make the edit with no question. Now levain refuses it with a plain denial: the agent is told
  the change is the operator's, not to retry it or route around it (the shell included), and to ask you; it carries on
  with the rest of its work. Claude Code's default mode and Codex with approvals on keep asking through the agent's own
  dialog, and with an enrolled key that can approve it the edit comes to you as before. `levain doctor` warns when no
  key can approve such edits and says how to allow them. To make such edits in one session, start it from your own shell
  with `LEVAIN_UNCONFINED=1`; a settings file that sets that variable is ignored, so a cloned repository cannot turn the
  gate off. Git's own config and hooks now count as agent config too, since git runs what they name at your next git
  command: everything inside a repository's `.git/` folder except its objects, refs, logs and state files (so its
  `config`, `hooks/`, `info/`, and a linked worktree's or submodule's git folder), a `.git` file pointing at another git
  folder, the folder git itself says it runs hooks from (levain asks git, so a `core.hooksPath` such as husky's
  `.husky/`, or one outside the repository, counts), and your `~/.gitconfig` and `~/.config/git/config`. Known edges: a
  shell command that writes agent config is not caught here (that belongs to the session sandbox); and a Codex hook that
  runs past Codex's timeout lets the call through, as Codex has no setting to block on a hook failure.
- `levain consent pending`, the undecided list and the cockpit card now say what a publish or a budget request is about:
  the package, version and index for `levain publish`; the agent, session, limit and counts for an extend. They used to
  read "none named: the working directory".


### Fixed
- **An OpenHands entity's turn runs once.** Saving a finished turn to memory used to run the conversation again, unchecked:
  a turn stopped by `--max-iterations` (or any run the runtime ends in its error or stuck state) started over with a fresh
  budget, and an action held at the gate during that second run was reported as one that ran. The turn now runs once; one
  that ends in error or stuck is reported as failed (exit 3 from `levain run --task`) and nothing from it is saved.
- **On macOS, bash now gets its own session ssh agent, not yours.** This landed in 0.11.7 without a line here: the 0.11.4
  limit ("on macOS bash still receives your `$SSH_AUTH_SOCK`") no longer holds. bash's `$SSH_AUTH_SOCK` names levain's
  session agent, which holds only keys you add with `levain ssh-add`, and a connect to any other unix socket by its path is
  refused unless you granted it.
- When several agents of one session crossed a budget limit at once, one of them could be refused with "the operator is
  being asked" even after the operator had already extended the limit, or file a second request for the same crossing.
  All of them now wait on the one request and go on together when it is extended, and none goes on once the operator has
  chosen End. A session stopped by End now says "The operator chose End." on every stop.
- **A confined agent can no longer write your git configuration.** The files your global git config makes git read (its
  includes and `core.attributesFile` among them) are now write-protected, so nothing inside the sandbox can plant a
  filter or credential-helper setting that later runs as you. If Levain finds no root-owned `git`, it says so once and
  protects only the default locations.
- **Starting a confined agent no longer fails at random when another Claude Code or Codex session deletes a folder under
  `~/.claude` or `~/.codex` at the same moment.**

### Known edges
- The hint is a pattern reader, not a shell parser: a command written to dodge the patterns gets no hint, and the full command
  is always shown above it.
- A download saved to a file and run in a later step (`curl -o x.sh URL; bash x.sh`) is not hinted.
- The digests are a record of what is signed, to compare with the terminal's output; a person cannot recompute them.
- Claude Code's transcript does not say which context window a session has, so the meter shows tokens and the 200K warn
  line, never a percentage, for Claude Code.
- A session started by an older install's hooks, under a relocated `CLAUDE_CONFIG_DIR`, shows "context not read" until its
  hooks are updated (`levain update`).
- A session the agent itself starts through the shell with `LEVAIN_UNCONFINED=1`, or one a trusted repository's startup
  hook sets it for, is unconfined: both need code you already let run.
- In Claude Code's default mode, your own allow rules (or a trusted repository's, or `--allowedTools`) can let an edit
  of agent config through with no dialog, for the paths Claude Code does not protect itself.
- An unconfined session's edits of agent config get no gate row in the ledger; the record of the call itself still
  lands.
- Codex, two calls at once in one session: a call that passed its last halt check just before the other halts the
  session can still run.
- A settings file passed to Claude Code with `--settings` cannot be seen by the hook, so one that sets
  `LEVAIN_UNCONFINED` is not caught.
- A config that turns hooks off (Claude Code's `disableAllHooks`, Codex's `[features] hooks = false`) turns the floor
  off with every other hook; `levain doctor` fails when it finds one. Codex's setting inside a `[profiles.<name>]`
  section is not in that check.
- levain asks git with your user's home and no `GIT_*` variables, so a `GIT_CONFIG_GLOBAL` or `GIT_CONFIG_SYSTEM` in
  your own shell can point git at a hooks folder levain does not see.
- A machine-wide Claude Code settings file your user cannot read is not checked for `LEVAIN_UNCONFINED` (the gate stays
  on there).
- An edit inside a `.git` folder's objects, refs or logs, or anywhere in a bare repository, counts as agent config; so
  does any file when git cannot answer (an unreadable config, or past its 3 seconds or 4 repositories in one call).
- levain asks `/usr/bin/git` first; on a machine without it, it uses `/opt/homebrew/bin/git` or `/usr/local/bin/git`,
  and an agent that can write that file could make levain misread where hooks run.
- A single Codex patch touching more than 4 repositories can have the agent-config path cut from its description in the
  ledger; the call is still gated.
- A rewrite of the receipts after the last signed answer (the unsigned tail: grant answers, expiries, undecided marks)
  is not evident until your next signed answer covers it, or ever if no one signs again.
- Delivery checks only the head's shape: an answer applied before a rewrite still delivers after it; the next apply and
  `receipts verify` are what recompute the log and see the rewrite.
- `receipts verify` costs grow with the number of distinct signed sizes times the receipts (71-136 ms per head over
  10,000 receipts, measured 10-19).
- A raw writer who drops the triggers and deletes the newest rows (receipts, or the request rows holding the latest
  signed answers) only lengthens the unsigned tail that `verify` reports; it cannot make an earlier signed head match a
  rewritten history.
- The authority ledger is not part of this log, and a writer who also holds an enrolled key can sign a head over
  anything: the key is the root of trust.
- An answer signed by a key you revoked without marking it compromised is listed as "signer revoked since", whatever its
  own signed time says: that time is the signer's claim, so it cannot be checked against the key's holder. Revoke a lost
  or stolen key with `levain cockpit revoke <key> --compromised-since <seq>` (the seq from `levain cockpit keys`), and
  every answer it signed then fails `verify`.
- A signed head over zero receipts always matches (the empty log's root is fixed): it attests nothing.
- An answer signed from the phone page (passkey) carrying the head has not been run on a phone yet.
- A limit is checked at tool calls, and tokens are read from the agent's own transcript, which can lag: a session can
  pass a token limit by up to the turn in flight.
- Without the session sandbox the agent's own shell can edit `limits.json`: a brake on a runaway, not a bound on an
  agent that tries.
- The loop limit catches one call repeating; a loop that changes an argument every time is caught later by the call and
  token limits.
- After an Extend, a call that itself needs your approval is asked for again (the agent is told to run it again), so
  two waits never share one hook.
- A Codex command typed into a terminal it opened earlier is not counted and is not stopped by a limit.
- `levain publish` is a path the agent is told to take, not a wall: a `twine upload` or `npm publish` the agent runs
  itself is not gated until Levain's hands run as another user.
- `levain publish` runs the twine and npm found on your PATH (Homebrew, `~/.local/bin`), which are writable by your user.
- Only a workspace that is its own git repository's root gets the subfolder fix. A workspace nested inside a larger repository, a
  linked worktree, a submodule, or a folder that is not under git keeps its hooks in `.claude/settings.json`, and a
  session started in a subfolder there still runs none of them: start it at the workspace root.
- A workspace set up before `git init` first takes the `settings.json` layout; run `git init`, then `levain update`, and
  it moves to the local layout.
- A write to `.claude/settings.local.json` is not locked against a running Claude Code saving a permission at the same
  instant; an approval saved in that window can be lost.
- A tool call through the gate from a subfolder was checked by replaying the hooks and by SessionStart in a real
  Claude Code run, not by an authenticated tool call.
- Someone on your tailnet who sends bad pairing codes fast enough can keep the phone server's per-minute cap full and block
  short and long pairings alike until they stop (availability only; it never pairs anyone).
- `levain ping pair` prints a live code, so run it yourself and not through an agent (the code would sit in the agent's
  transcript for 10 minutes).
- Capture (adding a new Tray item) is not offered on the phone: the cockpit tiers it above what the phone holds without a
  signature.
- Anything that can read your queue directory's `webpush/webpush.db` can send your phones a ping with its own words. The tap
  opens the real phone page; the words are not trusted for anything.
- The notification record keeps every ping (it is an append-only record); the phone and the cockpit show the newest 500.
- Turning pings on from the phone page pairs that phone: it shows up in `levain ping devices` and `levain ping revoke`
  stops it. A subscription saved before this change with no phone attached is switched off the first time Levain opens
  its push store; turn pings on again from that phone.
- A slow push service or network can leave a ping's delivery reading "no answer in time": Levain waits a
  bounded time and never rewrites that line after.
- A phone whose browser lost its saved pairing (site data cleared) cannot turn pings on again from the phone link while its
  old pairing still owns the subscription: `levain ping revoke` that phone, then turn pings on again.
- A phone whose pings were made for another Levain install (a reinstall, a rotated key) has them switched off when its page
  opens, and the page says so; turn pings on again. A browser that does not show a subscription's key is not checked: the
  laptop retires that phone's pings the first time Apple's push service refuses them (other push services' refusals were not measured), and the phone reads "on" until you tap
  Turn off, then turn on. If Turn off (or Turn off and then on) is tapped within one round trip of the page opening, or the laptop
  does not answer at that moment, the page may misreport once; the stale entry is retired by the next ping.
- On an install whose cockpit is governed, the phone takes no writes until the cockpit has run once on the laptop (it keeps
  the authority log the phone checks); reads work from the start.
- Kept refused answers are not covered by the signed receipts head: something that can write the queue's database can
  add or remove one.
- When several refused answers arrived for one request, the export carries the latest.
- A receipt that carries a refused answer ends at the time Levain refused that answer.
- A budget Extend does not resume a session started with `--setting-sources` or `--max-turns`: resume it by hand.
- Nothing executes yet. Jobs can be filed, read, signed and cancelled. The runner, the verifier run and adoption come in
  later slices.
- The configuration store is a file your own user account can write. A forged or edited item fails verification on read.
  A process running as you could still delete the newest items and roll the configuration back to an older signed state.
  The planned fix binds each signed contract to the configuration head it resolved under.
- There is no trust-root rotation yet. If the pinned key is revoked or lost, every read refuses, and recovery means a
  new store.
- The agent check on `sign`, `cancel`, `config init/sign/accept` checks intent. It is not a security boundary. Until the
  control principal is placed (S1), what protects a laptop signature is how the key is held (a touch, a passphrase, a
  key no agent can read).
- Codex does not get the `levain_dispatch` MCP server yet. A Codex planner files through the `levain dispatch` command.
- A template's provider and route are not cross-checked against the provider policy or the route map.
- A read path built from look-alike characters shows as the path it imitates in the signer's view.
- Whether you can set up a template from this CLI without reading code is yours to judge on the first real template.

## [0.11.9] — 2026-10-19


### Fixed
- **The confined shell starts again on a machine where Claude Code has run.** In 0.11.7 and 0.11.8 levain refused to give a session its shell when any file under `~/.claude` or `~/.codex` had a second hard link, and Claude Code's own file-history backups have them, so the shell was unavailable to almost every Claude Code user, on macOS and on Linux. The check now covers what those tools load as configuration: the files directly in `~/.claude` and `~/.codex` (and in `CLAUDE_CONFIG_DIR` / `CODEX_HOME` when they are set), the hooks, agents, commands, prompts, skills, plugins, rules, output-styles, scripts and bin directories under them, and Claude Code's project memory. A second hard link to one of those still refuses the shell, naming the file.
- **`sudo levain setup-isolation` on Linux refuses before it changes anything when `setfacl` is missing** (install the `acl` package), in a dry run too. Before, it stopped part way and left a user behind, and `--undo` crashed the same way; `--undo` now stops and names the package. Repairing an existing setup does not need it.

### Known limits closed
- **The two limits tracked for 1.0 are closed, and their lines are removed from the 0.9.1 and 0.11.4 notes.** Files a confined session writes in your home no longer run outside it later (0.11.4): measured in a macOS 26.6 VM and a Linux 7.2 VM on 2026-10-19, every planted shell startup file, git config, `~/bin` / `~/.local/bin` program, user service, autostart entry and agent config or hook was refused, and your own shell and git started as before. Bash can no longer reach your gpg-agent (0.9.1): on macOS, measured in the same VM, no confined session reached the agent or made it run a program, including through a gnupg home set inside the shell; on Linux this holds where the Landlock tier described in 0.11.4 enforces it, and under `levain setup-isolation` on any kernel (measured on Ubuntu with kernel 6.8 on 2026-10-19).

### Changed
- **Each passkey the phone page makes is named for when it was made.** The name (and display name) is `levain YYYY-MM-DD HH:MM:SS xxxx`: the phone's local time and the first four hex digits of the passkey's random user id, so a phone's password list can tell one levain passkey from another. The page's "passkey made" note shows the name beside the key's fingerprint. Passkeys made before this keep the name they were made with, "levain operator".
- **On Linux, the confined shell can no longer leave files that run outside it later.** Inside bash these are read-only: `~/bin`, `~/.local/bin`, user systemd units (`~/.config/systemd`, `~/.local/share/systemd`), `~/.config/environment.d`, autostart entries (`~/.config/autostart`), `~/.bashrc.d`, fish and zsh configuration (`~/.config/fish`, `~/.config/zsh`), KDE's `~/.config/plasma-workspace/env`, `~/.claude`, `~/.codex`, and the workspace's own `.claude` and `.codex`. One that does not exist yet is created empty first, so bash cannot create it either. When the agents' configuration resolves through a symlink in a directory bash can write, that directory is read-only too; when that directory is the workspace or one above it, the shell is refused, naming the link. Measured in a Linux 7.2 VM: every planted file that landed before this change is refused, in both cases where its directory did and did not exist.
- **On macOS, `~/.config/zsh` joins the shell startup files bash cannot write**, and an absolute `CLAUDE_CONFIG_DIR` or `CODEX_HOME` in levain's environment is protected as `~/.claude` and `~/.codex` are.
- **`levain setup-isolation` now creates the hands workspace's `.claude` and `.codex`**, owned by the hands user, so bash can be given them read-only. On Linux, an entity set up before this refuses bash until you run `sudo levain setup-isolation` again for it; that repairs it in place.
- **On macOS a confined entity can no longer list other processes' command lines with `pgrep -fl`.** `pgrep` and `pkill` read every process's command line from a system daemon (`sysmond`) that answers for any user, so a secret passed on another program's command line was visible to the entity's shell. The sandbox profile now denies that daemon. The cost: inside the entity's shell, `pgrep` and `pkill` stop working entirely, by name too; use `kill <pid>`, which is unaffected, as is Python's `psutil`. `ps` and `top` were already unavailable there. Measured 2026-10-07 on macOS 26.6.2.

### Known edges
- Two passkeys made in the same second share a name only when their random user ids share their first two bytes.
- Not yet seen in an iPhone's Settings > Passwords: the run was headless Chrome with a virtual authenticator.

## [0.11.8] — 2026-10-19

### Added
- **A question can carry the push it leads to, and one tap answers it and allows that push.** `levain ask --would-push KEY --repo PATH --remote URL --branch NAME` files that exact `levain push` (the repository, remote, branch and its current tip, derived as `levain push` derives it) as an undecided consent request tied to option KEY. On the phone page that option shows the push as its own consent card shows it; picking it with a passkey signs a late ruling ALLOW on that request and then records the answer. The answer is still only words: the ALLOW is the signed ruling, and `levain push` from the asking session takes it once. Any other answer, from any channel, allows nothing. Refused unless an active key can allow a push.

### Known edges
- v1 ties only `levain push` (a passkey allows up to T2, and `levain push` is the one gated T2 call); a commit on the branch after the ask is a different push, which asks again; the push must run from the session that asked; the tied request takes the session's next gap number although the agent was never refused.

## [0.11.7] — 2026-10-19

### Added
- **A request nobody answered in time is now left open for you to rule on later, and your ruling reaches the agent.** When the hold on a gated call ends with no signed answer, levain no longer treats it as a refusal: the request is UNDECIDED, numbered per agent session (gap #1, #2, ...), and the agent is told the action was not taken and is open, not rejected. `levain consent rule` with no gap lists the undecided requests; `levain consent rule <gap> allow|deny --signing-key <key>` signs your ruling on one. A gap is named by its number (`3` or `#3`) while no other recent session has that number, else with its session handle (`3@a1b2c3`, the random tail of the session id, and a subagent's handle adds `+` and the tail of its own session id; or `--session` / `--agent`). An allow of an agent-config edit needs a laptop-presence key. A session that is still running gets the ruling at its next step. A session that has ended is resumed with the ruling as its prompt, under the same session id, in the directory it started in and its permission mode (Claude Code `-p` runs and `--bg` runs; a Codex `codex exec` session whose process is gone is resumed with `codex exec resume`, under the sandbox it last ran in, read from Codex's own record of the session; Codex's approval policy is not restored). A resume is refused, and the ruling left for the session's next step, when levain cannot be sure it would bring back the same session with no fewer restrictions: the original command line carried an option levain has not judged safe to drop, or Codex's record of the sandbox cannot be read. `--no-deliver` records a ruling without delivering it, and `levain consent deliver` delivers every ruling whose text has not reached its session yet (a ruling's text reaches a session once). The ledger rows between the gap and the ruling are printed beside it.
- **The phone page can sign a late ruling.** An undecided request shows "rule allow with passkey" and "rule deny with passkey". A late allow of a T3 (agent config or hooks) request is the laptop's, as an answer is. A request whose display was cut at its limit can be denied from the phone, never allowed.
- **Claude Code's `PreToolUse` hook now gates edits of agent config and hooks, including where no dialog ever asks.** Claude Code asks a `PermissionRequest` hook only when it is about to show its dialog, so in bypass mode a T3 call ran with nothing to answer. The new gate holds such a call in bypass mode, and in default mode while away mode is on, for a signed answer (`gate_hold_seconds` in the consent settings: default 300, at most 840 seconds) and pings you through a notify command when one is set. If nobody answers it denies the call as UNDECIDED and stops the session. After the gate allows a call that Claude Code then asks about in its own dialog anyway, the dialog takes that same allow once, so you are asked once. `levain init` and `levain update` wire the gate.
- **Codex gets the same gate, and a per-session latch.** A hook can deny a Codex call but cannot stop its turn, so the first undecided T3 call halts the session in the queue store: every later tool call of that session is denied until your ruling on the halting request releases it. `levain init` and `levain update` now write Codex's trust for levain's own hooks (each confirmed with Codex) and exit non-zero, listing what is left, when they could not.
- **`levain push --remote URL --branch NAME` asks you before it pushes.** It files a push request at T2 naming the repository, the remote, the branch and the exact commit, waits for your signed answer (`--hold`, else `push_hold_seconds`, else 90 seconds, at most 840), and on an allow pushes that commit and nothing else: never a force, a delete or a tag. Exit codes: 0 pushed, 1 not pushed, 3 undecided, 4 outcome unknown. When the hold ends unanswered the push stays open as a gap; after an ALLOW ruling, run the same command again and it takes the ruling once. It is a path a cooperative agent takes, not a boundary: it runs as the agent, and a plain `git push` in a shell is still not gated.
- **The authority ledger: which tool calls ran, and on whose authority.** `levain consent ledger counts` counts calls by agent and stamp (`grant`, `answer`, `ruling`, `own_rules`, `classifier`, `bypass`, `dialog_unsigned`, `hook`, `sandbox`, `unknown`, `pending`); `levain consent ledger window --agent A --session ID --since T` lists one session's calls between two times (what `consent rule` prints for a gap); `digest` lists the calls a standing grant or a late ruling answered; `reconcile` re-reads the agent's own record for every call still `pending`. Only digests of tool input are stored, never command text or file content. The ledger lists calls that ran or that the gate denied; a call the agent's own rules or sandbox refused before it ran fires no hook and is not listed. A stamp other than levain's own (a grant, an answer, a ruling) is the agent's record of itself, which an agent running as you can write.
- **`levain consent phone --sign-namespace NS` lets the phone sign a statement for a script of yours.** A script on the laptop writes a sign request into the queue directory's `sign/` folder; the phone page lists it with the whole message, and you sign it with your passkey (Face ID, Touch ID or the device passcode, as the authenticator reports it) or deny it. The result lands beside the request, and `ssh-keygen -Y verify -n NS` checks the signature. levain refuses a signature without user verification before it records anything; the caller's `ssh-keygen` check covers the key, the message and the namespace only, not how you were verified. The flag repeats. A namespace beginning `levain`, one in a family levain itself verifies in, or `git`, `file` or `email` is refused, so a phone signature can never pass as a consent answer. The queue directory's `claude_code.json` must name the cockpit (`cockpit_data_root`).
- **An agent can ask you a question and keep working: `levain ask`.** It files what it needs, 2-6 options, the one it recommends and what waits on the answer, pings you, and returns at once. `levain question list|show|answer` reads and answers them. The answer reaches the asking session once, at its next tool call; a session that has ended is resumed with it (`levain question deliver` retries any that did not land). An answer authorises nothing: a gated call still goes through levain's gate.
- **An agent that reaches its tools more readily than its shell can ask you through an MCP tool, `ask`.** It takes the same question as `levain ask` (the question, 2-6 options, the one it recommends, what waits on the answer), files it the same way, pings you and returns at once; an answer authorises nothing. `levain init` and `levain update` register the `levain_band` server in the workspace's `.mcp.json` for Claude Code (and allow its `ask` tool in the workspace settings) and in Codex's `config.toml`, where the block carries `default_tools_approval_mode = "approve"` for that server alone, because `codex exec` refuses an unapproved MCP call. `levain doctor` reports whether each is registered as levain writes it (a warning, never a failure: `levain ask` works without it). The session a question names is the one the client reports in the call; with none, the environment Claude Code sets; with neither, the question says it names no session.

### Changed
- **Leaving an explicit `levain consent away on` now takes a signed leave.** `levain consent away off` and `levain consent away auto` need `--signing-key <key>`, a key enrolled in the install's cockpit (any class), when an `on` stands; with no `on` standing, no key is needed. `on` records an away mark in the consent queue store, and the hook reads that mark as well as the settings file, so rewriting the away words in the settings file no longer ends away mode. Revoking the key that signed a leave puts away mode back on. A queue store that cannot be read is never read as "no mark": `off` and `auto` refuse until it is repaired or moved aside. With `--agent all` the leaves are signed one agent at a time, and a failure part way leaves the earlier agents left and exits non-zero. Away mode that came from input idle needs no signature to end: your input ends it.
- **A gap is named by the random tail of its session id**, not the front of it, which sessions started close together shared.

### Security
- **A late ruling is signed in its own namespace and verified against a fresh replay of the authority log.**
- **The phone's passkey origin must be exactly `https://<name>`, with no port.** On `localhost` it is the phone port's own `http://localhost:<port>`.

### Known edges
- A process running as you can delete or rewrite the consent store or drop the ledger's triggers, and can point `cockpit_data_root` at a cockpit holding its own key, which lets it answer consent. Without OS confinement levain does not stop this; `levain doctor` warns on a foreign cockpit.
- Serving the phone port as `--name localhost --port 80` makes every assertion fail: browsers drop the default port from the origin, so it never matches. This fails closed.
- A `-p` session's last ledger stamp can stay `pending` when the session ends right after the call (the `hook_stopped` case); `levain consent ledger reconcile` re-reads the agent's record and stamps it (run 2026-10-18 on that case: the row was stamped `own_rules`, as the transcript records).
- `levain push`: Claude Code 2.1.303's Bash timeout moves the command to the background and sends it no signal, so the hold runs to its end and the agent reads the result when the command finishes (measured on that version; not re-run on others). A shell that sends SIGTERM withdraws the request; SIGKILL leaves it pending until it expires.
- Resuming an idle `--bg` session by stopping it first, then resuming it, is not run: such a session gets its ruling at its next hook step.
- Codex: input typed into an exec session opened before a halt runs no `PreToolUse` hook, so a halted session can keep typing into a shell it already holds; a patch sent through the shell reaches the gate as `Bash`, which levain never parses.
- A Claude Code session is resumed with an answer only while its transcript still shows the start directory and permission mode recorded when it asked; otherwise the answer waits for its next step.
- (Q-2, phone + cockpit) An answer sent from the phone page or a cockpit write token can resume an ended agent session with your words, with no passkey (ruled 26a: an answer authorises nothing; the resumed session keeps the permission mode it asked from).
- One delivery per session runs at a time. A delivery that waits more than 15 s on another one starts nothing and leaves the answer for the session's next step.
- Claude Code ignores a workspace's `.claude/settings.json` permissions until you have accepted its trust dialog for that workspace, so in a workspace you have not opened interactively the `ask` tool is refused. Claude Code's tool call carries no session id: the session comes from the environment Claude Code set when the server started, so after `/clear` in an interactive session a question may name the earlier session (not run).
- The Codex block records the levain home and Codex home that `levain init` or `levain update` saw; an update run from a shell with another `LEVAIN_HOME` repoints it.

## [0.11.6] — 2026-10-17

### Added
- **Away mode follows you on macOS.** Unless you have said otherwise, levain counts you away once your Mac has had no keyboard or mouse input for 10 minutes (`levain consent away auto --idle-after SECONDS` sets the time), and only while an enrolled key could sign an answer. `levain consent away on|off` still decides when you say it; `levain consent away auto` hands the choice back to the idle rule, and `levain consent away show` names what decided (including idle time that counts for nothing because no key could sign). A request held while you were away is released within a second or two of your return, of `away off`, or (when idle time made you away) of no enrolled key being able to sign, and the agent's own dialog then asks you. Off macOS levain reads no idle time: away is your word only. A settings file written by levain 0.11.5 or older keeps its mode (an `away off` stays off, and `show` and `levain doctor` say it was set before auto-away existed) until you run `levain consent away auto`; `levain init` now writes `auto`.
- **What the agent's own dialog did with a request levain left to it is now on the record, unsigned.** No hook sees a dialog's answer, so levain reads what followed from its own hooks (`PostToolUse`, `Stop`, `UserPromptSubmit`, and Claude Code's `PostToolUseFailure`; a new install, and an existing one after `levain update`): the tool ran (`ran`); it ran within half a second of levain stepping aside, as with Codex's approve-for-this-session (`ran_unasked`); or the session moved on to its next prompt or stopped without it running (`did_not_run`: a no, or abandoned, which levain cannot tell apart). `levain consent pending` lists the last day's requests levain left to the dialog under an UNSIGNED heading (with "no outcome seen yet" where none came), and the cockpit's per-request audit line (for a token holder) carries the outcome. An outcome never changes a request's answer or signature; one change of state follows from it: a request still held when its tool runs (you said yes in Claude Code's own dialog during the hold) is withdrawn, so the phone and `levain consent answer` can no longer answer it. The queue database gains a `dialog_outcomes` table on its next open. Known edges: when the tool call or the request carries no tool-use id, an identical request filed for another call can take the outcome; a repeated `PostToolUse` for a call already closed can mark another identical request as `ran`.
- **A standing consent grant can now name an MCP tool that may write, when it declares that tool's input fields.** `levain consent grant create ... --tool mcp__fs__write_file --path-field mcp__fs__write_file:path --mcp-field mcp__fs__write_file:content=str` signs a grant of the new `consent-mcp` kind: MCP does not say which of a tool's fields are paths, so you say it. `--path-field TOOL:FIELD` declares a field a target path; `--mcp-field TOOL:FIELD=KIND` declares one of kind `path`, `paths` (a non-empty list of paths), `str`, `strs` (a list of strings), `int` or `bool`; both repeat, and TOOL must be one of `--tool`. A request on a declared tool is answered only when its input carries declared fields alone, each value of its kind (a field it leaves out is not required), and every `path` value and `paths` item, together with the path fields levain reads for every tool (`file_path`, `path`, `paths`, `notebook_path`, `source`, `destination`, whatever you declared them), resolves inside the workspace; a declared tool whose input names no path is matched on the working directory alone. Anything else goes to you, as does every edit of agent config or hooks, whatever the grant declares. Measured with Claude Code 2.1.302 and the reference filesystem server (allowed the directory above the workspace): a write inside the workspace was answered, a write outside it and one carrying an extra `mode` argument were not. A declaration on one of the filesystem server's read tools replaces levain's own schema for it in that grant; a grant's built-in tools, and its MCP read tools left undeclared, are judged exactly as in a `consent` grant, and a shell tool or a tool levain does not know is still refused, a shell name as an MCP tool's own name included (`mcp__shell__exec_command`, in any case), at append and at every decision. Without a declaration `grant create` writes the `consent` kind as before, and naming an MCP tool that may write is refused as in 0.11.4, now with a line saying how to declare it. `levain consent grant list` prints each grant's declarations. Any change to a tool's declaration, and any change between the two kinds, is a loosen (a laptop-presence signature); a renew keeps the declarations.
- **Known edges, `consent-mcp`:** a field you declare `str` is never read as a path, so declare as `path` every field the server reads one from (a `str` field holding `/etc/hosts` is allowed); a relative path is checked against the session's working directory, while the server resolves it against its own. An MCP tool that runs commands under a name levain does not know as a shell (a server's `run`, say) is answered by a grant that declares it: your signed declaration, as with a field declared `str`. A `consent-mcp` grant is held inert by levain 0.11.5 (listed as a grant kind it does not know, answering nothing, its other grants unaffected; measured). **levain 0.11.4 and older refuse the whole authority log once it holds one**: every grant on it answers nothing there, the plain `consent` grants included, `levain consent grant list` fails with "the authority log is refused", and revoking the grant does not undo it, since its link stays in the log (0.11.4 measured; older releases read the same way in their code). Upgrade every levain that shares the levain home, or the cockpit, before creating one.
- **The Decisions view offers a standing-rule proposal card.** When you have allowed the same agent, tool and working directory by hand three times in the last 14 days, it shows the one `levain consent grant create` line (7 days, `default` permission mode) that would make that a grant. The page signs nothing; the card is text and a copy button, shown only to a page holding its token. Nothing the log would refuse is proposed (a shell tool under any name, a tool levain does not know for that agent, an MCP tool off the read-only list, a tool name of a shape the log does not take, a request at the agent-config tier, a working directory that no longer exists, or that is `/`, your home directory or above it, once symlinks are followed; the card names the resolved directory); any denial of the same agent, tool and directory in the window withdraws the card, and a key whose latest decision was answered by a standing grant shows none. The evidence is counted per key in the queue database over the whole window, so no number of newer requests of other keys can hide a card or its denial; at most three cards are offered, the most hand-allowed key first, then the most recent, and the page draws every card offered. A Decisions panel whose data errored shows no card, and a reload of the dashboard reads the cards again, so a denial or a grant answered since withdraws one. Known edges: the card counts hand-allows by agent, tool and working directory, but the grant it proposes answers only requests whose targets are inside the workspace, so approvals of out-of-workspace targets can yield a card whose grant would not cover them; after the grant a card proposed expires, the card stays hidden until your next hand-allow; the per-key count does not re-check each request's seal, so a same-user tamper with the queue database could raise a card (the page signs nothing, and the grant still needs your signing key at a terminal).
- **Known edge, the proposal card (planned fix):** every grant the card helps create is a class of action you stop seeing. The gate itself stays intact, but the choices you are offered drift toward your own yes-pattern. A periodic digest of what grants answered unseen (planned, a later 1.0 row; not shipped) is what closes it.


### Fixed
- **A row you opened in the cockpit stays open when the page redraws.** A store write (a seat adding or touching a spore), the refresh button or returning to the tab redrew the board and closed every expanded row and reset each panel's scroll; now the rows you opened, and each panel's scroll, come back, in the board, the focus view and a panel's own refresh, keyword search answers included. Known edges: a row is matched by its id inside its panel, so a row that is gone stays closed; a panel whose rows arrive late (a store search) keeps what you held for 15 seconds; a focus view rebuilt twice before its search answers keeps only the second rebuild's state.

## [0.11.5] — 2026-10-17

### Security
- **A standing consent grant now names only a tool levain knows for its agent, and never a shell under any name.** Codex 0.162.1 asks before it types into a terminal it launched outside the sandbox as `write_stdin`, not `Bash`. In 0.11.4 `levain consent grant create --agent codex --tool write_stdin` was accepted, and that grant answered a `touch` typed into `/bin/sh` with no dialog (reproduced with real Codex). The log now refuses a grant naming any shell name levain knows (`Bash`, `PowerShell`, `write_stdin`, `shell`, `exec_command`, `shell_command`, `local_shell`, in any case), and any tool not on levain's list for the agent: for Claude Code `Edit`, `Write`, `MultiEdit`, `NotebookEdit`, `Read`, `Grep`, `Glob`, `LS`, `NotebookRead`, `WebFetch` and `SendMessage`; for Codex `apply_patch`; for either, the filesystem server's MCP read tools. `WebFetch` and `SendMessage` name no path and are matched on the working directory alone, so a `SendMessage` grant answers every send from the workspace, one that resumes a subagent included. A tool an agent adds in a later release, or Claude Code's `Task` (which runs a subagent on any prompt), now comes to you rather than to a grant. A grant already signed on any other tool answers nothing for it and cannot be renewed: revoke it and create a new one. `levain consent grant list` marks it.

### Added
- A grant of a kind this levain does not know (one a newer levain wrote) is held inert: the authority log still replays, the grant answers nothing, `levain consent grant list` shows it and `levain consent grant revoke` removes it; this levain never creates or changes one.
- **Known edge:** levain 0.11.4 and older refuse, for good, an authority log that holds a grant of a kind they do not know. Their hook then stays silent and Claude Code's own dialog asks you every time (with away mode on, the phone gets a request it cannot answer); `levain consent grant list` shows the refusal. Revoking the grant does not undo it: upgrade every levain that shares the levain home.

## [0.11.4] — 2026-10-17

### Added
- **`levain consent away [show|on|off] [--agent claude_code|codex|all] [--hold SECONDS]` sets away mode without editing a file.** Away mode is what lets a signed answer reach the agent: off (what `levain init` writes), the consent hook answers only by a standing grant and otherwise leaves the agent's own dialog to ask you at once, so `levain consent answer` and the phone have nothing to answer; on, the hook holds each request no grant answers for up to the hold (more than 0 and at most 840 seconds, the longest the hook waits; default the file's hold, else 300 for Claude Code and 120 for Codex) so you can answer it. Claude Code shows its own dialog during the hold as well (an allow from the hook dismisses it); Codex shows no dialog until the hold ends, so for Codex away means away. `on` refuses when the install's cockpit has no enrolled key to sign an answer (or the settings name no cockpit, or it does not replay). Before this the only way to turn it on was editing `<levain home>/consent/claude_code.json` or `codex.json`. The verb changes only `away_mode` and `away_hold_seconds` in those files, keeps every other key, never creates one (`levain init` does, naming the install's cockpit), refuses a symlinked file or a queue directory that is not 0700 and yours, and replaces the file atomically; `show` (the default) prints each agent's mode and hold as its hook reads them, and whether an answer could verify, and writes nothing. Away mode grants nothing: every answer still verifies as a signature. Both hooks and this verb now read the settings file through one reader: a hold written as an integer too large for a number, or a file nested too deep to parse, reads as the defaults instead of stopping the hook (which then gave no decision). Known edges: two `away` runs at the same instant can each write, the last one winning; a Claude Code request you answer in the dialog stays listed on the phone until its hold ends, and an answer to it there does nothing; `on` checks that some enrolled key can sign, not that one covers T3, so with only a key that cannot sign T3 an edit of agent config waits out its hold; a hook installed with a shorter `--max-hold` than the default waits less than the hold `show` prints.


### Fixed
- **A standing consent grant on an MCP tool could answer an edit of agent config or hooks when the server names its path in a field levain did not know.** Edits of agent config and hooks (Claude Code and Codex settings and hook files, `.mcp.json`, `~/.claude.json`, plugin hooks, CODEX_HOME, levain's own installed code) go to you, never to a grant; for an MCP tool levain read only the fields `file_path`, `path`, `paths`, `notebook_path`, `source` and `destination`, so a grant on, say, a write tool taking `filepath` allowed a write to `.claude/settings.json` (reproduced with a real signed grant through the Claude Code hook). Every string key and value in an MCP tool's input (an object or not), at any depth, is now checked as a path, as given and also cut at a NUL, trimmed of whitespace, percent-decoded, read as a `file:` URI (its host, query and fragment dropped), these in any combination, and with `./`, `..` and repeated slashes collapsed (as a server's own path resolving would), and a string naming a directory that holds agent config or the code the consent hook runs (a `.claude`, `.codex` or `.claude-plugin` directory, your home directory, a levain install's `activation/hooks`, levain's own package, the directory holding the cockpit's authority database, or this Python's site directories, where a `.pth` file runs at every interpreter start), or a file called `.claude.json`, also goes to you (a server can take the directory and the file name in two fields). An input with more than 4096 strings or 16384 values, nested more than 64 deep, or whose spellings total more than 256 KiB goes to you. A request whose text merely names such a file (a search for `.mcp.json`) now also goes to you.
- **Known edges:** this check is best effort, because MCP gives a client no way to know which of a tool's fields are paths (a tool's annotations are untrusted and access control is the server's): levain cannot see a path an MCP server builds from something no input string holds whole: a root directory the server was configured with (except for `.claude.json`, matched by its name anywhere), or an encoding other than the ones above, and a string padded past 64 KiB that is still that long once `./`, `..` and slashes are collapsed is not decoded (one carrying an encoded dot or slash or a `file:` scheme goes to you). A directory in one field and a path below it (not just a file name) in another, such as `activation` and `hooks/x.py`, is not joined.

### Changed
- **A standing consent grant can no longer name an MCP tool that may write.** A grant's workspace bound reads the six path fields above, and MCP gives levain no way to learn which of a tool's fields are paths, so since 0.11.0 a grant on an MCP tool taking its path in any other field (such as `filepath`) answered that tool's writes anywhere, `~/.zshrc` or a workspace's `.git/hooks` included. `levain consent grant create` now refuses an MCP tool unless it is one of the filesystem server's read tools (`read_file`, `read_text_file`, `read_media_file`, `read_multiple_files`, `list_directory`, `list_directory_with_sizes`, `directory_tree`, `search_files`, `get_file_info`, `list_allowed_directories`, under any server name), and any other tool name containing `__`, before anything is signed; a grant already signed on any other MCP tool answers nothing and cannot be renewed (`levain consent grant list` marks it), so those requests come to you. A request on a granted read tool is answered only when its input is one the filesystem server's tool accepts (its required fields, no others, each of its type: `path`, `paths`, and `head`, `tail`, `sortBy`, `pattern` or `excludePatterns` where it takes them), so another server's `read_file` taking its path in another field goes to you. Known edges: a server that names a writing tool after one of those read tools and takes the same fields is believed (it is your own install); a relative `path` is checked against the session's working directory, while the server resolves it against its own.
- **On Linux, bash can no longer connect to a unix socket outside its sandbox unless you granted it.** levain reads the running kernel's Landlock version and applies the strongest policy it supports: on Landlock ABI 9 and later, pathname and abstract unix-socket connects to endpoints created outside the sandbox are refused, and so are signals to processes outside it; on ABI 6 to 8 abstract sockets and signals are closed while pathname sockets stay reachable; below ABI 6 (or where the kernel refuses Landlock) there is no socket-level claim, and `levain run` and `levain doctor` say so. Before bash starts, a self-test checks the policy against a canary socket, and a launch whose policy does not hold is refused by name.
- **Socket access is now something you declare.** The sockets bash may reach are listed in `<levain home>/confinement/grants.json` (owner-only, outside the entity's reach); each one is checked at launch (a real socket you own, in a directory bash cannot change) and printed by `levain run` and `levain doctor` as a named waiver. `/dev/log` is granted by default. The automatic bind-backs are gone: your gpg-agent, keyboxd and ssh-agent sockets no longer come back into bash, and `runtime_sockets` in `.levain/confinement.json` no longer grants anything (doctor reports it as ignored). **gpg signing from bash** now goes through the gpg-agent's extra socket, which you declare in the grant file. **The ssh agent:** on Linux each session's bash gets its own `ssh-agent`, started by levain outside the sandbox, holding only keys you add from your own shell with `levain ssh-add <key> -h <host>` (hosts checked against `<levain home>/ssh/known_hosts`, a lifetime of 8 hours unless you set `-t`); `levain run` and `levain doctor` print the agent and each key with its hosts and time left. Your own agent is never passed to bash. A grant of your ssh agent is refused: a path that is the same socket as the one your `$SSH_AUTH_SOCK` names or gpg-agent's ssh socket (under any name, a hardlink included), or, on Linux, any socket an `ssh-agent`, gnome-keyring or gpg-agent is listening on, except gpg-agent's extra socket at the exact path gpgconf names (to read what listens, `levain doctor` and each launch open one connection to every granted socket and close it without sending anything). Known edge: this refusal guards against a grant made by mistake, not against a process running as you outside the sandbox, which can replace sockets in your own gpg directory. Known edge, off Linux only: an agent is recognised only as the socket `$SSH_AUTH_SOCK` names in levain's own environment or as gpg-agent's ssh socket, so an agent at any other path, or yours when levain was started without `$SSH_AUTH_SOCK`, is not. macOS is unchanged in this release, and its grant lines say the grants are not applied there yet.
- **gnupg's secret keys are now behind the floor**, read and write, in every gnupg home levain knows, on Linux and macOS. On Linux, inside bash, `private-keys-v1.d` lists as empty rather than failing, and gpg without a granted agent socket reports "No secret key". A gnupg home levain knows is the default one and the one in levain's own `$GNUPGHOME` when that is an absolute path; a gnupg home elsewhere that nothing names is not covered. If that home still holds an unmigrated legacy `secring.gpg`, gpg in bash exits 2 even when the signature is made (so `git commit -S` fails); one `gpg -K` outside bash migrates it.
- **Known limits of the session ssh agent:** on macOS bash still receives your `$SSH_AUTH_SOCK` and no session agent starts, until levain's macOS socket rule lands; a levain that is killed leaves its agent running until the next levain start removes it; a key added with `--no-destination-limit` can sign non-login data and works from any host it is forwarded to (a `-h` key signs only logins to its hosts, so there is no ssh commit signing through one); the session's keys can be listed, removed or locked from bash; plain `ssh` in bash (not git) still reads `~/.ssh/known_hosts`; if the agent dies or is restarted during a session, bash ends for that session (every later command and respawn is refused until the session is started again); the OpenSSH version check reads `ssh -V`, so an older `ssh-agent` elsewhere on `PATH` refuses the first `-h` add with OpenSSH's own error.

## [0.11.3] — 2026-10-16

### Added
- **`levain messaging set isolate|governed|off|unmanaged` changes an install's messaging choice without re-running `init`; `levain messaging` (or `messaging show`) reads it.** `set` holds the install lock for the whole run, checks the choice first (claude-code adapter only, a readable `.levain/config.json`; nothing is written on a refusal), runs `levain update` for the install with the choice passed explicitly (the same renderer `init --messaging` uses, so the settings file is the same bytes a fresh `init --messaging` of that choice gives, loosening included; a plain `levain update` still never loosens), and records the choice in `.levain/config.json` only when `.claude/settings.json` carries exactly that choice's controls and the install's hook pin covers that file (also when the update stopped on an error after writing it, or held another file for review). Otherwise nothing is recorded and `set` says why; nothing is rolled back, and the next plain `levain update` re-renders the recorded choice (it keeps a tighter file and re-tightens a looser one). If `.claude/settings.json` was edited by you, update stages its version under `.levain/pending/`; `set` then says the choice is not applied and not recorded: merge the staged copy, then run `set` again. A successful change writes `messaging_changed` (`from`, `to`, `at`) beside the record, `levain doctor`'s messaging row ends with "(changed from X <time>)", and `set` prints `LOOSENED X -> Y` when the new choice is looser (strictest first: off, governed, isolate, unmanaged), or `REPLACED custom messaging controls -> Y` (stamped `from: custom`) when the file's controls matched no choice. `show` prints the effective choice (recorded; or, with nothing recorded, the choice the file's controls exactly match, as `<choice> (inferred from .claude/settings.json; no record)`; `custom (no record: ...)` when it carries messaging controls that match no choice; else `isolate`, the default) and whether the workspace's settings carry its controls; it reads only the workspace settings file (doctor reads every layer) and writes nothing. `--path` defaults to the current directory.
- Known edge, `messaging set`: an agent with shell access can run `levain messaging set unmanaged` as it can `levain init --force --messaging unmanaged`; levain records and shows the change (the stamp, the doctor row) and does not stop it. A workspace carrying an extra hand-added messaging control (a deny on another tool, say, beside the choice's own controls) reads as not carrying the choice, because the comparison is exact: `set` then records nothing and `show` reads it as custom.

### Changed
- The phone consent page (`levain consent phone`) now matches the cockpit's look (palette, type, card shapes, buttons; light and dark by the phone's setting); nothing it shows or signs changed. Known edges, phone page look: the waiting card's accent top line stays on a card already answered until you refresh (the page's script draws it, outside a look-only change); in light mode allow and deny differ mainly by hue, so the words "allow" and "deny" are always spelled out.


### Fixed
- **Security: a confined `levain run` entity could read and rewrite the consent queue, and forge the cockpit authority it is checked against.** The confinement floor hid `<levain home>/autonomic` and `<levain home>/keys` by name; the consent queue directory (`~/.levain/consent`, or under `$LEVAIN_HOME`), added later, was not among them, and neither was the cockpit data root its settings name (`<install>/.levain/cockpit`, which holds the authority log and its anchor). An entity's shell could read every Claude Code and Codex consent request, rewrite `claude_code.json` or `codex.json`, or replace that cockpit's authority log and anchor together with its own key, after which the consent hook allowed requests by the entity's grants and answers (measured). The floor now denies the whole levain home, read and write, as one subtree, so a directory levain adds there later is covered from the start, and it denies every cockpit data root the consent settings name. Upgrade if you run entities with `levain run` on a machine whose Claude Code or Codex installs use the consent hook.
- Known edges, the floor and the levain home: (1) when the levain home holds the entity itself (an install that lives at `~/.levain` and is run as an entity) or its workspace, or `$LEVAIN_HOME` is your home directory, the home cannot be hidden whole without hiding the entity, so the floor hides its `autonomic`, `keys` and `consent` directories by name instead; a directory a later release adds there is covered in that layout only once that release names it. (2) The cockpit roots are read when the floor is built: one a `levain init` names while an entity's session is running is denied from that entity's next session. (3) A queue or key directory you place outside the levain home (`--queue-dir`, `levain cockpit key create --dir`) is not on the floor; the defaults are. (4) A Claude Code or Codex session itself is not confined by levain and runs as you, so it can write these files whatever the floor says, as before. (5) A relative `$LEVAIN_HOME` is taken against the directory `levain run` starts in, while a hook takes it against its own; set it to an absolute path. (6) The floor refuses to start while a consent settings file is not a regular file levain could have written, or names its cockpit through a symbolic link; `levain init` writes the real path. (7) On Linux, a consent settings file naming a cockpit directory that no longer exists (its install was deleted) has the floor create an empty placeholder for it while a session runs, or refuse to start when its parent cannot be written; remove the settings file, or run `levain init` from the install that should answer.
- The Codex adapter's README and hook docstring said Stop is excluded; Codex's Stop event is wired (it passes the session transcript), and both now say so.

## [0.11.2] — 2026-10-16

### Added
- **`levain init` wires the consent hook for Claude Code and Codex, away mode off.** A new install, and an existing one after `levain update`, runs levain's `PermissionRequest` hook: a permission the agent would ask you for is answered at once by a standing consent grant or a signed answer the queue verifies, and otherwise left to the agent's own dialog, as before. The hook files a request on the consent queue only when something could answer it: away mode is on, or the cockpit its settings name holds an enrolled key or a standing grant; otherwise (a cockpit made before any key was enrolled included) it files nothing and the dialog shows. Away mode stays off unless you turn it on, so nothing waits: with no grant and no answer the hook prints nothing and the dialog shows at once. With away mode on, a request nobody answers in time also falls back to the agent's own dialog; it is never denied or allowed for you. The hook is a script in the install's `activation/hooks/` (`consent_claude_code.py` or `consent_codex.py`), wired through the same settings file (Claude Code: the install's `.claude/settings.json`) or `hooks.json` (Codex) as levain's other hooks, so `levain update` keeps it current, a file you edited is staged rather than overwritten, and the hook pin covers it. Init also creates the consent settings file in levain's consent queue directory (`~/.levain/consent/claude_code.json` or `codex.json`, or under `$LEVAIN_HOME`) naming this install's cockpit as the one grants and answers verify against, with away mode off; an existing settings file is never changed. Init and `levain doctor` warn when the installed agent is not a version the hook was tested with; the hook is wired anyway (it prints a decision only for a verified grant or answer), and `levain consent canary --agent claude_code|codex` checks that version. `levain doctor` gains three rows per agent: whether the hook is wired to this install, away mode (for Codex with away mode on, a WARN: Codex shows no dialog while the hook holds, only "Running hook", then its own dialog), and which cockpit grants verify against.
- Codex specifics: Codex runs the hook only after you trust it at its "Hooks need review" screen at the next start (levain never writes that trust; until then Codex does not run it and shows its own dialog). The hook is in Codex's machine-wide `CODEX_HOME/hooks.json`, like levain's other Codex hooks, so it runs in every Codex session on the machine, whatever the directory: once a request is filed (above), every Codex approval files a queue row, the request as Codex filed it, kept in the consent queue directory for good, with secrets inside free text (a shell command) not masked. A grant answers only inside the workspace it was given for.
- Known edges, consent hook: (1) one consent settings file per agent per machine (per `$LEVAIN_HOME`): a second install keeps the first install's cockpit, and init and doctor say so. (2) The hook reads the consent queue of `$LEVAIN_HOME` as the agent sees it; an agent started with a different `$LEVAIN_HOME` than init's reads a queue with no settings, so it shows its dialog. (3) Deleting an install, or switching it with `init --force` from Codex to Claude Code, leaves the machine-wide Codex entry naming a script that is gone; it does nothing (it exits 0 and Codex shows its own dialog) until another `levain init --adapter codex` replaces it. (4) A grant can verify only once this install's cockpit holds enrolled keys. (5) `levain update` keeps the recorded choice and has no flag of its own: to turn the hook off on an existing install, run `levain init --path <install> --force --consent-hook off` (`--answers <install>/.levain/answers.json` skips the interview). A `.levain/config.json` that cannot be read, or a `consent_hook` value other than `on` or `off`, reads as off until init records a choice again. (6) `levain update` creates a missing Codex settings file only while the machine-wide `hooks.json` runs this install's consent hook; a command there that merely names this install's consent script counts as running it, as `levain doctor`'s wiring rows read it.
- **The cockpit's Tray can list the items it is holding back.** The Tray still opens showing only what is due, with the "N held back — surface date not reached" line; that line now carries a "show" control, and "show" lists the held items under the due ones, each marked "surfaces Oct 22", with the same verbs any Tray item has (edit, schedule, dismiss, so you can reschedule or edit something that is not due yet). The filter box searches them while they are shown. While they are hidden, a filter that matches only held items says how many match and offers "show". Whether they are shown is kept in this browser only, hidden by default. The terminal views and `levain`'s text output are unchanged: they still print the count and never list the held items.
- Known edges, held Tray items: (1) on an install that has not enabled the reported class, pulling a held item forward to today or earlier is refused as it was before ("needs the broker"); moving it to a later date works. (2) A Tray with more than 5,000 held items lists the first 5,000 and says so. (3) More than 5,000 held items keep the order the store holds them in, not the soonest-surfacing first, and the partial banner calls the dropped tail "unreadable". (4) The panel's `match_label` (the `?q=` form of the panel read, which the page does not use) counts due rows only: held matches are returned under `held` but are in neither number. (5) A panel provider other than the Tray that declares `held` has those rows partitioned out of its rows, and no page draws them (only the Tray sets it). (6) A Tray whose panel kind is not a list or triage list does not show the "held back" line.
- **Choose how your agent gets the memory wrap procedure: loaded at the start, or read when it wraps.** The memory instructions are split in two. `seed/memory.md` keeps what the agent does every turn (start of session, what to record, the recording cadence, recall before decisions); the new `seed/memory_workflow.md` holds the wrap procedure (`prepare_wrap -> compress -> save_continuity`, the pre-compose recall, the contradiction scan, cold-pattern routing, durable facts, what the citation check does and does not catch). By default both load together, as before, so nothing changes until you ask. `levain init --memory-load on-demand` takes the procedure out of the always-loaded context: the agent's carrier (`CLAUDE.md` or `AGENTS.md`) then carries a short summary of the wrap and points at the file, which the agent reads before it wraps. `--memory-load claude-code=on-demand` (or `codex=on-demand`) sets one agent, and may be given again; a bare mode sets the install's default. The choice is kept in `.levain/config.json`, so `levain update` and `init --force` keep it; give the flag again to change it, or set `memory_load` in `.levain/config.json` and run `levain update`. `ADAPTER=MODE` for an agent the install does not carry is refused before anything is written. `levain doctor` shows one `memory workflow` row per install, `eager` or `on-demand (summary in the carrier)`. `levain update` brings the trimmed `seed/memory.md` and the new file into an existing install, and now installs a seed file a release adds even when the carrier would import it (it used to hold the carrier back on a file that was not there yet, and nothing wrote the file); a `memory.md` you edited is staged under `.levain/pending/`, as every seed file is.
- Known edges, memory load: (1) whether an agent in on-demand mode opens `seed/memory_workflow.md` when you say "wrap" has not been run against a live Claude Code or Codex session; the summary tells it to, and that is all that is known. Eager stays the default for that reason. (2) A bare `--memory-load MODE` sets the install default and keeps any per-agent entries already recorded; give `ADAPTER=MODE` to change one. (3) If you edited `seed/memory.md` before this release, `levain update` keeps your copy and stages the new one; until you merge it, your copy still holds the whole wrap procedure and the carrier also imports `memory_workflow.md`, so the agent reads the procedure twice (`levain update` says which block to remove when you merge). (4) `levain update` does not print the mode in force; `levain doctor` does. (5) Doctor's carrier check reads an import or pointer line wherever it appears, so one that sits only inside a code fence or an HTML comment counts as loaded.
- `levain doctor` now also reads your carrier for the seed files your install's memory-load mode expects it to load: an eager file with no import, an on-demand file with no pointer. It fails the `CLAUDE.md freshness` (or `AGENTS.md freshness`) row and names the file, so a carrier you edited that predates `memory_workflow.md` no longer reads as healthy; `levain update` also says so in one line when it stages your carrier and your copy leaves that file unloaded. `levain init --memory-load ...` on an OpenHands install is refused before anything is written, since that adapter has no carrier to load a seed file into.

## [0.11.1] — 2026-10-16

### Added
- **Answer an agent's consent request from your phone, with a passkey.** `levain consent phone` serves a small consent page on its own loopback port (default 7428) for an https proxy you run to put on a name your phone reaches (`--tailscale` finds this machine's tailnet name and prints the `tailscale serve` line; `--name` names any other proxy's). From the page you read each waiting request (the agent, the tool, the request as filed, where) and answer it with "allow with passkey" or "deny with passkey". The phone signs the exact answer record; the phone port turns the WebAuthn signature into an OpenSSH one and the queue verifies it with `ssh-keygen` against the cockpit's authority log, as for `levain consent answer`. The page also makes the phone's passkey and shows the laptop commands that enrol it; a passkey never enrols itself. A new custody word, `passkey`, is for a `display-phone` key only: it allows up to T2 and denies any request, so a T3 row offers deny only and a T3 allow is refused by the server. The phone port serves nothing else (not the cockpit, not the install), and its link carries its own token, which opens nothing on the cockpit.
- Known edges, phone answers: (1) the passkey prompt shows no request text; the page shows it and the signature covers exactly that request. (2) A passkey is made for one https name; a renamed machine or tailnet needs a new one. (3) Anyone holding the phone link who reaches the name can read the waiting requests until the phone port stops. (4) The page is served by your own user account, and levain does not confine a Claude Code or Codex session, so answer only on a phone port you started. (5) When more than one passkey is enrolled on a name and this browser made none of them, the page asks which one, and the phone's own prompt may then offer another; that answer is refused at verify, never recorded. (6) An iCloud or Google passkey syncs with its account and reports no signature counter, so a cloned one cannot be detected. (7) The phone port answers every connection that reaches it before the token is checked, so a device on your tailnet can hold connections open (each times out after 30 seconds); it reads and answers nothing without the token. (8) Cancelling the phone's passkey prompt reads to the page the same as a passkey the phone no longer holds, so the page forgets which passkey this browser made; with more than one passkey enrolled, the next answer asks which one.


### Fixed
- **A deleted or moved Codex install no longer blocks every Codex prompt on the machine.** Codex reads its hooks from one machine-wide `hooks.json`, and levain's commands there ran `python <install>/activation/hooks/<hook>.py`. When that install was deleted or moved, python exited 2 on the missing script, and Codex treats a hook's exit 2 as a block: every prompt in every Codex session showed "Blocked by hook" and never reached the model (measured on codex-cli 0.162.1). The commands now run `python -EP -m levain.hookrun <script>`, which exits 0 and prints nothing when the script is gone and turns any other failure of the hook into exit 1, which Codex shows as "Hook failed" and carries on. A missing interpreter (exit 127) or a venv without levain (exit 1) carries on too. The hooks now need levain itself importable from the interpreter they name, and they ignore `PYTHON*` environment variables such as `PYTHONPATH` (`-E`); `levain doctor` warns when the hooks name an interpreter other than its own (it never runs one to check), and `levain verify-hooks` runs each hook the way Codex does. Run `levain update` in the install Codex uses to rewrite its `hooks.json`; if that install is already gone, remove or replace the levain entries in `~/.codex/hooks.json` (or run `levain init --adapter codex` from the install Codex should use).
- Known edges, Codex hooks: (1) an interpreter of Python 3.10 or older at the path the hooks name (a venv rebuilt in place on an old Python) exits 2 on `-P` and blocks Codex; levain itself needs 3.12, and `levain doctor` warns that the hooks name an interpreter other than its own. (2) The hook pins cover the install's hook scripts and hooks.json, not the levain and anneal-memory packages the hooks import; a process running as you can change those, as it can any installed package.
- **A spore action refused because the spore moved now refreshes the view.** When a Tray, Keep or loop row was moved to another list on another surface (the TUI, a second browser tab, the agent) after you saw it, compost, remove, promote, re-tier and re-route were refused (nothing was written), but the cockpit page and the TUI kept showing the old row, so the same click was refused again until a manual reload. The refusal is now "stale" on every path, and both surfaces re-read on it: the TUI rebuilds at once, the page re-reads after showing the refusal for a moment. Every row-based promote and re-route now sends the row you saw and is checked against it before anything else, so a spore moved elsewhere is refused as stale rather than promoted or moved over the other change; a re-read the page could not run at once (another editor open, a job running) is kept and runs as soon as it can.
- Known edge: a spore's surface-at date (`next`) is written last-writer-wins. Two surfaces setting the date of one spore at the same moment keep the later one; nothing else on the spore is affected.
- **The cockpit keeps your place when it reloads.** A board reload (the refresh button, coming back to the tab, a stream reload) emptied the page and left it at the top; it now puts the page back where you were. Re-focusing a panel's filter box after a reload, or the `/` shortcut on a box already on screen, no longer moves the page; `/` on a box scrolled out of view scrolls to it. A panel read that started before a reload can no longer repaint the newer board with its older read; when that happens the board reloads instead.
- **The hero weather tile has a refresh button.** It re-reads only the weather panel; the source decides how fresh that read is, and the tile's "read ... ago" line says.
- Known edge: a panel whose body scrolls inside itself (a long list clamped to the card's height) goes back to the top of that list on a reload; the page itself keeps its place.

## [0.11.0] — 2026-10-16

### Added
- **Decisions waiting shows agents' consent requests, read-only.** A Claude Code or Codex tool call waiting on you now appears under "Agents waiting on you" (counted, first) with the agent, the tool, the request as filed in a monospace block, its working directory and targets, a live countdown, and a copy button for the `levain consent answer ... allow|deny` command to run in a terminal; requests decided in the last day list under Resolved with how they were answered. The request's details (what it asks, its working directory and targets, the answer commands, how it was answered) need the page's token on every bind, loopback included: the decisions panel itself carries only the agent, the tool, the tier, the state and the times, and a page opened without its token says it carries no token, so request details are not shown. A decided request shows its state and times from the panel row without a request fetch; its "details" button fetches the request and shows how it was answered (the audit line), and Resolved lists the newest decision first. A request whose input is too long to show offers no commands and says to answer it in the agent's own dialog. The page reads the queue without opening it for write and answers nothing; it reads at most 200 waiting requests (the soonest to expire; past that it says there are more than 200) and the last 50 decided ones, and a row whose seal or timestamps do not check out is skipped and counted. Known edge: a decided request's state and the key that answered it are shown as the queue row stores them, not re-verified against the signed answer.
- **A Sessions tile on At a glance.** It counts the agent sessions (Claude Code and Codex) whose levain hooks fired in the last 15 minutes and opens into the focus view: Active, then Quiet (15 minutes to 24 hours; older sessions are not listed). Each row names the agent and the working directory's last folder, when the session last fired a hook ("last active 3m ago"), Claude Code's sandbox as the settings files configure it for that directory ("sandbox configured on (project settings)", "sandbox configured off"; Codex says "sandbox not read"), whether the hook delivered the entity's memory at session start ("memory loaded at start" or "memory not loaded"; nothing when unknown) and how many consent requests that session has waiting. When two listed sessions share the agent and the folder, each of those rows also says when it started ("started 40m ago"); two whose starts round to the same minute (past an hour, the same hour) still read alike. The view's footer repeats what the settings readers cannot see (`CLAUDE_CONFIG_DIR` relocation, `--settings`, server-managed settings). It reads this install's `.levain/sessions/` only, the newest 50 records, each at most 16 KiB; a file that is not the registry shape is skipped and counted. The tile and panel carry no session id and only the folder name, never the full path.
- Known edges, Sessions: (1) the session files are a display, never an authority. Hooks write them as your own user, so any process running as you can forge one; nothing in levain reads them to decide anything. (2) The words say what was measured: no session-end hook is installed, so a silent session goes Quiet, never "ended", and "last active" is the last hook event, never proof the agent is running; sandbox is "configured", never "confined". (3) Only this install's sessions are shown; a session under another install does not appear (the documented default is one global install). (4) Two hooks firing for one session at the same moment can lose an update, so the memory word may be missing for it and "last active" can step back by seconds; nothing wrong is shown. (5) Past 200 waiting consent requests a session's waiting count can read low.
- **A session's transcript, from the Sessions view.** Each session row has a "transcript" button that shows, oldest first, what levain's own hooks recorded for that session: your prompts, the agent's final reply each turn, each tool it ran (the tool's name, plus a shell command's first line, a file or notebook tool's path, or an `apply_patch`'s first file), and the consent requests that session filed with how each was answered. Above it is the command that reopens the full session in the agent itself, with a copy button: `cd -- <dir> && claude --resume <id>` or `codex resume <id>`. The vendors' own session files are never read. The transcript names the session id, the full working directory and your words, so it needs the page's token on every bind, loopback included; the Sessions panel itself still carries none of them.
- **What this keeps at rest.** Claude Code and Codex installs now run more levain hooks, `Stop` and `PostToolUse` (and on Claude Code `PostToolUseFailure`, so a tool that ran and failed has its line too); they only record and print nothing. They write `<install>/.levain/sessions/<id>.log.jsonl` (owner-only, already git-ignored) beside the session file: each text cut at 4,000 characters, each log kept near 256 KiB by dropping its oldest half, the newest 200 sessions kept. Your prompts and the agent's replies are in it as written. Delete `.levain/sessions/` to clear it. A Codex install shows Codex's "Hooks need review" screen once after this update, because its hook list changed.
- Known edges, transcript: (1) it is a display, never an authority: hooks write it as your own user, and nothing in levain reads it to decide anything. (2) A tool the agent was refused leaves no tool line (the agent fires no PostToolUse for it); a refusal that went through levain's consent queue shows as its consent line. (3) If a hook cannot take the sessions lock within about a quarter second, that one event is not recorded. (4) A symlinked `.levain` or `.levain/sessions` directory is followed, as the Sessions view does: whoever can plant it already owns the install. (5) The hooks' locking and no-follow opens assume macOS or Linux, the platforms levain supports.
- **An At a glance tile that has more than fits opens all of it.** Health, Ops and any other glance tile that shows "+N more" is a button like Decisions waiting: a click (or Enter) opens the focus view listing every item, each metric with its status, value and unit, with a filter box once there are eight or more. No glance tile draws past its own edge any more: each draws as many items as fit its height on the desktop and the phone, and the "+N more" line is always whole.
- **Decisions waiting and Sessions open with a filter box.** Their focus views carry a filter from the first row, not only past eight: Decisions narrows by the agent and tool, the request's state word and its tier; Sessions by the agent, the folder and the row's chips ("1 waiting", "memory loaded at start"). It filters what the view already shows; no panel gains a field.

- **The cockpit has a Team zone, read-only.** When your install belongs to a team ledger, a fourth zone beside Work, Mind and Identity shows what the ledger says: Members, Waiting on you, Where agents were stopped, Held by nobody, In force and Joins pending, plus a Sync card (the remote, the last fetch and its error, integrity problems, canon status). It reads the ledger at most once per refresh and fetches nothing. A solo install shows one card saying it has no team ledger; a ledger this machine cannot read shows one card naming why, in words (git's own output stays out of the page and in `levain team status`); a ledger only partly judged here shows every pane with the reason beside them. Members lists each member by handle, the owner first: their role, whether a signing key is in force, waiting for them to confirm it, or missing (a member with no key cannot sign), and since when they hold the role when the ledger records it. Joins pending lists each proposed key that waits for its member to run `levain team key confirm` on their own machine (with its fingerprint) and an ownership offer that waits for `levain team accept`. No teammate's email address is shown.
- **The Team zone's cards size to their rows.** On the desktop each Team card is as tall as its rows (its usual height is the cap, past which it scrolls) and the cards in a row line up at the top, so a pane with three short rows no longer leaves a tall empty card. Anywhere in the cockpit, a date in a row's text (2026-10-15) no longer breaks across two lines at its hyphens.
- **The Team zone has two actions: Fetch now and accept.** *Fetch now* on the Sync card fetches the team ledger from its remote and rebases onto it, once, on bounded waits; it never pushes, and it does what the team hook already does on its own every interval, so it fires on one click in every install (a standing policy may still raise it). *Accept* on Joins pending appears only while a join waits on this machine: this machine's own key proposed for a member, or ownership offered to this machine's member. It brings that key into force or takes that ownership, the team's trust-root acts, so it is **T3**: it fires only on a `laptop-presence` signature over the cockpit's stored record, never on the page's token (the cockpit server holds the member's signing key, which an agent's sandbox does not, so a token-only accept would let any holder of the token act as the operator). What is signed is the kind, the member's handle and a digest of exactly the offer or proposed key the row showed; when the cockpit applies it, it fetches first and refuses anything that changed since the signature, and an act already done some other way is refused as changing nothing. A push that fails, a busy ledger or a missing signing key leaves the signed accept waiting and retried, never spent. The page itself cannot yet make a `laptop-presence` signature, so the row names the laptop command that does the same act (`levain team key confirm` or `levain team accept`).
- Known edges (Team actions): a fetch can hold the request for longer than one git call's 20 s bound (it runs several), and answers "busy" while the team hook's own fetch holds the ledger; a git read that hangs for a minute is replaced by a new one without stopping it; an accept whose fetch keeps failing for a permanent reason (the remote's branch deleted) is retried until it expires; the refusal reason of an accept the ledger moved under reads only "refused" in its outcome.
- Known edge: the Team zone reads the team of the install the cockpit serves (or the start you pass an embedder's `build_default_cockpit`); a cockpit with no install root shows no Team zone. A team read that hangs shows the last good read with its age after a minute.
- **The cockpit's Team control is gone; the Team zone replaces it.** The cockpit no longer lists running `levain team view` servers or links to them: `/team_views.json` and `dashboard_team.js` are removed, and `levain team view` no longer publishes an entry under `<levain home>/team-views/` (entries left there by an older version are never read again; delete the directory if you like). `levain team view` itself stays, for a clone with no levain install to serve a cockpit from.
- **`levain doctor` checks anneal's consolidate baton.** A new "consolidate baton" row fails when the store needs the baton and it is unclaimed, unreadable or held by another session, and the MCP launcher row fails a launcher with no `--session-id` (it names `levain update`) or one serving as a session other than `levain`.
- **Codex's consent hook, as a library; no `levain` command installs it yet.** `levain.consent.adapters.codex` files Codex's `PermissionRequest` on the consent queue and tells Codex `allow` or `deny` only for a signed answer or a standing grant the queue verified; anything else prints nothing and Codex shows its own approval prompt. `install(workspace)` writes the hook into the workspace's `.codex/hooks.json`, keeping your other hooks. Levain never trusts the hook for you: Codex shows "Hooks need review" at its next start and you trust it there. `trust_status(workspace)` asks Codex itself whether the hook is trusted, untrusted, changed or disabled. An `apply_patch` into Codex or Claude Code config or hooks is filed for you alone, like any other agent-config edit; a patch whose file headers cannot be read is treated as one.
- **`levain consent canary --agent claude_code|codex`** runs one short real agent turn in a new, empty scratch directory and passes only if the installed hook fired and its denial reached the agent (Codex runs under a terminal). It refuses a directory that is not new or empty, so it never touches a real project's hooks. Known edge: the Codex canary runs Codex interactively under your own `CODEX_HOME`, so Codex may record its own interface state in `~/.codex/config.toml` as any Codex start does; the canary writes no hooks or trust there.
- **The consent hooks pin the agent versions they were tested on** (Claude Code 2.1.300 and 2.1.301, Codex 0.162.1). Installing either hook (`claude_code.install`, `codex.install`) on another version of its agent is refused unless you pass `allow_untested_agent_version=True`; the canary installs past the pin, since it checks whatever version is installed; `levain doctor` warns when an installed consent hook meets an untested version, a Codex trust state other than trusted, a hook script that is not levain's, or a settings entry that names a missing script.
- **`levain consent pending` and `levain consent answer <id> allow|deny --signing-key <key>`: answer a waiting consent request from the terminal.** `pending` lists each open request, oldest first: its id, agent, tool, working directory, the paths it targets, its tier, when it was filed and how long its hold has left. `answer` first prints the request as the agent filed it (the tool, its input with secret-named keys masked, the working directory, the tier, the input digest and the request digest it signs), every agent-supplied text with its control, format, bidi and line-separator characters printed as `\uXXXX` escapes (in `pending` too), then signs the answer with your key in the `levain-consent-answer` namespace and hands it to the queue, which verifies it against a fresh replay of the authority log (an enrolled, unrevoked key; for an allow, a key class that covers the tier, so an edit of agent config or hooks needs a laptop-presence key) and records it; the hook waiting on the request then delivers it. A deny is signed too. One request, one signature: a second answer, an unknown or no-longer-pending id, an unenrolled key or a key class that cannot allow the tier is refused with the queue's own reason; an unknown or no-longer-pending id is refused before anything is signed, and Ctrl-C at the passphrase or touch prompt prints "answer cancelled" and records nothing. `--queue-dir` names the queue (default `$LEVAIN_HOME/consent`, else `~/.levain/consent`). The answer is verified against the cockpit the hook for the request's agent verifies against (`cockpit_data_root` in the queue directory's `claude_code.json` or `codex.json`); `--data-root` / `--path` naming another cockpit is refused before anything is signed, and when the queue names none, one of them is required; a root that holds no cockpit is refused before anything is signed too.
- **A consent request's display masks secret-named keys by the words inside them.** A key is split into words at `_`, `-` and camelCase, and masked when password, passwd, passphrase, secret, token, authorization, credential, cookie or bearer occurs inside one of its words, or apikey, privatekey or accesskey inside two adjacent words joined, or when a word is or ends in `auth` and the next word is not one that says what kind of thing it is about (url, type, method and the like): `api_key`, `accesstoken`, `secretKey`, `DB_PASSWORD`, `auth`, `basic_auth`, `basicauth`, `X-Auth` and `auth_header` are masked; `author`, `auth_url`, `authType` and a `private` flag are shown. A key that only looks secret-named is masked too (`max_tokens`, `tokenizer`): a secret shown costs more than a count hidden. A masked value shows its type, and a string its length: `[redacted: str, 24 chars]`. This changes the display of requests filed from now on; requests already filed keep the display they were sealed with.
- Known edge: `levain consent pending` and `answer` print an agent's control and format characters as `\uXXXX` escapes but leave spaces and backslashes as they are, so a working directory that literally contains the text `\u000a` reads like an escaped newline.
- Known edge: when the queue directory's adapter settings name no cockpit, the hook verifies no answer, so an answer given with `--data-root` is recorded and never delivered; the agent's own dialog answers it.
- **`levain cockpit pending` and `levain cockpit sign <pending_id> --signing-key <key>`: sign a waiting cockpit write from the terminal.** `pending` reads the cockpit journal (no server, no token) and lists each waiting pending, soonest expiry first: its id, verb, tier, panel and row, who requested it, when it was issued and how long it has left. `sign` reads the pending off the journal, prints its record in full from the very bytes it signs (every field of the signed record, each effect field by field, and the record's sha256; then, under their own heading because they are not signed, who requested it and your key's fingerprint and class), signs them in the `levain-cockpit` namespace, and posts the signature to the running `levain serve --write` at `--url` (default `http://127.0.0.1:7420`, loopback only), which verifies it over its own stored copy, decides and applies; the server's answer is printed as it gave it. The signature goes to `--url` only: no proxy setting is used and no redirect is followed (a redirect is printed as a refusal). The write token comes from `$LEVAIN_WRITE_TOKEN`, else a prompt, never an argument. Refused before anything is signed: a pending that is not waiting or has expired, a reported pending, a key whose class does not cover the pending's tier (the decision would otherwise end the pending refused), nothing listening at `--url`, a `--url` without a usable port, and no token or one that cannot be sent as a header. The exit code is 0 only when the server answers a fresh allowed decision that committed and the journal you named, read again, no longer shows the pending waiting; otherwise the CLI prints the server's answer and what the journal shows, and exits 1 (a replay of one already decided exits 1).
- Known edge: anyone running as your user with the server's write token and a usable signing key can run `levain cockpit sign`; a decision's authority rests on the key's custody, as for `levain consent answer`.
- Known edge: `levain cockpit sign` sends the write token and the signature only to a listener your own OS user holds on `--url` (on macOS one bound to exactly that address; on Linux, that address or a wildcard bind of the port), so another user's process on the port is refused before anything is sent; it does not check that the listener is the server holding the journal, so a process running as your user on that port receives them, as with any loopback token. A timeout or a dropped connection after the request was sent says the server may have applied it, never that nothing was decided. It cannot make the command report success, since that needs the journal to show the pending decided. `pending` and `sign` open `cockpit.db` the way the server does, so a zero-length file is initialized and a read-only mount fails; a missing one is refused. `sign` reads the pending and checks the key's class in two separate reads, so a key revoked between them passes the CLI's check and the server refuses it.
- Known edge: anyone running as your user with a usable signing key can run `levain consent answer` too; an answer's authority rests on the key's custody (its passphrase, touch or presence), as for `levain consent grant create`.
- Known edge: the queue keeps only a display of the tool input, cut at 2,000 characters with its keys sorted, while an answer binds the digest of the whole input. So a request whose display is cut (a long Bash command, a large Write) is not answered from the terminal: `pending` marks it "too long to answer here" and `answer` refuses to sign it, saying the input is too long to show here; the agent's own dialog stands. The answer is signed before the queue judges the key, so an answer refused for its key (unenrolled, revoked, or a class that cannot allow the tier) still asks for your passphrase or touch. A request withdrawn or expired between the CLI's read and your signature is refused by the queue after you sign. A secret under a key whose name holds none of the masked words (`auth`, `passphrase`, `pwd`) is shown, as is any secret inside a free-text value.
- **Standing consent grants: a signed grant can answer an agent's permission prompt for you.** `levain consent grant create --agent claude_code|codex --tool <name> --workspace <dir> --posture <mode> --expires <duration|stamp> --signing-key <key>` (`--tool` and `--posture` repeatable) appends a consent grant to the cockpit's signed authority log, signed by a laptop-presence key, the same as any loosen; `levain consent grant revoke <id> --signing-key <key>` (any enrolled key) ends one, and `levain consent grant list` shows them. A grant answers a request only when every criterion holds against a fresh replay of the log: it is not an edit of agent config or hooks; the agent and the exact tool name match (an MCP tool as `mcp__<server>__<tool>`; no globs or prefixes); the session's permission mode is one the grant names (a request with none matches no grant); the grant's workspace is still the directory it was signed for (its real path, with no symlink on it, is exactly what the grant names; never `/`, this machine's home or above it); the session's directory resolves inside it; every path the request names lies inside it with no symlink on the way: a file tool's file (Claude Code's Edit, Write, MultiEdit and NotebookEdit; Codex's `apply_patch`, by its patch headers) and, for any tool, its `file_path`, `path`, `paths`, `notebook_path`, `source` and `destination` fields (so a Read, Grep, LS or NotebookRead grant covers the workspace only), while a path field of another shape, and a Glob pattern that is absolute, starts with `~` or contains `..`, match nothing; the tool input checked is the one the request was filed with; and the grant has not expired. A grant never names the shell (`Bash`, for both agents: a shell command is never parsed), never names `/`, your home directory, a directory above it, a symlink or a directory that does not exist as its workspace, and lives at most 30 days past the link that made it. Grants only allow; none denies. Narrowing a grant (fewer tools or modes, a workspace inside the old one, an earlier expiry) is a tighten; anything wider is a loosen.
- **A grant's answer is recorded and re-verified at delivery.** The queue (store format `levain-consent-queue/3`) records how each request was answered (`via`: a signed answer or a grant). A grant answer's receipt is `by: binding` and names the grant, the authority link (seq and hash) that created or last changed it, and the posture. The hook delivers a grant-answered request only while the grant still answers it in a fresh replay, and only for the authority link it recorded, so a revoke, expiry, renew, tighten or loosen before delivery takes the answer back, and a raw write of the queue store can only take a decision away.
- **A grant's allow names the grant to Claude Code.** It carries a `systemMessage`: "levain: allowed by your standing consent grant <id> (posture <mode>); `levain consent grant revoke <id>` ends it." Known edge: in a real Claude Code 2.1.300 session the operator saw only Claude Code's own "Allowed by PermissionRequest hook", not that message; which grant answered is in levain's receipt (gate by binding, the grant id and its authority link).
- Known edge: a tool's paths are read from its path fields only (`file_path`, `path`, `paths`, `notebook_path`, `source`, `destination`, each checked when present) and a Glob's `pattern`; a URL, or a path the tool reads from elsewhere in its input (Grep's `glob`, say), is not checked, and a tool naming none (WebFetch) is matched on the session's directory only. A relative Glob or Grep under a granted workspace can walk a symlink inside it that points outside it: the pattern's start and the `path` field are checked, not the tree walked. A target with a `..` component, or one crossing an existing symlink, matches nothing and goes to you; an `apply_patch`'s path fields are checked beside its file headers.
- Known edge: the paths are checked when levain answers, not when the agent uses them: a process that swaps a path to a symlink between levain's answer and the agent's write redirects that write. Levain answers the prompt; it does not confine the agent's file operations.
- Known edge: Claude Code's permission modes other than `default` are taken from its documentation; only `default` was observed. Codex's only accepted mode is `default`. A Codex `apply_patch` is matched only when the Codex adapter has been imported in that process (it registers its patch reader then); otherwise it matches nothing.
- Known edge: the Codex hook fires in interactive sessions only. `codex exec` never asks for approval, and `codex -C <dir>` does not load `<dir>/.codex/hooks.json` (Codex loads project hooks from the directory it runs in).
- Known edge: after Codex runs a command levain allowed, by a signed answer or by a grant, Codex shows no "allowed by hook" text and no grant name; the record of who allowed it is levain's receipt.
- Known edge: a hook stopped while it waits (its agent session ended, or the hook was killed mid-hold) leaves its request answerable until the hold ends; an answer then reaches no agent, though the receipt records it as allowed (or denied). Likewise a grant revoked between levain recording a grant's allow and delivering it: the request stays recorded as allowed while the hook printed nothing and the agent showed its own prompt.
- **Claude Code's consent hook script re-runs its interpreter with `-E -P` before importing levain, as Codex's does,** so a package planted beside it in `.claude/hooks/` is never imported in levain's place. `-E -P`, not `-I`: `-I` would also drop your user site, where levain may be installed. A hook installed earlier keeps the old script until `install` runs again.
- **Both consent hooks are installed as `<python> -EP <script>`,** so their first interpreter already ignores `PYTHON*` variables (a `sitecustomize` on `PYTHONPATH` no longer runs before the hook) and the script's directory; the script's own `-E -P` re-run stays for a direct invocation. `install` recognises and replaces an entry in the earlier `<python> <script>` form, as do `levain doctor` and `trust_status`. Codex hashes the hook's command, so after `install` rewrites it Codex reports the hook as changed and shows "Hooks need review" again: trust it there once more (measured on Codex 0.162.1). Claude Code ran the new entry with nothing to re-approve in the canary (2.1.300, headless).
- Known edge: both hooks run their interpreter with `-E -P` so nothing in the workspace or `PYTHONPATH` can stand in for levain; a levain importable only through `PYTHONPATH` therefore never answers (the agent's own prompt stands). Two `install` calls racing on one workspace can interleave, and so can another program writing its settings: one that makes the settings file unmergeable during an install leaves the hook script without its settings entry (the install fails, saying why; run it again).

- **Claude Code's cross-session messaging can be governed in a levain workspace (`levain init --messaging governed`, opt in).** Governed, the workspace's `.claude/settings.json` (rendered by `levain init --adapter claude-code` and by `levain update` where you have not edited it, and merged by the consent hook's `install`) sets `crossSessionInbound: "hold"`, so a message from another of your sessions shows a notice and is not delivered (a workspace `hold` outranks an `accept` in your user settings), and asks for every `SendMessage` (`permissions.ask: ["SendMessage"]`, plus `isolatePeerMachines: true`). An explicit ask rule prompts in every permission mode except `dontAsk` (which denies it), bypass included, so every send reaches the permission flow: levain's consent hook, where a standing grant on `SendMessage` for the workspace can answer it and leaves a receipt, any other `PermissionRequest` hook you installed, or your own dialog. `install(..., inbound="hold"|"refuse"|"unmanaged", outbound="ask"|"isolate"|"deny"|"unmanaged")` chooses; `isolate` writes only `isolatePeerMachines`; `deny` turns sending and listing off (`SendMessage`, `ListAgents`); `unmanaged` writes nothing for that direction. A write never loosens what the file already says: it keeps the stricter inbound value and only adds rules. `levain doctor` reads the controls the settings files give, with Claude Code's own precedence (managed, the last managed file setting a key winning, then user, then the workspace's shared `settings.json` only when stricter; a deny or ask rule counts when it names the whole tool, as a bare name or a glob such as `*`; `settings.local.json` is not credited, since it can only tighten and where Claude Code keeps it depends on worktrees and ownership) and warns, naming each direction, when one is not governed; the hook pin now covers each settings file's messaging controls, so removing the `hold` or the ask rule shows as a change, while an install pinned before this version is not flagged for controls it never set.
- Known edge: `SendMessage` is one tool for other sessions, subagents and agent-team teammates, so the ask rule also asks before Claude resumes a subagent or messages a teammate; a standing grant on `SendMessage` for the workspace makes that quiet. Without one, a `claude --bg` session nobody is attached to waits at that prompt until someone answers it, so sessions that message each other stall, and in a `claude -p` run every send is denied. The ask-rule stall applies to a workspace that chose `governed` (below); under the default `isolate`, a send beyond this machine also waits at its `isolatePeerMachines` prompt. Nothing documented lets a hook see a held message, so the queue cannot release one: remove the workspace's `crossSessionInbound` to receive messages (nothing outside the workspace loosens it). Doctor reads files, not the running Claude Code: it cannot see `--settings`, `--setting-sources` (a session that loads no project settings has neither these controls nor levain's hooks), a relocated `CLAUDE_CONFIG_DIR`, MDM-profile, server-managed or (on WSL) inherited Windows settings (one with `allowManagedPermissionRulesOnly` discards the workspace's ask rule), or a plugin mod that approves calls (`tool.check`, which can answer an ask rule); Claude Code before 2.1.224 does not know these keys, and before 2.1.248 ignores a value it does not recognise. A session started in a directory below the workspace does not read the workspace's shared `settings.json`.
- **`levain init --messaging isolate|governed|off|unmanaged` picks the workspace's messaging posture; the default is `isolate`** (claude-code adapter only; any other adapter, and `--web`, are refused before anything is written). `isolate`, the default, writes only `isolatePeerMachines: true`, so Claude Code asks before a send leaves for a session beyond this machine, even in `bypassPermissions` mode; levain neither queues nor records a send to a session on this machine and holds no incoming message (Claude Code's own default decides per message), and `levain doctor` names those gaps in an OK row. `governed` writes the three keys above. `off` refuses inbound (`crossSessionInbound: "refuse"`) and denies `SendMessage` and `ListAgents`. `unmanaged` writes none of the keys. Whichever is chosen, Claude Code never counts a message from another session as your consent. The choice is recorded as `messaging` in `<install>/.levain/config.json`, and `levain update` and `init --force` render it again; `init` records the choice, the default included; an install with no record is read off its own `.claude/settings.json` (a deny on `SendMessage` reads `off`; a hold, refuse or ask reads `governed`; none reads `isolate`), so a re-render never drops a hold or an ask the file carries, and a recorded value that is not a choice, or a `config.json` that cannot be read, renders `governed`. Doctor warns when the files do not match the recorded choice (a recorded `isolate` with no `isolatePeerMachines`, a recorded `governed` or `off` that is not governed). The settings template itself carries none of the keys: the renderer adds them, after `hooks`.
- Known edge: moving an existing install to another posture re-runs `levain init --path <install> --force --messaging <choice>`; there is no lighter verb for it. A dashboard edit racing `init` can drop the recorded choice; the next `levain update` then reads it off the workspace's settings file, which keeps a governed file governed but turns a recorded `unmanaged` back into `isolate`.

- **`levain doctor` says whether the session transcript hooks are wired.** A Claude Code install gets a row for Stop, PostToolUse and PostToolUseFailure, a Codex install one for Stop and PostToolUse (only while Codex's `hooks.json` is this install's; otherwise the wiring rows already say it is not). A missing one is a warning naming it, never a failure: these hooks only record what a session did for the cockpit's transcript, so an install from before them works and records no transcript lines. `levain update` adds them (run on installs made before they existed, for both agents). Known edge: the row reads the install's `.claude/settings.json` and Codex's global `hooks.json`, the files levain writes; a hook you wired in another layer (`settings.local.json`, user settings, Codex's inline `[hooks]`) shows as not wired, and a layer that switches hooks off is not read here. A hook counts when its command names this install's script (the same reading as the SessionStart rows), and only from a group with no tool matcher, since a matched group records some tools only.

### Changed

- **levain now requires anneal-memory 1.0 (`>=1.0.0rc2,<2`).** anneal 1.0 lets only the session holding a store's consolidate baton consolidate it. `levain init` claims the baton for the session `levain` on every run, for a new store or an existing one; if another session holds it, init stops and says so and never takes it. The MCP launcher is now `... serve --session-id levain`, and `levain wrap` runs as that session. An existing install picks up the new launcher and the baton with `levain update`; restart your agent afterwards. Without the baton an entity records memory but never consolidates it.
- Known edge: levain's anneal version check ignores pre-release suffixes (`1.0.0rc1` reads as `1.0.0`); the `>=1.0.0rc2` pin keeps rc1 out of a pip-installed environment.
- **A cockpit row's project tag comes from its `repo` facet when it has one.** A provider that sets `repo` on a row tags that row with that repo, in the spelling your data first used, and its words are no longer read for a repo name; so a repo named like an ordinary word (`review`) no longer tags a row that only uses the word but belongs to another repo. A row with no `repo` facet is still tagged from its words, as before.
- Known edge: levain's own list panels set no `repo` facet yet, so their rows are still tagged from their words. A facet the page has not registered as a repo (longer than 100 characters, or past the registry's limit) is ignored and the row falls back to its words.

### Removed

- **`levain.consent.decide_by_policy`**, 0.10.4's placeholder that decided nothing. A policy answer now comes only from a standing grant, through `levain.consent.policy.guarded_policy`, which the adapters call with the request, the tool input it was filed with and the session's posture.


### Fixed
- **`levain verify-hooks` checks the transcript hooks and writes nothing into your install.** `stop.py` and `post_tool_use.py` run from your installed copies in a temporary install root with a test event, the one transcript line each writes is checked and the root is removed. Its prompt check no longer sends a session id, which had made the real hook record a session nobody ran in the install (the cockpit listed it).
- **`levain update` and `init --force` without `--messaging` never loosen the workspace's messaging controls.** A re-render with no explicit choice keeps every control the workspace's `.claude/settings.json` carries (a hold or refuse, an ask on `SendMessage`, a deny on `SendMessage` or `ListAgents`, `isolatePeerMachines`), merged without loosening, so a lost or unreadable `.levain/config.json` cannot turn an `off` or `governed` workspace into a looser one; only an explicit `--messaging` loosens. Doctor shows the not-governed OK row only for a choice recorded in `config.json`, and checks a recorded `off` against the files (refuse, `SendMessage` and `ListAgents` denied).
- **`levain update` reconciles the consolidate baton whatever else it reported,** and a baton another session holds is a warning, not a failed update; a store whose status cannot be read is named rather than failing the update.
- **The session registry is kept like the transcript:** the sessions directory is created (and tightened) to your user only, and each registry file is written 0600.
- **The team accept records only what it can stand behind:** a final record has the final shape (an empty or malformed entry is not taken as done), the accept marker and record are written with an fsync, a publish waits for one push round instead of six, a failure to record after publishing is a re-drivable fault, and an offer naming no known member is refused. The standalone `levain team view` says when the ledger was judged only in part, as the Team zone does.
- **The cockpit's request and transcript reads have a deadline** (15 s), reads queued for a view that closed are not made, request bodies are not kept after their view closes, the countdown timer stops when nothing counts down, and the board and a focus view searching the same query share one request.
- **Doctor's Claude Code sandbox row and the Sessions tile's sandbox chip read the files Claude Code reads for that directory.** Both credited a repository root's shared `.claude/settings.json` for an install or a session below the root, which Claude Code does not load there (it reads the shared file from the session's starting directory only), so a sandbox set only at the root showed "configured on" while Claude Code ran Bash unsandboxed (observed with Claude Code 2.1.301: a session started in a subdirectory wrote outside its directory; started at the root, the same write was denied). One reader now answers which project files Claude Code reads for a directory, by the vendor's rules: the shared file in that directory, and `settings.local.json` at the repository root (a linked worktree's main checkout), unless the root is your home directory, is not yours, or there is no git repository, when it stays beside the shared file; a local file an older Claude Code left in the starting directory is read after the root's. Doctor's sandbox row, the chip and the messaging row all call it. The chip now reads the session's own directory instead of walking up to a `.claude` or `.git`.
- **A folded empty card on a phone keeps its inset.** At phone width its note and empty line wrapped under the title and drew flush at the card edge; they now line up with the title (seen in iOS Safari 26.2).
- **Two cockpit pages open at once no longer leave each other half drawn.** The server made a cockpit read wait up to 5 seconds for a free slot (past 64 waiting reads it still answers 503) instead of refusing it at once when 8 were in flight, and the page now reads at most 4 panels at a time, in order. Known edge: other routes (writes, the substrate read) still refuse at once when the server is full, but reads hold at most 6 of the 8 slots, so two always stay free for them. Waiting reads are not served strictly first-come (a page's burst finishes well inside the wait), and a client that half-closes its connection before the answer is treated as gone.
- **The cockpit no longer keeps a CPU core busy while it is open.** The Cognition chart's glow breathed forever, so the browser drew a frame every refresh (measured 10-15: 53 frames a second and 7-11% of a core in headless Chrome, against about 2% without it). It now breathes three times and settles; a loading dot pulses for about ten seconds, then holds still.
- **A panel's action the page cannot perform draws nothing, and an action with no field is a bare button.** An external panel (such as the Team Joins pane) whose own action is laptop-only or refused no longer draws an empty compose box that clipped the row naming the laptop command (the compose card of such a verb draws nothing rather than an unavailable panel), and an action with no fields (such as a fetch) draws its button alone, not an empty field bar. A text panel keeps its edit button.
- **A second Codex install no longer makes doctor blame an agent for the first one's hooks.** Codex reads one `hooks.json` per `CODEX_HOME`, so `levain init --adapter codex` in install B repoints it at B. Install A's `levain doctor` then said its hook pin had "changed" and that "an agent or another program edited code that runs as you", although levain itself had made the change. When `hooks.json` is exactly what levain recorded writing for another install (every hook command names a script in that install's `activation/hooks/`, and that install's adapter receipt records these bytes), A's pin no longer compares it: the row is a WARN that names that install and says its scripts are not covered by A's pin. The wiring rows still FAIL, because A's Codex sessions really do run B's hooks now; they name B and give the command that takes the file back without a new interview: `levain init --path <A> --adapter codex --force --answers <A>/.levain/answers.json` (the old hint, plain `levain init`, refuses a directory that is not empty). `--force` keeps the store and backs up the files it replaces. Two more fixes on the same path: B's own init now pins what it wrote (it used to leave B unpinned, so B's first doctor warned "trust on first use"), and A taking the file back from a newer levain re-pins (it used to stay red until `--repin-hooks` whenever the new `hooks.json` differed from the first). `doctor --repin-hooks` while another install owns the file says that file was not pinned. Any other change to `hooks.json`, including a copy that differs by a byte from the owner's record, still fails doctor on every Codex install, and so does a change to A's own settings, activation scripts or scripts its settings name. Known edges: the adapter receipt is a file your own user can write, like the pin, so a process running as you can make a planted `hooks.json` look like another install's write; doctor then shows the WARN and the wiring FAIL naming that directory (and says to read its `activation/hooks/` if you did not run `levain init` there), never a clean pass. The takeover hint passes your recorded answers and recorded packs when they are on disk, and says the interview runs again when there is no answers file. Doctor reads the file once for the pin row and again for the wiring rows, so a change landing during one doctor run can show in one of them only.
- **A pack record that is not UTF-8 is read as corrupt, not a crash.** `.levain/manifest.json` that cannot be decoded raised out of levain's pack-record reader, so `levain doctor` on a Codex install (and any command reading the recorded packs, `levain update` among them) stopped with a traceback; it is now treated like any other unreadable record (found by review, reproduced by a test).
- Known edges (release review): a standing grant's answer recorded in the instant a revoke lands is not delivered (the agent's own dialog stands) but the queue row reads `allowed`; a queue directory made by 0.10.4 (`levain-consent-queue/2`) is refused by this version and its hook falls back to the agent's dialog (delete the directory); a grant matches an MCP tool's paths only by the fields it knows (`file_path`, `path`, `paths`, `notebook_path`, `source`, `destination`) and a Write or Edit grant covers every path in its workspace, `.git/hooks` and build files included; a relative path under a symlinked working directory refuses a grant (you are asked); `claude_code.install()` (the library) defaults to the governed messaging pair whatever `init` recorded; a signed team accept binds the proposal's content, so an identical proposal withdrawn and made again while the pending lives is accepted by the same signature; an accept whose act failed after its marker, followed by a manual accept of a changed proposal before the re-drive, is recorded as this pending's; a ledger read that never returns leaves its worker thread running while a new one takes over; a team discovery racing a join or a leave can briefly show the previous panes; `levain cockpit sign` checks the listener again after the key touch, so a listener that changes hands in the instant between that check and the request is not caught; a dashboard edit to `.levain/config.json` during `init` can leave the recorded messaging choice different from the one rendered (the workspace's controls are kept either way); a request body read for a row on the board stays in the page's memory until a focus view closes or the page reloads. A hook refuses a symlinked `.levain/sessions`, but a process running as you that swaps the directory for a link between that check and the hook's next open can redirect that one write; such a process can already write those files itself. A cockpit read retried after a busy answer has its own 15 s deadline, so a retry whose body stalls can hold its connection up to about 15 s after the queue has moved on.

## [0.10.4] — 2026-10-15

The consent queue's core and Claude Code adapter as a library, signed standing policy for the cockpit, doctor's hook pins, and the cockpit's phone layout, project hues and Decisions tile. No `levain` command installs the consent queue yet.

### Added

- **The consent queue's core and its Claude Code adapter, as a library; no `levain` command installs or shows them yet.** `levain.consent` files a request for a Claude Code `PermissionRequest` gate, keeps it in a store under `<levain home>/consent`, expires it, and delivers a decision only when it verifies as a signed answer against the cockpit's authority log (the row is never the authority; a signed answer uses the reserved SSHSIG namespace `levain-consent-answer`). With no verified answer the hook decides nothing and Claude Code shows its own dialog. `levain.consent.adapters.claude_code.install(workspace, ...)` writes the hook into a workspace's `.claude/settings.json`. Any request that would edit agent configuration or hooks (Claude Code and Codex settings and hook files, `.mcp.json`, `~/.claude.json`, `.claude/agents|skills|commands|plugins`, plugin hook files, levain's own installed code) is never answered by a policy; it waits for you. Policy answers, the Codex adapter and the cockpit's queue view are not built.

### Known edges

- **The cockpit's `flowconnect` surface-token id keeps its name.** It is a wire contract (`cockpit/verbs.py`, `web_server.py`), so a rename needs a compat alias and is left for a later slice.
- **Installing the Claude Code consent hook replaces a `PermissionRequest` handler it takes for its own.** A reinstall removes the earlier levain handler even when it was written for another Python, by its shape: a command of exactly two words whose second is levain's installed hook script path. A handler of your own written exactly that way (any program, then levain's script path, nothing else) would be replaced too; a handler with any other command (a third word, another script) is always kept.
- **An edit of levain's own installed code always goes to you.** The consent hook runs levain's package, so an Edit or Write into that package (or into the interpreter's `.pth`, `sitecustomize.py` or `usercustomize.py`, which can redirect it) is treated as agent config and never answered by policy. With an editable install the package is your levain checkout, so a session working on levain itself asks you about each such edit when Claude Code would ask at all.
- **A plugin's own scripts are agent config only inside `~/.claude/plugins`.** For a plugin outside it (a local marketplace, `--plugin-dir`), its `.claude-plugin/` directory and its `hooks/` are treated as agent config, but a script its `plugin.json` names elsewhere in the plugin (such as `scripts/x.sh`) is not, so an edit of that script can be answered by policy.

### Changed

- **The cockpit is tidier on a phone and carries one hue per project.** On a phone the header leads with the nav, the sky shows only rows that had events, and an empty card folds into a slim bar at the end of its zone instead of a void beside tall neighbours. The projects are your own repos: the names your install's data carries (a row's `repo` facet, an ambient event's `repo`), and levain ships no list of its own, so an install whose data names no repo shows no tags. Everywhere a repo shows (the sky's marks and row swatches, a tag on rows that name it) it takes one of five hues, checked for contrast and for colour-blind pairs; up to five repos never share one. A 2px stripe tops each panel of one levain subsystem (memory, decisions, the work lists). A Decisions tile glows while something waits on you, and a failed review glows red in the sky. The clock stays plain at every hour.
- Known edge: a row's repo tag is read from the row's own words, matched against your repo names, not from a structured repo field on the row, so a row that mentions two repos takes the longest name it finds, and a repo named like an ordinary word tags rows that use the word. Hues go to the repos in name order, so a repo seen for the first time can move another repo's hue (the row tags follow within two seconds, the sky's marks at its next redraw, up to 30 seconds); past five repos, two share a hue. A repo stays known until the page reloads, even after it leaves the data. Word boundaries are ASCII: a name next to an accented letter still matches.
- **Decisions waiting is a tile in At a glance, not a Work card.** It shows how many decisions wait on you, or "Nothing is waiting on you", and a click (or Enter) opens the list in the focus view, grouped as Waiting on you, Waiting on a signature, Resolved and Outcomes, with the filter box once the list is long. An embedder that registers its own decisions source still chooses its panel's place.
- **The cockpit's standing policy is now the signed authority log's.** A verb's tier floor comes from the policy links in the log, replayed for each request, and no longer from a constant in the code. `levain cockpit policy raise <verb> <T2|T3> --signing-key <key>` appends a raise, signed by a display-phone or laptop-presence key; a raise only ever lifts a tier, and lowering one is a loosen that only a laptop-presence key signs. After a raise, the cockpit shows and enforces the new tier on the next request, every page and manifest etag changes, a fire previewed before it is refused as moved, and a pending issued before it goes stale. A verb the broker has no adapter for (`spore_touch`) cannot fire once raised above C1: it is refused, naming why.
- **A cockpit whose authority log is refused shows every verb at T3.** A log it cannot read or verify may hold a raise it cannot see, so nothing fires at a lower tier until the log is readable again. The same holds for a server whose broker is not active (a second server on the same data root, one started without `--install-class`, or one whose broker failed to start) wherever a cockpit journal or anchor already exists: it cannot read the log, so it shows every verb at T3 and refuses the write. Only a log known to be empty (on an inactive server: no journal and no anchor on the data root), or a server with no broker at all, uses the shipped tiers.
- **Cancelling a pending honours a raise of `pending_cancel`.** Cancelling your own pending is C1 and another's is that pending's tier, each lifted to the floor a signed raise of `pending_cancel` sets, in the decisions panel and when the cancel is decided; with the log refused, a cancel is refused. `levain cockpit policy raise` refuses a verb the cockpit does not know, or one that honours no floor (`token_rotate`), instead of signing a raise that protects nothing.
- Known edge: a request admitted before a raise commits may finish at the old tier. A captured gate finishes (DESIGN §4); closing this needs admission serialized against policy appends.
- Known edge: a direct C1 action rendered before a raise and clicked after it carries no seen tier, so it is not refused as moved: it becomes a pending at the new tier (the safe direction), or is refused `not_built` for a verb with no adapter.
- **A downstream's own verb can be raised.** When the cockpit server is built it records the names of the verbs it serves, kernel and downstream, with the time, in `served_verbs.json` beside `cockpit.db`. `levain cockpit policy raise deploy T3 --signing-key <key>` checks a verb that is not the kernel's against that record by exact membership, so a misspelling such as `deplyo` is refused before anything is signed; with no record it refuses and says to start the deployment's cockpit server once; a server that cannot write its record removes an earlier one, and where the data root is read-only the removal fails as well and the earlier record stays. An embedder calling `policy_raise` passes the `VerbRegistry` it serves as `registry=`, which is checked instead of the record.
- Known edge: the record is a file under the data root, and a name planted in it can be raised. Raising only tightens, so a planted name is at worst a raise that protects nothing. A record left by an earlier server that served a verb the current one does not is the same case, and so is a second server on the same data root that waits on the lock (STANDBY): it writes its own record while the first one serves. So does a server whose broker is disabled (an install-disabled or network-filesystem broker): it records verbs it will not serve.

### Security

- **`levain doctor` pins the hook files the agents run, and says what changed.** It records a pin over Claude Code's hook entries (`.claude/settings.json` and `settings.local.json`), the scripts under `.claude/hooks/` and `activation/hooks/`, and Codex's `hooks.json`, in `.levain/agent_hook_pins.json`. The first run trusts what is there (a warning says so). A later run that finds any of them changed fails, naming the file and the old and new digest. The script contents are pinned, not only the settings entry, because Claude Code does not notice an edited hook script. If you made the change, `levain doctor --repin-hooks` records it. Doctor never edits your settings.
- **`levain init` and `levain update` re-pin the hook files they write.** After a legitimate init or update, `levain doctor` no longer fails "changed since pinned" until you run `--repin-hooks`. Each re-pins as its last act through the same pin writer doctor uses. If the pin was already broken by an edit that is not levain's, the re-pin is skipped and doctor keeps failing.
- **Doctor's hook pin is stricter (L3 round 1 on the pin).** A pin file that exists but is unreadable, not valid JSON, has no `pins` map or belongs to another install is no longer treated as "first use": doctor FAILS until `--repin-hooks`, and `init`/`update` never re-pin over it. Only a missing pin file is first use. `init --force` and `update` on an install that has hook files but no pin leave it unpinned (doctor's own first-run warning speaks); a first init into an empty directory is still pinned. Doctor now also pins the git repo root's `.claude/settings.json` and `settings.local.json` when the install sits below a repo root, and the files a hook command names under the install (see the known edge below for which operands are followed). A command that names no file doctor can pin is named "not pinned". A component that cannot be read or hashed safely (an unreadable script, a refused activation tree, a settings file that is not JSON) fails doctor by name, and `--repin-hooks` refuses instead of pinning a placeholder. The sandbox line says "configured enabled", warns unless `sandbox.failIfUnavailable` is true too, and names the settings files it read.
- **Doctor's hook pin, L3 round 2 on the pin.** An interpreter is no longer pinned: a hook command pins every file it names under the install (or `${CLAUDE_PROJECT_DIR}`), so a Python upgrade is not a change and `python3 ${CLAUDE_PROJECT_DIR}/x.py` (or an absolute path, as levain's own hook commands are written) pins the script; a relative operand does not (see the known edge). Settings pins now also cover `disableAllHooks`, `statusLine` and `apiKeyHelper` (and the commands those name). A symlinked hook script is pinned by its target's content and its link string. The workspace's `.codex/config.toml` `[hooks]` table is pinned. A Codex `hooks.json` that was already on disk before the first `init` is unvetted: doctor's first-run warning now lists what it pinned. The Claude Code sandbox line is no longer printed for a Codex install. **Pins written by earlier builds of this branch re-pin with `levain doctor --repin-hooks` (nothing shipped before this).**
- **Known edge (doctor pin): sources it does not read.** A non-boolean `sandbox.enabled` stops the precedence walk at that layer. A git worktree's main-checkout settings root is not followed. A relative bare-interpreter command (`python3 tools/x.py`) leaves its script unpinned, and doctor warns. argv[0] is never pinned when it is a known interpreter name or a native binary and later tokens exist (a copied venv python included); every later existing file or directory operand outside the install (lexically) is named "not pinned", even when another operand was pinned; an in-root symlink is pinned by link string and target content wherever the target lives. Doctor follows an operand only when it is itself a path: absolute, or starting with `~` or `$CLAUDE_PROJECT_DIR` (braced or not), or a relative path with a slash as the command's first token. A relative operand after the first (`python3 hook.py`, `python3 .claude/hooks/x.py`), a path inside an option (`--script=${CLAUDE_PROJECT_DIR}/x.py`), one behind another variable (`$PAYLOAD`) and a directory under the install are neither pinned nor named, so a command made only of such operands is named "not pinned" while one that also names a pinnable file reads clean. With arguments after it, a first token named like an interpreter (`python`, `node`, `sh`, `uv`, `npx`, `env` and the like) is skipped as one even when it is a script under the install. `env` in settings is not pinned (ordinary variables change for benign reasons), nor are the files a `statusLine` or `apiKeyHelper` command runs beyond what the pin of that command's own file covers.
- **Only a Codex install pins Codex's global `hooks.json`.** Codex has no per-project hooks file, so `levain init --adapter codex` in any directory rewrites `~/.codex/hooks.json` (or `$CODEX_HOME/hooks.json`). Pinning it from every install turned a Claude Code install's `levain doctor` red, with the "an agent may have edited code that runs as you" hint, as soon as a Codex install was made elsewhere. A Claude Code install now pins only its own hook files; a Codex install still pins the global file and still fails on a change to it, and a fresh Codex init still treats a `hooks.json` it did not write as unvetted.
- **Known edge (doctor pin): one global Codex file, several installs.** A Claude Code install does not watch Codex's global `hooks.json`, so a hook planted there from a Claude Code session is caught only by a Codex install's doctor. Two Codex installs on one machine share that file, and each `init` rewrites it for its own install, so the other Codex install's doctor fails until `--repin-hooks`. The pin follows the adapter the install attests (`CLAUDE.md` wins over `AGENTS.md`): `init --force --adapter codex` over a Claude Code install leaves `CLAUDE.md`, so that install stays a Claude Code one to levain and does not pin the global file, and a `CLAUDE.md` added by hand to a pinned Codex install reads as a changed pin until `--repin-hooks`.
- **Known edge (doctor pin): a write racing init or update.** An outside edit landing between `init`/`update` taking the pre-write pin status and its re-pin is blessed by that re-pin. The window is the length of the install.
- **Known edge (doctor sandbox line): sources it cannot see.** It reads managed, project and user settings files; it cannot see `CLAUDE_CONFIG_DIR` relocation, `--settings` or server-managed settings, and the line says so. A worktree whose Claude Code settings live in the main checkout's root is not followed.
- **`levain doctor` warns when Claude Code's own sandbox is off for the workspace.** It reads `sandbox.enabled` the way the host guard reads managed settings (managed, then the workspace's local and shared settings, then yours); none set means off.
- **`.codex` is now write-denied to hosted Claude Code's Bash**, like `.claude`. A sandboxed model's Bash could create `<workspace>/.codex/config.toml`.
- **Known edge:** in your own mode (skip-permissions, no Claude Code sandbox) a model's Bash edit of hook files is not stopped; doctor finds it afterwards.

## [0.10.3] — 2026-10-14

The cockpit's visual pass. No change to what a write needs, to the floor, or to any stored format.

### Changed

- **The cockpit page has one visual system.** Type, spacing and radii are tokens (seven font sizes, four weights, a nine-step space scale, four radii); the accent colour is kept to a short list of uses, and errors and "unavailable" read as a status dot with neutral text instead of orange; glows, gradients and pressed-button offsets are gone, and `prefers-reduced-motion` stops the motion. Monospace is for identifiers and uppercase for section labels only.
- **Every row has one shape.** The text runs full width, a meta line sits under it, and the row's verbs sit at the meta line's right as small text buttons that fade in without moving the row. The Tray shows edit, schedule and dismiss; other panels show the first three verbs the kernel offers, and the rest sit in a keyboard-reachable `···` menu. On a narrow row every verb moves into that menu. Rows clamp at three lines.
- **The hero is one control row**: the mode segment, the status counts, Sync, Ambient and Jump, with the jar (now in the accent orange). Below about 880px of width the controls regroup instead of wrapping one at a time.
- **The zone grid fills its last row in every mode**, and a panel that scrolls shows a fade at its bottom edge instead of a glyph over the text. The team view uses the same fade.
- **The Cognition chart draws its three series in three hues** (links in the accent, episodes and graduations in two chart-only colours), and on the Build tab the chart fits its card with no inner scroll.
- **List filters and episode search share one box style**, and the Tray's filter and order select are one box.
- **Team ledger callers go through one interface** (`levain.team.ledger_api`: `LedgerTransport`, `open_ledger`, `require_git_wire`). Commands that only work on a git-backed ledger say so through `require_git_wire`. `levain team status` and `doctor` print the same lines as before.


### Fixed

- **The Cognition chart's settled traces no longer draw dotted** in engines that dash a non-scaling stroke.
- **The overflow menu returns focus to its trigger** when it closes.

## [0.10.2] — 2026-10-13

A security fix. Upgrade if Levain runs bash on Linux where you use gpg, or if you use the cockpit's signing keys.

### Security

- **On Linux, gnupg's configuration files are now read-only to the entity in a gnupg home where Levain finds a running dirmngr.** 0.9.1 made `gpg-agent.conf` and gnupg's other configuration files read-only to bash in `~/.gnupg`, and in `$GNUPGHOME` when Levain's own environment sets it. That did not cover a gnupg home outside both that holds a dirmngr started where `/run/user/<uid>` did not exist, such as from cron or before linger. In 0.10.0, bash without network gets a fresh view of such a home, and that view bound the home's files back writable, next to the socket of the gpg-agent that reads them. A bash session could therefore get your gpg-agent to run a program of its choosing outside the floor. In that home those files are now read-only to bash, with or without network, and to the file editor. A configuration file that is a symlink comes back as its file's content, read-only. One that leads into a directory the floor denies, or leads nowhere, is left out. If bash has network and such a configuration file is a symlink that leads nowhere, bash is refused, as it has been in `~/.gnupg` since 0.9.1. Found in Levain's own review, reproduced in the floor's plan, and checked on Linux by running bash under bubblewrap.
- **Still open, as 0.9.1 stated:** a gnupg home that Levain is not told about, and where it finds no dirmngr, keeps writable configuration files, on Linux and macOS. That closes before Levain 2.0.
- **The cockpit's signing keys are now behind the floor.** `levain cockpit key create` puts the keys that sign T2 and T3 writes in `<levain home>/keys` (`~/.levain/keys`), and 0.10.0 did not deny that directory to bash or the file editor. A bash session could copy a passphrase-protected key and guess the passphrase offline. It could also replace a new key between `key create` and `enroll`, so the touch you gave to enrol it enrolled a key bash chose. The directory is now denied, read and write, like `~/.ssh`. Known edges: a directory you pick with `key create --dir` is not covered, and `enroll` still accepts the new key's public half without a signature from that key, so a program running as you outside Levain can make the same swap. The README's `passphrase` custody already says it is no defence against such a program.
- **A FIDO key now needs a touch for a cockpit write, as it already did for an authority link.** OpenSSH's signature check does not enforce the touch itself. A key made with `no-touch-required` had its authority links refused but could sign a T2 or T3 write without a touch. Both checks now use one rule.

## [0.10.1] — 2026-10-12

### Changed

- **A cockpit write fires on one click.** A write that may rise above its rendered tier still asks the server first (`dry_run`) and fires in the same click, naming the tier and gesture that dry run answered (`tier_seen`, `gesture_seen`); the server still refuses `409 tier_moved` / `gesture_moved` when either has changed. The second "Click again to fire" click is gone: a click is never the authority (the surface token in a reported install is, or the signature), and the tier and `claims cockpit token` stay in the audit record. A write whose answer was lost (or came back 5xx) keeps its envelope, and the next click sends that envelope again before any new dry run, so a write that landed returns its own answer; identical submits while one is in flight fire once.


### Fixed

- **The cockpit header keeps its weather in Build mode.** Build mode dropped every `reading` panel, the hero's weather included; anything placed in the hero now shows in every mode.
- **Cockpit rendering after the 0.10.0 launch:** a count widget's zero is final only on a clean, fresh read, and a zero-row read whose status is `empty` is a clean zero; panel address lines follow the panel's declared facets, not row values; the hero owns the sky's legend band, and a dropped or failed sky hides it; the Health panel leads with links, wraps, graduated and demoted; a row's meta line wraps inside its card at phone width; the Now panel carries the same filter box as Tray, Open loops and Keep; an inbox row draws sender -> reader from its own facets.

## [0.10.0] — 2026-10-12

### Added

- **The roster kernel computes health per kind and the Fleet card draws it grouped.** Each shown roster row carries `fleet_group` (`resident` / `on_call` / `roundtable`, the roundtable's seats fold into one line) and `health: {tone, text, at, memory}`: only a resident member whose silence is a signal can be an `alarm` (no episodes in 24 h, write path unavailable, or a `digest.last_wrap_at` older than `wrap_every`, resident + stateful only); an on-call member's last call, failed or not, is a neutral fact. A roster is no longer cut at the list default of 10 rows. Health is derived at render against the cockpit's clock, never stored; a resident whose evidence is missing or unreadable (no wrap time, no liveness data, a bad `episodes_24h`) reads `unknown` with the reason, never `ok`; an unreadable or zero `wrap_every` and a non-on-call roundtable member are refused.
- **The cockpit contract gains `hero`, `brief` and `widgets` regions, a zone grid and reading tokens** (still `levain.cockpit/2`; a current provider registers and renders unchanged). The hero holds one `reading` and one `ambient`; the brief holds `line`s and `brief`s; a widget is one answer, so a `list` there must declare `face="count"`. A zone panel takes `size`, `span` and `tabs` (same-zone panels sharing a `tabs` title are one card), and the manifest's `regions` carries `cards` beside the flat `panels`. The legacy `header` region is normalised at registration (a reading or ambient to the hero, a line or brief to the brief, anything else to `operate`) and `regions.header` stays as a derived compat key. A `reading` may send a `symbol` from a fixed token set and a sub-reading a `detail`; the band tokens are named in the contract.
- **A list panel may carry `compose: {verb, fields}`, and the inbox compose draws on the list's own card.** `ProviderSpec(compose=...)` names a verb the panel offers and typed fields of the form kind (`text`, `recipient`, `date`); registration refuses a verb the panel does not offer (or an empty one), an unknown field type, a recipient without its `enum` or with a blank or repeated id, more than one recipient field, a recipient whose facet or `source` the panel does not declare, or a compose on anything but a list or on a count face; once the verb registry is attached (at attach, at every later registration and on rediscovery) it also refuses a verb the registry does not hold, a row-bound or job verb, and a field outside the verb's `fields`. The declaration is stored detached, covered by the panel etag, and replaced (or refused, offering nothing) on rediscovery. The panel head carries the declaration. The page draws recipient fields as chips (none selected sends no value for the field: "none selected = everyone", said in words on the card; a chosen value that trims to nothing refuses the send), sends through the panel's own verb to that panel only (a panel that no longer offers it refuses; another panel's offer of the same verb is never used), reads unavailable on a writable surface whose panel does not offer the compose verb, refuses a field type it cannot draw visibly (the panel reads unavailable with the reason, never a dropped field), and draws every row of a list that composes to recipients as `source -> for` (`everyone` when the row names no reader), its meta after the address. The facet `for` is registered (readers, comma-joined).
- **`GateHandle`** (`levain.autonomic.handle`). A process holds one handle over its current `EfferentGate`. `swap(manifest, binding_catalog, aggregate)` builds a new gate on a frozen copy of the catalog and publishes it in one step; pending holds, receipts and the journal carry across. `FireDispatcher` and `ChainExecutor` read the handle once per admission, so an admission in flight completes on the gate it started with.
- **Every cockpit signing key states its custody, and `levain cockpit key create` makes a laptop-presence key on the hardware you already have.** Levain never requires bought hardware; a FIDO key is an optional upgrade. Each `enrol` link of the authority log carries `custody`, one of `touch-id`, `passphrase`, `tpm`, `fido`, `windows-hello`, `fido-attested` or `unstated`, signed with the rest of the record; `levain cockpit keys` prints it beside the class on every row, and `enroll` / `ledger init` take `--custody` (default: the key's sidecar). It is a declaration by the enrolling key, never a proof, and nothing here proves it: the verifier cannot tell a key made to require a touch from one made without (the Secure Enclave provider macOS ships echoes the presence flag it is handed), so a touch rests on how the key was made. The replay holds each declaration to the key types it can be true of: `touch-id`, `fido`, `windows-hello` and `fido-attested` need an `sk-` key, `tpm` a plain `ecdsa-sha2-nistp256`, `passphrase` a plain `ssh-ed25519` or `ecdsa-sha2-nistp256`. A laptop-presence key must state a real custody; `unstated` is for display-phone and presence keys and for a key `ledger init` carries over from 0.9.x. `fido-attested` is refused until attestation at enrol is built, with the governed install; `windows-hello` is a reserved label (levain runs on macOS and Linux). What each custody guarantees:
  - `touch-id`: a fingerprint per signature on a key that cannot leave the Secure Enclave, as created by `levain cockpit key create`. Declared, not proven: a Mac key carries no attestation.
  - `passphrase`: a typed passphrase per signature, and no defence against same-uid malware, which can keylog it or copy the key file. The lowest class, and the one that works on any machine.
  - `tpm`: a non-exportable key, and no presence.
  - `fido`: a touch on a key you plugged in, custody declared.
  - `fido-attested` (later): the only class proven.

  `levain cockpit key create --custody touch-id` (macOS) makes a Secure Enclave identity with Touch ID required (`sc_auth create-ctk-identity -k p-256-ne -t bio`; levain offers no way to make one without), confirms the identity is listed with protection `bio` before anything else happens, and fetches its key reference with `ssh-keygen -K -w /usr/lib/ssh-keychain.dylib`; any failure or interrupt once the create has started deletes the identity that run made (told apart by listing the identities before and after, deleted by its hash, never by label, so another run's identity is never touched). `--custody passphrase` makes an ed25519 key with a passphrase `ssh-keygen` asks for on the terminal, and refuses the key if the file is not encrypted (an empty passphrase). With no `--custody`, `key create` chooses `touch-id` on a Mac where `bioutil -r` shows Touch ID configured for unlock and `passphrase` otherwise, and prints which it chose and why. Keys go in `~/.levain/keys/` (`$LEVAIN_HOME/keys`, made 0700, or `--dir`), each beside a sidecar naming its custody and provider, so signing with a touch-id key needs no `SSH_SK_PROVIDER` (one set in the environment still wins). A passphrase key is always signed from its file, never through an agent, so every signature asks the passphrase. Every signature is checked against the signing key the moment it is made, so a cancelled Touch ID prompt (which lets `ssh-keygen` exit 0 without a signature by the key) is refused as `Touch ID was cancelled or did not complete`, and nothing is signed; a link that fails to verify when the log is replayed still reads `the signature does not verify`. The same check refuses, at signing, an `sk-` signature without the user-presence flag, which the replay already refused. A hand-made key enrolled with `--custody passphrase` gets the same encrypted-file check; a `touch-id` key whose identity is on this Mac must still be `bio`, and `levain cockpit keys` re-checks that on its row.
- **`levain cockpit ledger init`** writes the authority log's first links, each signed by `--signing-key`, a laptop-presence key of any custody (`--custody`, default the key's sidecar; a touch-id or fido key asks a touch per link, a passphrase key its passphrase). On a new store it enrols that key, signed by itself: trust on first use. On a store upgraded from 0.9.x it lists the keys the store held (fingerprint, class, key type, when enrolled) and re-enrols each one you confirm, the signing key included; a key that was revoked is enrolled and then revoked again, a key whose type its class does not accept (a laptop-presence key of a type no custody admits) is listed and skipped, and the rest are dropped; a carried key is re-enrolled with custody `unstated`, and the signing key with the custody you state. At a terminal it asks per key; otherwise pass `--confirm <fingerprint>` for each key to keep and `--drop-unlisted` for the rest, and with neither it refuses. The store reads initialised only once every link has landed: a run cut off part way leaves every signed act still refused, and running it again finishes the job. Ctrl-C at a prompt or during a touch ends it with `ledger init cancelled`, saying where the log stands once a link has landed.
- **`levain doctor` warns while only one laptop-presence key is enrolled** in the install's cockpit. Losing that key leaves nothing that can enrol another, so enrol a second laptop-presence key with `levain cockpit enroll` while you still hold the first. Recovery after losing the only one (`levain cockpit ledger re-genesis`, a new authority log) is not in this build. It is a warning, not a failure: the exit code is unchanged. A cockpit journal of a format this levain neither reads nor migrates (missing, unknown, or newer than `levain-cockpit/3`) is a failing row.

### Changed

- **The cockpit page draws the KE regions.** A manifest that declares `hero`, `brief` and `widgets` is drawn as the hero first (the clock column with the weather under it, the sky beside it, the mode / unit / live / jump strip above the sky), the brief, one equal-width row of 138px widgets, then each zone as a fixed 3-column grid whose rows are as tall as their tallest card (tall 540, medium 460, short 190 px) with `span` and `tabs` honoured; side gutters are 64px and the gap 24px, one token each. Verbs are 12px neutral buttons shown on the row you are on, and tags are plain meta text, so the accent is left to the now-line, failed reviews, overdue tags and a compose's one primary button. A reading draws an inline icon for its `symbol` and a sub-reading with a `band` as an AQI pill. The `operate` zone's title is now "Work" (its id is unchanged). A manifest with no KE regions renders as before. Every panel and widget carries `data-ck-panel` and `data-ck-kind`.
- **The KE page draws a region's alerts, notes and every contract shape, keeps tabs and panels in step, and clears when the data goes.** The hero, brief and widget regions draw a panel's banner (stale, partial, an alert), its note and its markdown, line or typed content; an alert suppresses the empty message. A status-dots widget draws up to 4 dots and 4 figures (a figure may be a whole, a fractional or a written value) and counts the rest in a "+N more" line. A card's tab row and its panels come from the same list, so a panel that cannot be drawn keeps its tab. Each region builds its content off the page and attaches it last, and a region that fails shows one error line in its own place and hands the masthead and the state line back to the static page (a failed hero also empties its weather and sky, so no stale reading sits beside the error), so one failing region no longer blanks the page or strands those nodes (the next good render recovers with no reload), and a null render clears the regions. Row verbs (spore, episode and pending-cancel rows alike) stay in the tab order and show on focus (and always on a touch screen); the edit chip and the expand control show on the card you are on.
- **The `levain run` REPL no longer reads your standard credential stores by default.** They are the stores the README lists under *Chat with a sovereign entity from the cockpit* (`~/.config/gh`, `~/.aws/credentials`, `~/.netrc` and the rest, and on macOS the Keychain). Chat, `--task` runs and scheduled seats already denied them; the REPL was the one drive that let the entity's shell and file editor read them, on the grounds that you read every turn. You read a turn only after it ran, so a read a prompt injection asked for was already in the model's context. This is the default changing, not a new prohibition: an entity that needs `gh`, `aws` or the Keychain in the REPL sets `"deny_standard_creds": false` in its `.levain/confinement.json`, which reopens them in every drive, as it did before for the others.
- **The cockpit schema is `levain.cockpit/2`.** The `/1` kinds `triage-list`, `metric`, `visual` and `prose` are accepted as aliases of `list`, `status-dots`, `ambient` and `brief`, and the manifest and every panel carry the new names. Field names are kept, and v2 validates them strictly: a metric needs a non-empty label and, when it sends a value, a finite number or a string; prose needs markdown or lines, markdown must be a string, and every prose line must be an object with a string text; a `visual` value's text must be a list. A provider whose `/1` value was looser is refused until it sends well-formed values.
- **Cockpit signing keys live in a signed authority log.** Who may sign a cockpit write is a verified replay of `authority_log` in `cockpit.db`, read once per request; the `keys` and `policy` tables are gone. `levain cockpit enroll` and `revoke` append a signed link and take `--signing-key`: an enrolment, which records the key's class and custody (above), is signed by a laptop-presence key, a revocation by a key of at least the revoked key's class. `--compromised-since` takes an authority seq, which `levain cockpit keys` lists. A pending's fence names the log head it was issued under, so a pending still waiting when any link is appended ends STALE at its decision.
- **Breaking: the journal format is `levain-cockpit/3`.** A 0.9.x journal migrates at first open: its keys are set aside, waiting pendings end STALE, and every signed act is refused, naming `levain cockpit ledger init`, until `levain cockpit ledger init` enrols the keys again, each link signed by a laptop-presence key. A 0.9.x binary refuses a migrated journal. The migration runs only under the cockpit's `cockpit.lock`: while a `levain serve` (an old one included) holds it, any `levain cockpit` command, a read-only `levain cockpit keys` included, refuses and leaves the journal untouched, so stop `levain serve`, upgrade, and restart it; the restarted server migrates. A `levain cockpit` command migrates only a journal started as `reported`, and checks that before it writes anything.
- **The Claude Code host's self-test refuses an entity whose `activation/` is a symlink.** The self-test checks that bash cannot rename `activation/`, and puts the directory back if it was moved, by its own identity read without following a link, so a symlinked `activation/` (a link to a directory included, which was accepted before) is refused as not a directory. If the self-test cannot put the directory back, it now fails with an error naming where the directory was left, instead of reporting it only as a failed check.

- **The kernel's default cockpit layout is mock-e's.** Operate bands: Tray 10, Open loops 15, Recent episodes 40, Keep 50, Recent edits 60, Recent jobs 70 (16-39 are left for an embedder's inbox, fleet and stream). Mind: the Neocortex tabs card (one tab per heading, ranked from 0), Crystallized patterns 100, and a Cognition card (trace and projection history as tabs, 200-210). Health moved to `widgets` and the state line to `brief`; `regions.header` keeps its shape and now carries Health. An embedder that wants its old position re-ranks against the new bands. Until the KE2 page lands, the current page draws Health in Operate.

### Removed

- **`ChainExecutor.gate`.** The executor now holds a `GateHandle`, exposed as `ChainExecutor.handle`; read the gate with `handle.current()`, once per admission. A gate kept across a policy swap goes stale, so the executor no longer hands one out.
- **`EfferentGate.sweep_timeouts`.** The silence-default sweep is `GateHandle.sweep_timeouts`, which decides each pending on the gate current for it; a sweep held on one gate decided pendings on that gate's policy after a swap had superseded it. A bare gate sweeps with `as_handle(gate).sweep_timeouts(now)`.


### Fixed

- **A shell command that fails for want of a credential the floor denied now says it may have been the floor.** When the standard credential stores are denied (by default in every drive, the interactive REPL included) and, this turn, a shell command that failed printed a line naming one of those stores with "operation not permitted" (as gh, docker, kubectl, aws, twine and npm report a denied read), or git printed `could not read Username for 'https://` (or `Password`), the turn's activity ends with one note: this may have been denied by the standard credential floor; for git, use an ssh remote if your key is in the ssh agent; otherwise set `"deny_standard_creds"` to `false`, which opens every listed store and the Keychain in every drive, a scheduled seat's included. A private or mistyped HTTPS URL makes git print the same, hence "may". The Keychain is denied by its services rather than a path, and a denied read looks like a missing item, so it shows up only through git's prompt. The streamed activity of `levain run` shows the note too, and the chat panel lists it apart from the actions that ran. The run banner's line for the denied stores now ends "(set deny_standard_creds false to allow)".
- **An unreadable tool call is no longer hidden or reported as an answer.** A reply holding a tool call as text and then a `finish` call (`{"name":"terminal",...}` then `{"name":"finish","arguments":{"message":"Created x"}}`) was shown as "Created x", though the terminal call never ran; such a text is now unwrapped to its finish message only when every call in it is `think` or `finish`, and otherwise kept whole and flagged. A reply that is nothing but one fenced block holding call markup (`<tool_call><function=terminal>...`) is now flagged; markup in a fence beside prose is still an answer. So is Qwen3-Coder's `<function=name>` tag followed by a `<parameter=...>` tag or its own close, without the `<tool_call>` wrapper, which glm-5.2:cloud via Ollama sent once the wrapper had been consumed upstream, MiniMax's `<invoke name="...">` call, GLM's `<tool_call>name</tool_call>` for one of the entity's own tools, a GLM call cut after its first `<arg_key>` tag, and Kimi's `functions.name:0{...}` once its special tokens are stripped. A function-call JSON object naming one of the entity's own tools now counts among other text too, outside code (it counted only as the whole reply), and an indented code block alone is read like a lone fence. Each agent message of a turn is now checked on its own as well as their join, so a call that is a whole message after a plan (for example after the act-now nudge) is caught, as is a call split across two messages, and so is the text a model sends beside a parsed `finish`, which was never shown, even when the finish carries no message; the reply now shows that text before the finish message, or as the reply when there is none. Text beside an action that ran is not checked, since it often restates that call. A failure of the check itself now flags the reply instead of passing it, with its own notice (levain could not check the reply, so it is not treated as an answer) rather than one about the model, and the turn result and the chat API carry `unreadable_unchecked: true`. A `finish` call whose arguments are a JSON string (the OpenAI wire form) raised out of the turn; it is now unwrapped to its message, as are a finish in the OpenAI `{"type": "function", ...}` wrapper and an array of think and finish calls. A reply over 200 kB, which was not checked at all, is now searched without the Markdown parser or a JSON decode, so nothing in it counts as code and a large array of calls is not expanded in memory; such a reply that quotes call markup or call JSON in a code block is flagged too, so a headless run exits 7 for it. A JSON call is recognised whatever the order of its keys. `levain run --task` exits 7 for such a reply (it exited 0) and leaves stdout empty, with `--quiet` too, where the markup used to be printed as the payload; the notice and the text go to stderr. A turn result with an unreadable call is no longer `ok`, and the chat API's `exit_code` for it is 7. The REPL's gate no longer asks `approve?` over a hold that lists nothing: it can only be rejected, as in the chat panel.
- **The chat panel no longer offers Approve on a hold that can only be rejected.** A held action whose bytes the server cannot bind an approval to was shown with an Approve button, though every call in it was displayed in full; clicking it was refused as undecidable and left the turn's outcome to be checked by hand. The server now says whether a hold is approvable (`approvable` in the turn result and in `/chat/session.json`), and the panel shows Approve only when that is `true`; otherwise it shows Reject alone and says why.
- **On Linux, bash can no longer reach your gnupg dirmngr by renaming one of your directories (the 0.7.1 known limit).** **Requires** bubblewrap 0.5.0 or later (its `--perms` option); an older bubblewrap now reads unavailable, and `levain doctor` names the version. Your user runtime directory (`/run/user/<uid>`, and `$XDG_RUNTIME_DIR`) is now a fresh, empty directory inside bash, holding only your gpg-agent, keyboxd and ssh-agent sockets, bound back as single files. The rename that reached a running dirmngr from the next session now finds nothing to move: measured 2026-10-12 on an Ubuntu host with a packet capture on port 53, the attack put a fresh name on the wire 63 times before this change and 0 times with it, and the host directory was untouched. The same view hides your user service manager and session bus (which bash could ask to run a command outside the sandbox), replacing the masks that did that, and every other daemon socket in that directory, including one started after bash. gpg signing and `ssh-add -l` through the agents keep working. New setting, `runtime_sockets` in `.levain/confinement.json`: sockets of that directory to put back, relative to it (`["podman/podman.sock"]`), each of which hands bash the daemon behind it. `systemd/`, `bus` and `S.dirmngr` are refused there. A dirmngr whose socket sits in its GNUPGHOME instead (one started where `/run/user/<uid>` did not exist, such as from cron before linger, and still running) gets the same treatment in bash without network: that GNUPGHOME is a fresh directory holding its files and the agent and keyboxd sockets, never dirmngr's, and the directories above it cannot be renamed. Measured the same day, same host: renaming that GNUPGHOME, or its parent, reached dirmngr from the next session (60 names on the wire each) before, and failed with "Device or resource busy" (0 on the wire) after; gpg listed keys and signed through the host's agent and keyboxd. gpg inside bash cannot `--import` into such a home, and its lock files there are not shared with gpg outside bash. A GNUPGHOME that holds the entity's workspace, or sits inside `~/.kube`, `~/.docker`, `~/.aws` or `~/.config/git` when the standard credentials are denied, is refused with a message naming the dirmngr to stop. A runtime directory that holds the workspace, the entity or your home (`XDG_RUNTIME_DIR` pointed at one of your own directories) is refused too. On GnuPG 2.4 with `use-keyboxd`, bash now reaches your keyboxd (it used to start a second one on the same keyring). `GNUPGHOME` now survives `levain run`'s restart, so these sockets are found for a non-default home. Known limit: an agent restarted during a session is unreachable from that session's bash.

## [0.9.1] — 2026-10-12

A security fix. Upgrade if you use gpg on a machine where Levain runs bash.

### Security

- **Bash can no longer write gnupg's configuration files.** Bash runs as you, so it could write `gpg-agent.conf` and gnupg's other configuration files. Your gpg-agent runs outside Levain's floor and starts programs those files name, so a bash session could get a program of its choosing run outside the floor, with your network and your files. `gpg-agent.conf`, `scdaemon.conf`, `gpg.conf`, `dirmngr.conf` and `common.conf` are now read-only to bash in `~/.gnupg`, and in `$GNUPGHOME` when Levain's own environment sets it to an absolute path. gpg signing and decryption through your agent keep working. Found in Levain's own review and reproduced on Linux; the fix was verified there against the same reproduction.

## [0.9.0] — 2026-10-12

Two things ship together: the signed team tier (team ledgers signed by SSH keys, the team replayed from signed history) and the live cockpit (eleven panel kinds, a page that redraws only what changed, an ambient sky).

Team ledgers are signed, and the team is derived from the ledger's signed history. **Breaking: 0.6.x team ledgers are not read.**

### Changed

- **A member is an SSH key, not an email.** Every commit on the team ledger is signed with the writer's SSH key (ed25519 or ecdsa), and levain verifies each signature by running `ssh-keygen -Y check-novalidate` itself, so `gpg.ssh.allowedSignersFile`, `gpg.ssh.program` and similar repository settings cannot change a verdict. A signature that fails to verify reads as unsigned, provided a known-good signature still verifies on the same machine at that moment; a check that cannot run (no `ssh-keygen`, a timeout, an `ssh-keygen` that fails on the known-good signature too) is never cached and never read as signed or unsigned, and the hook then denies edits it cannot judge.
- **New branch, `levain-team-ledger`, with `tenure.toml` beside `team.toml`.** The genesis commit carries both files and is signed by the owner's key that `tenure.toml` lists. `levain team init` and `levain team join` take `--signing-key`. `join` pins the genesis and prints the owner and key fingerprints in force, to check with the owner out of band. Running `join` again keeps the clone on the genesis it pinned, with its distrust list; it moves to another genesis only when `--root` names it.
- **The team is replayed, not read off the tip.** A change to `team.toml` or `tenure.toml` counts only if the key that signed it may make that change at that point in history, and it must name the last counted change (`Levain-Base` trailer). A ledger line is enforced only if it was signed by a key in force, at that point, for the member it is filed under (the owner, for pack lines). `levain team status` shows recent role changes, a pending hand-off, keys waiting for confirmation, and the reason when verdicts are frozen.
- **Keys:** `levain team key add | confirm | remove`. A proposed key comes into force only when a commit signed by that key confirms it (`join`, or `levain team key confirm`, on the machine holding it). A member proposes their own keys from a machine already in force. The owner proposes only a member's first key, and a removed member's handle is never added again, so no key the owner holds can come to sign as a handle that has signed before. A member who lost every key is invited again under a new handle.
- **Ownership:** `levain team owner <handle>` offers ownership to a member and `levain team accept` takes it, signed by a key already in force for that member. An offer carries no keys, and it lapses if its member is removed.
- **Recovery:** `levain team revoke <fingerprint> --after <commit>` (the owner) voids every line a stolen member key signed after that commit (the honest holder's included), which must be in the current owner's spell (an earlier one is refused, with the reason). An owner key is never revoked in the ledger: whoever holds a stolen owner key controls the ledger until a re-genesis. `levain team distrust <commit>` stops this clone counting one commit at once. `levain team veto <handle> --role member|owner --since <commit that began the spell>` (the owner) withdraws a past spell's authority: for a member's spell, every line that member's folder gained during it stops being enforced, which removing the member or revoking a key does not do for lines already signed; for an owner's spell, only its authority to supersede or retire others' entries (its pack lines and `PROJECT.md` still count). `--since` must be the commit that began the spell, or the veto is refused. A merge on the ledger branch freezes verdicts (from the last commit this clone judged before it) until `levain team accept-merge <merge> --parent N`, run on the owner's word (`--parent` is required, and `join --accept-merge` takes `SHA:N`: which parent counts is never a default). A sync re-publishes only commits this clone wrote (each carries a `Levain-Device` trailer), never one another clone of the same key published, and it never re-signs entries with a key that is not in force: those wait under `refs/levain/held/` until `levain team key confirm`; while the ledger is frozen, a held entry is re-signed only if the key counts both in the frozen view (the last commit judged) and in the published history judged with no anchor, as a clone joining now would judge it. Held entries and offline team changes are bound to the genesis they were made on: after `join --root` to another ledger they are kept and named, never replayed or re-applied there. A history rewrite is detected and never re-published; verdicts stay frozen at the last commit judged on the old history until `levain team repin`. `levain team regenesis` starts a fresh signed genesis on a new branch when the owner is permanently gone or the owner's key is stolen. The old ledger files at the old tip are carried as a read-only record that is not enforced (rulings are recorded again), and whoever runs it is the new owner, signing it with their own key (it runs only from a joined clone, from a commit of the ledger that clone is pinned to and has judged (an ancestor of its anchor or of its last counted team change), and an `--owner` naming a handle the old ledger knows is refused unless the runner's key is in force for it), so members check the fingerprints `join --root` prints. `levain team status` shows a re-genesis branch with its claimed owner and keys, as unverified.
- **0.6.x ledgers:** levain v2 refuses to read the unsigned `levain-ledger` branch. The owner starts again with `levain team init --replace-legacy`, re-records the entries to keep, and deletes the old remote branch with `levain team retire-legacy`.
- **`levain team export` writes anneal's team stream contract v3** (the `prev_root` header and the strict profile). Its import into anneal-memory waits on the anneal release that reads v3; until then the session-start import uses the earlier stream and says that an anneal-memory release reading stream v3 is needed (the version is named once it ships).

### Known limits

- A git host can withhold newer ledger commits from a clone, and the clone cannot tell. Signatures prove who wrote what the clone sees, not that it sees everything. The planned fix is the team server's transparency log.
- The owner can invite a new handle under any key the owner chooses, as with any admin-run identity system: the fingerprint check at `join` is a person's job.
- Role-change lines in `levain team status` are filtered by the commit's author date, which the writer sets.
- While verdicts are frozen (a merge, a rewrite), nothing recorded after the freeze counts, team changes included (a revoke made then is not in force), and the edit hook does not stop the edits those waiting rulings govern; `status` counts the waiting lines but does not list them. The hook denies only when it cannot judge at all, unless `LEVAIN_TEAM_UNJUDGED=allow`.
- A clone holding only a 0.6.x `levain-ledger` enforces nothing until the owner migrates (edits go through with a one-line notice).

### Cockpit: eleven panel kinds, a live page, an ambient sky

The cockpit's panel vocabulary grows from four kinds to eleven, the page stays current without a reload, and the board gets a new look. The schema string is now `levain.cockpit/2`; a server written against `/1` keeps working (see "Changed").

#### Added

- **Eleven panel kinds.** `reading`, `line`, `brief`, `count-queue`, `ambient`, `status-dots`, `metric-grid`, `roster`, `list`, `form` and `log`. Each has a fixed shape (written up in `docs/cockpit-contract.md`); data that does not fit makes that one panel show an error naming the panel and the field, never a half-drawn card. The terminal view and any renderer without a component for a kind draw it as text.
- **A filter box on long panels.** Any panel that is a list, a queue, a roster or a log gets a filter box once it holds more than 8 rows or declares a window. Type to narrow; terms are matched ignoring case and accents, all terms must match, and the text is plain (`.*` means those three characters). A panel that shows only the newest N says so ("searches the newest N only") unless its provider can search the whole store, in which case the box asks the store and labels the result ("41 matches in the store"). A search that fails or times out says so; it never reads as "no matches". Search text is kept out of the server's journal and its access log.
- **A live page.** The page opens one change stream (`GET /cockpit/events`) and redraws only the panel that moved, in place, without a reload: your scroll position, an open filter and a "show all" stay where they were. The stream carries no panel data, only which panel changed. A small chip in the header says where it stands: `connecting`, `live`, `paused` (the tab is hidden; a hidden tab closes its stream and catches up when you come back), `stale since HH:MM` (the server went away; the page retries with a growing delay and polls every 30 seconds meanwhile), or `not live` (the page never got a first draw). A server holds at most 8 streams at once; a ninth is told it is busy.
- **The ambient sky.** A panel of kind `ambient` draws the last 14 hours of events (commits, reviews, memory writes) as a timeline: a row per repository with the busiest rows named and the rest folded into "other", failures in orange, and a short meteor when a new event lands. An "Ambient: on / paused" button stops the motion, and the page honours the operating system's reduced-motion setting. The animation stops entirely when nothing is moving.
- **A mode bar and a jump box.** Morning, Board and Build modes in the header, and a jump box (Cmd-K) that goes to any panel by name.
- **Read-age chips.** A panel shows how long ago its data was read, and the chip updates in place when the panel does.

#### Changed

- **`levain.cockpit/2` renames the four `/1` kinds.** `triage-list` is now `list`, `metric` is `status-dots`, `visual` is `ambient`, `prose` is `brief`. A provider that still declares the old name is accepted and its panel carries the new one; the data shapes of the four are unchanged. The schema string is part of every ETag, so a browser holding an older cockpit's copy fetches a fresh one rather than reusing it.
- **The board is redrawn panel by panel.** A panel that changes shape (a list that becomes a card, say) is rebuilt; one that keeps its shape is updated where it stands.

#### Known edges

Each of these was found in review, judged not reachable in normal use, and left alone. None loses data.

- **Searching and the filter box**
  - A search with a pasted trailing newline is accepted as its trimmed text.
  - Searches are kept out of the access log by the name of their parameter (`q=`). A query sent under an escaped spelling of that name (`%71=`) is logged. Only someone typing such URLs by hand in a verbose local log reaches it.
  - A query made only of accent marks matches every row.
  - A search URL with an unencoded space in it can give a less helpful error message.
  - In the filter box the text survives a panel update only through the browser's session storage; with storage blocked, the filter clears when the panel redraws.
  - After a panel updates, old store-search results stay on screen until the new search returns; there is no "searching" flash.
  - The jump box shows a limited number of entries, which can push a "Search ..." entry out of view.
- **Panel data a provider could send**
  - A panel value that sends an explicit `null` for an optional field (`value`, `unit`, `read`, `silence_is_signal`) is read as if the field were absent.
  - A `brief` that carries markdown still has its `lines` checked.
  - A panel declared with `legacy_kind` is trusted: that argument is for our own code.
  - A provider's `as_of` that is not a valid timestamp makes `/cockpit/freshness.json` answer 500. This is how that route behaved before this release and the live page no longer reads it.
- **The page**
  - A panel read for the first time with no read time has no read-age chip until the next full draw. A panel opened in the focus window shows no chip at all.
  - An expanded long row folds back when its panel redraws.
  - A stale panel's banner age is part of its shape, so a stale panel that changes redraws as a whole.
  - A panel whose update loses its typed form leaves the old body in an open focus window until you close it, and the window's title is not refreshed.
  - Reached from another machine, after the write token has been rotated, two requests refused at the same moment can each raise the token prompt.
  - A panel that is not given a mode forces the Board view when you jump to it.
  - Update events arriving faster than the page can fetch can delay the repaint; the server folds them per panel, which keeps this rare.
- **The sky**
  - Event times must be extended ISO-8601 (`2026-10-12T01:00:00Z`) or carry no zone, in which case they are read as UTC. The compact form (`20261012T010000Z`) passes the contract but is not drawn correctly.
  - A feed that sends a `null` event, a repository named `__proto__` or `constructor`, or a non-numeric window length breaks the sky; our own feeds never do.
  - On time zones that are not on the hour (half-hour offsets), the hour labels sit slightly off.
  - An event dated in the future never gets a meteor.
  - Opening one repository's view can still draw meteors for other repositories.
  - A browser without `ResizeObserver` does not remeasure the label gutter when the window resizes.
- **The live stream**
  - The watcher reads panels under one access class. A change visible only to another class (a change in what a signed-in action may do) is not announced by the stream; the page picks it up on its next reload or reconnect.
  - A server whose connection is blocked mid-write notices a gone browser only after the socket timeout.
  - The first read when a stream opens on an idle server holds the watcher's lock while it runs, which can delay the first announcement.
  - After a stream comes back, the chip says `not live` until the first message arrives, a matter of seconds.
  - Two refetches of one panel that overlap, with the older answer arriving last, followed by a disconnect and an exact revert of the panel, can leave that panel showing the older content until its next change.
  - A refetch in flight when the tab is hidden that then fails keeps retrying on the hidden tab, on the normal retry schedule, and is not carried over to when you return.

## [0.8.0] — 2026-10-11

The cockpit's write side: K2a (one write route, the write token, writes bound to what was rendered) and K2b (the broker, which turns a write that needs more than the token into a consented, journalled fire). They ship together.

### Added

- **The cockpit broker.** A T2 or T3 write from the cockpit no longer fires inside the request: it is issued as a pending that holds a consent record (what will change, on which row and version, at which tier, and when the pending expires), decided in one transaction, then applied store by store and finished. Every step is journalled in `cockpit.db` under the install's cockpit data root, so a crash at any point is recovered at the next start and an apply that hit a transient fault is retried in the background, with a backoff that caps at two minutes. One broker owns a data root (an exclusive lock on `cockpit.lock` beside it); a second server on the same root waits in standby and its broker-backed reads say so.
- **Signing a pending.** A pending waiting for a signature shows on its row with a cancel button. It is signed with an enrolled OpenSSH key over the record's exact bytes (`ssh-keygen -Y sign -n levain-cockpit`) and the signature is posted to `/cockpit/pending/<id>/signature`. An unsigned pending expires after 15 minutes, and each key holds a bounded number of waiting pendings.
- **`levain cockpit enroll | revoke | keys`** manage a reported install's signing keys: a `laptop-presence` key signs T2 and T3, a `display-phone` key T2. The last laptop-presence key is revoked only with `--compromised-since`, and from that time on its signatures stop reading verified.
- **A reported install** (`levain serve --install-class reported`) may fire a T2 write on the write token alone; the write is recorded as reported and the page labels it `claims cockpit token`. T3 always needs a signature. A governed install (a root-written marker) is not built yet: a governed marker disables the broker and every write route says so.
- **An episode can be tombstoned from the cockpit** (the entity's own episodes; T2, irreversible, confirmed first), and **the neocortex State section is editable again**, written through anneal's own section writer on anneal's section version (anneal-memory 0.9.43's `Store.replace_section`), so an edit against a section another writer changed is refused.
- **Recent edits shows the broker's applied writes** beside the ledger's, and the decisions panel shows the broker's waiting pendings.

### Changed

- **One lock protocol for levain's two single-writer locks, and the install lock now fails closed.** The install lock (`.levain/install.lock`) and the codex home lock (`<CODEX_HOME>/levain.lock`) are one `flock` code path. Where `flock` answers "not supported" (some SMB, WebDAV and old FUSE mounts), both now refuse before writing anything but the empty lock file, which a re-run treats as no content; set `LEVAIN_UNLOCKED=1` to run without the lock when no other `init` or `update` writes that install or codex home at the same time. `LEVAIN_CODEX_HOME_UNLOCKED=1` still unlocks the codex home lock, but the install lock is taken first, so it needs `LEVAIN_UNLOCKED=1` too. Before this, the install lock went ahead unguarded there: reproduced on Linux with `flock` answering "not supported", 6 of 20 concurrent `levain init --force` pairs into one install failed mid-install; with this change all 20 refuse before writing. Every platform here means macOS and Linux, the two levain supports.
- **The lock holds across NFS clients, and a killed holder never leaves it stuck.** Measured on an NFSv4.2 export with two clients: 19 of 20 concurrent `init --force` pairs had one run refused as busy and none failed, including with one client on levain 0.7.2. A holder killed by SIGTERM or SIGKILL released the lock at once: the kernel owns it, so there is no stale lock file to clear.
- **Every write to the web cockpit needs the write token, on loopback too.** `levain serve --write` prints a token for this server run and a link that carries it (`#write_token=...` in the fragment, which the browser never sends anywhere); the page takes the token from the link and keeps it. A write with no token, or the wrong one, is refused `403 credential_required` before its body is read. Measured on Linux: another local user (`nobody`) posting to a `levain serve --write` on the same machine got 403 on the cockpit write route, on every `/edit` kind and on `/action`. A server given its own tokens (`make_server(surface_tokens=...)`, one per surface) refuses to start if two surfaces share one, or if a token is not printable ASCII without spaces. Under `levain daemon install` the token changes each time the daemon starts and is printed only to the daemon's log; `install` now says where that log is, and creates it (and the error log) owner-only, because systemd creates a missing `append:` log readable by other users (0664, measured on Ubuntu 24.04). On Linux levain's log directory (`~/.local/state/levain`) is made owner-only too, so a log deleted or rotated later is still private.
- **`levain init --web` needs its write token too.** Another local user could post the onboarding form to `127.0.0.1` while `init --web` was running, and its answers became your new entity's seed (measured on Linux: user `nobody`, 200, the seed written). The command now prints a token for this run and a link that carries it in the URL fragment; the form sends it, and a post without it is refused `403 credential_required` before anything is read or written. If the link opens without its token, the form asks for the printed one.
- **An off-box cockpit no longer shows "Chat is unavailable".** The chat panel asks `/chat.json` whether the server has chat; a writable server bound off loopback (which never has chat) answered that with the read token's 403 instead of 404, so the panel showed an error instead of staying away. It now answers 404.
- **Writes go through one route, `POST /cockpit/verb`, and `/edit` and `/action` are aliases of it.** The kernel decides each write's tier from the stored row, never from the caller: a C1 write (touch, snooze, a retier that does not park, a resolve, your state line) fires; a write that needs your signature or your laptop (T2, T3) is refused "needs the broker" until K2b. A panel offers only its own verbs, and a write names the row version or value version it was shown: a row changed since, or a value changed since, is refused `409 stale`. For a spore, that compare runs inside anneal's own write transaction, so a change that lands between the server's read and the write is refused too.
- **`/edit` bodies now bind to what was rendered.** An `operator_state` edit carries `panel_version` (the page sends the `version` its state line came with; the first line of a new install sends `"absent"`); a `spore_descend` carries the `expect_disposition` it was rendered with; an `/edit` on a spore that no Tray, Open loops or Keep panel shows is refused 409.
- **The state line has one lock.** `levain state`, the TUI and the web cockpit take one lock on the context file (on its real path, so a symlinked file and its target share it); the cockpit's version check runs under it. Where the lock cannot be taken (a filesystem without working locks), the state line is not written and the command says why. A writer outside levain does not take it.
- **Requires anneal-memory 0.9.43 or later** (the spore compare uses `expected_version`, first in 0.9.42; the cockpit broker is built and tested on 0.9.43).
- **Open loops comes before the Tray**, and the Tray has a **By age** view: the same box, the same rows, oldest first, with each item's age. The page switches between the Tray's order and By age without re-sorting anything itself: the server sends both orders.
- **The Episodes panel can show other agents' episodes.** An embedding server passes `episode_feeds={name: read}` to `build_default_cockpit`; each feed's rows appear with their agent, and the page gets an agent filter. A feed that fails, or sends rows that are not episodes, is named on the panel and shows nothing, and the entity's own episodes stay. Only the entity's own episodes can be tombstoned. The entity's own episodes are its newest few dozen, and the panel says so; other agents' rows are not cut to that window, so a busy entity never hides them.
- **The web dashboard draws itself from the cockpit manifest by default, and writes through it.** The legacy page is `/?source=substrate`; its writes post `/edit` and `/action`, where a T2 write is refused because it fires only through the broker. On the manifest page every affordance posts `/cockpit/verb` with the row it was shown. A write that may rise above its rendered tier first asks the server for a preview (`dry_run`), shows the tier it rises to and what it does, and fires the exact request you saw only on a second click; a fire that names an `intent` must also name the previewed `tier_seen` and `gesture_seen`, and the server refuses `409 tier_moved` / `gesture_moved` when either has changed. A T2 write in a reported install is labelled `claims cockpit token`. Each fire above C1 carries one idempotency key, kept for the tab, so a retry after a lost answer lands once. A pending your signature is waiting on shows on its row with a cancel button. The manifest names the server's write capability (`transport: {writable, write_token_required}`), and a resolve or promote action names the kinds its row's type takes (`options`). A row whose text holds characters the page cannot show faithfully (a zero-width space, a line separator) offers no inline edit; edit it with the CLI. Rename shows only where the cockpit offers it.
- **A spore's domain is editable** (`spore_update` takes `domain`; the inline edit has a domain field). The broker refuses a type change on a Keep note or an open loop, as the legacy route does.
- **A downstream verb can fire through the broker.** An embedding server passes `make_server(extra_effects={verb: DownstreamEffect(store, canonical)})`: an `EffectStore` and its `canonical_effect` for that verb, checked at construction. An `ActionVerb` may declare `fields`, which the manifest publishes and the route enforces.

### Known limits

- A filesystem with no lock support at all cannot be locked: there levain refuses unless `LEVAIN_UNLOCKED=1` says no other writer runs.
- An NFS mount with `local_lock=flock` or `local_lock=all` keeps the lock on that client, so two clients do not exclude each other.
- Two NFS clients that present the same NFSv4 client identity (cloned machines or containers sharing a hostname and client identifier) are one client to the server, and their locks do not exclude each other; measured with two network namespaces sharing the host's identity.
- On a Linux NFS client the lock belongs to the process, so two threads of one process that each take it do not exclude each other. No levain caller does that: the web init server runs one install at a time behind its own lock.
- The TUI's spore writes are compared on the item's list (`expect_disposition`), not on its version.
- Without `flock` (Windows), a version-bound write (the cockpit's state line and spore writes) is refused; an unchecked `levain state` writes as before.
- A downstream verb with no `extra_effects` entry is T2 and has no broker effect, so it is refused (`needs_broker` on `/action`, `not_built` on `/cockpit/verb`). An embedding server that sent messages through `/action` before this release needs an effect store for that verb.
- The preview binds the tier and the gesture, not the params; the page sends exactly the params it previewed.
- A downstream verb declared `job=True` runs no job. The broker applies the verb's effect store once and never calls its handler; `/action`, the route that runs handlers, refuses a T2 downstream verb. Asynchronous downstream work has no path through the broker yet.
- An embedding server's downstream write verb that worked through `/action` before this release (a message send, for example) is refused after the upgrade until that server passes an effect store for it (`extra_effects`). Upgrade the embedder and levain together.
- The page does not sign a pending yet: a T3 write, or a T2 write outside a reported install, waits until something posts an `ssh-keygen -Y sign` signature over its record. The full pending card comes with the governed install. `levain cockpit sign` does sign one from the terminal (added after 0.10.4; in this release neither the page nor the CLI signed).
- **Erased means erased at the API.** A tombstoned episode's content can remain in freed pages of the SQLite files and in anneal's tombstone record (a content hash), and a State section edit keeps the section's previous text in `cockpit.db` with no retention bound.
- A write that touches more than one store takes no lock across them, and a store records no receipt per effect: the broker's journal is the record of what it applied.
- In a reported install the write token's secret has no levain-owned file: a server mints a fresh token each start, and under `levain daemon install` it is printed only to the daemon's owner-only log.
- An embedding server that calls `make_server` itself with neither `write_token` nor `surface_tokens` gets a fresh write token on each start, kept only on the server object (`levain serve` prints its own). Pass `write_token` to keep one across restarts.
- One broker owns a data root: two servers over the same data root (two listeners for one install, say) leave the second in standby, so it fires no T2/T3 write and shows no pending rows. Run one server per data root.
- While an embedder's own decision source cannot be read, the composed decisions panel shows that error and hides the broker's waiting pendings with it. A waiting pending still cannot fire unsigned, and it expires after 15 minutes.
- The broker refuses to start when its data root or a registered store is on a network filesystem (NFS, SMB, FUSE), because its lock may not hold there; the cockpit's writes above C1 are then refused.

## [0.7.2] — 2026-10-10

The cockpit's read side: one manifest, the dashboard drawn from it, and a server's own panels in it.

### Changed

- **The state line now reaches a running session when it changes, and lasts 12 hours.** Claude Code and Codex sessions read it on the next prompt after a change, once per change, not only at session start. It expires after 12 hours (was 8), everywhere it is shown.

### Removed

- **`levain focus` and the focus line are gone.** The state line replaces them: `levain state "..."` sets it, `levain state --clear` removes it. A web or API write of kind `focus` is refused as an unknown kind, the TUI has no `f` key, and the cockpit draws no focus line. A session-start hook from an older release that `levain update` left pending still runs; it shows no focus line.

### Added

- **`levain.cockpit`: one manifest and one panel route, read side only.** `GET /cockpit/manifest.json` lists every panel (kind, priority, status, count, freshness, ETag) and `GET /cockpit/panel/<id>.json` serves one panel's rows or value, in a `full` or `compact` profile, with `?q=` to search row bodies and `?row=<id>` for one row in full. The kernel orders the rows once (a registered ordering with groups, so the Tray's TODAY / OVERDUE / ALSO bands are part of the order), computes status from a typed provider result (`Read`, `Absent` or `Fault`, so only a confirmed read of zero rows says `empty`), and a failed or silent refresher reads as `error`, not as a frozen `ok`. Both routes ride the dashboard's existing read gate; the responses are `Cache-Control: private` with `Vary` on the token header, so a shared cache never serves one caller's manifest to another. The built-in panels cover everything the dashboard shows. Nothing here writes: verbs and tiers are the next slice. `JobStore.list_recent` is new beside it, and `scripts/cockpit_payload_diff.py` diffs a saved or live payload against the design's field map.

- **The web dashboard can draw itself from the cockpit manifest: open it as `/?source=manifest`.** The same components render the kernel's panels, in the kernel's order and bands, with each panel's status and freshness. That view is read-only: edits, and the actions a server registers, stay on the plain URL, which is unchanged and still the default.
- **A server's own panels (`make_server(extra_panels=...)`) appear in the manifest view.** Each becomes a manifest panel in its zone, under the id `ext:<id>`: lines become a list that keeps the server's order and its `meta` text, markdown becomes a prose panel, and a panel's note, empty text and error are shown once. The server's callable runs once per refresh however many panels read it, and a call that does not answer within 8 s shows as not answering instead of holding the page.

## [0.7.1] — 2026-10-10

Closes two of 0.7.0's known open issues (the Linux resolver deputy and concurrent codex installs) and fixes defects in the codex adapter and `ws-adopt`.


### Fixed

- **On Linux, bash with no IP network can no longer have the host's resolver, gnupg's dirmngr, snapd or tailscaled look up or fetch a name for it.** Bash run as your own user (no `levain setup-isolation`) still saw the system D-Bus socket, and through it `systemd-resolved` resolved whatever name bash asked for, on the network; your gnupg `dirmngr` did the same for a key lookup. Wherever bash has no IP network (the default), the system D-Bus, `systemd-resolved`'s varlink, nscd, avahi and tailscaled directories are now hidden and the snapd and `dirmngr` sockets masked, as bash run as the entity's user already had them. Measured 2026-10-10 on an Ubuntu host with a packet capture on port 53: a fresh name asked through `resolvectl`, `varlinkctl`, `busctl` and dirmngr's `WKD_GET` reached the wire before this change and does not with it. (tailscaled answers such a query only where the host made your user its operator; that case is closed by the same hiding, not measured.) Every socket named `S.dirmngr` that the kernel lists as bound in levain's network namespace when bash starts is masked, wherever its `GNUPGHOME` is (also one bash later points gpg at), besides the default gnupg places; if that list (`/proc/net/unix`) cannot be read or parsed, bash without network is refused. These masks are by path, and bash runs as you, so a bash that deliberately renames one of your own directories holding a running dirmngr can reach it from its next session and have it fetch (reproduced); the resolver daemons above live in root's directories and are not affected. That closes before Levain 2.0. Cost, in that bash: no `systemctl` or `busctl` on the system bus, no `resolvectl`, `snap` or `tailscale`, no gpg network commands (gpg signing through gpg-agent still works). A daemon whose directory did not exist when the shell started is not hidden, and neither are the snapd and dirmngr sockets once their daemon recreates them during the session (snapd restarting itself, say). Other daemons that fetch for any local caller over a unix socket (an LDAP-backed sssd or nslcd, a print server, a dirmngr under another socket name or running in another network namespace, such as a container, with its socket on a path bash can see) are not hidden: bash run as the entity's own user (`levain setup-isolation`) is the mode that has none of the host's sockets in view.
- **Two `levain init --adapter codex` (or `levain update`) runs from different installs can no longer mix their writes to codex's shared configuration.** A lock on the codex home (a new file, `<CODEX_HOME>/levain.lock`) is now held across reading and writing both `config.toml` and `hooks.json`. A second `init` is refused before it writes anything and says so; a `levain update` that finds the lock held leaves the adapter files for review (exit 1) and changes nothing of them. Re-run either once the first has finished. Where the codex home cannot be locked (`flock` answers "not supported", as on some SMB or NFS homes, or on Windows), `init` and `update` refuse to write codex's files and say why; set `LEVAIN_CODEX_HOME_UNLOCKED=1` to write them anyway when no other levain install writes that codex home at the same time. Reproduced on 0.7.0 through the CLI on macOS and Ubuntu: 5 of 60 concurrent pairs left `config.toml` naming one install's store and `hooks.json` the other's hooks, both runs exiting 0; none of 40 with this release.
- **If `init` cannot write codex's `hooks.json`, it puts `config.toml` back as it was.** Before, `config.toml` was left registering the new install's memory server with no hooks behind it (reproduced on 0.7.0). The original is kept under a second name before anything is written and renamed back, so the undo needs no free space and keeps its mode (and, where the filesystem has hard links, its owner); a symlinked `config.toml` stays a link. If `config.toml` was edited after levain wrote it, the edit is kept and the original's name is printed. A crash in that moment, codex reading `config.toml` between the two writes, or an edit saved in the instant between levain's check and its put-back, is not covered.
- **`init` no longer crashes when codex's `hooks.json` is a symlink to a file that does not exist.** It raised `FileNotFoundError` while taking the backup, after `config.toml` was already written. The broken link is now kept as the backup and a new `hooks.json` written.
- **A staged copy under `.levain/pending/` is removed once its file is settled.** After you adopted a staged copy (or levain wrote the file), the copy stayed, and adopting it again later replaced the file with an older release's version, which the next `levain update` then left alone. `update` now removes the copy and says so.
- **`ws-adopt` fails closed when git cannot read the source repository's config in full.**

### Known open issues

- **On Linux, bash without network can still reach your own gnupg dirmngr if it deliberately renames one of your directories.** Bash runs as you, so it can rename the directory holding a running dirmngr's socket in one session and reach that socket from its next; dirmngr then fetches for it. The root-owned resolver daemons are not affected. This closes before Levain 2.0.
- **On Linux, a placeholder can outlive a levain that was killed.** Unchanged from 0.7.0.
- **If levain cannot save its record of those placeholders as a session ends, the record can name files already removed.** Unchanged from 0.7.0.

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

- **`levain update` replaces codex's `hooks.json` only when it is exactly what levain last wrote for this install.** A copy that differs from what this version writes and is not what levain last wrote (edited by you, written by another install, or from before levain recorded what it writes) is left untouched, and this release's version is put under `.levain/pending/codex-home/hooks.json` for you to review. An unedited `hooks.json` from an install made by 0.5.0 or later matches levain's record and is refreshed as before. An install made before 0.5.0 has no such record: if its `hooks.json` differs from what this version writes, the first `levain update` after upgrading lists it as pending once and exits 1 (if it is identical, nothing changes). If it is this install's and you never edited it, copy the pending file over it (that points every codex session on the machine at this install); from then on `levain update` refreshes it again.
- **Levain now requires anneal-memory 0.9.41 or later** (`anneal-memory>=0.9.41,<0.10`), the version this release is tested against: since 0.9.39 it no longer brings back a team entry you pruned or deleted on the next `team-import`. No schema or seed change.


### Fixed

- **`levain init` writes files that parse, whatever the install path holds.** The install path, the interpreter path and the `anneal-memory` path were pasted raw into string literals. A path with a `\` or `"` produced a codex `config.toml` that codex could not read (which disables every MCP server it lists, not only levain's), a codex `hooks.json` and a claude-code `.mcp.json` that were invalid JSON, and with a `"` could add keys of its own; `init` still exited 0. The same paste put the `anneal-memory` path into every activation hook's Python source, so a `"` there, or an ordinary Windows path such as `C:\Users\...`, left the hooks unable to compile. Each slot is now escaped for the string it sits in (and, in a hook's command, for the shell's double quotes as well, in a form `levain doctor` and `verify-hooks` read back as the same path); a path that is not valid UTF-8 is refused by name; and `init` parses the whole codex `config.toml` it is about to write and refuses, before changing `config.toml` or `hooks.json`, if its change is what would stop the file parsing. A `config.toml` that does not parse before levain's change and still does not after it is written as before, and levain says so once the write has landed. Reproduced and closed with a real `levain init`: `codex mcp list` reads such a path exactly, and both codex hooks fire from it.
- **A command can no longer forge its own completion or exit status.** The confined shell read its commands from a pipe and learned "done, with status N" from a line bash printed, so a command could read ahead in that pipe and print a status of its choosing (reproduced). Each command is now its own bash, and levain takes completion and the status from the operating system (waitpid), never from output. The old named command pipe, whose path another process of the same user could open, is gone with it, and no state file or profile file is left where another process could rewrite it: on macOS the sandbox profile is passed as text, never read from a file again between commands.
- **levain refuses to start its macOS floor with a profile that would make the kernel build a syscall mask.** A malformed syscall-mask rule in a hand-written Seatbelt profile kernel-panicked a Mac on 2026-10-07. levain never renders such a rule (a test pins it), and its one launch point now also scans the profile it is about to hand to the sandbox and refuses, before anything runs, if it finds one or a form that could compute one. A host-side PATH guard cannot see levain's launches, because levain calls the sandbox by absolute path.
- **The launch banner names every credential store the floor covers.** Its "READABLE" warning named three of the five stores (it left out both git credential files); it now reads the floor's own list.

### Known open issues

- **On Linux, bash under the sandbox can still look names up through the host's DNS resolver.** Bash has no IP network there, but unless it runs as the entity's own user the system D-Bus socket stays reachable, and through it `systemd-resolved` resolves whatever name bash asks for, on the network; the name asked for can carry data out. Measured 2026-10-10 on an Ubuntu host with this release: `resolvectl query` of a fresh name, run in that bash, was answered from the network. Bash run as the entity's user after `levain setup-isolation` gets its own empty `/run`, so these sockets are not in its view; there the resolver's D-Bus, varlink and nscd routes were closed when last measured (2026-10-09, on the build before this release). This closes before Levain 2.0.
- **Two `levain init --adapter codex` (or `levain update`) runs from different codex installs at the same moment can mix their writes to codex's shared configuration.** Codex's `hooks.json` and `config.toml` are machine-global, and the lock is per install, so interleaved runs can leave `hooks.json` pointing at one install and `config.toml` at the other, and an update can replace a `hooks.json` another install's init wrote a moment after update read it (the replaced copy is kept as `hooks.json.bak.*`; traced in the code, not reproduced). Run one codex init or update at a time; `levain doctor`, run from an install, says whether codex's hooks and memory server are wired to it. This closes before Levain 2.0.
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
