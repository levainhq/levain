"""``levain binding policy create|renew|revoke|list`` and ``levain binding graduate`` (14b S4) over a real authority
log (``tests/test_authority_graduation_kind.World``: an ssh-keygen laptop key, passkey stand-ins) and a real autonomic
store. The CLI's clock is the world's, moved a minute per command as the world's own links move it."""
from __future__ import annotations

import shutil
from datetime import timedelta

import pytest

import levain.cli as cli
from levain.autonomic.db import AutonomicDB
from levain.cockpit.firejournal import CockpitDB

pytest.importorskip("cryptography")
from tests.test_authority_graduation_kind import World, consent, guard_file, renew  # noqa: E402
from tests.test_graduation_policy import write_root  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


class CliWorld(World):
    def __init__(self, tmp, monkeypatch) -> None:
        super().__init__(tmp)
        assert CockpitDB(self.root).set_install_class("reported") == "reported"
        self.store = tmp / "store"
        self.store_id = AutonomicDB(self.store).meta("store_id")

        def now():
            self.clock.now += timedelta(minutes=1)
            return self.clock.now
        monkeypatch.setattr(cli, "_utc_now", now)

    def cli(self, *argv: str, key: str = "laptop") -> int:
        common = ["--data-root", str(self.root)]
        if argv[:2] in (("policy", "create"), ("policy", "renew"), ("policy", "revoke")):
            common += ["--signing-key", str(self.tmp / "keys" / key)]
        if argv[:2] == ("policy", "create"):
            common += ["--store-dir", str(self.store)]
            if "--guard" not in argv:
                common += ["--guard", str(guard_file(self.tmp))]
        return cli.main(["binding", *argv, *common])


@pytest.fixture
def cw(tmp_path, monkeypatch) -> CliWorld:
    return CliWorld(tmp_path, monkeypatch)


def test_create_prints_what_it_hands_over_before_signing_and_signs_the_stores_id(cw, capsys):
    assert cw.cli("policy", "create", "--action", "link0", "--lowest", "ON_LOOP", "--days", "7", "--id",
                  "gp-0123456789ab") == 0
    out = capsys.readouterr().out
    assert cli._POLICY_WARNING in out and "advisory" in out
    assert out.index(cli._POLICY_WARNING) < out.index("signed: gp-0123456789ab")
    # O-g1f1-2 (1026-22 RULINGS "G1"): the read-back claims only the check that runs, the one at signing
    assert "its guard (checked now, when it is signed; levain does not check it at fire yet):" in out
    assert "stopping every fire" not in out
    g = dict(cw.state().policy.grants["gp-0123456789ab"])
    assert g["store_id"] == cw.store_id and g["lowest_posture"] == "ON_LOOP"
    assert dict(g["actions"]["link0"]) == {"cls": 1, "reversible": True, "external": False, "financial": False}


@pytest.mark.parametrize("argv,why", [
    (("--action", "link0", "--lowest", "ON_LOOP", "--days", "31"), "--days 31"),
    (("--action", "link0", "--lowest", "ON_LOOP", "--days", "0"), "--days 0"),
    (("--action", "link0=HIGH", "--lowest", "ON_LOOP"), "LOW"),
    (("--action", "link0=SOMETIMES", "--lowest", "ON_LOOP"), "not a risk class"),
    (("--action", "link0=MEDIUM", "--lowest", "ON_LOOP"), "LOW"),
    (("--action", "link0", "--action", "link0", "--lowest", "ON_LOOP"), "twice"),
])
def test_create_refuses_what_the_log_or_the_verb_would_not_take(cw, capsys, argv, why):
    seq = cw.state().seq
    assert cw.cli("policy", "create", *argv) == 1
    assert why in capsys.readouterr().err and cw.state().seq == seq


