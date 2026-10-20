"""Q18 S-a: the default grant offered at the end of ``levain init`` (project_memory/1019-q18/DESIGN.md §2).

One grant per agent the consent hook was wired for, workspace = the install root, 14 days, id
``cg-default-<agent>-<8 hex>``. Offered only when the authority log the hook verifies against is initialised and an
enrolled laptop-presence key is in levain's key directory; at a terminal it prints the exact grant and asks [y/N]
(default No) and signs through ``levain consent grant create`` itself; without a terminal it prints that command and
signs nothing. A live default grant for the same workspace is kept, never offered twice. ``--default-grants no``
never offers, and there is no ``yes``."""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import levain.authority as auth
import levain.cli as cli
from levain.authority import CONSENT_GRANTABLE_TOOLS, CONSENT_POSTURES, ReplayCache, anchor_path
from levain.cockpit.firejournal import CockpitDB
from levain.consent import default_grants as dg
from levain.consent.adapters import claude_code, codex
from levain.consent.grants import list_grants
import levain.install as inst
from levain.install import run_init
from tests.test_repin_on_install import make_install  # noqa: F401  (fixture)
from tests.test_subfolder_hooks import cc_local_layout  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("cc_local_layout")

ID = re.compile(r"cg-default-(claude_code|codex)-[0-9a-f]{8}")


def _no_input(prompt: str = "") -> str:
    raise AssertionError(f"asked when it must not: {prompt!r}")


@pytest.fixture
def home(tmp_path, monkeypatch):
    lhome = tmp_path / "lhome"
    lhome.mkdir(mode=0o700)
    monkeypatch.setenv("LEVAIN_HOME", str(lhome))
    (lhome / "consent").mkdir(mode=0o700)
    return lhome


def _root(adapter: str) -> Path:
    mod = claude_code if adapter == "claude-code" else codex
    root = mod.load_settings(inst._consent_queue_dir()).cockpit_data_root
    assert root, "init wrote no consent settings naming a cockpit"
    return Path(root)


def _keygen(d: Path, name: str) -> tuple[Path, str, str]:
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = d / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(path)], check=True,
                   capture_output=True, timeout=30)
    pub = " ".join((d / f"{name}.pub").read_text().split()[:2])
    fp = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(path)], check=True, capture_output=True,
                        text=True).stdout.split()[1]
    return path, pub, fp


def _ledger(root: Path, key_dir: Path, *, enrol: bool = True) -> Path:
    """A reported cockpit journal at ``root``; with ``enrol``, its log's genesis enrols a laptop-presence key kept
    in ``key_dir``. Returns the key's path."""
    root.mkdir(parents=True, exist_ok=True)
    CockpitDB(root).set_install_class("reported")
    key, pub, fp = _keygen(key_dir, "laptop")
    if enrol:
        conn = __import__("sqlite3").connect(root / "cockpit.db", isolation_level=None)
        try:
            auth.sign_and_append(
                conn, {"op": "enrol", "fingerprint": fp, "public_key": pub, "class": "laptop-presence",
                       "custody": "passphrase"},
                signer_fp=fp,
                sign=lambda m: subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(key), "-n",
                                               auth.NAMESPACE], input=m, capture_output=True,
                                              check=True).stdout.decode(),
                clock=lambda: datetime.now(timezone.utc), cache=ReplayCache(), anchor=anchor_path(root))
        finally:
            conn.close()
    return key


def _grants(root: Path):
    return list_grants(root, now=datetime.now(timezone.utc))


def _printed_argv(out: str) -> list[str]:
    (line,) = [ln for ln in out.splitlines() if "levain consent grant create" in ln]
    cmd = line[line.index("levain consent grant create"):]
    argv = shlex.split(cmd)
    assert argv[0] == "levain"
    return argv[1:]


@pytest.fixture
def cc(home, make_install):
    return make_install("cc")


@pytest.fixture
def ready(cc, home):
    """A claude-code install whose hook's cockpit holds an initialised log and whose key sits in levain's key dir."""
    key = _ledger(_root("claude-code"), home / "keys")
    return cc, key


