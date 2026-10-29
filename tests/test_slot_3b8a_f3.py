"""S1 3b-8a fix round 3 (head rulings F1, F3, S-1 on the s3b8a L3 r2 triage).

F1: ``prepare_front_log`` acts below ``L/gw/`` only when it is root's alone (``seams.trusted``), as
``write_front_policy`` does for ``R/gw/``. F3: the front's log writer runs on its event loop only. S-1: a missing
``L/gw/slot<n>/`` (an install made before the store log dir existed) is refused ``store-missing`` naming
``levain repair``, and ``levain doctor`` reports it with that remedy."""
from __future__ import annotations

import ast
import dataclasses
import os
import re
import sys
from pathlib import Path

import pytest

from levain import doctor
from levain.broker import front as FR
from levain.slot import layout
from levain.slot import runtime_helper as H
from levain.slot.grammar import parse_install_argv
from tests.test_slot_3b8a_front_wiring import _world
from tests.test_slot_s2f2_runtime_helper import _blob, _prepared


# --- F1: L/gw must be root's alone ------------------------------------------------------------------------------------

@pytest.mark.parametrize("plant", ["group-writable", "symlink"])
def test_an_untrusted_store_gw_is_refused_before_the_log_is_touched(tmp_path, plant):
    w = _world(tmp_path)
    gw = w.L / "gw"
    if plant == "group-writable":
        os.chmod(gw, 0o775)
    else:
        moved = tmp_path / "gw-elsewhere"
        os.rename(gw, moved)
        os.symlink(moved, gw)
    s = _prepared(w)
    line = w.main(["render", s], _blob())[1]
    assert line == {"ok": False, "refused": "bad-root"}, w.last_err
    assert w.fronts.procs == {}
    assert all(rel != "gw/slot0/front.log" for rel, _u, _g in w.chowns)   # nothing chowned below an untrusted L/gw


def test_prepare_front_log_checks_store_gw_itself(tmp_path):
    """At the point of use: called directly, not only through render's earlier checks."""
    w = _world(tmp_path)
    os.chmod(w.L / "gw", 0o777)
    store = H.Store(w.seams)
    try:
        with pytest.raises(H.Refused) as e:
            H.prepare_front_log(store, H.SlotId("egress", 0), 898)
    finally:
        store.close()
    assert e.value.name == "bad-root" and "gw" in str(e.value)


# --- F3: the log writer runs on the event loop only --------------------------------------------------------------------

def test_every_log_write_is_made_on_the_event_loop():
    """complement LOW: ``Front._append`` is not atomic across threads, so it must never be reached from one. Every call
    of ``_log`` / ``_append`` in the front's module sits directly in an ``async def`` (a coroutine runs on the loop's
    thread), except ``_append``'s one call, inside ``_log``; the resolver's thread (``system_resolve``'s ``work``) and
    every other plain function call neither."""
    tree = ast.parse(Path(FR.__file__).read_text())
    seen = []

    def walk(node, fn):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child)
                continue
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) \
                    and child.func.attr in ("_log", "_append"):
                seen.append((child.func.attr, fn.name if fn else None, isinstance(fn, ast.AsyncFunctionDef)))
            walk(child, fn)

    walk(tree, None)
    assert sum(1 for a, _f, _c in seen if a == "_log") >= 5            # the probe found the callers it pins
    bad = [s for s in seen if not (s[2] or (s[0] == "_append" and s[1] == "_log"))]
    assert bad == [], bad
    assert [s for s in seen if s[0] == "_append"] == [("_append", "_log", False)]


# --- S-1: a missing L/gw/slot<n>/ names its remedy --------------------------------------------------------------------

def test_repair_line_is_a_command_the_install_helper_parses():
    for host in ("linux", "darwin"):
        line = layout.repair_line(host)
        sudo, helper, *argv = line.split()
        assert sudo == "sudo" and helper == str(layout.install_helper_path(host))
        assert parse_install_argv(argv).verb == "repair"


