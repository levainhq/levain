"""A held consent request reaches his NEEDS YOU band as a row with a sign link, and the row goes when the request does
(1105-19, levain 37; Phill 11-05: *"what in the fuck am I supposed to do with these? unless this comes as a band item in
needs you with an actual approve or deny what fucking good are these?"*).

Levain's half of the contract, in three parts, each held against a REAL child process (a notify command that records the
environment it was started with) and the real queue:

1. the HELD event: the command the hold already starts gets six more environment variables (and nothing else of the agent
   session's environment), among them the request's sign-sheet address;
2. the SETTLED event: the same command, once, when the request leaves ``pending`` by any path (a signed answer, a
   withdrawal, an expiry, an unanswered hold's undecided mark), and only for a request the command was told of;
3. the PRESELECT: the sheet opened with ``?choice=allow|deny`` highlights that option and signs nothing.

Before the build the child saw none of the variables (``LEVAIN_CONSENT_*`` keys: ``[]``), and a settled request started no
command. What stays undischarged here is a real Face ID approve from the band row on his phone."""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from levain import authority as auth
from levain.consent import band_notice
from levain.consent.adapters import claude_code, codex
from levain.consent.cockpit_origin import write_origin
from levain.consent.queue import ConsentQueue, authority_source
from levain.launch import child_env
from levain.webpush import sender
from tests._h4 import h4_home
from tests.test_cockpit_consent_sheet import PAGE, RP, _call, _file, _q, _queue, _serving, _signer, home, world  # noqa: F401
from tests.test_cockpit_consent_sheet_page import DESKTOP, FRAME, NOTE, STATE, TOK, _ConsentRig
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_sign_card import needs_ssh

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

ORIGIN = "https://phone.example"
HELD_KEYS = {"LEVAIN_CONSENT_EVENT", "LEVAIN_CONSENT_REQUEST", "LEVAIN_CONSENT_AGENT", "LEVAIN_CONSENT_WHAT",
             "LEVAIN_CONSENT_EXPIRES", "LEVAIN_CONSENT_SHEET_URL"}

# what a notify command that records itself does: one JSON line per start, its own consent variables, its argv after the
# file, and the names of every variable it was started with
_RECORDER = '''import json, os, sys
rec = {k: v for k, v in os.environ.items() if k.startswith("LEVAIN_CONSENT_")}
rec["_argv"] = sys.argv[2:]
rec["_names"] = sorted(os.environ)
with open(sys.argv[1], "a") as fh:
    fh.write(json.dumps(rec) + "\\n")
'''


class Cmd:
    """A recording notify command: ``argv`` to put in a settings file, ``lines()`` what its starts wrote."""

    def __init__(self, tmp_path: Path, name: str = "rec") -> None:
        script = tmp_path / f"{name}.py"
        script.write_text(_RECORDER)
        self.out = tmp_path / f"{name}.jsonl"
        self.argv = [sys.executable, str(script), str(self.out)]

    def read(self) -> list[dict[str, Any]]:
        try:
            return [json.loads(x) for x in self.out.read_text().splitlines()]
        except OSError:
            return []

    def wait(self, count: int, seconds: float = 20.0) -> list[dict[str, Any]]:
        """The lines once there are ``count``, then a beat more so a second start would show."""
        end = time.monotonic() + seconds
        while len(self.read()) < count and time.monotonic() < end:
            time.sleep(0.05)
        time.sleep(0.6)
        return self.read()

    def quiet(self, seconds: float = 1.0) -> list[dict[str, Any]]:
        time.sleep(seconds)
        return self.read()


@pytest.fixture(autouse=True)
def _no_record_children(monkeypatch):
    """The ping's record/push child is not what these tests are about: spied, never spawned."""
    calls: list[dict] = []
    monkeypatch.setattr(sender, "spawn", lambda qdir, **kw: calls.append(kw) or True)
    return calls


