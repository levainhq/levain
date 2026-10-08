"""``levain team export``: the ledger as JSON lines, for anneal-memory's team import and for people.

Default (``--jsonl``): the FRAMED stream of anneal's team-import contract v2: a header line, then one envelope per
VERIFIED ledger line, grouped per file in file order, so the reader can rebuild each file's ``prev -> hash`` chain, resolve
every ``supersedes`` target and require exactly one root per file. The ledger line travels as a JSON string value inside an
envelope this module builds with ``json.dumps``, so ledger content cannot become structure. A line that fails
verification (tampered, torn, misfiled) is left out here exactly as it is left out of enforcement; `levain team verify`
names it. ``in_force=True`` is a convenience view (only what is in force, time order): it is not framed and cannot be
chain-verified.

Framing makes per-file structure checkable (one root, one author per file); it is not authentication: the stream is one
trust unit this exporter vouches for.

Contract v3 (:func:`snapshot`, :func:`export_v3`; seam design ``spore1344_seam_design_1005.md`` §3a items 2-3, strict
profile, plus ``prev_root``): ONE CLONE'S COMPLETE VERDICT. A header naming this clone (``key``, clone-local, never
pushed), the ledger it pinned (``root``, ``prev_root``), its trust state (``epoch``, ``repin_n``) and the published
position it judged from (``pos``, ``seq``, ``judged``); then one envelope per non-blank line of the tenure CHAIN SET,
each saying whether levain enforces that exact line and which of its ``supersedes`` levain honours; then a trailer with
the envelope count. anneal-memory mirrors the verdict instead of judging links itself, so its store follows every
later verdict change (a revert, a veto, a hand-off) instead of keeping the links it first imported.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections import Counter
from dataclasses import dataclass

from . import entry as E
from . import index as I

STREAM_HEADER = {"anneal_team_stream": 2}
# anneal's frame-label rule; the paths levain writes (<handle>/<device>.jsonl) fit it, others get a digest label
_FRAME_LABEL = re.compile(r"[A-Za-z0-9._@:+/=-]{1,200}")


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n"


_OPAQUE = "unnamed-"


def _label(index: int, rel: str) -> str:
    """The frame label for the ``index``-th ledger file: its path when that fits anneal's rule, else
    ``unnamed-<index>-<sha256 of the path>``. Labels are unique BY CONSTRUCTION, with nothing to detect: a path that
    itself starts with ``unnamed-`` is treated as unframeable, so a literal label can never sit in the opaque namespace,
    and the file's index makes every opaque label differ even if two digests were ever equal. A path can be anything a
    member pushed under ``ledger/``, the label is only for grouping and report text, so a file that cannot be named is
    still exported, never a reason to withhold the rest."""
    if _FRAME_LABEL.fullmatch(rel) and not rel.startswith(_OPAQUE):
        return rel
    return f"{_OPAQUE}{index}-" + hashlib.sha256(rel.encode("utf-8", "surrogatepass")).hexdigest()


def _envelope(label: str, n: int, line: str) -> str:
    return json.dumps({"frame": label, "n": n, "line": line}, separators=(",", ":")) + "\n"


def export_stream(ledger: I.Ledger, *, in_force: bool = False) -> list[str]:
    """Output lines for ``levain team export --jsonl`` (each ends with a newline)."""
    if in_force:
        return [_line(e) for e in ledger.in_force]
    out = [json.dumps(STREAM_HEADER, separators=(",", ":")) + "\n"]
    for index, f in enumerate(sorted(ledger.files, key=lambda f: f.rel)):
        label = _label(index, f.rel)
        out.extend(_envelope(label, n, _line(obj).rstrip("\n")) for n, obj in enumerate(f.entries, 1))
    return out


# ---- contract v3: one clone's complete verdict ---------------------------------------------------------------

SNAPSHOT_VERSION = 3
STREAM_KEY = "anneal_team_stream"
STREAM_END = "anneal_team_stream_end"
# EXACTLY anneal-memory's ``_V3_HEADER`` / ``_V3_ENVELOPE`` (set equality on the reader side: one key more or less
# and the whole stream is refused). tests/test_team_export_v3.py checks both against anneal's own sets.
V3_HEADER = (STREAM_KEY, "key", "root", "prev_root", "epoch", "repin_n", "pos", "seq", "judged")
V3_ENVELOPE = ("frame", "n", "line", "enforced", "honours")
_INT_MAX = 2 ** 63 - 1          # anneal binds these into SQLite INTEGER
_KEY = re.compile(r"[0-9a-f]{32}")
_SHORT = re.compile(r"[0-9A-Za-z._:+/=-]{1,200}")
_LOCK = "anneal-export"         # serialises the clone key's creation and seq, the two read-modify-writes here


@dataclass
class Snapshot:
    """One clone's verdict, ready to frame: every header field but ``seq`` (taken only when the stream is sent)."""
    head: dict[str, object]
    envelopes: list[dict]
    enforced: list[tuple[str, str]]        # (id, hash) of every enforced line
    honoured: list[tuple[str, str]]        # (target, linker): what the enforced lines' ``honours`` carry
    state_hash: str                        # GitLedger.state_hash of the same ledger (entries, team, problems)

    def lines(self, seq: int) -> list[str]:
        """The framed v3 stream (each line ends with a newline)."""
        head = {STREAM_KEY: SNAPSHOT_VERSION, **self.head, "seq": int(seq)}
        out = [json.dumps({k: head[k] for k in V3_HEADER}, separators=(",", ":")) + "\n"]
        out += [json.dumps(env, separators=(",", ":")) + "\n" for env in self.envelopes]
        out.append(json.dumps({STREAM_END: len(self.envelopes)}, separators=(",", ":")) + "\n")
        return out


