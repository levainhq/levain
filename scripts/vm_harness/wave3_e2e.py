"""Wave-3 end to end (S14_RUNNER_DESIGN section 7, H1): a dispatch job driven FILED -> SIGNED -> RUNNING -> VERIFYING -> VERIFIED
-> ADOPTED on a real guest through the runner, then a second job that must stop on its dollar cap. Host side. usage:
  LEVAIN_WAVE3_KEY=... python3 -m scripts.vm_harness.wave3_e2e <vm> <evidence-dir> --install /path/of/the/install \\
        --repo /guest/repo --stage-call "CMD ARGS" --signing-key /guest/key [--overrun-goal "..."] [--profile darwin|linux]
        [--cred or-key] [--template ID] [--write-path PATH] [--onto main] [--data-root DIR] [--levain-root /tmp/lvclean]
(the OpenRouter key is read from the environment, never from an argument, and goes to the guest on stdin as the keys credential
``--cred``. ``--stage-call`` is the guest command, run as root, that stages the signed route map (one brokered route per
model), the signed R0 template (k = 2) and the signed verifier item, and prints ``staged gen=<n>``; it is the sitting's to
name, as threec10's ``--call`` is. ``--overrun-goal`` is a goal on which one run spends more than a 5,000 microdollar share.)
``tests/test_vm_harness_wave3_e2e.py`` proves it end to end against a FAKE guest and a FAKE provider, no VM and no vendor. The
real run is the half-day sitting; nothing here has touched a vendor or a runner process.

STOPGAP (np-667d731c (b)): the dispatch verbs run as ``_levain_dispatch`` with ``--dispatch-dir L/dispatch --data-root
L/dispatch/authority``, after one SYNC of the operator's ledger (inside W3), so the runner -> follower -> verify -> land chain is
exercised and the operator -> dispatch handover is NOT: the verdict file carries :data:`RESIDUE_HANDOVER`. ``--data-root`` is the
operator's cockpit data root (the SYNC's source only); ``--signing-key`` must be readable by the dispatch account. The stage call
must place the signed config in ``L/dispatch`` itself (the harness does not accept config).

The steps, each with ONE expected verdict (:data:`EXPECTED`), written to ``EXPECTED.txt`` in the evidence dir before the first
guest call (the table is code, so there is no committed copy to drift):
  W1  install the key as the keys credential; run the stage call                               -> ``staged gen=<n>``
  W2  ``levain doctor``: the key's limit and what remains; the runner process is up            -> ``key limit $X, remaining $Y, runner up pid=<n>``
  W3  ``levain dispatch file`` a one-file contract, ``dollars_cents = 20``                      -> ``filed dj-<32 hex> digest=sha256:<64 hex>``
  W4  ``levain dispatch sign``                                                                -> ``signed dj-... as <fingerprint>``
  W5  the runner takes it: ``running``, one executor ticket per run, from CONTROL's journal    -> ``running dj-... runs=2 reserved=100000,100000 index=0,1 digest=ok``
  W6  both runs end, the follower takes both artifacts                                         -> ``verifying dj-... cause=runs_exited runs=2``
  W7  verifier runs; the follower records them; run records carry the route map's identity      -> ``verified dj-... runs=2 source=routemap,routemap identities=2``
  W8  ``recommend``, ``authorise`` (run 0), ``land``; the landed diff is inside the write set   -> ``adopted dj-... onto=<branch> files=<n> outside=0``
  W9  lineage dollars per run against the provider's own usage delta, each run <= its share     -> ``AGREE ...``
  W10 a second job, ``dollars_cents = 1`` (share 5,000 microdollars), on a goal that spends more -> ``failed budget_cap overrun-row ...``
What a verdict PROVES, exactly: the raw line of every step is synthesized by this runner from what it read (the job's own
``show --json``, CONTROL's ticket facts, the job store's run records, the lineage sink, the provider's usage, git), so the
regex match is a format check; the real verdict is the Python check that chose the words (as threec10's). A step that
needs an earlier step's job is UNBOUND when that step did not pass, and the run ends INCOMPLETE, never PASS. A step that lost
its transport (ssh exit 255, a timeout) is ERROR. The credential is removed on the way out and the removal is graded as in
threec10; an install whose result is uncertain is never replaced or removed by this script (the run says so).
Not proven by the fake guest (the sitting is the first time): that the guest's ``levain dispatch`` verbs, the ticket-facts
reader and the run-record reader as ``_levain_dispatch`` run as written below; that OpenRouter answers
``data.usage`` (threec10's assumption too); that a verifier slot starts (R3's ``VerifierRuntime`` default refuses
``verifier-start-unbuilt``, so W6/W7 cannot pass on a build without root-side verifier start: that is a product finding, not a
harness one).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence

from scripts.vm_harness.driver import GuestError
from scripts.vm_harness.host_profile import DARWIN, PROFILES, HostProfile, profile_named
from scripts.vm_harness.journey8 import LEVAIN_ROOT, StepResult, levain_argv
from scripts.vm_harness.runbook import Evidence, RunbookError, Verdict, capture, judge, overall
from scripts.vm_harness.threec10 import (CRED_NAME_RE, INSTALL_SCRIPT, REMOVE_SCRIPT, USD, VERIFY_SCRIPT, FetchError,
                                         credential_json, default_fetch, lineage_rows, usage_microdollars)

KEY_ENV = "LEVAIN_WAVE3_KEY"
RUNNER_MODULE = "levain.dispatch.run_jobs"
JOB_RE = re.compile(r"dj-[0-9a-f]{32}")          # levain.dispatch.jobs._JOB_ID (a test pins them equal)
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
STAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"          # levain.cockpit.consent's fixed-width stamp (a test parses one)
K = 2                                           # the signed R0 template's model count
FIRST_DOLLARS_CENTS = 20                        # per run at k = 2: 20 * 10_000 // 2 = 100_000 microdollars
OVERRUN_DOLLARS_CENTS = 1                       # per run at k = 2: 5_000 microdollars


def share_microdollars(dollars_cents: int, k: int = K) -> int:
    """The runner's per-run share (``levain.dispatch.run_jobs``): exact integer multiply, floor division."""
    return (dollars_cents * 10_000) // k