def _settings_file(qdir: Path, name: str, argv: list[str]) -> None:
    path = qdir / name
    data = json.loads(path.read_text()) if path.exists() else {"away_mode": False, "away_hold_seconds": 0}
    data["notify_argv"] = argv
    path.write_text(json.dumps(data))


def _held(world, cmd: Cmd, *, agent: str = "claude_code", name: str = "a.txt", origin: str | None = ORIGIN):
    """A request filed in the rig's queue and its hold's ping started, as the hook does it. Returns (request, settings)."""
    qdir = world.qdir
    settings_name = (claude_code if agent == "claude_code" else codex).SETTINGS_NAME
    _settings_file(qdir, settings_name, cmd.argv)
    if origin is not None:
        assert write_origin(qdir, origin) is not None
    q = _queue(world)
    rid = q.file(agent=agent, agent_session_id="s1", tool_name="Write",
                 tool_input={"file_path": str(world.ws / name), "content": "hi"}, cwd=str(world.ws),
                 hold=timedelta(minutes=5)).id
    request = q.get(rid)[0]
    settings = claude_code.read_settings_file(qdir / settings_name)
    assert claude_code.hold_ping(settings, qdir, agent, "Write of a.txt", rid, 300.0, request=request) is True
    return request, settings


# --- 1. the HELD event ------------------------------------------------------------------------------------------------
def test_the_notify_child_sees_the_six_held_variables_and_its_argv_is_unchanged(world, tmp_path) -> None:
    cmd = Cmd(tmp_path)
    request, _ = _held(world, cmd)
    (got,) = cmd.wait(1)
    assert {k for k in got if k.startswith("LEVAIN_CONSENT_")} == HELD_KEYS
    assert got["LEVAIN_CONSENT_EVENT"] == "held" and got["LEVAIN_CONSENT_REQUEST"] == request.id
    assert got["LEVAIN_CONSENT_AGENT"] == "claude_code"
    assert got["LEVAIN_CONSENT_EXPIRES"] == request.expires_at and got["LEVAIN_CONSENT_EXPIRES"].endswith("Z")
    assert got["LEVAIN_CONSENT_SHEET_URL"] == f"{ORIGIN}/sign/consent/{urllib.parse.quote(request.id, safe='')}"
    assert ":" not in got["LEVAIN_CONSENT_SHEET_URL"].split("/sign/consent/")[1]      # the id's colon is encoded
    # the sheet's own text: the request's display, made visible (what GET sheet.json calls `message`)
    from levain.cockpit.hintview import visible

    assert got["LEVAIN_CONSENT_WHAT"] == visible(request.display) and "a.txt" in got["LEVAIN_CONSENT_WHAT"]
    # argv and message are the ping's own, as before
    assert got["_argv"] == [claude_code.hold_message("claude_code", "Write of a.txt", 300.0)]


def test_without_a_stored_cockpit_origin_the_sheet_url_is_left_out_never_guessed(world, tmp_path) -> None:
    cmd = Cmd(tmp_path)
    _held(world, cmd, origin=None)
    (got,) = cmd.wait(1)
    assert {k for k in got if k.startswith("LEVAIN_CONSENT_")} == HELD_KEYS - {"LEVAIN_CONSENT_SHEET_URL"}


def test_a_stored_origin_that_is_not_a_valid_https_origin_is_not_a_url(world, tmp_path) -> None:
    cmd = Cmd(tmp_path)
    (world.qdir / "cockpit-origin.json").write_text(json.dumps({"cockpit_origin": "http://10.0.0.2:8000"}))
    _held(world, cmd, origin=None)
    (got,) = cmd.wait(1)
    assert "LEVAIN_CONSENT_SHEET_URL" not in got


