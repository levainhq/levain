"""levain.agents.cli — ``levain agents``: the seam (A0 r6 §3.7). It opens the agent store through
:func:`levain.agents.store.open_agent_store` and says whether the agent schema is the one this levain writes. The
verbs of §3.6 arrive with their slices; so far ``role add|show`` (P1) and ``register|list|show|pause|resume|retire|zone``
(P2) exist.

Exit 0: done (bare ``agents``: the agent schema is this levain's). Exit 1: refused, the reason on stderr.
``--store-dir`` belongs to ``agents`` and goes before ``role``.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from levain.agents.role import MAX_VERSION, RoleError, add_role, get_role, latest_version, parse_role_text, role_by_hash
from levain.agents.refuse import AuditError, audit_verdicts, check_spec, derived_flags, refinement_of
from levain.agents.spec import (SpecError, finalize_spec, find_revert, get_agent, list_agents, load_json,
                                parse_spec, register_specs, retire_agent, set_operation, stored_specs)
from levain.agents.store import (AgentStoreRefused, get_default_zone, open_agent_store, open_agent_store_view,
                                 set_default_zone)
from levain.autonomic.agent_tables import AGENT_SCHEMA
from levain.autonomic.db import StoreFormatError


def _version_arg(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text[:40]!r} is not an integer") from None
    if not 1 <= value <= MAX_VERSION:
        raise argparse.ArgumentTypeError(f"a version is an integer from 1 to {MAX_VERSION}")
    return value


def add_parsers(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "agents", help="Named agents on the autonomic store (the store check; `role add|show`).",
        allow_abbrev=False,
        description="Open the autonomic store's agent tables and check them against this levain's schema. "
                    "Refuses a /3 store, and a store whose agent tables are not the ones this levain writes. "
                    "Verbs: `role add|show` (the role registry). Give --store-dir before `role`.")
    p.add_argument("--store-dir", dest="store_dir", type=Path, default=None,
                   help="The autonomic store directory (default: $LEVAIN_HOME/autonomic).")
    p.set_defaults(func=run_agents)
    verbs = p.add_subparsers(dest="agents_verb", metavar="VERB")
    role = verbs.add_parser("role", help="The role registry (P1): add, show.", allow_abbrev=False,
                            description="Roles are content-hashed and versioned. Give --store-dir before `role`.")
    rverbs = role.add_subparsers(dest="role_verb", metavar="VERB", required=True)
    add = rverbs.add_parser("add", help="Register a role from a JSON file; unknown fields are refused.",
                            allow_abbrev=False,
                            description="Register a role from a JSON file. --store-dir goes before `role`: "
                                        "levain agents --store-dir DIR role add FILE.")
    add.add_argument("file", type=Path, help="The role file (JSON).")
    add.set_defaults(func=run_role_add)
    show = rverbs.add_parser("show", help="Print a role, its version and its role_hash.", allow_abbrev=False,
                             description="Print a role, its version and its role_hash. --store-dir goes before "
                                         "`role`: levain agents --store-dir DIR role show NAME.")
    show.add_argument("name", help="The role name.")
    show.add_argument("--version", type=_version_arg, default=None, help="A version (default: the latest).")
    show.set_defaults(func=run_role_show)
    _add_p2_parsers(verbs)
    _add_signing_parsers(verbs)
    _add_revoke_parsers(verbs)
    _add_fence_parsers(verbs)


def run_agents(args: argparse.Namespace) -> int:
    try:
        store = open_agent_store(args.store_dir)
    except AgentStoreRefused as exc:
        print(f"levain agents: {exc}", file=sys.stderr)
        return 1
    except (StoreFormatError, sqlite3.Error, OSError) as exc:
        print(f"levain agents: cannot open the autonomic store: {exc}", file=sys.stderr)
        return 1
    print(f"agent schema {AGENT_SCHEMA}: ok ({store.path})")
    return 0


def _open(args: argparse.Namespace):
    try:
        return open_agent_store(args.store_dir)
    except AgentStoreRefused as exc:
        print(f"levain agents: {exc}", file=sys.stderr)
    except (StoreFormatError, sqlite3.Error, OSError) as exc:
        print(f"levain agents: cannot open the autonomic store: {exc}", file=sys.stderr)
    return None


def _open_view(args: argparse.Namespace):
    try:
        return open_agent_store_view(args.store_dir)
    except AgentStoreRefused as exc:
        print(f"levain agents: {exc}", file=sys.stderr)
    except (StoreFormatError, sqlite3.Error, OSError) as exc:
        print(f"levain agents: cannot read the autonomic store: {exc}", file=sys.stderr)
    return None


def run_role_add(args: argparse.Namespace) -> int:
    try:
        role = parse_role_text(args.file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        print(f"levain agents role add: cannot read {args.file}: {exc}", file=sys.stderr)
        return 1
    except RoleError as exc:
        print(f"levain agents role add: {args.file}: {exc}", file=sys.stderr)
        return 1
    store = _open(args)
    if store is None:
        return 1
    try:
        name, digest, version, created = add_role(store, role)
    except RoleError as exc:
        print(f"levain agents role add: {exc}", file=sys.stderr)
        return 1
    except (sqlite3.Error, StoreFormatError) as exc:
        print(f"levain agents role add: the store refused it: {exc}", file=sys.stderr)
        return 1
    if created:
        print(f"role {name} v{version} added: {digest}")
        return 0
    try:
        latest = latest_version(store, name)
    except (RoleError, sqlite3.Error, StoreFormatError):
        latest = version
    if latest is not None and latest != version:
        print(f"role {name} v{version} already registered: {digest}; v{version} is registered but the latest is "
              f"v{latest}, and role show serves v{latest}")
    else:
        print(f"role {name} v{version} already registered: {digest}")
    return 0


def run_role_show(args: argparse.Namespace) -> int:
    store = _open(args)
    if store is None:
        return 1
    try:
        digest, version, role = get_role(store, args.name, args.version)
    except RoleError as exc:
        print(f"levain agents role show: {exc}", file=sys.stderr)
        return 1
    except (sqlite3.Error, StoreFormatError) as exc:
        print(f"levain agents role show: the store refused it: {exc}", file=sys.stderr)
        return 1
    try:  # one write of the whole text, so a stdout that cannot encode it prints nothing
        print(f"role {args.name} v{version} {digest}\n" + json.dumps(role, indent=2, sort_keys=True, ensure_ascii=False))
    except UnicodeEncodeError as exc:
        print(f"levain agents role show: stdout cannot encode the role ({exc.encoding}); "
              f"set PYTHONIOENCODING=utf-8", file=sys.stderr)
        return 1
    return 0


# ---- P2: register, list, show, pause, resume, retire, zone -----------------------------------------------------

def _add_p2_parsers(verbs: argparse._SubParsersAction) -> None:
    reg = verbs.add_parser("register", help="Register AgentSpec(s) from JSON; refuses what DESIGN 4.1 refuses.",
                           allow_abbrev=False,
                           description="Register one spec, a JSON list of specs (a feeder group goes in one file), or, "
                                       "with --warn only, an audit file. --warn judges and stores nothing and exits 0. "
                                       "Give --store-dir before `register`.")
    reg.add_argument("--from", dest="source", type=Path, required=True, help="The spec / audit file (JSON).")
    reg.add_argument("--warn", action="store_true", help="Report verdicts, store nothing, exit 0.")
    reg.add_argument("--json", action="store_true", help="Verdicts as JSON.")
    reg.add_argument("--revise", action="store_true",
                     help="A name a live agent holds with new content writes that agent's next version.")
    reg.set_defaults(func=run_register)
    lst = verbs.add_parser("list", help="List agents (retired ones with --all).", allow_abbrev=False)
    lst.add_argument("--all", action="store_true", help="Include retired agents.")
    lst.set_defaults(func=run_list)
    show = verbs.add_parser("show", help="Print an agent: axes, version, spec_hash, spec.", allow_abbrev=False)
    show.add_argument("agent", help="An agent name (not retired) or agent_id.")
    show.add_argument("--version", type=_version_arg, default=None, help="A version (default: the current one).")
    show.add_argument("--json", action="store_true",
                      help="One JSON object: the agent, its occurrences, and the episodes stored under its agent id.")
    show.set_defaults(func=run_show)
    for verb, fn, text in (("pause", run_pause, "Pause an agent and its feeder group (only resume clears it)."),
                           ("resume", run_resume, "Resume an agent and its feeder group (the only clear of a pause)."),
                           ("retire", run_retire, "Retire an agent; lists its readers and feeder group.")):
        p = verbs.add_parser(verb, help=text, allow_abbrev=False)
        p.add_argument("agent", help="An agent name (not retired) or agent_id.")
        p.set_defaults(func=fn)
    zone = verbs.add_parser("zone", help="Show or set the install's default IANA zone.", allow_abbrev=False,
                            description="The install's one default zone (agent_settings row default_zone). While it "
                                        "is unset, register refuses a schedule that carries no zone.")
    zone.add_argument("iana", nargs="?", default=None, help="An IANA zone, e.g. America/New_York.")
    zone.set_defaults(func=run_zone)


def _fail(verb: str, exc: Exception) -> int:
    print(f"levain agents {verb}: {exc}", file=sys.stderr)
    return 1


def _specs_from(doc):
    if isinstance(doc, dict):
        return [doc]
    if isinstance(doc, list) and doc:
        return doc
    raise SpecError("the file holds neither a spec nor a non-empty list of specs")


def run_register(args: argparse.Namespace) -> int:
    try:
        doc = load_json(args.source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return _fail("register", f"cannot read {args.source}: {exc}")
    except SpecError as exc:
        return _fail("register", f"{args.source}: {exc}")
    if isinstance(doc, dict) and isinstance(doc.get("jobs"), list):
        if not args.warn:
            return _fail("register", "an audit file holds verdicts, not AgentSpecs: use --warn")
        try:
            verdicts = audit_verdicts(doc)
        except AuditError as exc:
            return _fail("register", f"{args.source}: {exc}")
        _print_verdicts(verdicts, args.json, audit=True)
        return 0
    store = _open_view(args) if args.warn else _open(args)     # --warn writes nothing, not even a new store file
    if store is None:
        return 1
    try:
        raw = _specs_from(doc)
        default_zone = get_default_zone(store)
        stored = stored_specs(store)
    except (SpecError, ValueError, sqlite3.Error, StoreFormatError) as exc:
        return _fail("register", str(exc))
    # phase 1: judge EVERY spec against the stored state plus the rest of the file (a feeder group's members see one
    # another as registered); phase 2: write them all in one transaction, or nothing when any one is refused
    rows: list[tuple[str, dict | None, list[str], dict]] = []
    for i, obj in enumerate(raw):
        try:
            rows.append(("", parse_spec(obj), [], {}))
        except SpecError as exc:
            rows.append((_label_of(obj, i), None, [f"rule 3: {exc}"], {}))
    names = [s["name"] for _, s, _, _ in rows if s is not None]
    counts = Counter(names)
    twice = {n for n, c in counts.items() if c > 1}
    lookup = {**stored, **{s["name"]: s for _, s, _, _ in rows if s is not None}}
    for label, spec, reasons, info in rows:
        if spec is None:
            continue
        held = set(stored)
        try:
            same = stored.get(spec["name"]) == finalize_spec(spec, default_zone)
        except SpecError:
            same = False
        revert = find_revert(store, spec, default_zone)
        if args.revise or same or revert:
            held.discard(spec["name"])
        try:
            role, bad_row = role_by_hash(store, spec["role"]["role_hash"]), None
        except RoleError as exc:        # a stored role that fails its own checks is not a role this spec may pin
            role, bad_row = None, f"rule 3: the store's role row is refused: {exc}"
            reasons.append(bad_row)
        # with the row refused, rule 3's own "not registered" would misname the cause, so only the refusal stands
        reasons += [str(r) for r in check_spec(spec, role=role, default_zone=default_zone, names_held=held,
                                               lookup=lookup) if not (bad_row and r.rule == "3")]
        if revert:
            reasons.append("rule 4: " + revert)
        if spec["name"] in twice:
            reasons.append(f"rule 4: name {spec['name']!r} appears {counts[spec['name']]} times in this file: a "
                           f"registration writes one version of a name (a feeder group names each member once)")
        info["refinement"] = refinement_of(spec)
        if role is not None:
            info["review_required"] = derived_flags(role)["review_required"]
    refused = [label or spec["name"] for label, spec, reasons, _ in rows if reasons]
    entries = []
    for label, spec, reasons, info in rows:
        entry = {"verdict": "refused" if reasons else "pass", "reasons": reasons, **info}
        if not reasons and refused:
            entry.update(verdict="refused", reasons=[
                "held back: its own checks pass, but a file registers whole or not at all and "
                + ", ".join(dict.fromkeys(refused)) + " refused"])
        entries.append((label or spec["name"], spec, entry))
    if not refused and not args.warn:
        try:
            written = register_specs(store, [s for _, s, _ in entries], default_zone, revise=args.revise)
        except (SpecError, sqlite3.Error, StoreFormatError) as exc:
            for _, _, entry in entries:
                entry.update(verdict="refused", reasons=[f"nothing written: {exc}"])
        else:
            for (_, _, entry), (agent_id, digest, version, created) in zip(entries, written):
                entry.update(agent_id=agent_id, spec_hash=digest, version=version, created=created)
    out_rows: dict = {}
    seen: Counter = Counter()
    for label, _, entry in entries:
        key = label
        while key in out_rows:          # a per-label counter, skipping any key the file itself already took
            seen[label] += 1
            key = f"{label}#{seen[label]}"
        out_rows[key] = entry
    _print_verdicts({"jobs": out_rows}, args.json, audit=False)
    code = 1 if any(e["verdict"] == "refused" for e in out_rows.values()) else 0
    return 0 if args.warn else code


def _label_of(obj, index: int) -> str:
    """An unparsable spec's label: its name when one can be read, else its index in the file."""
    name = obj.get("name") if isinstance(obj, dict) else None
    if isinstance(name, str) and name.strip() and name.isprintable():
        return name[:64]
    return f"spec[{index}]"


