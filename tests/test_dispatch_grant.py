"""F4 W2-c1: the typed contract grant decided against a REAL git worktree in tmp, from the recorded hook payloads.

The contract is resolved from a real repo (``make_repo``) at its base; the disk then moves on the way a background
process could move it (a symlinked directory, a symlinked file, a hard link), and the no-follow walk is what sees
it. The payload inputs below are the ones recorded in 1019-dispatch legB (Claude Code ``Write``) and 1017-presence
(Codex ``apply_patch``), with the path rewritten onto the tmp root. The patch cases are the ones the W2-c1 run fed
the installed Codex's own ``apply_patch`` (``project_memory/1020-f11/w2c_run/RUN.txt``): each one Codex wrote a
file for under a name ``patch_targets`` does not read back is refused here.
"""
from __future__ import annotations

import hashlib
import os
import unicodedata

import pytest

from levain.cockpit.consent import ConsentError, value_sha256
from levain.contracts.canon import canonical_bytes
from levain.dispatch.contract import NEW, PathEntry
from levain.dispatch.grant import (
    Allow, Queue, grant_decision, grant_paths, no_follow_check, path_bound_digest, write_list_match,
)
from tests.test_dispatch_contract import make_repo, resolve, submission

WRITE = ["src/a.py", "src/b.py", "src/new.py", "src/sub/deep.py", "docs/x.md", "src/run.sh"]
# recorded inputs: 1019-dispatch/legB/raw/hook_payloads.jsonl and 1017-presence/raw/codex_PermissionRequest_payload.json
CC_WRITE = {"file_path": "/private/tmp/levain-1019-14-b/inst/.claude/agents/probe2.md", "content": "probe two"}
CX_PATCH = {"command": "*** Begin Patch\n*** Add File: a1.txt\n+hi\n*** End Patch"}
UPDATE = "*** Begin Patch\n*** Update File: src/a.py\n@@\n-a = 1\n+a = 2\n*** End Patch"


@pytest.fixture
def world(tmp_path):
    """(contract, root): a contract signed over WRITE, then the disk moved on after signing."""
    repo, base = make_repo(tmp_path)
    contract = resolve(submission(repo, base, read=["src/a.py"], write=WRITE, grant=["Edit", "Write", "apply_patch"]))
    os.rename(repo / "docs", repo / "docs_real")
    os.symlink("docs_real", repo / "docs")
    os.unlink(repo / "src/run.sh")
    os.symlink("/etc/hosts", repo / "src/run.sh")
    os.link(repo / "src/b.py", repo / "src/b_twin.py")
    return contract, str(repo)


def cc(root: str, rel: str) -> dict:
    return {**CC_WRITE, "file_path": f"{root}/{rel}"}


def cx(rel: str) -> dict:
    return {"command": CX_PATCH["command"].replace("a1.txt", rel)}


def open_fds() -> int:
    return len(os.listdir("/dev/fd"))


# --- the pb1 digest ----------------------------------------------------------------------------------------------

def test_the_digest_is_the_canonical_hash_of_tool_paths_and_input():
    paths = ("/r/src/a.py", "/r/src/b.py")
    body = {"tool": "apply_patch", "paths": list(paths), "input": value_sha256(CX_PATCH)}
    want = "pb1:" + hashlib.sha256(canonical_bytes(body)).hexdigest()
    assert path_bound_digest("apply_patch", paths, CX_PATCH) == want
    assert len(want) == 4 + 64


def test_the_digest_binds_each_part():
    d = path_bound_digest("Write", ["/r/a"], CC_WRITE)
    assert d != path_bound_digest("Write", ["/r/b"], CC_WRITE)
    assert d != path_bound_digest("Edit", ["/r/a"], CC_WRITE)
    assert d != path_bound_digest("Write", ["/r/a"], {**CC_WRITE, "content": "probe three"})
    assert d != path_bound_digest("Write", ["/r/a", "/r/b"], CC_WRITE)


@pytest.mark.parametrize("paths", [["/r/b", "/r/a"], ["/r/a", "/r/a"], [1]])
def test_the_digest_takes_one_spelling_of_the_path_list(paths):
    with pytest.raises(ValueError):
        path_bound_digest("Write", paths, CC_WRITE)


def test_an_input_outside_jcs_has_no_digest():
    with pytest.raises(ConsentError):
        path_bound_digest("Write", ["/r/a"], {"file_path": "/r/a", "content": 1.5})


# --- grant_paths: the total parse --------------------------------------------------------------------------------

def test_the_recorded_payloads_parse():
    assert grant_paths("claude_code", "Write", CC_WRITE, "/x") == (CC_WRITE["file_path"],)
    assert grant_paths("codex", "apply_patch", CX_PATCH, "/w") == ("/w/a1.txt",)