@pytest.mark.parametrize("gone", ["slot0", "gw"])
def test_a_missing_store_log_dir_is_refused_naming_repair(tmp_path, gone):
    """An install made before b732b63a has neither ``L/gw/`` nor ``L/gw/slot<n>/``. S-6: the refusal names the dir
    that is actually missing."""
    w = _world(tmp_path)
    os.rmdir(w.L / "gw" / "slot0")
    if gone == "gw":
        for d in (w.L / "gw").iterdir():
            os.rmdir(d)
        os.rmdir(w.L / "gw")
    s = _prepared(w)
    line = w.main(["render", s], _blob())[1]
    assert line == {"ok": False, "refused": "store-missing"} and w.fronts.procs == {}
    assert layout.repair_line(w.seams.host) in w.last_err and "gw/slot0" in w.last_err, w.last_err
    missed = f"{w.L}/gw is missing" if gone == "gw" else f"{w.L}/gw/slot0: the front log dir is missing"
    assert missed in w.last_err, w.last_err


@pytest.mark.parametrize("gone", ["slot0", "gw"])
def test_prepare_front_log_rewrites_a_genuine_miss_naming_the_missing_dir(tmp_path, gone):
    """S-6 positive (deepseek MED 2, LOW 6): called directly, a missing ``L/gw/slot<n>/`` or ``L/gw/`` is the
    rewritten ``store-missing``, naming the path that failed and the repair line."""
    w = _world(tmp_path)
    os.rmdir(w.L / "gw" / "slot0")
    if gone == "gw":
        for d in (w.L / "gw").iterdir():
            os.rmdir(d)
        os.rmdir(w.L / "gw")
    store = H.Store(w.seams)
    try:
        with pytest.raises(H.Refused) as e:
            H.prepare_front_log(store, H.SlotId("egress", 0), 898)
    finally:
        store.close()
    assert e.value.name == "store-missing"
    assert e.value.detail.startswith(f"{w.L}/gw is missing," if gone == "gw"
                                     else f"{w.L}/gw/slot0: the front log dir is missing"), e.value.detail
    assert f"`{layout.repair_line(w.seams.host)}`" in e.value.detail


def _dirs(store: Path, *rels: str, mode: int = 0o755) -> None:
    """Make each dir below ``store`` with ``mode`` set explicitly (never the umask's)."""
    for rel in rels:
        p = store / rel
        p.mkdir()
        os.chmod(p, mode)


def _doctor_store(tmp: Path, tags: dict, gw: tuple = ()) -> Path:
    """A doctor store whose root and ``gw/`` (with ``gw/slot<n>`` for each ``n`` in ``gw``) have modes set
    explicitly: the runtime's walk holds the root and ``gw/`` to ``trusted`` (no group or other write)."""
    from tests.test_doctor_slot_isolation import _store
    live = [t for t, st in tags.items() if st != "retired"]
    store = _store(tmp, tags, pool=(sum(1 for t in live if t[0] == "e"), sum(1 for t in live if t[0] == "v")))
    os.chmod(store, 0o755)
    if gw:
        _dirs(store, "gw", *(f"gw/slot{n}" for n in gw))
    return store


#: what the tests' seams map the front account to: an id in §1.1's linux slot range (``ids.in_own_range``), which
#: ``chowned_uid`` maps to this test's own uid (nothing is chowned by a non-root test)
FRONT_UID = 898


def _test_user_seams(**over):
    """The runtime's predicates with root and the front account both mapped to this test's own uid."""
    uid = os.getuid()
    kw = dict(host="linux", trusted=lambda st: st.st_uid == uid and not st.st_mode & 0o022,
              account_uid=lambda name: FRONT_UID, chowned_uid=lambda u: uid,
              chown=lambda fd, u, g, rel: None, fsync=lambda fd: None)
    kw.update(over)
    return H.Seams(**kw)


def _front_row(store: Path, seams=None):
    rows = {r.name: r for r in doctor._check_slot_isolation(host="linux", store=store, free_bytes=lambda p: 100 << 30,
                                                            seams=seams or _test_user_seams())}
    return rows.get("agent front logs"), rows


def _named(row) -> set:
    """The slot tags the row names, from its ``<tags>: <why>`` segments."""
    return {t for seg in re.findall(r"(?:^|: |; )((?:[ev]\d+, )*[ev]\d+): ", row.detail) for t in seg.split(", ")}


def _runtime(store: Path, seams, n: int = 0):
    """What render would refuse for egress slot ``n``: its account lookup (``slot_ids``), then prepare_front_log."""
    seams = dataclasses.replace(seams, store=store)
    st = H.Store(seams)
    try:
        front = H.account_in_range(seams, H.SlotId("egress", n).accounts[1])
        H.prepare_front_log(st, H.SlotId("egress", n), front)
        return None
    except H.Refused as e:
        return e.name
    finally:
        st.close()