#: The ticket-facts reader, run as ``_levain_dispatch`` (CONTROL answers ``RUN-TICKET-FACTS`` to that peer only) with the
#: script on stdin: ``python -I - <levain-root> <control-socket> <job> <k>``. Prints a JSON list, one facts mapping per run
#: index (or ``{"error": ...}``).
READ_TICKETS = '''import json, sys
root, sock, job, k = sys.argv[1:5]
sys.path.insert(0, root)
from levain.slot.control_client import ControlClient
out = []
with ControlClient(sock) as c:
    for i in range(int(k)):
        try:
            out.append(c.call("RUN-TICKET-FACTS", job_id=job, run_index=i)["facts"])
        except Exception as e:
            out.append({"error": type(e).__name__ + ": " + str(e)})
print(json.dumps(out))
'''
#: The run-record reader, run as ``_levain_dispatch`` (``L/dispatch`` is its alone, 0700): ``python -I - <levain-root> <job>
#: <dispatch-dir>``. Prints a JSON list of the job's run records as the store re-checks them.
READ_RECORDS = '''import json, sys
from pathlib import Path
root, job, directory = sys.argv[1:4]
sys.path.insert(0, root)
from levain.dispatch.jobs import JobStore
print(json.dumps([r.to_mapping() for r in JobStore(Path(directory)).run_records(job)]))
'''
#: The one SYNC of the stopgap, run as the OPERATOR (the run server accepts the authority feed from the operator peer only):
#: ``python -I - <levain-root> <cockpit-data-root>``. It brings the run server's authority replica
#: (``L/dispatch/authority``) to the operator's ledger head with the product's own feed and chain, so ``_levain_dispatch``
#: can verify the operator's signatures against ``--data-root L/dispatch/authority``. Prints ``synced head=<seq>``, or
#: ``sync-refused <Name>: <why>``.
SYNC_AUTHORITY = '''import sys
root, data_root = sys.argv[1:3]
sys.path.insert(0, root)
from levain.agent_authority import AgentAuthority
from levain.agents import session_client as sc
conn = None
try:
    state = AgentAuthority(data_root).replay()
    conn = sc._Conn(sc.unix_connector(sc.default_run_socket())())
    sc._sync_to(conn, sc.AuthorityFeed(None, data_root), (state.seq, state.head_hash))
    print("synced head=%d" % state.seq)
except Exception as e:
    print("sync-refused " + type(e).__name__ + ": " + str(e)[:200])
finally:
    if conn is not None:
        conn.close()
'''

#: The handover the stopgap does not exercise, recorded on every verdict file (np-667d731c (b)): the operator's own file,
#: sign and accept reach ``L/dispatch`` only through a door that is not built yet (S14 design C, D1).
RESIDUE_HANDOVER = "operator-to-dispatch handover not exercised: stopgap per np-667d731c (b)"

#: (id, name, pattern, selector): the verdicts, one each, fixed before any run. rc is not part of a verdict (the raw lines are
#: built from what the runner read, so there is no probe exit code to compare).
EXPECTED: tuple[tuple[str, str, str, str], ...] = (
    ("W1", "stage the key, route map, template and verifier", r"staged gen=[1-9][0-9]*", r"^staged gen="),
    ("W2", "doctor: key limit and remainder, the runner up",
     r"key limit " + USD + r", remaining " + USD + r", runner up pid=[0-9]+", r"^key limit "),
    ("W3", "file a one-file contract", r"filed dj-[0-9a-f]{32} digest=sha256:[0-9a-f]{64}", r"^filed "),
    ("W4", "sign it", r"signed dj-[0-9a-f]{32} as \S+", r"^signed "),
    ("W5", "the runner takes it: two priced executor tickets",
     r"running dj-[0-9a-f]{32} runs=2 reserved=100000,100000 index=0,1 digest=ok", r"^running "),
    ("W6", "both runs end, both artifacts taken", r"verifying dj-[0-9a-f]{32} cause=runs_exited runs=2", r"^verifying "),
    ("W7", "verifier runs, recorded, route-map identities",
     r"verified dj-[0-9a-f]{32} runs=2 source=routemap,routemap identities=2", r"^verified "),
    ("W8", "recommend, authorise, land: adopted inside the write set",
     r"adopted dj-[0-9a-f]{32} onto=\S+ files=[1-9][0-9]* outside=0", r"^adopted "),
    ("W9", "lineage per run against the provider's usage",
     r"AGREE lineage_microdollars=[0-9]+ provider_delta_microdollars=[0-9]+ runs=2 max_run_microdollars=[0-9]+ "
     r"share_microdollars=100000 tolerance=[0-9]+", r"^(AGREE|DISAGREE|NOCALL|NOMOVE|OVERSHARE) "),
    ("W10", "a 5,000 microdollar job on a spendier goal stops on its cap",
     r"failed budget_cap overrun-row job=dj-[0-9a-f]{32} run_id=\S+ overrun=[1-9][0-9]*",
     r"^(failed budget_cap overrun-row|no-overrun-row|not-failed) "),
)
STEP_IDS = tuple(e[0] for e in EXPECTED)


