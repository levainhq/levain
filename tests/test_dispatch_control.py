"""Dispatch configuration: one signed item per change, one hash-chained generation over every kind, bound to its
store, a trust root pinned once. Keys are real ssh-keygen keys enrolled in a real signed authority log (the consent
queue tests' ``Key`` and ``Log``), the store is real SQLite in tmp, and the end-to-end filing runs over the contract
tests' real git repo."""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import sqlite3
from datetime import timedelta

import pytest

import levain.authority as auth
from levain.cockpit.keys import fingerprint_of
from levain.contracts.canon import canonical_bytes, parse
from levain.dispatch import ContractRefused, DispatchError
from levain.dispatch import control as ctl
from levain.dispatch.contract import ResolvedTemplate
from levain.dispatch.jobs import JobStore
from levain.dispatch.registry import (
    VERIFIER_FORMAT, active_policy, driver_template_digest, resolver, template_content_hash, template_from_mapping,
    verifier_artifact_digest,
)
from levain.dispatch.sign import DISPATCH_SIGN_NAMESPACE
from tests.test_consent_queue import Key, Log
from tests.test_dispatch_contract import NOW, POLICY, make_repo, submission, template
from tests.test_dispatch_sign import passkey_line

NS = ctl.CONFIG_NAMESPACE


def ventry(vid: str, kind: str, *, argv=("python3", "-m", "pytest", "-q"), expect_exit: int = 0, **over) -> dict:
    """A verifier entry on the shipped output_compare template, every digest computed (``over`` applied before the
    artifact digest, so a changed field is a consistent new entry)."""
    steps = [{"argv": list(argv), "wall_s": 300}]
    driver_input = {"steps": [{"exit": expect_exit}]}
    e = {"verifier_id": vid, "declared_kind": kind, "driver_template": "output_compare",
         "driver_template_digest": driver_template_digest("output_compare"), "steps": steps,
         "steps_digest": "sha256:" + hashlib.sha256(canonical_bytes(steps)).hexdigest(), "driver_input": driver_input,
         "driver_input_digest": "sha256:" + hashlib.sha256(canonical_bytes(driver_input)).hexdigest()}
    e.update(over)
    e["verifier_artifact_digest"] = verifier_artifact_digest(e)
    return e


def vbody(*entries) -> dict:
    return {"format": VERIFIER_FORMAT, "verifiers": list(entries)}


BB_ENTRY = ventry("pytest-bb", "black-box")
IP_ENTRY = ventry("pytest-ip", "in-process")
BB = BB_ENTRY["verifier_artifact_digest"]
IP = IP_ENTRY["verifier_artifact_digest"]
VERIFIERS = vbody(BB_ENTRY, IP_ENTRY)
WRITE_SET = ("src/a.py", "src/b.py", "src/new.py", "src/sub/deep.py")


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def tmpl(**over) -> dict:
    """A template entry (the contract tests' template, verifier pytest-bb, a write set without their deliberate case
    twin), its content_hash computed."""
    m = template(verifier_hash=BB, write_paths=frozenset(WRITE_SET)).to_mapping()
    m.update(over)
    m["content_hash"] = template_content_hash(m)
    return m


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("control-keys")
    return {n: Key(d, n) for n in ("laptop", "other", "phone", "stranger")}


@pytest.fixture(scope="module")
def log(keys, tmp_path_factory):
    """laptop (genesis, laptop-presence: the pinned root), other (laptop-presence: T3 but not the pin), phone
    (display-phone) and the recorded passkey (display-phone, custody passkey)."""
    lg = Log(tmp_path_factory.mktemp("control-auth"), keys)
    for name, cls in (("laptop", "laptop-presence"), ("other", "laptop-presence"), ("phone", "display-phone")):
        lg.enrol(name, cls)
    pk = passkey_line()
    lg.link({"op": "enrol", "fingerprint": fingerprint_of(pk), "public_key": pk, "class": "display-phone",
             "custody": auth.PASSKEY})
    return lg


