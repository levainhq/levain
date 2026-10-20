"""levain.coordinator.cli — ``levain drop`` (drop words for the coordinator and go) and ``levain thread`` (the threads
your drops became: list, show, and the one-tap fixes as commands).

``levain drop "words"`` stores the words verbatim and prints the drop receipt. Run inside a git work tree on a branch,
it records that repo and branch as where the drop was made from (C0 §3.1: a CLI drop's source is the branch checked out
where you typed it); a detached HEAD or no repo records none. Words come from the arguments, or from stdin with ``-``.
``levain drops split`` is the coordinator's door: it names the threads a drop becomes; ``levain drops list|show`` read
them back.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from levain import unshowable
from levain.cockpit.consent import utc_stamp
from levain.contracts.canon import canonical
from levain.coordinator import envelope as env
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.store import (KINDS, STATES, WORDS_MAX, CoordError, Drop, Part, Source, Thread, _words_sha256,
                                      check_source)
from levain.dispatch.jobs import JobStoreError

# what a store call can refuse with: the grammar's refusal, an untrustworthy store, a database it cannot use
_REFUSED = (CoordError, JobStoreError, sqlite3.Error, OSError)

__all__ = ["add_parsers", "cwd_source"]

_GIT_TIMEOUT = 10


def _git(cwd: str, *args: str) -> str | None:
    """One git call in ``cwd`` with the dispatch store's hardened environment; ``None`` when git fails or is absent
    (a drop's source is a convenience: no repo is ``none``, never an error)."""
    from levain.dispatch.contract import git_env

    try:
        cp = subprocess.run(["git", "--no-replace-objects", "-C", cwd, *args], capture_output=True,
                            timeout=_GIT_TIMEOUT, env=git_env(), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None
    out = cp.stdout.decode("utf-8", "surrogateescape")
    return out[:-1] if out.endswith("\n") else out          # git's terminator only: a path may end in a space


def cwd_source(cwd: str | None = None) -> Source:
    """Where a CLI drop is made from: the work tree's top, the checked-out branch and its head; ``Source()`` (none)
    outside a repo or on a detached HEAD."""
    cwd = cwd or os.getcwd()
    top = _git(cwd, "rev-parse", "--show-toplevel")          # already absolute; one line or it is not trusted
    ref = _git(cwd, "symbolic-ref", "-q", "HEAD")
    if not top or "\n" in top or not top.startswith("/") or not ref or not ref.startswith("refs/heads/"):
        return Source()
    # the head is read from the branch just named, never from HEAD again: a checkout in between cannot pair one
    # branch with another's commit
    head = _git(cwd, "rev-parse", "--verify", "-q", "--end-of-options", f"{ref}^{{commit}}")
    if not head:
        return Source()
    source = Source("cwd", os.path.realpath(top), ref, head)
    try:
        return check_source(source)
    except CoordError:
        return Source()          # a path the store cannot hold (not UTF-8, a hidden character) is recorded as none


def _store(args: argparse.Namespace) -> EnvelopeStore:
    d = args.queue_dir.expanduser() if args.queue_dir is not None else None
    return EnvelopeStore(d)


def _drop_row(d: Drop) -> dict[str, Any]:
    return {"drop_id": d.drop_id, "surface": d.surface, "outbox_id": d.outbox_id, "words": d.words,
            "words_sha256": d.words_sha256, "sent_at": d.sent_at, "received_at": d.received_at,
            "source": {"how": d.source.how, "repo": d.source.repo, "ref": d.source.ref, "head": d.source.head}}


def _thread_row(t: Thread) -> dict[str, Any]:
    return {"thread_id": t.thread_id, "kind": t.kind, "title": t.title, "state": t.state, "about": t.about,
            "repo": t.repo, "rev": t.rev, "members": list(t.members), "members_sha256": t.members_sha256,
            "created_at": t.created_at}


def _where(d: Drop) -> str:
    if d.source.how == "none":
        return "no repo or branch recorded"
    return f"from {d.source.repo} on {d.source.ref.removeprefix('refs/heads/')}"


def _thread_line(t: Thread) -> str:
    about = f" (about {t.about})" if t.about else ""
    return f"  {t.thread_id}  {t.kind.upper():5} {t.state:7} rev {t.rev}  {t.title}{about}"


def _shown(words: str) -> str:
    """Words for a terminal: quoted, with every character that would not show as itself (a control, a bidi override)
    marked by its code point. The stored words stay verbatim; this is only what a text listing prints."""
    return json.dumps(unshowable.mark(words), ensure_ascii=False)


def _emit(out: Any, as_json: bool, obj: Any, text: str) -> None:
    out.write((json.dumps(obj, ensure_ascii=False, indent=2) if as_json else text) + "\n")


def _read_words(args: argparse.Namespace) -> str:
    """The words: one argument kept as typed, several joined by one space, or ``-`` alone for stdin (strict UTF-8, one
    trailing newline dropped, so ``echo words | levain drop -`` stores what ``levain drop words`` does)."""
    if args.words == ["-"]:
        cap = WORDS_MAX * 4 + 2          # the most bytes WORDS_MAX characters can take, and a newline
        raw = sys.stdin.buffer.read(cap + 1)
        if len(raw) > cap:
            raise CoordError(f"a drop is at most {WORDS_MAX} characters; stdin holds more")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise CoordError("stdin is not UTF-8 text") from None
        return text[:-1] if text.endswith("\n") else text
    return " ".join(args.words)


def run_drop(args: argparse.Namespace, out: Any = None) -> int:
    out = out or sys.stdout
    try:
        store = _store(args)
        words = _read_words(args)
        source = Source() if args.no_source else cwd_source()
        if args.envelope is not None:
            return _drop_with_envelope(args, out, store, words, source)
        drop, created = store.accept_drop(words, surface="cli", outbox_id=args.outbox_id, source=source)
    except _faults() as exc:
        print(f"levain drop: {exc}", file=sys.stderr)
        return 1
    state = "delivered" if created else "already delivered (resend, nothing written)"
    _emit(out, args.json, {**_drop_row(drop), "created": created},
          f"drop receipt: {drop.drop_id} {state} at {drop.received_at}, {_where(drop)}")
    return 0


# --- thread envelopes (C2b): the operator's own door to the one authority the coordinator adds -----------------------
def _faults() -> tuple[type[BaseException], ...]:
    """What an envelope command refuses with: the store's, a body's shape, the dispatch command line's own refusals, a
    log or signer that would not answer. Lazy: the dispatch CLI is only loaded when an envelope command runs."""
    from levain.authority import AuthorityError, VerifierUnavailable
    from levain.dispatch import DispatchError

    return (*_REFUSED, env.EnvelopeError, DispatchError, AuthorityError, VerifierUnavailable, ValueError)


def _tick() -> datetime:
    return datetime.now(timezone.utc)


_DURATION = re.compile(r"(?:(\d{1,3})h)?(?:(\d{1,4})m)?")


def _parse_stop(text: str) -> timedelta:
    m = _DURATION.fullmatch(text.strip())
    if m is None or not (m.group(1) or m.group(2)):
        raise CoordError(f"--stop is a duration like 2h, 90m or 1h30m (got {text[:40]!r})")
    span = timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))
    if span <= timedelta(0) or span > env_lifetime():
        raise CoordError(f"--stop is more than 0 and at most {int(env_lifetime().total_seconds() // 3600)}h "
                         f"(got {text[:40]!r})")
    return span


