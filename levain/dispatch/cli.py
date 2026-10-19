"""levain.dispatch.cli — ``levain dispatch file|show|list|sign|cancel|recommend``: the planner's door and the
operator's.

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

Exit codes: 0 done; 2 the input or a refusal (the contract, the transition, the signature, the capability or the
command line was refused: the message says why, and nothing changed); 3 a fault on this machine, told apart by its
CLASS, never by its text: git could not run (:class:`levain.dispatch.contract.GitUnavailable`), a store, record or
log that cannot be read or trusted (:class:`levain.dispatch.jobs.JobStoreError`,
:class:`levain.dispatch.capability.CapabilityRecordError`, an operating system or database error, a ``ValueError``
from below), a signature verifier that cannot run (:class:`levain.authority.VerifierUnavailable`) or an ssh-keygen
that cannot run to sign (:class:`SignerUnavailable`). Every exit 3 from ``sign`` or ``cancel`` also says what did NOT
happen. The command line's own refusals are :class:`InputRefused`, so nothing raised below it reads as "refused".

Until the template registry and the provider policy live in signed control state (F3), :func:`resolvers` has neither,
and ``file`` refuses: every contract would be R2, which is never dispatched. Stdlib only.

Known edges: a read path made of homoglyphs shows as the path it imitates (a write path is bounded by the operator's
template, so only what is read can be dressed up). ``file`` reports a template resolver that raises as a refusal
(exit 2), not a fault; revisited when F3's resolver is wired in.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import unicodedata
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from levain.authority import VerifierUnavailable
from levain.dispatch import ContractRefused, DispatchError
from levain.dispatch import capability as caps
from levain.dispatch import sign as dsign
from levain.dispatch.contract import NEW, GitUnavailable, ResolvedTemplate
from levain.dispatch.jobs import (
    TERMINAL, CapabilityRefused, Job, JobStore, JobStoreError, SignatureRefused, TransitionRefused,
)
from levain.dispatch.policy import ProviderPolicy

__all__ = ["EXIT_FAULT", "EXIT_OK", "EXIT_REFUSED", "InputRefused", "SignerUnavailable", "add_parsers", "agent_caller",
           "confirm_summary", "file_job", "job_view", "planner_capability", "render", "resolvers", "run_dispatch"]

EXIT_OK, EXIT_REFUSED, EXIT_FAULT = 0, 2, 3
# a contract is a few kilobytes; the bound only stops a stray file from being read whole
_CONTRACT_MAX = 1024 * 1024
_TERM_UNSAFE = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
NO_REGISTRY = "no template registry is set up; every contract is R2, which is never dispatched"


class InputRefused(DispatchError):
    """The command line's own refusal: no terminal to ask on, a file it cannot read, a flag that does not fit, a
    signing key that would not sign. The message says why, and nothing changed."""


class SignerUnavailable(Exception):
    """ssh-keygen could not run (or timed out) to make the operator's signature: a fault on this machine, not a key
    that refused. Told apart by the cause chained to the key error, never by its message."""


class _NotDone(Exception):
    """A machine fault in ``sign`` or the human ``cancel``, with the sentence that says what did NOT happen."""

    def __init__(self, fault: BaseException, not_done: str) -> None:
        super().__init__(str(fault))
        self.fault, self.not_done = fault, not_done


# machine faults first: GitUnavailable is a ContractRefused and JobStoreError a DispatchError, and each must be read
# as the fault it is; a ValueError raised below the command line is a fault too (its own refusals are InputRefused)
_FAULTS = (GitUnavailable, JobStoreError, caps.CapabilityRecordError, VerifierUnavailable, SignerUnavailable,
           sqlite3.Error, OSError, ValueError)
_REFUSALS = (ContractRefused, TransitionRefused, SignatureRefused, CapabilityRefused, DispatchError)


def resolvers() -> tuple[Callable[[str], ResolvedTemplate | None], ProviderPolicy] | None:
    """The template registry and the provider policy a filing resolves through, or ``None``. ``None`` until F3
    places both in signed, generation-checked control state: a policy or registry read from a file an agent can
    write would let the planner choose its own gate."""
    return None


def _safe(text: object) -> str:
    """``text`` with every character a terminal could act on (or show as something else) written as its escape."""
    return "".join((f"\\u{ord(c):04x}" if ord(c) <= 0xFFFF else f"\\U{ord(c):08x}")
                   if unicodedata.category(c) in _TERM_UNSAFE else c for c in str(text))


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
             ) -> tuple[Job, str | None, str]:
    """File ``submission`` for the planner ``agent``/``session`` and keep its capability in that session's record;
    returns the job, its capability (``None`` on an idempotent replay) and one sentence on where the capability is
    kept. ``resolved`` is (template resolver, policy), :func:`resolvers` when not given.

    The session's record is checked before the filing (:func:`levain.dispatch.capability.preflight`): a record that
    cannot be kept for a reason already on disk is a fault on this machine, and nothing is filed. A record that
    still fails after the filing does not undo it: the job IS filed, so the capability is shown this once with
    "NOT kept" and the call succeeds; a retry with the same ``idem_key`` is the idempotent replay, which files
    nothing new."""
    resolved = resolved if resolved is not None else resolvers()
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


def _read_contract(source: str) -> Any:
    try:
        if source == "-":
            raw = sys.stdin.buffer.read(_CONTRACT_MAX + 1)
        else:
            with open(Path(source).expanduser(), "rb") as fh:
                raw = fh.read(_CONTRACT_MAX + 1)
    except OSError as exc:
        raise InputRefused(f"the contract file cannot be read: {exc.strerror or exc}") from None
    if len(raw) > _CONTRACT_MAX:
        raise InputRefused(f"the contract is over {_CONTRACT_MAX} bytes")
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


def _confirm(args: argparse.Namespace, job: Job, what: str, out: Any) -> None:
    """Ask y/N on the terminal, right below :func:`confirm_summary`; refuse without a terminal unless ``--yes``.
    The caller has printed the whole view."""
    if args.yes:
        return
    if not _is_tty():
        raise InputRefused(f"{what} asks y/N on the operator's terminal and this process has none; run "
                           f"`levain dispatch {what} {job.id}` in your own terminal")
    print("\n".join(confirm_summary(job, what)), file=sys.stderr if args.json else out, flush=True)
    try:
        answer = input(f"{what} this job? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        raise InputRefused(f"not confirmed; nothing was done and job {job.id} is unchanged")


def _authority(args: argparse.Namespace, store: JobStore) -> Any:
    """The authority source a laptop signature is verified against: ``--data-root``, else the one cockpit the
    consent hooks in the dispatch directory verify against; refused before anything is signed when there is no
    single cockpit there."""
    from levain.authority import anchor_path
    from levain.consent.answering import hook_roots
    from levain.consent.queue import authority_source

    if args.data_root is not None:
        root = args.data_root.expanduser()
    else:
        roots = hook_roots(store.directory)
        if len(roots) != 1:
            raise InputRefused("name the cockpit data root with --data-root (the consent hooks here name "
                             + ("none" if not roots else f"{len(roots)}: {', '.join(map(str, roots))}") + ")")
        root = roots[0]
    if not (root / "cockpit.db").is_file():
        raise InputRefused(f"{root} holds no cockpit (no cockpit.db); name the cockpit data root with --data-root")
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
    """``payload`` signed. An ssh-keygen that could not run or timed out (the chained cause) is
    :class:`SignerUnavailable`, a fault; a key that would not sign (a cancelled touch, a refusing exit) is the
    operator's refusal."""
    from levain.cockpit.keys import KeyError_

    try:
        return sign(dsign.sign_message(payload))
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
    state = now.state if now is not None else f"{last.state} when last read"
    if verb == "sign":
        return f"nothing was signed; job {shown} is " + ("unchanged " if now is None or last is None
                                                         or now.state == last.state else "") + f"({state})"
    if now is not None and now.state in TERMINAL:
        return f"this call cancelled nothing; job {shown} is {now.state}"
    return f"job {shown} is NOT cancelled; it is still {state} and holds its files"


