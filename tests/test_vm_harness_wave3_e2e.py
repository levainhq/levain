"""Wave-3 e2e as one command, proven against a FAKE guest and a FAKE provider: no VM, no ssh, no network, no real key. The
couplings to the product (the contract's keys and stamp, the job id and share formulas, the CLI's own parser, the readers'
calls) are pinned from the product's code, so a rename there breaks this test, not the sitting."""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import re
from datetime import datetime, timezone

import pytest

from scripts.vm_harness import runbook as RB
from scripts.vm_harness import threec10 as T3
from scripts.vm_harness import wave3_e2e as W
from scripts.vm_harness.driver import GuestError
from scripts.vm_harness.host_profile import DARWIN, LINUX

KEY = "sk-or-v1-WAVE3SECRET0123456789abcdef"
CRED = "or-key"
STORE = DARWIN.store
JOB1 = "dj-" + "a" * 32
JOB2 = "dj-" + "b" * 32
DIGEST = "sha256:" + "c" * 64
BASE = "d" * 40


class World:
    """One fake guest, one fake provider, two jobs. ``usage`` is the provider's books in microdollars. Each ``dispatch show`` of
    a job advances it one state along its path (a job is a state machine the runner would drive)."""

    def __init__(self, *, charges=(80_000, 90_000), lineage_charges=None, remaining=1.6, limit=2.0, runner=True,
                 identity_source="routemap", same_identity=False, diff=("wave3/hello.txt",), job2_cause="budget_cap",
                 job2_overrun=True, over_share=False, adopted_outcome="adopted"):
        self.files = {}
        self.argvs = []
        self.stdins = []
        self.rows = []
        self.usage = 5_000_000
        self.charges = charges
        self.lineage_charges = charges if lineage_charges is None else lineage_charges
        self.remaining, self.limit, self.runner = remaining, limit, runner
        self.identity_source, self.same_identity, self.diff = identity_source, same_identity, diff
        self.job2_cause, self.job2_overrun, self.over_share, self.adopted_outcome = job2_cause, job2_overrun, over_share, adopted_outcome
        self.filed = []                        # the submissions as filed
        self.jobs = {}                         # id -> {"path": [...], "i": n, "state": str, "digest": ...}
        self.fetches = 0
        self.synced = 0
        self.verbs = []
        self.sync_answer = "synced head=7\n"

    # ---- the guest ------------------------------------------------------------------------------------------------
    def guest(self, argv, timeout=60):
        assert list(argv[:4]) == list(RB.CAPTURE_ARGV)
        inner = list(argv[4:])
        self.argvs.append(inner)
        out, rc = self.dispatch(inner)
        return "\n".join(out) + ("\n" if out else "") + f"{RB.RC_MARK} {rc}\n"

    def dispatch(self, inner):
        if inner[:3] == ["sudo", "-n", "/bin/cat"]:
            return [json.dumps(r) for r in self.rows], 0
        if inner == ["stage", "it"]:
            return ["staging", "staged gen=4"], 0
        if "doctor" in inner:
            return self.doctor_rows(), 1
        if inner[0] == DARWIN.ps_bin:
            return (["  PID COMMAND", "  101 /sbin/launchd", f"  4242 {DARWIN.python} -I -m {W.RUNNER_MODULE}"] if self.runner
                    else ["  PID COMMAND", "  101 /sbin/launchd"]), 0
        if inner[:3] == ["git", "-C", "/g/repo"]:
            if inner[3] == "rev-parse":
                return [BASE], 0
            if inner[3] == "diff":
                assert inner[4:] == ["--name-only", BASE, "main"]
                return list(self.diff), 0
        if "levain" in inner and "dispatch" in inner:
            return self.verb(inner[inner.index("dispatch") + 1:], None)
        raise AssertionError(inner)

    def doctor_rows(self):
        return ["  [OK] slots fine",
                f"  [OK] invoice ceiling ({CRED}): key limit ${self.limit:.2f} total, ${self.remaining:.2f} remaining (read now)"]

    def verb(self, v, stdin):
        self.verbs.append(list(v))
        sub = v[0]
        if sub == "file":
            assert v[1] == "-" and "--json" in v and "--agent" in v and "--session" in v
            sub_ = json.loads(stdin)
            self.filed.append(sub_)
            jid = JOB1 if len(self.filed) == 1 else JOB2
            path = (["signed", "queued", "running", "verifying", "verified"] if jid == JOB1
                    else ["signed", "running", "failed"])
            self.jobs[jid] = {"path": path, "i": -1, "state": "filed", "digest": DIGEST, "hist": [], "dollars": sub_["budget"]["dollars_cents"]}
            return [json.dumps({"id": jid, "digest": DIGEST, "state": "filed", "lease_end": "x", "capability": "CAPSECRET", "capability_kept": "k"}, indent=2)], 0
        job = self.jobs[v[1]]
        if sub == "sign":
            assert "--yes" in v and "--signing-key" in v
            job["i"], job["state"] = 0, "signed"
            return ["---- you are about to sign ----", "  {not json}", json.dumps({"id": v[1], "state": "signed", "signer_fp": "SHA256:fp", "lease_end": "x"}, indent=2)], 0
        if sub == "show":
            return [json.dumps(self.view(v[1]), indent=2)], 0
        if sub == "recommend":
            assert v[v.index("--run") + 1] == "0" and v[v.index("--onto") + 1] == "main"
            return [json.dumps({"id": v[1], "state": "recommended"}, indent=2)], 0
        if sub == "authorise":
            assert "--yes" in v and v[v.index("--run") + 1] == "0"
            job["state"], job["hist"] = "adopted", job["hist"] + [{"seq": 9, "to_state": "adopted", "cause": "landed"}]
            return ["authorise view", json.dumps({"id": v[1], "state": "adopted" if self.adopted_outcome == "adopted" else "authorised",
                                                  "outcome": self.adopted_outcome, "moved": True, "message": "m", "command": None}, indent=2)], 0
        raise AssertionError(v)

    def view(self, jid):
        job = self.jobs[jid]
        if job["state"] != "adopted" and job["i"] + 1 < len(job["path"]):
            job["i"] += 1
            job["state"] = job["path"][job["i"]]
            cause = {"verifying": "runs_exited", "failed": self.job2_cause}.get(job["state"], "x")
            job["hist"].append({"seq": job["i"] + 2, "to_state": job["state"], "cause": cause})
        if jid == JOB2 and job["state"] == "failed" and self.job2_overrun:
            self.rows.append({"job_id": JOB2, "run_index": 0, "run_id": "r-9", "cost_microdollars": 5_000, "model_answered": "m",
                              "settled": "overrun", "overrun": 321})
        return {"id": jid, "state": job["state"], "digest": job["digest"], "base": BASE, "write": [{"path": "wave3/hello.txt", "mode": "new", "blob": "new"}],
                "history": job["hist"]}

    def stdin_run(self, argv, text, timeout=60):
        self.argvs.append(list(argv))
        self.stdins.append(text)
        if list(argv[:4]) ==["sudo", "-n", "/bin/sh", "-c"] and argv[5] == "sh":
            script, name = argv[4], argv[7]
            if script == T3.INSTALL_SCRIPT:
                if argv[8] != "replace" and name in self.files:
                    return f"cred-refused {name}: {argv[6]}/keys/cred/{name}.json already exists\n"
                self.files[name] = text
                return f"cred-installed {name}\n"
            if script == T3.REMOVE_SCRIPT:
                self.files.pop(name, None)
                return f"cred-removed {name}\n"
            if script == T3.VERIFY_SCRIPT:
                return f"cred-present {name}.json\n" if name in self.files else f"cred-absent {name}\n"
            raise AssertionError(script)
        if text == W.READ_TICKETS:
            assert tuple(argv[:5]) == ("sudo", "-n", "-u", "_levain_dispatch", "/usr/bin/env")
            assert tuple(argv[-4:-2]) == ("/tmp/lvclean", f"{DARWIN.run_dir}/control/control.sock")
            job = argv[-2]
            digest = DIGEST
            return json.dumps([{"job_id": job, "run_index": i, "contract_digest": digest,
                                "reserved_microdollars": W.share_microdollars(self.jobs[job]["dollars"])} for i in range(2)]) + "\n"
        if text == W.SYNC_AUTHORITY:
            assert tuple(argv[:2]) != ("sudo", "-n") and tuple(argv[-2:]) == ("/tmp/lvclean", "/g/cockpit")   # the operator, not sudo
            self.synced += 1
            return self.sync_answer
        if text == W.READ_RECORDS:
            assert tuple(argv[:5]) == ("sudo", "-n", "-u", "_levain_dispatch", "/usr/bin/env")
            assert argv[-1] == f"{DARWIN.store}/dispatch"
            return json.dumps([{"job_id": argv[-2], "run": i, "model": {"requested": "m", "answered": "m",
                               "backend_identity": "same" if self.same_identity else f"be-{i}", "identity_source": self.identity_source}}
                               for i in range(2)]) + "\n"
        if "levain" in argv and "dispatch" in argv:
            out, rc = self.verb(list(argv[list(argv).index("dispatch") + 1:]), text)
            if rc:
                raise GuestError(rc, "\n".join(out))
            return "\n".join(out) + "\n"
        raise AssertionError(argv)

    # ---- the provider ----------------------------------------------------------------------------------------------
    def fetch(self, url, headers, timeout):
        assert url == T3.KEY_URL and headers["Authorization"] == "Bearer " + KEY
        self.fetches += 1
        if JOB1 in self.jobs and self.jobs[JOB1]["state"] == "adopted" and not getattr(self, "_billed", False):
            self._billed = True
            for i, c in enumerate(self.charges):
                self.rows.append({"job_id": JOB1, "run_index": i, "run_id": f"r-{i}", "cost_microdollars": self.lineage_charges[i],
                                  "model_answered": "m", "settled": "settled", "overrun": None})
                self.usage += c
        return 200, json.dumps({"data": {"usage": self.usage / 1_000_000, "limit": 2}}).encode()