def _print_verdicts(verdicts: dict, as_json: bool, *, audit: bool) -> None:
    if as_json:
        print(json.dumps(verdicts, indent=1, ensure_ascii=False))
        return
    for name, v in verdicts["jobs"].items():
        reason = v["reason"] if audit else "; ".join(v["reasons"])
        extra = "" if audit or not v.get("agent_id") else \
            f" {v['agent_id']} v{v['version']} {'registered' if v['created'] else 'already registered'}"
        print(f"{v['verdict']:8} {name}" + (f": {reason}" if reason else "") + extra)


def run_list(args: argparse.Namespace) -> int:
    store = _open(args)
    if store is None:
        return 1
    try:
        rows = list_agents(store, include_retired=args.all)
    except (sqlite3.Error, StoreFormatError) as exc:
        return _fail("list", f"the store refused it: {exc}")
    for a in rows:
        print(f"{a['name']}  {a['agent_id']}  v{a['current_version']}  {a['registration']}  {a['operation']}")
    return 0


def run_show(args: argparse.Namespace) -> int:
    store = _open(args)
    if store is None:
        return 1
    try:
        a = get_agent(store, args.agent, args.version)
    except SpecError as exc:
        return _fail("show", str(exc))
    except (sqlite3.Error, StoreFormatError) as exc:
        return _fail("show", f"the store refused it: {exc}")
    if args.json:
        return _show_json(store, a)
    try:
        role, why = role_by_hash(store, a["spec"]["role"]["role_hash"]), "role not resolvable"
    except RoleError as exc:
        role, why = None, f"role refused: {exc}"
    flags = derived_flags(role) if role else None
    head = (f"agent {a['name']} {a['agent_id']} v{a['version']} (versions {a['versions']}) {a['spec_hash']}\n"
            f"registration: {a['registration']}  operation: {a['operation']}  arming: derived from the authority "
            f"log (P5a), not stored here  failure_streak: {a['failure_streak']}\n"
            + (f"review: {'required' if flags['review_required'] else 'not required'} "
               f"(calls_model={flags['calls_model']}, acts_outward={flags['acts_outward']}); the date is the link's "
               f"expires_at\n" if flags else f"review: {why}\n"))
    try:
        print(head + json.dumps(a["spec"], indent=2, sort_keys=True, ensure_ascii=False))
    except UnicodeEncodeError as exc:
        return _fail("show", f"stdout cannot encode the spec ({exc.encoding}); set PYTHONIOENCODING=utf-8")
    return 0


