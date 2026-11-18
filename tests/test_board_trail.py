"""P6 part A (alpha#104): ``levain board trail export|verify`` over the records of both board files.

Each behaviour was first run once on a scratch LEVAIN_HOME (``build_world`` is that run's store: legacy lines, a cutover,
asserted adds, a terminal-signed record by a throwaway key enrolled in a hand-built authority log, a relayed record citing a
scratch band answer, a torn line and its ack-torn, a signed and a relayed registry record), then tampered with, and pinned here.
No Touch ID ever runs: the seal is ``board_sign.terminal_seal`` over a throwaway ed25519 key (``tests.test_board_sign.Rig``)."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from levain.cli import main
from levain.cockpit import keys as ckeys
from levain.consent import terminal_sign as ts
from levain.desk import board_chain as bc
from levain.desk import board_registry as br
from levain.desk import board_relay as rel
from levain.desk import board_sign as sg
from levain.desk import board_store as bs
from levain.desk import board_trail as bt
from tests.test_board_sign import Rig

OP = {"kind": "operator", "name": "phill"}
DESK = {"kind": "agent", "name": "desk 24"}
OPD = {"kind": "operator", "name": "desk 1"}
NOW = "2026-11-17T12:00:00Z"
Q1 = "q-" + "1b3dd088" * 3
Q2 = "q-" + "0000000a" * 3
ANSWER = "yes, cut it. His sentence travels."
EXCERPT = "His sentence travels"
SIGNED = "operator, signed with a ceremony"


class FakeBand:
    def __init__(self, **rows: rel.BandRow) -> None:
        self.rows = dict(rows)

    def row(self, np_id: str) -> rel.BandRow:
        if np_id not in self.rows:
            raise rel.BandRefusal(f"{np_id} is not a row this band holds")
        return self.rows[np_id]


class SignBand(FakeBand):
    """A band whose rows also carry the answer's own signing record (what ``band_view.answer_sign_of`` returns)."""

    def __init__(self, signs: dict, **rows: rel.BandRow) -> None:
        super().__init__(**rows)
        self.signs = signs

    def sign(self, np_id: str):
        return self.signs.get(np_id)


def answer_sig(rig: Rig, np_id: str, *, tamper: bool = False, other: str | None = None, asked: str = "Cut alpha#3?",
               text: str | None = ANSWER, pick: str | None = None) -> dict:
    """A real answer signature over a terminal-answer message naming ``np_id`` (``other``: names that item instead), asking
    ``asked`` and answering ``text`` (his free words) or ``pick`` (an option, ``(a) label``)."""
    said = {"answer": pick} if pick is not None else {"text": text}
    msg = ts.build_message(store="levain-questions:abc", install="abc", session="s1", view="1", typed="yes", nonce="0123456789abcdef",
                           at=datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc),
                           entries=[ts.Entry(other or np_id, "rv-1", asked, "information", **said)])
    sig = ckeys.ssh_signer(rig.path, namespace=ts.NAMESPACE)[1](msg)
    if tamper:                                          # alter signature bytes inside the armored body
        head, body, tail = sig.partition("\n")[0], sig.splitlines()[1:-1], sig.splitlines()[-1]
        body[1] = body[1][:10] + ("A" if body[1][10] != "A" else "B") + body[1][11:]
        sig = "\n".join([head, *body, tail]) + "\n"
    return {"sig": sig, "message": msg.decode("utf-8"), "signer_fp": rig.fp, "sign_state": "signed", "sign_reason": None}


def git(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env).stdout.decode().strip()


def make_repo(root: Path, name: str) -> Path:
    bare = root / "bare" / f"{name}.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    d = root / "repos" / name
    subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True)
    (d / "README.md").write_text(f"hi {name}\n")
    git(d, "add", ".")
    git(d, "commit", "-qm", "init")
    git(d, "remote", "add", "origin", str(bare))
    git(d, "push", "-q", "origin", "main")
    git(d, "fetch", "-q")
    return d


@dataclass
class World:
    store: Path
    target: bs.Target
    rig: Rig
    band: FakeBand
    home: Path
    out: Path

    def lines(self, name: str) -> list[bytes]:
        raw = (self.store / name).read_bytes()
        return raw.split(b"\n")[:-1]

    def write(self, name: str, lines: list[bytes]) -> None:
        (self.store / name).write_bytes(b"".join(x + b"\n" for x in lines))


def build_world(tmp_path: Path, monkeypatch) -> World:
    """A scratch store: items.jsonl = 2 legacy lines, a cutover (asserted add), a signed decide, a relayed decide, a torn line, its
    ack-torn, an asserted add (the unanchored tail); repos.jsonl = register, a signed begin-track, register, a relayed begin-track."""
    home = tmp_path / "lhome"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    monkeypatch.delenv("LEVAIN_BOARD_ITEMS", raising=False)
    monkeypatch.delenv("LEVAIN_LAUNCH_KEY", raising=False)
    store = tmp_path / "store"
    assert main(["board", "use", str(store)]) == 0
    store = Path(os.path.realpath(store))
    legacy = [json.dumps({"v": 1, "op": "add", "id": f"alpha#{i}", "at": "2026-11-01T00:00:00Z", "by": "phill", "repo": "alpha",
                          "title": f"old {i}", "origin": "desk 1"}, separators=(",", ":")).encode() for i in (1, 2)]
    (store / "items.jsonl").write_bytes(b"".join(x + b"\n" for x in legacy))
    (store / "repos.jsonl").touch()
    tgt = bs.resolve_store(None)
    rig = Rig(tmp_path)
    monkeypatch.setattr(sg, "resolve_authority", lambda **kw: rig.source())
    mf = tmp_path / "manifest.json"
    mf.write_text(json.dumps({"plan_items": ["a"], "contract_items": []}))
    sha = hashlib.sha256(mf.read_bytes()).hexdigest()
    band = SignBand({Q1: answer_sig(rig, Q1)}, **{Q1: rel.BandRow("Cut alpha#3?", ANSWER, "signed-phone", "native"),
                       Q2: rel.BandRow(f"Begin tracking beta? manifest sha256 {sha}", "yes, begin beta", "unsigned-terminal", "native")})
    monkeypatch.setattr(rel, "band_source", lambda directory=None: band)
    alpha, beta = make_repo(tmp_path, "alpha"), make_repo(tmp_path, "beta")
    br.register(tgt, "alpha", str(alpha), "first", "phill", author=OP)
    br.register(tgt, "beta", str(beta), "second", "desk 24", author=DESK)
    br.begin_track(tgt, "alpha", mf, "yes", "phill", author=OP, seal=rig.seal())
    mig = br.read_registry(tgt).fold.repos["alpha"].migration

    def keyed(k: str) -> dict:
        return {"key": f"{mig}:{k}", "payload_hash": hashlib.sha256(k.encode()).hexdigest()}
    # items
    third = bs.add(tgt, "alpha", "third", "desk 1", "desk 24", author=DESK, now=NOW, **keyed("third"))[0]                  # the cutover, asserted
    bs.decide(tgt, third, "cut", "no longer wanted", "phill", author=OP, now=NOW, seal=rig.seal())      # signed
    fourth = bs.add(tgt, "alpha", "fourth", "desk 1", "desk 24", author=DESK, now=NOW, **keyed("fourth"))[0]
    bs.decide(tgt, fourth, "cut", EXCERPT, "desk 24", author=DESK, now=NOW, relay=(Q1, band))          # relayed
    with open(store / "items.jsonl", "ab") as fh:
        fh.write(b'{"v":1,"op":"ad\n')                                                                   # a torn line (line 7)
    assert main(["board", "ack-torn", "--file", "items.jsonl", "--line", "7", "--why", "crash", "--by", "phill",
                 "--author-kind", "operator"]) == 0
    bs.add(tgt, "alpha", "fifth", "desk 1", "desk 24", author=DESK, now=NOW, **keyed("fifth"))                            # the unanchored tail
    br.begin_track(tgt, "beta", mf, "yes, begin beta", "desk 24", author=DESK, relay=(Q2, band))
    return World(store, tgt, rig, band, home, tmp_path / "out")


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    return build_world(tmp_path, monkeypatch)


def trail_of(w: World) -> bt.Trail:
    return bt.read_live(w.target, data_root=None, band_dir=None)


def report(w: World, heads=None):
    return {r.name: r for r in bt.verify_trail(trail_of(w), heads or {})}


def run(capsys, *argv):
    rc = main(["board", "trail", *argv])
    out, err = capsys.readouterr()
    return rc, out, err


# ------------------------------------------------------------------------------------------------------- a clean store


def test_a_clean_store_verifies_with_the_design_vocabulary_one_verdict_per_record(world, capsys):
    rc, out, err = run(capsys, "verify")
    assert rc == 0, out + err
    items = report(world)["items.jsonl"]
    verdicts = {r.n: r.verdict for r in items.records}
    assert verdicts[1] == verdicts[2] == "legacy"
    assert verdicts[3] == "asserted by desk 24, unsigned" and verdicts[4] == SIGNED
    assert verdicts[6] == f"agent desk 24, relaying {Q1} (signed answer)"
    assert 7 not in verdicts and verdicts[8] == "asserted by phill, unsigned"          # line 7 is torn, not a record
    assert "line 7 torn, acknowledged by phill (asserted, line 8)" in items.notes
    repos = report(world)["repos.jsonl"]
    assert [r.verdict for r in repos.records] == ["asserted by phill, unsigned", "asserted by desk 24, unsigned", SIGNED,
                                                  f"agent desk 24, relaying {Q2} (unsigned answer)"]
    assert "no breaks" in out and "BREAK" not in out


def test_the_anchor_and_tail_lines_are_per_file_and_never_hidden(world):
    r = report(world)
    assert r["items.jsonl"].anchor == ("items.jsonl: anchored through line 4 (signed at 2026-11-17T12:00:00Z): every record up to it "
                                       "is committed by the operator's key")
    assert r["items.jsonl"].tail.startswith("items.jsonl: 4 records after the last signed record (at 2026-11-17T12:00:00Z) are unanchored: "
                                            "anyone who can write this file could have rewritten them")
    assert r["repos.jsonl"].anchor.startswith("repos.jsonl: anchored through line 3 ")
    assert r["repos.jsonl"].tail.startswith("repos.jsonl: 1 records after the last signed record ")
    # the repos relay rests on an UNSIGNED answer: never summarised as anchored; the items relay rests on a signed one
    assert r["repos.jsonl"].relayed == ("repos.jsonl: 1 relayed record(s) rest on an answer no signature of a key you hold binds: "
                                        "nothing anchors his words")
    assert r["items.jsonl"].relayed.startswith("items.jsonl: 1 relayed record(s) rest on a signed answer")


