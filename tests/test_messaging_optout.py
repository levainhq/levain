"""`levain init --messaging governed|off|unmanaged`: the one-step opt-out for Q9 (DESIGN_MESSAGING_OPTOUT). The
template carries no messaging keys; the renderer writes them through `merge_messaging`, the choice persists in
`.levain/config.json`, and `levain update` / `init --force` keep it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from levain import host_guard as hg
from levain.install import (
    InitError,
    MESSAGING_CHOICES,
    _apply_messaging,
    persisted_messaging,
    refresh_adapter,
    run_init,
)

TEMPLATE = (Path(__file__).resolve().parent.parent / "levain" / "templates" / "adapters" / "claude-code"
            / "settings.template.json")
KEYS = ("crossSessionInbound", "isolatePeerMachines")


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(hg, "load_managed_settings", lambda platform=None: [])
    return tmp_path


def _base() -> str:
    return TEMPLATE.read_text(encoding="utf-8").replace("{{PYTHON}}", "python3")


def _answers(env: Path, capsys) -> Path:
    from tests.test_init_answers import _filled

    af = env / "a.json"
    af.write_text(json.dumps(_filled(capsys)), encoding="utf-8")
    return af


def _settings(ent: Path) -> dict:
    return json.loads((ent / ".claude" / "settings.json").read_text(encoding="utf-8"))


def _msg(doc: dict) -> dict:
    perms = doc.get("permissions", {})
    return {"inbound": doc.get("crossSessionInbound"), "isolate": doc.get("isolatePeerMachines"),
            "ask": perms.get("ask"), "deny": perms.get("deny")}


def test_the_template_carries_no_messaging_keys():
    doc = json.loads(_base())
    assert not any(k in doc for k in KEYS)
    assert "ask" not in doc["permissions"] and "deny" not in doc["permissions"]
    assert doc["permissions"]["allow"] == ["mcp__anneal_memory"]


def test_renderer_governed_keys():
    doc = json.loads(_apply_messaging(_base(), "governed"))
    assert _msg(doc) == {"inbound": "hold", "isolate": True, "ask": ["SendMessage"], "deny": None}
    assert doc["permissions"]["allow"] == ["mcp__anneal_memory"]


def test_renderer_off_keys():
    doc = json.loads(_apply_messaging(_base(), "off"))
    assert _msg(doc) == {"inbound": "refuse", "isolate": None, "ask": None,
                         "deny": ["SendMessage", "ListAgents"]}


def test_renderer_unmanaged_writes_none_of_the_three_keys():
    doc = json.loads(_apply_messaging(_base(), "unmanaged"))
    assert _msg(doc) == {"inbound": None, "isolate": None, "ask": None, "deny": None}


@pytest.mark.parametrize("choice", MESSAGING_CHOICES)
def test_the_renderer_keeps_the_hooks_block_byte_order(choice):
    base = json.loads(_base())
    out = json.loads(_apply_messaging(_base(), choice))
    assert out["hooks"] == base["hooks"] and list(out["hooks"]) == list(base["hooks"])


def test_init_default_is_isolate_only_and_doctor_names_the_gaps(env, capsys):
    """Messaging is opt-in, isolatePeerMachines on by default: no --messaging writes only isolatePeerMachines;
    doctor reads the default as the install's choice (not a warning) and says what is not governed."""
    from levain import doctor

    ent = env / "e"
    assert run_init(ent, "claude-code", force=False, answers_file=_answers(env, capsys)) == 0
    s = _settings(ent)
    assert s["isolatePeerMachines"] is True and "crossSessionInbound" not in s
    assert "SendMessage" not in json.dumps(s.get("permissions", {}))
    cfg = ent / ".levain" / "config.json"
    assert json.loads(cfg.read_text(encoding="utf-8"))["messaging"] == "isolate"   # the default is recorded too
    (r,) = doctor._check_cc_messaging(ent)
    assert r.ok and not r.warn and "messaging choice `isolate`" in r.detail
    assert "does not queue or record sends" in r.detail and "never counts a message" in r.detail
    cfg.write_text("{not json")
    assert persisted_messaging(ent) == "governed"  # a record that cannot be read is never the looser default
    # seen red (review round 1): a governed file whose record is gone re-rendered as isolate, dropping the hold
    cfg.unlink()
    s["crossSessionInbound"] = "hold"
    (ent / ".claude" / "settings.json").write_text(json.dumps(s))
    assert persisted_messaging(ent) == "governed"


