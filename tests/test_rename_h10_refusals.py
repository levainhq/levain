"""Ruling H10 (spore -> intention rename): a retired spelling arriving as a NEW request is refused loudly with a pointer.

Three surfaces: the CLI (`levain spores ...`), the cockpit's wire (a verb id, an edit kind or a row id a page loaded before
the update still posts), and the keep-migrate plan file an older release wrote (history: it must still apply and revert).
The old release's data is read from its own tree's output (`tests/fixtures/keep_migrate_old_release/`, built by the code at
a8617e55c), never from a spelling this file retypes.
"""
from __future__ import annotations

import json
import re
import shlex
import shutil
from pathlib import Path
from typing import Any

import pytest

from levain import intention_legacy as legacy
from levain.cli import build_parser, main
from levain.cockpit.verbs import dispatch
from levain.writes import EditError

from tests.test_cockpit_k2a import CRED, INSTALL_CLASS, Rig
from tests.test_cockpit_sign_routes import _call, cockpit  # noqa: F401  (the fixture)

FIXTURES = Path(__file__).parent / "fixtures" / "keep_migrate_old_release"


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    try:
        code = main(argv)
    except SystemExit as exc:
        code = int(exc.code) if isinstance(exc.code, int) else 1
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def _pointer(err: str) -> list[str]:
    """The command line the refusal teaches: the one backticked span in its message, as argv."""
    spans = re.findall(r"`([^`]+)`", err)
    assert len(spans) == 1, err
    return shlex.split(spans[0])


# ---------------------------------------------------------------------------- the CLI

RETIRED_ARGV = [
    ["spores", "germinate", "--path", "/an/install", "--dry-run"],
    ["spores", "keep-migrate", "--apply", "plan.json"],
    ["spores", "keep-migrate"],
]


@pytest.mark.parametrize("argv", RETIRED_ARGV, ids=lambda a: " ".join(a[:2]))
def test_retired_cli_refuses_with_exit_2_and_the_same_line_under_intentions(argv, capsys):
    code, out, err = _run(argv, capsys)
    assert code == 2, err
    assert out == ""
    taught = _pointer(err)
    assert taught == ["levain", "intentions", *argv[1:]]


@pytest.mark.parametrize("argv", RETIRED_ARGV, ids=lambda a: " ".join(a[:2]))
def test_the_pointer_is_a_command_the_real_parser_accepts(argv, capsys):
    _code, _out, err = _run(argv, capsys)
    taught = _pointer(err)[1:]
    ns = build_parser().parse_args(taught)
    assert ns.command == "intentions" and ns.intentions_cmd == argv[1]
    assert callable(ns.func)


def test_a_bare_or_unknown_retired_command_names_every_verb_the_new_command_has(capsys):
    verbs = set(build_parser()._subparsers._group_actions[0].choices["intentions"]._levain_verbs.choices)
    assert verbs                                    # derived from the live parser, never retyped here
    for argv in (["spores"], ["spores", "bogus"], ["spores", "-h"]):
        code, _out, err = _run(argv, capsys)
        assert code == 2, (argv, err)
        assert "levain intentions" in err
        assert all(v in err for v in verbs), (argv, err)


def test_the_refusal_writes_nothing_and_the_old_command_is_not_taught_in_help(tmp_path, capsys):
    target = tmp_path / "install"
    code, _out, _err = _run(["spores", "germinate", "--path", str(target)], capsys)
    assert code == 2 and not target.exists()
    assert "spores" not in build_parser().format_help()


def test_the_parser_itself_no_longer_has_the_command(capsys):
    """A doc or hook that teaches the old line fails its parse (the H12 check relies on it); `main` alone answers it."""
    with pytest.raises(SystemExit) as ei:
        build_parser().parse_args([legacy.OLD_CLI_COMMAND, "germinate"])
    assert ei.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_the_refusal_comes_before_the_desks_door(monkeypatch, tmp_path, capsys):
    """Inside the desk's workspace an old hook gets the pointer, not the door's "this is the operator's"."""
    import levain.desk.door as door

    monkeypatch.setattr(door, "workspace", lambda: tmp_path)
    code, _out, err = _run(["spores", "germinate", "--path", str(tmp_path)], capsys)
    assert code == 2 and "levain intentions germinate" in err


# ------------------------------------------------------------------ keep-migrate, an OLD plan


