"""levain.dream.shadow (D1-3): the dream's shadow nights (D1_EXECUTOR §8 and §11 D1-3).

A shadow night runs the dream on a COPY of flow's own pre-dream state for one dream-day, then compares what Levain's
dream composed with what flow's dream composed. It saves nothing anywhere real: the only store it writes is its own
scratch copy, and flow's ``state/dream`` tree is read-only input. The comparison is evidence for Phill; it never
settles anything, so its card is never ``authority_relevant``.

The pieces, in the order a night uses them:

* :func:`read_flow_day` / :func:`day_ready`: flow's ledger ``end`` row for the dream-day with ``committed`` true, a
  snapshot directory whose every file matches its ``manifest.json`` hash, and no shadow ``done`` for the day. A day flow
  has not committed is QUIET (nothing to shadow); a snapshot that cannot be read or does not match its hash is
  UNREADABLE (fail-closed, never a guess).
* :func:`seed`: the snapshot into the shadow store, under that store's ``wrap.lock``, after the hash check. The
  directory must carry the shadow marker (it is made on first use of an empty directory); a directory holding a store
  without the marker is refused, so a mistaken path can never wipe a real store.
* :func:`episode_check`: the shadow's own ``prepare_wrap`` episode set against flow's ``package.json``. Flow's file
  carries ``episode_count`` (an int) and the ids only inside the ``package.episodes`` text, as ``- (xxxxxxxx)``
  8-hex prefixes; the comparison is by those ids, and by count when either side carries none. A mismatch ends the
  night ``skipped-no-input`` with reason ``input-diverged``.
* :func:`diff_outputs` / :func:`shadow_card`: ``<occ>/card_diff.txt`` plus counts in the card's ``own`` block.
* :func:`run_night`: the pieces in order with the compose step INJECTED (the existing executor is the real one).
  Nothing in this module composes.

The root of flow's dream tree is always a parameter. Tests build a small layout by hand; the real tree is read only
by the ``levain dream shadow`` verb's explicit ``--flow-root``.
"""
from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from levain.dream import card as _card
from levain.dream import ready as _ready
from levain.dream import snapshot as _snap

LEDGER = "ledger.jsonl"
SNAPSHOTS = "snapshots"
PACKAGE = "package.json"
COMPOSED = "composed.continuity.md"
STEPS_LOG = "steps.log"
MARKER = ".levain-shadow"
NIGHT_LOCK = "night.lock"
#: the manifest tiers a seed needs: a manifest that omits one is damaged, never seeded short
REQUIRED_TIERS = tuple(_snap.text_tier("x").keys())
#: the kinds a verdict carries (``<state>/<kind>`` is what a ``skipped-no-input`` row records)
KIND_NO_FLOW_DREAM = "no-flow-dream"
KIND_SHADOWED = "already-shadowed"
KIND_DIVERGED = "input-diverged"
KIND_DAMAGED = "snapshot-damaged"
KIND_UNREADABLE = "unreadable"

_RUN_ID = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]*$")
_EPISODE_ID = re.compile(r"^- \(([0-9a-f]{8})\)", re.MULTILINE)
_SHOWN = 5


class ShadowRefused(Exception):
    """A step refused; the message names what and why. Every refusal is fail-closed."""

    def __init__(self, kind: str, why: str) -> None:
        super().__init__(why)
        self.kind = kind
        self.why = why


def _sha256_file(path: Path) -> str | None:
    return _snap.sha256_file(path)


def _plain_name(name: object, what: str) -> str:
    if not isinstance(name, str) or not name or name != os.path.basename(name) or name in (".", ".."):
        raise ShadowRefused(KIND_DAMAGED, f"the manifest names {what} {name!r}, which is not a plain file name")
    return name


# -------------------------------------------------------------------------------------------------------- flow's day

@dataclass(frozen=True)
class FlowDay:
    """One flow dream-day that is fit to shadow: its ledger ``end`` row, the snapshot dir and the run dir."""
    day: str
    run_id: str
    snap_dir: Path
    run_dir: Path
    manifest: Mapping[str, Any]
    end: Mapping[str, Any]

    @property
    def db_backup(self) -> Path:
        return self.snap_dir / _snap.DB_BACKUP