@pytest.mark.parametrize("choice,inbound", [("off", "refuse"), ("unmanaged", None), ("governed", "hold")])
def test_init_persists_the_choice_and_renders_it(env, capsys, choice, inbound):
    ent = env / "e"
    assert run_init(ent, "claude-code", force=False, answers_file=_answers(env, capsys), messaging=choice) == 0
    assert json.loads((ent / ".levain" / "config.json").read_text(encoding="utf-8"))["messaging"] == choice
    assert _msg(_settings(ent))["inbound"] == inbound
    assert persisted_messaging(ent) == choice


@pytest.mark.parametrize("choice", ["off", "unmanaged"])
def test_update_refresh_keeps_an_opt_out(env, capsys, choice):
    ent = env / "e"
    assert run_init(ent, "claude-code", force=False, answers_file=_answers(env, capsys), messaging=choice) == 0
    before = (ent / ".claude" / "settings.json").read_bytes()
    assert refresh_adapter(ent, apply=True, emit=lambda *_: None) is not None
    assert (ent / ".claude" / "settings.json").read_bytes() == before
    assert persisted_messaging(ent) == choice


def test_update_refresh_rerenders_a_stale_governed_file_under_the_persisted_choice(env, capsys):
    """An update that finds levain's own old render (hash in the receipt) rewrites it, still under the choice."""
    ent = env / "e"
    assert run_init(ent, "claude-code", force=False, answers_file=_answers(env, capsys), messaging="off") == 0
    assert _msg(_settings(ent))["deny"] == ["SendMessage", "ListAgents"]
    refresh_adapter(ent, apply=True, emit=lambda *_: None)
    assert _msg(_settings(ent))["inbound"] == "refuse"  # not re-governed to hold


def test_force_keeps_the_persisted_choice_and_a_new_flag_changes_it(env, capsys):
    af = _answers(env, capsys)
    ent = env / "e"
    assert run_init(ent, "claude-code", force=False, answers_file=af, messaging="unmanaged") == 0
    assert run_init(ent, "claude-code", force=True, answers_file=af) == 0
    assert _msg(_settings(ent))["inbound"] is None and persisted_messaging(ent) == "unmanaged"
    assert run_init(ent, "claude-code", force=True, answers_file=af, messaging="off") == 0
    assert _msg(_settings(ent))["inbound"] == "refuse" and persisted_messaging(ent) == "off"


def test_the_choice_is_merged_beside_the_other_config_keys(env, capsys):
    af = _answers(env, capsys)
    ent = env / "e"
    assert run_init(ent, "claude-code", force=False, answers_file=af) == 0
    cfg = ent / ".levain" / "config.json"
    cfg.write_text(json.dumps({"entity_name": "Zed"}), encoding="utf-8")
    assert run_init(ent, "claude-code", force=True, answers_file=af, messaging="off") == 0
    assert json.loads(cfg.read_text(encoding="utf-8")) == {"entity_name": "Zed", "messaging": "off"}


@pytest.mark.parametrize("adapter", ["codex", "openhands"])
def test_other_adapters_refuse_the_flag_before_writing(env, capsys, adapter):
    ent = env / "e"
    assert run_init(ent, adapter, force=False, answers_file=_answers(env, capsys), messaging="off") == 1
    assert "claude-code adapter only" in capsys.readouterr().out
    assert not ent.exists()


def test_an_unknown_choice_refuses_before_writing_anything(env, capsys):
    ent = env / "e"
    assert run_init(ent, "claude-code", force=False, answers_file=_answers(env, capsys), messaging="maybe") == 1
    assert "--messaging must be one of" in capsys.readouterr().out
    assert not ent.exists()