def test_the_environment_carries_the_six_keys_and_nothing_else_the_session_had(world, tmp_path, monkeypatch) -> None:
    """The secrets rule: the child's variables are the allowlist (:func:`levain.launch.child_env`) as before, plus the six."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-for-the-child")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-not-for-the-child")
    plain, with_event = Cmd(tmp_path, "plain"), Cmd(tmp_path, "event")
    claude_code.run_notify_command(claude_code.AdapterSettings(notify_argv=tuple(plain.argv)), "m", cwd=tmp_path)
    _held(world, with_event)
    (a,) = plain.wait(1)
    (b,) = with_event.wait(1)
    assert set(b["_names"]) - set(a["_names"]) == HELD_KEYS
    assert set(a["_names"]) <= set(child_env()) | {"__CF_USER_TEXT_ENCODING", "LC_CTYPE", "__PYVENV_LAUNCHER__"}
    for name in ("ANTHROPIC_API_KEY", "GITHUB_TOKEN"):
        assert name not in a["_names"] and name not in b["_names"]
    assert "sk-not-for-the-child" not in json.dumps(b)


def test_the_value_is_one_line_and_short_whatever_the_call_displays(world, tmp_path) -> None:
    cmd = Cmd(tmp_path)
    q = _queue(world)
    _settings_file(world.qdir, "claude_code.json", cmd.argv)
    r = q.file(agent="claude_code", agent_session_id="s1", tool_name="Bash",
               tool_input={"command": "echo a\nsecret-looking\x00line " + "x" * 2000}, cwd=str(world.ws),
               hold=timedelta(minutes=5))
    claude_code.hold_ping(claude_code.read_settings_file(world.qdir / "claude_code.json"), world.qdir, "claude_code", "w",
                          r.id, 300.0, request=q.get(r.id)[0])
    (got,) = cmd.wait(1)
    what = got["LEVAIN_CONSENT_WHAT"]
    assert len(what) <= band_notice.WHAT_MAX and "\n" not in what and "\x00" not in what and "…" in what


def test_the_gates_own_ping_and_codexs_carry_the_event_too(world, tmp_path) -> None:
    """The PreToolUse gates ping a filed request through ``notify(…, request=)``, codex's through the same ``hold_ping``."""
    cmd = Cmd(tmp_path)
    _settings_file(world.qdir, "claude_code.json", cmd.argv)
    q = _queue(world)
    r = q.get(q.file(agent="claude_code", agent_session_id="s1", tool_name="Write",
                     tool_input={"file_path": str(world.ws / "g.txt"), "content": "x"}, cwd=str(world.ws),
                     hold=timedelta(minutes=5)).id)[0]
    s = claude_code.read_settings_file(world.qdir / "claude_code.json")
    claude_code.notify(s, claude_code.hold_message("claude_code", "w", 300.0), cwd=world.qdir, ref=f"request:{r.id}", request=r)
    ccmd = Cmd(tmp_path, "codex")
    _held(world, ccmd, agent="codex", name="c.txt")
    (a,) = cmd.wait(1)
    (b,) = ccmd.wait(1)
    assert (a["LEVAIN_CONSENT_EVENT"], a["LEVAIN_CONSENT_REQUEST"]) == ("held", r.id)
    assert (b["LEVAIN_CONSENT_EVENT"], b["LEVAIN_CONSENT_AGENT"]) == ("held", "codex")


def test_a_ping_with_no_request_carries_no_event(world, tmp_path) -> None:
    """A re-ping, a ruling or a limit's ask names no sheet: its child is as it was."""
    cmd = Cmd(tmp_path)
    claude_code.notify(claude_code.AdapterSettings(notify_argv=tuple(cmd.argv)), "levain: x", cwd=world.qdir)
    (got,) = cmd.wait(1)
    assert not {k for k in got if k.startswith("LEVAIN_CONSENT_")}
    assert not (world.qdir / band_notice.MARK_DIR).exists()