def _dir(tmp_path, name="dispatch"):
    d = tmp_path / name
    d.mkdir(mode=0o700)
    os.chmod(d, 0o700)
    return d


@pytest.fixture
def cdir(tmp_path, keys, log):
    d = _dir(tmp_path)
    ctl.ControlStore.init(d, keys["laptop"].fp, authority=log.source)
    return d


@pytest.fixture
def store(cdir, log):
    return ctl.ControlStore(cdir, authority=log.source, clock=Clock(NOW))


# a well-formed prev for an item that is refused before its chain link is looked at
OTHER_PREV = "sha256:" + "0" * 64


def signed(store, kind, gen, body, key, *, ns=NS, at=None, prev=None, store_id=None):
    """An item for ``store`` and its signature. ``prev`` defaults to the store's verified head digest when ``gen``
    is the next generation (what a signing tool builds), else a placeholder."""
    if prev is None:
        top, head = store.head()
        prev = "" if gen == 1 else head if gen == top + 1 else OTHER_PREV
    item = ctl.item_bytes(kind, gen, body, at or NOW - timedelta(minutes=1), prev=prev,
                          store_id=store_id or store.store_id())
    return item, key.sign(item, ns)


def accept(store, log, keys, kind, gen, body, key="laptop", **kw):
    return store.accept(*signed(store, kind, gen, body, keys[key], **kw))


def raw_item(**over) -> bytes:
    """Item bytes in canonical key order, built without the canon module so a malformed value can be written."""
    obj = {"format": ctl.FORMAT, "store_id": "0" * 32, "generation": 1, "prev": "",
           "issued_at": "2026-10-19T19:59:00.000000Z", "kind": "verifier", "body": {}}
    obj.update(over)
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def base_config(store, log, keys):
    accept(store, log, keys, "verifier", 1, VERIFIERS)
    accept(store, log, keys, "templates", 2, [tmpl()])
    accept(store, log, keys, "provider_policy", 3, POLICY.to_mapping())


# --- accept and read back ------------------------------------------------------------------------------------------

def test_accept_and_resolve_round_trip(store, log, keys):
    base_config(store, log, keys)
    active = store.active()
    assert set(active) == {"verifier", "templates", "provider_policy"} and store.generation() == 3
    assert active["templates"].generation == 2 and active["templates"].kind == "templates"
    resolve = resolver(store)
    got = resolve("fix-lint")
    assert isinstance(got, ResolvedTemplate) and got == template_from_mapping(tmpl())
    assert got.content_hash == tmpl()["content_hash"] and got.problem() is None
    assert resolve("nope") is None
    assert active_policy(store).content_hash() == POLICY.content_hash()


def test_the_active_item_is_the_latest_of_its_kind(store, log, keys):
    base_config(store, log, keys)
    accept(store, log, keys, "templates", 4, [tmpl(), tmpl(id="fix-docs")])
    assert resolver(store)("fix-docs") is not None
    accept(store, log, keys, "templates", 5, [tmpl(id="fix-docs")])
    assert resolver(store)("fix-lint") is None and resolver(store)("fix-docs") is not None


def test_item_text_and_bytes_both_accepted(store, log, keys):
    item, sig = signed(store, "verifier", 1, VERIFIERS, keys["laptop"])
    assert store.accept(item.decode("utf-8"), sig).generation == 1


# --- generation and the chain --------------------------------------------------------------------------------------

def test_a_replayed_older_item_is_refused(store, log, keys):
    old, old_sig = signed(store, "verifier", 1, VERIFIERS, keys["laptop"])
    store.accept(old, old_sig)
    accept(store, log, keys, "verifier", 2, vbody(BB_ENTRY))
    with pytest.raises(ctl.ConfigRefused, match="not above the highest accepted"):
        store.accept(old, old_sig)
    assert store.active()["verifier"].generation == 2