def test_doctor_reports_a_missing_slot_log_dir_with_the_repair_line(tmp_path):
    store = _doctor_store(tmp_path, {"e0": "reusable", "e1": "leased", "e2": "retired", "v1": "reusable"}, gw=(0,))
    row, rows = _front_row(store)
    assert row is not None and not row.ok and row.upgrade_pending
    assert _named(row) == {"e1"}                                                   # a tombstone is not counted
    assert f"{store}/gw/slot1: the front log dir is missing" in row.detail
    assert layout.repair_line("linux") in row.hint
    assert rows["agent slots"].ok                                                  # the slots row is kept


def test_doctor_front_log_row_green_absent_and_linked(tmp_path):
    store = _doctor_store(tmp_path / "a", {"e0": "reusable", "e1": "reusable"}, gw=(0, 1))
    row, _ = _front_row(store)
    assert row.ok and not row.warn and "2 egress slot" in row.detail
    # no egress slot (a verifier-only pool): no row at all
    assert _front_row(_doctor_store(tmp_path / "b", {"v0": "reusable"}))[0] is None
    # L/gw is a link: the runtime refuses it (bad-root), so the doctor does too, never green and never upgrade-pending
    store = _doctor_store(tmp_path / "c", {"e0": "reusable"})
    _dirs(tmp_path, "realgw", "realgw/slot0")
    os.symlink(tmp_path / "realgw", store / "gw")
    row, _ = _front_row(store)
    assert not row.ok and not row.upgrade_pending and f"{store}/gw is not a directory (bad-root)" in row.detail
    assert _runtime(store, _test_user_seams()) == "bad-root"
    # a whole store with no gw/ (made before it existed): every egress slot named, L/gw named as the missing dir
    store = _doctor_store(tmp_path / "d", {"e0": "reusable", "e3": "scrubbed"})
    row, _ = _front_row(store)
    assert not row.ok and row.upgrade_pending and _named(row) == {"e0", "e3"}
    assert f"{store}/gw is missing" in row.detail


# --- S-5: the doctor row judges L/gw and L/gw/slot<n> by the runtime's own predicates --------------------------------

def test_doctor_front_row_is_red_where_the_runtime_refuses_bad_root(tmp_path):
    for plant in ("slot-group-writable", "slot-other-writable", "gw-group-writable"):
        store = _doctor_store(tmp_path / plant, {"e0": "reusable"}, gw=(0,))
        if plant.startswith("slot"):
            os.chmod(store / "gw" / "slot0", 0o770 if "group" in plant else 0o707)
        else:
            os.chmod(store / "gw", 0o775)
        row, _ = _front_row(store)
        assert not row.ok and not row.upgrade_pending and "bad-root" in row.detail, (plant, row)
        assert _named(row) == {"e0"}
        assert (("gw/slot0 is not the front account's alone" if plant.startswith("slot") else f"{store}/gw (")
                in row.detail), row.detail


def test_doctor_front_row_is_red_for_a_wrong_owner(tmp_path):
    """Constructible through the seams the runtime reads: the front account is another uid than the dir's owner, and
    a non-root gw owner."""
    store = _doctor_store(tmp_path / "a", {"e0": "reusable"}, gw=(0,))
    row, _ = _front_row(store, _test_user_seams(chowned_uid=lambda uid: uid + 1))
    assert not row.ok and not row.upgrade_pending and "bad-root" in row.detail and _named(row) == {"e0"}
    store = _doctor_store(tmp_path / "b", {"e0": "reusable"}, gw=(0,))
    root = os.stat(store)                       # trusted: the store root only, so L/gw has a non-root owner
    seams = _test_user_seams(trusted=lambda st: (st.st_dev, st.st_ino) == (root.st_dev, root.st_ino))
    row, _ = _front_row(store, seams)
    assert not row.ok and not row.upgrade_pending and f"{store}/gw (bad-root)" in row.detail


def _plant(store: Path, tmp: Path, plant: str):
    """codex's L3 r4 scenarios (and the green control) on a store with ``gw/slot0``; the seams to judge it by."""
    gw = store / "gw"
    if plant == "missing-front-account":
        def gone(name):
            raise H.Refused("account-missing", name)
        return _test_user_seams(account_uid=gone)
    if plant == "regular-file-gw":
        os.rmdir(gw / "slot0")
        os.rmdir(gw)
        gw.write_text("")
    elif plant == "symlinked-slot0":
        _dirs(tmp, "real-slot0")
        os.rmdir(gw / "slot0")
        os.symlink(tmp / "real-slot0", gw / "slot0")
    elif plant == "gw-0777":
        os.chmod(gw, 0o777)
    elif plant == "slot0-a-file":
        os.rmdir(gw / "slot0")
        (gw / "slot0").write_text("")
    return _test_user_seams()


