"""S1 3c-9: ``routes-trust --replace`` (3c design r3 Q16, restored to 3c-9 by R4 §5). A real lib root under a scratch
dir: a route map signed by a real ssh-keygen key under one store (``tests/test_routemaps.py``'s fixtures), CONTROL's
head written by :class:`levain.slot.routemaps.ControlRoutes`, keys' by :class:`levain.slot.routemaps.KeysRoutes`, a real
ticket journal written by :class:`levain.slot.tickets.TicketStore`; then the install helper's own verb, run as this
uid through the seams the routes-trust tests use (``tests/test_slot_routes_verbs.py``), never as root."""
from __future__ import annotations

import fcntl
import json
import functools
import io
import os
import pwd
import secrets
import shutil
import stat
import tempfile
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused, routemaps as RM, tickets as T
from levain.slot import install_helper as IH, runtime_helper as RH
from levain.slot.grammar import parse_install_argv
from levain.slot.install_helper import Seams, main, operator_record_text
from tests.test_routemaps import OTHER_STORE, STORE_ID, Signer, Store, item, sha, submit
from tests.test_tickets import mint_args, write_ceiling

ME = os.getuid()
MY_NAME = pwd.getpwuid(ME).pw_name
SESSION = "s-" + "1" * 16


@pytest.fixture
def old():
    """A store pinned to signer ``op`` and STORE_ID, generation 1 active at CONTROL and served by keys."""
    d = Path(tempfile.mkdtemp(prefix="rtr", dir="/tmp"))
    try:
        st = Store(d, Signer(d, "op"))
        (d / "policy").mkdir()
        (d / "policy" / "operator").write_text(operator_record_text(MY_NAME, ME))
        (d / "sessions").mkdir(mode=0o700)
        (st.routes / "verifiers").mkdir(mode=0o700)
        (st.routes / "verifiers" / "head").write_bytes(b"{}")
        one, sig = st.signed(item(1))
        submit(RM.ControlRoutes(st.routes), one, sig)
        kr = RM.KeysRoutes(str(d))
        _checked, head = kr.check_reload(1, sha(one))
        kr.commit(kr.pruned(head, frozenset()))
        st.one = one
        st.new = Signer(d, "op2")
        _plain(st, _req(st.signer, STORE_ID))       # the pin as root writes it (the fixture's trust.pub has a comment)
        yield st
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _line(signer: Signer) -> str:
    return " ".join(signer.pub.split()[:2])          # ssh-keygen's .pub carries a comment; the request takes none


def _req(signer: Signer, store_id: str) -> bytes:
    return canon.canonical_bytes({"format": IH.ROUTES_TRUST_REQUEST, "store_id": store_id,
                                  "fingerprint": signer.fp, "public_key": _line(signer)})


class _Lock:
    def __call__(self, host=None):
        class _Cm:
            def __enter__(self):
                return None

            def __exit__(self, *a):
                return None
        return _Cm()


def _rs(L: Path) -> RH.Seams:
    return RH.Seams(host="darwin", store=L, trusted=lambda st: True)


def _plain(st: Store, raw: bytes) -> dict:
    return IH.run_routes_trust(ME, "darwin", read=io.BytesIO(raw).read, lock=_Lock(), store_seams=_rs(st.L))


def _replace(st: Store, raw: bytes, **kw) -> dict:
    return IH.run_routes_trust(ME, "darwin", read=io.BytesIO(raw).read, lock=_Lock(), store_seams=_rs(st.L),
                               replace=True, **kw)


def _snapshot(L: Path) -> dict:
    """Every file under ``L/routes`` and ``L/keys`` with its bytes (what a refused replace must leave as it was)."""
    out = {}
    for top in ("routes", "keys"):
        for p in sorted((L / top).rglob("*")):
            if p.is_file():
                out[str(p.relative_to(L))] = p.read_bytes()
    return out


def _mint(L: Path, **facts) -> T.TicketStore:
    write_ceiling(L)
    store = T.TicketStore(L / "sessions" / T.JOURNAL, L / "policy" / "dispatch_ceiling", ceiling_uid=ME)
    store.start()
    args = mint_args(**facts)
    store.mint(args.pop("attempt_id"), T.check_facts(args), lambda: "t-" + secrets.token_hex(16))
    return store


# --- the grammar ------------------------------------------------------------------------------------------------------

