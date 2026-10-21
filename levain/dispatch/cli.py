"""levain.dispatch.cli — ``levain dispatch file|show|list|sign|cancel|recommend|authorise|land``: the planner's
door and the operator's.

A planner files a contract (``file``) and gets the job's capability once; the door keeps it in the planner session's
own record (:mod:`levain.dispatch.capability`) so a restarted session can still ``recommend`` or ``cancel``. The
planner's agent and session are DETECTED first (the ancestor process, else the agent's environment), never read from
the flags: under a detected agent an ``--agent`` or ``--session`` that is not what was detected is refused, and the
flags stand only when no agent is detected. A planner reaches another session's job only by holding its capability
(``--capability``). A session record that cannot be kept stops the filing before it is made. The operator reads a job RESOLVED as a signer sees
it (``show``), and signs its digest (``sign``) or cancels it (``cancel``) with an enrolled laptop key, after reading
the same view, then a fixed-height summary right above the y/N prompt (so the view cannot have scrolled the point
away), and answering y/N on a terminal. ``cancel`` acts as the PLANNER when the caller's session record holds the
job's capability (or ``--capability`` is given), else as the HUMAN, signed, and says which it was. Nothing a planner
does here authorises anything.

THE OPERATOR'S VERBS ARE NOT AN AGENT'S. ``sign`` and the human ``cancel`` are refused, with or without ``--yes``,
when this process runs under an agent: a ``claude`` or ``codex`` ancestor process, or an agent's environment (the
question band's walk and checks, :func:`agent_caller`). This is a check of INTENT, not a boundary: an agent can still
run ssh-keygen over the payload itself, so until S1 places the control principal, a laptop signature an agent can
reach is gated only by the custody of the signing key (a touch, a passphrase, a key no agent can read).

LANDING is :func:`land_job`, the ONE place the land's authorities are bound (F11-s2b §0): the landing tier
(:func:`levain.consent.landing.landing_tier` over the dispatch directory's consent settings), the template registry
and the provider policy (:func:`resolvers`), the adoption rules (the control store, read at the land) and the
authority source a human authorise item is verified against all come from the production stores, and a verb passes
only the job, the branch, the operator's signed item and the operator's ``--data-root``. ``authorise`` is the
operator's (refused to an agent, as ``sign`` is): the job, the change set the floor will judge and the landing tier,
then y/N, then a signature in :data:`levain.dispatch.adopt.DISPATCH_AUTHORISE_NAMESPACE` over the payload the server
built from the store's own run record (:func:`levain.dispatch.adopt.authorise_payload_for`), then the land. ``land``
lands by a standing rule, or finishes an authorised job (a retry, or after the operator's own printed merge); an agent
may run it only with the capability of a job it filed, and never with ``--data-root`` or ``--dispatch-dir``. A land
and ``list`` sweep the staged refs of the jobs of that repository that ended without an adoption
(:func:`levain.dispatch.adopt.sweep_staged`). A land's outcome is its exit: adopted 0; a merge for the operator to
run 0, the command alone on the last line; refused 2; failed (MOVED BASE) 1.

Exit codes: 0 done; 1 a land that FAILED its job (MOVED BASE); 2 the input or a refusal (the contract, the transition, the signature, the capability or the
command line was refused: the message says why, and nothing changed); 3 a fault on this machine, told apart by its
CLASS, never by its text: git could not run (:class:`levain.dispatch.contract.GitUnavailable`,
:class:`levain.dispatch.adopt.GitFault`), a store, record or
log that cannot be read or trusted (:class:`levain.dispatch.jobs.JobStoreError`,
:class:`levain.dispatch.capability.CapabilityRecordError`, an operating system or database error, a ``ValueError``
from below), a signature verifier that cannot run (:class:`levain.authority.VerifierUnavailable`) or an ssh-keygen
that cannot run to sign (:class:`SignerUnavailable`). Every exit 3 from ``sign``, ``cancel`` or a ``config`` verb
that changes the store, and every fault reading the configuration a filing resolves through, also says what did NOT
happen. The command line's own refusals are :class:`InputRefused`, so nothing raised below it reads as "refused".

CONFIGURATION is the operator's too (``levain dispatch config``, the F3 store :mod:`levain.dispatch.control`):
``init`` pins the operator's laptop key as the store's trust root, ``sign`` turns a body file into the next signed
item (the registry judges the body first, and a body it refuses is refused before any key is touched; the item is
shown, kind, generation, store, prev and a diff against the active body of its kind, before the touch), ``accept``
takes an item signed elsewhere, and ``show`` reads the active configuration back. ``init``, ``sign`` and ``accept``
are refused to an agent, as ``sign`` is; ``show`` is open. :func:`resolvers` reads the template registry and the
provider policy from that store, in one verified read, so a filing resolves through what the operator signed; with
no store, ``file`` refuses (every contract would be R2, which is never dispatched), and a store that cannot be
trusted is exit 3 with nothing filed. Under a detected agent, ``file`` refuses ``--data-root`` and ``--dispatch-dir``:
a planner's filing resolves through the configuration in the dispatch directory, verified against the cockpit its
consent hooks name, never through a store or a cockpit the planner names. ``config accept`` asks the store's own
judgement (:meth:`levain.dispatch.control.ControlStore.check`, which writes nothing) before it shows the item, so an
item the store would refuse is refused before any question. The summary above each y/N names the head the item
builds on (generation and digest). ``config show`` writes every non-ASCII character as its escape. Stdlib only.

Known edges: a read path made of homoglyphs shows as the path it imitates (a write path is bounded by the operator's
template, so only what is read can be dressed up). ``file`` reports a template the active configuration refuses (one
that no longer loads, or whose verifier no longer agrees) as a refusal, exit 2. The configuration is read once per
filing, so a configuration item accepted while a filing resolves applies to the next filing, not that one. An
idempotent ``file`` retry after a configuration change resolves through the new configuration, so it can refuse or
differ: it needs a lost response and an accepted change inside the retry window, and the job and its kept capability
survive, so nothing is lost (10-19). The MCP door returns a store fault's text (paths, database words) to the planner,
which runs as the same OS user and can read those paths already; S1 changes the principal (10-19). ``levain update``
re-adds a hand-deleted ``levain_dispatch`` entry to the workspace ``.mcp.json``, the same rule as the band (10-19).
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from levain import unshowable
from levain.authority import VerifierUnavailable
from levain.dispatch import ContractRefused, DispatchError
from levain.dispatch import adopt as dadopt
from levain.dispatch import capability as caps
from levain.dispatch import control as dctl
from levain.dispatch import registry as dregistry
from levain.dispatch import sign as dsign
from levain.dispatch.contract import NEW, GitUnavailable, ResolvedTemplate
from levain.dispatch.jobs import (
    TERMINAL, CapabilityRefused, Job, JobStore, JobStoreError, SignatureRefused, TransitionRefused,
)
from levain.dispatch.policy import ProviderPolicy

__all__ = ["EXIT_FAILED", "EXIT_FAULT", "EXIT_OK", "EXIT_REFUSED", "CockpitUnnamed", "InputRefused",
           "SignerUnavailable", "add_parsers", "agent_caller", "confirm_summary", "file_job", "job_view", "land_job",
           "planner_capability", "render", "resolvers", "run_dispatch"]

EXIT_OK, EXIT_FAILED, EXIT_REFUSED, EXIT_FAULT = 0, 1, 2, 3
# a contract is a few kilobytes; the bound only stops a stray file from being read whole
_CONTRACT_MAX = 1024 * 1024
NO_REGISTRY = "no template registry is set up; every contract is R2, which is never dispatched"
NO_LAND = "no dispatch configuration is set up, so nothing can land"


class InputRefused(DispatchError):
    """The command line's own refusal: no terminal to ask on, a file it cannot read, a flag that does not fit, a
    signing key that would not sign. The message says why, and nothing changed."""


class CockpitUnnamed(InputRefused):
    """No single cockpit to verify against: the consent hooks in the dispatch directory name none or several, or the
    one named holds no cockpit. ``door`` says it as the operator's fix, for a caller (the MCP door) that has no
    ``--data-root`` to pass."""

    def __init__(self, message: str, door: str) -> None:
        super().__init__(message)
        self.door = door


class SignerUnavailable(Exception):
    """ssh-keygen could not run (or timed out) to make the operator's signature: a fault on this machine, not a key
    that refused. Told apart by the cause chained to the key error, never by its message."""


class _NotDone(Exception):
    """A machine fault in ``sign`` or the human ``cancel``, with the sentence that says what did NOT happen."""

    def __init__(self, fault: BaseException, not_done: str) -> None:
        super().__init__(str(fault))
        self.fault, self.not_done = fault, not_done


# machine faults first: GitUnavailable is a ContractRefused, and GitFault, JobStoreError and ControlStateUnavailable
# are DispatchErrors, and each must be read as the fault it is; a ValueError raised below the command line is a fault
# too (its own refusals are InputRefused)
_FAULTS = (GitUnavailable, dadopt.GitFault, JobStoreError, dctl.ControlStateUnavailable, caps.CapabilityRecordError,
           VerifierUnavailable, SignerUnavailable, sqlite3.Error, OSError, ValueError)
_REFUSALS = (ContractRefused, TransitionRefused, SignatureRefused, CapabilityRefused, DispatchError)


class _ActiveConfiguration:
    """One verified read of the active configuration, answering the registry's readers (:func:`registry.resolver`,
    :func:`registry.active_policy`) from it: the template and the policy a filing binds come from one generation."""

    def __init__(self, active: Any) -> None:
        self._active = active

    def active(self) -> Any:
        return self._active


def resolvers(directory: Path | None = None, data_root: Path | None = None,
              ) -> tuple[Callable[[str], ResolvedTemplate | None], ProviderPolicy] | None:
    """The template registry and the provider policy a filing resolves through, read from the signed control store
    in ``directory`` (default: the dispatch directory), verified against the cockpit ``data_root`` names (default:
    the one the consent hooks there name, as ``sign`` finds it). ``None`` when there is no control store: nothing is
    configured, so every contract is R2. A store that cannot be trusted or read raises
    :class:`levain.dispatch.control.ControlStateUnavailable` (a fault: nothing was filed), whatever the error below
    it; no single cockpit named is :class:`CockpitUnnamed`; a store with no provider policy accepted refuses
    (:class:`DispatchError`)."""
    from levain.dispatch.jobs import default_dispatch_dir

    d = Path(directory) if directory is not None else default_dispatch_dir()
    if not os.path.lexists(d / dctl.DB_NAME):
        return None
    root = Path(data_root) if data_root is not None else None
    try:
        active = dctl.ControlStore(d, authority=_authority_source(d, root)).active()
    except (dctl.ControlStateUnavailable, sqlite3.Error, OSError, ValueError) as exc:
        raise dctl.ControlStateUnavailable(f"the configuration could not be read ({type(exc).__name__}: "
                                           f"{str(exc)[:200]}); nothing was filed") from None
    config = _ActiveConfiguration(active)
    return dregistry.resolver(config), dregistry.active_policy(config)


def _safe(text: object) -> str:
    """``text`` with every character a terminal could act on (or show as something else) written as its escape."""
    return unshowable.mark(str(text), _escape, allow="")


def _escape(cp: int) -> str:
    return f"\\u{cp:04x}" if cp <= 0xFFFF else f"\\U{cp:08x}"


def _ascii(text: object) -> str:
    """``text`` terminal-safe (:func:`_safe`) with every other non-ASCII character written as its escape too, so a
    homoglyph shows as what it is."""
    return _safe(text).encode("ascii", "backslashreplace").decode("ascii")


# --- the planner's identity and capability ----------------------------------------------------------------------------

def _detected() -> tuple[str | None, str | None]:
    """The agent this process runs under and its session, told with no flag: the nearest ``claude`` or ``codex``
    ancestor, else the agent's own environment, as ``levain ask`` tells them; ``None`` for what cannot be told."""
    from levain.consent.band import NO_SESSION as BAND_NO_SESSION
    from levain.consent.band import _agent_and_session

    try:
        # no flag goes in: a supplied agent would stand in for the ancestor and the environment
        agent, session = _agent_and_session(None, None)
    except ValueError:
        return None, None
    return agent, None if session == BAND_NO_SESSION else session