def env_lifetime() -> timedelta:
    from levain.dispatch.contract import MAX_LIFETIME

    return MAX_LIFETIME


def _parse_budget(text: str | None) -> dict[str, int]:
    out = {k: 0 for k in env.BUDGET_KEYS}
    if text is None:
        return out
    for item in text.split(","):
        key, sep, val = item.partition("=")
        key = key.strip()
        if not sep or key not in out or not val.strip().isdigit():
            raise CoordError(f"--budget is {','.join(k + '=N' for k in env.BUDGET_KEYS)}, any of them (got "
                             f"{item[:40]!r})")
        out[key] = int(val)
    return out


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _parse_where(specs: list[str]) -> list[dict[str, Any]]:
    out = []
    for spec in specs:
        repo, sep, paths = spec.partition("=")
        listed = [p for p in paths.split(",") if p] if sep else ["."]
        if not repo or not listed:
            raise CoordError(f"--where is REPO or REPO=PATH,PATH (got {spec[:80]!r})")
        out.append({"repo": _real(repo), "paths": listed})
    return out


def _parse_land(spec: str) -> dict[str, Any]:
    repo, sep, onto = spec.rpartition("=")
    if not sep or not repo or not onto:
        raise CoordError(f"--land is REPO=ONTO, the branch it may land onto (got {spec[:80]!r})")
    return {"repo": _real(repo), "onto": onto, "from": "named", "onto_from": "inferred", "criteria": []}


def _store_id(store: EnvelopeStore) -> str:
    return store.store_id()


def _money(cents: int) -> str:
    return f"${cents // 100}.{cents % 100:02d}"


def _threads_line(store: EnvelopeStore | None, thread_id: str, rev: int | None) -> str:
    title = ""
    if store is not None:
        try:
            t = store.thread(thread_id)
            title = f"  {t.kind.upper()} {t.title}"
        except _REFUSED:
            pass
    return f"    {thread_id} rev {rev if rev is not None else '?'}{title}"


def _describe(body: dict[str, Any], store: EnvelopeStore | None = None, *, binds: bool = True) -> list[str]:
    """An envelope body as the lines a human signs: where, each effect, the budget, the stop. One function for the
    echo, the sign view, the drop view and show, so what is signed is what was read."""
    L = [f"  origin: {body['origin']}", "  where it may work:"]
    L += [f"    {w['repo']}  paths: {', '.join(w['paths'])}" for w in body["where"]] or ["    nowhere"]
    L.append("  lands (merges) it may do:")
    for land in body["lands"]:
        crit = ", ".join(json.dumps(c, sort_keys=True) for c in land["criteria"]) or "none"
        L.append(f"    {land['repo']}  onto {land['onto']}  ({'the branch the drop was made from'
                 if land['from'] == 'source' else 'named'}; criteria: {crit})")
    if not body["lands"]:
        L.append("    none")
    L.append("  pushes it may do:")
    L += [f"    {p['repo']}  {p['remote']} {p['url']}  branch {p['branch']}" for p in body["pushes"]] or ["    none"]
    L.append("  publishes it may do:")
    L += [f"    {p['package']} {p['version']}  to {p['index']}" for p in body["publishes"]] or ["    none"]
    b = body["budget"]
    L.append(f"  budget (a gauge): {b['tool_calls']} tool calls, {b['tokens']} tokens, {_money(b['dollars_cents'])}, "
             f"{b['hours']} hours")
    L.append(f"  issued {body['issued_at']}, stops {body['stop_at']}")
    if body["supersedes"] is not None:
        L.append(f"  supersedes {body['supersedes']['envelope_id']}")
    if binds:
        if body["binds"]:
            L.append("  threads it binds:")
            L += [_threads_line(store, b_["thread_id"], b_["rev"]) for b_ in body["binds"]]
        elif body["origin"] == "structured":
            L.append("  threads it binds: its own drop's thread, once the drop is split into exactly one")
    return L


