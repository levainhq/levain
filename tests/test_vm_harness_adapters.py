"""3b-8a: the real VM adapters' ordering, against a fake guest that models the file rendezvous. No VM, no ssh.
The point under test: the session id is in the cleanup journal BEFORE the vendor can run."""
from __future__ import annotations

import json

import pytest

from scripts.vm_harness.driver import AdapterError, GuestError, SlotPlan, VmAdapters, run_on_vm

SID = "s-" + "a" * 16

PS = "  PID ELAPSED %CPU UCOMM UID\n"


class FakeGuest:
    """The guest's /private/tmp/fh files plus a fake dispatch child. The child writes ``prepared`` at once, and
    writes ``released``/``final`` only if the ack file exists when the host next looks (the real child blocks on it).
    ``events`` is the cross-checked ordering log."""

    def __init__(self, journal_path):
        self.files: dict[str, str] = {}
        self.events: list[str] = []
        self.journal_path = journal_path
        self.slot_state = "reusable"
        self.slotof = "e8"
        self.child_dies = False
        self.child_error = None
        self.revoked: list[str] = []
        self.tag = None
        self.final = '{"poll": 0, "session": "%s"}' % SID
        self.fh_left = "0"
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self.cancelled: list[str] = []
        self.prepared_slot = "e8"
        self.prepared_session = SID

    # guest(argv, timeout)
    def __call__(self, argv, timeout=60):
        if argv[:1] == ["/bin/ps"]:
            return PS
        self.calls.append(list(argv))
        self.timeouts.append(timeout)
        verb, arg = argv[-2], argv[-1]
        if verb == "fhclean":
            self.events.append("fhclean")
            return self.fh_left + "\n"
        if verb == "cancel":
            self.cancelled.append(arg)
            return ""
        if verb == "slotstate":
            return self.slot_state + "\n"
        if verb == "slotof":
            return self.slotof + "\n"
        if verb == "resolve":
            return "3978\n"
        if verb == "revoke":
            self.revoked.append(arg)
            return '{"generation": 1}\n'
        if verb == "put":
            self.events.append(f"put:{arg}")
            self.files[arg] = ""
            if arg.endswith(".journaled"):
                self.events.append("journal_at_ack:" + ("yes" if self._journaled() else "NO"))
                tag = arg.split(".")[0]
                self.files[f"{tag}.released.json"] = "{}"
                self.events.append("vendor_ran")
                self.files[f"{tag}.final.json"] = self.final
            return ""
        if verb == "get":
            if arg not in self.files:
                raise GuestError(3, "absent")
            return self.files[arg]
        raise AssertionError(argv)

    def _journaled(self):
        try:
            return SID in open(self.journal_path).read()
        except OSError:
            return False

    def spawn(self, argv):
        self.tag = argv[-1]
        self.events.append("spawn")
        if self.child_error:
            self.files[f"{self.tag}.error.json"] = json.dumps({"exc": self.child_error})
        else:
            self.files[f"{self.tag}.prepared.json"] = json.dumps({"session": self.prepared_session, "slot": self.prepared_slot})
            self.events.append("prepared")
        g = self

        class Child:
            def poll(self):
                return 1 if g.child_dies else None

            def terminate(self):
                g.events.append("terminated")

            def wait(self, t=None):
                g.events.append("waited")
        return Child()


def build(tmp_path, **kw):
    ev = tmp_path / "ev"
    ev.mkdir()
    g = FakeGuest(ev / "cleanup.jsonl")
    for k, v in kw.items():
        setattr(g, k, v)
    return ev, g, VmAdapters(g, g.spawn, sleep=lambda s: None)


def test_id_is_journaled_before_the_vendor_runs(tmp_path):
    ev, g, ad = build(tmp_path)
    res = run_on_vm(ev, [SlotPlan(8, "L")], ad, deadline_s=1.0)
    assert res.ok, res.problems
    assert "journal_at_ack:yes" in g.events and "journal_at_ack:NO" not in g.events
    assert g.events.index("journal_at_ack:yes") < g.events.index("vendor_ran")
    assert g.revoked == [SID]
    assert "terminated" in g.events      # close() reaches the guest child
    assert res.result_path.read_text().rstrip().endswith("RESULT: PASS")