def cfg(**kw):
    kw.setdefault("key", KEY)
    kw.setdefault("repo", "/g/repo")
    kw.setdefault("stage_call", ("stage", "it"))
    kw.setdefault("signing_key", "/g/key")
    kw.setdefault("data_root", "/g/cockpit")
    kw.setdefault("overrun_goal", "spend more")
    kw.setdefault("poll_s", 0.0)
    kw.setdefault("settle_poll_s", 0.0)
    return W.Config(DARWIN, "/tmp/install", cred=CRED, **kw)


def go(tmp_path, w=None, **kw):
    w = w or World()
    ev = tmp_path / "ev"
    ev.mkdir()
    ticks = iter(range(100_000))
    out = W.run_wave3(w.guest, w.stdin_run, ev, cfg(**kw), fetch=w.fetch, sleep=lambda s: None, clock=lambda: float(next(ticks)))
    return w, ev, out


def by_id(steps):
    return {s.id: s for s in steps}


# ---- the table ---------------------------------------------------------------------------------------------------------

def test_expected_verdicts_are_single_patterns_one_per_step():
    assert [e[0] for e in W.EXPECTED] == [f"W{i}" for i in range(1, 11)]
    for sid, _n, pat, _sel in W.EXPECTED:
        RB.Verdict(None, pat).check()
        assert "|" not in pat, sid                            # the selectors of W9/W10 name their words; the verdict never does


