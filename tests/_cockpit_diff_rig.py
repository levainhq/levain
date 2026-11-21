"""The differential rig for the cockpit read layer: the same inputs read by a bare cockpit (the oracle, the path a page reads
without snapshots) and by one attached to the snapshot layer (the subject), and the differences between them as
``(path, old, new)`` triples that an approved-differences file either explains or leaves standing.

Both cockpits come from ONE ``RigConfig`` so they cannot drift: one install, one injected clock, one set of builder
arguments. The wire form of a value is what a page can see (``json.loads(json.dumps(x, default=str))``), so a dict's key
order never counts and a list's order always does."""
from __future__ import annotations

import dataclasses
import importlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from levain.cockpit import snapshot_host as sh
from levain.cockpit.providers import build_default_cockpit

# The clock of a run is fixed once per pair, an hour past the wall clock when the pair is built (``fixed_clock``). A host's
# writer opens its store on the WALL clock and a row derived before that instant reads stale (a cold start), so a clock
# set in the past would make every row stale for a reason that is the rig's, not the layer's. Past the wall clock, every
# row is derived after the writer opened, and nothing inside a run depends on time passing.
CRED = {"class": "none", "device_class": None}
APPROVED_PATH = Path(__file__).resolve().parent / "cockpit_diff_approved.json"

SCOPES = ("derive", "manifest", "payload", "freshness", "probe")
KINDS = ("extra_key_in_subject", "missing_key_in_subject", "value_rule", "extra_key_with_rule")
MAX_CYCLES = 6
HEALTHY = ("ok", "partial", "empty")      # a derive status that is a read, not a failure
MIN_REASON = 40
PROBE_VERSION = "#version"      # the probe's version string, beside its per-panel etags in the ``probe`` observation


# ------------------------------------------------------------------------------------------------------------ the two cockpits


def fixed_clock() -> datetime:
    return (datetime.now(timezone.utc) + timedelta(hours=1)).replace(second=0, microsecond=0)


@dataclass(frozen=True)
class RigConfig:
    """Everything that makes a cockpit, once. ``install`` builds the on-disk store under a scratch dir and returns the
    ``SubstrateSource`` both cockpits read."""

    name: str
    install: Callable[[Path], Any]
    now: datetime | None = None      # None: ``fixed_clock()`` at build time
    exclude: tuple = ()


@dataclass
class Pair:
    config: RigConfig
    bare: Any
    attached: Any
    host: Any
    derived: list
    levain_home: str | None = None      # LEVAIN_HOME as the pair was built under

    @property
    def reader(self) -> Any:
        return self.host.reader

    def close(self) -> None:
        self.host.stop(timeout=10)


def _cockpit(config: RigConfig, src: Any) -> Any:
    kw: dict = {"clock": lambda: config.now}
    if config.exclude:
        kw["exclude"] = config.exclude
    ck = build_default_cockpit(src, **kw)
    ck.read_cache_ttl_s = 0          # no shared read hides a difference
    return ck


def build_pair(config: RigConfig, tmp_path: Path) -> Pair:
    """The oracle (bare) and the subject (attached to a real host's reader), over ONE install. The host runs on this thread:
    each cycle publishes what the one before it discovered, until a cycle has nothing left to publish. The host is stopped
    on every way out that does not return the pair."""
    if config.now is None:
        config = dataclasses.replace(config, now=fixed_clock())
    install_dir = tmp_path / "install"      # a sibling of the host's directory: neither can see or collide with the other
    install_dir.mkdir()
    src = config.install(install_dir)
    bare, ck = _cockpit(config, src), _cockpit(config, src)
    root = tmp_path / "host"
    root.mkdir()
    host = sh.SnapshotHost(ck, root, clock=config.now.timestamp)
    try:
        host.attach()
        for _ in range(MAX_CYCLES):
            if not host.run_once(config.now.timestamp()) and host.wait_first_discovery(0):
                break
        else:
            raise AssertionError(f"the subject's host still publishing after {MAX_CYCLES} cycles")
        derived = [pid for pid in ck.panel_ids if not ck.serve_composed(pid)]
        return Pair(config, bare, ck, host, derived, os.environ.get("LEVAIN_HOME"))
    except BaseException:
        host.stop(timeout=10)
        raise