def test_slot_mismatch_blocks_the_vendor_and_still_revokes(tmp_path):
    ev, g, ad = build(tmp_path, slotof="e9")
    res = run_on_vm(ev, [SlotPlan(8, "L")], ad, deadline_s=1.0)
    assert not res.ok
    assert "vendor_ran" not in g.events and "put:" + g.tag + ".journaled" not in g.events
    assert g.revoked == [SID]       # journaled before render raised
    assert any("prepare said" in p for p in res.problems)


def test_alloc_refuses_a_slot_that_is_not_reusable(tmp_path):
    ev, g, ad = build(tmp_path, slot_state="leased")
    res = run_on_vm(ev, [SlotPlan(8, "L")], ad, deadline_s=1.0)
    assert not res.ok and "spawn" not in g.events
    assert any("not reusable" in p for p in res.problems)


def test_guest_error_before_prepare_fails_the_run(tmp_path):
    ev, g, ad = build(tmp_path, child_error="levain.slot.Refused: below-floor")
    res = run_on_vm(ev, [SlotPlan(8, "L")], ad, deadline_s=1.0)
    assert not res.ok and any("below-floor" in p for p in res.problems)


def test_child_that_dies_silently_fails_not_hangs(tmp_path):
    ev, g, ad = build(tmp_path, child_dies=True)
    g.spawn = lambda argv, _s=g.spawn: (lambda c: (g.files.pop(f"{argv[-1]}.prepared.json", None), c)[1])(_s(argv))
    ad._spawn = g.spawn
    res = run_on_vm(ev, [SlotPlan(8, "L")], ad, deadline_s=1.0)
    assert not res.ok and any("exited" in p for p in res.problems)


def test_prepare_timeout_is_a_failure(tmp_path):
    ev, g, ad = build(tmp_path)
    t = [0.0]
    ad._clock = lambda: t[0]
    ad._sleep = lambda s: t.__setitem__(0, t[0] + 30)
    ad._spawn = lambda argv: type("C", (), {"poll": lambda s: None, "terminate": lambda s: None})()
    res = run_on_vm(ev, [SlotPlan(8, "L")], ad, deadline_s=1.0)
    assert not res.ok and any("timed out" in p for p in res.problems)


def test_a_non_session_prepared_record_is_refused(tmp_path):
    ev, g, ad = build(tmp_path)
    orig = g.spawn

    def spawn(argv):
        c = orig(argv)
        g.files[f"{g.tag}.prepared.json"] = json.dumps({"session": ""})
        return c
    ad._spawn = spawn
    res = run_on_vm(ev, [SlotPlan(8, "L")], ad, deadline_s=1.0)
    assert not res.ok and any("no session id" in p for p in res.problems)


def test_vm_run_refuses_a_vm_not_named_vend(tmp_path):
    from scripts.vm_harness import vm_run
    with pytest.raises(SystemExit):
        vm_run.main(["vdis-mac", str(tmp_path / "ev")])


# ---- L3 r1 fix round -----------------------------------------------------------------------------------------

def run(tmp_path, **kw):
    ev, g, ad = build(tmp_path, **kw)
    return g, ad, run_on_vm(ev, [SlotPlan(8, "L")], ad, deadline_s=1.0)


@pytest.mark.parametrize("final", ['{"poll": 2, "session": "%s"}' % SID, '{"poll": null, "session": "%s"}' % SID,
                                   '{"poll": 0, "session": "s-bbbbbbbbbbbbbbbb"}'])
def test_a_vendor_that_did_not_exit_cleanly_is_not_a_pass(tmp_path, final):
    # revert: drop the `final.get("poll") != 0 ...` check in VmAdapters.render and these go green-PASS
    g, ad, res = run(tmp_path, final=final)
    assert not res.ok and any("did not finish cleanly" in p for p in res.problems)
    assert g.revoked == [SID]


def test_tags_are_unique_per_run_and_the_exchange_dir_is_emptied_first(tmp_path):
    # revert: VmAdapters.__init__ `nonce` default back to a counter (tag drv8x1) -> the two tags below collide
    tags = set()
    for i in (1, 2):
        sub = tmp_path / str(i)
        sub.mkdir()
        g, ad, res = run(sub)
        assert res.ok, res.problems
        tags.add(g.tag)
        assert g.events.index("fhclean") < g.events.index("spawn")
    assert len(tags) == 2 and all(t.startswith("drv8x") and t != "drv8x1" for t in tags)


