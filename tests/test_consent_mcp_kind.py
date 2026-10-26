"""The consent-mcp grant kind: a consent grant that declares its MCP tools' fields (ruling #3 (b), Phill 10-17; head
ruling 1017+4 12:11: a new KIND, never an optional field on ``consent``), written by ``levain consent grant create
--path-field / --mcp-field``. The decision cases are from the 1017+12 lane M RUN (project_memory/1017-mcpkind/
RUN_REPORT.md: real Claude Code 2.1.302 and the reference filesystem server, a write inside the workspace answered,
one outside it and one with an extra input key not; the same log read by the 0.11.5 reader holds the grant inert and
still answers its plain consent grant) and the 1017+4 hook-seam RUN (project_memory/1017-mcpgrant/PATHFIELD_RUN/)."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType

import pytest

import levain.authority as auth
from levain.authority import ReplayCache, anchor_path
from levain.cli import main
from levain.cockpit.consent import utc_stamp
from levain.cockpit.firejournal import CockpitDB
from levain.consent import policy
from levain.consent.queue import ConsentQueue, authority_source
from tests._h4 import h4_home

T = "mcp__fsw__write_file"


def _key(d: Path, name: str) -> tuple[Path, str, str]:
    path = d / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(path)], check=True,
                   capture_output=True, timeout=30)
    pub = " ".join((d / f"{name}.pub").read_text().split()[:2])
    fp = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(path)], check=True, capture_output=True,
                        text=True).stdout.split()[1]
    return path, pub, fp


class Scratch:
    """A reported cockpit whose anchored log enrols one laptop-presence key, a workspace (with a symlink out of it),
    a directory outside it, and the queue the hook builds over the cockpit."""

    def __init__(self, tmp_path: Path) -> None:
        keys = tmp_path / "keys"
        keys.mkdir()
        self.key, pub, self.fp = _key(keys, "laptop")
        self.root = tmp_path / "cockpit"
        self.root.mkdir()
        CockpitDB(self.root).set_install_class("reported")
        self.ws = Path(os.path.realpath(tmp_path / "proj"))
        (self.ws / "sub").mkdir(parents=True)
        self.outside = Path(os.path.realpath(tmp_path / "outside"))
        self.outside.mkdir()
        os.symlink(self.outside, self.ws / "escape")
        self.link({"op": "enrol", "fingerprint": self.fp, "public_key": pub, "class": "laptop-presence",
                   "custody": "passphrase"})
        qdir = tmp_path / "home" / "consent"
        qdir.mkdir(parents=True, mode=0o700)
        self.queue = ConsentQueue(qdir, authority=authority_source(self.root / "cockpit.db",
                                                                    anchor=anchor_path(self.root)), home=h4_home())

    def sign(self, m: bytes) -> str:
        return subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(self.key), "-n", auth.NAMESPACE],
                              input=m, capture_output=True, check=True).stdout.decode()

    def conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.root / "cockpit.db", isolation_level=None)

    def link(self, op, *, at: datetime | None = None):
        conn = self.conn()
        try:
            return auth.sign_and_append(conn, op, signer_fp=self.fp, sign=self.sign,
                                        clock=lambda: at or datetime.now(timezone.utc), cache=ReplayCache(),
                                        anchor=anchor_path(self.root))
        finally:
            conn.close()

    def state(self) -> auth.AuthoritySnapshot:
        conn = self.conn()
        try:
            return auth.snapshot(conn, cache=ReplayCache())
        finally:
            conn.close()

    def create(self, *extra: str, tool: str = T, gid: str = "declared-write") -> int:
        return main(["consent", "grant", "create", "--data-root", str(self.root), "--agent", "claude_code",
                     "--tool", tool, "--workspace", str(self.ws), "--posture", "default", "--expires", "2h",
                     "--id", gid, "--signing-key", str(self.key), *extra])

    def ask(self, tool_input, *, tool: str = T, cwd: str | None = None):
        cwd = str(self.ws) if cwd is None else cwd
        r = self.queue.file(agent="claude_code", agent_session_id="s", tool_name=tool, tool_input=tool_input,
                            cwd=cwd, hold=timedelta(minutes=5))
        return policy.guarded_policy(r, self.queue, posture="default", tool_input=tool_input)


@pytest.fixture
def scratch(tmp_path):
    return Scratch(tmp_path)


@pytest.fixture
def declared(scratch, capsys):
    rc = scratch.create("--path-field", f"{T}:path", "--mcp-field", f"{T}:content=str")
    out = capsys.readouterr()
    assert rc == 0, out.err
    assert f"declared fields (consent-mcp): {T} content=str,path=path" in out.out
    return scratch


def test_a_declared_write_tool_is_answered_inside_the_workspace(declared):
    ws = declared.ws
    for tool_input in ({"path": str(ws / "notes.txt"), "content": "hi"},
                       {"path": "sub/notes.txt", "content": "hi"},         # relative, joined to the cwd
                       {"path": str(ws / "empty.txt")}):                    # a subset of the declared fields
        d = declared.ask(tool_input)
        assert d is not None and (d.decision, d.via, d.grant_id) == ("allow", "grant", "declared-write"), tool_input


def test_a_declared_write_tool_is_not_answered_outside_or_off_its_declaration(declared):
    ws, out = declared.ws, declared.outside
    for tool_input, kw in (({"path": str(out / "notes.txt"), "content": "hi"}, {}),
                           ({"path": "../outside/notes.txt", "content": "hi"}, {}),
                           ({"path": str(ws / "escape" / "notes.txt"), "content": "hi"}, {}),
                           ({"path": str(ws / "notes.txt"), "content": "hi", "mode": "0777"}, {}),      # extra key
                           ({"path": str(ws / "n.txt"), "content": "hi", "filepath": "/etc/hosts"}, {}),
                           ({"path": str(ws / "notes.txt"), "content": 7}, {}),                         # wrong kind
                           ({"path": [str(ws / "notes.txt")], "content": "hi"}, {}),                    # wrong kind
                           ("x", {}),                                                                   # not an object
                           ({"path": str(ws / "notes.txt"), "content": "hi"}, {"cwd": str(out)}),
                           ({"path": str(ws / "notes.txt"), "edits": []}, {"tool": "mcp__fsw__edit_file"})):
        assert declared.ask(tool_input, **kw) is None, (tool_input, kw)


def test_a_str_field_is_never_read_as_a_path(declared):
    # the operator's signed declaration: a field declared str is not a target (a known edge, stated in --help)
    d = declared.ask({"path": str(declared.ws / "notes.txt"), "content": "/etc/hosts"})
    assert d is not None and d.decision == "allow"


def test_the_named_path_fields_are_still_read_whatever_the_declaration(declared):
    # never weaker than an undeclared tool: a field in MCP_PATH_FIELDS declared str is still a target
    rc = declared.create("--path-field", f"{T}:path", "--mcp-field", f"{T}:destination=str", gid="g2")
    assert rc == 0
    d = declared.ask({"path": str(declared.ws / "a"), "destination": str(declared.outside / "b")})
    assert d is None


def test_declared_kinds(scratch):
    tool = "mcp__srv__op"
    rc = scratch.create("--path-field", f"{tool}:file", "--mcp-field", f"{tool}:files=paths",
                        "--mcp-field", f"{tool}:tags=strs", "--mcp-field", f"{tool}:n=int",
                        "--mcp-field", f"{tool}:dry=bool", tool=tool, gid="kinds")
    assert rc == 0
    ws = str(scratch.ws)
    ok = {"file": f"{ws}/a", "files": [f"{ws}/b", "c"], "tags": [], "n": 3, "dry": False}
    assert scratch.ask(ok, tool=tool) is not None
    # `file` and `files` are not among the fields levain reads for every tool: only the declaration bounds them
    for bad in ({"file": str(scratch.outside / "a")}, {"files": []}, {"files": [f"{ws}/b", 1]},
                {"files": [f"{ws}/b", str(scratch.outside)]},
                {"tags": "x"}, {"n": True}, {"n": 1.5}, {"dry": 0}, {"file": None}):
        assert scratch.ask(bad, tool=tool) is None, bad


def test_create_refuses_an_undeclared_write_tool_and_a_bad_declaration(scratch, capsys):
    cases = (((), "mcp__fsw__edit_file", "declare every field it takes: --path-field mcp__fsw__edit_file:FIELD"),
             (("--path-field", f"{T}:path"), "Bash", "which is not among --tool"),
             (("--path-field", "mcp__fsw__move_file:source"), T, "which is not among --tool"),
             (("--mcp-field", f"{T}:path=dir"), T, "must be one of bool, int, path, paths, str, strs"),
             (("--path-field", f"{T}:path", "--mcp-field", f"{T}:path=str"), T, "is declared both path and str"),
             (("--mcp-field", f"{T}:path"), T, "is not TOOL:FIELD=KIND"),
             (("--path-field", f"{T}:"), T, "is not TOOL:FIELD"))
    for extra, tool, said in cases:
        assert scratch.create(*extra, tool=tool, gid="refused") == 1, extra
        assert said in capsys.readouterr().err, extra
    assert "refused" not in scratch.state().policy.grants


def test_a_refused_shell_or_malformed_mcp_name_is_not_told_to_declare_fields(scratch, capsys):
    """A declaration can never make a shell, or a name that is not mcp__<server>__<tool>, grantable, so the refusal
    names no --path-field/--mcp-field remedy for it (L3 r2 codex LOW: following that advice still fails)."""
    for tool in ("mcp__shell__exec_command", "mcp__terminal__Write_Stdin", "mcp__x__bash", "mcp____x"):
        assert scratch.create(tool=tool, gid="refused") == 1, tool
        err = capsys.readouterr().err
        assert "grant refused:" in err, tool
        assert "declare every field" not in err, (tool, err)
    assert "refused" not in scratch.state().policy.grants


def test_list_prints_the_declarations(declared, capsys):
    assert main(["consent", "grant", "list", "--data-root", str(declared.root)]) == 0
    out = capsys.readouterr().out
    assert "answers nothing" not in out
    assert f"  declared fields (consent-mcp): {T} content=str,path=path" in out


def test_an_undeclared_grant_is_recorded_as_before(scratch):
    """No declaration: the grant carries exactly the seven fields levain 0.11.4 replays (RUN: 0.11.4 listed such a
    log and its hook answered from it)."""
    assert scratch.create(tool="mcp__fsw__read_text_file", gid="undeclared-read") == 0
    conn = scratch.conn()
    try:
        record = json.loads(conn.execute("SELECT record FROM authority_log WHERE seq = 2").fetchone()[0])
    finally:
        conn.close()
    grant = record["grant"]
    assert sorted(grant) == ["agent", "expires_at", "id", "kind", "postures", "tools", "workspace"]
    assert grant["kind"] == "consent"
    from levain.consent.grants import list_grants
    got = list_grants(scratch.root, now=datetime.now(timezone.utc))
    assert [(g.id, g.kind, g.mcp_fields) for g in got] == [("undeclared-read", "consent", {})]


def test_a_declared_grant_is_its_own_kind_with_a_closed_field_set(declared):
    conn = declared.conn()
    try:
        record = json.loads(conn.execute("SELECT record FROM authority_log WHERE seq = 2").fetchone()[0])
    finally:
        conn.close()
    grant = record["grant"]
    assert grant["kind"] == "consent-mcp"
    assert sorted(grant) == ["agent", "expires_at", "id", "kind", "mcp_fields", "postures", "tools", "workspace"]
    assert grant["mcp_fields"] == {T: {"content": "str", "path": "path"}}


def test_renew_and_loosen_keep_a_declared_grant(declared):
    t = datetime.now(timezone.utc)
    declared.link({"op": "renew", "grant_id": "declared-write", "expires_at": utc_stamp(t + timedelta(hours=3)),
                   "signed_at": utc_stamp(t)}, at=t)
    pol = auth._thaw(declared.state().policy.as_dict())
    pol["grants"]["declared-write"]["postures"] = ["acceptEdits", "default"]
    declared.link({"op": "loosen", "policy": pol})
    pol = auth._thaw(declared.state().policy.as_dict())
    pol["grants"]["declared-write"]["mcp_fields"][T]["mode"] = "str"
    declared.link({"op": "loosen", "policy": pol})
    g = declared.state().policy.grants["declared-write"]
    assert auth._thaw(g["mcp_fields"]) == {T: {"content": "str", "mode": "str", "path": "path"}}
    assert declared.ask({"path": str(declared.ws / "a"), "content": "x", "mode": "0644"}) is not None
    pol = auth._thaw(declared.state().policy.as_dict())
    pol["grants"]["declared-write"]["tools"] = ["mcp__fsw__edit_file", T]
    with pytest.raises(auth.AuthorityError, match="does not know to be read-only"):
        declared.link({"op": "loosen", "policy": pol})


def _consent(**over):
    g = {"id": "g", "kind": "consent-mcp", "agent": "claude_code", "tools": [T], "workspace": "/w",
         "postures": ["default"], "expires_at": utc_stamp(datetime(2026, 10, 20, tzinfo=timezone.utc)),
         "mcp_fields": {T: {"path": "path"}}}
    g.update(over)
    return g


@pytest.mark.parametrize("mcp_fields", [
    {}, [], {"mcp__fsw__other": {"path": "path"}}, {"Write": {"path": "path"}}, {T: {}}, {T: ["path"]},
    {T: {"path": "dir"}}, {T: {"9path": "path"}}, {T: {"a b": "str"}}, {T: {f"f{i}": "str" for i in range(33)}},
])
def test_the_replay_refuses_a_malformed_declaration(mcp_fields):
    with pytest.raises(auth.AuthorityError, match="mcp_fields"):
        auth._validate_grant(_consent(mcp_fields=mcp_fields, tools=sorted({T, "Write"})), "grant")


def test_a_declaration_change_is_classified(scratch):
    s = auth.StandingPolicy
    base = _consent(tools=["mcp__fs__read_text_file", T],
                    mcp_fields={T: {"path": "path"}, "mcp__fs__read_text_file": {"path": "path"}})
    policy_of = lambda g: s.from_dict({"raise_to": {}, "timeouts": {}, "grants": {"g": g}})   # noqa: E731
    later = dict(base, expires_at=utc_stamp(datetime(2026, 10, 21, tzinfo=timezone.utc)))
    assert auth.classify(policy_of(base), policy_of(later)) == auth.RENEW
    narrower = dict(base, tools=[T], mcp_fields={T: {"path": "path"}})
    assert auth.classify(policy_of(base), policy_of(narrower)) == auth.TIGHTEN
    # the listed read tool kept, its declaration dropped: it falls back to the built-in schema, which takes more
    dropped = dict(base, mcp_fields={T: {"path": "path"}})
    assert auth.classify(policy_of(base), policy_of(dropped)) == auth.LOOSEN


def test_the_consent_kind_stays_closed_and_the_new_kind_needs_its_declarations():
    plain = dict(_consent(), kind="consent")
    with pytest.raises(auth.AuthorityError, match=r"\(a consent grant\) must carry exactly"):
        auth._validate_grant(plain, "grant")
    bare = {k: v for k, v in _consent().items() if k != "mcp_fields"}
    with pytest.raises(auth.AuthorityError, match=r"\(a consent-mcp grant\) must carry exactly"):
        auth._validate_grant(bare, "grant")
    assert auth._validate_grant(_consent(), "grant")["kind"] == "consent-mcp"


def _grants_main(scratch, *argv: str) -> int:
    return main(["consent", "grant", "create", "--data-root", str(scratch.root), "--agent", "claude_code",
                 "--workspace", str(scratch.ws), "--posture", "default", "--expires", "2h",
                 "--signing-key", str(scratch.key), *argv])


def test_a_declared_grant_still_refuses_a_shell_and_a_tool_levain_does_not_know(scratch, capsys):
    for other, said in (("Bash", "names the shell tool"), ("Task", "is not a tool levain knows for claude_code")):
        rc = _grants_main(scratch, "--id", "mixed", "--tool", T, "--tool", other, "--path-field", f"{T}:path")
        assert rc == 1 and said in capsys.readouterr().err, other
    assert "mixed" not in scratch.state().policy.grants


def test_a_declared_grant_answers_its_built_in_tools_as_a_consent_grant_does(scratch):
    assert _grants_main(scratch, "--id", "mixed", "--tool", T, "--tool", "Write", "--path-field", f"{T}:path") == 0
    ws, out = scratch.ws, scratch.outside
    assert scratch.ask({"file_path": str(ws / "a.txt"), "content": "x"}, tool="Write") is not None
    assert scratch.ask({"file_path": str(out / "a.txt"), "content": "x"}, tool="Write") is None
    assert scratch.ask({"path": str(ws / "a.txt")}) is not None


def test_a_listed_read_tool_left_undeclared_in_a_declared_grant_keeps_its_schema(scratch):
    read = "mcp__fsw__read_file"
    assert _grants_main(scratch, "--id", "mixed", "--tool", T, "--tool", read, "--path-field", f"{T}:path") == 0
    assert scratch.ask({"path": str(scratch.ws / "README")}, tool=read) is not None
    # a foreign read_file taking its path in another field: not the schema levain knows (RUN #3(c) readforeign)
    assert scratch.ask({"filepath": "/etc/hosts"}, tool=read) is None


def test_a_reader_that_does_not_know_the_kind_holds_it_inert(declared, monkeypatch):
    """RUN (ii): the 0.11.5 reader (seat/1017-12-rel bbd1537, real Claude Code) replays a log holding a consent-mcp
    grant, lists it as a kind it does not know, answers nothing with it, and still answers the log's plain consent
    grant."""
    read = "mcp__fsw__read_text_file"
    assert declared.create(tool=read, gid="reads") == 0
    assert declared.ask({"path": str(declared.ws / "a"), "content": "x"}) is not None      # this build: answered
    monkeypatch.setattr(auth, "_GRANT_KINDS", MappingProxyType(
        {k: v for k, v in auth._GRANT_KINDS.items() if k != auth.CONSENT_MCP_GRANT_KIND}))
    g = declared.state().policy.grants["declared-write"]
    assert auth.inert_grant(g) and not auth.consent_grant(g)
    assert declared.ask({"path": str(declared.ws / "b"), "content": "x"}) is None
    assert declared.ask({"path": str(declared.ws / "README")}, tool=read) is not None


def test_a_change_of_kind_is_a_loosen():
    s = auth.StandingPolicy
    policy_of = lambda g: s.from_dict({"raise_to": {}, "timeouts": {}, "grants": {"g": g}})   # noqa: E731
    plain = {k: v for k, v in _consent(tools=["mcp__fs__read_text_file"]).items() if k != "mcp_fields"}
    plain["kind"] = "consent"
    declared = dict(_consent(tools=["mcp__fs__read_text_file"]),
                    mcp_fields={"mcp__fs__read_text_file": {"path": "path"}})
    assert auth.classify(policy_of(plain), policy_of(declared)) == auth.LOOSEN
    # narrower in effect, but across kinds nothing is provably tighter: fail closed
    assert auth.classify(policy_of(declared), policy_of(plain)) == auth.LOOSEN


# L3 r1 (codex HIGH): an MCP tool whose own name is a shell name (mcp__shell__exec_command) was matched against
# CONSENT_SHELL_TOOLS by its full name, so a consent-mcp grant declaring it `command=str` answered `rm -rf`.
_SHELL_MCP = sorted({f"mcp__{server}__{case(name)}" for name in auth.CONSENT_SHELL_TOOLS
                     for server in ("shell", "x") for case in (str.lower, str.upper, str.title)})


def test_a_declared_mcp_shell_tool_is_refused_at_create(scratch, capsys):
    tool = "mcp__shell__exec_command"
    assert scratch.create("--mcp-field", f"{tool}:command=str", tool=tool, gid="mcp-shell") == 1
    assert "names the shell tool" in capsys.readouterr().err
    assert "mcp-shell" not in scratch.state().policy.grants


def test_a_declared_mcp_shell_tool_signed_before_the_rule_answers_nothing(scratch, monkeypatch):
    tool = "mcp__shell__exec_command"
    with monkeypatch.context() as m:
        m.setattr(auth, "_consent_mcp_tool_check", lambda grants, after="": None)
        assert scratch.create("--mcp-field", f"{tool}:command=str", tool=tool, gid="mcp-shell") == 0
    assert "mcp-shell" in scratch.state().policy.grants
    assert scratch.ask({"command": "rm -rf /tmp/x"}, tool=tool) is None


@pytest.mark.parametrize("tool", _SHELL_MCP)
def test_every_shell_name_as_an_mcp_leaf_is_refused_at_append_and_at_decision(tool):
    grant = _consent(tools=[tool], mcp_fields={tool: {"command": "str"}})
    problem = auth.consent_grant_tool_problem(grant, tool)
    assert problem is not None and "names the shell tool" in problem
    assert "names the shell tool" in (auth.consent_tool_problem("claude_code", tool) or "")
    with pytest.raises(auth.AuthorityError, match="names the shell tool"):
        auth._consent_mcp_tool_check([grant])
    with pytest.raises(auth.AuthorityError, match="names the shell tool"):
        auth._consent_tool_check([{k: v for k, v in grant.items() if k != "mcp_fields"}])


@pytest.mark.parametrize("tool", ["mcp__a__b__exec_command", "MCP__shell__bash", "mcp__shell__Bash"])
def test_a_shell_leaf_behind_a_server_name_with_a_double_underscore_or_any_case_is_refused(tool):
    assert "names the shell tool" in (auth.consent_grant_tool_problem(
        _consent(tools=[tool], mcp_fields={tool: {"command": "str"}}), tool) or "")


def test_an_mcp_tool_whose_name_only_contains_a_shell_name_is_not_a_shell():
    for tool in ("mcp__fsw__bash_history_reader", "mcp__shell__run"):
        assert not auth._consent_shell_name(tool), tool


@pytest.mark.parametrize("tool", ["mcp__shell__exec_command", "mcp__terminal__write_stdin", "mcp__x__Bash"])
def test_a_loosen_to_a_declared_mcp_shell_tool_is_refused(declared, tool):
    pol = auth._thaw(declared.state().policy.as_dict())
    g = pol["grants"]["declared-write"]
    g["tools"] = sorted({*g["tools"], tool})
    g["mcp_fields"] = {**g["mcp_fields"], tool: {"command": "str"}}
    with pytest.raises(auth.AuthorityError, match="names the shell tool"):
        declared.link({"op": "loosen", "policy": pol})


def test_a_renew_of_a_declared_mcp_shell_tool_signed_before_the_rule_is_refused(scratch, monkeypatch):
    tool = "mcp__terminal__write_stdin"
    with monkeypatch.context() as m:
        m.setattr(auth, "_consent_mcp_tool_check", lambda grants, after="": None)
        assert scratch.create("--mcp-field", f"{tool}:chars=str", tool=tool, gid="mcp-shell") == 0
    t = datetime.now(timezone.utc)
    with pytest.raises(auth.AuthorityError, match="names the shell tool"):
        scratch.link({"op": "renew", "grant_id": "mcp-shell", "expires_at": utc_stamp(t + timedelta(hours=3)),
                      "signed_at": utc_stamp(t)}, at=t)