def agent_caller() -> str | None:
    """The agent this process runs under (``claude_code`` / ``codex``), as :func:`_detected` tells it; ``None`` when
    it is neither. An intent check (see the module docstring), never a boundary."""
    return _detected()[0]


def _refuse_an_agent(verb: str) -> None:
    agent = agent_caller()
    if agent is not None:
        raise InputRefused(f"`levain dispatch {verb}` is the operator's own act, and this process runs under "
                           f"{agent} (an agent's process or environment): an agent never signs or cancels for the "
                           "operator. Run it from your own terminal.")


def _refuse_gate_flags(args: argparse.Namespace, verb: str = "file") -> None:
    """Under a detected agent, ``file`` and ``land`` take no ``--data-root`` or ``--dispatch-dir``: either would let
    the planner name the store or the cockpit its own filing (or land) is verified against, so choose its own gate.
    An intent check, as :func:`_refuse_an_agent` is."""
    agent = agent_caller()
    if agent is None:
        return
    for flag, value in (("--data-root", args.data_root), ("--dispatch-dir", args.dispatch_dir)):
        if value is not None:
            raise InputRefused(f"{flag} is refused to a planner, and this process runs under {agent} (an agent's "
                               f"process or environment): a {verb} resolves through the configuration in the "
                               "dispatch directory, verified against the cockpit its consent hooks name. Run "
                               f"`levain dispatch {verb}` without it.")


def planner_session(agent: str | None, session: str | None) -> tuple[str | None, str]:
    """The planner's agent and session. Detection (:func:`_detected`) comes first and never reads the flags; once
    an agent is detected, an ``agent`` or ``session`` flag that is not what was detected is refused (a session the
    environment does not name included): another session's job is reached by its capability, never by naming the
    agent or the session. The flags stand only when no agent is detected; ``(None, NO_SESSION)`` when there is
    none either way."""
    from levain.consent.band import _agent_and_session

    detected_agent, detected_session = _detected()
    if detected_agent is None:
        if agent is None:
            return None, caps.NO_SESSION
        try:
            agent = _agent_and_session(agent, None)[0]    # the flag's own check: one of the known agents
        except ValueError as exc:
            raise InputRefused(str(exc)) from None
    else:
        if agent is not None and agent != detected_agent:
            raise InputRefused(f"--agent {_safe(agent)[:40]!r} is not the agent this process runs under "
                               f"({detected_agent}, from its process or environment); a job filed by another agent "
                               "is reached with --capability")
        if session is not None and session != detected_session:
            shown = "none" if detected_session is None else repr(_safe(detected_session)[:80])
            raise InputRefused(f"--session {_safe(session)[:80]!r} is not this session ({shown}, from "
                               f"{detected_agent}'s environment); a job filed in another session is reached with "
                               "--capability")
        agent, session = detected_agent, detected_session
    session = caps.NO_SESSION if session is None else session
    return agent, session if caps.session_problem(session) is None else caps.NO_SESSION


def file_job(store: JobStore, submission: Any, *, agent: str | None, session: str, idem_key: str | None = None,
             resolved: tuple[Callable[[str], ResolvedTemplate | None], ProviderPolicy] | None = None,
             data_root: Path | None = None) -> tuple[Job, str | None, str]:
    """File ``submission`` for the planner ``agent``/``session`` and keep its capability in that session's record;
    returns the job, its capability (``None`` on an idempotent replay) and one sentence on where the capability is
    kept. ``resolved`` is (template resolver, policy), :func:`resolvers` over the store's directory and
    ``data_root`` when not given.

    The session's record is checked before the filing (:func:`levain.dispatch.capability.preflight`): a record that
    cannot be kept for a reason already on disk is a fault on this machine, and nothing is filed. A record that
    still fails after the filing does not undo it: the job IS filed, so the capability is shown this once with
    "NOT kept" and the call succeeds; a retry with the same ``idem_key`` is the idempotent replay, which files
    nothing new."""
    resolved = resolved if resolved is not None else resolvers(store.directory, data_root)
    if resolved is None:
        raise ContractRefused(NO_REGISTRY)
    resolve_template, policy = resolved
    if agent is not None:
        try:
            caps.preflight(store.directory, agent, session)
        except (caps.CapabilityRecordError, OSError) as exc:
            raise caps.CapabilityRecordError(f"this session's capability record cannot be kept ({exc}); nothing "
                                             "was filed") from exc
    job, capability = store.file(submission, resolve_template=resolve_template, policy=policy,
                                 filer_session=session, idem_key=idem_key, filer_agent=agent)
    if capability is None:
        return job, None, ("this is a replay of a filing already made: its capability was shown once, then, and is "
                           "not shown again")
    problem = caps.session_problem(session) if agent is not None else "the filing agent could not be told"
    if problem is not None or agent is None:
        return job, capability, f"not kept ({problem}): it is shown this once, so keep it now"
    try:
        path = caps.keep(store.directory, agent, session, job.id, capability)
    except (caps.CapabilityRecordError, OSError) as exc:
        return job, capability, (f"NOT kept, the session record could not be written ({type(exc).__name__}: "
                                 f"{exc}): it is shown this once, so keep it now")
    return job, capability, f"kept in {path}, where this session gets it back"


