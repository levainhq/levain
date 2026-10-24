"""The dispatch route maps (S1 3c-4; 3c design r3 §2.3 as R4 §2 amends it, paragraph by paragraph).

A route map is one `levain-dispatch-config/1` item of kind `routemap`, signed by the operator's configuration key in the
SSHSIG namespace `levain-dispatch-config`. It is identified by `(format, store_id, generation, item_sha256)`, where
`item_sha256` is the SHA-256 of the item's canonical bytes, the same bytes the signature covers (the identity rule). The
pinned values come from root: `L/routes/trust.pub` (the configuration key's public line) and `L/routes/trust.json` =
`{format: levain-routes-trust/1, store_id, fingerprint}`.

Two holders, one checker (:func:`check_item`, the one place an item is judged):

- CONTROL (:class:`ControlRoutes`) accepts `ROUTE-SUBMIT` against ITS OWN head `L/routes/dispatch/head` =
  `{generation, item_sha256, store_id}`, never against "the active file". Activation writes `<gen>.json` + `<gen>.sig`,
  then `head`, then `current`, then hands the head to keys (`RELOAD`). Its start pass FINISHES an interrupted
  activation (the head re-verified, `current` repaired, orphans newer than the head deleted). Both run under the ONE
  activation lock (:attr:`ControlRoutes.lock`, a `threading.Lock`: CONTROL serves each connection on its own thread).
- keys (:class:`KeysRoutes`) keeps its own trusted head `L/keys/routemap.head` = `{format: levain-routemap-head/1,
  store_id, generation, item_sha256, served: [{generation, item_sha256}]}` and re-runs every check itself on `RELOAD`
  and at start, writing its head durably BEFORE it serves a new generation or acks. A RELOAD keys refuses leaves every
  dispatch route refused until a valid one (TUF's rule: never replace a trusted version with a lower one).

Pins (R4 §2 "Pins"): a ticketed session is served its ticket's generation P for its life iff (1) P is in keys' `served`
list and keys verified it itself, (2) the route id is in the HEAD generation (re-checked on every `Q`), (3) SHA-256 of
P's entry `policy` == the ticket's digest. No grace, no follow, no revocation flag.

Runs on the root interpreter (3.9).
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from levain.authority_sig import VerifierUnavailable, verify_link_sig
from levain.broker.policy import DispatchRoute, PolicyError, RoutePolicy, body_policy_problem, parse_routemap
from levain.contracts import ContractError, canon
from levain.contracts.grammar import JSON_INT_MAX
from levain.dispatch.routemap_rules import envelope_problem
from levain.slot import Refused
from levain.slot.records import write_durably

CONFIG_FORMAT = "levain-dispatch-config/1"
CONFIG_NAMESPACE = "levain-dispatch-config"
KIND = "routemap"
#: an item's keys, exactly (dispatch's `levain.dispatch.control.ITEM_KEYS`; a test pins the two equal)
ITEM_KEYS = frozenset({"format", "store_id", "generation", "prev", "issued_at", "kind", "body"})
#: bounds checked before anything is parsed or verified (dispatch's `ITEM_MAX_BYTES` / `SIGNATURE_MAX`; a test pins them)
ITEM_MAX_BYTES = 1024 * 1024
SIGNATURE_MAX = 64 * 1024
TRUST_FORMAT = "levain-routes-trust/1"
KEYS_HEAD_FORMAT = "levain-routemap-head/1"
TRUST_PUB, TRUST_JSON = "trust.pub", "trust.json"
DISPATCH_DIR, HEAD, CURRENT = "dispatch", "head", "current"
#: keys' trusted head, under the lib root
KEYS_HEAD = ("keys", "routemap.head")
#: the most a trust, head or `current` file may hold (DECISION: far above a real one; a larger file is refused)
SMALL_FILE_MAX = 64 * 1024
#: what keys reads of `L/routes/base.json` for the collision rule (keys' own `BASE_MAP_CAP`)
BASE_MAP_MAX = 1024 * 1024
#: the fault points of an activation, in the order they are passed (the fault-gate tests SIGKILL at each)
FAULT_POINTS = ("before_head", "after_head", "after_current")

_STORE_ID = re.compile(r"[0-9a-f]{32}\Z")
_FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}\Z")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
_POLICY_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GEN_FILE = re.compile(r"([1-9][0-9]{0,15})\.(json|sig)\Z")

Verify = Callable[..., bool]


@dataclass(frozen=True)
class Trust:
    public_key: str
    fingerprint: str
    store_id: str


@dataclass(frozen=True)
class Checked:
    """An item that passed every check: its generation, its digest, its routes and each route's policy digest."""
    generation: int
    item_sha256: str
    store_id: str
    routes: Mapping[str, DispatchRoute]
    digests: Mapping[str, str]