@pytest.mark.parametrize("plant,refused", [("missing-front-account", "account-missing"),
                                           ("regular-file-gw", "bad-root"), ("symlinked-slot0", "bad-root"),
                                           ("gw-0777", "bad-root"), ("slot0-a-file", "bad-root"),
                                           ("green", None)])
def test_doctor_row_is_what_the_runtime_refuses(tmp_path, plant, refused):
    """S-7 (codex HIGH 1, HIGH 2, MED 3, MED 4): the row is the runtime's own verdict. A refusal the runtime makes is
    a failing row naming the slot and the refusal, and only a genuinely absent dir is upgrade-pending."""
    store = _doctor_store(tmp_path, {"e0": "reusable"}, gw=(0,))
    seams = _plant(store, tmp_path, plant)
    assert _runtime(store, seams) == refused
    row, _ = _front_row(store, seams)
    if refused is None:
        assert row.ok and not row.warn, row
        return
    assert not row.ok and not row.warn and not row.upgrade_pending, row
    assert f"({refused})" in row.detail and _named(row) == {"e0"}, row.detail


def test_doctor_and_runtime_agree_on_the_same_dirs(tmp_path):
    """One derivation: whatever the doctor row calls green, prepare_front_log accepts, and the reverse."""
    for where in ("gw/slot0", "gw"):
        for mode, ok in ((0o755, True), (0o775, False), (0o757, False)):
            store = _doctor_store(tmp_path / where.replace("/", "-") / f"m{mode:o}", {"e0": "reusable"}, gw=(0,))
            os.chmod(store / where, mode)
            seams = _test_user_seams()
            row, _ = _front_row(store, seams)
            assert (_runtime(store, seams) is None) == row.ok == ok, (where, mode, row)


def test_one_row_names_every_slots_refusal(tmp_path):
    """deepseek LOW 4/5: e0's dir missing, e1's front account missing, e3's dir wrong: one row names all three, and
    the gw half is still walked for the slot with no account. Not upgrade-pending: two are not an absence."""
    store = _doctor_store(tmp_path, {"e0": "reusable", "e1": "reusable", "e3": "reusable"}, gw=(1, 3))
    os.chmod(store / "gw" / "slot3", 0o775)

    def acct(name):
        if name == "_levain_gw1":
            raise H.Refused("account-missing", name)
        return FRONT_UID
    row, _ = _front_row(store, _test_user_seams(account_uid=acct))
    assert not row.ok and not row.upgrade_pending and _named(row) == {"e0", "e1", "e3"}, row.detail
    for why in ("(store-missing)", "_levain_gw1 (account-missing)", "gw/slot3 is not the front account's alone"):
        assert why in row.detail, row.detail
    # the same gw failure, met by every slot, is said once with every slot it stops
    os.chmod(store / "gw", 0o777)
    row, _ = _front_row(store, _test_user_seams(account_uid=acct))
    assert f"e0, e1, e3: {store}/gw (bad-root)" in row.detail, row.detail


@pytest.mark.skipif(os.getuid() == 0, reason="root reads a mode-0 dir")
def test_an_unreadable_front_log_dir_is_a_warning_never_absent_or_green(tmp_path):
    store = _doctor_store(tmp_path, {"e0": "reusable"}, gw=(0,))
    os.chmod(store / "gw", 0)
    try:
        row, _ = _front_row(store)
    finally:
        os.chmod(store / "gw", 0o755)
    assert row.ok and row.warn and not row.upgrade_pending and "could not be checked" in row.detail, row
    assert "e0 (permission denied" in row.detail


