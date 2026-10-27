"""1026+8 RULINGS A1/A2: the trust root's store handle demands of a directory comes from ONE place, the layout row
that creates it (``levain.slot.layout``), never a second rule in the runtime helper.

The property, over every directory row: each store dir the helper opens trusted, laid out on disk exactly as its row
says (owner, group, mode), passes the REAL :class:`runtime_helper.Store` with its DEFAULT seams (no ``trusted`` stub).
The disk shape is the row's because ``os.fstat`` reports it for the directories of a scratch store: a test cannot
chown to another uid, and the predicate under test only ever reads the fstat.

Reproduced first: on main (041f062d) ``Store.open_dir("export", "operator")`` refused
``bad-root: '<L>/export/operator'``, the refusal adoptvm ran into on r3c10-vm (``levain runtime-helper: refused
bad-root: '/private/var/levain/export/operator'``), because the row said ``_levain_export`` 2750 and the helper's
predicate demanded uid 0.
"""
from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

import pytest

import levain.slot as slot_pkg
from levain.slot import Refused
from levain.slot import layout as LY
from levain.slot import runtime_helper as RH
from levain.slot.ids import SlotId

HOSTS = ("darwin", "linux")
OPERATOR = "op"
SLOTS = (SlotId("egress", 0), SlotId("none", 0))
WILD = object()


def _uid(name: str) -> int:
    return 0 if name == "root" else 3000 + sum(map(ord, name))


def _gid(name: str) -> int:
    return 0 if name in ("wheel", "root") else 4000 + sum(map(ord, name))


def _dir_rows(host: str) -> dict:
    """Every directory row of the store, by its parts below ``L`` (``()`` is ``L`` itself)."""
    L = LY.STORE[host]
    rows = LY.base_store_rows(host, OPERATOR) + [r for s in SLOTS for r in LY.slot_store_rows(host, s)]
    return {r.path.relative_to(L).parts: r for r in rows if r.kind == "dir"}


def _fd_path(fd: int) -> str:
    if sys.platform == "darwin":
        import fcntl
        return os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0])
    return os.readlink(f"/proc/self/fd/{fd}")


@pytest.fixture
def row_store(tmp_path, monkeypatch):
    """``make(host, shape=None)``: a scratch ``L`` holding every directory row, whose fstat reports each row's owner,
    group and mode (``shape(parts, row) -> (uid, gid, mode)`` overrides one), and a Store on it with the default
    seams."""
    real_fstat = os.fstat

    def make(host, shape=None):
        L = tmp_path / host
        rows = _dir_rows(host)
        for parts in rows:
            (L.joinpath(*parts)).mkdir(parents=True, exist_ok=True)
        top = os.path.realpath(L)

        def fstat(fd):
            st = real_fstat(fd)
            if not isinstance(fd, int) or not stat.S_ISDIR(st.st_mode):
                return st
            p = os.path.realpath(_fd_path(fd))
            if p != top and not p.startswith(top + os.sep):
                return st
            parts = Path(p).relative_to(top).parts
            row = rows.get(parts)
            if row is None:
                return st
            uid, gid, mode = (shape(parts, row) if shape else None) or (_uid(row.owner), _gid(row.group), row.mode)
            return os.stat_result((stat.S_IFDIR | mode, st.st_ino, st.st_dev, st.st_nlink, uid, gid, st.st_size,
                                   int(st.st_atime), int(st.st_mtime), int(st.st_ctime)))

        monkeypatch.setattr(os, "fstat", fstat)
        seams = RH.Seams(host=host, store=L, account_uid=_uid, group_gid=_gid)
        return RH.Store(seams), rows
    return make


# --- which store dirs the helper opens trusted: read from the source, not typed out ---------------------------------

_CALL = re.compile(r"store\.(dir|open_dir)\(([^()]*)\)|store\.(read|replace|create)\(\(([^()]*)\)([^\n]*)")
_LIT = re.compile(r'"([^"]*)"\Z')


def _patterns() -> set:
    """Every ``(parts, trusted)`` a ``store.dir`` / ``open_dir`` / ``read`` / ``replace`` / ``create`` call in
    ``levain/slot`` names, a non-literal component as ``WILD``; a call whose first component is starred (a session's
    own parts) names no layout row and is left out, as is one with a ``...`` argument (prose naming a call, such as
    ``store.dir(..., trusted=True)`` in a docstring, not a call). Every component but the last is always checked
    trusted (``Store.open_dir``), so each prefix is a trusted open too."""
    root = Path(slot_pkg.__file__).parent
    out = set()
    for f in sorted(root.rglob("*.py")):
        for m in _CALL.finditer(f.read_text()):
            args, rest = (m.group(2), "") if m.group(1) else (m.group(4), m.group(5))
            toks = [t.strip() for t in args.split(",") if t.strip()]
            if m.group(1):
                rest = " ".join(t for t in toks if "=" in t)
            toks = [t for t in toks if "=" not in t]
            if not toks or toks[0].startswith("*") or "..." in toks:
                continue
            parts = []
            for t in toks:
                if t.startswith("*"):
                    break
                lit = _LIT.match(t)
                parts.append(lit.group(1) if lit else WILD)
            parts = tuple(parts)
            untrusted = "trusted=False" in rest
            for n in range(1, len(parts) + 1):
                out.add(parts[:n] if n < len(parts) or not untrusted else None)
    out.discard(None)
    return out


