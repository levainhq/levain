"""Row 33 R1b undo half, FIX1 (L3 r1 on seat/1022-18-r1b; HEAD 1022-19 rulings H1, M1-M3, L1, L2).

* H1: a recorded FILE kind is ours only when its bytes are recognisably Levain's. A foreign root-owned regular file at a
  recorded path (sudoers drop-in, systemd unit, pf rules file, pf token, the bwrap copy and its digest record) reads
  NOT_OURS: left in place and logged to ``Live.skips``, never deleted.
* M1: undo checks the sudoers config BEFORE it removes anything, and the install rule's removal can never strand undo
  (a check that fails while the rule still stands stops undo with the rule in place; the check after its removal is
  the plan's last step).
* M2: undo journals what it will remove before its first removal, and a re-run after a crash plans from that journal
  (the slot confinement a pre-row-33 install derived from live accounts survives their tombstones).
* M3: a FINISHED repair journal's records are not undo's to read (check_records' rule), an unknown kind there included.
* L1: undo retires only the uids of the accounts it removes (ours: live at the recorded uid).
* L2: ``desired_managed`` drops a group with no gid only when undo asks; every other caller still raises.
"""
from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from levain.slot import Refused, bwrap, pf, units
from levain.slot import generation as G
from levain.slot import install_helper as IH
from levain.slot.accounts import SLOT_MARKER
from levain.slot.grammar import parse_install_argv
from levain.slot.ids import RANGES, RETIRED_IDS_PATH, SlotId, loopback_alias
from levain.slot.intent import INTENT_PATH, Intent, intent_text, parse_intent
from levain.slot.sudoers import INSTALL_SUDOERS, RUNTIME_SUDOERS, runtime_sudoers_text

from tests.test_repair_remove import SUDOERS_DIR, _host, _put
from tests.test_undo_remove import OP, S0, _darwin_host, _facts, _shape

FOREIGN = b"# the admin's own rule\nadmin ALL=(ALL) ALL\n"


def _run(steps):
    return [s.call() for s in steps]


def _alone(tmp: Path, host: str, kind: str, ident, setup) -> tuple:
    h, live = _host(tmp, host)
    setup(h, live)
    steps = [s for ph in IH.REMOVE_PHASES[kind]
             for s in IH.removal_steps(host, [(kind, ident)], ph, live=live, held=frozenset(),
                                       end=lambda uid, name: (True, ""))]
    return h, live, _run(steps)


# --- H1: the class, per file kind ------------------------------------------------------------------------------------

def _linux_tools(monkeypatch) -> None:
    from levain.firing import hands

    real = hands.shutil.which
    monkeypatch.setattr(hands.shutil, "which", lambda n, path=None: real(n, path=path) or f"/usr/sbin/{n}")


@pytest.mark.parametrize("host,kind,ident,path", [
    ("darwin", "sudoers/1", "levain-old", SUDOERS_DIR / "levain-old"),
    ("linux", "unit/1", "old-svc", units.SYSTEMD_DIR / "levain-old-svc.service"),
    ("darwin", "pf-anchor/1", (pf.ANCHOR, str(pf.ANCHOR_FILE)), pf.ANCHOR_FILE),
    # the retired-ids file sits inside pf-enable/1's and bwrap-copy/1's bounds: a record naming it must not delete it
    ("darwin", "pf-enable/1", str(RETIRED_IDS_PATH["darwin"]), RETIRED_IDS_PATH["darwin"]),
    ("linux", "bwrap-copy/1", (str(bwrap.LEVAIN_BWRAP), str(RETIRED_IDS_PATH["linux"])), RETIRED_IDS_PATH["linux"]),
    ("linux", "bwrap-copy/1", (str(bwrap.LEVAIN_BWRAP), str(bwrap.BWRAP_DIGEST)), bwrap.LEVAIN_BWRAP),
])
def test_a_foreign_root_file_at_a_recorded_path_is_left(tmp_path: Path, monkeypatch, host, kind, ident, path) -> None:
    if host == "linux":
        _linux_tools(monkeypatch)
    data = b"3999\n4000\n" if path in RETIRED_IDS_PATH.values() else FOREIGN

    def setup(h, live):
        _put(live, path, data)
        if kind == "bwrap-copy/1" and path == bwrap.LEVAIN_BWRAP:     # a digest record that does not name this copy
            _put(live, bwrap.BWRAP_DIGEST, f"{'0' * 64}  {bwrap.LEVAIN_BWRAP}\n".encode())
    h, live, results = _alone(tmp_path, host, kind, ident, setup)
    assert all(ok for ok, _ in results), results
    assert live.at(path).read_bytes() == data, (kind, path)
    assert live.skips == {(kind, ident)}
    rms = [a for a in h.acts() if a[0].endswith("/rm")]
    assert not rms, rms


