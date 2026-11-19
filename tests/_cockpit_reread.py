"""The one way a browser test makes the cockpit read its board again (levain#159).

The tests used to click the masthead Sync button (`#refresh`). That click ran `load({ passive: true })` in dashboard_boot.js, which is the
same call as `window.LevainCockpitTransport.reload()`, so these snippets call that and need no button on the page. Like the click, a call
made while a read is already in flight is dropped (`load` returns false at once), and a passive read is refused while an edit is open.

Use REREAD where the click was `...click(); 1` (the test then waits on its own signal: a render counter, a replaced node, `until`), and
REREAD_SETTLED where the click was followed by a fixed wait for the draw. Both are JS expressions for the page driver's `eval` / `do` steps
(the driver awaits a returned promise); REREAD_CALL is the bare statement for building a longer expression.
"""
from __future__ import annotations

REREAD_CALL = "window.LevainCockpitTransport.reload();"

# fire the read and return at once (the old `document.getElementById('refresh').click(); 1`)
REREAD = REREAD_CALL + " 1"

# fire the read, wait for its promise (the whole draw, or the drop of a read already in flight), then give the redraw 800 ms to settle
# (the old `click(); new Promise(r => setTimeout(r, 800)).then(() => 1)`)
REREAD_SETTLED = "window.LevainCockpitTransport.reload().then(() => new Promise(r => setTimeout(r, 800))).then(() => 1)"
