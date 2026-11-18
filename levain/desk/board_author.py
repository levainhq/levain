"""levain.desk.board_author: the author of a close, derived from git (P4; levain#103).

Every close the board reports gets an author VERDICT, and NO close is ever refused for its author. Three tiers and one
"unattributed" verdict, the strongest that holds:

1. ``signed``: the closing commit's SSH signature is valid (:func:`levain.team.signing.verify_commit`) AND its fingerprint
   is a key in the authority log that was valid at the commit's time: ``operator <principal>, signed commit``.
2. ``trailer``: a ``Board-Seat: <durable key>`` trailer that a launch record maps to a seat: ``agent <name>, asserted
   (trailer)``. ASSERTED, never proof: anyone can copy the trailer (design section 9).
3. ``git``: otherwise the commit's git author: ``asserted by git author <name>``. The operator's own unsigned commit lands
   here and closes.
4. ``unattributed``: a ``document``/``run`` close whose path is outside version control: ``unattributed: the path exists
   outside version control``. Reported, never refused.

Where the close comes from:

* a close written by a NOTE (``refs/notes/board``) takes its author from the commit on that ref that wrote the note, never
  from the commit it annotates (a note is not covered by the annotated commit's signature);
* a ``document``/``run`` close inside the repo takes its author from the first-parent commit on main that ADDED the path
  (for a directory, the first such commit that added a file under it);
* a ``merged`` close carried by the commit's own ``Board-Closes`` trailer takes its author from that commit.

"Valid at the commit's time" uses the committer date, which the signer controls: a key that is now revoked and was
enrolled before the commit's date reads ``signed``, with the revocation named in ``how`` ("the commit's own time is the
signer's claim"), never silently valid. A key enrolled AFTER the commit's date, or revoked before it, is not tier 1.

The surfaced field is ``closed_by`` on a done item of ``levain board state --json`` (and a ``closed by:`` line of
``levain board context``): ``{"tier": signed|trailer|git|unattributed, "who": str|null, "how": str, "commit": sha|null}``.

The two lookups are the caller's, named by flags (``--data-root`` the cockpit whose authority log holds the operator's keys,
``--desk-dir`` the desk store whose launch records map a durable key to a seat); with neither named the tiers that need them
are not available and the verdict says so rather than guessing.
"""
from __future__ import annotations

import contextlib
import os
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from levain.desk import board_evidence as be
from levain.desk.board_state import parse_timestamp

__all__ = ["CockpitAuthority", "Closer", "Resolvers", "annotate", "closer_line", "closer_of_item", "describe",
           "resolvers_from_args", "add_resolver_args"]

MAIN_REF = "refs/remotes/origin/main"
NOTES_REF = "refs/notes/board"
OUTSIDE = "unattributed: the path exists outside version control"
_US, _RS, _GS = "\x1f", "\x1e", "\x1d"
_FACTS_FMT = ("%x1e%H%x1f%an%x1f%cI%x1f%(trailers:key=Board-Seat,valueonly,unfold,separator=%x1d)"
              "%x1f%(trailers:key=Board-Closes,valueonly,unfold,separator=%x1d)")
_SHA = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class Closer:
    tier: str                   # signed | trailer | git | unattributed
    who: str | None
    how: str
    commit: str | None = None   # the commit the verdict was read from (the note's writer for a note close); None: none
    note: str = ""              # what could not be checked, or the revoked key's caveat; "" when nothing

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"tier": self.tier, "who": self.who, "how": self.how, "commit": self.commit}
        if self.note:
            d["note"] = self.note
        return d


def describe(c: Closer | Mapping[str, Any]) -> str:
    """The verdict as one line: ``operator k.., signed commit`` | ``agent N, asserted (trailer)`` | ``asserted by git author
    N`` | ``unattributed: the path exists outside version control``."""
    d = c.to_dict() if isinstance(c, Closer) else dict(c)
    tier, who, how = d.get("tier"), d.get("who"), d.get("how")
    if tier == "git":
        line = f"{how} {who}"
    elif tier == "unattributed" or who is None:
        line = str(how)
    else:
        line = f"{who}, {how}"
    return line + (f" ({d['note']})" if d.get("note") else "")


# ------------------------------------------------------------------------------------------------ the two lookups
_DEFAULT = object()


