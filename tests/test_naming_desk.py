"""The user-facing naming: "desk" is the one word for the seat where the operator meets Levain and for the mechanism behind it
(store, lease, launch, record, workspace, role); the old word for the mechanism is spelled only in levain/desk/legacy.py.
"At the desk" for being physically present is "at the laptop". This test holds the README, docs/, the main parser's help text
and the double- and single-quoted literals with a space in every .js/.html under levain/templates/web to that. Strings the
CLI prints and the band's text are not scanned."""
import argparse
import re
from pathlib import Path

from levain import cli

ROOT = Path(__file__).resolve().parent.parent

PHYSICAL = re.compile(r"\b(?:(?:at|from) (?:the|your)|wait for (?:the|your)) desk\b", re.I)


def test_readme_says_laptop_for_presence():
    text = (ROOT / "README.md").read_text()
    assert not PHYSICAL.findall(text), PHYSICAL.findall(text)


def test_docs_say_laptop_for_presence():
    paths = sorted((ROOT / "docs").rglob("*.md"))
    assert paths
    for path in paths:
        found = PHYSICAL.findall(path.read_text())
        assert not found, (path.name, found)


def _help_strings(parser: argparse.ArgumentParser, seen=None):
    seen = set() if seen is None else seen
    if id(parser) in seen:
        return
    seen.add(id(parser))
    for s in (parser.description, parser.epilog):
        if s:
            yield s
    for act in parser._actions:
        if act.help and act.help != argparse.SUPPRESS:
            yield act.help
        if isinstance(act, argparse._SubParsersAction):
            for choice in act._choices_actions:
                if choice.help:
                    yield choice.help
            for sub in act.choices.values():
                yield from _help_strings(sub, seen)


def test_cli_help_says_laptop_for_presence():
    texts = list(_help_strings(cli.build_parser()))
    assert len(texts) > 100
    for t in texts:
        assert not PHYSICAL.search(t), t


def _literals(line: str):
    return [a or b for a, b in re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"|\'([^\'\\]*(?:\\.[^\'\\]*)*)\'', line)]


def test_cockpit_visible_strings_say_laptop_for_presence():
    web = ROOT / "levain" / "templates" / "web"
    bad = []
    for path in sorted(web.rglob("*")):
        if path.suffix not in (".js", ".html"):
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if re.match(r"\s*(//|/\*|\*|<!--)", line):
                continue
            for lit in _literals(re.sub(r"\s//\s.*$", "", line)):   # a trailing comment is not a string
                if " " not in lit.strip():
                    continue
                if PHYSICAL.search(lit):
                    bad.append(f"{path.name}:{n}: {lit[:100]}")
    assert not bad, bad


def test_the_wake_prompt_names_the_launches_own_role():
    from types import SimpleNamespace
    from pathlib import Path
    from levain.desk import launch as ln
    from levain.desk.legacy import ROLE_DESK

    worker = ln._prompt(SimpleNamespace(launch_id="ln-w", role="worker"), Path("/b/brief.md"))
    desk = ln._prompt(SimpleNamespace(launch_id="ln-d", role=ROLE_DESK), Path("/b/brief.md"))
    assert "worker session" in worker and "desk session" not in worker
    assert "desk session" in desk and "worker" not in desk


def test_cause_word_maps_only_the_stored_cause_and_accepts_none():
    from levain.desk.legacy import CAUSE_CODEX_REMOVED, cause_word

    assert cause_word(None) == ""
    assert cause_word(CAUSE_CODEX_REMOVED) == "codex desk removed"
    for text in ("see /h/.levain/coordinator/x", "session levain-coordinator-ab12", "coordinator-draft: x",
                 "the coordinator crashed"):
        assert cause_word(text) == text


def test_init_accepts_the_desk_role_word_and_names_the_bad_flag(capsys, tmp_path):
    from levain import install as inst
    from levain.desk.legacy import ROLE_DESK

    assert inst.run_init(tmp_path / "a", "claude-code", force=False, role="bogus") == 1
    out = capsys.readouterr().out
    assert "--role 'bogus'" in out and "is not an install role" in out and "desk" in out
    assert inst.run_init(tmp_path / "b", "gemini", force=False, role="desk") == 1
    out = capsys.readouterr().out
    assert "--role desk" in out and "adapter only" in out
    assert inst.by_word("desk") == ROLE_DESK     # the normalisation run_init applies


def test_a_refused_thread_transition_lists_the_desk_under_its_new_name(tmp_path):
    import pytest as _pt
    from levain.desk.legacy import ROLE_DESK
    from levain.desk.store import CoordError, CoordStore, Part

    st = CoordStore(tmp_path / "q")
    tok = st.coord_take()
    d, _ = st.accept_drop("w", surface="cli")
    t = st.split(d.drop_id, [Part(kind="work", title="w")], by=ROLE_DESK, token=tok)[0]
    with _pt.raises(CoordError) as e:
        st.set_state(t.thread_id, "done", by="operator")          # only the desk takes open -> done
    assert "(only desk)" in str(e.value) and ROLE_DESK not in str(e.value)


def test_the_by_option_lists_desk_and_the_alias_shim_survives_a_runpy_run():
    import subprocess, sys, argparse
    from levain.desk.legacy import by_choices, by_word

    p = argparse.ArgumentParser()
    p.add_argument("--by", type=by_word, choices=by_choices("operator", "coordinator"))
    assert p.parse_args(["--by", "desk"]).by == "coordinator"
    assert "desk" in p.format_help() and "coordinator" not in p.format_help()
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    code = (f"import sys; sys.path.insert(0, {str(root)!r}); import runpy, importlib.util, levain.coordinator.legacy as m; "
            "runpy.run_module('levain.coordinator.legacy', run_name='x'); "
            "print(m is importlib.import_module('levain.desk.legacy'))")
    r = subprocess.run([sys.executable, "-c", code], cwd="/", capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == "True", r.stderr[-400:]