def _guarded(store: JobStore, job_id: str, verb: str, act: Callable[[dict[str, Job]], _T]) -> _T:
    """``act(seen)`` with every machine fault it raises, the first store read included, said as what ``verb`` did
    NOT do to ``job_id``; ``act`` puts the job in ``seen["job"]`` once it has read it."""
    seen: dict[str, Job] = {}
    try:
        return act(seen)
    except _FAULTS as exc:
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


def _run(args: argparse.Namespace, out: Any) -> int:
    store = _store(args)
    cmd = args.dispatch_cmd

    def emit(data: Any, text: str) -> None:
        # ASCII-escaped JSON: no C1 control or line separator reaches a terminal raw
        print(json.dumps(data, ensure_ascii=True, indent=2) if args.json else text, file=out, flush=True)

    if cmd == "file":
        submission = _read_contract(args.contract)
        agent, session = planner_session(args.agent, args.session)
        job, capability, kept = file_job(store, submission, agent=agent, session=session, idem_key=args.idem_key)
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
        jobs = [j for j in store.list(repo=args.repo) if args.all or j.state not in TERMINAL]
        emit([{"id": j.id, "state": j.state, "repo": j.repo, "filed_at": j.filed_at, "lease_end": j.lease_end,
               "template": j.contract.template.template_id, "digest": j.digest} for j in jobs],
             "\n".join(_safe(f"{j.id}  {j.state:<12} {j.contract.template.template_id:<16} lease {j.lease_end}  "
                             f"{j.repo}") for j in jobs)
             or ("levain dispatch: no jobs." if args.all else "levain dispatch: no live jobs (--all for every job)."))
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
        job = store.recommend(args.job, cap)
        emit({"id": job.id, "state": job.state, "as": "planner"},
             f"levain dispatch: job {job.id} is recommended (advisory: it authorises nothing).")
        return EXIT_OK
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

    def signer(p: argparse.ArgumentParser) -> None:
        p.add_argument("--signing-key", dest="signing_key", type=Path, default=None,
                       help="An enrolled key that signs (its private key file, or the .pub of a key an agent holds).")
        p.add_argument("--data-root", dest="data_root", type=Path, default=None,
                       help="The cockpit data root whose authority log verifies the signature (default: the one the "
                            "consent hooks in the dispatch directory name).")
        p.add_argument("--yes", action="store_true",
                       help="Do not ask y/N; the whole view is still printed. Never from an agent's process.")

    f = sub.add_parser("file", help="File a task contract (planner); prints the job and its capability once.")
    f.add_argument("contract", help="The contract JSON file, or - for stdin.")
    f.add_argument("--idem-key", dest="idem_key", default=None,
                   help="A replay with the same key and contract returns the same job (and no capability).")
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
    planner(rc)
    for p in (f, sh, ls, sg, cn, rc):
        common(p)
