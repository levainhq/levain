"""CH-D2 S6: the setup record in the authority log (design §3.8.5(o) item 4).

Rows: R15-21-4 (one live record per (entity, canonical host), refused at setup; supersession is one write; a store
holding two live records refuses the launch naming both: codex's case), R15-18-L1's ruling red ("two live records for
one canonical host -> launch refused, no merge, no pick"), R15-21-6 (`Example.COM.` and `example.com.` are one host:
the second refused at setup), and the setup / revoke / list CLI. A real scratch log (ssh-keygen, tmp key); the
credential store is never touched here: a setup record names a SOURCE, never a value.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import levain.authority as auth
from levain.chat_egress import key_setup, keys
from levain.cli import main

from .keys_rig import KEY, SOURCE, Cockpit, artifact_hits, record

ACCOUNT = "levain-chat-test-account"


class FakeKeychain:
    """The keychain-item check a test hands setup: how many items match (service, account), and every ask kept.
    Never the real keychain (a real item is a person's, S10)."""

    def __init__(self, n=1):
        self.n = n
        self.asked = []

    def count(self, service, account):
        self.asked.append((service, account))
        return self.n


@pytest.fixture
def ck(tmp_path):
    return Cockpit(tmp_path)


@pytest.fixture
def one_item(monkeypatch):
    """The CLI's keychain check answers one matching item (the CLI builds the real one itself)."""
    monkeypatch.setattr(key_setup.KeychainItems, "count", lambda self, service, account: 1)


def _setup(ck, host="example.com", **kw):
    args = {"entity": "ent", "host": host, "source": SOURCE, "account": ACCOUNT, "placement": {"scheme": "bearer"},
            "repos": ["/o/r.git"], "expires": timedelta(days=7), "confirm": lambda preview: True,
            "keychain": FakeKeychain()}
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

def test_cli_setup_list_revoke(ck, capsys, tmp_path, one_item):
    base = ["--data-root", str(ck.root)]
    assert main(["chat", "key", "setup", "--entity", "ent", "--host", "Example.COM.", "--source", SOURCE,
                 "--account", ACCOUNT, "--yes",
                 "--repo", "/o/r.git", "--expires", "7d", "--signing-key", str(ck.key), *base]) == 0
    out = capsys.readouterr().out
    assert "example.com" in out and SOURCE in out
    rid = _live(ck)[0]
    assert main(["chat", "key", "setup", "--entity", "ent", "--host", "example.com", "--source", SOURCE,
                 "--account", ACCOUNT, "--yes",
                 "--expires", "7d", "--signing-key", str(ck.key), *base]) == 1
    assert "already has a live setup record" in capsys.readouterr().err
    assert main(["chat", "key", "setup", "--entity", "ent", "--host", "example.com", "--source", SOURCE,
                 "--account", ACCOUNT, "--yes",
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
        main(["chat", "key", "setup", "--entity", "ent", "--host", "example.com", "--source", SOURCE,
              "--account", ACCOUNT, "--yes", "--expires",
              "7d", "--signing-key", str(ck.key), "--data-root", str(ck.root), opt, KEY])
    assert exc.value.code == 2


# --- K-8: declared repos come ONLY from the signed record --------------------------------------------------------

def _git(*args):
    import subprocess

    subprocess.run(["git", *args], check=True, capture_output=True, timeout=30)


def test_k8_a_git_config_rewritten_after_setup_adds_no_declared_repo(ck, tmp_path):
    """Setup (Phill present) reads the workspace's remotes ONCE and signs them into the record. The sandbox can write
    the workspace's `.git/config`; a rewrite after setup (origin moved to B, a second remote C) declares nothing: the
    launch reads the record's repo list only, and takes no workspace input at all."""
    import inspect

    ws = tmp_path / "ws"
    ws.mkdir()
    _git("init", "-q", str(ws))
    _git("-C", str(ws), "remote", "add", "origin", "https://example.com/o/a.git")
    rec, _ = _setup(ck, repos=[], workspace=ws)
    assert rec["repos"] == ["/o/a.git"]                                  # signed into the record at setup
    _git("-C", str(ws), "remote", "set-url", "origin", "https://example.com/o/b.git")
    _git("-C", str(ws), "remote", "add", "c", "https://example.com/o/c.git")
    assert "workspace_remotes" not in inspect.signature(keys.build_route_set).parameters
    s = keys.build_route_set(ck.state(), entity="ent", now=datetime.now(timezone.utc))
    assert s.routes["example.com"].declared_repos == frozenset({"/o/a.git"})


# --- F6 (K-8 obs-1 ruling): one read, shown, confirmed, then exactly that is signed --------------------------------

def _ws(tmp_path, *remotes):
    ws = tmp_path / "ws"
    ws.mkdir()
    _git("init", "-q", str(ws))
    for name, url in remotes:
        _git("-C", str(ws), "remote", "add", name, url)
    return ws


def _setup_records(ck):
    return sorted(g["id"] for g in ck.state().policy.grants.values() if auth.egress_key_record(g))


def test_f6_confirm_is_asked_before_the_append_and_what_is_signed_is_what_was_shown(ck, tmp_path, monkeypatch):
    ws = _ws(tmp_path, ("origin", "https://example.com/o/a.git"))
    reads = []
    real = key_setup.workspace_remote_urls
    monkeypatch.setattr(key_setup, "workspace_remote_urls", lambda w: reads.append(w) or real(w))
    seen = {}

    def confirm(preview):
        seen["seq"] = ck.state().seq                   # nothing appended yet
        seen["record"] = dict(preview.record)
        # the sandbox rewrites the config while the operator reads the prompt: no re-read picks it up
        _git("-C", str(ws), "remote", "add", "late", "https://example.com/o/late.git")
        return True

    seq_before = ck.state().seq
    rec, _ = _setup(ck, repos=["/o/r.git"], workspace=ws, confirm=confirm)
    assert seen["seq"] == seq_before and ck.state().seq == seq_before + 1
    assert rec == seen["record"] and rec["repos"] == ["/o/a.git", "/o/r.git"]
    assert len(reads) == 1


def test_f6_no_appends_nothing(ck):
    seq_before = ck.state().seq
    with pytest.raises(key_setup.SetupDeclined, match="nothing was signed"):
        _setup(ck, confirm=lambda preview: False)
    assert ck.state().seq == seq_before and _setup_records(ck) == []


def test_f6_the_preview_names_every_remote_on_the_host_it_left_out(tmp_path):
    ws = _ws(tmp_path, ("origin", "https://example.com/o/a.git"), ("ssh", "git@example.com:o/s.git"),
             ("plain", "http://example.com/o/p.git"), ("port", "https://example.com:8443/o/q.git"),
             ("bare", "https://example.com/"), ("walls", "https://example.com/o/../x.git"),
             ("elsewhere", "https://other.example/o/z.git"))
    declared, excluded = key_setup._read_workspace(ws, "example.com")
    assert declared == ("/o/a.git",)
    why = {e.url: e.why for e in excluded}
    assert set(why) == {"git@example.com:o/s.git", "http://example.com/o/p.git", "https://example.com:8443/o/q.git",
                        "https://example.com/", "https://example.com/o/../x.git"}      # not other.example's
    assert "ssh" in why["git@example.com:o/s.git"] and "http " in why["http://example.com/o/p.git"]
    assert "8443" in why["https://example.com:8443/o/q.git"] and "no repo path" in why["https://example.com/"]


def test_f6_cli_no_on_a_terminal_appends_nothing_and_the_prompt_follows_the_listing(ck, capsys, monkeypatch,
                                                                                    tmp_path, one_item):
    ws = _ws(tmp_path, ("origin", "https://example.com/o/a.git"), ("ssh", "git@example.com:o/s.git"))
    monkeypatch.setattr("sys.stdin", type("Tty", (), {"isatty": staticmethod(lambda: True)})())
    asked = []

    def answer(prompt):
        asked.append((prompt, capsys.readouterr().out, ck.state().seq))
        return "n"

    monkeypatch.setattr("builtins.input", answer)
    seq_before = ck.state().seq
    assert main(["chat", "key", "setup", "--entity", "ent", "--host", "example.com", "--source", SOURCE,
                 "--account", ACCOUNT, "--workspace", str(ws), "--expires", "7d", "--signing-key", str(ck.key),
                 "--data-root", str(ck.root)]) == 1
    (prompt, shown, seq_at_prompt), = asked
    assert "[y/N]" in prompt and seq_at_prompt == seq_before
    assert "/o/a.git" in shown and "left out: git@example.com:o/s.git" in shown and ACCOUNT in shown
    assert "nothing was signed" in capsys.readouterr().err
    assert ck.state().seq == seq_before and _setup_records(ck) == []


def test_f6_cli_yes_on_a_terminal_signs(ck, capsys, monkeypatch, one_item):
    monkeypatch.setattr("sys.stdin", type("Tty", (), {"isatty": staticmethod(lambda: True)})())
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert main(["chat", "key", "setup", "--entity", "ent", "--host", "example.com", "--source", SOURCE,
                 "--account", ACCOUNT, "--repo", "/o/r.git", "--expires", "7d", "--signing-key", str(ck.key),
                 "--data-root", str(ck.root)]) == 0
    assert len(_setup_records(ck)) == 1


def test_f6_cli_not_a_terminal_without_yes_signs_nothing(ck, capsys, one_item):
    seq_before = ck.state().seq
    assert main(["chat", "key", "setup", "--entity", "ent", "--host", "example.com", "--source", SOURCE,
                 "--account", ACCOUNT, "--repo", "/o/r.git", "--expires", "7d", "--signing-key", str(ck.key),
                 "--data-root", str(ck.root)]) == 1
    assert "pass --yes" in capsys.readouterr().err
    assert ck.state().seq == seq_before


def test_f6_cli_has_no_workspace_default_and_says_so(ck, capsys, monkeypatch, tmp_path, one_item):
    """Run from inside a repo whose origin is on the host: without --workspace nothing of it is read or declared."""
    ws = _ws(tmp_path, ("origin", "https://example.com/o/a.git"))
    monkeypatch.chdir(ws)
    assert main(["chat", "key", "setup", "--entity", "ent", "--host", "example.com", "--source", SOURCE,
                 "--account", ACCOUNT, "--repo", "/o/r.git", "--yes", "--expires", "7d", "--signing-key",
                 str(ck.key), "--data-root", str(ck.root)]) == 0
    out = capsys.readouterr().out
    assert "no --workspace: only the --repo paths above are declared" in out
    (rid,) = _setup_records(ck)
    assert ck.state().policy.grants[rid]["repos"] == ("/o/r.git",)


def test_f6_a_remote_cannot_draw_a_line_of_its_own_in_the_preview(capsys):
    from levain.cli import _confirm_setup

    rec = {"id": "ek-000000000001", "entity": "ent", "host": "example.com", "source": SOURCE, "account": ACCOUNT,
           "placement": {"scheme": "bearer"}, "repos": [], "expires_at": "2026-11-01T00:00:00Z"}
    planted = key_setup.Excluded("https://example.com:1/x\n    /o/fake.git  (--repo)", "port 1")
    preview = key_setup.SetupPreview(rec, Path("/w"), (), (planted,), ())
    assert _confirm_setup(preview, yes=True) is True
    out = capsys.readouterr().out
    assert "\n    /o/fake.git" not in out and "⟨U+000A⟩" in out


# --- F5: the keychain item is service AND account, and exactly one item --------------------------------------------

def test_f5_the_record_carries_the_account_beside_the_service(ck):
    kc = FakeKeychain(1)
    rec, _ = _setup(ck, keychain=kc)
    assert rec["source"] == SOURCE and rec["account"] == ACCOUNT
    assert ck.state().policy.grants[rec["id"]]["account"] == ACCOUNT
    assert kc.asked == [(SOURCE, ACCOUNT)]


@pytest.mark.parametrize("n, said", [(0, "no keychain item"), (2, "2 keychain items")])
def test_f5_a_missing_or_ambiguous_item_is_refused_before_anything_is_signed(ck, n, said):
    seq_before = ck.state().seq
    with pytest.raises(key_setup.SetupRefused, match=said):
        _setup(ck, keychain=FakeKeychain(n), confirm=lambda p: pytest.fail("confirm asked for a refused item"))
    assert ck.state().seq == seq_before


def test_f5_an_empty_account_is_refused(ck):
    with pytest.raises(key_setup.SetupRefused, match="account"):
        _setup(ck, account="")


def test_f5_the_cli_requires_account(ck):
    with pytest.raises(SystemExit) as exc:
        main(["chat", "key", "setup", "--entity", "ent", "--host", "example.com", "--source", SOURCE, "--yes",
              "--expires", "7d", "--signing-key", str(ck.key), "--data-root", str(ck.root)])
    assert exc.value.code == 2


def test_f5_the_keychain_check_reads_attributes_only_and_counts_per_keychain(tmp_path):
    """The real KeychainItems against a fake `security` (never the person's keychain): no `-w`/`-g` is ever passed,
    each keychain in the search list is asked for service AND account."""
    log = tmp_path / "argv.log"
    fake = tmp_path / "security"
    fake.write_text("#!/bin/sh\n"
                    f"echo \"$@\" >> {log}\n"
                    "if [ \"$1\" = list-keychains ]; then printf '    \"/k/a.keychain-db\"\\n    \"/k/b.keychain-db\"\\n'; "
                    "exit 0; fi\n"
                    "case \"$*\" in *b.keychain-db*) exit 44;; esac\nexit 0\n")
    fake.chmod(0o755)
    assert key_setup.KeychainItems(str(fake)).count("svc", "acct") == 1
    calls = log.read_text().splitlines()
    assert calls[0] == "list-keychains -d user"
    assert calls[1:] == ["find-generic-password -s svc -a acct /k/a.keychain-db",
                         "find-generic-password -s svc -a acct /k/b.keychain-db"]
    assert not any(" -w" in c or " -g" in c for c in calls)


# --- F7: a replace built on a snapshot the log has moved past is refused ------------------------------------------

def test_f7_a_replace_does_not_undo_a_link_appended_after_its_snapshot(ck, monkeypatch):
    """Reproduced on 151ddfe4: the replace's full-policy loosen, built from the captured snapshot, chained onto the
    newer head and dropped the grant created in between."""
    _setup(ck)
    real = key_setup.prepare

    def racing(conn, op, **kw):
        ck.link({"op": "create_grant", "grant": {**record("ek-0000000000cc", "other.example", entity="b"),
                                                 "account": ACCOUNT}})
        return real(conn, op, **{**kw, "now": datetime.now(timezone.utc)})   # after the racing link

    monkeypatch.setattr(key_setup, "prepare", racing)
    with pytest.raises(key_setup.SetupRefused, match="moved"):
        _setup(ck, replace=True, repos=["/o/s.git"], confirm=lambda p: pytest.fail("confirm asked on a stale base"))
    assert "ek-0000000000cc" in ck.state().policy.grants


# --- F9: an expiry that is not after now is refused ---------------------------------------------------------------

@pytest.mark.parametrize("delta", [timedelta(hours=-1), timedelta(0)])
def test_f9_an_expiry_not_after_now_is_refused(ck, delta):
    now = datetime.now(timezone.utc)
    with pytest.raises(key_setup.SetupRefused, match="not after now"):
        _setup(ck, expires=now + delta, clock=lambda: now)


def test_f9_a_replace_with_a_past_expiry_does_not_drop_the_live_record(ck):
    """Reproduced on 151ddfe4: the log refuses a past expiry on create_grant, but a replace's loosen took it and the
    live record was superseded by a dead one."""
    old, _ = _setup(ck)
    with pytest.raises(key_setup.SetupRefused, match="not after now"):
        _setup(ck, replace=True, expires=datetime.now(timezone.utc) - timedelta(hours=1))
    assert _live(ck) == [old["id"]]
