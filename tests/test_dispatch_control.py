"""Dispatch configuration: one signed item per change, one generation over every kind, a trust root pinned once.
Keys are real ssh-keygen keys enrolled in a real signed authority log (the consent queue tests' ``Key`` and ``Log``),
the store is real SQLite in tmp, and the end-to-end filing runs over the contract tests' real git repo."""
from __future__ import annotations

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
from levain.dispatch.registry import active_policy, resolver, template_content_hash, template_from_mapping
from levain.dispatch.sign import DISPATCH_SIGN_NAMESPACE
from tests.test_consent_queue import Key, Log
from tests.test_dispatch_contract import NOW, POLICY, make_repo, submission, template
from tests.test_dispatch_sign import passkey_line

NS = ctl.CONFIG_NAMESPACE
BB = "sha256:" + "b" * 64
IP = "sha256:" + "c" * 64
VERIFIERS = {"pytest-bb": {"artifact_hash": BB, "kind": "black-box"},
             "pytest-ip": {"artifact_hash": IP, "kind": "in-process"}}
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
def cdir(tmp_path, keys):
    d = _dir(tmp_path)
    ctl.ControlStore.init(d, keys["laptop"].fp)
    return d


@pytest.fixture
def store(cdir, log):
    return ctl.ControlStore(cdir, authority=log.source, clock=Clock(NOW))


def signed(kind, gen, body, key, *, ns=NS, at=None):
    item = ctl.item_bytes(kind, gen, body, at or NOW - timedelta(minutes=1))
    return item, key.sign(item, ns)


def accept(store, log, keys, kind, gen, body, key="laptop", **kw):
    item, sig = signed(kind, gen, body, keys[key], **kw)
    return store.accept(item, sig, log.source)


def base_config(store, log, keys):
    accept(store, log, keys, "verifiers", 1, VERIFIERS)
    accept(store, log, keys, "templates", 2, [tmpl()])
    accept(store, log, keys, "provider_policy", 3, POLICY.to_mapping())


# --- accept and read back ------------------------------------------------------------------------------------------

def test_accept_and_resolve_round_trip(store, log, keys):
    base_config(store, log, keys)
    active = store.active()
    assert set(active) == {"verifiers", "templates", "provider_policy"} and store.generation() == 3
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
    item, sig = signed("verifiers", 1, VERIFIERS, keys["laptop"])
    assert store.accept(item.decode("utf-8"), sig, log.source).generation == 1


# --- generation ----------------------------------------------------------------------------------------------------

def test_a_replayed_older_item_is_refused(store, log, keys):
    old, old_sig = signed("verifiers", 1, VERIFIERS, keys["laptop"])
    store.accept(old, old_sig, log.source)
    accept(store, log, keys, "verifiers", 2, {"pytest-bb": VERIFIERS["pytest-bb"]})
    with pytest.raises(ctl.ConfigRefused, match="not above the highest accepted"):
        store.accept(old, old_sig, log.source)
    assert store.active()["verifiers"].generation == 2


def test_the_generation_spans_every_kind(store, log, keys):
    # a policy item signed at generation 1 and held back is refused once ANY kind has passed it
    held, held_sig = signed("provider_policy", 1, POLICY.to_mapping(), keys["laptop"])
    accept(store, log, keys, "verifiers", 2, VERIFIERS)
    with pytest.raises(ctl.ConfigRefused, match=r"generation 1 is not above the highest accepted \(2\)"):
        store.accept(held, held_sig, log.source)
    with pytest.raises(ctl.ConfigRefused, match="not above"):
        accept(store, log, keys, "templates", 2, [tmpl()])
    accept(store, log, keys, "templates", 7, [tmpl()])      # gaps are fine; only order matters
    assert store.generation() == 7


@pytest.mark.parametrize("gen", [0, -1, True, "3", 2 ** 53])
def test_generation_is_a_positive_integer(store, log, keys, gen):
    item = canonical_bytes({"format": ctl.FORMAT, "generation": gen, "issued_at": "2026-10-19T19:59:00.000000Z",
                            "kind": "verifiers", "body": VERIFIERS}) if gen != 2 ** 53 else \
        b'{"body":{},"format":"levain-dispatch-config/1","generation":9007199254740992,' \
        b'"issued_at":"2026-10-19T19:59:00.000000Z","kind":"verifiers"}'
    with pytest.raises(ctl.ConfigRefused):
        store.accept(item, keys["laptop"].sign(item, NS), log.source)


# --- the signer ----------------------------------------------------------------------------------------------------

def test_a_non_pinned_signer_is_refused_even_at_t3(store, log, keys):
    for name in ("other", "phone", "stranger"):
        with pytest.raises(ctl.ConfigRefused, match="does not verify as the pinned trust root's"):
            accept(store, log, keys, "verifiers", 1, VERIFIERS, key=name)
    assert store.generation() == 0


def test_a_t2_display_phone_root_cannot_sign(tmp_path, log, keys):
    d = _dir(tmp_path, "phone-root")
    ctl.ControlStore.init(d, keys["phone"].fp)
    store = ctl.ControlStore(d, authority=log.source, clock=Clock(NOW))
    with pytest.raises(ctl.ConfigRefused, match="key class display-phone does not cover T3"):
        accept(store, log, keys, "verifiers", 1, VERIFIERS, key="phone")


def test_a_passkey_root_cannot_sign(tmp_path, log, keys):
    d = _dir(tmp_path, "passkey-root")
    ctl.ControlStore.init(d, fingerprint_of(passkey_line()))
    store = ctl.ControlStore(d, authority=log.source, clock=Clock(NOW))
    item, _sig = signed("verifiers", 1, VERIFIERS, keys["laptop"])
    with pytest.raises(ctl.ConfigRefused, match="passkey"):
        store.accept(item, "-----BEGIN SSH SIGNATURE-----\nAAAA\n-----END SSH SIGNATURE-----\n", log.source)