def test_a_command_that_cannot_start_leaves_no_mark_and_owes_no_settle(world, tmp_path) -> None:
    cmd = Cmd(tmp_path)
    q = _queue(world)
    r = q.get(q.file(agent="claude_code", agent_session_id="s1", tool_name="Write",
                     tool_input={"file_path": str(world.ws / "n.txt"), "content": "x"}, cwd=str(world.ws),
                     hold=timedelta(minutes=5)).id)[0]
    s = claude_code.AdapterSettings(notify_argv=("bin/relative-command",))      # refused: a relative path
    claude_code.hold_ping(s, world.qdir, "claude_code", "w", r.id, 300.0, request=r)
    assert not band_notice.unmark_held(world.qdir, r.id)
    _settings_file(world.qdir, "claude_code.json", cmd.argv)
    assert q.withdraw(r.id) and cmd.quiet() == []


# --- 2. the SETTLED event ---------------------------------------------------------------------------------------------
def _settled(cmd: Cmd, request_id: str, outcome: str) -> None:
    lines = cmd.wait(2)
    assert [(x["LEVAIN_CONSENT_EVENT"], x["LEVAIN_CONSENT_REQUEST"]) for x in lines] == [("held", request_id),
                                                                                         ("settled", request_id)], lines
    last = lines[1]
    assert last["LEVAIN_CONSENT_OUTCOME"] == outcome
    assert {k for k in last if k.startswith("LEVAIN_CONSENT_")} == {"LEVAIN_CONSENT_EVENT", "LEVAIN_CONSENT_REQUEST",
                                                                    "LEVAIN_CONSENT_OUTCOME"}
    assert last["_argv"] == [f"levain: claude_code's approval request was {outcome}."]      # a plain sentence, no phone url


def test_a_signed_answer_settles_the_request_once_whichever_way_it_lands(home, tmp_path) -> None:
    """The cockpit server answers in ITS process, through the sheet's route: the hook that held the request is not the one
    that settles it."""
    cmd = Cmd(tmp_path)
    request, _ = _held(home, cmd)
    rid = request.id
    with _serving(tmp_path / "inst", signer=_signer(home), extra_hosts=[RP]) as (url, _httpd):
        status, ch = _call(url, f"/cockpit/consent/{_q(rid)}/challenge", {"decision": "allow", "signer_fp": home.phone.fp})
        assert status == 200, ch
        status, out = _call(url, f"/cockpit/consent/{_q(rid)}/answer",
                            {"payload": ch["payload"], **home.phone.assertion(ch["challenge"], origin=PAGE)})
        assert status == 200 and out["decision"] == "allow", out
    assert _queue(home).get(rid)[1] == "allowed"
    _settled(cmd, rid, "allowed")


def test_a_denied_answer_settles_as_denied(home, tmp_path) -> None:
    cmd = Cmd(tmp_path)
    request, _ = _held(home, cmd)
    rid = request.id
    with _serving(tmp_path / "inst", signer=_signer(home), extra_hosts=[RP]) as (url, _httpd):
        status, ch = _call(url, f"/cockpit/consent/{_q(rid)}/challenge", {"decision": "deny", "signer_fp": home.phone.fp})
        assert status == 200, ch
        status, out = _call(url, f"/cockpit/consent/{_q(rid)}/answer",
                            {"payload": ch["payload"], **home.phone.assertion(ch["challenge"], origin=PAGE)})
        assert status == 200 and out["decision"] == "deny", out
    _settled(cmd, rid, "denied")


def test_a_withdrawal_settles_the_request_once(world, tmp_path) -> None:
    cmd = Cmd(tmp_path)
    request, _ = _held(world, cmd)
    assert _queue(world).withdraw(request.id) is True
    assert _queue(world).withdraw(request.id) is False       # already gone: nothing to announce a second time
    _settled(cmd, request.id, "withdrawn")


def _past_queue(world, then: datetime) -> ConsentQueue:
    return ConsentQueue(world.qdir, authority=authority_source(world.root / "cockpit.db", anchor=auth.anchor_path(world.root)),
                        home=h4_home(), clock=lambda: then)


