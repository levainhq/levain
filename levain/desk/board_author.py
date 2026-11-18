"""levain.desk.board_author: the author of a close, derived from git (P4; levain#103).

The rule: a ``closed_by`` is the WEAKEST claim the evidence supports. Every tier is computed by the rule the rest of levain
already uses for that evidence (a signing key by :func:`levain.desk.board_sign.key_standing`, the rule ``verify_record``
applies; a ``done`` path by :func:`levain.desk.board_evidence.done_path`, the rule ``read_documents`` applies; a launch's
claim by its ``board_items``); this module never holds a second, narrower or wider, rule beside one of them.

Every close the board reports gets an author VERDICT, and NO close is ever refused for its author. Three tiers and one
"unattributed" verdict, the strongest that holds:

1. ``signed``: the closing commit's SSH signature is valid (:func:`levain.team.signing.verify_commit`), the board's key
   rule accepts its key as a terminal (laptop-presence) signer (in the log, of that class, not quarantined, not revoked),
   and the key is live in the operator's log and enrolled no later than the commit's own (signer-set) date: ``signed by an
   operator key <principal>``. It says a key of the operator's signed; it does not say a person acted.
2. ``trailer``: a ``Board-Seat: <durable key>`` trailer that a launch record maps to a launch which CLAIMED this item
   (its ``board_items``) and ran in this item's repo: ``agent <role> <launch>, asserted (trailer)``. ASSERTED, never
   proof: anyone can copy the trailer (design section 9). A trailer whose launch claimed other items, or ran elsewhere, is
   not read, and the note says why.
3. ``git``: otherwise the commit's git author: ``asserted by git author <name> <email>``. The operator's own unsigned
   commit lands here and closes.
4. ``unattributed``: no commit to read (a ``document``/``run`` path outside the repo's work tree, one in the tree that no
   commit on main added, a note whose writer is not found). Reported, never refused.

Every verdict read from a commit whose committer is not its author also names the committer (``committed by B``).

Where the close comes from:

* a ``merged`` close carried by the commit's own ``Board-Closes`` trailer takes its author from that commit (the commit the
  board state names: its close rule, latest by committer date, ties by sha);
* a close written by a NOTE (``refs/notes/board``) takes its author from the commit on that ref that wrote the note line,
  never from the commit it annotates (a note is not covered by the annotated commit's signature). Of several note commits
  that wrote the line (one removed it, another wrote it again), the LATEST addition, as the board's own close rule takes
  the latest close. ``commit`` is then the note's commit and ``close_commit`` the annotated commit. The note is read at the
  live ``refs/notes/board`` tip, not the board's evidence snapshot: a note rewritten between the two reads is attributed as
  it stands now;
* a ``document``/``run`` close is the file existing: nobody performed it. Its verdict is the tier of the first-parent
  commit on main that first ADDED the path (a directory: the first that added a file under it), and its ``how`` says
  ``first added <path>``, so it never reads as the closer. Later edits are not read.

"Enrolled no later than the commit's own date": the key's ENROL time (the authority log's enrol link ``at``, which
authority.py keeps as provenance) is compared with the commit's committer date. Both are claims (the committer date is set
by the signer), so this is a sanity check, never a proof of when the commit was made. The snapshot carries no time for a
REVOCATION (only its seq), so a revoked key is never credited as tier 1 whatever the commit's date: the commit falls to its
next tier and the note names the key's fingerprint and the revoked seq.

The surfaced field is ``closed_by`` on a done item of ``levain board state --json`` (and a ``closed by:`` line of
``levain board context``): ``{"tier", "who", "how", "commit", "email"?, "committer"?, "close_commit"?, "note"?}``.

The two lookups DEFAULT to this install's own: the cockpit whose authority log the board's signed verbs verify against
(:func:`levain.desk.board_sign.resolve_authority`, the one reader: ``read_authority`` over the verified replay, anchor
checked) and the desk store the board's native producer reads (:func:`levain.desk.work_attach.live_consent_dir`).
``--data-root`` / ``--desk-dir`` override. Both are read-only. A default that is absent or unreadable reads "not checked" with
the reason in the verdict's note (never an error, never a silent tier-3 claim presented as checked).
"""
from __future__ import annotations

import contextlib
import dataclasses
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from levain.desk import board_evidence as be
from levain.desk.board_state import parse_timestamp