def test_replace_is_the_one_flag_routes_trust_takes():
    assert parse_install_argv(["routes-trust", "--replace"]).replace is True
    assert parse_install_argv(["routes-trust"]).replace is False
    for argv in (["routes-trust", "--force"], ["routes-trust", "--replace", "x"]):
        with pytest.raises(Refused) as e:
            parse_install_argv(argv)
        assert e.value.name == "extra-argv"


# --- the rotation -----------------------------------------------------------------------------------------------------

def test_without_replace_another_store_is_refused_and_both_heads_stay_on_the_old_store(old):
    """The failure Q16 exists for: the plain verb refuses, and a new store's generation 1 is refused at CONTROL."""
    with pytest.raises(Refused) as e:
        IH.run_routes_trust(ME, "darwin", read=io.BytesIO(_req(old.new, OTHER_STORE)).read, lock=_Lock(),
                            store_seams=_rs(old.L))
    assert e.value.name == "routes-trust-exists"
    one, sig = old.signed(item(1, store_id=OTHER_STORE), old.new)
    with pytest.raises(Refused):
        submit(RM.ControlRoutes(old.routes), one, sig)


def test_replace_sets_the_old_store_aside_and_both_heads_restart_at_the_new_one(old):
    before = _snapshot(old.L)
    dmode = os.lstat(old.dispatch).st_mode
    got = _replace(old, _req(old.new, OTHER_STORE))
    assert got["store_id"] == OTHER_STORE and got["fingerprint"] == old.new.fp
    assert got["replaced"] == {"store_id": STORE_ID, "fingerprint": old.signer.fp}
    t = RM.read_trust(old.routes)
    assert (t.public_key, t.fingerprint, t.store_id) == (_line(old.new), old.new.fp, OTHER_STORE)
    # the old store whole, aside: CONTROL's generations, its verifier generations, keys' head, the old pair
    aside = Path(got["aside"])
    assert aside.parent == old.routes and aside.name.startswith(".replaced-")
    assert stat.S_IMODE(os.lstat(aside).st_mode) == 0o700
    moved = {k.replace("routes/dispatch/", "dispatch/").replace("routes/verifiers/", "verifiers/")
             .replace("keys/routemap.head", "routemap.head").replace("routes/trust.", "trust."): v
             for k, v in before.items() if k != "routes/base.json"}
    assert {str(p.relative_to(aside)): p.read_bytes() for p in aside.rglob("*") if p.is_file()} == moved
    # the live dirs again, empty, as they were
    assert os.listdir(old.dispatch) == [] and os.listdir(old.routes / "verifiers") == []
    assert os.lstat(old.dispatch).st_mode == dmode
    assert not (old.L / "keys" / "routemap.head").exists()
    assert (old.routes / "base.json").read_bytes() == before["routes/base.json"]
    # both heads restart at the new store: CONTROL's start pass finds no head, a new-store generation 1 is accepted,
    # and keys loads nothing, then serves that generation on its RELOAD
    cr = RM.ControlRoutes(old.routes)
    cr.start_pass()
    assert cr.problem is None and cr.head() is None
    one, sig = old.signed(item(1, store_id=OTHER_STORE), old.new)
    assert submit(cr, one, sig) == 1
    kr = RM.KeysRoutes(str(old.L))
    assert kr.load() == (None, {}, None)
    _checked, head = kr.check_reload(1, sha(one))
    assert head["store_id"] == OTHER_STORE and head["served"] == [{"generation": 1, "item_sha256": sha(one)}]


def test_replace_with_the_pinned_pair_sets_nothing_aside(old):
    before = _snapshot(old.L)
    got = _replace(old, _req(old.signer, STORE_ID))
    assert got["replaced"] is None and got["aside"] is None
    assert _snapshot(old.L) == before
    assert not [p for p in old.routes.iterdir() if p.name.startswith(".replaced-")]


def test_replace_sets_aside_a_trust_json_that_does_not_parse(old):
    (old.routes / "trust.json").write_bytes(b"not json")
    got = _replace(old, _req(old.new, OTHER_STORE))
    assert got["replaced"] == {"store_id": None, "fingerprint": None}
    assert (Path(got["aside"]) / "trust.json").read_bytes() == b"not json"
    assert RM.read_trust(old.routes).store_id == OTHER_STORE


# --- refused, and nothing changed -------------------------------------------------------------------------------------

def _refused(old, name):
    before = _snapshot(old.L)
    with pytest.raises(Refused) as e:
        _replace(old, _req(old.new, OTHER_STORE))
    assert e.value.name == name, e.value
    assert _snapshot(old.L) == before
    assert not [p for p in old.routes.iterdir() if p.name.startswith(".replaced-")]
    return e.value