def test_the_generation_spans_every_kind(store, log, keys):
    # a policy item signed at generation 1 and held back is refused once ANY kind has passed it
    held, held_sig = signed(store, "provider_policy", 1, POLICY.to_mapping(), keys["laptop"])
    accept(store, log, keys, "verifier", 1, VERIFIERS)
    with pytest.raises(ctl.ConfigRefused, match=r"generation 1 is not above the highest accepted \(1\)"):
        store.accept(held, held_sig)
    with pytest.raises(ctl.ConfigRefused, match="not above"):
        accept(store, log, keys, "templates", 1, [tmpl()])
    accept(store, log, keys, "templates", 2, [tmpl()])
    assert store.generation() == 2


def test_a_generation_gap_is_refused(store, log, keys):
    accept(store, log, keys, "verifier", 1, VERIFIERS)
    with pytest.raises(ctl.ConfigRefused, match="the next is 2"):
        accept(store, log, keys, "templates", 7, [tmpl()])
    assert store.generation() == 1


def test_an_item_not_chained_to_the_head_is_refused(store, log, keys):
    accept(store, log, keys, "verifier", 1, VERIFIERS)
    with pytest.raises(ctl.ConfigRefused, match="does not chain to the head"):
        accept(store, log, keys, "templates", 2, [tmpl()], prev=OTHER_PREV)
    _top, head = store.head()
    assert head == store.active()["verifier"].digest and head.startswith("sha256:")


@pytest.mark.parametrize("gen, prev", [(1, OTHER_PREV), (2, ""), (2, "sha256:xyz")])
def test_prev_is_empty_exactly_at_generation_one(store, keys, gen, prev):
    item = raw_item(generation=gen, prev=prev, store_id=store.store_id())
    with pytest.raises(ctl.ConfigRefused, match="prev"):
        store.accept(item, keys["laptop"].sign(item, NS))


def test_a_deleted_middle_item_fails_every_read_closed(store, log, keys, cdir):
    # the L2 probe: removing a newer policy from the middle would revive the older, looser one under a signed head
    base_config(store, log, keys)
    tighter = POLICY.to_mapping()
    tighter["never"] = list(tighter.get("never", [])) + ["secrets/**"]
    accept(store, log, keys, "provider_policy", 4, tighter)
    accept(store, log, keys, "templates", 5, [tmpl()])
    conn = _raw(cdir)
    try:
        conn.execute("DROP TRIGGER items_kept")
        conn.execute("DELETE FROM items WHERE generation = 4")
    finally:
        conn.close()
    for read in (store.active, store.generation, store.head, lambda: resolver(store)("fix-lint"),
                 lambda: active_policy(store)):
        with pytest.raises(DispatchError, match="no item 4"):
            read()
    with pytest.raises(ctl.ControlStateUnavailable, match="no item 4"):
        accept(store, log, keys, "verifier", 6, VERIFIERS, prev=OTHER_PREV)


def test_a_middle_item_swapped_for_another_signed_one_fails_closed(store, log, keys, cdir):
    # a validly signed item for the same store and generation, but not the one the head chains to
    base_config(store, log, keys)
    accept(store, log, keys, "verifier", 4, VERIFIERS)
    swap, swap_sig = signed(store, "provider_policy", 3, POLICY.to_mapping(), keys["laptop"],
                            at=NOW - timedelta(minutes=5), prev=store.active()["templates"].digest)
    conn = _raw(cdir)
    try:
        conn.execute("DROP TRIGGER items_frozen")
        conn.execute("UPDATE items SET item = ?, signature = ? WHERE generation = 3", (swap.decode(), swap_sig))
    finally:
        conn.close()
    with pytest.raises(ctl.ControlStateUnavailable, match="does not chain to item 3"):
        store.active()