def _count(v: object) -> int:
    return v if type(v) is int and 0 <= v <= _INT_MAX else 0


def _clone_key(gl) -> str:
    """This clone's export key: random, created on the first export, kept in clone-local state, never pushed. No
    ledger content can change it (a root sha could be changed by one merge, and a fork shares it)."""
    k = gl.state().get("anneal_key")
    if isinstance(k, str) and _KEY.fullmatch(k):
        return k
    with gl.lock(name=_LOCK, timeout=10.0):
        k = gl.state().get("anneal_key")          # another process may have created it while this one waited
        if isinstance(k, str) and _KEY.fullmatch(k):
            return k
        k = secrets.token_hex(16)
        gl.save_state(anneal_key=k)
        return k


def next_seq(gl) -> int:
    """``max(wall-clock ns, last seq + 1)`` under the export lock: a clock stepped back never regresses it."""
    with gl.lock(name=_LOCK, timeout=10.0):
        seq = min(max(time.time_ns(), _count(gl.state().get("anneal_seq")) + 1), _INT_MAX)
        gl.save_state(anneal_seq=seq)
        return seq


def _epoch(root: str, distrust: object, repair_point: object) -> str:
    """CONTENT, not a counter: honest clones that apply the owner's named repair agree on it."""
    ds = sorted(x for x in (distrust if isinstance(distrust, list) else []) if isinstance(x, str))
    rp = repair_point if isinstance(repair_point, str) else ""
    return hashlib.sha256(json.dumps(["levain-epoch-v1", root, ds, rp]).encode("utf-8")).hexdigest()


