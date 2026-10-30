# The cockpit kind contract (`levain.cockpit/2`)

A provider registers a panel with a `kind` and returns data for it. The kernel validates that data
against the kind's contract and renders it; a shape it does not accept makes that panel `error`
(naming the panel id and the field), never a half-drawn panel. This page is the contract a provider
builds against. The kernel's schema string is `levain.cockpit/2`.

## The kinds

The list is the `KINDS` tuple in `levain/cockpit/engine.py`; this table has one row per kind (a test fails when a kind has no row).

Value kinds return `Read(value=...)`. `list` and `count-queue` return `Read(rows=(RowIn, ...))`.
Every timestamp (`as_of`, `at`) is ISO-8601.

| kind | the provider sends | refused when |
|---|---|---|
| `reading` | `{value, unit, condition, as_of, hi?, lo?, symbol?, sub?: [{label, value, band?, detail?}]}`; `value` is a number or a non-empty string | `value` missing, boolean or null; `unit`/`condition` not strings (empty is fine); `as_of` not ISO; `hi`/`lo` not numbers; a `symbol` outside the token set below; a sub-reading without a label, or a `detail` that is not a string |
| `line` | `{lines: [{text, label?, at?}]}` | `lines` not a list of objects; a `text` that is not a string; an `at` that is not ISO |
| `brief` | `{markdown?, lines?: [{label?, text}], headline?, title?, source?, threads?: [{text, domain?, tier?}], overnight?: {episodes, since}}`; at least one of `markdown` / `lines`. `title`/`source` head a brief-region card (its name and what it reads from); `threads` are open loops the page groups by repo; `overnight` is episodes since the last wrap (`since` = that wrap's ISO time), present only when both are known | neither present; `markdown`, `headline`, `title` or `source` not a string; a line or thread without `text`; an `overnight` that is not `{episodes: int, since: ISO}` |
| `count-queue` | the `list` contract below; the head's `count` is the queue's big number | as `list` |
| `ambient` | `{events: [{kind, at, repo, agent, outcome}], legend}` | an event missing any of the five keys; `kind` empty; `at` not ISO; `repo`/`agent` neither string nor null; `outcome` not `ok`/`failed`; `legend` missing or blank. Extra keys on an event are accepted and ignored |
| `status-dots` | `{metrics: [{label, status?, value?, unit?, read?}], alerts?: [{message, severity?}]}`; `status` is `ok`/`warn`/`bad`/`unknown` (default `unknown`) | a metric without a label; a `status` outside the four; a `value` that is not a finite number or a string; a `unit` or `read` that is not a string; an alert without `message` |
| `metric-grid` | `{numbers: [{label, value, unit?}]}` | `value` not a finite number (a numeric string, a boolean, NaN and the infinities are refused) |
| `roster` | `{rows: [{name, memory, cadence, lifecycle, role?, group?, wrap_every?, silence_is_signal?, digest?, last_call?}]}` | `memory` not `stateful`/`stateless`; `cadence` not `resident`/`on_call`; `lifecycle` not `active`/`retired`; a repeated or empty `name`; `digest` neither an object nor null; `last_call` neither null nor `{at (ISO-8601), outcome (ok or failed)}` |
| `list` | `Read(rows=(RowIn, ...))` with `order` (a registered ordering) and `version_fields` declared on the spec; groups, facets, views and verbs as the row engine defines them | the spec lacks `order` or `version_fields`; a facet the spec did not declare |
| `form` | `{fields: [{name, type, label?, enum?}], verb?}`; `type` is `text`, `recipient` or `date` | no fields; a repeated or empty `name`; an unknown `type`; a `recipient` field whose `enum` is not a non-empty list of non-empty strings; an `enum` on a non-recipient field |
| `log` | `{rows: [{verb, target, actor, at}]}` | any of the four missing or empty; `at` not ISO |
| `board` | `{queue, columns: [{category, count?, cards: [{id, title, repo?, tier?, done_line?, depends_on?, status?, status_category?, held_reason?, well_formed?, rank?, unknown_category?, journal?: {iso, who, why, source, from_category, to_category, applied_by}\|null, band?, number?, source?, blocks?}]}], unreadable?: [string], note?: string\|null}` (see the note below) | `queue` not `ok`/`none`/`unreadable`; `columns` missing or not a list of objects; a `category` outside the six or repeated; `cards` not a list of objects; a card whose `id` is not a non-empty string or whose `title` is not a string; a card `id`, `title`, `repo` or `done_line` that is not a string (`repo` and `done_line` may be null), carries an invisible or format character, or is longer than `BOARD_TEXT_MAX` (1000) characters; a `depends_on` that is neither null nor a list of at most `BOARD_DEPENDS_MAX` (50) display-safe strings of at most `BOARD_TEXT_MAX` characters; a `journal` that is neither an object nor null; `unreadable` not a list of strings; `note` neither a string nor null; more than `BOARD_CARDS_MAX` cards |
| `topology` | `{hub: {id, label}, groups: [{id, label, repo}], nodes: [{id, group, parent, label, agent, model, state, tool, tokens, started, last_event, rate, card}], conflicts: [{path, nodes}]|null, conflicts_total, counts: {tokens_today}, as_of, tasks: [{id, kind, label, state, waits_on, card, outcomes: [{label, at, ok}], cost: {usd, tokens, source}, workers, opened?, age_days?, heat?}], lost_if_closed: [{node, dirty, dirty_names, unpushed, branches, uncaptured_episodes, running_children}], errors?, unreadable?}` (F12_SPEC); node `state` is `waiting`/`errored`/`stalled`/`running`/`idle`, task `kind` `dispatch`/`spore`/`consent`/`question`/`unattached`, task `state` `waiting`/`stuck`/`running`/`open`/`done`/`failed` (`open` = a spore or loop with no worker; tasks may carry `opened` (iso), `age_days` (integer, the calendar days open in the provider's own day: the number the page draws, never recomputed from `opened`) and `heat` (`hot`/`warm`/`cold`/`parked`)); `conflicts` is one record per file path naming EVERY node on it (`nodes`: two or more distinct node ids) and `conflicts_total` is the number of conflicting paths before the list's cap (never below the list; the page's "N file conflicts" reads it); an `apply_patch` logs at most 32 of its paths (the rest are counted as "(+N more)"), so a conflict on a path past the 32nd is not seen (a known edge); a panel read also carries `today` (the server's calendar day, `YYYY-MM-DD`), against which the page labels TODAY / OVERDUE and counts "overdue N d"; the browser's own clock and zone decide none of it; a `parent` chain may not cycle. REPLAY (F12, optional): a provider may offer a past read, `read_at(ctx, iso)` (on its `ProviderSpec.read_at`, or as `read.read_at` on its read function), returning the same value for a past instant; the panel head then carries `replay: true` (no flag, no replay control), and `GET /cockpit/panel/<id>.json?at=<iso>` (the same route, so the same read gate and token as the live read) returns the panel at T with `at` (T as checked) and `replay: true`, the value's `as_of` = T and `replay: true` in the value. T is an ISO instant with its zone, on a whole minute, inside `[now - 14 h, now]` (`check_replay_at`); anything else, `q`/`row`/`limit` beside it, or a panel with no past read is a 400, never clamped. A past read is bounded to what was known at T (events, filings, decisions, answers, transitions stamped at or before T); a source it cannot rebuild for T is named in `unreadable` with the reason `not kept for the past` in `errors`, its fields null, never drawn as empty (Levain's: `spores`, `lost_if_closed`, `context` (node model/tokens), and, when they apply, `session_logs` (a log rotated past T), `session_records` (more records than the scan or the 50-record cap reads, so a session live at T may be missing), `conflicts`, `decisions`/`questions` (a listing that does not reach T, or the consent listing overflowed or skipped rows)); a past read is served from a short per-(panel, minute) cache and its own bounded pool (two at once, apart from the write pool; over it the panel answers an error status naming the refusal), and its `today` is T's local calendar day. Known edges of the topology past read: sessions are grouped by the repository their directory is in NOW (a directory made a repository after T groups by today's repository); a session log rotating between the reachability check and the conflict read can drop that window's edits (a microsecond race); `at`'s offset must be percent-encoded (`+` reads as a space; `Z` needs none), and a blank, repeated or `q`/`row`/`limit`-accompanied `at` is a 400 | a string carrying an invisible or format character; a label over 48 characters; a repeated id or a reference (`group`, `parent`, `workers`, `node`, a conflict's nodes) naming none; an enum outside its set; a count that is negative or not an integer; a cost with numbers but no `source`; a `path` or file name that is not a basename. `null` is "unknown" wherever allowed (`model`, `tokens`, `conflicts`, the cost fields, every count in `lost_if_closed`) and is never read as zero |

Notes a provider relies on:

- **`roster`**: a `digest` of `null` means "no data" and renders as such; it is never read as zero. A
  row whose `lifecycle` is `retired` is dropped by the kernel (the value carries `retired_hidden: n`).
  `silence_is_signal` defaults to `cadence == "resident"` when omitted or null.
- **`form`**: the provider supplies a `recipient` field's `enum` from its own registry; the kernel
  checks only that it is a non-empty list of strings.
- **`log`**: the kernel adds a `sentence` to each row, `"<verb label> · <target>"`, from the
  manifest's verb labels (`Snoozed · spore-1238`). A verb with no label renders as its raw name.
- **`ambient`**: the kernel stores and serves the events as sent. The renderer owns the encoding.
- **`board`**: the ready queue as derived columns, the value levain's own `GET /cockpit/board.json` serves
  (`levain.cockpit.board.build`); a downstream's panel declares `kind="board"` and sends that value. It is a SHAPE check:
  the kernel parses and executes nothing, and any key it does not name (`band_hint`, `tell`, `journal_as_of_read`, a
  card's `held_reason`, `well_formed`, `rank`, `band`, `number`, `source`, `blocks`) is accepted and left alone.
  `queue` is `ok` (read whole), `none` (no queue made yet) or `unreadable`; `unreadable` lists the sources it could not read
  and `note` says why, so a board that could not be read is never drawn as an empty one. Each column's `category` is one
  of `backlog`, `ready`, `running`, `waiting`, `needs-you`, `done` (the coordinator's six, in column order; a column may be
  left out, never repeated), its `cards` a list of objects with a non-empty string `id` and a string `title`; a card's
  `journal` (who moved it, when, why: `{iso, who, why, source, from_category, to_category, applied_by}`) is an object or
  null. The whole board holds at most `BOARD_CARDS_MAX` cards. It is `empty` only when the queue is `ok`, nothing is
  `unreadable` and no column holds a card. **Two draws, one value:** the page draws the panel GROUPED, one heading per
  column in the coordinator's order with its count and the cards as rows beneath it, and the panel's ⤢ focus modal draws the
  six columns side by side (the renderers of `board_view.js`; with that script absent the panel says so in one line).
  The `compact` profile (the phone's) reduces a board as it reduces a topology: `{queue, note, unreadable, columns: [{category,
  count}]}` and a `next` link to the full read; no card goes out in it.
  A board carries no write verb in this slice: it offers no box and posts nothing.

## Regions and zone layout

`ProviderSpec.region` places a panel. A renderer draws, top to bottom: the **hero**, the **brief**, the
**widgets**, then the three zones (`operate`, `mind`, `identity`) on a three-column grid.

| region | holds | refused |
|---|---|---|
| `hero` | a `reading` (at most one) and an `ambient` (at most one); the renderer draws its own clock first, the reading under it, the ambient beside it | a second reading or ambient; any other kind |
| `brief` | `line` and `brief`, in rank order | any other kind |
| `widgets` | one answer each: `reading`, `line`, `status-dots`, `metric-grid`, and a `list` or `count-queue` that declares `face="count"` | `brief`, `ambient`, `roster`, `log`, `form`, `board`, and a row-engine kind without `face="count"`. A widget is one answer, never a list |
| `operate` / `mind` / `identity` | any kind | |

- **`face`** is `""` (the kind's own face) or `"count"`. `"count"` is valid only on a `list` or `count-queue`, in any
  region; in a widget it draws the row count and the panel's note, and the rows surface through `now` and the
  panel's own view. A panel head carries `face` only when it is non-empty.
- **`size`** (`tall`, `medium`, `short`; default `medium`), **`span`** (`1`, `2`, `3`; default `1`) and **`tabs`** (a card
  title) lay out a zone panel; a non-default on a hero, brief or widgets panel is refused. Panels of one zone that
  share a non-empty `tabs` title are the tabs of ONE card, in rank order. The card takes the first panel's size and
  span, and a later registration in the same zone and `tabs` with a different size or span is refused, naming both panels.
- **The legacy region `header`** is still accepted and is normalised when the spec is built, like a kind alias
  (`legacy_region` records the declared one): a `reading` or `ambient` becomes `hero`, a `line` or `brief` becomes
  `brief`, any other kind becomes `operate`. If the hero already holds a panel of that kind, the later legacy panel
  goes to `operate` rather than being refused; a declared `hero` is refused.

The manifest's `regions`:

```
"regions": {
  "hero": [ids], "brief": [ids], "widgets": [ids],
  "header": [hero ids..., brief ids..., widget ids...],
  "zones": [{"id", "title", "grid": 3, "panels": [ids],
             "cards": [{"panels": [ids], "size", "span", "tabs": title|null}]}]
}
```

`header` is **compat**: what a renderer that predates the hero reads (hero, brief, then widgets, so a widget is drawn
by the old page's header-extra path rather than lost), derived and never declared. A zone's `panels` is the flat list
as before, in rank order; `cards` is the layout over the same ids and is authoritative (the two differ when tab members
are not rank-contiguous). `now` leads `operate` as a card
of its own (size `medium`, span 1, no tabs). Regions are content: they are hashed into the manifest etag.

### Kernel default layout

`build_default_cockpit` places: `brief` the state line; `widgets` Health; `operate` (rank order) Now (engine-fixed),
Tray 10 and Open loops 15 (both `tall`), Recent episodes 40, Keep 50, Recent edits 60, Recent jobs 70 (`medium`);
`mind` the Neocortex tabs card (one tab per continuity heading, ranked in heading order from 0, span 2, `tall`), Crystallized patterns 100
(`tall`) and the Cognition card (graph 200 and projection history 210, `tabs="Cognition"`, `short`). Ranks 16-39 of
`operate` are left for an embedder's Inbox, Fleet and Stream; a rank outside the kernel's bands is the embedder's.
A zone grid row's height is its tallest card's, and the cards in a row stretch to it.
The `Neocortex` and `Cognition` tab titles are the kernel's: a panel that joins one with a different `size` or `span` than the card's first member is refused, so an embedder panel registered before the sections refuses every section, and one registered after them is refused itself. A discovered panel's rank and verbs are re-read at every manifest, so a heading moved while the server runs moves its tab.

**Reading tokens.** `value.symbol` is optional and one of `clear`, `clear-night`, `partly-cloudy`,
`partly-cloudy-night`, `cloudy`, `fog`, `drizzle`, `rain`, `heavy-rain`, `thunderstorm`, `snow`, `sleet`, `wind`,
`haze` (`READING_SYMBOLS`); a provider maps its own source vocabulary onto the set, and an unknown token is refused.
A sub-reading's `band` stays any string, and the contract names the lowercase tokens `good`, `moderate`,
`unhealthy-sensitive`, `unhealthy`, `very-unhealthy`, `hazardous` (`READING_BANDS`): a KE1 renderer
lowercases the token before comparing, `good` is calm, the rest are hot, and any other token renders neutral. A sub-reading's `detail`
is a free string (for instance the measuring source).

## Aliases and `levain.cockpit/1` providers

| declared | becomes |
|---|---|
| `triage-list` | `list` |
| `metric` | `status-dots` |
| `visual` | `ambient` |
| `prose` | `brief` |

A spec declaring an alias is normalised to the new kind when it is built (`ProviderSpec.kind` reads
the new name; `legacy_kind` records the declared one), so the manifest and every panel carry only the
new names. The four keep their field names, and v2 validates them more strictly than `/1` did. A
`metric` value is read as `status-dots`'s: it needs a `metrics` list, each metric a non-empty string
`label`, and, when it sends a `value`, a finite number or a string (`null` or absent passes). A `prose` value
(`{headline, markdown}`) is read as a `brief`: it needs `markdown` (a string) or `lines`, a `headline` that,
when sent, is a string, and lines that, when sent, are each an object with a string `text`. A `triage-list` is
a `list`. A `visual` keeps its `/1` value (`{visual, text}`) only while it is declared as `visual`: `visual`
must be one of the known pictures and `text` must be a list. A panel declared `ambient` sends events and a
legend. A renderer that
reads `head.kind` sees the new names whatever the provider declared.

## Schema and revalidation

The manifest carries `"schema": "levain.cockpit/2"`. The schema string is part of the manifest etag
and of every panel etag, and the HTTP validator carries a representation number, so a client holding
a body from an older kernel revalidates to a 200 and never a 304.

## Searchable

A property of every row-shaped kind (`list`, `count-queue`, `roster`, `log`), owned by the kernel.

- **The box is automatic.** The panel head carries `searchable: true` when the panel holds more than
  `SEARCH_THRESHOLD` (8) rows or declares a `window`. A provider cannot turn it off.
- **A provider declares what is searched, not whether.** Spec fields: `search_fields` (default
  `title`, `body`, `id` for `list` / `count-queue`; `name`, `role`, `group` for `roster`; `verb`,
  `target`, `actor` for `log`), `search_scope` (`rows` or `store`), `window` (the newest-N a windowed
  panel delivers) and `search` (the hook). Registration refuses a field the kind's row does not carry
  (`list`/`count-queue`: `title`, `body`, `id` and the spec's declared facets; `roster`: `name`,
  `role`, `group`, `memory`, `cadence`, `lifecycle`, `wrap_every`; `log`: `verb`, `target`, `actor`,
  `at`), a scope other than the two, a `store` scope without a `window`, and a hook on a `rows`-scope
  panel. The head carries `search: {fields, scope, default_visible, hook}` and `window`.
- **`rows` scope**: the provider delivered every row. A query is case- and accent-folded
  space-separated terms, ANDed, each a substring of some declared field. Plain text only: `.*` is
  three literal characters.
- **`store` scope**: the panel is a window. `GET /cockpit/panel/<id>.json?q=<text>[&limit=<n>]`
  calls the provider's `search(q, limit)` and returns its rows in place of the window's. The hook
  returns a sequence of rows (`RowIn` for `list`/`count-queue`, the kind's row objects for `roster`/`log`),
  or `StoreMatches(rows, total)` when the store matched more than the limit. Rows are validated by
  the kind's own contract. The panel then carries `matched` and `match_label`
  (`"41 matches in the store"`). A hook that raises, overruns the panel's `timeout_s` or returns
  unusable rows yields `search_error` and no rows: a failed search never reads as "no matches".
- **A search is its own answer.** The hook's rows are drained inside the panel's `timeout_s`, at most
  `limit` of them, so a lazy sequence cannot outrun the deadline. One search per panel runs at a time:
  while a timed-out hook is still alive the panel answers `search_error: "search busy"`. A refused
  row, or a `total` that is not an integer at least the number of rows returned, is a `search_error`.
  A response that searched has `status: ok` when it returned rows and `empty` when it did not, and its
  validator covers the returned rows, `matched`, `match_label` and `search_error`. A query with no
  searchable term (blank, or only combining marks) is no query.
- **The `now` view** declares no search (`searchable: false`, `search: null`).
- **A windowed panel with no hook** returns its window filtered and labelled
  `"searches the newest N only: m of n"`.
- **Bounds.** `q` is plain text of at most 200 characters with no control characters, checked as sent
  (nothing is trimmed first); longer or control-bearing input is refused with HTTP 400. `limit` is a positive integer, default 50, capped
  at 200 (a non-integer is a 400). A hook must treat `q` as plain text, never a pattern.
- **Auth and logging.** Search is a read on the panel's own read authentication; it adds no
  credential path. The kernel writes `q` to no journal and no log. It reaches the response validator
  only as a hash.

## Text fallback

`levain.cockpit.text` draws every kind as text lines, for the terminal view and for any renderer
that has no component for a kind: `reading` (value, unit, condition, hi/lo, age, sub-readings); it draws the hero, brief and widgets, then each zone,
`line`, `brief`, `ambient` (legend, event count, per-repo counts; a `/1` visual draws its own
text projection), `status-dots`, `metric-grid`, `roster` (grouped, with last call or "no data"),
`form` (fields and recipient choices), `log` (one sentence per row), `list` and `count-queue` (rows).
`topology` and `board` have no text form in this view: it says so in one line, never a blank panel.
A search result's `match_label` or `search_error` is drawn under the panel, an empty one included.

## The change stream

`GET /cockpit/events` is a server-sent-events stream that tells a page which panels moved. It carries
no panel data: the page reads a moved panel through the panel route, under its own credential. The
route takes the same Host allowlist, cross-site refusal and off-box token as every other cockpit read,
and holds its own cap of 8 open streams (a ninth gets `503 busy`).

| frame | data | meaning |
|---|---|---|
| `hello` | `{epoch, resync}` | the first frame of every stream. `resync: true` means the server cannot replay what the client missed (a new server run, a position it no longer holds, an unreadable `Last-Event-ID`): the client reconciles against the manifest |
| `panel-changed` | `{id, etag}` | that panel's content ETag moved |
| `manifest-changed` | `{}` | the set of panel ids changed, or the watcher's first good reading after its starting read failed: reload the board |
| `: hb` | (a comment line) | a heartbeat every 15 s on a quiet stream |

Events carry the id `<epoch>:<seq>`. Within one epoch a reconnecting client that names its last id in
`Last-Event-ID` is caught up from a ring of the last 256 events; from another epoch it is told to
resync. The server's watcher builds the manifest every 5 s while at least one stream is open, diffs
the panel ETags and the id set, and broadcasts; it stops with the last stream, and the next one is a
new epoch. Change detection rests on the content ETag only: `as_of` is outside it, so a read that
merely refreshes a panel does not announce itself.
