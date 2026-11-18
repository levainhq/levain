"""A scratch board store for the cockpit board tests and the look: two tracked repos (alpha, beta, each a real scratch git
repository with a bare origin), one registered but untracked (gamma), and items in every state a repository's own git can
produce: done, ready, ready with a start, landed not closed, blocked, needs grooming, unknown (a link that names nothing)
and off (cut, parked, out of 1.0). Running, waiting and needs-you take a live seat, a waiter or a band row: ``seed`` does
not make those (the tests that need one feed ``board._v2`` a derived document instead).

Used as ``seed(root)`` under a ``LEVAIN_HOME`` the caller has set; it uses only ``levain board`` and ``git``."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
from pathlib import Path

from levain.cli import main

A = ["--by", "desk 1", "--author-kind", "operator"]
HASH = "a" * 64
CHARTER = """# Charter: {name}
Charter-Version: 1
Export: repo

## Purpose
Test.

## Criteria
- C1: it works [judged: phill]

## Constraints
- K1: none

## Plan
{plan}
"""


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env).stdout.decode().strip()


def board(*argv: str) -> str:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(["board", *argv])
    assert rc == 0, err.getvalue()
    return out.getvalue().strip()


def mkrepo(root: Path, name: str) -> Path:
    bare = root / "bare" / f"{name}.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    d = root / "repos" / name
    subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True)
    (d / "README.md").write_text(f"hi {name}\n")
    git(d, "add", ".")
    git(d, "commit", "-qm", "init")
    git(d, "remote", "add", "origin", str(bare))
    git(d, "push", "-q", "origin", "main")
    git(d, "fetch", "-q")
    return d


def _item(repo: str, title: str, *, done: str | None = "green", tier: str | None = "sonnet", depends: list[str] = ()) -> str:
    argv = ["add", "--repo", repo, "--title", title, "--origin", "desk 1", *A]
    if done:
        argv += ["--done", done]
    if tier:
        argv += ["--tier", tier]
    for d in depends:
        argv += ["--depends", d]
    return board(*argv)


def seed(root: Path) -> dict[str, str]:
    """Build the store under ``root`` (the levain home is already set to ``root / 'lhome'``); returns ``{intent: item id}``."""
    store = root / "store"
    board("use", str(store))
    store = Path(os.path.realpath(store))
    (store / "repos.jsonl").touch()
    ids: dict[str, str] = {}
    repos: dict[str, Path] = {}
    for name in ("alpha", "beta"):
        repo = mkrepo(root, name)
        repos[name] = repo
        board("register", name, "--path", str(repo), "--words", "first", *A)
        manifest = root / f"manifest_{name}.json"
        manifest.write_text(json.dumps({"plan_items": ["a"], "contract_items": []}))
        board("begin-track", name, "--manifest", str(manifest), "--words", "yes", *A)
        mig = [json.loads(ln) for ln in (store / "repos.jsonl").read_text("utf-8").splitlines()
               if json.loads(ln)["op"] == "begin-track" and json.loads(ln)["repo"] == name][-1]["migration"]
        first = board("add", "--repo", name, "--title", f"Land the {name} charter", "--origin", "desk 1", "--key", f"{mig}:a",
                      "--payload-hash", HASH, "--done", "merged", "--tier", "sonnet", *A)
        (repo / "CHARTER.md").write_text(CHARTER.format(name=name, plan=f"- {first}"))
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "charter")
        git(repo, "push", "-q", "origin", "main")
        git(repo, "fetch", "-q")
        board("track", name, "--words", "go", *A)
        ids[f"{name}_first"] = first
    gamma = mkrepo(root, "gamma")                                              # registered, never tracked: not read
    board("register", "gamma", "--path", str(gamma), "--words", "w", *A)
    # alpha
    a, b = repos["alpha"], repos["beta"]
    git(a, "commit", "-q", "--allow-empty", "-m", f"close it\n\nBoard-Item: {ids['alpha_first']}\nBoard-Closes: {ids['alpha_first']}")
    git(a, "push", "-q", "origin", "main")
    git(a, "fetch", "-q")
    ids["done"] = ids["alpha_first"]
    ids["ready"] = _item("alpha", "Wire the board into the cockpit")
    ids["groom"] = _item("alpha", "Decide the card wording", done=None, tier=None)
    ids["blocked"] = _item("alpha", "Cut the release after the wiring", depends=[f"item:{ids['ready']}"])
    ids["unknown"] = _item("alpha", "Follow up on a link nobody wrote down", depends=["item:alpha#99"])
    ids["cut"] = _item("alpha", "A thing Phill cut")
    ids["parked"] = _item("alpha", "A thing parked for later")
    board("decide", "--id", ids["cut"], "--decision", "cut", "--words", "no", *A)
    board("decide", "--id", ids["parked"], "--decision", "parked", "--words", "later", *A)
    # beta
    ids["landed"] = _item("beta", "Landed, never closed")
    git(b, "commit", "-q", "--allow-empty", "-m", f"work\n\nBoard-Item: {ids['landed']}")
    git(b, "push", "-q", "origin", "main")
    ids["start"] = _item("beta", "Has a branch to start from")
    git(b, "checkout", "-q", "-b", "wip-start")
    git(b, "commit", "-q", "--allow-empty", "-m", f"begun\n\nBoard-Item: {ids['start']}")
    git(b, "push", "-q", "origin", "wip-start")
    git(b, "checkout", "-q", "main")
    git(b, "fetch", "-q")
    ids["beta_ready"] = _item("beta", "A fresh piece of beta work")
    ids["out"] = _item("beta", "Not for 1.0")
    board("decide", "--id", ids["out"], "--decision", "out-of-1.0", "--words", "later", *A)
    return ids