def _show_json(store, a: dict) -> int:
    """``show --json``: the agent, its occurrences (newest first), and the episodes stored under its agent id in the
    install's episodic store (``session_install.json``). An install with no episodic store says why, never an empty
    list that reads as "no episodes"."""
    from datetime import datetime, timezone
    from levain.agents import recall as _recall, session_wire
    try:
        with store.read() as conn:
            occ = [dict(zip(("occurrence_id", "state", "reason", "kind", "launch_epoch", "launched_at", "ended_at"), r))
                   for r in conn.execute("SELECT occurrence_id, state, reason, kind, launch_epoch, launched_at,"
                                         " ended_at FROM agent_occurrences WHERE agent_id = ?"
                                         " ORDER BY admitted_at DESC, occurrence_id", (a["agent_id"],))]
    except sqlite3.Error as exc:
        return _fail("show", f"the store refused it: {exc}")
    episodes, note = None, None
    try:
        path = session_wire.read_install(store.directory).episodic_path
    except session_wire.InstallRefused as exc:
        path, note = None, f"no episodic store: {exc}"
    if path is None and note is None:
        note = "no episodic store: the install names no episodic_path"
    if path is not None:
        try:
            found = _recall.recall_agent_episodes_at(path, a["agent_id"], now=datetime.now(timezone.utc))
            episodes = [{"episode_id": getattr(e, "id", None), "type": str(getattr(getattr(e, "type", None), "value",
                                                                                    getattr(e, "type", None))),
                         "source": getattr(e, "source", None), "timestamp": getattr(e, "timestamp", None),
                         "content": getattr(e, "content", None), "metadata": _recall._metadata_of(e)}
                        for e in found.episodes]
        except Exception as exc:  # noqa: BLE001 - an unreadable memory is said, not shown as empty
            note = f"episodes unreadable ({type(exc).__name__})"
    doc = {"agent": {k: a[k] for k in ("name", "agent_id", "version", "versions", "spec_hash", "registration",
                                       "operation", "failure_streak") if k in a},
           "spec": a["spec"], "occurrences": occ, "episodes": episodes, "episodes_note": note}
    try:
        print(json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False, default=str))
    except UnicodeEncodeError as exc:
        return _fail("show", f"stdout cannot encode the record ({exc.encoding}); set PYTHONIOENCODING=utf-8")
    return 0