def _ledger_rows(root: Path) -> tuple[list[dict[str, Any]], int]:
    """``(rows, malformed line count)``; a ledger that cannot be opened is a refusal."""
    try:
        text = (root / LEDGER).read_text(encoding="utf-8")
    except OSError as exc:
        raise ShadowRefused(KIND_UNREADABLE, f"flow's ledger {root / LEDGER} cannot be read ({type(exc).__name__}: {exc})")
    rows: list[dict[str, Any]] = []
    bad = 0
    last_ok = True
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            bad += 1
            last_ok = False
            continue
        if isinstance(doc, dict):
            rows.append(doc)
            last_ok = True
        else:
            bad += 1
            last_ok = False
    if not last_ok:
        # the newest line is the one that would supersede an older committed end: a torn tail is not read past
        raise ShadowRefused(KIND_UNREADABLE, f"flow's ledger {root / LEDGER} tail unreadable: its last line does not "
                                             "parse, so the newest end row cannot be known")
    return rows, bad


def verify_snapshot(snap_dir: Path, run_id: str) -> dict[str, Any]:
    """The snapshot's manifest, after every file it names matched its hash. ``files`` entries with a null hash name a
    file the dream found absent, so that file must be absent. Raises :class:`ShadowRefused` otherwise."""
    manifest = _snap._read_json(snap_dir / _snap.MANIFEST)
    if not isinstance(manifest, dict):
        raise ShadowRefused(KIND_UNREADABLE, f"{snap_dir / _snap.MANIFEST} is missing or not a JSON object")
    if manifest.get("id") not in (None, run_id):
        raise ShadowRefused(KIND_DAMAGED, f"the manifest in {snap_dir} is for {manifest.get('id')!r}, not {run_id!r}")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ShadowRefused(KIND_DAMAGED, "the manifest names no text-tier files")
    missing = [k for k in REQUIRED_TIERS if k not in files]
    if missing:
        raise ShadowRefused(KIND_DAMAGED, f"the manifest lacks the required tier(s) {', '.join(missing)}")
    for key, entry in files.items():
        if not isinstance(entry, dict):
            raise ShadowRefused(KIND_DAMAGED, f"the manifest entry {key!r} is not an object")
        name = _plain_name(entry.get("name"), f"the {key} file")
        want = entry.get("sha256")
        have = _sha256_file(snap_dir / name)
        if want is None:
            if have is not None:
                raise ShadowRefused(KIND_DAMAGED, f"{name} exists but the manifest records it absent")
        elif have != want:
            raise ShadowRefused(KIND_DAMAGED, f"{snap_dir / name} does not match its manifest hash")
    db_want = manifest.get("db_backup_sha256")
    if not isinstance(db_want, str) or not db_want:
        raise ShadowRefused(KIND_DAMAGED, "the manifest carries no db_backup_sha256")
    if _sha256_file(snap_dir / _snap.DB_BACKUP) != db_want:
        raise ShadowRefused(KIND_DAMAGED, f"{snap_dir / _snap.DB_BACKUP} does not match its manifest hash")
    return manifest


def read_flow_day(root: Path | str, day: date | str) -> FlowDay:
    """Flow's committed dream for ``day`` under ``root`` (flow's ``state/dream``), hash-checked. Reads only.

    Raises :class:`ShadowRefused` with kind ``no-flow-dream`` when the ledger holds no committed ``end`` row for the
    day (QUIET: flow did not dream it), and ``unreadable`` / ``snapshot-damaged`` for everything that could not be
    read or did not match (UNREADABLE). The newest committed row wins."""
    root = Path(root)
    key = day.isoformat() if isinstance(day, date) else str(day)
    rows, bad = _ledger_rows(root)
    ends = [r for r in rows if r.get("event") == "end" and r.get("day") == key and r.get("committed") is True]
    if not ends:
        if bad:
            raise ShadowRefused(KIND_UNREADABLE, f"flow's ledger holds {bad} unreadable line(s) and no committed end "
                                                 f"for {key}; one of them may be it")
        raise ShadowRefused(KIND_NO_FLOW_DREAM, f"flow's ledger has no committed dream for {key}")
    end = ends[-1]
    run_id = end.get("id")
    if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
        raise ShadowRefused(KIND_UNREADABLE, f"flow's end row for {key} carries no usable run id ({run_id!r})")
    snap_dir = root / SNAPSHOTS / run_id
    run_dir = root / run_id
    manifest = verify_snapshot(snap_dir, run_id)
    if not (run_dir / PACKAGE).is_file():
        raise ShadowRefused(KIND_UNREADABLE, f"{run_dir / PACKAGE} is missing")
    return FlowDay(key, run_id, snap_dir, run_dir, manifest, end)