def test_create_without_an_autonomic_store_is_refused(cw, capsys, tmp_path):
    seq = cw.state().seq
    assert cli.main(["binding", "policy", "create", "--action", "link0", "--lowest", "ON_LOOP", "--store-dir",
                     str(tmp_path / "none"), "--guard", str(guard_file(cw.tmp)), "--data-root", str(cw.root),
                     "--signing-key", str(cw.tmp / "keys" / "laptop")]) == 1
    assert "no autonomic store" in capsys.readouterr().err and cw.state().seq == seq


@pytest.mark.parametrize("hostile", ["\x1b[8mhidden\x1b[0m", "a ‮dessap‬ b", "ok\nkill: forged"])
def test_the_guard_read_back_renders_controls_bidi_and_newlines_inert(cw, capsys, hostile):
    """G1-4 (1026-21 RULINGS "G1 L3 r1 triage"): every dynamic line of the read-back printed before the signature
    passes through the CLI's terminal-safe rendering, so a guard cannot hide or forge what the signer reads."""
    import json

    from tests.autonomic_test_only_coverage import TEST_GUARD
    e = dict(TEST_GUARD[0], rationale=hostile,
             kill_predicate={"op": "==", "field": "s‮", "value": "x\x1b"}, kill_drill={"fields": {"s‮": "x\x1b"}})
    path = cw.tmp / "hostile.json"
    path.write_text(json.dumps([e]))
    cw.cli("policy", "create", "--action", "link0", "--lowest", "ON_LOOP", "--id", "gp-0123456789ab", "--guard",
           str(path))
    out = capsys.readouterr().out
    head = out.split("signed:")[0]
    assert "why it might be wrong:" in head
    assert "\x1b" not in head and "‮" not in head and "‬" not in head
    assert "\nkill: forged" not in head
    [line] = [ln for ln in head.splitlines() if "why it might be wrong:" in ln]
    assert line.endswith(cli._term_safe(hostile))


def test_renew_moves_the_expiry_and_keeps_the_terms(cw, capsys):
    cw.cli("policy", "create", "--action", "link0", "--lowest", "ON_LOOP", "--days", "2", "--id", "gp-0123456789ab")
    before = dict(cw.state().policy.grants["gp-0123456789ab"])
    assert cw.cli("policy", "renew", "gp-0123456789ab", "--days", "20") == 0
    after = dict(cw.state().policy.grants["gp-0123456789ab"])
    assert after["expires_at"] > before["expires_at"]
    assert {k: v for k, v in after.items() if k != "expires_at"} == {k: v for k, v in before.items()
                                                                    if k != "expires_at"}


def test_revoke_removes_it_and_refuses_an_id_that_is_not_a_graduation_policy(cw, capsys):
    cw.cli("policy", "create", "--action", "link0", "--lowest", "ON_LOOP", "--id", "gp-0123456789ab")
    assert cw.cli("policy", "revoke", "gp-ffffffffffff") == 1
    assert cw.cli("policy", "revoke", "gp-0123456789ab") == 0
    assert "gp-0123456789ab" not in cw.state().policy.grants


def test_list_says_which_signer_root_does_not_list_and_which_the_log_revoked(cw, capsys):
    cw.cli("policy", "create", "--action", "link0", "--lowest", "ON_LOOP", "--id", "gp-0123456789ab")
    capsys.readouterr()
    root = write_root(cw, "laptop")
    assert cw.cli("policy", "list", "--allowed-signers", str(root)) == 0
    out = capsys.readouterr().out
    assert "gp-0123456789ab  link0  lowest ON_LOOP" in out and "covers nothing" not in out
    root = write_root(cw, "phone")
    cw.cli("policy", "list", "--allowed-signers", str(root))
    assert "not in root's allowed-signers file" in capsys.readouterr().out
    cw.link(renew(cw, gid="gp-0123456789ab"), "pk")
    cw.link({"op": "revoke_key", "fingerprint": cw.fp("pk")})
    cw.cli("policy", "list", "--allowed-signers", str(write_root(cw, "laptop", "pk")))
    assert f"last renewed by {cw.fp('pk')} (REVOKED in the log: covers nothing)" in capsys.readouterr().out