@pytest.mark.skipif(os.getuid() == 0, reason="root reads a mode-0 dir")
def test_a_front_log_dir_the_operator_cannot_read_is_judged_by_its_stat(tmp_path):
    """``L/gw/slot<n>/`` is the front account's 2750 (``layout.slot_store_rows``): the operator running the doctor may
    search ``L/gw/`` but not read it, so an open of it would be EACCES on every healthy install. The check judges it
    by its stat through ``L/gw/``'s fd: a mode-0 dir this test owns stands in for the front account's dir, and the row
    is green (an open of it is refused, the stat is not)."""
    store = _doctor_store(tmp_path, {"e0": "reusable"}, gw=(0,))
    os.chmod(store / "gw" / "slot0", 0)
    try:
        with pytest.raises(PermissionError):
            os.close(os.open(store / "gw" / "slot0", os.O_RDONLY | os.O_DIRECTORY))
        row, _ = _front_row(store)
    finally:
        os.chmod(store / "gw" / "slot0", 0o755)
    assert row.ok and not row.warn, row
    # the row the mode-0 dir stands in for
    (r,) = [r for r in layout.slot_store_rows("linux", H.SlotId("egress", 0)) if str(r.path).endswith("gw/slot0")]
    assert r.mode == 0o2750 and r.owner == "_levain_gw0"


@pytest.mark.parametrize("swap", ["replaced", "removed", "linked"])
def test_prepare_front_log_refuses_a_slot_dir_changed_after_it_was_judged(tmp_path, monkeypatch, swap):
    """S-8: the walk judges ``L/gw/slot<n>/`` by its stat, and root's open after it must reach that same dir (its
    ``st_dev``, ``st_ino``). Here the dir is changed between the stat and the open: replaced by a fresh dir that
    passes the same predicate, removed, or replaced by a link to a good dir. Each is ``bad-root``, never the
    pre-move ``store-missing``, and no log is made in what took its place."""
    w = _world(tmp_path)
    slot0 = w.L / "gw" / "slot0"
    real = H._statat_dir

    def judged_then_changed(parent, name, where):
        st = real(parent, name, where)
        os.rename(slot0, tmp_path / "judged")
        if swap == "replaced":
            _dirs(w.L, "gw/slot0", mode=st.st_mode & 0o7777)
        elif swap == "linked":
            _dirs(tmp_path, "elsewhere", mode=st.st_mode & 0o7777)
            os.symlink(tmp_path / "elsewhere", slot0)
        return st
    monkeypatch.setattr(H, "_statat_dir", judged_then_changed)
    store = H.Store(w.seams)
    try:
        with pytest.raises(H.Refused) as e:
            H.prepare_front_log(store, H.SlotId("egress", 0), FRONT_UID)
    finally:
        store.close()
    assert e.value.name == "bad-root", e.value
    assert "gw/slot0" in e.value.detail and layout.repair_line(w.seams.host) not in e.value.detail
    assert not any(p.name == H.FRONT_LOG_NAME for p in tmp_path.rglob("*"))


def test_the_doctor_check_never_opens_the_slot_dir(tmp_path, monkeypatch):
    """S-8: ``check_front_log_dirs`` stops at the stat; only ``prepare_front_log`` opens ``L/gw/slot<n>/``."""
    store = _doctor_store(tmp_path, {"e0": "reusable"}, gw=(0,))
    opened = []
    real = os.open

    def spy(path, flags, *a, **k):
        opened.append(str(path))
        return real(path, flags, *a, **k)
    monkeypatch.setattr(H.os, "open", spy)
    row, _ = _front_row(store)
    assert row.ok and not row.warn, row
    assert "gw" in opened and not any(p.endswith("slot0") for p in opened), opened


def test_doctor_front_row_without_a_front_account_fails_naming_the_slot(tmp_path):
    """codex HIGH 1: render refuses ``account-missing`` (``slot_ids``), so the row fails, never a warning or green."""
    store = _doctor_store(tmp_path, {"e0": "reusable", "e1": "reusable"}, gw=(0, 1))

    def gone(name):
        if name == "_levain_gw1":
            raise H.Refused("account-missing", name)
        return FRONT_UID
    row, _ = _front_row(store, _test_user_seams(account_uid=gone))
    assert not row.ok and not row.warn and not row.upgrade_pending
    assert _named(row) == {"e1"} and "e1: _levain_gw1 (account-missing)" in row.detail
    # O-s3b8af5-2: repair re-renders owner and mode (install_helper.plan_repair's row_steps) and refuses a missing
    # account (_check_rendered_accounts), so the hint says so rather than promising repair fixes what is named
    assert "never makes a missing account" in row.hint and "does not replace a link" in row.hint, row.hint


# --- the real seams: the default construction and account_uid's raise contract -------------------------------------

