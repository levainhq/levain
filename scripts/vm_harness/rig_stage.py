"""Host side of the 1105-17 VM rig's staging: the rig ships NO copy of the fake vendor's source.

``tests/vm_fakes/fire_fakeclaude.c`` in the tree under test is the single source. The rig dir (project_memory/1105-17/
vmend/rig, untracked) used to keep its own ``fire_fakeclaude.c``; it went stale (the 11-07 driver lane copied main's file
over it by hand). :func:`stage_rig_files` therefore never copies a ``fire_fakeclaude.c`` from the rig dir: it writes the
canonical bytes (``git show <rev>:tests/vm_fakes/fire_fakeclaude.c``) and ``rig_seed.py`` (the guest-side identity-seed
writer, this directory's sibling), and :func:`check_staged` refuses a staging dir whose C file differs from the canonical."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

#: What the rev's archive carries. ``pyproject.toml`` rides beside ``levain/`` because ``levain doctor`` reads the
#: anneal-memory range from the file next to the package (manifest's repo-pyproject lookup); without it a vend's doctor
#: shows a WARN where the real requirement would FAIL on the image's anneal. Staging the file leaves doctor unchanged on
#: a real install, which is why the rig carries it rather than doctor learning a second source.
ARCHIVE_PATHS = ("levain", "pyproject.toml")

#: Where setup's vendor copier puts the vendors, and where the hands account looks (its PATH names /usr/local/bin first,
#: and ``hands login`` refuses by name when claude is not there).
VENDOR_BIN = "/usr/local/lib/levain-vendor/bin"
HANDS_BIN = "/usr/local/bin"
VENDOR_NAMES = ("claude", "codex")

FAKE_NAME = "fire_fakeclaude.c"
FAKE_REPO_PATH = "tests/vm_fakes/fire_fakeclaude.c"
SEED_NAME = "rig_seed.py"
SEED_SOURCE = Path(__file__).resolve().parent / SEED_NAME


def canonical_fakeclaude(repo: Path, rev: str) -> bytes:
    """The fake vendor's source as ``rev`` of ``repo`` has it (the tree being staged)."""
    r = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{FAKE_REPO_PATH}"], capture_output=True, timeout=60)
    if r.returncode or not r.stdout:
        raise RuntimeError(f"no {FAKE_REPO_PATH} at {rev}: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


def stage_rig_files(rig_dir: Path, dest: Path, canonical: bytes) -> list[Path]:
    """Copy the rig's files into ``dest`` (not ``__*``, never a stale ``fire_fakeclaude.c``), then write the canonical C
    source and ``rig_seed.py``. Returns the staged paths, sorted; verified by :func:`check_staged` before returning."""
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.iterdir():     # a reused dir keeps nothing from an earlier revision: only this call's files ship
        if not old.name.startswith("__") and (old.is_symlink() or old.is_file()):
            old.unlink()
    for p in sorted(Path(rig_dir).iterdir()):
        if p.name.startswith("__") or p.name in (FAKE_NAME, SEED_NAME) or not p.is_file():
            continue
        shutil.copy2(p, dest / p.name)
    (dest / FAKE_NAME).write_bytes(canonical)
    shutil.copy2(SEED_SOURCE, dest / SEED_NAME)
    check_staged(dest, canonical)
    return sorted(p for p in dest.iterdir() if p.is_file() and not p.name.startswith("__"))


def check_staged(dest: Path, canonical: bytes) -> None:
    """Raise when the staged fake-vendor source is missing or is not byte-identical to the canonical, or the seed writer is
    missing."""
    got = Path(dest) / FAKE_NAME
    if not got.is_file():
        raise RuntimeError(f"staged rig has no {FAKE_NAME}")
    if hashlib.sha256(got.read_bytes()).digest() != hashlib.sha256(canonical).digest():
        raise RuntimeError(f"staged {FAKE_NAME} differs from the canonical {FAKE_REPO_PATH}")
    if not (Path(dest) / SEED_NAME).is_file():
        raise RuntimeError(f"staged rig has no {SEED_NAME}")


def archive_argv(git: str, repo: Path, rev: str, out: Path) -> list[str]:
    """The ``git archive`` command that tars ``rev``'s :data:`ARCHIVE_PATHS` for the guest."""
    # ``-C`` changes git's directory before it opens ``-o``, so a relative ``out`` would land inside ``repo``: absolute it
    return [git, "-C", str(repo), "archive", "--format=tar.gz", "-o", str(Path(out).resolve()), rev, *ARCHIVE_PATHS]


def vendor_link_argvs(vendor_bin: str = VENDOR_BIN, hands_bin: str = HANDS_BIN) -> list[list[str]]:
    """Commands (run as root in the guest) that make ``hands_bin/<name>`` a symlink to ``vendor_bin/<name>`` for each of
    :data:`VENDOR_NAMES`. A link, never a copy, so the one vendored binary is what the hands account runs; ``-sfn``
    replaces an earlier link and may point at a target that setup has not populated yet."""
    return [["/bin/mkdir", "-p", hands_bin]] + [
        ["/bin/ln", "-sfn", f"{vendor_bin}/{n}", f"{hands_bin}/{n}"] for n in VENDOR_NAMES]
