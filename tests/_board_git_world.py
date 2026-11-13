"""A real git world for the board's evidence readers: a bare ``origin``, a working clone, and helpers to make commits
with trailers, queue refs and seat branches at fixed dates. Real git on purpose: the readers are git parsers, and a mock
of git would test the mock. Nothing here touches the operator's repos or home (every path is under ``tmp``)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

DAY = "2026-11-12T12:00:00Z"            # after the epoch (BOARD_EPOCH = 2026-11-12T00:00:00Z)
OLD = "2026-10-01T12:00:00Z"            # before it
NOW = "2026-11-12T20:00:00Z"
_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_TERMINAL_PROMPT": "0"}


def git(repo: Path, *args: str, date: str | None = None, stdin: str | None = None) -> str:
    env = {**os.environ, **_ENV}
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    cp = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, input=stdin, check=False)
    if cp.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {cp.stderr.strip()}")
    return cp.stdout.strip()


class World:
    """``origin`` (bare) and ``w`` (a clone with ``main`` pushed). Commits are made in ``w``."""

    def __init__(self, tmp: Path) -> None:
        self.origin = tmp / "origin.git"
        self.w = tmp / "levain"                                   # the name item ids use
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True, env={**os.environ, **_ENV})
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.w)], check=True, capture_output=True, env={**os.environ, **_ENV})
        git(self.w, "checkout", "-q", "-b", "main")
        self.n = 0

    def commit(self, subject: str, *, trailers: tuple[str, ...] = (), date: str = DAY, file: str | None = None,
               body: str = "x\n") -> str:
        self.n += 1
        path = self.w / (file or f"f{self.n}.txt")
        path.write_text(f"{body}{self.n}\n" if file is None else body, encoding="utf-8")
        git(self.w, "add", "-A")
        msg = subject + ("\n\nbody\n\n" + "\n".join(trailers) if trailers else "")
        git(self.w, "commit", "-q", "-m", msg, date=date)
        return git(self.w, "rev-parse", "HEAD")

    def commit_raw(self, message: str, *, file: str, body: str = "x\n", date: str = DAY) -> str:
        """A commit with the message exactly as given (the shape of a seat's real message, trailer block or not)."""
        self.n += 1
        (self.w / file).write_text(body, encoding="utf-8")
        git(self.w, "add", "-A")
        git(self.w, "commit", "-q", "-m", message, date=date)
        return git(self.w, "rev-parse", "HEAD")

    def empty_commit(self, subject: str, *, trailers: tuple[str, ...] = (), date: str = DAY) -> str:
        """A commit with no diff at all (no patch-id exists for it)."""
        self.n += 1
        msg = subject + ("\n\nbody\n\n" + "\n".join(trailers) if trailers else "")
        git(self.w, "commit", "-q", "--allow-empty", "-m", msg, date=date)
        return git(self.w, "rev-parse", "HEAD")

    def clone(self, dest: Path, *flags: str) -> Path:
        """A second clone of ``origin`` (``--depth 1`` for a shallow one, ``--filter=...`` for a partial one)."""
        subprocess.run(["git", "clone", "-q", *flags, f"file://{self.origin}", str(dest)], check=True, capture_output=True,
                       env={**os.environ, **_ENV})
        return dest

    def push_main(self) -> None:
        git(self.w, "push", "-q", "origin", "main")
        git(self.w, "fetch", "-q", "origin")

    def branch(self, name: str, at: str = "origin/main") -> None:
        git(self.w, "checkout", "-q", "-B", name, at)

    def push(self, name: str) -> None:
        git(self.w, "push", "-q", "-f", "origin", f"{name}:{name}")
        git(self.w, "fetch", "-q", "origin")

    def delete_local(self, name: str) -> None:
        git(self.w, "checkout", "-q", "main")
        git(self.w, "branch", "-q", "-D", name)

    def cherry_pick(self, sha: str, date: str = "2026-11-12T13:00:00Z") -> str:
        git(self.w, "cherry-pick", sha, date=date)
        return git(self.w, "rev-parse", "HEAD")