# ----------------------------------------------------------------------------------------------------------------- wire form


def wire(value: Any) -> Any:
    """The form a page can see: JSON-plain, key order irrelevant to every reader."""
    return json.loads(json.dumps(value, default=str))


def _raised(exc: BaseException) -> dict:
    return {"raised": " ".join(f"{type(exc).__name__}: {exc}".split())}


def _attempt(fn: Callable[[], Any]) -> Any:
    """A read that raises is a value the diff can show, not a crash that hides every other panel."""
    try:
        return wire(fn())
    except Exception as exc:  # noqa: BLE001 - the difference is the finding
        return _raised(exc)


def raised_paths(value: Any, path: tuple = ()) -> list:
    """Where in a wire-form observation a read raised (the ``{"raised": ...}`` wrapper of ``_attempt``). The same raise on
    both sides compares equal, so a caller that means to read something asks for these and expects none."""
    if isinstance(value, dict):
        if set(value) == {"raised"}:
            return [path]
        return [p for k, v in value.items() for p in raised_paths(v, path + (k,))]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in raised_paths(v, path + (i,))]
    return []


def triple_raised(t: "Triple") -> bool:
    """A difference that is a read raising on one side (or a value holding one), not a difference in what was read."""
    return bool(raised_paths(t.old) or raised_paths(t.new))


# ---------------------------------------------------------------------------------------------------------------- observation


def observe_derive_oracle(pair: Pair) -> dict:
    """``{pid: {status, body, error, valid_until}}``: the old path's own unit, one panel's derive result."""
    pair.bare.manifest(CRED)         # a bare cockpit finds its discovered panels on its first manifest read
    out = {}
    for pid in pair.derived:
        def one(pid=pid):
            res = sh.derive_panel_result(pair.bare, pid, lambda _pid: None)
            return {"status": res.status, "body": res.body, "error": res.error, "valid_until": res.valid_until}
        out[pid] = _attempt(one)
    return out


def observe_derive_subject(pair: Pair) -> dict:
    """The same unit through the snapshot layer: the published row, its body decoded by ``decode_snap`` and encoded again
    (what the head layer builds a payload from, put back into the codec's own shape)."""
    out = {}
    for pid in pair.derived:
        def one(pid=pid):
            row = pair.reader.read(pid)
            if row is None:
                return {"raised": "no published row"}
            body = None if row.body is None else pair.reader.decoded(row)
            snap = sh.decode_snap(row.status, body, as_of=None, error=row.error)
            status, back = sh.encode_snap(snap)
            return {"status": status, "body": back, "error": row.error, "valid_until": row.valid_until}
        out[pid] = _attempt(one)
    return out


def observe_request(ck: Any, derived: list, oracle_rows: dict | None = None) -> dict:
    """What a page reads: the manifest, every panel's whole and compact payload and its first row, and the freshness map.
    ``oracle_rows`` names the row each panel's by-row read asks for, so both sides ask for the same row."""
    manifest = _attempt(lambda: ck.manifest(CRED))
    panels = manifest.get("panels") if isinstance(manifest, dict) else None
    ids = list(ck.panel_ids)
    payload: dict = {}
    rows_asked: dict = {}
    for pid in ids:
        per = {"full": _attempt(lambda pid=pid: ck.panel(pid)),
               "compact": _attempt(lambda pid=pid: ck.panel(pid, profile="compact"))}
        rid = (oracle_rows or {}).get(pid)
        if oracle_rows is None:
            rows = per["full"].get("rows") if isinstance(per["full"], dict) else None
            rid = rows[0].get("id") if rows and isinstance(rows[0], dict) else None
        if rid is not None:
            rows_asked[pid] = rid
            per[f"row:{rid}"] = _attempt(lambda pid=pid, rid=rid: ck.panel(pid, row=rid))
        payload[pid] = per
    out = {"manifest": manifest, "payload": payload, "freshness": _attempt(ck.freshness),
           "_rows_asked": rows_asked,
           "_panel_order": list(panels) if isinstance(panels, dict) else None}
    try:
        probe = ck.change_probe()
    except Exception as exc:  # noqa: BLE001 - a probe that raises is a triple, not a crash of every dependent test
        probe, out["probe"] = None, _raised(exc)
    if probe is not None:
        def read_probe() -> dict:
            etags, version = probe.snapshot()
            return {**{pid: {"etag": tag} for pid, tag in etags.items()}, PROBE_VERSION: version}
        out["probe"] = _attempt(read_probe)
    return out