def _run_operation(args: argparse.Namespace, verb: str, operation: str) -> int:
    store = _open(args)
    if store is None:
        return 1
    try:
        members = set_operation(store, args.agent, operation)
    except SpecError as exc:
        return _fail(verb, str(exc))
    except (sqlite3.Error, StoreFormatError) as exc:
        return _fail(verb, f"the store refused it: {exc}")
    for agent, changed in members:
        print(f"{agent['name']} {agent['agent_id']}: {operation}" + ("" if changed else " (already)"))
    return 0


def run_pause(args: argparse.Namespace) -> int:
    return _run_operation(args, "pause", "paused")


def run_resume(args: argparse.Namespace) -> int:
    return _run_operation(args, "resume", "active")


def run_retire(args: argparse.Namespace) -> int:
    store = _open(args)
    if store is None:
        return 1
    try:
        agent, reader, group = retire_agent(store, args.agent)
    except SpecError as exc:
        return _fail("retire", str(exc))
    except (sqlite3.Error, StoreFormatError) as exc:
        return _fail("retire", f"the store refused it: {exc}")
    print(f"{agent['name']} {agent['agent_id']}: retired (operation stays {agent['operation']})")
    print(f"reader the output named: {reader or '(none)'}")
    print("feeder group left behind: " + (", ".join(group) if group else "(none)"))
    return 0


def run_zone(args: argparse.Namespace) -> int:
    store = _open(args)
    if store is None:
        return 1
    try:
        if args.iana is None:
            zone = get_default_zone(store)
            print(zone if zone else "unset (set one with: levain agents zone <IANA>)")
            return 0
        zone = set_default_zone(store, args.iana)
    except ValueError as exc:
        return _fail("zone", str(exc))
    except (sqlite3.Error, StoreFormatError) as exc:
        return _fail("zone", f"the store refused it: {exc}")
    print(f"default zone: {zone}")
    return 0


# --- sign, renew (P5a-2a): both through levain.agents.signing, the one signing entry ---------------------------------

def _add_signing_parsers(verbs: argparse._SubParsersAction) -> None:
    sign = verbs.add_parser("sign", help="Sign an agent link from a JSON file (a laptop-presence key).",
                            allow_abbrev=False,
                            description="Sign the agent link in FILE: its terms as the log's record has them. A "
                                        "missing id is drawn, a missing kind is agent-link, and a missing expires_at "
                                        "is --days from now.")
    sign.add_argument("file", type=Path, help="The link record (JSON).")
    sign.add_argument("--data-root", dest="data_root", type=Path, required=True, help="The cockpit's data root.")
    sign.add_argument("--signing-key", dest="signing_key", type=Path, required=True,
                      help="A laptop-presence key; it may ask for a touch or a passphrase.")
    sign.add_argument("--days", type=_link_days_arg, default=29, help="Life of a link with no expires_at (default 29).")
    sign.set_defaults(func=run_sign)
    renew = verbs.add_parser("renew", help="Move a live agent link's expiry (its terms stay).", allow_abbrev=False)
    renew.add_argument("link_id", help="The link's id (al-...).")
    renew.add_argument("--data-root", dest="data_root", type=Path, required=True, help="The cockpit's data root.")
    renew.add_argument("--signing-key", dest="signing_key", type=Path, required=True,
                       help="A laptop-presence key; it may ask for a touch or a passphrase.")
    renew.add_argument("--days", type=_link_days_arg, default=29, help="New life from now (default 29).")
    renew.set_defaults(func=run_renew)