def test_stale_rendezvous_entries_that_survive_the_clean_refuse_the_run(tmp_path):
    # revert: the `left != "0"` check in VmAdapters.alloc
    g, ad, res = run(tmp_path, fh_left="3")
    assert not res.ok and "spawn" not in g.events and any("old rendezvous" in p for p in res.problems)


def test_a_malformed_session_id_never_reaches_a_journal_or_a_guest_argv(tmp_path):
    # revert: the SESSION_RE.fullmatch check in VmAdapters.mint
    evil = "s-0123456789abcdef; touch /pwned #"
    g, ad, res = run(tmp_path, prepared_session=evil)
    assert not res.ok and any("malformed session id" in p for p in res.problems)
    assert not any(evil in " ".join(c) for c in g.calls)
    assert evil not in (tmp_path / "ev" / "cleanup.jsonl").read_text()


def test_ops_refuses_an_argument_a_remote_shell_would_parse(tmp_path):
    ev, g, ad = build(tmp_path)
    for bad in ("s-1; id", "a b", "$(id)", "../x", ""):
        with pytest.raises(AdapterError):
            ad.ops("revoke", bad)
    assert not g.calls


def test_ssh_sends_one_quoted_command(monkeypatch, tmp_path):
    # revert: vm_run.make_ssh `remote()` back to passing *argv through
    import subprocess
    from scripts.vm_harness import vm_run
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        return type("R", (), {"stdout": "10.0.0.5\n", "stderr": "", "returncode": 0})()
    monkeypatch.setattr(subprocess, "run", fake_run)
    guest, _ = vm_run.make_ssh("vend-x", tmp_path)
    guest(["/bin/echo", "a; touch /pwned"])
    last = seen[-1]
    assert last[-1] == "/bin/echo 'a; touch /pwned'" and last[-2] == "admin@10.0.0.5"


def test_a_non_absent_guest_failure_fails_fast_not_at_the_deadline(tmp_path):
    ev, g, ad = build(tmp_path)
    orig = g.__call__

    def broken(argv, timeout=60):
        if argv[-2] == "get":
            raise GuestError(255, "ssh down")
        return orig(argv, timeout)
    ad._guest = broken
    res = run_on_vm(ev, [SlotPlan(8, "L")], ad, deadline_s=1.0)
    assert not res.ok and any("guest get" in p and "255" in p for p in res.problems)


def test_every_wait_probe_is_bounded_by_the_remaining_deadline(tmp_path):
    ev, g, ad = build(tmp_path)
    t = [0.0]
    ad._clock = lambda: t[0]
    ad._sleep = lambda s: t.__setitem__(0, t[0] + 30)
    ad._spawn = lambda argv: type("C", (), {"poll": lambda s: None, "terminate": lambda s: None})()
    ad.prepare_s = 5.0
    run_on_vm(ev, [SlotPlan(8, "L")], ad, deadline_s=1.0)
    gets = [x for c, x in zip(g.calls, g.timeouts) if c[-2] == "get"]
    assert gets and max(gets) <= 5.0


def test_the_actual_slot_prepare_reports_is_carried_and_checked(tmp_path):
    # prepare put the session in e0 while the plan said e8: slotof agrees with prepare, the plan check refuses
    g, ad, res = run(tmp_path, prepared_slot="e0", slotof="e0")
    assert not res.ok and any("landed in slot 'e0'" in p for p in res.problems) and "vendor_ran" not in g.events
    # prepare and slotof disagree: refuse on that too
    sub = tmp_path / "x"
    sub.mkdir()
    g, ad, res = run(sub, prepared_slot="e0", slotof="e8")
    assert not res.ok and any("prepare said 'e0'" in p for p in res.problems)


def test_slot_check_also_checks_the_slot_prepare_used(tmp_path):
    ev, g, ad = build(tmp_path)
    ad._planned[SID], ad._actual[SID] = 8, "e0"
    states = {"8": "reusable", "0": "leased"}
    g2 = g.__call__
    ad._guest = lambda argv, timeout=60: (states[argv[-1]] + "\n") if argv[-2] == "slotstate" else g2(argv, timeout)
    assert ad.slot_check(8) is False
    states["0"] = "reusable"
    assert ad.slot_check(8) is True