def test_move_to_gives_both_paths_sorted_each_once():
    patch = UPDATE.replace("src/a.py\n", "src/a.py\n*** Move to: src/0.py\n") + "\n*** Delete File: src/a.py"
    assert grant_paths("codex", "apply_patch", {"command": patch}, "/w") == ("/w/src/0.py", "/w/src/a.py")


@pytest.mark.parametrize("agent,tool,tool_input", [
    ("codex", "Write", {"file_path": "/w/a", "content": ""}),          # a tool of the other agent
    ("claude_code", "apply_patch", CX_PATCH),
    ("claude_code", "MultiEdit", {"file_path": "/w/a", "edits": []}),   # a file tool, never grantable
    ("claude_code", "Bash", {"command": "true"}),
    ("nobody", "Write", CC_WRITE),
    ("claude_code", "Write", {"file_path": "a.py", "content": ""}),     # relative
    ("claude_code", "Write", {"content": ""}),
    ("claude_code", "Write", {"file_path": 7, "content": ""}),
    ("claude_code", "Write", {**CC_WRITE, "notebook_path": "/w/n.ipynb"}),  # a key whose meaning is not read
    ("claude_code", "Edit", {"file_path": "/w/a", "old_string": "a", "new_string": "b", "extra": 1}),
    ("claude_code", "Write", {"file_path": "/w/a", "content": 1.5}),    # outside JCS: no digest can bind it
    ("codex", "apply_patch", {"command": ["apply_patch", CX_PATCH["command"]]}),
    ("codex", "apply_patch", {**CX_PATCH, "workdir": "/elsewhere"}),
    ("codex", "apply_patch", "not a mapping"),
])
def test_anything_not_read_whole_is_none(agent, tool, tool_input):
    assert grant_paths(agent, tool, tool_input, "/w") is None


def test_a_patch_needs_an_absolute_root():
    assert grant_paths("codex", "apply_patch", CX_PATCH, "w") is None


B, E = "*** Begin Patch", "*** End Patch"


# each patch Codex 0.162.1 applied (or failed on) in the W2-c1 run where its reading and Levain's could differ
@pytest.mark.parametrize("patch", [
    f"{B}\n*** Add File: r1.txt\r*** Add File: r2.txt\n+hi\n{E}\n",    # Codex wrote one file named across the \r
    f"{B}\n*** Add File: r3\rx.txt\n+hi\n{E}\n",
    f"{B}\n*** Add File: u1 x.txt\n+hi\n{E}\n",                   # Codex wrote 'u1 x.txt', not 'u1'
    f"{B}\n*** Add File: u2\u0085x.txt\n+hi\n{E}\n",
    f"{B}\n*** Add File: u3\x0bx.txt\n+hi\n{E}\n",
    f"{B}\n*** Add File: u4\x0cx.txt\n+hi\n{E}\n",
    f"{B}\n*** Add File: u5.txt\x1f\n+hi\n{E}\n",                     # Rust trim keeps \x1f, Python strips it
    f"{B}\n\x1c*** Add File: u6.txt\n+hi\n{E}\n",
    f"{B}\n*** Add File: u7\x1dx.txt\n+hi\n{E}\n",
    f"{B}\n*** Add File: u8\x1ex.txt\n+hi\n{E}\n",
    f"{B}\n*** Add File: u9 x.txt\n+hi\n{E}\n",
    f"{B}\r\r\n*** Add File: cr.txt\n+hi\n{E}\n",                       # a \r outside a \r\n pair
    f"{B}\n*** Environment ID: local\n*** Add File: e1.txt\n+hi\n{E}\n",  # Codex applied it locally
    f"{B}\n  *** Environment ID: local\n*** Add File: e2.txt\n+hi\n{E}\n",
    f"{B}\n***\tAdd File: n5.txt\n+hi\n{E}\n",
    f"{B}\n*** Add File:\tn6.txt\n+hi\n{E}\n",
    f"{B}\n*** Update File: a.txt\n***\tMove to: m.txt\n@@\n-old a\n+new a\n{E}\n",
    f"{B}\n*** add file: lc.txt\n+hi\n{E}\n",
])
def test_a_patch_the_two_parsers_could_read_differently_is_refused(patch):
    assert grant_paths("codex", "apply_patch", {"command": patch}, "/w") is None