def _link_days_arg(text: str) -> int:
    from levain.authority import AGENT_LINK_MAX

    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text[:40]!r} is not an integer") from None
    if not 1 <= value <= AGENT_LINK_MAX.days:
        raise argparse.ArgumentTypeError(f"a link's life is 1 to {AGENT_LINK_MAX.days} days")
    return value


def _signing_refusals() -> tuple[type[BaseException], ...]:
    from levain.authority import AuthorityError
    from levain.cockpit.firejournal import CockpitFormatError
    from levain.cockpit.keys import KeyError_

    # ssh_signer and its sign() raise KeyError_ for every expected failure (an ssh-keygen that cannot run, times out,
    # exits non-zero or returns no valid signature) and VerifierUnavailable (an AuthorityError); anything else is a bug
    # and keeps its traceback.
    return (KeyError_, AuthorityError, OSError, UnicodeDecodeError, sqlite3.Error, CockpitFormatError, SpecError,
            ValueError, OverflowError)


def run_sign(args: argparse.Namespace) -> int:
    import secrets
    from datetime import datetime, timedelta, timezone

    from levain.agents.signing import sign_agent_link
    from levain.authority import AGENT_LINK_KIND
    from levain.cockpit.consent import utc_stamp
    from levain.cockpit.keys import ssh_signer

    try:
        grant = load_json(args.file.read_text(encoding="utf-8"))
        if not isinstance(grant, dict):
            raise ValueError("the link file is not a JSON object")
        now = datetime.now(timezone.utc)
        grant.setdefault("kind", AGENT_LINK_KIND)
        grant.setdefault("id", "al-" + secrets.token_hex(6))
        grant.setdefault("expires_at", utc_stamp(now + timedelta(days=args.days)))
        signer_fp, sign = ssh_signer(args.signing_key)
        new = sign_agent_link(args.data_root, grant, signer_fp=signer_fp, sign=sign,
                              clock=lambda: datetime.now(timezone.utc))
    except _signing_refusals() as exc:
        return _fail("sign", exc)
    print(f"signed: {grant['id']} at seq {new.seq}, until {grant['expires_at']}")
    if new.anchor_warning:
        print(f"warning: {new.anchor_warning}", file=sys.stderr)
    return 0


def run_renew(args: argparse.Namespace) -> int:
    from datetime import datetime, timedelta, timezone

    from levain.agents.signing import renew_agent_link
    from levain.cockpit.keys import ssh_signer

    try:
        signer_fp, sign = ssh_signer(args.signing_key)
        new, expires_at = renew_agent_link(args.data_root, args.link_id, life=timedelta(days=args.days),
                                           signer_fp=signer_fp, sign=sign,
                                           clock=lambda: datetime.now(timezone.utc))
    except _signing_refusals() as exc:
        return _fail("renew", exc)
    print(f"renewed: {args.link_id} at seq {new.seq}, until {expires_at}")
    if new.anchor_warning:
        print(f"warning: {new.anchor_warning}", file=sys.stderr)
    return 0


# ---- revoke (P5c-2c): the bar protocol's two surfaces, through levain.agent_authority's steps -----------------------

REVOKE_SIGN_ROUNDS = 3      # gestures one run asks for: a head that moved (or a bar prepared again) asks for one more


def _add_revoke_parsers(verbs: argparse._SubParsersAction) -> None:
    rv = verbs.add_parser(
        "revoke", help="Revoke all of an agent's links (a laptop-presence key), or --abandon an open revoke.",
        allow_abbrev=False,
        description="Bar the agent now, then sign one revoke link and append it (A0 section 2.2). --abandon AGENT "
                    "closes the agent's open bar without a signature; a bar is never deleted, and a revoke that "
                    "landed first stands.")
    rv.add_argument("agent", help="The agent id (ag-...).")
    rv.add_argument("--abandon", action="store_true", help="Close the agent's open bar instead of revoking.")
    rv.add_argument("--data-root", dest="data_root", type=Path, default=None,
                    help="The cockpit's data root (a revoke needs it; --abandon does not).")
    rv.add_argument("--signing-key", dest="signing_key", type=Path, default=None,
                    help="A laptop-presence key; it may ask for a touch or a passphrase (a revoke needs it).")
    rv.set_defaults(func=run_revoke)


