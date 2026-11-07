"""What ``install_helper undo`` leaves on the host (row 121, Linux VM L2 round): the store (``/var/levain``,
with its ``reserve`` file), the root import tree and the helper entries under HELPER_DIR. The kept set is the
documented contract (docs/slot-undo.md).

The plan is EXECUTED against a disposable root: every program a step runs is recorded (never run), every
file-classifying read answers "ours", and the tree under the fake root is compared before and after. So a removal
made by a ``Step.call`` (the entry/1, slot-state and policy removals) is seen, not only a literal ``rm`` argv."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from levain.slot import removal as RM
from levain.slot.layout import HELPER_DIR, IMPORT_TREE, STORE
from levain.slot.grammar import parse_install_argv
from levain.slot.ids import RANGES, SHARED_GROUPS
from levain.slot.install_helper import INTENT_PATH, plan_undo
from tests.test_slot_confinement_plan import HOSTS, _plan, _set_up, _tools_resolve  # noqa: F401  (the autouse fixture rides along)

#: The removed-in-store set, derived from ``plan_undo`` run over a store that holds every file undo may remove.
#: docs/slot-undo.md lists the same set.
REMOVED_IN_STORE = {"policy/pool", "policy/install-shape", "policy/operator"}
OPTIONAL_IN_STORE = {"policy/limits", "policy/dispatch_ceiling", "policy/authority_signers"}
KEPT_IN_STORE = {"reserve", "contracts/c", "lineage/l", "export/x"}
HELPERS = ("install-helper", "runtime-helper", "service-entry", "export-importer", "export-verifier")


def _populate(root: Path, host: str) -> None:
    files = [STORE[host] / p for p in (REMOVED_IN_STORE | OPTIONAL_IN_STORE | KEPT_IN_STORE)]
    files += [IMPORT_TREE[host] / "current", IMPORT_TREE[host] / "gen-1" / "m.py"]
    files += [HELPER_DIR[host] / h for h in HELPERS]
    (root / INTENT_PATH[host].parent.relative_to("/")).mkdir(parents=True, exist_ok=True)
    for f in files:
        p = root / f.relative_to("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")


def _tree(root: Path) -> set[str]:
    return {"/" + str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() or p.is_symlink()}


def _run_undo(host, tmp_path, monkeypatch):
    """The set of absolute paths the undo plan removes from the fake root: files that vanished plus every rm/rmdir
    target a program step recorded."""
    root = tmp_path / "root"
    _populate(root, host)
    ran: list[list[str]] = []

    def fake_run(argv, **kw):
        ran.append([str(a) for a in argv])
        gone = argv[1:2] == ["print"]          # launchctl print: the job is not loaded, so a stop does not wait
        return subprocess.CompletedProcess(argv, 113 if gone else 0, "", "")

    monkeypatch.setattr(RM, "root_file", lambda live, path, check=None: RM.OURS)
    live = RM.Live(run=fake_run, root=root)
    ids = dict(_plan(host).allocation.ids)
    plan = plan_undo(parse_install_argv(["undo"]), _set_up(host), accounts=ids, live=live, end=lambda uid, name: (True, ""),
                     shared_groups={g: RANGES[host].shared[i] for i, g in enumerate(SHARED_GROUPS)})
    before = _tree(root)
    for s in plan.steps:
        if s.argv:
            fake_run(s.argv)
        if s.call is not None:
            s.call()
    after = _tree(root)
    removed = before - after
    for argv in ran:
        if argv and argv[0].rsplit("/", 1)[-1] in ("rm", "rmdir"):
            for a in argv[1:]:
                if a.startswith(str(root)):
                    removed.add(a[len(str(root)):])      # a call step's act, rooted by ``Live.at``
                elif a.startswith("/"):
                    removed.add(a)                       # a literal argv step, which names the host path
    return removed


@pytest.mark.parametrize("host", HOSTS)
def test_undo_leaves_store_root_tree_and_helpers(host, tmp_path, monkeypatch):
    removed = _run_undo(host, tmp_path, monkeypatch)
    store, tree, helpers = str(STORE[host]), str(IMPORT_TREE[host]), str(HELPER_DIR[host])
    assert not [r for r in removed if r.startswith((tree, helpers))], removed
    assert not [r for r in removed if r.startswith(store) and r[len(store) + 1:] not in REMOVED_IN_STORE | OPTIONAL_IN_STORE]
    in_store = {r[len(store) + 1:] for r in removed if r.startswith(store + "/")}
    assert REMOVED_IN_STORE <= in_store <= REMOVED_IN_STORE | OPTIONAL_IN_STORE, in_store
    assert str(STORE[host] / "reserve") not in removed