def test_close_cancels_every_remote_dispatcher_and_kills_a_child_that_ignores_terminate(tmp_path):
    # revert: the `ops("cancel", tag)` loop or the terminate -> wait -> kill ladder in VmAdapters.close
    ev, g, ad = build(tmp_path, child_error="boom")      # mint fails: the session is unknown, the tag is not
    log = []

    class Stubborn:
        def poll(self):
            return None

        def terminate(self):
            log.append("terminate")

        def wait(self, t=None):
            log.append("wait")
            if "kill" not in log:
                raise TimeoutError

        def kill(self):
            log.append("kill")
    ad._spawn = lambda argv: Stubborn()
    ad._wait = lambda *a, **k: (_ for _ in ()).throw(AdapterError("boom"))
    with pytest.raises(AdapterError):
        ad.mint(8, "L")
    ad.close()
    assert len(g.cancelled) == 1 and g.cancelled[0].startswith("drv8x")
    assert log == ["terminate", "wait", "kill", "wait"]


# ---- the guest scripts (run here against a temp exchange dir) --------------------------------------------------

@pytest.fixture
def fh(tmp_path, monkeypatch):
    from scripts.vm_harness import guest_ops
    d = tmp_path / "fh"
    d.mkdir()
    monkeypatch.setattr(guest_ops, "FH", str(d))
    return guest_ops, d


def test_put_refuses_a_planted_symlink_and_never_touches_its_target(fh, tmp_path, capsys):
    # revert: drop O_EXCL|O_NOFOLLOW from guest_ops `put`
    ops, d = fh
    target = tmp_path / "root_owned"
    target.write_text("keep")
    target.chmod(0o600)
    (d / "drv8xaa.journaled").symlink_to(target)
    assert ops.main(["x", "put", "drv8xaa.journaled"]) == 6
    assert target.read_text() == "keep" and (target.stat().st_mode & 0o777) == 0o600


def test_put_creates_a_plain_file_once(fh):
    ops, d = fh
    assert ops.main(["x", "put", "drv8xbb.journaled"]) == 0
    assert (d / "drv8xbb.journaled").stat().st_mode & 0o777 == 0o644
    assert ops.main(["x", "put", "drv8xbb.journaled"]) == 6


def test_get_refuses_a_symlink_and_a_fifo_and_reports_absent(fh, tmp_path, capsys):
    # revert: drop O_NOFOLLOW / the S_ISREG check from guest_ops `_read_regular`
    import os
    ops, d = fh
    secret = tmp_path / "secret"
    secret.write_text("TOPSECRET")
    (d / "link.json").symlink_to(secret)
    assert ops.main(["x", "get", "link.json"]) == 5
    os.mkfifo(d / "fifo.json")
    assert ops.main(["x", "get", "fifo.json"]) == 5
    assert "TOPSECRET" not in capsys.readouterr().out
    assert ops.main(["x", "get", "nope.json"]) == 3
    (d / "ok.json").write_text('{"a": 1}')
    assert ops.main(["x", "get", "ok.json"]) == 0 and '{"a": 1}' in capsys.readouterr().out


def test_fhclean_removes_only_rendezvous_entries(fh):
    ops, d = fh
    (d / "drv8x0123abcd.journaled").write_text("")
    (d / ".drv8x0123abcd.final.tmp").write_text("")
    (d / "brief.txt").write_text("keep")
    (d / "repo").mkdir()
    assert ops.main(["x", "fhclean", "-"]) == 0
    assert sorted(p.name for p in d.iterdir()) == ["brief.txt", "repo"]


@pytest.fixture
def slots(tmp_path, monkeypatch):
    from scripts.vm_harness import guest_ops
    root = tmp_path / "slots"
    monkeypatch.setattr(guest_ops, "SLOTS", str(root))

    def put(index, state, session=None, as_dir=None):
        from levain.slot.ids import SlotId
        from levain.slot.records import SlotState, slot_state_bytes
        d = root / (as_dir or f"e{index}")
        d.mkdir(parents=True, exist_ok=True)
        (d / "state").write_bytes(slot_state_bytes(SlotState(SlotId("egress", index), 1, 1, state, True,
                                                              "2026-11-07T12:00:00Z", session)))
    return guest_ops, root, put


