"""CH-D2 S6: the setup record in the authority log (design §3.8.5(o) item 4).

Rows: R15-21-4 (one live record per (entity, canonical host), refused at setup; supersession is one write; a store
holding two live records refuses the launch naming both: codex's case), R15-18-L1's ruling red ("two live records for
one canonical host -> launch refused, no merge, no pick"), R15-21-6 (`Example.COM.` and `example.com.` are one host:
the second refused at setup), and the setup / revoke / list CLI. A real scratch log (ssh-keygen, tmp key); the
credential store is never touched here: a setup record names a SOURCE, never a value.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

import levain.authority as auth
from levain.chat_egress import key_setup, keys
from levain.cli import main

from .keys_rig import KEY, SOURCE, Cockpit, artifact_hits, record


@pytest.fixture
def ck(tmp_path):
    return Cockpit(tmp_path)


def _setup(ck, host="example.com", **kw):
    args = {"entity": "ent", "host": host, "source": SOURCE, "placement": {"scheme": "bearer"},
            "repos": ["/o/r.git"], "expires": timedelta(days=7)}
    return key_setup.setup_key(ck.root, **{**ck.kw(), **args, **kw})


def _live(ck):
    now = datetime.now(timezone.utc)
    return sorted(g["id"] for g in ck.state().policy.grants.values() if auth.egress_key_live(g, now))


# --- the record kind ---------------------------------------------------------------------------------------------

def test_a_setup_record_is_a_laptop_signed_create_and_replays_as_its_kind(ck):
    rec, warning = _setup(ck, host="Example.COM.")
    assert warning is None
    g = ck.state().policy.grants[rec["id"]]
    assert g["kind"] == auth.EGRESS_KEY_KIND and g["host"] == "example.com"     # stored canonical (R15-21-6)
    assert g["source"] == SOURCE and KEY not in repr(g)
    assert auth.egress_key_record(g) and not auth.consent_grant(g) and not auth.inert_grant(g)


@pytest.mark.parametrize("field, value, said", [
    ("host", "Example.com", "canonical"), ("host", "10.0.0.1", "DNS name"), ("host", "*.example.com", "canonical"),
    ("source", "", "source"), ("placement", {"scheme": "digest"}, "placement"),
    ("placement", {"scheme": "basic"}, "placement"), ("repos", ["/b.git", "/a.git"], "sorted"),
    ("repos", ["o/r.git"], "repo"), ("repos", ["/o/r/"], "repo"), ("repos", ["/o/../r"], "repo"),
])
def test_the_log_refuses_a_malformed_record(ck, field, value, said):
    bad = record("ek-000000000001", "example.com")
    bad[field] = value
    with pytest.raises(auth.AuthorityError, match=said):
        ck.link({"op": "create_grant", "grant": bad})


def test_a_record_lives_at_most_30_days_so_an_older_levain_holds_it_inert(ck):
    with pytest.raises(auth.AuthorityError, match="30 days"):
        ck.link({"op": "create_grant", "grant": record("ek-000000000001", "example.com", days=31)})


# --- R15-21-4 / R15-21-6: one live record per (entity, canonical host) -----------------------------------------

def test_r15_21_4_a_second_live_record_for_one_host_is_refused_at_setup(ck):
    first, _ = _setup(ck)
    with pytest.raises(key_setup.SetupRefused, match="already has a live setup record"):
        _setup(ck, repos=["/o/other.git"])
    assert _live(ck) == [first["id"]]


def test_r15_21_6_example_com_dot_while_Example_com_is_live_is_refused_at_setup(ck):
    first, _ = _setup(ck, host="Example.com")
    with pytest.raises(key_setup.SetupRefused, match="already has a live setup record"):
        _setup(ck, host="example.com.")
    assert _live(ck) == [first["id"]]


def test_r15_21_4_the_log_itself_refuses_a_second_live_record_at_append(ck):
    """The rule is the log's, not only the setup command's: a raw create_grant is refused too."""
    ck.link({"op": "create_grant", "grant": record("ek-00000000000a", "example.com")})
    with pytest.raises(auth.AuthorityError, match="one live setup record"):
        ck.link({"op": "create_grant", "grant": record("ek-00000000000b", "example.com", repos=("/b.git",))})


def test_another_entity_or_host_is_its_own_record(ck):
    a, _ = _setup(ck)
    b, _ = _setup(ck, entity="other")
    c, _ = _setup(ck, host="git.example.org")
    assert _live(ck) == sorted([a["id"], b["id"], c["id"]])


def test_r15_21_4_a_supersession_leaves_exactly_one_live_record_after_its_one_write(ck):
    old, _ = _setup(ck)
    seq_before = ck.state().seq
    new, _ = _setup(ck, replace=True, repos=["/o/r.git", "/o/s.git"])
    state = ck.state()
    assert state.seq == seq_before + 1                       # one link: the new revokes the old in the same write
    assert _live(ck) == [new["id"]] and old["id"] not in state.policy.grants


def test_a_replace_with_nothing_to_replace_is_refused(ck):
    with pytest.raises(key_setup.SetupRefused, match="nothing to replace"):
        _setup(ck, replace=True)


def test_r15_21_4_codex_case_cannot_be_made_live_and_a_store_holding_both_refuses_the_launch(ck, monkeypatch):
    """Records A and B for host H, repos /a.git and /b.git: refused at setup; a store written to hold both (the
    append rule switched off, as a foreign writer would) refuses the launch, naming both: no merge, no pick."""
    ck.link({"op": "create_grant", "grant": record("ek-0000000000aa", "h.example", repos=("/a.git",))})
    with pytest.raises(auth.AuthorityError, match="one live setup record"):
        ck.link({"op": "create_grant", "grant": record("ek-0000000000bb", "h.example", repos=("/b.git",))})
    with monkeypatch.context() as m:
        m.setattr(auth, "_egress_key_append_check", lambda state, rec, now: None)
        ck.link({"op": "create_grant", "grant": record("ek-0000000000bb", "h.example", repos=("/b.git",))})
    with pytest.raises(keys.LaunchRefused) as exc:
        keys.build_route_set(ck.state(), entity="ent", now=datetime.now(timezone.utc))
    assert "ek-0000000000aa" in str(exc.value) and "ek-0000000000bb" in str(exc.value)
    assert "h.example" in str(exc.value)


def test_an_expired_record_is_not_live_and_does_not_block_a_new_one(ck):
    ck.link({"op": "create_grant", "grant": record("ek-0000000000ee", "example.com", days=1)})
    later = datetime.now(timezone.utc) + timedelta(days=2)              # the old record has expired by then
    new, _ = _setup(ck, clock=lambda: later)
    grants = ck.state().policy.grants
    assert sorted(g for g in grants if auth.egress_key_live(grants[g], later)) == [new["id"]]
    assert "ek-0000000000ee" in grants


# --- revoke beside it --------------------------------------------------------------------------------------------

def test_revoke_removes_the_record(ck):
    rec, _ = _setup(ck)
    seq, warning = key_setup.revoke_key(ck.root, rec["id"], **ck.kw())
    assert warning is None and rec["id"] not in ck.state().policy.grants
    with pytest.raises(key_setup.SetupRefused, match="no setup record"):
        key_setup.revoke_key(ck.root, rec["id"], **ck.kw())


def test_revoke_refuses_a_consent_grant_id(ck, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert main(["consent", "grant", "create", "--data-root", str(ck.root), "--agent", "claude_code", "--tool",
                 "Read", "--workspace", str(ws), "--posture", "default", "--expires", "2h", "--id", "cg-x",
                 "--signing-key", str(ck.key)]) == 0
    with pytest.raises(key_setup.SetupRefused, match="no setup record"):
        key_setup.revoke_key(ck.root, "cg-x", **ck.kw())


# --- the CLI ------------------------------------------------------------------------------------------------------

def test_cli_setup_list_revoke(ck, capsys, tmp_path):
    base = ["--data-root", str(ck.root)]
    assert main(["chat", "key", "setup", "--entity", "ent", "--host", "Example.COM.", "--source", SOURCE,
                 "--repo", "/o/r.git", "--expires", "7d", "--signing-key", str(ck.key), *base]) == 0
    out = capsys.readouterr().out
    assert "example.com" in out and SOURCE in out
    rid = _live(ck)[0]
    assert main(["chat", "key", "setup", "--entity", "ent", "--host", "example.com", "--source", SOURCE,
                 "--expires", "7d", "--signing-key", str(ck.key), *base]) == 1
    assert "already has a live setup record" in capsys.readouterr().err
    assert main(["chat", "key", "setup", "--entity", "ent", "--host", "example.com", "--source", SOURCE,
                 "--basic-user", "x-access-token", "--replace", "--expires", "7d", "--signing-key", str(ck.key),
                 *base]) == 0
    capsys.readouterr()
    (new,) = _live(ck)
    assert new != rid and ck.state().policy.grants[new]["placement"] == {"scheme": "basic", "user": "x-access-token"}
    assert main(["chat", "key", "list", *base]) == 0
    listing = capsys.readouterr().out
    assert new in listing and "live" in listing and rid not in listing
    assert main(["chat", "key", "revoke", new, "--signing-key", str(ck.key), *base]) == 0
    assert _live(ck) == []
    assert artifact_hits(tmp_path) == []


@pytest.mark.parametrize("opt", ["--value", "--key", "--token", "--secret", "--password", "--credential"])
def test_setup_takes_no_value_anywhere(ck, opt):
    """The setup command cannot be handed the value: it names a source only (and the command exists)."""
    with pytest.raises(SystemExit) as ok:
        main(["chat", "key", "setup", "--help"])
    assert ok.value.code == 0
    with pytest.raises(SystemExit) as exc:
        main(["chat", "key", "setup", "--entity", "ent", "--host", "example.com", "--source", SOURCE, "--expires",
              "7d", "--signing-key", str(ck.key), "--data-root", str(ck.root), opt, KEY])
    assert exc.value.code == 2
