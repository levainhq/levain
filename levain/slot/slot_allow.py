"""Row 165: a slot's "don't ask again" reaches future slots only through the operator's own signed answer.

Claude Code writes the rule a slot's dialog saves into the slot clone's ``.claude/settings.local.json``, which the
session never reads back and which the retained tree keeps. This module is the operator-side half of carrying such a
rule forward without ever widening what the operator's own sessions do:

* :func:`parse_local_allow` reads that file's bytes (duplicate keys refused), keeps only the shapes the dialog
  writes and names the rest as dropped;
* :func:`read_regular` is the bounded read of a file a slot wrote (no symlink, a regular file, size-capped);
* :func:`file_rows` puts each kept rule on the band, one row per rule with a yes and a no, and keeps ONE pending
  record per rule (the row's question id): a rule with a record files nothing, whatever its row's answer;
* :func:`apply_answered` is the one consumer of those records, run first by every entry point: it walks them once,
  each from one read of its question by id, and appends a rule to ``slot-allow.json`` only for a row whose ``yes``
  carries the operator's own signature over exactly this rule's question and answer (:func:`rule_evidence`), storing
  that signature with it; a yes it cannot judge because the authority log cannot be read keeps its record for the
  next apply; any other answer drops the record, so "No: keep asking" and a refused yes are asked again at the next
  save;
* :func:`carried` re-verifies every stored rule's evidence against the authority log at each launch, so the file alone
  (an operator-uid file any session of his could write) carries nothing;
* :func:`merge_into_frozen` adds the verified rules to a slot's frozen ``permissions.allow`` and nothing else.

``slot-allow.json`` lives beside the question store in the operator's consent directory. Nothing here writes to the
operator's checkout, and the file is read by slot launches only.

Known edges:

* A revoke is a plain edit of an operator-owned file. A process running as the operator (or a backup restore) that
  holds a copy of a rule's signed entry from before the revoke can put it back, and it carries until its lease ends.
  A revoked list in the same file would be no stronger (the same process edits it); closing this needs a signed
  revoke in the authority log.
* The lock is held across the signature checks of an apply (the local ``ssh-keygen`` verify): a hung verifier
  delays other launches' writers until it returns.
* "No: keep asking" asks again the next time a slot saves the rule. A no to a row filed for a STORED rule (a renewal,
  or a stored rule that no longer carries) ends the rule instead: it leaves ``rules`` for ``revoked`` without its signed
  answer, as :func:`revoke` leaves it, and no launch asks about it again; it is asked again only the way any rule first
  is, when a later slot END saves it. A pending record written before records carried that mark reads as a first
  offer, so a no to it re-asks once more.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from levain import unshowable

FORMAT = "levain-slot-allow/1"
FILE_NAME = "slot-allow.json"
PENDING_NAME = "slot-allow-pending.json"
PENDING_FORMAT = "levain-slot-allow-pending/2"
_PENDING_FORMAT_KEYED = "levain-slot-allow-pending/1"
LOCK_NAME = ".slot-allow.lock"
AGENT = "claude_code"      # the band names a harness; the row is told apart by its key
KEY_PREFIX = "slot-allow:"
SETTINGS_CAP = 64 * 1024
FILE_CAP = 1024 * 1024
RULE_MAX = 300
RULES_PER_SESSION_MAX = 50
YES, NO = "yes", "no"
TERMINAL, DESK, PHONE = "terminal", "desk", "phone"
CHANNELS = (TERMINAL, DESK, PHONE)
_SIG_MAX = 16 * 1024
LEASE_DAYS = 30            # a rule is a lease from the signed answer's own time, as a graduation policy is
REMIND_DAYS = 3            # a renewal row is on the band from this many days before it lapses
SKEW = timedelta(minutes=5)  # how far ahead of this clock a signed time may run before it is refused

# The three kinds of approval the dialog saves as a rule. A bare tool name for Bash, a wildcard-only pattern and
# anything outside printable ASCII are not a shape this carries: a body of printable ASCII only reads on the band as
# the bytes it is (no lookalike letter from another script, no line break, no control character).
_BASH = re.compile(r"Bash\((?P<body>[\x20-\x27\x2a-\x7e]{1,%d})\)" % (RULE_MAX - 6))
_WEBFETCH = re.compile(r"WebFetch\(domain:[A-Za-z0-9][A-Za-z0-9.-]{0,252}\)")
_WILD_ONLY = re.compile(r"[\s*:]*")


def asked_text(rule: str) -> str:
    """The question the band shows for ``rule``: what a signature over its row must name."""
    return f"A slot session asked to always allow: {rule}"


def yes_label(rule: str) -> str:
    return f"Yes: allow {rule} in every future slot session"


def yes_text(rule: str) -> str:
    """The answer a signature over a yes to ``rule`` names: option (a), as the signed message writes it."""
    return f"(a) {yes_label(rule)}"


class AllowRefused(ValueError):
    """A file or document refused whole (not a dropped rule)."""


def shape_ok(rule: Any) -> bool:
    """Whether ``rule`` is a shape the dialog writes."""
    if type(rule) is not str or len(rule) > RULE_MAX:
        return False
    if unshowable.first_hidden(rule) is not None:       # a rule that renders other than its bytes is a lie on the band
        return False
    if rule == "WebSearch" or _WEBFETCH.fullmatch(rule):
        return True
    m = _BASH.fullmatch(rule)
    return m is not None and not _WILD_ONLY.fullmatch(m.group("body"))


def _no_dup_pairs(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise AllowRefused(f"duplicate key {k!r}")
        out[k] = v
    return out


def _loads(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dup_pairs)
    except AllowRefused:
        raise
    except (ValueError, UnicodeDecodeError, RecursionError) as e:
        raise AllowRefused(f"not JSON ({type(e).__name__})") from None


def read_regular(path: Path, cap: int = SETTINGS_CAP) -> Optional[bytes]:
    """The bytes of ``path`` when it is a regular file no larger than ``cap``; ``None`` when it is absent. A symlink,
    a non-regular file or one over the cap is refused (:class:`AllowRefused`), read through one descriptor opened
    without following a link."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise AllowRefused(f"{path}: {e.strerror or type(e).__name__}") from None
    if stat.S_ISLNK(st.st_mode):
        raise AllowRefused(f"{path}: a symlink")
    if not stat.S_ISREG(st.st_mode):
        raise AllowRefused(f"{path}: not a regular file")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError as e:
        raise AllowRefused(f"{path}: {e.strerror or type(e).__name__}") from None
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode) or (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
            raise AllowRefused(f"{path}: changed while opening")
        if fst.st_size > cap:
            raise AllowRefused(f"{path}: over {cap} bytes")
        data = os.read(fd, cap + 1)
    finally:
        os.close(fd)
    if len(data) > cap:
        raise AllowRefused(f"{path}: over {cap} bytes")
    return data


def parse_local_allow(raw: bytes) -> Tuple[List[str], List[str]]:
    """``(kept, dropped)`` from a slot's ``settings.local.json`` bytes: the ``permissions.allow`` rules whose shape
    the dialog writes (in file order, no duplicates, at most :data:`RULES_PER_SESSION_MAX`), and the rest named
    verbatim (cut to a short form). A document that is not an object, has a duplicate key, or holds an ``allow``
    that is not a list of strings is refused whole."""
    if len(raw) > SETTINGS_CAP:
        raise AllowRefused(f"over {SETTINGS_CAP} bytes")
    doc = _loads(raw)
    if not isinstance(doc, dict):
        raise AllowRefused("not an object")
    perms = doc.get("permissions")
    if perms is None:
        return [], []
    if not isinstance(perms, dict):
        raise AllowRefused("permissions is not an object")
    allow = perms.get("allow")
    if allow is None:
        return [], []
    if not isinstance(allow, list) or any(type(i) is not str for i in allow):
        raise AllowRefused("permissions.allow is not a list of strings")
    kept: List[str] = []
    dropped: List[str] = []
    for rule in allow:
        if rule in kept or rule in dropped:
            continue
        if shape_ok(rule) and len(kept) < RULES_PER_SESSION_MAX:
            kept.append(rule)
        else:
            dropped.append(rule[:80])
    return kept, dropped


# --- slot-allow.json ----------------------------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty() -> Dict[str, Any]:
    return {"format": FORMAT, "rules": [], "revoked": []}


def load(path: Path) -> Dict[str, Any]:
    """The store at ``path`` (an empty one when absent). A damaged file is refused (:class:`AllowRefused`), never
    read as empty: a launch that cannot read it says so."""
    raw = read_regular(path, FILE_CAP)
    if raw is None:
        return _empty()
    doc = _loads(raw)
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise AllowRefused(f"{path}: not a {FORMAT} document")
    for name in ("rules", "revoked"):
        items = doc.get(name)
        if not isinstance(items, list) or any(
                not isinstance(i, dict) or not shape_ok(i.get("rule")) for i in items):
            raise AllowRefused(f"{path}: {name} is malformed")
    return doc


_held = threading.local()


@contextmanager
def _locked(qdir: Path) -> Iterator[None]:
    """One writer at a time over ``slot-allow.json`` and the pending file in ``qdir``: an exclusive ``flock`` on a
    sibling lock file, held across a whole load-modify-save. Several slots launch and end at once, so two writers is
    a real path. The same thread re-entering holds the lock it has (a nested ``flock`` on a second descriptor would
    wait on itself), whatever spelling of the directory it names: the hold is keyed by the resolved path."""
    key = os.path.realpath(qdir)
    depth: Dict[str, int] = getattr(_held, "depth", None) or {}
    _held.depth = depth
    if depth.get(key):
        depth[key] += 1
        try:
            yield
        finally:
            depth[key] -= 1
        return
    fd = os.open(os.path.join(key, LOCK_NAME), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        depth[key] = 1
        try:
            yield
        finally:
            depth.pop(key, None)
    finally:
        os.close(fd)        # closing the only descriptor releases the lock


def _save(path: Path, doc: Mapping[str, Any]) -> None:
    data = (json.dumps(doc, indent=1, sort_keys=True) + "\n").encode()
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        fd = os.open(tmp, flags, 0o600)
    except FileExistsError:
        # a writer that died with this pid left it; the caller holds the lock, so no live writer owns it
        os.unlink(tmp)
        fd = os.open(tmp, flags, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def rules_of(path: Path) -> List[str]:
    return [r["rule"] for r in load(path)["rules"]]


EVIDENCE_KEYS = ("question_id", "channel", "signer_fp", "message", "sig", "answered_at")


def _signed_at(ev: Mapping[str, Any]) -> datetime:
    """The time the signed message names (``at``): inside the signed bytes, so it cannot be edited without breaking the
    signature, and the lease runs from it."""
    from levain.consent import terminal_sign as ts

    raw = ev["message"].encode("utf-8")
    return (ts.parse_desk_verified(raw) if ev["channel"] == DESK else ts.parse_verified(raw)).at


def lease_end(ev: Mapping[str, Any]) -> Optional[datetime]:
    """When the lease of the rule ``ev`` carries lapses, derived from the signed message; ``None`` when it cannot be."""
    try:
        return _signed_at(ev) + timedelta(days=LEASE_DAYS)
    except Exception:  # noqa: BLE001
        return None


def _signed_stamp(ev: Mapping[str, Any]) -> str:
    """The signed message's own time as a stamp, ``?`` when it cannot be read."""
    from levain.cockpit.consent import utc_stamp

    try:
        return utc_stamp(_signed_at(ev))
    except Exception:  # noqa: BLE001
        return "?"


def _lease_check(ev: Mapping[str, Any], now: datetime, *, recorded: bool = True) -> Tuple[Optional[str], str]:
    """``(None, "")`` when the lease of signature-verified ``ev`` holds at ``now``; ``("lapsed", why)`` when it ran
    out; ``("bad", why)`` when it cannot be a lease at all: the signed time is unreadable or more than :data:`SKEW`
    ahead of ``now`` (a lease runs from the signing, so a later time would lengthen it), or (``recorded``) the stored
    ``expires_at`` is not the end the signed time gives."""
    from levain.cockpit.consent import utc_stamp

    end = lease_end(ev)
    if end is None:
        return "bad", "its signed time cannot be read"
    if recorded and ev.get("expires_at") != utc_stamp(end):
        return "bad", "its lease is not the one the signed answer names"
    at = end - timedelta(days=LEASE_DAYS)
    if at > now + SKEW:
        return "bad", f"its signed time {utc_stamp(at)} is ahead of now: a lease runs from when it was signed"
    if now >= end:
        return "lapsed", f"its {LEASE_DAYS}-day lease lapsed {utc_stamp(end)}"
    return None, ""


def append(path: Path, rule: str, session: str, evidence: Mapping[str, Any], now: Callable[[], str] = _now,
           old_ok: Optional[Callable[[Mapping[str, Any]], bool]] = None) -> bool:
    """Add ``rule`` with where it came from and the signed answer that carries it (``evidence``: the fields of
    :data:`EVIDENCE_KEYS`), recording the lease's end and when it was applied (``applied_at``). A rule already there
    is renewed (its entry replaced, the lease reset) unless its old entry is still a good answer (``old_ok``: its
    signature verifies) whose lease runs at least as long, which returns False. An old entry ``old_ok`` does not pass,
    or any old entry when none is given, counts as absent: an unverified lease never blocks a signed one. Storing
    evidence is no proof of it: every launch verifies it again (:func:`carried`)."""
    from levain.cockpit.consent import utc_stamp

    if not shape_ok(rule):
        raise AllowRefused("not a dialog rule shape")
    if not isinstance(evidence, Mapping) or any(type(evidence.get(k)) is not str for k in EVIDENCE_KEYS):
        raise AllowRefused("a rule is stored with the signed answer that carries it")
    end = lease_end(evidence)
    if end is None:
        raise AllowRefused("the signed answer's time cannot be read")
    with _locked(path.parent):
        doc = load(path)
        entry = {"rule": rule, "session": session, "applied_at": now(), "expires_at": utc_stamp(end),
                 **{k: evidence[k] for k in EVIDENCE_KEYS}}
        old = [r for r in doc["rules"] if r["rule"] == rule]
        if old:
            prev = lease_end(old[0]) if old_ok is not None and old_ok(old[0]) else None
            if prev is not None and prev >= end:
                return False
            doc["rules"] = [r for r in doc["rules"] if r["rule"] != rule]
        doc["rules"].append(entry)
        _save(path, doc)
    return True


def revoke(path: Path, rule: str, *, confirm: bool, now: Callable[[], str] = _now) -> str:
    """Take ``rule`` out of the store. Its text stays under ``revoked`` and its signed answer is dropped, so nothing
    left in the file can carry it again: putting the entry back by hand carries nothing, and :func:`undo` asks for a
    new signed yes. Without ``confirm`` nothing is written and the answer says what would go."""
    with _locked(path.parent):
        doc = load(path)
        hit = [r for r in doc["rules"] if r["rule"] == rule]
        if not hit:
            raise AllowRefused(f"{rule!r} is not in {path.name}")
        if not confirm:
            return (f"would revoke {rule!r} (signed {_signed_stamp(hit[0])} from session {hit[0].get('session', '?')});"
                    " re-run with --yes")
        _retire(doc, rule, hit[0], now())
        _save(path, doc)
    return f"revoked {rule!r}: slot sessions stop carrying it; `levain slot-allow undo` re-offers it on the band"


DECLINED = "renewal declined"


def _retire(doc: Dict[str, Any], rule: str, entry: Mapping[str, Any], when: str, reason: Optional[str] = None) -> None:
    """Move ``rule`` from ``rules`` to ``revoked`` in ``doc``, keeping its text and session and dropping its signed
    answer, so nothing left in the file carries it."""
    doc["rules"] = [r for r in doc["rules"] if r["rule"] != rule]
    gone: Dict[str, Any] = {"rule": rule, "session": str(entry.get("session", "?")), "revoked_at": when}
    if reason is not None:
        gone["reason"] = reason
    doc["revoked"] = [r for r in doc["revoked"] if r["rule"] != rule] + [gone]


def _decline(path: Path, rule: str, now: Callable[[], str]) -> bool:
    """A no to ``rule``'s renewal row: the rule leaves the store as a revoke takes it out (reason :data:`DECLINED`).
    False when it is no longer stored."""
    with _locked(path.parent):
        doc = load(path)
        hit = [r for r in doc["rules"] if r["rule"] == rule]
        if not hit:
            return False
        _retire(doc, rule, hit[0], now(), DECLINED)
        _save(path, doc)
    return True


def undo(path: Path, rule: str, store: Any = None, judge: Optional[Judge] = None) -> str:
    """Undo a revoke of ``rule``. When :func:`carried` carries the rule now (a later signed yes, verified against the
    authority log), only the revoke record is cleared. Otherwise, after answered rows are applied
    (:func:`apply_answered`), a row for it goes on the band unless one is still open (never the one answered before),
    whatever entry for it the file holds: the rule carries again only on a new signed yes to that row, as every slot
    rule does. When the file holds an entry for it (an applied yes restored it), the row is a renewal: a no to it ends
    the rule. Restores nothing itself."""
    from levain.consent.questions import QuestionStore

    qdir = path.parent
    qs = store if store is not None else QuestionStore(qdir)
    with _locked(qdir):
        hit = [r for r in load(path)["revoked"] if r["rule"] == rule]
        if not hit:
            raise AllowRefused(f"{rule!r} has no revoke to undo")
        apply_answered(qdir, path, store=qs, judge=judge)
        doc = load(path)
        now_carried = rule in carried(path, judge, store_id_of(qs))[0]
        if not now_carried:
            stored = any(r["rule"] == rule for r in doc["rules"])
            file_rows(qdir, str(hit[-1].get("session", "?")), [rule], store=qs,
                      why={rule: "You revoked it, then undid the revoke"}, renew=[rule] if stored else [])
        doc["revoked"] = [r for r in doc["revoked"] if r["rule"] != rule]
        _save(path, doc)
    if now_carried:
        return f"{rule!r} is carried already (a later signed yes); the revoke record is cleared"
    return f"{rule!r} re-offered on the band: answer yes to carry it again"


# --- the signed yes -----------------------------------------------------------------------------------------------

def _no_live_check(public_key: str) -> Tuple[Optional[bool], str]:
    """A re-read of stored evidence asks the authority log who the key is, not the Secure Enclave: that check is made
    once, when the answer is applied."""
    return None, "not re-checked on a re-read"


def verify_evidence(rule: str, ev: Mapping[str, Any], *, authority: Any, store_id: Optional[str],
                    ctk: Optional[Callable[[str], Tuple[Optional[bool], str]]] = None,
                    now: Optional[datetime] = None, lease: bool = False) -> Optional[str]:
    """Why ``ev`` is not the operator's signature over a yes to exactly ``rule`` in this store, or ``None`` when it is.
    Both the question and the answer the signed message must name are rebuilt from ``rule`` (:func:`asked_text`,
    :func:`yes_text`), never read from the row; the key must be active in ``authority`` (a verified replay of the
    authority log) and of the class the channel needs: a laptop-presence key for a terminal or desk answer, a key whose
    class covers signing for a phone countersign. Nothing here reads the question store: the signed bytes are the whole
    evidence. Never raises."""
    try:
        why = _verify_evidence(rule, ev, authority, store_id, ctk)
        if why is None and lease:
            kind, why_lease = _lease_check(ev, now or datetime.now(timezone.utc))
            if kind == "lapsed":
                return f"{why_lease}: answer its renewal row on the band"
            if kind is not None:
                return why_lease
        return why
    except Exception as exc:  # noqa: BLE001 - evidence that cannot be checked does not carry
        return f"its signature could not be checked ({type(exc).__name__})"


def _verify_evidence(rule: str, ev: Any, authority: Any, store_id: Optional[str],
                     ctk: Optional[Callable[[str], Tuple[Optional[bool], str]]]) -> Optional[str]:
    from levain.authority import LAPTOP, VerifierUnavailable, custody_mismatch
    from levain.authority_sig import _signers_line, verify_link_sig
    from levain.consent import terminal_sign as ts
    from levain.consent.queue import class_covers
    from levain.contracts.namespaces import ANSWER_COUNTERSIGN_NAMESPACE
    from levain.desk.review import SIGN_TIER

    if not shape_ok(rule):
        return "not a dialog rule shape"
    if not isinstance(ev, Mapping) or any(type(ev.get(k)) is not str for k in EVIDENCE_KEYS):
        return "no signed answer is stored with it"
    channel, message, sig, fp, qid = ev["channel"], ev["message"], ev["sig"], ev["signer_fp"], ev["question_id"]
    if channel not in CHANNELS or len(sig) > _SIG_MAX or len(message) > ts.MAX_BYTES:
        return "the stored signed answer is not in a form this reads"
    if store_id is None:
        return "this store's id cannot be read"
    raw = message.encode("utf-8")
    try:
        if channel == DESK:
            parsed: Any = ts.parse_desk_verified(raw)
            namespace, countersign_ok = ts.DESK_NAMESPACE, parsed.countersign_of is None
        else:
            parsed = ts.parse_verified(raw)
            if channel == TERMINAL:
                namespace, countersign_ok = ts.NAMESPACE, parsed.countersign_of is None
            else:
                namespace, countersign_ok = ANSWER_COUNTERSIGN_NAMESPACE, parsed.countersign_of == ev["answered_at"]
    except ts.MessageError:
        return "the stored message is not a signed answer"
    if not countersign_ok:
        return f"the stored message is not a {channel} answer"
    if parsed.store != f"levain-questions:{store_id}" or parsed.install != store_id:
        return "it was signed for another store"
    entries = [e for e in parsed.entries if e.item == qid]
    if len(entries) != 1:
        return "the signed answer names no such question, or names it twice"
    e = entries[0]
    if e.asked != asked_text(rule) or e.text is not None or e.answer != yes_text(rule):
        return "the signature is over another question or another answer than a yes to this rule"
    key = authority.key(fp) if hasattr(authority, "key") else None
    if key is None or not key.active:
        return "the signing key is not an active key in the authority log"
    if channel == PHONE:
        if not class_covers(key.key_class, SIGN_TIER):
            return f"key class {key.key_class} cannot countersign here"
    else:
        if key.key_class != LAPTOP:
            return f"key class {key.key_class} cannot sign a slot rule: it needs {LAPTOP}"
        problem = custody_mismatch(key.public_key, key.custody)
        if problem is not None:
            return problem
    try:
        if channel == PHONE:
            good = verify_link_sig(raw, sig, public_key=key.public_key, fingerprint=key.fingerprint,
                                   namespace=namespace)
            got_fp = key.fingerprint if good else None
            problem = None if good else "the signature does not verify"
        else:
            principal = "authority-signer"
            res = ts.verify(raw, sig, _signers_line(principal, namespace, key.public_key), principal,
                            ctk=ctk, namespace=namespace)
            got_fp, problem = (res.fingerprint, None) if res.ok else (None, res.problem or "the signature does not verify")
    except (VerifierUnavailable, ts.KeyError_) as exc:
        return f"the signature could not be verified ({exc})"
    if problem is not None:
        return problem
    if got_fp != key.fingerprint or key.fingerprint != fp:
        return "it is signed by another key than the one it names"
    return None


@dataclass(frozen=True)
class Judge:
    """What a yes is judged by: ``authority`` (a call returning a verified replay of the authority log), the signing
    queue a phone countersign card lives in, and the live Secure Enclave check (``None`` = the real one)."""
    authority: Callable[[], Any]
    sign_queue: Optional[Path] = None
    ctk: Optional[Callable[[str], Tuple[Optional[bool], str]]] = None
    #: what "now" is when a lease is judged (``None`` = the wall clock)
    clock: Optional[Callable[[], datetime]] = None


def _clock(judge: Optional[Judge]) -> datetime:
    return judge.clock() if judge is not None and judge.clock is not None else datetime.now(timezone.utc)


def default_authority(qdir: Path) -> Any:
    """The cockpit's authority log, replayed (the cockpit the consent hooks in ``qdir`` name). Raises when none is
    named or the log does not replay."""
    from levain.authority import anchor_path
    from levain.consent.queue import authority_source
    from levain.consent.sign import sign_root

    root = sign_root(Path(qdir))
    return authority_source(root / "cockpit.db", anchor=anchor_path(root))()


def default_judge(qdir: Path) -> Judge:
    from levain.consent.answer_countersign import sign_queue_dir

    return Judge(authority=lambda: default_authority(qdir), sign_queue=sign_queue_dir(os.environ))


def store_id_of(store: Any) -> Optional[str]:
    """The question store's id (what a signed answer names as its store), ``None`` when it cannot be read."""
    from levain.band_view import _store_id_of

    return _store_id_of(store)


class _LogUnreadable(Exception):
    """The authority log could not be read: no verdict on the answer either way."""


def rule_evidence(store: Any, question: Any, rule: str, judge: Judge) -> Tuple[Optional[Dict[str, str]], str]:
    """``(evidence, "")`` when the answer to ``question`` is the operator's signed yes to exactly ``rule``, else
    ``(None, why)``. Direct: the band answer's own signature (a terminal or a desk click), fresh against the recorded
    answer time and verified live. Remote: the phone's countersign of the answer (``answer_countersign``), which must
    read as countersigned. The lease is judged too (:func:`_lease_check`, at the judge's clock): an answer signed
    ahead of now, or one whose lease is already over, is refused here and so never reported added. The same checks
    run again at every launch (:func:`carried`)."""
    try:
        return _rule_evidence(store, question, rule, judge)
    except _LogUnreadable as exc:
        return None, str(exc)


def _rule_evidence(store: Any, question: Any, rule: str, judge: Judge) -> Tuple[Optional[Dict[str, str]], str]:
    """:func:`rule_evidence`, raising :class:`_LogUnreadable` when the authority log cannot be read (or yields
    nothing) instead of refusing: a log read failure is not a verdict on the answer."""
    from levain.band_view import answer_sign_of
    from levain.cockpit.consent import parse_stamp
    from levain.consent import answer_countersign as ac
    from levain.consent import terminal_sign as ts

    a = question.answer
    if a is None or a.kind != "option" or a.value != YES:
        return None, "not a yes"
    sign = answer_sign_of(store, question.id)
    ev: Dict[str, str]
    if sign and sign.get("sign_state") == "signed" and sign.get("message") and sign.get("sig") and sign.get("signer_fp"):
        message = sign["message"]
        channel = DESK if message.startswith(ts.DESK_FORMAT + "\n") else TERMINAL
        ev = {"question_id": question.id, "channel": channel, "signer_fp": sign["signer_fp"], "message": message,
              "sig": sign["sig"], "answered_at": a.answered_at}
        try:
            parsed = (ts.parse_desk_verified if channel == DESK else ts.parse_verified)(message.encode("utf-8"))
            if not ts.fresh(parsed, parse_stamp(a.answered_at)):       # fresh reads only the signed time
                return None, "the signature is not fresh against the answer"
        except Exception as exc:  # noqa: BLE001
            return None, f"the signed answer cannot be read ({type(exc).__name__})"
    elif sign and sign.get("sign_state") == "unsigned" and sign.get("sign_reason") == "remote":
        got = ac.countersign_evidence(store, question.id, sign_queue=judge.sign_queue)
        if got is None:
            return None, "answered at a remote terminal and not countersigned on your phone"
        ev = {"question_id": question.id, "channel": PHONE, "signer_fp": got[2], "message": got[0], "sig": got[1],
              "answered_at": a.answered_at}
    else:
        return None, "answered unsigned"
    try:
        snapshot = judge.authority()
    except Exception as exc:  # noqa: BLE001
        raise _LogUnreadable(f"the authority log cannot be read ({type(exc).__name__})") from None
    if snapshot is None:
        raise _LogUnreadable("the authority log cannot be read (no replay)")
    why = verify_evidence(rule, ev, authority=snapshot, store_id=store_id_of(store), ctk=judge.ctk)
    if why:
        return None, why
    kind, why = _lease_check(ev, _clock(judge), recorded=False)     # a lease already over is never stored
    return (None, why) if kind is not None else (ev, "")


def carried(path: Path, judge: Optional[Judge], store_id: Optional[str],
            now: Optional[datetime] = None) -> Tuple[List[str], List[Tuple[str, str]]]:
    """``(rules, not_carried)`` from ``path``: a stored rule is carried only when its evidence verifies now, against the
    authority log as it stands (a swapped rule, an edited message, a revoked key and a rule with no evidence all fail),
    AND its lease holds at ``now`` (default: the judge's clock): a lease that lapsed :data:`LEASE_DAYS` days after the
    signed time, a signed time ahead of now, or a stored end that is not the signed one, each fail. ``not_carried``
    pairs each failing rule with why. A judge that cannot be had carries nothing."""
    doc = load(path)
    rules = doc["rules"]
    if not rules:
        return [], []
    try:
        snapshot = judge.authority() if judge is not None else None
    except Exception as exc:  # noqa: BLE001
        why = f"the authority log cannot be read ({type(exc).__name__})"
        return [], [(r["rule"], why) for r in rules]
    if snapshot is None:
        return [], [(r["rule"], "no authority to check its signature against") for r in rules]
    now = now or _clock(judge)
    ok: List[str] = []
    bad: List[Tuple[str, str]] = []
    for r in rules:
        why = verify_evidence(r["rule"], r, authority=snapshot, store_id=store_id, ctk=_no_live_check, now=now, lease=True)
        if why is None:
            ok.append(r["rule"])
        else:
            bad.append((r["rule"], why))
    return ok, bad


# --- the band rows ------------------------------------------------------------------------------------------------
#
# One pending record per rule: {rule, session, question_id, key, renew?, why?}. Its lifecycle has ONE consumer:
# :func:`apply_answered` is the only step that deletes or replaces a record, and every entry point (a slot END's offer,
# a launch's apply and reminder, undo) runs it before anything files, under the one lock. The filer only asks whether a
# record exists: it never judges an answer, so no answer can be lost to a reader that is not its consumer.
#
# A record is resolved by reading its question by id, never by a window of recent questions, and the question must
# still be the one filed for the rule (its key and its words). Each row is filed under a key no other row has. Filing
# writes the record first (key set, question id null), then the row, then the question id: a crash between them leaves
# a record whose row the next filing step finds by its key and adopts (:func:`_file_row`), or files when it never
# landed.

def _key(rule: str) -> str:
    return f"{KEY_PREFIX}{hashlib.sha256(rule.encode()).hexdigest()[:16]}:{os.urandom(8).hex()}"


def _pending_doc(qdir: Path) -> Dict[str, Any]:
    raw = read_regular(qdir / PENDING_NAME, FILE_CAP)
    if raw is None:
        return {"rows": {}, "refused": {}}
    doc = _loads(raw)
    if isinstance(doc, dict) and doc.get("format") == _PENDING_FORMAT_KEYED:
        return {"rows": {}, "refused": {}}       # records the band's keys held: none resolves; the next save re-asks
    if not isinstance(doc, dict) or doc.get("format") != PENDING_FORMAT:
        raise AllowRefused(f"{PENDING_NAME}: malformed")
    rows, refused = doc.get("rows"), doc.get("refused")
    if not isinstance(rows, dict) or not isinstance(refused, dict):
        raise AllowRefused(f"{PENDING_NAME}: malformed")
    for rule, rec in rows.items():
        if (not shape_ok(rule) or not isinstance(rec, dict) or rec.get("rule") != rule
                or any(type(rec.get(k)) is not str for k in ("session", "key"))
                or not (rec.get("question_id") is None or type(rec.get("question_id")) is str)
                or not (rec.get("why") is None or type(rec.get("why")) is str)
                or type(rec.get("renew", False)) is not bool):
            raise AllowRefused(f"{PENDING_NAME}: malformed")
    for rule, why in refused.items():
        if not shape_ok(rule) or type(why) is not str:
            raise AllowRefused(f"{PENDING_NAME}: malformed")
    return {"rows": rows, "refused": refused}


def _pending(qdir: Path) -> Dict[str, Any]:
    """The pending records, by rule."""
    return _pending_doc(qdir)["rows"]


def _save_pending(qdir: Path, doc: Mapping[str, Any]) -> None:
    _save(qdir / PENDING_NAME, {"format": PENDING_FORMAT, "rows": dict(doc["rows"]),
                                "refused": dict(doc["refused"])})


def _question_of(qs: Any, rec: Mapping[str, Any]) -> Any:
    """The question ``rec`` was filed as, read once by its id; ``None`` when it is gone, is not that rule's row, or
    has no row yet (its filing was cut short)."""
    if rec.get("question_id") is None:
        return None
    q = qs.get(rec["question_id"])
    if q is None or q.agent != AGENT or q.agent_session_id != rec["key"] or q.what != asked_text(rec["rule"]):
        return None
    return q


def _row_text(rule: str, session: str, said: Optional[str], ends: bool) -> Tuple[List[Tuple[str, str]], str]:
    """The options and words of ``rule``'s row: rebuilt from its record, so a row whose filing was cut short is filed
    again as it would have been."""
    no_does = ("No ends the rule: slots stop carrying it until one saves it again" if ends else
               "No asks again the next time a slot saves it")
    if said is None:
        blocks = (f"nothing: the slot already ran. Yes adds the rule to {FILE_NAME} (slot sessions only; never "
                  f"your checkout or your own sessions), for {LEASE_DAYS} days; {no_does}. Session {session}.")
    else:
        blocks = (f"nothing changes today. {said}. Yes carries it in slot sessions (never your checkout "
                  f"or your own sessions) for {LEASE_DAYS} days from now; {no_does}.")
    return [(YES, yes_label(rule)), (NO, "No: stop carrying it" if ends else "No: keep asking")], blocks


def _file_row(qs: Any, qdir: Path, rec: Mapping[str, Any]) -> Tuple[Any, bool]:
    """``rec``'s row, filed under its key unless the store already holds a row under that key, open or answered by a
    person (then that row is adopted): ``(question, filed)``."""
    from levain.consent.questions import ANSWER_CHANNELS

    options, blocks = _row_text(rec["rule"], rec["session"], rec.get("why"), rec.get("renew") is True)
    return qs.file_once(agent=AGENT, key=rec["key"], what=asked_text(rec["rule"]), options=options, rec=NO,
                        blocks=blocks, cwd=str(qdir), held_by=ANSWER_CHANNELS)


def file_rows(qdir: Path, session: str, rules: Sequence[str], store: Any = None,
              why: Optional[Mapping[str, str]] = None, renew: Sequence[str] = ()) -> List[str]:
    """One band row per rule (a yes and a no), listing the rule verbatim, for each rule with NO pending record: a rule
    that has one files nothing, whatever its row's answer (only :func:`apply_answered` resolves or drops a record, so
    the entry points run it first). ``why`` (rule -> words) says on the row why it is asked again (a lapsed lease, an
    answer that no longer carries, an undone revoke); a rule without one is asked for the first time. A rule in
    ``renew`` is one the store holds: a no to its row ends it (:func:`apply_answered`), and its No option says so.
    First, any record whose filing was cut short gets its row (:func:`_file_row`). Returns the rules newly filed.
    Never writes the allow file."""
    from levain.consent.questions import QuestionStore

    qs = store or QuestionStore(qdir)
    filed: List[str] = []
    with _locked(qdir):
        doc = _pending_doc(qdir)
        for rule, rec in sorted(doc["rows"].items()):
            if rec.get("question_id") is None:
                q, new = _file_row(qs, qdir, rec)
                rec["question_id"] = q.id
                _save_pending(qdir, doc)
                if new and rule in rules:
                    filed.append(rule)
        for rule in rules:
            if not shape_ok(rule) or rule in filed or rule in doc["rows"]:
                continue
            said = (why or {}).get(rule)
            rec = {"rule": rule, "session": session, "question_id": None, "key": _key(rule)}
            if rule in renew:
                rec["renew"] = True
            if said is not None:
                rec["why"] = said[:200]
            doc["rows"][rule] = rec
            doc["refused"].pop(rule, None)
            _save_pending(qdir, doc)                   # the intent is on disk before the row exists
            q, _new = _file_row(qs, qdir, rec)
            rec["question_id"] = q.id
            _save_pending(qdir, doc)
            filed.append(rule)
    return filed


@dataclass(frozen=True)
class _Standing:
    """Where a stored rule stands: ``carried`` (its signature verifies and its lease holds), ``lapsed`` (it verifies
    and its lease ran out: ``end``), or ``bad`` (it does not verify, or its lease cannot be one: ``why``)."""
    kind: str
    why: str = ""
    end: str = ""
    end_at: Optional[datetime] = None


def _standing(doc: Mapping[str, Any], judge: Optional[Judge], store_id: Optional[str],
              now: Optional[datetime] = None) -> Optional[Dict[str, _Standing]]:
    """Each stored rule's :class:`_Standing`, judged as :func:`carried` judges it; ``None`` when nothing can be judged
    (no judge, no store id, an authority log that cannot be read): then no stored rule is told apart from another."""
    from levain.cockpit.consent import utc_stamp

    if judge is None or store_id is None:
        return None
    try:
        snapshot = judge.authority()
    except Exception:  # noqa: BLE001
        return None
    if snapshot is None:
        return None
    now = now or _clock(judge)
    out: Dict[str, _Standing] = {}
    for r in doc["rules"]:
        why = verify_evidence(r["rule"], r, authority=snapshot, store_id=store_id, ctk=_no_live_check)
        kind = "bad"
        if why is None:
            got, why = _lease_check(r, now)
            kind = "carried" if got is None else got
        end = lease_end(r) if kind != "bad" else None
        out[r["rule"]] = _Standing(kind, why, utc_stamp(end) if end is not None else "", end)
    return out


def _renew_why(end: str) -> str:
    return f"The rule's {LEASE_DAYS}-day lease ends {end}: yes renews it; without it the rule lapses and slots ask again"


def _bad_why(why: str) -> str:
    return f"This rule is not carried: {why[:160]}"


def offer(qdir: Path, allow_file: Path, session: str, rules: Sequence[str], judge: Optional[Judge],
          store: Any = None, now: Optional[datetime] = None,
          refused: Optional[List[Tuple[str, str]]] = None) -> List[str]:
    """At a slot's END: first every answered row is applied (:func:`apply_answered`), then each rule the dialog saved
    goes on the band unless the store carries it now (:func:`file_rows` files a row only for a rule with no pending
    record). A stored rule whose verified lease ran out is asked as a renewal; one whose signed answer no longer
    carries it (a re-enrolled or revoked key, an edited entry) says why. With nothing to judge by (:func:`_standing`
    is ``None``) a stored rule files nothing. A yes the apply refuses is appended to ``refused`` when given. Returns
    the rules newly filed."""
    from levain.consent.questions import QuestionStore

    qs = store or QuestionStore(qdir)
    with _locked(qdir):
        apply_answered(qdir, allow_file, store=qs, judge=judge, refused=refused)
        doc = load(allow_file)
        stored = {r["rule"] for r in doc["rules"]}
        standing = _standing(doc, judge, store_id_of(qs), now) or {}
        why: Dict[str, str] = {}
        todo: List[str] = []
        for rule in rules:
            st = standing.get(rule)
            if rule not in stored:
                todo.append(rule)
            elif st is None or st.kind == "carried":
                continue
            else:
                why[rule] = _renew_why(st.end) if st.kind == "lapsed" else _bad_why(st.why)
                todo.append(rule)
        return file_rows(qdir, session, todo, store=qs, why=why, renew=[r for r in todo if r in stored])


def remind(qdir: Path, allow_file: Path, store: Any = None, now: Optional[datetime] = None,
           judge: Optional[Judge] = None) -> List[str]:
    """Run at each slot launch, after its apply: a renewal row for each stored rule whose VERIFIED lease ends within
    :data:`REMIND_DAYS` (or has ended), and a row for each stored rule whose signed answer does not verify (its stored
    lease is not trusted, so it never silences this). Answered rows are applied first (:func:`apply_answered`); a row
    is filed only when the rule has no pending record (:func:`file_rows`); its signed yes renews the rule. A rule whose
    yes was refused is not asked here: it is asked again when a slot END saves it (:func:`offer`). With nothing to
    judge by (no judge, an unreadable authority log) it files no new row. Returns the rules filed."""
    from levain.consent.questions import QuestionStore

    qs = store or QuestionStore(qdir)
    with _locked(qdir):
        apply_answered(qdir, allow_file, store=qs, judge=judge)
        doc = load(allow_file)
        standing = _standing(doc, judge, store_id_of(qs), now) or {}
        refused = _pending_doc(qdir)["refused"]
        soon = (now or _clock(judge)) + timedelta(days=REMIND_DAYS)
        sessions = {r["rule"]: str(r.get("session", "?")) for r in doc["rules"]}
        why: Dict[str, str] = {}
        for rule, st in standing.items():
            if rule in refused:
                continue
            if st.kind == "bad":
                why[rule] = _bad_why(st.why)
            elif st.end_at is not None and st.end_at <= soon:
                why[rule] = _renew_why(st.end)
        filed: List[str] = []
        for rule in why:
            filed += file_rows(qdir, sessions[rule], [rule], store=qs, why={rule: why[rule]}, renew=[rule])
        if not why:
            file_rows(qdir, "?", [], store=qs)     # a row whose filing was cut short is still filed
        return filed


def _resolve(qs: Any, rec: Mapping[str, Any], judge: Optional[Judge]) -> Tuple[str, Optional[Dict[str, str]], str]:
    """Where one pending record stands, from ONE read of its question: ``("open", None, "")`` while unanswered;
    ``("yes", evidence, "")`` for the operator's signed yes to exactly this rule; ``("unreadable", None, why)`` for a
    yes the authority log could not be read to judge (the record stays, as an open one does); ``("refused", None,
    why)`` for any other yes; ``("no", None, why)`` for a no; ``("drop", None, why)`` for anything else (words, a row
    that is gone or is not this rule's)."""
    from levain.consent.questions import ANSWER_CHANNELS

    if rec.get("question_id") is None:
        return "open", None, ""                # its filing was cut short: the filer finishes it, nothing to read yet
    q = _question_of(qs, rec)
    if q is None:
        return "drop", None, "its row is gone"
    a = q.answer
    if a is None:
        return "open", None, ""
    if a.kind == "option" and a.value == NO:
        return "no", None, "answered no"
    if a.kind != "option" or a.value != YES:
        return "drop", None, "not answered yes"
    if a.channel not in ANSWER_CHANNELS:
        return "refused", None, f"answered on {a.channel!r}, not a person's channel"
    if judge is None:
        return "refused", None, "no authority named to check its signature against"
    try:
        ev, why = _rule_evidence(qs, q, rec["rule"], judge)
    except _LogUnreadable as exc:
        return "unreadable", None, str(exc)
    return ("yes", ev, "") if ev is not None else ("refused", None, why)


def apply_answered(qdir: Path, allow_file: Path, store: Any = None, now: Callable[[], str] = _now,
                   judge: Optional[Judge] = None, refused: Optional[List[Tuple[str, str]]] = None) -> List[str]:
    """The ONE consumer of the pending records: the only step that deletes or replaces one. Walks them once, each
    resolved from one read of its question (:func:`_resolve`): the operator's signed yes to exactly the rule
    (:func:`rule_evidence`) carries it into ``allow_file`` with that answer stored beside it; a record still open (or
    still being filed, or a yes the authority log could not be read to judge) is left; anything else (a no, an unsigned or otherwise refused yes, a row that is gone) drops
    the record, so the next time a slot saves the rule it is asked again. A no to a row filed for a stored rule
    (``renew``) also ends that rule (:func:`_decline`). A refused yes is kept as refused (with why) until a slot END
    files the rule's next row: ``list`` names it and :func:`remind` does not re-ask it. It is appended to ``refused``
    when given. A rule already stored is replaced
    unless its old entry still verifies with a lease at least as long (:func:`append`). Returns the rules added."""
    from levain.consent.questions import QuestionStore

    qs = store if store is not None else QuestionStore(qdir)
    added: List[str] = []
    with _locked(qdir):
        doc = _pending_doc(qdir)
        if not doc["rows"]:
            return []
        old_ok: Optional[Callable[[Mapping[str, Any]], bool]] = None
        judged_old = False
        changed = False
        for rule, rec in sorted(doc["rows"].items()):
            state, ev, why = _resolve(qs, rec, judge)
            if state in ("open", "unreadable"):
                continue
            del doc["rows"][rule]
            changed = True
            if state == "yes" and ev is not None:
                if not judged_old:
                    old_ok, judged_old = _old_ok(judge, store_id_of(qs)), True
                doc["refused"].pop(rule, None)
                if append(allow_file, rule, str(rec.get("session", "?")), ev, now, old_ok=old_ok):
                    added.append(rule)
            elif state == "refused":
                doc["refused"][rule] = why
                if refused is not None:
                    refused.append((rule, why))
            else:
                doc["refused"].pop(rule, None)
                if state == "no" and rec.get("renew") is True:
                    _decline(allow_file, rule, now)
        if changed:
            _save_pending(qdir, doc)
    return added


def _old_ok(judge: Optional[Judge], store_id: Optional[str]) -> Optional[Callable[[Mapping[str, Any]], bool]]:
    """Whether a stored entry is still a good answer (its signature verifies and its stored lease is the signed one),
    judged against one replay of the authority log; ``None`` when nothing can be judged, so every old entry counts
    as absent."""
    if judge is None or store_id is None:
        return None
    try:
        snapshot = judge.authority()
    except Exception:  # noqa: BLE001
        return None
    if snapshot is None:
        return None
    now = _clock(judge)

    def ok(r: Mapping[str, Any]) -> bool:
        if verify_evidence(str(r.get("rule")), r, authority=snapshot, store_id=store_id, ctk=_no_live_check) is not None:
            return False
        return _lease_check(r, now)[0] in (None, "lapsed")
    return ok


def not_carried(qdir: Path, store: Any = None, judge: Optional[Judge] = None) -> List[Tuple[str, str]]:
    """The rules a person answered ``yes`` that are not carried, each with why: unsigned, signed over other words, a
    key no longer active, a remote answer nobody countersigned. A yes not applied yet is judged as the next launch will
    judge it; a yes a launch already refused is named until the rule's next row is filed."""
    from levain.consent.questions import QuestionStore

    qs = store if store is not None else QuestionStore(qdir)
    doc = _pending_doc(qdir)
    out: List[Tuple[str, str]] = []
    for rule, rec in sorted(doc["rows"].items()):
        state, _ev, why = _resolve(qs, rec, judge)
        if state == "refused":
            out.append((rule, why))
        elif state == "unreadable":
            out.append((rule, f"{why}; the answer is kept for the next launch"))
    out += [(rule, why) for rule, why in sorted(doc["refused"].items()) if rule not in doc["rows"]]
    return out


# --- the launch ---------------------------------------------------------------------------------------------------

def merge_into_frozen(settings: Mapping[str, Any], rules: Sequence[str]) -> Dict[str, Any]:
    """``settings`` (a frozen repo layer) with ``rules`` appended to ``permissions.allow``: no other key is touched."""
    out = dict(settings)
    if not rules:
        return out
    perms = {k: list(v) for k, v in dict(out.get("permissions", {})).items()}
    have = perms.setdefault("allow", [])
    have.extend(r for r in rules if r not in have)
    out["permissions"] = perms
    return out


# --- the verbs ----------------------------------------------------------------------------------------------------

def register(subparsers: Any) -> None:
    """``levain slot-allow list | revoke <rule> [--yes] | undo <rule>``: the rules slot sessions carry."""
    import argparse

    p = subparsers.add_parser("slot-allow", help="List, revoke and re-offer the always-allow rules slot sessions carry.")
    p.add_argument("--queue-dir", type=Path, default=None, help="The consent directory holding slot-allow.json.")
    sub = p.add_subparsers(dest="slot_allow_verb", required=True)
    sub.add_parser("list", help="The rules slot sessions carry, with where each came from.")
    r = sub.add_parser("revoke", help="Take a rule out (its signed answer is dropped; `undo` re-offers it).")
    r.add_argument("rule")
    r.add_argument("--yes", action="store_true", help="Confirm: without it nothing is changed.")
    u = sub.add_parser("undo", help="Offer a revoked rule again on the band, for a new signed yes.")
    u.add_argument("rule")
    p.set_defaults(func=run)


def _left(end: Optional[datetime]) -> str:
    """What is left of a lease ending ``end``, in days, or in hours under a day."""
    if end is None:
        return "lease unknown"
    left = end - datetime.now(timezone.utc)
    if left <= timedelta(0):
        return "lapsed"
    if left < timedelta(days=1):
        hours = int(left.total_seconds() // 3600)
        return "under an hour left" if hours == 0 else f"{hours} hour{'s' if hours != 1 else ''} left"
    return f"{left.days} days left"


def run(args: Any, out: Any = None) -> int:
    import sys

    from levain.consent.queue import default_queue_dir

    out = out or sys.stdout
    qdir = args.queue_dir.expanduser().resolve() if args.queue_dir is not None else default_queue_dir()
    path = qdir / FILE_NAME
    try:
        verb = args.slot_allow_verb
        if verb == "list":
            doc = load(path)
            judge = default_judge(qdir)
            try:
                from levain.consent.questions import QuestionStore

                ok, bad = carried(path, judge, store_id_of(QuestionStore(qdir)))
            except Exception as e:   # noqa: BLE001 - the listing does not depend on the authority log
                ok, bad = [], [(r["rule"], f"not checked ({type(e).__name__})") for r in doc["rules"]]
            why = dict(bad)
            try:
                from levain.consent.questions import QuestionStore

                asked_open = {q.what for q in QuestionStore(qdir).open_questions()}
            except Exception:   # noqa: BLE001 - the listing does not depend on the band
                asked_open = set()
            for r in doc["rules"]:
                end = lease_end(r) if isinstance(r.get("message"), str) and r.get("channel") in CHANNELS else None
                where = (f"session {r.get('session', '?')}, via {r.get('channel', '?')}, key {r.get('signer_fp', '?')}, "
                         f"signed {_signed_stamp(r)}, applied {r.get('applied_at', '?')}, {_left(end)}")
                if r["rule"] in why:
                    row = ("its row is open on the band: answer it again" if asked_text(r["rule"]) in asked_open else
                           "the next slot launch puts its row on the band")
                    out.write(f"{r['rule']}\t(NOT CARRIED: {why[r['rule']]}; {row}; {where})\n")
                else:
                    out.write(f"{r['rule']}\t({where})\n")
            try:
                waiting = not_carried(qdir, judge=judge)
            except Exception:   # noqa: BLE001 - the listing of carried rules does not depend on the band
                waiting = []
            for rule, reason in waiting:
                out.write(f"{rule}\t(answered yes, not carried: {reason})\n")
            stored = {r["rule"] for r in doc["rules"]}
            declined = [r for r in doc["revoked"] if r.get("reason") == DECLINED and r["rule"] not in stored]
            for r in declined:
                out.write(f"{r['rule']}\t(declined at renewal {r.get('revoked_at', '?')}: not carried; a slot saving it "
                          "again asks again)\n")
            if not doc["rules"] and not waiting and not declined:
                out.write("no slot allow rules\n")
            return 0
        if verb == "revoke":
            msg = revoke(path, args.rule, confirm=args.yes)
            out.write(msg + "\n")
            return 0 if args.yes else 1
        out.write(undo(path, args.rule, judge=default_judge(qdir)) + "\n")
        return 0
    except AllowRefused as e:
        out.write(f"levain slot-allow: {e}\n")
        return 1
    except OSError as e:
        out.write(f"levain slot-allow: {e.strerror or type(e).__name__}\n")
        return 1


__all__ = ["AGENT", "AllowRefused", "FILE_NAME", "FORMAT", "NO", "YES", "append", "apply_answered", "file_rows", "load",
           "merge_into_frozen", "parse_local_allow", "read_regular", "register", "revoke", "run", "rules_of", "shape_ok", "undo", "not_carried",
           "Judge", "carried", "lease_end", "offer", "remind", "default_judge", "rule_evidence", "verify_evidence", "asked_text", "yes_text"]
