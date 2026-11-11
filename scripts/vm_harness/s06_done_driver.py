"""S06 (VM sitting, B5): ONE ``done`` agent occurrence written by the PRODUCT from a real vendor exit. Guest side: runs under
the guest python as the guest OPERATOR (not root, not the dispatch user). usage, in the guest, from a booted ``vend-*`` clone:
  <guest python> -I <tools dir>/s06_done_driver.py --workspace-repo /path/of/the/stager-test-repo --evidence <fresh dir> \\
        --vendor-ready
(copied into the clone next to ``host_profile.py`` and ``runbook.py``, the same staging as ``guest_dispatch.py``; the guest
also needs the ``tests`` tree of the pinned sha and a pip dir with pytest + cryptography, exactly the 11-07 setup.)

What it does, in the order it does it: read the facts the run needs (:class:`Facts`) -> write ``EXPECTED.txt`` with its sha256
(the one verdict, before anything is fired) -> REFUSE, in words, if a precondition is absent -> build the occurrence with the
repo's own test helpers (a real ssh-keygen world, a real signed authority log, a real agent store; a pong role; the spec's win
check named ``session_completed``; an install file naming the production launcher and the workspace repo) -> fire it in a REAL
process (``controller._fire``: the identity check reads the occurrence id off the command line) -> read it back with the
product's own reader (``levain agents show <agent> --json``) -> judge ONE line.

(``session_key`` is NOT read: ``agents show --json`` does not expose that column, so the page's "session_key not null"
cannot be had through the product's reader; ``launched_at`` stands in for "the row really launched".)

What a PASS proves, exactly: the product's ``Supervisor.finish`` wrote ``done``/``completed`` after a real vendor session
exited 0 and its END was proven, with the production verify chain. It does NOT read the vendor's reply ("pong"): that is in the
retained session tree, not on this path. The 11-07 ``done`` was the test helper ``Rig.finish`` (a hand UPDATE); this is not.

Preconditions the code REFUSES on (never a silent pass): a dispatch run socket (a booted guest with the dispatch service), the
operator uid (not root: ``ControlStore`` wants the running uid to own its dir), ``--vendor-ready`` (S03 login and S05's real
passthrough start passed: this starts a REAL vendor session on the subscription), the signed provider policy readable
(S07's policy pack: the install-launcher path stages the workspace through the stager, B7), the workspace repo resolving to a
commit, the ``tests`` tree importable, and no earlier fire world (the dispatch server's replica of its authority would be stale).
Exit codes as the other runners: 0 PASS, 1 FAIL, 2 REFUSED, 3 INCOMPLETE.
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

if __package__:
    from scripts.vm_harness.host_profile import HostProfile, pick_levain_root, profile_for_platform
    from scripts.vm_harness.runbook import Evidence, RunbookError, Verdict, judge
else:   # run as a script in the clone, next to host_profile.py / runbook.py (python -I leaves the script's own dir off sys.path)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from host_profile import HostProfile, pick_levain_root, profile_for_platform
    from runbook import Evidence, RunbookError, Verdict, judge

PONG_BRIEF = "Reply with exactly the word: pong. Use no tools."
SPEC_NAME = "job_feeder_downstream"           # the fixture spec the test helpers register (its win check is overridden below)
ROLE_NAME = "model-role"                      # the fixture role: executor session, harness claude
OCCURRENCE_NAME = "fire-s06"
PYLIB = "/tmp/pylib"                          # pytest + cryptography, pip-installed in the guest (the 11-07 setup)
FIRE_NO_IDENTITY = 4                          # levain.agents.controller.FIRE_NO_IDENTITY (the fire cannot read its own identity)
GATE_UNPROVEN = 76                            # levain.agents.controller.GATE_UNPROVEN (END not proven: the row stays launched)

#: What a PASS line is. ONE pattern per episodes-setting (``episode`` is ``1`` when the install names an episodic store that
#: the guest python can create, else ``na``: decided BEFORE the expected table is written, never "or").
RAW_TEMPLATE = ("S06 occurrences=1 done=1 state=done reason=completed ended_at=set launched_at=set episode={episode} fire_rc=0")


def verdict_for(*, episodes: bool) -> Verdict:
    return Verdict(0, RAW_TEMPLATE.format(episode="1" if episodes else "na"))


# ---- the pure parts: what the read-back says, and what to make of it -----------------------------------------------------

def judge_show(doc: Mapping[str, Any], oid: str, fire_rc: int, *, episodes: bool) -> str:
    """The ONE raw line the verdict is checked against, from ``agents show --json``'s document (``occurrences``,
    ``episodes``) and the fire's exit code. A count, not a presence: two occurrences is not ONE ``done``."""
    occs = [o for o in (doc.get("occurrences") or []) if isinstance(o, Mapping)]
    mine = next((o for o in occs if o.get("occurrence_id") == oid), {})
    done = sum(1 for o in occs if o.get("state") == "done")
    if episodes:
        eps = sum(1 for e in (doc.get("episodes") or []) if isinstance(e, Mapping) and oid in (e.get("content") or "")
                  and "ended done: completed" in (e.get("content") or ""))
        episode = str(eps)
    else:
        episode = "na"
    return (f"S06 occurrences={len(occs)} done={done} state={mine.get('state')} reason={mine.get('reason')} "
            f"ended_at={'set' if mine.get('ended_at') else 'null'} launched_at={'set' if mine.get('launched_at') else 'null'} "
            f"episode={episode} fire_rc={fire_rc}")


