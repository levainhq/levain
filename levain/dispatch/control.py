"""levain.dispatch.control — dispatch CONFIGURATION: human-signed items in one hash chain, bound to one store.

DESIGN §3.2: the template registry, the verifier store, the provider policy, the gateway's route map and the adoption
rules are dispatch control state, and ONE rule covers all of it. Each change is a CONFIGURATION ITEM, canonical JSON
(:func:`levain.contracts.canon.canonical`)::

    {"format": "levain-dispatch-config/1", "store_id": "<32 hex>", "generation": <int>, "prev": "sha256:<hex>" | "",
     "issued_at": "<UTC stamp>", "kind": "templates" | "verifiers" | "provider_policy" | "routemap" |
     "adoption_rules", "body": {...}}

signed by the operator in its own SSHSIG namespace (:data:`CONFIG_NAMESPACE`), so a configuration signature verifies
as nothing else and nothing else verifies as one. The items form ONE chain over every kind (the authority log's
``prev_hash`` pattern): an item is accepted only at ``generation`` exactly one above the highest accepted, of ANY
kind, with ``prev`` the SHA-256 of the previous item's canonical text (empty at generation 1). So restoring an older,
still validly signed item is refused whatever its kind, and the signature on the newest item vouches, hash by hash,
for the exact text of every item before it. ``store_id`` is drawn at random when the store is created
(:meth:`ControlStore.store_id` reads it for whoever builds the bytes to sign), so an item signed for one store is
refused by every other, however the two are pinned. The ACTIVE configuration is the latest item of each kind.

The TRUST ROOT is a key fingerprint pinned once, at setup (:meth:`ControlStore.init`), and only when the authority
log shows it as a key that may sign configuration: enrolled, not revoked, of a class that covers
:data:`CONFIG_TIER` (configuration widens what runs, a laptop-only act, so a display-phone key never signs it) and
held at a stated presence custody (never a passkey, never ``unstated``). The same test applies on every
verification. A second init naming another fingerprint is refused. A signature by any other enrolled key, however
strong its class, is refused: the pin is the root, not the class.

:meth:`ControlStore.accept` verifies, checks the store id, the generation and the chain link, judges the body for
its kind against the active configuration (:func:`levain.dispatch.registry.body_refusal`) and stores the item, all
in one write transaction, so two accepts cannot both take one generation. Every read (:meth:`ControlStore.active`,
:meth:`ControlStore.head`, :meth:`ControlStore.generation`) walks the chain from generation 1, one hash per item,
and re-verifies the signature on the latest item of each kind (the newest item among them) against the pinned key
under a fresh authority replay, and REFUSES (:class:`ControlStateUnavailable`) when anything does not hold: no
store, no pin, a damaged, missing or reordered row, a broken link, a signature that no longer verifies, a root key
since revoked, an authority log that cannot be read. An unreadable configuration is never an empty one.
``ControlStore.active`` omits the adoption rules; the intent is that they reach a reader only through
:func:`levain.dispatch.registry.active_adoption_rules`, re-judged against the configuration active then.

The store is ``<dispatch dir>/control.db`` (:func:`levain.dispatch.jobs.default_dispatch_dir`; the intent is the
directory the confinement floor already covers, with no second name for it), in a directory that is the operator's
alone (0700), never through a symlink. Its tables are append-only (SQL triggers refuse UPDATE, DELETE and an INSERT
OR REPLACE through any key), and a trigger refuses an item row whose generation is not the next one.

KNOWN EDGES. Until S1 places the CONTROL principal (design §8), the store is an operator-uid file, so any process
of that uid can write it. What holds then is the signature and the chain: a forged, edited, swapped or deleted row
before the newest fails the walk on read, and an old item cannot be re-signed into a newer generation.
- What does NOT hold is truncating the NEWEST rows: a same-uid process that drops the triggers and deletes the tail
  rolls the configuration back to an older signed head, which this store cannot tell from the truth without a
  counter it does not own. The planned close: a signed contract binds the configuration head it resolved under, and
  filing refuses a store whose head is below any stored signed contract's (it changes the contract digest, so it
  follows the contract and signing work in its own slice).
- There is no root rotation yet. A revoked or lost root refuses every read; recovery is a new store. The planned
  shape: the root is pinned as a signed link in the authority log, so rotation is an anchored authority link (the
  old root signs the new, TUF's root rotation, in levain's own log).
- A template's provider and route are not cross-checked against the active provider policy or route map, and a
  store whose generation reaches :data:`levain.contracts.grammar.JSON_INT_MAX` accepts nothing more (only the
  operator signs generations).
- The signing tool, when it is built, shows the item before the touch: kind, generation, store id, and its
  difference from the active configuration.
Nothing executes on this configuration in this slice. Stdlib only.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

from levain.authority import (
    PASSKEY, UNSTATED, AuthorityError, AuthoritySnapshot, VerifierUnavailable, verify_link_sig,
)
from levain.autonomic.durable_sqlite import open_durable, read_transaction, write_transaction
from levain.autonomic.durable_sqlite import rollback as _rollback
from levain.cockpit.consent import ConsentError, parse_stamp, utc_stamp
from levain.consent.queue import AuthoritySource, class_covers
from levain.contracts import ContractError
from levain.contracts.canon import canonical_bytes, parse
from levain.contracts.grammar import JSON_INT_MAX
from levain.dispatch import DispatchError
from levain.dispatch import registry

__all__ = [
    "CONFIG_NAMESPACE", "CONFIG_TIER", "DB_NAME", "FORMAT", "ITEM_KEYS", "ITEM_MAX_BYTES", "KINDS", "SIGNATURE_MAX",
    "STORE_FORMAT", "ConfigItem", "ConfigRefused", "ControlStateUnavailable", "ControlStore", "item_bytes",
    "parse_item",
]

CONFIG_NAMESPACE = "levain-dispatch-config"
FORMAT = "levain-dispatch-config/1"
STORE_FORMAT = "levain-dispatch-control/1"
DB_NAME = "control.db"
KINDS = registry.KINDS
ITEM_KEYS = frozenset({"format", "store_id", "generation", "prev", "issued_at", "kind", "body"})
# the tier a configuration signature allows at: the class the trust root's key must cover
CONFIG_TIER = "T3"
# custodies that never hold a configuration root: a synced passkey, and a key whose custody nobody stated
_ROOT_CUSTODY_REFUSED = (PASSKEY, UNSTATED)
# bounds checked before anything is parsed or verified: a templates item with every bound full is well under this
ITEM_MAX_BYTES = 1024 * 1024
SIGNATURE_MAX = 64 * 1024
_ACCESS_BY_OTHERS = 0o077
_FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
_STORE_ID = re.compile(r"[0-9a-f]{32}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class ConfigRefused(DispatchError):
    """A configuration item that is not accepted: the message says why, and nothing was stored."""


class ControlStateUnavailable(DispatchError):
    """The configuration cannot be trusted or read: every read refuses, never an empty configuration."""


@dataclass(frozen=True)
class ConfigItem:
    """An item as parsed: ``text`` is its canonical form, the exact bytes signed, and ``digest`` their SHA-256, what
    the next item's ``prev`` names."""
    store_id: str
    generation: int
    prev: str
    issued_at: str
    kind: str
    body: Any
    text: str
    digest: str


