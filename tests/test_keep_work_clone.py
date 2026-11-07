"""The "keep the work" clone command for a retired entity's repository (rows 121/122, Linux VM L2 round).

On git 2.43 (Ubuntu 24.04) a local clone's upload-pack child checks the repository's ``.git``, so a
``safe.directory`` entry for the repository alone fails with "detected dubious ownership"; git 2.56 checks the
repository. The command names both. The CI isolation e2e runs the command the operator is told; these tests pin
the two to one flag set."""
from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

from levain.firing.hands import keep_work_clone_command

E2E = Path(__file__).parent / "ci" / "isolation_e2e.sh"


def _flags(argv: list[str]) -> list[str]:
    """The ``-c key=value`` pairs of a git argv, in order."""
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]


def test_command_names_the_repository_and_its_dot_git():
    argv = shlex.split(keep_work_clone_command("/ws/repo-h", "/out"))
    assert _flags(argv) == ["safe.directory=/ws/repo-h", "safe.directory=/ws/repo-h/.git", "core.hooksPath=/dev/null"]
    assert argv[-4:] == ["clone", "--no-local", "/ws/repo-h", "/out"]


def test_e2e_runs_the_command_the_operator_is_told():
    line = next(l for l in E2E.read_text().splitlines() if "operator can clone it to keep the work" in l)
    run = re.search(r"opgit (.*?) clone ", line).group(1)
    e2e_flags = _flags(["git"] + shlex.split(run.replace("'$WS/repo-h'", "REPO").replace("'$WS/repo-h/.git'", "REPO/.git")
                                             .replace("$WS/repo-h", "REPO")))
    told = _flags(shlex.split(keep_work_clone_command("REPO", "OUT")))
    assert e2e_flags == told


def test_doctor_message_carries_the_command(tmp_path):
    from levain.firing import hands

    src = Path(hands.__file__).read_text()
    assert "keep_work_clone_command(first, '<destination>')" in src


def test_command_clones_an_owned_repository(tmp_path):
    repo = tmp_path / "repo-h"
    git = ["git", "-c", "user.name=x", "-c", "user.email=x@x"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(git + ["-C", str(repo), "commit", "-q", "--allow-empty", "-m", "a"], check=True)
    out = tmp_path / "kept"
    subprocess.run(shlex.split(keep_work_clone_command(str(repo), str(out))), check=True, capture_output=True)
    assert (out / ".git").is_dir()