class CockpitAuthority:
    """The authority log of the cockpit at ``data_root``, replayed once (:class:`levain.consent.queue.AuthoritySource`: the
    verified replay, anchor checked) and asked which key was valid when. ``anchor`` defaults to the cockpit's own; ``None``
    checks none (a scratch log). A log that cannot be read raises on the first question; :func:`closer_of_item` turns that
    into a hint, never a refusal."""

    def __init__(self, data_root: Path | str, *, anchor: Any = _DEFAULT) -> None:
        from levain.authority import anchor_path

        self.root = Path(data_root).expanduser()
        self._anchor = anchor_path(self.root) if anchor is _DEFAULT else anchor
        self._snap: Any = None
        self._times: dict[int, datetime] | None = None

    def _load(self) -> None:
        if self._snap is not None:
            return
        from levain.consent.queue import authority_source

        db = self.root / "cockpit.db"
        if not db.is_file():
            raise FileNotFoundError(f"{self.root} holds no cockpit (no cockpit.db)")
        self._snap = authority_source(db, anchor=self._anchor)()
        times: dict[int, datetime] = {}
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=10.0)
        try:
            import json

            for seq, record in conn.execute("SELECT seq, record FROM authority_log ORDER BY seq"):
                try:
                    at = parse_timestamp(json.loads(record).get("at"))
                except (ValueError, AttributeError, TypeError):
                    at = None
                if at is not None:
                    times[int(seq)] = at
        finally:
            conn.close()
        self._times = times

    def valid_at(self, fingerprint: str, when: datetime) -> tuple[str, str] | None:
        """``(principal, note)`` when ``fingerprint`` is a key of the log that was valid at ``when`` (``note`` names a later
        revocation, "" when the key is still active), else None."""
        self._load()
        key = self._snap.key(fingerprint)
        if key is None:
            return None
        start = parse_timestamp(key.enrolled_at or key.legacy_enrolled_at)
        if start is None or when < start:
            return None
        ends = [self._times.get(s) for s in (key.revoked_seq, key.compromised_since) if s is not None]
        if any(s is not None and self._times.get(s) is None for s in (key.revoked_seq, key.compromised_since)):
            return None                             # a revocation whose time cannot be read: not valid, never silently valid
        end = min((e for e in ends if e is not None), default=None)
        if end is not None and when >= end:
            return None
        note = ""
        if end is not None:
            note = (f"the key was revoked at {end.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}; the commit's own "
                    "time is the signer's claim")
        return key.principal, note


@dataclass
class Resolvers:
    """What the verdict may ask beyond git. ``authority`` None: tier 1 unavailable. ``seat_of(key)`` returns the seat's name
    for a durable key or None; None itself: tier 2 unavailable."""
    authority: CockpitAuthority | None = None
    seat_of: Callable[[str], str | None] | None = None


def add_resolver_args(parser: Any) -> None:
    parser.add_argument("--data-root", dest="data_root", type=Path, default=None, metavar="DIR",
                        help="The cockpit data root whose authority log holds the operator's keys: a close's author is read "
                             "as 'operator ...' only when its signing key is there. Without it a signature is not checked.")
    parser.add_argument("--desk-dir", dest="desk_dir", type=Path, default=None, metavar="DIR",
                        help="The desk store whose launch records map a Board-Seat durable key to a seat. Without it a "
                             "Board-Seat trailer is not resolved.")


def resolvers_from_args(args: Any) -> Resolvers:
    res = Resolvers()
    root = getattr(args, "data_root", None)
    if root is not None:
        res.authority = CockpitAuthority(root)
    desk = getattr(args, "desk_dir", None)
    if desk is not None:
        res.seat_of = _desk_seat_of(Path(desk).expanduser())
    return res


def _desk_seat_of(directory: Path) -> Callable[[str], str | None]:
    """A seat-name lookup over the launch records of the desk store in ``directory`` (read-only; a store that cannot be read
    answers None for every key)."""
    def seat_of(key: str) -> str | None:
        from levain.desk import lease
        from levain.desk.envelopes import EnvelopeStore

        try:
            conn = EnvelopeStore(directory).open_readonly()
        except Exception:  # noqa: BLE001 - no launch record is readable: the trailer stays unresolved, the close stands
            return None
        try:
            ln = lease._launch_by_key(conn, key)
        except Exception:  # noqa: BLE001
            return None
        finally:
            conn.close()
        return None if ln is None else f"{ln.role} {ln.launch_id}"
    return seat_of


# ------------------------------------------------------------------------------------------------ the verdict
@dataclass(frozen=True)
class _Facts:
    sha: str
    author: str
    when: datetime | None
    seats: tuple[str, ...]
    closes: tuple[str, ...]


def _facts(repo: Path, shas: Iterable[str]) -> dict[str, _Facts]:
    """One git call for every commit's author, committer date and the two trailers."""
    want = [s for s in dict.fromkeys(shas) if _SHA.fullmatch(s)]
    out: dict[str, _Facts] = {}
    if not want:
        return out
    text = be._git(repo, "log", "--no-walk=unsorted", "--no-notes", f"--format={_FACTS_FMT}", *want, "--")
    for rec in text.split(_RS):
        if not rec.strip("\n"):
            continue
        f = rec.split(_US)
        if len(f) != 5 or not _SHA.fullmatch(f[0].strip()):
            raise be.GitFailed("git log gave a commit record this reader cannot parse")
        sha = f[0].strip()
        seats = tuple(dict.fromkeys(v.strip() for v in f[3].split(_GS) if v.strip()))
        closes = tuple(i for v in f[4].split(_GS) for i in be._ids_of(v))
        out[sha] = _Facts(sha, f[1], parse_timestamp(f[2].strip()), seats, closes)
    return out