def item_bytes(kind: str, generation: int, body: Any, issued_at: datetime | str, *, prev: str,
               store_id: str) -> bytes:
    """The canonical bytes of a configuration item: what the operator signs and :meth:`ControlStore.accept` takes.
    ``prev`` and ``store_id`` come from the store it is for (:meth:`ControlStore.head`,
    :meth:`ControlStore.store_id`)."""
    stamp = issued_at if isinstance(issued_at, str) else utc_stamp(issued_at)
    return canonical_bytes({"format": FORMAT, "store_id": store_id, "generation": generation, "prev": prev,
                            "issued_at": stamp, "kind": kind, "body": body})


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def parse_item(raw: Any) -> ConfigItem:
    """The item ``raw`` (text or bytes) holds, refused unless ``raw`` is exactly its canonical form: one item, one
    byte string, one signature. Shape only; the body is judged by kind at accept."""
    if isinstance(raw, str):
        try:
            raw = raw.encode("utf-8")
        except UnicodeEncodeError:
            raise ConfigRefused("the configuration item is not UTF-8 text") from None
    if not isinstance(raw, bytes):
        raise ConfigRefused("a configuration item is its canonical bytes or text")
    if len(raw) > ITEM_MAX_BYTES:
        raise ConfigRefused(f"the configuration item is over {ITEM_MAX_BYTES} bytes")
    try:
        obj = parse(raw)
    except ContractError as exc:
        raise ConfigRefused(f"the configuration item does not parse: {exc}") from None
    except Exception as exc:  # noqa: BLE001 - item bytes are input: any failure on them is a refusal
        raise ConfigRefused(f"the configuration item does not parse ({type(exc).__name__})") from None
    if not isinstance(obj, dict) or set(obj) != ITEM_KEYS:
        raise ConfigRefused(f"a configuration item is exactly {sorted(ITEM_KEYS)}")
    if obj["format"] != FORMAT:
        raise ConfigRefused(f"configuration format {str(obj['format'])[:64]!r} is not {FORMAT!r}")
    if not isinstance(obj["store_id"], str) or _STORE_ID.fullmatch(obj["store_id"]) is None:
        raise ConfigRefused("store_id is the 32 hex characters of the store the item is for")
    gen = obj["generation"]
    if type(gen) is not int or not 1 <= gen <= JSON_INT_MAX:
        raise ConfigRefused("generation is an integer from 1")
    prev = obj["prev"]
    if gen == 1 and prev != "":
        raise ConfigRefused("prev is empty at generation 1")
    if gen > 1 and (not isinstance(prev, str) or _DIGEST.fullmatch(prev) is None):
        raise ConfigRefused("prev is sha256:<64 hex>, the previous item's digest, from generation 2")
    try:
        parse_stamp(obj["issued_at"])
    except ConsentError:
        raise ConfigRefused("issued_at is a UTC stamp") from None
    if obj["kind"] not in KINDS:
        raise ConfigRefused(f"kind {str(obj['kind'])[:64]!r} is not one of {KINDS}")
    if canonical_bytes(obj) != raw:
        raise ConfigRefused("the configuration item is not in its canonical form")
    return ConfigItem(store_id=obj["store_id"], generation=gen, prev=prev, issued_at=obj["issued_at"],
                      kind=obj["kind"], body=obj["body"], text=raw.decode("utf-8"), digest=_digest(raw))