# --------------------------------------------------------------------------------------------------------- day-ready

@dataclass(frozen=True)
class SourceVerdict:
    verdict: str                       # ready | quiet | unreadable
    kind: str
    reason: str
    flow_day: FlowDay | None = None

    @property
    def ready(self) -> bool:
        return self.verdict == _ready.READY


def day_ready(root: Path | str, day: date | str, *, occurrences: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
              zone: str | None = None, agent_id: str | None = None) -> SourceVerdict:
    """The shadow's day-ready: flow committed ``day``, the snapshot matches its hashes, and no shadow occurrence of
    ``agent_id`` is ``done`` for the day. ``occurrences`` returns the shadow's own occurrence rows (omitted: no
    history, which is named). Never raises: a source that cannot be read is ``unreadable``, never ready."""
    key = day.isoformat() if isinstance(day, date) else str(day)
    if occurrences is not None:
        try:
            if zone is None:
                raise ValueError("a zone is needed to place an occurrence in its dream-day")
            for r in occurrences():
                if agent_id is not None and r.get("agent_id") != agent_id:
                    continue
                if r.get("state") == "done" and _ready.occurrence_dream_day(r, zone).isoformat() == key:
                    return SourceVerdict(_ready.QUIET, KIND_SHADOWED,
                                         f"dream-day {key} was already shadowed by {r.get('occurrence_id')}")
        except Exception as exc:  # noqa: BLE001 — a history that cannot be read has not said "not yet"
            return SourceVerdict(_ready.UNREADABLE, KIND_UNREADABLE,
                                 f"the shadow's occurrence table: {type(exc).__name__}: {exc}")
    try:
        fd = read_flow_day(root, key)
    except ShadowRefused as exc:
        state = _ready.QUIET if exc.kind == KIND_NO_FLOW_DREAM else _ready.UNREADABLE
        return SourceVerdict(state, exc.kind, exc.why)
    except Exception as exc:  # noqa: BLE001
        return SourceVerdict(_ready.UNREADABLE, KIND_UNREADABLE, f"{type(exc).__name__}: {exc}")
    note = "" if occurrences is not None else " (no shadow occurrence history was given)"
    return SourceVerdict(_ready.READY, "", f"flow committed {key} as {fd.run_id} and its snapshot matches{note}", fd)


# -------------------------------------------------------------------------------------------------------------- seed

@dataclass(frozen=True)
class Seeded:
    db: Path
    run_id: str
    files: tuple[str, ...]


def _lock(store_dir: Path, name: str = "wrap.lock"):
    from levain import wrap as _w

    handle = _w._lock_wrap_file(store_dir / name)
    if handle is _w._ANOTHER_WRAP_RUNNING:
        raise ShadowRefused(KIND_UNREADABLE, f"another process holds {store_dir / name}; the shadow store is busy")
    return handle


def _unlock(handle: Any) -> None:
    from levain import wrap as _w

    _w._unlock_wrap(handle)


@contextlib.contextmanager
def night_lock(flow_day: FlowDay, shadow_db: Path | str):
    """Hold the night-level lock of the scratch directory of ``shadow_db`` (after the real-store guard, creating the
    directory), so two shadow verbs, or a verb and a night, cannot interleave seed and episode check on it. A directory
    another process holds raises :class:`ShadowRefused` (unreadable) with the store busy."""
    shadow_db = Path(shadow_db)
    _guard_target(shadow_db, flow_day)
    shadow_db.parent.mkdir(parents=True, exist_ok=True)
    handle = _lock(shadow_db.parent, NIGHT_LOCK)
    try:
        yield
    finally:
        _unlock(handle)