def diagnose(state: str | None, reason: str | None, ended_at: str | None, fire_rc: int) -> str:
    """The cause a non-pass names, read from code (the page's pre-written failure expectations), so a FAIL is read right."""
    if fire_rc == FIRE_NO_IDENTITY:
        return "FIRE_NO_IDENTITY: the fire could not read its identity: it was not a real process naming its occurrence (harness)"
    if state == "launched" and not ended_at:
        return (f"END not proven (GATE_UNPROVEN {GATE_UNPROVEN}) or the fire died: a product-correct hold, the row stays "
                "launched; read the S1 END result, not a product defect")
    if state == "failed" and (reason or "").startswith("win_unverified"):
        return "failed/win_unverified: the session_completed win override is missing from the spec (harness)"
    if state == "failed" and (reason or "").startswith("session_refused"):
        return f"a launch refusal named by the product ({reason}): read the refusal name"
    if state == "failed" and (reason or "").startswith("session_exit_"):
        return f"the vendor session exited nonzero or died by signal ({reason}): the vendor, not the harness"
    return f"unexpected row: state={state} reason={reason} ended_at={ended_at} fire_rc={fire_rc}"


def expected_text(cfg: "S06Config", *, episodes: bool) -> str:
    v = verdict_for(episodes=episodes)
    return "\n".join([
        f"S06: expected verdict, os={cfg.profile.name}",
        "ONE occurrence of the pong role, written done by the product from a real vendor exit (no hand UPDATE, no rig.finish):",
        f"  raw line must fullmatch /{v.pattern}/ and the fire process must exit {v.rc}",
        "  episode=1: one episode 'session run <oid> of agent <agent> ended done: completed' " if episodes else
        "  episode=na: no episodic store (anneal_memory is not importable in the guest): the episode clause is not asked",
        "reading a non-pass: launched/ended_at null = END not proven (GATE_UNPROVEN 76), a product-correct hold; failed/"
        "win_unverified = the win override missing (harness); failed/session_refused:* = a launch refusal the product named; "
        "failed/session_exit_N = the vendor; fire_rc 4 = FIRE_NO_IDENTITY (not a real process)",
        "NOT read back: the vendor's reply text (pong); it is in the retained session tree",
    ])


# ---- facts and preconditions -----------------------------------------------------------------------------------------------

@dataclass
class Facts:
    run_socket: bool
    euid: int
    vendor_ready: bool
    policy_ok: bool
    repo_ok: bool
    tests_importable: bool
    anneal_importable: bool
    world_fresh: bool = True
    details: dict = field(default_factory=dict)


def preconditions(f: Facts) -> list[str]:
    """Every precondition that is absent, in words. Empty: the run may fire."""
    out = []
    if not f.run_socket:
        out.append("refusing: no dispatch run socket here: this is not a booted guest with the dispatch service (S01 install and "
                   "S05 first) " + f.details.get("run_socket", ""))
    if f.euid == 0:
        out.append("refusing: running as root: this must run as the guest operator (ControlStore wants the running uid to own "
                   "its directory, and the fire is the operator's)")
    if not f.vendor_ready:
        out.append("refusing: --vendor-ready not given: this starts a REAL vendor session on the subscription; run it only "
                   "after S03 (login, slot marked dispatch) and S05 (the real passthrough start) passed on this clone")
    if not f.policy_ok:
        out.append("refusing: the signed provider policy cannot be read here (workspace-policy-unavailable): the install-launcher "
                   "path stages the workspace through the stager, which needs it; install S07's policy pack first "
                   + f.details.get("policy", ""))
    if not f.repo_ok:
        out.append("refusing: the workspace repo does not resolve to a commit: " + f.details.get("repo", "check --workspace-repo"))
    if not f.tests_importable:
        out.append("refusing: the tests tree of the pinned sha is not importable here (extract it next to levain/, and pip "
                   "install pytest + cryptography into the pip dir)")
    if not f.world_fresh:
        out.append("refusing: a fire world already exists here: the dispatch server's replica of its authority is stale; reset it "
                   "(remove <store>/dispatch/authority, restart the dispatch service) and remove the old world dir first "
                   + f.details.get("world", ""))
    return out