def test_the_sudoers_kind_state_reads_the_content(tmp_path: Path) -> None:
    h, live = _host(tmp_path, "darwin")
    _put(live, SUDOERS_DIR / "levain-old", FOREIGN)
    assert IH.kind_state("darwin", "sudoers/1", "levain-old", live) == "not-ours"
    _put(live, SUDOERS_DIR / "levain-old", runtime_sudoers_text("darwin", OP).encode())
    assert IH.kind_state("darwin", "sudoers/1", "levain-old", live) == "ours"


def test_levain_written_files_still_read_ours(tmp_path: Path, monkeypatch) -> None:
    """Control: the bytes each writer produces pass its kind's check."""
    _linux_tools(monkeypatch)
    svc = next(s for s in units.SERVICES if "linux" in s.hosts)
    copy = b"\x7fELF the slots' bwrap"
    sha = hashlib.sha256(copy).hexdigest()
    cases = [
        ("darwin", "sudoers/1", "levain-runtime", {RUNTIME_SUDOERS: runtime_sudoers_text("darwin", OP).encode()}),
        ("linux", "unit/1", svc.name, {units.SYSTEMD_DIR / f"levain-{svc.name}.service":
                                       units.systemd_unit_text(svc).encode()}),
        ("darwin", "pf-anchor/1", (pf.ANCHOR, str(pf.ANCHOR_FILE)),
         {pf.ANCHOR_FILE: pf.anchor_text((), {}).encode()}),
        ("darwin", "pf-enable/1", str(pf.PF_TOKEN), {pf.PF_TOKEN: b"12345\n"}),
        ("linux", "bwrap-copy/1", (str(bwrap.LEVAIN_BWRAP), str(bwrap.BWRAP_DIGEST)),
         {bwrap.LEVAIN_BWRAP: copy, bwrap.BWRAP_DIGEST: bwrap.digest_record_text("/usr/bin/bwrap", sha, sha).encode()}),
    ]
    for i, (host, kind, ident, files) in enumerate(cases):
        def setup(h, live, files=files):
            for p, b in files.items():
                _put(live, p, b)
        h, live, results = _alone(tmp_path / str(i), host, kind, ident, setup)
        assert all(ok for ok, _ in results) and not live.skips, (kind, results, live.skips)
        assert not any(live.at(p).exists() for p in files), kind


def test_undo_leaves_a_foreign_file_at_a_recorded_drop_in(tmp_path: Path) -> None:
    """codex HIGH, through plan_undo: the record names ``sudoers/1 levain-old``; the file there is now the admin's."""
    h, live, shared = _darwin_host(tmp_path)
    _put(live, SUDOERS_DIR / "levain-old", FOREIGN)
    plan = IH.plan_undo(parse_install_argv(["undo"]), _facts("darwin", _shape(("sudoers/1", "levain-old"))),
                        accounts={"_levain_s0": S0}, shared_groups=shared, live=live, end=lambda u, n: (True, ""))
    for s in plan.steps:
        if s.call is not None and s.write is None:
            assert s.call()[0], s.why
    assert live.at(SUDOERS_DIR / "levain-old").read_bytes() == FOREIGN
    assert ("sudoers/1", "levain-old") in live.skips


# --- M1: the sudoers check before any removal; the install rule never strands undo -----------------------------------

def _walk(plan, h, before=None):
    """Run ``plan``'s call steps in order until one fails; ``before(i, step)`` runs ahead of each. Returns the index of
    the failed step (or None) and the log index where each step began."""
    for i, s in enumerate(plan.steps):
        if before:
            before(i, s)
        if s.call is not None and s.write is None:
            ok, why = s.call()
            if not ok:
                return i, why
    return None, ""