# ------------------------------------------------------------------------------------------------------------------- the diff


@dataclass(frozen=True)
class Triple:
    path: tuple
    kind: str                # "value", "extra_key_in_subject" or "missing_key_in_subject"
    old: Any
    new: Any

    @property
    def scope(self) -> str:
        return self.path[0]

    @property
    def panel(self) -> str:
        """The panel id a path is about, or ``-`` for a path about the whole manifest."""
        p = self.path
        if p[0] in ("derive", "payload", "freshness", "probe") and len(p) > 1 and not p[1].startswith("#"):
            return p[1]
        if p[0] == "manifest" and len(p) > 2 and p[1] == "panels":
            return p[2]
        return "-"

    def dotted(self) -> str:
        return ".".join(str(s) for s in self.path)


def diff(old: Any, new: Any, path: tuple = ()) -> list:
    """Every difference between two wire-form values as triples. Dicts are compared by key (never by order), lists by
    position and length (order is semantic), scalars by value."""
    if isinstance(old, dict) and isinstance(new, dict):
        out = []
        for k in sorted(set(old) | set(new), key=str):
            if k not in new:
                out.append(Triple(path + (k,), "missing_key_in_subject", old[k], None))
            elif k not in old:
                out.append(Triple(path + (k,), "extra_key_in_subject", None, new[k]))
            else:
                out.extend(diff(old[k], new[k], path + (k,)))
        return out
    if isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            return [Triple(path, "value", old, new)]
        out = []
        for i, (a, b) in enumerate(zip(old, new)):
            out.extend(diff(a, b, path + (i,)))
        return out
    if type(old) is not type(new) or old != new:
        if isinstance(old, float) and isinstance(new, float) and math.isnan(old) and math.isnan(new):
            return []
        return [Triple(path, "value", old, new)]
    return []


def compare(pair: Pair) -> list:
    """Every triple between oracle and subject, derive layer and request layer."""
    out = diff({"derive": observe_derive_oracle(pair)}, {"derive": observe_derive_subject(pair)})
    oracle = observe_request(pair.bare, pair.derived)
    subject = observe_request(pair.attached, pair.derived, oracle["_rows_asked"])
    order = Triple(("manifest", "panels", "#order"), "value", oracle.pop("_panel_order"), subject.pop("_panel_order"))
    oracle.pop("_rows_asked"), subject.pop("_rows_asked")
    # the probe is the subject's own surface (a bare cockpit has none): its etags are held against the subject's own manifest
    # etags (the oracle's differ by design), and its version has no oracle, so it is held to be text here (that it is
    # stable across reads is ``self_diff``'s; that it moves with the state is DIFF-2's, levain#245)
    probe = subject.pop("probe", None)
    out.extend(diff(oracle, subject))
    if order.old != order.new:
        out.append(order)
    if probe is not None:
        version = probe.pop(PROBE_VERSION, None) if isinstance(probe, dict) else None
        if isinstance(probe, dict) and set(probe) != {"raised"} and not (isinstance(version, str) and version):
            out.append(Triple(("probe", PROBE_VERSION), "value", "non-empty text", version))
        heads = subject["manifest"].get("panels") if isinstance(subject["manifest"], dict) else None
        mine = {pid: {"etag": h.get("etag") if isinstance(h, dict) else None}
                for pid, h in (heads if isinstance(heads, dict) else {}).items()}
        if isinstance(probe, dict) and set(probe) == {"raised"}:
            out.append(Triple(("probe",), "value", mine, probe))      # one triple holding the raise, not its keys
        else:
            out.extend(diff({"probe": mine}, {"probe": probe}))
    return out


def self_diff(pair: Pair, side: str = "bare") -> list:
    """One side against itself: the same cockpit read twice (``side`` is "bare", the oracle, or "attached", the subject). A
    path that differs here is an input the harness has not fixed, never a difference to approve."""
    ck = pair.bare if side == "bare" else pair.attached
    derive = observe_derive_oracle if side == "bare" else observe_derive_subject

    def read() -> dict:
        return {"derive": derive(pair), **observe_request(ck, pair.derived)}

    return diff(read(), read())


