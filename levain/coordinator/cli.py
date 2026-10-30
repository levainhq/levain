"""levain.coordinator.cli — ``levain drop`` (drop words for the coordinator and go) and ``levain thread`` (the threads
your drops became: list, show, and the one-tap fixes as commands).

``levain drop "words"`` stores the words verbatim and prints the drop receipt. Run inside a git work tree on a branch,
it records that repo and branch as where the drop was made from (C0 §3.1: a CLI drop's source is the branch checked out
where you typed it); a detached HEAD or no repo records none. Words come from the arguments, or from stdin with ``-``.
``levain drops split`` is the coordinator's door: it names the threads a drop becomes; ``levain drops list|show`` read
them back.

``levain envelope`` (echo, sign, accept, show, list, kill, narrow) and ``levain drop --envelope`` are the operator's own
door to a thread envelope: what the coordinator may do for a thread, signed by you (y/N on your terminal, or ``--yes``).
Exit codes follow C1's convention: 0 done, 1 refused or faulted (a store that would not answer, a signer that could
not run included), the refusal on stderr; a drop whose envelope was refused still keeps its words and exits 1.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from levain import unshowable
from levain.autonomic.durable_sqlite import read_transaction
from levain.cockpit.consent import parse_stamp, utc_stamp
from levain.contracts.canon import canonical
from levain.coordinator import envelope as env
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.store import (KINDS, STATES, WORDS_MAX, CoordError, Drop, Part, Source, Thread, check_source,
                                      check_words, words_sha256)
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
    _launch_woken(store, drop)
    return 0


def _launch_woken(store: EnvelopeStore, drop: Drop) -> None:
    """Start the coordinator launch this drop's own transaction asked for, if it asked for one, and say how it went on
    stderr. The drop's exit is never changed by it: the drop was delivered either way."""
    launch_id = store.woken_launch(drop.drop_id)
    if launch_id is None:
        return
    from levain.coordinator.launch import launch_session

    try:
        got = launch_session(store, launch_id)
    except _REFUSED as exc:
        print(f"coordinator launch {launch_id}: not started ({exc})", file=sys.stderr)
        return
    print(_outcome_text(got), file=sys.stderr)


def _outcome_text(got: Any) -> str:
    if got.started:
        return f"coordinator launch {got.launch_id} started: {got.attach}"
    text = f"coordinator launch {got.launch_id}: not started: {got.refusal}"
    if got.remedy:
        text += (f"\nrun this once and accept the trust prompt:\n  {got.remedy}\nthen the next drop starts a "
                 "coordinator, or levain coord wake starts one, once the wait levain coord status shows is over")
    return text


# --- thread envelopes (C2b): the operator's own door to the one authority the coordinator adds -----------------------
def _faults() -> tuple[type[BaseException], ...]:
    """What an envelope command refuses with: the store's, a body's shape, the dispatch command line's own refusals, a
    log or signer that would not answer. Lazy: the dispatch CLI is only loaded when an envelope command runs."""
    from levain.authority import AuthorityError, VerifierUnavailable
    from levain.dispatch import DispatchError
    from levain.dispatch.cli import SignerUnavailable

    return (*_REFUSED, env.EnvelopeError, DispatchError, AuthorityError, VerifierUnavailable, SignerUnavailable,
            ValueError)


def _tick() -> datetime:
    return datetime.now(timezone.utc)


_DURATION = re.compile(r"(?:([0-9]{1,3})h)?(?:([0-9]{1,4})m)?")


def _parse_stop(text: str) -> timedelta:
    m = _DURATION.fullmatch(text.strip())
    if m is None or not (m.group(1) or m.group(2)):
        raise CoordError(f"--stop is a duration like 2h, 90m or 1h30m (got {text[:40]!r})")
    span = timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))
    if span <= timedelta(0) or span > env_lifetime():
        raise CoordError(f"--stop is more than 0 and at most {int(env_lifetime().total_seconds() // 3600)}h "
                         f"(got {text[:40]!r})")
    return span


def _parse_stop_earlier(text: str) -> str:
    """``--stop-earlier`` as the store's stamp: a duration from now (``30m``, ``1h``) or a time with its zone
    (``2026-10-20T23:06:36Z``, ``...-04:00``)."""
    m = _DURATION.fullmatch(text.strip())
    if m is not None and (m.group(1) or m.group(2)):
        return utc_stamp(_tick().replace(microsecond=0) + timedelta(hours=int(m.group(1) or 0),
                                                                     minutes=int(m.group(2) or 0)))
    try:
        at = datetime.fromisoformat(text.strip())
    except ValueError:
        at = None
    if at is None or at.tzinfo is None:
        raise CoordError(f"--stop-earlier is a duration from now like 30m, or a time with its zone like "
                         f"2026-10-20T23:06:36Z (got {text[:40]!r})")
    try:
        return utc_stamp(at.astimezone(timezone.utc))
    except (OverflowError, ValueError):
        raise CoordError(f"--stop-earlier is a time this machine cannot place (got {text[:40]!r})") from None


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
        if not sep or key not in out or not (val.strip().isascii() and val.strip().isdigit()):
            raise CoordError(f"--budget is {','.join(k + '=N' for k in env.BUDGET_KEYS)}, any of them (got "
                             f"{item[:40]!r})")
        out[key] = int(val)
    return out


def _named(text: str) -> set[str]:
    return {item.partition("=")[0].strip() for item in text.split(",")}


def _echo_budget(text: str | None) -> dict[str, int]:
    """``echo --budget``: parsed as :func:`_parse_budget`, and refused when it leaves out a key the gauge measures
    (head ruling C6-3): a key left out reads 0, and 0 stops the first worker, so a partial budget is never read as
    meant. Name the key with 0 to mean 0; the signed meaning of 0 is unchanged. No ``--budget`` at all is the
    all-zero budget the sign view states."""
    from levain.coordinator.budget import METERS

    out = _parse_budget(text)
    if text is not None:
        missing = [k for k in METERS if k not in _named(text)]
        if missing:
            raise CoordError(f"--budget leaves out {', '.join(missing)}, which the budget gauge measures; a key left "
                             "out would be 0, and 0 stops the first worker. Name each one (K=0 means none): "
                             f"{','.join(k + '=N' for k in METERS)}")
    return out


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _parse_where(specs: list[str]) -> list[dict[str, Any]]:
    out = []
    for spec in specs:
        repo, sep, paths = spec.partition("=")
        listed = [p for p in paths.split(",") if p]
        if not sep or not repo or not listed:
            raise CoordError("--where is REPO=PATH,PATH; the whole repo is REPO=. written out, never left off "
                             f"(got {spec[:80]!r})")
        out.append({"repo": _real(repo), "paths": listed})
    return out


def _parse_land(spec: str) -> dict[str, Any]:
    repo, sep, onto = spec.rpartition("=")
    if not sep or not repo or not onto:
        raise CoordError(f"--land is REPO=ONTO, the branch it may land onto (got {spec[:80]!r})")
    return {"repo": _real(repo), "onto": onto, "from": "named", "onto_from": "inferred", "criteria": []}


def _money(cents: int) -> str:
    return f"${cents // 100}.{cents % 100:02d}"


# --- plain words: what a person reads before signing -------------------------------------------------------------------
NOT_IN_FORCE_PLAIN = ("NOT IN FORCE YET: until Levain's separate-user setup is in place, signing this lets nothing run "
                      "by itself; every land, push and publish still asks you separately.")
_BOUND = "bound (not in force yet)"


def _clock(dt: datetime) -> str:
    """A moment in the system's local time and in UTC: ``7:42 PM EDT, 23:42 UTC``."""
    local = dt.astimezone()
    return f"{local.strftime('%I:%M %p %Z').lstrip('0')}, {dt.astimezone(timezone.utc).strftime('%H:%M')} UTC"