# ---- the run -----------------------------------------------------------------------------------------------------------

def test_full_run_passes_every_step_with_its_one_verdict(tmp_path):
    w, ev, (status, steps, path) = go(tmp_path)
    assert status == "PASS", path.read_text()
    s = by_id(steps)
    assert [x.id for x in steps] == list(W.STEP_IDS) and all(x.status == "PASS" for x in steps)
    assert s["W1"].raw == "staged gen=4"
    assert s["W2"].raw == "key limit $2.00, remaining $1.60, runner up pid=4242"
    assert s["W5"].raw == f"running {JOB1} runs=2 reserved=100000,100000 index=0,1 digest=ok"
    assert s["W6"].raw == f"verifying {JOB1} cause=runs_exited runs=2"
    assert s["W7"].raw == f"verified {JOB1} runs=2 source=routemap,routemap identities=2"
    assert s["W8"].raw == f"adopted {JOB1} onto=main files=1 outside=0"
    assert s["W9"].raw.startswith("AGREE lineage_microdollars=170000 provider_delta_microdollars=170000 runs=2 max_run_microdollars=90000 share_microdollars=100000")
    assert s["W10"].raw == f"failed budget_cap overrun-row job={JOB2} run_id=r-9 overrun=321"
    assert w.files == {}                                                   # the credential is gone
    assert w.filed[0]["budget"]["dollars_cents"] == 20 and w.filed[1]["budget"]["dollars_cents"] == 1