# ---------------------------------------------------------------------------------------------------------- the approved list


_HEX64 = re.compile(r"[0-9a-f]{64}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _is_etag(old: Any, new: Any) -> bool:
    """Both sides are an etag (a sha256 hex digest). They differ between a row-built and a content-built head by design, so
    equality is not the rule for the panels an entry covers."""
    return isinstance(old, str) and isinstance(new, str) and bool(_HEX64.fullmatch(old)) and bool(_HEX64.fullmatch(new))


def _etag_rule(t: "Triple", now: datetime | None, derived: Any = None) -> bool:
    """The etag of a panel the oracle derived (``derived``: its ids) or of the manifest as a whole. A serve-composed panel
    is read the same way by both cockpits, so for it an etag that differs is never explained; with no set given, no
    panel's is."""
    return _is_etag(t.old, t.new) and (t.panel == "-" or (derived is not None and t.panel in derived))


def _iso_not_before(value: Any, now: datetime | None) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or now is None or now.tzinfo is None:      # a naive clock cannot be compared: closed
        return False
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return at.tzinfo is not None and at >= now


# the keys the engine's manifest freshness block carries (``Cockpit._manifest_compute``); a block with more, fewer or
# malformed members is a change to what a page reads, so it is not approved by its two headline members
FRESH_BLOCK_KEYS = frozenset({"worker", "heartbeat_age_s", "deriving", "stream"})


def _fresh_block_at_rest(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != FRESH_BLOCK_KEYS:
        return False
    age, stream = value["heartbeat_age_s"], value["stream"]
    return (value["worker"] == "live" and value["deriving"] == []
            and isinstance(age, (int, float)) and not isinstance(age, bool) and math.isfinite(age) and age >= 0
            and isinstance(stream, dict) and isinstance(stream.get("state"), str))


def _fresh_at_rest(t: "Triple", now: datetime | None, derived: Any = None) -> bool:
    """The value of a freshness key the subject adds, for a corpus read at rest (every row derived at the run's clock, the
    worker alive, nothing in flight): a panel is not stale and not pending, its bound is absent or not before the clock, and
    the layer behind the page reports a live worker with nothing deriving. A ``valid_until`` of None is accepted for every
    panel: it is None by design for a panel whose provider declares no bound (``_stamp_fresh`` copies the row's, which has
    one only when the provider set it, and ``_stamp_unrowed`` sets none), and the corpora declare none."""
    key, value = t.path[-1], t.new
    if key in ("stale", "pending"):
        return value is False
    if key == "valid_until":
        return _iso_not_before(value, now)
    if key == "freshness":
        return _fresh_block_at_rest(value)
    return False


RULES: dict = {"etag_is_a_tag": _etag_rule, "fresh_at_rest": _fresh_at_rest}
VALUE_KINDS = ("value_rule", "extra_key_with_rule")


def resolve_symbol(dotted: str) -> Any:
    """The object a ``module.Class.attr`` name points at; raises when any part is gone."""
    parts = dotted.split(".")
    for i in range(len(parts) - 1, 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:i]))
        except ImportError:
            continue
        for name in parts[i:]:
            obj = getattr(obj, name)
        return obj
    raise ImportError(dotted)


def source_symbols(source: str) -> list:
    """The dotted names in an entry's ``source``: comma separated, any parenthetical words dropped."""
    return [n.strip() for n in re.sub(r"\([^)]*\)", "", source).split(",") if n.strip()]