def _utcnow():
    """The revoke verbs' clock (one place to replace it)."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _raise_attention(key: str, text: str) -> None:
    """The abandon's attention record: printed, keyed. (The agent store holds no attention table yet, so the line on
    stderr is the record a caller can route.)"""
    print(f"attention [{key}]: {text}", file=sys.stderr)


def run_revoke(args: argparse.Namespace) -> int:
    if args.abandon:
        return _run_abandon(args)
    if args.data_root is None or args.signing_key is None:
        return _fail("revoke", "a revoke needs --data-root and --signing-key")
    return _run_revoke_signed(args)


def _run_revoke_signed(args: argparse.Namespace) -> int:
    from levain import agent_authority as aa
    from levain.agents import bars
    from levain.authority import anchor_path
    from levain.cockpit.keys import _journal, ssh_signer

    store = _open(args)
    if store is None:
        return 1
    clock = lambda: _utcnow()      # noqa: E731  (looked up per call)
    begun = False          # begin has run: a bar may be open, so every later failure says the agent stays barred
    bar_id: str | None = None
    try:
        signer_fp, sign = ssh_signer(args.signing_key)
        db = _journal(args.data_root)
        with db.autocommit() as cockpit:
            autonomic = store._db._connect()        # a connection with no transaction open: the steps own theirs
            try:
                sid, why = bars._store_id(autonomic, None)
                if sid is None:
                    return _fail("revoke", why)
                anchor = anchor_path(db.data_root)
                begun = True
                step = aa.revoke_agent_begin(autonomic, cockpit, args.agent, store_id=sid, signer_fp=signer_fp,
                                             clock=clock)
                bar_id = step.bar_id
                _say(step)
                rounds = 0
                while step.state in ("prepared", "stale_head", "reprepared"):
                    if rounds >= REVOKE_SIGN_ROUNDS:
                        print(f"the agent stays barred; the revoke is not signed in: run `levain agents revoke "
                              f"{args.agent}` again, or --abandon it", file=sys.stderr)
                        return 1
                    rounds += 1
                    step = aa.revoke_agent_sign(autonomic, step.bar_id, sign, clock=clock)
                    if step.state != "signed":
                        _say(step)
                        continue
                    step = aa.revoke_agent_finish(autonomic, cockpit, step.bar_id, clock=clock, anchor=anchor)
                    _say(step)
                if step.state not in _REVOKE_TERMINAL:
                    return _fail("revoke", f"unexpected revoke state {step.state!r}: {step.text}" + _STAYS_BARRED)
            finally:
                autonomic.close()
    except aa.AgentAuthorityError as exc:
        landed = _landed_seq(args, bar_id) if bar_id is not None else None
        if landed is not None:
            print(f"the revoke had already landed at seq {landed}")
            return 0
        return _fail("revoke", f"{exc}" + (_STAYS_BARRED if begun else ""))
    except (_signing_refusals() + (StoreFormatError,)) as exc:
        return _fail("revoke", f"{exc}" + (_STAYS_BARRED if begun else ""))
    return 0     # landed, landed on an abandoned bar, or abandoned: each is the true outcome (A0 section 2.2)


_REVOKE_TERMINAL = ("landed", "landed_bar_abandoned", "abandoned")
_STAYS_BARRED = "; the agent stays barred; run revoke again or --abandon"


def _say(step) -> None:
    """Print a revoke step; an abandoned one says plainly that nothing was revoked (the exit stays 0, c6(b))."""
    if step.state == "abandoned":
        print(f"{step.text}; the agent is NOT revoked")
    else:
        print(step.text)


def _landed_seq(args: argparse.Namespace, bar_id: str) -> int | None:
    """The seq this call's own bar closed with, when a concurrent revoke landed it first; None otherwise (or on any
    store trouble: the caller then reports the failure it already has)."""
    from levain.agents import bars

    try:
        store = _open(args)
        if store is None:
            return None
        conn = store._db._connect()
        try:
            bar = bars.read_bar(conn, bar_id)
        finally:
            conn.close()
    except (sqlite3.Error, StoreFormatError):
        return None
    if bar is not None and bar.reason in ("signed", "from_log") and bar.revoke_seq is not None:
        return bar.revoke_seq
    return None


def _run_abandon(args: argparse.Namespace) -> int:
    from levain.agents import bars

    store = _open(args)
    if store is None:
        return 1
    try:
        autonomic = store._db._connect()
        try:
            bar = bars.open_bar_of(autonomic, args.agent)
            if bar is None:
                return _no_open_bar(autonomic, args.agent)
            if bars.close_bar(autonomic, bar.bar_id, revoke_seq=None, reason="abandoned", now=_utcnow()):
                _raise_attention(f"agent-abandon:{bar.bar_id}",
                                 f"the revoke of {args.agent} was abandoned; the agent's links stand as signed")
                print(f"abandoned: the bar {bar.bar_id} is closed; nothing was appended")
                return 0
            return _no_open_bar(autonomic, args.agent, bar_id=bar.bar_id)     # step 4 closed it between the two
        finally:
            autonomic.close()
    except (sqlite3.Error, StoreFormatError) as exc:
        return _fail("revoke", f"the store refused it: {exc}")


def _no_open_bar(autonomic: sqlite3.Connection, agent_id: str, *, bar_id: str | None = None) -> int:
    """The abandon found no row to close. With a ``bar_id`` this call saw the bar open and lost the close: say what the
    bar's own record says (a revoke that landed first stands). With none, no revoke was ever open to this call: 1."""
    from levain.agents import bars

    row = bars.read_bar(autonomic, bar_id) if bar_id is not None else None
    if row is not None and not isinstance(row, bars.Bar):
        row = bars._bar(row)
    if row is not None and row.reason in ("signed", "from_log") and row.revoke_seq is not None:
        print(f"the revoke had already landed at seq {row.revoke_seq}; nothing to abandon")
        return 0
    print(f"levain agents revoke: no open revoke for {agent_id}; nothing to abandon", file=sys.stderr)
    return 1


# ---- the agent fence (P7-2 wiring, shape C): `levain agents fence start|status` -----------------------------------