def planner_capability(directory: Path, agent: str | None, session: str, job_id: str, given: str | None
                       ) -> str | None:
    """The capability a planner call presents: ``given``, else the one this session's record kept for ``job_id``,
    else ``None``."""
    if given is not None:
        return given
    if agent is None:
        return None
    return caps.recall(directory, agent, session, job_id)


# --- the land (F11 §6), its authorities bound here and nowhere else ---------------------------------------------------

def _landing_tier(directory: Path) -> Callable[[str, tuple[str, ...]], str]:
    """The landing tier over the consent settings in ``directory`` (the dispatch directory, which is the consent
    directory by default): what the floor judges a change's paths by."""
    from functools import partial

    from levain.consent.landing import landing_tier

    return partial(landing_tier, queue_dir=directory)


def land_job(store: JobStore, job_id: str, *, onto: str | None = None, signed: tuple[Any, Any] | None = None,
             data_root: Path | None = None) -> dadopt.Landing:
    """:func:`levain.dispatch.adopt.land` for ``job_id``, every authority it takes bound from the production stores
    (F11-s2b §0): the landing tier over the dispatch directory's consent settings, the registry and the policy
    (:func:`resolvers`), and either the operator's ``signed`` item ``(payload, signature)``, verified against the
    authority source :func:`_authority_source` picks, or the adoption rules read from the control store at the land.
    ``data_root`` is the operator's ``--data-root`` alone (the MCP door and the phone pass ``None``). No control
    store refuses (nothing configured lands). The staged refs of the repository's jobs that ended without an
    adoption are swept after the land (:func:`levain.dispatch.adopt.sweep_staged`); a ref git kept is added to the
    landing's message."""
    from dataclasses import replace

    from levain.dispatch.registry import adoption_rules_at

    resolved = resolvers(store.directory, data_root)
    if resolved is None:
        raise ContractRefused(NO_LAND)
    resolve_template, policy = resolved
    human: tuple[Any, Any, Any] | None = None
    rules: Callable[[], Any] | None = None
    if signed is not None:
        human = (signed[0], signed[1], _authority_source(store.directory, data_root))
    else:
        def rules() -> Any:
            return adoption_rules_at(dctl.ControlStore(store.directory,
                                                       authority=_authority_source(store.directory, data_root)))
    landing = dadopt.land(store, job_id, landing_tier=_landing_tier(store.directory),
                          resolve_template=resolve_template, policy=policy, onto=onto, human=human, rules=rules)
    kept = dadopt.sweep_staged(store, landing.job.repo)
    return replace(landing, message="; ".join([landing.message, *kept])) if kept else landing


def _change_view(store: JobStore, job: Job, run: int) -> tuple[list[str], list[str]]:
    """What ``authorise`` shows before the touch: run ``run``'s change set as the floor will judge it (each path,
    +/- lines, new, deleted, binary or gitlink named) and its landing tier, and the fixed-height lines for the summary
    above the prompt. A change set over a cap, or a tier that cannot be read, refuses before anything is signed."""
    record = store.run_record(job.id, run)
    if record is None:
        raise DispatchError(f"run {run} of job {job.id} has no verified record")
    changes = dadopt.change_set(job.repo, record.base, record.staged_commit)
    if isinstance(changes, str):
        raise DispatchError(f"{changes}; nothing was signed")
    tier = dadopt._tier(_landing_tier(store.directory), job.repo, changes.paths)
    if tier is None:
        raise DispatchError("the landing tier could not be read; nothing was signed")
    lines = [f"  the change the floor judges, run {run} (staged {record.staged_commit}, base {record.base}):"]
    for c in changes.entries:
        what = ("gitlink" if c.non_blob else "binary" if c.binary
                else f"+{c.added} -{c.deleted}" + (" (new file)" if c.status == "A" else
                                                   " (deleted)" if c.status == "D" else ""))
        lines.append(f"    {c.status}  {_safe(c.path)}  {what}")
    lines.append(f"  landing tier {tier}: only a key whose class covers {tier} may authorise it")
    counted = changes.changed_lines
    summary = [f"  run       {run}  ({dadopt.artifact_digest(record)})",
               f"  change    {len(changes.entries)} file{'s' if len(changes.entries) != 1 else ''}, "
               + (f"{counted} changed lines" if counted is not None else "binary or gitlink entries")
               + f", landing tier {tier}"]
    return [_safe(line) for line in lines], [_safe(line) for line in summary]


def _landed(args: argparse.Namespace, landing: dadopt.Landing, emit: Callable[[Any, str], None]) -> int:
    """A land's outcome as its exit: adopted 0, merge 0 (the operator's command alone on the last line), refused 2,
    failed 1."""
    job = landing.job
    message = _safe(landing.message)
    data = {"id": job.id, "state": job.state, "outcome": landing.outcome, "moved": landing.moved,
            "message": landing.message, "command": landing.command or None}
    if landing.outcome == "adopted":
        emit(data, f"levain dispatch: job {job.id} is adopted ("
                   f"{'Levain moved the branch' if landing.moved else 'Levain moved nothing'}): {message}")
        return EXIT_OK
    if landing.outcome == "merge":
        emit(data, "\n".join([f"levain dispatch: job {job.id} is {job.state}; {message}",
                              f"After the merge, `levain dispatch land {job.id}` records the adoption. The merge:",
                              landing.command]))
        return EXIT_OK
    if landing.outcome == "failed":
        emit(data, f"levain dispatch: job {job.id} FAILED (base_moved), its write files released for a new filing: "
                   f"{message}")
        return EXIT_FAILED
    emit(data, f"levain dispatch: not landed, refused: {message}")
    return EXIT_REFUSED


def _land_not_done(store: JobStore, job_id: str, last: Job | None) -> str:
    """What a faulted ``authorise`` / ``land`` of ``job_id`` did NOT do, with the job read again when the store
    answers: an authorised job may already have moved its branch, so it says to run the land again."""
    try:
        now = store.get(job_id)
    except (*_FAULTS, DispatchError):
        now = None
    shown = _safe(job_id)[:40]
    if now is None:
        return f"whether job {shown} landed is not known (the store could not be read): read it with `levain " \
               "dispatch show`"
    if now.state == "authorised":
        return f"job {shown} is authorised and not yet adopted: run `levain dispatch land {shown}` again to finish it"
    if last is not None and now.state == last.state:
        return f"nothing was landed; job {shown} is still {now.state}"
    return f"job {shown} is now {now.state}: read it with `levain dispatch show`"


# --- the view a signer reads ------------------------------------------------------------------------------------------

def job_view(job: Job) -> dict[str, Any]:
    """Every field of ``job`` a signer is shown, resolved: the contract as stored and digested, the state and its
    history with their causes."""
    c = job.contract
    t = c.template
    return {"id": job.id, "state": job.state, "digest": job.digest, "repo": c.repo, "base": c.base,
            "base_tree": c.base_tree, "goal": c.goal, "read": [e.to_mapping() for e in c.read],
            "write": [e.to_mapping() for e in c.write], "template": t.to_mapping(), "budget": dict(c.budget),
            "grant": list(c.grant), "expires_at": c.expires_at, "policy_hash": c.policy_hash, "filed_at": job.filed_at,
            "lease_end": job.lease_end, "signer_fp": job.signer_fp, "filer_agent": job.filer_agent,
            "filer_session": job.filer_session,
            "history": [{"seq": h.seq, "to_state": h.to_state, "cause": h.cause, "at": h.at, "evidence": h.evidence}
                        for h in job.history]}


def _entries(rows: list[dict[str, str]]) -> list[str]:
    return [f"    {_safe(e['path'])}  " + ("(new file)" if e["mode"] == NEW else f"{e['mode']} {e['blob']}")
            for e in rows]