def load_approved(path: Path = APPROVED_PATH) -> list:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def entry_problems(entries: Any) -> list:
    """What is wrong with an approved list, as readable lines (empty when it is sound). A reason is mandatory."""
    if not isinstance(entries, list):
        return ["the approved list is a JSON list"]
    problems: list = []
    seen: set = set()
    for i, e in enumerate(entries):
        who = e.get("id", f"#{i}") if isinstance(e, dict) else f"#{i}"
        if not isinstance(e, dict):
            problems.append(f"{who}: an entry is an object")
            continue
        if not isinstance(e.get("id"), str) or not e["id"].startswith("AD-"):
            problems.append(f"{who}: id is text starting AD-")
        elif e["id"] in seen:
            problems.append(f"{who}: id repeated")
        else:
            seen.add(e["id"])
        reason = e.get("reason")
        if not isinstance(reason, str) or len(reason.strip()) < MIN_REASON:
            problems.append(f"{who}: reason is missing, empty or shorter than {MIN_REASON} characters")
        if not isinstance(e.get("source"), str) or not e["source"].strip():
            problems.append(f"{who}: source is missing")
        if e.get("scope") not in SCOPES:
            problems.append(f"{who}: scope not in {SCOPES}")
        if not isinstance(e.get("path"), str) or not e["path"]:
            problems.append(f"{who}: path is missing")
        elif not any(seg not in ("*", "**") for seg in e["path"].split(".")):
            problems.append(f"{who}: path has no literal segment, so it would match everything in its scope")
        if e.get("kind") not in KINDS:
            problems.append(f"{who}: kind not in {KINDS}")
        elif e["kind"] in VALUE_KINDS:
            rule = e.get("rule")
            if not isinstance(rule, str) or rule not in RULES:      # a list or dict is not hashable: text first
                problems.append(f"{who}: rule {rule!r} is not in the registry")
        elif "rule" in e:      # matching ignores it, so it would read as a check that never runs
            problems.append(f"{who}: kind {e['kind']!r} does not take a rule")
        if not isinstance(e.get("owner"), str) or not e["owner"].strip():
            problems.append(f"{who}: owner is a non-empty text")
        dates = {}
        for f in ("added", "review_by"):
            raw = e.get(f)
            try:      # text of exactly that form: fromisoformat alone takes other forms and, via str(), non-text
                if not isinstance(raw, str) or not _DATE.fullmatch(raw):
                    raise ValueError(raw)
                dates[f] = date.fromisoformat(raw)
            except ValueError:
                problems.append(f"{who}: {f} is a YYYY-MM-DD date")
        if len(dates) == 2 and dates["review_by"] <= dates["added"]:
            problems.append(f"{who}: review_by is later than added")
    return problems


def glob_match(pattern: str, path: tuple) -> bool:
    """``*`` is one path segment, ``**`` any run of segments (including none); ``a|b`` is either name; every other segment
    matches itself."""
    segs = [str(s) for s in path]
    pat = pattern.split(".")

    def go(pi: int, si: int) -> bool:
        if pi == len(pat):
            return si == len(segs)
        if pat[pi] == "**":
            return any(go(pi + 1, k) for k in range(si, len(segs) + 1))
        return si < len(segs) and (pat[pi] == "*" or segs[si] in pat[pi].split("|")) and go(pi + 1, si + 1)

    return go(0, 0)


def entry_matches(entry: dict, t: Triple, now: datetime | None = None, derived: Any = None) -> bool:
    if entry["scope"] != t.scope or not glob_match(entry["path"], t.path[1:]):
        return False
    if entry["kind"] == "value_rule":
        return t.kind == "value" and bool(RULES[entry["rule"]](t, now, derived))
    if entry["kind"] == "extra_key_with_rule":
        return t.kind == "extra_key_in_subject" and bool(RULES[entry["rule"]](t, now, derived))
    return entry["kind"] == t.kind


@dataclass
class Verdict:
    unmatched: list = field(default_factory=list)
    matched: dict = field(default_factory=dict)      # entry id -> how many triples it explained


def classify(triples: list, entries: list, now: datetime | None = None, derived: Any = None) -> Verdict:
    """``now`` is the run's clock and ``derived`` the ids of the panels the oracle derived (``Pair.derived``), for the rules
    that compare a value with the clock or are about those panels only."""
    v = Verdict()
    for t in triples:
        hit = next((e for e in entries if entry_matches(e, t, now, derived)), None)
        if hit is None:
            v.unmatched.append(t)
        else:
            v.matched[hit["id"]] = v.matched.get(hit["id"], 0) + 1
    return v


def _short(x: Any, n: int = 200) -> str:
    s = json.dumps(x, default=str, sort_keys=True)
    return s if len(s) <= n else s[: n - 3] + "..."


def render(t: Triple) -> str:
    return f"{t.dotted()} [{t.kind}] oracle={_short(t.old)} subject={_short(t.new)}"