@dataclass(frozen=True)
class Pin:
    """What a ticketed session is pinned to (its ticket's `routemap_generation` and `route_policy_digest`)."""
    generation: int
    policy_digest: str


def item_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def policy_digest(policy: object) -> str:
    """CONTROL's and keys' one digest of a route entry's `policy` object: `sha256:` + the hex SHA-256 of its canonical
    bytes (the ticket's `route_policy_digest`)."""
    return "sha256:" + hashlib.sha256(canon.canonical_bytes(policy)).hexdigest()


def pin_of(claims: Mapping[str, object]) -> Optional[Pin]:
    """The pin a session's claims carry (`run.routemap_generation` + `run.route_policy_digest`, CONTRACT §3.3
    TOKEN-RESOLVE), or None: a session with no ticket is pinned to nothing and is served no dispatch route."""
    run = claims.get("run")
    if not isinstance(run, dict):
        return None
    gen, dig = run.get("routemap_generation"), run.get("route_policy_digest")
    if type(gen) is not int or not 1 <= gen <= JSON_INT_MAX or type(dig) is not str or not _POLICY_DIGEST.match(dig):
        return None
    return Pin(gen, dig)


# --- small files ------------------------------------------------------------------------------------------------

def _read(path: Path, cap: int) -> Optional[bytes]:
    """`path`'s bytes, None when absent; a link, a non-regular file or one past `cap` raises OSError."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(fd)
        if not (st.st_mode & 0o170000) == 0o100000:
            raise OSError(f"{path} is not a regular file")
        out = b""
        while len(out) <= cap:
            part = os.read(fd, cap + 1 - len(out))
            if not part:
                break
            out += part
        if len(out) > cap:
            raise OSError(f"{path} is over {cap} bytes")
        return out
    finally:
        os.close(fd)


def _canon_obj(data: bytes, what: str, refusal: str) -> dict:
    """`data` parsed as one canonical JSON object, else `Refused(refusal)`."""
    try:
        obj = canon.parse(data)
    except (ContractError, ValueError, RecursionError):
        raise Refused(refusal, f"{what} does not parse") from None
    if not isinstance(obj, dict) or canon.canonical_bytes(obj) != data:
        raise Refused(refusal, f"{what} is not one canonical object")
    return obj


def _gen(v: object) -> bool:
    return type(v) is int and 1 <= v <= JSON_INT_MAX


def read_trust(routes_dir: Path) -> Trust:
    """`trust.pub` + `trust.json`, written by root (trust.json LAST, so finding both finds a matched pair). Absent or
    malformed = `routemap-verifier`: a fault on this machine, never a verdict on an item."""
    try:
        pub = _read(Path(routes_dir) / TRUST_PUB, SMALL_FILE_MAX)
        doc = _read(Path(routes_dir) / TRUST_JSON, SMALL_FILE_MAX)
    except OSError as e:
        raise Refused("routemap-verifier", f"trust unreadable ({type(e).__name__})") from None
    if pub is None or doc is None:
        raise Refused("routemap-verifier", "no route trust (trust.pub + trust.json)")
    try:
        line = pub.decode("ascii").strip()
    except UnicodeDecodeError:
        raise Refused("routemap-verifier", "trust.pub is not ASCII") from None
    if not line or "\n" in line:
        raise Refused("routemap-verifier", "trust.pub is one public key line")
    t = _canon_obj(doc, "trust.json", "routemap-verifier")
    if (set(t) != {"format", "store_id", "fingerprint"} or t["format"] != TRUST_FORMAT
            or not isinstance(t["store_id"], str) or not _STORE_ID.match(t["store_id"])
            or not isinstance(t["fingerprint"], str) or not _FINGERPRINT.match(t["fingerprint"])):
        raise Refused("routemap-verifier", f"trust.json is exactly {{format: {TRUST_FORMAT}, store_id, fingerprint}}")
    return Trust(line, t["fingerprint"], t["store_id"])


def base_route_ids(routes_dir: Path) -> frozenset:
    """The route ids `L/routes/base.json` names, for the collision rule (no dispatch id may be a base id). A base map
    that cannot be read as `{format, routes: [..]}` refuses `config-invalid:base`: no collision can then be ruled
    out. Only the ids are read here; keys parses the base map whole on its own `Q` path."""
    return frozenset(base_route_types(routes_dir))


def base_route_types(routes_dir: Path) -> Dict[str, object]:
    """Each route id `L/routes/base.json` names, with its `type` as written (a ticket's `egress_routes` must each be
    an `egress` one, CONTRACT §3.5). Read as :func:`base_route_ids` reads the file, with its refusals."""
    try:
        data = _read(Path(routes_dir) / "base.json", BASE_MAP_MAX)
    except OSError:
        data = None
    if data is None:
        raise Refused("config-invalid:base", "base.json is unreadable")
    try:
        doc = canon.parse(data)
    except (ContractError, ValueError, RecursionError):
        raise Refused("config-invalid:base", "base.json does not parse") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("routes"), list):
        raise Refused("config-invalid:base", "base.json has no routes list")
    return {r["route"]: r.get("type") for r in doc["routes"] if isinstance(r, dict) and isinstance(r.get("route"), str)}


# --- the one checker -----------------------------------------------------------------------------------------------

def check_item(raw: bytes, sig: object, trust: Trust, base_ids: frozenset, *,
               verify: Optional[Verify] = None) -> Checked:
    """Every check an item must pass at CONTROL and again at keys (r3 §2.3 accept steps 1, 2 and 4, as R4 §2 strikes
    step 4's per-call cap), in this order: the size bounds; the signature against `trust` in `CONFIG_NAMESPACE`
    (`routemap-verifier` when the verifier cannot run, `routemap-signature` when it says no); the item's canonical
    form and keys; `format` and `kind`; `store_id` == trust's (`routemap-store`); the generation; the envelope's
    `prev` and `issued_at` (`levain.dispatch.routemap_rules.envelope_problem`, the producer's own check: `parse_item`
    calls it too, so this accepts exactly what dispatch emits); the body
    (`levain.broker.policy.parse_routemap`: every route brokered with a codec and a price on every model, no per-call
    cap, no session cap, no id twice); each route's `body_policy` (`route-invalid:body_policy`, head ruling H2); no route
    id that is a base id. The generation order is the caller's, against
    its own head."""
    if not isinstance(raw, bytes) or len(raw) > ITEM_MAX_BYTES:
        raise Refused("routemap-invalid:item", f"an item is at most {ITEM_MAX_BYTES} bytes")
    if not isinstance(sig, str) or not sig or len(sig) > SIGNATURE_MAX:
        raise Refused("routemap-signature", "no signature")
    try:
        good = (verify or verify_link_sig)(raw, sig, public_key=trust.public_key, fingerprint=trust.fingerprint,
                                           namespace=CONFIG_NAMESPACE)
    except VerifierUnavailable as e:
        raise Refused("routemap-verifier", str(e)) from None
    if good is not True:
        raise Refused("routemap-signature", "the signature does not verify against trust.pub")
    obj = _canon_obj(raw, "the item", "routemap-invalid:item")
    if set(obj) != ITEM_KEYS:
        raise Refused("routemap-invalid:item", f"an item is exactly {sorted(ITEM_KEYS)}")
    if obj["format"] != CONFIG_FORMAT:
        raise Refused("routemap-invalid:format", f"an item is {CONFIG_FORMAT}")
    if obj["kind"] != KIND:
        raise Refused("routemap-invalid:kind", f"the item's kind is not {KIND}")
    if obj["store_id"] != trust.store_id:
        raise Refused("routemap-store", "the item was signed for another store")
    if not _gen(obj["generation"]):
        raise Refused("routemap-invalid:generation", "generation is an integer from 1")
    problem = envelope_problem(obj["generation"], obj["prev"], obj["issued_at"])
    if problem is not None:
        raise Refused("routemap-invalid:envelope", problem)
    try:
        routes = parse_routemap(obj["body"])
    except PolicyError as e:
        raise Refused("routemap-invalid:body", str(e)) from None
    except (RecursionError, TypeError, ValueError) as e:
        raise Refused("routemap-invalid:body", type(e).__name__) from None
    for r in routes:
        # head ruling H2: a body_policy is judged here, at ROUTE-SUBMIT (and keys' own load), never at request time
        problem = body_policy_problem(r.policy, r.body_policy)
        if problem is not None:
            raise Refused("route-invalid:body_policy", problem)
    hit = sorted({r.policy.route for r in routes} & set(base_ids))
    if hit:
        raise Refused("routemap-invalid:route", f"route id {hit[0]} is a base route")
    by_id = {r.policy.route: r for r in routes}
    digests = {raw_r["policy"]["route"]: policy_digest(raw_r["policy"]) for raw_r in obj["body"]["routes"]}
    return Checked(obj["generation"], item_digest(raw), obj["store_id"], by_id, digests)


def _head_obj(data: bytes, *, keys: bool) -> dict:
    """A parsed head file: CONTROL's `{generation, item_sha256, store_id}` or keys' `levain-routemap-head/1`."""
    h = _canon_obj(data, "the head", "routemap-head-unverifiable")
    want = {"format", "store_id", "generation", "item_sha256", "served"} if keys else \
        {"generation", "item_sha256", "store_id"}
    if (set(h) != want or not _gen(h["generation"]) or not isinstance(h["item_sha256"], str)
            or not _SHA256_HEX.match(h["item_sha256"]) or not isinstance(h["store_id"], str)
            or not _STORE_ID.match(h["store_id"])):
        raise Refused("routemap-head-unverifiable", "the head's fields")
    if keys:
        if h["format"] != KEYS_HEAD_FORMAT or not isinstance(h["served"], list):
            raise Refused("routemap-head-unverifiable", "the keys head's format or served list")
        gens = []
        for e in h["served"]:
            if (not isinstance(e, dict) or set(e) != {"generation", "item_sha256"} or not _gen(e["generation"])
                    or not isinstance(e["item_sha256"], str) or not _SHA256_HEX.match(e["item_sha256"])):
                raise Refused("routemap-head-unverifiable", "a served entry")
            gens.append(e["generation"])
        if gens != sorted(set(gens)) or not gens or gens[-1] != h["generation"] \
                or h["served"][-1]["item_sha256"] != h["item_sha256"]:
            raise Refused("routemap-head-unverifiable", "served is ascending and ends at the head")
    return h


def _gen_paths(dispatch: Path, gen: int) -> Tuple[Path, Path]:
    return dispatch / f"{gen}.json", dispatch / f"{gen}.sig"


def _load_generation(routes_dir: Path, gen: int, sha: str, trust: Trust, base: frozenset,
                     verify: Optional[Verify]) -> Checked:
    """Generation `gen`'s files, re-hashed against `sha` and re-checked whole. A missing or truncated file, or one
    whose digest differs, is `routemap-invalid:item_sha256`."""
    jp, sp = _gen_paths(Path(routes_dir) / DISPATCH_DIR, gen)
    try:
        raw, sig = _read(jp, ITEM_MAX_BYTES), _read(sp, SIGNATURE_MAX)
    except OSError:
        raw = sig = None
    if raw is None or sig is None:
        raise Refused("routemap-invalid:item_sha256", f"generation {gen}'s files are missing")
    if item_digest(raw) != sha:
        raise Refused("routemap-invalid:item_sha256", f"generation {gen}'s file is not the one recorded")
    try:
        sig_text = sig.decode("ascii")
    except UnicodeDecodeError:
        raise Refused("routemap-signature", "the signature file is not ASCII") from None
    c = check_item(raw, sig_text, trust, base, verify=verify)
    if c.generation != gen:
        raise Refused("routemap-invalid:generation", f"{gen}.json holds generation {c.generation}")
    return c


def _read_current(dispatch: Path) -> Optional[int]:
    try:
        data = _read(dispatch / CURRENT, SMALL_FILE_MAX)
    except OSError:
        return None
    if data is None or not re.fullmatch(rb"[1-9][0-9]{0,15}", data):
        return None
    return int(data)


# --- CONTROL's half ----------------------------------------------------------------------------------------------

def _nothing(_point: str) -> None:
    return None


class ControlRoutes:
    """CONTROL's route maps over `routes_dir` (`L/routes`). `fault(point)` is the fault-gate tests' seam, called at
    each of `FAULT_POINTS` during an activation; `verify` replaces the SSHSIG verifier (tests use the real one)."""

    def __init__(self, routes_dir: Path, *, verify: Optional[Verify] = None,
                 fsync: Callable[[int], None] = os.fsync, fault: Callable[[str], None] = _nothing) -> None:
        self.dir = Path(routes_dir)
        self.dispatch = self.dir / DISPATCH_DIR
        self._verify = verify
        self._fsync = fsync
        self.fault = fault
        #: THE activation lock (R4 §2): ROUTE-SUBMIT from its head read through `current` and the RELOAD result, the
        #: start pass, and the attach-time RELOAD each hold it.
        self.lock = threading.Lock()
        self._active: Optional[Checked] = None
        #: the doctor row's name while the head does not verify (`routemap-head-unverifiable`), else None, and why
        self.problem: Optional[str] = None
        self.problem_detail = ""

    def head(self) -> Optional[Dict[str, object]]:
        """`{generation, item_sha256}` of the verified head, for keys; None before the first map or while the head
        does not verify (every dispatch route then refused)."""
        a = self._active
        return None if a is None else {"generation": a.generation, "item_sha256": a.item_sha256}

    def active(self) -> Optional[Checked]:
        return self._active

    def _read_head(self) -> Optional[dict]:
        try:
            data = _read(self.dispatch / HEAD, SMALL_FILE_MAX)
        except OSError:
            raise Refused("routemap-head-unverifiable", "the head is unreadable") from None
        return None if data is None else _head_obj(data, keys=False)

    def _write(self, path: Path, data: bytes) -> None:
        write_durably(path, data, mode=0o640, fsync=self._fsync)

    def start_pass(self) -> None:
        """r3 §2.3 "CONTROL's start pass", in this order (an assertion): (1) read the head and verify its files
        exactly as ROUTE-SUBMIT does; a head that does not verify leaves every dispatch route refused and a doctor
        row, and nothing is rewritten; (2) if `current` is not the head's generation, rewrite it; (3) delete the
        generation files newer than the head (a crash before the head's rename). Step (4), the RELOAD keys acks
        before any mint, is the attach-time RELOAD: keys states its generation on every KEYS-ATTACH, and CONTROL
        sends this head when they differ (no keys is attached while the start pass runs)."""
        with self.lock:
            self._active = None
            try:
                head = self._read_head()
                if head is not None:
                    trust = read_trust(self.dir)
                    if head["store_id"] != trust.store_id:
                        raise Refused("routemap-head-unverifiable", "the head names another store")
                    checked = _load_generation(self.dir, head["generation"], head["item_sha256"], trust,
                                               base_route_ids(self.dir), self._verify)
            except Refused as e:
                self.problem = "routemap-head-unverifiable"
                self.problem_detail = f"{e.name}: {e.detail}"
                return
            top = 0 if head is None else head["generation"]
            if head is not None and _read_current(self.dispatch) != top:
                self._write(self.dispatch / CURRENT, str(top).encode("ascii"))
            self._delete_orphans(top)
            self._active = None if head is None else checked
            self.problem = None

    def _delete_orphans(self, top: int) -> None:
        """Every `<gen>.json` / `.sig` with `gen` above `top`, and every temp file a dead writer left."""
        try:
            names = os.listdir(self.dispatch)
        except FileNotFoundError:
            return
        gone = False
        for n in names:
            m = _GEN_FILE.match(n)
            if (m and int(m.group(1)) > top) or n.endswith(".tmp"):
                try:
                    os.unlink(self.dispatch / n)
                    gone = True
                except FileNotFoundError:
                    pass
        if gone:
            dfd = os.open(self.dispatch, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                self._fsync(dfd)
            finally:
                os.close(dfd)

    def submit(self, item: object, sig: object, reload: Callable[[Dict[str, object]], object],
               served: Optional[Callable[[], object]] = None) -> int:
        """`ROUTE-SUBMIT {map, sig}` (r3 §2.3 "Accept at CONTROL" + "Activation", R4 §2), under the activation lock from
        the head read through `current` and the RELOAD's result: `item` is the whole config item as a JSON object, its
        canonical bytes the signed ones. Accepted when its generation is above the head's (gaps allowed), or equal
        with the same digest; any other equal or lower generation is `routemap-generation`. Activation writes
        `<gen>.json` + `.sig`, then `head`, then `current` (each settled: fsynced, its directory fsynced), publishes
        the head as `_active`, then calls `reload(head)` (CONTROL's RELOAD to keys; its outcome is CONTROL's, never
        this answer's). Returns the generation.

        An equal-generation, equal-digest resubmission is a CONVERGENT REPAIR, never a bare ack (the one rule keys'
        :meth:`KeysRoutes.check_reload` keeps too): the same writes run again, so an activation a failed write left
        part-done (a `current` never written, a directory fsync that failed after its rename) is finished; `_active`
        is published; and the RELOAD is sent again unless `served()` (what keys acked or stated) already names this
        head (with no `served`, always)."""
        if not isinstance(item, dict):
            raise Refused("routemap-invalid:item", "map is the config item as one JSON object")
        try:
            raw = canon.canonical_bytes(item)
        except (ContractError, TypeError, ValueError, RecursionError):
            raise Refused("routemap-invalid:item", "map has no canonical form") from None
        with self.lock:
            head = self._read_head()
            trust = read_trust(self.dir)
            checked = check_item(raw, sig, trust, base_route_ids(self.dir), verify=self._verify)
            if head is not None:
                if head["store_id"] != trust.store_id:
                    raise Refused("routemap-store", "CONTROL's head names another store")
                if checked.generation < head["generation"] or (
                        checked.generation == head["generation"] and checked.item_sha256 != head["item_sha256"]):
                    raise Refused("routemap-generation",
                                  f"generation {checked.generation} against the head's {head['generation']}")
            again = head is not None and checked.generation == head["generation"]
            jp, sp = _gen_paths(self.dispatch, checked.generation)
            assert isinstance(sig, str)
            self._write(jp, raw)
            self._write(sp, sig.encode("ascii"))
            self.fault("before_head")
            self._write(self.dispatch / HEAD, canon.canonical_bytes(
                {"generation": checked.generation, "item_sha256": checked.item_sha256, "store_id": checked.store_id}))
            self.fault("after_head")
            self._write(self.dispatch / CURRENT, str(checked.generation).encode("ascii"))
            self._active = checked
            self.problem = None
            self.fault("after_current")
            want = {"generation": checked.generation, "item_sha256": checked.item_sha256}
            if not again or served is None or served() != want:
                reload(want)
            return checked.generation


# --- keys' half --------------------------------------------------------------------------------------------------

class KeysRoutes:
    """keys' route maps under `lib_root`. The file work (`check_reload`, `commit`, `load`) is synchronous and returns
    new values, so keys runs it off its loop and installs the result on the loop (:meth:`install`); the serving half
    (:meth:`policy`, :meth:`stated`) reads only what was installed. `usable` False refuses every dispatch route: a head
    that does not parse or whose generation does not load, and every RELOAD keys refused, until a valid one."""

    def __init__(self, lib_root: str, *, verify: Optional[Verify] = None,
                 fsync: Callable[[int], None] = os.fsync) -> None:
        self.routes_dir = Path(lib_root) / "routes"
        self.head_path = Path(lib_root).joinpath(*KEYS_HEAD)
        self._verify = verify
        self._fsync = fsync
        self.head: Optional[dict] = None
        self.maps: Dict[int, Checked] = {}
        self.usable = True
        self.problem: Optional[str] = None
        #: the route ids a dispatch session may be pinned to are UNKNOWN (keys' head on disk does not verify, so no head
        #: is held): set by :meth:`install` and :meth:`refuse`, cleared only by :meth:`accept` of a verified head, so a
        #: later refused RELOAD, whatever it names, never reads unknown ids as none (:meth:`route_ids`)
        self.ids_unknown = False
        #: THE RELOAD transaction lock: keys' `_reload_files` holds it from `check_reload` through `commit`, so one
        #: RELOAD's file work owns keys' disk head at a time, whatever happens to the asyncio task that started it (a
        #: preemption cancels the task, never the executor thread doing the work).
        self.txn = threading.Lock()

    # -- the file work (off the loop) --

    def _disk_head(self) -> Optional[dict]:
        try:
            data = _read(self.head_path, SMALL_FILE_MAX)
        except OSError:
            raise Refused("routemap-head-unverifiable", "keys' head is unreadable") from None
        return None if data is None else _head_obj(data, keys=True)

    def load(self) -> Tuple[Optional[dict], Dict[int, Checked], Optional[str]]:
        """keys' start: its head and every served generation, each re-hashed against the recorded digest and re-checked.
        Returns `(head, maps, problem)`; `problem` set = every dispatch route refused (the head does not parse, or the
        head generation does not load). A served generation that does not load is simply not served."""
        try:
            head = self._disk_head()
        except Refused as e:
            return None, {}, e.name
        if head is None:
            return None, {}, None
        maps: Dict[int, Checked] = {}
        try:
            trust = read_trust(self.routes_dir)
            base = base_route_ids(self.routes_dir)
        except Refused as e:
            return head, {}, e.name
        if trust.store_id != head["store_id"]:
            return head, {}, "routemap-store"
        for e in head["served"]:
            try:
                maps[e["generation"]] = _load_generation(self.routes_dir, e["generation"], e["item_sha256"], trust,
                                                         base, self._verify)
            except Refused:
                continue
        if head["generation"] not in maps:
            return head, maps, "routemap-head-unverifiable"
        return head, maps, None

    def check_reload(self, generation: int, item_sha256: str) -> Tuple[Checked, dict]:
        """r3 §2.3 "Load at keys" (R4 §2): `current` == the RELOAD's generation == the file's == the envelope's; the
        file's SHA-256 == `item_sha256`; every item check, re-run here; and against keys' OWN head on disk: a greater
        generation, or the same one with the same digest. Returns the checked item and the head keys serves after
        this RELOAD, which the caller always settles (:meth:`commit`) and then installs (:meth:`accept`): a new head
        for a greater generation, or the disk head itself for the same one (the convergent repair: a head whose
        directory fsync failed after its rename is settled again, and a head on disk that keys does not hold in
        memory is installed)."""
        current = _read_current(self.routes_dir / DISPATCH_DIR)
        if current != generation:
            raise Refused("routemap-invalid:current", f"current is {current}, not {generation}")
        trust = read_trust(self.routes_dir)
        checked = _load_generation(self.routes_dir, generation, item_sha256, trust,
                                   base_route_ids(self.routes_dir), self._verify)
        head = self._disk_head()
        if head is not None:
            if head["store_id"] != trust.store_id:
                raise Refused("routemap-store", "keys' head names another store")
            if generation < head["generation"] or (generation == head["generation"]
                                                   and item_sha256 != head["item_sha256"]):
                raise Refused("routemap-generation", f"generation {generation} against keys' {head['generation']}")
            if generation == head["generation"]:
                return checked, head
        served = [] if head is None else list(head["served"])
        served.append({"generation": generation, "item_sha256": item_sha256})
        return checked, {"format": KEYS_HEAD_FORMAT, "store_id": trust.store_id, "generation": generation,
                         "item_sha256": item_sha256, "served": served}

    def commit(self, new_head: dict) -> None:
        """keys' head, durably, BEFORE the generation is served or acked (the same bytes again settle it)."""
        os.makedirs(self.head_path.parent, mode=0o700, exist_ok=True)
        write_durably(self.head_path, canon.canonical_bytes(new_head), mode=0o600, fsync=self._fsync)

    # -- the serving half (on the loop) --

    def install(self, head: Optional[dict], maps: Mapping[int, Checked], problem: Optional[str]) -> None:
        self.head, self.maps, self.problem = head, dict(maps), problem
        self.usable = problem is None
        self.ids_unknown = head is None and problem is not None   # `load`: a head on disk that does not verify

    def accept(self, checked: Checked, head: dict) -> None:
        """A RELOAD keys acks: `head` (already durable) installed and the generation served."""
        self.head = head
        self.maps[checked.generation] = checked
        self.usable, self.problem = True, None
        self.ids_unknown = False

    def refuse(self, name: str) -> None:
        """A RELOAD keys refused: every dispatch route refused until a valid one (CONTRACT §4.3)."""
        self.usable, self.problem = False, name
        if self.head is None and name == "routemap-head-unverifiable":
            self.ids_unknown = True   # `check_reload` found keys' head on disk unreadable and none is held

    def route_ids(self) -> Optional[frozenset]:
        """Every route id a dispatch session may be pinned to: the union across EVERY generation keys' head serves
        (served or refused alike; a session pinned to an older generation keeps a route a newer one dropped). No base
        route id may be one of them at the point of use (keys' `_route_policy`), however the base map changed since
        the RELOAD that checked it. An empty set only when there is no head; None when the ids are UNKNOWN (a head on
        disk that does not parse, or a served generation whose map is not loaded): the caller fails closed."""
        if self.ids_unknown:
            return None
        if self.head is None:
            return frozenset()   # no head is held only before keys acked any generation, and a pin exists only for one
        ids: set = set()
        for e in self.head["served"]:
            m = self.maps.get(e["generation"])
            if m is None:
                return None
            ids.update(m.routes)
        return frozenset(ids)

    def stated(self) -> Tuple[Optional[int], Optional[str]]:
        """What KEYS-ATTACH states keys serves: its head, or `(None, None)` when it serves no map or refuses every
        dispatch route (so CONTROL's attach-time RELOAD reaches it)."""
        if not self.usable or self.head is None or self.head["generation"] not in self.maps:
            return None, None
        return self.head["generation"], self.head["item_sha256"]

    def policy(self, route: str, pin: Optional[Pin]) -> RoutePolicy:
        """`dispatch_policy(route, pin)`: P's entry for `route` iff the three-part pin rule holds (R4 §2 "Pins"), else
        `Refused("route-not-allowed", <the rule that failed>)`."""
        if pin is None:
            raise Refused("route-not-allowed", "no pin: a session with no ticket has no dispatch route")
        if not self.usable or self.head is None:
            raise Refused("route-not-allowed", "no dispatch map is served")
        served = {e["generation"]: e["item_sha256"] for e in self.head["served"]}
        p = self.maps.get(pin.generation)
        if p is None or served.get(pin.generation) != p.item_sha256:
            raise Refused("route-not-allowed", f"rule 1: generation {pin.generation} is not served")
        top = self.maps.get(self.head["generation"])
        if top is None or route not in top.routes:
            raise Refused("route-not-allowed", f"rule 2: route {route} is not in the head generation")
        if route not in p.routes or p.digests.get(route) != pin.policy_digest:
            raise Refused("route-not-allowed", f"rule 3: generation {pin.generation}'s {route} is not the pinned policy")
        return p.routes[route].policy


__all__ = ["CONFIG_FORMAT", "CONFIG_NAMESPACE", "CURRENT", "Checked", "ControlRoutes", "DISPATCH_DIR", "FAULT_POINTS",
           "HEAD", "ITEM_KEYS", "ITEM_MAX_BYTES", "KEYS_HEAD", "KEYS_HEAD_FORMAT", "KIND", "KeysRoutes", "Pin",
           "SIGNATURE_MAX", "TRUST_FORMAT", "TRUST_JSON", "TRUST_PUB", "Trust", "base_route_ids", "base_route_types", "check_item",
           "item_digest", "pin_of", "policy_digest", "read_trust"]