def test_slotof_is_the_strict_leased_slot_not_a_substring_hit(slots, capsys):
    # revert: guest_ops `slotof` back to `arg in open(p).read()` -> the e0 reusable record would not matter, but the
    # prefix id below would resolve to e8
    ops, root, put = slots
    put(0, "reusable")
    put(8, "leased", SID)
    assert ops.main(["x", "slotof", SID]) == 0 and capsys.readouterr().out.strip() == "e8"
    assert ops.main(["x", "slotof", "s-aaaaaaaaaaaaaaa"]) == 0 and capsys.readouterr().out.strip() == "none"


def test_a_malformed_slot_record_fails_closed(slots, capsys):
    ops, root, put = slots
    put(8, "leased", SID)
    bad = root / "e9"
    bad.mkdir()
    (bad / "state").write_text(json.dumps({"state": "reusable", "session": SID}))   # not the product's grammar
    assert ops.main(["x", "slotof", SID]) == 4
    assert ops.main(["x", "slotstate", "8"]) == 4


def test_a_record_in_the_wrong_directory_fails_closed(slots):
    ops, root, put = slots
    put(3, "reusable", as_dir="e8")
    assert ops.main(["x", "slotstate", "8"]) == 4


def test_slotstate_reads_the_strict_state(slots, capsys):
    ops, root, put = slots
    put(8, "reusable")
    assert ops.main(["x", "slotstate", "8"]) == 0 and capsys.readouterr().out.strip() == "reusable"


# ---- guest_dispatch ---------------------------------------------------------------------------------------------

@pytest.fixture
def gd(tmp_path, monkeypatch):
    import scripts.vm_harness.guest_dispatch as m
    d = tmp_path / "gdfh"
    d.mkdir()
    monkeypatch.setattr(m, "FH", d)
    monkeypatch.setattr(m, "tag", "drv8x0123abcd")
    monkeypatch.setattr(m, "ACK_S", 0.2)
    return m, d


def test_the_session_is_published_when_prepare_returns_before_any_staging(gd, monkeypatch):
    # revert: drop the `if verb == "prepare": say("prepared", ...)` in RendezvousRuntime._run
    m, d = gd
    monkeypatch.setattr(m.HelperSlotRuntime, "_run", lambda self, verb, *a, **k: {"verb": verb})
    monkeypatch.setattr(m, "prepare_answer", lambda out, host, store: (SID, 8))
    rt = object.__new__(m.RendezvousRuntime)
    rt.host, rt._store = "darwin", None
    assert rt._run("prepare", "egress") == {"verb": "prepare"}
    rec = json.loads((d / "drv8x0123abcd.prepared.json").read_text())
    assert rec["session"] == SID and rec["slot"] == "e8"
    # the vendor is gated by the ack: no ack, render refuses without calling the product's render
    with pytest.raises(RuntimeError, match="no journal ack"):
        rt._run("render", SID)
    (d / "drv8x0123abcd.journaled").write_text("")
    assert rt._run("render", SID) == {"verb": "render"}


def test_a_stale_ack_or_final_of_the_same_tag_refuses_the_dispatch(gd):
    # revert: the `stale` check in guest_dispatch.claim
    m, d = gd
    (d / "drv8x0123abcd.journaled").write_text("")
    with pytest.raises(RuntimeError, match="already exist"):
        m.claim()


def test_a_fresh_tag_is_claimed_once(gd):
    m, d = gd
    m.claim()
    assert (d / "drv8x0123abcd.pid").read_text().strip().isdigit()
    with pytest.raises(RuntimeError):
        m.claim()


def test_the_render_blob_lists_no_egress_route_like_the_product(gd, monkeypatch):
    # revert: drop egress=False from guest_dispatch RendezvousRuntime._render_blob
    m, d = gd
    seen = {}
    monkeypatch.setattr(m, "read_base_map", lambda p: [])
    monkeypatch.setattr(m, "pick_routes", lambda base, vendor, auth, **kw: seen.update(kw) or ())
    rt = object.__new__(m.RendezvousRuntime)
    rt.host = "darwin"
    m.RendezvousRuntime._render_blob(rt, type("Req", (), {"harness": "claude", "model": "sonnet"})())
    assert seen == {"egress": False}