def _span(delta: timedelta) -> str:
    mins = max(int(delta.total_seconds() // 60), 0)
    return f"{mins // 60}h {mins % 60}m"


def _stops(stamp: str, now: datetime) -> str:
    stop = parse_stamp(stamp)
    return f"stops in {_span(stop - now)} ({_clock(stop)})" if stop > now else f"stopped {_clock(stop)}"


def _threads_line(store: EnvelopeStore | None, thread_id: str, rev: int | None) -> str:
    title = ""
    if store is not None:
        try:
            t = store.thread(thread_id)
            title = f"  {t.kind.upper()} {t.title}  (from the store, not signed)"
        except _REFUSED:
            pass
    return f"    {thread_id} rev {rev if rev is not None else '?'}{title}"


def _effect_text(kind: str, e: dict[str, Any]) -> str:
    if kind == "lands":
        crit = ", ".join(json.dumps(c, sort_keys=True) for c in e["criteria"])
        # onto_from is signed: an inferred branch says so in the words read before signing (C0 §1, his 10-20 item 1)
        src = "the branch the drop was made from" if e["from"] == "source" else \
            "inferred, not where you dropped from" if e["onto_from"] == "inferred" else "named"
        return f"{e['repo']}  onto {e['onto']}  ({src}; {'criteria: ' + crit if crit else 'may land with no conditions'})"
    if kind == "pushes":
        return f"{e['repo']}  {e['remote']} {e['url']}  branch {e['branch']}"
    return f"{e['package']} {e['version']}  to {e['index']}"


_EFFECT_HEAD = {"lands": "lands (merges) it may do:", "pushes": "pushes it may do:",
                "publishes": "publishes it may do:"}


def _effects(body: dict[str, Any], old: dict[str, Any] | None) -> list[str]:
    """The effect lists; against ``old`` (the envelope this supersedes) each line is marked + added, = unchanged,
    ~ changed (same target, other criteria), and what ``old`` held and this does not is - removed."""
    L: list[str] = []
    for kind in env.EFFECT_LISTS:
        L.append("  " + _EFFECT_HEAD[kind])
        before = {env.effect_key(kind, e): e for e in old[kind]} if old is not None else {}
        now_keys = set()
        for e in body[kind]:
            key = env.effect_key(kind, e)
            now_keys.add(key)
            mark = "" if old is None else "+ added: " if key not in before else \
                "= unchanged: " if before[key] == e else "~ changed: "
            L.append(f"    {mark}{_effect_text(kind, e)}")
        for key, e in before.items():
            if key not in now_keys:
                L.append(f"    - removed: {_effect_text(kind, e)}")
        if not body[kind] and not any(k not in now_keys for k in before):
            L.append("    none")
    return L


def _wider(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """True when ``new`` grants anything ``old`` did not: a later stop, a bigger budget, a repo or path outside old's, an
    effect old did not hold, or the same target with other criteria."""
    if parse_stamp(new["stop_at"]) > parse_stamp(old["stop_at"]):
        return True
    if any(new["budget"][k] > old["budget"][k] for k in env.BUDGET_KEYS):
        return True
    for w in new["where"]:
        allowed = tuple(p for o in old["where"] if o["repo"] == w["repo"] for p in o["paths"])
        if not allowed or any(not env.path_within(p, allowed) for p in w["paths"]):
            return True
    for kind in env.EFFECT_LISTS:
        before = {env.effect_key(kind, e): e for e in old[kind]}
        if any(before.get(env.effect_key(kind, e)) != e for e in new[kind]):
            return True
    return False


def _describe(body: dict[str, Any], store: EnvelopeStore | None = None, *, binds: bool = True,
              old: dict[str, Any] | None = None, now: datetime | None = None, ended: bool = False) -> list[str]:
    """An envelope body as the lines a human signs: where, each effect, the budget, the stop. One function for the
    echo, the sign view, the drop view and show, so what is signed is what was read."""
    now = now or _tick()
    L = [f"  origin: {body['origin']}", "  where it may work:"]
    for w in body["where"]:
        paths = ", ".join("the WHOLE repo (every file)" if p == "." else p for p in w["paths"])
        L.append(f"    {w['repo']}  paths: {paths}")
    if not body["where"]:
        L.append("    nowhere")
    L += _effects(body, old)
    b = body["budget"]
    L.append("  budget (the budget is a gauge: crossing it stops new steps, it is not a hard stop):")
    def _allowed(v: Any, shown: str) -> str:
        return f"{shown} allowed" if v else "0 allowed (none named: new steps stop at once)"
    L += [f"    tool calls: {_allowed(b['tool_calls'], str(b['tool_calls']))}",
          f"    tokens: {_allowed(b['tokens'], str(b['tokens']))}",
          f"    dollars: {_money(b['dollars_cents'])} (not metered: Levain counts no dollars, so this never stops a step)",
          f"    hours: {_allowed(b['hours'], str(b['hours']))}"]
    stop = (f"was to stop {_clock(parse_stamp(body['stop_at']))}" if ended else _stops(body['stop_at'], now))
    L.append(f"  issued {_clock(parse_stamp(body['issued_at']))}; {stop}")
    if body["supersedes"] is not None:
        L.append(f"  supersedes {body['supersedes']['envelope_id']}")
    if binds:
        if body["binds"]:
            L.append("  threads it binds:")
            L += [_threads_line(store, b_["thread_id"], b_["rev"]) for b_ in body["binds"]]
        elif body["origin"] == "structured":
            L.append("  threads it binds: its own drop's thread, once the drop is split into exactly one")
    return L


def _old_of(body: dict[str, Any], store: EnvelopeStore | None) -> Any:
    sup = body["supersedes"]
    if sup is None or store is None:
        return None
    try:
        return store.envelope(sup["envelope_id"])
    except _REFUSED:
        return None


def _request_line(body: dict[str, Any], store: EnvelopeStore, words: str | None) -> str:
    """What the envelope is for: the drop's words, read from the store and held to the hash the body signs."""
    if words is None:
        try:
            drop = store.drop(body["drop_id"])
        except _REFUSED:
            return "  for your words: (that drop is not in this store)"
        if drop.words_sha256 != body["words_sha256"]:
            raise CoordError(f"drop {drop.drop_id} holds other words than this envelope names; echo it again")
        words = drop.words
    return f"  for your words: {_shown(words[:120])}"


def _view(body: dict[str, Any], store: EnvelopeStore, *, words: str | None = None) -> str:
    old = _old_of(body, store)
    L = [f"envelope {body['envelope_id']}  digest {env.envelope_digest(body)}", NOT_IN_FORCE_PLAIN,
         _request_line(body, store, words), *_describe(body, store, old=None if old is None else old.body)]
    if old is not None:
        L.append(f"WIDER than {old.envelope_id}: that is why it needs your signature"
                 if _wider(old.body, body) else f"not wider than {old.envelope_id}")
    L.append("if Levain later acts on it, it may do only this, nothing outside it.")
    return "\n".join(L)


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


def _head_reader(args: argparse.Namespace, store: EnvelopeStore) -> Any:
    """The same authority source's head reader (``AuthoritySource.head``): the log's stored head, no replay."""
    from levain.dispatch.cli import _authority_source

    return _authority_source(store.directory, args.data_root).head


def _need_terminal(args: argparse.Namespace, what: str) -> None:
    from levain.dispatch import cli as dcli

    if not args.yes and not dcli._is_tty():
        raise CoordError(f"{what} asks y/N on your terminal and this process has none; run it in your own terminal, "
                         "or pass --yes")


def _confirm(args: argparse.Namespace, label: str, prompt: str) -> None:
    """y/N on the operator's terminal (dispatch's own prompt); refused with no terminal unless ``--yes``. ``prompt``
    names what is being signed."""
    from levain.dispatch import cli as dcli

    if args.yes:
        return
    _need_terminal(args, label)
    try:
        answer = dcli._ask(args, f"{prompt}? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        raise CoordError("not confirmed; nothing was signed or stored")


def _signer_class(authority: Any, fp: str) -> str:
    key = authority.key(fp)
    return "not an enrolled key" if key is None else str(key.key_class)


def _prompt(body: dict[str, Any], fp: str, klass: str) -> str:
    return (f"sign {body['envelope_id']} (digest …{env.envelope_digest(body)[-8:]}, {len(body['binds'])} thread(s), "
            f"{len(body['lands'])} land(s), stops {_clock(parse_stamp(body['stop_at']))}) as {fp} ({klass})")


def _live_for(store: EnvelopeStore, envelope_id: str, thread_id: str, authority: Any, unchecked: str = "") -> str:
    """``live`` or why not for one bound thread (the chain's, :meth:`EnvelopeStore.chain_live_refusal`)."""
    if authority is None:
        return unchecked or "not checked (no authority log named; pass --data-root)"
    refused = store.chain_live_refusal(envelope_id, thread_id, authority=authority)
    return "live" if refused is None else refused


def _label(live: str) -> str:
    """``live`` is true of the chain and misleading alone: nothing is in force yet."""
    return _BOUND if live == "live" else live


def _write_new(path: Path, text: str, force: bool) -> None:
    p = path.expanduser()
    try:
        with open(p, "w" if force else "x", encoding="utf-8") as f:
            f.write(text)
    except FileExistsError:
        raise CoordError(f"{p} already exists; pass --force to replace it") from None
    except OSError as exc:
        raise CoordError(f"cannot write {p}: {exc.strerror or exc}") from None


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
    heads = [store.head(t.thread_id) for t in threads]
    first = heads[0]
    if any((h is None) != (first is None) or (h is not None and h.envelope_id != first.envelope_id) for h in heads):
        raise CoordError("these threads do not share one current envelope; echo them separately")
    where = _parse_where(args.where)
    now = _tick().replace(microsecond=0)
    body = {"format": env.BODY_FORMAT, "store_id": store.store_id(), "envelope_id": "ev-" + secrets.token_hex(16),
            "origin": "worded", "drop_id": drop.drop_id, "words_sha256": drop.words_sha256,
            "binds": [{"thread_id": t.thread_id, "rev": t.rev, "members_sha256": t.members_sha256} for t in threads],
            "supersedes": None if first is None else {"envelope_id": first.envelope_id, "digest": first.digest},
            "where": where, "lands": [_parse_land(s) for s in args.land], "pushes": [], "publishes": [],
            "budget": _echo_budget(args.budget), "stop_at": utc_stamp(now + _parse_stop(args.stop)),
            "issued_at": utc_stamp(now)}
    text = canonical(body)
    store.check_body(text)          # the shape and the disk checks this reader will sign, before a file holds it
    shown = _view(body, store)
    if args.output is not None:
        _write_new(args.output, text + "\n", args.force)
    _emit(out, args.json, {"body": body, "digest": env.envelope_digest(body),
                           "file": None if args.output is None else str(args.output)},
          shown + (f"\nbody written to {args.output}; sign it with `levain envelope sign {args.output} "
                   "--signing-key KEY`" if args.output is not None else f"\nbody: {text}"))
    return 0


def run_envelope_extend(args: argparse.Namespace, out: Any) -> int:
    """``levain envelope extend THREAD --budget K=N,...`` (C6, T6): the body that raises a thread's budget, for YOU to
    sign. It is the thread's head envelope as narrowed (its effective where, effects and stop) with the budget you
    name (keys you leave out keep the head's), a worded envelope bound to the thread's current revision that supersedes
    the head. Writing it grants nothing: only ``levain envelope sign`` (your key) makes it an envelope."""
    from levain.coordinator import budget

    store = _store(args)
    t = store.thread(args.thread)
    head = store.head(t.thread_id)
    if head is None:
        raise CoordError(f"thread {t.thread_id} has no envelope, so no budget to extend (levain envelope echo)")
    ended = store.end_of(head.envelope_id, t.thread_id)
    if ended is not None:
        raise CoordError(f"envelope {head.envelope_id} ended for this thread ({ended[0]}); a new one is an echo")
    eff = store.effective(head.envelope_id, t.thread_id)
    now = _tick().replace(microsecond=0)
    if parse_stamp(eff["stop_at"]) - now < _MIN_LEFT:
        raise CoordError(f"envelope {head.envelope_id} has stopped or stops within 5 minutes; echo a new one")
    given = _parse_budget(args.budget)
    named = _named(args.budget)
    lower = [f"{k}={given[k]} (now {eff['budget'][k]})" for k in env.BUDGET_KEYS
             if k in named and given[k] < eff["budget"][k]]
    if lower:
        # extend means extend (L3 r1 E1): a smaller budget is a new echo, never this command
        raise CoordError(f"--budget names a value below the thread's current budget: {', '.join(lower)}; extend only "
                         "raises")
    new_budget = {k: (given[k] if k in named else eff["budget"][k]) for k in env.BUDGET_KEYS}
    if not any(new_budget[k] > eff["budget"][k] for k in env.BUDGET_KEYS):
        raise CoordError("--budget names no value greater than the thread's current budget; nothing to extend")
    if head.drop_id not in t.members:
        raise CoordError(f"the head's drop {head.drop_id} left thread {t.thread_id}; echo a new envelope")
    drop = store.drop(head.drop_id)
    body = {"format": env.BODY_FORMAT, "store_id": store.store_id(), "envelope_id": "ev-" + secrets.token_hex(16),
            "origin": "worded", "drop_id": drop.drop_id, "words_sha256": drop.words_sha256,
            "binds": [{"thread_id": t.thread_id, "rev": t.rev, "members_sha256": t.members_sha256}],
            "supersedes": {"envelope_id": head.envelope_id, "digest": head.digest},
            "where": eff["where"], "lands": eff["lands"], "pushes": eff["pushes"], "publishes": eff["publishes"],
            "budget": new_budget, "stop_at": eff["stop_at"], "issued_at": utc_stamp(now)}
    text = canonical(body)
    store.check_body(text)
    with read_transaction(store._connect) as conn:
        g = budget.gauge(conn, store.directory, t.thread_id)
    used = "" if g is None else ("used so far on this thread: " + ", ".join(
        f"{k} {budget._num(k, g.used[k])}" for k in budget.METERS) + "; dollars not metered")
    shown = _view(body, store) + (f"\n{used}" if used else "")
    if args.output is not None:
        _write_new(args.output, text + "\n", args.force)
    _emit(out, args.json, {"body": body, "digest": env.envelope_digest(body), "used": None if g is None else g.used,
                           "file": None if args.output is None else str(args.output)},
          shown + (f"\nbody written to {args.output}; YOU sign it: `levain envelope sign {args.output} "
                   "--signing-key KEY`" if args.output is not None else f"\nbody: {text}"))
    return 0


def _accepted(args: argparse.Namespace, out: Any, store: EnvelopeStore, stored: Any, created: bool,
              authority: Any, *, yes: bool = False) -> None:
    from levain.coordinator.envelopes import NOT_IN_FORCE

    threads = store.threads_of_envelope(stored.envelope_id)
    live = {t: _live_for(store, stored.envelope_id, t, authority) for t in threads}
    state = "accepted" if created else "already accepted (same record, nothing written)"
    how = ", signed with --yes (no prompt)" if yes else ""
    text = [f"envelope {stored.envelope_id} {state}, signed by {stored.signer_fp} ({stored.signer_class}){how}"]
    text += [f"  {t}: {_label(live[t])}" for t in threads]
    text.append(f"  authority today: {NOT_IN_FORCE}")
    _emit(out, args.json, {"envelope_id": stored.envelope_id, "created": created, "signer_fp": stored.signer_fp,
                           "live": live, "authority_today": NOT_IN_FORCE, "signed_with_yes": yes}, "\n".join(text))


_MIN_LEFT = timedelta(minutes=5)


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
    body = store.check_body(text)          # a body the store can only refuse is never shown for signing
    if parse_stamp(body["stop_at"]) - _tick() < _MIN_LEFT:
        raise CoordError(f"envelope {body['envelope_id']} has stopped or stops within 5 minutes; echo again")
    print(_view(body, store), file=sys.stderr if args.json else out, flush=True)
    _need_terminal(args, "signing")
    signer_fp, sign = dcli._signer(args, env.SIGN_NAMESPACE, "envelope sign")
    _confirm(args, "signing", _prompt(body, signer_fp, _signer_class(_snapshot(args, store), signer_fp)))
    # the clock is read only now: the store refuses a signed_at far from its own, and a slow reader is not a stale one
    payload = env.sign_payload(body, signer_fp=signer_fp, signed_at=_tick())
    signature = dcli._sign_bytes(sign, env.sign_message(payload))
    # and the authority log too: the prompt's snapshot only names the key; a key revoked while it waited signs nothing
    authority = _snapshot(args, store)
    stored, created = store.accept_envelope(text, payload, signature, authority=authority)
    sigfile = args.file.expanduser().with_name(args.file.name + ".sig.json")
    record: dict[str, Any] = {"payload": payload, "signature": signature}
    if args.yes:
        record["note"] = "signed with --yes (no prompt)"
    try:
        sigfile.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"levain envelope sign: accepted, but cannot write {sigfile}: {exc.strerror or exc}", file=sys.stderr)
    _accepted(args, out, store, stored, created, authority, yes=args.yes)
    return 0


def run_envelope_accept(args: argparse.Namespace, out: Any) -> int:
    store = _store(args)
    text, body = _body_from_file(args.file)
    raw = _read_file(args.sigfile, env.SIGNATURE_MAX + 4096, "the signature file")
    try:
        sig = json.loads(raw)
    except ValueError:
        raise CoordError("the signature file is not JSON") from None
    if not isinstance(sig, dict) or not {"payload", "signature"} <= set(sig) <= {"payload", "signature", "note"}:
        raise CoordError('the signature file is {"payload": ..., "signature": ...}')
    payload = sig["payload"]
    fp = payload.get("signer_fp") if isinstance(payload, dict) else None
    print(_view(body, store), file=sys.stderr if args.json else out, flush=True)
    if isinstance(fp, str):
        print(f"signed by {fp[:80]} ({_signer_class(_snapshot(args, store), fp)})"
              + (f", {str(sig['note'])[:80]}" if "note" in sig else ""), file=sys.stderr if args.json else out,
              flush=True)
    authority = _snapshot(args, store)          # read after the output, which can block, right before the write
    stored, created = store.accept_envelope(text, sig["payload"], sig["signature"], authority=authority)
    _accepted(args, out, store, stored, created, authority)
    return 0


_SPEC_KEYS = {"where", "lands", "pushes", "publishes", "budget", "stop"}


def _spec_list(spec: dict[str, Any], key: str) -> list[Any]:
    value = spec.get(key, [])
    if not isinstance(value, list):
        raise CoordError(f"{key} in an envelope spec is a list")
    return value


def _spec_body(spec: Any, words: str, source: Source, store: EnvelopeStore) -> dict[str, Any]:
    """The structured body a drop carries: the user's SPEC (where, lands, pushes, publishes, budget, stop) plus what the
    CLI picks (drop id, envelope id, the words' hash, the store, the clock)."""
    if not isinstance(spec, dict) or not set(spec) <= _SPEC_KEYS:
        raise CoordError(f"an envelope spec is a JSON object with keys among {sorted(_SPEC_KEYS)}")
    where = []
    for w in _spec_list(spec, "where"):
        if not isinstance(w, dict) or not isinstance(w.get("repo"), str) or not isinstance(w.get("paths", ["."]), list):
            raise CoordError('each where entry is {"repo": ABSOLUTE_PATH, "paths": [...]}')
        where.append({"repo": _real(w["repo"]), "paths": w.get("paths", ["."])})
    lands = []
    for land in _spec_list(spec, "lands"):
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
        criteria = land.get("criteria", [])
        if not isinstance(criteria, list):
            raise CoordError("a land's criteria are a list")
        lands.append({"repo": _real(repo), "onto": onto, "from": frm,
                      "onto_from": land.get("onto_from", "drop" if frm == "source" else "inferred"),
                      "criteria": criteria})
    pushes = [{**p, "repo": _real(p["repo"])} if isinstance(p, dict) and isinstance(p.get("repo"), str) else p
              for p in _spec_list(spec, "pushes")]
    publishes = _spec_list(spec, "publishes")
    given = spec.get("budget", {})
    if not isinstance(given, dict):
        raise CoordError("budget in an envelope spec is an object")
    budget = {**{k: 0 for k in env.BUDGET_KEYS}, **given}
    stop = spec.get("stop", "2h")
    if not isinstance(stop, str):
        raise CoordError('stop is a duration text like "2h"')
    now = _tick().replace(microsecond=0)
    return {"format": env.BODY_FORMAT, "store_id": store.store_id(), "envelope_id": "ev-" + secrets.token_hex(16),
            "origin": "structured", "drop_id": "dr-" + secrets.token_hex(16), "words_sha256": words_sha256(words),
            "binds": [], "supersedes": None, "where": where, "lands": lands, "pushes": pushes,
            "publishes": publishes, "budget": budget, "stop_at": utc_stamp(now + _parse_stop(stop)),
            "issued_at": utc_stamp(now)}


def _drop_with_envelope(args: argparse.Namespace, out: Any, store: EnvelopeStore, words: str, source: Source) -> int:
    from levain.dispatch import cli as dcli

    check_words(words)          # a drop the store would refuse is never signed for
    try:
        spec = json.loads(_read_file(args.envelope, 64 * 1024, "the envelope spec"))
    except ValueError:
        raise CoordError("the envelope spec is not JSON") from None
    if args.outbox_id is not None and (sent := store.drop_by_outbox(args.outbox_id)) is not None:
        # a resend returns the stored drop as it was, so an envelope signed now would never be read
        raise CoordError(f"outbox id {args.outbox_id[:60]} is already delivered as {sent.drop_id}; nothing new is "
                         f"signed for it (levain drops show {sent.drop_id})")
    body = _spec_body(spec, words, source, store)
    text = canonical(body)
    body = store.check_body(text)          # a body the store can only refuse is never shown for signing
    print(_view(body, store, words=words), file=sys.stderr if args.json else out, flush=True)
    _need_terminal(args, "a drop with an envelope")
    signer_fp, sign = dcli._signer(args, env.SIGN_NAMESPACE, "a drop with an envelope")
    _confirm(args, "a drop with an envelope",
             _prompt(body, signer_fp, _signer_class(_snapshot(args, store), signer_fp)))
    payload = env.sign_payload(body, signer_fp=signer_fp, signed_at=_tick())          # after the answer, not before
    signature = dcli._sign_bytes(sign, env.sign_message(payload))
    authority = _snapshot(args, store)          # after the answer too: a key revoked while it waited signs nothing
    drop, created = store.accept_drop(words, surface="cli", outbox_id=args.outbox_id, source=source,
                                      envelope=(text, payload, signature), authority=authority)
    if not created and drop.envelope_id != body["envelope_id"]:
        # the same outbox id delivered between the check above and this write
        raise CoordError(f"outbox id {drop.outbox_id[:60]} was delivered as {drop.drop_id} while you signed; the "
                         f"envelope you signed was not read and nothing was written (levain drops show {drop.drop_id})")
    refused = None if drop.envelope_id else (store.envelope_refused(drop.drop_id) or "the envelope was not taken")
    how = " (signed with --yes, no prompt)" if args.yes else ""
    line = f"envelope: taken ({drop.envelope_id}){how}" if drop.envelope_id \
        else f"envelope: refused: {refused}\nyour words are kept: {drop.drop_id} holds them"
    state = "delivered" if created else "already delivered (resend, nothing written)"
    _emit(out, args.json, {**_drop_row(drop), "created": created,
                           "envelope": {"taken": drop.envelope_id is not None, "envelope_id": drop.envelope_id,
                                        "refused": refused}},
          f"drop receipt: {drop.drop_id} {state} at {drop.received_at}, {_where(drop)}\n{line}")
    _launch_woken(store, drop)
    return 0 if drop.envelope_id else 1


def run_envelope_show(args: argparse.Namespace, out: Any) -> int:
    store = _store(args)
    stored = store.envelope(args.envelope_id)
    threads = store.threads_of_envelope(stored.envelope_id)
    if args.thread is not None:
        if args.thread not in threads:
            raise CoordError(f"envelope {stored.envelope_id} is not bound to thread {args.thread[:40]}")
        threads = [args.thread]
    return _show(args, out, store, stored, threads)


def _ended(store: EnvelopeStore, envelope_id: str, thread_id: str) -> str | None:
    """Why the envelope is over for this thread, read from the row that ended it, or None."""
    end = store.end_of(envelope_id, thread_id)
    if end is None:
        return None
    kind, _by, at = end
    when = _clock(parse_stamp(at))
    return f"its thread is done, at {when}" if kind == "thread_done" else f"killed by you at {when}"


def _show(args: argparse.Namespace, out: Any, store: EnvelopeStore, stored: Any, threads: list[str]) -> int:
    authority, unchecked = None, ""
    try:
        authority = _snapshot(args, store)
    except _faults() as exc:
        unchecked = f"not checked ({str(exc)[:160]})"
    effs = {t: store.effective(stored.envelope_id, t) for t in threads}
    ends = {t: _ended(store, stored.envelope_id, t) for t in threads}
    head = [f"envelope {stored.envelope_id}  digest {stored.digest}  signed by {stored.signer_fp} "
            f"({stored.signer_class}), accepted {stored.accepted_at}"]
    if threads and all(ends.values()):
        head.insert(0, "ENDED (" + "; ".join(dict.fromkeys(str(r) for r in ends.values())) + ")")
    rows, text = [], list(head)
    if not threads:
        text += _describe(stored.body, store)
        text.append("  not bound to any thread yet (a structured envelope binds when its drop is split into one thread)")
        text.append("  authority today: " + store.authority_refusal(stored.envelope_id, ""))
    for t in threads:
        eff = effs[t]
        live = _live_for(store, stored.envelope_id, t, authority, unchecked)
        now = store.authority_refusal(stored.envelope_id, t, authority=authority)
        if ends[t]:
            text.append(f"thread {t}: ENDED ({ends[t]})" if not all(ends.values()) else f"thread {t}:")
            text.append("  it had allowed:")
            text += _describe(stored.body, store, binds=False, ended=True)
        else:
            text.append(f"thread {t} (as narrowed):")
            text += _describe(eff, store, binds=False)
            for k in env.EFFECT_LISTS:
                text += [f"    {k[:-1]} identity: {env.effect_identity(k, e)}" for e in eff[k]]
        text += [f"  live: {_label(live)}", f"  authority today: {now}"]
        rows.append({"thread_id": t, "effective": eff, "live": live, "authority_today": now,
                     "ended": ends[t]})
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
        kind, value, what, done = "kill", "", "ended", "already ended"
    elif args.stop_earlier is not None:
        kind, value = "stop_earlier", _parse_stop_earlier(args.stop_earlier)
        what, done = f"narrowed (stop moved earlier to {value})", f"already narrowed (stop already {value} or earlier)"
    else:
        kind, value = "drop_effect", args.drop_effect
        what, done = f"narrowed (effect {value} dropped)", f"already narrowed (effect {value} already dropped)"
    wrote = store.attenuate(args.envelope_id, kind, value, thread_id=args.thread, by="operator")
    scope = f" for thread {args.thread}" if args.thread else ""
    _emit(out, args.json, {"envelope_id": args.envelope_id, "kind": kind, "value": value, "thread_id": args.thread,
                           "written": wrote},
          f"envelope {args.envelope_id}{scope}: {what if wrote else done}")
    return 0


_ENVELOPE_RUN = {"echo": run_envelope_echo, "extend": run_envelope_extend, "sign": run_envelope_sign,
                 "accept": run_envelope_accept,
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
        threads = store.split(args.drop_id, parts, by=args.by, token=args.token)
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


def _chat_line(r: Any) -> str:
    """One timeline row for a terminal. Every body is quoted (:func:`_shown`); a vendor row says ``vendor`` and that it
    is quoted data, so nothing a worker's thread showed can read as a line of yours."""
    at = r.at if r.gseq is None else f"#{r.gseq} {r.at}"
    if r.kind == "message":
        status = f" [{r.status or 'no outbox row'}]"
        return f"  {at}  {r.source} said{status}: {_shown(r.body)}"
    if r.source == "vendor":
        return f"  {at}  vendor {r.kind.removeprefix('vendor_')} (quoted data): {_shown(r.body)}"
    body = "" if r.body is None else f": {_shown(r.body)}"
    return f"  {at}  {r.kind} ({r.source}) {r.ref_id}{body}"


def run_chat(args: argparse.Namespace, out: Any = None) -> int:
    from levain.coordinator.chat import ChatStore

    out = out or sys.stdout
    try:
        before, merged = ChatStore(_store(args)).timeline(args.thread_id)
    except _REFUSED as exc:
        print(f"levain chat {args.chat_cmd}: {exc}", file=sys.stderr)
        return 1
    def rows(rs: list[Any]) -> list[dict[str, Any]]:
        return [{"gseq": r.gseq, "kind": r.kind, "source": r.source, "ref_id": r.ref_id, "launch_id": r.launch_id,
                 "status": r.status, "body": r.body, "at": r.at} for r in rs]

    text = [f"thread {args.thread_id}"]
    if before:
        text += ["  before the chat record:", *(_chat_line(r) for r in before)]
    text += [_chat_line(r) for r in merged] or ["  no chat record yet"]
    _emit(out, args.json, {"thread_id": args.thread_id, "before": rows(before), "timeline": rows(merged)},
          "\n".join(text))
    return 0


def _chat_subparsers(subparsers: argparse._SubParsersAction) -> argparse._SubParsersAction:
    """The ``chat`` command's verb table. ``levain chat key`` (the chat launch's held keys) may have registered
    ``chat`` already; its verbs and ``show`` share the one command rather than argparse refusing a second ``chat``."""
    existing = subparsers.choices.get("chat")
    if existing is None:
        ch = subparsers.add_parser("chat", help="A thread's chat record and the chat launch's network.")
        return ch.add_subparsers(dest="chat_cmd", required=True)
    for action in existing._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise RuntimeError("the chat command was registered without a verb table")


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
            t = store.set_state(args.thread_id, args.state, by=args.by, cause=args.cause, token=args.token)
        elif cmd == "merge":
            t = store.merge(args.into, args.other, by=args.by, token=args.token)
        elif cmd == "detach":
            t = store.detach(args.thread_id, args.drop_id, by=args.by, token=args.token)
        elif cmd == "add":
            if args.about is not None and args.attach:
                raise CoordError("--about names what a NEW thread concerns; an attach joins an existing one")
            part = _parse_attach(args.attach) if args.attach else _parse_part(args.thread)
            if args.about is not None:
                part = Part(part.kind, part.title, about=args.about)
            t = store.add(args.drop_id, part, by=args.by, token=args.token)
        else:   # set
            t = store.set_field(args.thread_id, args.field, args.value, by=args.by, cause=args.cause,
                                token=args.token)
    except _REFUSED as exc:
        print(f"levain thread {args.thread_cmd}: {exc}", file=sys.stderr)
        return 1
    _emit(out, args.json, _thread_row(t), _thread_line(t))
    return 0


def _lease_line(lease: Any) -> str:
    if lease is None:
        return "lease: nobody holds the coordinator role"
    live = "live" if lease["live"] else "past its until"
    return f"lease: {lease['holder']} holds it until {lease['until']} ({live}), token {lease['token']}"


def run_coord(args: argparse.Namespace, out: Any = None) -> int:
    """``levain coord ...``. A ``review feeds --max-seconds N`` run is bounded by the kernel for the WHOLE command: the
    alarm is armed before the store is opened and disarmed only after the error line and the final flush, so no step of
    the process can outlive it. The caller's SIGALRM disposition and any alarm it had outstanding are put back
    afterwards (the CLI process exits and never notices; an in-process caller keeps its own)."""
    out = out or sys.stdout
    if args.coord_cmd != "review" or getattr(args, "review_cmd", None) != "feeds":
        return _run_coord(args, out)
    limit = getattr(args, "max_seconds", None) or 0
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        print("levain coord review: --max-seconds is a whole number of seconds >= 1, or 0 for no bound",
              file=sys.stderr)
        return 1
    if not limit:
        return _run_coord(args, out)
    if not hasattr(signal, "SIGALRM"):
        print("levain coord review feeds: no run bound on this platform (no SIGALRM)", file=sys.stderr)
        return _run_coord(args, out)
    # launchd coalesces per label, so a hung scheduled run would otherwise stop every later one
    prev_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    prev_alarm = signal.alarm(limit)
    try:
        return _run_coord(args, out)
    finally:
        _flush(out)
        _flush(sys.stderr)
        signal.alarm(0)
        if prev_handler is not None:
            signal.signal(signal.SIGALRM, prev_handler)
        if prev_alarm:
            signal.alarm(prev_alarm)


def _run_coord(args: argparse.Namespace, out: Any) -> int:
    cmd = args.coord_cmd
    try:
        store = _store(args)
        if cmd == "take":
            hold = None
            if args.hold is not None:
                m = _DURATION.fullmatch(args.hold.strip())
                if m is None or not (m.group(1) or m.group(2)):
                    raise CoordError(f"--for is a duration like 30m or 2h (got {args.hold[:40]!r})")
                hold = timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))
            tok = store.coord_take(hold)
            _emit(out, args.json, {"token": tok}, f"you hold the coordinator role, token {tok}")
            return 0
        if cmd == "release":
            ended = store.coord_release(args.token, by=args.by)
            _emit(out, args.json, {"ended": None if ended is None else ended.token},
                  "nobody held the lease; recorded that you looked" if ended is None
                  else f"released token {ended.token} ({ended.holder})")
            return 0
        if cmd == "beat":
            held = store.coord_beat(args.token)
            _emit(out, args.json, {"token": held.token, "until": held.until},
                  f"token {held.token} held until {held.until}")
            return 0
        if cmd == "claim":
            tok = store.coord_claim(args.launch, args.session)
            _emit(out, args.json, {"token": tok}, f"claimed: token {tok}")
            return 0
        if cmd == "agent":
            return _coord_agent(args, out, store)
        if cmd in ("questions", "answer", "escalate"):
            return _coord_first_reader(args, out, store)
        if cmd in ("morning", "away", "budget"):
            return _coord_c6(args, out, store)
        if cmd == "review":
            return _coord_review(args, out, store)
        if cmd == "launch":
            from levain.coordinator.launch import launch_session

            launch_id = args.launch_id
            if launch_id is None:
                pending = store.pending_coordinator()
                if pending is None:
                    raise CoordError("no coordinator launch is pending (levain coord status says why; "
                                     "levain coord wake asks for one)")
                launch_id = pending.launch_id
            got = launch_session(store, launch_id)
            _emit(out, args.json, {"launch_id": got.launch_id, "started": got.started, "attach": got.attach,
                                   "refusal": got.refusal, "remedy": got.remedy,
                                   "harness_session_id": got.harness_session_id},
                  got.attach if got.started else _outcome_text(got))
            return 0 if got.started else 1
        if cmd == "wake":
            # a pending launch has a starter wherever it is written: the wake starts the one it wrote, as a drop does
            ln = store.coord_wake()
            got = None
            if ln is not None:
                from levain.coordinator.launch import launch_session

                got = launch_session(store, ln.launch_id)
            st = store.coord_status()
            st["started"] = None if got is None else {"started": got.started, "attach": got.attach,
                                                      "refusal": got.refusal, "remedy": got.remedy}
            _emit(out, args.json, st, (f"asked for coordinator launch {ln.launch_id}\n{_outcome_text(got)}"
                                       if ln is not None else f"no launch asked for: {st['why']}"))
            return 0 if got is None or got.started else 1
        if cmd == "watch":
            from levain.coordinator.launch import migrate_setting, watch, watch_once

            if args.once:
                lines = migrate_setting(store) + watch_once(store)
                _emit(out, args.json, {"lines": lines}, "\n".join(lines) or "nothing to do")
                return 0
            if args.every < 1:
                raise CoordError("--every is at least 1 second")
            try:
                watch(store, every=args.every, emit=lambda line: print(line, file=out, flush=True))
            except KeyboardInterrupt:
                return 0
            return 0
        st = store.coord_status()
    except _REFUSED as exc:
        print(f"levain coord {cmd}: {exc}", file=sys.stderr)
        return 1
    lines = [_lease_line(st["lease"]), f"next drop: {st['wake']} ({st['why']})"]
    if st.get("setting"):
        lines.append(user_layer_text(st["setting"]))
    for ln in st["launches"]:
        lines.append(f"  {ln['launch_id']}  {ln['role']}  {ln['harness']}  {ln['state']} ({ln['cause']})  "
                     f"session {ln['session_key']}  asked {ln['at']}")
    _emit(out, args.json, st, "\n".join(lines))
    return 0


def user_layer_text(setting: Any) -> str:
    """The setting's user-layer line, as ``coord agent`` and ``coord status`` say it (a Setting or its dict)."""
    agent, on = (setting["agent"], setting.get("user_scope")) if isinstance(setting, dict) else (
        setting.agent, setting.user_scope)
    if agent != "claude":
        return "user layer: not applicable (not a claude coordinator)"
    if on:
        return "user layer: on (the coordinator loads your user settings, hooks and CLAUDE.md)"
    return "user layer: off (project settings only)"


def _coord_agent(args: argparse.Namespace, out: Any, store: EnvelopeStore) -> int:
    """``levain coord agent``: for claude, the directory is made a Levain install for Claude Code first when it is not
    one (``levain init``, non-interactive), and the setting is recorded only once its consent hook is live; codex is
    :func:`_coord_agent_codex`. Either workspace goes in the coordinator record before the setting names it."""
    user_scope = None if args.user_scope is None else args.user_scope == "on"
    if args.agent is None:
        # `levain coord agent --user-scope on|off` alone: the standing setting is kept whole, only the layer choice moves
        if user_scope is None:
            raise CoordError("name an agent (claude, codex or none), or give --user-scope on|off to change only that")
        if args.cwd is not None or args.model is not None:
            raise CoordError("--user-scope alone keeps the agent, model and directory; name the agent to change them")
        checked = None
        if user_scope:
            # turning it on: the hook check the next launch runs, now, so the refusal is this command's and not every
            # launch's (a user-level disableAllHooks the off setting was not consulting)
            from levain.coordinator.launch import hook_live

            was = store.coord_setting()
            if was is not None and was.agent == "claude":
                live, detail = hook_live(Path(was.cwd), user_scope=True)
                if not live:
                    raise CoordError(f"not set: with your user layer the consent hook is not live in {was.cwd}: "
                                     f"{detail}")
                checked = was.cwd
        got = store.coord_set_user_scope(user_scope, cwd=checked)
        _emit(out, args.json, {"agent": got.agent, "model": got.model, "cwd": got.cwd, "user_scope": got.user_scope},
              f"coordinator agent: {got.agent}; {user_layer_text(got)}")
        return 0
    if args.agent == "none":
        if args.cwd is not None or args.model is not None or user_scope is not None:
            raise CoordError("agent none takes no --model, no --cwd and no --user-scope")
        store.coord_set_agent("none")
        _emit(out, args.json, {"agent": "none"}, "coordinator agent: none (drops queue for you; nothing launches)")
        return 0
    from levain.coordinator import door
    from levain.coordinator.lease import check_model

    check_model(args.model)          # before any install is made for it (the setting checks it again as it is written)
    if args.agent == "codex":
        return _coord_agent_codex(args, out, store)
    if args.agent != "claude":
        raise CoordError(f"a {args.agent} coordinator cannot be launched yet; set claude, codex or none")
    from levain.coordinator.launch import default_coordinator_dir, setup_install, trust_remedy

    log = sys.stderr if args.json else out
    standing = store.coord_setting()
    effective = user_scope if user_scope is not None else (standing is not None and standing.user_scope)
    target, live, detail = setup_install(args.cwd or default_coordinator_dir(),
                                         emit=lambda line: print(line, file=log, flush=True), user_scope=effective)
    if not live:
        raise CoordError(f"not set: the consent hook is not live in {target}: {detail}")
    if not door.marked(target):
        # A live install Levain did not make for the coordinator is the operator's own: the door is open there, so a
        # coordinator launched into it would run his commands. It is never marked here either, since marking it would
        # door his own sessions in it. A fresh directory gets its own marked install.
        raise CoordError(f"not set: {target} is a Levain install, but not a coordinator's own (its "
                         ".levain/config.json records no coordinator role), and a coordinator runs only in its own "
                         f"workspace; leave out --cwd to use {default_coordinator_dir()}, or name a missing or empty "
                         "directory for Levain to set up")
    _record_workspace(target)
    got = store.coord_set_agent("claude", model=args.model, cwd=str(target), user_scope=user_scope)
    remedy = trust_remedy(target)
    _emit(out, args.json, {"agent": got.agent, "model": got.model, "cwd": got.cwd, "user_scope": got.user_scope,
                           "trust": remedy},
          f"coordinator agent: claude{f' ({got.model})' if got.model else ''} in {got.cwd}\n"
          f"{user_layer_text(got)}\n"
          "Claude Code starts a session only in a directory you trusted; run this once and accept the trust prompt:\n"
          f"{remedy}")
    return 0


def _band_snapshot(args: argparse.Namespace, store: EnvelopeStore) -> tuple[Any, str]:
    """The authority snapshot the envelope check reads, or ``(None, why)``: with none, nothing is live and every
    question escalates (an unchecked envelope never makes a question answerable)."""
    try:
        return _snapshot(args, store), ""
    except _faults() as exc:
        return None, str(exc)[:200]


def _coord_first_reader(args: argparse.Namespace, out: Any, store: EnvelopeStore) -> int:
    """``levain coord questions|answer|escalate`` (C5): the coordinator reads the question band first."""
    from levain.coordinator import first_reader as fr

    cmd = args.coord_cmd
    if cmd == "escalate":
        from levain.consent.band import release_held_pings

        reply_id = fr.escalate(store, args.ref, args.why, token=args.token)
        released = release_held_pings(store.directory)        # a ping held for it goes now (C5-1)
        _emit(out, args.json, {"reply_id": reply_id, "pings": released},
              f"escalated: question {args.ref} waits on the operator (ANSWER {reply_id}); the question is untouched"
              + "".join(f"\n{line}" for line in released))
        return 0
    if cmd == "questions":
        authority, unchecked = _band_snapshot(args, store)
        rows = fr.read_band(store, authority=authority)
        obj = [{"n": r.question.n, "id": r.question.id, "agent": r.question.agent,
                "session": r.question.agent_session_id, "what": r.question.what,
                "options": [{"key": k, "label": v} for k, v in r.question.options], "rec": r.question.rec,
                "blocks": r.question.blocks, "may_answer": r.may_answer, "why": r.why, "launch_id": r.launch_id,
                "thread_id": r.thread_id, "envelope_id": r.envelope_id} for r in rows]
        lines = [f"#{r.question.n} {json.dumps(r.question.what, ensure_ascii=False)}\n  "
                 + ("MAY ANSWER as a KNOW: " if r.may_answer else "ESCALATE: ") + r.why for r in rows]
        if unchecked:
            lines.append(f"(the envelopes were not checked: {unchecked})")
        _emit(out, args.json, {"questions": obj, "unchecked": unchecked or None},
              "\n".join(lines) if rows else "no unread questions" + (f" ({unchecked})" if unchecked else ""))
        return 0
    # the snapshot (a verified replay) is taken just before the answer's fenced transaction, and only the log's head is
    # compared inside it (C5 L3 r1, r2); with no head reader the snapshot is never live-checked again, so none is used
    try:
        head = _head_reader(args, store)
    except _faults():
        head = None
    item, reply_id = fr.answer(store, args.ref, option=args.option, text=args.text, token=args.token,
                               authority_source=(lambda: _band_snapshot(args, store)[0]) if head is not None
                               else (lambda: None), authority_head=head)
    _emit(out, args.json, {"item_id": item.item_id, "reply_id": reply_id},
          f"KNOW {reply_id} filed for your review ({item.item_id}); the seat gets it labelled as the coordinator's "
          "own, and the question stays open for you")
    return 0


def _coord_c6(args: argparse.Namespace, out: Any, store: EnvelopeStore) -> int:
    """``levain coord morning|away|budget`` (C6): the morning card, away from your word, the thread budget gauges."""
    from levain.coordinator import budget, morning

    cmd = args.coord_cmd
    if cmd == "morning":
        c = morning.card(store)
        _emit(out, args.json, c, morning.render(c))
        return 0
    if cmd == "away":
        got = morning.away_from_drop(store, args.drop_id, token=args.token)
        skipped = "".join(f"\n  NOT set for {a}: {w}; its SIGNs are not held as away" for a, w in
                          got["skipped"].items())
        only = " only" if got["skipped"] else ""
        _emit(out, args.json, got, f"away ON for {', '.join(got['marked'])}{only}, on the operator's word in drop "
                                   f"{args.drop_id}; KNOW {got['item_id']} tells him. Only he can leave it.{skipped}")
        return 0
    threads = [args.thread] if args.thread else [t.thread_id for t in store.threads()]
    with read_transaction(store._connect) as conn:
        gauges = [g for g in (budget.gauge(conn, store.directory, t) for t in threads) if g is not None]
    if args.thread and not gauges:
        raise CoordError(f"thread {args.thread[:40]} has no envelope, so no budget")
    lines = []
    for g in gauges:
        if g.unreadable is not None:
            # never "crossed" with zero usage: the meter could not be read (L3 r1 U1)
            lines.append(f"{g.thread_id} ({g.envelope_id}): UNREADABLE: {g.refusal}")
            continue
        used = ", ".join(f"{k} {budget._num(k, g.used[k])} of {g.budget[k]}" for k in budget.METERS)
        lines.append(f"{g.thread_id} ({g.envelope_id}): {used}; dollars not metered (Q13 counts none)")
        lines.append(f"  {g.refusal or 'within budget: new steps may start'}")
        if g.uncounted:
            lines.append(f"  not counted by Q13 yet: {', '.join(r for _a, r in g.uncounted)}")
    _emit(out, args.json, {"gauges": [g.as_dict() for g in gauges]}, "\n".join(lines) or "no thread has an envelope")
    return 0


def _coord_review_feeds(args: argparse.Namespace, out: Any, store: EnvelopeStore) -> int:
    """``levain coord review feeds``: one exclusive run. The ``--max-seconds`` bound is :func:`run_coord`'s, around the
    whole command."""
    from levain.coordinator import deliver

    ran = deliver.run_exclusive(store)
    if ran is None:
        _emit(out, args.json, {"skipped": True, "feeds": {}, "delivery": {}, "new_items": 0,
                               "reason": "another feeds run holds the lock"},
              "feeds: skipped, another run is in progress")
        return 0
    delivery = ran["delivery"]
    faults = deliver.faults(ran)
    ran["new_items"] = len(ran["feeds"].get("created", []))
    lines = [f"feeds: {ran['new_items']} new review item(s)",
             f"delivery: {len(delivery['signs'])} SIGN and {len(delivery['answers'])} ANSWER pointer(s) "
             f"reached, {delivery['created']} new"]
    lines += [f"could not read: {f}" for f in faults]
    _emit(out, args.json, ran, "\n".join(lines))
    return 1 if faults else 0


def _flush(out: Any) -> None:
    flush = getattr(out, "flush", None)
    if flush is not None:
        flush()


def _coord_review(args: argparse.Namespace, out: Any, store: EnvelopeStore) -> int:
    """``levain coord review list|clear|reopen|feeds`` (C4-a): the review queue. ``list`` and ``feeds`` run the feed
    runner first (:func:`levain.coordinator.deliver.run`, the one call a panel's read path will make too)."""
    from levain.coordinator import deliver, review

    cmd = args.review_cmd
    if cmd in ("clear", "reopen"):
        act = review.clear_item if cmd == "clear" else review.reopen_item
        item = act(store, args.item_id, mode=args.mode, token=args.token)
        _emit(out, args.json, {"item_id": item.item_id, "state": item.state, "mode": args.mode},
              f"{item.item_id} {'cleared' if cmd == 'clear' else 'reopened'} ({args.mode})")
        return 0
    if cmd == "feeds":
        return _coord_review_feeds(args, out, store)
    ran = deliver.run(store)
    faults = deliver.faults(ran)
    waiting = review.waiting_on_you(store, sign_open=review.queue_request_open(store.directory),
                                    answer_open=review.question_open(store.directory))
    items = review.list_items(store, include_cleared=args.all, feed=args.feed)
    rows = [{"item_id": i.item_id, "feed": i.feed, "thread_id": i.thread_id, "state": i.state,
             "authority_relevant": i.authority_relevant, "at": i.at,
             "text": str(review.body_fields(i).get("text", ""))[:500]}
            for i in items]
    lines = [f"review queue: {len(rows)} {'item(s)' if args.all else 'open item(s)'}"]
    for r in rows:
        lines.append(f"  {r['item_id']}  {r['feed']}{' (yours to clear)' if r['authority_relevant'] else ''}"
                     f"{'' if r['state'] == 'open' else ' [cleared]'}"
                     + (f"  [thread {r['thread_id']}]" if r["thread_id"] else "") + f": {r['text'][:160]}")
    lines += deliver.waiting_lines(waiting)
    lines += [f"could not read: {f}" for f in faults]
    _emit(out, args.json, {"items": rows, "waiting": waiting, "errors": faults}, "\n".join(lines))
    return 1 if faults else 0


def _record_workspace(target: Path) -> None:
    """Put ``target`` in the coordinator record the door reads (head ruling R1), before any setting names it."""
    from levain.coordinator import door

    try:
        door.record(target)
    except (OSError, door.RecordUnreadable) as exc:
        raise CoordError(f"not set: {target} could not be put in the coordinator record ({exc})") from exc


def _coord_agent_codex(args: argparse.Namespace, out: Any, store: EnvelopeStore) -> int:
    """``levain coord agent codex``: :func:`levain.coordinator.codex_launch.setup` makes the workspace (never the
    operator's own Codex install, never his hooks file), then it is recorded and the setting written."""
    from levain.coordinator import codex_launch

    if args.user_scope is not None:
        raise CoordError("--user-scope is for a claude coordinator (a Codex coordinator loads its own config)")
    target, ready, detail = codex_launch.setup(args.cwd)
    if not ready:
        raise CoordError(f"not set: {detail}")
    _record_workspace(target)
    got = store.coord_set_agent("codex", model=args.model, cwd=str(target), user_scope=False)
    _emit(out, args.json, {"agent": got.agent, "model": got.model, "cwd": got.cwd, "detail": detail},
          f"coordinator agent: codex{f' ({got.model})' if got.model else ''} in {got.cwd}\n{detail}")
    return 0


def review_feeds() -> tuple[str, ...]:
    from levain.coordinator import review

    return review.FEEDS


def review_modes() -> tuple[str, ...]:
    from levain.coordinator import review

    return review.MODES


def add_parsers(subparsers: argparse._SubParsersAction) -> None:
    qdir_help = "The consent directory holding dispatch.db (default: the consent queue's)."

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None, help=qdir_help)
        p.add_argument("--json", action="store_true", help="Machine-readable output.")

    def token(p: argparse.ArgumentParser) -> None:
        p.add_argument("--token", type=int, default=None,
                       help="With --by coordinator: the coordinator lease's token (levain coord status shows it); "
                            "the write is refused unless that token is the live lease's.")

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
    d.add_argument("--yes", action="store_true",
                   help="With --envelope: do not ask y/N; the whole view is still printed.")
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
    sp.add_argument("--by", default="operator", choices=("operator", "coordinator"))
    token(sp)
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
    token(tst)
    tst.add_argument("--cause", default="")
    common(tst)
    tm = t_sub.add_parser("merge", help="Fold OTHER into INTO (one intent the split cut in two).")
    tm.add_argument("into")
    tm.add_argument("other")
    tm.add_argument("--by", default="operator", choices=("operator", "coordinator"))
    token(tm)
    common(tm)
    td = t_sub.add_parser("detach", help="Take a drop out of a thread (it must stay in another).")
    td.add_argument("thread_id")
    td.add_argument("drop_id")
    td.add_argument("--by", default="operator", choices=("operator", "coordinator"))
    token(td)
    common(td)
    ta = t_sub.add_parser("add", help="Put an already-split drop into one more thread.")
    ta.add_argument("drop_id")
    g = ta.add_mutually_exclusive_group(required=True)
    g.add_argument("--thread", metavar="KIND:TITLE")
    g.add_argument("--attach", metavar="KIND:THREAD")
    ta.add_argument("--about", default=None, metavar="THREAD")
    ta.add_argument("--by", default="operator", choices=("operator", "coordinator"))
    token(ta)
    common(ta)
    tf = t_sub.add_parser("set", help="Re-kind, re-title or set the repo of a thread.")
    tf.add_argument("thread_id")
    tf.add_argument("field", choices=("kind", "title", "repo"))
    tf.add_argument("value")
    tf.add_argument("--by", default="operator", choices=("operator", "coordinator"))
    token(tf)
    tf.add_argument("--cause", default="")
    common(tf)
    for p in (tl, ts, tst, tm, td, ta, tf):
        p.set_defaults(func=run_thread)

    ch_sub = _chat_subparsers(subparsers)
    chs = ch_sub.add_parser(
        "show", help="One thread's chat timeline in the store's order: messages, what the worker's thread showed "
                     "(quoted, never your words), replies and envelope rows. Writes no row.")
    chs.add_argument("thread_id")
    common(chs)
    chs.set_defaults(func=run_chat)

    c = subparsers.add_parser(
        "coord", help="The coordinator lease: who holds the coordinator role, and the sessions launched for it.",
        description="One session holds the coordinator role at a time, or you do. Its writes carry the lease token "
                    "and are refused once the lease is lost or past its until. A drop with no holder asks for a "
                    "coordinator launch. Before S1 the lease is not authority: it decides whose thread writes count.")
    c_sub = c.add_subparsers(dest="coord_cmd", required=True)
    cs = c_sub.add_parser("status", help="Who holds the lease, what the next drop would do, the latest launches.")
    ct = c_sub.add_parser("take", help="Hold the coordinator role yourself (voids every pending launch).")
    ct.add_argument("--for", dest="hold", default=None, metavar="DURATION",
                    help="How long you hold it (like 30m or 2h; default 30m).")
    cr = c_sub.add_parser("release", help="End the lease (yours, or a session's: you looking after it).")
    cr.add_argument("--by", default="operator", choices=("operator", "coordinator"))
    token(cr)
    cb = c_sub.add_parser("beat", help="The holder keeps the lease (moves its until on).")
    cb.add_argument("--token", type=int, required=True)
    cc = c_sub.add_parser("claim", help="An active coordinator session claims a lease nobody holds.")
    cc.add_argument("--launch", required=True, metavar="LAUNCH_ID")
    cc.add_argument("--session", required=True, metavar="SESSION_KEY")
    cw = c_sub.add_parser("wake", help="Ask for a coordinator now, by the rules a drop runs (after a backoff).")
    ca = c_sub.add_parser(
        "agent", help="Choose the coordinator a drop launches: claude or codex (each in its own workspace), or none.",
        description="claude: drops with no coordinator start a Claude Code session in DIR (default "
                    "<levain home>/coordinator), which is made a Levain install for Claude Code first when it is not "
                    "one. codex: they start a thread on your Codex app-server daemon in DIR (default "
                    "<levain home>/coordinator-codex); Codex's consent hook must already be live (levain init "
                    "--adapter codex). Either workspace is put in Levain's coordinator record, which the coordinator's "
                    "door reads. none: drops only queue for you. Levain never edits a harness's trust list: run the "
                    "printed line once and trust the directory.")
    ca.add_argument("agent", nargs="?", default=None, choices=("claude", "codex", "none"))
    ca.add_argument("--user-scope", choices=("on", "off"), default=None, dest="user_scope",
                    help="claude: whether the coordinator also loads your user-level Claude Code settings, hooks, "
                         "CLAUDE.md (default off: it loads the project and local layers only). Alone "
                         "(no agent) it changes only this and keeps the standing agent, model and directory.")
    ca.add_argument("--cwd", type=Path, default=None, metavar="DIR",
                    help="The directory the coordinator runs in (default: <levain home>/coordinator, or "
                         "coordinator-codex): a missing or empty one, which Levain sets up, or a coordinator "
                         "workspace Levain set up before. Your own Levain install is refused.")
    ca.add_argument("--model", default=None, help="The model the session runs (default: the harness's own).")
    cl = c_sub.add_parser("launch", help="Start a pending launch's session now (default: the pending coordinator's).")
    cl.add_argument("launch_id", nargs="?", default=None, metavar="LAUNCH_ID")
    cwa = c_sub.add_parser(
        "watch", help="Keep the coordinator's sessions honest: start pending launches, end the gone, beat the live.",
        description="Each pass starts every pending launch nobody started (a drop from the phone or the cockpit), "
                    "ends a launch whose Claude Code session is no longer listed, beats the lease of the coordinator "
                    "whose session is listed, not done and writing as coordinator, and stops a session whose launch "
                    "is over. Only sessions Levain started.")
    cwa.add_argument("--every", type=float, default=30.0, metavar="S", help="Seconds between passes (default 30).")
    cwa.add_argument("--once", action="store_true", help="One pass, then exit.")
    band_root = ("The cockpit data root whose authority log re-verifies the asking session's envelope (default: the one "
                 "the consent hooks in the queue directory name).")
    cq = c_sub.add_parser(
        "questions", help="The seats' open questions you have not read yet, each with whether you may answer it.",
        description="You read the question band first (Q17). You may answer a question only when it was asked by a "
                    "worker Levain started under a live envelope, from inside a repo the envelope names, and no "
                    "option of it is tied to a consent request; everything else you escalate. Whether its subject "
                    "is inside the envelope is your judgement: the operator reviews every answer you give. A "
                    "question's directory and session are the asking process's own word, so 'asked from inside the "
                    "envelope' is a label, not proof.")
    cq.add_argument("--data-root", dest="data_root", type=Path, default=None, help=band_root)
    can = c_sub.add_parser(
        "answer", help="Answer a seat's question as your own KNOW (never the operator's answer).",
        description="Files a KNOW (the operator's review queue, never a push) and delivers it to the asking session "
                    "labelled as yours. The question stays open: the operator may still answer, and his answer is "
                    "the one that counts. Refused (escalate instead) outside the envelope.")
    can.add_argument("ref", help="The question's number or id.")
    pick = can.add_mutually_exclusive_group(required=True)
    pick.add_argument("--option", default=None, help="The option KEY you pick.")
    pick.add_argument("--text", default=None, help="Your answer in your own words.")
    can.add_argument("--token", type=int, required=True, help="Your coordinator lease token.")
    can.add_argument("--data-root", dest="data_root", type=Path, default=None, help=band_root)
    ces = c_sub.add_parser("escalate", help="Leave a seat's question to the operator (an ANSWER waiting on him).")
    ces.add_argument("ref", help="The question's number or id.")
    ces.add_argument("--why", required=True, help="Why it is the operator's, one line.")
    ces.add_argument("--token", type=int, required=True, help="Your coordinator lease token.")
    cmo = c_sub.add_parser(
        "morning", help="The morning card: away, what waits UNDECIDED, thread budgets, what waits on you.",
        description="What waits for you after a night away, read from the stores (display only: every line names the "
                    "command that decides it, which is yours).")
    caw = c_sub.add_parser(
        "away", help="Read a drop as the operator's word that he is away: set away ON (never off).",
        description="Sets away ON for both agents through `levain consent away on`'s own path, so every SIGN waits "
                    "UNDECIDED for his morning card, and files a KNOW saying so. Leaving away takes his signature.")
    caw.add_argument("drop_id", metavar="DROP", help="The drop whose words say he is away.")
    caw.add_argument("--token", type=int, required=True, help="Your coordinator lease token.")
    cbu = c_sub.add_parser(
        "budget", help="Each thread's budget against what its sessions used (Q13's counts): the gauge.",
        description="The budget is the thread's head envelope's; usage is Q13's count of the worker sessions pinned to "
                    "the thread's envelopes. Crossing it stops new steps; a SIGN extends it (levain envelope extend).")
    cbu.add_argument("thread", nargs="?", default=None, metavar="THREAD")
    crv = c_sub.add_parser(
        "review", help="The review queue: what waits for a look and never pushes (list, clear, reopen, feeds).",
        description="list and feeds first run the feed runner: every mechanical feed, and the delivery pass that files a "
                    "SIGN or ANSWER pointer for each open consent request or question of a coordinator thread. "
                    "Running either again files nothing new. Clearing is yours (manual), a fresh session you asked "
                    "(fresh), or the coordinator's own mode, which is refused for an authority-relevant item.")
    rv_sub = crv.add_subparsers(dest="review_cmd", required=True)
    rvl = rv_sub.add_parser("list", help="Open items and what waits on you.")
    rvl.add_argument("--all", action="store_true", help="Cleared items too.")
    rvl.add_argument("--feed", default=None, choices=review_feeds(), help="Only this feed.")
    rvc = rv_sub.add_parser("clear", help="Clear an open item.")
    rvo = rv_sub.add_parser("reopen", help="Open a cleared item again.")
    for p in (rvc, rvo):
        p.add_argument("item_id", metavar="ITEM")
        p.add_argument("--mode", default="manual", choices=review_modes(),
                       help="manual (you, the default), fresh (a fresh session you asked), coordinator (needs --token).")
        token(p)
    rvf = rv_sub.add_parser("feeds", help="Run the feeds and the delivery pass once and say what they reached.",
                            description="Takes a lock: while another feeds run holds it this one skips (exit 0). "
                                        "levain daemon install-feeds runs this on a schedule.")
    rvf.add_argument("--max-seconds", dest="max_seconds", type=int, default=None,
                     help="Whole seconds >= 1: the kernel ends the run (SIGALRM) if it is still going after this "
                          "long. 0 or omitted: no bound.")
    for p in (rvl, rvc, rvo, rvf):
        common(p)
        p.set_defaults(func=run_coord)
    for p in (cs, ct, cr, cb, cc, cw, ca, cl, cwa, cq, can, ces, cmo, caw, cbu):
        common(p)
        p.set_defaults(func=run_coord)

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
                    help="A repo the work may happen in, with the paths it may touch; the whole repo is REPO=. "
                         "written out. Repeatable.")
    ee.add_argument("--land", action="append", default=[], metavar="REPO=ONTO",
                    help="A merge the coordinator may land onto that branch. Repeatable.")
    ee.add_argument("--stop", default="2h", metavar="DURATION", help="When it stops: 2h, 90m, 1h30m (at most 24h).")
    ee.add_argument("--budget", default=None, metavar="K=N,...",
                    help="tool_calls=N,tokens=N,hours=N, each named (K=0 means none), and dollars_cents=N if you "
                         "want it (not metered; left out is 0). Without --budget every key is 0.")
    ee.add_argument("-o", "--output", type=Path, default=None, metavar="FILE",
                    help="Write the canonical body here (default: print it).")
    ee.add_argument("--force", action="store_true", help="Replace an existing -o file.")
    ee.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None, help=qdir_help)
    ee.add_argument("--json", action="store_true", help="Machine-readable output.")

    ex = e_sub.add_parser(
        "extend", help="Write the envelope that raises a thread's budget (a SIGN: only your signature extends it).",
        description="The thread's head envelope, as narrowed, with the budget you name; it supersedes the head once "
                    "you sign it with `levain envelope sign`. Keys you leave out keep the head's budget; a value "
                    "you name may not be below the current one, and at least one must be greater.")
    ex.add_argument("thread", metavar="THREAD")
    ex.add_argument("--budget", required=True, metavar="K=N,...",
                    help="tool_calls=N,tokens=N,dollars_cents=N,hours=N: the new total for any you name.")
    ex.add_argument("-o", "--output", type=Path, default=None, metavar="FILE",
                    help="Write the canonical body here (default: print it).")
    ex.add_argument("--force", action="store_true", help="Replace an existing -o file.")
    ex.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None, help=qdir_help)
    ex.add_argument("--json", action="store_true", help="Machine-readable output.")

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
    g.add_argument("--stop-earlier", dest="stop_earlier", metavar="WHEN",
                   help="Stop sooner: a duration from now (30m, 1h) or a time with its zone (2026-10-20T23:06:36Z).")
    g.add_argument("--drop-effect", dest="drop_effect", metavar="IDENTITY",
                   help="Drop one effect, by the identity `levain envelope show` prints beside it.")
    en.add_argument("--thread", default=None, metavar="THREAD")
    common(en)
    for p in (ee, ex, es, ea, esh, el, ek, en):
        p.set_defaults(func=run_envelope)