@pytest.mark.parametrize("patch,want", [
    (f"{B}\n   *** Add File: n2.txt\n+hi\n{E}\n", ["n2.txt"]),
    (f"{B}\n*** Add File: n3.txt   \n+hi\n{E}\n", ["n3.txt"]),
    (f"{B}\n\t*** Add File: n4.txt\n+hi\n{E}\n", ["n4.txt"]),
    (f"{B}\n*** Add File:  n7.txt\n+hi\n{E}\n", [" n7.txt"]),          # Codex wrote ' n7.txt' too
    (f"{B}\n *** Add File: n8.txt\n+hi\n{E}\n", ["n8.txt"]),
    (f"{B}\n　*** Add File: n9.txt\n+hi\n{E}\n", ["n9.txt"]),
    (f"{B}\r\n*** Add File: crlf.txt\r\n+hi\r\n{E}\r\n", ["crlf.txt"]),
    (f"<<'EOF'\n{B}\n*** Add File: hd.txt\n+hi\n{E}\nEOF\n", ["hd.txt"]),
    (f"{B}\n*** Add File: h1.txt\n+hi\n  *** Add File: h2.txt\n+x\n{E}\n", ["h1.txt", "h2.txt"]),
    (f"{B}\n*** Add File: c1.txt\n+*** Add File: c2.txt\n{E}\n", ["c1.txt"]),
    # Codex reads the indented line as context, Levain as a header: the grant reads more, never less
    (f"{B}\n*** Update File: a.txt\n@@\n-old a\n+new a\n *** Update File: b.txt\n@@\n-o\n+n\n{E}\n",
     ["a.txt", "b.txt"]),
])
def test_a_patch_both_parsers_read_alike_gives_every_path_codex_writes(patch, want):
    got = grant_paths("codex", "apply_patch", {"command": patch}, "/w")
    assert got == tuple(sorted(f"/w/{p}" for p in want))


# --- write_list_match ------------------------------------------------------------------------------------------

ENTRIES = (PathEntry("src/a.py", "100644", "a" * 40), PathEntry("src/café.py", NEW, NEW))


def test_write_list_match_is_casefold_and_nfc_over_whole_entries():
    assert write_list_match("src/a.py", ENTRIES) is ENTRIES[0]
    assert write_list_match("SRC/A.PY", ENTRIES) is ENTRIES[0]
    assert write_list_match(unicodedata.normalize("NFD", "src/café.py"), ENTRIES) is ENTRIES[1]
    for rel in ("src", "src/", "src/a", "src/a.py/x", "src/a.pyc", "a.py", ""):
        assert write_list_match(rel, ENTRIES) is None


# --- no_follow_check -------------------------------------------------------------------------------------------

def test_the_walk_on_the_moved_disk(world):
    _, root = world
    assert no_follow_check(root, "src/a.py", new_ok=False) is None
    assert no_follow_check(root, "src/new.py", new_ok=True) is None
    assert no_follow_check(root, "src/sub/deep.py", new_ok=True) is None
    assert "absent" in no_follow_check(root, "src/new.py", new_ok=False)
    assert "absent" in no_follow_check(root, "src/sub/deep.py", new_ok=False)
    assert "2 hard links" in no_follow_check(root, "src/b.py", new_ok=False)
    assert "docs is not a plain directory" in no_follow_check(root, "docs/x.md", new_ok=True)
    assert "is a symlink" in no_follow_check(root, "src/run.sh", new_ok=True)
    assert "src/alias.py is a symlink" in no_follow_check(root, "src/alias.py", new_ok=True)   # committed at base
    assert "srclink is not a plain directory" in no_follow_check(root, "srclink/a.py", new_ok=True)
    assert "src/a.py is not a plain directory" in no_follow_check(root, "src/a.py/x", new_ok=True)
    assert "not a regular file" in no_follow_check(root, "src", new_ok=True)


def test_the_walk_refuses_a_fifo_and_a_symlinked_root(world, tmp_path):
    _, root = world
    os.mkfifo(os.path.join(root, "src/pipe"))
    assert "not a regular file" in no_follow_check(root, "src/pipe", new_ok=True)
    os.symlink(root, tmp_path / "rootlink")
    assert "worktree root" in no_follow_check(str(tmp_path / "rootlink"), "src/a.py", new_ok=False)


def test_the_walk_closes_every_fd(world):
    _, root = world
    before = open_fds()
    for _ in range(50):
        for rel in ("src/a.py", "src/sub/deep.py", "docs/x.md", "src/run.sh", "src/a.py/x", "src/b.py"):
            no_follow_check(root, rel, new_ok=True)
    assert open_fds() == before


# --- grant_decision --------------------------------------------------------------------------------------------

ALLOWED = ["src/a.py", "src/new.py", "src/sub/deep.py"]
QUEUED = [("src/b.py", "2 hard links"), ("docs/x.md", "not a plain directory"), ("src/run.sh", "is a symlink"),
          ("src/other.py", "not a file of the signed write list"), ("src/A.py", "case or form variant"),
          ("src/../src/a.py", "not a normalised path"), ("./src/a.py", "not a normalised path")]