def test_expected_txt_is_written_before_any_guest_call_and_the_secret_never_leaves_stdin(tmp_path):
    w = World()
    seen = []
    og, os_ = w.guest, w.stdin_run
    w.guest = lambda argv, timeout=60: (seen.append((tmp_path / "ev" / "EXPECTED.txt").exists()), og(argv, timeout))[1]
    w.stdin_run = lambda argv, text, timeout=60: (seen.append((tmp_path / "ev" / "EXPECTED.txt").exists()), os_(argv, text, timeout))[1]
    _w, ev, (status, _s, _p) = go(tmp_path, w)
    assert seen and all(seen) and status == "PASS"
    body = (ev / "EXPECTED.txt").read_text()
    assert "W9 | lineage per run" in body and "100000 microdollars per run" in body and "sha256:" in body
    assert KEY not in json.dumps(w.argvs)
    assert any(KEY in t for t in w.stdins)
    for p in ev.iterdir():
        assert KEY not in p.read_text() and "CAPSECRET" not in p.read_text(), p.name


def test_the_contract_filed_is_the_products_submission_shape(tmp_path):
    from levain.cockpit.consent import parse_stamp
    from levain.dispatch.contract import SUBMISSION_KEYS
    w, _ev, _ = go(tmp_path)
    for sub in w.filed:
        assert set(sub) == SUBMISSION_KEYS
        assert sub["write"] == ["wave3/hello.txt"] and sub["base"] == BASE and sub["repo"] == "/g/repo"
        assert parse_stamp(sub["expires_at"]) > datetime.now(timezone.utc)
    assert w.filed[1]["goal"] == "spend more"


def test_a_stage_that_prints_no_gen_stops_the_run_unbound_and_removes_the_key(tmp_path):
    w = World()
    og = w.dispatch
    w.dispatch = lambda inner: ((["nothing"], 0) if inner == ["stage", "it"] else og(inner))
    _w, ev, (status, steps, path) = go(tmp_path, w)
    s = by_id(steps)
    assert s["W1"].status == "FAIL" and all(s[f"W{i}"].status == "UNBOUND" for i in range(2, 11)) and status == "FAIL"
    assert w.files == {}


def test_missing_inputs_are_unbound_and_the_run_is_incomplete(tmp_path):
    w, _ev, (status, steps, _p) = go(tmp_path, key=None)
    assert by_id(steps)["W1"].status == "UNBOUND" and status == "INCOMPLETE" and w.argvs == [] and w.stdins == []
    (tmp_path / "b").mkdir()
    w, _ev, (status, steps, _p) = go(tmp_path / "b", overrun_goal="")
    s = by_id(steps)
    assert s["W9"].status == "PASS" and s["W10"].status == "UNBOUND" and status == "INCOMPLETE"


@pytest.mark.parametrize("kw,step", [
    ({"remaining": 0.30}, "W2"), ({"limit": 5.0}, "W2"), ({"runner": False}, "W2"),
])
def test_w2_refuses_a_key_or_runner_not_as_the_sitting_needs(tmp_path, kw, step):
    _w, _ev, (status, steps, _p) = go(tmp_path, World(**kw))
    s = by_id(steps)
    assert s[step].status == "FAIL" and status == "FAIL"
    assert all(s[f"W{i}"].status == "UNBOUND" for i in range(3, 11))


@pytest.mark.parametrize("field,value", [("reserved_microdollars", 99_999), ("contract_digest", "sha256:" + "e" * 64),
                                         ("job_id", JOB2), ("run_index", 7)])
def test_w5_refuses_a_ticket_that_is_not_the_jobs_share_digest_id_or_index(tmp_path, field, value):
    w = World()
    og = w.stdin_run

    def run(argv, text, timeout=60):
        out = og(argv, text, timeout)
        if text == W.READ_TICKETS:
            rows = json.loads(out)
            rows[1][field] = value
            return json.dumps(rows)
        return out

    w.stdin_run = run
    _w, _ev, (status, steps, _p) = go(tmp_path, w)
    s = by_id(steps)
    assert s["W5"].status == "FAIL" and s["W6"].status == "UNBOUND" and status == "FAIL"