class FenceSetupError(Exception):
    """The operator's fence unit cannot be built for this host (no isolation install, no cockpit root)."""


class NoIsolationInstall(FenceSetupError):
    """This host has no isolation install (no ``L/policy``): no queue broker to fence for."""


def _add_fence_parsers(verbs: argparse._SubParsersAction) -> None:
    fence = verbs.add_parser(
        "fence", help="The operator's agent fence service the queue broker asks on every dispatch tool call.",
        allow_abbrev=False,
        description="The agent fence runs as you (never root) and answers the queue broker, once per dispatch GATE, "
                    "whether the session's agent is still current. While it is down every dispatch session's tool "
                    "calls are refused. A host has one fence unit, for one install. `start` installs and starts it "
                    "for this install (idempotent), replacing a unit another install holds; `levain update` runs "
                    "the same install but leaves another install's unit alone. `status` shows the unit and the "
                    "server's own STATUS.")
    fverbs = fence.add_subparsers(dest="fence_verb", metavar="VERB", required=True)
    for verb, fn, text in (("start", run_fence_start, "Install and start the per-user fence unit (idempotent)."),
                           ("status", run_fence_status, "The fence unit's state and the server's STATUS.")):
        p = fverbs.add_parser(verb, help=text, allow_abbrev=False)
        p.add_argument("--path", type=Path, default=None,
                       help="The install whose cockpit root the fence reads (default: cwd).")
        p.set_defaults(func=fn)


def _host() -> str:
    return "darwin" if sys.platform == "darwin" else "linux"


def fence_roots(install: Path, *, agent_store: Path | None = None) -> tuple[Path, Path]:
    """The agent store and cockpit root the fence server reads for ``install``, resolved: ``agent_store`` (default
    :func:`levain.autonomic.db.default_store_dir`, the store every ``levain agents`` verb opens) and the install's
    cockpit root, derived as ``levain serve --write`` derives it (its write scope's ledger root's ``cockpit/``).
    ``levain doctor`` compares the server's STATUS with these."""
    from levain.autonomic.db import default_store_dir
    from levain.dashboard import _resolve_source

    scope = _resolve_source(Path(install).expanduser()).write_scope
    if scope is None:
        raise FenceSetupError(f"{install} has no writable install, so it has no cockpit root to fence on")
    store = agent_store if agent_store is not None else default_store_dir()
    return Path(store).expanduser().resolve(), (scope.ledger_root / "cockpit").expanduser().resolve()


def fence_spec(install: Path, *, agent_store: Path | None = None, host: str | None = None,
               store_root: Path | None = None, run_dir: Path | None = None):
    """The fence unit's :class:`levain.daemon.DaemonSpec` for ``install``, with ``run_dir`` = this host's ``R``
    (:data:`levain.slot.layout.RUN`, where the queue broker and ``levain doctor`` look). Refuses
    (:class:`FenceSetupError`) on a host without the isolation install (no ``L/policy``): there is no broker to answer
    and no ``_levain_qbroker`` account, so the server could only exit. ``store_root``/``run_dir``: a test's seams."""
    from levain import daemon
    from levain.slot import layout

    host = host or _host()
    L = store_root if store_root is not None else layout.STORE.get(host)
    R = run_dir if run_dir is not None else layout.RUN.get(host)
    if L is None or R is None or not (Path(L) / "policy").is_dir():
        raise NoIsolationInstall("this host has no isolation install (no L/policy), so there is no queue broker to "
                              "fence for: run `levain slot install` first")
    store, cockpit = fence_roots(install, agent_store=agent_store)
    return daemon.build_fence_spec(agent_store=store, cockpit_root=cockpit, run_dir=Path(R))


class UnreadableFenceUnit(Exception):
    """The fence unit on disk exists but its agent store and cockpit root cannot be read from it."""


def _unit_roots(argv: list[str]) -> tuple[Path, Path]:
    """The ``(agent store, cockpit root)`` a fence unit's argv names, as :func:`levain.daemon.build_fence_spec`
    renders them (``<python> -m <FENCE_MODULE> --agent-store S --cockpit-root C --run R``). Raises
    :class:`UnreadableFenceUnit` for any other shape."""
    from levain import daemon

    try:
        at = next(i for i in range(len(argv) - 1) if argv[i] == "-m" and argv[i + 1] == daemon.FENCE_MODULE)
    except StopIteration:
        raise UnreadableFenceUnit(f"its command does not run {daemon.FENCE_MODULE}") from None
    rest = argv[at + 2:]
    found: dict[str, str] = {}
    for flag in ("--agent-store", "--cockpit-root"):
        hits = [i for i, a in enumerate(rest) if a == flag]
        if len(hits) != 1 or hits[0] + 1 >= len(rest):
            raise UnreadableFenceUnit(f"its command does not name {flag} exactly once")
        found[flag] = rest[hits[0] + 1]
    return Path(found["--agent-store"]), Path(found["--cockpit-root"])


def _installed_fence_roots(provider) -> tuple[Path, Path] | None:
    """The roots of the fence unit already installed on this host, read from its unit file through the provider
    (never from a running server's STATUS), or None when there is no unit. Raises on a unit that cannot be read."""
    from levain import daemon

    argv = provider.installed_argv(daemon.DEFAULT_FENCE_LABEL)
    return None if argv is None else _unit_roots(argv)


def _roots_text(roots: tuple[Path, Path]) -> str:
    store, cockpit = roots
    return f"{cockpit} (agent store {store})"


