"""levain.dream.cli — ``levain dream`` (BUILD_PLAN r7 R-16; D1_EXECUTOR §11).

D1-1a's ``run``: ``levain dream run --store-dir <copy> --mind claude|ollama`` consolidates a COPY of a store through
:func:`levain.dream.executor.consolidate_store`. D1-1b added ``rollback``, ``recover`` and ``drill``; D1-1c added
``ready`` (the day-ready predicate, read-only); D1-2 added ``enable`` (registers the role and spec; signs nothing). ``run`` refuses the operator's live store
(``~/.anneal-memory``): it is for copies, and it is never pointed at that dir by default."""
from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import sys
import time
import uuid
from pathlib import Path


def add_parsers(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("dream", help="Dreaming: run on a store copy; rollback, recover and drill.",
                              allow_abbrev=False,
                              description="Consolidate a store through a compose mind (`run`, on a COPY of a store), "
                                          "and roll back, recover or drill a dream. Nothing schedules a dream.")
    p.set_defaults(func=run_dream)
    sub = p.add_subparsers(dest="dream_command")
    r = sub.add_parser("run", allow_abbrev=False, help="Consolidate a store COPY once.",
                       description="One consolidate on a store copy: lock, prepare, compose (checked on a scratch "
                                   "copy, up to 3 composes), save. Writes usage.json and progress.json under the "
                                   "occurrence dir. The claude mind spends real money; the ollama mind sends the "
                                   "episodes to its endpoint.")
    r.add_argument("--store-dir", required=True, help="A directory holding memory.db (or .levain/memory.db): a COPY.")
    r.add_argument("--mind", required=True, choices=("claude", "ollama"))
    r.add_argument("--model", default=None, help="claude: the pinned model id (must have a price row); "
                                                 "ollama: the compose model.")
    r.add_argument("--claude-bin", default="claude", help="The claude executable (tests point this at a stub).")
    r.add_argument("--base-url", default="http://localhost:11434", help="ollama: the endpoint.")
    r.add_argument("--api-key-env", default=None, help="ollama: the NAME of the env var holding the key.")
    r.add_argument("--instructions", default=None, help="A file of consolidate instructions (else anneal's package).")
    r.add_argument("--max-seconds", type=float, default=None,
                   help="Wall bound in seconds on the COMPOSE and the checks (default 3000; must be positive). It does "
                        "NOT bound the final save, which runs after the bound is disarmed on purpose (a hard exit "
                        "mid-save would be worse; the saving-phase journal is the recovery); no compose starts unless "
                        "the save's time reserve is left.")
    r.add_argument("--occurrence-dir", default=None, help="Where progress.json and usage.json go "
                                                          "(default <store dir>/dream/<time>-<random id>); never "
                                                          "inside ~/.anneal-memory.")
    r.add_argument("--dry-run", action="store_true", help="Prepare and print the package, compose nothing.")
    r.add_argument("--json", action="store_true", help="Print the typed result as JSON on stdout.")
    r.set_defaults(func=run_dream_run)
    rb = sub.add_parser("rollback", allow_abbrev=False, help="Restore the memory text a dream replaced (one step).",
                        description="Restore the memory TEXT the dream replaced, from its snapshot. One step: only the "
                                    "newest dream, only if the live text is what it saved or replaced. memory.db is "
                                    "never restored, so the day's episodes stay counted as consolidated.")
    _store_and_occurrence(rb)
    rb.add_argument("--who", default="operator", help="Recorded in rollback.json.")
    rb.add_argument("--json", action="store_true")
    rb.set_defaults(func=run_dream_rollback)
    rc = sub.add_parser("recover", allow_abbrev=False, help="Settle the store side of a dream that did not end cleanly.",
                        description="Cancel an unfinished wrap by token, end an orphaned compose child after an identity "
                                    "check, and record a committed save that was never recorded. Refuses while a run "
                                    "holds the store lock.")
    _store_and_occurrence(rc)
    rc.add_argument("--state", choices=("launched", "stopped"), default="launched",
                    help="The occurrence's state as the supervisor knows it.")
    rc.add_argument("--card-filed", action="store_true", help="The occurrence's card already exists.")
    rc.add_argument("--json", action="store_true")
    rc.set_defaults(func=run_dream_recover)
    rd = sub.add_parser("ready", allow_abbrev=False, help="Is a dream due for this dream-day? (the day-ready predicate)",
                        description="Run the six day-ready checks against a store COPY and print ready, quiet or "
                                    "unreadable with every check recorded. Reads only; it consolidates nothing.")
    rd.add_argument("--store-dir", required=True, help="A directory holding memory.db (or .levain/memory.db): a COPY.")
    rd.add_argument("--agent-dir", default=None, help="The directory holding the agent store (autonomic.db) with the "
                                                      "occurrence table; omitted means no occurrence history.")
    rd.add_argument("--coord-dir", default=None, help="The coordinator queue directory (dispatch.db, awake.json); "
                                                      "omitted means no launches and no work threads.")
    rd.add_argument("--agent-id", default="dream")
    rd.add_argument("--day", default=None, help="The dream-day YYYY-MM-DD (default: the one holding --now; with "
                    "--manual, the one a run-now at --now would consolidate).")
    rd.add_argument("--now", default=None, help="An aware ISO time standing in for the clock (default: now).")
    rd.add_argument("--zone", default=None, help="An IANA zone (default: the TZ variable, else the host's zone file).")
    rd.add_argument("--manual", action="store_true", help="Evaluate as a manual run (waives checks 5 and 6).")
    rd.add_argument("--json", action="store_true")
    rd.set_defaults(func=run_dream_ready)
    dr = sub.add_parser("drill", allow_abbrev=False, help="Prove the latest snapshot restores, on a scratch copy.",
                        description="Hash the latest snapshot, integrity-check its db backup, run the real rollback on a "
                                    "scratch copy of the store and open the result. Never touches the store itself.")
    dr.add_argument("--store-dir", required=True, help="A directory holding memory.db (or .levain/memory.db).")
    dr.add_argument("--root", default=None, help="The occurrences dir (default dream/ beside the store's resolved memory.db).")
    dr.add_argument("--json", action="store_true")
    dr.set_defaults(func=run_dream_drill)
    sh = sub.add_parser("shadow", allow_abbrev=False, help="Dry shadow night: read flow's snapshot, seed a scratch store, diff.",
                        description="Read flow's committed pre-dream snapshot for a dream-day (read-only, hash-checked). "
                                    "With --shadow-dir, seed it into that SCRATCH store and compare the episode set; "
                                    "with --composed too, diff an already-composed shadow output against flow's and "
                                    "write card_diff.txt. Composes nothing and files nothing.")
    sh.add_argument("--flow-root", required=True, type=Path, help="Flow's state/dream directory (read only).")
    sh.add_argument("--day", required=True, help="The dream-day YYYY-MM-DD.")
    sh.add_argument("--shadow-dir", type=Path, default=None,
                    help="A scratch directory to seed (created with a .levain-shadow marker; a non-empty directory "
                         "without it is refused). Omitted: only the reader and the hash check run.")
    sh.add_argument("--composed", type=Path, default=None, help="A file holding the shadow's composed neocortex text.")
    sh.add_argument("--occurrence-dir", type=Path, default=None, help="Where card_diff.txt goes (needs --composed).")
    sh.add_argument("--occurrence", default="shadow-dry", help="The occurrence id the card names.")
    sh.add_argument("--agent-dir", default=None, help="The shadow's agent store dir, for the no-shadow-done check.")
    sh.add_argument("--agent-id", default=None)
    sh.add_argument("--zone", default=None, help="An IANA zone (needed with --agent-dir).")
    sh.add_argument("--json", action="store_true")
    sh.set_defaults(func=run_dream_shadow)
    en = sub.add_parser("enable", allow_abbrev=False, help="Register the dream as a named agent and print its signing card.",
                        description="Register the shipped `dream` role and AgentSpec in the agent store (idempotent) and "
                                    "print the words its link's signing card carries. It signs nothing: the link is "
                                    "his signature, and the dream launches only under a live signed link.")
    en.add_argument("--agent-store-dir", type=Path, default=None,
                    help="The autonomic store directory (default: $LEVAIN_HOME/autonomic).")
    en.add_argument("--store-dir", type=Path, default=None,
                    help="The anneal store the dream consolidates (a dir holding memory.db or .levain/memory.db). "
                         "With it, the install facts the scheduled fire reads are written beside the agent store; "
                         "without it none are, and a fire refuses naming the missing file.")
    en.add_argument("--cockpit-root", type=Path, default=None,
                    help="The cockpit root the authority log is replayed from (default: the home's, "
                         "<levain home>/cockpit).")
    en.add_argument("--coord-dir", type=Path, default=None,
                    help="The coordinator queue directory (default: <levain home>/coordinator).")
    en.add_argument("--shadow-flow-root", type=Path, default=None,
                    help="SHADOW install (the D1-3 recipe): flow's state/dream directory this install reads its input "
                         "from. --store-dir is then the SCRATCH directory re-seeded each night (never ~/.anneal-memory); "
                         "needs --flow-instructions.")
    en.add_argument("--flow-instructions", type=Path, default=None,
                    help="With --shadow-flow-root: flow's instructions file (global/CONTINUITY_MANAGEMENT.md), copied "
                         "read-only into the agent store dir as the compose's instructions, and copied again at "
                         "each night's seed.")
    en.add_argument("--replace-install", action="store_true",
                    help="Allow this enable to turn an existing live install into a shadow, or a shadow into a live "
                         "one (refused without it: both answer to the same signed link).")
    en.add_argument("--revise", action="store_true", help="Write a new version when the stored spec differs.")
    en.add_argument("--json", action="store_true")
    en.set_defaults(func=run_dream_enable)


def _store_and_occurrence(p: argparse.ArgumentParser) -> None:
    p.add_argument("occurrence", help="An occurrence id under <store dir>/dream/, or the path of its dir.")
    p.add_argument("--store-dir", required=True, help="A directory holding memory.db (or .levain/memory.db).")


def run_dream(args: argparse.Namespace) -> int:
    print("levain dream: choose run, rollback, recover, ready, drill or enable (see `levain dream --help`)",
          file=sys.stderr)
    return 1


def run_dream_enable(args: argparse.Namespace) -> int:
    from levain.agents.store import AgentStoreRefused, get_default_zone, open_agent_store
    from levain.dream import agent as da
    from levain.dream import executor as ex

    try:
        store = open_agent_store(args.agent_store_dir)
        zone = get_default_zone(store)
    except AgentStoreRefused as exc:
        print(f"levain dream enable: {exc}", file=sys.stderr)
        return 1
    # Every refusal of the install is judged before the dream is registered and before the instructions copy or the
    # facts file is written (the scratch directory is never created here: the first seed makes it).
    if (args.shadow_flow_root is not None or args.flow_instructions is not None) and (
            args.store_dir is None or args.shadow_flow_root is None or args.flow_instructions is None):
        print("levain dream enable: a shadow install needs --store-dir (the scratch directory), --shadow-flow-root and "
              "--flow-instructions together; nothing was registered or written", file=sys.stderr)
        return 1
    was = da.install_kind(store.directory)
    if args.store_dir is not None:
        try:
            _write_install_facts(args, store, str(zone), da, check_only=True)
        except (da.InstallRefused, OSError) as exc:
            print(f"levain dream enable: {exc}; nothing was registered or written", file=sys.stderr)
            return 1
    try:
        reg = da.register_dream(store, revise=args.revise)
    except da.EnableRefused as exc:
        print(f"levain dream enable: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - nothing was reported as registered
        print(f"levain dream enable: cannot register ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 1
    install_path = None
    if args.store_dir is not None:
        try:
            install_path = _write_install_facts(args, store, str(zone), da)
        except (da.InstallRefused, OSError) as exc:
            print(f"levain dream enable: registered, but the install facts were not written: {exc}", file=sys.stderr)
            return 1
    # the card describes the install the link would run: the facts file as it stands now, written by this call or not
    now = da.install_kind(store.directory)
    replaced = f"replaced the {was} install with a {now} install (--replace-install)" if (
        install_path is not None and was is not None and was != now) else None
    r = ex.PER_RUN_LIMIT_USD_MICROS / 1_000_000
    expires = "the link's expires_at (a link lasts at most 29 days)"
    if now == "shadow":
        try:
            inst = da.read_install(store.directory)
            card = da.shadow_signing_card_text(str(zone), flow_root=inst.shadow_flow_root,
                                               scratch=inst.store_path.parent, model=inst.model, per_run_usd=r,
                                               night_usd=2 * r, expires=expires)
        except da.InstallRefused as exc:
            card = f"The shadow install's facts cannot be read ({exc}); no fire runs until `levain dream enable` " \
                   "writes them again."
    else:
        card = da.signing_card_text(str(zone), per_run_usd=r, night_usd=2 * r, expires=expires)
    doc = {"agent_id": reg.agent_id, "spec_hash": reg.spec_hash, "role_hash": reg.role_hash, "version": reg.version,
           "created": reg.created, "zone": reg.zone, "signing_card": card,
           "install_facts": None if install_path is None else str(install_path), "install_kind": now,
           "replaced": replaced}
    if args.json:
        print(json.dumps(doc, sort_keys=True))
    else:
        print(f"{'registered' if reg.created else 'already registered'}: dream {reg.agent_id} v{reg.version} "
              f"spec_hash {reg.spec_hash[:12]} role_hash {reg.role_hash[:12]}")
        print(f"install facts ({now}): {install_path}" if install_path else
              "install facts: not written (pass --store-dir); a scheduled fire refuses without them")
        if replaced:
            print(f"install facts: {replaced}")
        print(card)
        print("Not signed. The dream launches only under a live signed agent link for this spec_hash.")
    return 0


def _write_install_facts(args: argparse.Namespace, store: object, zone: str, da: object, *,
                         check_only: bool = False) -> Path:
    """Resolve the install this verb names to absolute paths and write the facts file in the agent store's directory
    (``check_only``: make every check :func:`levain.dream.agent.write_install` makes and write nothing). The live store
    is allowed for a live install (the scheduled dream targets it); only ``run`` is for copies."""
    from levain.coordinator.launch import default_coordinator_dir

    base = Path(args.store_dir).expanduser().resolve()
    home = os.environ.get("LEVAIN_HOME")
    levain_home = Path(home).expanduser() if home else Path.home() / ".levain"
    cockpit = Path(args.cockpit_root if args.cockpit_root is not None else levain_home / "cockpit").expanduser().resolve()
    coord = Path(args.coord_dir if args.coord_dir is not None else default_coordinator_dir()).expanduser().resolve()
    if args.shadow_flow_root is not None:
        # The shadow recipe: the scratch directory is created by the first seed, never here; write_install refuses a
        # scratch store inside ~/.anneal-memory or flow's root, a non-empty scratch directory with no shadow marker,
        # and a flow root without its ledger, then copies flow's instructions read-only beside the facts.
        return da.write_install(store.directory, store_path=base / "memory.db", cockpit_root=cockpit, coord_dir=coord,
                                zone=zone, shadow_flow_root=Path(args.shadow_flow_root).expanduser().resolve(),
                                flow_instructions=Path(args.flow_instructions).expanduser().resolve(),
                                replace_install=args.replace_install, check_only=check_only)
    db = next((c for c in (base / "memory.db", base / ".levain" / "memory.db") if c.is_file()), None)
    if db is None:
        raise da.InstallRefused(f"no memory.db under {base} (looked in the dir and in .levain/)")
    return da.write_install(store.directory, store_path=db.resolve(), cockpit_root=cockpit, coord_dir=coord, zone=zone,
                            replace_install=args.replace_install, check_only=check_only)


def _refused(p: Path) -> str | None:
    """Why ``p`` may not be written by a dream run, or None: it RESOLVES (symlinks followed) inside the operator's
    live store ``~/.anneal-memory`` (case-insensitive)."""
    from levain.firing.isolation import _is_within_ci, flow_store_dir

    r = p.resolve()
    if _is_within_ci(r, flow_store_dir().resolve()):
        return (f"{p} resolves to {r}, inside the operator's live store (~/.anneal-memory): `levain dream run` "
                "is for a COPY. Refused.")
    return None


def _resolve_store(store_dir: str) -> tuple[Path | None, str]:
    """The store's RESOLVED ``memory.db`` under ``store_dir``, or a refusal. The db is returned canonical (symlinks
    followed), so the lock, crystal, continuity text and default occurrence dir the core derives from it sit beside
    the file SQLite actually writes, never beside an alias. Every path the run writes is refused if it resolves inside
    ``~/.anneal-memory``: the dir, the candidate db, its canonical dir, every ``<stem>*`` sibling there (continuity
    text, crystal, WAL, audit) and the ``wrap.lock`` beside it. A copy dir whose ``memory.db`` is a symlink to the live
    db is the live store."""
    base = Path(store_dir).expanduser()
    why = _refused(base)
    if why:
        return None, why
    base = base.resolve()
    for alias in (base / "memory.db", base / ".levain" / "memory.db"):
        if not alias.is_file():
            continue
        why = _refused(alias)
        if why:
            return None, why
        cand = alias.resolve()
        for p in (cand.parent, cand.parent / "wrap.lock"):
            why = _refused(p)
            if why:
                return None, why
        try:
            siblings = [p for p in cand.parent.iterdir() if p.name.startswith(cand.stem)]
        except OSError as exc:
            return None, f"{cand.parent} cannot be listed ({type(exc).__name__}: {exc}); refused."
        for p in siblings:
            why = _refused(p)
            if why:
                return None, why
        return cand, ""
    return None, f"no memory.db under {base} (looked in the dir and in .levain/)."


def run_dream_run(args: argparse.Namespace) -> int:
    from levain import wrap as _w
    from levain.dream import executor as ex

    max_seconds = ex.DREAM_BUDGET_S if args.max_seconds is None else args.max_seconds
    if not (math.isfinite(max_seconds) and max_seconds > 0):
        print(f"levain dream run: --max-seconds must be a positive number of seconds (got {args.max_seconds!r}); "
              "an unbounded run leaves a hung compose child unbounded.", file=sys.stderr)
        return 2
    store_path, err = _resolve_store(args.store_dir)
    if store_path is None:
        print(f"levain dream run: {err}", file=sys.stderr)
        return 2
    # Absolute once, here (the core does it again): the package, the child's cwd, --add-dir and the prompt must all
    # name the same dir. A default id carries a random suffix: one-second ids collide and overwrite a prior record.
    occ = (Path(args.occurrence_dir).expanduser() if args.occurrence_dir else
           store_path.parent / "dream" / f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex}").resolve()
    why = _refused(occ)
    if why:
        print(f"levain dream run: --occurrence-dir {why}", file=sys.stderr)
        return 2
    if args.mind == "claude":
        model = args.model or ex.DEFAULT_CLAUDE_MODEL
        mind: object = ex.ClaudeMind(model, claude_bin=args.claude_bin)
        budget: ex.Budget | None = ex.Budget.for_model(model)
        # A dry run composes nothing and spends nothing: it needs no price row.
        if budget.m_usd_micros is None and not args.dry_run:
            print(f"levain dream run: no price row for {model!r} in levain.dream.executor.PRICE_TABLE, so the "
                  "one-call ceiling M is unknown and the Claude mind refuses to compose.", file=sys.stderr)
            return 2
    else:
        key = os.environ.get(args.api_key_env) if args.api_key_env else None
        mind = ex.OllamaMind(args.model or _w.DEFAULT_COMPOSE_MODEL, base_url=args.base_url, api_key=key)
        budget = None
    from levain.dream import snapshot as snap

    on_snapshot, on_saved = snap.hooks()
    result = ex.consolidate_store(
        store_path, mind=mind, on_snapshot=on_snapshot, on_saved=on_saved, composer=getattr(mind, "model", None), max_seconds=max_seconds, budget=budget,
        instructions_path=args.instructions, occurrence_dir=occ, dry_run=args.dry_run,
        hard_exit_code=_w.EXIT_TIMEOUT, label="levain dream run")
    if args.json:
        print(json.dumps(result.as_dict(), sort_keys=True, default=str))
    return result.exit_code


_OCC_FORMS = ("an occurrence id (a plain name under <store>/dream/), an absolute path, a path starting with ./ or "
              "~/, or a path containing a separator")


def _occurrence_dir(store_path: Path, occurrence: str) -> tuple[Path | None, str]:
    """The occurrence dir named by ``occurrence``, refused if it resolves inside the operator's live store. A path only
    when it is absolute, starts with ``./`` or ``~/``, or contains a separator; anything else is an id under
    ``<store dir>/dream/``, never a directory of that name in the working dir. ``.``, ``..`` and any ``..`` component
    are refused outright."""
    seps = [x for x in (os.sep, os.altsep, "/") if x]
    parts = [occurrence]
    for sep in seps:
        parts = [q for part in parts for q in part.split(sep)]
    if not occurrence.strip() or occurrence in (".", "~") or ".." in parts:
        return None, f"{occurrence!r} is not an occurrence. Accepted: {_OCC_FORMS}."
    is_path = os.path.isabs(occurrence) or occurrence.startswith("~/") or any(x in occurrence for x in seps)
    if is_path:
        occ = Path(occurrence).expanduser().resolve()
    else:
        occ = (store_path.parent / "dream" / occurrence).resolve()
    why = _refused(occ)
    if why:
        return None, why
    if not occ.is_dir():
        return None, f"{occ} is not a directory: no such occurrence."
    return occ, ""


def _store_and_occ(verb: str, args: argparse.Namespace) -> tuple[Path, Path] | None:
    store_path, err = _resolve_store(args.store_dir)
    if store_path is None:
        print(f"levain dream {verb}: {err}", file=sys.stderr)
        return None
    occ, err = _occurrence_dir(store_path, args.occurrence)
    if occ is None:
        print(f"levain dream {verb}: {err}", file=sys.stderr)
        return None
    return store_path, occ


def run_dream_rollback(args: argparse.Namespace) -> int:
    from levain.dream import snapshot as snap

    got = _store_and_occ("rollback", args)
    if got is None:
        return 2
    res = snap.rollback(got[0], got[1], who=args.who)
    if args.json:
        print(json.dumps({"outcome": res.outcome, "code": res.code, "message": res.message, "restored": res.restored,
                          "card": res.card}, sort_keys=True))
    else:
        print(f"levain dream rollback: {res.message}")
        if res.card:
            print(f"  card to file: {json.dumps(res.card, sort_keys=True)}")
    return 0 if res.ok else 1


def run_dream_recover(args: argparse.Namespace) -> int:
    from levain.dream import recover as rec

    got = _store_and_occ("recover", args)
    if got is None:
        return 2
    res = rec.recover(got[0], got[1], state=args.state, card_filed=args.card_filed)
    if args.json:
        print(json.dumps(res.as_dict(), sort_keys=True))
    else:
        print(f"levain dream recover: ends {res.ending!r} ({res.reason or 'no change'}); child {res.child}; "
              f"wrap {res.wrap}")
        for n in res.notes:
            print(f"  {n}")
        if res.card:
            print(f"  card to file: {res.card['kind']} / {res.card['reason']}  (remedy: {res.card['remedy']})")
            for line in res.card.get("his", []):
                if line not in res.notes:
                    print(f"  for you: {line}")
    if not res.settled or res.ending == "none":
        print(f"levain dream recover: UNSETTLED ({res.reason or 'nothing changed'}). Nothing here is finished: "
              f"fix what the notes above name, then run `levain dream recover {shlex.quote(args.occurrence)} --store-dir "
              f"{shlex.quote(args.store_dir)}` again.", file=sys.stderr if args.json else sys.stdout)
    return 0 if (res.settled and res.ending != "none") else 1


def run_dream_drill(args: argparse.Namespace) -> int:
    from levain.dream import snapshot as snap

    store_path, err = _resolve_store(args.store_dir)
    if store_path is None:
        print(f"levain dream drill: {err}", file=sys.stderr)
        return 2
    root = (Path(args.root).expanduser() if args.root else store_path.parent / "dream").resolve()
    why = _refused(root)
    if why:
        print(f"levain dream drill: --root {why}", file=sys.stderr)
        return 2
    res = snap.drill(store_path, root)
    if args.json:
        print(json.dumps(res.as_dict(), sort_keys=True))
    else:
        print(f"levain dream drill: {res.outcome}" + (f" (snapshot {res.snapshot})" if res.snapshot else
                                                       " (no snapshot yet: not a failure)"))
        for st in res.steps:
            print(f"  {st['name']:10s} {st['status']:8s} {st['detail']}")
    return 1 if res.outcome == "failed" else 0


def _host_zone() -> str | None:
    """An IANA zone name from ``TZ`` or the host's zone file, or None when neither names one."""
    from zoneinfo import ZoneInfo

    cands = [os.environ.get("TZ", "").lstrip(":")]
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            cands.append(link.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    for c in cands:
        if c:
            try:
                ZoneInfo(c)
                return c
            except (KeyError, ValueError, OSError):
                continue
    return None


def run_dream_ready(args: argparse.Namespace) -> int:
    from datetime import date, datetime, timezone
    from zoneinfo import ZoneInfo

    from levain.dream import ready as rd

    store_path, err = _resolve_store(args.store_dir)
    if store_path is None:
        print(f"levain dream ready: {err}", file=sys.stderr)
        return 2
    zone = args.zone or _host_zone()
    try:
        if zone is None:
            raise ValueError("no zone: pass --zone <IANA name> (the host's zone is not one this verb can name)")
        ZoneInfo(zone)
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("--now must carry a timezone offset")
        if args.day:
            day = date.fromisoformat(args.day)
        else:
            day = rd.manual_day_at(now, zone) if args.manual else rd.dream_day_at(now, zone)
    except (ValueError, KeyError) as exc:
        print(f"levain dream ready: {exc}", file=sys.stderr)
        return 2
    try:
        src, close = rd.sources_for(store_path, agent_dir=args.agent_dir, coord_dir=args.coord_dir, now=now)
    except Exception as exc:  # noqa: BLE001 — a store that will not open is unreadable, never ready
        print(f"levain dream ready: the store could not be opened ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 3
    try:
        v = rd.evaluate(src, day=day, now=now, zone=zone, agent_id=args.agent_id, manual=args.manual)
    finally:
        close()
    if args.json:
        print(json.dumps(v.as_dict(), sort_keys=True))
    else:
        print(f"levain dream ready: {v.verdict} for dream-day {v.day} ({v.reason})")
        for c in v.checks:
            print(f"  {c.n} {c.status:10s} {c.name}: {c.detail}")
    return 0 if v.ready else (3 if v.verdict == rd.UNREADABLE else 1)


def run_dream_shadow(args: argparse.Namespace) -> int:
    from levain.dream import card as card_mod
    from levain.dream import ready as rd
    from levain.dream import shadow as sh

    if args.composed is not None and args.shadow_dir is None:
        print("levain dream shadow: --composed needs --shadow-dir (without a seeded scratch store there is nothing to "
              "compare it against)", file=sys.stderr)
        return 2
    if args.composed is not None and args.occurrence_dir is None:
        print("levain dream shadow: --composed needs --occurrence-dir", file=sys.stderr)
        return 2
    if args.agent_dir and not args.agent_id:
        print("levain dream shadow: --agent-dir needs --agent-id: the no-shadow-done check is scoped to the shadow "
              "agent, and every agent's done rows would answer it otherwise", file=sys.stderr)
        return 2
    out: dict[str, object] = {"day": args.day}
    zone = args.zone or (_host_zone() if args.agent_dir else None)
    occurrences = None
    if args.agent_dir:
        agent_dir = Path(args.agent_dir)
        occurrences = lambda: rd._read_occurrences(agent_dir)  # noqa: E731
    v = sh.day_ready(args.flow_root, args.day, occurrences=occurrences, zone=zone, agent_id=args.agent_id)
    out["day_ready"] = {"verdict": v.verdict, "kind": v.kind, "reason": v.reason}
    code = 0 if v.ready else (3 if v.verdict == rd.UNREADABLE else 1)
    if v.ready and args.shadow_dir is not None and v.flow_day is not None:
        db = args.shadow_dir / "memory.db"
        try:
            with sh.night_lock(v.flow_day, db):
                try:
                    seeded = sh.seed(v.flow_day, db)
                except sh.ShadowRefused as exc:
                    out["seed"] = {"refused": exc.kind, "why": exc.why}
                    code = 3
                else:
                    out["seed"] = {"db": str(seeded.db), "files": list(seeded.files)}
                    ev = sh.episode_check(v.flow_day, db)
                    out["episodes"] = {"verdict": ev.verdict, "kind": ev.kind, "reason": ev.reason}
                    code = 0 if ev.ready else (3 if ev.verdict == rd.UNREADABLE else 1)
                    if ev.ready and args.composed is not None:
                        try:
                            text = args.composed.read_text(encoding="utf-8")
                            c = sh.shadow_card(v.flow_day, text, occurrence=args.occurrence,
                                               occ_dir=args.occurrence_dir)
                        except (OSError, UnicodeError, ValueError, sh.ShadowRefused, card_mod.CardError) as exc:
                            print(f"levain dream shadow: {exc}", file=sys.stderr)
                            return 3
                        out["card"] = c.as_dict()
        except sh.ShadowRefused as exc:
            out["seed"] = {"refused": exc.kind, "why": exc.why}
            code = 3
        except OSError as exc:
            out["seed"] = {"refused": sh.KIND_UNREADABLE, "why": f"{type(exc).__name__}: {exc}"}
            code = 3
    if args.json:
        print(json.dumps(out, sort_keys=True, default=str))
    else:
        print(f"levain dream shadow: day-ready {v.verdict}{'/' + v.kind if v.kind else ''}: {v.reason}")
        if "seed" in out:
            print(f"  seed: {out['seed']}")
        if "episodes" in out:
            e = out["episodes"]
            print(f"  episodes: {e['verdict']}{'/' + e['kind'] if e['kind'] else ''}: {e['reason']}")  # type: ignore[index]
        if "card" in out:
            for line in out["card"]["body"]["own"]:  # type: ignore[index]
                print(f"  {line}")
    return code