@pytest.mark.parametrize("gen", [0, -1, True, "3", 2 ** 53])
def test_generation_is_a_positive_integer(store, log, keys, gen):
    item = raw_item(generation=gen, prev=OTHER_PREV, store_id=store.store_id())
    with pytest.raises(ctl.ConfigRefused):
        store.accept(item, keys["laptop"].sign(item, NS))


def test_generation_reads_the_verified_head(store, log, keys, cdir):
    base_config(store, log, keys)
    assert store.generation() == 3
    conn = _raw(cdir)
    try:
        conn.execute("DROP TRIGGER items_frozen")
        conn.execute("UPDATE items SET signature = 'x' WHERE generation = 3")
    finally:
        conn.close()
    with pytest.raises(ctl.ControlStateUnavailable, match="does not verify"):
        store.generation()


# --- the store id --------------------------------------------------------------------------------------------------

def test_an_item_signed_for_another_store_is_refused(store, log, keys, tmp_path):
    # the L2 probe: the same signed items replayed into a fresh store pinned to the same root
    items = [signed(store, "verifier", 1, VERIFIERS, keys["laptop"])]
    store.accept(*items[0])
    items.append(signed(store, "templates", 2, [tmpl()], keys["laptop"]))
    store.accept(*items[1])
    d2 = _dir(tmp_path, "fresh")
    ctl.ControlStore.init(d2, keys["laptop"].fp, authority=log.source)
    s2 = ctl.ControlStore(d2, authority=log.source, clock=Clock(NOW))
    assert s2.store_id() != store.store_id()
    for it, sig in items:
        with pytest.raises(ctl.ConfigRefused, match="another control store"):
            s2.accept(it, sig)
    assert s2.generation() == 0


@pytest.mark.parametrize("sid", ["", "0" * 31, "G" * 32, 7])
def test_a_malformed_store_id_is_refused(store, keys, sid):
    item = raw_item(store_id=sid)
    with pytest.raises(ctl.ConfigRefused, match="store_id"):
        store.accept(item, keys["laptop"].sign(item, NS))


def test_the_store_id_is_drawn_once(cdir, log, keys):
    s = ctl.ControlStore(cdir, authority=log.source)
    first = s.store_id()
    ctl.ControlStore.init(cdir, keys["laptop"].fp, authority=log.source)
    assert s.store_id() == first and len(first) == 32


# --- the signer ----------------------------------------------------------------------------------------------------

def test_a_non_pinned_signer_is_refused_even_at_t3(store, log, keys):
    for name in ("other", "phone", "stranger"):
        with pytest.raises(ctl.ConfigRefused, match="does not verify as the pinned trust root's"):
            accept(store, log, keys, "verifier", 1, VERIFIERS, key=name)
    assert store.generation() == 0


def _two_logs(tmp_path_factory, ks, name, first, second):
    """Two authority logs over the same keys, ``name`` enrolled by its ``first`` op in one and its ``second`` in the
    other: a store pinned under the first, then read under the second, shows the verify-time rule apart from
    init's."""
    logs = []
    for op in (first, second):
        lg = Log(tmp_path_factory.mktemp("root-auth"), ks)
        lg.enrol("laptop", "laptop-presence")
        lg.link(op(ks[name]))
        logs.append(lg)
    return logs


def _legacy_unstated(k):
    return {"op": "enrol", "fingerprint": k.fp, "public_key": k.public_key, "class": "laptop-presence",
            "custody": auth.UNSTATED, "legacy_enrolled_at": "2026-01-01T00:00:00.000000Z"}


def _passphrase(k):
    return {"op": "enrol", "fingerprint": k.fp, "public_key": k.public_key, "class": "laptop-presence",
            "custody": "passphrase"}


def test_a_t2_display_phone_root_is_refused(tmp_path, log, keys):
    with pytest.raises(ctl.ConfigRefused, match="key class display-phone does not cover T3"):
        ctl.ControlStore.init(_dir(tmp_path, "phone-root"), keys["phone"].fp, authority=log.source)