def test_graduate_with_no_store_names_the_path_policy_create_would_use(capsys, tmp_path):
    from levain.autonomic.db import DB_NAME

    store = tmp_path / "nostore"
    assert cli.main(["binding", "graduate", "--store-dir", str(store)]) == 1
    err = capsys.readouterr().err
    assert "nothing graduates" in err
    assert f"no autonomic store at {store / DB_NAME}" in err
    assert "executor_not_confined" in err


def test_graduate_with_a_store_present_drops_the_store_blocker_and_keeps_the_executor_one(capsys, tmp_path):
    AutonomicDB(tmp_path / "here").meta("store_id")
    assert cli.main(["binding", "graduate", "--store-dir", str(tmp_path / "here")]) == 1
    err = capsys.readouterr().err
    assert "no autonomic store" not in err
    assert "executor_not_confined" in err


def test_graduate_checks_the_store_dir_it_is_given_not_the_default(capsys, tmp_path):
    from levain.autonomic.db import DB_NAME, default_store_dir

    AutonomicDB(default_store_dir()).meta("store_id")          # the default store exists (under the test's $LEVAIN_HOME)
    elsewhere = tmp_path / "elsewhere"
    assert cli.main(["binding", "graduate", "--store-dir", str(elsewhere)]) == 1
    assert f"no autonomic store at {elsewhere / DB_NAME}" in capsys.readouterr().err