def test_cli_parses_and_passes_the_flag(env, capsys, monkeypatch):
    from levain import cli

    seen = {}
    monkeypatch.setattr("levain.install.run_init", lambda **kw: seen.update(kw) or 0)
    assert cli.main(["init", "--path", str(env / "x"), "--adapter", "claude-code", "--messaging", "off"]) == 0
    assert seen["messaging"] == "off"
    with pytest.raises(SystemExit):
        cli.main(["init", "--messaging", "bogus"])


def test_an_unrecognised_persisted_value_is_governed(env):
    ent = env / "e"
    (ent / ".levain").mkdir(parents=True)
    (ent / ".levain" / "config.json").write_text(json.dumps({"messaging": "wide-open"}), encoding="utf-8")
    assert persisted_messaging(ent) == "governed"


def test_the_renderer_refuses_a_malformed_permissions_block():
    from levain.install import _MESSAGING_KNOBS

    assert set(_MESSAGING_KNOBS) == set(MESSAGING_CHOICES)
    with pytest.raises(InitError):
        _apply_messaging(json.dumps({"permissions": "nope"}), "governed")


# ---- L3 r1 fixes ----

def test_a_failed_render_leaves_the_choice_unrecorded(env, capsys, monkeypatch):
    """The choice is recorded after the adapter files are written, so an interrupted render cannot leave
    config.json saying `unmanaged` over a governed settings.json."""
    ent = env / "e"
    af = _answers(env, capsys)

    def boom(*a, **kw):
        raise InitError("render failed")

    monkeypatch.setattr("levain.install._install_claude_code", boom)
    assert run_init(ent, "claude-code", force=False, answers_file=af, messaging="unmanaged") == 1
    cfg = ent / ".levain" / "config.json"
    assert not cfg.is_file() or "messaging" not in json.loads(cfg.read_text(encoding="utf-8"))


def test_the_render_uses_the_explicit_choice_not_a_re_read_of_config(env, capsys, monkeypatch):
    """A config.json that loses the key between the write and the render must not turn `off` into governed."""
    ent = env / "e"
    af = _answers(env, capsys)
    monkeypatch.setattr("levain.install.persisted_messaging", lambda install: "governed")
    assert run_init(ent, "claude-code", force=False, answers_file=af, messaging="off") == 0
    assert _msg(_settings(ent))["inbound"] == "refuse"


def test_apply_init_refuses_a_bad_pairing_before_any_write(env):
    from levain.install import _templates_root, apply_init

    ent = env / "e"
    ent.mkdir()
    (ent / "seed").mkdir()
    (ent / "seed" / "world.md").write_text("keep me", encoding="utf-8")
    with _templates_root() as tr:
        with pytest.raises(InitError):
            apply_init(ent, "codex", {}, tr, "python3", "anneal-memory", [], [], messaging="off")
    assert (ent / "seed" / "world.md").read_text(encoding="utf-8") == "keep me"
    assert sorted(p.name for p in ent.iterdir()) == ["seed"]


@pytest.mark.parametrize("text", ["[]", "null", '"x"'])
def test_a_non_object_template_is_an_init_error(text):
    with pytest.raises(InitError):
        _apply_messaging(text, "governed")


def test_a_non_object_config_is_unreadable_and_left_alone(env, capsys):
    af = _answers(env, capsys)
    ent = env / "e"
    assert run_init(ent, "claude-code", force=False, answers_file=af) == 0
    cfg = ent / ".levain" / "config.json"
    cfg.write_text("[1, 2]", encoding="utf-8")
    assert run_init(ent, "claude-code", force=True, answers_file=af, messaging="off") == 1
    assert cfg.read_text(encoding="utf-8") == "[1, 2]"


def test_answers_template_refuses_messaging(env, capsys):
    from levain import cli

    assert cli.main(["init", "--answers-template", "--messaging", "off"]) == 1
    assert "--messaging" in capsys.readouterr().out


def test_force_says_when_it_keeps_a_prior_opt_out(env, capsys):
    af = _answers(env, capsys)
    ent = env / "e"
    assert run_init(ent, "claude-code", force=False, answers_file=af, messaging="off") == 0
    capsys.readouterr()
    assert run_init(ent, "claude-code", force=True, answers_file=af) == 0
    assert "messaging:  off (kept from .levain/config.json)" in capsys.readouterr().out
