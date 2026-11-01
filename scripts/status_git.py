#!/usr/bin/env python3
"""Derive the git-checkable half of the 1.0 status from ancestry against origin/main, instead of keeping it by hand.

Two outputs, both generated and never edited:

* ``ancestry``: for every ``rel <sha>`` -> ``vX.Y.Z`` pair in a status file's table cells, whether ``<sha>`` is an ancestor
  of ``origin/main``, which release tag FIRST contains it (``git tag --contains``, version order), and whether that is the tag
  the cell claims. A cell that says RELEASED v0.11.4 while the sha is not in any tag, or sits in another tag, shows DRIFT.
  The human-judged columns (status words, remaining, his residue) stay hand-written; only this derived table is regenerated.
* ``state``: the header of a pickup file: main's tip, the newest tag and the distance from it, and the newest ``seat/*``
  branches with how many commits each holds that main does not, and whether it is merged.

Landed by fast-forward, a branch is merged exactly when it is an ancestor of main; a squash or rebase merge would read
NOT merged here. Exit 2 with a message when git cannot answer (no origin/main, no tags, not a repository) or the markers
are malformed: nothing is written then.

``python3 scripts/status_git.py ancestry <STATUS_1_0.md> [--write]`` rewrites the block between the markers
``<!-- GEN:git-ancestry -->`` and ``<!-- /GEN:git-ancestry -->`` (it adds them before the first ``## `` heading when
absent); without ``--write`` it prints the block. ``python3 scripts/status_git.py state [--branches N]`` prints the header.
Every command runs ``git`` in the current directory's repository; run it from a levain checkout.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

START, END = "<!-- GEN:git-ancestry -->", "<!-- /GEN:git-ancestry -->"
TAG = r"v\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.\-]+)?"
SHA = r"`rel ([0-9a-fA-F]{7,40})`"
# one or more `rel <sha>` (comma separated) followed by -> <tag>: the tag is claimed for each of them
PAIR = re.compile(r"((?:`rel [0-9a-fA-F]{7,40}`\s*,?\s*)+)->\s*(" + TAG + ")")
REL_ANY = re.compile(r"`rel [^`]*`")
ROW = re.compile(r"^\|\s*([^|]+?)\s*\|")
REF = "origin/main"
MAIN = REF                  # replaced by the commit id `origin/main` resolved to, once, by :func:`pin_main`
ROOT: str | None = None     # the repository to read; None = the current directory


class GitError(Exception):
    """git could not answer: not a repository, no origin/main, git missing. The tool stops; it never reads this as data."""


def git(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=ROOT)
    except OSError as exc:
        raise GitError(f"git could not run: {exc}") from exc


def pin_main() -> str:
    """Resolve ``origin/main`` to one commit id and use it for every question after, so a fetch running meanwhile cannot make
    one row's answer disagree with the next; raise :class:`GitError` when it cannot be resolved or the clone has no tags."""
    global MAIN
    got = git("rev-parse", "--verify", "--quiet", REF + "^{commit}")
    if got.returncode != 0 or not got.stdout.strip():
        raise GitError(f"{REF} does not resolve here (not a levain clone, or the remote is not called origin)")
    if not git("tag", "--list", "v*").stdout.split():
        raise GitError("no v* tags here (a shallow clone, or tags not fetched)")
    MAIN = got.stdout.strip()
    return MAIN


def resolves(sha: str) -> bool:
    return git("rev-parse", "--verify", "--quiet", sha + "^{commit}").returncode == 0


def first_tag(sha: str) -> str | None:
    # only tags main contains, in version order with a pre-release (-rc1) before its release
    got = git("-c", "versionsort.suffix=-", "tag", "--contains", sha, "--merged", MAIN, "--list", "v*", "--sort=version:refname")
    if got.returncode != 0:
        raise GitError(f"git tag --contains {sha} failed: {got.stderr.strip()}")
    out = got.stdout.split()
    return out[0] if out else None


def on_main(sha: str) -> bool:
    got = git("merge-base", "--is-ancestor", sha, MAIN)
    if got.returncode not in (0, 1):                     # 1 = not an ancestor; anything else is git failing
        raise GitError(f"git merge-base --is-ancestor {sha} failed: {got.stderr.strip()}")
    return got.returncode == 0


def pairs(text: str) -> list[tuple[str, str, str]]:
    """(row id, sha, claimed tag) for every rel pair in a table row, in file order."""
    out = []
    for line in text.splitlines():
        m = ROW.match(line)
        if not m or not line.startswith("|") or set(m.group(1)) <= set("-: "):
            continue
        for group, tag in PAIR.findall(line):
            for sha in re.findall(SHA, group):
                out.append((m.group(1), sha, tag))
    return out


