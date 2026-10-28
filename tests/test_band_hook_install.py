"""Q17-ALL S1: `levain init` lays the band's calls into the Claude Code hooks and `levain update` refreshes them,
idempotently, without touching the wiring."""
from __future__ import annotations

import hashlib
import json

import pytest

from levain import host_guard as hg
from levain import install as inst
from levain.install import refresh_adapter
from tests.test_repin_on_install import make_install  # noqa: F401  (fixture)
from tests.test_subfolder_hooks import cc_local_layout  # noqa: E402,F401  (fixture)

pytestmark = pytest.mark.usefixtures("cc_local_layout")

SCRIPTS = ("user_prompt_submit.py", "session_start.py")


def _hooks(install):
    return install / "activation" / "hooks"


def test_init_installs_the_band_calls_and_update_is_idempotent(make_install):
    cc = make_install("cc")
    for name in SCRIPTS:
        assert "levain.band_view" in (_hooks(cc) / name).read_text(encoding="utf-8")
    settings = cc / ".claude" / "settings.local.json"
    before = {n: (_hooks(cc) / n).read_bytes() for n in SCRIPTS} | {"settings": settings.read_bytes()}
    for _ in range(2):
        r = refresh_adapter(cc, apply=True, emit=lambda _l: None)
        assert r.refreshed == [] and r.review == [], r
    after = {n: (_hooks(cc) / n).read_bytes() for n in SCRIPTS} | {"settings": settings.read_bytes()}
    assert after == before
    ups = json.loads(settings.read_text())["hooks"]["UserPromptSubmit"]
    assert sum("user_prompt_submit.py" in h["command"] for g in ups for h in g["hooks"]) == 1


def test_update_refreshes_an_install_that_predates_the_band(make_install):
    """An install made by a levain before the band: the scripts as levain last wrote them, without the call."""
    cc = make_install("cc")
    act = inst.activation_receipt_path(cc)
    receipt = json.loads(act.read_text())
    for name in SCRIPTS:
        p = _hooks(cc) / name
        old = p.read_text(encoding="utf-8").replace("levain.band_view", "levain.gone")
        p.write_text(old, encoding="utf-8")
        sha = hashlib.sha256(old.encode("utf-8")).hexdigest()
        receipt["files"][f"hooks/{name}"] = {"installed": sha, "source": sha}
    act.write_text(json.dumps(receipt))
    hg.repin_agent_hooks(cc)
    r = refresh_adapter(cc, apply=True, emit=lambda _l: None)
    assert all(f"activation/hooks/{n}" in r.refreshed for n in SCRIPTS), r
    assert r.review == [], r
    for name in SCRIPTS:
        assert "levain.band_view" in (_hooks(cc) / name).read_text(encoding="utf-8")
    again = refresh_adapter(cc, apply=True, emit=lambda _l: None)
    assert again.refreshed == [] and again.review == [], again