def test_a_run_record_that_is_not_route_map_sourced_fails_w7_and_so_does_one_identity(tmp_path):
    _w, _ev, (status, steps, _p) = go(tmp_path, World(identity_source="passthrough"))
    s = by_id(steps)
    assert s["W7"].status == "FAIL" and s["W7"].raw.startswith(f"verified {JOB1} runs=2 source=passthrough,passthrough")
    assert s["W8"].status == "UNBOUND" and s["W9"].status == "UNBOUND"
    (tmp_path / "b").mkdir()
    _w, _ev, (_st, steps, _p) = go(tmp_path / "b", World(same_identity=True))
    assert by_id(steps)["W7"].status == "FAIL" and "identities=1" in by_id(steps)["W7"].raw


def test_a_landed_file_outside_the_write_set_fails_w8_and_a_merge_outcome_is_not_adopted(tmp_path):
    _w, _ev, (status, steps, _p) = go(tmp_path, World(diff=("wave3/hello.txt", "README.md")))
    s = by_id(steps)
    assert s["W8"].status == "FAIL" and s["W8"].raw.endswith("files=2 outside=1")
    (tmp_path / "b").mkdir()
    _w, _ev, (_st, steps, _p) = go(tmp_path / "b", World(adopted_outcome="merge"))
    assert by_id(steps)["W8"].status == "FAIL" and by_id(steps)["W8"].raw.startswith("not-adopted")


def test_w9_lineage_off_the_bill_or_a_provider_that_did_not_move_or_a_run_over_its_share_is_not_agree(tmp_path):
    _w, _ev, (_s, steps, _p) = go(tmp_path, World(charges=(80_000, 90_000), lineage_charges=(100_000, 100_000)))
    assert by_id(steps)["W9"].raw.startswith("DISAGREE") and by_id(steps)["W9"].status == "FAIL"
    (tmp_path / "b").mkdir()
    _w, _ev, (_s, steps, _p) = go(tmp_path / "b", World(charges=(0, 0), lineage_charges=(500, 400)), settle_wait_s=3.0)
    assert by_id(steps)["W9"].raw.startswith("NOMOVE")                    # a small lineage total is inside any floor: not an agreement
    (tmp_path / "c").mkdir()
    _w, _ev, (_s, steps, _p) = go(tmp_path / "c", World(charges=(150_000, 10_000)))
    assert by_id(steps)["W9"].raw.startswith("OVERSHARE") and by_id(steps)["W9"].status == "FAIL"
    (tmp_path / "d").mkdir()
    _w, _ev, (_s, steps, _p) = go(tmp_path / "d", World(charges=(80_000, 90_000), lineage_charges=(82_000, 90_000)))
    assert by_id(steps)["W9"].status == "PASS"                            # 1.2% inside 5%


def test_w10_needs_the_budget_cap_cause_and_an_overrun_row(tmp_path):
    _w, _ev, (_s, steps, _p) = go(tmp_path, World(job2_cause="run_failed"))
    s = by_id(steps)["W10"]
    assert s.status == "FAIL" and s.raw.startswith(f"not-budget_cap {JOB2}")
    (tmp_path / "b").mkdir()
    _w, _ev, (_s, steps, _p) = go(tmp_path / "b", World(job2_overrun=False))
    assert by_id(steps)["W10"].raw.startswith(f"no-overrun-row job={JOB2}")


def test_w10_reads_only_a_positive_overrun_as_the_hit(tmp_path):
    # L3 codex L229: an earlier normal row of the job with overrun 0 was taken as the hit and the step reported it
    class ZeroFirst(World):
        def view(self, jid):
            if jid == JOB2 and self.jobs[jid]["i"] + 1 == len(self.jobs[jid]["path"]) - 1:
                self.rows.append({"job_id": JOB2, "run_index": 1, "run_id": "r-8", "cost_microdollars": 1,
                                  "model_answered": "m", "settled": "settled", "overrun": 0})
            return super().view(jid)
    _w, _ev, (_s, steps, _p) = go(tmp_path, ZeroFirst())
    assert by_id(steps)["W10"].raw == f"failed budget_cap overrun-row job={JOB2} run_id=r-9 overrun=321"
    (tmp_path / "b").mkdir()
    _w, _ev, (_s, steps, _p) = go(tmp_path / "b", ZeroFirst(job2_overrun=False))
    assert by_id(steps)["W10"].raw.startswith(f"no-overrun-row job={JOB2}")       # a 0 alone is no overrun row