def test_refused_while_an_open_ticket_references_a_generation(old):
    _mint(old.L).close()
    e = _refused(old, "routes-trust-in-use")
    assert "generation 1" in e.detail


def test_refused_while_a_live_session_references_a_generation(old, monkeypatch):
    """A ticket no longer open (released) still bound to a session whose record is live: strict pins hold its
    generation for the session's life (R4 §5)."""
    store = _mint(old.L)
    t = next(iter(store._by_ticket.values()))
    store.bind(t.ticket, SESSION, ("egress", 0, 1))
    store._append({"op": "release", "ticket": t.ticket, "session": SESSION})
    store.close()
    (old.L / "sessions" / f"{SESSION}.json").write_bytes(b"{}")
    live = {SESSION: True}
    monkeypatch.setattr(IH, "_session_live", lambda data, session: live[session])
    _refused(old, "routes-trust-in-use")
    live[SESSION] = False                         # the session ended: nothing references generation 1 now
    assert _replace(old, _req(old.new, OTHER_STORE))["store_id"] == OTHER_STORE


def test_a_session_record_that_does_not_parse_refuses(old):
    (old.L / "sessions" / f"{SESSION}.json").write_bytes(b"not a record")
    _refused(old, "routes-trust-in-use")


def test_a_ticket_journal_that_does_not_parse_refuses(old):
    (old.L / "sessions" / T.JOURNAL).write_bytes(b"not json\n")
    _refused(old, "routes-trust-in-use")