__all__ = ["CockpitAuthority", "Closer", "Resolvers", "Seat", "annotate", "closer_line", "closer_of_item", "describe",
           "mark_unread", "resolvers_from_args", "add_resolver_args"]

MAIN_REF = f"refs/remotes/{be.REMOTE}/{be.MAIN_BRANCH}"     # the evidence reader's own remote and main
NOTES_REF = be._NOTES_REF
COMMIT_VIA = "terminal"     # a git commit is SSH-signed at a terminal: board_sign's laptop-presence class
OUTSIDE = "unattributed: the path exists outside version control (outside the repo's work tree)"
NOT_ON_MAIN = "unattributed: the path is in the repo's work tree but no commit on main added it"
_US, _RS, _GS = "\x1f", "\x1e", "\x1d"
_FACTS_FMT = ("%x1e%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%cI%x1f%(trailers:key=Board-Seat,valueonly,unfold,separator=%x1d)"
              "%x1f%(trailers:key=Board-Closes,valueonly,unfold,separator=%x1d)")
_SHA = re.compile(r"[0-9a-f]{40}")


def _clip(text: str, n: int = 220) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 3] + "..."


@dataclass(frozen=True)
class Closer:
    tier: str                   # signed | trailer | git | unattributed
    who: str | None             # signed: the key's principal; trailer: the agent; git: the author's name
    how: str
    commit: str | None = None   # the commit the verdict was read from (the note's writer for a note close); None: none
    note: str = ""              # what could not be checked, or why a stronger tier was not credited; "" when nothing
    email: str | None = None    # git tier: the author's email
    committer: str | None = None       # "name <email>" when the committer is not the author
    close_commit: str | None = None    # a note close: the annotated commit the note closes

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"tier": self.tier, "who": self.who, "how": self.how, "commit": self.commit}
        for k in ("email", "committer", "close_commit"):
            if getattr(self, k) is not None:
                d[k] = getattr(self, k)
        if self.note:
            d["note"] = self.note
        return d


def describe(c: Closer | Mapping[str, Any]) -> str:
    """The verdict as one line: ``signed by an operator key k..`` | ``agent N, asserted (trailer)`` | ``asserted by git
    author N <e>`` | ``unattributed: ...``, then ``, committed by B`` and the note."""
    d = c.to_dict() if isinstance(c, Closer) else dict(c)
    tier, who, how = d.get("tier"), d.get("who"), d.get("how")
    if tier == "unattributed" or who is None:
        line = str(how)
    elif tier == "trailer":
        line = f"{who}, {how}"
    else:
        line = f"{how} {who}" + (f" <{d['email']}>" if d.get("email") else "")
    if d.get("committer"):
        line += f", committed by {d['committer']}"
    return line + (f" ({d['note']})" if d.get("note") else "")


# ------------------------------------------------------------------------------------------------ the two lookups
class NotChecked(Exception):
    """A lookup that could not be made (absent, unreadable): its text is the reason the verdict's note carries."""


class CockpitAuthority:
    """The authority log of the cockpit at ``data_root`` (default: the one the board's own signed verbs resolve,
    :func:`levain.desk.board_sign.resolve_authority`), replayed once through the ONE reader
    (:func:`levain.desk.board_sign.read_authority`: verified replay, anchor checked) and judged by the ONE key rule
    (:func:`levain.desk.board_sign.key_standing`). A log that cannot be resolved or read raises :class:`NotChecked` on every
    question (the failure is remembered: one read per board read); :class:`_Reader` turns that into a note."""

    def __init__(self, data_root: Path | str | None = None) -> None:
        self.root = None if data_root is None else Path(data_root).expanduser()
        self._snap: Any = None
        self._failed: str | None = None

    def _load(self) -> Any:
        if self._snap is not None:
            return self._snap
        if self._failed is not None:
            raise NotChecked(self._failed)
        from levain.desk import board_sign as bsign

        try:
            self._snap = bsign.read_authority(bsign.resolve_authority(data_root=self.root))
        except (bsign.bs.BoardError, OSError, ValueError) as exc:
            text = str(exc)     # board_sign's refusal wraps the reason in parentheses after its "nothing was written" text
            got = re.search(r"\((.+)\)", text, re.S)     # first "(" to last ")"; none (or garbled): the whole text
            self._failed = got.group(1) if got else text
            raise NotChecked(self._failed) from None
        return self._snap

    def credit(self, fingerprint: str, when: datetime) -> tuple[str | None, str]:
        """``(principal, "")`` when the board's key rule accepts ``fingerprint`` as a terminal signer and its enrol time is
        not after ``when``; else ``(None, why)``."""
        from levain.desk import board_sign as bsign

        standing, why, key = bsign.key_standing(self._load(), fingerprint, COMMIT_VIA)
        if standing == "refused":
            return None, f"the commit is signed, but the board's key rule refuses the key: {why}"
        if standing == "revoked":
            return None, (f"the commit is signed by {fingerprint}, revoked at seq {key.revoked_seq} in the authority log; "
                          "the log carries no revocation time, so the signature is not credited")
        raw = key.enrolled_at or key.legacy_enrolled_at
        start = parse_timestamp(raw)
        if start is None:
            return None, (f"the commit is signed by {fingerprint}, whose enrolment carries no readable time"
                          f"{f' ({_clip(str(raw), 40)})' if raw else ''}, so it cannot be placed before the commit's date")
        if when < start:
            return None, (f"the commit is signed by {fingerprint}, enrolled {start.isoformat()}, after the commit's own "
                          f"date {when.isoformat()}")
        return key.principal, ""