def test_graduate_store_read_failure_is_a_blocker_line_not_a_traceback(capsys, monkeypatch, tmp_path):
    from levain.autonomic.db import DB_NAME

    def boom(store_dir):
        raise RuntimeError("symlink loop")

    monkeypatch.setattr(cli, "_autonomic_store_id", boom)
    assert cli.main(["binding", "graduate", "--store-dir", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert f"could not read the autonomic store at {tmp_path / DB_NAME}: symlink loop" in err
    assert "nothing graduates" in err and "executor_not_confined" in err


def test_graduate_gate_failure_is_a_blocker_line_not_a_traceback(capsys, monkeypatch, tmp_path):
    from levain.autonomic.gate import EfferentGate

    def boom(self):
        raise AttributeError("_journal")

    monkeypatch.setattr(EfferentGate, "binding_effect_refusal", boom)
    assert cli.main(["binding", "graduate", "--store-dir", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "could not determine whether an executor may run a binding effect: _journal" in err
    assert "no autonomic store" in err


def test_graduate_reads_the_executor_reason_from_the_gate_not_a_restatement(capsys, monkeypatch, tmp_path):
    from levain.autonomic.gate import EfferentGate

    monkeypatch.setattr(EfferentGate, "binding_effect_refusal", lambda self: "some_other_reason")
    assert cli.main(["binding", "graduate", "--store-dir", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "some_other_reason" in err and "executor_not_confined" not in err


def test_policy_create_help_says_cooling_off_is_denied_on_silence_while_nothing_auto_fires(capsys):
    with pytest.raises(SystemExit):
        cli.main(["binding", "policy", "create", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "COOLING_OFF" in text and "denied on silence" in text and "like CONFIRM" in text


def test_a_renew_whose_policy_lapses_while_it_is_signed_is_refused(cw, capsys, monkeypatch):
    # RULINGS_R1 item 1: prepared a minute before the expiry, signed (the touch) past it. The append judges the time
    # it commits, never the time the renew was prepared.
    import levain.cockpit.keys as ckeys
    from datetime import datetime

    cw.cli("policy", "create", "--action", "link0", "--lowest", "ON_LOOP", "--days", "1", "--id", "gp-0123456789ab")
    expires = datetime.strptime(cw.state().policy.grants["gp-0123456789ab"]["expires_at"],
                                "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=cw.clock.now.tzinfo)
    cw.clock.now = expires - timedelta(minutes=2)     # the renew's own clock read moves it a minute: 1 min to go
    real = ckeys.ssh_signer

    def slow_signer(path, **kw):
        fp, sign = real(path, **kw)

        def touch(message: bytes) -> str:
            cw.clock.now = expires + timedelta(seconds=30)
            return sign(message)
        return fp, touch
    monkeypatch.setattr(ckeys, "ssh_signer", slow_signer)
    seq = cw.state().seq
    assert cw.cli("policy", "renew", "gp-0123456789ab", "--days", "20") == 1
    err = capsys.readouterr().err
    assert "lapsed while you signed" in err and "create it again" in err
    assert cw.state().seq == seq


@pytest.mark.parametrize("verb", ["renew", "revoke"])
def test_renew_and_revoke_refuse_a_consent_grant_by_name(cw, capsys, verb):
    # RULINGS_R1 item 3: `binding policy renew|revoke` act on a graduation policy only
    cw.link({"op": "create_grant", "grant": consent(cw)})
    seq = cw.state().seq
    argv = ("policy", verb, "cg-0123456789ab") + (("--days", "5") if verb == "renew" else ())
    assert cw.cli(*argv) == 1
    assert "no graduation policy 'cg-0123456789ab'" in capsys.readouterr().err
    assert cw.state().seq == seq and "cg-0123456789ab" in cw.state().policy.grants


@pytest.mark.parametrize("verb", ["renew", "revoke"])
def test_a_log_that_moves_between_the_kind_check_and_the_prepare_is_refused(cw, capsys, monkeypatch, verb):
    # the kind is judged on the head the link is prepared on: a revoke and a consent grant made again under the
    # id between the check and the prepare refuse the command, nothing it signed is appended
    import levain.authority as auth

    cw.cli("policy", "create", "--action", "link0", "--lowest", "ON_LOOP", "--id", "gp-0123456789ab")
    real = auth.prepare
    moved = []

    def prepare(conn, op, **kw):
        if op.get("grant_id") == "gp-0123456789ab" and not moved:
            monkeypatch.setattr(auth, "prepare", real)
            cw.clock.now -= timedelta(seconds=150)     # the interleaved links sit before the command's own time
            cw.link({"op": "revoke_grant", "grant_id": "gp-0123456789ab"})
            moved.append(cw.link({"op": "create_grant", "grant": consent(cw, gid="gp-0123456789ab")}))
        return real(conn, op, **kw)
    monkeypatch.setattr(auth, "prepare", prepare)
    argv = ("policy", verb, "gp-0123456789ab") + (("--days", "5") if verb == "renew" else ())
    assert cw.cli(*argv) == 1
    assert "moved" in capsys.readouterr().err
    assert cw.state().seq == moved[0].seq
    assert cw.state().policy.grants["gp-0123456789ab"]["kind"] == "consent"     # the consent grant stands as made


@pytest.mark.parametrize("content", [b"not a database at all, just text\n" * 64, None], ids=["text", "no-meta"])
def test_create_over_a_file_that_is_not_an_autonomic_store_is_refused_by_name(cw, capsys, tmp_path, content):
    # RULINGS_R1 item 9: a stray file or another sqlite database under --store-dir is a named refusal
    import sqlite3

    d = tmp_path / "stray"
    d.mkdir()
    if content is None:
        sqlite3.connect(d / "autonomic.db").execute("CREATE TABLE other (x)").connection.close()
    else:
        (d / "autonomic.db").write_bytes(content)
    seq = cw.state().seq
    assert cli.main(["binding", "policy", "create", "--action", "link0", "--lowest", "ON_LOOP", "--store-dir", str(d),
                     "--guard", str(guard_file(cw.tmp)),
                     "--data-root", str(cw.root), "--signing-key", str(cw.tmp / "keys" / "laptop")]) == 1
    assert f"cannot read the autonomic store at {d / 'autonomic.db'}" in capsys.readouterr().err
    assert cw.state().seq == seq


def test_list_names_why_anchoring_is_unknown_never_none(cw, capsys, monkeypatch):
    import levain.autonomic.coverage as cov
    import levain.trust_anchor as ta

    cw.cli("policy", "create", "--action", "link0", "--lowest", "ON_LOOP", "--id", "gp-0123456789ab")
    root = write_root(cw, "laptop")
    capsys.readouterr()
    monkeypatch.setattr(ta, "trust_anchor_problem", lambda *a, **kw: (None, None))
    assert cw.cli("policy", "list", "--allowed-signers", str(root)) == 0
    out = capsys.readouterr().out
    assert "anchoring unknown: " in out and "anchoring unknown: None" not in out
    monkeypatch.undo()
    monkeypatch.setattr(cli, "_utc_now", lambda: cw.clock.now)

    def bad(raw):
        raise ValueError("a line root's file cannot hold")
    monkeypatch.setattr(cov, "parse_root_signers", bad)
    assert cw.cli("policy", "list", "--allowed-signers", str(root)) == 0
    assert "anchoring unknown: a line root's file cannot hold" in capsys.readouterr().out


def test_graduate_answers_an_engine_failure_with_one_line_and_exit_1(capsys, monkeypatch):
    # RULINGS_R1 item 10
    def engine(apply):
        raise RuntimeError("the authority log is locked")
    monkeypatch.setattr(cli, "_graduation_engine", lambda args: engine)
    assert cli.main(["binding", "graduate"]) == 1
    assert capsys.readouterr().err.strip() == "graduation failed: RuntimeError: the authority log is locked"


def test_list_says_a_signer_absent_from_the_log_apart_from_a_revoked_one(cw, capsys, monkeypatch):
    # RULINGS_R1 item 11 (deepseek 4)
    import levain.authority as auth

    cw.cli("policy", "create", "--action", "link0", "--lowest", "ON_LOOP", "--id", "gp-0123456789ab")
    capsys.readouterr()
    real = auth.graduation_links
    monkeypatch.setattr(auth, "graduation_links", lambda state, gid: ("SHA256:absent", real(state, gid)[1]))
    assert cw.cli("policy", "list") == 0
    out = capsys.readouterr().out
    assert "signed by SHA256:absent (not in the log: covers nothing)" in out and "REVOKED" not in out


def test_graduate_takes_the_cockpit_flags(capsys, tmp_path):
    # RULINGS_R1 item 11 (deepseek 6): the engine S6 wires reads the same --data-root / --path as the policy verbs
    assert cli.main(["binding", "graduate", "--data-root", str(tmp_path), "--preview"]) == 1
    assert "nothing graduates" in capsys.readouterr().err


def test_graduate_with_an_error_step_prints_what_committed_and_exits_1(capsys, monkeypatch, tmp_path):
    # RULINGS_R2 item 1: binding 1 graduates, the authority log is locked when binding 2 refreshes: the run prints the
    # committed step and the error step, and the exit says it was partial
    import sqlite3

    from tests.autonomic_test_only_coverage import TestOnlyCoverage
    from tests.test_autonomic_graduation_due import LATER, LOW, _due, _three, policy

    s, bs = _three(tmp_path)
    src = TestOnlyCoverage([policy("gp-1", {"deliver": LOW}, expires_at=LATER)])
    real = src.refresh

    def refresh():
        if src.refreshes == 1:
            src.refreshes += 1
            raise sqlite3.OperationalError("database is locked")
        return real()
    monkeypatch.setattr(src, "refresh", refresh)
    monkeypatch.setattr(cli, "_graduation_engine", lambda args: lambda apply: _due(s, src, apply=apply))
    assert cli.main(["binding", "graduate"]) == 1
    out = capsys.readouterr()
    lines = out.out.splitlines()
    assert [x.split(":")[0] for x in lines if not x.startswith(" ")] == ["graduated", "error", "graduated"]
    assert "graduation failed" not in out.err
    assert out.err.strip() == "graduation partial: 1 of 3 steps failed (the others are as printed)"
