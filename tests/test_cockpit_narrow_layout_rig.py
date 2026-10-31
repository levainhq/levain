"""OS-3 rig, shared by the narrow-layout tests: a cockpit that holds every phone tab's content (two waiting consent requests, a
standing grant, a Signer so the drop card draws, a notification record), and a driver that can present the page as a phone does.
A phone's pointer is coarse and it cannot hover; the shared driver (``tests/_page_drive.mjs``) sets a device size but no touch,
so this module runs a copy of it with one more step, ``{"touch": true}``, that turns touch emulation on (and so the page's
``pointer: coarse`` / ``hover: none`` rules). It holds no test of its own."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from levain.consent.grants import create_grant
from levain.cockpit.keys import fingerprint_of
from levain import authority as auth
import subprocess

from tests.test_cockpit_consent_rows import OPEN_DECISIONS
from tests.test_cockpit_consent_sheet_page import _ConsentRig
import json
from tests._page_drive_run import run as run_driver
from tests.test_cockpit_flip_parity import CHROME, DRIVE, NODE, _drive, needs_browser  # noqa: F401
from tests.test_cockpit_sign_card import TOK, needs_ssh

NEEDS = "document.querySelector('#ck-needs:not([hidden]) .needs-row')"
SETTLE = {"eval": "new Promise(r => setTimeout(r, 1200)).then(() => 1)"}
PHONE = {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}
DESKTOP = {"emulate": {"w": 1440, "h": 900, "dpr": 1}}


class FullRig(_ConsentRig):
    """The consent rig plus one standing grant (signed by the rig's laptop key) and a second waiting request."""

    def __init__(self, tmp_path: Path, monkeypatch: Any) -> None:
        super().__init__(tmp_path, monkeypatch)
        laptop = tmp_path / "laptop"
        lfp = fingerprint_of(" ".join((tmp_path / "laptop.pub").read_text().split()[:2]))
        ws = tmp_path / "proj"
        ws.mkdir()

        def sign(m: bytes) -> str:
            return subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(laptop), "-n", auth.NAMESPACE], input=m,
                                  capture_output=True, check=True).stdout.decode()

        create_grant(tmp_path / "ck", agent="claude_code", tools=["Write"], workspace=ws, postures=["default"],
                     expires=timedelta(days=1), signer_fp=lfp, sign=sign, clock=lambda: datetime.now(timezone.utc),
                     grant_id="cg-0000000000aa")
        self.second = self.file("T3")


def drive_touch(tmp_path: Path, url: str, steps: list) -> dict:
    """`_drive` over a copy of the driver that also answers ``{"touch": true}`` (touch emulation: a coarse pointer, no hover)."""
    marker = "  } else if (s.emulate !== undefined) {"
    text = DRIVE.read_text()
    assert marker in text, "the shared driver's step chain moved: the touch step needs a new anchor"
    # `{"init": js}`: the script runs in every document the page loads from here on, and the page is loaded again so that its first
    # render, its first reads included, already runs under it (a script set after the page started cannot reach what the first
    # render cached). The wait is for the new document's own sign, which the old one cannot show.
    init = ('  } else if (s.init !== undefined) {\n    await send("Page.addScriptToEvaluateOnNewDocument", { source: "window.__levainInit = 1;" + s.init });\n'
            '    await send("Page.reload", {});\n'
            '    for (const end = Date.now() + scaled(15000); Date.now() < end;) { await sleep(200); if (await evaluate("window.__levainInit === 1 && document.readyState") === "complete") break; }\n')
    patched = text.replace(marker, '  } else if (s.touch !== undefined) {\n    await send("Emulation.setTouchEmulationEnabled", { enabled: !!s.touch, maxTouchPoints: 5 });\n'
                           '    await sleep(300);\n' + init + marker, 1)
    d = tmp_path / "touch_driver"
    d.mkdir()
    (d / "_page_drive.mjs").write_text(patched)
    for sibling in DRIVE.parent.iterdir():   # everything the driver may read beside itself (its flags file, any module it imports)
        if sibling.is_file() and sibling.suffix in (".json", ".mjs", ".js") and sibling.name != DRIVE.name:
            (d / sibling.name).write_text(sibling.read_text())
    f = d / "steps.json"
    f.write_text(json.dumps(steps))
    res = run_driver([NODE, str(d / "_page_drive.mjs"), CHROME, url, str(f)], timeout=300)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


PHONE_TOUCH = [PHONE, {"touch": True}]