def _view(body: dict[str, Any], store: EnvelopeStore | None = None) -> str:
    return "\n".join([f"envelope {body['envelope_id']}  digest {env.envelope_digest(body)}", *_describe(body, store),
                      "signing this lets the coordinator do exactly this; nothing outside it."])


def _read_file(path: Path, high: int, what: str) -> str:
    try:
        raw = path.expanduser().read_bytes()
    except OSError as exc:
        raise CoordError(f"{what}: cannot read {path}: {exc.strerror or exc}") from None
    if len(raw) > high:
        raise CoordError(f"{what}: {path} is over {high} bytes")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise CoordError(f"{what}: {path} is not UTF-8 text") from None


def _body_from_file(path: Path) -> tuple[str, dict[str, Any]]:
    text = _read_file(path, env.BODY_MAX + 2, "the envelope body")
    text = text[:-1] if text.endswith("\n") else text          # the one newline a file ends with
    return text, env.parse_body(text)


def _snapshot(args: argparse.Namespace, store: EnvelopeStore) -> Any:
    from levain.dispatch.cli import _authority_source

    return _authority_source(store.directory, args.data_root)()


def _confirm(args: argparse.Namespace, out: Any, what: str) -> None:
    """y/N on the operator's terminal (dispatch's own prompt); refused with no terminal unless ``--yes``."""
    from levain.dispatch import cli as dcli

    if args.yes:
        return
    if not dcli._is_tty():
        raise CoordError(f"{what} asks y/N on your terminal and this process has none; run it in your own terminal, "
                         "or pass --yes")
    try:
        answer = dcli._ask(args, f"{what}? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        raise CoordError("not confirmed; nothing was signed or stored")


def _live_lines(store: EnvelopeStore, envelope_id: str, thread_ids: list[str], authority: Any) -> dict[str, str]:
    """Per bound thread: ``live`` or why not (the chain's, :meth:`EnvelopeStore.chain_live_refusal`)."""
    out = {}
    for t in thread_ids:
        refused = None if authority is None else store.chain_live_refusal(envelope_id, t, authority=authority)
        out[t] = "live" if authority is not None and refused is None else \
            refused if refused is not None else "not checked (no authority log named; pass --data-root)"
    return out


def run_envelope_echo(args: argparse.Namespace, out: Any) -> int:
    store = _store(args)
    threads = [store.thread(t) for t in args.threads]
    if len({t.thread_id for t in threads}) != len(threads):
        raise CoordError("a thread is named twice")
    for t in threads:
        if not t.members:
            raise CoordError(f"thread {t.thread_id} holds no drops; nothing to bind")
    shared = [d for d in threads[0].members if all(d in t.members for t in threads)]
    if not shared:
        raise CoordError("these threads share no drop; one envelope covers threads that hold the same drop")
    drop = store.drop(shared[0])
    heads = []
    for t in threads:
        chain = store.envelopes_of(t.thread_id)
        heads.append(chain[-1] if chain else None)
    first = heads[0]
    if any((h is None) != (first is None) or (h is not None and h.envelope_id != first.envelope_id) for h in heads):
        raise CoordError("these threads do not share one current envelope; echo them separately")
    where = _parse_where(args.where)
    now = _tick().replace(microsecond=0)
    body = {"format": env.BODY_FORMAT, "store_id": _store_id(store), "envelope_id": "ev-" + secrets.token_hex(16),
            "origin": "worded", "drop_id": drop.drop_id, "words_sha256": drop.words_sha256,
            "binds": [{"thread_id": t.thread_id, "rev": t.rev, "members_sha256": t.members_sha256} for t in threads],
            "supersedes": None if first is None else {"envelope_id": first.envelope_id, "digest": first.digest},
            "where": where, "lands": [_parse_land(s) for s in args.land], "pushes": [], "publishes": [],
            "budget": _parse_budget(args.budget), "stop_at": utc_stamp(now + _parse_stop(args.stop)),
            "issued_at": utc_stamp(now)}
    text = canonical(body)
    env.parse_body(text)          # the shape this reader will sign, checked before a file holds it
    if args.output is not None:
        try:
            args.output.expanduser().write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            raise CoordError(f"cannot write {args.output}: {exc.strerror or exc}") from None
    shown = _view(body, store)
    _emit(out, args.json, {"body": body, "digest": env.envelope_digest(body),
                           "file": None if args.output is None else str(args.output)},
          shown + (f"\nbody written to {args.output}; sign it with `levain envelope sign {args.output} "
                   "--signing-key KEY`" if args.output is not None else f"\nbody: {text}"))
    return 0


def _bound_threads(store: EnvelopeStore, envelope_id: str) -> list[str]:
    """The threads ``envelope_id`` is bound to."""
    return store.threads_of_envelope(envelope_id)


def _accepted(args: argparse.Namespace, out: Any, store: EnvelopeStore, stored: Any, created: bool,
              authority: Any) -> None:
    from levain.coordinator.envelopes import NOT_IN_FORCE

    threads = _bound_threads(store, stored.envelope_id)
    live = _live_lines(store, stored.envelope_id, threads, authority)
    state = "accepted" if created else "already accepted (same record, nothing written)"
    text = [f"envelope {stored.envelope_id} {state}, signed by {stored.signer_fp} ({stored.signer_class})"]
    text += [f"  {t}: {live[t]}" for t in threads]
    text.append(f"  authority today: {NOT_IN_FORCE}")
    _emit(out, args.json, {"envelope_id": stored.envelope_id, "created": created, "signer_fp": stored.signer_fp,
                           "live": live, "authority_today": NOT_IN_FORCE}, "\n".join(text))


def run_envelope_sign(args: argparse.Namespace, out: Any) -> int:
    from levain.dispatch import cli as dcli

    store = _store(args)
    text, body = _body_from_file(args.file)
    try:
        stored = store.envelope(body["envelope_id"])
    except CoordError:
        stored = None
    if stored is not None:
        # a second signature would be a new signed_at, which the store refuses as another record for this id
        raise CoordError(f"envelope {stored.envelope_id} is already accepted; there is nothing to sign "
                         f"(levain envelope show {stored.envelope_id})")
    print(_view(body, store), file=sys.stderr if args.json else out, flush=True)
    if not args.yes and not dcli._is_tty():
        raise CoordError("signing asks y/N on your terminal and this process has none; run it in your own terminal, "
                         "or pass --yes")
    signer_fp, sign = dcli._signer(args, env.SIGN_NAMESPACE, "envelope sign")
    authority = _snapshot(args, store)
    payload = env.sign_payload(body, signer_fp=signer_fp, signed_at=_tick())
    _confirm(args, out, "sign this envelope")
    signature = dcli._sign_bytes(sign, env.sign_message(payload))
    sigfile = args.file.expanduser().with_name(args.file.name + ".sig.json")
    try:
        sigfile.write_text(json.dumps({"payload": payload, "signature": signature}, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise CoordError(f"cannot write {sigfile}: {exc.strerror or exc}") from None
    stored, created = store.accept_envelope(text, payload, signature, authority=authority)
    _accepted(args, out, store, stored, created, authority)
    return 0


def run_envelope_accept(args: argparse.Namespace, out: Any) -> int:
    store = _store(args)
    text, _body = _body_from_file(args.file)
    raw = _read_file(args.sigfile, env.SIGNATURE_MAX + 4096, "the signature file")
    try:
        sig = json.loads(raw)
    except ValueError:
        raise CoordError("the signature file is not JSON") from None
    if not isinstance(sig, dict) or set(sig) != {"payload", "signature"}:
        raise CoordError('the signature file is {"payload": ..., "signature": ...}')
    authority = _snapshot(args, store)
    stored, created = store.accept_envelope(text, sig["payload"], sig["signature"], authority=authority)
    _accepted(args, out, store, stored, created, authority)
    return 0


_SPEC_KEYS = {"where", "lands", "pushes", "publishes", "budget", "stop"}


def _spec_body(spec: Any, words: str, source: Source, store: EnvelopeStore) -> dict[str, Any]:
    """The structured body a drop carries: the user's SPEC (where, lands, pushes, publishes, budget, stop) plus what the
    CLI picks (drop id, envelope id, the words' hash, the store, the clock)."""
    if not isinstance(spec, dict) or not set(spec) <= _SPEC_KEYS:
        raise CoordError(f"an envelope spec is a JSON object with keys among {sorted(_SPEC_KEYS)}")
    where = []
    for w in spec.get("where", []):
        if not isinstance(w, dict) or not isinstance(w.get("repo"), str):
            raise CoordError('each where entry is {"repo": ABSOLUTE_PATH, "paths": [...]}')
        where.append({"repo": _real(w["repo"]), "paths": w.get("paths", ["."])})
    lands = []
    for land in spec.get("lands", []):
        if not isinstance(land, dict):
            raise CoordError("each land is an object")
        frm = land.get("from", "named")
        repo, onto = land.get("repo"), land.get("onto")
        if frm == "source":
            if source.how == "none":
                raise CoordError("a land from source needs the drop made inside a git work tree on a branch")
            repo = repo or source.repo
            onto = onto or (source.ref or "").removeprefix("refs/heads/")
        if not isinstance(repo, str) or not isinstance(onto, str):
            raise CoordError("each land names a repo and a branch to land onto")
        lands.append({"repo": _real(repo), "onto": onto, "from": frm,
                      "onto_from": land.get("onto_from", "drop" if frm == "source" else "inferred"),
                      "criteria": land.get("criteria", [])})
    pushes = [{**p, "repo": _real(p["repo"])} if isinstance(p, dict) and isinstance(p.get("repo"), str) else p
              for p in spec.get("pushes", [])]
    budget = {**{k: 0 for k in env.BUDGET_KEYS}, **spec.get("budget", {})} if isinstance(spec.get("budget", {}), dict) \
        else spec["budget"]
    stop = spec.get("stop", "2h")
    if not isinstance(stop, str):
        raise CoordError('stop is a duration text like "2h"')
    now = _tick().replace(microsecond=0)
    return {"format": env.BODY_FORMAT, "store_id": _store_id(store), "envelope_id": "ev-" + secrets.token_hex(16),
            "origin": "structured", "drop_id": "dr-" + secrets.token_hex(16), "words_sha256": _words_sha256(words),
            "binds": [], "supersedes": None, "where": where, "lands": lands, "pushes": pushes,
            "publishes": spec.get("publishes", []), "budget": budget, "stop_at": utc_stamp(now + _parse_stop(stop)),
            "issued_at": utc_stamp(now)}


def _drop_with_envelope(args: argparse.Namespace, out: Any, store: EnvelopeStore, words: str, source: Source) -> int:
    from levain.dispatch import cli as dcli

    try:
        spec = json.loads(_read_file(args.envelope, 64 * 1024, "the envelope spec"))
    except ValueError:
        raise CoordError("the envelope spec is not JSON") from None
    body = _spec_body(spec, words, source, store)
    text = canonical(body)
    env.parse_body(text)
    signer_fp, sign = dcli._signer(args, env.SIGN_NAMESPACE, "a drop with an envelope")
    authority = _snapshot(args, store)
    print(_view(body, store), file=sys.stderr if args.json else out, flush=True)
    payload = env.sign_payload(body, signer_fp=signer_fp, signed_at=_tick())
    signature = dcli._sign_bytes(sign, env.sign_message(payload))
    drop, created = store.accept_drop(words, surface="cli", outbox_id=args.outbox_id, source=source,
                                      envelope=(text, payload, signature), authority=authority)
    refused = None if drop.envelope_id else (store.envelope_refused(drop.drop_id) or "the envelope was not taken")
    line = f"envelope: taken ({drop.envelope_id})" if drop.envelope_id else f"envelope: refused: {refused}"
    state = "delivered" if created else "already delivered (resend, nothing written)"
    _emit(out, args.json, {**_drop_row(drop), "created": created,
                           "envelope": {"taken": drop.envelope_id is not None, "envelope_id": drop.envelope_id,
                                        "refused": refused}},
          f"drop receipt: {drop.drop_id} {state} at {drop.received_at}, {_where(drop)}\n{line}")
    return 0


def run_envelope_show(args: argparse.Namespace, out: Any) -> int:
    store = _store(args)
    stored = store.envelope(args.envelope_id)
    threads = _bound_threads(store, stored.envelope_id)
    if args.thread is not None:
        if args.thread not in threads:
            raise CoordError(f"envelope {stored.envelope_id} is not bound to thread {args.thread[:40]}")
        threads = [args.thread]
    return _show(args, out, store, stored, threads)


def _show(args: argparse.Namespace, out: Any, store: EnvelopeStore, stored: Any, threads: list[str]) -> int:
    authority, unchecked = None, ""
    try:
        authority = _snapshot(args, store)
    except _faults() as exc:
        unchecked = f"not checked ({str(exc)[:160]})"
    head = [f"envelope {stored.envelope_id}  digest {stored.digest}  signed by {stored.signer_fp} "
            f"({stored.signer_class}), accepted {stored.accepted_at}"]
    rows, text = [], list(head)
    if not threads:
        text += _describe(stored.body, store)
        text.append("  not bound to any thread yet (a structured envelope binds when its drop is split into one thread)")
        text.append("  authority today: " + store.authority_refusal(stored.envelope_id, ""))
    for t in threads:
        eff = store.effective(stored.envelope_id, t)
        live = unchecked
        if authority is not None:
            refused = store.chain_live_refusal(stored.envelope_id, t, authority=authority)
            live = "live" if refused is None else refused
        now = store.authority_refusal(stored.envelope_id, t, authority=authority)
        text.append(f"thread {t} (as narrowed):")
        text += _describe(eff, store, binds=False)
        for k in env.EFFECT_LISTS:
            text += [f"    {k[:-1]} identity: {env.effect_identity(k, e)}" for e in eff[k]]
        text += [f"  live: {live}", f"  authority today: {now}"]
        rows.append({"thread_id": t, "effective": eff, "live": live, "authority_today": now})
    obj = {"envelope_id": stored.envelope_id, "digest": stored.digest, "origin": stored.origin,
           "signer_fp": stored.signer_fp, "accepted_at": stored.accepted_at, "body": stored.body, "threads": rows}
    _emit(out, args.json, obj, "\n".join(text))
    return 0


def run_envelope_list(args: argparse.Namespace, out: Any) -> int:
    store = _store(args)
    envelopes = store.envelopes_of(args.thread)
    if not envelopes:
        _emit(out, args.json, [], f"no envelopes bound to thread {args.thread}")
        return 0
    rc, rows = 0, []
    for e in envelopes:
        buf = _Capture()
        ns = argparse.Namespace(**{**vars(args), "json": False})
        rc |= _show(ns, buf, store, e, [args.thread])
        rows.append({"envelope_id": e.envelope_id, "text": buf.text()})
    _emit(out, args.json, rows, "\n".join(r["text"] for r in rows))
    return rc


class _Capture:
    def __init__(self) -> None:
        self._parts: list[str] = []

    def write(self, s: str) -> int:
        self._parts.append(s)
        return len(s)

    def text(self) -> str:
        return "".join(self._parts).rstrip("\n")


def run_envelope_narrow(args: argparse.Namespace, out: Any) -> int:
    store = _store(args)
    if args.envelope_cmd == "kill":
        kind, value, what = "kill", "", "killed"
    elif args.stop_earlier is not None:
        kind, value, what = "stop_earlier", args.stop_earlier, f"stop moved earlier to {args.stop_earlier}"
    else:
        kind, value, what = "drop_effect", args.drop_effect, f"effect {args.drop_effect} dropped"
    wrote = store.attenuate(args.envelope_id, kind, value, thread_id=args.thread, by="operator")
    scope = f" for thread {args.thread}" if args.thread else ""
    _emit(out, args.json, {"envelope_id": args.envelope_id, "kind": kind, "value": value, "thread_id": args.thread,
                           "written": wrote},
          f"envelope {args.envelope_id}{scope}: {what} (a row was written)" if wrote
          else f"envelope {args.envelope_id}{scope}: already so; nothing was written")
    return 0


_ENVELOPE_RUN = {"echo": run_envelope_echo, "sign": run_envelope_sign, "accept": run_envelope_accept,
                 "show": run_envelope_show, "list": run_envelope_list, "kill": run_envelope_narrow,
                 "narrow": run_envelope_narrow}


def run_envelope(args: argparse.Namespace, out: Any = None) -> int:
    out = out or sys.stdout
    try:
        return _ENVELOPE_RUN[args.envelope_cmd](args, out)
    except _faults() as exc:
        print(f"levain envelope {args.envelope_cmd}: {exc}", file=sys.stderr)
        return 1


def _parse_part(spec: str) -> Part:
    kind, sep, title = spec.partition(":")
    if not sep or kind not in KINDS or not title.strip():
        raise CoordError(f"--thread takes KIND:TITLE with KIND one of {', '.join(KINDS)} (got {spec[:80]!r})")
    return Part(kind=kind, title=title.strip())


def _parse_attach(spec: str) -> Part:
    kind, sep, thread_id = spec.partition(":")
    if not sep or kind not in KINDS or not thread_id:
        raise CoordError(f"--attach takes KIND:THREAD, KIND the intent the drop adds (one of {', '.join(KINDS)}) "
                         f"(got {spec[:80]!r})")
    return Part(kind=kind, attach=thread_id)


def run_split(args: argparse.Namespace, out: Any = None) -> int:
    out = out or sys.stdout
    try:
        store = _store(args)
        parts = [_parse_part(s) for s in args.thread]
        if args.about is not None:
            if all(p.kind == "work" for p in parts):
                raise CoordError("--about applies to a new ask, note or relay thread; this split names none")
            parts = [Part(p.kind, p.title, about=None if p.kind == "work" else args.about) for p in parts]
        parts += [_parse_attach(a) for a in args.attach]
        threads = store.split(args.drop_id, parts, by=args.by)
    except _REFUSED as exc:
        print(f"levain drops split: {exc}", file=sys.stderr)
        return 1
    n = len(threads)
    _emit(out, args.json, [_thread_row(t) for t in threads],
          f"drop receipt: {args.drop_id} split into {n} thread{'s' if n != 1 else ''}\n"
          + "\n".join(_thread_line(t) for t in threads))
    return 0


def run_drop_show(args: argparse.Namespace, out: Any = None) -> int:
    out = out or sys.stdout
    try:
        store = _store(args)
        drop, threads, receipts = store.drop_view(args.drop_id)
    except _REFUSED as exc:
        print(f"levain drops show: {exc}", file=sys.stderr)
        return 1
    obj = {**_drop_row(drop), "threads": [_thread_row(t) for t in threads],
           "drop_receipts": [{"reply_id": r.reply_id, "at": r.at, **r.body} for r in receipts]}
    text = (f"{drop.drop_id}  {drop.surface}  received {drop.received_at}, {_where(drop)}\n"
            f"  words (verbatim, data): {_shown(drop.words)}\n"
            + ("\n".join(_thread_line(t) for t in threads) if threads else "  not split yet")
            + "\n  drop receipts: " + ", ".join(f"{r.body.get('stage')} {r.at}" for r in receipts))
    _emit(out, args.json, obj, text)
    return 0


def run_drop_list(args: argparse.Namespace, out: Any = None) -> int:
    out = out or sys.stdout
    try:
        drops = _store(args).drops(limit=args.limit)
    except _REFUSED as exc:
        print(f"levain drops list: {exc}", file=sys.stderr)
        return 1
    _emit(out, args.json, [_drop_row(d) for d in drops],
          "\n".join(f"{d.drop_id}  {d.received_at}  {_shown(d.words[:80])}" for d in drops)
          or "no drops")
    return 0


def run_thread(args: argparse.Namespace, out: Any = None) -> int:
    out = out or sys.stdout
    try:
        store = _store(args)
        cmd = args.thread_cmd
        if cmd == "list":
            threads = store.threads(include_closed=args.all)
            _emit(out, args.json, [_thread_row(t) for t in threads],
                  "\n".join(_thread_line(t) for t in threads) or "no open threads")
            return 0
        if cmd == "show":
            t, revs, events = store.thread_view(args.thread_id)
            obj = {**_thread_row(t), "revisions": [{"rev": r, "members": list(m), "members_sha256": s, "cause": c}
                                                   for r, m, s, c in revs],
                   "events": [{"seq": e[0], "field": e[1], "value": e[2], "cause": e[3], "by": e[4], "at": e[5]}
                              for e in events]}
            text = (_thread_line(t) + "\n"
                    + "\n".join(f"    rev {r} ({c}): {', '.join(m) or '(no drops)'}" for r, m, _s, c in revs) + "\n"
                    + "\n".join(f"    #{e[0]} {e[1]} = {e[2]}  [{e[4]}: {e[3]}]" for e in events))
            _emit(out, args.json, obj, text)
            return 0
        if cmd == "state":
            t = store.set_state(args.thread_id, args.state, by=args.by, cause=args.cause)
        elif cmd == "merge":
            t = store.merge(args.into, args.other, by=args.by)
        elif cmd == "detach":
            t = store.detach(args.thread_id, args.drop_id, by=args.by)
        elif cmd == "add":
            if args.about is not None and args.attach:
                raise CoordError("--about names what a NEW thread concerns; an attach joins an existing one")
            part = _parse_attach(args.attach) if args.attach else _parse_part(args.thread)
            if args.about is not None:
                part = Part(part.kind, part.title, about=args.about)
            t = store.add(args.drop_id, part, by=args.by)
        else:   # set
            t = store.set_field(args.thread_id, args.field, args.value, by=args.by, cause=args.cause)
    except _REFUSED as exc:
        print(f"levain thread {args.thread_cmd}: {exc}", file=sys.stderr)
        return 1
    _emit(out, args.json, _thread_row(t), _thread_line(t))
    return 0


def add_parsers(subparsers: argparse._SubParsersAction) -> None:
    qdir_help = "The consent directory holding dispatch.db (default: the consent queue's)."

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None, help=qdir_help)
        p.add_argument("--json", action="store_true", help="Machine-readable output.")

    d = subparsers.add_parser(
        "drop", help="Drop words for the coordinator and go (kept verbatim; never authority).", allow_abbrev=False,
        description="Store your words as a drop for the coordinator and print its drop receipt. The words are data: "
                    "they authorise nothing. Read them back with `levain drops`. Words that start with '-' go after "
                    "`--` (levain drop -- -words) or through stdin (levain drop -).")
    d.add_argument("words", nargs="+", help="The words (or - to read them from stdin).")
    d.add_argument("--outbox-id", dest="outbox_id", default=None,
                   help="An idempotency key: a resend with the same key and words writes nothing.")
    d.add_argument("--no-source", dest="no_source", action="store_true",
                   help="Record no repo or branch, even inside a git work tree.")
    d.add_argument("--envelope", type=Path, default=None, metavar="SPEC_FILE",
                   help="Send a structured envelope with the drop: a JSON file {where, lands, pushes, publishes, "
                        "budget, stop}, signed with --signing-key. The words are kept either way.")
    d.add_argument("--signing-key", dest="signing_key", type=Path, default=None,
                   help="An enrolled key that signs the envelope (its private key file, or the .pub of a key an "
                        "agent holds).")
    d.add_argument("--data-root", dest="data_root", type=Path, default=None,
                   help="The cockpit data root whose authority log verifies the signature.")
    common(d)
    d.set_defaults(func=run_drop)

    ds = subparsers.add_parser("drops", help="Your drops: list, show, and split (the coordinator's door).")
    d_sub = ds.add_subparsers(dest="drops_cmd", required=True)
    sp = d_sub.add_parser("split", help="Name the threads a drop becomes (the coordinator's door).")
    sp.add_argument("drop_id")
    sp.add_argument("--thread", action="append", default=[], metavar="KIND:TITLE",
                    help=f"A new thread holding the drop; KIND is one of {', '.join(KINDS)}. Repeatable.")
    sp.add_argument("--attach", action="append", default=[], metavar="KIND:THREAD",
                    help="Put the drop into an existing open thread of that kind. Repeatable.")
    sp.add_argument("--about", default=None, metavar="THREAD",
                    help="The work thread each new ask/note/relay thread here concerns, or @N for this split's own "
                         "N-th --thread.")
    sp.add_argument("--by", default="coordinator", choices=("coordinator", "operator"))
    common(sp)
    sp.set_defaults(func=run_split)

    sh = d_sub.add_parser("show", help="One drop: its words, its threads, its drop receipts.")
    sh.add_argument("drop_id")
    common(sh)
    sh.set_defaults(func=run_drop_show)

    ls = d_sub.add_parser("list", help="Recent drops, newest first.")
    ls.add_argument("--limit", type=int, default=50)
    common(ls)
    ls.set_defaults(func=run_drop_list)

    t = subparsers.add_parser("thread", help="The threads your drops became.")
    t_sub = t.add_subparsers(dest="thread_cmd", required=True)
    tl = t_sub.add_parser("list", help="Threads not yet closed (with --all, every thread).")
    tl.add_argument("--all", action="store_true")
    common(tl)
    ts = t_sub.add_parser("show", help="One thread: its revisions and events.")
    ts.add_argument("thread_id")
    common(ts)
    tst = t_sub.add_parser("state", help=f"Move a thread ({' -> '.join(STATES)}; reopen to open).")
    tst.add_argument("thread_id")
    tst.add_argument("state", choices=STATES)
    tst.add_argument("--by", default="operator", choices=("operator", "coordinator"))
    tst.add_argument("--cause", default="")
    common(tst)
    tm = t_sub.add_parser("merge", help="Fold OTHER into INTO (one intent the split cut in two).")
    tm.add_argument("into")
    tm.add_argument("other")
    tm.add_argument("--by", default="operator", choices=("operator", "coordinator"))
    common(tm)
    td = t_sub.add_parser("detach", help="Take a drop out of a thread (it must stay in another).")
    td.add_argument("thread_id")
    td.add_argument("drop_id")
    td.add_argument("--by", default="operator", choices=("operator", "coordinator"))
    common(td)
    ta = t_sub.add_parser("add", help="Put an already-split drop into one more thread.")
    ta.add_argument("drop_id")
    g = ta.add_mutually_exclusive_group(required=True)
    g.add_argument("--thread", metavar="KIND:TITLE")
    g.add_argument("--attach", metavar="KIND:THREAD")
    ta.add_argument("--about", default=None, metavar="THREAD")
    ta.add_argument("--by", default="operator", choices=("operator", "coordinator"))
    common(ta)
    tf = t_sub.add_parser("set", help="Re-kind, re-title or set the repo of a thread.")
    tf.add_argument("thread_id")
    tf.add_argument("field", choices=("kind", "title", "repo"))
    tf.add_argument("value")
    tf.add_argument("--by", default="coordinator", choices=("coordinator", "operator"))
    tf.add_argument("--cause", default="")
    common(tf)
    for p in (tl, ts, tst, tm, td, ta, tf):
        p.set_defaults(func=run_thread)

    e = subparsers.add_parser(
        "envelope", help="Thread envelopes: what the coordinator may do for a thread, signed by you.",
        description="An envelope is the one authority the coordinator adds: where work may happen, which merges, "
                    "pushes and publishes are granted, a budget and a stop time. Sign the envelope, not the plan. "
                    "Before S1 nothing here is authority: every effect is still a per-call SIGN.")
    e_sub = e.add_subparsers(dest="envelope_cmd", required=True)

    def signer(p: argparse.ArgumentParser, *, key: bool = True) -> None:
        if key:
            p.add_argument("--signing-key", dest="signing_key", type=Path, default=None,
                           help="An enrolled key that signs (its private key file, or the .pub of a key an agent "
                                "holds).")
        p.add_argument("--data-root", dest="data_root", type=Path, default=None,
                       help="The cockpit data root whose authority log verifies the signature (default: the one the "
                            "consent hooks in the queue directory name).")

    ee = e_sub.add_parser("echo", help="Write the worded envelope for threads you are about to sign.")
    ee.add_argument("threads", nargs="+", metavar="THREAD")
    ee.add_argument("--where", action="append", default=[], required=True, metavar="REPO[=PATH,PATH]",
                    help="A repo the work may happen in, with the paths it may touch (default: the whole repo). "
                         "Repeatable.")
    ee.add_argument("--land", action="append", default=[], metavar="REPO=ONTO",
                    help="A merge the coordinator may land onto that branch. Repeatable.")
    ee.add_argument("--stop", default="2h", metavar="DURATION", help="When it stops: 2h, 90m, 1h30m (at most 24h).")
    ee.add_argument("--budget", default=None, metavar="K=N,...",
                    help="tool_calls=N,tokens=N,dollars_cents=N,hours=N; any you leave out is 0.")
    ee.add_argument("-o", "--output", type=Path, default=None, metavar="FILE",
                    help="Write the canonical body here (default: print it).")
    ee.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None, help=qdir_help)
    ee.add_argument("--json", action="store_true", help="Machine-readable output.")

    es = e_sub.add_parser("sign", help="Read the whole envelope, then sign and accept it.")
    es.add_argument("file", type=Path, metavar="FILE")
    signer(es)
    es.add_argument("--yes", action="store_true", help="Do not ask y/N; the whole view is still printed.")
    common(es)

    ea = e_sub.add_parser("accept", help="Accept an envelope signed elsewhere.")
    ea.add_argument("file", type=Path, metavar="FILE")
    ea.add_argument("sigfile", type=Path, metavar="SIGFILE")
    signer(ea, key=False)
    common(ea)

    esh = e_sub.add_parser("show", help="One envelope as it is now, whether it is live, and what it grants today.")
    esh.add_argument("envelope_id")
    esh.add_argument("--thread", default=None, metavar="THREAD")
    signer(esh, key=False)
    common(esh)

    el = e_sub.add_parser("list", help="The envelopes bound to a thread, oldest first.")
    el.add_argument("--thread", required=True, metavar="THREAD")
    signer(el, key=False)
    common(el)

    ek = e_sub.add_parser("kill", help="End an envelope (no signature; never undone).")
    ek.add_argument("envelope_id")
    ek.add_argument("--thread", default=None, metavar="THREAD")
    common(ek)

    en = e_sub.add_parser("narrow", help="Narrow an envelope: stop it earlier or drop one effect.")
    en.add_argument("envelope_id")
    g = en.add_mutually_exclusive_group(required=True)
    g.add_argument("--stop-earlier", dest="stop_earlier", metavar="STAMP")
    g.add_argument("--drop-effect", dest="drop_effect", metavar="IDENTITY")
    en.add_argument("--thread", default=None, metavar="THREAD")
    common(en)
    for p in (ee, es, ea, esh, el, ek, en):
        p.set_defaults(func=run_envelope)
