"""`levain update` brings the base verbatim seed files to this release.

Reproduced through the CLI before this existed: an install made by levain 0.5.4 from
PyPI, then this branch's `levain update`, refreshed CLAUDE.md but left seed/memory.md at
0.5.4's bytes, because the refresh skipped seeds and the pack reconcile covers only
`--pack` layers. These build a REAL install with `run_init`, because the refresh decides
from the real receipt and the real template bytes.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import levain.install as install_mod
from levain.install import ADAPTER_RECEIPT_REL, PENDING_REL, _base_seed_files, refresh_adapter
from levain.packs import SeedEntry
from tests.test_update_refresh import make_install  # noqa: F401  (fixture)

BASE_SEEDS = ("seed/memory.md", "seed/partnership.md", "seed/spore_instructions.md")
OLD = b"# Memory\n\nan earlier release's seed text\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _receipt(install: Path) -> dict[str, str]:
    return json.loads(install.joinpath(*ADAPTER_RECEIPT_REL).read_text())["files"]


def _write_receipt(install: Path, files: dict[str, str]) -> None:
    install.joinpath(*ADAPTER_RECEIPT_REL).write_text(
        json.dumps({"schema": 1, "files": files}))


def _refresh(install: Path, **kw):
    lines: list[str] = []
    return refresh_adapter(install, apply=True, emit=lines.append, **kw), "\n".join(lines)


def _as_pre_receipt_install(install: Path, seed_bytes: bytes) -> bytes:
    """Make ``install`` look like one made by a release that recorded no seed hashes,
    with ``seed_bytes`` as its memory.md. Returns this release's memory.md bytes."""
    memory = install / "seed" / "memory.md"
    current = memory.read_bytes()
    memory.write_bytes(seed_bytes)
    _write_receipt(install, {k: v for k, v in _receipt(install).items()
                             if not k.startswith("seed/")})
    return current


def test_init_records_every_base_verbatim_seed(make_install):
    install = make_install()
    receipt = _receipt(install)
    for key in BASE_SEEDS:
        assert receipt[key] == _sha(install.joinpath(key).read_bytes())
    assert not any(k in receipt for k in ("seed/world.md", "seed/origin.md",
                                          "seed/continuity.md", "seed/README.md"))
    r, out = _refresh(install)
    assert not r.refreshed and not r.review and out == ""


def test_an_unedited_seed_from_an_earlier_release_is_replaced_and_backed_up(
        make_install, monkeypatch):
    install = make_install()
    monkeypatch.setitem(install_mod._RELEASED_BASE_SEED_SHA256, "seed/memory.md",
                        frozenset({_sha(OLD)}))
    current = _as_pre_receipt_install(install, OLD)
    r, out = _refresh(install)
    assert "seed/memory.md" in r.refreshed and not r.review
    assert (install / "seed" / "memory.md").read_bytes() == current
    backups = list((install / ".levain" / "backups" / "seed").iterdir())
    assert [b.read_bytes() for b in backups] == [OLD]
    assert not list((install / "seed").glob("*.bak*"))
    assert _receipt(install)["seed/memory.md"] == _sha(current)
    r2, out2 = _refresh(install)
    assert not r2.refreshed and not r2.review and out2 == ""


def test_an_edited_seed_without_a_record_is_kept_staged_and_listed_once(make_install):
    install = make_install()
    edited = b"# Memory\n\nmy own rules\n"
    current = _as_pre_receipt_install(install, edited)
    r, out = _refresh(install)
    assert r.review == ["seed/memory.md"] and "seed/memory.md" not in r.refreshed
    assert (install / "seed" / "memory.md").read_bytes() == edited
    assert install.joinpath(*PENDING_REL, "seed", "memory.md").read_bytes() == current
    r2, out2 = _refresh(install)
    assert not r2.review and "seed/memory.md" not in out2


def test_a_recorded_unedited_seed_is_refreshed_without_a_backup(make_install):
    install = make_install()
    memory = install / "seed" / "memory.md"
    current = memory.read_bytes()
    memory.write_bytes(OLD)
    _write_receipt(install, {**_receipt(install), "seed/memory.md": _sha(OLD)})
    r, _out = _refresh(install)
    assert "seed/memory.md" in r.refreshed and not r.review
    assert memory.read_bytes() == current
    assert not (install / ".levain" / "backups" / "seed").exists()


def test_a_recorded_seed_edited_after_install_is_staged_when_the_release_moved(make_install):
    install = make_install()
    memory = install / "seed" / "memory.md"
    memory.write_bytes(b"edited\n")
    _write_receipt(install, {**_receipt(install), "seed/memory.md": _sha(OLD)})
    r, _out = _refresh(install)
    assert r.review == ["seed/memory.md"]
    assert memory.read_bytes() == b"edited\n"


def test_seeds_are_held_while_the_pack_layer_is_unsettled(make_install):
    install = make_install()
    memory = install / "seed" / "memory.md"
    memory.write_bytes(OLD)
    _write_receipt(install, {**_receipt(install), "seed/memory.md": _sha(OLD)})
    r, _out = _refresh(install, carrier=False)
    assert "seed/memory.md" not in r.refreshed
    assert memory.read_bytes() == OLD


def test_only_the_base_layers_verbatim_seeds_are_refreshed(tmp_path):
    base = tmp_path / "templates"
    (base / "seed").mkdir(parents=True)
    pack = tmp_path / "pack" / "seed"
    pack.mkdir(parents=True)
    for p in (base / "seed" / "memory.md", base / "seed" / "world.md",
              pack / "partnership.md"):
        p.write_text(p.name)
    entries = [SeedEntry("memory.md", base / "seed" / "memory.md", "verbatim"),
               SeedEntry("world.md", base / "seed" / "world.md", "render"),
               SeedEntry("partnership.md", pack / "partnership.md", "verbatim")]
    install = tmp_path / "install"
    assert _base_seed_files(install, base, entries) == {
        install / "seed" / "memory.md": "memory.md"}


def test_the_released_seed_table_matches_the_release_tags():
    """Re-derives _RELEASED_BASE_SEED_SHA256 from git, where the tags are present."""
    repo = Path(__file__).resolve().parents[1]
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    tags = subprocess.run(["git", "-C", str(repo), "tag", "-l", "v0.*"],
                          capture_output=True, text=True).stdout.split()

    def released(tag: str) -> bool:
        major, minor, patch = (int(x) for x in tag[1:].split(".")[:3])
        return (major, minor, patch) <= (0, 5, 4)

    tags = [t for t in tags if released(t)]
    if "v0.5.4" not in tags:
        pytest.skip("release tags not available in this checkout")
    derived: dict[str, set[str]] = {}
    for key in BASE_SEEDS:
        for tag in tags:
            r = subprocess.run(["git", "-C", str(repo), "show",
                                f"{tag}:levain/templates/{key}"], capture_output=True)
            if r.returncode == 0:
                derived.setdefault(key, set()).add(_sha(r.stdout))
    assert {k: set(v) for k, v in install_mod._RELEASED_BASE_SEED_SHA256.items()} == derived
