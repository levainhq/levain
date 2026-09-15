"""C6 amendment (Phill, 2026-09-15): a CORRUPT or EMPTY install receipt is not a
pre-receipt install and must not downgrade an edited hook to pending. See
levain/doctor.py `_check_hook_freshness` and flow episode flow-20260915-074114-4e7ca53aaca6
for the ruling and its rationale.

Each test names the mutation it exists to kill.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import levain.doctor as doctor
import levain.install as inst


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _edited_hook_install(tmp_path: Path, receipt_mode: str) -> Path:
    """A hooked install whose `_levain_hook.py` is edited AFTER the receipt (if any) is
    written, under one of five receipt states:
      "intact"      -- a full, correct receipt naming every hook as init wrote it
      "corrupt"     -- unparseable JSON at the receipt path
      "empty"       -- valid JSON, valid schema, but an empty `files` map
      "deleted"     -- no receipt file at all (absent)
      "ok_no_entry" -- a valid, non-empty receipt that omits this one hook
    """
    install = tmp_path / "ent"
    hooks = install / "activation" / "hooks"
    hooks.mkdir(parents=True)
    with inst._templates_root() as tr:
        for f in (inst._base_activation_root("claude-code", tr) / "hooks").glob("*.py"):
            (hooks / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    (install / "CLAUDE.md").write_text("# tag\n", encoding="utf-8")
    hook = hooks / "_levain_hook.py"
    hook.write_text(hook.read_text(encoding="utf-8") + "\n# older release\n", encoding="utf-8")

    files = {
        f"hooks/{p.name}": {"installed": _sha(p), "source": _sha(p)} for p in hooks.glob("*.py")
    }
    receipt_path = inst.activation_receipt_path(install)
    if receipt_mode == "intact":
        inst._write_activation_receipt(install, files)
    elif receipt_mode == "ok_no_entry":
        del files["hooks/_levain_hook.py"]
        inst._write_activation_receipt(install, files)
    elif receipt_mode == "corrupt":
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text("{", encoding="utf-8")
    elif receipt_mode == "empty":
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            '{"schema": %d, "files": {}}' % inst.ACTIVATION_RECEIPT_SCHEMA, encoding="utf-8"
        )
    elif receipt_mode == "deleted":
        pass
    else:
        raise ValueError(receipt_mode)

    # The edit lands AFTER the receipt is written, so every mode above records the
    # pre-edit hook (or nothing) -- this is what makes the hook genuinely edited.
    hook.write_text(hook.read_text(encoding="utf-8") + "import os\n", encoding="utf-8")
    return install


@pytest.mark.parametrize(
    "receipt_mode, expect_ok, expect_pending, text_fragment",
    [
        ("intact", False, False, "changed since `levain init` wrote them"),
        ("corrupt", False, False, "is unreadable (corrupt)"),
        ("empty", False, False, "is unreadable (empty)"),
        ("deleted", False, True, "no install receipt covers them"),
        ("ok_no_entry", False, True, "no install receipt covers them"),
    ],
)
def test_hook_freshness_by_receipt_status(
    tmp_path: Path, receipt_mode: str, expect_ok: bool, expect_pending: bool, text_fragment: str
):
    """MUTATION: collapse "corrupt"/"empty" back into the "no receipt entry" (pending)
    branch -- corrupt/empty would then read `text_fragment` as "no install receipt
    covers them" and exit 6 instead of 1, silently downgrading a tampered hook."""
    install = _edited_hook_install(tmp_path, receipt_mode)
    r = doctor._check_hook_freshness(install)[0]
    assert r.ok is expect_ok
    assert r.upgrade_pending is expect_pending
    assert text_fragment in r.detail, r.detail


def test_corrupt_and_empty_name_the_receipt_path_and_remedy(tmp_path: Path):
    """The C6 amendment requires the check's own text to name the unreadable receipt
    and the `init --force` remedy -- not just say "differ from the package"."""
    install = _edited_hook_install(tmp_path, "corrupt")
    r = doctor._check_hook_freshness(install)[0]
    assert str(inst.activation_receipt_path(install)) in r.detail
    assert r.hint is not None and "levain init --force" in r.hint