class StepFail(Exception):
    """A step's Python check said no: ``raw`` is the line to record (or empty), the message the reason."""

    def __init__(self, why: str, raw: str = "", status: str = "FAIL") -> None:
        super().__init__(why)
        self.raw, self.status = raw, status


@dataclass
class Config:
    profile: HostProfile
    install: str
    repo: str = ""                                     # the guest checkout the contract is filed against
    cred: str = "or-key"
    levain_root: str = LEVAIN_ROOT
    stage_call: tuple[str, ...] = ()
    signing_key: str = ""
    data_root: str = ""
    template: str = "wave3-r0"
    write_path: str = "wave3/hello.txt"
    onto: str = "main"
    goal: str = "Create the file wave3/hello.txt containing exactly one line, hello."
    overrun_goal: str = ""
    agent: str = "claude_code"
    session: str = ""                                  # the planner session word; empty: made fresh per run
    key: str | None = None
    limit_usd: float = 2.00
    min_remaining_usd: float = 0.40
    start_remaining_usd: float | None = None           # what a fresh key has left; None: the limit itself
    tolerance_pct: float = 5.0
    tolerance_floor_microdollars: int = 1000
    step_wait_s: float = 900.0                         # how long a job may take to reach a state
    poll_s: float = 10.0
    settle_wait_s: float = 120.0
    settle_poll_s: float = 5.0


def dispatch_dir(cfg: Config) -> str:
    """``L/dispatch``, the job store the runner and the follower open (``levain.slot.layout.dispatch_store_dir``)."""
    return f"{cfg.profile.store}/dispatch"


def authority_root(cfg: Config) -> str:
    """The run server's authority replica (``dispatch_run``: ``store / "dispatch" / "authority"``)."""
    return f"{dispatch_dir(cfg)}/authority"


def expected_text(cfg: Config) -> str:
    out = [f"wave3 e2e: expected verdicts, os={cfg.profile.name}, cred={cfg.cred}, template={cfg.template}, k={K}",
           "step | name | expected raw line (regex)"]
    for sid, name, pat, _sel in EXPECTED:
        out.append(f"{sid} | {name} | /{pat}/")
    out.append(f"W2 limit: the doctor's limit is ${cfg.limit_usd:.2f} and at least ${cfg.min_remaining_usd:.2f} remains")
    out.append(f"shares: dollars_cents {FIRST_DOLLARS_CENTS} -> {share_microdollars(FIRST_DOLLARS_CENTS)} microdollars per run; "
               f"dollars_cents {OVERRUN_DOLLARS_CENTS} -> {share_microdollars(OVERRUN_DOLLARS_CENTS)}")
    out.append(f"STOPGAP: every dispatch verb runs as {cfg.profile.dispatch_user} with --dispatch-dir {dispatch_dir(cfg)} and "
               f"--data-root {authority_root(cfg)} (one SYNC of the operator's ledger first, data root {cfg.data_root or '<unset>'})")
    out.append(f"RESIDUE: {RESIDUE_HANDOVER}")
    out.append(f"W9 tolerance: |lineage - provider| <= max({cfg.tolerance_pct}% of the provider delta, "
               f"{cfg.tolerance_floor_microdollars} microdollars); each run's settled dollars <= its share")
    return "\n".join(out)


def contract_text(cfg: Config, base: str, dollars_cents: int, goal: str, *, now: datetime | None = None) -> str:
    """The submission a planner files (exactly ``levain.dispatch.contract.SUBMISSION_KEYS``)."""
    when = (now or datetime.now(timezone.utc)) + timedelta(hours=4)
    return json.dumps({"template": cfg.template, "goal": goal, "read": [], "write": [cfg.write_path], "base": base,
                       "budget": {"tool_calls": 40, "tokens": 200_000, "dollars_cents": dollars_cents},
                       "grant": ["Write", "Edit"], "expires_at": when.strftime(STAMP_FORMAT), "repo": cfg.repo},
                      sort_keys=True)


def _json_in(text: str) -> object:
    """The JSON document an answer ends with: the last line-start ``{`` or ``[`` that decodes (the views a verb prints beside it
    on the merged stream are text, and ``--json`` indents, so a nested value never starts a line)."""
    for m in reversed(list(re.finditer(r"(?m)^[\[{]", text))):
        try:
            return json.JSONDecoder().raw_decode(text[m.start():])[0]
        except ValueError:
            continue
    raise ValueError("no JSON document in the answer")