# --- the pack ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_the_pack_names_only_tools_and_postures_the_log_accepts(agent):
    tools, postures = dg.PACKS[agent]
    assert set(tools) <= CONSENT_GRANTABLE_TOOLS[agent]
    assert set(postures) <= CONSENT_POSTURES[agent]


def test_the_pack_is_the_design_pack_without_websearch():
    # WebSearch is not on levain's list for claude_code (CHANGELOG 10-17 "names only a tool levain knows"), so the
    # log would refuse the whole grant: it is dropped, the list is not widened
    assert dg.PACKS["claude_code"] == (("Edit", "MultiEdit", "NotebookEdit", "WebFetch", "Write"),
                                       ("acceptEdits", "default"))
    assert dg.PACKS["codex"] == (("apply_patch",), ("default",))
    assert dg.EXPIRES == "14d"


# --- when it is not offered --------------------------------------------------------------------------------------

def test_no_offer_without_a_journal(cc, home, capsys):
    dg.offer(cc, "claude-code", interactive=True, input_fn=_no_input)
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if "default grant" in ln]
    assert len(lines) == 1 and "levain cockpit ledger init" in lines[0], out
    assert "Edit" in lines[0] and str(os.path.realpath(cc)) in lines[0]
    assert not (home / "keys").exists()          # no key creation from init


def test_no_offer_with_an_empty_log(cc, home, capsys):
    _ledger(_root("claude-code"), home / "keys", enrol=False)
    dg.offer(cc, "claude-code", interactive=True, input_fn=_no_input)
    out = capsys.readouterr().out
    assert "levain cockpit ledger init" in out
    assert "consent grant create" not in out


def test_no_offer_without_a_laptop_presence_key(cc, home, tmp_path, capsys):
    # the enrolled key is held somewhere levain does not look: nothing resolves, nothing is asked or signed
    _ledger(_root("claude-code"), tmp_path / "elsewhere")
    dg.offer(cc, "claude-code", interactive=True, input_fn=_no_input)
    out = capsys.readouterr().out
    assert "no enrolled laptop-presence key" in out, out
    assert _grants(_root("claude-code")) == []


def test_default_grants_no_never_offers(ready, capsys):
    cc, _key = ready
    dg.offer(cc, "claude-code", mode="no", interactive=True, input_fn=_no_input)
    assert "default grant" not in capsys.readouterr().out
    assert _grants(_root("claude-code")) == []


# --- without a terminal ------------------------------------------------------------------------------------------

def test_non_tty_prints_the_command_and_signs_nothing(ready, capsys):
    cc, key = ready
    dg.offer(cc, "claude-code", interactive=False, input_fn=_no_input)
    out = capsys.readouterr().out
    argv = _printed_argv(out)
    assert argv[:3] == ["consent", "grant", "create"]
    assert ID.fullmatch(argv[argv.index("--id") + 1])
    assert argv[argv.index("--signing-key") + 1] == str(key)
    assert argv[argv.index("--workspace") + 1] == os.path.realpath(cc)
    assert argv[argv.index("--expires") + 1] == "14d"
    assert _grants(_root("claude-code")) == []


def test_codex_non_tty_names_apply_patch_under_default(home, make_install, capsys):
    cx = make_install("cx", adapter="codex")
    _ledger(_root("codex"), home / "keys")
    dg.offer(cx, "codex", interactive=False, input_fn=_no_input)
    argv = _printed_argv(capsys.readouterr().out)
    tools = [argv[i + 1] for i, a in enumerate(argv) if a == "--tool"]
    postures = [argv[i + 1] for i, a in enumerate(argv) if a == "--posture"]
    assert (tools, postures) == (["apply_patch"], ["default"])
    assert argv[argv.index("--agent") + 1] == "codex"


# --- at a terminal -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("answer", ["n", "", "N", "no", "maybe"])
def test_tty_anything_but_yes_signs_nothing(ready, capsys, answer):
    cc, _key = ready
    asked = []
    dg.offer(cc, "claude-code", interactive=True, input_fn=lambda p="": asked.append(p) or answer)
    assert len(asked) == 1 and "[y/N]" in asked[0]
    assert _grants(_root("claude-code")) == []
    assert "not signed" in capsys.readouterr().out