def _filed_long_ago(world, cmd: Cmd, hold: timedelta) -> str:
    """A request filed ten minutes ago with ``hold``, its ping started then (the mark and the held line exist)."""
    _settings_file(world.qdir, "claude_code.json", cmd.argv)
    then = datetime.now(timezone.utc) - timedelta(minutes=10)
    old = _past_queue(world, then)
    rid = old.file(agent="claude_code", agent_session_id="s1", tool_name="Write",
                   tool_input={"file_path": str(world.ws / "e.txt"), "content": "x"}, cwd=str(world.ws), hold=hold).id
    write_origin(world.qdir, ORIGIN)
    claude_code.hold_ping(claude_code.read_settings_file(world.qdir / "claude_code.json"), world.qdir, "claude_code", "w",
                          rid, 60.0, request=old.get(rid)[0])
    return rid


def test_the_expiry_sweep_settles_a_request_nobody_answered(world, tmp_path) -> None:
    cmd = Cmd(tmp_path)
    rid = _filed_long_ago(world, cmd, timedelta(minutes=1))
    assert _queue(world).sweep() == 1
    assert _queue(world).sweep() == 0
    _settled(cmd, rid, "expired")


def test_a_read_that_expires_a_request_settles_it_too(world, tmp_path) -> None:
    cmd = Cmd(tmp_path)
    rid = _filed_long_ago(world, cmd, timedelta(minutes=1))
    assert _queue(world).get(rid)[1] == "expired"
    assert _queue(world).get(rid)[1] == "expired"
    _settled(cmd, rid, "expired")


def test_a_hold_that_ends_undecided_is_announced_as_expired(world, tmp_path) -> None:
    """The hook's own end of a hold (``mark_undecided``): the call was not run, so the row has nothing left to sign."""
    cmd = Cmd(tmp_path)
    request, _ = _held(world, cmd)
    q = _queue(world)
    q_future = ConsentQueue(world.qdir, authority=q._authority, home=h4_home(),
                            clock=lambda: datetime.now(timezone.utc) + timedelta(minutes=6))
    assert q_future.mark_undecided(request.id).state == "undecided"
    _settled(cmd, request.id, "expired")


def test_a_request_the_command_was_never_told_of_settles_silently(world, tmp_path) -> None:
    """Most of the queue's moves (a grant, a request handed to the agent's own dialog) had no held ping: no command starts."""
    cmd = Cmd(tmp_path)
    _settings_file(world.qdir, "claude_code.json", cmd.argv)
    q = _queue(world)
    rid = _file(world)
    assert q.withdraw(rid) is True
    assert cmd.quiet() == []


def test_the_settled_event_is_sent_only_while_the_mark_stands(world, tmp_path) -> None:
    """Exactly once is the mark's removal: a late ruling on the same request (undecided -> denied) announces nothing."""
    cmd = Cmd(tmp_path)
    request, _ = _held(world, cmd)
    q = _queue(world)
    assert q.withdraw(request.id)
    cmd.wait(2)
    assert band_notice.announce_settled(world.qdir, request, "denied") is False
    assert len(cmd.quiet()) == 2


def test_codexs_request_settles_through_codexs_own_command(world, tmp_path) -> None:
    cc, cx = Cmd(tmp_path, "cc"), Cmd(tmp_path, "cx")
    _settings_file(world.qdir, "claude_code.json", cc.argv)
    request, _ = _held(world, cx, agent="codex", name="x.txt")
    assert _queue(world).withdraw(request.id)
    lines = cx.wait(2)
    assert [x["LEVAIN_CONSENT_EVENT"] for x in lines] == ["held", "settled"]
    assert cc.quiet() == []