@pytest.mark.parametrize("rel", ALLOWED)
def test_a_clean_write_list_file_is_allowed_by_either_agent(world, rel):
    contract, root = world
    assert grant_decision(contract, "claude_code", "Write", cc(root, rel), root=root) == Allow((f"{root}/{rel}",))
    assert grant_decision(contract, "codex", "apply_patch", cx(rel), root=root) == Allow((f"{root}/{rel}",))


@pytest.mark.parametrize("rel,why", QUEUED)
def test_a_moved_or_outside_path_queues(world, rel, why):
    contract, root = world
    for d in (grant_decision(contract, "claude_code", "Write", cc(root, rel), root=root),
              grant_decision(contract, "codex", "apply_patch", cx(rel), root=root)):
        assert isinstance(d, Queue) and why in d.reason


def test_the_recorded_payloads_as_recorded_queue(world):
    contract, root = world
    assert "not a normalised path under the worktree root" in grant_decision(
        contract, "claude_code", "Write", CC_WRITE, root=root).reason
    assert "a1.txt is not a file of the signed write list" in grant_decision(
        contract, "codex", "apply_patch", CX_PATCH, root=root).reason


def test_absolute_and_escaping_paths_queue(world):
    contract, root = world
    assert isinstance(grant_decision(contract, "claude_code", "Write", {**CC_WRITE, "file_path": "/etc/hosts"},
                                     root=root), Queue)
    for rel in ("/etc/hosts", "../escape.txt", f"{root}/src/a.py"):
        d = grant_decision(contract, "codex", "apply_patch", cx(rel), root=root)
        if rel.startswith(root):
            assert d == Allow((f"{root}/src/a.py",))       # an absolute header inside the root is that file
        else:
            assert isinstance(d, Queue) and "not a normalised path" in d.reason


def test_edit_of_an_absent_file_needs_a_new_entry(world):
    contract, root = world
    os.unlink(os.path.join(root, "src/a.py"))
    edit = {"file_path": f"{root}/src/a.py", "old_string": "a", "new_string": "b", "replace_all": False}
    assert "absent and is not a new file" in grant_decision(contract, "claude_code", "Edit", edit, root=root).reason
    assert grant_decision(contract, "claude_code", "Write", cc(root, "src/a.py"), root=root) == Allow(
        (f"{root}/src/a.py",))
    assert grant_decision(contract, "claude_code", "Edit", {**edit, "file_path": f"{root}/src/new.py"},
                          root=root) == Allow((f"{root}/src/new.py",))


def test_every_path_of_a_patch_must_hold(world):
    contract, root = world
    move_in = UPDATE.replace("src/a.py\n", "src/a.py\n*** Move to: src/sub/deep.py\n")
    assert grant_decision(contract, "codex", "apply_patch", {"command": move_in}, root=root) == Allow(
        (f"{root}/src/a.py", f"{root}/src/sub/deep.py"))
    move_out = UPDATE.replace("src/a.py\n", "src/a.py\n*** Move to: src/elsewhere.py\n")
    assert "elsewhere.py" in grant_decision(contract, "codex", "apply_patch", {"command": move_out}, root=root).reason
    two = f"{B}\n*** Update File: src/a.py\n@@\n-a\n+b\n*** Delete File: docs/x.md\n{E}"
    assert "docs is not a plain directory" in grant_decision(contract, "codex", "apply_patch", {"command": two},
                                                             root=root).reason


def test_the_contract_must_grant_the_tool(world, tmp_path):
    contract, root = world
    assert "does not grant Bash" in grant_decision(contract, "codex", "Bash", {"command": "true"}, root=root).reason
    repo, base = make_repo(tmp_path / "other")
    write_only = resolve(submission(repo, base, read=["src/a.py"], write=["src/a.py"], grant=["Write"]))
    d = grant_decision(write_only, "codex", "apply_patch", cx("src/a.py"), root=str(repo))
    assert d == Queue("the contract does not grant apply_patch")
    assert grant_decision(write_only, "claude_code", "Write", cc(str(repo), "src/a.py"), root=str(repo)) == Allow(
        (f"{repo}/src/a.py",))


@pytest.mark.parametrize("bad_root", ["relative/root", "/", "/a/../b", "/a/b/", 7])
def test_the_root_must_be_absolute_and_normalised(world, bad_root):
    contract, root = world
    d = grant_decision(contract, "claude_code", "Write", cc(root, "src/a.py"), root=bad_root)
    assert isinstance(d, Queue) and "worktree root" in d.reason


def test_a_non_contract_queues(world):
    _, root = world
    assert isinstance(grant_decision({"grant": ["Write"]}, "claude_code", "Write", cc(root, "src/a.py"), root=root),
                      Queue)


def test_an_unreadable_patch_queues_even_inside_the_write_list(world):
    contract, root = world
    d = grant_decision(contract, "codex", "apply_patch", cx("src/a.py x"), root=root)
    assert isinstance(d, Queue) and "not read whole" in d.reason