def run_wave3(guest, stdin_run, evidence_dir, cfg: Config, *, fetch=default_fetch,
              sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
    """Run W1-W10 through ``guest`` (``guest(argv, timeout) -> stdout``) and ``stdin_run`` (``(argv, stdin_text, timeout) ->
    stdout``), the provider through ``fetch``. Returns ``(status, [StepResult], result_path)``. The credential is removed in a
    ``finally``; every line written to the evidence dir has the key scrubbed from it."""
    if not CRED_NAME_RE.fullmatch(cfg.cred):
        raise RunbookError(f"credential name {cfg.cred!r} is not [a-z0-9-]{{1,48}}")
    for _sid, _n, pat, _sel in EXPECTED:
        Verdict(None, pat).check()
    if not (math.isfinite(cfg.tolerance_pct) and cfg.tolerance_pct >= 0):
        raise RunbookError(f"--tolerance-pct must be a finite number >= 0, not {cfg.tolerance_pct!r}")
    session = cfg.session or "wave3-" + secrets.token_hex(6)
    ev = Evidence(evidence_dir)
    hidden = [cfg.key] if cfg.key else []

    def scrub(text: str) -> str:
        for k in hidden:
            text = text.replace(k, "***")
        return text

    digest = ev.write_expected(expected_text(cfg))
    verdicts = {sid: (Verdict(None, pat), sel) for sid, _n, pat, sel in EXPECTED}
    names = {sid: name for sid, name, *_ in EXPECTED}
    results: dict[str, StepResult] = {}

    def done(sid: str, status: str, raw: str = "", detail: str = "") -> None:
        r = StepResult(sid, names[sid], status, scrub(raw), f"/{verdicts[sid][0].pattern}/", scrub(detail))
        results[sid] = r
        ev.log(r.line())

    def judged(sid: str, raw: str) -> bool:
        v, sel = verdicts[sid]
        ok, got = judge([raw], 0, v, sel)
        done(sid, "PASS" if ok else "FAIL", got or raw, "" if ok else "the check's line does not fit the verdict")
        return ok

    def step(sid: str, body: Callable[[], str]) -> bool:
        """Run one step's body (it returns the raw line, or raises): a transport loss is ERROR, a refusal or a failed check FAIL."""
        try:
            raw = body()
        except StepFail as e:
            done(sid, e.status, e.raw, str(e))
            return False
        except GuestError as e:
            done(sid, "ERROR" if e.rc == 255 else "FAIL", detail=f"guest: {e}")
            return False
        except FetchError as e:
            done(sid, "ERROR", detail=f"{type(e).__name__}: {e}")
            return False
        except (RunbookError, OSError, ValueError, KeyError, TypeError) as e:
            done(sid, "FAIL", detail=f"{type(e).__name__}: {e}")
            return False
        except subprocess.SubprocessError as e:
            done(sid, "ERROR", detail=f"{type(e).__name__}: {e}")
            return False
        return judged(sid, raw)

    def unbound(sid: str, why: str) -> None:
        done(sid, "UNBOUND", detail=why)

    # ---- the guest's verbs --------------------------------------------------------------------------------------------
    sel_planner = ("--agent", cfg.agent, "--session", session)
    sel_sign = ("--signing-key", cfg.signing_key) if cfg.signing_key else ()

    def dispatch_argv(verb: Sequence[str]) -> tuple[str, ...]:
        """A levain verb as the STOPGAP runs it (np-667d731c (b)): as the dispatch account, against ``L/dispatch``, verified
        against the run server's authority replica. ``file``, ``sign`` and ``authorise`` also name the data root."""
        extra = ("--dispatch-dir", dispatch_dir(cfg))
        if verb[:2] in (("dispatch", "file"), ("dispatch", "sign"), ("dispatch", "authorise")):
            extra += ("--data-root", authority_root(cfg))
        return ("sudo", "-n", "-u", cfg.profile.dispatch_user, "/usr/bin/env", f"HOME={dispatch_dir(cfg)}", *levain_argv(cfg.profile, (*verb, *extra), cfg.levain_root))

    def operator(*verb: str, stdin: str | None = None, timeout: float = 300) -> tuple[str, int]:
        """A levain verb as the stopgap runs it: (merged output, exit code). A refusal (exit 2) is data, not an error."""
        argv = dispatch_argv(verb)
        if stdin is None:
            lines, rc = capture(guest, argv, timeout)
            return "\n".join(lines), rc
        try:
            return stdin_run(argv, stdin, timeout), 0
        except GuestError as e:
            if e.rc == 255:
                raise
            return str(e), e.rc

    def show(job: str) -> dict:
        out, rc = operator("dispatch", "show", job, "--json")
        if rc != 0:
            raise StepFail(f"dispatch show exited {rc}: {out[-160:]}")
        view = _json_in(out)
        if not isinstance(view, dict) or view.get("id") != job:
            raise StepFail("dispatch show answered another job or no job")
        return view

    def last_cause(view: dict) -> str:
        hist = view.get("history") or []
        return str(hist[-1].get("cause")) if hist else "?"

    def wait_state(job: str, want: str, *, ok_via: Sequence[str] = ()) -> dict:
        """Poll the job until it is ``want`` (or already past it by ``ok_via``); a terminal failure or the deadline is a FAIL."""
        deadline = clock() + cfg.step_wait_s
        while True:
            view = show(job)
            state = view.get("state")
            if state == want:
                return view
            if state in ok_via:
                return view
            if state in ("failed", "cancelled"):
                raise StepFail(f"the job is {state} (cause {last_cause(view)}) before it reached {want}",
                               f"not-{want} {job} state={state} cause={last_cause(view)}")
            if clock() >= deadline:
                raise StepFail(f"the job is still {state} after {cfg.step_wait_s:.0f}s (wanted {want})",
                               f"not-{want} {job} state={state}")
            sleep(cfg.poll_s)

    def dispatch_user_argv() -> tuple[str, ...]:
        return ("sudo", "-n", "-u", cfg.profile.dispatch_user, "/usr/bin/env", "PATH=/usr/bin:/bin", f"HOME={dispatch_dir(cfg)}", cfg.profile.python, "-I", "-")

    def tickets(job: str) -> list[dict]:
        sock = f"{cfg.profile.run_dir}/control/control.sock"
        out = stdin_run((*dispatch_user_argv(), cfg.levain_root, sock, job, str(K)), READ_TICKETS, 60)
        facts = _json_in(out)
        if not isinstance(facts, list):
            raise StepFail("the ticket reader answered something other than a list")
        return facts

    def records(job: str) -> list[dict]:
        out = stdin_run((*dispatch_user_argv(), cfg.levain_root, job, dispatch_dir(cfg)), READ_RECORDS, 60)
        rows = _json_in(out)
        if not isinstance(rows, list):
            raise StepFail("the run-record reader answered something other than a list")
        return rows

    def base_commit() -> str:
        lines, rc = capture(guest, ("git", "-C", cfg.repo, "rev-parse", cfg.onto), 60)
        if rc != 0 or not lines or not re.fullmatch(r"[0-9a-f]{40}", lines[-1].strip()):
            raise StepFail(f"git rev-parse {cfg.onto} in {cfg.repo} gave no commit (rc {rc})")
        return lines[-1].strip()

    def file_and_sign(goal: str, dollars_cents: int) -> tuple[str, str]:
        """File a contract and read the answer; returns (job id, digest). Raises StepFail naming the refusal."""
        text = contract_text(cfg, base_commit(), dollars_cents, goal)
        out, rc = operator("dispatch", "file", "-", "--json", *sel_planner, stdin=text)
        if rc != 0:
            raise StepFail(f"dispatch file exited {rc}: {out[-200:]}")
        ans = _json_in(out)
        if (not isinstance(ans, dict) or not isinstance(ans.get("id"), str) or not JOB_RE.fullmatch(ans["id"])
                or not isinstance(ans.get("digest"), str) or not DIGEST_RE.fullmatch(ans["digest"]) or ans.get("state") != "filed"):
            raise StepFail("dispatch file answered no filed job with a digest")
        return ans["id"], ans["digest"]

    def sign(job: str) -> str:
        out, rc = operator("dispatch", "sign", job, "--yes", "--json", *sel_sign)
        if rc != 0:
            raise StepFail(f"dispatch sign exited {rc}: {out[-200:]}")
        ans = _json_in(out)
        if not isinstance(ans, dict) or ans.get("id") != job or ans.get("state") != "signed" or not ans.get("signer_fp"):
            raise StepFail("dispatch sign did not answer a signed job with a signer")
        return str(ans["signer_fp"])

    # ---- credential state (threec10's rules, one credential) --------------------------------------------------------
    cred = {"state": "none", "why": ""}     # none | confirmed | uncertain

    def install_key() -> None:
        argv = ("sudo", "-n", "/bin/sh", "-c", INSTALL_SCRIPT, "sh", cfg.profile.store, cfg.cred, "new")
        try:
            out = stdin_run(argv, credential_json(cfg.cred, cfg.key or ""), 60)
        except (GuestError, OSError, subprocess.SubprocessError) as e:
            cred.update(state="uncertain", why=f"W1: no answer from the install script ({type(e).__name__}: {e})")
            raise StepFail(f"transport: {e}; install result UNCERTAIN, {cfg.cred}.json will not be touched", status="ERROR") from None
        lines = out.splitlines()
        refusal = next((ln for ln in lines if ln.startswith("cred-refused ")), None)
        if refusal:
            raise StepFail(refusal, status="REFUSED")
        if f"cred-installed {cfg.cred}" not in lines:
            cred.update(state="uncertain", why="W1: the install script printed neither cred-installed nor cred-refused")
            raise StepFail("no cred-installed line: install result UNCERTAIN", status="ERROR")
        cred["state"] = "confirmed"

    def cleanup() -> list[str]:
        if cred["state"] == "none":
            return []
        if cred["state"] == "uncertain":
            p = [f"{cred['why']}; {cfg.profile.store}/keys/cred/{cfg.cred}.json may be this run's or his: check it by hand, "
                 f"it was NOT touched"]
            ev.log(scrub("cleanup: " + p[0]))
            return p
        base = ("sudo", "-n", "/bin/sh", "-c")
        tail = ("sh", cfg.profile.store, cfg.cred)
        problems: list[str] = []
        try:
            out = stdin_run(base + (REMOVE_SCRIPT,) + tail, "", 60)
            if f"cred-removed {cfg.cred}" not in out.splitlines():
                problems.append(f"removal of {cfg.cred} printed no cred-removed line")
            seen = stdin_run(base + (VERIFY_SCRIPT,) + tail + ("all",), "", 60)
            if f"cred-absent {cfg.cred}" not in seen.splitlines():
                problems.append(f"{cfg.cred} is STILL on the guest after removal ({seen.strip()[:80]})")
        except (GuestError, OSError, subprocess.SubprocessError) as e:
            problems.append(f"credential {cfg.cred} NOT confirmed removed ({type(e).__name__}: {e}); remove "
                            f"{cfg.profile.store}/keys/cred/{cfg.cred}.json* by hand")
        for p in problems:
            ev.log(scrub("cleanup: " + p))
        if not problems:
            ev.log(f"cleanup: credential {cfg.cred} removed and confirmed absent")
        return problems

    # ---- the steps ----------------------------------------------------------------------------------------------------
    state: dict = {"synced": False, "u0": None, "job": None, "digest": None, "view": None, "tickets": None}

    def w1() -> str:
        if not cfg.key:
            raise StepFail("the key is not in the environment", status="UNBOUND")
        if not cfg.stage_call:
            raise StepFail("no --stage-call: the guest command that stages the route map, template and verifier is the sitting's to name",
                           status="UNBOUND")
        install_key()
        lines, rc = capture(guest, cfg.stage_call, 600)
        line = next((ln.strip() for ln in lines if ln.startswith("staged gen=")), "")
        m = re.fullmatch(r"staged gen=([0-9]+)", line)
        if rc != 0 or not m or int(m.group(1)) < 1:
            raise StepFail(f"the stage call exited {rc} and printed no 'staged gen=<n>' (last: {lines[-1:]!r})", line)
        return line

    def w2() -> str:
        argv = ("sudo", "-n", "/usr/bin/env", f"PYTHONPATH={cfg.levain_root}", cfg.profile.python, "-m", "levain", "doctor",
                "--path", cfg.install)
        lines, _rc = capture(guest, argv, 180)           # doctor exits 1 on unrelated rows: the verdict ignores rc
        row = next((ln for ln in lines if re.search(r"invoice ceiling \(" + re.escape(cfg.cred), ln)), "")
        m = re.search(r"key limit (" + USD + r")( total)?, (" + USD + r") remaining(; BYOK usage is not counted against this limit)?",
                      row)
        if not m:
            raise StepFail("the invoice-ceiling row for the credential has no 'key limit $X total, $Y remaining'", row.strip())
        limit, remaining = (float(g.replace("$", "").replace(",", "")) for g in (m.group(1), m.group(3)))
        if not m.group(2):
            # No "total": the doctor words a row this way only when it warns. The one such row taken is a fresh key whose
            # only caveat is BYOK, at exactly the expected limit and the expected remainder; reset, exhausted or any other
            # caveat, or other figures, fail.
            start = cfg.limit_usd if cfg.start_remaining_usd is None else cfg.start_remaining_usd
            if not (m.group(4) and abs(limit - cfg.limit_usd) <= 0.005 and abs(remaining - start) <= 0.005):
                raise StepFail(f"the row is not a total limit of ${cfg.limit_usd:.2f}: it words a warning, or other figures",
                               row.strip())
        ps, _ = capture(guest, (cfg.profile.ps_bin, "-Ao", "pid,command"), 60)
        pid = next((ln.split()[0] for ln in ps if RUNNER_MODULE in ln and "grep" not in ln and ln.split()[0].isdigit()), None)
        raw = f"key limit ${limit:.2f}, remaining ${remaining:.2f}, " + (f"runner up pid={pid}" if pid else "runner DOWN")
        if abs(limit - cfg.limit_usd) > 0.005:
            raise StepFail(f"the key's limit is ${limit:.2f}, the sitting's is ${cfg.limit_usd:.2f}", raw)
        if remaining < cfg.min_remaining_usd:
            raise StepFail(f"${remaining:.2f} remains, under the ${cfg.min_remaining_usd:.2f} the run needs", raw)
        if not pid:
            raise StepFail(f"no process runs {RUNNER_MODULE}", raw)
        state["u0"] = usage_microdollars(fetch, cfg.key or "")        # the provider's books before any job spends
        return raw

    def sync_authority() -> None:
        """The stopgap's one SYNC, as the operator, before the first signature is checked by the dispatch account."""
        if not cfg.data_root:
            raise StepFail("no --data-root: the operator's cockpit data root is what the one SYNC feeds the replica from",
                           status="UNBOUND")
        out = stdin_run((cfg.profile.python, "-I", "-", cfg.levain_root, cfg.data_root), SYNC_AUTHORITY, 120)
        if not re.search(r"(?m)^synced head=[0-9]+$", out):
            raise StepFail(f"the one SYNC did not land: {out.strip()[-200:]}")
        state["synced"] = True

    def w3() -> str:
        sync_authority()
        job, dig = file_and_sign_first()
        return f"filed {job} digest={dig}"

    def file_and_sign_first() -> tuple[str, str]:
        job, dig = file_and_sign(cfg.goal, FIRST_DOLLARS_CENTS)
        state.update(job=job, digest=dig)
        return job, dig

    def w4() -> str:
        return f"signed {state['job']} as {sign(state['job'])}"

    def w5() -> str:
        job = state["job"]
        view = wait_state(job, "running")
        facts = tickets(job)
        share = share_microdollars(FIRST_DOLLARS_CENTS)
        bad = [f for f in facts if "error" in f]
        if bad or len(facts) != K:
            raise StepFail(f"CONTROL answered no ticket for every run: {bad[:1] or len(facts)}")
        idx = [f.get("run_index") for f in facts]
        res = [f.get("reserved_microdollars") for f in facts]
        ok = all(f.get("job_id") == job and f.get("contract_digest") == view.get("digest") == state["digest"] for f in facts)
        raw = f"running {job} runs={len(facts)} reserved={','.join(map(str, res))} index={','.join(map(str, idx))} " \
              f"digest={'ok' if ok else 'MISMATCH'}"
        if res != [share] * K or idx != list(range(K)):
            raise StepFail(f"the tickets do not carry the share {share} per run and run index 0..{K - 1}", raw)
        if not ok:
            raise StepFail("a ticket's job id or contract digest is not the job's", raw)
        state["tickets"] = facts
        return raw

    def w6() -> str:
        view = wait_state(state["job"], "verifying", ok_via=("verified",))
        hist = view.get("history") or []
        cause = next((h.get("cause") for h in reversed(hist) if h.get("to_state") == "verifying"), None)
        if cause != "runs_exited":
            raise StepFail(f"the job reached verifying by cause {cause!r}, not runs_exited", f"verifying {state['job']} cause={cause} runs={K}")
        return f"verifying {state['job']} cause={cause} runs={K}"

    def w7() -> str:
        job = state["job"]
        wait_state(job, "verified")
        rows = records(job)
        models = [r.get("model") or {} for r in rows]
        src = [m.get("identity_source") for m in models]
        ids = {m.get("backend_identity") for m in models}
        raw = f"verified {job} runs={len(rows)} source={','.join(map(str, src))} identities={len(ids)}"
        if len(rows) != K or src != ["routemap"] * K or len(ids) != K:
            raise StepFail(f"want {K} run records, every identity_source routemap, {K} distinct backend identities", raw)
        return raw

    def w8() -> str:
        job = state["job"]
        out, rc = operator("dispatch", "recommend", job, "--onto", cfg.onto, "--run", "0", "--json", *sel_planner)
        if rc != 0:
            raise StepFail(f"dispatch recommend exited {rc}: {out[-200:]}")
        out, rc = operator("dispatch", "authorise", job, "--run", "0", "--onto", cfg.onto, "--yes", "--json", *sel_sign)
        if rc != 0:
            raise StepFail(f"dispatch authorise exited {rc}: {out[-200:]}")
        land = _json_in(out)
        if not isinstance(land, dict) or land.get("outcome") != "adopted" or land.get("state") != "adopted":
            raise StepFail(f"the land did not adopt: {(land or {}) if isinstance(land, dict) else out[-160:]}",
                           f"not-adopted {job} outcome={land.get('outcome') if isinstance(land, dict) else '?'}")
        view = show(job)
        write = {e["path"] for e in view.get("write") or []}
        lines, rc = capture(guest, ("git", "-C", cfg.repo, "diff", "--name-only", view["base"], cfg.onto), 60)
        if rc != 0:
            raise StepFail(f"git diff in {cfg.repo} exited {rc}")
        changed = [ln.strip() for ln in lines if ln.strip()]
        outside = [p for p in changed if p not in write]
        raw = f"adopted {job} onto={cfg.onto} files={len(changed)} outside={len(outside)}"
        if not changed or outside:
            raise StepFail(f"the landed diff is {changed} against the write set {sorted(write)}", raw)
        return raw

    def w9() -> str:
        job = state["job"]
        share = share_microdollars(FIRST_DOLLARS_CENTS)
        u0 = state["u0"]
        deadline = clock() + cfg.settle_wait_s
        while True:     # both books lag the runs: read them together until they have settled all k runs and the provider has caught up
            rows = [r for r in lineage_rows(guest, cfg.profile) if r.get("job_id") == job]
            settled = [r for r in rows if type(r.get("cost_microdollars")) is int and r.get("model_answered") is not None]
            lin = sum(r["cost_microdollars"] for r in settled)
            per_run: dict = {}
            for r in settled:
                per_run[r.get("run_index")] = per_run.get(r.get("run_index"), 0) + r["cost_microdollars"]
            u1 = usage_microdollars(fetch, cfg.key or "")
            if (len(per_run) >= K and u1 != u0 and u1 - u0 >= lin) or clock() >= deadline:
                break
            sleep(cfg.settle_poll_s)
        prov = u1 - u0
        tol = max(cfg.tolerance_pct / 100.0 * prov, cfg.tolerance_floor_microdollars)
        top = max(per_run.values(), default=0)
        if not settled:
            word = "NOCALL"
        elif u1 == u0 or prov <= 0:
            word = "NOMOVE"
        elif top > share:
            word = "OVERSHARE"
        else:
            word = "AGREE" if abs(lin - prov) <= tol else "DISAGREE"
        raw = (f"{word} lineage_microdollars={lin} provider_delta_microdollars={prov} runs={len(per_run)} "
               f"max_run_microdollars={top} share_microdollars={share} tolerance={int(tol)}")
        if word != "AGREE":
            raise StepFail(f"lineage against the provider: {word}", raw)
        return raw

    def w10() -> str:
        if not cfg.overrun_goal:
            raise StepFail("no --overrun-goal: a goal on which one run spends more than its share is the sitting's to name",
                           status="UNBOUND")
        if not state["synced"]:
            raise StepFail("the one SYNC (W3) did not land: the dispatch account cannot verify the operator's signature", status="UNBOUND")
        before = len(lineage_rows(guest, cfg.profile))
        job, _dig = file_and_sign(cfg.overrun_goal, OVERRUN_DOLLARS_CENTS)
        sign(job)
        try:
            view = wait_state(job, "failed")
        except StepFail as e:
            raise StepFail(str(e), f"not-failed {job}") from None
        cause = last_cause(view)
        new = lineage_rows(guest, cfg.profile)[before:]
        # an overrun of 0 is a normal settle, not a hit: only a positive overrun is the one budget_cap names
        hits = [r for r in new if r.get("job_id") == job and type(r.get("overrun")) is int and r["overrun"] > 0]
        if cause != "budget_cap":
            raise StepFail(f"the job failed by cause {cause!r}, not budget_cap", f"not-budget_cap {job} cause={cause}")
        if not hits:
            raise StepFail("no lineage row of the job carries an overrun", f"no-overrun-row job={job} rows={len(new)}")
        return f"failed {cause} overrun-row job={job} run_id={hits[0].get('run_id')} overrun={hits[0]['overrun']}"

    try:
        ok1 = step("W1", w1)
        if ok1:
            if step("W2", w2):
                if step("W3", w3) and step("W4", w4) and step("W5", w5) and step("W6", w6) and step("W7", w7) and step("W8", w8):
                    step("W9", w9)
                else:
                    for sid in STEP_IDS[:9]:
                        if sid not in results:
                            unbound(sid, "an earlier step of the first job did not pass")
                step("W10", w10)
            else:
                for sid in STEP_IDS[2:]:
                    unbound(sid, "W2 did not pass: the key, its remainder or the runner is not as the sitting needs")
        else:
            for sid in STEP_IDS[1:]:
                unbound(sid, "W1 did not pass")
    finally:
        problems = cleanup()
    ordered = [results[sid] for sid in STEP_IDS]
    status = overall([r.status for r in ordered])
    if problems and status == "PASS":
        status = "INCOMPLETE"       # a live credential left behind never sits beside a PASS
    body = [f"RESULT: {status}", f"wave3 e2e, os={cfg.profile.name}, expected-table sha256 {digest} (EXPECTED.txt, written before the first step)"]
    body += [f"RESIDUE: {RESIDUE_HANDOVER}"]
    body += [r.line() for r in ordered]
    body += [scrub(f"CLEANUP: {p}") for p in problems] or ["CLEANUP: ok"]
    return status, ordered, ev.write_result("\n".join(body) + "\n")


def main(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("vm")
    ap.add_argument("evidence")
    ap.add_argument("--install", required=True, help="the install dir `levain doctor --path` checks")
    ap.add_argument("--repo", required=True, help="the guest checkout the contract is filed against")
    ap.add_argument("--profile", default=DARWIN.name, choices=sorted(PROFILES))
    ap.add_argument("--cred", default="or-key")
    ap.add_argument("--levain-root", default=LEVAIN_ROOT)
    ap.add_argument("--stage-call", default="", help="the guest command (one quoted string) that stages and prints 'staged gen=<n>'")
    ap.add_argument("--signing-key", default="", help="the guest path of the enrolled key `dispatch sign` and `authorise` use")
    ap.add_argument("--data-root", default="")
    ap.add_argument("--template", default="wave3-r0")
    ap.add_argument("--write-path", default="wave3/hello.txt")
    ap.add_argument("--onto", default="main")
    ap.add_argument("--goal", default=Config.goal)
    ap.add_argument("--overrun-goal", default="")
    ap.add_argument("--agent", default="claude_code")
    ap.add_argument("--tolerance-pct", type=float, default=5.0)
    a = ap.parse_args(argv)
    profile = profile_named(a.profile)
    cfg = Config(profile, a.install, a.repo, a.cred, a.levain_root, tuple(shlex.split(a.stage_call)), a.signing_key, a.data_root,
                 a.template, a.write_path, a.onto, a.goal, a.overrun_goal, a.agent, "", os.environ.get(KEY_ENV),
                 tolerance_pct=a.tolerance_pct)
    from scripts.vm_harness import vm_run
    from scripts.vm_harness.driver import AdapterError, VmAdapters
    from scripts.vm_harness.host_profile import ProfileMismatch
    from scripts.vm_harness.runbook import make_stdin_runner

    vm_run._clone_guard(a.vm)
    with tempfile.TemporaryDirectory() as td:
        guest, spawn = vm_run.make_transport(a.vm, profile, Path(td))
        try:
            VmAdapters(guest, spawn, profile=profile).verify_host()
        except (ProfileMismatch, AdapterError) as e:
            print(f"wave3_e2e: refusing: {e}", file=sys.stderr)
            return 2
        try:
            stdin_run = make_stdin_runner(a.vm, profile, Path(td), guest.kind)
            status, _steps, path = run_wave3(guest, stdin_run, a.evidence, cfg)
        except RunbookError as e:
            print(f"wave3_e2e: refusing: {e}", file=sys.stderr)
            return 2
    print(path.read_text())
    return {"PASS": 0, "FAIL": 1, "REFUSED": 2}.get(status, 3)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