def test_a_transport_loss_is_error_and_a_job_that_never_runs_is_a_fail_not_a_pass(tmp_path):
    w = World()
    og = w.dispatch
    w.dispatch = lambda inner: (_ for _ in ()).throw(GuestError(255, "ssh dropped")) if "doctor" in inner else og(inner)
    _w, _ev, (status, steps, _p) = go(tmp_path, w)
    assert by_id(steps)["W2"].status == "ERROR" and status == "INCOMPLETE" and w.files == {}
    (tmp_path / "b").mkdir()
    w = World()
    w.jobs_path_never_runs = True
    og2 = w.view

    def stuck(jid):
        v = og2(jid)
        if jid == JOB1 and v["state"] in ("running", "verifying", "verified"):
            w.jobs[jid].update(i=1, state="queued")
            v["state"] = "queued"
        return v

    w.view = stuck
    _w, _ev, (status, steps, _p) = go(tmp_path / "b", w, step_wait_s=50.0)
    s = by_id(steps)
    assert s["W5"].status == "FAIL" and "queued" in s["W5"].raw and s["W6"].status == "UNBOUND" and status == "FAIL"


def test_a_removal_that_fails_is_said_loudly_and_never_beside_a_pass(tmp_path):
    w = World()
    og = w.stdin_run

    def run(argv, text, timeout=60):
        if len(argv) > 4 and argv[4] == T3.REMOVE_SCRIPT:
            raise GuestError(1, "rm refused")
        return og(argv, text, timeout)

    w.stdin_run = run
    _w, ev, (status, _s, path) = go(tmp_path, w)
    assert status == "INCOMPLETE" and "NOT confirmed removed" in (ev / "steps.log").read_text() and "CLEANUP:" in path.read_text()


def test_an_install_with_an_uncertain_result_is_never_removed_by_this_script(tmp_path):
    w = World()
    og = w.stdin_run

    def run(argv, text, timeout=60):
        out = og(argv, text, timeout)
        if len(argv) > 4 and argv[4] == T3.INSTALL_SCRIPT:
            raise GuestError(255, "dropped after the write")
        return out

    w.stdin_run = run
    _w, _ev, (status, steps, path) = go(tmp_path, w)
    assert by_id(steps)["W1"].status == "ERROR" and status == "INCOMPLETE" and CRED in w.files
    assert "check it by hand" in path.read_text()
    assert all(len(a) < 5 or a[4] != T3.REMOVE_SCRIPT for a in w.argvs)


def test_an_existing_credential_refuses_and_is_left(tmp_path):
    w = World()
    w.files[CRED] = "HIS"
    _w, _ev, (status, steps, _p) = go(tmp_path, w)
    assert by_id(steps)["W1"].status == "REFUSED" and status == "REFUSED" and w.files == {CRED: "HIS"}


def test_an_install_that_exits_clean_echoing_part_of_the_key_records_no_stdout_line(tmp_path):
    w = World()
    frag = KEY[:20]
    orig = w.stdin_run
    w.stdin_run = lambda argv, text, timeout=60: (frag + "\n") if argv[4] == T3.INSTALL_SCRIPT else orig(argv, text, timeout)
    _w, ev, (_status, steps, path) = go(tmp_path, w)
    assert by_id(steps)["W1"].status == "ERROR"
    assert frag not in (ev / "steps.log").read_text() and frag not in path.read_text()


# ---- couplings to the product -------------------------------------------------------------------------------------------

def test_job_id_share_and_stamp_agree_with_the_products():
    from levain.dispatch import jobs, run_jobs
    assert W.JOB_RE.pattern == jobs._JOB_ID.pattern
    for cents in (20, 1, 0, 7):
        for k in (1, 2, 3):
            assert W.share_microdollars(cents, k) == run_jobs.share_micros(cents, k)
    assert W.share_microdollars(W.FIRST_DOLLARS_CENTS) == 100_000 and W.share_microdollars(W.OVERRUN_DOLLARS_CENTS) == 5_000
    assert W.RUNNER_MODULE == run_jobs.__name__