def _pos(gl, d) -> int:
    """Commits on the walk up to this clone's ``anchor`` (the last PUBLISHED tip it derived in full); with no anchor,
    the walk to the remote-tracking tip, or to the local tip when the clone has no remote. 0 when none resolves."""
    from . import tenure as T
    st = gl.state()
    anchor = st.get("anchor") if isinstance(st.get("anchor"), str) and st.get("anchor") else None
    target = anchor
    if target is None:
        if gl.remote:
            rref = f"refs/remotes/{gl.remote}/{gl.branch}"
            target = rref if gl._has(rref) else None
        else:
            target = d.tip
    if target is None:
        return 0
    if target in d.walk:
        return d.walk.index(target) + 1
    try:
        top = gl.repo.toplevel
        sha = T._git(top, ["rev-parse", "--verify", "-q", target + "^{commit}"]).strip()
        return len(T.effective_walk(T.parents_of(top, sha), sha, gl.clone().accepted))
    except T.Unjudgeable:
        return 0


def snapshot(gl, d=None) -> Snapshot:
    """This clone's v3 verdict from the tenure derivation ``d`` (default: the tip's). Creates the clone key on the
    first call. Raises TeamError when the clone cannot judge its ledger."""
    from .transport import GitLedger, TeamError
    d = d if d is not None else gl.derivation()
    ledger = GitLedger._ledger_of(d)
    st = gl.state()
    root = gl.pinned_root
    if not root:
        raise TeamError("this clone has not pinned a team ledger: run `levain team join`")
    prev = st.get("anneal_prev_root")
    head: dict[str, object] = {
        "key": _clone_key(gl),
        "root": root,
        # the root this key last exported under (strict profile): differs from root only on the first export after
        # `levain team repin --root`, so anneal can tell this key's proven move from a copied state file
        "prev_root": prev if isinstance(prev, str) and _SHORT.fullmatch(prev) else root,
        "epoch": _epoch(root, st.get("distrust"), st.get("repair_point")),
        "repin_n": _count(st.get("repin_n")),
        "pos": min(_pos(gl, d), _INT_MAX),
        "judged": d.judged if d.judged in ("full", "partial") else "partial",
    }
    by_rel = {f.rel: f for f in ledger.files}
    kept = {id(e) for e in ledger.entries}
    pairs = ledger.honoured_pairs
    envelopes: list[dict] = []
    enforced: list[tuple[str, str]] = []
    honoured: list[tuple[str, str]] = []
    for index, (rel, texts) in enumerate(sorted(d.files.items())):
        label = _label(index, rel)
        f = by_rel.get(rel)
        # how many lines with each hash build() KEPT in this file (an id twin or a misfiled line is dropped there)
        left = Counter(e["hash"] for e in (f.entries if f else []) if id(e) in kept)
        skip = d.unenforced.get(rel, set())
        for n, text in enumerate(texts, 1):
            if not text.strip():
                continue
            try:
                obj = E.parse_line(text)
            except E.LineError:
                obj = None
            line, is_enforced, honours = text.rstrip("\r"), False, []
            if isinstance(obj, dict):
                # the entry as levain parsed and hashed it (the hash covers the parsed fields, so it still verifies)
                line = json.dumps(obj, ensure_ascii=False, sort_keys=True)
                h, lid = obj.get("hash"), obj.get("id")
                if isinstance(h, str) and h not in skip and left[h] > 0:
                    left[h] -= 1
                    is_enforced = True
                    enforced.append((str(lid), h))
                    sup = obj.get("supersedes")
                    honours = list(dict.fromkeys(
                        t for t in (sup if isinstance(sup, list) else []) if isinstance(t, str) and (t, lid) in pairs))
                    honoured += [(t, str(lid)) for t in honours]
            envelopes.append({"frame": label, "n": n, "line": line, "enforced": is_enforced, "honours": honours})
    return Snapshot(head, envelopes, enforced, sorted(set(honoured)), GitLedger.state_hash(ledger, d.team))


def record_exported(gl, snap: Snapshot) -> None:
    """After an export anneal accepted: the root it went under becomes this key's previous root."""
    gl.save_state(anneal_prev_root=snap.head["root"])


def export_v3(gl, d=None) -> list[str]:
    """Output lines of the v3 stream for ``gl`` (each ends with a newline). Takes a fresh ``seq``."""
    snap = snapshot(gl, d)
    return snap.lines(next_seq(gl))