def _root_problem(snapshot: Any, trust_root: str) -> str | None:
    """Why ``trust_root`` may not sign configuration under ``snapshot``; ``None`` when it may. The one test, at
    init and at every verification."""
    if not isinstance(snapshot, AuthoritySnapshot):
        return "no authority snapshot to verify the trust root against"
    key = snapshot.key(trust_root)
    if key is None:
        return "the trust root is not an enrolled key"
    if not key.active:
        return "the trust root's key is revoked"
    if key.custody == PASSKEY:
        return "the trust root is a passkey; configuration is signed on the laptop"
    if not class_covers(key.key_class, CONFIG_TIER):
        return f"key class {key.key_class} does not cover {CONFIG_TIER}"
    if key.custody in _ROOT_CUSTODY_REFUSED:
        return (f"the trust root's custody is {key.custody}; a configuration root is held at a stated presence "
                "custody")
    return None


def _signer_problem(snapshot: AuthoritySnapshot, trust_root: str, message: bytes, signature: Any,
                    ssh_keygen: Path | str | None) -> str | None:
    """Why ``signature`` is not the trust root's configuration signature over ``message``; ``None`` when it is."""
    problem = _root_problem(snapshot, trust_root)
    if problem is not None:
        return problem
    key = snapshot.key(trust_root)
    if not isinstance(signature, str) or not signature:
        return "the configuration signature is empty"
    if len(signature) > SIGNATURE_MAX:
        return f"the configuration signature is over {SIGNATURE_MAX} characters"
    kwargs: dict[str, Any] = {} if ssh_keygen is None else {"ssh_keygen": ssh_keygen}
    try:
        ok = verify_link_sig(message, signature, public_key=key.public_key, fingerprint=key.fingerprint,
                             namespace=CONFIG_NAMESPACE, **kwargs)
    except VerifierUnavailable as exc:
        return f"the configuration signature could not be verified: {exc}"
    return None if ok else "the configuration signature does not verify as the pinned trust root's"


