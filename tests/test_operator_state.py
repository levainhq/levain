"""The operator `state` line: the one freeform, expiring "what's going on" line in the
live-context file (it replaced the separate `focus` field). Budget: contract read, expiry, fail-soft, hook/dashboard agreement."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1] / "levain" / "templates" / "activation" / "hooks"
sys.path.insert(0, str(HOOKS))
import _levain_hook as hook  # noqa: E402

from levain.dashboard import _read_state, render_summary, render_text, write_state  # noqa: E402

NOW = datetime(2026, 10, 9, 20, 0, tzinfo=timezone.utc)


def _stamp(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def _ctx(tmp_path: Path, **keys) -> Path:
    p = tmp_path / ".levain" / "context.json"
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(keys), encoding="utf-8")
    return p


def test_contract_reads_the_three_keys_verbatim_and_superset_tolerant(tmp_path):
    p = _ctx(tmp_path, state="  wiped,\n keep it light ", state_set_at=_stamp(2),
             state_source="app", focus="x", energy=3, body=[1])
    st = _read_state(p, NOW)
    assert (st.text, st.source, st.age_label) == ("wiped, keep it light", "app", "set 2h ago")
    assert st.set_at == _stamp(2)
    assert _read_state(None, NOW) is None  # no context source at all


def test_expiry_drops_the_line_and_write_clear_pops_all_three(tmp_path):
    for hours in (12, 30):  # at the boundary and well past it
        p = _ctx(tmp_path, state="old", state_set_at=_stamp(hours), state_source="cli")
        assert _read_state(p, NOW).text is None
    # clock skew within the tolerance reads as just-now; far-future and a missing stamp: not shown
    skewed = _read_state(_ctx(tmp_path, state="x", state_set_at=_stamp(-0.05)), NOW)
    assert skewed.text == "x" and skewed.age_label == "set just now"
    assert _read_state(_ctx(tmp_path, state="x", state_set_at=_stamp(-1)), NOW).text is None
    assert _read_state(_ctx(tmp_path, state="y" * 501, state_set_at=_stamp(1)), NOW).text is None  # over-cap
    assert _read_state(_ctx(tmp_path, state="x"), NOW).text is None
    # a newer write replaces, an explicit clear removes all three keys, other keys untouched
    # (an older install's focus keys are such keys: never migrated, never deleted)
    p = _ctx(tmp_path, focus="f", focus_set_at=_stamp(1), focus_source="cli")
    write_state(p, "one", source="cli"); write_state(p, "two", source="app")
    assert json.loads(p.read_text())["state"] == "two"
    write_state(p, "  ", source="cli")
    data = json.loads(p.read_text())
    assert not {"state", "state_set_at", "state_source"} & data.keys()
    assert data["focus"] == "f"
    write_state(p, "s", source="cli")  # a later write still keeps the legacy keys
    assert json.loads(p.read_text())["focus"] == "f" and json.loads(p.read_text())["state"] == "s"
    import pytest
    with pytest.raises(ValueError):
        write_state(p, "z" * 501)  # the writer refuses what every reader would drop


def test_malformed_context_fails_soft_to_nothing(tmp_path, monkeypatch):
    p = tmp_path / ".levain" / "context.json"
    p.parent.mkdir()
    for body in ("{not json", "[1,2]", '{"state": 7, "state_set_at": 9}', ""):
        p.write_text(body, encoding="utf-8")
        assert _read_state(p, NOW).text is None
        monkeypatch.setattr(hook, "install_root", lambda: tmp_path)
        assert hook.state_notice() is None


def test_hook_text_and_dashboard_agree(tmp_path, monkeypatch):
    from levain.dashboard import AnnealPaths, SubstrateView
    now = datetime.now(timezone.utc)
    stamp = (now - timedelta(hours=3)).isoformat()
    p = _ctx(tmp_path, state="sharp, go deep", state_set_at=stamp, state_source="cli")
    monkeypatch.setattr(hook, "install_root", lambda: tmp_path)
    note = hook.state_notice()
    view = SubstrateView(paths=AnnealPaths(episodic_db=tmp_path / "memory.db", continuity_md=tmp_path / "c.md", crystal_json=tmp_path / "x.json", spores_json=tmp_path / "s.json"))
    view.state = _read_state(p, now)
    rendered = render_text(view)
    assert hook._STATE_MAX_TEXT_LEN == 500 and hook._STATE_CLOCK_SKEW_SECONDS == 300
    assert '"sharp, go deep"' in note and "set 3h ago" in note
    assert "state: sharp, go deep (set 3h ago)" in rendered
    assert 'State: "sharp, go deep" (set 3h ago)' in render_summary(view)  # the model-visible MCP text
    # a state with a quote cannot break out of its delimiters
    p.write_text(json.dumps({"state": 'a" b', "state_set_at": stamp}))
    assert 'a\\" b' in hook.state_notice()
    p.write_text(json.dumps({"state": "sharp, go deep", "state_set_at": stamp, "state_source": "cli"}))
    # expiry agrees on both surfaces, and the hook's mirrored bound IS the kernel's
    from levain.dashboard import STATE_EXPIRES_AFTER_HOURS
    assert hook._STATE_EXPIRES_AFTER_HOURS == STATE_EXPIRES_AFTER_HOURS == 12
    p.write_text(json.dumps({"state": "sharp", "state_set_at": (now - timedelta(hours=13)).isoformat()}))
    view.state = _read_state(p, now)
    assert hook.state_notice() is None and "state:" not in render_text(view)


def test_focus_is_gone_from_the_hooks_the_cli_and_the_write_seam(tmp_path, monkeypatch):
    # the two live-context fields collapsed into one: nothing of `focus` remains as a surface
    import pytest
    from levain import cli
    from levain.writes import EditError, WriteScope, apply_edit

    # the helper keeps a silent focus_notice() for one release: a session_start.py that `levain update` left
    # pending still calls it, and must not lose its other sections
    assert hook.focus_notice() is None
    for hooks_dir in (HOOKS, HOOKS.parents[1] / "adapters" / "codex" / "activation" / "hooks"):
        for name in ("session_start.py", "user_prompt_submit.py"):
            assert "focus" not in (hooks_dir / name).read_text(encoding="utf-8").lower(), (hooks_dir, name)

    with pytest.raises(SystemExit) as ei:
        cli.main(["focus", "x"])  # argparse: invalid choice
    assert ei.value.code == 2

    p = _ctx(tmp_path, focus="legacy", focus_set_at=_stamp(1), focus_source="cli")
    scope = WriteScope.from_install_root(tmp_path)
    assert scope.context_json == p
    with pytest.raises(EditError) as ee:
        apply_edit(scope, {"kind": "focus", "text": "x"})
    assert ee.value.code == "bad_kind" and ee.value.http_status == 400
    assert json.loads(p.read_text())["focus"] == "legacy"  # refused, not applied


def test_the_operator_manual_states_the_expiry_the_code_uses() -> None:
    from levain.dashboard import STATE_EXPIRES_AFTER_HOURS
    manual = (Path(__file__).resolve().parent.parent / "levain" / "templates" / "docs" / "operator-manual.md").read_text()
    assert f"after {STATE_EXPIRES_AFTER_HOURS} hours" in manual