def test_the_verbs_the_harness_runs_parse_under_the_products_own_dispatch_parser():
    from levain.dispatch.cli import add_parsers
    ap = argparse.ArgumentParser()
    add_parsers(ap.add_subparsers(dest="cmd", required=True))
    sel = ["--agent", "claude_code", "--session", "wave3-x"]
    sgn = ["--signing-key", "/g/key", "--data-root", "/g/dr"]
    for argv in (["dispatch", "file", "-", "--json", *sel], ["dispatch", "show", JOB1, "--json"],
                 ["dispatch", "sign", JOB1, "--yes", "--json", *sgn],
                 ["dispatch", "recommend", JOB1, "--onto", "main", "--run", "0", "--json", *sel],
                 ["dispatch", "authorise", JOB1, "--run", "0", "--onto", "main", "--yes", "--json", *sgn]):
        ap.parse_args(argv)
    from levain.dispatch import capability
    assert "claude_code" in capability.AGENTS


def test_the_readers_compile_and_call_what_the_product_has():
    compile(W.READ_TICKETS, "READ_TICKETS", "exec")
    compile(W.READ_RECORDS, "READ_RECORDS", "exec")
    from levain.dispatch.adopt import ArtifactRecord
    from levain.dispatch.jobs import JobStore
    from levain.slot import control_client
    assert hasattr(JobStore, "run_records") and hasattr(ArtifactRecord, "to_mapping")
    assert "RUN-TICKET-FACTS" in W.READ_TICKETS and hasattr(control_client.ControlClient, "call")
    assert set(inspect.signature(control_client.ControlClient.call).parameters) >= {"op", "fields"}
    from levain.agents import session_launcher
    src = inspect.getsource(session_launcher.RealLauncher._facts)
    for name in ("job_id", "run_index", "contract_digest", "reserved_microdollars"):
        assert f'"{name}"' in src, name


def test_the_credential_json_and_ps_selector_helpers_are_threec10s():
    assert W.INSTALL_SCRIPT is T3.INSTALL_SCRIPT and W.credential_json is T3.credential_json


def test_linux_profile_uses_its_own_store_and_the_expected_text_names_the_os():
    c = W.Config(LINUX, "/tmp/install", "/g/repo")
    assert W.expected_text(c).splitlines()[0].startswith("wave3 e2e: expected verdicts, os=linux")
    assert W.expected_text(c).count("\n/") == 0


def test_a_bad_credential_name_refuses_before_any_evidence(tmp_path):
    ev = tmp_path / "ev"
    ev.mkdir()
    with pytest.raises(RB.RunbookError):
        W.run_wave3(lambda *a, **k: "", lambda *a: "", ev, W.Config(DARWIN, "/i", "/g", "Bad Name"))
    assert list(ev.iterdir()) == []


def test_json_in_takes_the_document_a_verb_ends_with_and_not_a_bracketed_view_line():
    text = '---- view ----\n[WARN] something {not json}\n{\n  "id": "x",\n  "n": [1, 2]\n}\n'
    assert W._json_in(text) == {"id": "x", "n": [1, 2]}
    with pytest.raises(ValueError):
        W._json_in("no document here")
    assert hashlib.sha256(b"x").hexdigest() and re.fullmatch(r"dj-[0-9a-f]{32}", JOB1)


# ---- the Wed stopgap (np-667d731c (b)): the chain past the door, the door not exercised ----------------------------------------

RESIDUE_LINE = "operator-to-dispatch handover not exercised: stopgap per np-667d731c (b)"
STOP_DIR = f"{DARWIN.store}/dispatch"


def test_the_verdict_file_and_expected_txt_carry_the_handover_as_residue_in_the_fixed_words(tmp_path):
    assert W.RESIDUE_HANDOVER == RESIDUE_LINE
    _w, ev, (status, _s, path) = go(tmp_path)
    assert status == "PASS"
    assert f"RESIDUE: {RESIDUE_LINE}" in path.read_text().splitlines()
    assert f"RESIDUE: {RESIDUE_LINE}" in (ev / "EXPECTED.txt").read_text().splitlines()


def test_a_run_that_does_not_pass_still_carries_the_residue(tmp_path):
    _w, _ev, (status, _s, path) = go(tmp_path, World(runner=False))
    assert status != "PASS" and f"RESIDUE: {RESIDUE_LINE}" in path.read_text().splitlines()


