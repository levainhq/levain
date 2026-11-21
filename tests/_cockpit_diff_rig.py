"""The differential rig for the cockpit read layer: the same inputs read by a bare cockpit (the oracle, the path a page reads
without snapshots) and by one attached to the snapshot layer (the subject), and the differences between them as
``(path, old, new)`` triples that an approved-differences file either explains or leaves standing.

Both cockpits come from ONE ``RigConfig`` so they cannot drift: one install, one injected clock, one set of builder
arguments. The wire form of a value is what a page can see (``json.loads(json.dumps(x, default=str))``), so a dict's key
order never counts and a list's order always does."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from levain.cockpit import snapshot_host as sh
from levain.cockpit.providers import build_default_cockpit

# The one clock of a run. A host opens its store with the wall clock for the instant its worker started, and a row derived
# before that instant reads stale (a cold start), so a clock set in the past would make every row stale for a reason that
# is the rig's, not the layer's. T0 is therefore fixed once, an hour past the wall clock at import: every row is derived
# after the worker started, and nothing in a run depends on the time passing.
T0 = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(second=0, microsecond=0)
CRED = {"class": "none", "device_class": None}
APPROVED_PATH = Path(__file__).resolve().parent / "cockpit_diff_approved.json"

SCOPES = ("derive", "manifest", "payload", "freshness", "probe")
KINDS = ("extra_key_in_subject", "missing_key_in_subject", "value_rule")
OWNERS = ("levain-cockpit", "levain-speed", "flow-bridge")
MAX_CYCLES = 6
MIN_REASON = 40
BANNED_REASONS = frozenset({"todo", "known", "tbd", "flaky", "fixme", "wip"})


# ------------------------------------------------------------------------------------------------------------ the two cockpits


@dataclass(frozen=True)
class RigConfig:
    """Everything that makes a cockpit, once. ``install`` builds the on-disk store under a scratch dir and returns the
    ``SubstrateSource`` both cockpits read."""

    name: str
    install: Callable[[Path], Any]
    now: datetime = T0
    exclude: tuple = ()


@dataclass
class Pair:
    config: RigConfig
    bare: Any
    attached: Any
    host: Any
    derived: list

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
    its first cycle does discovery and the entity, and the cycles after it derive what discovery found, until a cycle has
    nothing left to publish. Nothing runs on a thread of its own, so the clock is the only time there is."""
    src = config.install(tmp_path)
    bare, ck = _cockpit(config, src), _cockpit(config, src)
    root = tmp_path / "host"
    root.mkdir()
    host = sh.SnapshotHost(ck, root, clock=config.now.timestamp)
    host.attach()
    for _ in range(MAX_CYCLES):
        if not host.run_once(config.now.timestamp()) and host.wait_first_discovery(0):
            break
    else:
        host.stop(timeout=10)
        raise AssertionError(f"the subject's host still publishing after {MAX_CYCLES} cycles")
    derived = [pid for pid in ck.panel_ids if not ck.serve_composed(pid)]
    return Pair(config, bare, ck, host, derived)


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
    probe = ck.change_probe()
    out = {"manifest": manifest, "payload": payload, "freshness": _attempt(ck.freshness),
           "_rows_asked": rows_asked,
           "_panel_order": list(panels) if isinstance(panels, dict) else None}
    if probe is not None:
        out["probe"] = _attempt(lambda: {pid: {"etag": tag} for pid, tag in probe.snapshot()[0].items()})
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
        if p[0] in ("derive", "payload", "freshness", "probe") and len(p) > 1:
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
    # the probe is the subject's own surface (a bare cockpit has none): its etags are held against the oracle's manifest etags
    probe = subject.pop("probe", None)
    out.extend(diff(oracle, subject))
    if order.old != order.new:
        out.append(order)
    if probe is not None:
        mine = {pid: {"etag": (h or {}).get("etag")} for pid, h in (subject["manifest"].get("panels") or {}).items()}
        out.extend(diff({"probe": mine}, {"probe": probe}))
    return out


def self_diff(pair: Pair) -> list:
    """The ORACLE against itself: the same cockpit read twice. A path that differs here is an input the harness has not
    fixed, never a difference to approve."""
    a = {"derive": observe_derive_oracle(pair), **{k: v for k, v in observe_request(pair.bare, pair.derived).items()}}
    b = {"derive": observe_derive_oracle(pair), **{k: v for k, v in observe_request(pair.bare, pair.derived).items()}}
    return diff(a, b)


# ---------------------------------------------------------------------------------------------------------- the approved list


def _is_etag(old: Any, new: Any) -> bool:
    """Both sides are an etag: a non-empty text. They differ by construction (the attached one hashes the row's body hash
    and its freshness, the bare one the content), so equality is not the rule; two panels of different content must still
    not share one, which the test file holds separately."""
    return isinstance(old, str) and isinstance(new, str) and bool(old) and bool(new)


RULES: dict = {"etag_is_a_tag": _is_etag}


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
        if not isinstance(reason, str) or len(reason.strip()) < MIN_REASON \
                or reason.strip().lower().rstrip(".") in BANNED_REASONS:
            problems.append(f"{who}: reason is missing, empty, too short or a banned word")
        if not isinstance(e.get("source"), str) or not e["source"].strip():
            problems.append(f"{who}: source is missing")
        if e.get("scope") not in SCOPES:
            problems.append(f"{who}: scope not in {SCOPES}")
        if not isinstance(e.get("path"), str) or not e["path"]:
            problems.append(f"{who}: path is missing")
        if e.get("kind") not in KINDS:
            problems.append(f"{who}: kind not in {KINDS}")
        elif e["kind"] == "value_rule" and e.get("rule") not in RULES:
            problems.append(f"{who}: rule {e.get('rule')!r} is not in the registry")
        if e.get("owner") not in OWNERS:
            problems.append(f"{who}: owner not in {OWNERS}")
        for f in ("added", "review_by"):
            try:
                datetime.strptime(str(e.get(f)), "%Y-%m-%d")
            except ValueError:
                problems.append(f"{who}: {f} is a YYYY-MM-DD date")
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


def entry_matches(entry: dict, t: Triple) -> bool:
    if entry["scope"] != t.scope or not glob_match(entry["path"], t.path[1:]):
        return False
    if entry["kind"] == "value_rule":
        return t.kind == "value" and bool(RULES[entry["rule"]](t.old, t.new))
    return entry["kind"] == t.kind


@dataclass
class Verdict:
    unmatched: list = field(default_factory=list)
    matched: dict = field(default_factory=dict)      # entry id -> how many triples it explained


def classify(triples: list, entries: list) -> Verdict:
    v = Verdict()
    for t in triples:
        hit = next((e for e in entries if entry_matches(e, t)), None)
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