def _trusted_rows(host: str) -> list:
    rows = _dir_rows(host)
    hit = set()
    for pat in _patterns():
        for parts in rows:
            if len(parts) == len(pat) and all(p is WILD or p == q for p, q in zip(pat, parts)):
                hit.add(parts)
    return sorted(hit)


def test_the_source_scan_finds_the_adopt_class_dir():
    # the scan must see the call that broke (``ctx.store.dir("export", export_class)``), else the property is vacuous
    for host in HOSTS:
        got = _trusted_rows(host)
        assert ("export", "operator") in got and ("export", "dispatch") in got and ("alloc",) in got, got


@pytest.mark.parametrize("host", HOSTS)
def test_every_dir_the_helper_opens_trusted_passes_its_own_row(row_store, host):
    """A2: the property. Red on main at ``export/operator`` and ``export/dispatch``."""
    store, _rows = row_store(host)
    refused = {}
    for parts in _trusted_rows(host):
        try:
            os.close(store.open_dir(*parts))
        except Refused as e:
            refused["/".join(parts)] = f"{e.name}: {e.detail}"
    assert not refused, refused


@pytest.mark.parametrize("host", HOSTS)
def test_every_dir_the_helper_opens_trusted_is_roots_by_its_row(host):
    """The design answer to A1 as a property: root never acts trusted in a dir another account can write. The class
    dirs are root's (CONTRACT §2.1: ``root : levain-export 0750``); a row that hands one to another owner, or gives
    its group or other a write bit, goes red here before it reaches an install."""
    rows = _dir_rows(host)
    bad = {"/".join(p): (rows[p].owner, oct(rows[p].mode)) for p in _trusted_rows(host)
           if rows[p].owner != "root" or rows[p].mode & 0o022}
    assert not bad, bad


@pytest.mark.parametrize("host", HOSTS)
def test_a_trusted_open_of_a_row_another_account_owns_is_refused(row_store, host):
    """Every row whose owner is not root: a trusted open refuses ``bad-root`` even when the disk matches the row, so
    a later trusted call on such a dir fails closed instead of widening root's trust."""
    store, rows = row_store(host)
    others = [p for p, r in rows.items() if r.owner != "root"]
    assert others
    for parts in others:
        with pytest.raises(Refused) as e:
            os.close(store.open_dir(*parts))
        assert e.value.name == "bad-root", parts


@pytest.mark.parametrize("host", HOSTS)
@pytest.mark.parametrize("drift", ["owner", "group-write", "other-write"])
def test_a_root_row_off_its_shape_on_disk_is_refused(row_store, host, drift):
    """The disk side: a root row whose dir on disk has another owner, or a write bit its row does not give, refuses."""
    target = ("export", "operator")

    def shape(parts, row):
        if parts != target:
            return None
        if drift == "owner":
            return _uid("_levain_export"), _gid(row.group), row.mode
        return 0, _gid(row.group), row.mode | (0o020 if drift == "group-write" else 0o002)
    store, rows = row_store(host, shape)
    assert rows[target].owner == "root"
    with pytest.raises(Refused) as e:
        os.close(store.open_dir(*target))
    assert e.value.name == "bad-root"


@pytest.mark.parametrize("host", HOSTS)
def test_the_root_predicate_is_every_root_rows_own_shape(host):
    """One home: ``layout.admits_root`` (the helper's default ``seams.trusted``) accepts a stat exactly when it has a
    root row's shape, because no root directory row gives its group or other a write bit."""
    rows = _dir_rows(host)
    assert [p for p, r in rows.items() if r.owner == "root" and r.mode & 0o022] == []
    assert RH.Seams().trusted is LY.admits_root
    # store_dir_row answers None for a slot's dirs, so a trusted open of one demands the root shape. The one slot row
    # that is not root's is an egress slot's front log dir (3b-8a T1, the front account's): no trusted open may name it
    trusted = set(_trusted_rows(host))
    others = {p for p, r in rows.items() if r.owner != "root" and LY.store_dir_row(host, p) is None}
    assert others == {("gw", f"slot{s.index}") for s in SLOTS if s.slot_class == "egress"}
    assert not others & trusted, others & trusted
    for parts, r in rows.items():
        st = os.stat_result((stat.S_IFDIR | r.mode, 1, 1, 2, _uid(r.owner), _gid(r.group), 0, 0, 0, 0))
        assert LY.admits_root(st) is (r.owner == "root"), parts