@dataclass(frozen=True)
class Seat:
    """A launch record as the trailer tier reads it: its name, the board items it claimed (None: none, or unreadable, then
    ``items_why``) and its working directory."""
    name: str
    items: tuple[str, ...] | None
    cwd: str | None
    items_why: str | None = None


@dataclass
class Resolvers:
    """What the verdict may ask beyond git. ``authority`` None: tier 1 unavailable. ``seat_of(key)`` returns the
    :class:`Seat` for a durable key or None, and raises :class:`NotChecked` when the launch records cannot be read; None
    itself: tier 2 unavailable."""
    authority: CockpitAuthority | None = None
    seat_of: Callable[[str], Seat | None] | None = None


def add_resolver_args(parser: Any) -> None:
    parser.add_argument("--data-root", dest="data_root", type=Path, default=None, metavar="DIR",
                        help="The cockpit data root whose authority log holds the operator's keys (default: this install's "
                             "own cockpit, the one the board's signed verbs use): a close reads 'signed by an operator key' "
                             "only when the board's key rule accepts its signing key there. An absent cockpit reads 'not "
                             "checked'.")
    parser.add_argument("--desk-dir", dest="desk_dir", type=Path, default=None, metavar="DIR",
                        help="The desk store whose launch records map a Board-Seat durable key to a seat (default: this "
                             "install's own, the one the board's seat producer reads). An absent store reads 'not checked'.")


def resolvers_from_args(args: Any) -> Resolvers:
    """The install's own cockpit and desk store unless a flag names another; both read-only and lazy (an absent default is
    said when a commit needs it, never an error)."""
    root = getattr(args, "data_root", None)
    desk = getattr(args, "desk_dir", None)
    return Resolvers(authority=CockpitAuthority(root), seat_of=_desk_seat_of(None if desk is None else Path(desk).expanduser()))