def _plan(tmp: Path, record=()):
    h, live, shared = _darwin_host(tmp)
    plan = IH.plan_undo(parse_install_argv(["undo"]), _facts("darwin", _shape(*record)),
                        accounts={"_levain_s0": S0}, shared_groups=shared, live=live, end=lambda u, n: (True, ""))
    return h, live, plan


def test_a_broken_sudoers_refuses_undo_before_anything_is_removed(tmp_path: Path) -> None:
    h, live, plan = _plan(tmp_path)
    h.fail.add("visudo")
    i, why = _walk(plan, h)
    assert i is not None and "sudoers" in why and "nothing" in why, (i, why)
    assert not [a for a in h.acts() if a[0].endswith("/rm") or "bootout" in a], h.acts()
    assert live.at(RUNTIME_SUDOERS).exists() and live.at(INSTALL_SUDOERS).exists()


def test_a_check_failing_at_the_install_rule_leaves_the_rule(tmp_path: Path) -> None:
    """The config breaks mid-undo (a foreign drop-in appears): undo stops with the install rule still in place, so the
    operator can re-run it through the helper."""
    h, live, plan = _plan(tmp_path)
    at = next(i for i, s in enumerate(plan.steps) if s.why == "remove the install record")

    def before(i, s):
        if i == at:
            h.fail.add("visudo")
    i, why = _walk(plan, h, before)
    assert i is not None and i > at, (i, why)
    assert live.at(INSTALL_SUDOERS).exists()


def test_the_check_after_the_install_rule_is_the_last_step(tmp_path: Path) -> None:
    h, live, plan = _plan(tmp_path)
    whys = [s.why for s in plan.steps]
    rm_install = next(i for i, w in enumerate(whys) if w.startswith(f"remove {INSTALL_SUDOERS} "))
    assert "visudo -c" in whys[-1] and whys[-1] != whys[rm_install]
    assert whys[rm_install + 1:-1] == [IH.intent_remove_step("darwin").why, "remove the operator record (last)"]
    assert "visudo -c" in whys[rm_install - 1]                    # checked while the rule still stands
    assert "visudo -c" in whys[0]                                 # and before anything at all


# --- M2: undo journals its set before its first removal; a re-run plans from the journal ----------------------------

def test_a_re_run_after_the_tombstones_still_removes_the_derived_confinement(tmp_path: Path) -> None:
    h, live, shared = _darwin_host(tmp_path)
    alias = loopback_alias(SlotId("egress", 0))
    facts = _facts("darwin", None)                                # a pre-row-33 install: no install record
    plan = IH.plan_undo(parse_install_argv(["undo"]), facts, accounts={"_levain_s0": S0}, shared_groups=shared,
                        live=live, end=lambda u, n: (True, ""))
    assert plan.intent is not None and plan.intent.verb == "undo"
    assert ("pf-alias/1", alias) in plan.intent.managed and ("account/1", ("_levain_s0", S0)) in plan.intent.managed
    whys = [s.why for s in plan.steps]
    journal = next(i for i, w in enumerate(whys) if w.startswith("journal what this undo"))
    first_rm = next(i for i, s in enumerate(plan.steps) if s.why.startswith("remove ") or s.why.startswith("stop "))
    assert journal < first_rm
    # run to the tombstones, then die
    tomb_done = max(i for i, w in enumerate(whys) if "_levain_s0" in w)
    for s in plan.steps[:tomb_done + 1]:
        if s.call is not None and s.write is None:
            assert s.call()[0], s.why
    written = live.at(INTENT_PATH["darwin"]).read_bytes()
    assert parse_intent(written, "darwin") == plan.intent
    assert alias in h.aliases and h.users["_levain_s0"][1] != SLOT_MARKER
    # the re-run: the directory service no longer reports the slot account; the journal does
    facts2 = replace(facts, intent=parse_intent(written, "darwin"), host_ids={"_levain_s0": S0})
    plan2 = IH.plan_undo(parse_install_argv(["undo"]), facts2, accounts={}, shared_groups=shared, live=live,
                         end=lambda u, n: (True, ""))
    assert plan2.intent.txn == plan.intent.txn and set(plan.intent.managed) <= set(plan2.intent.managed)
    for s in plan2.steps:
        if s.call is not None and s.write is None:
            assert s.call()[0], s.why
    assert alias not in h.aliases


