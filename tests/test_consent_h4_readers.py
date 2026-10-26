"""H4 (CH-D1c-A): every reader of the environment on the consent core path takes ``environ`` / ``home`` as a REQUIRED
parameter, so a call site that does not pass one raises ``TypeError`` on its first run; the hook entry points bind
``os.environ`` / ``Path.home()`` once, at their top. One call per reader row of the design's H4 table, then one
runtime run of the core with the process environment replaced by a mapping that records every access."""

from __future__ import annotations

import os
import subprocess
from collections.abc import MutableMapping
from pathlib import Path

import pytest

from levain import authority
from levain.consent import policy, queue
from levain.consent.adapters import claude_code as cc
from levain.consent.adapters import codex as cx
from levain.consent.queue import ConsentQueue, authority_source
from levain.consent.record import ExternalGateRequest
from levain.firing import confinement

H = Path("/Users/someone")
ENV: dict[str, str] = {"HOME": str(H)}
REQ = ExternalGateRequest.create(
    agent="claude_code", agent_session_id="s", event_id="e", tool_name="Read", tool_input_digest="jcs:x",
    display="d", cwd="/w", tier="T1", received_at="2026-10-13T20:00:00.000000Z",
    expires_at="2026-10-13T20:10:00.000000Z")
EVENT = {"hook_event_name": "PermissionRequest", "session_id": "s", "cwd": "/w", "tool_name": "Read",
         "tool_input": {"file_path": "/w/x"}}


def _queue(tmp_path):
    return ConsentQueue(tmp_path, authority=authority_source(tmp_path, anchor=None), home=H)


# (row, the call without its required parameter, the parameter it names): each row of the H4 table (and each of
# its threaded intermediates), so a default re-added to any one of them turns its row red
ROWS = [
    ("operator_home", lambda t: cc.operator_home(), "environ"),
    ("global_git_configs", lambda t: cc.global_git_configs(H), "environ"),
    ("_git_envs", lambda t: cc._git_envs(), "environ"),
    ("_ask_git", lambda t: cc._ask_git(str(t), cc._git_budget({})), "environ"),
    ("_git_answer", lambda t: cc._git_answer(t, {}), "environ"),
    ("_under_hooks_path", lambda t: cc._under_hooks_path(t / "x", str(t), {}), "environ"),
    ("_tags_path", lambda t: cc._tags_path(t / "x", str(t), (), (), frozenset()), "environ"),
    ("names_config_dir", lambda t: cc.names_config_dir("d", str(t), H, (), (), frozenset()), "environ"),
    ("cc.targets_agent_config environ", lambda t: cc.targets_agent_config("Write", {"file_path": "x"}, "/w", home=H),
     "environ"),
    ("cc.targets_agent_config home",
     lambda t: cc.targets_agent_config("Write", {"file_path": "x"}, "/w", environ=ENV), "home"),
    ("_settings_files", lambda t: cc._settings_files("/w", H, None), "environ"),
    ("unconfined_state", lambda t: cc.unconfined_state("/w", home=H), "environ"),
    ("unconfined", lambda t: cc.unconfined("/w", home=H), "environ"),
    ("queue_socket_planted", lambda t: cc.queue_socket_planted("/w", home=H), "environ"),
    ("resume_meta_of environ", lambda t: cc.resume_meta_of(EVENT, claude_pid=None), "environ"),
    ("resume_meta_of claude_pid", lambda t: cc.resume_meta_of(EVENT, environ=ENV), "claude_pid"),
    ("cx.targets_agent_config codex_dir",
     lambda t: cx.targets_agent_config("apply_patch", {"command": ""}, "/w", home=H, environ=ENV), "codex_dir"),
    ("cx.targets_agent_config environ",
     lambda t: cx.targets_agent_config("apply_patch", {"command": ""}, "/w", home=H, codex_dir=H / ".codex"),
     "environ"),
    ("policy._resolved", lambda t: policy._resolved("~/x", "/w"), "home"),
    ("policy.grant_refusal", lambda t: policy.grant_refusal({}, REQ, tool_input={}, posture=None, now=None), "home"),
    ("authority.consent_root_problem", lambda t: authority.consent_root_problem("/w"), "home"),
    ("ConsentQueue", lambda t: ConsentQueue(t, authority=authority_source(t, anchor=None)), "home"),
    ("queue.queue_dir_for", lambda t: queue.queue_dir_for(), "environ"),
    ("cc.decide", lambda t: cc.decide(EVENT, _queue(t), cc.AdapterSettings(), home=H), "environ"),
    ("cc.gate", lambda t: cc.gate(EVENT, _queue(t), cc.AdapterSettings(), home=H), "environ"),
    ("cc.permission_context", lambda t: cc.permission_context(t, home=H), "environ"),
    ("cc.protected_paths", lambda t: cc.protected_paths(t, cc.AdapterSettings(), home=H), "environ"),
    ("cc.levain_home_protected", lambda t: cc.levain_home_protected(H, None), "environ"),
    ("confinement.levain_home_spellings", lambda t: confinement.levain_home_spellings(H), "environ"),
    ("confinement.levain_home_base", lambda t: confinement.levain_home_base(H), "environ"),
    ("cx.decide", lambda t: cx.decide(EVENT, _queue(t), cx.AdapterSettings(), environ=ENV), "home"),
]