def _desk_seat_of(directory: Path | None) -> Callable[[str], Seat | None]:
    """A launch lookup over the launch records of the desk store in ``directory`` (default: the install's own,
    :func:`levain.desk.work_attach.live_consent_dir`). Read-only. An absent or unreadable store raises :class:`NotChecked`;
    a readable one that holds no such key answers None."""
    def seat_of(key: str) -> Seat | None:
        from levain.desk import lease, work_attach
        from levain.desk.envelopes import EnvelopeStore
        from levain.desk.store import StoreAbsent

        try:
            where = directory if directory is not None else work_attach.live_consent_dir()
            conn = EnvelopeStore(where).open_readonly()
        except StoreAbsent as exc:
            raise NotChecked(str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - the default home cannot even be named, or the store cannot be opened
            raise NotChecked(f"the desk store cannot be read ({type(exc).__name__}: {exc})") from None
        try:
            ln = lease._launch_by_key(conn, key)
        except Exception as exc:  # noqa: BLE001
            raise NotChecked(f"the launch records cannot be read ({type(exc).__name__})") from None
        finally:
            conn.close()
        if ln is None:
            return None
        return Seat(f"{ln.role} {ln.launch_id}", ln.board_items, ln.cwd, ln.board_items_why)
    return seat_of


# ------------------------------------------------------------------------------------------------ the verdict
@dataclass(frozen=True)
class _Facts:
    sha: str
    author: str
    email: str
    committer: str | None       # "name <email>" when not the author
    when: datetime | None       # the committer date
    seats: tuple[str, ...]
    closes: tuple[str, ...]


def _facts(repo: Path, shas: Iterable[str]) -> dict[str, _Facts]:
    """One git call for every commit's author, committer, committer date and the two trailers."""
    want = [s for s in dict.fromkeys(shas) if _SHA.fullmatch(s)]
    out: dict[str, _Facts] = {}
    if not want:
        return out
    text = be._git(repo, "log", "--no-walk=unsorted", "--no-notes", f"--format={_FACTS_FMT}", *want, "--")
    for rec in text.split(_RS):
        if not rec.strip("\n"):
            continue
        f = rec.split(_US)
        if len(f) != 8 or not _SHA.fullmatch(f[0].strip()):
            raise be.GitFailed("git log gave a commit record this reader cannot parse")
        sha = f[0].strip()
        committer = None if (f[3], f[4]) == (f[1], f[2]) else f"{f[3]} <{f[4]}>"
        seats = tuple(dict.fromkeys(v.strip() for v in f[6].split(_GS) if v.strip()))
        closes = tuple(i for v in f[7].split(_GS) for i in be._ids_of(v))
        out[sha] = _Facts(sha, f[1], f[2], committer, parse_timestamp(f[5].strip()), seats, closes)
    return out


class _Reader:
    """One run's memo: the facts of a repo's commits, the signature verdict per commit, the repo's own git dir."""

    def __init__(self, repo: Path, res: Resolvers) -> None:
        self.repo, self.res = repo, res
        self._facts: dict[str, _Facts] = {}
        self._signed: dict[str, tuple[str | None, list[str]]] = {}
        self._common: dict[str, str | None] = {}

    def prime(self, shas: Iterable[str]) -> None:
        need = [s for s in shas if s not in self._facts]
        if need:
            self._facts.update(_facts(self.repo, need))

    def facts(self, sha: str) -> _Facts:
        self.prime([sha])
        if sha not in self._facts:
            raise be.GitFailed(f"commit {sha} cannot be read")
        return self._facts[sha]

    def _signature(self, sha: str, f: _Facts) -> tuple[str | None, list[str]]:
        """``(principal, notes)``: the principal when tier 1 holds, else None and why not (memoised per commit)."""
        if sha in self._signed:
            return self._signed[sha]
        notes: list[str] = []
        principal = None
        sig = None
        try:
            from levain.team import signing

            sig = signing.verify_commit(self.repo, sha)
        except Exception as exc:  # noqa: BLE001 - a signature that cannot be judged is said, never raised
            notes.append(f"the commit's signature could not be checked ({type(exc).__name__})")
        if sig is not None and sig.kind == "indeterminate":
            notes.append(f"the commit's signature could not be checked ({_clip(sig.reason, 120)})")
        elif sig is not None and sig.kind == "unsigned" and sig.reason != "no signature":
            notes.append(f"the commit carries a signature that does not verify ({_clip(sig.reason, 120)})")
        elif sig is not None and sig.kind == "signed":
            if self.res.authority is None:
                notes.append("the commit is signed but no authority log is consulted, so the signature is not checked")
            elif f.when is None:
                notes.append("the commit is signed but its date cannot be read, so the key's enrolment cannot be judged")
            else:
                try:
                    principal, why = self.res.authority.credit(sig.fingerprint or "", f.when)
                    if why:
                        notes.append(why)
                except NotChecked as exc:  # an authority log that is absent or unreadable: not checked, said, never a refusal
                    notes.append(f"the commit is signed but the authority log was not checked ({_clip(str(exc))})")
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"the commit is signed but the authority log cannot be read ({type(exc).__name__})")
        self._signed[sha] = (principal, notes)
        return principal, notes

    def _git_dir(self, where: str | Path) -> str | None:
        key = str(where)
        if key not in self._common:
            try:
                cp = be._run(Path(where), "rev-parse", "--path-format=absolute", "--git-common-dir")
                self._common[key] = cp.stdout.decode("utf-8", "replace").strip() if cp.returncode == 0 else None
            except be.GitFailed:
                self._common[key] = None
        return self._common[key]

    def _seat_refusal(self, seat: Seat, item: Any) -> str | None:
        """Why the launch ``seat`` is not credited with closing ``item`` (None: it claimed the item, in this repo)."""
        if seat.items is None:
            return (f"the launch's board-items claim cannot be read ({_clip(seat.items_why, 100)})" if seat.items_why
                    else "the launch claimed no board items")
        if item.id not in seat.items:
            shown = ", ".join(seat.items[:4]) + (", ..." if len(seat.items) > 4 else "")
            return f"the launch claimed {shown}, not {item.id}"
        if not seat.cwd:
            return "the launch records no working directory, so its repo cannot be read"
        theirs = self._git_dir(seat.cwd)
        if theirs is None:
            return f"the launch's working directory {_clip(seat.cwd, 100)} is not a git repo this reader can read"
        ours = self._git_dir(self.repo)
        if ours is None or theirs != ours:
            return f"the launch ran in another repo ({_clip(seat.cwd, 100)})"
        return None

    def author(self, sha: str, item: Any) -> Closer:
        """The three-tier verdict for the commit ``sha`` as the close of ``item``."""
        f = self.facts(sha)
        principal, sig_notes = self._signature(sha, f)
        if principal is not None:
            return Closer("signed", principal, "signed by an operator key", sha, committer=f.committer)
        notes = list(sig_notes)
        if len(f.seats) == 1:
            if self.res.seat_of is None:
                notes.append("a Board-Seat trailer is present but no launch records are consulted")
            else:
                try:
                    seat = self.res.seat_of(f.seats[0])
                except NotChecked as exc:
                    seat = None
                    notes.append(f"a Board-Seat trailer is present but the launch records were not checked ({_clip(str(exc))})")
                except Exception:  # noqa: BLE001
                    seat = None
                    notes.append("a Board-Seat trailer is present but the launch records cannot be read")
                else:
                    if seat is None:
                        notes.append("its Board-Seat trailer maps to no launch record")
                if seat is not None:
                    refused = self._seat_refusal(seat, item)
                    if refused is None:
                        return Closer("trailer", f"agent {seat.name}", "asserted (trailer)", sha, "; ".join(notes),
                                      committer=f.committer)
                    notes.append(f"its Board-Seat trailer maps to launch {seat.name}, but {refused}, so the trailer is not read")
        elif len(f.seats) > 1:
            notes.append("the commit carries several different Board-Seat trailers, so none is read")
        return Closer("git", f.author or "(no author name)", "asserted by git author", sha, "; ".join(notes),
                      email=f.email or None, committer=f.committer)