def render(job: Job, *, goal_label: str = "goal (sent to the provider, in full)") -> str:
    """The job as text, every field of :func:`job_view`, the goal in full under ``goal_label``. Every line is passed
    through the terminal-safe escape, whatever field it shows."""
    v = job_view(job)
    t = v["template"]
    b = v["budget"]
    lines = [
        f"job {v['id']}  [{v['state']}]",
        f"  repo        {_safe(v['repo'])}",
        f"  base        {v['base']} (tree {v['base_tree']})",
        f"  digest      {v['digest']}",
        f"  template    {t['id']}  tier {t['tier']}  ({t['content_hash']})",
        f"  executor    {t['executor']}, models {', '.join(t['models'])} (k={t['k']}), provider {t['provider']} "
        f"via {t['route']}",
        f"  verifier    {t['verifier_id']} ({t['verifier_kind']}, {t['verifier_hash']})",
        f"  tools       {', '.join(f'{n} {ver}' for n, ver in t['tool_versions']) or '(none)'}",
        f"  budget      {b['tool_calls']} tool calls, {b['tokens']} tokens, {b['dollars_cents']} cents (executor)",
        f"  grant       {', '.join(v['grant'])} over the write list (Bash is never granted)",
        f"  expires     {v['expires_at']}",
        f"  lease ends  {v['lease_end']}" + ("  (unsigned: it lapses then)" if v["state"] == "filed" else ""),
        f"  policy      {v['policy_hash']}",
        f"  signer      {v['signer_fp'] or '(not signed)'}",
        f"  filed by    {v['filer_agent'] or '(no agent named)'}, session {v['filer_session']} (as the filing door "
        "named them)",
        f"  {goal_label}:",
        *[f"    | {_safe(line)}" for line in v["goal"].split("\n")],
        f"  read ({len(v['read'])}, what the executor sees and the provider receives):",
        *_entries(v["read"]),
        f"  write ({len(v['write'])}, the only files a result may change):",
        *_entries(v["write"]),
        f"  template write paths: {', '.join(_safe(p) for p in t['write_paths'])}",
        "  history:",
        *[f"    {h['seq']:>2} {h['to_state']:<12} {h['cause']:<20} {h['at']}"
          + (f"  {_safe(h['evidence'])}" if h["evidence"] else "") for h in v["history"]],
    ]
    # _safe leaves only printable text, so a second pass over a line it already escaped changes nothing
    return "\n".join(_safe(line) for line in lines)


def confirm_summary(job: Job, verb: str) -> list[str]:
    """The fixed-height summary printed right above the y/N prompt: the same number of lines for every job, each one
    line however long the goal or the lists, so the point of the view is on screen when the question is."""
    c = job.contract
    t = c.template
    b = c.budget
    goal_lines = len(c.goal.split("\n"))
    rows = [
        f"---- you are about to {verb} job {job.id} ----",
        f"  digest    {job.digest}",
        f"  template  {t.template_id}  tier {t.tier}",
        f"  models    {', '.join(t.models)} (k={t.k})",
        f"  budget    {b['tool_calls']} tool calls, {b['tokens']} tokens, {b['dollars_cents']} cents",
        f"  expires   {c.expires_at}",
        f"  files     {len(c.read)} read, {len(c.write)} write",
        f"  goal      {len(c.goal.encode('utf-8'))} bytes, {goal_lines} line{'s' if goal_lines != 1 else ''} "
        "(in full above)",
        f"  filed by  {job.filer_agent or '(no agent named)'}, session {job.filer_session}",
    ]
    return [_safe(r) for r in rows]


# --- the commands -----------------------------------------------------------------------------------------------------

def _store(args: argparse.Namespace) -> JobStore:
    return JobStore(args.dispatch_dir.expanduser() if args.dispatch_dir is not None else None)


def _read_input(source: str, limit: int, what: str) -> bytes:
    """The bytes of file ``source`` (``-``: stdin), refused when it cannot be read or is over ``limit`` bytes."""
    try:
        if source == "-":
            raw = sys.stdin.buffer.read(limit + 1)
        else:
            with open(Path(source).expanduser(), "rb") as fh:
                raw = fh.read(limit + 1)
    except OSError as exc:
        raise InputRefused(f"the {what} file cannot be read: {exc.strerror or exc}") from None
    if len(raw) > limit:
        raise InputRefused(f"the {what} is over {limit} bytes")
    return raw


def _read_contract(source: str) -> Any:
    raw = _read_input(source, _CONTRACT_MAX, "contract")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise InputRefused("the contract is not JSON") from None


def _must_get(store: JobStore, job_id: str) -> Job:
    job = store.get(job_id)
    if job is None:
        raise TransitionRefused(f"no dispatch job {str(job_id)[:40]!r}")
    return job


