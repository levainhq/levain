"""3b-8a: the real VM adapters' ordering, against a fake guest that models the file rendezvous. No VM, no ssh.
The point under test: the session id is in the cleanup journal BEFORE the vendor can run."""
from __future__ import annotations

import json

import pytest

from scripts.vm_harness.driver import AdapterError, SlotPlan, VmAdapters, run_on_vm

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

    # guest(argv, timeout)
    def __call__(self, argv, timeout=60):
        if argv[:1] == ["/bin/ps"]:
            return PS
        verb, arg = argv[-2], argv[-1]
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
                self.files[f"{tag}.final.json"] = '{"poll": 0}'
            return ""
        if verb == "get":
            if arg not in self.files:
                raise RuntimeError("rc=3")
            return self.files[arg]
        raise AssertionError(argv)

    def _journaled(self):
        try:
            return "s-aaaa" in open(self.journal_path).read()
        except OSError:
            return False

    def spawn(self, argv):
        self.tag = argv[-1]
        self.events.append("spawn")
        if self.child_error:
            self.files[f"{self.tag}.error.json"] = json.dumps({"exc": self.child_error})
        else:
            self.files[f"{self.tag}.prepared.json"] = json.dumps({"session": "s-aaaa"})
            self.events.append("prepared")
        g = self

        class Child:
            def poll(self):
                return 1 if g.child_dies else None

            def terminate(self):
                g.events.append("terminated")
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
    assert g.revoked == ["s-aaaa"]
    assert "terminated" in g.events      # close() reaches the guest child
    assert res.result_path.read_text().rstrip().endswith("RESULT: PASS")


def test_slot_mismatch_blocks_the_vendor_and_still_revokes(tmp_path):
    ev, g, ad = build(tmp_path, slotof="e9")
    res = run_on_vm(ev, [SlotPlan(8, "L")], ad, deadline_s=1.0)
    assert not res.ok
    assert "vendor_ran" not in g.events and "put:" + g.tag + ".journaled" not in g.events
    assert g.revoked == ["s-aaaa"]       # journaled before render raised
    assert any("landed in slot" in p for p in res.problems)


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