def _guard_target(shadow_db: Path, flow_day: FlowDay) -> None:
    """Refuse a target that could be a real store: flow's own, or a directory with a store and no shadow marker."""
    anneal_db = flow_day.manifest.get("anneal_db")
    if isinstance(anneal_db, str) and anneal_db:
        try:
            same = Path(anneal_db).resolve() == shadow_db.resolve()
        except OSError as exc:
            raise ShadowRefused(KIND_UNREADABLE, f"cannot tell whether {shadow_db} is flow's own store "
                                                 f"({type(exc).__name__}: {exc}); refusing")
        if same:
            raise ShadowRefused(KIND_UNREADABLE, f"{shadow_db} is flow's own store; a shadow never seeds it")
    d = shadow_db.parent
    if d.is_dir() and not (d / MARKER).exists() and any(x.name != NIGHT_LOCK for x in d.iterdir()):
        raise ShadowRefused(KIND_UNREADABLE, f"{d} is not empty and carries no {MARKER} marker; refusing to seed over it")


def _put(src: Path, dest: Path) -> None:
    fd, tmp = tempfile.mkstemp(prefix=dest.name + ".", suffix=".tmp", dir=str(dest.parent))
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def seed(flow_day: FlowDay, shadow_db: Path | str, *, claim_baton: bool = True,
         session_id: str = _ready.SESSION_ID) -> Seeded:
    """Seed ``flow_day``'s snapshot into the shadow store at ``shadow_db``, under its ``wrap.lock``, after verifying
    the snapshot's hashes AGAIN under the lock (the check and the copy must see the same bytes). Everything of the
    previous seed (db, sidecars, baton, a left-over wal) is replaced; then the baton is claimed for ``session_id`` on
    the copy so its ``prepare_wrap`` runs as a consolidate."""
    shadow_db = Path(shadow_db)
    d = shadow_db.parent
    _guard_target(shadow_db, flow_day)
    d.mkdir(parents=True, exist_ok=True)
    lock = _lock(d)
    try:
        manifest = verify_snapshot(flow_day.snap_dir, flow_day.run_id)
        # the marker goes down FIRST: a seed that dies part-way leaves a marked scratch dir the retry may replace, never
        # an unmarked half-written one the guard would refuse for good
        (d / MARKER).write_text(json.dumps({"run_id": flow_day.run_id, "day": flow_day.day}, sort_keys=True),
                                encoding="utf-8")
        stem = shadow_db.stem
        for p in list(d.iterdir()):
            if p.name.startswith(stem + ".") or p.name == shadow_db.name:
                if p.is_file() and p.name != "wrap.lock":
                    p.unlink()
        _put(flow_day.db_backup, shadow_db)
        if _sha256_file(shadow_db) != manifest["db_backup_sha256"]:
            raise ShadowRefused(KIND_DAMAGED, "the seeded db does not match the snapshot's hash")
        tier = _snap.text_tier(shadow_db)
        put = [shadow_db.name]
        for key, entry in manifest["files"].items():
            if key not in tier:
                raise ShadowRefused(KIND_DAMAGED, f"the manifest names a file kind {key!r} this install does not know")
            if entry.get("sha256") is None:
                continue
            _put(flow_day.snap_dir / entry["name"], tier[key])
            put.append(tier[key].name)
        if claim_baton:
            from anneal_memory import sessions

            sessions.claim_baton(tier["continuity"], session_id)
        return Seeded(shadow_db, flow_day.run_id, tuple(put))
    finally:
        _unlock(lock)


# --------------------------------------------------------------------------------------------------- episode set check

@dataclass(frozen=True)
class EpisodeSet:
    count: int | None
    ids: frozenset[str]


def package_ids(text: object) -> frozenset[str]:
    """The ``- (xxxxxxxx)`` 8-hex ids at line starts of a package's episodes text."""
    return frozenset(_EPISODE_ID.findall(text)) if isinstance(text, str) else frozenset()


def episode_set(package: Mapping[str, Any]) -> EpisodeSet:
    """The count and ids a ``prepare_wrap`` result (or flow's ``package.json``, the same shape) carries."""
    count = package.get("episode_count")
    inner = package.get("package")
    text = inner.get("episodes") if isinstance(inner, Mapping) else None
    return EpisodeSet(count if isinstance(count, int) and not isinstance(count, bool) else None, package_ids(text))