@pytest.fixture()
def old_install(tmp_path: Path) -> tuple[Path, Path]:
    """An install an a8617e55c release left behind: its store (spore-NNN ids, a note, a handoff, an undated agenda) and the
    plan its `keep-migrate` wrote, pointed at this temp install."""
    from anneal_memory import Store

    lv = tmp_path / ".levain"
    (lv / "migrations").mkdir(parents=True)
    shutil.copy(FIXTURES / "memory.spores.json", lv / "memory.spores.json")
    with Store(str(lv / "memory.db")):
        pass
    plan = json.loads((FIXTURES / "plan.json").read_text())
    plan[legacy.KEEP_PLAN_STORE_KEY] = str(lv / "memory.spores.json")
    plan["episodic_db"] = str(lv / "memory.db")
    out = lv / "migrations" / "keep-old.json"
    out.write_text(json.dumps(plan, indent=2))
    return tmp_path, out


def _open_ids(root: Path) -> list[str]:
    return [i for i, _t, _d in _open_rows(root)]


def _open_rows(root: Path) -> list[tuple[str, str, str]]:
    from anneal_memory.spores import IntentionStore

    return sorted((r["id"], r["text"], r.get("disposition")) for r in IntentionStore(
        root / ".levain" / "memory.spores.json", id_prefix="intention").list_open())


def _episode_meta(root: Path) -> list[dict[str, Any]]:
    from anneal_memory import Store
    from levain import keep_migrate as km

    with Store(str(root / ".levain" / "memory.db")) as st:
        return [dict(e.metadata or {}) for e in st.recall(source=km.EPISODE_SOURCE, limit=50).episodes]


def test_an_old_plan_applies_and_reverts_under_the_new_command(old_install, capsys):
    root, plan = old_install
    originals = _open_rows(root)
    before = _open_ids(root)
    assert before == ["spore-001", "spore-002", "spore-003"]

    code, out, err = _run(["intentions", "keep-migrate", "--path", str(root), "--apply", str(plan)], capsys)
    assert code == 0, err
    assert "applied: 3 items" in out
    doc = json.loads(plan.read_text())
    assert [i["state"] for i in doc["items"]] == ["applied"] * 3
    assert not set(before) & set(_open_ids(root))               # the originals are composted ...
    assert len(_open_ids(root)) == 2                            # ... and the handoff and the agenda re-planted
    meta = _episode_meta(root)
    assert [m.get(legacy.KEEP_EPISODE_META_KEY) for m in meta] == ["spore-001"]   # the episode names its origin item

    code, out, err = _run(["intentions", "keep-migrate", "--path", str(root), "--revert", str(plan)], capsys)
    assert code == 0, err
    assert "reverted: 3 items" in out
    # the originals are back (revert re-adds each under a fresh id, the same text and disposition), the destinations gone
    assert sorted((t, d) for _i, t, d in _open_rows(root)) == sorted(
        (t, d) for _i, t, d in originals)


def test_a_new_plan_keeps_the_persisted_keys_of_the_old_one(tmp_path, capsys):
    from anneal_memory import Store
    from anneal_memory.spores import IntentionStore

    lv = tmp_path / ".levain"
    lv.mkdir()
    with Store(str(lv / "memory.db")):
        pass
    s = IntentionStore(lv / "memory.spores.json", id_prefix="intention")
    s.add(type="thought", text="gotcha: never run `foo --all` on the prod box", disposition="note")
    s.add(type="task", text="pick up the Liz thread", disposition="handoff")
    code, _out, err = _run(["intentions", "keep-migrate", "--path", str(tmp_path)], capsys)
    assert code == 0, err
    new = json.loads(next((lv / "migrations").glob("keep-*.json")).read_text())
    old = json.loads((FIXTURES / "plan.json").read_text())
    assert set(new) == set(old)
    assert set(new["items"][0]) == set(old["items"][0])
    assert {i["destination"]["kind"] for i in new["items"]} <= {i["destination"]["kind"] for i in old["items"]}
    assert legacy.KEEP_PLAN_STORE_KEY in new and legacy.KEEP_PLAN_ID_KEY in new["items"][0]
    assert legacy.KEEP_PLAN_DEST_KIND in {i["destination"]["kind"] for i in new["items"]}


# ------------------------------------------------------------------ the wire, a stale page


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def _refusal(route: str, rig: Rig, req: dict[str, Any]) -> EditError:
    with pytest.raises(EditError) as ei:
        dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, req=req, credential=CRED,
                 install_class=INSTALL_CLASS, route=route)             # type: ignore[arg-type]
    return ei.value


