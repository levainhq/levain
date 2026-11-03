"""Presence 1b lane C (DESIGN section 12, row 7): ONE line everywhere. For each preset set from the CLI, ``levain ping status``
prints exactly ``GET /webpush/presence`` ``.line`` and ``intent.describe_line``; a set from the cockpit POST is then printed by
the CLI status unchanged; and a refused write leaves every surface on the line it had."""
from __future__ import annotations

import pytest

from levain import cli as levain_cli
from levain.consent import band
from levain.webpush import intent
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import T, _call
from tests.test_cockpit_webpush import _signed

IDLE = 120.0   # two minutes at the laptop: a fixed reading, so the lines of two instants cannot differ by a clock tick


@pytest.fixture(autouse=True)
def _operator_with_a_fixed_idle(monkeypatch):
    monkeypatch.setattr(band, "agent_process", lambda: None)
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: IDLE)


@pytest.fixture
def rig(tmp_path):
    q = tmp_path / "signq"
    q.mkdir(mode=0o700)
    return _install(tmp_path)[1], q


def _cli(capsys, q, *argv) -> tuple[int, str]:
    capsys.readouterr()
    rc = levain_cli.main(["ping", *argv, "--queue-dir", str(q)])
    return rc, capsys.readouterr().out.strip()


def _status(capsys, q) -> str:
    rc, out = _cli(capsys, q, "status")
    assert rc == 0
    return out


def _same_everywhere(capsys, port, q) -> str:
    line = _status(capsys, q)
    st, got, _ = _call(port, "GET", "/webpush/presence", token=T)
    assert st == 200
    assert got["line"] == line == got["text"] == intent.describe_line(q, idle=lambda: IDLE), (got["line"], line)
    return line


PRESETS = [("away",), ("quiet", "30m"), ("sleep",), ("loud", "1h"), ("back",), ("loud", "off")]
SETTINGS = [("devices", "off"), ("devices", "follow", "--after", "15m"), ("lets", "needs", "--for", "1h"),
            ("lets", "nothing", "--until-input"), ("devices", "on", "--for", "2h")]


def test_every_surface_prints_the_one_line(rig, capsys):
    src, q = rig
    seen = set()
    with _signed(src, q) as (port, _):
        for argv in PRESETS + SETTINGS:
            rc, printed = _cli(capsys, q, *argv)       # a write verb prints the line after it
            assert rc == 0, (argv, printed)
            assert printed == _same_everywhere(capsys, port, q), argv
            seen.add(printed)
        assert len(seen) >= 8                           # the verbs really changed the line, they did not all print one thing

        prior = _status(capsys, q)
        for body in ({"preset": "away"}, {"preset": "quiet", "for": "1h"}, {"word": "sleep"}, {"preset": "back"},
                     {"devices": "follow", "follow_after": 1200}, {"lets_through": "approvals", "until": "30m"},
                     {"devices": "off", "until": "input"}):
            st, posted, _ = _call(port, "POST", "/webpush/presence/set", body, token=T)
            assert st == 200, (body, posted)
            assert posted["line"] == _status(capsys, q) == _same_everywhere(capsys, port, q), body   # cockpit write -> terminal read
            assert posted["line"] != prior, body            # and the write took effect: a dropped write would leave every surface agreeing on the old line
            prior = posted["line"]


def test_a_refused_write_leaves_every_surface_on_the_line_it_had(rig, capsys):
    src, q = rig
    with _signed(src, q) as (port, _):
        assert _cli(capsys, q, "quiet", "30m")[0] == 0
        before = _same_everywhere(capsys, port, q)
        for argv in (("lets", "everything", "--for", "soon"), ("policy", "set", "sleep", "approval", "hold"), ("policy", "reset")):
            assert _cli(capsys, q, *argv)[0] == 2
        st, got, _ = _call(port, "POST", "/webpush/presence/set", {"devices": "follow", "follow_after": 5}, token=T)
        assert st == 422 and "message" in got
        assert _same_everywhere(capsys, port, q) == before


def test_the_lets_through_words_are_the_same_string_in_every_surface(rig, capsys):
    """Phill 11-02, 11-03 (12a): each level's visible words are GENERATED from its member classes, so the label lists exactly
    what the level passes, and the cockpit payload, describe_line and ``levain ping status`` print the same string. The page keeps no
    copy; the record keys stay everything|needs|approvals|nothing."""
    from pathlib import Path

    src, q = rig
    js = (Path(intent.__file__).resolve().parent.parent / "templates" / "web" / "devices_panel.js").read_text()
    assert "PR_LETS" not in js and "Approvals and needs" not in js and "Approvals only" not in js
    view = intent.view(q, idle=lambda: IDLE)
    assert [o["value"] for o in view["lets"]] == list(intent.LETS) == list(intent.LETS_CLASSES)
    classes = {"approval": "approval", "attention": "attention", "other": "other"}
    for o in view["lets"]:
        members = intent.LETS_CLASSES[o["value"]]
        text = (o["label"] + " " + o["describe"]).lower()
        # the words name exactly the member classes: each member's word appears, no non-member's does
        for c in classes:
            word = intent._CLASS_WORDS[c][0]
            assert (word in o["label"].lower()) == (c in members), (o, c)
        assert (o["label"] == "Nothing") == (not members), o
        assert o == {"value": o["value"], "label": intent.lets_label(o["value"]), "describe": intent.lets_describe(o["value"])}
    words = {o["value"]: o["label"] for o in view["lets"]}
    assert len(set(words.values())) == len(words)
    for key, words in words.items():
        rc, printed = _cli(capsys, q, "lets", key, "--for", "1h")
        assert rc == 0, printed
        assert f"Lets through: {words}." in printed == _status(capsys, q) == intent.describe_line(q, idle=lambda: IDLE), key