class _Reader:
    """One run's memo: the facts of a repo's commits and the verdict per commit."""

    def __init__(self, repo: Path, res: Resolvers) -> None:
        self.repo, self.res = repo, res
        self._facts: dict[str, _Facts] = {}
        self._verdicts: dict[str, Closer] = {}

    def prime(self, shas: Iterable[str]) -> None:
        need = [s for s in shas if s not in self._facts]
        if need:
            self._facts.update(_facts(self.repo, need))

    def facts(self, sha: str) -> _Facts:
        self.prime([sha])
        if sha not in self._facts:
            raise be.GitFailed(f"commit {sha} cannot be read")
        return self._facts[sha]

    def author(self, sha: str) -> Closer:
        """The three-tier verdict for the commit ``sha``."""
        if sha in self._verdicts:
            return self._verdicts[sha]
        f = self.facts(sha)
        notes: list[str] = []
        sig = None
        with contextlib.suppress(Exception):
            from levain.team import signing

            sig = signing.verify_commit(self.repo, sha)
        if sig is not None and sig.kind == "signed":
            if self.res.authority is None:
                notes.append("the commit is signed but no authority log was named (--data-root), so the signature is not checked")
            elif f.when is None:
                notes.append("the commit is signed but its date cannot be read, so the key's validity cannot be judged")
            else:
                try:
                    got = self.res.authority.valid_at(sig.fingerprint or "", f.when)
                except Exception as exc:  # noqa: BLE001 - an authority log that cannot be read: not tier 1, said, never a refusal
                    got = None
                    notes.append(f"the commit is signed but the authority log cannot be read ({type(exc).__name__})")
                else:
                    if got is None:
                        notes.append("the commit is signed by a key that was not valid in the authority log at its date")
                if got is not None:
                    principal, note = got
                    return self._remember(sha, Closer("signed", f"operator {principal}", "signed commit", sha, note))
        if len(f.seats) == 1:
            if self.res.seat_of is None:
                notes.append("a Board-Seat trailer is present but no launch records were named (--desk-dir)")
            else:
                try:
                    name = self.res.seat_of(f.seats[0])
                except Exception:  # noqa: BLE001
                    name = None
                if name:
                    return self._remember(sha, Closer("trailer", f"agent {name}", "asserted (trailer)", sha, "; ".join(notes)))
                notes.append("its Board-Seat trailer maps to no launch record")
        elif len(f.seats) > 1:
            notes.append("the commit carries several different Board-Seat trailers, so none is read")
        return self._remember(sha, Closer("git", f.author or "(no author name)", "asserted by git author", sha, "; ".join(notes)))

    def _remember(self, sha: str, c: Closer) -> Closer:
        self._verdicts[sha] = c
        return c


# ------------------------------------------------------------------------------------------------ where the close comes from
def _note_writer(repo: Path, close_sha: str, item_id: str) -> str | None:
    """The commit on ``refs/notes/board`` that wrote the ``Board-Closes: <item>`` line of the note on ``close_sha``: the
    oldest commit whose version of the note carries it (the notes tree fans out as it grows, so every path form is tried)."""
    cp = be._run(repo, "rev-parse", "--verify", "--quiet", f"{NOTES_REF}^{{commit}}")
    tip = cp.stdout.decode().strip() if cp.returncode == 0 else ""
    if not tip:
        return None
    paths = [close_sha, f"{close_sha[:2]}/{close_sha[2:]}", f"{close_sha[:2]}/{close_sha[2:4]}/{close_sha[4:]}"]
    log = be._git(repo, "log", "--reverse", "--no-renames", "--format=%H", tip, "--", *paths).split()
    for commit in log:
        for p in paths:
            cp = be._run(repo, "cat-file", "blob", f"{commit}:{p}")
            if cp.returncode != 0:
                continue
            text = cp.stdout.decode("utf-8", "replace")
            for kind, value in be._NOTE_LINE.findall(text):
                if kind.lower() == "closes" and item_id in be._ids_of(value):
                    return commit
    return None