def test_a_store_with_no_signed_record_says_nothing_is_anchored(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    monkeypatch.delenv("LEVAIN_BOARD_ITEMS", raising=False)
    assert main(["board", "use", str(tmp_path / "s")]) == 0
    tgt = bs.resolve_store(None)
    bs.add(tgt, "alpha", "t", "desk 1", "desk 24", author=DESK, now=NOW)
    r = {x.name: x for x in bt.verify_trail(bt.read_live(tgt, data_root=None, band_dir=None))}
    assert "no signed record: nothing in this file is anchored" in r["items.jsonl"].anchor
    assert "all are unanchored" in r["items.jsonl"].tail and not r["items.jsonl"].breaks


# ----------------------------------------------------------------------------------------------------------- the bundle


def test_export_writes_a_bundle_prints_the_two_heads_and_verify_reads_it_back(world, capsys, monkeypatch):
    rc, out, err = run(capsys, "export", "--out", str(world.out) + os.sep)
    assert rc == 0, err
    files = list(world.out.glob("trail-*.json"))
    assert len(files) == 1 and (files[0].stat().st_mode & 0o777) == 0o600
    heads = out.strip().splitlines()[-1]
    items_head = bc.line_hash(world.lines("items.jsonl")[-1])
    repos_head = bc.line_hash(world.lines("repos.jsonl")[-1])
    assert heads == f"items={items_head} repos={repos_head}"
    doc = json.loads(files[0].read_text("utf-8"))
    assert doc["format"] == bt.BUNDLE_FORMAT and doc["authority"]["keys"][0]["fingerprint"] == world.rig.fp
    assert set(doc["answers"]) == {Q1, Q2} and doc["answers"][Q1]["answer"] == ANSWER
    # the bundle verifies with the live band GONE (it carries the answers) and the authority log gone (it carries the keys)
    world.band.rows.clear()

    def gone(**kw):
        raise bs.BoardError("gone")
    monkeypatch.setattr(sg, "resolve_authority", gone)
    rc, out, err = run(capsys, "verify", str(files[0]), "--head", f"items={items_head}", "--head", f"repos={repos_head}")
    assert rc == 0, out + err
    assert f"kept head {items_head[:12]} is the current head: nothing was removed" in out
    # with no authority of the verifier's own, the bundle's keys are claims: said as unchecked, and no anchor is claimed
    assert f"items.jsonl:4 decide alpha#3: {bt.BUNDLE_KEY_WORDS}" in out and SIGNED not in out
    assert "items.jsonl: NOT ANCHORED: the signatures were checked only against keys the bundle itself names" in out
    assert "NOT checked against an authority you hold" in out and "(signed answer)" not in out


def test_export_defaults_under_the_levain_home_and_never_overwrites(world, capsys):
    rc, out, err = run(capsys, "export")
    assert rc == 0, err
    path = Path(out.splitlines()[0].removeprefix("bundle: "))
    assert path.parent == (world.home / "board-trail").resolve() or path.parent == world.home / "board-trail"
    rc, _, err = run(capsys, "export", "--out", str(path))
    assert rc == 2 and "never overwritten" in err


def test_export_refuses_a_destination_inside_a_git_work_tree(world, tmp_path, capsys):
    repo = make_repo(tmp_path, "gamma")
    rc, out, err = run(capsys, "export", "--out", str(repo / "sub" / "t.json"))
    assert rc == 2 and "inside a git work tree" in err and not (repo / "sub").exists()


def test_a_damaged_or_foreign_bundle_is_refused_not_a_traceback(tmp_path, capsys):
    p = tmp_path / "b.json"
    p.write_text("{}")
    rc, _, err = run(capsys, "verify", str(p))
    assert rc == 2 and "not a levain-board-trail/1 bundle" in err
    p.write_text(json.dumps({"format": bt.BUNDLE_FORMAT, "store": "s"}))
    rc, _, err = run(capsys, "verify", str(p))
    assert rc == 2 and "damaged" in err
    rc, _, err = run(capsys, "verify", str(tmp_path / "nope.json"))
    assert rc == 2


def test_a_malformed_head_is_a_usage_error(world, capsys):
    assert run(capsys, "verify", "--head", "items=abc")[0] == 2
    assert run(capsys, "verify", "--head", "other=" + "a" * 64)[0] == 2


# ------------------------------------------------------------------------------------------------------------ tampering


def test_editing_one_byte_before_the_last_signed_record_breaks_the_chain_and_the_signature_chain(world, capsys):
    lines = world.lines("items.jsonl")
    lines[2] = lines[2].replace(b"third", b"thirD")                       # line 3, before the signed line 4
    world.write("items.jsonl", lines)
    rc, out, err = run(capsys, "verify")
    assert rc == 3
    assert "BREAK items.jsonl: line 4:" in out and "prev does not match line 3" in out
    # L3 r2 (gemini MED 1): a signed record that carries a break anchors nothing; with no clean signed record before it, none
    assert ("items.jsonl: NOT ANCHORED: the signed record at line 4 carries a break, and no signed record without one precedes "
            "it") in out
    assert "the tail is anchored" not in out


def test_editing_a_signed_record_body_fails_its_signature(world, capsys):
    lines = world.lines("items.jsonl")
    rec = json.loads(lines[3])
    rec["words"] = "something else"
    lines[3] = json.dumps(rec, separators=(",", ":")).encode()
    world.write("items.jsonl", lines)
    rc, out, err = run(capsys, "verify")
    assert rc == 3 and "signature does not verify" in out and "items.jsonl:4 decide alpha#3" in out


def test_a_rewrite_that_recomputes_every_prev_is_caught_by_the_signature_for_what_is_before_the_last_signed_record(world, capsys):
    lines = world.lines("items.jsonl")
    rec = json.loads(lines[2])
    rec["title"] = "forged"
    lines[2] = json.dumps(rec, separators=(",", ":")).encode()
    for i in range(3, len(lines)):                                        # the forger recomputes prev down the file
        r = bc.record_of(lines[i])
        if r is not None:
            r["prev"] = bc.line_hash(lines[i - 1])
            lines[i] = json.dumps(r, separators=(",", ":")).encode()
    world.write("items.jsonl", lines)
    rc, out, err = run(capsys, "verify")
    assert rc == 3 and "items.jsonl:4 decide alpha#3" in out and "signature does not verify" in out      # the sig committed the old prev


def test_truncating_the_tail_with_a_kept_head_is_a_break(world, capsys):
    kept = bc.line_hash(world.lines("items.jsonl")[-1])
    rc, out, err = run(capsys, "verify", "--head", f"items={kept}")
    assert rc == 0 and "is the current head: nothing was removed" in out
    world.write("items.jsonl", world.lines("items.jsonl")[:-2])           # the ack-torn and the tail add gone
    rc, out, err = run(capsys, "verify", "--head", f"items={kept}")
    assert rc == 3 and "TRUNCATION OR REWRITE" in out and "is not on this chain" in out


def test_rewriting_an_unanchored_tail_passes_the_chain_and_is_reported_by_the_tail_line(world, capsys):
    kept_before = bc.line_hash(world.lines("items.jsonl")[-1])
    lines = world.lines("items.jsonl")
    rec = json.loads(lines[-1])
    rec["title"] = "rewritten by whoever can write this file"
    lines[-1] = json.dumps(rec, separators=(",", ":")).encode()           # the last line has no successor: no prev to fix
    world.write("items.jsonl", lines)
    rc, out, err = run(capsys, "verify")
    assert rc == 0                                                        # nothing a chain can see: that is the honest claim
    assert "4 records after the last signed record" in out and "unanchored: anyone who can write this file could have rewritten them" in out
    rc, out, err = run(capsys, "verify", "--head", f"items={kept_before}")
    assert rc == 3 and "TRUNCATION OR REWRITE" in out                      # a kept head catches it


def test_the_two_files_report_their_own_truncation(world, capsys):
    ih = bc.line_hash(world.lines("items.jsonl")[-1])
    rh = bc.line_hash(world.lines("repos.jsonl")[-1])
    world.write("repos.jsonl", world.lines("repos.jsonl")[:-1])
    rc, out, err = run(capsys, "verify", "--head", f"items={ih}", "--head", f"repos={rh}")
    assert rc == 3
    assert f"items.jsonl: kept head {ih[:12]} is the current head" in out and "BREAK repos.jsonl: repos.jsonl: TRUNCATION OR REWRITE" in out


def test_an_unchained_record_after_the_cutover_is_a_named_break(world, capsys):
    lines = world.lines("items.jsonl")
    rec = json.loads(lines[-1])
    del rec["prev"]
    lines[-1] = json.dumps(rec, separators=(",", ":")).encode()
    world.write("items.jsonl", lines)
    rc, out, err = run(capsys, "verify")
    assert rc == 3 and "unchained record after the cutover (an old writer?)" in out


def test_an_inserted_or_deleted_legacy_line_breaks_the_cutover(world, capsys):
    lines = world.lines("items.jsonl")
    world.write("items.jsonl", lines[1:])
    rc, out, err = run(capsys, "verify")
    assert rc == 3 and "cutover prev does not commit the legacy prefix" in out


def test_an_unacknowledged_torn_line_is_a_break_and_names_the_ack_verb(world, capsys):
    world.write("repos.jsonl", world.lines("repos.jsonl") + [b'{"v":1,"op":"pau'])
    rc, out, err = run(capsys, "verify")
    assert rc == 3 and "line 5: torn" in out and "ack-torn --file repos.jsonl --line 5" in out


def test_an_unterminated_tail_line_is_a_break(world, capsys):
    with open(world.store / "items.jsonl", "ab") as fh:
        fh.write(b'{"v":1,"op"')
    rc, out, err = run(capsys, "verify")
    assert rc == 3 and "no terminating newline" in out


# ------------------------------------------------------------------------------------------------- signatures and keys


def test_a_signed_record_by_a_key_the_log_does_not_hold_is_a_break(world, capsys, monkeypatch):
    other = Rig(world.out.parent / "other")
    monkeypatch.setattr(sg, "resolve_authority", lambda **kw: other.source())
    rc, out, err = run(capsys, "verify")
    assert rc == 3 and "is not a key in the authority log" in out


def test_a_revoked_key_is_listed_never_silently_valid_and_never_a_break_by_itself(world, capsys, monkeypatch):
    monkeypatch.setattr(sg, "resolve_authority", lambda **kw: world.rig.source(revoked_seq=9))
    rc, out, err = run(capsys, "verify")
    assert rc == 0
    assert f"{SIGNED}, by a key revoked since (seq 9)" in out and "revoked at seq 9" in out


def test_no_authority_log_makes_every_signed_record_a_break_but_not_the_unsigned_ones(world, capsys, monkeypatch):
    def gone(**kw):
        raise bs.BoardError("holds no cockpit.db")
    monkeypatch.setattr(sg, "resolve_authority", gone)
    rc, out, err = run(capsys, "verify")
    assert rc == 3 and out.count("cannot be checked: no authority log") >= 2 and "holds no cockpit.db" in out
    assert "asserted by desk 24, unsigned" in out


def test_a_signed_record_of_another_store_is_a_break(world, capsys):
    lines = world.lines("items.jsonl")
    rec = json.loads(lines[3])
    assert rec["store"]
    t = trail_of(world)
    t.store_id = "some-other-store"
    rep = bt.verify_file("items.jsonl", t, {})
    assert any("names store" in b and "some-other-store" in b for b in rep.breaks)


# ----------------------------------------------------------------------------------------------------- relayed citations


def test_a_relayed_record_whose_answer_changed_is_a_break_naming_why(world, capsys):
    world.band.rows[Q1] = rel.BandRow("Cut alpha#3?", "no, keep it", "signed-phone", "native")
    rc, out, err = run(capsys, "verify")
    assert rc == 3 and "relayed citation does not hold: digest mismatch" in out


def test_a_relayed_record_whose_words_are_not_in_the_answer_is_a_break(world, capsys):
    lines = world.lines("items.jsonl")
    i = next(k for k, x in enumerate(lines) if b'"relayed"' in x)
    rec = json.loads(lines[i])
    rec["words"] = "words never said"
    lines[i] = json.dumps(rec, separators=(",", ":")).encode()
    world.write("items.jsonl", lines)
    rc, out, err = run(capsys, "verify")
    assert rc == 3 and "are not a verbatim excerpt" in out


def test_an_unsigned_relayed_answer_is_reported_as_exactly_that_and_a_signed_one_as_signed(world):
    verdicts = {r.n: r.verdict for r in report(world)["repos.jsonl"].records}
    assert verdicts[4].endswith("(unsigned answer)")
    assert {r.n: r.verdict for r in report(world)["items.jsonl"].records}[6].endswith("(signed answer)")


def _relay_report(world):
    return {r.n: r for r in report(world)["items.jsonl"].records}[6]


def test_a_relayed_record_on_an_answer_with_no_signature_reads_unsigned_never_promoted(world, capsys):
    world.band.signs.clear()
    world.band.rows[Q1] = rel.BandRow("Cut alpha#3?", ANSWER, "unsigned-terminal", "native")
    r = _relay_report(world)
    assert r.ok and r.verdict == f"agent desk 24, relaying {Q1} (unsigned answer)"
    assert run(capsys, "verify")[0] == 0


def test_a_row_that_claims_a_signed_answer_with_no_signature_to_check_is_a_break_never_unsigned(world, capsys):
    world.band.signs.clear()                            # the row's grade still CLAIMS signed-phone; there is nothing to verify
    r = _relay_report(world)
    assert not r.ok and "the band row says the answer is signed-phone, and no signature is there to check" in r.verdict
    assert run(capsys, "verify")[0] == 3


@pytest.mark.parametrize("sign, said", [
    ({"sign_state": "signed", "sig": "", "message": "m", "signer_fp": "f"}, "says signed and does not carry a complete signature"),
    ({"sign_state": "signed", "sig": "s", "message": "m"}, "says signed and does not carry a complete signature"),
    ({"sign_state": "unsigned", "sig": "s", "message": None}, "carries signature material under sign_state 'unsigned'"),
    (["not", "a", "mapping"], "malformed"),
])
def test_a_signing_record_with_missing_or_stray_material_is_a_break(world, sign, said):
    world.band.signs[Q1] = sign
    r = _relay_report(world)
    assert not r.ok and said in r.verdict


def test_a_relayed_record_on_an_answer_whose_signature_bytes_are_altered_is_a_break(world, capsys):
    world.band.signs[Q1] = answer_sig(world.rig, Q1, tamper=True)
    r = _relay_report(world)
    assert not r.ok and "the answer's signature does not verify" in r.verdict
    rc, out, err = run(capsys, "verify")
    assert rc == 3 and "does not verify" in out


def test_an_answer_signed_for_another_item_or_by_an_unenrolled_key_is_a_break(world):
    world.band.signs[Q1] = answer_sig(world.rig, Q1, other=Q2)
    assert "does not name" in _relay_report(world).verdict
    world.band.signs[Q1] = {**answer_sig(world.rig, Q1), "signer_fp": "SHA256:" + "A" * 43}
    assert "is not a key in the authority log" in _relay_report(world).verdict


def test_an_answer_signature_is_carried_by_the_bundle_and_re_verified_from_it(world, capsys):
    assert run(capsys, "export", "--out", str(world.out) + os.sep)[0] == 0
    bundle = next(world.out.glob("trail-*.json"))
    assert json.loads(bundle.read_text("utf-8"))["answers"][Q1]["sign"]["sign_state"] == "signed"
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 0 and f"relaying {Q1} (signed answer)" in out
    doc = json.loads(bundle.read_text("utf-8"))
    doc["answers"][Q1]["sign"] = answer_sig(world.rig, Q1, tamper=True)
    bundle.write_text(json.dumps(doc))
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 3 and "the answer's signature does not verify" in out


def test_a_relayed_begin_track_whose_question_does_not_carry_the_manifest_is_a_break(world, capsys):
    world.band.rows[Q2] = rel.BandRow("Begin tracking beta? manifest sha256 " + "e" * 64, "yes, begin beta", "unsigned-terminal", "native")
    # the digest check fires first (the question changed); a record re-written for the new question hits the manifest check
    rc, out, err = run(capsys, "verify")
    assert rc == 3 and "relayed citation does not hold" in out


def test_a_relayed_track_binds_the_manifest_its_begin_track_recorded():
    sha = "c" * 64
    rec = {"op": "track", "repo": "beta", "migration": "m-1"}
    assert bt._manifest_sha(rec, {("beta", "m-1"): sha}) == sha and bt._manifest_sha(rec, {}) is None
    assert bt._manifest_sha({"op": "begin-track", "manifest_sha256": sha}, {}) == sha


def test_a_band_that_cannot_be_opened_is_a_per_record_break_not_a_crash(world, capsys, monkeypatch):
    def boom(directory=None):
        raise OSError("no band")
    monkeypatch.setattr(rel, "band_source", boom)
    rc, out, err = run(capsys, "verify")
    assert rc == 3 and "the band could not be opened" in out


# ---------------------------------------------------------------------------------------- a cut's cause, as of its position


def abandon_world(world: World):
    """alpha abandoned under a signed abandon (its remaining item cut, asserted, citing it), then abandon-done: every old cut now
    fails "its repo is aborting", and each must still verify."""
    repo, cuts = br.abandon_track(world.target, "alpha", "walk away from alpha", "phill", author=OP, seal=world.rig.seal())
    assert cuts
    br.abandon_done(world.target, "alpha", "", "phill", author=OP, seal=world.rig.seal())
    assert br.read_registry(world.target).fold.repos["alpha"].mode == "untracked"
    return cuts


def forge_cut(world: World, cause: str, item: str = "alpha#2") -> None:
    lines = world.lines("items.jsonl")
    cut = next(bc.record_of(x) for x in reversed(lines) if (bc.record_of(x) or {}).get("cause"))
    forged = {**cut, "cause": cause, "id": item, "prev": bc.line_hash(lines[-1])}
    world.write("items.jsonl", lines + [json.dumps(forged, separators=(",", ":")).encode()])


def test_a_cut_whose_cause_held_when_written_verifies_after_abandon_done(world, capsys):
    abandon_world(world)
    cut = next(bc.record_of(x) for x in reversed(world.lines("items.jsonl")) if (bc.record_of(x) or {}).get("cause"))
    assert sg.cut_cause_problem(cut["cause"], cut["id"], bs._store_lines(world.target), None, world.band) is not None   # today's fold refuses it
    rc, out, err = run(capsys, "verify")
    assert rc == 0, out + err


def test_a_cut_with_a_forged_cause_is_a_break_with_a_reason(world, capsys):
    abandon_world(world)
    reg = next(x for x in world.lines("repos.jsonl") if (bc.record_of(x) or {}).get("op") == "register")
    forge_cut(world, hashlib.sha256(reg).hexdigest())
    rc, out, err = run(capsys, "verify")
    assert rc == 3 and "the cause does not hold as of this cut: cause names a record that is not an abandon-track" in out
    lines = world.lines("items.jsonl")[:-1]
    world.write("items.jsonl", lines)
    forge_cut(world, "ab" * 32)
    rc, out, err = run(capsys, "verify")
    assert rc == 3 and "names no abandon-track record in the store's registry" in out


def test_a_cut_of_an_item_the_cited_abandon_does_not_cover_is_a_break(world, capsys):
    """The real abandon's hash, on an item that is not keyed to its migration: the predicate's item half holds as of the cut."""
    abandon_world(world)
    abandon = next(x for x in world.lines("repos.jsonl") if (bc.record_of(x) or {}).get("op") == "abandon-track")
    forge_cut(world, hashlib.sha256(abandon).hexdigest(), "alpha#1")
    rc, out, err = run(capsys, "verify")
    assert rc == 3 and "the cause does not hold as of this cut" in out and "alpha#1" in out


# --------------------------------------------------------------------------------------------------- the part B seam


# ------------------------------------------------------------------------------------------- part B: one verdict per close


def commit_as(repo: Path, who: str, msg: str, *, files: dict[str, str] | None = None, email: str = "x@x") -> str:
    """A commit by ``who`` (author and committer), pushed to origin/main and fetched, so it is on the main the board reads."""
    for name, text in (files or {}).items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    env = {**os.environ, "GIT_AUTHOR_NAME": who, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": who, "GIT_COMMITTER_EMAIL": email}
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", msg], cwd=repo, check=True, capture_output=True, env=env)
    git(repo, "push", "-q", "origin", "main")
    git(repo, "fetch", "-q")
    return git(repo, "rev-parse", "HEAD")


@dataclass
class ClosesWorld:
    store: Path
    target: bs.Target
    repo: Path
    out: Path
    merged: str          # alpha#N closed by a commit's Board-Closes trailer (tier: git author)
    doc: str             # closed by a document a commit added (first added <path>)
    ghost: str           # a document in the tree that no commit on main added (unattributed)
    open: str            # not closed
    rig: Rig | None = None


def build_closes_world(tmp_path: Path, monkeypatch, who: str = "Plain Person", email: str = "x@x") -> ClosesWorld:
    """A scratch store whose repo ``alpha`` is TRACKED (the real ceremony: register, signed begin-track, charter, signed track) with
    three closes in P4's shapes: a ``merged`` item closed by a commit's trailer, a ``document`` item closed by the commit that added
    its file, and a ``document`` item whose file is in the work tree but on no commit of main."""
    from tests.test_board_registry import land, manifest
    from tests.test_board_registry_sign import keyed, registered
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lhome"))
    monkeypatch.delenv("LEVAIN_BOARD_ITEMS", raising=False)
    monkeypatch.delenv("LEVAIN_LAUNCH_KEY", raising=False)
    store = tmp_path / "store"
    assert main(["board", "use", str(store)]) == 0
    store = Path(os.path.realpath(store))
    (store / "repos.jsonl").touch()
    tgt = bs.resolve_store(None)
    rig = Rig(tmp_path)
    monkeypatch.setattr(sg, "resolve_authority", lambda **kw: rig.source())
    repo = registered(tgt, lambda name: make_repo(tmp_path, name), "alpha")
    br.begin_track(tgt, "alpha", manifest(tmp_path, plan_items=["a"]), "yes", "desk 1", author=OPD, seal=rig.seal())
    mig = br.read_registry(tgt).fold.repos["alpha"].migration
    open_item = keyed(tgt, "alpha", f"{mig}:a")
    land(repo, "CHARTER.md", f"# Charter: alpha\nCharter-Version: 1\nExport: repo\n\n## Purpose\nT.\n\n## Criteria\n- C1: ok [judged: phill]\n\n"
                              f"## Constraints\n- K1: none\n\n## Plan\n- {open_item}\n")
    br.track(tgt, "alpha", "go", "desk 1", author=OPD, seal=rig.seal())
    merged = bs.add(tgt, "alpha", "merged one", "desk 1", "desk 1", author=OPD, now=NOW)[0]
    doc = bs.add(tgt, "alpha", "doc one", "desk 1", "desk 1", done="docs/report.md", done_kind="document", author=OPD, now=NOW)[0]
    ghost = bs.add(tgt, "alpha", "ghost doc", "desk 1", "desk 1", done="docs/ghost.md", done_kind="document", author=OPD, now=NOW)[0]
    commit_as(repo, who, f"close it\n\nBoard-Item: {merged}\nBoard-Closes: {merged}", email=email)
    commit_as(repo, "Doc Person", "adds the report", files={"docs/report.md": "r\n"})
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "ghost.md").write_text("never committed\n")
    return ClosesWorld(store, tgt, repo, tmp_path / "out", merged, doc, ghost, open_item, rig)


@pytest.fixture
def cw(tmp_path, monkeypatch) -> ClosesWorld:
    return build_closes_world(tmp_path, monkeypatch)


def state_closes(capsys) -> dict[str, dict]:
    """``levain board state --json``'s closed_by per item: what the trail's close lines must agree with."""
    main(["board", "state", "--json"])
    out, _ = capsys.readouterr()
    return {s["id"]: s["closed_by"] for s in json.loads(out)["items"] if s.get("closed_by")}


def close_lines(out: str) -> dict[str, str]:
    return {x.split(":", 1)[0].removeprefix("close ").removeprefix("BREAK close "): x for x in out.splitlines()
            if x.startswith(("close ", "BREAK close "))}


def test_live_verify_prints_one_verdict_per_close_derived_by_the_boards_own_function(cw, capsys):
    from levain.desk import board_author as ba
    closes = state_closes(capsys)
    assert set(closes) == {cw.merged, cw.doc, cw.ghost}
    rc, out, err = run(capsys, "verify")
    assert rc == 0, out + err
    lines = close_lines(out)
    assert set(lines) == set(closes)
    for item_id, by in closes.items():
        assert lines[item_id] == f"close {item_id}: {ba.describe(by)}"              # P4's words, from P4's function, never a second rule
    assert "asserted by git author Plain Person <x@x>" in lines[cw.merged]
    assert "first added docs/report.md, asserted by git author Doc Person" in lines[cw.doc]
    assert lines[cw.ghost].startswith(f"close {cw.ghost}: unattributed") and "no commit on main added it" in lines[cw.ghost]
    assert cw.open not in lines


def test_a_close_line_escapes_control_characters_an_author_name_carries(tmp_path, monkeypatch, capsys):
    cw = build_closes_world(tmp_path, monkeypatch, who="Evil\x1b[2J Person")
    rc, out, err = run(capsys, "verify")
    assert rc == 0, out + err
    assert "\x1b" not in out and "Evil\\x1b[2J Person" in out


def test_export_writes_each_closes_derived_closed_by_as_state_emits_it(cw, capsys):
    closes = state_closes(capsys)
    rc, out, err = run(capsys, "export", "--out", str(cw.out) + os.sep)
    assert rc == 0, err
    bundle = json.loads(next(cw.out.glob("trail-*.json")).read_text("utf-8"))
    assert bundle["closes"] == closes and "closes_unavailable" not in bundle


def test_a_close_by_an_author_with_an_empty_email_exports_and_verifies_with_no_break(tmp_path, monkeypatch, capsys):
    cw = build_closes_world(tmp_path, monkeypatch, who="No Mail", email="")
    bundle = exported(cw, capsys)
    assert json.loads(bundle.read_text("utf-8"))["closes"][cw.merged]["email"] == ""      # present-but-empty, not dropped
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 0, out + err
    assert "BREAK" not in out and close_lines(out)[cw.merged].endswith("(re-derived from the repo; matches the bundle)")


def exported(cw, capsys) -> Path:
    assert run(capsys, "export", "--out", str(cw.out) + os.sep)[0] == 0
    return next(cw.out.glob("trail-*.json"))


def test_bundle_verify_re_derives_every_close_when_the_repo_is_reachable(cw, capsys):
    bundle = exported(cw, capsys)
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 0, out + err
    lines = close_lines(out)
    assert set(lines) == {cw.merged, cw.doc, cw.ghost}
    assert all(x.endswith("(re-derived from the repo; matches the bundle)") for x in lines.values())
    assert "not re-derived" not in out


def test_bundle_verify_says_recorded_not_re_derived_when_the_repo_is_not_reachable_and_never_verified(cw, capsys):
    bundle = exported(cw, capsys)
    os.rename(cw.repo, str(cw.repo) + ".away")
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 0, out + err
    lines = close_lines(out)
    assert set(lines) == {cw.merged, cw.doc, cw.ghost}
    assert all(x.endswith("(recorded in the bundle, not re-derived (repo not available))") for x in lines.values())
    assert "matches" not in out and "re-derived from the repo" not in out and "closes: not re-derived (" in out


def test_a_bundled_closed_by_that_the_repo_derives_differently_is_a_break(cw, capsys):
    bundle = exported(cw, capsys)
    doc = json.loads(bundle.read_text("utf-8"))
    doc["closes"][cw.merged]["who"] = "Somebody Stronger"
    bundle.write_text(json.dumps(doc), "utf-8")
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 3, out + err
    assert f"BREAK close {cw.merged}: the bundle records [" in out and "Somebody Stronger" in out and "and the repo derives [" in out
    assert "matches the bundle" in close_lines(out)[cw.doc]                 # the others are unaffected


def test_a_bundled_close_of_an_item_the_repo_does_not_show_closed_is_a_break(cw, capsys):
    bundle = exported(cw, capsys)
    doc = json.loads(bundle.read_text("utf-8"))
    doc["closes"][cw.open] = doc["closes"][cw.merged]
    bundle.write_text(json.dumps(doc), "utf-8")
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 3 and f"BREAK close {cw.open}: " in out and "the repo does not show the item closed" in out


def test_a_close_the_repo_shows_that_the_bundle_does_not_record_is_said_and_not_a_break(cw, capsys):
    bundle = exported(cw, capsys)
    doc = json.loads(bundle.read_text("utf-8"))
    del doc["closes"][cw.ghost]
    bundle.write_text(json.dumps(doc), "utf-8")
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 0 and "the bundle records no close for it" in close_lines(out)[cw.ghost]


def test_a_bundle_with_no_closes_says_so_and_a_malformed_one_is_refused(cw, capsys):
    bundle = exported(cw, capsys)
    doc = json.loads(bundle.read_text("utf-8"))
    del doc["closes"]
    bundle.write_text(json.dumps(doc), "utf-8")
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 0 and bt.CLOSES_NONE in out
    doc["closes"] = {cw.merged: "not an object"}
    bundle.write_text(json.dumps(doc), "utf-8")
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 2 and "damaged bundle" in err


def test_a_derivation_that_fails_is_a_note_never_a_crash_and_never_a_break(cw, capsys, monkeypatch):
    from levain.desk import board_cli
    monkeypatch.setattr(board_cli, "state_doc", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    rc, out, err = run(capsys, "verify")
    assert rc == 0, out + err
    assert "closes: not derived (the closes could not be derived (RuntimeError: boom))" in out and "BREAK" not in out


def test_one_close_whose_author_cannot_be_read_is_that_closes_note_not_a_break(cw, capsys, monkeypatch):
    from levain.desk import board_author as ba
    real = ba.closer_of_item

    def flaky(repo, item, *a, **k):
        if item.id == cw.merged:
            raise ba.be.GitFailed("git went away")
        return real(repo, item, *a, **k)
    monkeypatch.setattr(ba, "closer_of_item", flaky)
    rc, out, err = run(capsys, "verify")
    assert rc == 0, out + err
    lines = close_lines(out)
    assert "the author could not be read (git went away)" in lines[cw.merged] and "Doc Person" in lines[cw.doc]


# --------------------------------------------------------------------- P6 fix 2: never a verdict stronger than the evidence


def rechain(lines: list[bytes], start: int) -> list[bytes]:
    """Recompute every ``prev`` from line ``start`` (0-based) down: what a forger who can write the file does."""
    for i in range(start, len(lines)):
        r = bc.record_of(lines[i])
        if r is not None and "prev" in r:
            r["prev"] = bc.line_hash(lines[i - 1])
            lines[i] = json.dumps(r, separators=(",", ":")).encode()
    return lines


def export_bundle(world: World, capsys) -> Path:
    rc, out, err = run(capsys, "export", "--out", str(world.out) + os.sep)
    assert rc == 0, err
    return next(world.out.glob("trail-*.json"))


def test_an_unsigned_ack_of_a_break_before_the_last_signed_record_never_reads_the_prefix_anchored(world, capsys):
    """L1 F1, run: line 3 rewritten, line 4 (signed) chain-broken, the break acknowledged by an unsigned ack-torn at the tail."""
    lines = world.lines("items.jsonl")
    lines[2] = lines[2].replace(b"third", b"thirD")
    world.write("items.jsonl", lines)
    assert main(["board", "ack-torn", "--file", "items.jsonl", "--line", "4", "--why", "x", "--by", "phill",
                 "--author-kind", "operator"]) == 0
    rc, out, err = run(capsys, "verify")
    assert ("items.jsonl: anchored from line 4 through line 4 only (signed at 2026-11-17T12:00:00Z): the signature commits the "
            "chain back to the acknowledged line 4, by the operator's key; lines 1-3 are committed by NOTHING but the "
            "acknowledgement of line 4 by phill (asserted, line 10)") in out
    assert "every record up to it is committed" not in out.split("repos.jsonl (")[0]


def test_an_acknowledged_tear_before_the_last_signed_record_bounds_the_anchor(world):
    """A tear at line t severs the chain: line t+1 hashes the torn bytes, and nothing links line t to what precedes it."""
    # the clean world: the acked tear (line 7) is AFTER the last signed record (line 4), so the anchor is whole
    assert report(world)["items.jsonl"].anchor.startswith("items.jsonl: anchored through line 4 ")
    bs.decide(world.target, "alpha#5", "parked", "later", "phill", author=OP, now=NOW, seal=world.rig.seal())   # signed, line 10
    assert report(world)["items.jsonl"].anchor == (
        "items.jsonl: anchored from line 7 through line 10 only (signed at 2026-11-17T12:00:00Z): the signature commits the chain "
        "back to the acknowledged line 7, by the operator's key; lines 1-6 are committed by NOTHING but the acknowledgement of "
        "line 7 by phill (asserted, line 8)")


def test_a_relayed_answer_signature_binds_this_question_and_these_words(world, capsys):
    """L1 F2 / codex HIGH 3: an old signature, valid, over words the row no longer holds, never reads "(signed answer)"."""
    forged = "yes, cut it. Forged words he never typed."
    world.band.rows[Q1] = rel.BandRow("Cut alpha#3?", forged, "signed-phone", "native")
    lines = world.lines("items.jsonl")
    i = next(k for k, x in enumerate(lines) if b'"relayed"' in x)
    rec = json.loads(lines[i])
    rec["words"] = "Forged words he never typed"
    rec["source"]["answer_sha256"] = rel.digest(forged)
    lines[i] = json.dumps(rec, separators=(",", ":")).encode()
    world.write("items.jsonl", rechain(lines, i + 1))
    rc, out, err = run(capsys, "verify")
    assert rc == 3 and "the answer's signature is over other words than the row's answer: it does not bind this answer" in out
    world.band.signs[Q1] = answer_sig(world.rig, Q1, asked="Cut it?", text=forged)
    assert "it does not bind this question" in _relay_report(world).verdict


def test_an_answer_signed_as_an_option_pick_says_what_the_signature_covers_and_no_more(world, capsys):
    world.band.signs[Q1] = answer_sig(world.rig, Q1, pick="(a) yes")
    r = _relay_report(world)
    assert r.ok and "(answer signed as the pick '(a) yes'; the signature does not cover the option key" in r.verdict
    assert "(signed answer)" not in r.verdict
    rc, out, err = run(capsys, "verify")
    assert rc == 0 and "items.jsonl: 1 relayed record(s) rest on an answer no signature of a key you hold binds" in out


def test_an_answer_signed_by_a_key_of_another_class_is_a_break(world, monkeypatch):
    """codex MED 5: the band's badge trusts the laptop's terminal key; a display-phone key's terminal signature is not his answer."""
    phone = Rig(world.out.parent / "phone", key_class="display-phone")
    world.band.signs[Q1] = answer_sig(phone, Q1)
    both = {**world.rig.snapshot().keys, **phone.snapshot().keys}
    snap = world.rig.snapshot()
    monkeypatch.setattr(sg, "resolve_authority", lambda **kw: lambda: type(snap)(store_id="s", seq=2, head_hash="h", keys=both,
                                                                                 policy=snap.policy))
    r = _relay_report(world)
    assert not r.ok and "is a display-phone key; an answer is signed with the laptop's terminal key (laptop-presence)" in r.verdict


def forge_bundle_key(world: World, capsys, tmp: Path) -> Path:
    """A bundle whose records, answer and keys were all re-signed with a forger's own laptop-class key, chain recomputed."""
    bundle = export_bundle(world, capsys)
    doc = json.loads(bundle.read_text("utf-8"))
    other = Rig(tmp / "forger")
    for name in ("items.jsonl", "repos.jsonl"):
        lines = [x.encode() for x in doc["files"][name]["lines"]]
        for i, x in enumerate(lines):
            r = bc.record_of(x)
            if r is not None and r.get("grade") in sg.SIGNED_GRADES:
                r["sig"]["fingerprint"] = other.fp
                r["sig"]["sshsig"] = other.sign(sg.signing_bytes(r))
                lines[i] = json.dumps(r, separators=(",", ":")).encode()
                lines = rechain(lines, i + 1)
        doc["files"][name]["lines"] = [x.decode() for x in lines]
    k = dict(doc["authority"]["keys"][0])
    k.update(fingerprint=other.fp, public_key=other.public)
    doc["authority"]["keys"] = [k]
    doc["answers"][Q1]["sign"] = answer_sig(other, Q1)
    bundle.write_text(json.dumps(doc))
    return bundle


def test_a_bundle_never_supplies_its_own_trust_root(world, capsys, tmp_path, monkeypatch):
    """L1 F3 / codex HIGH 2: the verifier's own authority log is the trust root; the bundle's keys are claims compared to it."""
    bundle = forge_bundle_key(world, capsys, tmp_path)
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 3 and "which the authority log you hold does not" in out and "is not a key in the authority log" in out
    assert SIGNED not in out and "(signed answer)" not in out and "anchored through" not in out

    def gone(**kw):
        raise bs.BoardError("holds no cockpit.db")
    monkeypatch.setattr(sg, "resolve_authority", gone)
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 0 and SIGNED not in out and bt.BUNDLE_KEY_WORDS in out
    assert "NOT ANCHORED: the signatures were checked only against keys the bundle itself names" in out


def test_a_bundle_and_the_live_store_give_the_same_verdicts_under_the_same_trust_root(world, capsys):
    bundle = export_bundle(world, capsys)
    live = run(capsys, "verify")[1]
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 0
    body = lambda text: [x for x in text.splitlines() if not x.startswith(("authority:", "close"))]   # noqa: E731  (a close line says whether it was re-derived: that differs by design, and is pinned in the part B tests)
    assert body(out) == body(live)


def test_a_signed_record_in_a_store_with_no_id_is_a_break(world, capsys):
    """L1 F8 / codex HIGH 4: an empty store id is never a skipped comparison."""
    bundle = export_bundle(world, capsys)
    doc = json.loads(bundle.read_text("utf-8"))
    doc["store"] = ""
    bundle.write_text(json.dumps(doc))
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 3 and "this store has no id: the store it was signed for cannot be compared" in out


def test_a_well_chained_record_its_fold_rejects_is_a_break(world, capsys):
    """codex HIGH 1: the file's own fold judges every record; v: 2 is not a record this store reads."""
    lines = world.lines("items.jsonl")
    rec = json.loads(lines[-1])
    rec.update(v=2, id="alpha#99", prev=bc.line_hash(lines[-1]))
    world.write("items.jsonl", lines + [json.dumps(rec, separators=(",", ":")).encode()])
    rc, out, err = run(capsys, "verify")
    assert rc == 3 and "items.jsonl:10 add alpha#99: asserted by desk 24, unsigned, but: the fold rejects this record" in out
    world.write("repos.jsonl", world.lines("repos.jsonl") + [b'{"v":1,"op":"pause","repo":"zeta","prev":"'
                                                             + bc.line_hash(world.lines("repos.jsonl")[-1]).encode() + b'"}'])
    rc, out, err = run(capsys, "verify")
    assert rc == 3 and "repos.jsonl:5 pause zeta" in out and "the fold rejects this record" in out


def test_a_signed_begin_track_binds_a_later_relayed_track(world, capsys):
    """gemini HIGH 1 / complement MED 2 / codex MED 7: a signed begin-track records its manifest like any other."""
    lines = world.lines("repos.jsonl")
    begin = next(json.loads(x) for x in lines if b'"begin-track"' in x and b'"signed-' in x)
    relayed = json.loads(next(x for x in lines if b'"relayed"' in x))
    q3 = "q-" + "0000000c" * 3
    question = f"Track alpha? manifest sha256 {begin['manifest_sha256']}"
    world.band.rows[q3] = rel.BandRow(question, "yes, track alpha", "unsigned-terminal", "native")
    rec = {"v": relayed["v"], "store": relayed["store"], "op": "track", "repo": "alpha", "at": NOW, "by": "desk 24",
           "words": "track alpha", "migration": begin["migration"], "charter_blob": "c" * 40, "retire": [], "author": DESK, "grade": "relayed",
           "source": {"np": q3, "producer": "native", "question_sha256": rel.digest(question),
                      "answer_sha256": rel.digest("yes, track alpha")},
           "prev": bc.line_hash(lines[-1])}
    world.write("repos.jsonl", lines + [json.dumps(rec, separators=(",", ":")).encode()])
    rc, out, err = run(capsys, "verify")
    assert rc == 0, out + err
    assert f"repos.jsonl:5 track alpha: agent desk 24, relaying {q3} (unsigned answer)" in out


def test_the_empty_file_head_is_on_every_chain_and_a_file_with_no_head_says_so(world, capsys):
    """L1 F7: a head kept while a file was empty is still on its chain once it grew; one head given reports the other unchecked."""
    empty = hashlib.sha256(b"").hexdigest()
    rc, out, err = run(capsys, "verify", "--head", f"items={empty}")
    assert rc == 0, out + err
    assert f"items.jsonl: kept head {empty[:12]} is the empty file's; 9 lines were written after it (the file grew" in out
    assert "repos.jsonl: no kept head given: a truncation of this file is not checked" in out
    assert run(capsys, "verify", "--head", f"items={empty}", "--head", f"items={empty}")[0] == 2


def test_a_revoked_signer_is_named_on_the_anchor_line(world, monkeypatch):
    """L1 F5: the anchor never says "committed by the operator's key" of a key revoked since."""
    monkeypatch.setattr(sg, "resolve_authority", lambda **kw: world.rig.source(revoked_seq=9))
    anchor = report(world)["items.jsonl"].anchor
    assert anchor == ("items.jsonl: NOT ANCHORED by a key you can still trust: the signature at line 4 (signed at "
                      "2026-11-17T12:00:00Z) commits every record up to it, by a key revoked since (seq 9): a forgery signed "
                      "with it after the revocation would read the same")
    assert "the operator's key" not in anchor


def test_a_legacy_grade_after_the_cutover_and_an_asserted_decision_are_breaks(world, capsys):
    """L1 F6 / complement MED 4: the writer's policy, held by the reader."""
    lines = world.lines("items.jsonl")
    rec = json.loads(lines[-1])
    legacy = {**rec, "grade": "legacy", "id": "alpha#6", "prev": bc.line_hash(lines[-1])}
    lines.append(json.dumps(legacy, separators=(",", ":")).encode())
    parked = {"v": 1, "op": "decide", "id": "alpha#5", "at": NOW, "by": "desk 24", "decision": "parked", "date": "2026-12-01",
              "words": "later", "author": DESK, "grade": "asserted", "prev": bc.line_hash(lines[-1])}
    lines.append(json.dumps(parked, separators=(",", ":")).encode())
    world.write("items.jsonl", lines)
    rc, out, err = run(capsys, "verify")
    assert rc == 3
    assert "items.jsonl:10 add alpha#6: legacy, but: graded legacy after the cutover" in out
    assert "items.jsonl:11 decide alpha#5: asserted by desk 24, unsigned, but: a decision-class decide recorded asserted" in out


def test_every_record_gets_exactly_one_verdict_line(world, capsys):
    """complement LOW 9 / codex LOW 13 / gemini MED 3: a bad cause is part of the record's one verdict, and one break."""
    abandon_world(world)
    reg = next(x for x in world.lines("repos.jsonl") if (bc.record_of(x) or {}).get("op") == "register")
    forge_cut(world, hashlib.sha256(reg).hexdigest())
    rep = report(world)["items.jsonl"]
    ns = [r.n for r in rep.records]
    assert len(ns) == len(set(ns))
    n = len(world.lines("items.jsonl"))
    assert sum(1 for b in rep.breaks if b.startswith(f"line {n}:")) == 1


def test_control_characters_in_a_record_are_escaped_in_every_printed_line(world, capsys):
    """L1 F9."""
    lines = world.lines("items.jsonl")
    rec = json.loads(lines[-1])
    rec["author"]["name"] = "x\x1b[2K\x1b[1Aevil "
    lines[-1] = json.dumps(rec, separators=(",", ":")).encode()
    world.write("items.jsonl", lines)
    rc, out, err = run(capsys, "verify")
    assert "\x1b" not in out and " " not in out and "x\\x1b[2K\\x1b[1Aevil\\u2028" in out


def test_a_bundle_with_a_malformed_answer_is_refused_not_a_traceback(world, capsys):
    """complement MED 6 / gemini MED 7."""
    bundle = export_bundle(world, capsys)
    for bad in ({}, None, {"question": "q", "answer": 1, "answer_grade": "unknown", "producer": "native"}):
        doc = json.loads(bundle.read_text("utf-8"))
        doc["answers"][Q1] = bad
        bundle.write_text(json.dumps(doc))
        rc, out, err = run(capsys, "verify", str(bundle))
        assert rc == 2 and "damaged bundle" in err
    doc["answers"][Q1] = {"question": "q", "answer": "a", "answer_grade": "unknown", "producer": "native"}
    doc["files"]["items.jsonl"]["lines"] = "abc"
    bundle.write_text(json.dumps(doc))
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 2 and "damaged bundle" in err


def test_export_carries_an_unreadable_signing_record_and_never_crashes(world, capsys):
    """codex MED 10 / L1 F12: a sidecar that raises is carried as unreadable; verifying the bundle is a break for that record."""
    def boom(np_id):
        raise RuntimeError("sidecar")
    world.band.sign = boom
    bundle = export_bundle(world, capsys)
    assert "signing record could not be read" in json.loads(bundle.read_text("utf-8"))["answers"][Q1]["unreadable"]
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 3 and "could not be read when the bundle was made" in out


def test_a_failed_export_write_removes_its_own_partial_file_and_exits_2(world, capsys, monkeypatch):
    """codex MED 12 / L1 F13: a failed fsync leaves nothing behind; a directory that cannot be made is a clean refusal."""
    def no_fsync(fd):
        raise OSError(28, "No space left on device")
    with monkeypatch.context() as m:
        m.setattr(bt.os, "fsync", no_fsync)
        rc, out, err = run(capsys, "export", "--out", str(world.out / "b.json"))
    assert rc == 2 and "the partial file was removed" in err and not (world.out / "b.json").exists()
    blocker = world.out.parent / "afile"
    blocker.write_text("x")
    rc, out, err = run(capsys, "export", "--out", str(blocker / "sub" / "b.json"))
    assert rc == 2 and "cannot be made" in err


def test_export_refuses_a_git_work_tree_reached_through_a_symlink(world, tmp_path, capsys):
    """codex MED 11 / gemini LOW 8 / complement LOW-MED 8."""
    repo = make_repo(tmp_path, "delta")
    (repo / "sub").mkdir()
    link = tmp_path / "outside-link"
    link.symlink_to(repo / "sub")
    rc, out, err = run(capsys, "export", "--out", str(link / "t.json"))
    assert rc == 2 and "inside a git work tree" in err and not (repo / "sub" / "t.json").exists()


def test_a_store_id_never_steers_the_bundle_name_out_of_its_directory():
    """L1 F14."""
    assert bt.destination("/tmp/x" + os.sep, "../../e").name.startswith("trail-______e-")


# ------------------------------------------------- P6 fix 3 (L3 round 2): no line claims more than another line's evidence


def test_the_tail_never_says_anchored_where_the_anchor_line_does_not(world, capsys, monkeypatch):
    """codex 1 HIGH / gemini MED 1: a file ending at a signature-valid record, read from a bundle with no authority of yours,
    or with the signer revoked since: the tail line and the anchor line are one statement."""
    bs.decide(world.target, "alpha#1", "parked", "later", "phill", author=OP, now=NOW, seal=world.rig.seal())    # line 10, last
    assert report(world)["items.jsonl"].tail == ("items.jsonl: 0 records after the last signed record (at 2026-11-17T12:00:00Z): "
                                                 "the tail is anchored")
    bundle = export_bundle(world, capsys)

    def gone(**kw):
        raise bs.BoardError("holds no cockpit.db")
    monkeypatch.setattr(sg, "resolve_authority", gone)
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 0 and "items.jsonl: NOT ANCHORED: the signatures were checked only against keys the bundle itself names" in out
    assert "the tail is anchored" not in out
    assert ("items.jsonl: 0 records after the last signature-valid record (line 10, at 2026-11-17T12:00:00Z); no anchor you can "
            "trust covers it: its signature was checked only against a key the bundle names") in out
    monkeypatch.setattr(sg, "resolve_authority", lambda **kw: world.rig.source(revoked_seq=9))
    r = report(world)["items.jsonl"]
    assert r.anchor.startswith("items.jsonl: NOT ANCHORED by a key you can still trust: the signature at line 10 ")
    assert r.tail.endswith("no anchor you can trust covers it: its signer's key is revoked since (seq 9)")


def test_a_revoked_answer_signer_never_reads_anchored(world, monkeypatch):
    """codex 14: the answer's key revoked since: the record says so, a note says what it means, the count is apart."""
    monkeypatch.setattr(sg, "resolve_authority", lambda **kw: world.rig.source(revoked_seq=9))
    r = report(world)["items.jsonl"]
    assert {x.n: x.verdict for x in r.records}[6] == f"agent desk 24, relaying {Q1} (signed answer, by a key revoked since (seq 9))"
    assert any(n.startswith(f"line 6: the answer {Q1} is signed by a key revoked at seq 9") for n in r.notes)
    assert r.relayed == ("items.jsonl: 1 relayed record(s) rest on an answer signed by a key revoked since: a forgery signed with "
                         "it after the revocation would read the same, so nothing you can trust anchors his words")
    assert "anchored by the answer's own signature" not in r.relayed


def test_an_option_pick_says_what_it_covers_in_every_trust_mode(world, capsys, monkeypatch):
    """codex 2 / gemini LOW 3: the pick-only scope survives bundle-only trust (no early return drops it)."""
    world.band.signs[Q1] = answer_sig(world.rig, Q1, pick="(a) yes")
    bundle = export_bundle(world, capsys)

    def gone(**kw):
        raise bs.BoardError("holds no cockpit.db")
    monkeypatch.setattr(sg, "resolve_authority", gone)
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 0
    assert ("(answer signed as the pick '(a) yes' under a key the bundle names; not checked against an authority you hold; the "
            "signature does not cover the option key") in out


def test_an_ack_of_a_line_that_is_not_torn_is_a_break(world, capsys):
    """codex 3: the file's own fold reports it; verify never suppresses it."""
    lines = world.lines("items.jsonl")
    ack = json.loads(next(x for x in lines if b'"ack-torn"' in x))
    ack.update(line=3, sha256=bc.line_hash(lines[2]), prev=bc.line_hash(lines[-1]))
    world.write("items.jsonl", lines + [json.dumps(ack, separators=(",", ":")).encode()])
    rc, out, err = run(capsys, "verify")
    assert rc == 3 and ("items.jsonl:10 ack-torn: asserted by phill, unsigned, but: it acknowledges line 3 as torn, and line 3 is "
                        "not torn (the file's own fold reports this ack)  [BREAK]") in out


def test_an_asserted_move_the_writer_would_call_a_decision_is_a_break(world, capsys):
    """codex 8: classified by board_sign.move_is_decision on the registry as it stood before the move."""
    lines = world.lines("repos.jsonl")
    reg = json.loads(next(x for x in lines if b'"register"' in x and b'"alpha"' in x))
    rec = {"v": reg["v"], "store": reg["store"], "op": "move", "repo": "alpha", "at": NOW, "by": "phill", "words": "moved",
           "path": reg["path"], "origin": reg["origin"], "root": reg["root"], "author": OP, "grade": "asserted",
           "prev": bc.line_hash(lines[-1])}
    rec["path"] = reg["path"] + "-relocated"                # the same repository relocated: asserted-ok, as the writer says
    world.write("repos.jsonl", lines + [json.dumps(rec, separators=(",", ":")).encode()])
    rc, out, err = run(capsys, "verify")
    assert rc == 0, out + err
    rec.update(origin="/elsewhere/alpha.git")                # a tracking repo's origin changed: decision-class
    world.write("repos.jsonl", lines + [json.dumps(rec, separators=(",", ":")).encode()])
    rc, out, err = run(capsys, "verify")
    assert rc == 3 and ("repos.jsonl:5 move alpha: asserted by phill, unsigned, but: a decision-class move (the repo is tracking "
                        "and the move changes its recorded origin or root set) recorded asserted") in out


def test_a_signed_record_with_a_break_is_never_the_last_signed_record(world, capsys):
    """gemini MED 1, second half: a signed record that carries a break anchors nothing."""
    bs.decide(world.target, "alpha#1", "parked", "later", "phill", author=OP, now=NOW, seal=world.rig.seal())    # line 10
    lines = world.lines("items.jsonl")
    lines[8] = lines[8].replace(b"fifth", b"fifTH")         # line 9 edited: the signed line 10 is chain-broken
    world.write("items.jsonl", lines)
    r = report(world)["items.jsonl"]
    assert r.anchor.startswith("items.jsonl: anchored through line 4 ") and "the tail is anchored" not in r.tail
    assert r.tail.startswith("items.jsonl: 5 records after the last signed record")


def test_a_line_the_fold_dropped_by_an_ack_the_chain_rule_does_not_honour_is_attributed_never_a_crash(world, capsys):
    """gemini MED 2 / deepseek B: line 9 malformed AND chain-broken, dropped by a torn-style ack (the fold's rule) the chain's
    own ack rule does not honour (a chain break needs a chain ack): attributed to the ack the fold names, no KeyError."""
    lines = world.lines("items.jsonl")
    r = json.loads(lines[8])
    r.update(v=2, prev="0" * 64)
    lines[8] = json.dumps(r, separators=(",", ":")).encode()
    ack = {k: v for k, v in json.loads(lines[7]).items() if k not in ("have", "want")}
    ack.update(line=9, sha256=bc.line_hash(lines[8]), prev=bc.line_hash(lines[8]))
    world.write("items.jsonl", lines + [json.dumps(ack, separators=(",", ":")).encode()])
    bs.decide(world.target, "alpha#1", "parked", "later", "phill", author=OP, now=NOW, seal=world.rig.seal())    # line 11
    rc, out, err = run(capsys, "verify")
    assert rc == 0, out + err
    assert "items.jsonl:9 add alpha#5: not read: torn for itself and acknowledged by phill (asserted, line 10)" in out
    assert ("lines 1-8 are committed by NOTHING but the acknowledgement of line 9 by phill (asserted, line 10)") in out


def test_a_bundle_key_of_the_wrong_shape_is_a_damaged_bundle(world, capsys):
    """gemini LOW 4: exit 2, never a traceback in the trust-root comparison."""
    bundle = export_bundle(world, capsys)
    doc = json.loads(bundle.read_text("utf-8"))
    doc["authority"]["keys"][0]["public_key"] = None
    bundle.write_text(json.dumps(doc))
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 2 and "is a damaged bundle" in err


def test_safe_escapes_the_unicode_line_separators_by_codepoint():
    """complement LOW: the pattern names U+2028/U+2029 by escape, and a plain space is never escaped."""
    assert bt.safe("a\u2028b\u2029c d") == "a\\u2028b\\u2029c d"


def test_an_asserted_decision_class_record_before_the_cutover_is_not_a_break_and_after_it_is(world, capsys):
    """complement raw 43 / codex 86 / gemini 124 / deepseek 166: a grandfathered record is judged by the pre-cutover rules."""
    first = json.loads(world.lines("items.jsonl")[0])
    first.pop("prev", None)
    first.pop("grade", None)
    parked = {"v": 1, "op": "decide", "id": first["id"], "at": NOW, "by": "desk 24", "decision": "parked", "date": "2026-12-01",
              "words": "later", "author": DESK, "grade": "asserted"}
    dump = lambda *rs: [json.dumps(r, separators=(",", ":")).encode() for r in rs]       # noqa: E731
    world.write("items.jsonl", dump(first, parked))                                       # no chained record: all pre-cutover
    rc, out, err = run(capsys, "verify")
    assert "a decision-class decide recorded asserted" not in out, out
    world.write("items.jsonl", dump(first, {**parked, "prev": bc.line_hash(dump(first)[0])}))   # the cutover is line 2: it is after
    rc, out, err = run(capsys, "verify")
    assert "a decision-class decide recorded asserted" in out, out
    rl = world.lines("repos.jsonl")
    reg, track = json.loads(rl[0]), json.loads(next(x for x in rl if b'"begin-track"' in x and b'"alpha"' in x))
    for r in (reg, track):
        for k in ("prev", "grade", "sig", "source"):
            r.pop(k, None)
    mv = {k: v for k, v in reg.items() if k not in ("prev", "grade")}
    mv.update(op="move", words="moved", origin="/elsewhere/alpha.git", author=OP, grade="asserted")   # the repo is tracking
    world.write("repos.jsonl", dump(reg, track, mv))                                      # unchained throughout: all pre-cutover
    rc, out, err = run(capsys, "verify")
    assert "a decision-class move" not in out, out
    world.write("repos.jsonl", dump(reg, track, {**mv, "prev": bc.line_hash(dump(reg, track)[1])}))   # the cutover is line 3
    rc, out, err = run(capsys, "verify")
    assert "a decision-class move" in out, out


def test_the_control_pattern_holds_no_literal_line_separator_characters():
    """complement raw 42: the pattern is written with escapes; an editor that normalised them would be caught here."""
    src = Path(bt.__file__).read_text("utf-8")
    line = next(x for x in src.splitlines() if x.startswith("_CONTROL = re.compile"))
    assert " " not in line and " " not in line and "\\u2028\\u2029" in line
    assert " " not in bt._CONTROL.pattern and " " not in bt._CONTROL.pattern


def test_stray_signer_fp_under_a_non_signed_state_is_a_break(world):
    world.band.signs[Q1] = {"sign_state": "unsigned", "signer_fp": "x"}
    r = _relay_report(world)
    assert not r.ok and "carries signature material under sign_state 'unsigned'" in r.verdict


# --------------------------------------------------------------------- P6 fix 4: "could not check" is never "a different claim"
# REAL tiers: a scratch cockpit authority log (enrolled with P4's own link builder, tests/test_p4_close_author.py's
# ``Scratch._link``: ``auth.sign_and_append`` over a cockpit.db with its anchor), a commit SSH-signed by a key enrolled there,
# and a Board-Seat trailer whose launch record CLAIMED the item (a real desk store under the scratch LEVAIN_HOME).
_REAL_RESOLVE = sg.resolve_authority


@dataclass
class TierWorld:
    cw: ClosesWorld
    cockpit: Path
    laptop: object
    boot: object
    signed: str
    trailer: str

    def link(self, op: dict, at: datetime) -> None:
        import sqlite3

        from levain import authority as auth
        from levain.authority import ReplayCache, anchor_path
        conn = sqlite3.connect(self.cockpit / "cockpit.db", isolation_level=None)
        try:
            auth.sign_and_append(conn, op, signer_fp=self.boot.fp, sign=lambda m: self.boot.sign(m, auth.NAMESPACE),
                                 clock=lambda: at, cache=ReplayCache(), anchor=anchor_path(self.cockpit))
        finally:
            conn.close()

    def revoke(self) -> None:
        self.link({"op": "revoke_key", "fingerprint": self.laptop.fp}, datetime(2026, 11, 15, tzinfo=timezone.utc))


def build_tier_world(tmp_path: Path, monkeypatch) -> TierWorld:
    from levain.cockpit.firejournal import CockpitDB
    from levain.desk import lease
    from levain.desk.store import CoordStore
    from tests.test_consent_queue import Key
    cw = build_closes_world(tmp_path, monkeypatch)
    monkeypatch.setattr(sg, "resolve_authority", _REAL_RESOLVE)       # from here the install's OWN (scratch) cockpit log
    consent = tmp_path / "lhome" / "consent"
    consent.mkdir(parents=True, exist_ok=True)
    os.chmod(consent, 0o700)
    kd = tmp_path / "keys-tier"
    kd.mkdir()
    laptop, boot = Key(kd, "laptop"), Key(kd, "boot")
    ck = tmp_path / "cockpit"
    ck.mkdir()
    CockpitDB(ck).set_install_class("reported")
    tw = TierWorld(cw, ck, laptop, boot, "", "")
    for fp, pub in ((boot.fp, boot.public_key), (laptop.fp, laptop.public_key), (cw.rig.fp, cw.rig.public)):
        tw.link({"op": "enrol", "fingerprint": fp, "public_key": pub, "class": "laptop-presence", "custody": "passphrase"},
                datetime(2026, 11, 1, tzinfo=timezone.utc))
    (consent / "claude_code.json").write_text(json.dumps({"cockpit_data_root": str(ck)}))
    store = CoordStore(consent)
    store.coord_set_agent("claude", cwd=str(cw.repo))
    store.accept_drop("fix the build", surface="cli")
    ln = store.coord_status()["launches"][0]
    tw.signed = bs.add(cw.target, "alpha", "signed-commit close", "desk 1", "desk 1", author=OPD, now=NOW)[0]
    tw.trailer = bs.add(cw.target, "alpha", "trailer close", "desk 1", "desk 1", author=OPD, now=NOW)[0]
    with store._write() as (conn, now):
        lease.claim_board_items(conn, now, ln["launch_id"], [tw.trailer])

    def commit(msg: str, who: str, key=None) -> None:
        env = {**os.environ, "GIT_AUTHOR_NAME": who, "GIT_AUTHOR_EMAIL": "x@x", "GIT_COMMITTER_NAME": who,
               "GIT_COMMITTER_EMAIL": "x@x", "GIT_AUTHOR_DATE": "2026-11-12T11:00:00+00:00",
               "GIT_COMMITTER_DATE": "2026-11-12T12:00:00+00:00"}
        pre = ["-c", "gpg.format=ssh", "-c", f"user.signingkey={key.path}", "-c", "commit.gpgsign=true"] if key else []
        subprocess.run(["git", *pre, "commit", "-q", "--allow-empty", "-m", msg], cwd=cw.repo, check=True, capture_output=True,
                       env=env)
        git(cw.repo, "push", "-q", "origin", "main")
        git(cw.repo, "fetch", "-q")
    commit(f"tier1\n\nBoard-Item: {tw.signed}\nBoard-Closes: {tw.signed}", "Op Person", key=laptop)
    commit(f"tier2\n\nBoard-Item: {tw.trailer}\nBoard-Closes: {tw.trailer}\nBoard-Seat: {ln['session_key']}", "Seat Person")
    return tw


@pytest.fixture
def tw(tmp_path, monkeypatch) -> TierWorld:
    return build_tier_world(tmp_path, monkeypatch)


def no_authority_log(monkeypatch) -> None:
    def gone(**kw):
        raise bs.BoardError("holds no cockpit.db")
    monkeypatch.setattr(sg, "resolve_authority", gone)


def edit_bundle(bundle: Path, change) -> Path:
    doc = json.loads(bundle.read_text("utf-8"))
    change(doc)
    bundle.write_text(json.dumps(doc), "utf-8")
    return bundle


def test_the_real_signed_and_trailer_tiers_re_derive_and_match_and_the_bundle_carries_no_degraded(tw, capsys):
    bundle = exported(tw.cw, capsys)
    closes = json.loads(bundle.read_text("utf-8"))["closes"]
    assert closes[tw.signed]["tier"] == "signed" and closes[tw.trailer]["tier"] == "trailer"
    assert not any("degraded" in c for c in closes.values())
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 0, out + err
    lines = close_lines(out)
    assert lines[tw.signed].endswith("(re-derived from the repo; matches the bundle)") and "signed by an operator key" in lines[tw.signed]
    assert lines[tw.trailer].endswith("(re-derived from the repo; matches the bundle)") and "asserted (trailer)" in lines[tw.trailer]


# --------------------------------------------------------------------- P6 fix 6: each recorded close as independent predicates
def _no_launch_records(monkeypatch) -> None:
    import sqlite3

    from levain.desk import lease

    def unreadable(conn, key):
        raise sqlite3.OperationalError("database disk image is malformed")
    monkeypatch.setattr(lease, "_launch_by_key", unreadable)


def _author_unread(monkeypatch) -> None:
    from levain.desk import board_author as ba

    def unread(repo, item, *a, **k):
        raise ba.be.GitFailed("git went away")
    monkeypatch.setattr(ba, "closer_of_item", unread)


def _other_repo(tw: TierWorld, tmp_path: Path) -> None:
    import shutil
    elsewhere = make_repo(tmp_path / "other", "alpha")
    shutil.move(str(tw.cw.repo), str(tmp_path / "alpha.real"))
    shutil.move(str(elsewhere), str(tw.cw.repo))


CAPS = ("full", "no-log", "no-launch-records", "author-unread", "other-repo")


def _apply(cap: str, tw: TierWorld, monkeypatch, tmp_path: Path) -> None:
    if cap == "no-log":
        no_authority_log(monkeypatch)
    elif cap == "no-launch-records":
        _no_launch_records(monkeypatch)
    elif cap == "author-unread":
        _author_unread(monkeypatch)
    elif cap == "other-repo":
        _other_repo(tw, tmp_path)


_VACUOUS = {"tier": "unattributed", "who": None, "how": "the author could not be read (git went away)", "commit": None}


def _rows(tw: TierWorld, base: dict) -> list[tuple[str, str, Any, set[str]]]:
    """``(name, item, mutate(record), the capabilities under which rc 0 is RIGHT)``: one field changed per row. rc 0 is right only
    where that field's predicate depends on the capability the verifier lacks; everywhere else the row is a BREAK."""
    S, T, G, D = tw.signed, tw.trailer, tw.cw.merged, tw.cw.doc
    held_other = base[G]["commit"]
    blind = {"author-unread", "other-repo"}           # no facts of the commit (unread) / a repo that does not hold it
    return [
        ("none", S, lambda r: None, set(CAPS)),
        ("signed: tier -> git, who kept", S, lambda r: r.update(tier="git", how="asserted by git author"), blind),
        ("signed: who -> kEVIL", S, lambda r: r.update(who="kEVIL"), blind),
        ("signed: email added", S, lambda r: r.update(email="x@x"), set()),            # the tier's own shape: every capability
        ("signed: committer added", S, lambda r: r.update(committer="Evil <e@e>"), blind),
        ("signed: how altered", S, lambda r: r.update(how="signed by the operator"), set()),
        ("signed: commit -> another HELD close", S, lambda r: r.update(commit=held_other), {"other-repo"}),
        ("signed: commit -> unheld sha", S, lambda r: r.update(commit="ab" * 20), {"other-repo"}),
        ("signed: close_commit added", S, lambda r: r.update(close_commit=held_other), {"other-repo"}),
        ("git: tier -> signed (forged, unsigned commit)", G,
         lambda r: (r.update(tier="signed", who="kEVIL", how="signed by an operator key"), r.pop("email", None)), blind),
        ("git: who -> Mallory", G, lambda r: r.update(who="Mallory"), blind),
        ("git: email -> evil", G, lambda r: r.update(email="evil@x"), blind),
        ("git: committer added", G, lambda r: r.update(committer="Evil <e@e>"), blind),
        ("trailer: who -> another launch", T, lambda r: r.update(who="agent coordinator ln-other"),
         {"no-launch-records", "author-unread", "other-repo"}),
        ("trailer: tier -> signed (forged, unsigned commit)", T,
         lambda r: r.update(tier="signed", who="kEVIL", how="signed by an operator key"), blind),
        ("document: how -> another path", D, lambda r: r.update(how="first added docs/other.md, asserted by git author"),
         {"other-repo"}),
        ("document: who -> Mallory", D, lambda r: r.update(who="Mallory"), blind),
        ("vacuous: committer added", G, lambda r: (r.clear(), r.update(_VACUOUS, committer="Evil <e@e>")), set()),
        ("vacuous: email added", G, lambda r: (r.clear(), r.update(_VACUOUS, email="evil@x")), set()),
        ("open item: a git close added, unheld sha", tw.cw.open,
         lambda r: r.update(tier="git", who="Op Person", how="asserted by git author", commit="ab" * 20, email="x@x"), set(CAPS)),   # fix 8: git-kind, repo-derived: unheld is undetermined
    ]


_DEGRADED = ({"kind": "unread", "why": "x"}, {"kind": "unchecked", "from_tier": "signed", "why": "x"},
             {"kind": "unchecked", "from_tier": "trailer", "why": "x"}, {"kind": "revoked", "from_tier": "signed", "key": "SHA256:x",
                                                                           "principal": "kEVIL", "revoked_seq": 1})


_DEGRADED_ROWS = {"none", "signed: who -> kEVIL", "git: tier -> signed (forged, unsigned commit)", "git: who -> Mallory",
                  "trailer: who -> another launch", "signed: commit -> unheld sha"}


@pytest.mark.parametrize("cap", CAPS)
def test_matrix_single_field_mutations_by_verifier_capability(cap, tw, capsys, monkeypatch, tmp_path):
    """Every row: rc 0 ONLY where the changed field's predicate depends on the capability this verifier lacks, a BREAK naming
    the close otherwise; and setting any ``degraded`` on the changed record (deleting it is the plain row) never changes the
    rc (the bundle never reduces scrutiny)."""
    import copy
    bundle = edit_bundle(exported(tw.cw, capsys), lambda d: d["closes"].pop(tw.cw.ghost))   # its open item: see the other-repo test
    base = json.loads(bundle.read_text("utf-8"))
    _apply(cap, tw, monkeypatch, tmp_path)
    work = tmp_path / "mutated.json"
    wrong = []
    for name, item_id, mutate, zero in _rows(tw, base["closes"]):
        variants = [None, *(_DEGRADED if name in _DEGRADED_ROWS else ())]
        for deg in variants:
            doc = copy.deepcopy(base)
            mutate(doc["closes"].setdefault(item_id, {}))
            if deg is not None:
                doc["closes"][item_id]["degraded"] = deg
            work.write_text(json.dumps(doc), "utf-8")
            rc, out, err = run(capsys, "verify", str(work))
            want = 0 if cap in zero else 3
            line = close_lines(out).get(item_id, "")
            if rc != want or (want == 3 and not line.startswith(f"BREAK close {item_id}: ")) or (want == 0 and "BREAK" in out):
                wrong.append(f"[{cap}] {name} degraded={deg}: rc {rc}, want {want}: {line}")
            if want == 0 and name != "none":
                assert "matches" not in line, (name, line)
    assert not wrong, "\n".join(wrong)


def test_a_key_revoked_in_this_install_is_undetermined_never_since_never_matches(tw, capsys):
    bundle = exported(tw.cw, capsys)
    who = json.loads(bundle.read_text("utf-8"))["closes"][tw.signed]["who"]
    tw.revoke()
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 0, out + err
    line = close_lines(out)[tw.signed]
    assert line == (f"close {tw.signed}: signed by an operator key {who} (recorded in the bundle, not re-derived here: signature by "
                    f"{who} verifies; that key is revoked in this install's authority log (seq 4))")
    assert "since" not in line and "BREAK" not in out


def test_a_revocation_never_covers_another_commit_or_another_signer(tw, capsys):
    bundle = exported(tw.cw, capsys)
    rec = json.loads(bundle.read_text("utf-8"))["closes"]
    tw.revoke()
    edit_bundle(bundle, lambda d: d["closes"][tw.signed].update(commit=rec[tw.cw.merged]["commit"]))
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 3, out + err
    line = close_lines(out)[tw.signed]
    assert line.startswith(f"BREAK close {tw.signed}: the bundle records [signed by an operator key") and "(refuted: commit: " in line
    edit_bundle(bundle, lambda d: d["closes"][tw.signed].update(commit=rec[tw.signed]["commit"], who="kSOMEONEELSE"))
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 3 and f"BREAK close {tw.signed}: the bundle records [signed by an operator key kSOMEONEELSE]" in out
    assert "signature: the commit is signed by k" in close_lines(out)[tw.signed]


def test_a_weaker_true_claim_holds_and_names_the_higher_tier_this_install_derives(tw, capsys):
    bundle = exported(tw.cw, capsys)
    rec = json.loads(bundle.read_text("utf-8"))["closes"][tw.signed]
    edit_bundle(bundle, lambda d: d["closes"].__setitem__(tw.signed, {"tier": "git", "who": "Op Person", "how": "asserted by git author",
                                                                    "commit": rec["commit"], "email": "x@x"}))
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 0, out + err
    assert close_lines(out)[tw.signed] == (f"close {tw.signed}: asserted by git author Op Person <x@x> (holds; this install also "
                                           f"derives signed by an operator key {rec['who']})")


def test_with_no_authority_log_a_signed_close_is_undetermined_on_the_standing_alone(tw, capsys, monkeypatch):
    bundle = exported(tw.cw, capsys)
    no_authority_log(monkeypatch)
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 0, out + err
    line = close_lines(out)[tw.signed]
    assert line.startswith(f"close {tw.signed}: signed by an operator key k")
    assert line.endswith("(recorded in the bundle, not re-derived here: the commit is signed but the authority log was not checked "
                         "(holds no cockpit.db))")
    assert close_lines(out)[tw.trailer].endswith("(re-derived from the repo; matches the bundle)")     # the trailer needs no log


def test_with_the_launch_records_unreadable_a_trailer_close_is_undetermined(tw, capsys, monkeypatch):
    bundle = exported(tw.cw, capsys)
    _no_launch_records(monkeypatch)
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 0, out + err
    line = close_lines(out)[tw.trailer]
    assert line.startswith(f"close {tw.trailer}: agent ") and "asserted (trailer)" in line
    assert line.endswith("(recorded in the bundle, not re-derived here: a Board-Seat trailer is present but the launch records were "
                         "not checked (the launch records cannot be read (OperationalError)))")


def test_a_trailer_that_maps_to_no_launch_record_here_is_undetermined_launch_records_are_install_scoped(tw, capsys, monkeypatch):
    from levain.desk import lease
    bundle = exported(tw.cw, capsys)
    monkeypatch.setattr(lease, "_launch_by_key", lambda conn, key: None)
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 0, out + err
    assert close_lines(out)[tw.trailer].endswith("(recorded in the bundle, not re-derived here: its Board-Seat trailer maps to no "
                                                 "launch record in this install's desk store)")


def test_another_repo_at_the_registered_path_is_undetermined_and_a_commitless_close_still_breaks(tw, tmp_path, capsys):
    bundle = exported(tw.cw, capsys)
    _other_repo(tw, tmp_path)
    rc, out, err = run(capsys, "verify", str(bundle))
    lines = close_lines(out)
    for item_id in (tw.signed, tw.trailer, tw.cw.merged, tw.cw.doc):
        assert f"(recorded in the bundle, not re-derived here: the repo at {tw.cw.repo} does not hold commit " in lines[item_id], lines[item_id]
    # a recorded close with NO commit (the unattributed ghost) has nothing to look for: it keeps the break (head ruling, open)
    assert rc == 3 and lines[tw.cw.ghost].startswith(f"BREAK close {tw.cw.ghost}: ") and out.count("BREAK") == 1
    edit_bundle(bundle, lambda d: d["closes"].pop(tw.cw.ghost))
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 0, out + err
    assert "BREAK" not in out and "matches" not in out


def test_a_repo_that_holds_the_recorded_commit_and_does_not_show_the_close_is_a_break(tw, capsys):
    bundle = exported(tw.cw, capsys)
    edit_bundle(bundle, lambda d: d["closes"].__setitem__(tw.cw.open, d["closes"][tw.signed]))
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 3 and f"BREAK close {tw.cw.open}: " in out and "the repo does not show the item closed" in out


def test_the_bundle_s_note_is_labelled_as_the_bundle_s_and_never_compared(tw, capsys, monkeypatch):
    bundle = edit_bundle(exported(tw.cw, capsys), lambda d: d["closes"][tw.signed].update(note="trust me"))
    no_authority_log(monkeypatch)
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 0, out + err
    assert "(the bundle's note: trust me) (recorded in the bundle, not re-derived here: " in close_lines(out)[tw.signed]


def test_a_recorded_close_whose_exporter_could_not_read_the_author_claims_nothing_and_prints_this_install_s(cw, capsys):
    bundle = edit_bundle(exported(cw, capsys), lambda d: d["closes"].__setitem__(
        cw.merged, {"tier": "unattributed", "who": None, "how": "the author could not be read (git went away)", "commit": None}))
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 0, out + err
    assert close_lines(out)[cw.merged] == (f"close {cw.merged}: the author could not be read (git went away) (the bundle records no "
                                           f"author; this install derives asserted by git author Plain Person <x@x>)")


# ---------------------------------------------- the four L3 r1 scenarios that reached rc 0 on c28b0188c: each is a BREAK now
def test_l3_forged_signed_on_an_unsigned_commit_with_an_unchecked_trailer_is_a_break(tw, capsys, monkeypatch):
    bundle = edit_bundle(exported(tw.cw, capsys), lambda d: d["closes"][tw.trailer].update(
        tier="signed", who="kEVIL", how="signed by an operator key"))
    _no_launch_records(monkeypatch)
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 3, out + err
    assert "signature: the commit carries no signature" in close_lines(out)[tw.trailer]


def test_l3_an_altered_git_author_under_an_exporter_unchecked_marker_is_a_break(tw, capsys, monkeypatch):
    no_authority_log(monkeypatch)
    bundle = exported(tw.cw, capsys)
    monkeypatch.setattr(sg, "resolve_authority", _REAL_RESOLVE)
    edit_bundle(bundle, lambda d: d["closes"][tw.signed].update(
        tier="git", who="Mallory", how="asserted by git author",
        degraded={"kind": "unchecked", "from_tier": "signed", "why": "the commit is signed but the authority log was not checked"}))
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 3, out + err
    assert "git author: the commit's git author is Op Person" in close_lines(out)[tw.signed]


def test_l3_an_altered_record_under_an_unread_author_is_a_break(tw, capsys, monkeypatch):
    bundle = exported(tw.cw, capsys)
    other = json.loads(bundle.read_text("utf-8"))["closes"][tw.cw.merged]["commit"]
    edit_bundle(bundle, lambda d: d["closes"][tw.signed].update(commit=other, who="kEVIL"))
    _author_unread(monkeypatch)
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 3, out + err
    assert close_lines(out)[tw.signed].startswith(f"BREAK close {tw.signed}: ") and "commit: the board state here names" in out


def test_l3_a_swapped_commit_the_repo_does_not_hold_while_it_holds_another_close_is_a_break(tw, capsys):
    bundle = edit_bundle(exported(tw.cw, capsys), lambda d: d["closes"][tw.signed].update(commit="ab" * 20))
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 3, out + err
    assert "commit: the repo derives the close from commit " in close_lines(out)[tw.signed]


def test_a_close_for_an_id_the_bundles_own_records_do_not_hold_is_a_break(cw, capsys):
    bundle = exported(cw, capsys)
    edit_bundle(bundle, lambda d: d["closes"].__setitem__("zeta#9", {"tier": "signed", "who": "kEVIL", "how": "signed by an operator key",
                                                                    "commit": None}))
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 3, out + err
    assert "BREAK close zeta#9: the bundle records a close for zeta#9, which its own records do not hold" in out
    assert "signed by an operator key kEVIL" not in out
    os.rename(cw.repo, str(cw.repo) + ".away")                  # with no repo either: the bundle contradicts itself all the same
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 3 and "kEVIL" not in out


def test_bundle_verify_prints_a_band_close_as_live_verify_does(cw, capsys, monkeypatch):
    bundle = exported(cw, capsys)
    real = bt.derive_closes

    def with_band(trail, *a, **k):
        closes, bands, read, why = real(trail, *a, **k)
        return closes, {**bands, cw.open: "no git close to read an author from"}, read, why
    monkeypatch.setattr(bt, "derive_closes", with_band)
    live = close_lines(run(capsys, "verify")[1])
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 0, out + err
    assert close_lines(out)[cw.open] == live[cw.open] == f"close {cw.open}: no git close to read an author from"


def test_an_old_bundle_s_close_lines_are_labelled_this_install_s_derivation(cw, capsys):
    bundle = edit_bundle(exported(cw, capsys), lambda d: d.pop("closes"))
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 0 and bt.CLOSES_NONE in out
    lines = close_lines(out)
    assert set(lines) == {cw.merged, cw.doc, cw.ghost}
    assert all(x.endswith("(derived by this install from the repo now; the bundle records none)") for x in lines.values())
    assert "re-derived" not in out


def test_matches_carries_the_bundle_s_note_when_it_differs(cw, capsys):
    bundle = edit_bundle(exported(cw, capsys), lambda d: d["closes"][cw.merged].update(note="the exporter's caveat"))
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 0, out + err
    assert close_lines(out)[cw.merged].endswith("(re-derived from the repo; matches the bundle; the bundle's note: the exporter's caveat)")
    assert "the bundle's note" not in close_lines(out)[cw.doc]


def test_an_unread_author_at_verify_is_recorded_not_re_derived_here(cw, capsys, monkeypatch):
    from levain.desk import board_author as ba
    bundle = exported(cw, capsys)
    real = ba.closer_of_item

    def flaky(repo, item, *a, **k):
        if item.id == cw.merged:
            raise ba.be.GitFailed("git went away")
        return real(repo, item, *a, **k)
    monkeypatch.setattr(ba, "closer_of_item", flaky)
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 0, out + err
    assert close_lines(out)[cw.merged] == (f"close {cw.merged}: asserted by git author Plain Person <x@x> (recorded in the bundle, "
                                           "not re-derived here: the author could not be read (git went away))")


# ---------------------------------------------------------------- P6 fix 7 (levain#104): bound fixes from L3 round 2
def test_fix7_a_close_for_an_item_the_records_show_open_or_band_closed_is_a_break_on_any_capability(cw, capsys):
    """Ruling A: with a held sha, an unheld sha, and with the repo gone; and a band-kind item (no git close) with the repo gone."""
    band = bs.add(cw.target, "alpha", "band one", "desk 1", "desk 1", done="np-q1", done_kind="band", author=OPD, now=NOW)[0]
    bundle = exported(cw, capsys)
    base = json.loads(bundle.read_text("utf-8"))["closes"][cw.merged]
    for item_id, sha in ((band, "ab" * 20), (band, base["commit"])):         # fix 8: a git-kind open item is repo-derived, not here
        edit_bundle(bundle, lambda d: d["closes"].__setitem__(item_id, {**base, "commit": sha}))
        rc, out, err = run(capsys, "verify", str(bundle))
        assert rc == 3, (item_id, sha, out)
        assert f"BREAK close {item_id}: the bundle records a git close for {item_id}, which its own records show " in out
    os.rename(cw.repo, str(cw.repo) + ".away")            # no repo at all: the band item still breaks (its kind is in the fold)
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 3 and f"BREAK close {band}: the bundle records a git close for {band}, which its own records show " in out


def test_fix7_vacuous_is_only_the_exact_unread_shape(cw, capsys):
    from levain.desk import board_author as ba
    assert bt._vacuous(ba._unread(Exception("boom")).to_dict())
    for extra in ({"committer": "Evil <e@e>"}, {"email": "evil@x"}, {"close_commit": None}, {"note": "n"}):
        assert not bt._vacuous({**ba._unread(Exception("boom")).to_dict(), **extra}), extra
    bundle = exported(cw, capsys)
    for extra in ({"committer": "Evil <e@e>"}, {"email": "evil@x"}):
        edit_bundle(bundle, lambda d: d["closes"].__setitem__(cw.merged, {**_VACUOUS, **extra}))
        rc, out, err = run(capsys, "verify", str(bundle))
        assert rc == 3 and close_lines(out)[cw.merged].startswith(f"BREAK close {cw.merged}: "), out


def test_fix7_record_shape_is_refuted_before_any_blind_path(tw, capsys, monkeypatch, tmp_path):
    bundle = exported(tw.cw, capsys)
    _author_unread(monkeypatch)
    for item_id, change in ((tw.signed, {"email": "x@x"}),                              # email on a signed close
                            (tw.signed, {"how": "signed by the operator"}),             # how off the tier's template
                            (tw.cw.doc, {"how": "asserted by git author"}),             # a document close without its prefix
                            (tw.trailer, {"who": "Mallory"}),                           # a trailer close naming no agent
                            (tw.cw.merged, {"tier": "unattributed", "who": "Mallory", "how": "x"})):   # unattributed naming someone
        work = tmp_path / "shape.json"
        doc = json.loads(bundle.read_text("utf-8"))
        doc["closes"][item_id].update(change)
        work.write_text(json.dumps(doc), "utf-8")
        rc, out, err = run(capsys, "verify", str(work))
        assert rc == 3 and close_lines(out)[item_id].startswith(f"BREAK close {item_id}: "), (item_id, change, out)


def test_fix7_a_malformed_or_empty_fingerprint_is_a_signature_that_cannot_be_judged(tw, capsys, monkeypatch):
    import types

    from levain.desk import board_author as ba
    from levain.team import signing
    bundle = exported(tw.cw, capsys)
    sha = json.loads(bundle.read_text("utf-8"))["closes"][tw.signed]["commit"]
    for fp in ("", 12345):
        monkeypatch.setattr(signing, "verify_commit", lambda repo, s, fp=fp: types.SimpleNamespace(kind="signed", fingerprint=fp, reason=""))
        rd = ba._Reader(tw.cw.repo, ba.Resolvers())
        principal, notes, facts = rd._signature(sha, rd.facts(sha))
        assert principal is None and facts.state == "unjudged" and "could not be checked" in notes[0]
        rc, out, err = run(capsys, "verify", str(bundle))
        assert rc == 0, out + err
        assert "its fingerprint cannot be read" in close_lines(out)[tw.signed]


# ---------------------------------------------------------------- P6 fix 8 (levain#104): L3 round 3's delete and bound fixes
def test_fix8_a_stale_clone_of_the_right_repo_never_reads_a_git_kind_item_as_open_in_its_own_records(cw, capsys):
    """Deleting fix 7's 'this repo derives other closes' proxy (RED on 227678b19: BREAK "its own records show open"). The repo
    derives other closes and lacks this item's close commit: UNDETERMINED, rc 0."""
    stale = bs.add(cw.target, "alpha", "stale one", "desk 1", "desk 1", done="docs/stale.md", done_kind="document", author=OPD, now=NOW)[0]
    bundle = exported(cw, capsys)
    base = json.loads(bundle.read_text("utf-8"))["closes"][cw.doc]
    edit_bundle(bundle, lambda d: d["closes"].__setitem__(stale, {**base, "commit": "ab" * 20}))
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 0, out + err
    line = close_lines(out)[stale]
    assert not line.startswith("BREAK") and "its own records show" not in out
    assert "does not hold commit abababababab" in line


def test_fix8_a_recorded_close_the_repo_does_not_show_with_its_commit_held_is_still_a_break(cw, capsys):
    stale = bs.add(cw.target, "alpha", "stale two", "desk 1", "desk 1", done="docs/stale2.md", done_kind="document", author=OPD, now=NOW)[0]
    bundle = exported(cw, capsys)
    base = json.loads(bundle.read_text("utf-8"))["closes"][cw.doc]            # base["commit"] is a commit this repo holds
    edit_bundle(bundle, lambda d: d["closes"].__setitem__(stale, dict(base)))
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 3 and close_lines(out)[stale].startswith(f"BREAK close {stale}: ") and "the repo does not show the item closed" in out


def test_fix8_the_repo_unavailable_path_refutes_a_record_shape_violation(tw, capsys):
    bundle = exported(tw.cw, capsys)
    os.rename(tw.cw.repo, str(tw.cw.repo) + ".away")
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 0, out + err                                                   # unaltered: recorded, not re-derived
    assert "repo not available" in close_lines(out)[tw.signed]
    for change in ({"email": "x@x"}, {"how": "signed by the operator"}, {"email": None}):
        edit_bundle(bundle, lambda d: d["closes"][tw.signed].update(change))
        rc, out, err = run(capsys, "verify", str(bundle))
        assert rc == 3, (change, out)
        line = close_lines(out)[tw.signed]
        assert line.startswith(f"BREAK close {tw.signed}: ") and "repo is not available" in line
        assert ("email:" in line) == ("email" in change) and ("how:" in line) == ("how" in change)
        edit_bundle(bundle, lambda d: [d["closes"][tw.signed].pop(k, None) for k in ("email",)] and d["closes"][tw.signed].update(
            how="signed by an operator key"))


def test_fix8_a_null_email_key_is_a_shape_violation_under_full_capability(tw, capsys):
    from levain.desk import board_author as ba
    assert "email" not in ba.Closer("signed", "k", "signed by an operator key").to_dict()       # this install never emits a null key
    bundle = edit_bundle(exported(tw.cw, capsys), lambda d: d["closes"][tw.signed].update(email=None))
    rc, out, err = run(capsys, "verify", str(bundle))
    assert rc == 3 and "email: a signed close carries no email" in close_lines(out)[tw.signed]


def test_fix8_one_predicate_name_is_printed_once():
    from levain.desk.board_trail import _Pred, _joined
    got = _joined([_Pred("how", "refuted", "first"), _Pred("who", "refuted", "w"), _Pred("how", "refuted", "second"),
                   _Pred("x", "holds", "")])
    assert got == "how: first; who: w"