def install_fence(install: Path, *, provider=None, **kw) -> str:
    """``levain agents fence start``: install and start the fence unit through the provider every ``levain daemon
    install*`` verb uses (:meth:`levain.daemon.DaemonProvider.install`: idempotent, resident, kickstarted now and at
    login). The operator named this install, so a unit holding another install's roots (one host has one fence unit
    and one ``R/fence/fence.sock``) is replaced, and the returned text names the old roots and the new. Raises
    :class:`FenceSetupError` or :class:`levain.daemon.DaemonError`."""
    from levain import daemon

    spec = fence_spec(install, **kw)
    if provider is None:
        provider = daemon.select_provider()
    note = ""
    try:
        old = _installed_fence_roots(provider)
    except Exception as exc:   # noqa: BLE001 - said; the operator asked for this install's unit
        note = f"the installed fence unit could not be read ({type(exc).__name__}: {exc}); replacing it\n"
    else:
        new = _unit_roots(spec.argv)
        if old is not None and old != new:
            note = f"replacing the fence unit for {_roots_text(old)} with this install's {_roots_text(new)}\n"
    _ensure_fence_store(spec)
    return note + provider.install(spec)


def _ensure_fence_store(spec) -> None:
    """The unit's WorkingDirectory is the agent store, and launchd exits 78 (EX_CONFIG) when it is absent or cannot be
    entered, so it is made (the store's own mode; the umask can only narrow it) and checked immediately before either
    install path hands the unit to the provider. Raises :class:`FenceSetupError`; nothing is installed after it."""
    store = Path(spec.working_dir)
    try:
        store.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise FenceSetupError(f"the agent store {store} does not exist and could not be created "
                              f"({type(exc).__name__}: {exc}); nothing was installed") from exc
    if not (store.is_dir() and os.access(store, os.R_OK | os.W_OK | os.X_OK)):
        raise FenceSetupError(f"the agent store {store} is not a directory this user can enter and write; "
                              "nothing was installed")


def refresh_fence_after_update(install: Path, *, provider=None, **kw) -> str | None:
    """``levain update``'s step: re-install the fence unit so an upgrade re-writes it from the interpreter and paths it
    now runs from (the unit names both). None on a host without the isolation install (nothing to fence).

    The automatic path never takes the host's one fence unit from another install (P7-2 wiring r2, codex HIGH): a
    unit whose recorded agent store or cockpit root differs from this install's, or one whose roots cannot be read,
    is left alone, and the line returned names both and the explicit command that replaces it. A failure is said in
    the line returned, never the reason the update fails: ``levain doctor``'s ``agent fence`` row and every refused
    dispatch GATE say it too."""
    from levain import daemon

    try:
        spec = fence_spec(install, **kw)
    except NoIsolationInstall:
        return None
    except FenceSetupError as exc:
        return f"agent fence: not installed: {exc}"
    except Exception as exc:   # noqa: BLE001 - said, never the reason the update fails
        return (f"agent fence: the unit could not be installed ({type(exc).__name__}: {exc}); every dispatch "
                "session's tool calls are refused until `levain agents fence start` succeeds")
    command = f"levain agents fence start --path {Path(install).expanduser().resolve()}"
    try:
        if provider is None:
            provider = daemon.select_provider()
        try:
            old = _installed_fence_roots(provider)
        except Exception as exc:   # noqa: BLE001 - an unread unit is left alone, and said
            return (f"agent fence: left alone: the installed unit could not be read ({type(exc).__name__}: {exc}); "
                    f"`{command}` replaces it")
        new = _unit_roots(spec.argv)
        if old is not None and old != new:
            return (f"agent fence: left alone: the installed unit fences {_roots_text(old)}, not this install's "
                    f"{_roots_text(new)}; `{command}` points it here")
        try:
            _ensure_fence_store(spec)
        except FenceSetupError as exc:
            return f"agent fence: not installed: {exc}"
        result = provider.install(spec)
    except Exception as exc:   # noqa: BLE001 - said, never the reason the update fails
        return (f"agent fence: the unit could not be installed ({type(exc).__name__}: {exc}); every dispatch "
                "session's tool calls are refused until `levain agents fence start` succeeds")
    return "agent fence: " + result.splitlines()[0] if result else "agent fence: installed"


def run_fence_start(args: argparse.Namespace) -> int:
    from levain import daemon

    install = args.path if args.path is not None else Path.cwd()
    try:
        print(install_fence(install, agent_store=args.store_dir))
    except (NotImplementedError, FenceSetupError, daemon.DaemonError) as exc:
        return _fail("fence start", exc)
    return 0


def run_fence_status(args: argparse.Namespace) -> int:
    from levain import daemon, doctor

    install = args.path if args.path is not None else Path.cwd()
    try:
        provider = daemon.select_provider()
    except NotImplementedError as exc:
        return _fail("fence status", exc)
    st = provider.status(daemon.DEFAULT_FENCE_LABEL)
    print(f"{daemon.DEFAULT_FENCE_LABEL}: installed={st.installed} running={st.running}")
    print(f"  {st.detail}")
    try:
        expected = fence_roots(install, agent_store=args.store_dir)
    except FenceSetupError as exc:
        return _fail("fence status", exc)
    rows = doctor._check_agent_fence(expected=expected)
    if not rows:
        print("agent fence: this host has no isolation install, so nothing is fenced")
        return 1
    ok = True
    for r in rows:
        print(f"agent fence: {r.detail}")
        if not r.ok:
            ok = False
            if r.hint:
                print(f"  {r.hint}")
    return 0 if ok else 1