@pytest.mark.parametrize("row,call,param", ROWS, ids=[r[0] for r in ROWS])
def test_core_reader_without_environ_is_type_error(tmp_path, row, call, param):
    with pytest.raises(TypeError, match=f"missing .*'{param}'"):
        call(tmp_path)


def test_absolute_root_no_longer_expands_a_tilde(tmp_path):
    """A ``~`` cockpit root is no authority (the safe direction); an absolute one is canonicalized as before."""
    assert cc._absolute_root("~/cockpit") is None
    assert cc._absolute_root("~") is None
    assert cc.settings_from({"cockpit_data_root": "~/cockpit"}).cockpit_data_root is None
    assert cc._absolute_root(str(tmp_path)) == os.path.realpath(tmp_path)


def test_queue_dir_for_reads_only_the_environ_it_is_given(tmp_path):
    """``queue_dir_for`` is :func:`default_queue_dir` over the mapping it is handed, never this process's."""
    assert queue.queue_dir_for({"HOME": str(tmp_path)}) == (tmp_path / ".levain" / "consent").resolve()
    assert queue.queue_dir_for({"HOME": str(tmp_path), "LEVAIN_HOME": str(tmp_path / "lh")}) \
        == (tmp_path / "lh" / "consent").resolve()
    assert queue.queue_dir_for({"HOME": str(tmp_path), "LEVAIN_HOME": "~/lh"}) == (tmp_path / "lh" / "consent").resolve()
    assert queue.queue_dir_for(dict(os.environ)) == queue.default_queue_dir()


def test_grant_tilde_target_expands_under_the_queue_home_not_home_env(tmp_path, monkeypatch):
    """``policy._resolved`` joins a ``~`` target to the ``home`` it is handed, as ``os.path.expanduser`` would have
    to ``$HOME``."""
    (tmp_path / "h").mkdir()
    monkeypatch.setenv("HOME", "/nonexistent-elsewhere")
    assert policy._resolved("~/x", "/w", home=tmp_path / "h") == str(tmp_path / "h" / "x")
    assert policy._resolved("~", "/w", home=tmp_path / "h") == str(tmp_path / "h")
    assert authority.consent_root_problem(str(tmp_path), home=tmp_path / "h") is not None   # above that home
    assert authority.consent_root_problem(str(tmp_path / "h" / "p"), home=tmp_path / "h") is None


class _Recording(MutableMapping):
    """``os.environ`` stand-in: records every access and raises, so a reader that still reads the process
    environment is caught even when its caller swallows the raise."""

    def __init__(self):
        self.seen: list[str] = []

    def _hit(self, what):
        self.seen.append(what)
        raise RuntimeError(f"the process environment was read: {what}")

    def __getitem__(self, k):
        self._hit(f"[{k}]")

    def __setitem__(self, k, v):
        self._hit(f"set {k}")

    def __delitem__(self, k):
        self._hit(f"del {k}")

    def __iter__(self):
        self._hit("iter")

    def __len__(self):
        self._hit("len")

    def __contains__(self, k):
        self._hit(f"in {k}")

    def get(self, k, default=None):
        self._hit(f"get {k}")

    def copy(self):
        self._hit("copy")


