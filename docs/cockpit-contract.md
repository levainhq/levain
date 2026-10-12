# The cockpit kind contract (`levain.cockpit/2`)

A provider registers a panel with a `kind` and returns data for it. The kernel validates that data
against the kind's contract and renders it; a shape it does not accept makes that panel `error`
(naming the panel id and the field), never a half-drawn panel. This page is the contract a provider
builds against. The kernel's schema string is `levain.cockpit/2`.

## The eleven kinds

Value kinds return `Read(value=...)`. `list` and `count-queue` return `Read(rows=(RowIn, ...))`.
Every timestamp (`as_of`, `at`) is ISO-8601.

| kind | the provider sends | refused when |
|---|---|---|
| `reading` | `{value, unit, condition, as_of, hi?, lo?, sub?: [{label, value, band?}]}`; `value` is a number or a non-empty string | `value` missing, boolean or null; `unit`/`condition` not strings (empty is fine); `as_of` not ISO; `hi`/`lo` not numbers; a sub-reading without a label |
| `line` | `{lines: [{text, label?, at?}]}` | `lines` not a list of objects; a `text` that is not a string |
| `brief` | `{markdown?, lines?: [{label?, text}], headline?}`; at least one of `markdown` / `lines` | neither present; `markdown` or `headline` not a string; a line without `text` |
| `count-queue` | the `list` contract below; the head's `count` is the queue's big number | as `list` |
| `ambient` | `{events: [{kind, at, repo, agent, outcome}], legend}` | an event missing any of the five keys; `kind` empty; `at` not ISO; `repo`/`agent` neither string nor null; `outcome` not `ok`/`failed`; `legend` missing or blank. Extra keys on an event are accepted and ignored |
| `status-dots` | `{metrics: [{label, status?, value?, unit?, read?}], alerts?: [{message, severity?}]}`; `status` is `ok`/`warn`/`bad`/`unknown` (default `unknown`) | a metric without a label; a `status` outside the four; an alert without `message` |
| `metric-grid` | `{numbers: [{label, value, unit?}]}` | `value` not a number (a numeric string and a boolean are refused) |
| `roster` | `{rows: [{name, memory, cadence, lifecycle, role?, group?, wrap_every?, silence_is_signal?, digest?, last_call?}]}` | `memory` not `stateful`/`stateless`; `cadence` not `resident`/`on_call`; `lifecycle` not `active`/`retired`; a repeated or empty `name`; `digest` neither an object nor null; `last_call` neither null nor `{at, outcome}` |
| `list` | `Read(rows=(RowIn, ...))` with `order` (a registered ordering) and `version_fields` declared on the spec; groups, facets, views and verbs as the row engine defines them | the spec lacks `order` or `version_fields`; a facet the spec did not declare |
| `form` | `{fields: [{name, type, label?, enum?}], verb?}`; `type` is `text`, `recipient` or `date` | no fields; a repeated or empty `name`; an unknown `type`; a `recipient` field whose `enum` is not a non-empty list of non-empty strings; an `enum` on a non-recipient field |
| `log` | `{rows: [{verb, target, actor, at}]}` | any of the four missing or empty; `at` not ISO |

Notes a provider relies on:

- **`roster`**: a `digest` of `null` means "no data" and renders as such; it is never read as zero. A
  row whose `lifecycle` is `retired` is dropped by the kernel (the value carries `retired_hidden: n`).
  `silence_is_signal` defaults to `cadence == "resident"` when omitted.
- **`form`**: the provider supplies a `recipient` field's `enum` from its own registry; the kernel
  checks only that it is a non-empty list of strings.
- **`log`**: the kernel adds a `sentence` to each row, `"<verb label> · <target>"`, from the
  manifest's verb labels (`Snoozed · spore-1238`). A verb with no label renders as its raw name.
- **`ambient`**: the kernel stores and serves the events as sent. The renderer owns the encoding.

## Aliases and `levain.cockpit/1` providers

| declared | becomes |
|---|---|
| `triage-list` | `list` |
| `metric` | `status-dots` |
| `visual` | `ambient` |
| `prose` | `brief` |

A spec declaring an alias is normalised to the new kind when it is built (`ProviderSpec.kind` reads
the new name; `legacy_kind` records the declared one), so the manifest and every panel carry only the
new names. The data contracts of the four are unchanged: a `metric` value is `status-dots`'s, a
`prose` value (`{headline, markdown}`) is a valid `brief`, a `triage-list` is a `list`. A `visual`
keeps its `/1` value (`{visual, text}`, with `visual` one of the known pictures) only while it is
declared as `visual`; a panel declared `ambient` sends events and a legend. A renderer that
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
- **A windowed panel with no hook** returns its window filtered and labelled
  `"searches the newest N only: m of n"`.
- **Bounds.** `q` is plain text of at most 200 characters with no control characters; longer or
  control-bearing input is refused with HTTP 400. `limit` is a positive integer, default 50, capped
  at 200 (a non-integer is a 400). A hook must treat `q` as plain text, never a pattern.
- **Auth and logging.** Search is a read on the panel's own read authentication; it adds no
  credential path. The kernel writes `q` to no journal and no log. It reaches the response validator
  only as a hash.

## Text fallback

`levain.cockpit.text` draws every kind as text lines, for the terminal view and for any renderer
that has no component for a kind: `reading` (value, unit, condition, hi/lo, age, sub-readings),
`line`, `brief`, `ambient` (legend, event count, per-repo counts; a `/1` visual draws its own
text projection), `status-dots`, `metric-grid`, `roster` (grouped, with last call or "no data"),
`form` (fields and recipient choices), `log` (one sentence per row), `list` and `count-queue` (rows).
A search result's `match_label` or `search_error` is drawn under the panel.