def test_a_passkey_root_is_refused_at_init_and_at_verify(tmp_path, tmp_path_factory, log, keys):
    pk = passkey_line()
    with pytest.raises(ctl.ConfigRefused, match="passkey"):
        ctl.ControlStore.init(_dir(tmp_path, "passkey-root"), fingerprint_of(pk), authority=log.source)
    # the same public key enrolled at a presence custody pins; read under the log where it is a passkey, it refuses
    fido = Log(tmp_path_factory.mktemp("fido-auth"), keys)
    fido.enrol("laptop", "laptop-presence")
    fido.link({"op": "enrol", "fingerprint": fingerprint_of(pk), "public_key": pk, "class": "laptop-presence",
               "custody": "fido"})
    d = _dir(tmp_path, "fido-root")
    ctl.ControlStore.init(d, fingerprint_of(pk), authority=fido.source)
    store = ctl.ControlStore(d, authority=log.source, clock=Clock(NOW))
    item = ctl.item_bytes("verifier", 1, VERIFIERS, NOW, prev="", store_id=store.store_id())
    with pytest.raises(ctl.ConfigRefused, match="passkey"):
        store.accept(item, "-----BEGIN SSH SIGNATURE-----\nAAAA\n-----END SSH SIGNATURE-----\n")


def test_an_unstated_custody_root_is_refused_at_init_and_at_verify(tmp_path, tmp_path_factory):
    ks = {n: Key(tmp_path_factory.mktemp("unstated-keys"), n) for n in ("laptop", "root")}
    stated, unstated = _two_logs(tmp_path_factory, ks, "root", _passphrase, _legacy_unstated)
    assert unstated.source().key(ks["root"].fp).custody == auth.UNSTATED
    with pytest.raises(ctl.ConfigRefused, match="unstated"):
        ctl.ControlStore.init(_dir(tmp_path, "u1"), ks["root"].fp, authority=unstated.source)
    d = _dir(tmp_path, "u2")
    ctl.ControlStore.init(d, ks["root"].fp, authority=stated.source)
    store = ctl.ControlStore(d, authority=stated.source, clock=Clock(NOW))
    accept(store, stated, ks, "verifier", 1, VERIFIERS, key="root")
    read_unstated = ctl.ControlStore(d, authority=unstated.source, clock=Clock(NOW))
    with pytest.raises(ctl.ControlStateUnavailable, match="unstated"):
        read_unstated.active()
    with pytest.raises(ctl.ConfigRefused, match="unstated"):
        read_unstated.accept(*signed(store, "verifier", 2, VERIFIERS, ks["root"]))


def test_init_refuses_an_unenrolled_or_revoked_root(tmp_path, tmp_path_factory, log, keys):
    # the L2 probe: init pinned a fingerprint no log had ever enrolled
    with pytest.raises(ctl.ConfigRefused, match="not an enrolled key"):
        ctl.ControlStore.init(_dir(tmp_path, "x"), "SHA256:" + "A" * 43, authority=log.source)
    with pytest.raises(ctl.ConfigRefused, match="not an enrolled key"):
        ctl.ControlStore.init(_dir(tmp_path, "y"), keys["stranger"].fp, authority=log.source)
    assert not (tmp_path / "x" / ctl.DB_NAME).exists()
    ks = {n: Key(tmp_path_factory.mktemp("init-revoke-keys"), n) for n in ("laptop", "root")}
    lg = Log(tmp_path_factory.mktemp("init-revoke-auth"), ks)
    lg.enrol("laptop", "laptop-presence")
    lg.enrol("root", "laptop-presence")
    lg.revoke("root")
    with pytest.raises(ctl.ConfigRefused, match="revoked"):
        ctl.ControlStore.init(_dir(tmp_path, "z"), ks["root"].fp, authority=lg.source)