def _core_call(qdir, repo, environ, home, monkeypatch=None, recording=None):
    with cc.permission_context(qdir, environ=environ, home=home) as pc:
        if recording is not None:
            monkeypatch.setattr(os, "environ", recording)
        try:
            out = cc.decide({"hook_event_name": "PermissionRequest", "session_id": "0f0e0d0c-0b0a-4908-8706-050403020100",
                             "cwd": str(repo), "tool_name": "Write",
                             "tool_input": {"file_path": str(repo / "notes.txt"), "content": "x"},
                             "tool_use_id": "toolu_1"},
                            pc.queue, pc.settings, environ=environ, home=home, max_hold=1.0,
                            policy=lambda *a, **kw: None, idle=lambda: None, still_away=pc.recheck)
        finally:
            if recording is not None:
                monkeypatch.undo()
        return out, pc.queue


def test_core_reads_no_process_environment(tmp_path, monkeypatch):
    """The core path (``permission_context`` + ``decide``) for a Write of a plain file inside a scratch git
    repository (not a config path, so the classifier asks git where hooks run) reads nothing of the process
    environment: every access to the replaced ``os.environ`` is recorded, raised or swallowed."""
    import site
    import tempfile

    cc.import_site_dirs()       # the stdlib caches read the environment once, at first use (design §15)
    tempfile.gettempdir()
    site.getusersitepackages()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    qdir = tmp_path / "q"
    qdir.mkdir(mode=0o700)
    environ = {"HOME": str(tmp_path / "h"), "LEVAIN_HOME": str(tmp_path / "lh"), "LEVAIN_UNCONFINED": "1",
               "CLAUDE_PID": "1", "CLAUDE_PROJECT_DIR": str(tmp_path / "p"), "CLAUDE_CONFIG_DIR": str(tmp_path / "c"),
               "CODEX_HOME": str(tmp_path / "x"), "XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    recording = _Recording()
    _core_call(qdir, repo, environ, tmp_path / "h", monkeypatch, recording)
    assert recording.seen == [], recording.seen


def test_the_levain_home_protected_is_named_by_the_environ_passed(tmp_path, monkeypatch):
    """``protected_paths`` (and the floor's own derivation under it) takes ``LEVAIN_HOME`` from the mapping it is
    handed: the process's ``$LEVAIN_HOME`` names some other directory and is neither read nor protected."""
    bound, ambient = tmp_path / "bound", tmp_path / "ambient"
    monkeypatch.setenv("LEVAIN_HOME", str(ambient))
    environ = {"HOME": str(tmp_path / "h"), "LEVAIN_HOME": str(bound)}
    got = cc.protected_paths(tmp_path / "q", cc.AdapterSettings(), home=tmp_path / "h", environ=environ)
    assert bound in got and ambient not in got
    assert confinement.levain_home_base(tmp_path / "h", environ=environ) == bound
    assert confinement.levain_home_base(tmp_path / "h", environ={}) == tmp_path / "h" / ".levain"


def test_core_resume_record_comes_from_the_environ_passed(tmp_path):
    """The core runs to completion on a handed ``environ`` (no decision: no policy answers, no hold), and the
    resume record a request is filed with names that mapping's ``CLAUDE_PROJECT_DIR``, config directory and
    entrypoint; an explicit ``claude_pid`` replaces ``CLAUDE_PID``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    qdir = tmp_path / "q"
    qdir.mkdir(mode=0o700)
    environ = {"HOME": str(tmp_path / "h"), "CLAUDE_PROJECT_DIR": str(tmp_path / "p"),
               "CLAUDE_CONFIG_DIR": str(tmp_path / "c"), "CLAUDE_CODE_ENTRYPOINT": "sdk-cli", "CLAUDE_PID": "x"}
    out, _ = _core_call(qdir, repo, environ, tmp_path / "h")
    assert out is None
    event = {"session_id": "0f0e0d0c-0b0a-4908-8706-050403020100", "cwd": str(repo)}
    meta = cc.resume_meta_of(event, environ=environ, claude_pid=None)
    assert (meta["cwd"], meta["config_dir"], meta["invocation"], meta["claude_pid"]) \
        == (str(tmp_path / "p"), str(tmp_path / "c"), "-p", None)
    assert cc.resume_meta_of(event, environ={}, claude_pid=os.getpid())["claude_pid"] == os.getpid()