def _readable(err: EditError, new: str) -> None:
    low = str(err).lower()
    assert "reload" in low and new in str(err), str(err)


@pytest.mark.parametrize("old,new", sorted(legacy.OLD_VERB_TO_NEW.items()))
def test_a_stale_pages_retired_verb_id_is_refused_naming_the_new_one(rig, old, new):
    err = _refusal("cockpit", rig, {"verb": old, "panel_id": "tray", "params": {}})
    assert (err.code, err.http_status) == ("unknown_verb", 404)         # the generic path's code and status ...
    _readable(err, new)                                                  # ... with a message the page can show


def test_an_unknown_verb_that_was_never_a_spore_verb_keeps_the_generic_message(rig):
    err = _refusal("cockpit", rig, {"verb": "nope", "panel_id": "tray", "params": {}})
    assert (err.code, err.http_status) == ("unknown_verb", 404)
    assert "reload" not in str(err).lower()


@pytest.mark.parametrize("old,new", sorted(legacy.OLD_VERB_TO_NEW.items()))
def test_a_retired_verb_id_on_the_action_route_is_refused_the_same_way(rig, old, new):
    err = _refusal("action", rig, {"verb": old, "params": {}})
    assert (err.code, err.http_status) == ("unknown_verb", 404)
    _readable(err, new)


@pytest.mark.parametrize("old,new", sorted(legacy.OLD_KIND_TO_NEW.items()))
def test_a_retired_edit_kind_is_refused_naming_the_new_one(rig, old, new):
    err = _refusal("edit", rig, {"kind": old, legacy.OLD_ROW_ID_KEY: "spore-001"})
    assert (err.code, err.http_status) == ("bad_kind", 400)
    _readable(err, new)


def test_an_old_row_id_with_a_current_verb_is_refused_naming_the_new_row_id(rig):
    old_row = f"{legacy.OLD_ROW_PREFIX}{rig.ids['seed']}"
    err = _refusal("cockpit", rig, {"verb": "intention_touch", "panel_id": "tray", "params": {},
                                    "row_id": old_row, "row_version": "x"})
    assert (err.code, err.http_status) == ("stale", 409)            # what a row that is gone answers
    _readable(err, f"intention:{rig.ids['seed']}")


def test_an_old_edit_body_key_is_refused_naming_the_new_key(rig):
    err = _refusal("edit", rig, {"kind": "intention_touch", legacy.OLD_ROW_ID_KEY: rig.ids["seed"]})
    assert (err.code, err.http_status) == ("bad_request", 400)
    _readable(err, "intention_id")


def test_the_sign_compose_route_answers_a_retired_verb_the_same_way(cockpit):  # noqa: F811
    old = next(o for o, n in legacy.OLD_VERB_TO_NEW.items() if n == "intention_seed")
    status, body = _call(cockpit["url"], "/cockpit/sign/compose", {"verb": old, "params": {}, "client_txn": "abcdefgh12"})
    assert (status, body["error"]) == (404, "unknown_verb"), body
    assert "reload" in body["message"].lower() and "intention_seed" in body["message"], body


# ------------------------------------------------------------------ journal history, an OLD row


@pytest.mark.parametrize("old,new", sorted(legacy.OLD_VERB_TO_NEW.items()))
def test_an_old_journal_rows_verb_and_key_read_as_the_new_ones_do(old, new):
    """Recent edits shows a journal row an older release wrote (its verb id and `<old kind>:<id>` resource key are signed
    history): it reads as the same sentence its renamed verb gets."""
    from levain.cockpit.providers import _edit_target, plain_edit_title

    old_row = plain_edit_title(old, _edit_target(f"{legacy.OLD_ROW_KIND}:origin-1"), "token:browser:3")
    new_row = plain_edit_title(new, _edit_target("intention:origin-1"), "token:browser:3")
    assert old_row == new_row
    assert "item origin-1" in old_row and legacy.OLD_ROW_KIND not in old_row


def test_a_retired_resource_key_kind_is_read_not_taught():
    """The history reader accepts the old kind it reads and no other: an unknown kind is named as the store names it."""
    from levain.cockpit.providers import _edit_target

    assert _edit_target(f"{legacy.OLD_ROW_KIND}x:origin-1") == f"{legacy.OLD_ROW_KIND}x:origin-1"
