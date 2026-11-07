"""What ``install_helper undo`` leaves on the host (row 121, Linux VM L2 round): the store (``/var/levain``,
with its ``reserve`` file), the root import tree and the helper entries under HELPER_DIR. The kept set is the
documented contract (docs/slot-undo.md); a plan that removes any of them fails here."""
from __future__ import annotations

import pytest

from .test_slot_confinement_plan import HOSTS, _plan, _tools_resolve, _undo  # noqa: F401  (the autouse fixture rides along)
from levain.slot.layout import HELPER_DIR, IMPORT_TREE, STORE, reserve_row

#: The only paths under the store that undo removes: the pool config and the two install records.
REMOVED_IN_STORE = {"policy/pool", "policy/install-shape", "policy/operator"}


def _removed(plan):
    """Every path an rm/rmdir step of the plan names (chown and chmod of a kept directory are not removals)."""
    out = []
    for s in plan.steps:
        argv = [str(a) for a in (s.argv or ())]
        if argv and argv[0].rsplit("/", 1)[-1] in ("rm", "rmdir"):
            out += [a for a in argv[1:] if a.startswith("/")]
    return out


@pytest.mark.parametrize("host", HOSTS)
def test_undo_leaves_store_root_tree_and_helpers(host):
    ids = dict(_plan(host).allocation.ids)
    removed = _removed(_undo(host, ids))
    store, tree, helpers = str(STORE[host]), str(IMPORT_TREE[host]), str(HELPER_DIR[host])
    assert not [t for t in removed if t in (store, tree, helpers) or t.startswith((tree + "/", helpers + "/"))]
    assert str(reserve_row(host).path) not in removed
    in_store = {t[len(store) + 1:] for t in removed if t.startswith(store + "/")}
    assert in_store == REMOVED_IN_STORE, in_store ^ REMOVED_IN_STORE