@dataclass
class S06Config:
    workspace_repo: str
    evidence: Path
    vendor_ready: bool = False
    profile: HostProfile = field(default_factory=lambda: profile_for_platform(sys.platform))
    fire_timeout_s: float = 900.0
    episodes: bool = False

    @property
    def world(self) -> Path:
        return Path(self.profile.tools_dir) / "world"


def _stage_paths(profile: HostProfile) -> str | None:
    """Put the pip dir and the staged levain tree on ``sys.path`` (a ``-I`` run has neither). Returns the levain root."""
    root = pick_levain_root(profile)
    for p in (PYLIB, root):
        if p and p not in sys.path:
            sys.path.insert(0, p)
    return root


def read_facts(cfg: S06Config) -> Facts:
    """The real probes. Off a guest (no dispatch run socket) nothing else is probed: no policy store is read."""
    sock = Path(cfg.profile.run_dir) / "dispatch" / "run.sock"
    details: dict[str, str] = {}
    try:
        has_sock = stat.S_ISSOCK(os.stat(sock).st_mode)
    except OSError:
        has_sock = False
    anneal = importlib.util.find_spec("anneal_memory") is not None
    world_fresh = not cfg.world.exists()
    if not world_fresh:
        details["world"] = f"({cfg.world})"
    facts = Facts(run_socket=has_sock, euid=os.geteuid(), vendor_ready=cfg.vendor_ready, policy_ok=False, repo_ok=False,
                  tests_importable=False, anneal_importable=anneal, world_fresh=world_fresh, details=details)
    if not has_sock:
        details["run_socket"] = f"({sock})"
        details["policy"] = "(not probed: no guest)"
        return facts
    _stage_paths(cfg.profile)
    try:
        import tests.test_agents_launch  # noqa: F401
        facts.tests_importable = True
    except Exception as exc:  # noqa: BLE001
        details["tests"] = f"{type(exc).__name__}"
    try:
        from levain.dispatch import stager
        stager._control_policy()
        facts.policy_ok = True
        stager.resolve_head(cfg.workspace_repo)
        facts.repo_ok = True
    except Exception as exc:  # noqa: BLE001
        which = "repo" if facts.policy_ok else "policy"
        details[which] = f"({type(exc).__name__}: {exc})"[:200]
    return facts


# ---- building and firing the occurrence ------------------------------------------------------------------------------------

def pong_role() -> dict:
    from tests.test_agents_launch import ROLES
    return {**copy.deepcopy(ROLES[ROLE_NAME]), "brief": {"template": PONG_BRIEF, "slots": {}}, "model": "haiku", "harness": "claude"}


def prepare(rig: Any, workspace_repo: str, episodic_path: Path | str | None, *, launcher: str | None = None) -> tuple[str, str]:
    """``(occurrence id, agent id)`` of ONE launched occurrence of the pong role on ``rig``, its install written. The win
    check is ``session_completed`` for this occurrence only (the shared fixture spec is put back afterwards): with the
    fixture's ``compute_anomalies`` a clean exit ends ``failed``/``win_unverified``. The pinned role is registered in the
    store (``fire_factory`` refuses a role it cannot find). ``launcher`` is the install's launcher name (default: the
    production client, ``s1-dispatch``)."""
    from levain.agents import session_client as C, session_wire as W
    from levain.agents.role import add_role, parse_role
    from tests import test_agents_session_launch as TL
    from tests.test_agents_launch import SPECS
    role = pong_role()
    spec = SPECS[SPEC_NAME]
    had = "win" in spec
    saved = copy.deepcopy(spec.get("win"))
    spec["win"] = {"kind": "verifier", "check": W.SESSION_WIN_CHECK}
    try:
        oid = TL._occurrence(rig, billing="subscription", role=role, name=OCCURRENCE_NAME)
    finally:
        if had:
            spec["win"] = saved
        else:
            spec.pop("win", None)
    add_role(rig.store, parse_role(role))
    agent = rig.row(oid)["agent_id"]
    if episodic_path is not None:
        from anneal_memory import Store
        Store(Path(episodic_path), audit=False).close()
    W.write_install(rig.store.directory, cockpit_root=rig.world.root, launcher=launcher or C.LAUNCHER_NAME,
                    episodic_path=episodic_path, workspaces={agent: {"repo": workspace_repo}})
    return oid, agent


def fire_command(store_dir: Path | str, oid: str, *, python: str) -> list[str]:
    """The fire as a real process whose command line names the occurrence (``FIRE_NO_IDENTITY`` otherwise)."""
    from levain.agents import controller as ctl
    argv = ctl.fire_argv(store_dir, oid)
    argv[0] = python
    return argv