def flow_episode_set(flow_day: FlowDay) -> EpisodeSet:
    doc = _snap._read_json(flow_day.run_dir / PACKAGE)
    if not isinstance(doc, dict):
        raise ShadowRefused(KIND_UNREADABLE, f"{flow_day.run_dir / PACKAGE} is not a JSON object")
    return episode_set(doc)


def shadow_episode_set(shadow_db: Path | str, *, session_id: str | None = None) -> EpisodeSet:
    """The shadow store's own ``prepare_wrap`` episode set, read the way the executor reads it (unattended, crystal
    store refusing), and the wrap it opened cancelled by its token. Nothing is composed."""
    import uuid

    from anneal_memory import AnnealMemoryError, CrystalStore, Store
    from anneal_memory.continuity import prepare_wrap

    from levain import wrap as _w
    from levain.firing.crystallization import refuse_crystallization

    shadow_db = Path(shadow_db)
    token = uuid.uuid4().hex
    try:
        store = Store(str(shadow_db), project_name=shadow_db.parent.name, section_schema=None)
    except AnnealMemoryError as exc:
        raise ShadowRefused(KIND_UNREADABLE, f"the shadow store cannot be opened ({type(exc).__name__}: {exc})")
    try:
        crystal = refuse_crystallization(CrystalStore(_snap.text_tier(shadow_db)["crystal"]))
        sid = session_id if session_id is not None else (_ready.SESSION_ID if store.consolidate_requires_baton() else None)
        res = prepare_wrap(store, crystal_store=crystal, wrap_token=token, session_id=sid)  # type: ignore[arg-type]
        status = res.get("status")
        try:
            if status == "empty":
                return EpisodeSet(0, frozenset())
            if status != "ready":
                raise ShadowRefused(KIND_UNREADABLE, f"the shadow's prepare_wrap answered {status!r}: {res.get('message')}")
            return episode_set(res)
        finally:
            if status == "ready" and not _w._cancel_if_ours(store, token):
                raise ShadowRefused(KIND_UNREADABLE, "the shadow's probe wrap could not be cancelled; the store is left "
                                                     "with an open wrap (clear it with wrap-cancel before the next night)")
    finally:
        store.close()


def compare_episode_sets(flow: EpisodeSet, shadow: EpisodeSet) -> str | None:
    """None when the two sets agree, else the reason they do not. By id when both sides carry ids, else by count; a
    side with neither ids nor a count cannot be compared and is a mismatch (never a pass)."""
    if flow.ids and shadow.ids:
        if flow.ids == shadow.ids:
            return None
        only_f, only_s = sorted(flow.ids - shadow.ids), sorted(shadow.ids - flow.ids)
        return (f"the shadow's episode set differs from flow's: {len(only_f)} only in flow"
                f"{' (' + ', '.join(only_f[:_SHOWN]) + ')' if only_f else ''}, {len(only_s)} only in the shadow"
                f"{' (' + ', '.join(only_s[:_SHOWN]) + ')' if only_s else ''}")
    if flow.count is None or shadow.count is None:
        return f"the episode sets cannot be compared (flow count {flow.count}, shadow count {shadow.count}, no ids)"
    if flow.count != shadow.count:
        return f"the shadow prepared {shadow.count} episode(s) where flow's package holds {flow.count}"
    return None


def episode_check(flow_day: FlowDay, shadow_db: Path | str) -> SourceVerdict:
    """The input-diverged check for a seeded shadow store. READY, or QUIET with kind ``input-diverged``."""
    try:
        flow = flow_episode_set(flow_day)
        lock = _lock(Path(shadow_db).parent)
        try:
            shadow = shadow_episode_set(shadow_db)
        finally:
            _unlock(lock)
    except ShadowRefused as exc:
        return SourceVerdict(_ready.UNREADABLE, exc.kind, exc.why, flow_day)
    except Exception as exc:  # noqa: BLE001
        return SourceVerdict(_ready.UNREADABLE, KIND_UNREADABLE, f"{type(exc).__name__}: {exc}", flow_day)
    why = compare_episode_sets(flow, shadow)
    if why is not None:
        return SourceVerdict(_ready.QUIET, KIND_DIVERGED, why, flow_day)
    n = flow.count if flow.count is not None else len(flow.ids)
    return SourceVerdict(_ready.READY, "", f"the shadow prepared the same {n} episode(s) as flow's package", flow_day)