def test_the_doctor_builds_the_runtimes_default_seams_for_its_store(tmp_path, monkeypatch):
    """deepseek MED 1: ``_check_slot_isolation()``'s production path (no host, no seams: ``_run_doctor``'s call) builds
    ``H.Seams`` with the host it resolves and the store it reads. Nothing here reaches NSS: the one construction is
    observed and given this test's accounts."""
    store = _doctor_store(tmp_path, {"e0": "reusable"}, gw=(0,))
    built = []
    host = "darwin" if sys.platform == "darwin" else "linux"
    # _levain_gw0 in §1.1's slot range on each host (the front-wiring tests' darwin id; FRONT_UID on linux)
    base = _test_user_seams(account_uid=lambda name: {"linux": FRONT_UID, "darwin": 3998}[host])

    def seams(**kw):
        built.append(kw)
        return dataclasses.replace(base, **kw)
    monkeypatch.setattr(H, "Seams", seams)
    rows = {r.name: r for r in doctor._check_slot_isolation(store=store, free_bytes=lambda p: 100 << 30)}
    assert built == [{"host": host, "store": store}]
    row = rows["agent front logs"]
    assert row.ok and not row.warn, row


@pytest.mark.skipif(os.getuid() == 0, reason="the test store is root's when root runs it")
def test_the_real_default_seams_refuse_a_store_root_does_not_own(tmp_path):
    """The default ``H.Seams`` unpatched: the test's store is not root's, so the runtime refuses it ``bad-root``
    before any account is read, and the doctor row says the same (no exception, a failing row)."""
    store = _doctor_store(tmp_path, {"e0": "reusable"}, gw=(0,))
    with pytest.raises(H.Refused) as e:
        H.Store(H.Seams(store=store))
    assert e.value.name == "bad-root"
    row = {r.name: r for r in doctor._check_slot_isolation(store=store,
                                                           free_bytes=lambda p: 100 << 30)}["agent front logs"]
    assert not row.ok and not row.upgrade_pending and "(bad-root)" in row.detail, row


def test_the_real_account_uid_raises_only_refused_for_a_missing_account(monkeypatch):
    """deepseek MED 3: the default ``Seams.account_uid`` is ``_account_uid``, which turns ``pwd``'s ``KeyError`` (the
    one miss ``pwd.getpwnam`` reports) into ``Refused("account-missing")``."""
    import pwd

    assert H.Seams().account_uid is H._account_uid

    def miss(name):
        raise KeyError(name)
    monkeypatch.setattr(pwd, "getpwnam", miss)
    with pytest.raises(H.Refused) as e:
        H.Seams().account_uid("_levain_gw0")
    assert e.value.name == "account-missing" and e.value.detail == "_levain_gw0"


@pytest.mark.parametrize("exc", [KeyError("_levain_gw0"), OSError(5, "Input/output error")])
def test_an_account_lookup_that_raises_otherwise_is_could_not_be_read(tmp_path, exc):
    """A seam that raises what is not a ``Refused`` (a directory service fault) is said as unreadable: the row warns,
    the doctor goes on."""
    store = _doctor_store(tmp_path, {"e0": "reusable"}, gw=(0,))

    def broken(name):
        raise exc
    row, _ = _front_row(store, _test_user_seams(account_uid=broken))
    assert row.ok and row.warn and "could not be checked, so not judged: e0 (" in row.detail, row


# --- S-6: only the gw / gw/slot<n> miss is rewritten as the missing front log dir -------------------------------------

def test_a_store_missing_from_elsewhere_keeps_its_own_message(tmp_path, monkeypatch):
    w = _world(tmp_path)

    def other(*a, **k):
        raise H.Refused("store-missing", "something-else")
    monkeypatch.setattr(H.Store, "open_dir", other)
    store = H.Store(w.seams)
    try:
        with pytest.raises(H.Refused) as e:
            H.prepare_front_log(store, H.SlotId("egress", 0), 898)
    finally:
        store.close()
    assert e.value.name == "store-missing" and e.value.detail == "something-else"
    assert "front log dir is missing" not in str(e.value)


# --- r5: the row's clauses, the opened slot dir re-judged, every slot account -----------------------------------------

#: the row's clause headers (doctor._front_log_rows): refused, refused until the missing dir is made, not checked
_CLAUSES = ("refused, so no session on them starts: ", "refused until the missing dir is made: ",
            "could not be checked, so not judged: ")