def _is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _confirm(args: argparse.Namespace, job: Job, what: str, out: Any, extra: tuple[str, ...] = ()) -> None:
    """Ask y/N on the terminal, right below :func:`confirm_summary` and the verb's own fixed-height ``extra`` lines;
    refuse without a terminal unless ``--yes``. The caller has printed the whole view."""
    if args.yes:
        return
    if not _is_tty():
        raise InputRefused(f"{what} asks y/N on the operator's terminal and this process has none; run "
                           f"`levain dispatch {what} {job.id}` in your own terminal")
    print("\n".join([*confirm_summary(job, what), *extra]), file=sys.stderr if args.json else out, flush=True)
    try:
        answer = _ask(args, f"{what} this job? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        raise InputRefused(f"not confirmed; nothing was done and job {job.id} is unchanged")


def _ask(args: argparse.Namespace, prompt: str) -> str:
    """The operator's answer to ``prompt``; under ``--json`` the prompt goes to stderr, so stdout stays one JSON
    document."""
    if not args.json:
        return input(prompt)
    print(prompt, end="", file=sys.stderr, flush=True)
    return input("")


def _authority(args: argparse.Namespace, store: JobStore) -> Any:
    return _authority_source(store.directory, args.data_root)


def _authority_source(directory: Path, data_root: Path | None) -> Any:
    """The authority source a laptop signature is verified against: ``data_root`` (``--data-root``), else the one
    cockpit the consent hooks in ``directory`` (the dispatch directory) verify against; refused before anything is
    signed when there is no single cockpit there."""
    from levain.authority import anchor_path
    from levain.consent.answering import hook_roots
    from levain.consent.queue import authority_source

    fix = (f"the operator sets cockpit_data_root, in the consent settings in {directory} (claude_code.json, "
           "codex.json), to the one cockpit whose authority log holds their key; `levain doctor` shows what they "
           "name. A planner cannot fix this")
    if data_root is not None:
        root = data_root.expanduser()
    else:
        roots = hook_roots(directory)
        if len(roots) != 1:
            named = "none" if not roots else f"{len(roots)}: {', '.join(map(str, roots))}"
            raise CockpitUnnamed(f"name the cockpit data root with --data-root (the consent hooks here name {named})",
                                 f"the dispatch configuration is verified against the cockpit the consent hooks in "
                                 f"the dispatch directory name, and they name {named}; {fix}")
        root = roots[0]
    if not (root / "cockpit.db").is_file():
        named_by = "the --data-root flag" if data_root is not None else "the consent hooks"
        raise CockpitUnnamed(f"{root} holds no cockpit (no cockpit.db); name the cockpit data root with --data-root",
                             f"the cockpit {named_by} name{'s' if data_root is not None else ''} ({root}) holds no "
                             f"cockpit.db; " + (fix if data_root is None else "the operator names a cockpit data root "
                                                "that holds one. A planner cannot fix this"))
    return authority_source(root / "cockpit.db", anchor=anchor_path(root))


def _signer(args: argparse.Namespace, namespace: str, what: str) -> tuple[str, Callable[[bytes], str]]:
    """The signer for ``--signing-key``. A key file that cannot be read or is not a key is the operator's input
    (exit 2); a verifier that cannot run to fingerprint it propagates as the fault it is."""
    from levain.cockpit.keys import KeyError_, ssh_signer

    if args.signing_key is None:
        raise InputRefused(f"{what} needs --signing-key (an enrolled key: its private key file, or the .pub of a key "
                           "an agent holds)")
    try:
        return ssh_signer(args.signing_key, namespace=namespace)
    except KeyError_ as exc:
        raise InputRefused(str(exc)) from None


def _signature(sign: Callable[[bytes], str], payload: dict[str, str]) -> str:
    """``payload`` signed, as :func:`_sign_bytes` signs its message."""
    return _sign_bytes(sign, dsign.sign_message(payload))


def _sign_bytes(sign: Callable[[bytes], str], message: bytes) -> str:
    """``message`` signed. An ssh-keygen that could not run or timed out (the chained cause) is
    :class:`SignerUnavailable`, a fault; a key that would not sign (a cancelled touch, a refusing exit) is the
    operator's refusal."""
    from levain.cockpit.keys import KeyError_

    try:
        return sign(message)
    except KeyError_ as exc:
        if isinstance(exc.__cause__ or exc.__context__, (OSError, subprocess.TimeoutExpired)):
            raise SignerUnavailable(str(exc)) from exc
        raise InputRefused(str(exc)) from None


_UNKNOWN = "state unknown: the store could not be read"
_T = TypeVar("_T")


def _not_done(store: JobStore, job_id: str, last: Job | None, verb: str) -> str:
    """What a faulted ``sign`` / ``cancel`` of ``job_id`` did NOT do, with the job's state read again when the store
    answers; ``last`` is the job as this call last read it, ``None`` when it never could."""
    try:
        now = store.get(job_id)
    except (*_FAULTS, DispatchError):
        now = None
    shown = _safe(job_id)[:40]
    if now is None and last is None:
        if verb == "sign":
            return f"nothing was signed; job {shown} is unchanged ({_UNKNOWN})"
        return f"job {shown} is NOT cancelled; its state is unknown: the store could not be read"
    if now is not None and last is not None and now.state != last.state:
        # the job moved: the fault may have come after this call's own write committed, so who moved it is not known
        return f"job {shown} is now {now.state} (was {last.state}); the store faulted, so whether this call made that " \
               "change is not known: read it with `levain dispatch show`"
    state = now.state if now is not None else f"{last.state} when last read"
    if verb == "sign":
        return f"nothing was signed; job {shown} is unchanged ({state})"
    if now is not None and now.state in TERMINAL:
        if last is None:
            # never read before the fault, so whether this call's own write made it terminal is not known
            return f"job {shown} is {now.state}; the store faulted, so whether this call made that change is not " \
                   "known: read it with `levain dispatch show`"
        return f"this call cancelled nothing; job {shown} is {now.state}"
    return f"job {shown} is NOT cancelled; it is still {state} and holds its files"


def _guarded(store: JobStore, job_id: str, verb: str, act: Callable[[dict[str, Job]], _T]) -> _T:
    """``act(seen)`` with every machine fault it raises, the first store read included, said as what ``verb`` did
    NOT do to ``job_id``; ``act`` puts the job in ``seen["job"]`` once it has read it."""
    seen: dict[str, Job] = {}
    try:
        return act(seen)
    except _FAULTS as exc:
        if verb in ("authorise", "land"):
            raise _NotDone(exc, _land_not_done(store, job_id, seen.get("job"))) from exc
        raise _NotDone(exc, _not_done(store, job_id, seen.get("job"), verb)) from exc


def _show_first(args: argparse.Namespace, job: Job, out: Any) -> None:
    # the view always precedes a signature; under --json it goes to stderr so stdout stays one JSON document
    print(render(job), file=sys.stderr if args.json else out, flush=True)


def _signed_cancel(args: argparse.Namespace, store: JobStore, seen: dict[str, Job], out: Any) -> Job:
    """The operator's signed cancel of ``args.job``; runs inside the cancel's :func:`_guarded`."""
    _refuse_an_agent("cancel")
    problem = dsign.reason_problem(args.reason)
    if problem is not None:
        raise InputRefused(problem)
    job = seen["job"] = _must_get(store, args.job)
    if job.state in TERMINAL:
        raise TransitionRefused(f"job {job.id} is {job.state}, which is final; only a live job can be cancelled")
    _show_first(args, job, out)
    signer_fp, sign = _signer(args, dsign.DISPATCH_CANCEL_NAMESPACE, "the operator's signed cancel")
    authority = _authority(args, store)
    payload = dsign.cancel_payload(job, signer_fp=signer_fp, signed_at=datetime.now(timezone.utc),
                                   reason=args.reason)
    _confirm(args, job, "cancel", out)
    return store.cancel_by_human(job.id, payload, _signature(sign, payload), authority)


# --- configuration (the F3 control store) ----------------------------------------------------------------------------

def _config_dir(args: argparse.Namespace) -> Path:
    from levain.dispatch.jobs import default_dispatch_dir

    return args.dispatch_dir.expanduser() if args.dispatch_dir is not None else default_dispatch_dir()


def _control(args: argparse.Namespace, directory: Path) -> dctl.ControlStore:
    """The control store in ``directory``, verified against the cockpit :func:`_authority_source` picks; refused,
    pointing at ``config init``, when there is none."""
    if not os.path.lexists(directory / dctl.DB_NAME):
        raise InputRefused(f"there is no dispatch configuration store at {directory / dctl.DB_NAME}; create it with "
                           "`levain dispatch config init --trust-root <your laptop key's fingerprint>`")
    return dctl.ControlStore(directory, authority=_authority_source(directory, args.data_root))


def _verified(store: dctl.ControlStore) -> tuple[dict[str, dctl.ConfigItem], int, str]:
    """``(items, top, head)`` from ONE verified read: the latest item of every kind (adoption rules included), the
    highest generation and its digest. The newest item is the latest of its own kind, so it is among the items."""
    items = dict(store._verified_items())
    newest = max(items.values(), key=lambda i: i.generation, default=None)
    return items, (0 if newest is None else newest.generation), ("" if newest is None else newest.digest)


def _pretty(body: Any) -> list[str]:
    # the canonical value (keys sorted, as canonical() sorts them), one key per line, every non-ASCII character
    # escaped so a homoglyph shows as what it is
    return json.dumps(body, indent=2, sort_keys=True, ensure_ascii=True).split("\n")


def _item_view(kind: str, generation: int, item_store: str, store_id: str, prev: str, body: Any,
               active: dctl.ConfigItem | None) -> tuple[list[str], str]:
    """The item as shown before it is signed or accepted, in a fixed shape (kind, generation, store, prev, then the
    body as a unified diff against the active body of its kind, or in full under "new kind"), and a one-line count
    of the change for the summary above the prompt."""
    shown_store = _ascii(item_store)
    store_line = shown_store if item_store == store_id else f"{shown_store} (NOT this store, {_ascii(store_id)})"
    kind = _ascii(kind)
    lines = ["levain dispatch config: the item", f"  kind        {kind}", f"  generation  {generation}",
             f"  store       {store_line}", f"  prev        {_ascii(prev) or '(none: the first item)'}"]
    new = _pretty(body)
    if active is None:
        lines.append(f"  new kind: no {kind} item is active; its body in full:")
        lines += [f"    +{line}" for line in new]
        change = f"new kind, {len(new)} lines"
    else:
        diff = list(difflib.unified_diff(_pretty(active.body), new, lineterm="",
                                         fromfile=f"{kind}, active (generation {active.generation})",
                                         tofile=f"{kind}, generation {generation}"))
        if diff:
            lines.append(f"  change against the active {kind} (generation {active.generation}):")
            lines += [f"    {line}" for line in diff]
        else:
            lines.append(f"  no change: the body is the active {kind} body (generation {active.generation})")
        added = sum(1 for d in diff[2:] if d.startswith("+"))
        removed = sum(1 for d in diff[2:] if d.startswith("-"))
        change = f"+{added} -{removed} lines against generation {active.generation}"
    return [_safe(line) for line in lines], change


def _confirm_item(args: argparse.Namespace, verb: str, kind: str, generation: int, store_id: str, change: str,
                  top: int, head: str, out: Any) -> None:
    """y/N right below a fixed-height summary of the item, the store's head (generation ``top``, digest ``head``)
    among it; refused without a terminal unless ``--yes``."""
    if args.yes:
        return
    if not _is_tty():
        raise InputRefused(f"config {verb} asks y/N on the operator's terminal and this process has none; run "
                           f"`levain dispatch config {verb}` in your own terminal")
    print("\n".join([f"---- you are about to {verb} a configuration item ----", f"  kind        {_ascii(kind)}",
                     f"  generation  {generation} (the store is at {top})", f"  store       {store_id}",
                     f"  builds on   generation {top}, {head or '(none: the store is empty)'}",
                     f"  change      {change} (in full above)"]),
          file=sys.stderr if args.json else out, flush=True)
    try:
        answer = _ask(args, f"{verb} {_ascii(kind)} as generation {generation}? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        done = "signed" if verb == "sign" else "accepted"
        raise InputRefused(f"not confirmed; nothing was {done} and the configuration is still generation {top}")


def _config_not_done(store: dctl.ControlStore, top: int) -> str:
    """What a faulted ``config sign`` / ``config accept`` did NOT do, after its item was handed to the store at
    generation ``top`` + 1, with the generation read again when the store answers."""
    try:
        now = _verified(store)[1]
    except (*_FAULTS, DispatchError):
        return ("whether the item was accepted is not known (the store could not be read again): read it with "
                "`levain dispatch config show`")
    if now == top:
        return f"nothing was accepted; generation is still {top}"
    return (f"the configuration is now generation {now} (was {top}); the store faulted, so whether this item is the "
            "one accepted is not known: read it with `levain dispatch config show`")


def _head_unmoved(store: dctl.ControlStore, top: int, head: str, when: str, done: str) -> None:
    """Refuse when the verified head is no longer generation ``top`` with digest ``head``: what the operator saw
    (or what was judged) is not what the store holds now."""
    now, now_head = _verified(store)[1:]
    if (now, now_head) != (top, head):
        raise InputRefused(f"the store moved to generation {now} {when}; nothing was {done}, re-run")


def _sign_fault_not_done(store: dctl.ControlStore, top: int, head: str) -> str:
    """What a ``config sign`` whose signer faulted did NOT do, with the head read again."""
    try:
        now, now_head = _verified(store)[1:]
    except (*_FAULTS, DispatchError):
        return ("nothing was signed or accepted; the generation could not be read again: read it with "
                "`levain dispatch config show`")
    if (now, now_head) == (top, head):
        return f"nothing was signed or accepted; generation is still {top}"
    return f"nothing was signed or accepted; the store is now generation {now} (was {top})"


def _accept_guarded(store: dctl.ControlStore, item: bytes, signature: str, top: int) -> dctl.ConfigItem:
    try:
        return store.accept(item, signature)
    except _FAULTS as exc:
        raise _NotDone(exc, _config_not_done(store, top)) from exc


def _read_body(source: str) -> Any:
    from levain.contracts.canon import parse

    raw = _read_input(source, dctl.ITEM_MAX_BYTES, "configuration body")
    try:
        return parse(raw)
    except ValueError as exc:   # ContractError: the leaf-data rules (no floats, no duplicate keys, ...)
        raise InputRefused(f"the configuration body is not JSON a configuration item can hold: {exc}") from None


def _kind_summary(kind: str, item: dctl.ConfigItem, items: dict[str, dctl.ConfigItem]) -> tuple[dict[str, Any], str]:
    """One active kind as data and as one line of text."""
    body, gen = item.body, item.generation
    if kind == "templates":
        ids = [str(t.get("id")) for t in body if isinstance(t, dict)]
        return {"generation": gen, "ids": ids}, ", ".join(ids) or "(no templates)"
    if kind == "verifiers":
        kinds = {str(v): str(spec.get("kind")) for v, spec in body.items()}
        return {"generation": gen, "verifiers": kinds}, ", ".join(f"{v} ({k})" for v, k in kinds.items())
    if kind == "provider_policy":
        providers = sorted(body.get("providers", {}))
        return ({"generation": gen, "providers": providers, "never": len(body.get("never", []))},
                f"providers {', '.join(providers) or '(none)'}; {len(body.get('never', []))} never-patterns")
    if kind == "routemap":
        routes = [str(r.get("route")) for r in body.get("routes", []) if isinstance(r, dict)]
        return {"generation": gen, "routes": routes}, ", ".join(routes) or "(no routes)"
    # adoption rules: counted, and re-judged against the configuration active now, as their only reader judges them
    problem = dregistry.body_refusal(kind, body, items)
    data: dict[str, Any] = {"generation": gen, "count": len(body), "hold": problem is None}
    if problem is not None:
        data["why"] = problem
    return data, f"{len(body)} rule{'s' if len(body) != 1 else ''}" + (
        "" if problem is None else f" (they NO LONGER HOLD: {problem})")


def _run_config(args: argparse.Namespace, out: Any, emit: Callable[[Any, str], None]) -> int:
    cmd = args.config_cmd
    d = _config_dir(args)
    if cmd == "show":
        path = d / dctl.DB_NAME
        if not os.path.lexists(path):
            emit({"configured": False, "store": str(path)},
                 f"levain dispatch config: nothing is configured (no control store at {path}); "
                 "`levain dispatch config init` creates it.")
            return EXIT_OK
        store = _control(args, d)
        items, top, head = _verified(store)
        kinds = {k: _kind_summary(k, items[k], items) for k in dctl.KINDS if k in items}
        emit({"configured": True, "store": str(path), "store_id": store.store_id(), "trust_root": store.trust_root(),
              "generation": top, "head": head, "kinds": {k: v[0] for k, v in kinds.items()}},
             "\n".join(_ascii(line) for line in [
                 f"levain dispatch config: store {store.store_id()}, generation {top}, head {head or '(none)'}",
                 f"  trust root       {store.trust_root()}",
                 *[f"  {k:<16} " + (f"(generation {kinds[k][0]['generation']})  {kinds[k][1]}" if k in kinds
                                    else "(none accepted)") for k in dctl.KINDS]]))
        return EXIT_OK
    _refuse_an_agent(f"config {cmd}")
    try:
        return _change_config(args, cmd, d, out, emit)
    except _FAULTS as exc:
        raise _NotDone(exc, _config_fault_not_done(cmd, d)) from exc


def _config_fault_not_done(cmd: str, directory: Path) -> str:
    """What a ``config`` verb that faulted before its item reached the store did NOT do."""
    if cmd == "init":
        if not os.path.lexists(directory / dctl.DB_NAME):
            return "nothing was pinned (no control store was created)"
        return ("whether the store was pinned is not known (it exists, and the fault came after it was opened): read "
                "it with `levain dispatch config show`")
    return f"nothing was {'signed' if cmd == 'sign' else 'accepted'}; the store was not given an item"


def _change_config(args: argparse.Namespace, cmd: str, d: Path, out: Any, emit: Callable[[Any, str], None]) -> int:
    """``config init|sign|accept``; a fault after an item reached the store is a :class:`_NotDone` already."""
    if cmd == "init":
        authority = _authority_source(d, args.data_root)
        dctl.ControlStore.init(d, args.trust_root, authority=authority)
        store = dctl.ControlStore(d, authority=authority)
        sid = store.store_id()
        emit({"store": str(store.path), "store_id": sid, "trust_root": store.trust_root()},
             f"levain dispatch config: the control store {store.path} (store id {sid}) is pinned to "
             f"{store.trust_root()}. Sign its first item with `levain dispatch config sign <kind> <body.json> "
             "--signing-key <that key>` (verifiers before templates).")
        return EXIT_OK
    view_out = sys.stderr if args.json else out
    if cmd == "sign":
        body = _read_body(args.body)
        store = _control(args, d)
        items, top, head = _verified(store)
        problem = dregistry.body_refusal(args.kind, body, items)
        if problem is not None:
            raise dctl.ConfigRefused(f"{args.kind}: {problem}; nothing was signed")
        sid = store.store_id()
        item = dctl.item_bytes(args.kind, top + 1, body, datetime.now(timezone.utc), prev=head, store_id=sid)
        dctl.parse_item(item)    # the item's own bounds, judged before the touch rather than at accept
        lines, change = _item_view(args.kind, top + 1, sid, sid, head, body, items.get(args.kind))
        print("\n".join(lines), file=view_out, flush=True)
        signer_fp, sign = _signer(args, dctl.CONFIG_NAMESPACE, "config sign")
        root = store.trust_root()
        if signer_fp != root:
            raise InputRefused(f"--signing-key is {signer_fp}, which is not the pinned trust root ({root}): only the "
                               "root's signature is accepted. Nothing was signed")
        _confirm_item(args, "sign", args.kind, top + 1, sid, change, top, head, out)
        _head_unmoved(store, top, head, "while you were confirming", "signed")
        try:
            signature = _sign_bytes(sign, item)
        except SignerUnavailable as exc:
            raise _NotDone(exc, _sign_fault_not_done(store, top, head)) from exc
        try:
            accepted = _accept_guarded(store, item, signature, top)
        except dctl.ConfigRefused as exc:
            raise dctl.ConfigRefused(f"signed but not accepted: {exc}; this signature cannot be used, re-run config "
                                     "sign") from None
    else:   # accept
        raw = _read_input(args.item, dctl.ITEM_MAX_BYTES, "configuration item")
        try:
            signature = _read_input(args.signature, dctl.SIGNATURE_MAX, "signature").decode("utf-8")
        except UnicodeDecodeError:
            raise InputRefused("the signature file is not UTF-8 text") from None
        dctl.parse_item(raw)
        store = _control(args, d)
        # the store's own judgement first, writing nothing: an item it would refuse is refused before the view and
        # the question; accept judges again in its write transaction
        parsed = store.check(raw, signature)
        items, top, head = _verified(store)
        # the view shows the head check judged against, or nothing
        if (top, head) != (parsed.generation - 1, parsed.prev):
            raise InputRefused(f"the store moved to generation {top} after the item was checked; nothing was "
                               "accepted, re-run")
        sid = store.store_id()
        lines, change = _item_view(parsed.kind, parsed.generation, parsed.store_id, sid, parsed.prev, parsed.body,
                                   items.get(parsed.kind))
        print("\n".join(lines), file=view_out, flush=True)
        _confirm_item(args, "accept", parsed.kind, parsed.generation, sid, change, top, head, out)
        _head_unmoved(store, top, head, "while you were confirming", "accepted")
        accepted = _accept_guarded(store, raw, signature, top)
    emit({"kind": accepted.kind, "generation": accepted.generation, "digest": accepted.digest},
         f"levain dispatch config: accepted generation {accepted.generation} ({accepted.kind}), {accepted.digest}.")
    return EXIT_OK


def _run(args: argparse.Namespace, out: Any) -> int:
    cmd = args.dispatch_cmd

    def emit(data: Any, text: str) -> None:
        # ASCII-escaped JSON: no C1 control or line separator reaches a terminal raw
        print(json.dumps(data, ensure_ascii=True, indent=2) if args.json else text, file=out, flush=True)

    if cmd == "config":
        return _run_config(args, out, emit)
    if cmd in ("file", "land"):
        _refuse_gate_flags(args, cmd)
    store = _store(args)
    if cmd == "file":
        submission = _read_contract(args.contract)
        agent, session = planner_session(args.agent, args.session)
        job, capability, kept = file_job(store, submission, agent=agent, session=session, idem_key=args.idem_key,
                                         data_root=args.data_root)
        emit({"id": job.id, "digest": job.digest, "state": job.state, "lease_end": job.lease_end,
              "capability": capability, "capability_kept": kept},
             "\n".join([f"levain dispatch: filed job {job.id} ({job.state})",
                        f"  digest      {job.digest}",
                        f"  lease ends  {job.lease_end} (unsigned, it lapses then)",
                        f"  capability  {capability if capability is not None else '(not shown again)'}",
                        f"  {kept}",
                        "Nothing runs until the operator signs this digest on their own device "
                        f"(`levain dispatch sign {job.id}`). A recommend confers no authority."]))
        return EXIT_OK
    if cmd == "show":
        job = _must_get(store, args.job)
        emit(job_view(job), render(job))
        return EXIT_OK
    if cmd == "list":
        every = store.list(repo=args.repo)
        jobs = [j for j in every if args.all or j.state not in TERMINAL]
        emit([{"id": j.id, "state": j.state, "repo": j.repo, "filed_at": j.filed_at, "lease_end": j.lease_end,
               "template": j.contract.template.template_id, "digest": j.digest} for j in jobs],
             "\n".join(_safe(f"{j.id}  {j.state:<12} {j.contract.template.template_id:<16} lease {j.lease_end}  "
                             f"{j.repo}") for j in jobs)
             or ("levain dispatch: no jobs." if args.all else "levain dispatch: no live jobs (--all for every job)."))
        # the staged refs of the jobs that ended without an adoption, swept when a verb runs; what git kept is said
        for repo in sorted({j.repo for j in every}):
            for line in dadopt.sweep_staged(store, repo):
                print(f"levain dispatch: {_safe(line)}", file=sys.stderr if args.json else out, flush=True)
        return EXIT_OK
    if cmd == "sign":
        _refuse_an_agent("sign")

        def sign_it(seen: dict[str, Job]) -> Job:
            job = seen["job"] = _must_get(store, args.job)
            if job.state != "filed":
                raise SignatureRefused(f"job {job.id} is {job.state}; only a filed job can be signed")
            _show_first(args, job, out)
            signer_fp, sign = _signer(args, dsign.DISPATCH_SIGN_NAMESPACE, "signing")
            authority = _authority(args, store)
            payload = dsign.sign_payload(job, signer_fp=signer_fp, signed_at=datetime.now(timezone.utc))
            _confirm(args, job, "sign", out)
            return store.sign(job.id, payload, _signature(sign, payload), authority)

        job = _guarded(store, args.job, "sign", sign_it)
        emit({"id": job.id, "state": job.state, "signer_fp": job.signer_fp, "lease_end": job.lease_end},
             f"levain dispatch: signed job {job.id} as {job.signer_fp}; it is {job.state} until {job.lease_end}.")
        return EXIT_OK
    if cmd == "recommend":
        agent, session = planner_session(args.agent, args.session)
        cap = planner_capability(store.directory, agent, session, args.job, args.capability)
        if cap is None:
            raise CapabilityRefused("recommend is the filing planner's alone: this session's record holds no "
                                    f"capability for {str(args.job)[:40]!r}; pass --capability")
        job = store.recommend(args.job, cap, onto=args.onto, run=args.run)
        proposal = "" if args.onto is None and args.run is None else (
            " Proposed for the operator's authorise: " + ", ".join(
                p for p in (None if args.onto is None else f"onto {_safe(args.onto)}",
                            None if args.run is None else f"run {args.run}") if p) + ".")
        emit({"id": job.id, "state": job.state, "as": "planner", "onto": args.onto, "run": args.run},
             f"levain dispatch: job {job.id} is recommended (advisory: it authorises nothing).{proposal}")
        return EXIT_OK
    if cmd == "authorise":
        _refuse_an_agent("authorise")

        def authorise_it(seen: dict[str, Job]) -> dadopt.Landing:
            job = seen["job"] = _must_get(store, args.job)
            if job.state not in ("verified", "recommended"):
                raise TransitionRefused(f"job {job.id} is {job.state}; only a verified or recommended job is "
                                        "authorised")
            _show_first(args, job, out)
            signer_fp, sign = _signer(args, dadopt.DISPATCH_AUTHORISE_NAMESPACE, "authorise")
            payload = dadopt.authorise_payload_for(store, job.id, run=args.run, onto=args.onto, signer_fp=signer_fp)
            view, summary = _change_view(store, job, args.run)
            print("\n".join(view), file=sys.stderr if args.json else out, flush=True)
            _confirm(args, job, "authorise", out,
                     (*summary, _safe(f"  onto      {args.onto}, signed by {signer_fp}")))
            signature = _sign_bytes(sign, dadopt.authorise_message(payload))
            return land_job(store, job.id, onto=args.onto, signed=(payload, signature), data_root=args.data_root)

        return _landed(args, _guarded(store, args.job, "authorise", authorise_it), emit)
    if cmd == "land":
        agent = agent_caller()
        if agent is not None or args.capability is not None:
            # an agent lands only a job it filed (its capability), as it recommends one; the authority is still
            # the rule or the operator's earlier authorisation, never the capability
            agent, session = planner_session(args.agent, args.session)
            cap = planner_capability(store.directory, agent, session, args.job, args.capability)
            if cap is None:
                raise CapabilityRefused("an agent lands only a job it filed: this session's record holds no "
                                        f"capability for {str(args.job)[:40]!r}; pass --capability")
            store.check_capability(args.job, cap)

        def land_it(seen: dict[str, Job]) -> dadopt.Landing:
            seen["job"] = _must_get(store, args.job)
            return land_job(store, args.job, onto=args.onto, data_root=args.data_root)

        return _landed(args, _guarded(store, args.job, "land", land_it), emit)
    if cmd == "cancel":
        agent, session = planner_session(args.agent, args.session)

        def cancel_it(seen: dict[str, Job]) -> tuple[Job, str]:
            # every read from here on, the capability record's included, is inside the guard
            cap = None if args.human else planner_capability(store.directory, agent, session, args.job,
                                                             args.capability)
            if cap is not None:
                try:
                    return store.cancel_by_planner(args.job, cap), "planner"
                except TransitionRefused as exc:
                    now = store.get(args.job)
                    if now is None or now.state != "authorised":
                        raise
                    raise TransitionRefused(f"{exc}. The planner may cancel only before a human authorises; the "
                                            "operator can still cancel it, signed: `levain dispatch cancel --human`") \
                        from None
            if args.capability is None and not args.human:
                # the job is read before a word on how it would be cancelled: an unknown id is refused as unknown
                job = seen["job"] = _must_get(store, args.job)
                if job.state in TERMINAL:
                    raise TransitionRefused(f"job {job.id} is {job.state}, which is final; only a live job can be "
                                            "cancelled")
                print(f"levain dispatch: no capability for job {_safe(args.job)[:40]} in this session's record "
                      f"({agent or 'no agent'}, "
                      f"{'no session' if session == caps.NO_SESSION else _safe(session)[:80]}); cancelling as the "
                      "operator, signed.",
                      file=sys.stderr if args.json else out, flush=True)
            return _signed_cancel(args, store, seen, out), "human"

        job, by = _guarded(store, args.job, "cancel", cancel_it)
        if by == "planner":
            emit({"id": job.id, "state": job.state, "as": "planner"},
                 f"levain dispatch: cancelled job {job.id} as the PLANNER (its capability).")
            return EXIT_OK
        emit({"id": job.id, "state": job.state, "as": "human", "signer_fp": json.loads(job.history[-1].evidence)
              ["signer_fp"]},
             f"levain dispatch: cancelled job {job.id} as the HUMAN (signed); it holds none of its files now.")
        return EXIT_OK
    raise InputRefused(f"unknown dispatch command {cmd!r}")


def run_dispatch(args: argparse.Namespace, out: Any = None) -> int:
    """``levain dispatch <verb>``: exit 0, 2 (input or refusal) or 3 (a machine fault, by class)."""
    out = out if out is not None else sys.stdout
    try:
        return _run(args, out)
    except _NotDone as exc:
        print(f"levain dispatch: this machine could not do it ({type(exc.fault).__name__}): {_safe(exc.fault)}. "
              f"{exc.not_done}.", file=out, flush=True)
        return EXIT_FAULT
    except _FAULTS as exc:
        print(f"levain dispatch: this machine could not do it ({type(exc).__name__}): {_safe(exc)}", file=out,
              flush=True)
        return EXIT_FAULT
    except _REFUSALS as exc:
        print(f"levain dispatch: refused, nothing changed: {_safe(exc)}", file=out, flush=True)
        return EXIT_REFUSED


def add_parsers(subparsers: argparse._SubParsersAction) -> None:
    """``levain dispatch``, under the top-level subparsers."""
    d = subparsers.add_parser(
        "dispatch", help="File, read, sign or cancel a dispatch job (a planner's task run by cheaper executors).",
        description="A planner files a task contract; the operator reads it resolved and signs its digest once. "
                    "Filing creates a contract the operator must sign on their own device before anything runs; a "
                    "recommend confers no authority. Exit 0 done, 2 refused (nothing changed), 3 a fault on this "
                    "machine.")
    sub = d.add_subparsers(dest="dispatch_cmd", required=True)
    dhelp = "The dispatch directory (default: the consent directory, beside questions.db)."

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--dispatch-dir", dest="dispatch_dir", type=Path, default=None, help=dhelp)
        p.add_argument("--json", action="store_true", help="Machine-readable output.")
        p.set_defaults(func=run_dispatch)

    def planner(p: argparse.ArgumentParser) -> None:
        p.add_argument("--agent", default=None, choices=caps.AGENTS,
                       help="The planner agent, when none is detected (under a detected agent it must match).")
        p.add_argument("--session", default=None,
                       help="The planner session, when no agent is detected (under a detected agent it must match).")

    def data_root(p: argparse.ArgumentParser) -> None:
        p.add_argument("--data-root", dest="data_root", type=Path, default=None,
                       help="The cockpit data root whose authority log verifies the signature (default: the one the "
                            "consent hooks in the dispatch directory name).")

    def yes(p: argparse.ArgumentParser) -> None:
        p.add_argument("--yes", action="store_true",
                       help="Do not ask y/N; the whole view is still printed. Never from an agent's process.")

    def signer(p: argparse.ArgumentParser) -> None:
        p.add_argument("--signing-key", dest="signing_key", type=Path, default=None,
                       help="An enrolled key that signs (its private key file, or the .pub of a key an agent holds).")
        data_root(p)
        yes(p)

    f = sub.add_parser("file", help="File a task contract (planner); prints the job and its capability once.")
    f.add_argument("contract", help="The contract JSON file, or - for stdin.")
    f.add_argument("--idem-key", dest="idem_key", default=None,
                   help="A replay with the same key and contract returns the same job (and no capability).")
    f.add_argument("--data-root", dest="data_root", type=Path, default=None,
                   help="The cockpit data root the dispatch configuration is verified against (default: the one the "
                        "consent hooks in the dispatch directory name). Refused under an agent, as --dispatch-dir "
                        "is for file.")
    planner(f)
    sh = sub.add_parser("show", help="One job, resolved as the signer sees it.")
    sh.add_argument("job")
    ls = sub.add_parser("list", help="The live jobs (with --all, every job).")
    ls.add_argument("--repo", default=None, help="Only jobs in this repository (its absolute path).")
    ls.add_argument("--all", action="store_true", help="Include adopted, failed and cancelled jobs.")
    sg = sub.add_parser("sign", help="Sign a filed job's digest (the operator), after reading it resolved.")
    sg.add_argument("job")
    signer(sg)
    cn = sub.add_parser("cancel", help="Cancel a job: as its planner (capability), else as the operator (signed).")
    cn.add_argument("job")
    cn.add_argument("--reason", default="", help="One line kept with a signed cancel (may be empty).")
    cn.add_argument("--capability", default=None, help="The job's capability (default: this session's record).")
    cn.add_argument("--human", action="store_true",
                    help="Cancel as the operator (signed) even when this session's record holds the capability.")
    planner(cn)
    signer(cn)
    rc = sub.add_parser("recommend", help="Mark a verified job recommended (planner; advisory, authorises nothing).")
    rc.add_argument("job")
    rc.add_argument("--capability", default=None, help="The job's capability (default: this session's record).")
    rc.add_argument("--onto", default=None,
                    help="The branch you propose the operator authorise this onto (advisory; the phone's card).")
    rc.add_argument("--run", type=int, default=None,
                    help="The run you propose (advisory; default on the phone: the lowest passing run).")
    planner(rc)
    au = sub.add_parser("authorise", help="Authorise a verified run to land on a branch (the operator), signed.",
                        description="Shows the job, the change set the floor will judge and its landing tier, asks "
                                    "y/N, signs the authorise item for the run and branch, then lands it.")
    au.add_argument("job")
    au.add_argument("--run", type=int, required=True, help="The verified run to land (0..k-1).")
    au.add_argument("--onto", required=True, help="The branch to land on (a bare name, outside levain/).")
    signer(au)
    ld = sub.add_parser("land", help="Land a job by a standing adoption rule, or finish an authorised one.",
                        description="Exit 0 adopted, or a merge for you to run (its command alone on the last line); "
                                    "2 refused; 1 the job failed (its branch moved off the contract's base).")
    ld.add_argument("job")
    ld.add_argument("--onto", default=None, help="The branch a land by rule lands on (an authorised job's is fixed).")
    ld.add_argument("--capability", default=None,
                    help="The job's capability, for a planner (default: this session's record).")
    data_root(ld)
    planner(ld)
    cf = sub.add_parser(
        "config", help="The dispatch configuration (the operator): init the store, show it, sign or accept an item.",
        description="Templates, verifiers, the provider policy, the route map and the adoption rules are signed "
                    "configuration items, one hash chain, verified against the trust root pinned at init. An item "
                    "is shown (kind, generation, store, prev, and its change against the active body of its kind) "
                    "before it is signed or accepted.")
    csub = cf.add_subparsers(dest="config_cmd", required=True)
    ci = csub.add_parser("init", help="Create the control store and pin the operator's laptop key as its trust root.")
    ci.add_argument("--trust-root", dest="trust_root", required=True,
                    help="The fingerprint (SHA256:...) of an enrolled, active laptop key that covers T3, held at a "
                         "stated presence custody.")
    data_root(ci)
    cs = csub.add_parser("show", help="The active configuration: store, generation, head and each kind.")
    data_root(cs)
    cg = csub.add_parser("sign", help="Sign a body as the next item of its kind and accept it, after reading it.")
    cg.add_argument("kind", choices=dctl.KINDS)
    cg.add_argument("body", help="The item's body as a JSON file, or - for stdin.")
    signer(cg)
    ca = csub.add_parser("accept", help="Accept an item signed elsewhere (its canonical bytes and its signature).")
    ca.add_argument("item", help="The item's canonical bytes, as a file.")
    ca.add_argument("signature", help="Its SSHSIG signature, as a file.")
    data_root(ca)
    yes(ca)
    for p in (f, sh, ls, sg, cn, rc, au, ld, ci, cs, cg, ca):
        common(p)