# ---------------------------------------------------------------------------------------------------------------- diff

@dataclass(frozen=True)
class ShadowDiff:
    text: str
    flow_chars: int
    shadow_chars: int
    flow_lines: int
    shadow_lines: int
    sections: dict[str, dict[str, int]]
    decisions_missing: tuple[str, ...]

    def own_lines(self) -> list[str]:
        out = [f"shadow: sizes: flow {self.flow_chars} chars / {self.flow_lines} lines, "
               f"shadow {self.shadow_chars} chars / {self.shadow_lines} lines"]
        for name, c in self.sections.items():
            out.append(f"shadow: {name}: {c['only_in_flow']} line(s) only in flow, {c['only_in_shadow']} only in the shadow")
        if self.decisions_missing:
            shown = "; ".join(_card._first(x, 80) for x in self.decisions_missing[:_SHOWN])
            more = f" (+{len(self.decisions_missing) - _SHOWN} more)" if len(self.decisions_missing) > _SHOWN else ""
            out.append(f"shadow: Decisions: {len(self.decisions_missing)} flow line(s) missing from the shadow: {shown}{more}")
        else:
            out.append("shadow: Decisions: no flow line is missing from the shadow")
        return out


def _norm_lines(lines: Sequence[str]) -> set[str]:
    return {x.strip() for x in lines if x.strip()}


def diff_outputs(flow_text: str, shadow_text: str) -> ShadowDiff:
    """What the two composed neocortex texts differ by. Counts are of distinct stripped non-blank lines, so a moved
    line is not a difference; only the sections that differ are listed."""
    a, b = _card.sections(flow_text), _card.sections(shadow_text)
    sections: dict[str, dict[str, int]] = {}
    for name in [*a, *(n for n in b if n not in a)]:
        fa, fb = _norm_lines(a.get(name, [])), _norm_lines(b.get(name, []))
        if fa != fb:
            sections[name] = {"only_in_flow": len(fa - fb), "only_in_shadow": len(fb - fa)}
    shadow_all = _norm_lines(shadow_text.splitlines())
    missing = tuple(x.strip() for x in a.get("Decisions", []) if x.strip() and x.strip() not in shadow_all)
    text = "".join(difflib.unified_diff(flow_text.splitlines(True), shadow_text.splitlines(True), "flow", "shadow", n=1))
    return ShadowDiff(text, len(flow_text), len(shadow_text), len(flow_text.splitlines()),
                      len(shadow_text.splitlines()), sections, missing)


def flow_refusals(run_dir: Path | str) -> list[str]:
    """The save refusals flow's dream met, from its ``steps.log``: every ``$ save ...`` step that exited non-zero, with
    its first output line. A log that cannot be read is named as such (never an empty list that reads as none)."""
    try:
        text = (Path(run_dir) / STEPS_LOG).read_text(encoding="utf-8")
    except OSError as exc:
        return [f"flow's steps.log could not be read ({type(exc).__name__})"]
    out: list[str] = []
    for block in re.split(r"(?m)^(?=\$ )", text):
        m = re.match(r"\$ (save\b[^\n]*?)\s+->\s+(-?\d+)\s*\n(.*)", block, re.S)
        if m and m.group(2) != "0":
            first = next((ln.strip() for ln in m.group(3).splitlines() if ln.strip()), "")
            out.append(f"flow {m.group(1).split()[0]} exit {m.group(2)}: {_card._first(first)}")
    return out


def _refusal_lines(flow: Sequence[str], shadow: Sequence[str]) -> list[str]:
    out = [f"shadow: refusals: flow {len(flow)}, shadow {len(shadow)}"]
    out += [f"shadow: flow refusal: {_card._first(x)}" for x in flow[:_SHOWN]]
    out += [f"shadow: shadow refusal: {_card._first(x)}" for x in shadow[:_SHOWN]]
    return out