def unparsed(text: str) -> list[tuple[str, str]]:
    """(row id, cell text) for every `rel ...` cell in a table row that :data:`PAIR` did not read, so a form the tool does
    not understand shows as a row instead of vanishing from the count."""
    out = []
    for line in text.splitlines():
        m = ROW.match(line)
        if not m or not line.startswith("|"):
            continue
        read = {f"`rel {sha}`" for group, _ in PAIR.findall(line) for sha in re.findall(SHA, group)}
        for cell in REL_ANY.findall(line):
            if cell not in read:
                out.append((m.group(1), cell))
    return out


def ancestry(text: str) -> str:
    rows = ["| row | sha | claimed | first tag containing it | on main | verdict |", "|---|---|---|---|---|---|"]
    drift = 0
    for row, sha, claimed in pairs(text):
        if not resolves(sha):
            rows.append(f"| {row} | `{sha}` | {claimed} | (sha not in this clone) | | UNRESOLVED |")
            drift += 1
            continue
        tag, main = first_tag(sha), on_main(sha)
        ok = tag == claimed and main
        drift += 0 if ok else 1
        rows.append(f"| {row} | `{sha}` | {claimed} | {tag or '(no tag)'} | {'yes' if main else 'NO'} | {'ok' if ok else 'DRIFT'} |")
    pair_rows = len(rows) - 2
    for row, cell in unparsed(text):
        rows.append(f"| {row} | {cell} | | | | UNPARSED |")
        drift += 1
    head = (f"Generated by `scripts/status_git.py ancestry` against `{REF}` ({MAIN[:8]}); do not edit. "
            f"{pair_rows} pairs, {drift} not ok.")
    return "\n".join([START, head, "", *rows, END])


def splice(text: str, block: str) -> str:
    """``text`` with the generated block replaced, or added before the first ``## `` heading. Markers count only as whole
    lines; exactly one start before exactly one end, or neither, else :class:`ValueError` (nothing is guessed)."""
    starts = [m.start() for m in re.finditer(r"^" + re.escape(START) + r"[ \t]*$", text, re.M)]
    ends = [m.end() for m in re.finditer(r"^" + re.escape(END) + r"[ \t]*$", text, re.M)]
    if not starts and not ends:
        m = re.search(r"^## ", text, re.M)
        at = m.start() if m else len(text)
        return text[:at] + block + "\n\n" + text[at:]
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        raise ValueError(f"the status file has {len(starts)} start and {len(ends)} end markers (or them out of order): fix by hand")
    return text[:starts[0]] + block + text[ends[0]:]


def write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def state(branches: int) -> str:
    tip = git("log", "-1", "--format=%h %ad %s", "--date=format:%m-%d %H:%M", MAIN).stdout.strip()
    tag = git("describe", "--tags", "--abbrev=0", "--match", "v*", MAIN).stdout.strip()
    if not tip:
        raise GitError("git log could not read the tip of " + REF)
    since = git("rev-list", "--count", f"{tag}..{MAIN}").stdout.strip() if tag else "?"
    out = [f"- main: `{tip}`", f"- newest tag: {tag or '(none)'}, {since} commits on main since it"]
    refs = git("for-each-ref", "refs/remotes/origin/seat", "--sort=-committerdate", f"--count={branches}",
               "--format=%(refname:short)%1f%(objectname:short)%1f%(committerdate:format:%m-%d %H:%M)").stdout.splitlines()
    out.append(f"- newest {len(refs)} seat branches (commits main lacks, merged?):")
    for line in refs:
        ref, sha, when = line.split("\x1f")
        ahead = git("rev-list", "--count", f"{MAIN}..{ref}").stdout.strip()
        out.append(f"  - `{ref}` {sha} ({when}): {ahead} ahead, {'merged' if on_main(ref) else 'NOT merged'}")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ancestry")
    a.add_argument("status_file")
    a.add_argument("--write", action="store_true")
    s = sub.add_parser("state")
    s.add_argument("--branches", type=int, default=12)
    args = ap.parse_args(argv)
    try:
        pin_main()
        if args.cmd == "state":
            print(state(args.branches))
            return 0
        path = Path(args.status_file)
        text = path.read_text(encoding="utf-8")            # one snapshot: what is checked is what is spliced
        block = ancestry(text)
        if args.write:
            write_atomic(path, splice(text, block))
        else:
            print(block)
    except (GitError, ValueError, OSError) as exc:
        print(f"status_git: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