# --- the runtime seam: every trusted open the helper makes while it runs a verb --------------------------------------
# The source scan above reads call sites with a regex, so a call written another way (a nested paren, a starred or
# computed first part, a single-quoted literal) drops out of it. This seam records what Store.open_dir is ASKED at
# runtime, while real verbs run through runtime_helper.main, and checks each one against the layout row it names.

def _load(name: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"layout_trust_{name}", Path(__file__).with_name(f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class OpenLog:
    """Every ``Store.open_dir`` call, recorded before the real one runs, as ``(host, parts, trusted)``."""

    def __init__(self, monkeypatch) -> None:
        self.opens: list = []
        real, log = RH.Store.open_dir, self.opens

        def open_dir(store, *parts, trusted=True):
            log.append((store.seams.host, tuple(parts), trusted))
            return real(store, *parts, trusted=trusted)
        monkeypatch.setattr(RH.Store, "open_dir", open_dir)

    def checked(self) -> set:
        """Every ``(host, prefix)`` the open checks trusted: each prefix of a trusted open, every prefix but the last
        of an untrusted one (``Store.open_dir``)."""
        out = set()
        for host, parts, trusted in self.opens:
            for n in range(1, len(parts) + (1 if trusted else 0)):
                out.add((host, parts[:n]))
        return out

    def off_row(self) -> dict:
        """Each checked dir that has a layout row the row itself fails: not root's, or a shape ``admits_root``
        refuses. A dir with no base row (a session's, a slot's) is the verb's to make root's and is not judged here."""
        bad = {}
        for host, parts in self.checked():
            row = LY.store_dir_row(host, parts)
            if row is None:
                continue
            st = os.stat_result((stat.S_IFDIR | row.mode, 1, 1, 2, _uid(row.owner), _gid(row.group), 0, 0, 0, 0))
            if row.owner != "root" or not LY.admits_root(st):
                bad["/".join(parts)] = (row.owner, oct(row.mode))
        return bad


@pytest.fixture
def open_log(monkeypatch):
    return OpenLog(monkeypatch)


def test_discard_and_restore_open_trusted_only_dirs_their_rows_give_root(open_log, tmp_path):
    rv = _load("test_slot_rootverbs_discard_restore")
    world = rv.World(tmp_path)
    assert world.main("discard")[0] == RH.EXIT_OK
    assert world.main("restore")[0] == RH.EXIT_OK
    rows = {p for _h, p in open_log.checked() if LY.store_dir_row("darwin", p) is not None}
    # the seam saw the verbs' own Store opens; the retained -> trash move itself goes by path in store_verbs, not
    # through Store (an observation for the head, 1026+8 adoptf1)
    assert {("alloc",), ("policy",)} <= rows, sorted(rows)
    assert not open_log.off_row(), open_log.off_row()


def test_adopt_open_and_verify_open_trusted_only_dirs_their_rows_give_root(open_log, tmp_path):
    """The verbs that broke: adopt-open's ``export/operator`` open refused ``bad-root`` on r3c10-vm (1026+8 A1)."""
    av = _load("test_slot_s5d1b_adopt_verify")
    if sys.platform != "darwin" or av.fx.GIT is None or not os.path.exists(av.ER.HDIUTIL):
        pytest.skip("the seam run needs macOS hdiutil and a git")
    vrig = av.VRig(tmp_path)                        # runs adopt-open, asserting it exits 0
    try:
        rc, _out, err = vrig.verify()
        assert rc == 0, err
    finally:
        vrig.rig.cleanup()
    rows = {p for _h, p in open_log.checked() if LY.store_dir_row("darwin", p) is not None}
    assert {("export",), ("export", "operator"), ("alloc",), ("retained",)} <= rows, sorted(rows)
    assert not open_log.off_row(), open_log.off_row()


def test_the_seam_goes_red_on_a_row_another_account_owns(open_log, tmp_path, monkeypatch):
    """The seam is not vacuous: with the row 041f062d had (``export/operator`` ``_levain_export`` 2750), a recorded
    trusted open of it is named."""
    host = "darwin"
    real = LY._store_dir_rows(host)
    old = LY.Row(LY.STORE[host] / "export" / "operator", "_levain_export", "levain-export", 0o2750)
    patched = {**real, ("export", "operator"): old}
    monkeypatch.setattr(LY, "store_dir_row", lambda h, parts: patched.get(tuple(parts)) if h == host
                        else real.get(tuple(parts)))
    open_log.opens.append((host, ("export", "operator", "s-x"), False))
    assert open_log.off_row() == {"export/operator": ("_levain_export", "0o2750")}


def test_the_row_lookup_builds_the_layout_once_per_host(monkeypatch):
    LY._store_dir_rows.cache_clear()
    calls = []
    real = LY.base_store_rows
    monkeypatch.setattr(LY, "base_store_rows", lambda h, op: calls.append(h) or real(h, op))
    try:
        for _ in range(3):
            assert LY.store_dir_row("darwin", ("export", "operator")).owner == "root"
            assert LY.store_dir_row("darwin", ("live", "s-x")) is None
        assert calls == ["darwin"]
    finally:
        LY._store_dir_rows.cache_clear()