def shadow_card(flow_day: FlowDay, shadow_text: str, *, occurrence: str, occ_dir: Path | str,
                usage_doc: Mapping[str, Any] | None = None, agent_id: str = "dream", flow_text: str | None = None,
                reason: str = "", instructions: tuple[Path, str | None] | None = None) -> _card.Card:
    """The comparison card: ``card_diff.txt`` beside the occurrence and counts in the card's ``own`` block. The card
    is never ``authority_relevant``: it carries no ``his`` entry, and the compose's notes are not read at all.
    ``instructions`` is ``(the instructions copy the compose was given, its sha256 or None)``, stamped as an ``own``
    line so two nights' cards say whether they measured the same instructions."""
    occ = Path(occ_dir)
    if flow_text is None:
        try:
            flow_text = (flow_day.run_dir / COMPOSED).read_text(encoding="utf-8")
        except OSError as exc:
            raise ShadowRefused(KIND_UNREADABLE, f"flow's composed text {flow_day.run_dir / COMPOSED} cannot be read "
                                                 f"({type(exc).__name__}: {exc})")
    d = diff_outputs(flow_text, shadow_text)
    _card._write(occ / _card.DIFF_FILE, d.text)
    sha = hashlib.sha256(d.text.encode("utf-8")).hexdigest()
    shadow_refs = _card._refusals(usage_doc)
    own = [*d.own_lines(), *_refusal_lines(flow_refusals(flow_day.run_dir), shadow_refs),
           f"shadow: full diff {occ / _card.DIFF_FILE} (sha256 {sha[:16]})"]
    if instructions is not None:
        ipath, isha = instructions
        own.append(f"shadow: instructions {ipath} (sha256 {isha[:16]})" if isha else
                   f"shadow: instructions {ipath} (sha256 not recorded)")
    return _card.build_card("consolidated", occurrence=occurrence, agent_id=agent_id, day=flow_day.day,
                            reason=reason or f"shadow night against flow's {flow_day.run_id}; nothing was saved to a real store",
                            own_extra=own, usage_doc=usage_doc, out_dir=occ,
                            remedy="nothing to undo: the shadow store is a scratch copy")


# ------------------------------------------------------------------------------------------------------------ the night

@dataclass
class NightResult:
    state: str                         # skipped-no-input | composed | failed
    reason: str = ""
    card: _card.Card | None = None
    flow_day: FlowDay | None = None
    detail: dict[str, Any] = field(default_factory=dict)


#: ``compose(flow_day, shadow_db, occ_dir) -> (shadow composed text, usage doc or None)``; the real one wraps the
#: executor's ``consolidate_store`` with flow's pinned model and a read-only copy of flow's instructions. INJECTED.
Compose = Callable[[FlowDay, Path, Path], "tuple[str, Mapping[str, Any] | None]"]


def run_night(flow_root: Path | str, day: date | str, shadow_db: Path | str, occ_dir: Path | str, *, occurrence: str,
              compose: Compose, occurrences: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
              zone: str | None = None, agent_id: str | None = None) -> NightResult:
    """One shadow night in order: day-ready, seed, episode check, the INJECTED compose, diff and card. An input that
    is not ready ends the night ``skipped-no-input`` with the verdict's ``<state>/<kind>`` and reason; the compose
    is never reached then. A compose that raises ends ``failed``. ``occurrences`` (the shadow's own history) is
    required: without it a day already shadowed cannot be told from a fresh one, and the night would spend a second
    compose. Flow's composed text is read before anything is seeded or composed. One night-level lock is held from
    the seed through the card, so two nights on one scratch directory cannot interleave."""
    if occurrences is None:
        return NightResult("skipped-no-input", f"{_ready.UNREADABLE}/{KIND_UNREADABLE}: no shadow occurrence history was "
                                               "given, so an already-shadowed day cannot be ruled out")
    v = day_ready(flow_root, day, occurrences=occurrences, zone=zone, agent_id=agent_id)
    if not v.ready:
        return NightResult("skipped-no-input", f"{v.verdict}/{v.kind}: {v.reason}")
    fd = v.flow_day
    assert fd is not None
    try:
        flow_text = (fd.run_dir / COMPOSED).read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        return NightResult("skipped-no-input", f"{_ready.UNREADABLE}/{KIND_UNREADABLE}: flow's composed text "
                                               f"{fd.run_dir / COMPOSED} cannot be read ({type(exc).__name__}: {exc})",
                           flow_day=fd)
    shadow_db = Path(shadow_db)
    try:
        with night_lock(fd, shadow_db):
            seed(fd, shadow_db)
            v = episode_check(fd, shadow_db)
            if not v.ready:
                return NightResult("skipped-no-input", f"{v.verdict}/{v.kind}: {v.reason}", flow_day=fd)
            try:
                text, usage_doc = compose(fd, shadow_db, Path(occ_dir))
            except Exception as exc:  # noqa: BLE001 — a compose that died is a failed night, not an input verdict
                return NightResult("failed", f"compose raised {type(exc).__name__}: {exc}", flow_day=fd)
            try:
                card = shadow_card(fd, text, occurrence=occurrence, occ_dir=occ_dir, usage_doc=usage_doc,
                                   agent_id=agent_id or "dream", flow_text=flow_text)
            except (ShadowRefused, _card.CardError, OSError) as exc:
                return NightResult("failed", f"the comparison card could not be built: {exc}", flow_day=fd)
            return NightResult("composed", "", card, fd)
    except ShadowRefused as exc:
        return NightResult("skipped-no-input", f"{_ready.UNREADABLE}/{exc.kind}: {exc.why}", flow_day=fd)
    except OSError as exc:
        return NightResult("skipped-no-input", f"{_ready.UNREADABLE}/{KIND_UNREADABLE}: the shadow directory "
                                               f"{shadow_db.parent} cannot be used ({type(exc).__name__}: {exc})",
                           flow_day=fd)