def test_a_command_that_fails_changes_no_decision_and_raises_into_no_gate(world, tmp_path, monkeypatch) -> None:
    cmd = Cmd(tmp_path)
    request, _ = _held(world, cmd)
    cmd.wait(1)

    def boom(*a, **kw):
        raise RuntimeError("the notify command could not be run")

    monkeypatch.setattr(claude_code, "run_notify_command", boom)
    q = _queue(world)
    assert q.withdraw(request.id) is True                # the move stands and the call returned
    assert q.get(request.id)[1] == "withdrawn"
    assert len(cmd.quiet()) == 1                         # only the held line: the settle was lost, not the decision


def test_a_command_that_exits_nonzero_changes_nothing(world, tmp_path) -> None:
    script = tmp_path / "bad.py"
    script.write_text("import sys\nsys.exit(3)\n")
    _settings_file(world.qdir, "claude_code.json", [sys.executable, str(script)])
    q = _queue(world)
    rid = _file(world)
    r = q.get(rid)[0]
    assert claude_code.hold_ping(claude_code.read_settings_file(world.qdir / "claude_code.json"), world.qdir,
                                 "claude_code", "w", rid, 300.0, request=r) is True
    assert q.withdraw(rid) is True and q.get(rid)[1] == "withdrawn"


def test_a_move_that_rolls_back_announces_nothing(world, tmp_path) -> None:
    """The announcement follows the COMMIT: a transaction that raises after the move leaves the request pending, the mark
    in place and the command unstarted."""
    cmd = Cmd(tmp_path)
    request, _ = _held(world, cmd)
    cmd.wait(1)
    q = _queue(world)
    with pytest.raises(RuntimeError):
        with q._write() as conn:
            assert q._transition(conn, request, "withdrawn", stamp="2026-11-05T00:00:00.000000Z")
            raise RuntimeError("the write failed after the move")
    assert q.get(request.id)[1] == "pending"
    assert cmd.quiet() == [cmd.read()[0]] and band_notice.unmark_held(world.qdir, request.id) is True


def test_the_queues_own_state_gates_a_second_source_of_truth_is_not_added(world, tmp_path) -> None:
    """The mark records that a command was told, not what became of the request: deleting every mark changes no state."""
    cmd = Cmd(tmp_path)
    request, _ = _held(world, cmd)
    shutil.rmtree(world.qdir / band_notice.MARK_DIR)
    q = _queue(world)
    assert q.get(request.id)[1] == "pending" and q.withdraw(request.id) is True
    assert len(cmd.wait(1)) == 1                          # no mark: no settled line, and no harm


# --- 3. the PRESELECT -------------------------------------------------------------------------------------------------
def _open_with(rig: _ConsentRig, query: str, rid: str | None = None) -> list[dict]:
    path = f"/sign/consent/{rig.enc(rid)}"
    return [{"eval": f"(() => {{ window.localStorage.setItem('levain_write_token', {json.dumps(TOK)}); "
                     f"location.href = '{path}{query}'; return 1; }})()"},
            {"until": f"location.pathname === '{path}' && {STATE} && {STATE} !== 'reading' && {STATE}", "ms": 30000}]


_SHEET_PROBE = ("JSON.stringify({ note: (document.getElementById('sheet-preselect') || {}).textContent || null,"
                " miss: (document.getElementById('sheet-pick-miss') || {}).textContent || null,"
                " marked: [...document.querySelectorAll('#sheet-actions [data-choice].sheet-preselected')].map(b => b.dataset.choice),"
                " current: [...document.querySelectorAll('#sheet-actions [aria-current]')].map(b => b.dataset.choice),"
                " choices: [...document.querySelectorAll('#sheet-actions [data-choice]')].map(b => b.dataset.choice),"
                " search: location.search, state: document.getElementById('sheet').dataset.state })")


