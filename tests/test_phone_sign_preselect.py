"""Phone preselect (project_memory 1025-18/preselect/DESIGN.md §2): a sign link may carry ``choice=<key>``, the option
the operator tapped on his phone. The page opens on that card with a "Sign (<key>) with your screen lock" button (headless Chrome is not an iPhone; an iPhone says Face ID) and the exact
line it signs; the tap runs the same challenge as the option's own button, so the bytes signed are still built by the
server from the request file. A key the card does not offer draws nothing and says so. A linked request no longer open
says where it stands on the first listing (``GET /sign/<id>/state``).

The page half runs in headless Chrome against a real phone port on ``http://localhost:<port>``: Chrome's virtual
authenticator holds the passkey the Signer's keys name, and the ``.sig`` it writes is checked with stock
``ssh-keygen -Y verify``."""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from levain.consent.sign import deep_link, read_request

NS = "flow-needs-phill"
TOK = "preselect-phone-token"
RID = "np-pre00001-c9f2a1"
MESSAGE = ("flow needs-phill answer\nitem: np-pre00001\nasked: ship the sweep fix tonight or hold it?\n"
           "recommended: (b) hold for the morning\nnonce: c9f2a1")
OPTIONS = [{"key": "a", "text": "ship it tonight"}, {"key": "b", "text": "hold for the morning"}]
PHONE = {"emulate": {"w": 390, "h": 844, "dpr": 3, "mobile": True}}
needs_ssh = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
SHOTS = os.environ.get("LEVAIN_PRESELECT_SHOTS", "")      # a directory to keep the drive's screenshots in


def _file(qdir: Path, rid: str, *, options: "list[dict] | None" = OPTIONS, message: str = MESSAGE,
          expires_in: timedelta = timedelta(minutes=10), ns: str = NS, **extra: Any) -> None:
    d = qdir / "sign"
    d.mkdir(parents=True, exist_ok=True)
    body: dict[str, Any] = {"format": "levain-sign-request/1", "id": rid, "namespace": ns, "message": message,
                            "title": "needs phill " + rid[:11], "requester": "flow desk",
                            "expires_at": (datetime.now(timezone.utc) + expires_in).isoformat()}
    if options is not None:
        body.update(format="levain-sign-choice/1", options=options, recommended="b", **extra)
    (d / f".{rid}.tmp").write_text(json.dumps(body))
    os.replace(d / f".{rid}.tmp", d / f"{rid}.json")






def _shot(tmp_path: Path, name: str) -> dict:
    return {"shot": str(Path(SHOTS) / name if SHOTS else tmp_path / name)}     # the viewport: what he sees on arrival


# the linked card's picked block, read off the page: what it says and what it would sign
_PICKED = ("(() => { const c = document.querySelector('.req.sign.linked'); if (!c) return null; "
           "const p = c.querySelector('.picked'); const m = c.querySelector('.pick-miss'); "
           "const t = c.querySelector('.pick-top'); "
           "return { top: t ? t.textContent : null, topAbove: !!t && !!(t.compareDocumentPosition("
           "c.querySelector('pre.display')) & 4), picked: p ? { button: p.querySelector('button').textContent, "
           "line: p.querySelector('.line').textContent, words: p.querySelector('.why').textContent } : null, "
           "miss: m ? m.textContent : null, missTop: !!m && m.previousElementSibling === c.querySelector('.req-head'), "
           "options: c.querySelectorAll('.actions .option').length, "
           "shownAbove: !!p && !!(c.querySelector('pre.display').compareDocumentPosition(p) & 4), "
           "first: document.getElementById('requests').firstChild === c }; })()")


def _legacy_link(page: str, rid: str, token: str) -> str:
    """The phone page's own link shape (phone.js reads it until OS-6 deletes the page): ``deep_link`` builds the
    cockpit sheet's now, with no token, so these page tests build the legacy one by hand."""
    from urllib.parse import quote

    return page.split("#", 1)[0] + f"#phone_token={quote(token, safe='')}&sign={rid}"


def _signed(qdir: Path, rid: str) -> "dict | None":
    f = qdir / "sign" / f"{rid}.signed.json"
    return json.loads(f.read_text()) if f.exists() else None


# --- the link --------------------------------------------------------------------------------------------------------


def test_the_deep_link_carries_his_pick_and_its_version_after_the_sheet_and_only_an_option_key() -> None:
    sha = "0123456789abcdef" + "f" * 48
    # the version alone is the base link an app adds `&choice=<key>` to; with a choice the link is that base plus it
    base = deep_link("https://h.ts.net:8443/", RID, version=sha)
    assert base == f"https://h.ts.net:8443/sign/{RID}#v=0123456789abcdef"
    assert deep_link("https://h.ts.net:8443/", RID, choice="b", version=sha) == base + "&choice=b"
    assert deep_link("https://h.ts.net:8443/", RID, choice="b", version=sha[:16]) == base + "&choice=b"
    assert deep_link("https://h.ts.net:8443/", RID) == f"https://h.ts.net:8443/sign/{RID}"
    for bad in ("*", "", "B", "toolongkey", "a&sign=x", "a b"):
        with pytest.raises(ValueError):
            deep_link("https://h.ts.net/", RID, choice=bad, version=sha)
    for bad_v in ("", "0123", sha.upper(), sha[:17], "stat:" + sha, sha + "&x=1"):
        with pytest.raises(ValueError):
            deep_link("https://h.ts.net/", RID, choice="b", version=bad_v)
    with pytest.raises(ValueError):                 # a pick holds only on a version: never a choice without one
        deep_link("https://h.ts.net/", RID, choice="b")


# --- the state route -------------------------------------------------------------------------------------------------






# --- the page --------------------------------------------------------------------------------------------------------










# --- L3 r1 (project_memory 1025-18/preselect/RULINGS_R1.md) -----------------------------------------------------------


def _v(qdir: Path, rid: str) -> str:
    """The version a link carries for ``rid`` as filed now: the first 16 hex of its listing ``sha256``."""
    return read_request(qdir, rid).sha256[:16]




_SLEEP = {"until": "false", "may_timeout": True}        # with "ms": the drive waits that long (a poll is ten seconds)
_ALL_PICKS = ("(() => ({ picked: document.querySelectorAll('.picked').length, "
              "miss: document.querySelectorAll('.pick-miss').length, top: document.querySelectorAll('.pick-top').length, "
              "linked: document.querySelectorAll('.req.sign.linked').length, "
              "cards: document.querySelectorAll('.req.sign').length }))()")


















