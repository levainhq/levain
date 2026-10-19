"""levain.dispatch.cli — ``levain dispatch file|show|list|sign|cancel|recommend``: the planner's door and the operator's.

A planner files a contract (``file``) and gets the job's capability once; the door keeps it in the planner session's
own record (:mod:`levain.dispatch.capability`) so a restarted session can still ``recommend`` or ``cancel``. The operator
reads a job RESOLVED as a signer sees it (``show``), and signs its digest (``sign``) or cancels it (``cancel``) with
an enrolled laptop key, after reading the same view and answering y/N on a terminal. ``cancel`` acts as the PLANNER when
the caller's session record holds the job's capability (or ``--capability`` is given), else as the HUMAN, signed, and
says which it was. Nothing a planner does here authorises anything.

Exit codes: 0 done; 2 the input or a refusal (the contract, the transition, the signature or the capability was
refused: the message says why, and nothing changed); 3 a fault on this machine, told apart by its CLASS, never by its
text: git could not run (:class:`levain.dispatch.contract.GitUnavailable`), a store or record that cannot be trusted
(:class:`levain.dispatch.jobs.JobStoreError`, :class:`levain.dispatch.capability.CapabilityRecordError`, an operating
system or database error), or a signature verifier that cannot run (:class:`levain.authority.VerifierUnavailable`).

Until the template registry and the provider policy live in signed control state (F3), :func:`resolvers` has neither,
and ``file`` refuses: every contract would be R2, which is never dispatched. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import unicodedata
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from levain.authority import VerifierUnavailable
from levain.dispatch import ContractRefused, DispatchError
from levain.dispatch import capability as caps
from levain.dispatch import sign as dsign
from levain.dispatch.contract import NEW, GitUnavailable, ResolvedTemplate
from levain.dispatch.jobs import (
    TERMINAL, CapabilityRefused, Job, JobStore, JobStoreError, SignatureRefused, TransitionRefused,
)
from levain.dispatch.policy import ProviderPolicy

__all__ = ["EXIT_FAULT", "EXIT_OK", "EXIT_REFUSED", "add_parsers", "file_job", "job_view", "planner_capability",
           "render", "resolvers", "run_dispatch"]

EXIT_OK, EXIT_REFUSED, EXIT_FAULT = 0, 2, 3
# a contract is a few kilobytes; the bound only stops a stray file from being read whole
_CONTRACT_MAX = 1024 * 1024
_TERM_UNSAFE = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
NO_REGISTRY = "no template registry is set up; every contract is R2, which is never dispatched"
# machine faults first: GitUnavailable is a ContractRefused and JobStoreError a DispatchError, and each must be read
# as the fault it is
_FAULTS = (GitUnavailable, JobStoreError, caps.CapabilityRecordError, VerifierUnavailable, sqlite3.Error, OSError)
_REFUSALS = (ContractRefused, TransitionRefused, SignatureRefused, CapabilityRefused, DispatchError, ValueError)


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

def planner_session(agent: str | None, session: str | None) -> tuple[str | None, str]:
    """The planner's agent and session: the flags, else the agent's own environment, as ``levain ask`` reads them;
    ``(None, NO_SESSION)`` when no agent can be told."""
    from levain.consent.band import _agent_and_session

    try:
        agent, session = _agent_and_session(agent, session)
    except ValueError:
        return None, caps.NO_SESSION
    return agent, session if caps.session_problem(session) is None else caps.NO_SESSION


def file_job(store: JobStore, submission: Any, *, agent: str | None, session: str, idem_key: str | None = None,
             resolved: tuple[Callable[[str], ResolvedTemplate | None], ProviderPolicy] | None = None,
             ) -> tuple[Job, str | None, str]:
    """File ``submission`` for the planner ``agent``/``session`` and keep its capability in that session's record;
    returns the job, its capability (``None`` on an idempotent replay) and one sentence on where the capability is
    kept. ``resolved`` is (template resolver, policy), :func:`resolvers` when not given."""
    resolved = resolved if resolved is not None else resolvers()
    if resolved is None:
        raise ContractRefused(NO_REGISTRY)
    resolve_template, policy = resolved
    job, capability = store.file(submission, resolve_template=resolve_template, policy=policy,
                                 filer_session=session, idem_key=idem_key)
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
            "lease_end": job.lease_end, "signer_fp": job.signer_fp,
            "history": [{"seq": h.seq, "to_state": h.to_state, "cause": h.cause, "at": h.at, "evidence": h.evidence}
                        for h in job.history]}


def _entries(rows: list[dict[str, str]]) -> list[str]:
    return [f"    {_safe(e['path'])}  " + ("(new file)" if e["mode"] == NEW else f"{e['mode']} {e['blob']}")
            for e in rows]


def render(job: Job) -> str:
    """The job as text, every field of :func:`job_view`, the goal in full."""
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
        "  goal (sent to the provider, in full):",
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
    return "\n".join(lines)


# --- the commands -------------------------------------------------------------------------------------------------------

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
        raise ValueError(f"the contract file cannot be read: {exc.strerror or exc}") from None
    if len(raw) > _CONTRACT_MAX:
        raise ValueError(f"the contract is over {_CONTRACT_MAX} bytes")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ValueError("the contract is not JSON") from None


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


def _confirm(args: argparse.Namespace, what: str) -> None:
    """Ask y/N on the terminal; refuse without one unless ``--yes``. The caller has printed the whole view."""
    if args.yes:
        return
    if not _is_tty():
        raise ValueError(f"{what} asks y/N on a terminal and there is none; pass --yes to {what} without asking "
                         "(the whole view is still printed first)")
    try:
        answer = input(f"{what} this job? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        raise ValueError("not confirmed; nothing was signed and the job is unchanged")


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
            raise ValueError("name the cockpit data root with --data-root (the consent hooks here name "
                             + ("none" if not roots else f"{len(roots)}: {', '.join(map(str, roots))}") + ")")
        root = roots[0]
    if not (root / "cockpit.db").is_file():
        raise ValueError(f"{root} holds no cockpit (no cockpit.db); name the cockpit data root with --data-root")
    return authority_source(root / "cockpit.db", anchor=anchor_path(root))


def _signer(args: argparse.Namespace, namespace: str) -> tuple[str, Callable[[bytes], str]]:
    from levain.cockpit.keys import KeyError_, ssh_signer

    if args.signing_key is None:
        raise ValueError("signing needs --signing-key (an enrolled key: its private key file, or the .pub of a key an "
                         "agent holds)")
    try:
        return ssh_signer(args.signing_key, namespace=namespace)
    except KeyError_ as exc:
        raise ValueError(str(exc)) from None


def _show_first(args: argparse.Namespace, job: Job, out: Any) -> None:
    # the view always precedes a signature; under --json it goes to stderr so stdout stays one JSON document
    print(render(job), file=sys.stderr if args.json else out, flush=True)


def _signed_cancel(args: argparse.Namespace, store: JobStore, job: Job, out: Any) -> Job:
    from levain.cockpit.keys import KeyError_

    problem = dsign.reason_problem(args.reason)
    if problem is not None:
        raise ValueError(problem)
    if job.state in TERMINAL:
        raise TransitionRefused(f"job {job.id} is {job.state}, which is final; only a live job can be cancelled")
    _show_first(args, job, out)
    authority = _authority(args, store)
    signer_fp, sign = _signer(args, dsign.DISPATCH_CANCEL_NAMESPACE)
    payload = dsign.cancel_payload(job, signer_fp=signer_fp, signed_at=datetime.now(timezone.utc), reason=args.reason)
    _confirm(args, "cancel")
    try:
        signature = sign(dsign.sign_message(payload))
    except KeyError_ as exc:
        raise ValueError(str(exc)) from None
    return store.cancel_by_human(job.id, payload, signature, authority)


def _run(args: argparse.Namespace, out: Any) -> int:
    store = _store(args)
    cmd = args.dispatch_cmd

    def emit(data: Any, text: str) -> None:
        print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else text, file=out, flush=True)

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
             "\n".join(f"{j.id}  {j.state:<12} {j.contract.template.template_id:<16} lease {j.lease_end}  "
                       f"{_safe(j.repo)}" for j in jobs)
             or ("levain dispatch: no jobs." if args.all else "levain dispatch: no live jobs (--all for every job)."))
        return EXIT_OK
    if cmd == "sign":
        from levain.cockpit.keys import KeyError_

        job = _must_get(store, args.job)
        if job.state != "filed":
            raise SignatureRefused(f"job {job.id} is {job.state}; only a filed job can be signed")
        _show_first(args, job, out)
        authority = _authority(args, store)
        signer_fp, sign = _signer(args, dsign.DISPATCH_SIGN_NAMESPACE)
        payload = dsign.sign_payload(job, signer_fp=signer_fp, signed_at=datetime.now(timezone.utc))
        _confirm(args, "sign")
        try:
            signature = sign(dsign.sign_message(payload))
        except KeyError_ as exc:
            raise ValueError(str(exc)) from None
        job = store.sign(job.id, payload, signature, authority)
        emit({"id": job.id, "state": job.state, "signer_fp": job.signer_fp, "lease_end": job.lease_end},
             f"levain dispatch: signed job {job.id} as {job.signer_fp}; it is {job.state} until {job.lease_end}.")
        return EXIT_OK
    if cmd in ("cancel", "recommend"):
        agent, session = planner_session(args.agent, args.session)
        cap = None if getattr(args, "human", False) else planner_capability(
            store.directory, agent, session, args.job, args.capability)
        if cmd == "recommend":
            if cap is None:
                raise CapabilityRefused("recommend is the filing planner's alone: this session's record holds no "
                                        f"capability for {str(args.job)[:40]!r}; pass --capability")
            job = store.recommend(args.job, cap)
            emit({"id": job.id, "state": job.state, "as": "planner"},
                 f"levain dispatch: job {job.id} is recommended (advisory: it authorises nothing).")
            return EXIT_OK
        if cap is not None:
            try:
                job = store.cancel_by_planner(args.job, cap)
            except TransitionRefused as exc:
                now = store.get(args.job)
                if now is None or now.state != "authorised":
                    raise
                raise TransitionRefused(f"{exc}. The planner may cancel only before a human authorises; the "
                                        "operator can still cancel it, signed: `levain dispatch cancel --human`") \
                    from None
            emit({"id": job.id, "state": job.state, "as": "planner"},
                 f"levain dispatch: cancelled job {job.id} as the PLANNER (its capability).")
            return EXIT_OK
        job = _signed_cancel(args, store, _must_get(store, args.job), out)
        emit({"id": job.id, "state": job.state, "as": "human", "signer_fp": json.loads(job.history[-1].evidence)
              ["signer_fp"]},
             f"levain dispatch: cancelled job {job.id} as the HUMAN (signed); it holds none of its files now.")
        return EXIT_OK
    raise ValueError(f"unknown dispatch command {cmd!r}")


def run_dispatch(args: argparse.Namespace, out: Any = None) -> int:
    """``levain dispatch <verb>``: exit 0, 2 (input or refusal) or 3 (a machine fault, by class)."""
    out = out if out is not None else sys.stdout
    try:
        return _run(args, out)
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
                       help="The planner agent (default: read from the agent's environment).")
        p.add_argument("--session", default=None, help="The planner session (default: from the agent's environment).")

    def signer(p: argparse.ArgumentParser) -> None:
        p.add_argument("--signing-key", dest="signing_key", type=Path, default=None,
                       help="An enrolled key that signs (its private key file, or the .pub of a key an agent holds).")
        p.add_argument("--data-root", dest="data_root", type=Path, default=None,
                       help="The cockpit data root whose authority log verifies the signature (default: the one the "
                            "consent hooks in the dispatch directory name).")
        p.add_argument("--yes", action="store_true",
                       help="Sign without asking y/N (needed without a terminal); the whole view is still printed.")

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