def shadow_input(flow_root: Path | str, shadow_db: Path | str, row: Mapping[str, Any], *, zone: str,
                 occurrences: Callable[[], Sequence[Mapping[str, Any]]] | None = None, agent_id: str | None = None,
                 on_seeded: Callable[[], None] | None = None):
    """The shadow's input check for one occurrence row, in the dream's ``InputVerdict`` shape (so the same adapter
    that gives the controller its bool applies): day-ready, then seed and the episode check under the night lock. It
    seeds the SCRATCH store only. ``occurrences`` (the shadow's own history) is required, as in :func:`run_night`:
    without it an already-shadowed day cannot be ruled out, so the check answers unreadable. ``on_seeded`` runs
    under the same lock after a seed whose episode check passed; one that raises makes the answer unreadable. Never
    raises."""
    from levain.dream.agent import InputVerdict

    if occurrences is None:
        return InputVerdict(False, _ready.UNREADABLE, KIND_UNREADABLE,
                            "no shadow occurrence history was given, so an already-shadowed day cannot be ruled out")
    me = row.get("occurrence_id")
    try:
        day = _ready.occurrence_dream_day(row, zone)
        full = occurrences
        rows = lambda: [r for r in full() if r.get("occurrence_id") != me]  # noqa: E731
        v = day_ready(flow_root, day, occurrences=rows, zone=zone, agent_id=agent_id)
        if v.ready and v.flow_day is not None:
            try:
                with night_lock(v.flow_day, shadow_db):
                    seed(v.flow_day, shadow_db)
                    v = episode_check(v.flow_day, shadow_db)
                    if v.ready and on_seeded is not None:
                        try:
                            on_seeded()
                        except Exception as exc:  # noqa: BLE001 — a seeded night without its inputs is not ready
                            return InputVerdict(False, _ready.UNREADABLE, KIND_UNREADABLE,
                                                f"after the seed: {type(exc).__name__}: {exc}")
            except ShadowRefused as exc:
                return InputVerdict(False, _ready.UNREADABLE, exc.kind, exc.why)
            except OSError as exc:
                return InputVerdict(False, _ready.UNREADABLE, KIND_UNREADABLE,
                                    f"the shadow directory cannot be used ({type(exc).__name__}: {exc})")
    except Exception as exc:  # noqa: BLE001 — a check that cannot run has not said ready
        return InputVerdict(False, _ready.UNREADABLE, KIND_UNREADABLE, f"{type(exc).__name__}: {exc}")
    return InputVerdict(v.ready, v.verdict, v.kind, v.reason)


__all__ = ["FlowDay", "SourceVerdict", "ShadowRefused", "ShadowDiff", "NightResult", "EpisodeSet", "Seeded",
           "read_flow_day", "verify_snapshot", "day_ready", "seed", "episode_check", "compare_episode_sets",
           "diff_outputs", "shadow_card", "run_night", "night_lock", "shadow_input", "flow_refusals", "package_ids"]
