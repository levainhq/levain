# RETURN: P9b (agents panel page half), seat/1107-26-p9b

DONE
- Production wiring: `agents_panel.wire_agents(ck, source, dispatch_dir=...)` called at the end of `build_default_cockpit` (providers.py). Before: nothing set `cockpit.agents_wiring` outside tests, panel absent from the page (reproduced: browser test red, panel never appears).
- `cockpit_view.js` TABS_OF_ID `agents: "build"`; new `agents_view.js` (row open gesture, detail modal via LevainDashboard.openCustom, verbs from the row's own offered actions, second tap for renew/retire); script tag in dashboard.html; route in web_server.py; `LevainCockpitTransport.rowActions/rowVerb` in dashboard_boot.js (not in the brief's owned list; needed for confirm).
- "cleared or not": `agents_panel._card_marks/_cards_of`, row body says "cards: N open, M cleared" (agent feed items matched on id/name as a colon part of dedupe_key); unreadable queue is said in the panel note. No new facets (registry.py untouched; counts are in `stored` and body only).
- dashboard_core.js NOT touched: the generic list render in cockpit_view.js draws the groups, so no CK_PIN_BUILD hunk was needed.
- tests/test_cockpit_agents_panel_browser.py: 4 tests x (390, 1280) = 8, pass. Mutation checks (revert -> red -> restore): no wiring (4 red), tab mapping (2 red), script tag (3 red), transport (2 red), cards (1 red), route (3 red).
- tests/test_coordinator_two_rebuilds.py: store old in BOTH replies (C11 S4) and review_items (P8a) opens once, all rows/events/gseq/triggers kept, second open a no-op. Mutation: either rebuild off -> red.
- Passing: browser(8) + test_cockpit_agents_panel(27) + two_rebuilds(1) + test_one_board_browser + test_cockpit_narrow_layout = 51 passed.

NOT DONE / OPEN
- The wide run (tests/test_cockpit_*.py, web_server, dashboard, 159 files) was still running at wrap; result unknown. Next session: run it, since build_default_cockpit now wires every cockpit (a populated LEVAIN_HOME could add the panel to manifest-count tests).
- "run now" has no owner (AGENT_VERBS has four); not drawn. Renew is offered on rows with a link but this cockpit carries no signing key, so it is refused with the 501 words until a key is wired.
- Collision: open seats seat/1107-20-v17, -micS3, -micS4 touch dashboard.html/web_server.py (script lines ~113 / _ASSETS ~1268); mine are at the topology_view lines (distinct hunks). seat/1107-26-consentcopy touches dashboard_core.js ~4380 only. No race on files edited here.
- Only a person: his one whole-page look at a RUNNING preview: is "Operator agent"/group "Agent" the right word, is a name shown, does the uncounted pinned group read as header or clutter, legibility on 15 Pro Max glass.
- CI and push: not pushed.