def test_init_takes_the_authority_source(tmp_path, log, keys):
    with pytest.raises(ctl.ConfigRefused, match="AuthoritySource"):
        ctl.ControlStore.init(_dir(tmp_path), keys["laptop"].fp, authority=log.source())


def test_init_refuses_a_symlinked_store(tmp_path, log, keys):
    d = _dir(tmp_path)
    real = _dir(tmp_path, "elsewhere")
    ctl.ControlStore.init(real, keys["laptop"].fp, authority=log.source)
    os.symlink(real / ctl.DB_NAME, d / ctl.DB_NAME)
    with pytest.raises(ctl.ConfigRefused, match="symlink"):
        ctl.ControlStore.init(d, keys["laptop"].fp, authority=log.source)


def test_the_wrong_namespace_is_refused(store, log, keys):
    # a contract-sign signature over the very same bytes
    with pytest.raises(ctl.ConfigRefused, match="does not verify"):
        accept(store, log, keys, "verifier", 1, VERIFIERS, ns=DISPATCH_SIGN_NAMESPACE)


def test_a_tampered_body_is_refused(store, log, keys):
    item, sig = signed(store, "verifier", 1, VERIFIERS, keys["laptop"])
    obj = parse(item)
    obj["body"]["verifiers"][0]["verifier_artifact_digest"] = "sha256:" + "d" * 64
    with pytest.raises(ctl.ConfigRefused, match="does not verify"):
        store.accept(canonical_bytes(obj), sig)


def test_a_non_canonical_item_is_refused(store, log, keys):
    item, sig = signed(store, "verifier", 1, VERIFIERS, keys["laptop"])
    loose = item.replace(b'"format":', b'"format": ')
    with pytest.raises(ctl.ConfigRefused, match="canonical"):
        store.accept(loose, keys["laptop"].sign(loose, NS))


@pytest.mark.parametrize("raw, match", [
    (b"[]", "exactly"),
    (raw_item()[:-1] + b',"extra":1}', "exactly"),
    (raw_item(format="levain-dispatch-config/2"), "format"),
    (raw_item(issued_at="today"), "issued_at"),
    (raw_item(kind="jobs"), "kind"),
    (raw_item(body=1.5), "parse"),
    (12, "canonical bytes or text"),
])
def test_item_shape_refusals(store, raw, match):
    with pytest.raises(ctl.ConfigRefused, match=match):
        store.accept(raw, "sig")


def test_accept_uses_the_stores_own_authority():
    # one authority source: the store's, given at construction and checked there
    assert list(inspect.signature(ctl.ControlStore.accept).parameters) == ["self", "item", "signature"]


def test_the_store_takes_the_authority_source_never_a_snapshot(cdir, log):
    with pytest.raises(ctl.ControlStateUnavailable, match="AuthoritySource"):
        ctl.ControlStore(cdir, authority=log.source())


def test_an_empty_or_oversized_signature_is_refused(store, log, keys):
    item, _sig = signed(store, "verifier", 1, VERIFIERS, keys["laptop"])
    with pytest.raises(ctl.ConfigRefused, match="empty"):
        store.accept(item, "")
    with pytest.raises(ctl.ConfigRefused, match="over"):
        store.accept(item, "x" * (ctl.SIGNATURE_MAX + 1))


# --- the pin -------------------------------------------------------------------------------------------------------

def test_the_pin_is_set_once(cdir, log, keys):
    ctl.ControlStore.init(cdir, keys["laptop"].fp, authority=log.source)   # the same root again: a no-op
    with pytest.raises(ctl.ConfigRefused, match="another trust root"):
        ctl.ControlStore.init(cdir, keys["other"].fp, authority=log.source)


def test_init_refuses_a_non_fingerprint(tmp_path, log):
    with pytest.raises(ctl.ConfigRefused, match="fingerprint"):
        ctl.ControlStore.init(_dir(tmp_path), "laptop", authority=log.source)


