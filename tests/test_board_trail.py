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


def answer_sig(rig: Rig, np_id: str, *, tamper: bool = False, other: str | None = None) -> dict:
    """A real answer signature over a terminal-answer message naming ``np_id`` (``other``: names that item instead)."""
    msg = ts.build_message(store="levain-questions:abc", install="abc", session="s1", view="1", typed="yes", nonce="0123456789abcdef",
                           at=datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc),
                           entries=[ts.Entry(other or np_id, "rv-1", "Cut it?", "information", answer="(a) yes")])
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
    assert "relayed record(s) are anchored to his words by the band row's signature" in r["repos.jsonl"].relayed


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
    assert "items.jsonl: NOT ANCHORED: the file breaks at line 4, at or before the last signed record (line 4)" in out


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
    world.band.signs.clear()                            # the row's grade still CLAIMS signed-phone; there is nothing to verify
    r = _relay_report(world)
    assert r.ok and r.verdict == f"agent desk 24, relaying {Q1} (unsigned answer)"
    assert run(capsys, "verify")[0] == 0


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


def test_close_verdicts_is_the_part_b_seam_and_prints_nothing_yet(world):
    t = trail_of(world)
    assert bt.close_verdicts(t, bt.verify_trail(t)) == []
