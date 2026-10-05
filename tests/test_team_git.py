"""levain team against real git: a bare origin, several clones, concurrent writers, the hook as a subprocess.

POSIX only (the transport uses fcntl); every test drives the real CLI entry point or the real hook module.
"""
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from levain.cli import main as levain_main
from levain.team import entry as E
from levain.team.transport import GitLedger, Repo

pytestmark = pytest.mark.skipif(shutil.which("git") is None or os.name != "posix", reason="needs git + POSIX")

PY = sys.executable
JUDGMENT = """[pack]
name = "ledgerline"
version = "1.0"
[[rule]]
id = "write-batch"
paths = ["src/settlement.py"]
owner = "client:Dana"
words = "settlement.write_batch is called by name from the nightly job; never delete or rename it."
"""


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def clone(tmp: Path, name: str, email: str) -> Path:
    git("clone", "-q", str(tmp / "origin.git"), name, cwd=tmp)
    d = tmp / name
    git("config", "user.email", email, cwd=d)
    git("config", "user.name", name, cwd=d)
    return d


def team(*args, repo: Path) -> int:
    return levain_main(["team", *args, "--repo", str(repo)])


@pytest.fixture
def two(tmp_path):
    """origin + ana (owner, ledger initialised with a pack) + ben (joined)."""
    git("init", "-q", "--bare", "--initial-branch=main", "origin.git", cwd=tmp_path)
    ana = clone(tmp_path, "ana", "ana@ex.com")
    (ana / "src").mkdir()
    (ana / "src" / "billing.py").write_text("def line_total(q, p):\n    return round(q * p, 2)\n")
    (ana / "src" / "settlement.py").write_text("def write_batch(): pass\n")
    git("add", ".", cwd=ana)
    git("commit", "-qm", "init", cwd=ana)
    git("push", "-q", "origin", "HEAD:main", cwd=ana)
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "judgment.toml").write_text(JUDGMENT)
    assert team("init", "--project", "ledgerline", "--owner", "ana", "--member", "ana=ana@ex.com",
                "--member", "ben=ben@ex.com", "--client-owner", "Dana", "--pack", str(pack), repo=ana) == 0
    ben = clone(tmp_path, "ben", "ben@ex.com")
    assert team("join", repo=ben) == 0
    return tmp_path, ana, ben


def ledger(repo):
    return GitLedger(Repo.discover(repo)).ledger()


def hook(event: str, payload: dict, env=None) -> dict:
    cp = subprocess.run([PY, "-P", "-m", "levain.team.hook", event], input=json.dumps(payload),
                        capture_output=True, text=True, timeout=60, env={**os.environ, **(env or {})})
    assert cp.returncode == 0, cp.stderr
    return json.loads(cp.stdout) if cp.stdout.strip() else {}


def edit(repo: Path, rel: str, session="s1", tool="Edit") -> dict:
    return hook("pretooluse", {"session_id": session, "transcript_path": "/x", "cwd": str(repo),
                               "permission_mode": "default", "hook_event_name": "PreToolUse", "tool_name": tool,
                               "tool_input": {"file_path": str(repo / rel)}, "tool_use_id": "t"})


def record_ruling(repo, path="src/billing.py", words="Per-line rounding stays.", *extra):
    return team("record", "decision", "--kind", "ruling", "--owner", "client:Dana", "--paths", path,
                "--words", words, *extra, repo=repo)


# ---- transport ----------------------------------------------------------------------------------------------

def test_init_join_record_reaches_the_other_clone(two):
    tmp, ana, ben = two
    assert "ledgerline@1.0" in [e.get("pack") for e in ledger(ben).entries]
    assert record_ruling(ana) == 0
    assert team("sync", repo=ben) == 0
    words = [e.get("words") for e in ledger(ben).in_force]
    assert "Per-line rounding stays." in words
    # the code branch is untouched: the ledger lives only on levain-ledger
    assert git("ls-tree", "-r", "--name-only", "origin/main", cwd=ben).split() == ["src/billing.py", "src/settlement.py"]
    assert git("status", "--porcelain", cwd=ben) == ""


def test_concurrent_writers_in_both_clones_converge(two):
    tmp, ana, ben = two
    jobs = [(ana if i % 2 else ben, f"src/f{i}.py") for i in range(8)]
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(lambda j: subprocess.run(
            [PY, "-m", "levain", "team", "record", "finding", "--paths", j[1], "--summary", f"note {j[1]}",
             "--repo", str(j[0])], capture_output=True, text=True, timeout=120), jobs))
    codes = [r.returncode for r in results]
    print([r.stderr for r in results if r.returncode])
    assert codes == [0] * 8
    for r in (ana, ben):
        assert team("sync", repo=r) == 0
    a, b = ledger(ana), ledger(ben)
    assert {e["id"] for e in a.entries} == {e["id"] for e in b.entries}
    assert len([e for e in a.entries if e["type"] == "finding"]) == 8
    assert a.problems == [] and b.problems == []


def test_owner_on_two_machines_does_not_wedge(two):
    tmp, ana, ben = two
    ana2 = clone(tmp, "ana2", "ana@ex.com")
    assert team("join", repo=ana2) == 0
    assert team("member", "add", "cat", "cat@ex.com", repo=ana) == 0
    assert team("member", "add", "dan", "dan@ex.com", "--no-push", repo=ana2) == 0
    assert record_ruling(ana2, "src/x.py", "x stays") == 0   # sync hits the team.toml conflict
    assert team("sync", repo=ana2) == 0
    gl = GitLedger(Repo.discover(ana2))
    assert "cat" in gl.team().members                      # the remote's version won the conflicting file
    assert team("sync", repo=ben) == 0
    assert "x stays" in [e.get("words") for e in ledger(ben).in_force]   # the entry still travelled


def test_moved_clone_repairs_its_ledger_link(two):
    tmp, ana, ben = two
    moved = tmp / "ben_moved"
    ben.rename(moved)
    assert record_ruling(moved, "src/y.py", "y stays") == 0


def test_first_push_creates_the_remote_branch(tmp_path):
    git("init", "-q", "--bare", "--initial-branch=main", "origin.git", cwd=tmp_path)
    ana = clone(tmp_path, "ana", "ana@ex.com")
    (ana / "a").write_text("a")
    git("add", ".", cwd=ana)
    git("commit", "-qm", "i", cwd=ana)
    assert team("init", "--owner", "ana", "--member", "ana=ana@ex.com", repo=ana) == 0
    assert git("ls-remote", "--heads", "origin", "levain-ledger", cwd=ana).strip()


def test_init_refuses_a_non_member_and_a_second_init(two):
    tmp, ana, ben = two
    assert team("init", "--owner", "ana", "--member", "ana=ana@ex.com", repo=ben) == 2


# ---- authority + integrity ------------------------------------------------------------------------------

def test_member_cannot_retire_a_client_ruling_via_the_cli(two, capsys):
    tmp, ana, ben = two
    assert record_ruling(ana) == 0
    assert team("sync", repo=ben) == 0
    rid = next(e["id"] for e in ledger(ben).in_force if e.get("words") == "Per-line rounding stays.")
    assert team("retire", rid, "--words", "Dana said drop it", repo=ben) == 2
    assert "or the owner (ana) may supersede" in capsys.readouterr().err


