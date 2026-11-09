"""Row 165: a slot's "don't ask again" reaches future slots only through the operator's own signed answer.

Claude Code writes the rule a slot's dialog saves into the slot clone's ``.claude/settings.local.json``, which the
session never reads back and which the retained tree keeps. This module is the operator-side half of carrying such a
rule forward without ever widening what the operator's own sessions do:

* :func:`parse_local_allow` reads that file's bytes (duplicate keys refused), keeps only the shapes the dialog
  writes and names the rest as dropped;
* :func:`read_regular` is the bounded read of a file a slot wrote (no symlink, a regular file, size-capped);
* :func:`file_rows` puts each kept rule on the band, one row per rule with a yes and a no;
* :func:`apply_answered` appends a rule to ``slot-allow.json`` only for a row a person answered ``yes``;
* :func:`merge_into_frozen` adds the file's rules to a slot's frozen ``permissions.allow`` and nothing else.

``slot-allow.json`` lives beside the question store in the operator's consent directory. Nothing here writes to the
operator's checkout, and the file is read by slot launches only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

FORMAT = "levain-slot-allow/1"
FILE_NAME = "slot-allow.json"
PENDING_NAME = "slot-allow-pending.json"
PENDING_FORMAT = "levain-slot-allow-pending/1"
AGENT = "claude_code"      # the band names a harness; the row is told apart by its key
KEY_PREFIX = "slot-allow:"
SETTINGS_CAP = 64 * 1024
FILE_CAP = 1024 * 1024
RULE_MAX = 300
RULES_PER_SESSION_MAX = 50
YES, NO = "yes", "no"

# The three kinds of approval the dialog saves as a rule. A bare tool name for Bash, a wildcard-only pattern and
# anything with a line break or control character are not a shape the dialog writes.
_BASH = re.compile(r"Bash\((?P<body>[^()\x00-\x1f\x7f]{1,%d})\)" % (RULE_MAX - 6))
_WEBFETCH = re.compile(r"WebFetch\(domain:[A-Za-z0-9][A-Za-z0-9.-]{0,252}\)")
_WILD_ONLY = re.compile(r"[\s*:]*")


class AllowRefused(ValueError):
    """A file or document refused whole (not a dropped rule)."""


def shape_ok(rule: Any) -> bool:
    """Whether ``rule`` is a shape the dialog writes."""
    if type(rule) is not str or len(rule) > RULE_MAX:
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


def _save(path: Path, doc: Mapping[str, Any]) -> None:
    data = (json.dumps(doc, indent=1, sort_keys=True) + "\n").encode()
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def rules_of(path: Path) -> List[str]:
    return [r["rule"] for r in load(path)["rules"]]


def append(path: Path, rule: str, session: str, now: Callable[[], str] = _now) -> bool:
    """Add ``rule`` with where it came from. False when it is already there."""
    if not shape_ok(rule):
        raise AllowRefused("not a dialog rule shape")
    doc = load(path)
    if any(r["rule"] == rule for r in doc["rules"]):
        return False
    doc["rules"].append({"rule": rule, "session": session, "signed_at": now()})
    _save(path, doc)
    return True


def revoke(path: Path, rule: str, *, confirm: bool, now: Callable[[], str] = _now) -> str:
    """Take ``rule`` out of the store, keeping it under ``revoked`` so :func:`undo` can put it back. Without
    ``confirm`` nothing is written and the answer says what would go."""
    doc = load(path)
    hit = [r for r in doc["rules"] if r["rule"] == rule]
    if not hit:
        raise AllowRefused(f"{rule!r} is not in {path.name}")
    if not confirm:
        return f"would revoke {rule!r} (signed {hit[0].get('signed_at', '?')} from session {hit[0].get('session', '?')}); re-run with --yes"
    doc["rules"] = [r for r in doc["rules"] if r["rule"] != rule]
    doc["revoked"] = [r for r in doc["revoked"] if r["rule"] != rule] + [{**hit[0], "revoked_at": now()}]
    _save(path, doc)
    return f"revoked {rule!r}; `levain slot-allow undo` restores it"


def undo(path: Path, rule: str) -> str:
    doc = load(path)
    hit = [r for r in doc["revoked"] if r["rule"] == rule]
    if not hit:
        raise AllowRefused(f"{rule!r} has no revoke to undo")
    back = {k: v for k, v in hit[-1].items() if k != "revoked_at"}
    doc["revoked"] = [r for r in doc["revoked"] if r["rule"] != rule]
    if not any(r["rule"] == rule for r in doc["rules"]):
        doc["rules"].append(back)
    _save(path, doc)
    return f"restored {rule!r}"


# --- the band rows ------------------------------------------------------------------------------------------------

def _key(rule: str) -> str:
    return KEY_PREFIX + hashlib.sha256(rule.encode()).hexdigest()[:16]


def _pending(qdir: Path) -> Dict[str, Any]:
    raw = read_regular(qdir / PENDING_NAME, FILE_CAP)
    if raw is None:
        return {}
    doc = _loads(raw)
    if not isinstance(doc, dict) or doc.get("format") != PENDING_FORMAT or not isinstance(doc.get("rows"), dict):
        raise AllowRefused(f"{PENDING_NAME}: malformed")
    return doc["rows"]


def _save_pending(qdir: Path, rows: Mapping[str, Any]) -> None:
    _save(qdir / PENDING_NAME, {"format": PENDING_FORMAT, "rows": dict(rows)})


def file_rows(qdir: Path, session: str, rules: Sequence[str], store: Any = None) -> List[str]:
    """One band row per rule (a yes and a no), listing the rule verbatim. A rule already on the band (open, or
    answered by a person) files nothing again. Returns the rules newly filed. Never writes the allow file."""
    from levain.consent.questions import ANSWER_CHANNELS, QuestionStore

    qs = store or QuestionStore(qdir)
    pend = _pending(qdir)
    filed: List[str] = []
    for rule in rules:
        if not shape_ok(rule):
            continue
        key = _key(rule)
        q, new = qs.file_once(
            agent=AGENT, key=key, cwd=str(qdir), held_by=ANSWER_CHANNELS, rec=NO,
            what=f"A slot session asked to always allow: {rule}",
            options=[(YES, f"Yes: allow {rule} in every future slot session"), (NO, "No: keep asking")],
            blocks=(f"nothing: the slot already ran. Yes adds the rule to {FILE_NAME} (slot sessions only; never "
                    f"your checkout or your own sessions); No asks again next run. Session {session}."))
        if new:
            pend[key] = {"rule": rule, "session": session}
            filed.append(rule)
    if filed:
        _save_pending(qdir, pend)
    return filed


def apply_answered(qdir: Path, allow_file: Path, store: Any = None, now: Callable[[], str] = _now) -> List[str]:
    """Carry every rule whose row a person answered ``yes`` into ``allow_file``; a ``no`` drops the pending note.
    A row not yet answered is left. Returns the rules added."""
    from levain.consent.questions import ANSWER_CHANNELS, QuestionStore

    pend = _pending(qdir)
    if not pend:
        return []
    qs = store or QuestionStore(qdir)
    by_key = {q.agent_session_id: q for q in qs.recent(500) if q.agent_session_id.startswith(KEY_PREFIX)}
    added: List[str] = []
    for key in sorted(pend):
        q = by_key.get(key)
        a = None if q is None else q.answer
        if a is None or a.channel not in ANSWER_CHANNELS:
            continue
        if a.kind == "option" and a.value == YES:
            if append(allow_file, pend[key]["rule"], str(pend[key].get("session", "?")), now):
                added.append(pend[key]["rule"])
        del pend[key]
    _save_pending(qdir, pend)
    return added


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

    p = subparsers.add_parser("slot-allow", help="List, revoke and restore the always-allow rules slot sessions carry.")
    p.add_argument("--queue-dir", type=Path, default=None, help="The consent directory holding slot-allow.json.")
    sub = p.add_subparsers(dest="slot_allow_verb", required=True)
    sub.add_parser("list", help="The rules slot sessions carry, with where each came from.")
    r = sub.add_parser("revoke", help="Take a rule out (it is kept, so `undo` can restore it).")
    r.add_argument("rule")
    r.add_argument("--yes", action="store_true", help="Confirm: without it nothing is changed.")
    u = sub.add_parser("undo", help="Put a revoked rule back.")
    u.add_argument("rule")
    p.set_defaults(func=run)


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
            for r in doc["rules"]:
                out.write(f"{r['rule']}\t(session {r.get('session', '?')}, signed {r.get('signed_at', '?')})\n")
            if not doc["rules"]:
                out.write("no slot allow rules\n")
            return 0
        if verb == "revoke":
            msg = revoke(path, args.rule, confirm=args.yes)
            out.write(msg + "\n")
            return 0 if args.yes else 1
        out.write(undo(path, args.rule) + "\n")
        return 0
    except AllowRefused as e:
        out.write(f"levain slot-allow: {e}\n")
        return 1
    except OSError as e:
        out.write(f"levain slot-allow: {e.strerror or type(e).__name__}\n")
        return 1


__all__ = ["AGENT", "AllowRefused", "FILE_NAME", "FORMAT", "NO", "YES", "append", "apply_answered", "file_rows", "load",
           "merge_into_frozen", "parse_local_allow", "read_regular", "register", "revoke", "run", "rules_of", "shape_ok", "undo"]