def _path_adder(repo: Path, key: str, main_ref: str) -> tuple[str | None, bool]:
    """``(the first-parent commit on main that added the path, inside_repo)``. The path is a ``done`` text: relative to the
    repo, absolute, or with ``~``."""
    path = Path(os.path.expanduser(key))
    if not path.is_absolute():
        path = Path(repo) / path
    top = be._git(repo, "rev-parse", "--show-toplevel").strip()
    real, root = os.path.realpath(path), os.path.realpath(top)
    try:
        inside = os.path.commonpath([real, root]) == root
    except ValueError:
        inside = False
    if not inside or real == root:
        return None, False
    rel = os.path.relpath(real, root)
    out = be._git(repo, "log", "--first-parent", "--no-renames", "--diff-filter=A", "--format=%H", "--reverse", main_ref, "--",
                  f":(literal){rel}").split()
    return (out[0] if out else None), True


def closer_of_item(repo: Path, item: Any, state_ref: str | None, res: Resolvers, *, main_ref: str = MAIN_REF,
                   reader: _Reader | None = None) -> Closer:
    """The verdict for the close of ``item``: ``state_ref`` is the board state's first ref (the closing commit of a ``merged``
    item). A git trouble raises :class:`~levain.desk.board_evidence.GitFailed`; :func:`annotate` catches it and says it."""
    rd = reader or _Reader(Path(repo), res)
    kind = getattr(item, "done_kind", None)
    if kind == "merged":
        if not state_ref or not _SHA.fullmatch(state_ref):
            return Closer("unattributed", None, "the closing commit is not named by the board state")
        f = rd.facts(state_ref)
        if item.id in f.closes:
            return rd.author(state_ref)
        writer = _note_writer(Path(repo), state_ref, item.id)
        if writer is None:
            return Closer("unattributed", None, f"the close is a note on {state_ref[:9]} and the commit on {NOTES_REF} that "
                                                f"wrote it was not found")
        return rd.author(writer)
    if kind in ("document", "run"):
        sha, inside = _path_adder(Path(repo), (item.done or "").strip(), main_ref)
        if sha is None:
            return Closer("unattributed", None, OUTSIDE)
        return rd.author(sha)
    return Closer("unattributed", None, f"a {kind} close is not a git close")


# ------------------------------------------------------------------------------------------------ surfacing
def annotate(doc: dict[str, Any], items: Mapping[str, Any], repos: Mapping[str | None, Path], res: Resolvers, *,
             default_repo: Path | None = None, main_ref: str = MAIN_REF) -> int:
    """Set ``closed_by`` (:meth:`Closer.to_dict`) on every ``done`` item of the board ``doc`` whose close is git's (a ``merged``,
    ``document`` or ``run`` item; a band row's close is the np row's, with no author to read here). ``repos`` maps an item's
    repo name to its path (``default_repo`` for any other: the single-repo board reads every item from one repo). One memo
    per repo, one git call for every closing commit's facts. A git trouble on one item is said on that item
    (``unattributed``), never raised and never a refusal. Returns how many items it annotated."""
    readers: dict[str, _Reader] = {}
    todo: list[tuple[dict[str, Any], Any, Path]] = []
    for s in doc.get("items", ()):
        if s.get("state") != "done":
            continue
        item = items.get(s.get("id"))
        if item is None or getattr(item, "done_kind", None) not in ("merged", "document", "run"):
            continue
        path = repos.get(s.get("repo"), default_repo)
        if path is None:
            continue
        todo.append((s, item, Path(path)))
    for s, item, path in todo:
        readers.setdefault(str(path), _Reader(path, res))
    by_repo: dict[str, list[str]] = {}
    for s, item, path in todo:
        refs = s.get("refs") or [None]
        if item.done_kind == "merged" and isinstance(refs[0], str):
            by_repo.setdefault(str(path), []).append(refs[0])
    for key, shas in by_repo.items():
        with contextlib.suppress(be.GitFailed):
            readers[key].prime(shas)
    n = 0
    for s, item, path in todo:
        refs = s.get("refs") or [None]
        try:
            c = closer_of_item(path, item, refs[0] if isinstance(refs[0], str) else None, res, main_ref=main_ref,
                               reader=readers[str(path)])
        except be.GitFailed as exc:
            c = Closer("unattributed", None, f"the author could not be read ({exc})")
        s["closed_by"] = c.to_dict()
        n += 1
    return n


def closer_line(repo: Path, item: Any, state: Any, res: Resolvers, *, main_ref: str = MAIN_REF) -> str | None:
    """The ``closed by: ...`` line of ``levain board context`` for a done item (None for a state that is not done or a close
    that is not git's)."""
    if getattr(state, "state", None) != "done" or getattr(item, "done_kind", None) not in ("merged", "document", "run"):
        return None
    refs = list(getattr(state, "refs", ()) or ())
    try:
        c = closer_of_item(Path(repo), item, refs[0] if refs and isinstance(refs[0], str) else None, res, main_ref=main_ref)
    except be.GitFailed as exc:
        c = Closer("unattributed", None, f"the author could not be read ({exc})")
    return f"closed by: {describe(c)}"