def test_refused_while_control_holds_its_journal(old):
    fd = os.open(old.L / "sessions" / (T.JOURNAL + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _refused(old, "control-running")
    finally:
        os.close(fd)


def test_control_cannot_start_while_the_replace_holds_its_journal(old, monkeypatch):
    """The replace holds CONTROL's single-CONTROL lock from its reference check through the last write, so no CONTROL
    starts (and mints against the old store) between the check and the move."""
    seen = []
    real = IH._set_aside

    def spy(*a, **kw):
        store = T.TicketStore(old.L / "sessions" / T.JOURNAL, old.L / "policy" / "dispatch_ceiling")
        try:
            store.acquire()
            seen.append("acquired")
        except Refused as e:
            seen.append(e.name)
        finally:
            store.close()
        return real(*a, **kw)
    monkeypatch.setattr(IH, "_set_aside", spy)
    _replace(old, _req(old.new, OTHER_STORE))
    assert seen == ["control-running"]
    store = T.TicketStore(old.L / "sessions" / T.JOURNAL, old.L / "policy" / "dispatch_ceiling")
    store.acquire()                               # and after it, CONTROL takes the lock root created as its own
    store.close()


def test_a_dispatch_entry_that_is_not_a_directory_refuses_before_any_move(old):
    shutil.rmtree(old.routes / "verifiers")
    (old.routes / "verifiers").symlink_to(old.dispatch)
    _refused(old, "routes-trust-replace-layout")


def test_refused_for_a_caller_who_is_not_the_operator(old):
    (old.L / "policy" / "operator").write_text(operator_record_text("someone-else", ME + 1))
    _refused(old, "operator-mismatch")


# --- a failed write puts the old store back ---------------------------------------------------------------------------

@pytest.mark.parametrize("fail_at", ["trust.pub", "trust.json"])
def test_a_failed_write_puts_the_old_store_back(old, monkeypatch, fail_at):
    before = _snapshot(old.L)
    real = IH.R.write_durably

    def write(path, data, **kw):
        if Path(path).parent == old.routes and Path(path).name == fail_at:
            raise OSError(28, "No space left on device")
        return real(path, data, **kw)
    monkeypatch.setattr(IH.R, "write_durably", write)
    with pytest.raises(Refused) as e:
        _replace(old, _req(old.new, OTHER_STORE))
    assert e.value.name == "routes-trust-write-failed"
    assert _snapshot(old.L) == before
    assert not [p for p in old.routes.iterdir() if p.name.startswith(".replaced-")]
    cr = RM.ControlRoutes(old.routes)
    cr.start_pass()
    assert cr.problem is None and cr.active().store_id == STORE_ID


# --- through main -----------------------------------------------------------------------------------------------------

def test_routes_trust_replace_through_main(old):
    run = functools.partial(IH.run_routes_trust, store_seams=_rs(old.L))
    out, err = io.StringIO(), io.StringIO()
    seams = Seams(euid=lambda: 0, host=lambda: "darwin", lock=_Lock(),
                  stdin=io.BytesIO(_req(old.new, OTHER_STORE)).read, routes_trust=run)
    rc = main(["routes-trust", "--replace"], env={"SUDO_UID": str(ME)}, seams=seams, out=out, err=err)
    assert rc == 0, err.getvalue()
    got = json.loads(out.getvalue())
    assert got["verb"] == "routes-trust" and got["store_id"] == OTHER_STORE and got["replaced"]["store_id"] == STORE_ID


# --- L3 r1 Q1: a replace killed part way (the intent marker) ----------------------------------------------------------

INTENT = ".replace-intent"


def _killed(old, point: str, req: bytes | None = None) -> None:
    """The replace run in a child and SIGKILLed at ``point`` (``tests/_routes_replace_kill.py``)."""
    import subprocess
    import sys
    rq = old.L / "req.bin"
    rq.write_bytes(req if req is not None else _req(old.new, OTHER_STORE))
    root = str(Path(__file__).resolve().parent.parent)
    p = subprocess.run([sys.executable, "-m", "tests._routes_replace_kill", str(old.L), str(rq), point],
                       cwd=root, env=dict(os.environ, PYTHONPATH=root), capture_output=True, text=True, timeout=60)
    assert p.returncode == -9, (p.returncode, p.stdout, p.stderr)
    rq.unlink()


def _control_start(old):
    from levain.slot import control as C
    control = C.Control(C.StateStore(old.L / "sessions"), ME, dispatch_uid=ME + 1, routes=RM.ControlRoutes(old.routes))
    control.start(wake=False)
    return control


KILL_POINTS = ["mkdir:2", "rename:2", "mkdir:3", "rename:3", "written"]


@pytest.mark.parametrize("point", KILL_POINTS)
def test_a_killed_replace_leaves_its_marker_and_control_refuses_to_start(old, point):
    _killed(old, point)
    marker = old.routes / INTENT
    assert marker.exists()
    with pytest.raises(Refused) as e:
        _control_start(old)
    assert e.value.name == "routes-trust-replace-unfinished"
    from levain.slot.grammar import replace_intent_problem
    assert replace_intent_problem(old.routes) is not None          # keys' start reads the same test
    with pytest.raises(Refused) as e:
        _plain(old, _req(old.new, OTHER_STORE))                    # the plain verb never steps over it
    assert e.value.name == "routes-trust-replace-unfinished"


@pytest.mark.parametrize("point", KILL_POINTS[:-1])
def test_a_rerun_rolls_a_killed_replace_back(old, point):
    """A rerun asking for the OLD pair: the half-done replace is rolled back first, then it is the same pair, so
    nothing more moves; the store is byte for byte what it was before the killed run, and CONTROL starts on it."""
    before = _snapshot(old.L)
    _killed(old, point)
    got = _replace(old, _req(old.signer, STORE_ID))
    assert got["recovered"] == "rolled-back" and got["aside"] is None
    assert _snapshot(old.L) == before
    assert not [p for p in old.routes.iterdir() if p.name.startswith(".replaced-") or p.name == INTENT]
    _control_start(old)
    cr = RM.ControlRoutes(old.routes)
    cr.start_pass()
    assert cr.problem is None and cr.active().store_id == STORE_ID


@pytest.mark.parametrize("point", KILL_POINTS)
def test_a_rerun_of_the_same_replace_ends_as_one_clean_replace(old, point):
    before = _snapshot(old.L)
    _killed(old, point)
    got = _replace(old, _req(old.new, OTHER_STORE))
    assert got["recovered"] == ("completed" if point == "written" else "rolled-back")
    asides = [p for p in old.routes.iterdir() if p.name.startswith(".replaced-")]
    assert len(asides) == 1 and not (old.routes / INTENT).exists()
    moved = {k.replace("routes/dispatch/", "dispatch/").replace("routes/verifiers/", "verifiers/")
             .replace("keys/routemap.head", "routemap.head").replace("routes/trust.", "trust."): v
             for k, v in before.items() if k != "routes/base.json"}
    assert {str(p.relative_to(asides[0])): p.read_bytes() for p in asides[0].rglob("*") if p.is_file()} == moved
    assert RM.read_trust(old.routes).store_id == OTHER_STORE
    assert os.listdir(old.dispatch) == [] and os.listdir(old.routes / "verifiers") == []
    _control_start(old)


def test_a_marker_that_does_not_parse_is_left_for_the_operator(old):
    (old.routes / INTENT).write_bytes(b"not json")
    before = _snapshot(old.L)
    with pytest.raises(Refused) as e:
        _replace(old, _req(old.new, OTHER_STORE))
    assert e.value.name == "routes-trust-replace-unfinished" and INTENT in e.value.detail
    assert _snapshot(old.L) == before


def test_a_failed_put_back_keeps_the_aside_and_the_marker(old, monkeypatch):
    """complement MED 1: a rename back that fails never lets the aside be removed with the entry still in it."""
    real_write, real_rename = IH.R.write_durably, os.rename

    def write(path, data, **kw):
        if Path(path) == old.routes / "trust.json":
            raise OSError(28, "No space left on device")
        return real_write(path, data, **kw)

    def rename(src, dst, *a, **kw):
        if Path(src).name == "dispatch" and Path(src).parent.name.startswith(".replaced-"):
            raise OSError(5, "Input/output error")
        return real_rename(src, dst, *a, **kw)
    monkeypatch.setattr(IH.R, "write_durably", write)
    monkeypatch.setattr(IH.os, "rename", rename)
    with pytest.raises(Refused) as e:
        _replace(old, _req(old.new, OTHER_STORE))
    assert e.value.name == "routes-trust-write-failed" and "FAILED" in e.value.detail
    asides = [p for p in old.routes.iterdir() if p.name.startswith(".replaced-")]
    assert len(asides) == 1 and (asides[0] / "dispatch").is_dir() and str(asides[0]) in e.value.detail
    assert (old.routes / INTENT).exists()


# --- L3 r1 Q2: keys running ---------------------------------------------------------------------------------------------

def test_refused_while_keys_holds_its_ledger(old):
    path = old.L / "keys" / "ledger.jsonl"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _refused(old, "keys-running")
    finally:
        os.close(fd)


def test_keys_cannot_start_while_the_replace_holds_its_ledger(old, monkeypatch):
    from levain.broker import ledger
    path = old.L / "keys" / "ledger.jsonl"
    os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))
    seen = []
    real = IH._set_aside

    def spy(*a, **kw):
        try:
            os.close(ledger.Ledger.open(str(path))._fd)
            seen.append("opened")
        except ledger.LedgerOpenError as e:
            seen.append(e.name)
        return real(*a, **kw)
    monkeypatch.setattr(IH, "_set_aside", spy)
    _replace(old, _req(old.new, OTHER_STORE))
    assert seen == ["ledger-locked"]