def show_command(store_dir: Path | str, agent: str, *, python: str) -> list[str]:
    """The product's own reader: ``levain agents show <agent> --json`` (the occurrences and the episodes under the agent)."""
    return [python, "-m", "levain", "agents", "--store-dir", str(store_dir), "show", agent, "--json"]


def real_fire_once(cfg: S06Config) -> tuple[int, str, dict]:
    """Build the world and the occurrence, fire it, read it back. ``(fire rc, occurrence id, the show document)``."""
    profile = cfg.profile
    root = _stage_paths(profile)
    from tests.test_agents_launch import Rig
    cfg.world.mkdir(parents=True, mode=0o700)
    rig = Rig(cfg.world)
    oid, agent = prepare(rig, cfg.workspace_repo, (cfg.world / "episodes.db") if cfg.episodes else None)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in (root, PYLIB) if p)}
    fired = subprocess.run(fire_command(rig.store.directory, oid, python=profile.python), env=env, timeout=cfg.fire_timeout_s)
    shown = subprocess.run(show_command(rig.store.directory, agent, python=profile.python), env=env, capture_output=True,
                           text=True, timeout=120)
    if shown.returncode:
        raise RuntimeError(f"agents show exited {shown.returncode}: {shown.stderr.strip()[-300:]}")
    return fired.returncode, oid, json.loads(shown.stdout)


# ---- the run ---------------------------------------------------------------------------------------------------------------

def run(cfg: S06Config, facts: Facts, *, fire_once: Callable[[S06Config], tuple[int, str, dict]] = real_fire_once):
    """``(status, RESULT path)``. EXPECTED (with its sha256) is written before anything is fired, even when the run is then
    REFUSED; a precondition that is absent refuses and fires nothing; a fire that raises is INCOMPLETE, never a pass."""
    episodes = bool(facts.anneal_importable)
    cfg = dataclasses.replace(cfg, episodes=episodes)
    ev = Evidence(cfg.evidence)
    digest = ev.write_expected(expected_text(cfg, episodes=episodes))
    head = f"S06, os={cfg.profile.name}, expected sha256 {digest} (EXPECTED.txt, written before the fire)"
    problems = preconditions(facts)
    if problems:
        for p in problems:
            ev.log(p)
        return "REFUSED", ev.write_result("\n".join(["RESULT: REFUSED", head, "S06: REFUSED", *problems]) + "\n")
    try:
        rc, oid, doc = fire_once(cfg)
    except BaseException as exc:  # noqa: BLE001 - no verdict was observed: INCOMPLETE, whatever raised
        line = f"S06: ERROR, no verdict observed ({type(exc).__name__}: {exc})"
        ev.log(line)
        return "INCOMPLETE", ev.write_result("\n".join(["RESULT: INCOMPLETE", head, line]) + "\n")
    raw = judge_show(doc, oid, rc, episodes=episodes)
    ok, _ = judge([raw], rc, verdict_for(episodes=episodes))
    mine = next((o for o in doc.get("occurrences") or [] if o.get("occurrence_id") == oid), {})
    status = "PASS" if ok else "FAIL"
    body = ["RESULT: " + status, head, f"S06: {status} raw={raw!r} expected=/{verdict_for(episodes=episodes).pattern}/"]
    if not ok:
        body.append("diagnosis: " + diagnose(mine.get("state"), mine.get("reason"), mine.get("ended_at"), rc))
    ev.log(body[-1] if not ok else body[2])
    return status, ev.write_result("\n".join(body) + "\n")


def main(argv: Sequence[str], *, read_facts_fn: Callable[[S06Config], Facts] = read_facts,
         fire_once: Callable[[S06Config], tuple[int, str, dict]] = real_fire_once) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--workspace-repo", required=True, help="absolute path of the stager test repo in the guest")
    ap.add_argument("--evidence", required=True, help="a fresh (or absent) directory for EXPECTED.txt / steps.log / RESULT.txt")
    ap.add_argument("--vendor-ready", action="store_true",
                    help="S03 login and S05's real passthrough start passed on this clone: a real vendor session is wanted")
    a = ap.parse_args(list(argv))
    ev_dir = Path(a.evidence)
    try:
        ev_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        cfg = S06Config(workspace_repo=a.workspace_repo, evidence=ev_dir, vendor_ready=a.vendor_ready)
        status, path = run(cfg, read_facts_fn(cfg), fire_once=fire_once)
    except RunbookError as exc:
        print(f"s06: refusing: {exc}", file=sys.stderr)
        return 2
    text = path.read_text()
    print(text, file=sys.stderr if status == "REFUSED" else sys.stdout)
    return {"PASS": 0, "FAIL": 1, "REFUSED": 2}.get(status, 3)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