def test_tty_eof_is_no(ready, capsys):
    cc, _key = ready

    def eof(_p=""):
        raise EOFError

    dg.offer(cc, "claude-code", interactive=True, input_fn=eof)
    assert _grants(_root("claude-code")) == []


def test_tty_yes_signs_exactly_the_printed_grant_through_grant_create(ready, capsys, monkeypatch):
    cc, key = ready
    calls = []
    real_main = cli.main
    monkeypatch.setattr(cli, "main", lambda argv=None: calls.append(list(argv)) or real_main(argv))
    dg.offer(cc, "claude-code", interactive=True, input_fn=lambda p="": "y")
    out = capsys.readouterr().out
    printed = _printed_argv(out)
    assert calls == [printed]                     # the one grant-create path, with the argv the user was shown
    (g,) = _grants(_root("claude-code"))
    assert g.id == printed[printed.index("--id") + 1] and ID.fullmatch(g.id)
    assert (g.agent, g.tools, g.postures) == ("claude_code", dg.PACKS["claude_code"][0], dg.PACKS["claude_code"][1])
    assert g.workspace == os.path.realpath(cc)
    for t in g.tools:
        assert f"--tool {t}" in shlex.join(printed)
    expires = datetime.fromisoformat(g.expires_at.replace("Z", "+00:00"))
    assert abs(expires - (datetime.now(timezone.utc) + timedelta(days=14))) < timedelta(minutes=5)
    assert f"granted: {g.id}" in out
    # what the user is shown before the question
    for word in ("Edit", "MultiEdit", "NotebookEdit", "WebFetch", "Write", "acceptEdits", "14 days", str(key)):
        assert word in out, word


def test_rerun_keeps_a_live_default_grant(ready, capsys):
    cc, _key = ready
    dg.offer(cc, "claude-code", interactive=True, input_fn=lambda p="": "y")
    (g,) = _grants(_root("claude-code"))
    capsys.readouterr()
    dg.offer(cc, "claude-code", interactive=True, input_fn=_no_input)
    dg.offer(cc, "claude-code", interactive=False, input_fn=_no_input)
    out = capsys.readouterr().out
    assert out.count(f"kept (expires {g.expires_at})") == 2, out
    assert "consent grant create" not in out
    assert len(_grants(_root("claude-code"))) == 1


def test_a_default_grant_for_another_workspace_is_not_kept(ready, tmp_path, capsys):
    cc, _key = ready
    other = tmp_path / "other"
    other.mkdir()
    dg.offer(other, "claude-code", interactive=True, input_fn=lambda p="": "y")
    capsys.readouterr()
    dg.offer(cc, "claude-code", interactive=False, input_fn=_no_input)
    out = capsys.readouterr().out
    assert "kept" not in out and "consent grant create" in out


# --- through levain init -----------------------------------------------------------------------------------------

def test_init_without_a_terminal_prints_the_command(ready, tmp_path, capsys):
    cc, _key = ready
    from tests.test_init_answers import _filled

    af = tmp_path / "again-answers.json"
    af.write_text(__import__("json").dumps(_filled(capsys)), encoding="utf-8")
    capsys.readouterr()
    assert run_init(cc, "claude-code", force=True, answers_file=af) == 0
    out = capsys.readouterr().out
    assert "levain consent grant create" in out
    assert _grants(_root("claude-code")) == []


def test_init_default_grants_no(ready, tmp_path, capsys):
    cc, _key = ready
    from tests.test_init_answers import _filled

    af = tmp_path / "again-answers.json"
    af.write_text(__import__("json").dumps(_filled(capsys)), encoding="utf-8")
    capsys.readouterr()
    assert run_init(cc, "claude-code", force=True, answers_file=af, default_grants="no") == 0
    assert "default grant" not in capsys.readouterr().out


def test_init_flag_has_no_yes(capsys):
    with pytest.raises(SystemExit):
        cli.main(["init", "--default-grants", "yes"])
    assert "invalid choice" in capsys.readouterr().err