def test_every_dispatch_verb_runs_as_the_dispatch_account_against_l_dispatch_never_the_operators_store(tmp_path):
    w, _ev, (status, _s, _p) = go(tmp_path)
    assert status == "PASS"
    verbs = [a for a in w.argvs if "dispatch" in a and "levain" in a]
    assert {a[a.index("dispatch") + 1] for a in verbs} == {"file", "show", "sign", "recommend", "authorise"}
    for a in verbs:
        assert a[:5] == ["sudo", "-n", "-u", "_levain_dispatch", "/usr/bin/env"], a
        assert a[a.index("--dispatch-dir") + 1] == STOP_DIR, a
        sub = a[a.index("dispatch") + 1]
        if sub in ("file", "sign", "authorise"):
            assert a[a.index("--data-root") + 1] == f"{STOP_DIR}/authority", a
        else:
            assert "--data-root" not in a, a


def test_the_one_sync_runs_as_the_operator_once_before_the_first_filing(tmp_path):
    w, _ev, _out = go(tmp_path)
    assert w.synced == 1
    first_file = next(i for i, a in enumerate(w.argvs) if "dispatch" in a and "file" in a)
    sync_at = next(i for i, a in enumerate(w.argvs) if a[:3] == [DARWIN.python, "-I", "-"] and a[-2:] == ["/tmp/lvclean", "/g/cockpit"])
    assert sync_at < first_file


def test_a_sync_that_does_not_land_stops_the_chain_before_anything_is_filed(tmp_path):
    w = World()
    w.sync_answer = "sync-refused _Refused: ledger-fork\n"
    _w, _ev, (status, steps, _p) = go(tmp_path, w)
    s = by_id(steps)
    assert status != "PASS" and s["W3"].status == "FAIL" and "SYNC" in s["W3"].detail
    assert w.filed == [] and s["W4"].status == "UNBOUND"


def test_no_cockpit_data_root_is_unbound_not_a_guess(tmp_path):
    w, _ev, (status, steps, _p) = go(tmp_path, data_root="")
    assert by_id(steps)["W3"].status == "UNBOUND" and status != "PASS" and w.filed == [] and w.synced == 0


def test_the_stopgap_flags_and_both_readers_agree_with_the_products_own_parser_and_names():
    from levain.dispatch.cli import add_parsers
    ap = argparse.ArgumentParser()
    add_parsers(ap.add_subparsers(dest="cmd", required=True))
    both = ["--dispatch-dir", STOP_DIR, "--data-root", f"{STOP_DIR}/authority"]
    sel = ["--agent", "claude_code", "--session", "wave3-x"]
    for argv in (["dispatch", "file", "-", "--json", *sel, *both], ["dispatch", "sign", JOB1, "--yes", "--json", "--signing-key", "/g/key", *both],
                 ["dispatch", "authorise", JOB1, "--run", "0", "--onto", "main", "--yes", "--json", "--signing-key", "/g/key", *both],
                 ["dispatch", "show", JOB1, "--json", "--dispatch-dir", STOP_DIR],
                 ["dispatch", "recommend", JOB1, "--onto", "main", "--run", "0", "--json", *sel, "--dispatch-dir", STOP_DIR]):
        ap.parse_args(argv)
    from levain.slot import layout
    assert W.dispatch_dir(W.Config(DARWIN, "/i")) == str(layout.dispatch_store_dir("darwin"))
    assert W.dispatch_dir(W.Config(LINUX, "/i")) == str(layout.dispatch_store_dir("linux"))
    assert "store / \"dispatch\" / \"authority\"" in inspect.getsource(__import__("levain.slot.services.dispatch_run", fromlist=["x"]))
    compile(W.SYNC_AUTHORITY, "SYNC_AUTHORITY", "exec")
    from levain.agent_authority import AgentAuthority
    from levain.agents import session_client as sc
    assert callable(sc._sync_to) and callable(sc.unix_connector) and callable(sc.default_run_socket) and hasattr(sc, "_Conn")
    assert hasattr(sc.AuthorityFeed, "rows") and hasattr(AgentAuthority, "replay")
    assert "JobStore(Path(directory))" in W.READ_RECORDS