def _append_only(table: str, message: str) -> tuple[str, str]:
    return (f"CREATE TRIGGER IF NOT EXISTS {table}_frozen BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{message}'); END",
            f"CREATE TRIGGER IF NOT EXISTS {table}_kept BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{message}'); END")


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS items (generation INTEGER PRIMARY KEY, kind TEXT NOT NULL, item TEXT NOT NULL, "
    "signature TEXT NOT NULL, accepted_at TEXT NOT NULL)",
    *_append_only("meta", "control metadata is written once"),
    *_append_only("items", "a configuration item is written once"),
    "CREATE TRIGGER IF NOT EXISTS meta_no_replace BEFORE INSERT ON meta "
    "WHEN EXISTS (SELECT 1 FROM meta WHERE key = NEW.key OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'control metadata is written once'); END",
    # generation is the rowid: a new row's generation one above every stored one is also never a replacement
    "CREATE TRIGGER IF NOT EXISTS items_next BEFORE INSERT ON items "
    "WHEN NEW.generation IS NULL OR NEW.generation != COALESCE((SELECT MAX(generation) FROM items), 0) + 1 "
    "BEGIN SELECT RAISE(ABORT, 'a configuration generation is the next after the highest accepted'); END",
)


class ControlStore:
    """The configuration store ``<directory>/control.db``. Create and pin it once with :meth:`init`; open it with the
    trusted authority source every read is verified against (:func:`levain.consent.queue.authority_source`),
    replayed afresh on each read."""

    def __init__(self, directory: Path | str | None = None, *, authority: AuthoritySource,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 ssh_keygen: Path | str | None = None) -> None:
        if type(authority) is not AuthoritySource:
            raise ControlStateUnavailable("authority must be the AuthoritySource authority_source() returns")
        self.directory = _directory(directory)
        self.path = self.directory / DB_NAME
        self._authority = authority
        self._clock = clock
        self._ssh_keygen = ssh_keygen

    # --- setup ---------------------------------------------------------------------------------------------------
    @staticmethod
    def init(directory: Path | str | None, trust_root_fp: str, *, authority: AuthoritySource) -> None:
        """Create the store in ``directory`` (default the dispatch directory), draw its store id and pin
        ``trust_root_fp``, the operator's signing key fingerprint, once ``authority`` shows it may sign
        configuration (the same test every verification applies). Again with the same fingerprint is a no-op; with
        another, refused."""
        if not isinstance(trust_root_fp, str) or _FINGERPRINT.fullmatch(trust_root_fp) is None:
            raise ConfigRefused("the trust root is an SSH key fingerprint, SHA256:<43 base64>")
        if type(authority) is not AuthoritySource:
            raise ConfigRefused("authority must be the AuthoritySource authority_source() returns")
        try:
            snapshot = authority()
        except (AuthorityError, sqlite3.Error, OSError, ValueError) as exc:
            raise ConfigRefused(f"the authority log could not be read: {type(exc).__name__}: "
                                f"{str(exc)[:200]}") from None
        problem = _root_problem(snapshot, trust_root_fp)
        if problem is not None:
            raise ConfigRefused(f"{problem}; it is not pinned")
        d = _directory(directory)
        _check_directory(d, create=True)
        path = d / DB_NAME
        if path.is_symlink() or (os.path.lexists(path) and not path.is_file()):
            raise ConfigRefused(f"{path} is a symlink or not a file; the control store is a plain file")
        conn = open_durable(path)
        try:
            os.chmod(path, 0o600)
            conn.execute("BEGIN IMMEDIATE")
            try:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                if tables:
                    meta = _meta(conn)
                    if meta["trust_root"] != trust_root_fp:
                        raise ConfigRefused("the control store is already pinned to another trust root; a pin is "
                                            "set once")
                else:
                    for stmt in _SCHEMA:
                        conn.execute(stmt)
                    conn.execute("INSERT INTO meta (key, value) VALUES ('format', ?)", (STORE_FORMAT,))
                    conn.execute("INSERT INTO meta (key, value) VALUES ('store_id', ?)", (secrets.token_hex(16),))
                    conn.execute("INSERT INTO meta (key, value) VALUES ('trust_root', ?)", (trust_root_fp,))
                conn.execute("COMMIT")
            except BaseException:
                _rollback(conn)
                raise
        finally:
            conn.close()

    # --- connections ---------------------------------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        try:
            _check_directory(self.directory, create=False)
        except DispatchError as exc:
            raise ControlStateUnavailable(str(exc)) from None
        if not self.path.is_file() or self.path.is_symlink():
            raise ControlStateUnavailable(f"no control store at {self.path}; it is created by init")
        try:
            return open_durable(self.path)
        except sqlite3.Error as exc:
            raise ControlStateUnavailable(f"the control store cannot be opened: {exc}") from None

    @contextmanager
    def _guard(self) -> Iterator[None]:
        # a store or authority fault is an unavailable configuration, never a crash or an empty answer
        try:
            yield
        except (sqlite3.Error, OSError, AuthorityError, ValueError, KeyError) as exc:
            raise ControlStateUnavailable(f"the control store cannot be read: {type(exc).__name__}: "
                                          f"{str(exc)[:200]}") from None

    def _snapshot(self) -> AuthoritySnapshot:
        try:
            return self._authority()
        except (AuthorityError, sqlite3.Error, OSError, ValueError) as exc:
            raise ControlStateUnavailable(f"the authority log could not be read: {type(exc).__name__}: "
                                          f"{str(exc)[:200]}") from None

    def _chain(self, conn: sqlite3.Connection,
               snapshot: AuthoritySnapshot) -> tuple[dict[str, ConfigItem], int, str]:
        """``(active, top, head)``: the latest item of each kind, the highest generation and its digest ("" when
        empty), after walking the whole chain from generation 1 in the caller's transaction and re-verifying the
        latest item of each kind against the pinned key; raises :class:`ControlStateUnavailable` on anything that
        does not hold."""
        meta = _meta(conn)
        prev = ""
        latest: dict[str, tuple[ConfigItem, Any]] = {}
        expected = 0
        for gen, kind, text, signature in conn.execute(
                "SELECT generation, kind, item, signature FROM items ORDER BY generation"):
            expected += 1
            try:
                item = parse_item(text)
            except ConfigRefused as exc:
                raise ControlStateUnavailable(f"stored item {gen} is not a configuration item: {exc}") from None
            if item.generation != gen or item.kind != kind:
                raise ControlStateUnavailable(f"stored item {gen} does not match its row")
            if gen != expected:
                raise ControlStateUnavailable(f"the configuration chain has no item {expected}; a row was removed")
            if item.store_id != meta["store_id"]:
                raise ControlStateUnavailable(f"stored item {gen} was signed for another control store")
            if item.prev != prev:
                raise ControlStateUnavailable(f"stored item {gen} does not chain to item {gen - 1}")
            prev = item.digest
            latest[kind] = (item, signature)
        # the newest item is the latest of its kind, so its signature is among these, and it vouches through the
        # chain for the exact text of every item before it
        for kind, (item, signature) in latest.items():
            problem = _signer_problem(snapshot, meta["trust_root"], item.text.encode("utf-8"), signature,
                                      self._ssh_keygen)
            if problem is not None:
                raise ControlStateUnavailable(f"stored item {item.generation} ({kind}) does not verify: {problem}")
        return {kind: item for kind, (item, _sig) in latest.items()}, expected, prev

    def _read(self) -> tuple[dict[str, ConfigItem], int, str]:
        with self._guard(), read_transaction(self._connect) as conn:
            return self._chain(conn, self._snapshot())

    # --- the public operations -----------------------------------------------------------------------------------
    def accept(self, item: Any, signature: Any) -> ConfigItem:
        """Verify ``item`` (its canonical bytes or text) and ``signature`` against the pinned trust root under the
        store's authority source (replayed INSIDE the write transaction), check it is for this store, at the next
        generation and chained to the verified head, and its body for its kind against the active configuration,
        and store it; all one write transaction. Raises :class:`ConfigRefused` saying why, or
        :class:`ControlStateUnavailable` when the store cannot be trusted."""
        parsed = parse_item(item)
        with self._guard(), write_transaction(self._connect) as conn:
            meta = _meta(conn)
            try:
                snapshot = self._authority()
            except (AuthorityError, sqlite3.Error, OSError, ValueError) as exc:
                raise ConfigRefused(f"the authority log could not be read: {type(exc).__name__}: "
                                    f"{str(exc)[:200]}") from None
            problem = _signer_problem(snapshot, meta["trust_root"], parsed.text.encode("utf-8"), signature,
                                      self._ssh_keygen)
            if problem is not None:
                raise ConfigRefused(problem)
            if parsed.store_id != meta["store_id"]:
                raise ConfigRefused("the item was signed for another control store (its store_id is not this "
                                    "store's)")
            active, top, head = self._chain(conn, snapshot)
            if parsed.generation <= top:
                raise ConfigRefused(f"generation {parsed.generation} is not above the highest accepted ({top}); an "
                                    "older or replayed item is refused")
            if parsed.generation != top + 1:
                raise ConfigRefused(f"generation {parsed.generation} skips: the next is {top + 1}")
            if parsed.prev != head:
                raise ConfigRefused(f"generation {parsed.generation} does not chain to the head (its prev is not "
                                    f"item {top}'s digest)")
            problem = registry.body_refusal(parsed.kind, parsed.body, active)
            if problem is not None:
                raise ConfigRefused(f"{parsed.kind}: {problem}")
            conn.execute("INSERT INTO items (generation, kind, item, signature, accepted_at) VALUES (?, ?, ?, ?, ?)",
                         (parsed.generation, parsed.kind, parsed.text, signature, utc_stamp(self._now())))
        return parsed

    def active(self) -> Mapping[str, ConfigItem]:
        """The active configuration, kind -> its latest accepted item, after the chain walk and re-verification
        under a fresh authority replay; the adoption rules are left out (the intent: they are read re-judged, through
        :func:`levain.dispatch.registry.active_adoption_rules`). Refuses (:class:`ControlStateUnavailable`)
        rather than answer from a store it cannot verify."""
        items = dict(self._read()[0])
        items.pop("adoption_rules", None)
        return MappingProxyType(items)

    def _verified_items(self) -> Mapping[str, ConfigItem]:
        # every kind, the adoption rules included: for the registry's re-judging reader, never handed out raw
        return MappingProxyType(self._read()[0])

    def head(self) -> tuple[int, str]:
        """``(generation, digest)`` of the verified newest item, ``(0, "")`` before the first: the next item is
        generation + 1 with ``prev`` = digest."""
        _active, top, head = self._read()
        return top, head

    def generation(self) -> int:
        """The highest generation accepted, of any kind (0 before the first item), read through the verified
        chain."""
        return self.head()[0]

    def store_id(self) -> str:
        """This store's id, drawn at init: every item signed for it carries it."""
        with self._guard(), read_transaction(self._connect) as conn:
            return _meta(conn)["store_id"]

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ConfigRefused("the control store's clock returned a naive datetime")
        return now


