# Envelope `launches` grant: exact shape (design, np-3e152c37)

Read against origin/main: levain/coordinator/envelope.py (BODY_KEYS l.51, EFFECT_LISTS l.56, parse_body l.208, widens, effect_key),
envelopes.py (NARROW_KEYS, narrow_scope, _effective, accept_envelope), coordinator/cli.py (_effects card text, _SPEC_KEYS, spec builder),
cockpit/threads.py (_effects_words), templates/web/envelope_form.js (composer), envelope_draft.py / envelope_drafts.py (EDITABLE, _EDIT_SPEC).

## 1. Field shape: a LIST of exact-key objects, one per repo, like lands/pushes/publishes
`"launches": [{"repo": "<a where repo>", "count": 2}]`  (key sits beside lands/pushes/publishes; `[]` = no grant)
Signed body example (canonical JSON, keys sorted; abridged to the relevant keys):
```
{"binds":[],"budget":{"dollars_cents":0,"hours":4,"tokens":2000000,"tool_calls":2000},"draft":null,
 "drop_id":"dr-<32hex>","envelope_id":"ev-<32hex>","format":"levain-thread-envelope/2","issued_at":"2026-11-07T18:00:00Z",
 "lands":[],"launches":[{"count":2,"repo":"/Users/phillipclapham/Briefcase/flow"}],"origin":"structured",
 "pushes":[],"publishes":[],"stop_at":"2026-11-07T22:00:00Z","store_id":"<32hex>","supersedes":null,"words_sha256":"sha256:<64hex>",
 "where":[{"paths":["."],"repo":"/Users/phillipclapham/Briefcase/flow"}]}
```
Why a per-repo list, not one scalar: `where` scoping, duplicate refusal, widens(), narrowing by identity, supersede diff marks (+ - = ~)
and the card line all come for free from the effect-list machinery; a scalar would need parallel code for each. The composer writes one
entry per where repo it grants (toggle on = every selected repo, same count; an operator may untick a repo).

## 2. Constants (envelope.py, exported in __all__)
`LAUNCH_DEFAULT = 2`, `LAUNCH_MAX = 4`. Reason for 4: the desk fan-in runs 4 lanes per round at most (pace ceiling 8 total across all work);
a signed no-further-auth grant is the widest standing authority in the envelope, so the ceiling is the smallest number that still
covers a real fan-in round; raising it is a code change, never a typo. Also `_LAUNCH_LIST_MAX = len(where)` (one entry per repo).

## 3. Files and functions that change
- envelope.py: add `LAUNCH_LIST = "launches"`; `OPTIONAL_KEYS = frozenset({"launches"})`; parse_body: accept `set(body) in (BODY_KEYS, BODY_KEYS|OPTIONAL)`
  (replace `set(body) != BODY_KEYS`); validate `_exact(entry, {"repo","count"})`, repo via `_repo` and in `scope`, `type(count) is int` and
  `1 <= count <= LAUNCH_MAX` (bool refused by the `type is int` test), repo named twice refused. Add `"launches"` to `__all__`.
  New `GRANT_LISTS = EFFECT_LISTS + ("launches",)`; `widens()` loops GRANT_LISTS with `base.get(kind, [])`/`new.get(kind, [])` (a bigger count or new repo
  widens); `effect_key("launches", e) = ("launches", e["repo"])` (so a changed count is "same target, other value", compared by the widens equality: larger count = widens;
  add `count <=` special case: new count > before count -> True, lower is a narrowing); `effect_identity` unchanged.
  EFFECT_LISTS itself stays three: lands/pushes/publishes are irreversible outward effects and agents/refuse.py, cockpit/authority.py, role CEILING_LISTS
  key off it; launching a seat is not one of them and must not become a role-ceiling key by accident.