# ------------------------------------------------------------------------------------------------ where the close comes from
def _note_writer(repo: Path, close_sha: str, item_id: str) -> str | None:
    """The commit on ``refs/notes/board`` that wrote the ``Board-Closes: <item>`` line the note on ``close_sha`` carries
    now: of the commits that ADDED the line (its previous version did not carry it), the latest, as the board's close rule
    takes the latest close. None when the note at the tip does not carry it. The notes tree fans out as it grows, so every
    path form is tried."""
    cp = be._run(repo, "rev-parse", "--verify", "--quiet", f"{NOTES_REF}^{{commit}}")
    tip = cp.stdout.decode().strip() if cp.returncode == 0 else ""
    if not tip:
        return None
    paths = [close_sha, f"{close_sha[:2]}/{close_sha[2:]}", f"{close_sha[:2]}/{close_sha[2:4]}/{close_sha[4:]}"]
    log = be._git(repo, "log", "--reverse", "--no-renames", "--format=%H", tip, "--", *paths).split()

    def carries(commit: str) -> bool:
        for p in paths:
            got = be._run(repo, "cat-file", "blob", f"{commit}:{p}")
            if got.returncode != 0:
                continue
            text = got.stdout.decode("utf-8", "replace")
            if any(kind.lower() == "closes" and item_id in be._ids_of(value) for kind, value in be._NOTE_LINE.findall(text)):
                return True
        return False

    writer, had = None, False
    for commit in log:
        has = carries(commit)
        if has and not had:
            writer = commit
        had = has
    return writer if had else None