def test_the_wrong_namespace_is_refused(store, log, keys):
    # a contract-sign signature over the very same bytes
    with pytest.raises(ctl.ConfigRefused, match="does not verify"):
        accept(store, log, keys, "verifiers", 1, VERIFIERS, ns=DISPATCH_SIGN_NAMESPACE)


def test_a_tampered_body_is_refused(store, log, keys):
    item, sig = signed("verifiers", 1, VERIFIERS, keys["laptop"])
    obj = parse(item)
    obj["body"]["pytest-bb"]["artifact_hash"] = "sha256:" + "d" * 64
    with pytest.raises(ctl.ConfigRefused, match="does not verify"):
        store.accept(canonical_bytes(obj), sig, log.source)


def test_a_non_canonical_item_is_refused(store, log, keys):
    item, sig = signed("verifiers", 1, VERIFIERS, keys["laptop"])
    loose = item.replace(b'"format":', b'"format": ')
    with pytest.raises(ctl.ConfigRefused, match="canonical"):
        store.accept(loose, keys["laptop"].sign(loose, NS), log.source)


@pytest.mark.parametrize("raw, match", [
    (b"[]", "exactly"),
    (b'{"body":{},"format":"levain-dispatch-config/2","generation":1,"issued_at":"2026-10-19T19:59:00.000000Z",'
     b'"kind":"verifiers"}', "format"),
    (b'{"body":{},"format":"levain-dispatch-config/1","generation":1,"issued_at":"today","kind":"verifiers"}',
     "issued_at"),
    (b'{"body":{},"format":"levain-dispatch-config/1","generation":1,"issued_at":"2026-10-19T19:59:00.000000Z",'
     b'"kind":"jobs"}', "kind"),
    (b'{"body":1.5,"format":"levain-dispatch-config/1","generation":1,"issued_at":"2026-10-19T19:59:00.000000Z",'
     b'"kind":"verifiers"}', "parse"),
    (12, "canonical bytes or text"),
])
def test_item_shape_refusals(store, log, raw, match):
    with pytest.raises(ctl.ConfigRefused, match=match):
        store.accept(raw, "sig", log.source)


def test_accept_takes_the_authority_source_never_a_snapshot(store, log, keys):
    item, sig = signed("verifiers", 1, VERIFIERS, keys["laptop"])
    with pytest.raises(ctl.ConfigRefused, match="AuthoritySource"):
        store.accept(item, sig, log.source())


def test_an_empty_or_oversized_signature_is_refused(store, log, keys):
    item, _sig = signed("verifiers", 1, VERIFIERS, keys["laptop"])
    with pytest.raises(ctl.ConfigRefused, match="empty"):
        store.accept(item, "", log.source)
    with pytest.raises(ctl.ConfigRefused, match="over"):
        store.accept(item, "x" * (ctl.SIGNATURE_MAX + 1), log.source)


# --- the pin -------------------------------------------------------------------------------------------------------

def test_the_pin_is_set_once(cdir, keys):
    ctl.ControlStore.init(cdir, keys["laptop"].fp)           # the same root again: a no-op
    with pytest.raises(ctl.ConfigRefused, match="another trust root"):
        ctl.ControlStore.init(cdir, keys["other"].fp)


def test_init_refuses_a_non_fingerprint(tmp_path):
    with pytest.raises(ctl.ConfigRefused, match="fingerprint"):
        ctl.ControlStore.init(_dir(tmp_path), "laptop")


def test_a_revoked_root_fails_every_read_closed(tmp_path, tmp_path_factory):
    ks = {n: Key(tmp_path_factory.mktemp("revoke-keys"), n) for n in ("laptop", "root")}
    lg = Log(tmp_path_factory.mktemp("revoke-auth"), ks)
    lg.enrol("laptop", "laptop-presence")
    lg.enrol("root", "laptop-presence")
    d = _dir(tmp_path)
    ctl.ControlStore.init(d, ks["root"].fp)
    store = ctl.ControlStore(d, authority=lg.source, clock=Clock(NOW))
    item, sig = signed("verifiers", 1, VERIFIERS, ks["root"])
    store.accept(item, sig, lg.source)
    assert store.active()["verifiers"].generation == 1
    lg.revoke("root")
    with pytest.raises(ctl.ControlStateUnavailable, match="revoked"):
        store.active()
    with pytest.raises(ctl.ConfigRefused, match="revoked"):
        accept(store, lg, ks, "verifiers", 2, VERIFIERS, key="root")


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
    accept(store, log, keys, "verifiers", 2, VERIFIERS)
    conn = _raw(cdir)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
    finally:
        conn.close()


def test_a_forged_row_fails_every_read_closed(store, log, keys, cdir):
    base_config(store, log, keys)
    conn = _raw(cdir)
    try:
        conn.execute("DROP TRIGGER items_frozen")
        forged = tmpl(write_paths=["src/a.py", "src/b.py", "src/new.py", "src/c.py"])
        text = ctl.item_bytes("templates", 2, [forged], NOW).decode()
        conn.execute("UPDATE items SET item = ? WHERE generation = 2", (text,))
    finally:
        conn.close()
    with pytest.raises(ctl.ControlStateUnavailable, match="does not verify"):
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

    accept(store, log, keys, "verifiers", 1, VERIFIERS)
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
    accept(store, log, keys, "verifiers", 1, VERIFIERS)
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
