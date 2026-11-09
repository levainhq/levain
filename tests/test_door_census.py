"""The coordinator's door is derived from the verbs, and every verb declares its side of it (1101-1, Diogenes
5bfdf21a99fe: `coord ready add|move|correct` was documented as the coordinator's write but missing from a hand-kept list)."""
import argparse

from levain.cli import build_parser, main
from levain.coordinator import door

_TREES = ("coord", "drops", "thread", "team")

_EXPECTED = frozenset({
    ("coord", "status"), ("coord", "claim"), ("coord", "beat"), ("coord", "release"),
    ("coord", "questions"), ("coord", "answer"), ("coord", "escalate"),
    ("coord", "away"), ("coord", "budget"), ("coord", "morning"),
    ("coord", "reply"), ("coord", "spawn"),
    ("coord", "report"), ("coord", "import-status"), ("coord", "reports"),
    ("coord", "handoff", "worker"), ("coord", "log"),
    ("coord", "open"), ("coord", "close"), ("coord", "handoff", "show"),
    ("coord", "ready", "show"), ("coord", "ready", "add"), ("coord", "ready", "move"), ("coord", "ready", "correct"),
    ("drops", "list"), ("drops", "show"), ("drops", "split"), ("drops", "note"),
    ("thread", "list"), ("thread", "show"), ("thread", "state"), ("thread", "merge"), ("thread", "detach"),
    ("thread", "add"), ("thread", "set"),
    ("team", "draft", "add"), ("team", "draft", "list"), ("team", "draft", "show"),
    ("team", "status"), ("team", "verify"), ("team", "export"), ("team", "doctor"),
})


def test_every_verb_of_the_coordinators_trees_declares_its_door():
    undeclared = sorted(" ".join(p) for p, mark in door.declared_leaves(build_parser()).items()
                        if p[0] in _TREES and mark is None)
    assert not undeclared, f"declare door_ok(...) or not_door(...) where these are defined: {undeclared}"


def test_no_verb_outside_the_coordinators_trees_is_open():
    outside = sorted(" ".join(p) for p, mark in door.declared_leaves(build_parser()).items()
                     if p[0] not in _TREES and mark is True)
    assert not outside, f"a verb of another tree is marked door_ok: {outside}"


def test_the_derived_door_is_exactly_the_expected_set():
    assert door.door_paths(build_parser()) == _EXPECTED


def _workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    ws = tmp_path / "coordinator"
    (ws / ".levain").mkdir(parents=True)
    (ws / ".levain" / "config.json").write_text('{"role": "coordinator"}\n')
    door.record(ws)
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    monkeypatch.chdir(ws)
    return q


def test_coord_ready_add_passes_the_door_and_a_not_door_verb_is_refused(tmp_path, monkeypatch, capsys):
    q = _workspace(tmp_path, monkeypatch)
    main(["coord", "ready", "add", "it-1", "--repo", "levain", "--title", "t", "--who", "c", "--why", "w",
          "--queue-dir", str(q)])                     # no --token: the lease fence refuses it, not the door
    err = capsys.readouterr().err
    assert "is the operator's" not in err and "levain coord ready add:" in err, err
    assert main(["coord", "take", "--queue-dir", str(q)]) == 1
    assert "is the operator's, and this is the coordinator's workspace" in capsys.readouterr().err


def test_a_leaf_with_no_declaration_is_refused(tmp_path, monkeypatch):
    _workspace(tmp_path, monkeypatch)
    root = argparse.ArgumentParser(prog="levain")
    sub = root.add_subparsers(dest="command", required=True)
    for name, mark in (("open", door.door_ok), ("shut", door.not_door), ("bare", None)):
        leaf = sub.add_parser(name)
        if mark:
            mark(leaf)
    assert door.refusal(root, root.parse_args(["open"])) is None
    for name in ("shut", "bare"):
        assert "is the operator's" in door.refusal(root, root.parse_args([name]))


def test_the_door_is_declared_on_a_leaf_never_on_a_group():
    import pytest

    root = argparse.ArgumentParser(prog="levain")
    group = root.add_subparsers(dest="command").add_parser("tree")
    group.add_subparsers(dest="verb").add_parser("leaf")
    for declare in (door.door_ok, door.not_door):
        with pytest.raises(ValueError):
            declare(group)


def test_no_group_of_verbs_carries_a_door_default():
    """A default on a group reaches every leaf below it that declares nothing, whether it was set before or after the
    group's subcommands were added (door_ok refuses a group, but a parser built with parents=[...] or a direct
    set_defaults would not pass through it)."""
    def walk(parser, path=()):
        subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        for sub in subs:
            assert parser.get_default(door.DOOR_DEST) is None, f"group {' '.join(path) or 'levain'} carries a door default"
            for name, child in sub.choices.items():
                walk(child, (*path, name))
    walk(build_parser())