- envelopes.py: `NARROW_KEYS` unchanged (a role ceiling never grants launches; `narrow_scope` copies other keys through, so a narrowed scope would CARRY launches:
  add `out["launches"] = []` there, so a role/agent scope never inherits the operator's launch grant); `_effective` (l.652): iterate GRANT_LISTS with `.get(k, [])`,
  and the empty effective body (l.661) gets `"launches": []`; drop_effect attenuation accepts a launches identity (identities set over GRANT_LISTS).
  `accept_envelope`: nothing new beyond parse_body (stop_at > now and <= MAX_LIFETIME already bound it); also refuse a launches repo outside `where` (parse_body does).
- coordinator/cli.py: `_EFFECT_HEAD`/`_effect_text`/`_effects` card (l.398-425) add "launches": line `<repo>  may launch up to N seats`; `_SPEC_KEYS` (l.845) and
  the spec builder (l.856-898) accept `launches` (list of `{repo,count}`) and default `[]`; the two body builders (l.675, l.727) emit the key ONLY when non-empty
  (absent otherwise, so a no-grant envelope's bytes and digest are what they were); l.993/l.412 loops over GRANT_LISTS.
- cockpit/threads.py `_effects_words` l.119: append `, N launch` only when the key is present (plain words, below).
- envelope_draft.py (EDITABLE l.34, l.122, l.245, l.272) and envelope_drafts.py (_EDIT_SPEC l.43, l.1013, l.1042): add "launches" to the editable set and to GRANT_LISTS loops.
- templates/web/envelope_form.js (spec at l.409): toggle "Launch seats without asking again", OFF by default; on shows a count field defaulting to 2, clamped to 1..4 in the UI
  and again server-side; emits `spec.launches = [{repo: r, count: n}]` only when on. phone.py (l.117) sheet: NOT settable from the phone (like pushes/publishes), laptop signs it.
- launch.py l.571 and lease.py `_SCOPE_KEYS` (l.456): leave alone (a seat lease scope never carries launches); add a test that it does not.

## 4. Compat: absent = no grant; old signed envelopes verify
The signature covers canonical bytes of the body; adding a required key would break every stored body and the `format/2` check. So the key is OPTIONAL in the
body: an old body (no `launches`) still passes parse_body unchanged, its digest and signature are untouched, and every reader uses `body.get("launches", [])`.
Never rewrite or inject the key into a stored body. `format` stays `levain-thread-envelope/2`. An OLD levain (pre-change) refuses a body that has the key (exact key-set check): fail
closed, acceptable and desired. Old rows in the store need no migration.

## 5. Refusal words (EnvelopeError, verbatim)
- `launches[i].repo is not one of the envelope's where repos`
- `launches[i].count is an integer from 1 to 4`   (zero, negative, 5, 2.0, "2", true all land here)
- `launches names one repo twice`
- `launches is a list of {repo, count}`   (not a list / extra keys)
Compose-time (cli/draft/composer) calls the same parse_body, so compose and verify refuse in the same words.

## 6. Plain words on the signed card (and the sheet text that is signed)
Card line (cli `_effects`): `launches (seats) it may do:` then `<repo>  may launch up to 2 seats in <repo> until 2026-11-07 22:00 ET` — generated from the
signed body (count, repo, effective `stop_at`), never from the draft. Summary (`_effects_words`): `effects named: 0 land, 0 push, 0 publish, launch up to 2 seats`.
Supersede diff marks it like the others (+ added / ~ changed / - removed).

## 7. Consumer (flow desk) must check at EACH launch, re-resolved, never from the relay payload
1. envelope id exists in the store, its signature and digest verify (`accept_envelope` already ran; re-read via store `_envelope` + digest recompute);
2. effective (not signed) stop_at > now: `_effective` honours `stop_earlier` attenuations and drop_effect removals;
3. an entry with `repo == target repo` exists in the EFFECTIVE launches (so repo is also in effective `where`);
4. `used(envelope_id, repo) < count`. Used count lives in FLOW's own launch log (one line per launch citing envelope id, repo, seat), because the launch happens in flow;
   levain must not keep a mutable counter (it would be a second writer of a signed grant's state).
Levain offers the pure helper, no I/O beyond the store read:
`def launch_allowance(envelope: Mapping[str, Any], where_repo: str, now: datetime, used: int) -> int`
  in envelope.py; `envelope` is the EFFECTIVE body (what `_effective` returns); returns the remaining seats (0 = none: grant absent, repo not covered, `now >= stop_at`,
  or used >= count; never negative; `used` negative/non-int raises EnvelopeError). Desk launches iff it returns > 0, then logs used+1.
Store-side wrapper: `EnvelopeStore.launch_allowance(envelope_id, repo, used)` that resolves the effective body and the store clock.

## 8. Tests (each from a failing scenario)
1. grant absent (old body) -> parse_body ok, digest unchanged vs a fixture, launch_allowance == 0.
2. count 0, -1, 5, 2.0, "2", True -> refused in parse_body AND at compose (cli spec, draft edit, composer clamp).
3. launches repo outside where -> refused; repo duplicated -> refused; extra key -> refused.
4. launches key present but body not canonical / missing a sibling key -> still refused (key-set test: exactly BODY_KEYS or BODY_KEYS+launches, nothing else).
5. launch_allowance: used == count -> 0; other repo -> 0; now >= stop_at -> 0; effective stop earlier via stop_earlier -> 0; count 2 used 1 -> 1.
6. widens(): larger count / new repo widens; same or lower does not; supersede that adds a grant needs the signer's fresh signature.
7. narrow_scope: a role scope from an envelope with launches carries `launches == []`.
8. card text: signed card contains "may launch up to 2 seats in <repo> until <time>"; toggle off -> no launches line and body has no key.
9. composer: toggle off by default (no key in spec), on -> default count 2, clamp 1..4; phone sheet cannot set it.
10. Signed round trip: sign a body with launches, accept, tamper count 2->4 in stored body -> digest/signature verify fails.
