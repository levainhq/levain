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