def test_a_revoked_root_fails_every_read_closed(tmp_path, tmp_path_factory):
    ks = {n: Key(tmp_path_factory.mktemp("revoke-keys"), n) for n in ("laptop", "root")}
    lg = Log(tmp_path_factory.mktemp("revoke-auth"), ks)
    lg.enrol("laptop", "laptop-presence")
    lg.enrol("root", "laptop-presence")
    d = _dir(tmp_path)
    ctl.ControlStore.init(d, ks["root"].fp, authority=lg.source)
    store = ctl.ControlStore(d, authority=lg.source, clock=Clock(NOW))
    accept(store, lg, ks, "verifier", 1, VERIFIERS, key="root")
    assert store.active()["verifier"].generation == 1
    _top, head = store.head()
    lg.revoke("root")
    with pytest.raises(ctl.ControlStateUnavailable, match="revoked"):
        store.active()
    with pytest.raises(ctl.ConfigRefused, match="revoked"):
        accept(store, lg, ks, "verifier", 2, VERIFIERS, key="root", prev=head)


def test_an_empty_store_under_a_revoked_root_reads_closed(tmp_path, tmp_path_factory):
    # L3 r1 (codex): with no item stored, no signature is checked, so the root itself must be judged first
    ks = {n: Key(tmp_path_factory.mktemp("empty-revoke-keys"), n) for n in ("laptop", "root")}
    lg = Log(tmp_path_factory.mktemp("empty-revoke-auth"), ks)
    lg.enrol("laptop", "laptop-presence")
    lg.enrol("root", "laptop-presence")
    d = _dir(tmp_path)
    ctl.ControlStore.init(d, ks["root"].fp, authority=lg.source)
    store = ctl.ControlStore(d, authority=lg.source, clock=Clock(NOW))
    assert store.active() == {} and store.generation() == 0
    lg.revoke("root")
    for read in (store.active, store.head, store.generation):
        with pytest.raises(ctl.ControlStateUnavailable, match="revoked"):
            read()


# --- append-only and fail-closed -----------------------------------------------------------------------------------

def _raw(cdir):
    return sqlite3.connect(cdir / ctl.DB_NAME, isolation_level=None)


@pytest.mark.parametrize("sql", [
    "UPDATE items SET item = 'x'",
    "DELETE FROM items",
    "INSERT OR REPLACE INTO items (generation, kind, item, signature, accepted_at) VALUES (1, 'verifiers', 'x', 's', 't')",
    "INSERT INTO items (generation, kind, item, signature, accepted_at) VALUES (1, 'templates', 'x', 's', 't')",
    "INSERT INTO items (kind, item, signature, accepted_at) VALUES ('templates', 'x', 's', 't')",
    "UPDATE meta SET value = 'SHA256:' || substr(value, 8)",
    "DELETE FROM meta",
    "INSERT OR REPLACE INTO meta (key, value) VALUES ('trust_root', 'x')",
])
def test_the_tables_are_append_only(store, log, keys, cdir, sql):
    accept(store, log, keys, "verifier", 1, VERIFIERS)
    conn = _raw(cdir)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
    finally:
        conn.close()


def test_a_forged_middle_row_fails_every_read_closed(store, log, keys, cdir):
    # well formed, for this store, chained to its predecessor: the next item's prev no longer matches it
    base_config(store, log, keys)
    conn = _raw(cdir)
    try:
        conn.execute("DROP TRIGGER items_frozen")
        forged = tmpl(write_paths=["src/a.py", "src/b.py", "src/new.py", "src/c.py"])
        text = ctl.item_bytes("templates", 2, [forged], NOW, prev=store.active()["verifier"].digest,
                              store_id=store.store_id()).decode()
        conn.execute("UPDATE items SET item = ? WHERE generation = 2", (text,))
    finally:
        conn.close()
    with pytest.raises(ctl.ControlStateUnavailable, match="item 3 does not chain to item 2"):
        store.active()
    with pytest.raises(DispatchError):
        resolver(store)("fix-lint")
    with pytest.raises(DispatchError):
        active_policy(store)