def _clauses(row) -> dict:
    """Each clause header the detail carries -> the slot tags named under it (a tag is ``[ev]<n>`` before ``: `` or
    `` (``)."""
    found, at = {}, sorted((row.detail.find(h), h) for h in _CLAUSES if h in row.detail)
    for i, (start, h) in enumerate(at):
        end = at[i + 1][0] if i + 1 < len(at) else len(row.detail)
        found[h] = set(re.findall(r"\b([ev]\d+)(?=[:, ]|$)", row.detail[start + len(h):end]))
    return found


@pytest.mark.parametrize("kinds", [("absent", "unread"), ("refused", "unread"), ("refused", "absent"),
                                   ("absent", "refused", "unread")])
def test_each_slot_is_said_under_its_own_verdict(tmp_path, kinds):
    """codex r5 MED: e0's dir genuinely absent, e1's account lookup a directory-service fault, e2's dir group-writable.
    Each slot is named only under its own clause: one never judged is never said refused, and the could-not-be-checked
    clause is kept beside a failure. The verdict is the worst real one: a refusal fails, only-absent is
    upgrade-pending."""
    tags = {"absent": "e0", "unread": "e1", "refused": "e2"}
    live = {tags[k]: "reusable" for k in kinds}
    gw = tuple(int(tags[k][1:]) for k in kinds if k != "absent")
    store = _doctor_store(tmp_path, live, gw=gw)
    if "refused" in kinds:
        os.chmod(store / "gw" / "slot2", 0o775)

    def acct(name):
        if name.endswith("1"):
            raise OSError(5, "Input/output error")
        return FRONT_UID
    row, _ = _front_row(store, _test_user_seams(account_uid=acct))
    want = {h: {tags[k]} for k, h in zip(("refused", "absent", "unread"), _CLAUSES) if k in kinds}
    assert _clauses(row) == want, row.detail
    assert not row.ok and not row.warn, row
    assert row.upgrade_pending == ("refused" not in kinds), row
    if "unread" in kinds:
        assert "Input/output error" in row.detail and "not judged" in row.hint, row


def test_prepare_front_log_re_judges_the_opened_slot_dir(tmp_path, monkeypatch):
    """complement r5 LOW 1: the front account owns ``L/gw/slot<n>/`` and can chmod it between the stat and root's open:
    the same inode, so the identity check passes. The opened fd is held to the same predicate, and no log is made."""
    w = _world(tmp_path)
    slot0 = w.L / "gw" / "slot0"
    real = H._openat_dir

    def chmod_then_open(parent, name, where):
        if name == "slot0":
            os.chmod(slot0, 0o775)
        return real(parent, name, where)
    monkeypatch.setattr(H, "_openat_dir", chmod_then_open)
    store = H.Store(w.seams)
    try:
        with pytest.raises(H.Refused) as e:
            H.prepare_front_log(store, H.SlotId("egress", 0), FRONT_UID)
    finally:
        store.close()
    assert e.value.name == "bad-root" and "gw/slot0" in e.value.detail, e.value
    assert not (slot0 / H.FRONT_LOG_NAME).exists()


@pytest.mark.parametrize("refusal", ["account-missing", "account-out-of-range"])
def test_a_missing_slot_account_fails_the_row_as_render_refuses(tmp_path, refusal):
    """complement r5 LOW 2: render's ``slot_ids`` looks up both accounts of the slot, and no other doctor row reads the
    slot account [judged by s3b8af7, 10-26, against git grep of levain/doctor.py], so this row does: a missing or
    out-of-range ``_levain_s0`` fails it, as render would."""
    store = _doctor_store(tmp_path, {"e0": "reusable"}, gw=(0,))

    def acct(name):
        if name == "_levain_s0":
            if refusal == "account-missing":
                raise H.Refused("account-missing", name)
            return 5                                # outside §1.1's slot range
        return FRONT_UID
    seams = _test_user_seams(account_uid=acct)
    with pytest.raises(H.Refused) as e:
        {n: H.account_in_range(seams, n) for n in H.SlotId("egress", 0).accounts}     # slot_ids' own loop
    assert e.value.name == refusal
    row, _ = _front_row(store, seams)
    assert not row.ok and not row.warn and not row.upgrade_pending, row
    assert f"({refusal})" in row.detail and "_levain_s0" in row.detail and _named(row) == {"e0"}, row.detail