# --- M3: a finished repair journal is the record's, never undo's to read ----------------------------------------------

def _finished_journal_with(kind: str) -> Intent:
    txn = "c" * 32                                               # == _shape()'s completed_txn
    text = intent_text(Intent("repair", OP, {}, {}, txn, None, ()))
    obj = G.canon.parse(text.encode())
    obj["managed"] = [[kind, "x"]]
    return parse_intent(G.canon.canonical(obj).encode(), "darwin")


def test_a_finished_journal_with_an_unknown_kind_does_not_stop_undo(tmp_path: Path) -> None:
    h, live, shared = _darwin_host(tmp_path)
    j = _finished_journal_with("future-thing/1")
    facts = replace(_facts("darwin", _shape()), intent=j)
    IH.check_records(facts, OP)                                  # check_records omits a finished journal
    plan = IH.plan_undo(parse_install_argv(["undo"]), facts, accounts={"_levain_s0": S0}, shared_groups=shared,
                        live=live, end=lambda u, n: (True, ""))
    assert not [r for r in plan.intent.managed if r[0] == "future-thing/1"]
    # control: the same journal UNFINISHED refuses, naming the kind
    unfinished = replace(j, txn="d" * 32)
    with pytest.raises(Refused) as e:
        IH.plan_undo(parse_install_argv(["undo"]), replace(facts, intent=unfinished), accounts={"_levain_s0": S0},
                     shared_groups=shared, live=live, end=lambda u, n: (True, ""))
    assert e.value.name == "downgrade-unsupported:future-thing/1"


# --- L1: only the removed accounts' uids are retired -------------------------------------------------------------------

def test_undo_retires_no_uid_of_an_account_it_leaves(tmp_path: Path) -> None:
    h, live, shared = _darwin_host(tmp_path)
    s5 = S0 - 5                                                  # in the slot range (account/1's bound)
    assert s5 in RANGES["darwin"].slots
    h.users["_levain_s5"] = [s5 - 100, "someone else's"]         # the recorded name, live at another uid: NOT_OURS
    plan = IH.plan_undo(parse_install_argv(["undo"]),
                        _facts("darwin", _shape(("account/1", ("_levain_s5", s5)))),
                        accounts={"_levain_s0": S0}, shared_groups=shared, live=live, end=lambda u, n: (True, ""))
    assert S0 in plan.retired_after and s5 not in plan.retired_after


# --- L2: the missing-group filter is undo's alone ------------------------------------------------------------------------

def test_desired_managed_raises_on_a_missing_group_outside_undo() -> None:
    accts = {"_levain_s0": S0}
    with pytest.raises(KeyError):
        IH.desired_managed("darwin", accounts=accts, shared={}, operator=OP, services=(), slots=IH.slots_of(accts))
    got = IH.desired_managed("darwin", accounts=accts, shared={}, operator=OP, services=(), slots=IH.slots_of(accts),
                             partial_groups=True)
    assert not [r for r in got if r[0] == "member/1"]


def test_no_other_verb_plans_over_an_interrupted_undo(tmp_path: Path) -> None:
    """M2's journal is undo's: repair and replenish refuse ``install-interrupted:undo`` (re-run undo), never resume it
    as their own; the journal round-trips and carries no generation."""
    from tests.test_repair_plan import TXN_B, _plan as repair_plan, _pre_change

    j = Intent("undo", OP, {"_levain_s0": S0}, {}, TXN_B, None, (("account/1", ("_levain_s0", S0)),))
    assert parse_intent(intent_text(j).encode(), "darwin") == j
    obj = G.canon.parse(intent_text(j).encode())
    obj["generation"] = "gen-" + "0" * 16
    with pytest.raises(Refused):
        parse_intent(G.canon.canonical(obj).encode(), "darwin")
    facts, rf = _pre_change(tmp_path, intent=j)
    with pytest.raises(Refused) as e:
        repair_plan(facts, rf)
    assert (e.value.name, e.value.detail) == ("install-interrupted", "undo")
    with pytest.raises(Refused) as e:
        IH.plan_replenish(parse_install_argv(["replenish"]), facts, slots=1, verifier_slots=1,
                          live={"egress": [0], "none": [0]}, accounts={"_levain_s0": S0})
    assert (e.value.name, e.value.detail) == ("install-interrupted", "undo")