def test_a_row_moved_to_another_generation_fails_closed(store, log, keys, cdir):
    base_config(store, log, keys)
    conn = _raw(cdir)
    try:
        conn.execute("DROP TRIGGER items_frozen")
        conn.execute("UPDATE items SET generation = 9 WHERE generation = 3")
    finally:
        conn.close()
    with pytest.raises(ctl.ControlStateUnavailable, match="does not match its row"):
        store.active()


def test_no_store_and_no_pin_refuse(tmp_path, log):
    d = _dir(tmp_path)
    store = ctl.ControlStore(d, authority=log.source)
    with pytest.raises(ctl.ControlStateUnavailable, match="created by init"):
        store.active()
    with pytest.raises(ctl.ControlStateUnavailable):
        resolver(store)("fix-lint")
    conn = sqlite3.connect(d / ctl.DB_NAME)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit()
    conn.close()
    with pytest.raises(ctl.ControlStateUnavailable, match="format"):
        store.active()


def test_an_unreadable_authority_fails_reads_closed(cdir, store, log, keys, tmp_path):
    from levain.consent.queue import authority_source

    accept(store, log, keys, "verifier", 1, VERIFIERS)
    blind = ctl.ControlStore(cdir, authority=authority_source(tmp_path / "missing" / "cockpit.db", anchor=None))
    with pytest.raises(ctl.ControlStateUnavailable, match="authority log"):
        blind.active()


def test_a_directory_others_can_reach_is_refused(cdir, log):
    os.chmod(cdir, 0o755)
    try:
        with pytest.raises(ctl.ControlStateUnavailable, match="0700"):
            ctl.ControlStore(cdir, authority=log.source).active()
    finally:
        os.chmod(cdir, 0o700)


def test_no_policy_is_never_an_empty_one(store, log, keys):
    accept(store, log, keys, "verifier", 1, VERIFIERS)
    with pytest.raises(DispatchError, match="no provider policy"):
        active_policy(store)


# --- end to end ----------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    return make_repo(tmp_path_factory.mktemp("control-repo"))


def test_end_to_end_a_contract_files_through_the_active_configuration(store, log, keys, repo, tmp_path):
    base_config(store, log, keys)
    path, base = repo
    jobs = JobStore(_dir(tmp_path, "jobs"), clock=Clock(NOW))
    job, cap = jobs.file(submission(path, base), resolve_template=resolver(store), policy=active_policy(store),
                         filer_session="sess-1")
    assert cap and job.state == "filed"
    c = job.contract
    assert c.template.tier == "R0" and c.template.content_hash == tmpl()["content_hash"]
    assert c.template.verifier_hash == BB and c.policy_hash == POLICY.content_hash()
    # an id the active registry does not hold is R2, never dispatched
    with pytest.raises(ContractRefused, match="not registered"):
        jobs.file(submission(path, base, template="fix-docs"), resolve_template=resolver(store),
                  policy=active_policy(store), filer_session="sess-2")


def test_end_to_end_a_store_that_does_not_verify_files_nothing(store, log, keys, repo, tmp_path, cdir):
    base_config(store, log, keys)
    policy = active_policy(store)
    conn = _raw(cdir)
    try:
        conn.execute("DROP TRIGGER items_frozen")
        conn.execute("UPDATE items SET signature = 'x' WHERE generation = 2")
    finally:
        conn.close()
    path, base = repo
    jobs = JobStore(_dir(tmp_path, "jobs"), clock=Clock(NOW))
    with pytest.raises(ContractRefused, match="resolver failed"):
        jobs.file(submission(path, base), resolve_template=resolver(store), policy=policy, filer_session="s")
    assert jobs.list() == []