def _path_adder(repo: Path, key: str, main_ref: str) -> tuple[str | None, str]:
    """``(the first-parent commit on main that first added the path, "")`` or ``(None, the unattributed text)``. The path
    is :func:`levain.desk.board_evidence.done_path`'s; whether it is in the repo's work tree, and its name there, are git's
    answers from inside the path's own directory (nothing is resolved here)."""
    path = be.done_path(key, repo)
    probe = path if path.is_dir() else path.parent
    if not probe.is_dir():
        return None, "unattributed: the path cannot be found now"
    here = be._run(probe, "rev-parse", "--show-toplevel", "--show-prefix")
    if here.returncode != 0:
        return None, OUTSIDE
    lines = here.stdout.decode("utf-8", "replace").split("\n")
    top = be._git(repo, "rev-parse", "--show-toplevel").strip()
    if len(lines) < 2 or lines[0] != top:
        return None, OUTSIDE
    rel = lines[1] + ("" if path.is_dir() else path.name)
    rel = rel.rstrip("/")
    if not rel:
        return None, "unattributed: the path is the repo's whole work tree"
    out = be._git(repo, "log", "--first-parent", "--no-renames", "--diff-filter=A", "--format=%H", "--reverse", main_ref, "--",
                  f":(top,literal){rel}").split()
    return (out[0], "") if out else (None, NOT_ON_MAIN)


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
            return rd.author(state_ref, item)
        writer = _note_writer(Path(repo), state_ref, item.id)
        if writer is None:
            return Closer("unattributed", None, f"the close is a note on {state_ref[:9]} and the commit on {NOTES_REF} that "
                                                f"wrote it was not found", close_commit=state_ref)
        return dataclasses.replace(rd.author(writer, item), close_commit=state_ref)
    if kind in ("document", "run"):
        key = (item.done or "").strip()
        sha, why = _path_adder(Path(repo), key, main_ref)
        if sha is None:
            return Closer("unattributed", None, why)
        c = rd.author(sha, item)
        return dataclasses.replace(c, how=f"first added {_clip(key, 120)}, {c.how}")
    return Closer("unattributed", None, f"a {kind} close is not a git close")


# ------------------------------------------------------------------------------------------------ surfacing
def _git_closes(doc: dict[str, Any], items: Mapping[str, Any]) -> Iterable[tuple[dict[str, Any], Any]]:
    for s in doc.get("items", ()):
        if s.get("state") != "done":
            continue
        item = items.get(s.get("id"))
        if item is None or getattr(item, "done_kind", None) not in ("merged", "document", "run"):
            continue
        yield s, item


def _unread(exc: BaseException) -> Closer:
    text = str(exc) if isinstance(exc, be.GitFailed) else f"{type(exc).__name__}: {exc}"
    return Closer("unattributed", None, f"the author could not be read ({_clip(text, 160)})")


def mark_unread(doc: dict[str, Any], items: Mapping[str, Any], exc: BaseException) -> None:
    """Every git close of ``doc`` that :func:`annotate` left without a verdict says the author could not be read: the
    caller's guard when annotate itself fails, so attribution never fails a board read."""
    for s, _item in _git_closes(doc, items):
        s.setdefault("closed_by", _unread(exc).to_dict())


def annotate(doc: dict[str, Any], items: Mapping[str, Any], repos: Mapping[str | None, Path], res: Resolvers, *,
             default_repo: Path | None = None, main_ref: str = MAIN_REF) -> int:
    """Set ``closed_by`` (:meth:`Closer.to_dict`) on every ``done`` item of the board ``doc`` whose close is git's (a ``merged``,
    ``document`` or ``run`` item; a band row's close is the np row's, with no author to read here). ``repos`` maps an item's
    repo name to its path (``default_repo`` for any other: the single-repo board reads every item from one repo). One memo
    per repo, one git call for every closing commit's facts. Any failure on one item is said on that item
    (``unattributed``), never raised and never a refusal. Returns how many items it annotated."""
    readers: dict[str, _Reader] = {}
    todo: list[tuple[dict[str, Any], Any, Path]] = []
    for s, item in _git_closes(doc, items):
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
        with contextlib.suppress(Exception):    # a failed batch is read again per item, where its failure is said
            readers[key].prime(shas)
    n = 0
    for s, item, path in todo:
        refs = s.get("refs") or [None]
        try:
            c = closer_of_item(path, item, refs[0] if isinstance(refs[0], str) else None, res, main_ref=main_ref,
                               reader=readers[str(path)])
        except Exception as exc:  # noqa: BLE001 - one item's attribution never fails the board read
            c = _unread(exc)
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
    except Exception as exc:  # noqa: BLE001
        c = _unread(exc)
    return f"closed by: {describe(c)}"