@needs_browser
@needs_ssh
def test_the_link_preselects_deny_and_the_load_alone_posts_and_signs_nothing(tmp_path: Path, monkeypatch) -> None:
    rig = _ConsentRig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [DESKTOP, rig.authenticator(),
                                                      *_open_with(rig, "?choice=deny"), {"eval": _SHEET_PROBE},
                                                      {"eval": "new Promise(r => setTimeout(r, 1500)).then(() => 1)"}])
    got = json.loads(out["values"][-2])
    assert got["marked"] == ["deny"] and got["current"] == ["deny"] and got["choices"] == ["allow", "deny"], got
    assert got["note"].startswith("Your pick from the link: Deny.") and "Nothing is signed until you do" in got["note"], got
    assert got["search"] == "" and got["state"] == "open", got           # the query is taken out of the bar
    assert [p for p in out["posts"] if "/challenge" in p or "/answer" in p] == [], out["posts"]
    assert out["posts"] == [] and rig.state() == "pending"               # the URL alone sent and signed nothing


@needs_browser
@needs_ssh
def test_a_tap_on_the_preselected_option_runs_the_existing_challenge_answer_and_passkey(tmp_path: Path, monkeypatch) -> None:
    rig = _ConsentRig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *_open_with(rig, "?choice=allow"),
            {"tap": "document.querySelector('#sheet-actions .sheet-preselected')"},
            {"until": f"/^signed/.test({NOTE}) && {NOTE}", "ms": 30000}])
    assert str(out["values"][-1]).startswith("signed (allow) · by " + rig.pk.key.fingerprint), out["values"]
    assert rig.state() == "allowed"
    assert [p.split(" ", 1)[0] for p in out["posts"]] == [f"/cockpit/consent/{rig.enc()}/challenge",
                                                          f"/cockpit/consent/{rig.enc()}/answer"], out["posts"]


@needs_browser
@needs_ssh
@pytest.mark.parametrize("query", ["?choice=maybe", "?choice=ALLOW", "?choice=allow&choice=deny&x=1", "?choice=", "?x=allow"])
def test_an_unknown_value_is_ignored(tmp_path: Path, monkeypatch, query) -> None:
    rig = _ConsentRig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [DESKTOP, rig.authenticator(), *_open_with(rig, query),
                                                      {"eval": _SHEET_PROBE}])
    got = json.loads(out["values"][-1])
    # `?choice=allow&choice=deny` reads the first (URLSearchParams.get): a KNOWN value, so allow is the one preselected
    if query.startswith("?choice=allow&"):
        assert got["marked"] == ["allow"], got
    else:
        assert got["marked"] == [] and got["note"] is None and got["miss"] is None, got
    assert out["posts"] == [] and rig.state() == "pending"


@needs_browser
@needs_ssh
def test_a_preselect_the_sheet_does_not_offer_says_so_and_picks_nothing(tmp_path: Path, monkeypatch) -> None:
    rig = _ConsentRig(tmp_path, monkeypatch, tier="T3")                   # a passkey cannot allow a T3: only Deny is offered
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [DESKTOP, rig.authenticator(), *_open_with(rig, "?choice=allow"),
                                                      {"eval": _SHEET_PROBE}])
    got = json.loads(out["values"][-1])
    assert got["choices"] == ["deny"] and got["marked"] == [] and "not offered for this request" in got["miss"], got
    assert out["posts"] == [] and rig.state() == "pending"


@needs_browser
@needs_ssh
def test_a_request_that_is_not_open_still_says_so_with_a_choice_in_the_link(tmp_path: Path, monkeypatch) -> None:
    rig = _ConsentRig(tmp_path, monkeypatch)
    assert rig.queue.withdraw(rig.rid)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(),
            {"eval": f"(() => {{ window.localStorage.setItem('levain_write_token', {json.dumps(TOK)}); "
                     f"location.href = '/sign/consent/{rig.enc()}?choice=allow'; return 1; }})()"},
            {"until": "document.body.textContent.includes('no longer open') && 1", "ms": 30000},
            {"eval": "document.querySelectorAll('#sheet-actions [data-choice]').length"}])
    assert out["values"][-1] == 0 and rig.state() == "withdrawn"
    assert out["posts"] == []