def test_hand_forged_retire_is_ignored_and_reported(two):
    """A teammate with push access bypasses the CLI and writes a validly chained retire in their own file."""
    tmp, ana, ben = two
    assert record_ruling(ana) == 0
    assert team("sync", repo=ben) == 0
    gl = GitLedger(Repo.discover(ben))
    rid = next(e["id"] for e in gl.ledger().in_force if e.get("words") == "Per-line rounding stays.")
    forged = E.seal(E.build("ben", "retire", supersedes=[rid], words="trust me"), "")
    path = gl.file_for("ben")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(forged) + "\n")
    git("add", ".", cwd=gl.wt)
    git("commit", "-qm", "forged", cwd=gl.wt)
    assert rid in {e["id"] for e in gl.ledger().in_force}
    assert edit(ben, "src/billing.py")["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert team("verify", repo=ben) == 1


def test_entry_filed_under_someone_else_is_not_enforced_and_verify_names_the_committer(two, capsys):
    tmp, ana, ben = two
    gl = GitLedger(Repo.discover(ben))
    fake = E.seal(E.build("ana", "decision", kind="ruling", owner="client:Dana", paths=["src/**"],
                          words="Dana: nobody but ben edits src"), "")
    p = gl.wt / "ledger" / "ana" / "ffffffffffffffff.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(fake) + "\n")
    git("add", ".", cwd=gl.wt)
    git("commit", "-qm", "impersonate", cwd=gl.wt)
    assert team("verify", repo=ben) == 1
    out = capsys.readouterr().out
    assert "added by ben@ex.com (ben), who is not ana" in out


def test_tampered_line_fails_verify(two):
    tmp, ana, ben = two
    assert record_ruling(ana) == 0
    gl = GitLedger(Repo.discover(ana))
    f = gl.file_for("ana")
    line = json.loads(f.read_text().splitlines()[0])
    line["words"] = "round once"
    f.write_text(json.dumps(line) + "\n")
    git("commit", "-qam", "rewrite", cwd=gl.wt)
    assert team("verify", repo=ana) == 1   # the rewrite is reported, and the original line still counts
    assert "Per-line rounding stays." in [e.get("words") for e in gl.ledger().in_force]
    assert all(e.get("words") != "round once" for e in gl.ledger().in_force)


# ---- the hook ---------------------------------------------------------------------------------------------

def test_hook_contract_deny_once_then_ack(two):
    tmp, ana, ben = two
    assert record_ruling(ana) == 0
    assert team("sync", repo=ben) == 0
    first = edit(ben, "src/billing.py")["hookSpecificOutput"]
    assert first["hookEventName"] == "PreToolUse" and first["permissionDecision"] == "deny"
    assert "Per-line rounding stays." in first["permissionDecisionReason"]
    assert "client:Dana" in first["permissionDecisionReason"]
    second = edit(ben, "src/billing.py")["hookSpecificOutput"]
    assert "permissionDecision" not in second and "Per-line rounding stays." in second["additionalContext"]
    acks = [e for e in ledger(ben).entries if e["type"] == "ack"]
    assert len(acks) == 1 and acks[0]["session"] == "s1" and acks[0]["author"] == "ben"
    assert edit(ben, "src/billing.py", session="s2")["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("tool", ["Write", "MultiEdit", "NotebookEdit"])
def test_hook_covers_every_edit_tool(two, tool):
    tmp, ana, ben = two
    out = hook("pretooluse", {"session_id": "s", "cwd": str(ben), "hook_event_name": "PreToolUse",
                              "tool_name": tool, "tool_input": {"file_path": str(ben / "src/settlement.py"),
                                                                "notebook_path": str(ben / "src/settlement.py")}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_is_silent_where_nothing_applies(two, tmp_path_factory):
    tmp, ana, ben = two
    assert edit(ben, "src/other.py") == {}
    assert hook("pretooluse", {"tool_name": "Read", "cwd": str(ben), "tool_input": {"file_path": str(ben / "src/settlement.py")}}) == {}
    outside = tmp_path_factory.mktemp("plain")
    assert edit(outside, "x.py") == {}


def test_hook_denies_edits_inside_the_ledger_worktree(two):
    tmp, ana, ben = two
    out = edit(ben, ".git/levain-team/worktree/team.toml", tool="Write")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "levain team record" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_unparseable_team_toml_falls_back_to_the_last_good_version(two):
    tmp, ana, ben = two
    wt = GitLedger(Repo.discover(ben)).wt
    (wt / "team.toml").write_text("not = [valid")
    git("commit", "-qam", "broken", cwd=wt)
    out = edit(ben, "src/settlement.py")["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "team.toml at the tip is unusable" in out["permissionDecisionReason"]


def test_uncommitted_worktree_edits_are_not_the_ledger(two):
    tmp, ana, ben = two
    wt = GitLedger(Repo.discover(ben)).wt
    (wt / "team.toml").write_text('project = "p"\nowner = "ben"\nmode = "surface"\n[members]\nben = "ben@ex.com"\n')
    assert edit(ben, "src/settlement.py")["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_sessionstart_lists_rulings_and_the_flag_hides_them(two):
    tmp, ana, ben = two
    payload = {"session_id": "s", "cwd": str(ben), "hook_event_name": "SessionStart", "source": "startup"}
    text = hook("sessionstart", payload)["hookSpecificOutput"]["additionalContext"]
    assert "you are ben" in text and "never delete or rename" in text and "no canon yet" in text
    off = hook("sessionstart", payload, env={"LEVAIN_TEAM_SESSIONSTART_RULINGS": "off"})
    assert "never delete or rename" not in off["hookSpecificOutput"]["additionalContext"]


def test_hook_fetch_brings_a_new_ruling_without_a_manual_sync(two):
    tmp, ana, ben = two
    gl = GitLedger(Repo.discover(ben))
    gl.save_state(last_fetch_attempt=0)
    assert record_ruling(ana, "src/z.py", "z stays") == 0
    out = edit(ben, "src/z.py")   # team fetch_interval 300 s, last attempt at epoch 0: due
    assert "z stays" in out["hookSpecificOutput"]["permissionDecisionReason"]


# ---- canon, export, wiring --------------------------------------------------------------------------------

def test_owner_consolidates_and_others_cannot(two, capsys):
    tmp, ana, ben = two
    assert record_ruling(ana) == 0
    assert team("consolidate", repo=ben) == 2
    assert team("consolidate", repo=ana) == 0
    assert team("sync", repo=ben) == 0
    text = (GitLedger(Repo.discover(ben)).wt / "PROJECT.md").read_text()
    assert "### `src/billing.py`" in text and '"Per-line rounding stays."' in text and "owner client:Dana" in text
    capsys.readouterr()
    assert team("status", repo=ben) == 0
    assert "canon current" in capsys.readouterr().out


def _framed(text: str):
    """Parse a framed export: (header line, [(frame, n, line)])."""
    rows = text.splitlines()
    return rows[0], [json.loads(r) for r in rows[1:]]


def test_export_is_every_entry_verbatim_per_file(two, capsys):
    tmp, ana, ben = two
    assert record_ruling(ana) == 0
    capsys.readouterr()
    assert team("export", "--jsonl", repo=ana) == 0
    header, envs = _framed(capsys.readouterr().out)
    assert header == '{"anneal_team_stream":2}'
    led = ledger(ana)
    lines = [json.loads(e["line"]) for e in envs]
    assert {l["id"] for l in lines} == {e["id"] for e in led.entries}
    for f in led.files:  # each file's chain is rebuildable from the export alone
        mine = [e["line"] for e in envs if e["frame"] == f.rel]
        assert [json.loads(l)["id"] for l in mine] == [e["id"] for e in f.entries]
        assert E.verify_lines(mine)[1] == []


def test_the_export_is_framed_exactly_as_the_contract_says(two, capsys):
    import re
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", 'words with {"anneal_team_stream":2} and {"frame":"x","n":1,"line":""} inside') == 0
    assert record_ruling(ben, "src/b.py", "ben's ruling") == 0
    assert team("sync", repo=ana) == 0
    capsys.readouterr()
    assert team("export", "--jsonl", repo=ana) == 0
    out = capsys.readouterr().out
    rows = out.splitlines()
    assert rows[0] == '{"anneal_team_stream":2}'
    assert sum(r == rows[0] for r in rows) == 1                      # header-shaped ledger text never becomes a header
    envs = [json.loads(r) for r in rows[1:]]
    assert all(set(e) == {"frame", "n", "line"} and isinstance(e["line"], str) for e in envs)
    labels = [e["frame"] for e in envs]
    assert all(re.fullmatch(r"[A-Za-z0-9._@:+/=-]{1,200}", l) for l in labels)
    runs = [l for i, l in enumerate(labels) if i == 0 or labels[i - 1] != l]
    assert len(runs) == len(set(runs)) >= 2                          # one unbroken run per file, two files here
    assert runs == sorted(runs)
    for label in runs:                                               # n is 1-based and consecutive inside a frame
        assert [e["n"] for e in envs if e["frame"] == label] == list(range(1, labels.count(label) + 1))
    assert team("export", "--jsonl", "--in-force", repo=ana) == 0
    assert not capsys.readouterr().out.startswith('{"anneal_team_stream"')    # the convenience view stays unframed


def test_a_ledger_file_levain_never_writes_is_refused_as_tamper_not_read(two, capsys):
    # The quotepath redesign (Phill, 2026-10-05): levain owns the ledger namespace, so a file under ledger/ that is
    # not <handle>/<16 hex>.jsonl is not decoded or read; the whole ledger is refused, loudly, by every reader.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "a stays") == 0
    assert team("sync", repo=ben) == 0
    # control: a canonical ledger is enforced
    assert edit(ben, "src/a.py")["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "a stays" in edit(ben, "src/a.py", session="s9")["hookSpecificOutput"]["permissionDecisionReason"]
    gl = GitLedger(Repo.discover(ana))
    root = E.seal(E.build("ana", "decision", kind="ruling", owner="client:Dana", paths=["src/odd.py"],
                          words="odd name stays"), "")
    (gl.wt / "ledger" / "ana" / "my file.jsonl").write_text(json.dumps(root, sort_keys=True) + "\n")
    git("add", ".", cwd=gl.wt)
    git("commit", "-qm", "an odd file name", cwd=gl.wt)
    git("push", "-q", "origin", "HEAD:levain-ledger", cwd=gl.wt)
    assert team("sync", repo=ben) == 0
    # the teammate's hook denies ANY edit, naming the file and the commit author
    reason = edit(ben, "src/unrelated.py", session="s2")["hookSpecificOutput"]["permissionDecisionReason"]
    assert "REFUSED as tampered" in reason and "my file.jsonl" in reason and "ana@ex.com" in reason
    capsys.readouterr()
    assert team("export", "--jsonl", repo=ben) == 3            # export refuses
    assert "my file.jsonl" in capsys.readouterr().err
    team("doctor", repo=ben)                                   # doctor flags it
    assert "integrity problem" in capsys.readouterr().out
    payload = {"session_id": "s", "cwd": str(ben), "hook_event_name": "SessionStart", "source": "startup"}
    ctx = hook("sessionstart", payload)["hookSpecificOutput"]["additionalContext"]
    assert "REFUSED as tampered" in ctx and "odd name stays" not in ctx


def test_the_tamper_refusal_clears_when_the_bad_file_is_deleted(two):
    # Tamper is judged from the tip tree, not accumulated from history: once the file is gone nothing is refused.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "a stays") == 0
    gl = GitLedger(Repo.discover(ana))
    bad = gl.wt / "ledger" / "ana" / "notes.txt"
    bad.write_text("not a ledger file\n")
    git("add", ".", cwd=gl.wt)
    git("commit", "-qm", "a planted name", cwd=gl.wt)
    git("push", "-q", "origin", "HEAD:levain-ledger", cwd=gl.wt)
    assert team("sync", repo=ben) == 0
    reason = edit(ben, "src/unrelated.py", session="t1")["hookSpecificOutput"]["permissionDecisionReason"]
    assert "REFUSED as tampered" in reason and "notes.txt" in reason
    bad.unlink()
    git("add", "-A", ".", cwd=gl.wt)
    git("commit", "-qm", "the owner deletes it", cwd=gl.wt)
    git("push", "-q", "origin", "HEAD:levain-ledger", cwd=gl.wt)
    assert team("sync", repo=ben) == 0
    after = edit(ben, "src/unrelated.py", session="t2")
    assert "REFUSED as tampered" not in json.dumps(after)


@pytest.mark.parametrize("kind", ["symlink", "blob"])
def test_a_ledger_entry_that_is_not_a_directory_is_tamper_and_nothing_is_written_through_it(two, kind):
    # `ledger` itself as a symlink or a plain file is invisible to a `ledger/` pathspec; it must still be refused,
    # and levain's own write must never follow it out of the worktree.
    tmp, ana, ben = two
    outside = tmp / "outside"
    outside.mkdir()
    gl = GitLedger(Repo.discover(ana))
    git("rm", "-rq", "ledger", cwd=gl.wt)
    shutil.rmtree(gl.wt / "ledger", ignore_errors=True)
    if kind == "symlink":
        (gl.wt / "ledger").symlink_to(outside)
    else:
        (gl.wt / "ledger").write_text("not a directory\n")
    git("add", "-A", ".", cwd=gl.wt)
    git("commit", "-qm", "ledger is not a directory", cwd=gl.wt)
    git("push", "-q", "origin", "HEAD:levain-ledger", cwd=gl.wt)
    assert team("sync", repo=ben) == 0
    reason = edit(ben, "src/unrelated.py", session="n1")["hookSpecificOutput"]["permissionDecisionReason"]
    assert "REFUSED as tampered" in reason and "'ledger'" in reason and "ana@ex.com" in reason
    assert team("status", "--path", "src/a.py", repo=ben) == 3
    try:
        rc = record_ruling(ben, "src/a.py", "must not land")
    except Exception:
        rc = 1
    assert rc != 0
    assert list(outside.iterdir()) == []                               # nothing was written through the link


def _push_wt(gl, msg, *, add="-A"):
    git("add", add, ".", cwd=gl.wt)
    git("commit", "-qm", msg, cwd=gl.wt)
    git("push", "-q", "origin", "HEAD:levain-ledger", cwd=gl.wt)


def _own_file(gl):
    return next((gl.wt / "ledger" / "ana").glob("*.jsonl"))


def test_a_reorder_of_an_append_only_file_is_a_rewrite_problem_and_not_accepted(two):
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    assert record_ruling(ana, "src/b.py", "second") == 0
    assert team("sync", repo=ben) == 0
    before = len(ledger(ben).entries)
    gl = GitLedger(Repo.discover(ana))
    f = _own_file(gl)
    lines = f.read_text().splitlines()
    assert len(lines) >= 2
    f.write_text("\n".join(reversed(lines)) + "\n")
    _push_wt(gl, "reorder")
    assert team("sync", repo=ben) == 0
    led = ledger(ben)
    assert any("append-only" in p and "not accepted" in p for p in led.problems), led.problems
    assert len(led.entries) == before


def _plumb(gl, parts, entry):
    """Commit on top of HEAD with `entry` (mode, type, sha) at the nested path `parts`, built with mktree only."""
    def tree_of(prefix, rest):
        listing = git("ls-tree", "-z", f"HEAD:{prefix}" if prefix else "HEAD", cwd=gl.wt)
        ents = [e for e in listing.split("\0") if e]
        head = rest[0]
        ents = [e for e in ents if e.split("\t", 1)[1] != head]
        if len(rest) == 1:
            ents.append(f"{entry[0]} {entry[1]} {entry[2]}\t{head}")
        else:
            sub = tree_of(f"{prefix}/{head}" if prefix else head, rest[1:])
            ents.append(f"040000 tree {sub}\t{head}")
        out = subprocess.run(["git", "mktree", "-z"], cwd=gl.wt, input="\0".join(ents) + "\0", check=True,
                             capture_output=True, text=True).stdout.strip()
        return out
    root = tree_of("", parts)
    c = git("commit-tree", root, "-p", "HEAD", "-m", "plumbed", cwd=gl.wt).strip()
    git("update-ref", "refs/heads/levain-ledger", c, cwd=gl.wt)
    git("reset", "-q", "--hard", c, cwd=gl.wt)
    git("push", "-q", "origin", "HEAD:levain-ledger", cwd=gl.wt)


def test_an_empty_tree_at_a_leaf_path_is_tamper(two):
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    gl = GitLedger(Repo.discover(ana))
    empty = subprocess.run(["git", "mktree"], cwd=gl.wt, input="", check=True, capture_output=True, text=True).stdout.strip()
    _plumb(gl, ["ledger", "ana", "0123456789abcdef.jsonl"], ("040000", "tree", empty))
    assert team("sync", repo=ben) == 0
    assert any("0123456789abcdef.jsonl" in t for t in ledger(ben).tamper)


def test_regular_to_symlink_to_regular_with_a_dropped_line_is_a_problem(two):
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    assert record_ruling(ana, "src/b.py", "second") == 0
    gl = GitLedger(Repo.discover(ana))
    f = _own_file(gl)
    full = f.read_text()
    kept = full.splitlines()[0] + "\n"
    f.unlink()
    f.symlink_to(tmp)
    _push_wt(gl, "symlink")
    f.unlink()
    f.write_text(kept)
    _push_wt(gl, "regular again, a line dropped")
    assert team("sync", repo=ben) == 0
    led = ledger(ben)
    assert sum("append-only" in p for p in led.problems) >= 2, led.problems
    assert not led.tamper


def test_consolidate_on_a_tampered_ledger_refuses_and_commits_nothing(two, capsys):
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    gl = GitLedger(Repo.discover(ana))
    (gl.wt / "ledger" / "ana" / "notes.txt").write_text("x\n")
    _push_wt(gl, "plant")
    head = git("rev-parse", "HEAD", cwd=gl.wt)
    capsys.readouterr()
    assert team("consolidate", "--dry-run", repo=ana) != 0
    assert capsys.readouterr().out == ""
    assert team("consolidate", repo=ana) != 0
    assert git("rev-parse", "HEAD", cwd=gl.wt) == head
    assert git("ls-tree", "--name-only", "HEAD", cwd=gl.wt).split().count("PROJECT.md") == 0


def test_plain_status_on_a_tampered_ledger_is_refused_with_no_entries(two, capsys):
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "a stays visible") == 0
    gl = GitLedger(Repo.discover(ana))
    (gl.wt / "ledger" / "ana" / "notes.txt").write_text("x\n")
    _push_wt(gl, "plant")
    capsys.readouterr()
    assert team("status", repo=ana) == 3
    out = capsys.readouterr().out
    assert "REFUSED as tampered" in out and "a stays visible" not in out
    assert team("status", "--json", repo=ana) == 3
    assert json.loads(capsys.readouterr().out)["entries"] == []


def test_a_device_id_that_is_not_16_hex_is_refused(two):
    tmp, ana, ben = two
    gl = GitLedger(Repo.discover(ben))
    gl.save_state(device="../x")
    assert team("record", "decision", "--kind", "ruling", "--owner", "client:Dana", "--paths", "src/a.py",
                "--words", "must not land", repo=ben) != 0
    assert not (gl.wt / "ledger" / "x.jsonl").exists()


def test_an_append_onto_a_hard_linked_ledger_file_is_refused(two):
    tmp, ana, ben = two
    assert record_ruling(ben, "src/a.py", "first") == 0
    gl = GitLedger(Repo.discover(ben))
    mine = next((gl.wt / "ledger" / "ben").glob("*.jsonl"))
    outside = tmp / "outside.jsonl"
    os.link(mine, outside)
    before = outside.read_bytes()
    assert record_ruling(ben, "src/b.py", "second") != 0
    assert outside.read_bytes() == before


def test_frame_labels_are_unique_and_a_literal_unnamed_path_cannot_shadow_an_opaque_one():
    from levain.team.export import _label
    rels = ["ana/a.jsonl", "ana/device!.jsonl", "ana/other!.jsonl", "unnamed-0-" + "0" * 64, "ana/" + "x" * 300 + ".jsonl"]
    labels = [_label(i, r) for i, r in enumerate(rels)]
    assert len(set(labels)) == len(rels)
    assert labels[0] == "ana/a.jsonl"
    assert all(l.startswith("unnamed-") for l in labels[1:]) and labels[3] != rels[3]
    # even if two different paths had the same digest, the index keeps the labels apart
    assert _label(1, "x!") != _label(2, "x!")
    assert all(__import__("re").fullmatch(r"[A-Za-z0-9._@:+/=-]{1,200}", l) for l in labels)


def _anneal_team_import(stdin: str, db: Path, extra=()):
    return subprocess.run([sys.executable, "-P", "-m", "anneal_memory", "--db", str(db), "team-import",
                           "--link-authority", "ana", *extra, "-"], input=stdin, capture_output=True, text=True, timeout=60)


def test_the_shipped_export_imports_into_anneal_and_a_planted_second_root_is_refused(two, capsys, tmp_path):
    pytest.importorskip("anneal_memory.team")
    from levain.team.export import _envelope
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "a stays") == 0
    assert record_ruling(ben, "src/b.py", "b stays") == 0
    assert team("sync", repo=ana) == 0
    capsys.readouterr()
    assert team("export", "--jsonl", repo=ana) == 0
    stream = capsys.readouterr().out
    db = tmp_path / "team.db"
    assert subprocess.run([sys.executable, "-P", "-m", "anneal_memory", "--db", str(db), "init"],
                          capture_output=True, text=True, timeout=60).returncode == 0
    first = _anneal_team_import(stream, db)
    assert first.returncode == 0, first.stderr + first.stdout
    assert "Imported 3 entries" in first.stdout       # the pack rule + the two rulings
    second = _anneal_team_import(stream, db)
    assert second.returncode == 0 and "Imported 0 entries" in second.stdout            # idempotent
    # a second root planted into the last file of the stream: a valid root of the same author, so only the frame rule can refuse it
    last = [json.loads(r) for r in stream.splitlines()[1:]][-1]
    forged = E.seal(E.build(last["frame"].split("/")[0], "decision", kind="ruling", owner="client:Dana",
                            paths=["src/evil.py"], words="evil stays"), "")
    n = last["n"] + 1
    planted = stream + _envelope(last["frame"], n, json.dumps(forged, ensure_ascii=False, sort_keys=True))
    db2 = tmp_path / "team2.db"
    subprocess.run([sys.executable, "-P", "-m", "anneal_memory", "--db", str(db2), "init"], capture_output=True, timeout=60)
    bad = _anneal_team_import(planted, db2)
    assert bad.returncode == 3, bad.stderr + bad.stdout                                 # refused, the verified prefix still imports
    assert "a second root in one file, not imported" in bad.stdout + bad.stderr and "Imported 3 entries" in bad.stdout
    eps = subprocess.run([sys.executable, "-P", "-m", "anneal_memory", "--db", str(db2), "episodes"],
                         capture_output=True, text=True, timeout=60).stdout
    assert "Episodes: 3 total matching" in eps and "evil" not in eps                      # the prefix is there, the planted root is not


def test_install_is_idempotent_keeps_foreign_hooks_and_covers_worktrees(two):
    tmp, ana, ben = two
    s = ben / ".claude" / "settings.local.json"
    data = json.loads(s.read_text())
    data["hooks"]["PreToolUse"].append({"matcher": "Bash", "hooks": [{"type": "command", "command": "mine"}]})
    s.write_text(json.dumps(data))
    git("worktree", "add", "-q", str(tmp / "ben-wt2"), cwd=ben)
    assert team("install", repo=ben) == 0
    assert team("install", repo=ben) == 0
    data = json.loads(s.read_text())
    cmds = [h["command"] for g in data["hooks"]["PreToolUse"] for h in g["hooks"]]
    assert cmds.count("mine") == 1 and sum("levain.team.hook" in c for c in cmds) == 1
    assert (tmp / "ben-wt2" / ".claude" / "settings.local.json").is_file()
    assert git("status", "--porcelain", cwd=ben) == ""
    assert team("doctor", repo=tmp / "ben-wt2") == 0


# ---- regressions from the L1 review (each reproduced by running before the fix) ----------------------------

def test_line_separator_inside_words_is_still_enforced(two):
    tmp, ana, ben = two
    assert record_ruling(ana, "src/ls.py", "No. Keep it.") == 0
    assert team("verify", repo=ana) == 0
    assert edit(ana, "src/ls.py", session="x")["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_torn_last_line_does_not_swallow_the_next_record(two):
    tmp, ana, ben = two
    gl = GitLedger(Repo.discover(ana))
    gl.file_for("ana").parent.mkdir(parents=True, exist_ok=True)
    with open(gl.file_for("ana"), "a") as fh:   # ana has no file yet; a crash left a torn fragment
        fh.write('{"v":1,"id":"ana-2026')
    git("add", ".", cwd=gl.wt)
    git("commit", "-qm", "torn", cwd=gl.wt)
    assert record_ruling(ana, "src/t.py", "t stays") == 0
    assert "t stays" in [e.get("words") for e in gl.ledger().in_force]


def test_hook_reads_while_another_process_syncs(two):
    tmp, ana, ben = two
    assert record_ruling(ben, "src/q.py", "q stays") == 0
    assert team("sync", repo=ana) == 0
    gl = GitLedger(Repo.discover(ana))
    gl.save_state(last_fetch_attempt=0)   # a fetch is due, and it cannot get the net lock
    with gl.lock(name="net"):   # a long fetch/push in another process holds only the net lock
        out = edit(ana, "src/q.py")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("rel", ["state.json", "lock", "sessions/s1", "worktree/team.toml"])
def test_agent_cannot_edit_any_ledger_machinery(two, rel):
    tmp, ana, ben = two
    out = edit(ben, f".git/levain-team/{rel}", tool="Write")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_broken_ledger_link_is_visible_not_silent(two):
    tmp, ana, ben = two
    (ben / ".git" / "levain-team" / "state.json").write_text("{")
    out = edit(ben, "src/settlement.py")
    assert out["systemMessage"].startswith("[team] ledger unavailable:")


def test_pack_resync_does_not_resurrect_a_rule_the_team_retired(two, capsys):
    tmp, ana, ben = two
    rid = next(e["id"] for e in ledger(ana).in_force if e.get("pack"))
    assert team("retire", rid, "--words", "Dana: obsolete now", repo=ana) == 0
    assert team("pack-sync", str(tmp / "pack"), repo=ana) == 0
    assert not [e for e in ledger(ana).in_force if e.get("pack")]
    (tmp / "pack" / "judgment.toml").write_text(JUDGMENT.replace("1.0", "1.1").replace("never delete", "NEVER delete"))
    assert team("pack-sync", str(tmp / "pack"), repo=ana) == 0   # a changed rule is offered again
    assert [e["pack"] for e in ledger(ana).in_force if e.get("pack")] == ["ledgerline@1.1"]


def test_retiring_a_retire_is_refused(two, capsys):
    tmp, ana, ben = two
    assert record_ruling(ana) == 0
    rid = next(e["id"] for e in ledger(ana).in_force if e.get("words") == "Per-line rounding stays.")
    assert team("retire", rid, "--words", "Dana: drop", repo=ana) == 0
    ret = next(e["id"] for e in ledger(ana).entries if e["type"] == "retire")
    assert team("retire", ret, "--words", "undo", repo=ana) == 2
    assert "record that entry again" in capsys.readouterr().err


def test_interrupted_canon_write_does_not_wedge_the_clone(two):
    tmp, ana, ben = two
    gl = GitLedger(Repo.discover(ana))
    (gl.wt / "PROJECT.md").write_text("half written")
    assert record_ruling(ana, "src/w.py", "w stays") == 0
    assert team("sync", repo=ana) == 0


def test_a_directory_path_governs_its_tree_and_paths_are_cwd_relative(two, monkeypatch):
    tmp, ana, ben = two
    monkeypatch.chdir(ana / "src")
    assert team("record", "constraint", "--kind", "practice", "--paths", ".", "--summary", "Decimal only",
                repo=ana) == 0
    e = next(e for e in ledger(ana).in_force if e.get("summary") == "Decimal only")
    assert e["paths"] == ["src/"]
    assert "Decimal only" in edit(ana, "src/billing.py")["hookSpecificOutput"]["additionalContext"]


def test_retire_forged_under_the_owners_name_is_not_enforced(two):
    """ben writes a retire that claims to be ana's (the owner) into ana's folder and commits it as himself."""
    tmp, ana, ben = two
    assert record_ruling(ana) == 0
    assert team("sync", repo=ben) == 0
    gl = GitLedger(Repo.discover(ben))
    rid = next(e["id"] for e in gl.ledger().in_force if e.get("words") == "Per-line rounding stays.")
    forged = E.seal(E.build("ana", "retire", supersedes=[rid], words="Dana: drop it"), "")
    p = gl.wt / "ledger" / "ana" / "000000000000beef.jsonl"
    p.write_text(json.dumps(forged) + "\n")
    git("add", ".", cwd=gl.wt)
    git("commit", "-qm", "as ana", cwd=gl.wt)
    led = gl.ledger()
    assert rid in {e["id"] for e in led.in_force}
    assert any("added by ben@ex.com" in p for p in led.problems)
    assert edit(ben, "src/billing.py")["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---- regressions from L3 round 1 ---------------------------------------------------------------------------

def _push_as(repo: Path, message: str):
    gl = GitLedger(Repo.discover(repo))
    git("add", "-A", ".", cwd=gl.wt)
    git("commit", "-qm", message, cwd=gl.wt)
    git("push", "-q", "origin", "HEAD:levain-ledger", cwd=gl.wt)
    return gl


def test_deleting_a_members_file_does_not_silence_their_rulings(two):
    tmp, ana, ben = two
    assert record_ruling(ana) == 0
    assert team("sync", repo=ben) == 0
    gl = GitLedger(Repo.discover(ben))
    for f in (gl.wt / "ledger" / "ana").glob("*.jsonl"):
        f.unlink()
    _push_as(ben, "drop ana")
    assert team("sync", repo=ana) == 0
    for r in (ana, ben):
        assert edit(r, "src/billing.py", session="del")["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert team("verify", repo=ana) == 1


def test_one_stranger_line_does_not_silence_a_members_file(two):
    tmp, ana, ben = two
    assert record_ruling(ana) == 0
    assert team("sync", repo=ben) == 0
    gl = GitLedger(Repo.discover(ben))
    for f in (gl.wt / "ledger" / "ana").glob("*.jsonl"):
        with open(f, "a") as fh:
            fh.write("\n")
    _push_as(ben, "blank line")
    assert edit(ben, "src/billing.py", session="blank")["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_owner_swap_by_hand_push_is_reported(two, capsys):
    tmp, ana, ben = two
    gl = GitLedger(Repo.discover(ben))
    t = gl.team()
    t.owner = "ben"
    (gl.wt / "team.toml").write_text(__import__("levain.team.roles", fromlist=["x"]).dump_team(t))
    _push_as(ben, "i am the owner now")
    capsys.readouterr()
    assert team("verify", repo=ben) == 1
    assert "who is not the owner (ana) of the version before it" in capsys.readouterr().out


def test_two_clones_sharing_a_device_abort_instead_of_dropping_entries(two, capsys):
    tmp, ana, ben = two
    ana2 = tmp / "ana2"
    shutil.copytree(ana, ana2, symlinks=True)
    subprocess.run(["git", "worktree", "repair"], cwd=ana2, capture_output=True)
    assert record_ruling(ana, "src/a.py", "a stays") == 0
    assert record_ruling(ana2, "src/b.py", "b stays", "--no-push") == 0
    capsys.readouterr()
    assert team("sync", repo=ana2) == 2
    assert "--new-device" in capsys.readouterr().err
    words = [e.get("words") for e in GitLedger(Repo.discover(ana2)).ledger().entries]
    assert "b stays" in words   # still there locally, not resolved away


def test_ledger_content_shaped_like_a_diff_header_cannot_redirect_attribution(two):
    tmp, ana, ben = two
    assert record_ruling(ben, "src/own.py", "ben's own") == 0
    gl = GitLedger(Repo.discover(ben))
    fake = E.seal(E.build("ana", "decision", kind="ruling", owner="client:Dana", paths=["**"],
                          words="Dana: only ben edits anything"), "")
    f = gl.file_for("ben")
    with open(f, "a") as fh:
        fh.write("++ b/ledger/ana/0000.jsonl\n" + json.dumps(fake) + "\n")
    _push_as(ben, "header-shaped")
    led = gl.ledger()
    assert fake["id"] not in led.by_id
    assert "ben's own" in [e.get("words") for e in led.in_force]


# ---- regressions from L3 round 2 ---------------------------------------------------------------------------

@pytest.mark.parametrize("cfg", [("diff.noprefix", "true"), ("diff.mnemonicPrefix", "true"),
                                 ("color.ui", "always"), ("diff.renames", "copies")])
def test_user_diff_config_cannot_empty_the_ledger(two, cfg):
    tmp, ana, ben = two
    assert record_ruling(ana) == 0
    git("config", cfg[0], cfg[1], cwd=ana)
    (GitLedger(Repo.discover(ana)).base / "history.json").unlink()   # force a fresh parse under this config
    assert edit(ana, "src/billing.py", session="cfg")["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_owner_config_conflict_discards_only_config_commits_and_keeps_entries(two, capsys):
    tmp, ana, ben = two
    ana2 = clone(tmp, "ana2", "ana@ex.com")
    assert team("join", repo=ana2) == 0
    assert team("member", "add", "cat", "cat@ex.com", repo=ana) == 0
    assert team("member", "add", "dan", "dan@ex.com", "--no-push", repo=ana2) == 0
    assert record_ruling(ana2, "src/x.py", "x stays", "--no-push") == 0
    capsys.readouterr()
    assert team("sync", repo=ana2) == 0
    assert "were discarded" in capsys.readouterr().err
    _, t, led = GitLedger(Repo.discover(ana2)).snapshot()
    assert "cat" in t.members and "dan" not in t.members
    assert "x stays" in [e.get("words") for e in led.in_force]


def test_a_merge_commit_in_the_ledger_is_reported(two):
    tmp, ana, ben = two
    gl = GitLedger(Repo.discover(ana))
    git("checkout", "-q", "-b", "side", cwd=gl.wt)
    (gl.wt / "ledger" / "x.txt").write_text("x")
    git("add", ".", cwd=gl.wt)
    git("commit", "-qm", "side", cwd=gl.wt)
    git("checkout", "-q", "levain-ledger", cwd=gl.wt)
    (gl.wt / "ledger" / "y.txt").write_text("y")
    git("add", ".", cwd=gl.wt)
    git("commit", "-qm", "main", cwd=gl.wt)
    git("merge", "-q", "--no-edit", "side", cwd=gl.wt)
    assert any("merge commit" in p for p in gl.ledger().problems)


def test_join_new_device_in_a_copied_clone_never_touches_the_original(two):
    tmp, ana, ben = two
    before = git("rev-parse", "levain-ledger", cwd=ana).strip()
    copy = tmp / "ana_copy"
    shutil.copytree(ana, copy, symlinks=True)
    assert team("join", "--new-device", repo=copy) == 0
    gl = GitLedger(Repo.discover(copy))
    assert gl._wt_is_ours() and gl.device != GitLedger(Repo.discover(ana)).device
    assert record_ruling(copy, "src/c.py", "from the copy") == 0
    assert git("rev-parse", "levain-ledger", cwd=ana).strip() == before   # the original was not written


def test_a_missing_session_id_still_acks_with_a_schema_safe_token(two):
    tmp, ana, ben = two
    p = {"cwd": str(ben), "hook_event_name": "PreToolUse", "tool_name": "Edit",
         "transcript_path": "/x/" + "y" * 300 + "/t.jsonl", "tool_input": {"file_path": str(ben / "src/settlement.py")}}
    assert hook("pretooluse", p)["hookSpecificOutput"]["permissionDecision"] == "deny"
    second = hook("pretooluse", p)["hookSpecificOutput"]
    assert "permissionDecision" not in second and "acknowledgement not recorded" not in second["additionalContext"]


# ---- regressions from L3 round 3 ---------------------------------------------------------------------------

def test_conflict_replay_skips_commits_already_upstream(two, capsys):
    tmp, ana, ben = two
    ana2 = clone(tmp, "ana2", "ana@ex.com")
    assert team("join", repo=ana2) == 0
    assert record_ruling(ana, "src/e.py", "e stays", "--no-push") == 0
    gl = GitLedger(Repo.discover(ana))
    e_sha = git("rev-parse", "HEAD", cwd=gl.wt).strip()
    assert team("member", "add", "cat", "cat@ex.com", repo=ana2) == 0          # remote moves
    git("fetch", "-q", "origin", cwd=gl.wt)
    git("checkout", "-q", "--detach", "origin/levain-ledger", cwd=gl.wt)
    git("cherry-pick", e_sha, cwd=gl.wt)                                         # E' upstream, new SHA
    git("push", "-q", "origin", "HEAD:refs/heads/levain-ledger", cwd=gl.wt)
    git("checkout", "-q", "levain-ledger", cwd=gl.wt)
    assert team("member", "add", "dan", "dan@ex.com", "--no-push", repo=ana) == 0  # conflicts with cat
    capsys.readouterr()
    assert team("sync", repo=ana) == 0
    led = gl.ledger()
    assert [e.get("words") for e in led.in_force].count("e stays") == 1


def test_an_interrupted_replay_is_rolled_back_without_loss(two):
    tmp, ana, ben = two
    assert record_ruling(ana, "src/k.py", "k stays", "--no-push") == 0
    gl = GitLedger(Repo.discover(ana))
    tip = git("rev-parse", "levain-ledger", cwd=ana).strip()
    git("checkout", "-q", "--detach", "HEAD~1", cwd=gl.wt)     # what a kill mid-replay leaves behind
    assert git("rev-parse", "levain-ledger", cwd=ana).strip() == tip
    assert "k stays" in [e.get("words") for e in gl.ledger().in_force]   # readers never saw a loss
    assert record_ruling(ana, "src/l.py", "l stays") == 0
    assert git("symbolic-ref", "HEAD", cwd=gl.wt).strip() == "refs/heads/levain-ledger"
    words = [e.get("words") for e in gl.ledger().in_force]
    assert "k stays" in words and "l stays" in words


def test_a_merge_resolved_to_its_first_parent_cannot_hide_side_lines(two):
    tmp, ana, ben = two
    gl = GitLedger(Repo.discover(ana))
    git("checkout", "-q", "-b", "side", cwd=gl.wt)
    e = E.seal(E.build("ana", "decision", kind="ruling", owner="client:Dana", paths=["src/s.py"], words="s stays"), "")
    gl.file_for("ana").parent.mkdir(parents=True, exist_ok=True)
    gl.file_for("ana").write_text(json.dumps(e) + "\n")
    git("add", ".", cwd=gl.wt)
    git("commit", "-qm", "side entry", cwd=gl.wt)
    git("checkout", "-q", "levain-ledger", cwd=gl.wt)
    git("merge", "-q", "-s", "ours", "--no-edit", "side", cwd=gl.wt)     # TREESAME to the first parent
    led = gl.ledger()
    assert "s stays" in [e.get("words") for e in led.in_force]
    assert any("merge commit" in p for p in led.problems)


# ---- 0.6.1 replay hardening ------------------------------------------------------------------------------------

def _conflicting_replay(two):
    """ana has an unpushed entry E and an unpushed team.toml change that conflicts with a remote change."""
    tmp, ana, ben = two
    ana2 = clone(tmp, "ana2", "ana@ex.com")
    assert team("join", repo=ana2) == 0
    assert record_ruling(ana, "src/e.py", "e stays", "--no-push") == 0
    assert team("member", "add", "cat", "cat@ex.com", repo=ana2) == 0          # remote moves
    assert team("member", "add", "dan", "dan@ex.com", "--no-push", repo=ana) == 0  # conflicts with cat
    return GitLedger(Repo.discover(ana)), ana


def _spy_git(monkeypatch, fail_pick=False, pick_rc=1, stale_pick_marker=False, fail_checkout=False):
    """Record every git argv the transport runs; optionally make a replay cherry-pick fail, or the final checkout."""
    import levain.team.transport as T
    real, calls = T.git, []

    def spy(args, cwd, **kw):
        calls.append(list(args))
        if fail_pick and "--allow-empty" in args and "cherry-pick" in args:
            if stale_pick_marker:
                marker = Path(real(["rev-parse", "--git-path", "CHERRY_PICK_HEAD"], cwd).stdout.strip())
                (marker if marker.is_absolute() else Path(cwd) / marker).write_text("0" * 40 + "\n")
            return subprocess.CompletedProcess(["git", *args], pick_rc, "", "fatal: Unable to create index.lock")
        if fail_checkout and args == ["checkout", "-q", "levain-ledger"]:
            raise T.TeamError("git checkout failed: untracked file in the way")
        return real(args, cwd, **kw)
    monkeypatch.setattr(T, "git", spy)
    return calls


def test_the_replay_runs_with_rerere_off_and_in_topological_order(two, monkeypatch):
    gl, ana = _conflicting_replay(two)
    calls = _spy_git(monkeypatch)
    assert team("sync", repo=ana) == 0
    for verb, marker in (("rebase", "--empty=drop"), ("cherry-pick", "--allow-empty")):
        argv = next(c for c in calls if verb in c and marker in c)
        assert "rerere.enabled=false" in argv and "rerere.autoupdate=false" in argv
        assert "commit.gpgsign=false" in argv
    assert "--topo-order" in next(c for c in calls if "rev-list" in c and "--reverse" in c)
    assert [e.get("words") for e in gl.ledger().in_force].count("e stays") == 1


def test_a_cherry_pick_that_failed_without_a_pick_in_progress_is_not_skipped_as_upstream(two, monkeypatch, capsys):
    gl, ana = _conflicting_replay(two)
    tip = git("rev-parse", "levain-ledger", cwd=ana).strip()
    calls = _spy_git(monkeypatch, fail_pick=True)
    capsys.readouterr()
    assert team("sync", repo=ana) != 0
    assert "index.lock" in capsys.readouterr().err                      # the real cause is named
    assert not any("--skip" in c for c in calls)                        # the entry was not dropped as "already upstream"
    assert git("rev-parse", "levain-ledger", cwd=ana).strip() == tip    # nothing published, nothing lost
    assert git("symbolic-ref", "HEAD", cwd=gl.wt).strip() == "refs/heads/levain-ledger"


@pytest.mark.parametrize("rc", [1, 128])
def test_a_marker_for_another_commit_is_not_skipped_as_upstream(two, monkeypatch, rc):
    gl, ana = _conflicting_replay(two)
    tip = git("rev-parse", "levain-ledger", cwd=ana).strip()
    calls = _spy_git(monkeypatch, fail_pick=True, pick_rc=rc, stale_pick_marker=True)
    assert team("sync", repo=ana) != 0
    assert not any("--skip" in c for c in calls)
    assert git("rev-parse", "levain-ledger", cwd=ana).strip() == tip    # the entry is still on the branch


def test_an_empty_pick_git_stopped_on_is_really_skipped_and_the_entry_survives_once(two, capsys, monkeypatch):
    tmp, ana, ben = two
    calls = _spy_git(monkeypatch)
    ana2 = clone(tmp, "ana2", "ana@ex.com")
    assert team("join", repo=ana2) == 0
    assert record_ruling(ana, "src/e.py", "e stays", "--no-push") == 0
    gl = GitLedger(Repo.discover(ana))
    e_sha = git("rev-parse", "HEAD", cwd=gl.wt).strip()
    entry_file = git("diff-tree", "--no-commit-id", "--name-only", "-r", e_sha, cwd=gl.wt).split()[0]
    assert team("member", "add", "cat", "cat@ex.com", repo=ana2) == 0          # remote moves
    git("fetch", "-q", "origin", cwd=gl.wt)
    git("checkout", "-q", "--detach", "origin/levain-ledger", cwd=gl.wt)
    git("checkout", e_sha, "--", entry_file, cwd=gl.wt)                           # upstream gets E's change ...
    (gl.wt / "side.txt").write_text("x\n")                                       # ... inside a different patch
    git("add", ".", cwd=gl.wt)
    git("commit", "-qm", "E plus another file", cwd=gl.wt)
    git("push", "-q", "origin", "HEAD:refs/heads/levain-ledger", cwd=gl.wt)
    git("checkout", "-q", "levain-ledger", cwd=gl.wt)
    assert team("member", "add", "dan", "dan@ex.com", "--no-push", repo=ana) == 0  # forces the replay path
    capsys.readouterr()
    assert team("sync", repo=ana) == 0
    assert any(c[:2] == ["cherry-pick", "--skip"] for c in calls)               # the real empty-pick path ran
    assert [e.get("words") for e in gl.ledger().in_force].count("e stays") == 1
    assert git("symbolic-ref", "HEAD", cwd=gl.wt).strip() == "refs/heads/levain-ledger"


def test_a_failing_reattach_does_not_mask_the_error_that_sent_us_there(two, monkeypatch, capsys):
    gl, ana = _conflicting_replay(two)
    _spy_git(monkeypatch, fail_pick=True)
    from levain.team.transport import GitLedger as G, TeamError
    monkeypatch.setattr(G, "_reattach", lambda self: (_ for _ in ()).throw(TeamError("reattach exploded")))
    capsys.readouterr()
    assert team("sync", repo=ana) != 0
    err = capsys.readouterr().err
    assert "index.lock" in err                                  # the replay's own error is the one raised
    assert "left detached (reattach exploded)" in err           # and the state it leaves behind is stated


def test_a_checkout_that_fails_after_the_replay_published_keeps_the_discard_warning_and_reattaches(two, monkeypatch,
                                                                                                capsys):
    gl, ana = _conflicting_replay(two)
    _spy_git(monkeypatch, fail_checkout=True)
    capsys.readouterr()
    assert team("sync", repo=ana) == 0                          # the forced checkout recovers, nothing is lost
    err = capsys.readouterr().err
    assert "were discarded" in err
    assert git("symbolic-ref", "HEAD", cwd=gl.wt).strip() == "refs/heads/levain-ledger"
    assert [e.get("words") for e in gl.ledger().in_force].count("e stays") == 1


def _plant_hooks(gl, *names):
    common = Path(git("rev-parse", "--git-common-dir", cwd=gl.wt).strip())
    hooks = (common if common.is_absolute() else gl.wt / common) / "hooks"
    hooks.mkdir(exist_ok=True)
    git("config", "core.hooksPath", str(hooks), cwd=gl.wt)   # a machine's own hooksPath would otherwise make these inert
    for name in names:
        hook = hooks / name
        # the nonzero exit fails any step the hook runs in; the sentinel proves it ran
        hook.write_text("#!/bin/sh\necho ran >> \"$0.ran\"\nexit 1\n")
        hook.chmod(0o755)
    return hooks


def test_a_failing_project_hook_cannot_make_the_replay_fail_or_drop_an_entry(two):
    # checkout and commit hooks run on Linux git for these steps; post-checkout runs on every git, so this fails
    # on a regression here too
    gl, ana = _conflicting_replay(two)
    hooks = _plant_hooks(gl, "post-checkout", "prepare-commit-msg", "reference-transaction", "post-commit")
    subprocess.run(["git", "checkout", "-q", "levain-ledger"], cwd=gl.wt)         # control: the planted hooks do run
    assert list(hooks.glob("*.ran")), "the planted hooks never fire here, so this test proves nothing"
    for f in hooks.glob("*.ran"):
        f.unlink()
    assert team("sync", repo=ana) == 0
    assert [e.get("words") for e in gl.ledger().in_force].count("e stays") == 1
    assert not list(hooks.glob("*.ran"))                      # none of the project's hooks ran for levain's plumbing
    assert git("symbolic-ref", "HEAD", cwd=gl.wt).strip() == "refs/heads/levain-ledger"


def test_levains_hook_isolation_does_not_reach_the_remotes_own_server_hooks(two):
    tmp, ana, ben = two
    git("config", "core.hooksPath", str(tmp / "origin.git" / "hooks"), cwd=tmp / "origin.git")   # beats a machine-wide one
    hook = tmp / "origin.git" / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'declined by the remote' >&2\nexit 1\n")
    hook.chmod(0o755)
    before = git("rev-parse", "levain-ledger", cwd=tmp / "origin.git").strip()
    assert record_ruling(ana, "src/z.py", "z stays") != 0                      # the push is refused ...
    assert git("rev-parse", "levain-ledger", cwd=tmp / "origin.git").strip() == before   # ... and the remote is unchanged


def test_a_repository_path_holding_a_line_break_is_still_discovered(tmp_path):
    # rev-parse printed two paths and the reader split them into lines, so a clone at "proj<LF>x" read as no
    # repository and both hooks went silent (quotepath L3 r5, RUN).
    for name in ("proj\nx", "proj x", "proj\rx"):
        d = tmp_path / name
        d.mkdir()
        git("init", "-q", cwd=d)
        repo = Repo.discover(d / "sub" / "file.py")
        assert repo is not None and os.path.realpath(repo.toplevel) == os.path.realpath(d), name


def test_a_joined_clone_without_git_on_path_says_so_instead_of_going_silent(two):
    # discover read "git is not on PATH" as "no repository", so both hooks returned with no output (L3 r6).
    tmp, ana, ben = two
    payload = {"session_id": "s", "cwd": str(ben), "hook_event_name": "SessionStart", "source": "startup"}
    out = hook("sessionstart", payload, env={"PATH": str(tmp / "no-git-here")})
    assert "ledger unavailable" in out.get("systemMessage", "") and "git" in out["systemMessage"], out
