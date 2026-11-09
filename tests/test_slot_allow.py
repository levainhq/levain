"""Row 165: a slot's "don't ask again" reaches future slots only through the operator's own answer."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path

import pytest

from levain.consent.questions import QuestionStore
from levain.slot import slot_allow as SA
from levain.slot import supervisor as sup
from levain.slot.render import Frozen


def local(*rules, **extra):
    return json.dumps({"permissions": {"allow": list(rules)}, **extra}).encode()


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


def test_only_dialog_shapes_are_offered_and_the_rest_named():
    kept, dropped = SA.parse_local_allow(local(
        "Bash(npm test *)", "WebFetch(domain:example.com)", "WebSearch", "Bash(*)", "Bash", "Read(./x)",
        "Bash(a\nb)", "Bash(npm test *)"))
    assert kept == ["Bash(npm test *)", "WebFetch(domain:example.com)", "WebSearch"]
    assert dropped == ["Bash(*)", "Bash", "Read(./x)", "Bash(a\nb)"]


def test_a_duplicate_key_or_wrong_shape_is_refused_whole():
    with pytest.raises(SA.AllowRefused):
        SA.parse_local_allow(b'{"permissions": {"allow": ["WebSearch"]}, "permissions": {}}')
    with pytest.raises(SA.AllowRefused):
        SA.parse_local_allow(b'{"permissions": {"allow": "WebSearch"}}')
    with pytest.raises(SA.AllowRefused):
        SA.parse_local_allow(b"[]")


def test_a_symlinked_or_oversized_file_is_refused(tmp_path):
    real = tmp_path / "real.json"
    real.write_bytes(local("WebSearch"))
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(SA.AllowRefused, match="symlink"):
        SA.read_regular(link)
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (SA.SETTINGS_CAP + 1))
    with pytest.raises(SA.AllowRefused, match="over"):
        SA.read_regular(big)
    assert SA.read_regular(tmp_path / "absent.json") is None
    assert SA.read_regular(real) == real.read_bytes()


def test_an_unsigned_rule_never_reaches_a_launch_and_a_yes_does(qdir, tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / ".claude").mkdir(parents=True)
    (checkout / ".claude" / "settings.json").write_text('{"permissions": {"allow": ["Bash(make)"]}}')
    before = tree_hash(checkout)
    allow = qdir / SA.FILE_NAME
    qs = QuestionStore(qdir)
    filed = SA.file_rows(qdir, "S1", ["Bash(npm test *)", "WebSearch"], store=qs)
    assert filed == ["Bash(npm test *)", "WebSearch"]
    # filed and unanswered: the launch carries nothing, and a second filing is not a second row
    assert SA.apply_answered(qdir, allow, store=qs) == []
    assert SA.file_rows(qdir, "S2", ["Bash(npm test *)"], store=qs) == []
    assert len(qs.open_questions()) == 2

    yes = next(q for q in qs.open_questions() if "npm test" in q.what)
    no = next(q for q in qs.open_questions() if "WebSearch" in q.what)
    qs.answer(yes.id, option="yes", channel="cli")
    qs.answer(no.id, option="no", channel="cli")
    assert SA.apply_answered(qdir, allow, store=qs) == ["Bash(npm test *)"]
    assert SA.rules_of(allow) == ["Bash(npm test *)"]
    assert SA.load(allow)["rules"][0]["session"] == "S1"
    # a no is not refiled while it stands, and the checkout is byte-identical
    assert SA.file_rows(qdir, "S3", ["WebSearch"], store=qs) == []
    assert tree_hash(checkout) == before


def test_the_launch_merges_the_rules_into_permissions_allow_only(qdir):
    allow = qdir / SA.FILE_NAME
    SA.append(allow, "Bash(make *)", "S1")
    seams = sup.Seams(host="linux", helper=("x",), client_factory=lambda p: None, control_socket=Path("/x"),
                      store=Path("/x"), base_map=Path("/x"), allow_dir=qdir)
    said = []
    s = sup.Supervisor.__new__(sup.Supervisor)
    s.s = seams
    s.say = said.append
    fz = Frozen({"hooks": {"Stop": []}, "permissions": {"allow": ["Bash(make)"], "deny": ["Read(./.env)"]}}, (), ())
    out = s._with_slot_allow(fz)
    assert out.settings["permissions"] == {"allow": ["Bash(make)", "Bash(make *)"], "deny": ["Read(./.env)"]}
    assert out.settings["hooks"] == {"Stop": []}
    assert str(allow) in said[0]
    # off when no directory is named
    s.s = dataclasses.replace(seams, allow_dir=None)
    assert s._with_slot_allow(fz) is fz


def test_a_damaged_store_is_said_and_the_launch_goes_on(qdir):
    (qdir / SA.FILE_NAME).write_text("not json")
    seams = sup.Seams(host="linux", helper=("x",), client_factory=lambda p: None, control_socket=Path("/x"),
                      store=Path("/x"), base_map=Path("/x"), allow_dir=qdir)
    said = []
    s = sup.Supervisor.__new__(sup.Supervisor)
    s.s, s.say = seams, said.append
    fz = Frozen({}, (), ())
    assert s._with_slot_allow(fz) is fz
    assert "not read" in said[0]


def test_end_files_rows_from_the_retained_file_and_names_the_dropped(qdir):
    seams = sup.Seams(host="linux", helper=("x",), client_factory=lambda p: None, control_socket=Path("/x"),
                      store=Path("/x"), base_map=Path("/x"), allow_dir=qdir,
                      retained_allow=lambda session: local("WebSearch", "Read(./x)"))
    said = []
    s = sup.Supervisor.__new__(sup.Supervisor)
    s.s, s.say = seams, said.append
    s._file_allow_rows("S9")
    assert len(QuestionStore(qdir).open_questions()) == 1
    assert any("on your band" in m for m in said) and any("Read(./x)" in m for m in said)
    assert not (qdir / SA.FILE_NAME).exists()          # filing writes no rule


def test_revoke_needs_confirm_and_undo_restores(qdir):
    allow = qdir / SA.FILE_NAME
    SA.append(allow, "WebSearch", "S1")
    assert "re-run with --yes" in SA.revoke(allow, "WebSearch", confirm=False)
    assert SA.rules_of(allow) == ["WebSearch"]
    SA.revoke(allow, "WebSearch", confirm=True)
    assert SA.rules_of(allow) == []
    SA.undo(allow, "WebSearch")
    assert SA.rules_of(allow) == ["WebSearch"]
    with pytest.raises(SA.AllowRefused):
        SA.revoke(allow, "WebFetch(domain:x.com)", confirm=True)