def _directory(directory: Path | str | None) -> Path:
    if directory is not None:
        return Path(directory)
    from levain.dispatch.jobs import default_dispatch_dir

    return default_dispatch_dir()


def _check_directory(d: Path, *, create: bool) -> None:
    # the directory is the operator's alone, never through a symlink (the dispatch job store's rule, mirrored)
    if create and not d.exists():
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not os.path.isdir(d) or os.path.islink(d):
        raise DispatchError(f"control store directory {d} is not a directory")
    st = os.lstat(d)
    if st.st_uid != os.getuid():
        raise DispatchError(f"control store directory {d} is not owned by uid {os.getuid()}")
    if st.st_mode & _ACCESS_BY_OTHERS:
        raise DispatchError(f"control store directory {d} is reachable by group or others (mode "
                            f"{st.st_mode & 0o777:o}); it must be 0700")


def _meta(conn: sqlite3.Connection) -> dict[str, str]:
    """The store's format, store id and pinned trust root, or :class:`ControlStateUnavailable`."""
    try:
        rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    except sqlite3.Error as exc:
        raise ControlStateUnavailable(f"the control store has no metadata: {exc}") from None
    if rows.get("format") != STORE_FORMAT:
        raise ControlStateUnavailable(f"control store format {rows.get('format')!r} is not {STORE_FORMAT!r}")
    sid = rows.get("store_id")
    if not isinstance(sid, str) or _STORE_ID.fullmatch(sid) is None:
        raise ControlStateUnavailable("the control store has no store id")
    root = rows.get("trust_root")
    if not isinstance(root, str) or _FINGERPRINT.fullmatch(root) is None:
        raise ControlStateUnavailable("the control store has no pinned trust root")
    return rows