# --- L3 r1 Q7 / Q8 / Q9 -------------------------------------------------------------------------------------------------

def test_no_sessions_dir_is_no_references(old):
    shutil.rmtree(old.L / "sessions")
    got = _replace(old, _req(old.new, OTHER_STORE))
    assert got["store_id"] == OTHER_STORE
    st = os.lstat(old.L / "sessions")
    assert stat.S_ISDIR(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o700 and st.st_uid == ME
    assert os.listdir(old.L / "sessions") == []        # no lock file of root's left for CONTROL to trip on


def test_any_exception_after_the_put_back_is_a_write_failed_refusal(old, monkeypatch):
    """deepseek MED 3: an exception of no expected class, after every entry moved, is undone and named."""
    before = _snapshot(old.L)
    real = IH._set_aside

    def boom(*a, **kw):
        real(*a, **kw)
        raise ValueError("an unexpected shape")
    monkeypatch.setattr(IH, "_set_aside", boom)
    with pytest.raises(Refused) as e:
        _replace(old, _req(old.new, OTHER_STORE))
    assert e.value.name == "routes-trust-write-failed" and "back in place" in e.value.detail
    assert _snapshot(old.L) == before


def test_a_record_that_does_not_parse_is_named(old):
    (old.L / "sessions" / f"{SESSION}.json").write_bytes(b"not a record")
    e = _refused(old, "routes-trust-in-use")
    assert f"{SESSION}.json" in e.detail


def test_a_record_in_a_state_the_helper_does_not_know_counts_as_live(old, monkeypatch):
    class Rec:
        session, state = SESSION, "SOMETHING-NEW"
    monkeypatch.setattr(IH.R, "parse_state", lambda data: Rec)
    assert IH._session_live(b"{}", SESSION) is True


def test_keys_refuses_to_start_on_a_killed_replace(old, capsys):
    from levain.broker import keys as K
    _killed(old, "rename:2")
    rc = K.main(["--runtime-root", str(old.L / "run"), "--lib-root", str(old.L), "--control", str(old.L / "c.sock"),
                 "--test-single-uid"])
    assert rc == 3 and "routes-trust-replace-unfinished" in capsys.readouterr().err
    assert not (old.L / "keys" / "ledger.jsonl").exists()          # refused before the ledger is opened
